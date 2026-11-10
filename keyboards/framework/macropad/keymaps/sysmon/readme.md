# sysmon — system monitor lighting for the Framework 16 RGB Macropad

> **Using the pad?** The [user manual](https://github.com/Msprg/framework_qmk_firmware/blob/macropad-sysmon/keyboards/framework/macropad/keymaps/sysmon/manual/README.md) explains what every
> light means, the keys, the settings and troubleshooting. This readme is the
> technical reference.

Turns the macropad's six key rows into live bar graphs, driven by a small
host-side Python script over the VIA raw HID endpoint (VIA keeps working
normally — the stats ride on a private custom-value channel, `0x42`):

| Row | Indicator | Colors |
|-----|-----------|--------|
| 1 (top, small keys) | Battery | charging: level bar (red→green by level), segments from the filling one upward pulse in sync; discharging: level bar (red unplugged, amber when the battery is assisting an overloaded charger on AC), segments from the boundary downward flare and fade in sync, blinks below 15%; the charge/discharge power decides how many segments animate (1 segment for a trickle up to all 4 at ≥75 W); idle on AC: whole row steady dim green — a sub-10 W drain counts as idle, and if the EC is measurably drifting off the cap the boundary segment breathes faintly as a tell (a 0 W "discharging" report, which is what the EC says while it floats a pack it is done charging, is plain idle: no tell); the bar is rescaled against the max-charge cap, so charged-to-cap shows completely full |
| 2 | CPU usage | red bar; breathes when a limit holds the APU back, and the pad names it (see below) |
| 3 | Memory usage | gold bar; whole row blinks at ~1 Hz when usage is critically high (≥90%) |
| 4 | iGPU + NPU usage | red bar for the iGPU; NPU usage as a dim cyan *ghost* bar behind it, visible where the NPU is busier than the iGPU (see below) |
| 5 | dGPU usage | green bar (dark if no dGPU); breathes when a limit holds the dGPU back, and the pad names it (see below); while the dGPU is powered off (D3cold) dim light blue/teal keys grow from the right, one per 15 minutes it stays off |
| 6 | System temperature | fixed green→deep-red gradient across the row; bar length scales 30–90 °C; whole row blinks at ~1 Hz above 90 °C |

**Lights off.** With NumLock off, the top-left key is the macropad's RGB
toggle (Framework's stock layer). It switches the whole pad dark, bars
included, and the pad remembers that across restarts; press it again to bring
them back. VIA's "All Off" switches it off the same way (kept across
restarts once VIA saves it), and the same key brings it back. The service log and `--selftest`
both say when the pad is dark for this reason; limit messages due meanwhile
are dropped, not saved up.

## Boot rainbow

From power-up until the host first connects, the macropad shows a dim, static
rainbow running diagonally from red at the top-left key to violet at the
bottom-right one. It's introduced by a short animation picked from the
power-up pool (`boot_pool[]` in `keymap.c`), currently all 22:

| # | Animation | |
|---|-----------|---|
| 1 | Wipe | a soft diagonal edge sweeps from the top-left corner, revealing it |
| 2 | Shine | the same sweep with a white edge that cools into the colours |
| 3 | Spin | the rainbow flows along the diagonal, slows down and freezes |
| 4 | Bloom | the pad fades in white, then the rainbow colours fade in |
| 5 | Twinkle | keys ignite as white sparks roughly along the diagonal |
| 6 | Rotate | the gradient spins around the centre and settles on the diagonal |
| 7 | Twinkle, scatter | sparks land anywhere, in random order |
| 8 | Fire | red/orange/yellow flames climb from the bottom row, red embers shoot up ahead of them, and the burned keys cool into the still rainbow behind |
| 9 | Converge | ragged white edges from opposite corners meet in the middle |
| 10 | Flicker | each key sputters on like a warming neon tube |
| 11 | Lock-in | the whole pad sweeps through the spectrum as one colour; each key freezes as it reaches its own |
| 12 | Fireworks | rockets rise and burst, lighting the keys around them and throwing sparks that arc down; a finale lights the rest |
| 13 | Lightning | jagged bolts strike top to bottom with a flickering sky flash, leaving struck keys lit; the last lights everything |
| 14 | Stack | colours drop in from above like falling blocks, bottom row first, each landing with a flash |
| 15 | Spotlight | a spotlight wanders the dark pad showing the rainbow where it shines, then widens to light it all |
| 16 | Glitch | the picture boots like a corrupted video signal until it holds clean |
| 17 | FW16 pop | F, W, 1, 6 pop up one at a time as big letters in the rainbow colours; the rest sparkles in around the 6, darkest patches first, so the pad fills in evenly |
| 18 | FW16 scroll | "FW16" scrolls through like an LED ticker, then the rainbow slides in behind it |
| 19 | FRAMEWORK scroll | the same with "FRAMEWORK", at an easy reading pace (~9 s) |
| 20 | FRAMEWORK flash | white letters flash one at a time like a sign spelling itself out, then the rainbow sputters on like neon |
| 21 | FRAMEWORK scroll, letter colours | each letter keeps its own colour as it moves, F red through K violet |
| 22 | FRAMEWORK16 scroll, letter colours | the same with "FRAMEWORK16" (~10.5 s) |

Each power-up picks one from a shuffle bag: every cycle of power-ups (one per
pooled animation) plays each pooled animation once, in a fresh order, never
the same one twice in a row. The position is a counter in the EEPROM user word
(written once the animation is over, played out or cut short by a key, and
USB is configured, so clear of enumeration). The random parts
(twinkles, fire, flicker, fireworks, lightning, glitches...) are re-randomized on
every boot, and on every round of a replay.

Once the host connects, the bars fade in over the rainbow, every key going
straight from its rainbow colour to its bar colour, and the rainbow is done
until the next power-up. Pressing any key also dismisses it (cutting the
animation short if it is still playing): without a host, the pad switches to
the regular RGB effect at the first touch and stays there until the host
connects.

If the host later goes away:

- **It stopped on purpose** (service stop, shutdown, Ctrl+C): the host sends a
  goodbye, and the macropad switches to the regular RGB effect at once.
- **It vanished** (a crash, a hung process): after the no-data timeout the
  macropad shows a disconnect warning for 10 seconds, then falls back to the
  regular RGB effect. If the host comes back meanwhile, the bars cross-fade
  back in. The warning is black-and-yellow caution stripes; which variant is
  `SYSMON_WARN_STYLE` in `keymap.c`: 1 static "/" bands, 2 static "\\" bands,
  3 static and slowly breathing, 4 blinking slowly, 5 stepping like barrier
  chaser lights. `bootanim_preview.py warn` previews them.
- **The laptop slept**: no warning. Silence across a USB suspend is expected.

The goodbye needs the host from this version; an older installed service just
goes quiet when stopped, which the macropad reads as a vanished host.

Brightness (`BOOT_BRIGHTNESS`, same scale as the host's brightness byte) and the
hue span (`BOOT_HUE_START`/`BOOT_HUE_END`) are defines in `keymap.c`. To preview
animations without power-cycling, use `host/bootanim_preview.py`. It needs
`hidapi`, so run it with the host venv's interpreter — from an **elevated**
prompt, since that interpreter is requireAdministrator-manifested. It is safe
alongside the running service. From `host\`:

```powershell
.venv\Scripts\python.exe -I bootanim_preview.py          # every animation, each named by dots
.venv\Scripts\python.exe -I bootanim_preview.py 7-12 x2  # a range, twice
.venv\Scripts\python.exe -I bootanim_preview.py 5        # just one (5 x3: three times)
.venv\Scripts\python.exe -I bootanim_preview.py 17 --speed 0.25  # at a quarter of the speed (0.1 to 10; trace N too)
.venv\Scripts\python.exe -I bootanim_preview.py boot     # the next power-up pick, as a boot plays it
.venv\Scripts\python.exe -I bootanim_preview.py status   # boot counter + this power-up's pick
.venv\Scripts\python.exe -I bootanim_preview.py reboot   # restart the macropad, like replugging it
.venv\Scripts\python.exe -I bootanim_preview.py reboot 5 # five starts in a row, each after the last finished
.venv\Scripts\python.exe -I bootanim_preview.py text FRAMEWORK FRAmEWORK -r 3   # A/B, three times
.venv\Scripts\python.exe -I bootanim_preview.py text FRAMEWORK16 -s ticker -c letters
.venv\Scripts\python.exe -I bootanim_preview.py msg "CPU STAPM 28W" "GPU HOT 87C" -c green  # limit messages, over the live bars
.venv\Scripts\python.exe -I bootanim_preview.py trace    # what the LEDs were sent over the last ~13 s
.venv\Scripts\python.exe -I bootanim_preview.py trace 1  # animation 1 and the fade into the bars after it (trace 1 x3: three times)
```

`text` shows any text (up to 23 characters: A-Z, 0-9, space, `-`, `!`, `.`)
as a `flash` (letters one at a time, the default), `ticker` or `pop`, in
`white` (default), `rainbow` or per-`letters` colours, at `--speed` ms per
letter (flash/pop) or columns per second (ticker). Several texts play one
after another, each when the previous has finished. Letter case picks the
form: uppercase W and M are tall (they reach into the small top row, which
keeps them from reading as an H), lowercase w and m short; every other
letter has a single form.

`trace` reads out the firmware's LED flight recorder, which always holds the
last ~13 seconds (768 frames, one every ~16 ms) of what was sent to the LED
chip: each frame's global current, every key's PWM, what was showing, and any
change of the current (and in which order it went out). It writes three files
to `host\traces\`: a CSV with every value, a summary (what showed when, every
current change, the biggest frame-to-frame jumps on a key, stretches where a
very low PWM runs at a high current), and a timeline PNG with a column per
frame: the display mode, the current, and a band per key (pad rows named on
the left) colored by its light (PWM x current) relative to the trace's
brightest moment, over a ruler in ms. Where a fade into the bars is in the
trace, the summary also checks it: a key jumping from one frame to the next
(a step much bigger than the steps either side of it), a key going brighter or
dimmer than both where it started and where it ends up, a key dipping after it
had nearly reached its final brightness, and big steps of the current late in
the fade (a live bar, such as the CPU's, can move on its own and trip the key
checks). Notes list what is expected: whole-PWM rounding steps on keys fading
out at a very low PWM, and the current stepping down after the fade when the
bars are dimmer than the rainbow.
`trace --render FILE.csv` redraws a saved trace's PNG and summary. `trace N` records
animation N from the start and the fade after it. After a `reboot`, the ring
starts at power-up, so a `trace` within ~13 s holds the whole startup. It shows
the values sent, not the light: how the LEDs turn very low PWM at a high
current into light, for one, it cannot show.

For a glitch seen where no tool can run (the laptop still booting, say), hold
the top-left key (Esc) for a second within ~13 s of it: the macropad saves the
recorder to its own flash, and the whole pad flashes white once when it's
done (about a second later). It keeps the last four saves, through power-off and
reflashing. The hold never reaches the laptop; a quick tap still sends the key
(on release). Later, `trace saved` lists the saves (when after power-up, which
start of the pad), and `trace saved N` (or `trace saved last`) writes save N's
CSV, summary and PNG like `trace` does. `trace save` saves from the laptop, as
the key does. The recorder only records while something shows: a save made
with the lights off (with NumLock off, a tap on Esc is the RGB toggle) ends
where they went off, and `trace saved` says so ("nothing recorded in at least
its last 6.1 s").

A replay of several animations names each with dots before it plays: up to
9, N white dots in reading order from the top-left key; from 10, the tens as
cyan dots on the top row and the units as white dots from the second row. A
replay of a single animation, however many times, plays it without them.
Either way each one starts from dark and holds its finished rainbow a moment.

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

## dGPU sleep marker

On a hybrid-graphics laptop the dGPU powers itself off when nothing uses it.
While it is off, in **D3cold**, the dGPU row's last (right) key glows a dim,
steady light blue/teal (`HUE_DGPU_OFF`, `ROW_VAL_DGPU_OFF`). The longer the GPU
stays off without a break, the further the dim bar grows to the left: each key
stands for a 15-minute stretch that has begun (`DGPU_OFF_STEP_S`). The last key
lights as the GPU powers off, the next after 15 minutes, the third after 30 and
the fourth after 45; it stays at four keys after that. When something wakes the
GPU, even for a second, the bar fades out, and the count starts again from one
key once the GPU is off again. The green usage bar is unchanged.

A wake is only seen if the host is looking: it checks the power state four
times a second, on a thread of its own, so a busy tick or a disconnected pad
can't hide one. Time nobody watched can't count: a gap of more than a few
seconds between two checks (the laptop slept) starts the count over, and so
does the host starting.

The state comes from Plug and Play: each device's most recent power state
(what Device Manager shows as *Power data*), a property lookup that involves
no I/O to the GPU, so reading it can't wake it. PnP reports D3 for D3hot and
D3cold alike; it is D3cold when the PCIe port above the GPU is in D3 as well,
since Windows only powers the port down once the link is off. On the FW16's
RTX 5070, an idle GPU drops to D3hot within about a second and its port
follows 6-8 s later, so the key lights up a few seconds after the GPU goes
idle. Background activity wakes the GPU now and then for a second or two; the
key goes out for those wakes too.

`--verbose` shows the state at the end of the `limits` line (`power=D0`,
`D3hot`, or `D3cold for 1234s`, the time off so far), and `--selftest`
reports it. The marker needs the
firmware from this version (it is a right-anchored ghost bar, protocol FRAME C
below); with older firmware the host sends no marker at all, rather than one
at the wrong end of the row.

## Limit indication

The CPU (row 2, whole APU) and dGPU (row 5) rows **breathe** when a limit is
holding the component back. The bar stays visible: it dips to a dim floor, not
off, since off is the critical blinks' language. The cadence says which kind:

- **Brisk breathe (~1.5 s)** — the chip is at one of its own limits (below).
- **Slow breathe (~4 s)** — the *power source* is the bottleneck: the battery
  is assisting an overloaded/undersized charger on AC, so a loaded component is
  being throttled to fit. This pairs with the battery row turning **amber** in
  the same situation (the battery row says the charger is overloaded; the
  component breathe says which component it's choking).

Which limits count is `LIMIT_KINDS` in `sysmon.ini` (all five by default):

| Kind | APU (CPU row) | dGPU | When |
|------|---------------|------|------|
| `power` | STAPM, slow PPT (`APU_LIMIT_SIGNALS`; fast PPT is left out, as its short boost window would flicker) | the driver's power cap, the hardware power brake | on AC only: on battery the limits drop by design, and the battery row already shows that |
| `current` | TDC and EDC, core and SoC | — | always |
| `thermal` | Tctl at its THM limit (within `APU_THM_MARGIN_C`) | the driver's or the hardware's thermal slowdown | always |
| `clock` | held at its base clock under load (boost off) with no SMU limit near (see below) | — | on AC only |
| `charger` | a STAPM/PPT limit binding (the EC lowered it), or the clock held at base, while the battery assists an overloaded charger: the slow breathe | a loaded dGPU capped while the battery assists: the slow breathe | on AC |

The clock limit covers a CPU held back with none of the SMU limits near: boost
switched off. On this FW16, running from a 29 W USB-C source (a monitor's
port) with the battery making up the rest, every core stayed at 2.0 GHz under
an all-core load, at 27 W and 90% of the slow PPT, and even a single busy
thread never passed 2.02 GHz, in every Windows power mode. Nothing in the SMU
table bound, so the CPU row showed nothing. On the 240 W charger the same
loads ran at 2.4 GHz all-core and 4 GHz on one core. Windows reports each
logical CPU's clock relative to base and how busy it is: the CPU counts as held
when a core is at least `CLOCK_BUSY_PCT` (80%) busy, both its threads together,
yet no busy logical CPU runs faster than `CLOCK_BASE_MARGIN` (1.05) times base,
for `CLOCK_DEBOUNCE_READS` (3) reads in a row. While the battery assists, the
charger is the likely cause, and it breathes slowly as `charger`.

A capped value hovers around its threshold: under an all-core load STAPM
crossed 95% of its limit every 1-3 s. So a limit counts until it has been
clear for `LIMIT_RELEASE_S` (8 s), and the breathe doesn't flicker.

### The pad names the limit

For the brisk breathe the cadence alone can't say which limit it is, so the pad
says it. When a limit has held for 3 s and still binds, the bars dim out, the
pad scrolls which component, which limit and its current value across, in the
row's colour, and the bars fade back in:

```text
CPU STAPM 28W     APU sustained power budget, now 28 W (it falls as the chassis warms)
CPU PPT 30W       slow PPT          CPU FAST PPT 45W   fast PPT (only with fast in APU_LIMIT_SIGNALS)
CPU TDC 54A       core current      CPU SOC TDC 30A    SoC current     CPU EDC 54A / CPU SOC EDC 30A
CPU HOT 100C      Tctl at its THM limit
CPU CLK LIMITED 2GHZ   held at the base clock (the busy cores' fastest clock; 2.6GHZ when not round)
GPU POWER 100W    the dGPU's power cap (its enforced limit)
GPU BRAKE         the hardware power brake
GPU HOT 87C       thermal slowdown (the GPU's temperature)
CPU POWER 54W     the package-power heuristic, without ryzenadj (package power)
```

That takes about 9.5 s. It plays once per episode: again only when the limit
changes (STAPM → TDC, say; at most every 30 s), and a new episode starts once
the component has been free of limits for 2 minutes. When several limits bind
at once the closest to its limit is named, and it stays named while it holds.
Which kinds get named is `MSG_KINDS` (`power, current, thermal, clock`): the
slow charger breathe is clear enough without text, so it isn't. Each message
sent is also written to `sysmon.log` (`Message to the pad: CPU STAPM 28W`).

**Keypad 5 with NumLock off** replays it on demand: the pad names what makes
each row breathe right now, the charger included (`CPU CHARGER`, in amber), or
says NO LIMIT. While the host is streaming and the pad is lit, the pad keeps
that key to itself (with NumLock off it would only send Clear); otherwise it
is an ordinary keypad 5.

The words, the timings (`MSG_ONSET_S`, `MSG_MIN_GAP_S`, `MSG_REARM_S`), the
speed and whether the value is shown are all in the `[messages]` section of
`sysmon.ini`; `MSG_ENABLE = 0` keeps only the replay key. A message needs the
firmware from this version (`--selftest` says whether the module has it);
older firmware just breathes. `bootanim_preview.py msg TEXT` scrolls any text
the same way, over the live bars, for trying wording out.

Detection sources (per component, degrades gracefully if absent):

- **dGPU** — NVIDIA only, via **NVML** (`pip install pynvml`): authoritative
  throttle reasons (`SwPowerCap`, `HwPowerBrakeSlowdown`, the thermal
  slowdowns) plus the enforced power limit. Each must hold for
  `NV_DEBOUNCE_READS` reads before it counts. No NVIDIA card / no pynvml → no
  dGPU limit indication.
- **APU (CPU/iGPU)** — read **authoritatively** from the AMD SMU power-metrics
  table via **`ryzenadj`** (its `libryzenadj.dll`, loaded in-process). This is
  the same source HWiNFO reads for *APU STAPM Limit* etc., and the same table
  `ryzenadj -i` prints: a power or current limit is *binding* once its live
  value reaches `APU_LIMIT_NEAR` (95%) of the limit. Install the tool
  (`scoop install ryzenadj`); the DLL is auto-discovered, or point
  `--ryzenadj-dir` at it. It loads a ring0 driver, so the script must be
  **elevated**.
  - *Fallback (no ryzenadj):* AMD exposes no throttle flags, so the cap is
    *inferred* from **LibreHardwareMonitor** package power + CPU load — flagged
    when, under sustained high load on AC, package power plateaus at the
    ceiling (auto-calibrated; pass `--cpu-power-cap <watts>` to pin it). It
    can't tell which limit it is, so it is named POWER. With neither ryzenadj
    nor LHM only the clock limit can make the CPU row breathe.
- **CPU clock** — Windows' per-CPU performance counters (`% Processor
  Performance`, `% Processor Time`), no extra install: the clock limit above.

LED brightness follows the laptop screen brightness (scaled, capped well
below full brightness). If no stats arrive for 5 seconds (script stopped,
reboot, etc.) the macropad falls back to the regular RGB matrix effect, so
set a calm/dim effect via VIA as the fallback.

## Limit log

The pad shows a limit while it binds; the **limit log** keeps the evidence.
The service records every limit that binds, when, for how long, and what the
machine was doing then, into `limitlog.sqlite` beside `sysmon.ini`. It records
from the same readings and the same SMU handle as the pad, and keeps running
while the pad is unplugged. Read it with the Start menu's **Limit report**, or:

```powershell
fw16-sysmon.exe --limit-report                         # everything so far
fw16-sysmon.exe --limit-report --report-days 7 --export-csv C:\temp\limits
python host\limitreport.py C:\copy\limitlog.sqlite --days 7   # stdlib only, any Python
```

The report has these sections:
- How long the machine was observed, by power source and Windows power mode.
- Per component: each reason's *primary* time (the binding limit closest to
  its own limit), its *bound* time, its share of busy time, and its episodes
  (count, median, p90, longest), broken down by power source and power mode.
- For STAPM, and the top CPU limits, a closer look:
  - the limit's own value (it moves), with temperatures, fans, clocks,
    charging state and the PD contract at each level;
  - the headroom to the slow and fast PPT;
  - clocks while bound, against busy and unbound.
- Every SMU limit change, with what the machine was doing when it changed.
- The busiest processes during each kind of episode.

What is recorded, beyond what the pad reads:

- **Raw reasons**, unfiltered: every SMU limit at its threshold (fast PPT
  included, on battery too, whatever `LIMIT_KINDS` says). It also records the
  skin-temperature (STT) limit, the clock held at base, and the EC's own APU
  throttle flags (`AP Throttle Status`: soft SPPT and PROCHOT#). For the dGPU,
  every NVML throttle bit. The pad's own verdict is recorded next to them.
- **More of the SMU table**: the STAPM and slow PPT time constants, APU-only
  slow PPT, STT limits and values, the boost setpoint, and socket/SoC power and
  clocks. The **raw PM table** is saved as well, at each CPU episode's start
  and every 10 minutes, for decoding the fields libryzenadj doesn't name
  (`--export-csv` writes it out as float columns). libryzenadj decodes the
  extra fields per table version, and some decode wrong: on this FW16's Strix
  Point table (0x5d000b), socket power reads the boost setpoint, and the GFX
  temperature and dGPU STT values are nonsense. They are kept as read; the
  report doesn't use them, and the STAPM / PPT / TDC / EDC / THM pairs check
  out against the raw table.
- **Context**:
  - power: AC or battery, and whether the battery is assisting the charger;
    the USB-PD contract (port, V, A, W); charger input current; battery
    voltage, current and charge state.
  - Windows: the power mode (efficiency / balanced / performance), the plan,
    Energy Saver, and Windows' performance-limit counters.
  - thermals: every EC temperature sensor and both fans.
  - activity: CPU load, busy cores and clocks; dGPU power state, draw, limit,
    temperature and clocks (read only while it's awake, so it isn't woken);
    the top CPU processes.
- **Events**:
  - every change of an SMU limit (STAPM, PPT, current, thermal and the time
    constants), with the full context it changed in. A limit that drifts, as
    STAPM does on Strix Point, logs one each time it has moved 2 W (or A, or
    C) from where the last one left it;
  - AC and charger changes, power mode, charge cap, and the EC throttle flags;
  - suspend/resume, the pad connecting, and every message sent to it.

Samples come every `LIMITLOG_ACTIVE_S` (2 s) while something binds or a
component is busy, every `LIMITLOG_IDLE_S` (30 s) otherwise, plus one at each
episode start/end and event. The CPU counts as busy at 25% total load, or
with a core at `CLOCK_BUSY_PCT` (80%) averaged over about
`LIMITLOG_BUSY_TAU_S` (10 s); the dGPU at 30% or 15 W. That is a few MB per
day of use. Samples and raw tables are kept for `LIMITLOG_RETENTION_DAYS` (90), capped at
`LIMITLOG_MAX_MB`. Episodes, events and the daily tallies are kept for good.
The settings are in `[limitlog]` in `sysmon.ini`; `LIMITLOG_ENABLE = 0` turns
it off. The database uses a rollback journal, not WAL, so it can be read
(and copied) without admin rights while the service writes it. The schema is
described at the top of `host\limitlog.py`.

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
| `C:\ProgramData\FW16Sysmon\limitlog.sqlite` | the [limit log](#limit-log) |
| Start menu | self-test, live console view, edit settings, view log, limit report, flash firmware, restart service |

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
makefile directly, from your checkout (`/c/path/to/qmk_firmware` is its path
in MSYS form). `host\packaging\build.ps1 -BuildFirmware` does all of this
for you.

```powershell
$env:MSYSTEM='MINGW64'
& C:\QMK_MSYS\usr\bin\bash.exe -l -c "cd /c/path/to/qmk_firmware && make -r -R -f builddefs/build_keyboard.mk -j8 KEYBOARD=framework/macropad KEYMAP=sysmon TARGET=framework_macropad_sysmon QMK_BIN=qmk SILENT=false"
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

The **AMD APU limit breathe** comes from `ryzenadj`/`libryzenadj` (the SMU
table; authoritative) with the LibreHardwareMonitor package-power heuristic as
fallback — see [Limit indication](#limit-indication) above. With `--verbose`,
the `limits …` line shows either `cpu ryzenadj[stapm=…/…W slow=…/…W tdc=…/…A
… thm=…/…C]` (a `*` marks a limit binding this tick) or `cpu heuristic(pkg_w=…
load=…)`, then `clock[…GHz busy=…%]` (the busy CPUs' fastest clock and the
busiest core; `HELD` when held at base), then `->` and the limit the row
breathes for, so you can confirm which source is driving the CPU row.
`Message to the pad: …` lines show what the pad is told.

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
  FW16, Tctl sat at exactly 100 °C while the EC read 41 °C, pinning the
  temperature row full and blinking. (That one turned out to be the host
  reading libryzenadj's THM *limit* getter, `get_tctl_temp`, instead of the
  value, `get_tctl_temp_value`; fixed, and a Tctl that never moves is still
  ignored.) Needs elevation.
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
  `TEMP_MIN_C` / `TEMP_MAX_C`, `ASSIST_MIN_W`, `DRIFT_MIN_W`,
  `DGPU_OFF_STEP_S` (how long each key of the dGPU sleep marker stands for)
- **limits**: `LIMIT_KINDS` (which kinds — `power`/`current`/`thermal`/`clock`/
  `charger` — breathe), `APU_LIMIT_NEAR` (how close to a limit counts as
  binding), `APU_LIMIT_SIGNALS` (which SMU power limits — `stapm`/`slow`/`fast` —
  count), `APU_THM_MARGIN_C`, `LIMIT_RELEASE_S`, `CLOCK_BUSY_PCT` /
  `CLOCK_BASE_MARGIN` / `CLOCK_DEBOUNCE_READS` (the clock limit)
- **messages**: `MSG_ENABLE`, `MSG_KINDS` (which kinds the pad names on its
  own), `MSG_ONSET_S` / `MSG_MIN_GAP_S` / `MSG_REARM_S`,
  `MSG_SPEED`, `MSG_SHOW_VALUE`, and every word (`MSG_NAME_*`, `MSG_WORD_*`,
  `MSG_NO_LIMIT`)
- **colours**: the `HUE_*` constants (including `HUE_NPU`, the ghost bar, and
  `HUE_DGPU_OFF`, the dGPU sleep marker) and the temperature gradient endpoints
- **mappings**: `RATE_SEG_W1/2/3` (watts → animated segments),
  `ROW_VAL_BATT_IDLE`, `ROW_VAL_NPU_GHOST`, `ROW_VAL_DGPU_OFF`,
  `BATT_DRIFT_FLOOR`, `LIMIT_FLOOR`
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
               0x04 = GOODBYE (no payload; sent once on a clean exit)
               0x05 = MESSAGE (a line of text over the bars; when there is one)
               0x10 = BOOT REPLAY, 0x11 = REBOOT, 0x12 = TRACE (testing only, see below)
[3..] payload: FRAME A/B: three back-to-back 8-byte row descriptors (FRAME A
               is sent first; FRAME B keeps the display alive: the bars show
               from the first one on, drawn with its globals)
               FRAME C:   six back-to-back 4-byte ghost descriptors
               MESSAGE:   see below

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
FRAME B echo:
  [29] the sequence number of the last MESSAGE dropped unplayed (replaced by a
       newer one, dropped as the pad went dark or the host went away; 0 = none)
  [30] ...and of the last one played to its end
  [31] status      bit0 a MESSAGE is waiting or playing,
                   bit1 the replay key was pressed (reported once; older firmware: 0),
                   bit2 the RGB lighting is off (RGB toggle, or VIA's All Off): nothing shows,
                   bit4 the pad is suspended (lid closed): nothing shows either

ghost descriptor (4 bytes, FRAME C):
  +0 level   0-255  fraction of the 4-segment bar (0 = no ghost)
  +1 hue     0-255  ghost colour
  +2 val     0-255  ghost brightness (the row's envelope scales it too)
  +3 flags   bit0    anchor: 0 = fills from the left, like the main bar;
                     1 = from the right end (level 63 = the last key alone)
             bits1-7 reserved (0)
FRAME C echo:
  [27] 0x01  the firmware draws ghost bars (older firmware echoes 0)
  [28] 0x01  ...and reads their flags (firmware before them draws every ghost from the left)
```

The firmware glides each row's `level`, fills the bar (solid or gradient),
applies the envelope to the whole-row brightness, then paints any overlay
segments from the fill boundary — all timed off its own clock, so the bars stay
smooth between the host's updates. A ghost bar glides the same way and adds,
per segment, whatever part the ghost covers beyond the main fill; a
right-anchored one fills from the last key leftward and shows in what the main
bar leaves unfilled at that end. A ghost sent at level 0 glides out in the
colour and at the end it had. VIA's echo is the ACK.

When frames stop, what happens depends on how. After a GOODBYE the firmware
restores the stock global current and hands over to the regular RGB effect at
once. If frames just stop, it waits `timeout`, shows the disconnect warning for
10 s, and then hands over. After a USB suspend it skips the warning. The next
FRAME B cancels all of this; the bars cross-fade back in over 0.7 s from the
boot rainbow or the warning, and cut in from the regular effect.

FRAME C is optional in both directions. Firmware that predates it ignores value
`0x03` and draws the plain bars. Firmware that has it fades the ghosts out on
their own when FRAME C stops arriving within `timeout`, so an older host can
never leave a stale ghost behind. Ghost-capable firmware also sets byte `[27]`
of its FRAME C echo to `0x01` (older firmware echoes the frame unchanged).
`--selftest` reads the same from the BOOT REPLAY status's capability byte, so
it sends no frames (a frame would show, and end the boot rainbow); firmware
from before that byte predates this host, and the self-test says to flash the
bundled image.
Byte `[28]` says the firmware reads the ghost flags; the host sends a
right-anchored ghost only once it has seen that, since firmware without the
flags would draw it at the wrong end.

MESSAGE (`0x05`) is how the host names a limit (see [The pad names the
limit](#the-pad-names-the-limit)). The bars dim out over 0.3 s, the text
scrolls through on dark at a steady pace, in the boot text's font, and once
its last column is gone the bars fade back in the way they do after the boot
rainbow. The bars keep running underneath, so they return as they are by then.
A message waits until the bars show and have settled (not mid-fade), is dropped
if the host goes away first, and never plays over the boot rainbow or the
disconnect warning. A new one replaces one waiting or playing; the host sends
the next only once the FRAME B echo reports this one played or dropped (by its
sequence number), and counts a limit as named only once its message played.
With older firmware, which can't say, it waits for the echo's bit0 to clear
instead, and counts a limit as named as its message is sent.

```text
[3]      style: 0 = ticker (others reserved; played as the ticker)
[4] [5]  hue, saturation of the text (saturation 0 = white)
[6]      speed, columns per second (0 = 8; 2..40)
[7]      sequence number, 1..255 (0 = none), reported back in the FRAME B echo
[8..30]  text, NUL-terminated, up to 23 characters (A-Z, 0-9, space, - ! .;
         uppercase W and M are the tall forms). Empty: nothing happens -- a probe
echo [31] 0x01: the firmware shows messages (older firmware echoes it unchanged)
     [9]  0x02, in the probe's echo only: ...and reports each one's outcome
          (FRAME B echo [29..30])
```

The replay key is keypad 5 while NumLock is off. The firmware catches it by
keycode rather than through the keymap (VIA keeps the keymap in EEPROM, where a
new default would never reach a pad that already has one), in the pre-process
hook, so neither its press nor its release reaches the computer -- but only
while the host is streaming and the pad is lit (dark, there is nothing to show
the answer on): a host that never reports NumLock (macOS, a pre-boot
environment) keeps an ordinary keypad 5. The host answers through the FRAME B
echo's bit1.

Value `0x10` (BOOT REPLAY) isn't sent by the service; it replays boot
animations for previewing (`host/bootanim_preview.py`), and its `0xFD` status
query, which changes nothing, is how `--selftest` identifies the firmware. It
never counts as host data, so it doesn't end the pre-connect rainbow:

```text
[3] first variant, 1-based (0 = all; 0xFC = preview warning styles [4]..[5];
    0xFB = custom text, see below; 0xFD = status query only;
    0xFE = the next power-up pick, unnumbered; past the end = stop)
[4] last variant (0 = only the first)
[5] rounds (0 = 1)
[6..7] speed, percent of normal (u16 LE; 0 = 100; 10..1000): the animations
       play that much faster or slower; the naming dots, the dark gap before
       and the hold after each don't change
echo [11..14] boot counter in RAM (u32 LE)   [15] this power-up's pick (1-based)
     [16..19] boot counter in EEPROM (u32 LE; lags by one until the boot animation ends)
     [20]     display: 0 = regular RGB effect, 1 = boot rainbow, 2 = bars, 3 = warning,
              4 = nothing (the lighting off, or the pad suspended)
     [21]     1 while an animation is playing (the power-up one too)
     [22]     1 while a MESSAGE waits or plays
     [23]     capabilities: bit7 set (older firmware: 0), bit0 ghost bars,
              bit1 ghost flags, bit2 messages
     [24]     1 while the RGB lighting is on, 0 while it is off (RGB toggle, or VIA's All Off)

custom text (0xFB): [4] style 0 flash / 1 ticker / 2 pop, [5] colour 0 rainbow /
1 per letter / 2 white, [6..7] speed (u16 LE; 0 = default), [8..] text, NUL-terminated
or 23 characters
```

Value `0x11` (REBOOT, no payload) restarts the macropad about 300 ms after the
echo, exactly as a power-up would, for trying the startup without replugging
(`bootanim_preview.py reboot`). If the boot animation is still playing, its
pick is saved first, so the next start moves on to the next pick.

Value `0x12` (TRACE) reads out the LED flight recorder (`bootanim_preview.py
trace`):

```text
[3] 0 = status, 1 = freeze (stop recording, so it can be read), 2 = read,
    3 = restart (clear and record again), 4 = read saved, 5 = save (as
    holding the save key does)
read:   [4..7] byte offset (u32 LE) into the frozen frames, oldest first;
        echo [8..31] = the next 24 bytes
read saved: [4..7] byte offset (u32 LE) into the saved traces: slot k at
        k x 64 KiB, a 256-byte header ([0..3] magic "TRC1", [4..7] save
        number, the highest the newest, [8..11] ms after power-up it was
        saved, [12..15] the power-up counter, [16..17] frames, [18] frame
        size, [19] 1 the key, 2 the host), then its frames, oldest first;
        echo [8..31] = the next 24 bytes
others: echo [8..9] frames held (u16 LE), [10] frame size (76), [11] frozen,
        [12..13] frames the ring holds (u16 LE), [14] save slots (0: this
        firmware can't save), [15] a save under way, [16] slot size in KiB
frame (76 bytes): [0..1] ms (the timer's low 16 bits), [2] global current,
        [3] bits 0-1 display (as data[20] of BOOT REPLAY), bits 2-3 current
        change (1 rising: PWM sent first, 2 falling: current first), bit 4
        fading into the bars, bit 5 a message showing; [4..75] PWM, pad rows
        top to bottom, keys left to right, R G B
```
