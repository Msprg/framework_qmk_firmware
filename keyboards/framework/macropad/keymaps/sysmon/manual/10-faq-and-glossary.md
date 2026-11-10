# 10. FAQ, limitations and glossary

[Manual](README.md) › FAQ, limitations and glossary

## FAQ

**Does it work without the host program?**
The pad works as a normal keypad, and its regular RGB effect runs. The bars
need the host: the macropad is a plain USB device with no access to the
laptop's sensors, so something on the laptop has to measure and send.

**Does it work on Linux or macOS?**
The host is Windows-only. On other systems, the pad works as a keypad with
its regular RGB effect, after the boot rainbow, which ends when you press a
key.

**Does it slow the laptop down or drain the battery?**
Barely. The host wakes every 0.4 s, and most readings are cached for a second
or more. The LEDs draw little at the dim brightness used. The host takes
care not to wake the dGPU: it reads the power state from Windows, not from
the card.

**Does it work before I sign in?**
Yes. The service starts at boot, so the bars show at the sign-in screen.

**Why is the pad dimmer than when it shows its regular effect?**
The bars follow your screen brightness and are capped well below the LEDs'
maximum. The regular effect uses its own brightness. To make the bars
brighter, raise `max-led` (see [Recipes](06-settings.md#recipes)).

**Can I choose what each row shows?**
Colours, thresholds and timings can all be changed, but the six rows and
their order are fixed.

**Can I use the pad as a keypad while it's a monitor?**
Yes. Typing is unaffected. The only key the sysmon firmware takes over is
keypad 5 with NumLock off, and only while the host is running.

**Can I still use VIA?**
Yes. See [VIA](04-keys-and-lighting.md#via).

**Why does the CPU row also cover the iGPU?**
On the Ryzen APU, the CPU cores and the iGPU share one chip and one set of
power limits. A heavy graphics load can make the "CPU" hit STAPM even when
the cores are idle.

**Why do I see `CPU STAPM` with a different number each time?**
STAPM moves with the chassis temperature, so the number is the limit as it
stands at that moment. The [limit log](07-limit-log.md) shows how it moves.

**Is a limit bad?**
No. Limits are how the laptop stays within its power, current and
temperature budgets. The pad is there so you know why things are as fast as
they are. Worth acting on: an amber battery row (charger too weak),
`CLK LIMITED` (usually the same cause), and frequent `HOT` at moderate load
(cooling).

**Can sysmon raise the limits?**
No. It only observes. It never changes any processor or GPU setting.

**What does the host send to the internet?**
Nothing, in normal operation. Two exceptions, both ones you start yourself:
winget installing `framework_tool` during Setup, if you tick that box, and the
developer option `--fetch-lhm`, which downloads LibreHardwareMonitor from
NuGet. The installed program already includes it.

## Memory Integrity

Windows' **Core Isolation → Memory Integrity** setting blocks old kernel
drivers. That includes WinRing0, the driver that ryzenadj and
LibreHardwareMonitor use to read the processor. With Memory Integrity **on**:

- the CPU row can't breathe for STAPM, PPT, TDC, EDC or thermal limits (it
  still can for the clock limit);
- the processor's own temperature reading is unavailable. Temperature still
  works through `framework_tool`, so install it.

Everything else works. Turning Memory Integrity off (Windows Security →
Device security → Core isolation → Memory integrity, then restart) restores
those features. It's a real security trade-off, and the choice is yours. The
installer's system check and the self-test both say when this applies.

## Known limitations

The host degrades one row at a time rather than refusing to run. Most of
these cost a single indicator:

| Situation | What you lose |
|-----------|---------------|
| Memory Integrity on | The processor limit breathe (except the clock limit), and the processor's own temperature reading. See above. |
| Intel Framework 16 | No AMD power table: processor power and temperature come from LibreHardwareMonitor instead, so limits aren't named as specifically. |
| No dGPU | Row 5 stays dark. |
| AMD dGPU | Usage shows, but not its limits, which need the NVIDIA driver. |
| No `framework_tool` | The battery bar is scaled to 100 % instead of your charge limit, and one temperature source is missing. |
| Built-in screen off (external monitor only) | Brightness can't follow the screen, and uses `fallback-brightness`. |
| Another program driving the pad | The display jumps. Only one program may drive it. |

## Glossary

| Term | Meaning |
|------|---------|
| **APU** | The AMD processor: CPU cores and iGPU (and NPU) on one chip |
| **iGPU** | The graphics built into the processor (Radeon 890M on the Ryzen AI 9 HX 370) |
| **dGPU** | The discrete graphics card in the Framework 16's expansion bay |
| **NPU** | The processor's AI accelerator ("Ryzen AI", XDNA) |
| **D3cold** | A device's deepest power state: completely powered off. The dGPU enters it when nothing uses it. The pad shows it as the teal sleep marker. (**D0** is on.) |
| **D3hot** | A device's light sleep: asleep, but its connection (for the dGPU, the PCIe link) is still up. The dGPU passes through it for a few seconds on its way to D3cold. The pad shows it as the orange sleep marker. |
| **STAPM** | Skin Temperature Aware Power Management: AMD's sustained power budget, moved with the chassis temperature |
| **PPT** | Package Power Tracking: the chip's power limits, a *slow* (sustained) and a *fast* (short boost) one |
| **TDC / EDC** | Thermal / Electrical Design Current: the sustained and peak current limits, for the cores and for the SoC |
| **Tctl / THM** | The processor's control temperature, and its limit |
| **SMU** | The processor's System Management Unit, which enforces the limits and publishes them in a power table |
| **ryzenadj** | The open-source tool whose library reads the SMU's power table |
| **NVML** | NVIDIA's management library, part of the driver. It reports usage and why the card is slowed. |
| **EC** | The laptop's embedded controller: battery, charging, fans, temperatures |
| **framework_tool** | Framework's command-line tool for talking to the EC |
| **Base clock** | The clock a core runs at without boost. "Held at base" means boost is off. |
| **QMK** | The open-source keyboard firmware the pad runs. The sysmon firmware is a QMK keymap. |
| **VIA** | A tool for remapping QMK keyboards and setting their lighting |
| **Host** | The FW16 Macropad Sysmon program on the laptop |
| **Episode** | One continuous spell of a limit binding |
| **Bound / binding** | A limit is binding when the chip is at it and held back by it |

---

Previous: [9. Installing, updating, flashing, removing](09-install-update-remove.md) · Back to the [manual](README.md)
