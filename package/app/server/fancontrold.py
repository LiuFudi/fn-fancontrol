#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of niufan, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
"""niufan daemon.

Reads a temperature source (CPU, GPU or disks), runs it through a per-fan
curve and drives the SuperIO PWM output accordingly.  A small HTTP server --
by default on a Unix socket for the fnOS gateway -- serves the web UI and a
JSON API.

Subcommands::

    fancontrold.py run          start the control loop and the HTTP server
    fancontrold.py init-config  write a default config for the detected fans
    fancontrold.py restore      hand every fan back to the BIOS
    fancontrold.py status       print the current status as JSON

The daemon needs root: it writes ``/sys/class/hwmon/*/pwm*`` and may load
kernel modules.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import signal
import socketserver
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fanconfig  # noqa: E402
import fanhardware  # noqa: E402

APP_NAME = "niufan"
#: Name shown to the operator in log lines and --help.  Purely cosmetic: the
#: identifier above stays ``niufan`` because it names the config directory, the
#: gateway path and the value reported by /api/ping.
APP_TITLE = "NiuFan"
#: What the app was called before 2.0.0.  Only used to pick up an existing
#: configuration on the first start after the rename.
LEGACY_APP_NAME = "fn-fancontrol"
# Must be kept in step with the ``version`` field of the package manifest:
# the app center does not export TRIM_APPVER to the daemon.
VERSION = "2.0.1"

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #


class Log:
    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.lock = threading.Lock()

    def __call__(self, message):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            self.stream.write("%s - %s\n" % (stamp, message))
            self.stream.flush()


# --------------------------------------------------------------------------- #
# controller
# --------------------------------------------------------------------------- #


class Controller:
    """Owns the fan state: it is the only place that writes PWM values."""

    def __init__(self, config_path, log):
        self.config_path = config_path
        self.log = log
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None

        self.channels = {}
        self.chip = None
        self.profile = None
        self.controllable = False
        self.paused = threading.Event()   # set while a probe owns the hardware
        self.config = None
        self.applied = {}       # channel -> "manual" | "auto"
        self.written = {}       # channel -> last duty written
        self.hold = {}          # channel -> (temp, duty) used for hysteresis
        self.fan_state = {}     # channel -> last evaluated state
        self.original = {}      # channel -> (enable, duty) before we touched it
        self.mode_at = {}       # channel -> when manual mode was last asserted
        self.probe_results = {} # channel -> latest calibration result
        self.hardware_changes = []  # what changed since the last calibration
        # keyed by source key: "cpu", "hdd", one "gpu:<id>" per GPU and one
        # "aux:<chip>:<tempN>" per extra sensor
        self.sources = {}
        self.disks = []
        self.gpus = []
        self.aux = []
        self.hdd_read_at = 0.0
        self.degraded = False
        self.updated = 0.0
        self.hardware_ready = False
        self.last_identity = None

    def record_identity(self, identity):
        """Remember who last came through the gateway, for the UI footer."""
        self.last_identity = {
            "username": identity.get("username"),
            "isadmin": identity.get("isadmin"),
            "at": time.time(),
        }

    # -- hardware ---------------------------------------------------------- #

    @property
    def gpu_keys(self):
        """Source keys for the GPUs present on this machine."""
        return [fanconfig.GPU_PREFIX + gpu.id for gpu in self.gpus]

    @property
    def aux_keys(self):
        """Source keys for the extra sensors present on this machine."""
        return [fanconfig.AUX_PREFIX + sensor.id for sensor in self.aux]

    def hardware_fingerprint(self):
        """The two things that can invalidate a calibration.

        The CPU (headers may be bound to it through PECI) and the set of fan
        headers themselves.  Disks and GPUs are deliberately left out: they come
        and go without the fans caring, and a calibration re-run over a USB
        enclosure plugged in for an afternoon would be noise, not safety.
        """
        with self.lock:
            channels = sorted(self.channels)
        model = fanhardware.cpu_model()
        return {
            "cpu": [model] if model else [],
            "channels": ["CH%d" % index for index in channels],
        }

    def refresh_hardware(self):
        """Re-scan hwmon; safe to call at any time."""
        loaded = fanhardware.ensure_modules(self.log)
        if loaded:
            self.log("loaded kernel module(s): %s" % ", ".join(loaded))
        path, chip, profile = fanhardware.find_controller()
        with self.lock:
            self.chip = chip
            self.profile = profile
            self.channels = {c.index: c for c in fanhardware.fan_channels(profile, path)}
            self.hardware_ready = bool(self.channels)
            self.controllable = bool(profile and profile.controllable)
            self.disks = fanhardware.list_disk_sensors()
            self.gpus = fanhardware.list_gpu_sensors()
            self.aux = fanhardware.list_aux_sensors()
        if self.aux:
            self.log("extra temperature sensor(s): %s"
                     % ", ".join(sensor.label for sensor in self.aux))
        if not self.hardware_ready:
            self.log("no fan controller with a PWM channel was found")
        elif not self.controllable:
            self.log("fan controller %s (%s) is monitor-only: %s"
                     % (chip, profile.key, profile.note))
        else:
            headers = ["%d%s" % (c.index, "" if c.present else "?")
                       for c in sorted(self.channels.values(), key=lambda x: x.index)]
            self.log("fan controller %s (%s), %d PWM header(s): %s"
                     % (chip, profile.key, len(self.channels), " ".join(headers)))

    # -- config ------------------------------------------------------------ #

    def _migrate_disk_selection(self, config):
        """Rewrite position-derived device names (``sda``) into stable disk ids.

        ``sda`` only means "whichever disk the kernel enumerated first", so a
        configuration that stores it silently starts following a *different*
        drive as soon as disks are re-cabled or the boot order changes.
        Existing selections are upgraded in place using the current mapping.
        """
        selected = (config.get("sources") or {}).get("hdd_devices") or []
        if not selected:
            return config
        by_device = {sensor.device: sensor.id for sensor in self.disks if sensor.device}
        known = {sensor.id for sensor in self.disks}
        migrated = []
        changed = False
        for item in selected:
            if item in known:
                migrated.append(item)
            elif item in by_device and by_device[item] != item:
                migrated.append(by_device[item])
                changed = True
            else:
                migrated.append(item)   # unknown disk: leave it alone
        if changed:
            config["sources"]["hdd_devices"] = migrated
            fanconfig.save(self.config_path, config)
            self.log("disk selection migrated from device names to stable ids")
            for old, new in zip(selected, migrated):
                if old != new:
                    self.log("  %s -> %s" % (old, new))
        return config

    def _migrate_legacy_config(self):
        """Pull the pre-rename configuration across, once.

        The app used to be called ``fn-fancontrol``.  Its config sits in a
        sibling directory under the same ``@appconf`` volume, so a user who had
        already tuned curves and run a calibration would otherwise meet the
        renamed app with an empty wizard.  Copy it over only when there is
        nothing here yet, and never look at it again.
        """
        if os.path.exists(self.config_path):
            return False
        config_dir = os.path.dirname(os.path.abspath(self.config_path))
        legacy = os.path.join(os.path.dirname(config_dir), LEGACY_APP_NAME,
                              os.path.basename(self.config_path))
        if not os.path.isfile(legacy):
            return False
        try:
            with open(legacy, "rb") as source:
                payload = source.read()
            os.makedirs(config_dir, exist_ok=True)
            with open(self.config_path, "wb") as target:
                target.write(payload)
        except OSError as exc:
            self.log("could not migrate the old configuration: %s" % exc)
            return False
        self.log("migrated the configuration from %s (the app used to be called "
                 "%s)" % (legacy, LEGACY_APP_NAME))
        return True

    def load_config(self, create_if_missing=True):
        channels = list(self.channels.values())
        self._migrate_legacy_config()

        # A config written before the detection wizard existed has no
        # setup_complete key.  Show the wizard once so those users get to pick
        # from every real channel too, then persist the decision.
        legacy = False
        try:
            with open(self.config_path, "r") as handle:
                legacy = "setup_complete" not in json.load(handle)
        except (OSError, ValueError):
            legacy = False

        config = fanconfig.load(self.config_path, channels, self.gpu_keys)
        if config is None:
            if not create_if_missing:
                return None
            config = fanconfig.build_default_config(channels, gpu_keys=self.gpu_keys)
            fanconfig.save(self.config_path, config)
            self.log("created default config at %s" % self.config_path)
        elif legacy:
            config["setup_complete"] = False
            fanconfig.save(self.config_path, config)
            self.log("config predates the detection wizard; showing it once")

        self._migrate_disk_selection(config)

        # Reload the calibrations that are already stored, so the detection
        # table is populated the moment the UI opens instead of looking empty
        # after every restart or upgrade.
        stored = {}
        probe = config.get("probe") or {}
        for item in probe.get("results") or []:
            try:
                entry = dict(item)
                entry["stored"] = True
                if probe.get("at"):
                    entry["at"] = int(probe["at"])
                stored[int(entry["channel"])] = entry
            except (KeyError, TypeError, ValueError):
                continue
        # Older configs only kept a calibration for the channels being managed;
        # fold those in so an upgrade does not blank the table either.
        for fan in config.get("fans", []):
            if fan["channel"] in stored:
                continue
            result = fanconfig.calibration_as_result(fan["channel"],
                                                     fan.get("calibration"))
            if result:
                result["stored"] = True
                stored[fan["channel"]] = result

        with self.lock:
            self.config = config
            self.probe_results = stored

        self._check_hardware_change(config)
        return config

    def _check_hardware_change(self, config):
        """Re-open the wizard when the machine is no longer the calibrated one."""
        current = self.hardware_fingerprint()
        stored = config.get("fingerprint")
        if not stored:
            # Nothing on record yet: the config predates this feature, or the
            # calibration was never finished.  Adopt the hardware as it is now
            # -- treating "no record" as "everything changed" would force a
            # pointless re-calibration on every upgrade.
            config["fingerprint"] = current
            try:
                fanconfig.save(self.config_path, config)
            except OSError as exc:
                self.log("could not record the hardware fingerprint: %s" % exc)
            self.hardware_changes = []
            return []
        changes = fanhardware.describe_hardware_changes(stored, current)
        self.hardware_changes = changes
        if not changes:
            return changes
        if config.get("setup_complete", True):
            config["setup_complete"] = False
            try:
                fanconfig.save(self.config_path, config)
            except OSError as exc:
                self.log("could not save the re-calibration flag: %s" % exc)
        self.log("hardware changed since the last calibration: %s"
                 % "; ".join(changes))
        return changes

    def apply_config(self, raw):
        """Validate, persist and immediately apply a new configuration."""
        channels = list(self.channels.values())
        config = fanconfig.normalise(raw, channels, self.gpu_keys)
        self._migrate_disk_selection(config)
        fanconfig.save(self.config_path, config)
        with self.lock:
            previously_managed = set(self.applied)
            managed = {fan["channel"] for fan in config["fans"]}
            self.config = config
            # force a rewrite on the next tick
            self.applied.clear()
            self.mode_at.clear()
            self.written.clear()
            self.hold.clear()
            self.fan_state = {
                index: state
                for index, state in self.fan_state.items()
                if index in managed
            }
            released = sorted(previously_managed - managed)

        # A channel this app used to drive but no longer manages must be put
        # back, otherwise it is stranded on the last duty we wrote to it.
        for index in released:
            self._release(index)

        self.log("configuration updated (%d fan(s))" % len(config["fans"]))
        return config

    # -- temperature sources ----------------------------------------------- #

    def _read_sources(self, force_disks=False):
        with self.lock:
            config = self.config or fanconfig.DEFAULT_CONFIG
            gpus = list(self.gpus)
            aux = list(self.aux)
        wanted = config["sources"]
        now = time.time()

        # CPU and GPU are read unconditionally, even when switched off: the UI
        # can then show "43C (not enabled)" rather than a bare dash, which is
        # the difference between "no sensor on this machine" and "you have not
        # turned it on".  The enabled flag only decides who may follow it.
        value, detail = fanhardware.read_cpu()
        self.sources["cpu"] = {"temperature": value, "detail": detail}

        for gpu in gpus:
            key = fanconfig.GPU_PREFIX + gpu.id
            self.sources[key] = {
                "temperature": gpu.temperature(),
                "detail": gpu.label or gpu.id,
            }

        # Extra sensors are likewise read whether or not they are switched on.
        for sensor in aux:
            self.sources[fanconfig.AUX_PREFIX + sensor.id] = {
                "temperature": sensor.temperature(),
                "detail": sensor.label or sensor.id,
            }

        if not wanted.get("hdd"):
            # Disks are different: reading them can spin a sleeping drive up,
            # so a switched-off disk source is genuinely not polled.
            self.sources["hdd"] = {"temperature": None, "detail": "未启用"}
            return

        # Disks change temperature slowly, so poll them far less often: this
        # keeps SATA disks from being woken up every few seconds.
        if force_disks or now - self.hdd_read_at >= config["hdd_interval"]:
            self.hdd_read_at = now
            selected = wanted.get("hdd_devices") or None
            self.disks = fanhardware.list_disk_sensors()
            value, detail = fanhardware.read_hdd(self.disks, selected)
            self.sources["hdd"] = {"temperature": value, "detail": detail}

    @staticmethod
    def _source_enabled(key, wanted, gpu_switches):
        if key.startswith(fanconfig.GPU_PREFIX):
            return bool(gpu_switches.get(key, False))
        if key.startswith(fanconfig.AUX_PREFIX):
            return bool((wanted.get("aux") or {}).get(key, False))
        return bool(wanted.get(key))

    def _source_state(self):
        """Latest readings merged with the configured on/off switches.

        ``enabled`` is taken from the *config*, not from the last poll, so a
        toggle is reflected the moment it is saved rather than up to one control
        interval later.
        """
        with self.lock:
            gpus = list(self.gpus)
            aux = list(self.aux)
            readings = {key: dict(info) for key, info in self.sources.items()}
            config = self.config or fanconfig.DEFAULT_CONFIG
        wanted = config["sources"]
        gpu_switches = wanted.get("gpus") or {}

        keys = (["cpu"] + [fanconfig.GPU_PREFIX + g.id for g in gpus] + ["hdd"]
                + [fanconfig.AUX_PREFIX + s.id for s in aux])
        state = {}
        for key in keys:
            info = readings.get(key) or {"temperature": None, "detail": None}
            info["enabled"] = self._source_enabled(key, wanted, gpu_switches)
            state[key] = info
        return state

    def _effective_temperature(self, keys):
        state = self._source_state()
        best = None
        for key in keys:
            info = state.get(key) or {}
            if not info.get("enabled", True):
                continue  # switched off: readable, but not a control input
            value = info.get("temperature")
            if value is None:
                continue
            best = value if best is None else max(best, value)
        return best

    # -- control ----------------------------------------------------------- #

    def _remember_original(self, channel):
        """Snapshot a channel's BIOS state before the first write to it.

        Needed so that removing a fan from the configuration can put the
        channel back exactly as the BIOS had it, instead of forcing auto mode
        onto a header the BIOS deliberately runs in manual mode.
        """
        if channel.index in self.original:
            return
        self.original[channel.index] = (channel.enable, channel.duty)

    #: Re-assert manual mode this often.  Some boards (ASUS in particular) take
    #: the SuperIO back periodically, which silently drops the fan to the BIOS
    #: curve while we keep believing we are in control.  One register write is
    #: cheap insurance against that.
    REASSERT_SECONDS = 30.0

    def _apply_duty(self, channel, duty):
        if not channel.controllable:
            return
        index = channel.index
        mode = self.applied.get(index)
        stale = time.time() - self.mode_at.get(index, 0.0) >= self.REASSERT_SECONDS
        if mode != "manual" or stale:
            if mode != "manual":
                self._remember_original(channel)
            # profile.manual, not a hard-coded 1: the value differs per driver
            fanhardware.write_text(channel.path_enable, channel.profile.manual)
            self.applied[index] = "manual"
            self.mode_at[index] = time.time()
            if mode != "manual":
                self.written.pop(index, None)
        if self.written.get(index) != duty:
            fanhardware.write_text(channel.path_pwm, duty)
            self.written[index] = duty

    def _apply_auto(self, channel):
        if self.applied.get(channel.index) != "auto":
            self._remember_original(channel)
            channel.set_auto()
            self.applied[channel.index] = "auto"
            self.written.pop(channel.index, None)

    def _release(self, index):
        """Hand one channel back to the state it had before this app ran."""
        channel = self.channels.get(index)
        if channel is None:
            return
        original = self.original.pop(index, None)
        try:
            detail = fanhardware.restore_channel(channel, original, self.log)
            self.log("fan%d removed from config, restored to %s" % (index, detail))
        except OSError as exc:
            self.log("releasing fan%d failed: %s" % (index, exc))

    def tick(self):
        if self.paused.is_set():
            return  # a probe currently owns the hardware
        with self.lock:
            config = self.config
            channels = dict(self.channels)
        if config is None:
            return
        self._read_sources()

        for fan in config["fans"]:
            channel = channels.get(fan["channel"])
            if channel is None:
                continue
            state = {
                "channel": fan["channel"],
                "temperature": None,
                "percent": None,
                "duty": None,
                "degraded": False,
                "error": None,
            }
            try:
                if not config["enabled"] or fan["mode"] == "auto":
                    self._apply_auto(channel)
                    state["duty"] = channel.duty
                elif fan["mode"] == "manual":
                    self._apply_duty(channel, fan["manual_duty"])
                    state["percent"] = round(fan["manual_duty"] * 100.0 / 255.0, 1)
                    state["duty"] = fan["manual_duty"]
                else:
                    temperature = self._effective_temperature(fan["source"])
                    state["temperature"] = temperature
                    if temperature is None:
                        # Every configured source is unavailable: fail safe.
                        duty = config["fail_safe_duty"]
                        state["degraded"] = True
                    else:
                        percent = fanconfig.interpolate(fan["points"], temperature)
                        duty = fanconfig.duty_from_percent(
                            percent, fan["min_duty"], fan["max_duty"]
                        )
                        state["curve_percent"] = round(percent, 1)
                        previous = self.hold.get(fan["channel"])
                        if previous is not None:
                            held_temp, held_duty = previous
                            if (duty < held_duty
                                    and temperature > held_temp - config["hysteresis"]):
                                # Not cooled down far enough yet: hold the current
                                # duty AND the reference temperature it belongs to.
                                # Re-seeding the reference every tick would compare
                                # against the previous tick instead of against the
                                # temperature that produced this duty, so the fan
                                # would ratchet up on every transient spike and never
                                # come back down.
                                duty = held_duty
                            else:
                                self.hold[fan["channel"]] = (temperature, duty)
                        else:
                            self.hold[fan["channel"]] = (temperature, duty)
                    self._apply_duty(channel, duty)
                    state["duty"] = duty
                    # Report what actually reaches the register (hysteresis or
                    # the min/max limits may differ from the raw curve value);
                    # curve_percent keeps the unmodified reading for debugging.
                    state["percent"] = round(duty * 100.0 / 255.0, 1)
            except OSError as exc:
                state["error"] = str(exc)
                channel.last_error = str(exc)
                self.log("fan%d write failed: %s" % (fan["channel"], exc))
            self.fan_state[fan["channel"]] = state

        self.degraded = any(
            state.get("degraded") for state in self.fan_state.values()
        )
        self.updated = time.time()

    # -- lifecycle --------------------------------------------------------- #

    def run(self):
        config = self.config or fanconfig.DEFAULT_CONFIG
        self.log("control loop started (interval=%ss, hysteresis=%sC)"
                 % (config["interval"], config["hysteresis"]))
        while not self.stop_event.is_set():
            started = time.time()
            try:
                self.tick()
            except Exception as exc:  # keep the loop alive no matter what
                self.log("control loop error: %r" % (exc,))
            interval = (self.config or {}).get("interval", 3)
            self.stop_event.wait(max(0.5, interval - (time.time() - started)))
        self.log("control loop stopped")

    def start(self):
        self.thread = threading.Thread(target=self.run, name="fancontrol", daemon=True)
        self.thread.start()

    def shutdown(self):
        """Stop the loop and hand back the channels this app was driving.

        Only channels this app actually wrote to are touched.  A header the
        BIOS runs in manual mode that we never managed must keep its BIOS
        setting -- blanket-writing auto mode to every header changes hardware
        state the user never asked us to touch.
        """
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

        managed = sorted(self.applied)
        if not managed:
            self.log("no channel was under control, nothing to restore")
            return
        restored = []
        for index in managed:
            channel = self.channels.get(index)
            if channel is None:
                continue
            original = self.original.pop(index, None)
            try:
                detail = fanhardware.restore_channel(channel, original, self.log)
                restored.append("%d (%s)" % (index, detail))
            except OSError as exc:
                self.log("restore fan%d failed: %s" % (index, exc))
        self.applied.clear()
        self.mode_at.clear()
        self.written.clear()
        self.log("handed channel(s) back: %s" % (", ".join(restored),))

    # -- first-run detection ----------------------------------------------- #

    def probe(self, settle=None):
        """Pause control and spin every header up to see which ones have a fan.

        Returns one result dict per header.  Every header is put back exactly as
        it was, and the control loop is blocked for the duration so the two
        never fight over the same PWM register.
        """
        with self.lock:
            channels = list(self.channels.values())
        if not channels:
            return []
        self.paused.set()
        try:
            time.sleep(0.3)  # let an in-flight tick finish and stay out
            self.log("probing %d header(s), %.1fs each" % (len(channels), settle))
            # ``settle`` is the spin-down wait; spinning up is quicker, so the
            # high point gets a proportionally shorter one.
            kwargs = {}
            if settle:
                kwargs["low_settle"] = float(settle)
                kwargs["high_settle"] = max(1.2, float(settle) * 0.7)
            results = fanhardware.probe_channels(channels, self.log, **kwargs)
            with self.lock:
                self.applied.clear()
                self.mode_at.clear()
                self.written.clear()
                self.hold.clear()
                self.probe_results = {item["channel"]: item for item in results}
            # Persist the whole result set, not just the channels the user goes
            # on to manage: the detection table reads every header back, and a
            # calibration should not evaporate because a header was left unticked.
            self._store_probe(results)
            return results
        finally:
            self.paused.clear()

    def _store_probe(self, results):
        """Write the latest probe results into the configuration."""
        with self.lock:
            config = dict(self.config or fanconfig.DEFAULT_CONFIG)
        config["probe"] = {
            "at": time.time(),
            "results": [
                {key: item.get(key) for key in (
                    "channel", "rpm_high", "rpm_low", "duty_high", "duty_low",
                    "rpm_min", "rpm_max", "responsive", "stops", "detected")}
                for item in results
            ],
        }
        config = fanconfig.normalise(config, list(self.channels.values()), self.gpu_keys)
        try:
            fanconfig.save(self.config_path, config)
        except OSError as exc:
            self.log("could not store the calibration results: %s" % exc)
            return
        with self.lock:
            self.config = config

    def _calibration_for(self, index):
        """Turn a probe result into a storable calibration record."""
        result = self.probe_results.get(index)
        if not result or result.get("rpm_high") is None:
            return None
        if not result.get("rpm_max"):
            return None
        return {
            "duty_low": int(result["duty_low"]),
            "rpm_low": int(result["rpm_low"] or 0),
            "duty_high": int(result["duty_high"]),
            "rpm_high": int(result["rpm_high"] or 0),
            "rpm_min": int(result["rpm_min"] or 0),
            "rpm_max": int(result["rpm_max"] or 0),
            "responsive": bool(result.get("responsive")),
            "stops": bool(result.get("stops")),
            "at": int(time.time()),
        }

    def apply_setup(self, selected):
        """Complete the first-run wizard.

        Channels the user already had are kept verbatim, so re-running the
        detection never throws away curves that were already tuned; newly
        selected channels get sensible defaults.
        """
        with self.lock:
            channels = dict(self.channels)
            existing = {fan["channel"]: fan
                        for fan in (self.config or {}).get("fans", [])}
        fans = []
        for index in selected:
            channel = channels.get(index)
            if channel is None:
                continue
            fan = dict(existing.get(index) or fanconfig.default_fan_for_channel(channel))
            calibration = self._calibration_for(index)
            if calibration:
                fan["calibration"] = calibration
            fans.append(fan)
        raw = dict(self.config or fanconfig.DEFAULT_CONFIG)
        raw["fans"] = fans
        raw["setup_complete"] = True
        # Record the machine this calibration belongs to, so swapping a CPU, a
        # GPU or a disk re-opens the wizard rather than quietly keeping numbers
        # measured against different hardware.
        raw["fingerprint"] = self.hardware_fingerprint()
        self.hardware_changes = []
        self.log("setup complete: channel(s) %s selected" % (selected,))
        return self.apply_config(raw)

    # -- reporting --------------------------------------------------------- #

    def source_list(self):
        """Describe every temperature source for the UI, in display order.

        The frontend renders exactly this list, so a machine with two GPUs just
        gets two cards -- the UI needs no idea how many GPUs exist.
        """
        with self.lock:
            gpus = list(self.gpus)
            aux = list(self.aux)
        sources = self._source_state()

        items = []

        def add(key, kind, label, info):
            items.append({
                "key": key,
                "kind": kind,
                "label": label,
                "enabled": bool(info.get("enabled")),
                "temperature": info.get("temperature"),
                "detail": info.get("detail"),
            })

        add("cpu", "cpu", "CPU", sources.get("cpu") or {})
        for index, gpu in enumerate(gpus, start=1):
            key = fanconfig.GPU_PREFIX + gpu.id
            label = "显卡%d" % index if len(gpus) > 1 else "显卡"
            add(key, "gpu", label,
                sources.get(key) or {"detail": gpu.label or gpu.id})
        add("hdd", "hdd", "硬盘", sources.get("hdd") or {})
        for sensor in aux:
            key = fanconfig.AUX_PREFIX + sensor.id
            add(key, "aux", sensor.label or sensor.id,
                sources.get(key) or {"detail": sensor.label or sensor.id})
        return items

    def status(self):
        with self.lock:
            config = self.config or fanconfig.DEFAULT_CONFIG
            channels = dict(self.channels)
            fans = []
            for fan in config["fans"]:
                channel = channels.get(fan["channel"])
                state = self.fan_state.get(fan["channel"], {})
                if not config["enabled"] or fan["mode"] == "auto":
                    effective = "auto"
                else:
                    effective = fan["mode"]
                fans.append(
                    {
                        "channel": fan["channel"],
                        "name": fan["name"],
                        "mode": fan["mode"],
                        "effective_mode": effective,
                        "source": fan["source"],
                        "points": fan["points"],
                        "min_duty": fan["min_duty"],
                        "max_duty": fan["max_duty"],
                        "manual_duty": fan["manual_duty"],
                        "calibration": fan.get("calibration"),
                        "rpm": channel.rpm if channel else None,
                        "enable": channel.enable if channel else None,
                        "present": channel.present if channel else False,
                        "temperature": state.get("temperature"),
                        "percent": state.get("percent"),
                        "curve_percent": state.get("curve_percent"),
                        "duty": state.get("duty"),
                        "degraded": state.get("degraded", False),
                        "error": state.get("error"),
                    }
                )
            return {
                "ok": True,
                "version": VERSION,
                "app": APP_NAME,
                "enabled": config["enabled"],
                "detail": "running",
                "chip": self.chip,
                "hardware_ready": self.hardware_ready,
                "controllable": self.controllable,
                "controller": self.profile.as_dict() if self.profile else None,
                "needs_setup": not config.get("setup_complete", True),
                #: What changed since the calibration, so the wizard can say why
                #: it came back instead of just appearing again.
                "hardware_changes": list(self.hardware_changes),
                "probe_at": (config.get("probe") or {}).get("at"),
                "probing": self.paused.is_set(),
                "degraded": self.degraded,
                "updated": self.updated,
                "caller": self.last_identity,
                "unsupported": fanhardware.detect_other_controllers(),
                "locked": config["fail_safe_duty"] == 255 and self.degraded,
                "sources": self._source_state(),
                "source_list": self.source_list(),
                "devices": [d.as_dict() for d in self.disks],
                "gpus": [g.as_dict() for g in self.gpus],
                "aux": [s.as_dict() for s in self.aux],
                "fans": fans,
                "config": config,
            }

    def hardware_info(self):
        with self.lock:
            results = {index: dict(item)
                       for index, item in self.probe_results.items()}
            channels = []
            for channel in self.channels.values():
                entry = channel.as_dict()
                # Fold the last calibration back in, so the detection table
                # still reads properly after a restart, an upgrade, or simply
                # reloading the page.
                for key, value in (results.get(channel.index) or {}).items():
                    if key != "channel":
                        entry[key] = value
                channels.append(entry)
            disks = [d.as_dict() for d in self.disks]
            gpus = [g.as_dict() for g in self.gpus]
            aux = [s.as_dict() for s in self.aux]
            chip = self.chip
        return {
            "chip": chip,
            "controller": self.profile.as_dict() if self.profile else None,
            "channels": channels,
            "disks": disks,
            "gpus": gpus,
            "aux": aux,
            # Everything the machine exposes, including inputs no source offers,
            # so the detection screen can account for all of them.
            "temp_inventory": fanhardware.list_temp_inventory(),
            "profiles": [p.as_dict() for p in fanhardware.PROFILES],
            "unsupported": fanhardware.detect_other_controllers(),
        }


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "%s/%s" % (APP_NAME, VERSION)
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------- #

    def address_string(self):
        return "unix"

    def log_message(self, fmt, *args):
        self.server.log("http %s" % (fmt % args))

    def _relative(self):
        path = urllib.parse.urlparse(self.path).path
        prefix = self.server.gateway_prefix
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        if not path.startswith("/"):
            path = "/" + path
        return urllib.parse.unquote(path)

    def _respond(self, code, body=b"", ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code=200):
        self._respond(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _error(self, code, message):
        self._json({"ok": False, "error": message}, code)

    def _read_body(self, limit=512 * 1024):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        return self.rfile.read(length)

    # -- authorisation ----------------------------------------------------- #

    def gateway_identity(self):
        """Identity headers injected by the fnOS gateway.

        The gateway authenticates the *session* and forwards who the caller
        is; it does not decide what this application permits, so the policy
        check below is ours to make.
        """
        return {
            "userid": self.headers.get("X-Trim-Userid"),
            "username": self.headers.get("X-Trim-Username"),
            "isadmin": self.headers.get("X-Trim-Isadmin"),
        }

    def authorise(self):
        """Return None when the request may proceed, else a rejection reason."""
        identity = self.gateway_identity()

        if identity["userid"] is None:
            # Reached the socket directly instead of through the gateway.  The
            # socket is root-only (0600) so the peer is already trusted; this
            # also keeps local TCP debugging usable.
            return None

        self.server.controller.record_identity(identity)
        if not self.server.require_admin:
            return None
        if str(identity["isadmin"]).strip().lower() in ("1", "true", "yes"):
            return None
        return "需要管理员权限"

    # -- static files ------------------------------------------------------ #

    def _serve_static(self, rel):
        if rel in ("/", ""):
            rel = "/index.html"
        root = os.path.realpath(self.server.ui_dir)
        target = os.path.realpath(os.path.join(root, rel.lstrip("/")))
        # never escape the UI directory
        if target != root and not target.startswith(root + os.sep):
            self._error(403, "forbidden")
            return
        if not os.path.isfile(target):
            self._error(404, "not found")
            return
        ctype = MIME_TYPES.get(os.path.splitext(target)[1].lower(),
                               "application/octet-stream")
        if target.endswith(".html"):
            try:
                with open(target, "r", encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as exc:
                self._error(500, str(exc))
                return
            # The browser always reaches us under the gateway prefix, even when
            # the gateway strips it before forwarding, so inject it directly.
            # The placeholder is deliberately not a substring of
            # ``window.FANCONTROL_BASE`` so a blanket replace stays safe.
            body = text.replace("__FC_BASE__", self.server.base_path).encode("utf-8")
            self._respond(200, body, ctype)
            return
        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            self._error(500, str(exc))
            return
        self._respond(200, body, ctype)

    # -- routes ------------------------------------------------------------ #

    def do_GET(self):
        denied = self.authorise()
        if denied:
            self._error(403, denied)
            return
        path = self._relative()
        controller = self.server.controller
        if path == "/api/status":
            self._json(controller.status())
        elif path == "/api/config":
            self._json(controller.status()["config"])
        elif path == "/api/hardware":
            self._json(controller.hardware_info())
        elif path == "/api/ping":
            self._json({"ok": True, "app": APP_NAME, "version": VERSION})
        elif path.startswith("/api/"):
            self._error(404, "unknown endpoint")
        else:
            self._serve_static(path)

    do_HEAD = do_GET

    def do_POST(self):
        denied = self.authorise()
        if denied:
            self._error(403, denied)
            return
        path = self._relative()
        controller = self.server.controller
        body = self._read_body()
        if body is None:
            self._error(400, "missing or oversized body")
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error(400, "invalid JSON")
            return

        if path == "/api/config":
            try:
                config = controller.apply_config(payload)
            except OSError as exc:
                self._error(500, "cannot save config: %s" % exc)
                return
            self._json({"ok": True, "config": config})
        elif path == "/api/probe":
            try:
                settle = float((payload or {}).get("settle", 3.5))
            except (TypeError, ValueError):
                settle = 3.5
            settle = max(1.0, min(15.0, settle))
            results = controller.probe(settle)
            self._json({"ok": True, "results": results,
                        "hardware": controller.hardware_info()})
        elif path == "/api/setup":
            selected = (payload or {}).get("channels")
            if not isinstance(selected, list):
                self._error(400, "channels must be a list")
                return
            try:
                indexes = sorted({int(item) for item in selected})
            except (TypeError, ValueError):
                self._error(400, "channels must be integers")
                return
            try:
                config = controller.apply_setup(indexes)
            except OSError as exc:
                self._error(500, "cannot save config: %s" % exc)
                return
            self._json({"ok": True, "config": config})
        elif path == "/api/action":
            action = (payload or {}).get("action")
            if action == "restore-auto":
                restored = fanhardware.restore_all_to_auto(self.server.log)
                controller.applied.clear()
                controller.written.clear()
                self._json({"ok": True, "restored": restored})
            elif action == "skip-setup":
                raw = dict(controller.config or {})
                raw["setup_complete"] = True
                controller.apply_config(raw)
                self._json({"ok": True})
            elif action == "refresh-hardware":
                controller.refresh_hardware()
                controller.load_config()
                self._json({"ok": True, "hardware": controller.hardware_info()})
            else:
                self._error(400, "unknown action")
        else:
            self._error(404, "unknown endpoint")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def resolve_paths(args):
    here = os.path.dirname(os.path.abspath(__file__))
    appdest = (
        getattr(args, "appdest", None)
        or os.environ.get("TRIM_APPDEST")
        or os.path.dirname(here)
    )
    etc = (
        getattr(args, "etc", None)
        or os.environ.get("TRIM_PKGETC")
        or os.path.join(appdest, "etc")
    )
    return {
        "appdest": appdest,
        "etc": etc,
        "config": os.path.join(etc, "config.json"),
        "ui": getattr(args, "ui", None) or os.path.join(appdest, "ui"),
        "socket": (
            getattr(args, "socket", None)
            or os.environ.get("FANCONTROL_SOCKET")
            or os.path.join(appdest, "app.sock")
        ),
    }


def cmd_init_config(args, log, paths):
    controller = Controller(paths["config"], log)
    controller.refresh_hardware()
    existing = controller.load_config(create_if_missing=False)
    if existing is not None and not args.force:
        log("config already exists at %s (use --force to overwrite)" % paths["config"])
        return 0
    config = fanconfig.build_default_config(list(controller.channels.values()),
                                            setup_complete=False)

    # Switch on exactly the sources this machine can actually read, so a fresh
    # install never starts with a working sensor silently turned off.
    config["sources"]["cpu"] = fanhardware.read_cpu()[1] is not None
    config["sources"]["hdd"] = fanhardware.read_hdd(controller.disks)[1] is not None
    config["sources"]["gpus"] = {
        fanconfig.GPU_PREFIX + gpu.id: gpu.temperature() is not None
        for gpu in controller.gpus
    }
    log("detected temperature sources: cpu=%s hdd=%s gpus=%s"
        % (config["sources"]["cpu"], config["sources"]["hdd"],
           [k for k, v in config["sources"]["gpus"].items() if v] or "none"))

    fanconfig.save(paths["config"], config)
    log("wrote %s with %d fan(s): %s"
        % (paths["config"], len(config["fans"]),
           ", ".join("fan%d=%s" % (f["channel"], f["name"]) for f in config["fans"])
           or "none detected"))
    return 0


def cmd_restore(args, log, paths):
    restored = fanhardware.restore_all_to_auto(log)
    log("restored BIOS auto control on header(s): %s" % (restored,))
    return 0


def cmd_status(args, log, paths):
    controller = Controller(paths["config"], log)
    controller.refresh_hardware()
    controller.load_config(create_if_missing=False)
    controller.tick()
    json.dump(controller.status(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_run(args, log, paths):
    ui_dir = args.ui or paths["ui"]
    if not os.path.isdir(ui_dir):
        log("warning: UI directory %s is missing" % ui_dir)

    controller = Controller(paths["config"], log)
    controller.refresh_hardware()
    controller.load_config()
    controller.start()

    gateway_prefix = args.gateway_prefix

    if args.host:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        endpoint = "http://%s:%d" % (args.host, args.port)
        base_path = "/"
    else:
        socket_path = paths["socket"]
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        os.makedirs(os.path.dirname(socket_path), exist_ok=True)
        server = UnixHTTPServer(socket_path, Handler)
        # Only the fnOS gateway (/usr/trim/bin/trim_http_cgi, which runs as
        # root) needs to reach this socket.  Keeping it root-only stops any
        # local account from bypassing the gateway and driving the fans.
        os.chmod(socket_path, 0o600)
        endpoint = "unix:%s" % socket_path
        base_path = gateway_prefix.rstrip("/") + "/"

    server.controller = controller
    server.log = log
    server.ui_dir = ui_dir
    server.gateway_prefix = gateway_prefix
    server.base_path = base_path
    server.require_admin = getattr(args, "require_admin", True)
    server.timeout = 1

    stopping = threading.Event()

    def handle_signal(signum, _frame):
        if not stopping.is_set():
            log("received signal %d, shutting down" % signum)
            stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handle_signal)

    log("%s %s listening on %s (ui=%s)" % (APP_TITLE, VERSION, endpoint, ui_dir))
    http_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5},
                                   name="http", daemon=True)
    http_thread.start()

    while not stopping.is_set():
        stopping.wait(1.0)

    server.shutdown()
    server.server_close()
    if not args.host and os.path.exists(paths["socket"]):
        try:
            os.unlink(paths["socket"])
        except OSError:
            pass
    controller.shutdown()
    log("stopped")
    return 0


def _add_command(sub, name, help_text, extra=()):
    parser = sub.add_parser(name, help=help_text)
    parser.add_argument("--appdest", help="installed app dir (default $TRIM_APPDEST)")
    parser.add_argument("--etc", help="config dir (default $TRIM_PKGETC)")
    for flag, kwargs in extra:
        parser.add_argument(flag, **kwargs)
    return parser


def build_parser():
    parser = argparse.ArgumentParser(
        prog="fancontrold", description="%s daemon" % APP_TITLE)
    sub = parser.add_subparsers(dest="command")

    _add_command(
        sub,
        "run",
        "run the control loop and the HTTP server",
        (
            ("--ui", {"help": "directory holding the web UI"}),
            ("--socket", {"help": "gateway socket path"}),
            (
                "--gateway-prefix",
                {
                    "default": "/app/" + APP_NAME,
                    "help": "path prefix prepended by the fnOS gateway",
                },
            ),
            ("--host", {"help": "serve on TCP instead of a Unix socket (testing)"}),
            ("--port", {"type": int, "default": 8099}),
            (
                "--require-admin",
                {
                    "action": "store_true",
                    "default": True,
                    "dest": "require_admin",
                    "help": "only accept the fnOS administrator flag (default)",
                },
            ),
            (
                "--allow-non-admin",
                {
                    "action": "store_false",
                    "dest": "require_admin",
                    "help": "let any logged-in fnOS user control the fans",
                },
            ),
        ),
    )
    _add_command(
        sub,
        "init-config",
        "write a default configuration",
        (("--force", {"action": "store_true"}),),
    )
    _add_command(sub, "restore", "hand every fan back to the BIOS")
    _add_command(sub, "status", "print status as JSON")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv = ["run"] + argv
    args = build_parser().parse_args(argv)

    log = Log()
    paths = resolve_paths(args)
    handlers = {
        "run": cmd_run,
        "init-config": cmd_init_config,
        "restore": cmd_restore,
        "status": cmd_status,
    }
    try:
        return handlers[args.command](args, log, paths)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
