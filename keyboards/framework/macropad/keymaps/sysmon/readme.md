# sysmon — system monitor lighting for the Framework 16 RGB Macropad

Turns the macropad's six key rows into live bar graphs, driven by a small
host-side Python script over the VIA raw HID endpoint (VIA keeps working
normally — the stats ride on a private custom-value channel, `0x42`):

| Row | Indicator | Colors |
|-----|-----------|--------|
| 1 (top, small keys) | Battery | charging: level bar (red→green by level), segments from the filling one upward pulse in sync; discharging: level bar (red unplugged, amber when the battery is assisting an overloaded charger on AC), segments from the boundary downward flare and fade in sync, blinks below 15%; the charge/discharge power decides how many segments animate (1 segment for a trickle up to all 4 at ≥75 W); idle on AC: whole row steady dim green — a sub-10 W cap-drift drain counts as idle, with the boundary segment breathing faintly as a tell; the bar is rescaled against the max-charge cap, so charged-to-cap shows completely full |
| 2 | CPU usage | red bar; breathes when the APU is power-limited (see below) |
| 3 | Memory usage | gold bar; whole row blinks at ~1 Hz when usage is critically high (≥90%) |
| 4 | iGPU usage | red bar |
| 5 | dGPU usage | green bar (dark if no dGPU); breathes when the dGPU is power-limited (see below) |
| 6 | System temperature | fixed green→deep-red gradient across the row; bar length scales 30–90 °C; whole row blinks at ~1 Hz above 90 °C |

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

Flash by holding the **top-left + bottom-left keys while inserting the
module**, or — much easier — send the bootloader-jump packet from the host
script (same mechanism Framework's official updater uses: VIA command
`0x0B` padded with `0xFE`):

```powershell
python host\fw16_sysmon.py --bootloader
```

Then copy the built `framework_macropad_sysmon.uf2` onto the `RPI-RP2`
USB drive that appears.

## Host script (Windows)

```powershell
pip install hidapi psutil wmi pynvml pythonnet
scoop install ryzenadj                    # AMD APU STAPM/PPT limits + Tctl (authoritative)
python host\fw16_sysmon.py --fetch-lhm   # one-time: download LibreHardwareMonitorLib into lib/
python host\fw16_sysmon.py --verbose
```

(`pynvml` enables the NVIDIA dGPU power-limit breathe; `ryzenadj` enables the
authoritative AMD APU power-limit breathe (and an in-process temperature
source); `pythonnet` + `--fetch-lhm` enable the LibreHardwareMonitor APU
package-power + temperature path. All optional; the core bars work without
them. The AMD limit + temperature sources load a ring0 driver, so run the
script **elevated**.)

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
  (auto-discovered from a `ryzenadj` install / scoop shim if unset)
- `--fetch-lhm` — download LibreHardwareMonitorLib + its deps into `lib/` and exit
- `--no-lhm-lib` — disable the in-process LibreHardwareMonitorLib APU source

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
- **ryzenadj Tctl** — when `ryzenadj` is installed for the power-limit breathe,
  its die temperature (Tctl) is read from the same in-process SMU handle for
  free, so the APU power-limit feature alone is enough to light the temperature
  row even without LibreHardwareMonitor. Also elevated-only.
- the [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
  **app** running with its WMI provider enabled — fallback when the in-process
  lib isn't set up; same CPU package-power sensor.
- [framework_tool](https://github.com/FrameworkComputer/framework-system)
  on PATH (`winget install FrameworkComputer.framework_tool`) — throttled
  fallback for temperature only (APU die temperature from the EC; needs the
  script elevated). Also used (every 60 s) to read the battery max-charge cap so
  the battery bar scales to the cap.
- a working ACPI thermal zone — last resort; the FW16 exposes zones that
  *look* valid but report frozen values (a constant ~63 °C), and reading them
  needs admin rights anyway. Temperature stays dark if no source provides data.

### Start at login (no service required)

Task Scheduler → Create Task:

- Trigger: *At log on*
- Action: `pythonw.exe C:\path\to\fw16_sysmon.py`
- Check *Run only when user is logged on* (WMI brightness/GPU counters are
  per-session)

## Tuning (no reflash)

The firmware is a generic render engine — it holds *no* monitoring policy, so
every threshold, colour, mapping and cadence is a constant near the top of
`host/fw16_sysmon.py`. Edit and restart the script; no recompile or reflash:

- **thresholds**: `BATT_CRITICAL_PCT`, `MEM_CRITICAL_PCT`, `TEMP_CRITICAL_C`,
  `TEMP_MIN_C` / `TEMP_MAX_C`, `ASSIST_MIN_W`
- **APU power limit**: `APU_LIMIT_NEAR` (how close to a limit counts as
  binding) and `APU_LIMIT_SIGNALS` (which SMU limits — `stapm`/`slow`/`fast` —
  trigger the breathe)
- **colours**: the `HUE_*` constants and the temperature gradient endpoints
- **mappings**: `RATE_SEG_W1/2/3` (watts → animated segments),
  `ROW_VAL_BATT_IDLE`, `BATT_DRIFT_FLOOR`, `LIMIT_FLOOR`
- **cadences**: the `PERIOD_*` constants (blink / breathe / pulse, in 32 ms units)
- **globals**: `GLIDE_MS_DEFAULT`, `TIMEOUT_MS_DEFAULT`
- **polling**: the `TTL_*` source-cache lifetimes (how often each slow sensor is read)

Only the wire protocol below is shared with the firmware; everything else lives
on the host.

## Protocol (v2)

Two 32-byte VIA "custom set value" reports are sent per interval on channel
`0x42`. The host describes each row fully; the firmware just draws it.

```text
[0]   0x07  id_custom_set_value
[1]   0x42  sysmon channel
[2]   value_id: 0x01 = FRAME A (rows 0,1,2)
               0x02 = FRAME B (rows 3,4,5 + globals)
[3..] payload: three back-to-back 8-byte row descriptors

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
```

The firmware glides each row's `level`, fills the bar (solid or gradient),
applies the envelope to the whole-row brightness, then paints any overlay
segments from the fill boundary — all timed off its own clock, so the bars stay
smooth between the host's updates. After `timeout` with no frame it restores the
stock global current and lets the regular RGB effect run. VIA's echo is the ACK.
