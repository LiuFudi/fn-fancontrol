<div align="center">

# fn-fancontrol · Fan control for fnOS (FeiNiu NAS)

**Drive your chassis and CPU fans from CPU, GPU and disk temperatures**

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-x86-lightgrey.svg)](#compatibility)
[![fnOS](https://img.shields.io/badge/fnOS-%E2%89%A51.1.3100-green.svg)](https://www.fnnas.com/)
[![Version](https://img.shields.io/badge/version-1.3.0-orange.svg)](CHANGELOG.md)

[简体中文](README.md) · [English](README.en.md)

</div>

---

## Why

Off-the-shelf NAS fan policies usually offer three presets — quiet / balanced / full —
and they only look at the **CPU** temperature.

But the CPU is rarely what suffers. WD recommends keeping hard drives below 50 °C, yet
on many machines the disks sit at 55 °C while the CPU idles, simply because the chassis
fan never considers them.

This app lets you **drive the chassis fan from disk temperature** while the CPU fan keeps
following the CPU — two independent curves. The design is inspired by
[FanControl](https://github.com/Rem0o/FanControl.Releases), deliberately reduced to the
essentials: **temperature sources + control curves + persisted configuration**.

## Features

| | |
|---|---|
| 🌡️ **Three source types** | CPU (coretemp / k10temp / PECI), GPU (amdgpu / i915 / xe / nouveau / nvidia-smi), disks (drivetemp / nvme, `smartctl` fallback) |
| 🔍 **Hardware wizard** | On first launch it lists **every** PWM channel the controller exposes, with live RPM and the temperature source the BIOS bound to it; a spin-up probe finds fans that were stopped |
| 📈 **Visual curves** | One curve per fan, edited by dragging points in the browser, 2–8 points |
| 🎛️ **Modes** | Temperature curve / fixed duty / BIOS automatic, per fan |
| 🔀 **Max-of-many** | A fan can bind several sources and follows the hottest one |
| 🐢 **Hysteresis** | The fan only slows down after the temperature drops far enough, avoiding constant ramping |
| 💾 **Disk friendly** | Disks are polled on their own (longer) interval to avoid waking sleeping drives; the `smartctl` path uses `-n standby` and never spins a disk up |
| 🛡️ **Fail-safe** | If every source fails, the fan goes to a configurable safe duty and the UI warns you |
| ↩️ **Hands back safely** | Stopping or uninstalling returns the fans to the BIOS; removing a fan from the config restores that header to its pre-takeover state |
| 🪶 **Zero dependencies** | Backend is pure Python 3 standard library, frontend is plain JS with no build step — no nodejs / python312 runtime app required |

## Screenshots

> 📷 *TODO — contributions welcome.*

<!--
![Overview](docs/screenshots/overview.png)
![Curve editor](docs/screenshots/curve.png)
-->

The UI has three sections: **temperature sources** (CPU / GPU / disks, with per-disk
selection), **fans** (one card each: RPM, duty, driving temperature, mode, sources,
min/max duty and a draggable curve), and **global settings** (poll interval, disk poll
interval, hysteresis, fail-safe duty).

## Installation

### Requirements

- fnOS (`os_min_version ≥ 1.1.3100`, tested on `1.2.0604`)
- x86 board whose fans hang off a supported controller (see [Compatibility](#compatibility))
- An **administrator** account (installing third-party apps requires one)

### From a Release

1. Download `fn-fancontrol-<version>.fpk` from [Releases](../../releases) (or use the copy in `dist/`)
2. fnOS **App Center → manual install**, pick the `.fpk`, choose a volume
3. Open **Fan Control**

Or from the shell:

```bash
sudo appcenter-cli install-fpk fn-fancontrol-1.2.0.fpk -v 3   # -v is the volume index
sudo appcenter-cli start fn-fancontrol
```

> ⚠️ `appcenter-cli install-fpk` does **not** upgrade an already-installed app of the same
> name — uninstall first (or use the App Center upgrade entry). Uninstalling keeps
> `config.json`, so your curves survive a reinstall.

## Usage

1. **Check the sources** — the disk section lists every disk whose temperature can be read;
   untick the ones that should not take part
2. **Verify the header mapping** — the first install guesses from the BIOS configuration
   (headers watching a CPU core become CPU fans, the rest become chassis fans), but please
   **confirm which card is which physical header** by loading the CPU and watching which
   fan speeds up
3. **Tune the curve** — drag the dots, double-click empty space to add a point,
   right-click a dot to delete it
4. **Save** — applied immediately and written to disk

> 💡 **Suggested starting point**: chassis fan → `disks`, CPU fan → `CPU`.
> A disk curve of `40 °C → 30 %`, `48 °C → 60 %`, `55 °C → 100 %` works well.

## Compatibility

Whether it works depends on **which controller the fans hang off, and whether its kernel
driver is supported here**. Each driver family uses **different `pwm_enable` values** —
writing the wrong one can hand fan control back to the BIOS or disable it altogether — so
every family has its own profile, taken from the kernel's `Documentation/hwmon/*.rst`:

| Controller family | Kernel driver | Manual | Auto | Status |
| --- | --- | :---: | :---: | --- |
| Nuvoton NCT6775 / 6776 / 6779 / 679x / 6106 | `nct6775` | `1` | `5` Smart Fan IV | ✅ tested |
| ITE IT87xx (IT8603E … IT87952E) | `it87` | `1` | `2` * | ✅ supported |
| Fintek F718xx / F8000 / F81865F | `f71882fg` | `1` | `2` | ✅ supported |
| Fintek F71805F / F71872F | `f71805f` | `1` | `2` | ✅ supported |
| Winbond W83627EHF / DHG / UHG / W83667HG | `w83627ehf` | `1` | `2` | ✅ supported |
| Winbond W83627HF / THF / W83697HF | `w83627hf` | `1` | `2` | ✅ supported |
| SMSC SCH5627 / SCH5636 | `sch5627` / `sch5636` | `1` | `2` | ✅ supported |
| **Nuvoton NCT6683 / 6686 / 6687** | `nct6683` | — | — | ⚠️ **monitor only** |
| Any other hwmon node exposing `pwmN` | any | `1` | `2` | ⚠️ generic fallback, unverified |
| ACPI `PNP0C0B` fan objects | — | — | — | ❌ DSDT stubs |
| Fans managed by an EC (laptops, some mini PCs) | — | — | — | ❌ no PWM node |

> Apart from the NCT6775 family, these profiles come from the kernel documentation and
> have **not been verified on real hardware**. If you own an ITE / Fintek / Winbond board,
> feedback is very welcome — see [Contributing](#contributing).

Development and test platform:

```
Board     ASUSTeK TUF B365M-PLUS GAMING (B365, LGA1151)
CPU       Intel CC150  8C/16T
Chip      Nuvoton NCT6796D @ 0x2e:0x290
Headers   6 PWM channels, 2 with a fan attached
System    fnOS 1.2.0604 / kernel 6.18.18.c1032-trim
```

### Two things worth knowing

**1. ITE automatic mode only works on old silicon.** The `it87` driver implements
"Smart Guardian" only for IT8705F ≤ rev F and IT8712F ≤ rev G; newer chips reject a `2`.
The app therefore falls back step by step: restore the recorded pre-takeover state →
try the driver's automatic mode → otherwise **go to full speed**. That last step matters:
without it, stopping the app on an ITE board that cannot select an automatic mode would
leave the fan stranded on the last duty it was given.

**2. ITE boards often need one extra step.** The `it87` driver may refuse to take over
because ACPI already claims the SuperIO I/O ports (`ACPI: resource conflict` in `dmesg`):

```bash
sudo modprobe it87 ignore_resource_conflict=1   # affects only this driver
# or add acpi_enforce_resources=lax to the kernel command line (broader)
```

**Why NCT6683 / 6686 / 6687 are monitor-only**: the kernel `nct6683` documentation states
that on Intel EC firmware the register layout does not match the Nuvoton datasheet, and
**writing any value from the OS is considered too risky and has been disabled** — the
driver simply does not expose a writable `pwmN`. Boards such as ASRock B650/X670E and
MSI B550/X670 are detected and reported honestly as monitor-only.

## Building from source

Requires the official `fnpack` tool, which ships with fnOS at `/usr/local/bin/fnpack`.

```bash
git clone https://github.com/LiuFudi/fn-fancontrol.git
cd fn-fancontrol
./build-fpk.sh                     # produces dist/fn-fancontrol-<version>.fpk
```

`build-fpk.sh` reads `appname`/`version` from `package/manifest`, cross-checks the `VERSION`
constant inside the daemon, runs `fnpack build`, renames the artifact to
`<appname>-<version>.fpk` in `dist/`, and verifies the version inside the finished package
matches its filename.

Icons can be regenerated with pure Python (no imaging library needed):

```bash
python3 tools/make_icons.py
```

## How it works

```
browser iframe
   └─ fnOS gateway /app/fn-fancontrol     ← validates the session, forwards X-Trim-Isadmin
        └─ Unix socket  package/target/app.sock   ← no TCP port is ever opened
             └─ fancontrold.py (runs as root)
                  ├─ control loop: read temps → interpolate curve → write /sys/class/hwmon/*/pwmN
                  └─ HTTP thread: static UI + /api/*
```

Every `interval` seconds the control loop reads the enabled sources, takes the hottest
value among each fan's bound sources, interpolates the curve, clamps the result into
`min_duty`..`max_duty`, and writes `pwmN` **only when the value actually changes**. Speeding
down additionally requires passing the hysteresis check.

**Why root**: writing `/sys/class/hwmon/*/pwm*` (`0644 root:root`), `modprobe nct6775` and
reading SMART data all need it. That privilege belongs to the *application*, not the user —
install once and the daemon stays root; day-to-day tuning is web-only, no sudo, no shell.

## Configuration

Stored as JSON in the app config directory (`/volN/@appconf/fn-fancontrol/config.json`) and
written atomically. Hand edits are normalised on the next save.

```jsonc
{
  "version": 1,
  "enabled": true,          // master switch; false hands everything back to the BIOS
  "interval": 3,            // control period, seconds
  "hdd_interval": 60,       // disk poll interval, seconds (raise it to wake disks less)
  "hysteresis": 3,          // °C
  "fail_safe_duty": 255,    // duty used when every source fails
  "sources": {
    "cpu": true,
    "gpu": false,
    "hdd": true,
    "hdd_devices": []       // empty = hottest of all disks
  },
  "fans": [
    {
      "channel": 2,         // SuperIO channel
      "name": "CPU_FAN",
      "mode": "curve",      // curve | manual | auto
      "source": ["cpu"],    // several allowed, hottest wins
      "points": [[30, 20], [45, 35], [60, 60], [75, 100]],   // [°C, percent]
      "min_duty": 60,
      "max_duty": 255,
      "manual_duty": 128
    }
  ]
}
```

## HTTP API

A small JSON API over a Unix socket, authenticated and proxied by the fnOS gateway:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ping` | liveness probe |
| GET | `/api/status` | sources, live fan state, current config |
| GET | `/api/config` | config only |
| GET | `/api/hardware` | detected SuperIO channels and disks |
| POST | `/api/config` | validate → persist atomically → apply |
| POST | `/api/action` | `{"action":"restore-auto"}` / `{"action":"refresh-hardware"}` |

The daemon also has standalone subcommands for troubleshooting (all need root):

```bash
python3 package/app/server/fancontrold.py status      --appdest <dir> --etc <dir>
python3 package/app/server/fancontrold.py init-config --appdest <dir> --etc <dir> [--force]
python3 package/app/server/fancontrold.py restore     --appdest <dir> --etc <dir>
python3 package/app/server/fancontrold.py run --host 127.0.0.1 --port 8099   # local debug
```

## Safety design

| Situation | Behaviour |
|---|---|
| Stop / uninstall | Before exiting, the daemon writes `pwmN_enable=5` (BIOS auto) to the channels it **actually drove**; headers it never managed are left untouched |
| Daemon killed hard | `cmd/main` runs a blanket restore when it had to `kill -9` |
| Fan removed from config | That header is restored to its **recorded pre-takeover state** (enable + duty) |
| All sources fail | Fan goes to `fail_safe_duty` (full speed by default) and the UI warns |
| Non-administrator | The server checks the gateway's `X-Trim-Isadmin` header and returns `403` unless it is true (fail-closed) |
| Other local accounts | The app socket is `0600 root:root` — the gateway cannot be bypassed; access control stays with fnOS login |
| Path traversal | Static file serving validates the resolved path stays inside the UI directory |

## FAQ

<details>
<summary><b>The UI says no fan controller was found</b></summary>

Check that the driver loaded:

```bash
sudo modprobe nct6775
dmesg | grep -iE "nct|it87|f718|w836|sch56"   # was the chip recognised?
cat /sys/class/hwmon/*/name                    # which nodes did drivers register?
ls /sys/class/hwmon/*/name | xargs cat | grep -i nct
```

If `dmesg` shows `ACPI: resource conflict`, ACPI has claimed the SuperIO I/O ports and you
need `acpi_enforce_resources=lax` on the kernel command line, then reboot.

If the UI names a chip such as `it87`, that model simply is not supported yet — see
[Compatibility](#compatibility).
</details>

<details>
<summary><b>RPM reads fine but changing PWM does nothing</b></summary>

The channel may be in automatic mode and the driver refuses the override, or the BIOS has
locked fan control. Try switching that fan to the "temperature curve" mode in the UI and
watch whether the duty value changes.
</details>

<details>
<summary><b><code>pwmN_enable</code> does not read back what was written</b></summary>

A known driver quirk. On NCT6796D, writing `1` (manual) reads back as `0`. The app therefore
does **not** rely on the readback: it tracks the mode it applied itself, and the UI shows the
app's effective mode.
</details>

<details>
<summary><b>A 4-pin fan reports 0 RPM</b></summary>

Either no fan is plugged into that header, or the fan has no tachometer wire. The default
configuration only includes headers that report a tach signal; if a fan was stopped at scan
time and got missed, add it back with the "add this channel" control and use the card's
"remove" button if you change your mind.
</details>

<details>
<summary><b>I worry about waking my disks</b></summary>

Raise **Global settings → disk temperature interval** (e.g. 300 s). Disks that expose a
`drivetemp` / `nvme` hwmon node are read through plain sysfs; only disks without one fall back
to `smartctl -n standby`, which by design does not wake a sleeping drive.
</details>

## Repository layout

```
fn-fancontrol/
├── LICENSE                   MIT
├── README.md / README.en.md
├── CHANGELOG.md
├── build-fpk.sh              build + version-stamped artifact name
├── package/                  fnpack source tree (packaged into the fpk)
│   ├── manifest
│   ├── ICON.PNG / ICON_256.PNG
│   ├── app/
│   │   ├── server/           daemon (standard library only)
│   │   │   ├── fancontrold.py    control loop + HTTP API + static files
│   │   │   ├── fanhardware.py    SuperIO / hwmon / temperature sources
│   │   │   └── fanconfig.py      config validation + curve interpolation
│   │   └── ui/               frontend (plain JS)
│   ├── cmd/                  lifecycle scripts
│   └── config/               privilege / resource
├── tools/make_icons.py       pure-Python icon generator
└── dist/                     build output
```

## Contributing

Issues and PRs are welcome, especially:

- **Hardware coverage** — if you have an ITE / Fintek / other SuperIO board, please share
  `dmesg`, `ls /sys/class/hwmon/*/name` and `sensors` output so support can be evaluated
- **Screenshots** — they would make this README much more complete
- **English proofreading** — this file

## Disclaimer

This software writes directly to the SuperIO PWM registers. The author designed it to fail
safely (fail-safe duty, handing fans back on stop, minimum duty limits), but **accepts no
liability for hardware damage or data loss** arising from its use. Evaluate the risk
yourself, especially on a board that has not been verified.

## License

[MIT License](LICENSE) © 2026 [LiuFudi](https://github.com/LiuFudi)
