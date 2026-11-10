# 6. Settings

[Manual](README.md) › Settings

The firmware on the pad only draws what it's told. **Every decision lives in
one settings file on the laptop**: thresholds, colours, timings, which limits
count, the words in the messages, brightness. You can change any of it by
editing the file and restarting the service. Nothing needs rebuilding or
reflashing.

```text
C:\ProgramData\FW16Sysmon\sysmon.ini
```

## Editing it

1. **Open it with administrator rights.** Only administrators can change the
   file. The Start menu's **Edit settings (sysmon.ini)** opens it in Notepad
   for reading. If saving fails, start Notepad with *Run as administrator* and
   open the file from there.
2. **Change a value.** Each setting has a comment above it that explains it
   and gives its unit. The file is grouped into sections such as `[colours]`
   and `[messages]`, and a setting must stay in its section.
3. **Save.** Plain UTF-8 is fine, and so is Notepad's "UTF-8 with BOM".
   UTF-16 ("Unicode") isn't.
4. **Restart the service**: Start menu → **Restart the service**. Changes
   only take effect at a restart.
5. **Check it took.** Run the [self-test](05-the-host-program.md#self-test).
   Its *Settings file* line says how many settings were applied and names any
   that were ignored.

The file is forgiving:

- **Delete a line** and that setting goes back to its built-in default.
- **A bad value is ignored, never fatal.** An out-of-range number, a misspelt
  name or an unknown word keeps the default. A line in
  [the log](05-the-host-program.md#the-log) says why. The service always
  starts, typo or not.
- `TEMP_MIN_C` must stay below `TEMP_MAX_C`. If it doesn't, both revert to
  their defaults.

### After an update

The installer **never overwrites** an existing `sysmon.ini`, so your changes
survive updates. As a result, settings added by a newer version are missing
from your file. They still work, at their built-in defaults, but you won't
see them or their comments. To get a fresh, fully commented file:

1. Rename your `sysmon.ini`, for example to `sysmon.old.ini`.
2. In an elevated terminal, run
   `& "C:\Program Files\FW16 Macropad Sysmon\fw16-sysmon.exe" --write-default-config`.
3. Copy your changes from the old file into the new one, then restart the
   service.

## Colours: QMK hues

Colours are **hues from 0 to 255**, going once around the colour wheel. The
pad always uses full saturation.

| Hue | Colour | Used for, by default |
|-----|--------|----------------------|
| 0 | red | CPU and iGPU bars, battery on its own power, the hot end of the temperature row |
| 16 | orange | dGPU sleep marker, light sleep (D3hot) |
| 28 | amber | Battery helping an overloaded charger |
| 36 | gold | Memory bar |
| 43 | yellow | n/a |
| 85 | green | dGPU bar, a full battery, the cool end of the temperature row |
| 128 | cyan | NPU ghost bar |
| 140 | light blue / teal | dGPU sleep marker, powered off (D3cold) |
| 170 | blue | n/a |
| 191 | violet (looks blue on these LEDs) | n/a |
| 213 | magenta | n/a |

The macropad's green LEDs are much stronger than its red and blue ones, so
colours between red and green look greener than the numbers suggest. Change
hues in small steps, and judge on the pad, not on a colour picker.

## Recipes

Each recipe shows the section and the line to change. Restart the service
afterwards.

**The pad is too bright or too dim.** Brightness follows the screen, scaled
between `min-led` (screen at 0 %) and `max-led` (screen at 100 %), on a scale
of 0-255:

```ini
[cli]
max-led = 100
min-led = 4
```

**I don't want the scrolling messages,** only the replay key:

```ini
[messages]
MSG_ENABLE = 0
```

**Only name power limits** (current, thermal and clock limits still breathe):

```ini
[messages]
MSG_KINDS = power
```

**No breathing for limits at all:**

```ini
[power_limits]
LIMIT_KINDS = none
```

**Slower, easier-to-read messages, without the numbers:**

```ini
[messages]
MSG_SPEED = 6
MSG_SHOW_VALUE = 0
```

**Different words.** Up to 12 characters each (8 for the component names, 23
for the no-limit answer). Allowed are A-Z, 0-9, space, `-`, `!` and `.`.
Uppercase `W` and `M` use the tall letter forms, lowercase `w` and `m` the
short ones.

```ini
[messages]
MSG_NAME_GPU = DGPU
MSG_WORD_STAPM = SUSTAINED
MSG_NO_LIMIT = ALL CLEAR
```

**Name the fast boost limit too** (it binds in short bursts, so the CPU row
breathes more often):

```ini
[power_limits]
APU_LIMIT_SIGNALS = stapm, slow, fast
```

**The dGPU row breathes too often.** Either require it to be closer to its
power limit, or stop counting the power limit:

```ini
[power_limits]
NV_NEAR_CAP = 0.97
; or: LIMIT_KINDS = current, thermal, clock, charger
```

**Temperature scale.** A bar from 40 to 95 °C that blinks only above 95 °C:

```ini
[thresholds]
TEMP_MIN_C = 40
TEMP_MAX_C = 95
TEMP_CRITICAL_C = 95
```

**Memory and battery alerts:**

```ini
[thresholds]
MEM_CRITICAL_PCT = 95
BATT_CRITICAL_PCT = 10
```

**Other colours.** Say, a blue dGPU bar and a magenta NPU ghost:

```ini
[colours]
HUE_GREEN = 170
HUE_NPU = 213
```

`HUE_GREEN` is also the colour of a full battery, so this changes both.

**The dGPU sleep marker in 10-minute steps:**

```ini
[thresholds]
DGPU_OFF_STEP_S = 600
```

**A brighter dGPU sleep marker or idle battery row:**

```ini
[levels]
ROW_VAL_DGPU_OFF = 120
ROW_VAL_BATT_IDLE = 120
```

**Snappier bars**, or calmer ones. `GLIDE_MS_DEFAULT` is how long a bar takes
to slide to a new value. `interval` is how often the host sends new values.

```ini
[globals]
GLIDE_MS_DEFAULT = 150

[cli]
interval = 0.25
```

**Fall back to the regular effect sooner when the host stops unexpectedly:**

```ini
[globals]
TIMEOUT_MS_DEFAULT = 3000
```

**The service can't find `framework_tool`** (the self-test's *Service's own
view* line says so). A per-user winget install isn't visible to the service,
so give it the full path:

```ini
[cli]
framework-tool = C:\Program Files\WinGet\Links\framework_tool.exe
```

**A temperature source reads nonsense.** Turn that source off:

```ini
[cli]
no-ryzenadj = true
; or: no-lhm-lib = true
```

`no-ryzenadj` also turns off the processor limit detection that comes from
the same source. Prefer installing `framework_tool`, which outranks ryzenadj
for temperature.

**Stop keeping the limit log,** or keep less of it:

```ini
[limitlog]
LIMITLOG_ENABLE = 0
; or: LIMITLOG_RETENTION_DAYS = 30
```

## Command-line defaults: [cli]

The `[cli]` section holds defaults for the program's command-line options,
which is how the service gets them. An option given on an actual command line
wins over the file. Switches take `true` or `false`.

<!-- BEGIN CLI-REFERENCE (generated) -->
| Option | Default | What it does |
|---|---|---|
| `interval` | `0.4` | seconds between updates (default: 0.4) |
| `min-led` | `8` | LED brightness at 0% screen brightness (default: 8) |
| `max-led` | `150` | LED brightness at 100% screen brightness, 0-255 (default: 150; full 255 is blinding) |
| `fallback-brightness` | `30` | assumed screen brightness % if unreadable (default: 30) |
| `led-gamma` | `2.0` | curve between min/max LED brightness; higher dims the mid-range, 1.0 is linear (default: 2.0) |
| `igpu-match` | `Intel\|Iris\|UHD\|Radeon.*(Graphics\|M\b)` | regex matching the iGPU adapter name |
| `dgpu-match` | `NVIDIA\|GeForce\|RTX\|GTX\|Radeon RX` | regex matching the dGPU adapter name |
| `cpu-power-cap` | *(unset)* | APU sustained power ceiling in watts for power-limit detection; auto-calibrated (approx) if unset |
| `no-nvml` | `false` | disable NVIDIA dGPU power/throttle detection |
| `no-ryzenadj` | `false` | disable the in-process libryzenadj APU STAPM/PPT power-limit + Tctl source (fall back to the LHM package-power heuristic) |
| `ryzenadj-dir` | *(unset)* | directory containing libryzenadj.dll + WinRing0x64.sys (auto-discovered from a ryzenadj install / scoop shim if unset) |
| `no-lhm-lib` | `false` | disable the in-process LibreHardwareMonitorLib APU source (fall back to the LHM WMI provider / EC) |
| `framework-tool` | *(unset)* | full path to framework_tool.exe (searched on PATH, in Program Files and in every user's winget install if unset; the service cannot rely on PATH) |
| `verbose` | `false` | print each tick's per-row descriptors |
<!-- END CLI-REFERENCE -->

Leave `verbose` at `false` for the service. With it on, the service writes
every update to `sysmon.log`.

## Every setting

Generated from the program's own list of settings (`config.py`) and its
built-in defaults. The comments in your `sysmon.ini` say the same.

<!-- BEGIN SETTINGS-REFERENCE (generated) -->
### [globals]

Frame globals sent to the firmware every tick.

| Setting | Default | Range | What it does |
|---|---|---|---|
| `GLIDE_MS_DEFAULT` | `300` | 0–1020 | Per-row bar glide, milliseconds. The firmware eases each row towards its new level over this long, so the bars stay smooth between host updates. Sent in 4 ms units, so the usable maximum is 1020. |
| `TIMEOUT_MS_DEFAULT` | `5000` | 100–25500 | How long the firmware waits with no frames before giving up and restoring the stock RGB effect. Covers service stop, reboot and crashes alike. Sent in 100 ms units. |

### [cadence]

Animation periods, in units of 32 ms (the firmware's tick).

| Setting | Default | Range | What it does |
|---|---|---|---|
| `PERIOD_BLINK` | `32` | 1–255 | Critical blink period (memory >= MEM_CRITICAL_PCT, temperature > TEMP_CRITICAL_C, battery below BATT_CRITICAL_PCT). 32 ms units, so 32 = ~1 Hz. |
| `PERIOD_BREATHE_FAST` | `48` | 1–255 | Brisk breathe: a limit (LIMIT_KINDS) holds the CPU or dGPU back. 32 ms units, so 48 = ~1.5 s. |
| `PERIOD_BREATHE_SLOW` | `128` | 1–255 | Slow breathe: the power *source* is the bottleneck (the battery is assisting an overloaded charger), with 'charger' in LIMIT_KINDS. 128 = ~4 s. |
| `PERIOD_PULSE` | `128` | 1–255 | Battery charge/discharge trapezoid pulse. 128 = ~4 s. |
| `PERIOD_SINE` | `128` | 1–255 | The faint AC cap-drift 'tell' sine on the battery row. |

### [colours]

QMK hues, 0-255: 0 red, 36 gold, 85 green, 170 blue.

| Setting | Default | Range | What it does |
|---|---|---|---|
| `HUE_RED` | `0` | 0–255 | QMK hue for the CPU and iGPU bars, and a discharging battery on its own power. |
| `HUE_AMBER` | `28` | 0–255 | Battery discharging *while on AC* -- the charger is overloaded and the battery is making up the difference. |
| `HUE_GOLD` | `36` | 0–255 | Memory bar. |
| `HUE_GREEN` | `85` | 0–255 | Full battery and the dGPU bar. |
| `HUE_TEMP_LO` | `85` | 0–255 | Temperature gradient, cool end (85 = green). |
| `HUE_TEMP_HI` | `0` | 0–255 | Temperature gradient, hot end (0 = red). |
| `HUE_CHARGE_FULL` | `85` | 0–255 | Charging hue at 100%; the bar interpolates red -> this. |
| `HUE_NPU` | `128` | 0–255 | NPU ghost bar on the iGPU row (128 = cyan). Pick a hue well away from HUE_RED: a segment the two bars share mixes both colors. |
| `HUE_DGPU_OFF` | `140` | 0–255 | The dGPU row's keys from the right while the dGPU is powered off (D3cold): 140 = light blue/teal, kept off HUE_NPU (the row above). Always fully saturated (the marker is a ghost bar, which has no saturation of its own). |
| `HUE_DGPU_D3HOT` | `16` | 0–255 | The same marker while the dGPU idles in D3hot (asleep, but its PCIe link still up): 16 = orange, kept off HUE_AMBER's yellower 28. |

### [levels]

Row brightness levels and animation floors, 0-255.

| Setting | Default | Range | What it does |
|---|---|---|---|
| `ROW_VAL_FULL` | `255` | 1–255 | Base brightness of a normal row, before the global screen-brightness scaling. |
| `ROW_VAL_BATT_IDLE` | `80` | 1–255 | Dim green shown when on AC and the battery is idle (held at its charge cap). |
| `ROW_VAL_NPU_GHOST` | `96` | 1–255 | Brightness of the NPU ghost bar behind the iGPU bar. Kept dim so the bright iGPU bar reads first; the ghost only shows where the NPU is busier than the iGPU. Needs the ghost-bar firmware (FRAME C); older firmware ignores it. |
| `ROW_VAL_DGPU_OFF` | `80` | 1–255 | Brightness of the dGPU row's keys from the right while the dGPU is powered off (D3cold) or idles in D3hot: steady and dim, like the idle battery row. Needs firmware that reads the ghost flags; older firmware shows no marker. |
| `BATT_DRIFT_FLOOR` | `68` | 0–255 | Trough of the faint cap-drift breathe; the peak is ROW_VAL_BATT_IDLE. Keep the gap small -- this is meant to be a barely-there tell, not an alert. |
| `LIMIT_FLOOR` | `110` | 0–255 | Trough of the power-limit breathe. Deliberately not 0: dipping fully off is the critical-blink language, so a power-limited row must stay readable throughout. |

### [thresholds]

When a row switches into its alert state.

| Setting | Default | Range | What it does |
|---|---|---|---|
| `BATT_CRITICAL_PCT` | `15` | 0–100 | Raw battery % below which a *discharging* battery blinks. |
| `ASSIST_MIN_W` | `10` | 0–255 | Battery-assist drain (watts, on AC) that counts as a real power deficit. Below this, a trickle out of the battery on AC is just EC charge-cap housekeeping and is shown as idle. |
| `DRIFT_MIN_W` | `0.1` | 0–255 | Smallest drain (watts, on AC) that counts as real cap drift and earns the faint boundary-segment tell. The EC leaves the ACPI discharging bit asserted at a flat 0 W while it floats a pack it is done charging (what you get with the charge cap at 100%), so a bit with no measured flow behind it is idle, not drift. |
| `MEM_CRITICAL_PCT` | `90` | 0–100 | Memory usage % at or above which the whole row blinks. |
| `TEMP_CRITICAL_C` | `90` | 0–150 | Temperature above which the whole row blinks. This is the temperature row's own alert; a CPU or dGPU slowed down by a thermal limit breathes its row instead (LIMIT_KINDS). |
| `TEMP_MIN_C` | `30` | 0–150 | Temperature mapped to an empty bar. Must be below TEMP_MAX_C (if not, both keep their defaults). |
| `TEMP_MAX_C` | `90` | 0–150 | Temperature mapped to a full bar. |
| `DGPU_OFF_STEP_S` | `900.0` | 1–86400 | The dGPU sleep marker grows from the right: its last key lights as the dGPU enters D3cold (teal) or D3hot (orange), and one more after each further this many seconds it stays in that state, up to all 4 (900 = every 15 minutes). Any change of state, a wake included, starts it over. |

### [battery]

Charge/discharge power -> how many bar segments animate.

| Setting | Default | Range | What it does |
|---|---|---|---|
| `RATE_SEG_W1` | `20` | 0–255 | Charge/discharge watts above which 2 segments animate. |
| `RATE_SEG_W2` | `45` | 0–255 | ... 3 segments. |
| `RATE_SEG_W3` | `75` | 0–255 | ... all 4 segments. Below RATE_SEG_W1 a single segment animates, so a trickle still reads as movement. |

### [power_limits]

Limit detection: when the CPU and dGPU rows breathe.

| Setting | Default | Range | What it does |
|---|---|---|---|
| `LIMIT_KINDS` | `power, current, thermal, clock, charger` | list of: power, current, thermal, clock, charger, none | Which kinds of limit make the CPU or dGPU row breathe (MSG_KINDS says which of them the pad also names): power - APU STAPM/PPT (APU_LIMIT_SIGNALS), dGPU power cap and power brake; on AC only (on battery the limits drop by design; the battery row says so) current - APU TDC/EDC, core and SoC thermal - APU Tctl at its THM limit, dGPU thermal slowdown clock - the CPU held at its base clock under load (boost off) with no SMU limit near; on AC only charger - a component held back while the battery assists an overloaded charger: breathes slowly (PERIOD_BREATHE_SLOW), next to the battery row's amber; leave it out to breathe for the limit the EC lowered instead none - no limit indication at all |
| `APU_LIMIT_NEAR` | `0.95` | 0.1–1.0 | How close to an SMU power or current limit counts as *binding*: the live value must reach this fraction of the limit. The value sits right at the limit when the chip is genuinely capped. |
| `APU_LIMIT_SIGNALS` | `stapm, slow` | list of: stapm, fast, slow | Which SMU *power* limits count (with power in LIMIT_KINDS). stapm (the skin-temperature-aware sustained budget HWiNFO shows as 'APU STAPM Limit') and slow are sustained, so they give a steady signal. fast is the short boost window: adding it makes the row flicker under bursty load. |
| `APU_THM_MARGIN_C` | `3.0` | 0–50 | Tctl within this many degrees of the APU's THM limit (100 C on the HX 370) counts as a binding thermal limit. |
| `LIMIT_RELEASE_S` | `8.0` | 0–600 | Seconds a limit must stay clear before it stops counting. A capped value hovers around the threshold (STAPM crossed it every 1-3 s under an all-core load), which flickered the breathe on and off. |
| `CLOCK_BUSY_PCT` | `80` | 1–100 | The clock limit (clock in LIMIT_KINDS): a core whose two threads together are this busy (%) wants to boost... |
| `CLOCK_BASE_MARGIN` | `1.05` | 0.5–2.0 | ... so the CPU counts as held at its base clock when no busy logical CPU runs faster than this times base (Windows' Processor Performance counter; a boosting core reads 1.5-2.5x). |
| `CLOCK_DEBOUNCE_READS` | `3` | 1–100 | Consecutive reads that must find the CPU held at base before it counts (one read per --interval). |
| `CPU_LOAD_HIGH` | `80` | 0–100 | Fallback heuristic only (no ryzenadj): CPU load % above which APU power limits are even evaluated. |
| `CPU_POWER_FLOOR_W` | `15` | 0–255 | Fallback heuristic only: ignore power-cap inference below this package power. |
| `CEIL_NEAR` | `0.95` | 0.1–1.0 | Fallback heuristic only: fraction of the observed ceiling that reads as 'at it'. |
| `NV_ACTIVE_MIN_W` | `15` | 0–1000 | dGPU draw above which the card counts as actively loaded. |
| `NV_NEAR_CAP` | `0.92` | 0.1–1.0 | dGPU draw as a fraction of its enforced limit before NVIDIA's SwPowerCap flag is believed. NVIDIA sets that flag eagerly -- even near idle -- and without this filter the dGPU row breathes on and off every few seconds. |
| `NV_DEBOUNCE_READS` | `3` | 1–100 | Consecutive reads a dGPU limit flag must hold before it counts, so one NVML blip cannot flash the breathe. |

### [messages]

Limit messages: the pad names the limit a row breathes for.

| Setting | Default | Range | What it does |
|---|---|---|---|
| `MSG_ENABLE` | `1` | 0–1 | 1: when a limit holds the CPU or dGPU back, scroll which one across the pad, e.g. 'CPU STAPM 28W'. 0: only on demand -- keypad 5 with NumLock off always says what binds right now (or NO LIMIT). Needs the firmware from this version. |
| `MSG_KINDS` | `power, current, thermal, clock` | list of: power, current, thermal, clock, charger, none | Which kinds of limit (see LIMIT_KINDS) the pad names on its own. The others only breathe: the slow charger breathe next to the amber battery row says enough by itself. The replay key names whatever makes a row breathe. |
| `MSG_ONSET_S` | `3.0` | 0–600 | How long a limit must have held before it is named. |
| `MSG_MIN_GAP_S` | `30.0` | 0–3600 | Shortest time between two messages about one component (when the limit holding it back changes). |
| `MSG_REARM_S` | `120.0` | 0–36000 | A limit is named once per episode; an episode ends when the component has been free of limits this many seconds. |
| `MSG_SPEED` | `8` | 2–40 | Ticker speed, columns per second (a letter is about 6). |
| `MSG_SHOW_VALUE` | `1` | 0–1 | 1: end with the limit's value right now ('28W'; for a dGPU thermal slowdown its temperature). 0: just the names. |
| `MSG_NAME_CPU` | `CPU` | text, ≤8 chars | The words on the pad. A-Z, 0-9, space, - ! . only; uppercase W and M are the tall letter forms, w and m the short ones. The CPU row stands for the whole APU. |
| `MSG_NAME_GPU` | `GPU` | text, ≤8 chars | The dGPU. |
| `MSG_NO_LIMIT` | `NO LIMIT` | text, ≤23 chars | The replay key's answer when nothing binds. |
| `MSG_WORD_STAPM` | `STAPM` | text, ≤12 chars | APU sustained power budget (skin temperature). |
| `MSG_WORD_SLOW` | `PPT` | text, ≤12 chars | APU slow PPT. |
| `MSG_WORD_FAST` | `FAST PPT` | text, ≤12 chars | APU fast PPT (only with fast in APU_LIMIT_SIGNALS). |
| `MSG_WORD_TDC` | `TDC` | text, ≤12 chars | APU core current, sustained. |
| `MSG_WORD_TDC_SOC` | `SOC TDC` | text, ≤12 chars | APU SoC current, sustained. |
| `MSG_WORD_EDC` | `EDC` | text, ≤12 chars | APU core current, peak. |
| `MSG_WORD_EDC_SOC` | `SOC EDC` | text, ≤12 chars | APU SoC current, peak. |
| `MSG_WORD_THM` | `HOT` | text, ≤12 chars | APU at its temperature limit. |
| `MSG_WORD_POWER` | `POWER` | text, ≤12 chars | dGPU power cap (and the APU's, when only the package-power heuristic is available). |
| `MSG_WORD_BRAKE` | `BRAKE` | text, ≤12 chars | dGPU hardware power brake. |
| `MSG_WORD_THERMAL` | `HOT` | text, ≤12 chars | dGPU thermal slowdown. |
| `MSG_WORD_CLOCK` | `CLK LIMITED` | text, ≤12 chars | CPU held at its base clock (boost off), e.g. 'CPU CLK LIMITED 2GHZ'. |
| `MSG_WORD_CHARGER` | `CHARGER` | text, ≤12 chars | Held back by an overloaded charger (the replay key's word for the slow breathe; named on its own only with charger in MSG_KINDS). |

### [limitlog]

Limit log: a long-term record of which limits bound, when, and under what conditions, for tallying later (--limit-report).

| Setting | Default | Range | What it does |
|---|---|---|---|
| `LIMITLOG_ENABLE` | `1` | 0–1 | 1: keep the limit log, limitlog.sqlite beside this file: every limit that binds (raw, on battery too), when, for how long, and what the machine was doing. Read it with the Start menu's 'Limit report', or fw16-sysmon.exe --limit-report. |
| `LIMITLOG_ACTIVE_S` | `2.0` | 0.4–600 | Seconds between samples while a limit binds or the CPU or dGPU is busy (and LIMITLOG_LINGER_S after). |
| `LIMITLOG_IDLE_S` | `30.0` | 1–3600 | ...and otherwise. Each sample is roughly 1 KB. |
| `LIMITLOG_LINGER_S` | `30.0` | 0–3600 | How long the busy cadence lasts after the last busy tick. |
| `LIMITLOG_CPU_ACTIVE_PCT` | `25` | 0–100 | Total CPU load (%) that counts as busy for the cadence. |
| `LIMITLOG_BUSY_TAU_S` | `10.0` | 0–600 | A core this busy (CLOCK_BUSY_PCT) also counts as busy, for the cadence and the report's busy time, once its load averaged over about this many seconds gets there; idle Windows spikes some core that high for a moment every few seconds. 0: no averaging. |
| `LIMITLOG_ONSET_S` | `2.0` | 0–600 | A limit held this long becomes an episode. Its time counts in the tallies from the first read regardless. |
| `LIMITLOG_EC_S` | `5.0` | 1–3600 | framework_tool --thermal (EC temperatures, fans, and the EC's own APU throttle flags) while busy. Each read spawns a process. |
| `LIMITLOG_EC_IDLE_S` | `30.0` | 1–3600 | ...and while idle. |
| `LIMITLOG_PROC_S` | `10.0` | 1–3600 | Top CPU processes while busy (six times that while idle). |
| `LIMITLOG_PMTABLE_S` | `600.0` | 0–86400 | Keep the raw SMU power table this often (and at the start of every CPU limit episode), for decoding the fields libryzenadj doesn't name. 0: only at episode starts. |
| `LIMITLOG_RETENTION_DAYS` | `90` | 1–3650 | Samples and raw tables older than this are deleted. Episodes, events and the daily tallies are kept for good (they are small). |
| `LIMITLOG_MAX_MB` | `1024` | 16–1048576 | Past this size the oldest samples go first. |

### [polling]

Per-source cache lifetimes. Most ticks reuse cached readings so the poll loop stays cheap; raise these to cut CPU use further, lower them for a more responsive display.

| Setting | Default | Range | What it does |
|---|---|---|---|
| `TTL_BRIGHTNESS` | `0.5` | 0–3600 | Screen-brightness cache lifetime, seconds. Keep this short: a longer TTL makes the macropad visibly lag a brightness change. 0 re-reads every tick. |
| `TTL_BATTERY` | `1.0` | 0–3600 | Battery charge/discharge state cache lifetime. |
| `TTL_RYZENADJ` | `1.0` | 0–3600 | One ryzenadj SMU refresh feeds both the APU limit verdict and the Tctl temperature. |
| `TTL_TEMP` | `2.0` | 0–3600 | ACPI thermal-zone cache lifetime: the last-resort temperature source, a WMI query, used only when nothing better reads. |
| `TTL_LHM_SNAPSHOT` | `1.0` | 0–3600 | One LibreHardwareMonitor walk feeds temperature, package power and CPU load together. |
| `TTL_EC_THERMAL` | `2.5` | 0–3600 | framework_tool --thermal fallback. Each read spawns a process, so do not lower this much. |
<!-- END SETTINGS-REFERENCE -->

---

Previous: [5. The host program](05-the-host-program.md) · Next: [7. The limit log](07-limit-log.md)
