# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of fn-fancontrol, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
"""Hardware access layer for fn-fancontrol.

Three responsibilities:

* identify the fan controller behind a hwmon node and know that driver's
  ``pwmN_enable`` convention (they are **not** the same across vendors);
* expose per-header PWM / tachometer access plus a brief spin-up probe that
  finds out which headers actually have a fan attached;
* resolve the CPU, GPU and disk temperature sources the user can pick from.

Everything goes through sysfs (or, as a last resort, ``smartctl``), so the
module stays dependency free.

The ``pwm_enable`` values below come from the kernel documentation for each
driver (``Documentation/hwmon/*.rst``), not from guesswork: writing the wrong
value can hand fan control back to the BIOS or disable it altogether.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time

# --------------------------------------------------------------------------- #
# low level sysfs helpers
# --------------------------------------------------------------------------- #


def read_text(path):
    try:
        with open(path, "r") as handle:
            return handle.read().strip()
    except (OSError, ValueError):
        return None


def read_int(path):
    raw = read_text(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def write_text(path, value):
    """Write to a sysfs attribute. Raises OSError on failure."""
    with open(path, "w") as handle:
        handle.write(str(value))


# --------------------------------------------------------------------------- #
# controller profiles
# --------------------------------------------------------------------------- #


class ControllerProfile:
    """What we need to know about one fan-controller driver family."""

    __slots__ = ("key", "label", "prefixes", "manual", "auto", "controllable",
                 "modules", "note", "tested")

    def __init__(self, key, label, prefixes, manual=1, auto=2,
                 controllable=True, modules=(), note="", tested=False):
        self.key = key
        self.label = label
        self.prefixes = tuple(prefixes)
        self.manual = manual
        self.auto = auto
        self.controllable = controllable
        self.modules = tuple(modules)
        self.note = note
        self.tested = tested

    def as_dict(self):
        return {
            "key": self.key,
            "label": self.label,
            "manual": self.manual,
            "auto": self.auto,
            "controllable": self.controllable,
            "tested": self.tested,
            "note": self.note,
        }


#: Keyed on the ``name`` the driver publishes in ``/sys/class/hwmon/*/name``,
#: which is the chip prefix, not the module name.
PROFILES = (
    ControllerProfile(
        key="nct6775",
        label="Nuvoton NCT6775 / 6776 / 6779 / 679x",
        prefixes=("nct6106", "nct6775", "nct6776", "nct6779", "nct6791", "nct6792",
                  "nct6793", "nct6795", "nct6796", "nct6797", "nct6798", "nct6799"),
        manual=1,          # manual mode
        auto=5,            # Smart Fan IV, the best automatic mode on these chips
        modules=("nct6775",),
        tested=True,
    ),
    ControllerProfile(
        key="nct6683",
        label="Nuvoton NCT6683 / 6686 / 6687",
        prefixes=("nct6683", "nct6686", "nct6687"),
        manual=1, auto=2,
        controllable=False,
        modules=("nct6683",),
        note="内核 nct6683 驱动出于安全考虑**禁用了该芯片的 PWM 写入**"
             "（Intel EC 固件的寄存器布局与 Nuvoton 数据手册不符，写错地址有风险），"
             "因此本应用只能读取它的转速与温度，无法调速。"
             "非 Intel 主板可以尝试 modprobe nct6683 force=1 让它先识别出芯片。",
    ),
    ControllerProfile(
        key="it87",
        label="ITE IT87xx",
        prefixes=("it87", "it8603", "it8620", "it8623", "it8628", "it8689",
                  "it8712", "it8716", "it8718", "it8720", "it8721", "it8726",
                  "it8728", "it8732", "it8758", "it8771", "it8772", "it8781",
                  "it8782", "it8783", "it8786", "it8790", "it8792", "it87952"),
        manual=1,
        auto=2,            # "Smart Guardian" -- only implemented for
                           # pre-IT8705F-revF / pre-IT8712F-revG chips;
                           # newer silicon rejects it, and restore_channel()
                           # falls back to full speed in that case
        modules=("it87",),
    ),
    ControllerProfile(
        key="f71882fg",
        label="Fintek F718xx / F8000",
        prefixes=("f71808e", "f71808a", "f71858fg", "f71862fg", "f71863fg", "f71869",
                  "f71869a", "f71882fg", "f71883fg", "f71889fg", "f71889ed", "f71889a",
                  "f8000", "f81801u", "f81865f"),
        manual=1,
        auto=2,            # 3 = thermostat mode, F8000 duty-cycle only
        modules=("f71882fg",),
    ),
    ControllerProfile(
        key="f71805f",
        label="Fintek F71805F / F71872F",
        prefixes=("f71805f", "f71872f"),
        manual=1, auto=2,
        modules=("f71805f",),
    ),
    ControllerProfile(
        key="w83627ehf",
        label="Winbond W83627EHF / DHG / W83667HG",
        prefixes=("w83627ehf", "w83627dhg", "w83627uhg", "w83667hg"),
        manual=1, auto=2,
        modules=("w83627ehf",),
    ),
    ControllerProfile(
        key="w83627hf",
        label="Winbond W83627HF / THF / W83697HF",
        prefixes=("w83627hf", "w83627thf", "w83697hf", "w83687thf"),
        manual=1, auto=2,
        modules=("w83627hf",),
    ),
    ControllerProfile(
        key="sch56xx",
        label="SMSC SCH5627 / SCH5636",
        prefixes=("sch5627", "sch5636"),
        manual=1, auto=2,
        modules=("sch5627", "sch5636"),
    ),
)

#: Applied to any hwmon node that exposes ``pwmN`` but matches no profile
#: above.  The generic hwmon ABI defines 1 = manual and 2 = automatic, which is
#: what most of the remaining drivers implement.
GENERIC_PROFILE = ControllerProfile(
    key="generic",
    label="其它 hwmon 风扇控制器",
    prefixes=(),
    manual=1, auto=2,
    modules=(),
    note="该控制器没有专门的适配配置，按 hwmon 通用约定（1=手动、2=自动）驱动。"
         "不同驱动的自动模式编号可能不同，使用「BIOS 自动」模式前请谨慎确认。",
)

#: Modules worth loading even when no fan controller is present yet.
CANDIDATE_MODULES = (
    "nct6775", "it87", "f71882fg", "f71805f", "w83627ehf", "w83627hf",
    "sch5627", "sch5636", "nct6683",
)


def profile_for(hwmon_name):
    """Return the profile matching a hwmon ``name``, or None."""
    if not hwmon_name:
        return None
    for profile in PROFILES:
        for prefix in profile.prefixes:
            if hwmon_name == prefix or hwmon_name.startswith(prefix):
                return profile
    return None


# --------------------------------------------------------------------------- #
# hwmon discovery
# --------------------------------------------------------------------------- #

CPU_DRIVERS = ("coretemp", "k10temp", "zenpower", "k8temp", "cpu_thermal")
GPU_DRIVERS = ("amdgpu", "radeon", "nouveau", "i915", "xe")
DISK_DRIVERS = ("drivetemp", "nvme")

_HWMON_INDEX = re.compile(r"hwmon(\d+)$")
_PWM_NAME = re.compile(r"pwm(\d+)$")
_FAN_INPUT = re.compile(r"fan(\d+)_input$")


def hwmon_nodes():
    """Return ``[(path, name)]`` for every hwmon node, in numeric order."""
    nodes = []
    for path in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read_text(os.path.join(path, "name"))
        if not name:
            continue
        match = _HWMON_INDEX.search(path)
        nodes.append((int(match.group(1)) if match else 0, path, name))
    nodes.sort()
    return [(path, name) for _, path, name in nodes]


def has_pwm_attribute(path):
    for entry in glob.glob(os.path.join(path, "pwm*")):
        if _PWM_NAME.fullmatch(os.path.basename(entry)):
            return True
    return False


def find_controller():
    """Locate the fan controller.

    Returns ``(hwmon_path, chip_name, profile)``, or ``(None, None, None)``.
    A known chip always wins over the generic fallback, so a supported board is
    never driven with the wrong ``pwm_enable`` convention.
    """
    fallback = None
    for path, name in hwmon_nodes():
        profile = profile_for(name)
        if profile is not None:
            return path, name, profile
        if fallback is None and has_pwm_attribute(path):
            fallback = (path, name, GENERIC_PROFILE)
    if fallback is not None:
        return fallback
    return None, None, None


def find_superio():
    """Backwards compatible ``(path, name)`` accessor."""
    path, name, _profile = find_controller()
    return path, name


# --------------------------------------------------------------------------- #
# kernel modules
# --------------------------------------------------------------------------- #


def load_module(name, log=None):
    """Best effort ``modprobe``. Returns True when the module loaded."""
    if shutil.which("modprobe") is None:
        return False
    try:
        proc = subprocess.run(["modprobe", name], capture_output=True,
                              timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        if log:
            log("modprobe %s failed: %s" % (name, exc))
        return False
    if proc.returncode != 0 and log:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        if detail:
            log("modprobe %s rc=%s %s" % (name, proc.returncode, detail))
    return proc.returncode == 0


def ensure_modules(log=None):
    """Load whichever fan controller (and disk temperature) drivers we can.

    Loading a driver that finds no chip is harmless, and it is the only way to
    discover what the board actually has: these modules are never autoloaded,
    so without this a supported board looks identical to an unsupported one.
    """
    notes = []
    if find_controller()[0] is None:
        for module in CANDIDATE_MODULES:
            if os.path.isdir("/sys/module/%s" % module):
                continue
            if load_module(module, log):
                notes.append(module)
                if find_controller()[0] is not None:
                    break
    load_module("drivetemp", log)
    return notes


# --------------------------------------------------------------------------- #
# fan channels
# --------------------------------------------------------------------------- #


class FanChannel:
    """One PWM-capable fan header."""

    def __init__(self, hwmon, index, profile):
        self.hwmon = hwmon
        self.index = index
        self.profile = profile
        self.path_rpm = os.path.join(hwmon, "fan%d_input" % index)
        self.path_pwm = os.path.join(hwmon, "pwm%d" % index)
        self.path_enable = os.path.join(hwmon, "pwm%d_enable" % index)
        self.path_temp_sel = os.path.join(hwmon, "pwm%d_temp_sel" % index)
        self.path_mode = os.path.join(hwmon, "pwm%d_mode" % index)
        self.peak_rpm = 0
        self.low_rpm = None
        self.last_error = None

    # -- readings ---------------------------------------------------------- #

    @property
    def rpm(self):
        value = read_int(self.path_rpm)
        if value is None:
            return None
        if value > self.peak_rpm:
            self.peak_rpm = value
        if value > 0 and (self.low_rpm is None or value < self.low_rpm):
            self.low_rpm = value
        return value

    @property
    def duty(self):
        """Current PWM duty 0-255 as reported by the chip."""
        return read_int(self.path_pwm)

    @property
    def enable(self):
        return read_int(self.path_enable)

    @property
    def present(self):
        """True once this header has ever reported a tach signal."""
        return self.peak_rpm > 0

    @property
    def mode(self):
        """Output mode: 0 = DC (voltage), 1 = PWM, None when unsupported.

        A header wired for DC control can ignore duty-cycle writes entirely,
        which is one of the most common reasons a fan spins but cannot be
        regulated -- so it is worth surfacing.
        """
        return read_int(self.path_mode)

    @property
    def temp_sel(self):
        """Temperature source index the BIOS bound to this header."""
        return read_int(self.path_temp_sel)

    @property
    def temp_sel_label(self):
        """Driver label of the temperature source bound to this header.

        The raw index is meaningless on its own: on an NCT6796D ``temp_sel=3``
        is AUXTIN0, an *unconnected* thermistor, while ``1`` is SYSTIN and
        ``8`` is PECI.  Deciding whether a header watches the CPU needs the
        label, not the number.
        """
        selector = self.temp_sel
        if selector is None:
            return None
        return read_text(os.path.join(self.hwmon, "temp%d_label" % selector))

    @property
    def controllable(self):
        return self.profile.controllable

    def as_dict(self):
        return {
            "channel": self.index,
            "rpm": self.rpm,
            "duty": self.duty,
            "enable": self.enable,
            "present": self.present,
            "rpm_min_seen": self.low_rpm,
            "rpm_max_seen": self.peak_rpm or None,
            "temp_sel": self.temp_sel,
            "temp_sel_label": self.temp_sel_label,
            "mode": self.mode,
            "controllable": self.controllable,
            "error": self.last_error,
        }

    # -- control ----------------------------------------------------------- #

    def set_manual(self, duty):
        """Drive the header from the manual PWM register."""
        duty = max(0, min(255, int(duty)))
        write_text(self.path_enable, self.profile.manual)
        write_text(self.path_pwm, duty)

    def set_auto(self):
        """Select the chip's own automatic fan curve, if the driver has one."""
        write_text(self.path_enable, self.profile.auto)


def fan_channels(profile=None, hwmon=None):
    """Every PWM-capable header on the fan controller, tachometer ones first."""
    if hwmon is None:
        hwmon, _name, detected = find_controller()
        if profile is None:
            profile = detected
    if hwmon is None or profile is None:
        return []

    indices = set()
    for path in glob.glob(os.path.join(hwmon, "fan*_input")):
        match = _FAN_INPUT.match(os.path.basename(path))
        if match:
            indices.add(int(match.group(1)))
    for path in glob.glob(os.path.join(hwmon, "pwm*")):
        match = _PWM_NAME.match(os.path.basename(path))
        if match:
            indices.add(int(match.group(1)))

    channels = []
    for index in sorted(indices):
        if not os.path.exists(os.path.join(hwmon, "pwm%d" % index)):
            continue
        channel = FanChannel(hwmon, index, profile)
        channel.rpm  # prime the peak so ``present`` is meaningful immediately
        channels.append(channel)
    channels.sort(key=lambda item: (not item.present, item.index))
    return channels


# --------------------------------------------------------------------------- #
# returning a header to a known-good state
# --------------------------------------------------------------------------- #


def restore_channel(channel, snapshot=None, log=None):
    """Put one header back, most precise option first.

    1. the state recorded before this app touched the header;
    2. the driver's automatic mode;
    3. full speed -- the safe last resort, so a fan is never stranded on a low
       duty just because the driver cannot select an automatic mode (newer ITE
       chips are exactly that case).

    Returns a short description of what was applied.
    """
    if snapshot is not None:
        enable, duty = snapshot
        if duty is not None:
            write_text(channel.path_pwm, max(0, min(255, int(duty))))
        if enable is not None:
            write_text(channel.path_enable, int(enable))
            return "enable=%s duty=%s" % (enable, duty)
    try:
        channel.set_auto()
        return "automatic mode (%s auto=%s)" % (channel.profile.key,
                                                channel.profile.auto)
    except OSError as exc:
        if log:
            log("fan%d: automatic mode unavailable (%s), using full speed instead"
                % (channel.index, exc))
    write_text(channel.path_enable, channel.profile.manual)
    write_text(channel.path_pwm, 255)
    return "full speed (last resort)"


def restore_all_to_auto(log=None):
    """Emergency sweep: hand every controllable header back to a safe state."""
    restored = []
    for channel in fan_channels():
        if not channel.controllable:
            continue
        try:
            restore_channel(channel, None, log)
            restored.append(channel.index)
        except OSError as exc:
            if log:
                log("restore fan%d failed: %s" % (channel.index, exc))
    return restored


# --------------------------------------------------------------------------- #
# active probing
# --------------------------------------------------------------------------- #


#: Duties used for the two-point calibration: ~30 % and full speed.
PROBE_DUTIES = (76, 255)


def _sample_peak(channel, duration, sample=0.3):
    """Highest tach reading over ``duration`` seconds."""
    peak = 0
    deadline = time.time() + duration
    while time.time() < deadline:
        time.sleep(sample)
        value = channel.rpm
        if value and value > peak:
            peak = value
    return peak


def diagnose_unresponsive(channel, entry):
    """Explain why a header that reports a fan does not follow the duty cycle.

    Ordered by how specific the evidence is: a register that refuses the write
    is a different problem from a register that accepts it while the fan does
    not move.
    """
    hints = []
    index = channel.index
    high = entry.get("duty_high")
    readback = entry.get("pwm_readback")

    if entry.get("pwm_mode") == 0:
        hints.append(
            "该通道是 DC 电压调速模式（pwm%d_mode=0）。部分主板在这个模式下"
            "不响应占空比写入，可以切到 PWM 模式后重测：echo 1 > %s"
            % (index, channel.path_mode))

    if entry.get("enable_writable") is False:
        hints.append(
            "写 pwm%d_enable=%s 被驱动拒绝，芯片没能进入手动模式，仍由 BIOS 的"
            "自动曲线控制 —— 这种情况下写占空比不会有任何效果。"
            % (index, channel.profile.manual))
    elif entry.get("pwm_writable") is False:
        hints.append("写 pwm%d 被驱动拒绝（权限或驱动不支持）。" % index)
    elif readback is not None and high is not None and readback != high:
        hints.append(
            "写入占空比 %s 但读回 %s：寄存器没有接受写入，说明驱动或芯片忽略了它。"
            % (high, readback))
    else:
        hints.append(
            "寄存器写入与读回都正常，但转速不变 —— 问题多半在风扇或接线："
            "3 针（DC）风扇插在 PWM 接头上时第 4 根 PWM 线不起作用，风扇会恒速运转；"
            "也可能是该接头的 PWM 针脚没有实际接到插座。"
            "换一个接头、或换一把 4 针 PWM 风扇即可确认。")

    hints.append(
        "还要确认 BIOS 没有锁死风扇控制：部分主板（尤其 ASUS）会周期性重新接管 "
        "SuperIO，可以查 dmesg 里有没有 ACPI resource conflict。")
    return hints


#: Calibration runs from full speed down to *zero*.  Starting at 0 % is the
#: whole point: a fan that supports a stop mode reads 0 RPM there, which both
#: proves the PWM really reaches the fan and tells you the lowest speed you can
#: safely ask for.
PROBE_LOW_DUTY = 0
PROBE_HIGH_DUTY = 255


def probe_channels(channels, log=None, low_duty=PROBE_LOW_DUTY,
                   high_duty=PROBE_HIGH_DUTY, high_settle=2.5,
                   low_settle=3.5, window=1.2):
    """Two-point calibration: measure RPM at full duty and at a low duty.

    A stopped fan reads 0 RPM whether or not one is plugged in, so passive
    scanning cannot tell an empty header from an idle fan.  A single sample
    only proves a fan exists; two points also prove the PWM register actually
    *controls* it, which is the question that matters when a fan clearly does
    not follow its curve.

    Full speed is measured first: that identifies empty headers in one step and
    spares them the much slower spin-down measurement.  The second point is
    0 %, which is what reveals a fan that can be stopped.

    The waits are generous because a large chassis fan can take several seconds
    to change speed, and reading too early returns the *previous* speed -- which
    produced nonsense like "1730 RPM at 30 %, 1397 RPM at 100 %".
    """
    results = []
    for channel in channels:
        snapshot = (channel.enable, channel.duty)
        entry = {
            "channel": channel.index,
            "controllable": channel.controllable,
            "temp_sel": channel.temp_sel,
            "temp_sel_label": channel.temp_sel_label,
            "rpm_before": channel.rpm or 0,
            "rpm_high": None,
            "rpm_low": None,
            "duty_high": high_duty,
            "duty_low": low_duty,
            "rpm_min": None,
            "rpm_max": None,
            "detected": False,
            "responsive": False,
            "stops": False,
            "pwm_mode": channel.mode,
            "pwm_writable": None,
            "pwm_readback": None,
            "enable_writable": None,
            "hints": [],
            "error": None,
        }
        if channel.controllable:
            try:
                # point 1: full speed, capturing whether each write is accepted
                try:
                    write_text(channel.path_enable, channel.profile.manual)
                    entry["enable_writable"] = True
                except OSError as exc:
                    entry["enable_writable"] = False
                    entry["error"] = str(exc)
                try:
                    write_text(channel.path_pwm, high_duty)
                    entry["pwm_writable"] = True
                    entry["pwm_readback"] = channel.duty
                except OSError as exc:
                    entry["pwm_writable"] = False
                    entry["error"] = entry["error"] or str(exc)

                time.sleep(high_settle)
                entry["rpm_high"] = _sample_peak(channel, window)

                # point 2: only worth doing when something actually spins
                if entry["rpm_high"]:
                    try:
                        write_text(channel.path_pwm, low_duty)
                    except OSError:
                        pass
                    time.sleep(low_settle)
                    entry["rpm_low"] = _sample_peak(channel, window)
            except OSError as exc:
                entry["error"] = entry["error"] or str(exc)
            finally:
                try:
                    restore_channel(channel, snapshot, log)
                except OSError as exc:
                    entry["error"] = entry["error"] or str(exc)
                    if log:
                        log("fan%d: could not restore after probe: %s"
                            % (channel.index, exc))

        seen = [v for v in (entry["rpm_before"], entry["rpm_low"],
                            entry["rpm_high"]) if v is not None]
        if seen:
            entry["rpm_min"] = min(seen)
            entry["rpm_max"] = max(seen)
        entry["detected"] = bool(entry["rpm_max"])
        if entry["rpm_high"] and entry["rpm_low"] is not None:
            delta = entry["rpm_high"] - entry["rpm_low"]
            entry["responsive"] = delta >= max(40, 0.05 * entry["rpm_high"])
            # 0 RPM at 0 % duty means the fan really does stop when asked
            entry["stops"] = bool(entry["rpm_high"] and entry["rpm_low"] == 0)

        # A header that reports a fan but ignores the duty cycle is the single
        # most confusing failure mode, so explain it right where it is seen.
        if entry["detected"] and not entry["responsive"]:
            entry["hints"] = diagnose_unresponsive(channel, entry)

        results.append(entry)
        if log:
            log("probe fan%d: %s RPM @%s%% / %s RPM @%s%%  detected=%s responsive=%s "
                "stops=%s mode=%s%s"
                % (channel.index,
                   entry["rpm_high"] if entry["rpm_high"] is not None else "-",
                   round(high_duty * 100.0 / 255.0),
                   entry["rpm_low"] if entry["rpm_low"] is not None else "-",
                   round(low_duty * 100.0 / 255.0),
                   entry["detected"], entry["responsive"], entry["stops"],
                   "DC" if entry["pwm_mode"] == 0 else
                   ("PWM" if entry["pwm_mode"] == 1 else "?"),
                   "" if not entry["error"] else " error=%s" % entry["error"]))
            for hint in entry["hints"]:
                log("  fan%d hint: %s" % (channel.index, hint))
    return results


# --------------------------------------------------------------------------- #
# temperature sources
# --------------------------------------------------------------------------- #


def _max_temp_in(hwmon, pattern="temp*_input"):
    best = None
    for path in glob.glob(os.path.join(hwmon, pattern)):
        value = read_int(path)
        if value is None or value <= 0 or value > 150000:
            continue  # unconnected thermistor inputs read absurd values
        best = value if best is None else max(best, value)
    return best


def _labelled_temp(hwmon, wanted):
    """Return the temperature whose ``*_label`` matches one of ``wanted``."""
    for path in glob.glob(os.path.join(hwmon, "temp*_label")):
        label = (read_text(path) or "").strip().lower()
        if not label:
            continue
        for candidate in wanted:
            if candidate in label:
                value = read_int(path.replace("_label", "_input"))
                if value is not None:
                    return value
    return None


CPU_PREFERRED_LABELS = ("package id 0", "tctl", "tdie", "physical id 0", "package")


def read_cpu():
    """CPU package temperature in degrees Celsius, or ``(None, None)``."""
    for path, name in hwmon_nodes():
        if name not in CPU_DRIVERS:
            continue
        value = _labelled_temp(path, CPU_PREFERRED_LABELS)
        if value is None:
            value = _max_temp_in(path)
        if value:
            return value / 1000.0, "%s (hwmon)" % name

    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        if (read_text(os.path.join(zone, "type")) or "") != "x86_pkg_temp":
            continue
        value = read_int(os.path.join(zone, "temp"))
        if value:
            return value / 1000.0, "x86_pkg_temp (thermal zone)"

    hwmon = find_controller()[0]
    if hwmon:
        for path in glob.glob(os.path.join(hwmon, "temp*_label")):
            if "peci agent 0" not in (read_text(path) or "").lower():
                continue
            value = read_int(path.replace("_label", "_input"))
            if value:
                return value / 1000.0, "PECI Agent 0 (fan controller)"
    return None, None


_PCI_LABEL_CACHE = {}


def _pci_address(hwmon_path):
    """PCI address (``0000:07:00.0``) behind a hwmon node, if there is one."""
    device = os.path.realpath(os.path.join(hwmon_path, "device"))
    match = re.search(r"/([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])$", device)
    return match.group(1) if match else None


def _pci_label(address):
    """Marketing name for a PCI address, resolved once through lspci.

    Without this a GPU is only ever called "i915" or "amdgpu", which says
    nothing about *which* card it is -- unhelpful on a box that has two.
    """
    if not address:
        return None
    if address in _PCI_LABEL_CACHE:
        return _PCI_LABEL_CACHE[address]

    label = None
    exe = shutil.which("lspci")
    if exe:
        try:
            proc = subprocess.run([exe, "-mm", "-s", address],
                                  capture_output=True, timeout=10, check=False)
            if proc.returncode == 0:
                fields = re.findall(r'"([^"]*)"', proc.stdout.decode("utf-8", "replace"))
                if len(fields) >= 3:
                    vendor = fields[1].split()[0] if fields[1] else ""
                    device = fields[2].strip()
                    if vendor and device and not device.lower().startswith(vendor.lower()):
                        label = "%s %s" % (vendor, device)
                    else:
                        label = device or None
                if label:
                    label = "%s (%s)" % (label, address)
        except (OSError, subprocess.SubprocessError):
            label = None
    _PCI_LABEL_CACHE[address] = label
    return label


class GpuSensor:
    """One GPU temperature source."""

    def __init__(self, ident, hwmon, driver, label):
        self.id = ident
        self.hwmon = hwmon
        self.driver = driver
        self.label = label
        self.current = None

    def temperature(self):
        value = None
        if self.hwmon:
            raw = _labelled_temp(self.hwmon, ("edge", "junction"))
            if raw is None:
                raw = _max_temp_in(self.hwmon)
            if raw:
                value = raw / 1000.0
        self.current = value
        return value

    def as_dict(self):
        return {
            "id": self.id,
            "label": self.label,
            "driver": self.driver,
            "temperature": self.current,
        }


class NvidiaGpuSensor(GpuSensor):
    """A GPU with no hwmon node, readable only through nvidia-smi."""

    CACHE_SECONDS = 10.0

    def __init__(self, ident, index, label):
        GpuSensor.__init__(self, ident, None, "nvidia-smi", label)
        self.index = index
        self._read_at = 0.0

    def temperature(self):
        now = time.time()
        if now - self._read_at < self.CACHE_SECONDS:
            return self.current
        self._read_at = now
        exe = shutil.which("nvidia-smi")
        if not exe:
            self.current = None
            return None
        try:
            proc = subprocess.run(
                [exe, "-i", str(self.index), "--query-gpu=temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            self.current = None
            return None
        text = proc.stdout.decode("utf-8", "replace").strip()
        self.current = float(text) if text.isdigit() else None
        return self.current


def _nvidia_sensors():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "--query-gpu=index,name", "--format=csv,noheader"],
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    sensors = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        sensors.append(NvidiaGpuSensor("nvidia%s" % parts[0], int(parts[0]), parts[1]))
    return sensors


def list_gpu_sensors():
    """Every GPU that exposes a readable temperature, kernel driver first."""
    sensors = []
    seen = set()
    for path, name in hwmon_nodes():
        if name not in GPU_DRIVERS:
            continue
        address = _pci_address(path)
        ident = address or path
        if ident in seen:
            continue
        seen.add(ident)
        sensors.append(GpuSensor(ident, path, name, _pci_label(address) or name))
    sensors.extend(_nvidia_sensors())
    return sensors


def read_gpu(sensors=None, selected=None):
    """Hottest selected GPU, plus a short description, or ``(None, None)``."""
    sensors = list_gpu_sensors() if sensors is None else sensors
    readings = []
    for sensor in sensors:
        if selected and sensor.id not in selected:
            continue
        value = sensor.temperature()
        if value is not None:
            readings.append((value, sensor.label or sensor.id))
    if not readings:
        return None, None
    value, label = max(readings)
    if len(readings) == 1:
        return value, label
    return value, "%s @ %.0fC, %d GPU(s)" % (label, value, len(readings))


# -- disks ------------------------------------------------------------------ #


def _block_name_for_hwmon(hwmon):
    """Map a hwmon node back to a kernel block device name such as ``sda``."""
    device = os.path.realpath(os.path.join(hwmon, "device"))
    match = re.search(r"/block/([^/]+)$", device)
    if match:
        return match.group(1)
    hits = glob.glob(os.path.join(device, "block", "*"))
    if hits:
        return os.path.basename(sorted(hits)[0])
    match = re.search(r"/nvme/(nvme\d+)$", device)
    if match:
        namespaces = sorted(glob.glob("/sys/class/nvme/%s/%sn*"
                                      % (match.group(1), match.group(1))))
        if namespaces:
            return os.path.basename(namespaces[0])
    return None


def _clean_serial(text):
    """Tidy a kernel serial/wwid string.

    A t10 wwid pads with the two characters ``\0`` (backslash + zero), not with
    NUL bytes, so both forms have to be stripped or the padding shows up in the
    UI as ``\0\0\0\0``.
    """
    if not text:
        return None
    cleaned = text.replace("\x00", " ").replace("\\0", " ")
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def _sysfs_serial(blk):
    """Stable hardware identifier published by the kernel for a block device.

    SATA/SAS expose a ``wwid`` (``naa.…`` or ``t10.…``), NVMe exposes a
    ``serial``.  Either is tied to the drive itself rather than to the order in
    which the kernel happened to enumerate it.
    """
    if not blk:
        return None
    base = "/sys/block/%s/device" % blk
    for name in ("wwid", "serial"):
        cleaned = _clean_serial(read_text(os.path.join(base, name)))
        if cleaned:
            return cleaned
    return None


def _disk_by_id(blk):
    """udev's ``/dev/disk/by-id`` name for a block device, if there is one."""
    if not blk or not os.path.isdir("/dev/disk/by-id"):
        return None
    try:
        entries = os.listdir("/dev/disk/by-id")
    except OSError:
        return None
    matches = []
    for name in entries:
        if name.startswith(("wwn-", "dm-", "lvm-", "md-", "usb-", "virtio-")) \
                or "part" in name:
            continue
        try:
            target = os.path.realpath(os.path.join("/dev/disk/by-id", name))
        except OSError:
            continue
        if os.path.basename(target) == blk:
            matches.append(name)
    if not matches:
        return None
    matches.sort(key=len)
    return matches[0]


#: udev by-id prefixes whose remainder reads as "model_serial".
_BY_ID_PREFIXES = ("ata-", "scsi-", "sata-", "nvme-", "mmc-")


def _disk_identity(blk):
    """Model and serial for a block device, for display next to its name.

    ``/dev/disk/by-id`` names carry the full model and serial (sysfs truncates
    the model to 16 characters), so they make the friendliest label.  EUI-only
    names are skipped in favour of model + serial, which is readable.
    """
    if not blk:
        return blk

    by_id = _disk_by_id(blk)
    if by_id and "eui." not in by_id and "wwn-" not in by_id:
        text = by_id
        for prefix in _BY_ID_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        text = text.replace("_", " ").strip()
        if text:
            return text

    model = (read_text("/sys/block/%s/device/model" % blk) or "").strip()
    serial = _sysfs_serial(blk) or ""
    if len(serial) > 28:                       # a padded t10 wwid
        serial = serial.split()[-1]
    parts = [part for part in (model, serial) if part]
    return " · ".join(parts) if parts else blk


def smartctl_temperature(device):
    """Read a disk temperature through smartctl without waking a sleeping disk."""
    exe = shutil.which("smartctl") or "/usr/sbin/smartctl"
    if not os.path.exists(exe):
        return None
    try:
        proc = subprocess.run(
            [exe, "-n", "standby", "-A", "-j", "/dev/%s" % device],
            capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # smartctl sets bit 1 of the exit status when -n standby skipped the device
    if proc.returncode & 0x02:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None
    current = (data.get("temperature") or {}).get("current")
    if isinstance(current, int):
        return current
    for row in data.get("ata_smart_attributes", {}).get("table", []):
        if row.get("name") in ("Temperature_Celsius", "Airflow_Temperature_Cel"):
            raw = (row.get("raw") or {}).get("value")
            if isinstance(raw, int):
                return raw
    return None


class DiskSensor:
    """One disk temperature source.

    ``id`` is the stable identity (``/dev/disk/by-id`` name, or the kernel's
    wwid/serial) and is what gets stored in the configuration.  ``device`` is
    only the current kernel name (``sda``), which changes when disks are
    re-cabled or the boot order changes -- selecting by that would silently
    start following a *different* drive.
    """

    def __init__(self, device, hwmon, driver, label, ident=None):
        self.device = device
        self.hwmon = hwmon
        self.driver = driver
        self.label = label
        self.id = ident or _disk_by_id(device) or _sysfs_serial(device) or device or hwmon
        self.current = None

    @property
    def kind(self):
        return "nvme" if self.driver == "nvme" else "sata"

    def temperature(self):
        """Read the temperature, caching it on the instance.

        The cache matters: the ``smartctl`` path forks a process, so the UI
        must never trigger a fresh read on its own polling loop.
        """
        value = None
        if self.hwmon:
            raw = read_int(os.path.join(self.hwmon, "temp1_input"))
            if raw:
                value = raw / 1000.0
        if value is None and self.device:
            value = smartctl_temperature(self.device)
        self.current = value
        return value

    def as_dict(self):
        return {
            "id": self.id,
            "device": self.device,
            "label": self.label,
            "kind": self.kind,
            "driver": self.driver,
            "temperature": self.current,
        }


def list_disk_sensors():
    """Every disk that exposes a temperature, hwmon first."""
    sensors = []
    seen = set()
    for path, name in hwmon_nodes():
        if name not in DISK_DRIVERS:
            continue
        device = _block_name_for_hwmon(path)
        if device in seen:
            continue
        seen.add(device)
        sensors.append(DiskSensor(device, path, name, _disk_identity(device)))

    if shutil.which("smartctl") or os.path.exists("/usr/sbin/smartctl"):
        for path in sorted(glob.glob("/sys/block/*")):
            device = os.path.basename(path)
            if device in seen or device.startswith(
                ("loop", "zd", "dm-", "md", "ram", "sr")
            ):
                continue
            if not os.path.isdir(os.path.join(path, "device")):
                continue
            sensors.append(DiskSensor(device, None, "smartctl", _disk_identity(device)))
    sensors.sort(key=lambda sensor: sensor.device)
    return sensors


def read_hdd(sensors=None, selected=None):
    """Highest disk temperature among ``selected`` device names (or all disks).

    Every sensor passed in is refreshed, so the returned detail doubles as the
    cache the web UI reads back from.
    """
    sensors = list_disk_sensors() if sensors is None else sensors
    readings = []
    for sensor in sensors:
        value = sensor.temperature()
        # Accept the current device name too, so a configuration written before
        # selections became stable keeps working until it is migrated.
        if selected and sensor.id not in selected and sensor.device not in selected:
            continue
        if value is not None:
            readings.append((value, sensor.device))
    if not readings:
        return None, None
    value, device = max(readings)
    return value, "%s @ %.0fC, %d disk(s)" % (device, value, len(readings))


# --------------------------------------------------------------------------- #
# PWM-capable nodes we are not driving
# --------------------------------------------------------------------------- #


def detect_other_controllers():
    """PWM-capable hwmon nodes that are neither the active controller nor
    something we should touch (GPU fans, disk bays, CPU drivers)."""
    active = find_controller()[0]
    found = []
    for path, name in hwmon_nodes():
        if path == active or not has_pwm_attribute(path):
            continue
        if name in GPU_DRIVERS or name in DISK_DRIVERS or name in CPU_DRIVERS:
            continue
        profile = profile_for(name)
        found.append(
            {
                "hwmon": path,
                "name": name,
                "supported": False,
                "known": profile is not None,
                "fans": len(glob.glob(os.path.join(path, "fan*_input"))),
            }
        )
    return found
