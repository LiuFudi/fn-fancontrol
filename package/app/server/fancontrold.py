#!/usr/bin/env python3
"""fn-fancontrol daemon.

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

APP_NAME = "fn-fancontrol"
# Must be kept in step with the ``version`` field of the package manifest:
# the app center does not export TRIM_APPVER to the daemon.
VERSION = "1.3.0"

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
        self.sources = {
            "cpu": {"temperature": None, "detail": None},
            "gpu": {"temperature": None, "detail": None},
            "hdd": {"temperature": None, "detail": None},
        }
        self.disks = []
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

    def load_config(self, create_if_missing=True):
        channels = list(self.channels.values())

        # A config written before the detection wizard existed has no
        # setup_complete key.  Show the wizard once so those users get to pick
        # from every real channel too, then persist the decision.
        legacy = False
        try:
            with open(self.config_path, "r") as handle:
                legacy = "setup_complete" not in json.load(handle)
        except (OSError, ValueError):
            legacy = False

        config = fanconfig.load(self.config_path, channels)
        if config is None:
            if not create_if_missing:
                return None
            config = fanconfig.build_default_config(channels)
            fanconfig.save(self.config_path, config)
            self.log("created default config at %s" % self.config_path)
        elif legacy:
            config["setup_complete"] = False
            fanconfig.save(self.config_path, config)
            self.log("config predates the detection wizard; showing it once")

        with self.lock:
            self.config = config
        return config

    def apply_config(self, raw):
        """Validate, persist and immediately apply a new configuration."""
        channels = list(self.channels.values())
        config = fanconfig.normalise(raw, channels)
        fanconfig.save(self.config_path, config)
        with self.lock:
            previously_managed = set(self.applied)
            managed = {fan["channel"] for fan in config["fans"]}
            self.config = config
            # force a rewrite on the next tick
            self.applied.clear()
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
        config = self.config or fanconfig.DEFAULT_CONFIG
        wanted = config["sources"]
        now = time.time()

        if wanted.get("cpu"):
            value, detail = fanhardware.read_cpu()
            self.sources["cpu"] = {"temperature": value, "detail": detail}
        else:
            self.sources["cpu"] = {"temperature": None, "detail": "disabled"}

        if wanted.get("gpu"):
            value, detail = fanhardware.read_gpu()
            self.sources["gpu"] = {"temperature": value, "detail": detail}
        else:
            self.sources["gpu"] = {"temperature": None, "detail": "disabled"}

        if not wanted.get("hdd"):
            self.sources["hdd"] = {"temperature": None, "detail": "disabled"}
            return

        # Disks change temperature slowly, so poll them far less often: this
        # keeps SATA disks from being woken up every few seconds.
        if force_disks or now - self.hdd_read_at >= config["hdd_interval"]:
            self.hdd_read_at = now
            selected = wanted.get("hdd_devices") or None
            self.disks = fanhardware.list_disk_sensors()
            value, detail = fanhardware.read_hdd(self.disks, selected)
            self.sources["hdd"] = {"temperature": value, "detail": detail}

    def _effective_temperature(self, keys):
        best = None
        for key in keys:
            value = self.sources.get(key, {}).get("temperature")
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

    def _apply_duty(self, channel, duty):
        if not channel.controllable:
            return
        if self.applied.get(channel.index) != "manual":
            self._remember_original(channel)
            fanhardware.write_text(channel.path_enable, 1)
            self.applied[channel.index] = "manual"
            self.written.pop(channel.index, None)
        if self.written.get(channel.index) != duty:
            fanhardware.write_text(channel.path_pwm, duty)
            self.written[channel.index] = duty

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
                        state["percent"] = round(percent, 1)
                        previous = self.hold.get(fan["channel"])
                        if previous and duty < previous[1]:
                            if temperature > previous[0] - config["hysteresis"]:
                                # not cooled down enough yet -- keep the fan where it is
                                duty = previous[1]
                                state["percent"] = round(previous[1] * 100.0 / 255.0, 1)
                        self.hold[fan["channel"]] = (temperature, duty)
                    self._apply_duty(channel, duty)
                    state["duty"] = duty
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
        self.written.clear()
        self.log("handed channel(s) back: %s" % (", ".join(restored),))

    # -- first-run detection ----------------------------------------------- #

    def probe(self, settle=2.0):
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
            results = fanhardware.probe_channels(channels, self.log, settle=settle)
            with self.lock:
                self.applied.clear()
                self.written.clear()
                self.hold.clear()
            return results
        finally:
            self.paused.clear()

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
            fans.append(existing.get(index, fanconfig.default_fan_for_channel(channel)))
        raw = dict(self.config or fanconfig.DEFAULT_CONFIG)
        raw["fans"] = fans
        raw["setup_complete"] = True
        self.log("setup complete: channel(s) %s selected" % (selected,))
        return self.apply_config(raw)

    # -- reporting --------------------------------------------------------- #

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
                        "rpm": channel.rpm if channel else None,
                        "enable": channel.enable if channel else None,
                        "present": channel.present if channel else False,
                        "temperature": state.get("temperature"),
                        "percent": state.get("percent"),
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
                "probing": self.paused.is_set(),
                "degraded": self.degraded,
                "updated": self.updated,
                "caller": self.last_identity,
                "unsupported": fanhardware.detect_other_controllers(),
                "locked": config["fail_safe_duty"] == 255 and self.degraded,
                "sources": {k: dict(v) for k, v in self.sources.items()},
                "devices": [d.as_dict() for d in self.disks],
                "fans": fans,
                "config": config,
            }

    def hardware_info(self):
        with self.lock:
            channels = [c.as_dict() for c in self.channels.values()]
            disks = [d.as_dict() for d in self.disks]
            chip = self.chip
        return {
            "chip": chip,
            "controller": self.profile.as_dict() if self.profile else None,
            "channels": channels,
            "disks": disks,
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
                settle = float((payload or {}).get("settle", 2.0))
            except (TypeError, ValueError):
                settle = 2.0
            settle = max(0.5, min(10.0, settle))
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

    log("%s %s listening on %s (ui=%s)" % (APP_NAME, VERSION, endpoint, ui_dir))
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
        prog="fancontrold", description="fn-fancontrol daemon")
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
