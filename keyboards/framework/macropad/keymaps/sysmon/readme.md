# sysmon — system monitor lighting for the Framework 16 RGB Macropad

Turns the macropad's six key rows into live bar graphs, driven by a small
host-side Python script over the VIA raw HID endpoint (VIA keeps working
normally — the stats ride on a private custom-value channel, `0x42`):

| Row | Indicator | Colors |
|-----|-----------|--------|
| 1 (top, small keys) | Battery | charging: level bar (red→green by level), segments from the filling one upward pulse in sync; discharging: level bar (red unplugged, amber when the battery is assisting an overloaded charger on AC), segments from the boundary downward flare and fade in sync, blinks below 15%; the charge/discharge power decides how many segments animate (1 segment for a trickle up to all 4 at ≥75 W); idle on AC: whole row steady dim green — a sub-10 W drain counts as idle, and if the EC is measurably drifting off the cap the boundary segment breathes faintly as a tell (a 0 W "discharging" report, which is what the EC says while it floats a pack it is done charging, is plain idle: no tell); the bar is rescaled against the max-charge cap, so charged-to-cap shows completely full |
| 2 | CPU usage | red bar; breathes when the APU is power-limited (see below) |
| 3 | Memory usage | gold bar; whole row blinks at ~1 Hz when usage is critically high (≥90%) |
| 4 | iGPU + NPU usage | red bar for the iGPU; NPU usage as a dim cyan *ghost* bar behind it, visible where the NPU is busier than the iGPU (see below) |
| 5 | dGPU usage | green bar (dark if no dGPU); breathes when the dGPU is power-limited (see below) |
| 6 | System temperature | fixed green→deep-red gradient across the row; bar length scales 30–90 °C; whole row blinks at ~1 Hz above 90 °C |

## NPU on the iGPU row

The NPU (the XDNA "Ryzen AI" engine on the HX 370) shares the iGPU row rather
than taking a row of its own: it sits on the same die and is idle most of the
time. The iGPU keeps its bright red bar; the NPU is a dim cyan bar drawn
*behind* it, so:

- NPU idle → the row is exactly the iGPU bar it always was;
- NPU busier than the iGPU → cyan segments extend past the end of the red
  bar, up to the NPU's level;
- iGPU busier than the NPU → the ghost is hidden under the red bar.

A segment the two bars share mixes both colours in proportion to how much of
it each one covers.

The NPU is found by what the kernel says it is — a *compute-only* adapter
(`D3DKMT_ADAPTERTYPE.ComputeOnly`, the same flag Task Manager keys its NPU graph
on), so no name matching is involved — and its utilization is read from the
same PDH `GPU Engine` counters as the GPUs, scoped to the NPU's LUID. On a
machine without an NPU the row is just the iGPU bar. Drawing the ghost needs the
firmware from this version (protocol FRAME C, below); older firmware ignores it
and shows the plain iGPU bar.

## Power-limit indication

The CPU (row 2, whole APU) and dGPU (row 5) rows **breathe** when the component
is running at reduced capacity because of a *power* limit. The bar stays
visible (it dips to a dim floor, not off — that's reserved for the critical
blinks), and the cadence says which kind of limit:

- **Brisk breathe (~1.5 s)** — the chip is at its own power/TDP ceiling on AC
  (chip-bound).
- **Slow breathe (~4 s)** — the *power source* is the bottleneck: the battery
  is assisting an overloaded/undersized charger on AC, so a loaded component is
  being throttled to fit. This pairs with the battery row turning **amber** in
  the same situation (the battery row says the charger is overloaded; the
  component breathe says which component it's choking). Plain battery operation
  is *not* shown here — the battery row's discharging colour already conveys it.

Thermal throttling is intentionally not shown here — the temperature row's
>90 °C blink already covers it.

Detection sources (per component, degrades gracefully if absent):

- **dGPU** — NVIDIA only, via **NVML** (`pip install pynvml`): authoritative
  throttle reasons (`SwPowerCap`, `HwPowerBrakeSlowdown`) plus the enforced
  power limit. No NVIDIA card / no pynvml → no dGPU limit indication.
- **APU (CPU/iGPU)** — read **authoritatively** from the AMD SMU power-metrics
  table via **`ryzenadj`** (its `libryzenadj.dll`, loaded in-process). This is
  the same source HWiNFO reads for *APU STAPM Limit* etc.: the CPU row breathes
  when a power limit is *binding* — its live value has reached the limit. STAPM
  (the skin-temperature-aware sustained budget) and the slow PPT are checked
  (the short fast-PPT boost window is read for diagnostics but not used to
  trigger, so brief boosts don't flicker — see `APU_LIMIT_SIGNALS`). Install
  the tool (`scoop install ryzenadj`); the DLL is auto-discovered, or point
  `--ryzenadj-dir` at it. It loads a ring0 driver, so the script must be
  **elevated**.
  - *Fallback (no ryzenadj):* AMD exposes no throttle flags, so the cap is
    *inferred* from **LibreHardwareMonitor** package power + CPU load — flagged
    when, under sustained high load on AC, package power plateaus at the
    ceiling (auto-calibrated; pass `--cpu-power-cap <watts>` to pin it). With
    neither ryzenadj nor LHM the CPU row never breathes.

LED brightness follows the laptop screen brightness (scaled, capped well
below full brightness). If no stats arrive for 5 seconds (script stopped,
reboot, etc.) the macropad falls back to the regular RGB matrix effect, so
set a calm/dim effect via VIA as the fallback.

## Install

Download `FW16-Sysmon-Setup-<version>.exe` and run it. It bundles everything —
there is no Python to install, no `pip`, no `scoop`.

The installer:

- checks the machine first and tells you what will and will not work here
  (macropad attached? AMD or Intel? Memory Integrity blocking the sensor
  driver? `framework_tool` present?), and offers to install the one optional
  dependency it can fetch for you (`framework_tool`, via winget);
- installs the host as a **Windows service running as LocalSystem, set to start
  at boot** — the bars are live at the login screen, not just after you sign in;
- optionally **flashes the bundled firmware** onto the macropad (tick the box on
  the "After installing" page, or run *Flash firmware onto the macropad* from
  the Start menu later);
- writes a fully commented `sysmon.ini` you can retune without rebuilding
  anything;
- removes the old `FW16 Macropad Sysmon` scheduled task if you were running the
  mod that way. This matters: the macropad has a single raw-HID endpoint, and
  two processes writing it fight over it.

The installer is **not code-signed**, so SmartScreen shows
"Windows protected your PC" on first run — *More info* → *Run anyway*.

### After installing

| Where | What |
| ----- | ---- |
| `C:\Program Files\FW16 Macropad Sysmon\` | the program and its bundled DLLs |
| `C:\ProgramData\FW16Sysmon\sysmon.ini` | every tunable (see [Tuning](#tuning-no-reflash)) |
| `C:\ProgramData\FW16Sysmon\sysmon.log` | rotating service log |
| Start menu | self-test, live console view, edit settings, view log, flash firmware, restart service |

Managing the service:

```powershell
sc query FW16Sysmon
sc stop FW16Sysmon
sc start FW16Sysmon          # also: the "Restart the service" Start-menu item
```

If a row is dark and you want to know why, run the **self-test** — it probes
every sensor and dependency and prints a remedy for anything missing:

```powershell
fw16-sysmon.exe --selftest
```

## Known limitations

sysmon degrades one row at a time rather than refusing to run, so most of these
cost you a single indicator:

- **Memory Integrity / the vulnerable-driver blocklist.** Both the AMD SMU
  reader (`libryzenadj`) and LibreHardwareMonitor get at the CPU through the
  WinRing0 ring0 driver, which Windows blocks when Core Isolation → Memory
  Integrity is on. You lose the CPU-row power-limit breathe and the in-process
  temperature; temperature still comes from the EC via `framework_tool`.
  `--selftest` says plainly when this is what is happening.
- **Intel Framework 16s.** No AMD SMU, so no `ryzenadj` path; CPU power and
  temperature come from LibreHardwareMonitor instead. Everything else is
  unchanged.
- **No dGPU / no NVIDIA card.** Row 5 stays dark by design. An AMD dGPU still
  shows *usage* (via the PDH counters) but not the power-limit breathe, which
  needs NVML.
- **`framework_tool` not installed.** The battery bar is scaled to 100% instead
  of the EC's charge cap, and one temperature fallback is unavailable. Optional.

## Build and flash the firmware

```sh
qmk compile -kb framework/macropad -km sysmon
```

On Windows with QMK MSYS: its GNU Make 4.4.1 can't parse QMK's
`keyboard:keymap` targets in this firmware vintage, so invoke the build
makefile directly (this is what worked here):

```powershell
$env:MSYSTEM='MINGW64'
& C:\QMK_MSYS\usr\bin\bash.exe -l -c "cd /c/Users/Msprg/repo/framework_qmk_firmware && make -r -R -f builddefs/build_keyboard.mk -j8 KEYBOARD=framework/macropad KEYMAP=sysmon TARGET=framework_macropad_sysmon QMK_BIN=qmk SILENT=false"
```

Flashing is one command — it jumps to the bootloader, waits for the `RPI-RP2`
drive, copies the image and waits for the module to come back (pausing the
service around it, since both want the same raw-HID endpoint):

```powershell
fw16-sysmon.exe --flash                       # the bundled image
fw16-sysmon.exe --flash path\to\custom.uf2    # or your own build
```

The bootloader jump is the same mechanism Framework's official updater uses:
VIA command `0x0B` padded with `0xFE`. If the module is not attached, or you
would rather do it by hand, hold the **top-left + bottom-left keys while
inserting the module** and copy the `.uf2` onto the drive that appears.
`--bootloader` still exists if you only want the jump.

## Building the installer

```powershell
host\packaging\build.ps1                 # -Clean, -BuildFirmware, -SkipVendor
```

One command, from a clean checkout to `host\dist\FW16-Sysmon-Setup-<ver>.exe`:
it creates an isolated build venv, downloads the redistributable native
libraries (`libryzenadj` + WinRing0, LibreHardwareMonitorLib — see
[packaging/THIRD-PARTY-NOTICES.md](host/packaging/THIRD-PARTY-NOTICES.md)),
freezes the host with PyInstaller, smoke-tests the result and wraps it with
Inno Setup (installing Inno via winget if it is missing). `-BuildFirmware`
rebuilds the `.uf2` through QMK MSYS first; otherwise the existing
`.build\framework_macropad_sysmon.uf2` is reused.

The bundle is deliberately **onedir**, not onefile: this runs as a boot-start
service, so a self-extracting bundle would pay a ~30 MB unpack on every boot,
and — more importantly — WinRing0 loads its kernel driver from a path relative
to the running executable, which only a stable directory provides.

## Running the host from source (development)

Only needed to hack on the host itself — end users install the packaged build.

```powershell
pip install hidapi psutil wmi pynvml pythonnet
host\packaging\fetch-vendor.ps1           # libryzenadj + WinRing0 -> vendor\, LHM -> lib\
host\dev.ps1                              # the iteration loop
```

`dev.ps1` is the fast path, because three things otherwise have to be
remembered every single time:

- the macropad has **one** raw-HID endpoint, so the installed service has to be
  stopped or it fights the source run — `dev.ps1` stops it and restarts it on
  exit, **including on Ctrl+C**, which is how a live view normally ends;
- the ring0 sensors need elevation and `.venv\Scripts\python.exe` is manifested
  `requireAdministrator`, so it cannot be launched from a normal shell at all —
  `dev.ps1` self-elevates rather than failing with `code 740`;
- a source run resolves the firmware image to `.build\framework_macropad_sysmon.uf2`,
  so `-Flash` always flashes what was just built.

```powershell
host\dev.ps1                       # live view (--verbose)
host\dev.ps1 -Build -Flash         # rebuild firmware via QMK MSYS, then flash it
host\dev.ps1 -SelfTest             # probe sensors from the source tree
host\dev.ps1 -- --interval 1.0     # anything after -- goes to fw16_sysmon.py
```

Or run the script directly, if you would rather manage the service yourself:

```powershell
python host\fw16_sysmon.py --verbose
```

(`pynvml` enables the NVIDIA dGPU power-limit breathe; `libryzenadj` enables the
authoritative AMD APU power-limit breathe and an in-process temperature source;
`pythonnet` + the LHM DLLs enable the LibreHardwareMonitor APU package-power +
temperature path. All optional; the core bars work without them. The AMD limit
and temperature sources load a ring0 driver, so run the script **elevated**.)

A source checkout keeps everything local — `sysmon.ini` and `sysmon.log` sit
next to the script rather than in `%ProgramData%`, so a checkout never collides
with an installed copy. The layout rules live in
[`host/paths.py`](host/paths.py).

The service can be exercised before packaging too: `--install-service` registers
the interpreter plus the script, so `sc start FW16Sysmon` runs the working tree.

Useful flags:

- `--max-led 150` — LED brightness at 100% screen brightness (0–255)
- `--min-led 8` — LED brightness at 0% screen brightness
- `--interval 1.0` — update period in seconds
- `--igpu-match / --dgpu-match` — regexes that classify GPU adapters by
  name (run with `--verbose` to see the detected adapters; defaults handle
  AMD iGPU + NVIDIA or Radeon RX dGPU)
- `--cpu-power-cap 54` — APU sustained power ceiling (watts) for the *fallback*
  package-power heuristic; auto-calibrated (approximately) if unset (ignored
  when ryzenadj is providing the authoritative limit)
- `--no-nvml` — disable NVIDIA dGPU power/throttle detection
- `--no-ryzenadj` — disable the authoritative libryzenadj APU STAPM/PPT limit +
  Tctl source (falls back to the LHM package-power heuristic)
- `--ryzenadj-dir DIR` — directory holding `libryzenadj.dll` + `WinRing0x64.sys`
  (the bundled copy beside the executable is used first, then a `ryzenadj`
  install / scoop shim)
- `--fetch-lhm` — download LibreHardwareMonitorLib + its deps into `lib/` and
  exit; add `--force` to refresh files that are already there
- `--no-lhm-lib` — disable the in-process LibreHardwareMonitorLib APU source

Deployment flags (also used by the installer):

- `--selftest` — probe every sensor and dependency, print a remedy for anything
  missing; `--json` emits the same as machine-readable output
- `--flash [UF2]` — bootloader jump, wait for `RPI-RP2`, copy, wait for the
  module to return (defaults to the bundled image)
- `--install-service` / `--uninstall-service` / `--start-service` /
  `--stop-service` — manage the LocalSystem boot-start service
- `--config PATH` — use a specific tunables INI
- `--write-default-config` — write the fully commented default INI (never
  overwrites an existing one)

The **AMD APU power-limit breathe** comes from `ryzenadj`/`libryzenadj` (the SMU
table; authoritative) with the LibreHardwareMonitor package-power heuristic as
fallback — see [Power-limit indication](#power-limit-indication) above. With
`--verbose`, the `limits …` line shows either `cpu ryzenadj[stapm=…/…W slow=…/…W
…]` (a `*` marks a binding limit) or `cpu heuristic(pkg_w=… load=…)`, so you can
confirm which source is driving the CPU row.

**Temperature** comes from one of (tried in this order):

- **LibreHardwareMonitorLib in-process** (recommended) — `pip install pythonnet`,
  then run `python host\fw16_sysmon.py --fetch-lhm` once to download the engine
  DLLs into `lib/` (gitignored). The script then reads CPU package power, load
  and temperature directly — no separate app and no WMI provider. It loads a
  ring0 driver to read the CPU, so the script must run **elevated**; if Windows
  *Core Isolation → Memory Integrity* is enabled it may block that driver (the
  same limitation the LHM app hits). Disable with `--no-lhm-lib`.
- [framework_tool](https://github.com/FrameworkComputer/framework-system)
  on PATH (`winget install FrameworkComputer.framework_tool`) — the EC's own APU
  sensor, throttled to one subprocess every few seconds. This is deliberately
  ranked **above** ryzenadj Tctl: Tctl is decoded from the AMD SMU power table,
  whose layout depends on the CPU model, and a wrong decode does not fail — it
  returns a plausible-looking number. On a clean Windows install on the same
  FW16, Tctl sat at exactly 100 °C (an unpopulated field) while the EC read
  41 °C, pinning the temperature row full and blinking. Needs elevation.
- **ryzenadj Tctl** — when `ryzenadj` is installed for the power-limit breathe,
  its die temperature is read from the same in-process SMU handle for free, so
  ryzenadj alone is enough to light the temperature row on a machine with
  neither LibreHardwareMonitor nor framework_tool. Also elevated-only. Run
  `--selftest` to see what every source reports and which one is being used.
- the [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
  **app** running with its WMI provider enabled — fallback when the in-process
  lib isn't set up; same CPU package-power sensor.
- a working ACPI thermal zone — last resort; the FW16 exposes zones that
  *look* valid but report frozen values (a constant ~63 °C), and reading them
  needs admin rights anyway. Temperature stays dark if no source provides data.

### Starting automatically

The installer registers a **LocalSystem service set to Automatic start**, so the
macropad is live at the login screen rather than after sign-in. Running as
LocalSystem also means the process is already elevated, which is what the ring0
sensors need — the old Task Scheduler job had to ask for "highest privileges" to
get the same.

The service stops cleanly on shutdown and handles resume from sleep (the module
re-enumerates and the SMU table mapping does not reliably survive S0ix, so both
are rebuilt). Nothing is sent on the way out: the firmware's own no-data timeout
restores the stock RGB effect a few seconds later, which is also what happens on
a crash or a yanked module, so there is only one fallback path to reason about.

If you would rather not use a service, `fw16-sysmon.exe` still runs perfectly
well in the foreground, and a Task Scheduler *At log on* job pointed at it works
exactly as it always did.

## Tuning (no reflash)

The firmware is a generic render engine — it holds *no* monitoring policy, so
every threshold, colour, mapping and cadence lives on the host. In an installed
copy they are all in

```text
C:\ProgramData\FW16Sysmon\sysmon.ini
```

which is generated fully commented, with the units and the reasoning for each
value. Edit it and restart the service (*Restart the service* in the Start menu,
or `sc stop FW16Sysmon` / `sc start FW16Sysmon`) — no rebuild, no reflash. A
malformed value is reported in `sysmon.log` and ignored rather than being fatal,
so a typo can never stop a boot-start service from coming up.

Running from source, the same file is written next to the script; create it with
`--write-default-config`. The values are still ordinary module constants in
`host/fw16_sysmon.py` — the INI simply rebinds them at start-up — so editing
either works during development. What is available:

- **thresholds**: `BATT_CRITICAL_PCT`, `MEM_CRITICAL_PCT`, `TEMP_CRITICAL_C`,
  `TEMP_MIN_C` / `TEMP_MAX_C`, `ASSIST_MIN_W`, `DRIFT_MIN_W`
- **APU power limit**: `APU_LIMIT_NEAR` (how close to a limit counts as
  binding) and `APU_LIMIT_SIGNALS` (which SMU limits — `stapm`/`slow`/`fast` —
  trigger the breathe)
- **colours**: the `HUE_*` constants (including `HUE_NPU`, the ghost bar) and
  the temperature gradient endpoints
- **mappings**: `RATE_SEG_W1/2/3` (watts → animated segments),
  `ROW_VAL_BATT_IDLE`, `ROW_VAL_NPU_GHOST`, `BATT_DRIFT_FLOOR`, `LIMIT_FLOOR`
- **cadences**: the `PERIOD_*` constants (blink / breathe / pulse, in 32 ms units)
- **globals**: `GLIDE_MS_DEFAULT`, `TIMEOUT_MS_DEFAULT`
- **polling**: the `TTL_*` source-cache lifetimes (how often each slow sensor is read)
- **`[cli]`**: defaults for every command-line option (`max-led`, `interval`,
  `igpu-match`, …). An explicitly passed flag still wins over the file.

Only the wire protocol below is shared with the firmware; everything else lives
on the host.

## Protocol (v2)

Three 32-byte VIA "custom set value" reports are sent per interval on channel
`0x42`. The host describes each row fully; the firmware just draws it.

```text
[0]   0x07  id_custom_set_value
[1]   0x42  sysmon channel
[2]   value_id: 0x01 = FRAME A (rows 0,1,2)
               0x02 = FRAME B (rows 3,4,5 + globals)
               0x03 = FRAME C (ghost bars, rows 0..5; optional)
[3..] payload: FRAME A/B: three back-to-back 8-byte row descriptors
               FRAME C:   six back-to-back 4-byte ghost descriptors

row descriptor (8 bytes):
  +0 level         0-255  fraction of the 4-segment bar (0=empty, 255=full)
  +1 row_val       0-255  base row brightness (normally 255)
  +2 hue0          0-255  solid colour, or gradient start
  +3 hue1          0-255  gradient end hue (only when the gradient bit is set)
  +4 flags         bit0    gradient: 0=solid hue0, 1=hue0->hue1 across segments
                   bits1-2 envelope: 0=none, 1=blink, 2=breathe
                   bits3-4 overlay:  0=none, 1=trap-rise, 2=trap-fall, 3=sine
                   bit5    overlay direction: 0=up from boundary, 1=down
  +5 overlay_count 0=off; 1-4 = segments animated from the fill boundary
  +6 period        units of 32 ms (the row's envelope/overlay cadence)
  +7 floor         0-255  breathe trough / sine-tell floor

FRAME B appends three globals after its three row descriptors:
  [27] brightness  0-255           overall brightness (drives the LED global current)
  [28] glide       units of 4 ms   per-row level glide   (default 300 ms => 75)
  [29] timeout     units of 100 ms no-data fallback       (default 5000 ms => 50)

ghost descriptor (4 bytes, FRAME C):
  +0 level   0-255  fraction of the 4-segment bar (0 = no ghost)
  +1 hue     0-255  ghost colour
  +2 val     0-255  ghost brightness (the row's envelope scales it too)
  +3 flags   reserved (0)
```

The firmware glides each row's `level`, fills the bar (solid or gradient),
applies the envelope to the whole-row brightness, then paints any overlay
segments from the fill boundary — all timed off its own clock, so the bars stay
smooth between the host's updates. A ghost bar glides the same way and adds,
per segment, whatever part the ghost covers beyond the main fill. After
`timeout` with no frame it restores the stock global current and lets the
regular RGB effect run. VIA's echo is the ACK.

FRAME C is optional in both directions. Firmware that predates it ignores value
`0x03` and draws the plain bars. Firmware that has it fades the ghosts out on
their own when FRAME C stops arriving within `timeout`, so an older host can
never leave a stale ghost behind. Ghost-capable firmware also sets byte `[27]`
of its FRAME C echo to `0x01` (older firmware echoes the frame unchanged), which
is how `--selftest` reports that the module needs a reflash to draw the NPU.
