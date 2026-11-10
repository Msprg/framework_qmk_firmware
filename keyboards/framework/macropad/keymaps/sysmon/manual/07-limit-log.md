# 7. The limit log

[Manual](README.md) › The limit log

The pad shows a limit while it binds, and names it once. The **limit log**
keeps the evidence: every limit that bound, when, for how long, and what the
machine was doing at the time. Over days or weeks it answers questions the
pad can't:

- How often does STAPM hold my CPU back, and for how long each time?
- Is it worse on one charger than another, or in one Windows power mode?
- Does the dGPU hit its power limit in the games I play?
- What was running when it happened?
- How does STAPM move with temperature, and how much headroom is there to the
  next limit?

The service writes it all the time, from the same readings that drive the pad,
**whether or not the pad is plugged in**.

## What it records

- **Every limit that binds, unfiltered.** That includes limits the pad
  ignores by design (the fast boost limit, limits on battery, kinds left out
  of `LIMIT_KINDS`), and some the pad never shows: the skin-temperature limit,
  and the embedded controller's own throttle signals. What the pad showed is
  recorded alongside.
- **Episodes.** Each limit that holds for 2 seconds or more
  (`LIMITLOG_ONSET_S`) becomes an episode, with its start, end, how close the
  value came to the limit, temperatures, clocks, the power situation, and the
  busiest processes.
- **Samples.** Every 2 seconds while a limit binds or the CPU or dGPU is busy,
  and every 30 seconds otherwise. Each one records:
  - **power:** AC or battery, whether the battery is helping the charger, the
    USB-C charger's contract (port, volts, amps, watts), and the battery's
    charge and current. Only a port the laptop actually draws power from
    counts: a dock or monitor can hold a contract it isn't drawing on. The
    rear USB-C port on the Framework 16's graphics module isn't among the
    ports `framework_tool` lists, so with the charger there the contract stays
    empty (`-` under `PD W` in the report), and the sample notes that the
    charger is on an unlisted port (`pd_unlisted` in the CSV export);
  - **Windows:** the power mode (best efficiency / balanced / best
    performance), the power plan, Energy Saver;
  - **thermals:** every temperature sensor of the embedded controller, and
    both fans;
  - **activity:** CPU load and clocks, and the dGPU's power state, draw, limit,
    temperature and clocks (read only while the dGPU is awake, so logging
    never wakes it);
  - **the processor's power table,** beyond what the pad uses.
- **Events.** Every change of a processor limit, with the full context it
  changed in. STAPM drifts constantly, so it is logged each time it has moved
  2 W from where the last event left it. Also: plugging in and unplugging,
  charger changes, power mode changes, charge-limit changes, sleep and
  resume, the pad connecting, and every message sent to it.
- **Daily tallies.** Seconds bound per day, per limit, per power source and
  per power mode.

**Privacy.** The log stays on your laptop. Nothing is sent anywhere. It does
record the names of the busiest processes, though, so think before sharing a
copy.

**Size.** A few MB per day of use. Samples are kept for 90 days
(`LIMITLOG_RETENTION_DAYS`) and capped at 1 GB (`LIMITLOG_MAX_MB`), oldest
first. Episodes, events and daily tallies are small and kept for good.

The file is `C:\ProgramData\FW16Sysmon\limitlog.sqlite`, a SQLite database.
Anyone can read it, even while the service writes it, and you can copy it
elsewhere. Only the service writes to it.

## The report

**Start menu → Limit report**, or from an elevated terminal:

```powershell
$exe = "C:\Program Files\FW16 Macropad Sysmon\fw16-sysmon.exe"
& $exe --limit-report                                   # everything so far
& $exe --limit-report --report-days 7                   # the last week
& $exe --limit-report --export-csv C:\temp\limits       # also every table as CSV
& $exe --limit-report --limitlog-db D:\copy\limitlog.sqlite   # a copy
```

The report reader is also a standalone script with no dependencies. With any
Python 3.8 or newer, and no admin rights, it reads the live file or a copy:

```powershell
python limitreport.py C:\ProgramData\FW16Sysmon\limitlog.sqlite --days 7 --csv C:\temp\limits
```

The script is
[`host/limitreport.py`](https://github.com/Msprg/framework_qmk_firmware/blob/macropad-sysmon/keyboards/framework/macropad/keymaps/sysmon/host/limitreport.py) in this repository.

### Reading it

What follows is an **invented example**, shortened, to show the layout. Your
numbers will differ.

```text
Limit log   C:\ProgramData\FW16Sysmon\limitlog.sqlite  (41.3 MB)
Machine     Laptop 16 (AMD Ryzen AI 300 Series), AMD Ryzen AI 9 HX 370 w/ Radeon 890M, BIOS 03.05 (01/15/2026)
Window      since 2026-03-01 22:45; samples 2026-03-01 22:45 .. 2026-03-08 22:40
            (the times from the tallies cover whole days, from 2026-03-01 00:00; episodes and samples start at the window)
Rows        96210 samples, 214 episodes, 905 events

Observed 61.5 h (the service running, the machine awake)
  by power source: ac 48.2 h (78%), battery 13.3 h (22%)
  by power mode: balanced 40.1 h (65%), efficiency 21.4 h (35%)
```

**The header** says which file, which machine, which time window, and how much
data. With a window (`--report-days`), the times that come from the daily
tallies (Observed, and each limit's primary and bound time) cover whole days,
from midnight of the window's first day, so they can count up to a day more
than the window. Episodes, samples and events start exactly at the window.
**Observed** is the time the service ran with the laptop awake, split by
power source and Windows power mode. All the percentages below are relative
to it.

```text
== CPU (the whole APU): what held it back ==
  busy (a core at 80%+) 19.6 h (32% of observed); limited 3.1 h, of which while busy 2.4 h (12% of busy time)
  primary = the closest to its limit of those binding at once; %busy = bound while busy / busy time
  reason   primary   bound  %busy  episodes  median    p90  longest
  stapm       2.2 h   2.4 h    11%       151    25 s  4m10s   38m02s
  fast       34m10s  51m22s     3%        40    12 s    48 s    3m15s
  thm        11m45s  12m03s     1%         9    41 s  2m50s    4m05s
  primary time by power source:
    stapm        ac 2.0 h (4% of its time); battery 12m30s (2% of its time)
  primary time by power mode:
    stapm        balanced 1.8 h (5% of its time); efficiency 24m (2% of its time)
```

**Per component** (CPU, then dGPU):

- **busy** is how long the component had real work (CPU: a core at 80 %+;
  dGPU: 30 %+ or drawing 15 W+). **limited** is how long any limit bound,
  and how much of the busy time that was. A limit on an idle chip costs
  nothing. A limit during busy time is what slows things down.
- One row per limit:
  - **primary**: the time it was the binding limit closest to its own limit.
    When several bind at once, only one is primary, so the primary times add
    up to the total.
  - **bound**: all the time it bound, primary or not.
  - **%busy**: bound while busy, as a share of busy time.
  - **episodes**, **median**, **p90**, **longest**: how many separate spells,
    and how long they typically and at worst lasted.
- **By power source / power mode** splits the primary time, each as a share
  of the time spent in that source or mode. This shows whether a limit is a
  charger problem or a power-mode problem.

The limit names here are the raw ones:

| Name | Limit |
|------|-------|
| `stapm` | Sustained power budget (STAPM) |
| `slow` / `fast` | Slow / fast package power limit (PPT) |
| `tdc` / `tdc_soc` | Sustained current limit, cores / SoC |
| `edc` / `edc_soc` | Peak current limit, cores / SoC |
| `thm` | Processor temperature limit |
| `stt_apu` | Skin-temperature limit (when the processor's table has one) |
| `base_clock` | Held at base clock under load (boost off) |
| `pkg_power` | Package power at its ceiling (the estimate used without ryzenadj) |
| `ec_sppt` | The embedded controller asked the processor to throttle (AMD's soft package power limit) |
| `prochot` | The embedded controller asserted PROCHOT, a hard emergency throttle |
| `power` (dGPU) | At its power limit |
| `brake` (dGPU) | Hardware power brake |
| `sw_thermal` / `hw_thermal` (dGPU) | Thermal slowdown, by the driver / by the hardware |
| `hw_slowdown` (dGPU) | Hardware slowdown with neither brake nor thermal reason |

When the embedded controller throttles, it's the cause and the processor's
limit is the effect, so `ec_sppt` and `prochot` take precedence over the
limit they lower.

```text
== stapm: while it was the CPU's primary limit ==
  by the limit's own value (time-weighted; it moves):
    limit    time  share  value  busy GHz  slow PPT  pkg W  EC apu C  EC amb C  fan rpm  load %  charging  PD W
    30W    48m10s    37%   29.8      3.52       71%   29.9        66        58     3900      41        0%    96
    35W    55m02s    42%   34.7      3.88       83%   34.8        61        52     3350      38        0%    96
    40W    26m40s    20%   39.6      4.10       94%   39.7        57        47     2700      33       12%    96
  headroom: STAPM 34.1 W against slow PPT 42.0 W and fast PPT 63.0 W; slow PPT sat at 81% of its limit. Raising STAPM to the slow PPT would allow +7.9 W (+23%) before the next limit.
  all-core load (>=80%): stapm bound 18m, busy CPUs at 2.95 GHz (fastest 3.40), 34.2 W package; nothing bound 9m, 3.30 GHz (fastest 3.85), 29.0 W
```

**The deep dive** covers STAPM, and the top one or two processor limits if
they're different:

- **By the limit's own value.** STAPM moves, so the time is split by what
  the limit was set to, with what else was true at each level: temperatures,
  fans, clocks, load, charging, and the charger's wattage (`PD W`). Look for
  the limit falling as `EC amb C` (the chassis) warms.
- **Headroom.** How far the next limit is. It says how much more power the
  processor could use if this limit were raised.
- **Clocks while bound vs. not**, at full and partial load. They say what
  the limit costs in GHz.

```text
== SMU limit changes: 312 ==
  changes per limit: stapm_l 305, slow_l 4, fast_l 3
  the last 20, with what the machine was doing:
    when                       changed    source  mode      PD W  battery    apu C  amb C   fan  load%  stapm W
    2026-03-08 21:02  stapm_l 40.1->38.0   ac  balanced    96  idle 0W     55     49  2100     12     14.3
```

**SMU limit changes** lists every time a processor limit moved, with the
situation at that moment: power source, power mode, charger wattage, battery
state, temperatures, fan and load. This is where you see what makes STAPM
drop or rise.

```text
== Busiest processes during episodes (CPU % of the machine, time-weighted) ==
  cpu stapm        game.exe 21%, browser.exe 4%, compiler.exe 3%, System 1%

== Events ==
  smu_limits 305, pad 12, message 46, start 9, stop 8, pd_contract 6, ac 4, power_mode 3
    2026-03-08 22:38  message      {"text": "CPU STAPM 35W", "replay": false}
```

**Busiest processes** shows what ran during each kind of episode. **Events**
counts every kind of event, then lists the last ones.

### CSV export

`--export-csv DIR` (or `--csv DIR` with the script) also writes every table
(samples, episodes, events, tallies) as CSV files, to open in a spreadsheet.
The raw processor power tables are written as numeric columns too.

## Settings

The `[limitlog]` section of [`sysmon.ini`](06-settings.md#limitlog) sets the
sample intervals, what counts as busy, how often the slower readings are
taken, how long data is kept and the size cap. `LIMITLOG_ENABLE = 0` turns the
log off. The pad works the same either way.

---

Previous: [6. Settings](06-settings.md) · Next: [8. Troubleshooting](08-troubleshooting.md)
