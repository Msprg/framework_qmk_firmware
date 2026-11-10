# 8. Troubleshooting

[Manual](README.md) › Troubleshooting

**Start with the self-test**: Start menu → **Self-test (what works on this
machine)**. Most problems show up there as a `[warn]` or `[FAIL]` line with a
remedy printed under it. [Below](#the-self-test-line-by-line) is what each
line means. The second place to look is the log: Start menu → **View log**.

## By symptom

### The pad shows the rainbow and never the bars

The host isn't talking to the pad.

- **The service isn't running.** Start menu → **Restart the service**, then
  check the self-test's *Service* line. If it stops again, see the log.
- **The pad doesn't run the sysmon firmware, or runs an old one.** Check the
  self-test's *sysmon firmware* line. Fix: Start menu → **Flash firmware onto
  the macropad**.
- **Another program holds the pad.** See
  [The display jumps between states](#the-display-jumps-between-states).

### The pad shows its regular RGB effect, not the bars

The host stopped. If it stopped cleanly, the pad switched over at once. If it
crashed, you'd have seen the yellow and black stripes first.

- Start menu → **Restart the service**.
- If the service keeps stopping, look at the end of `sysmon.log` for the
  error, usually a Python traceback.
- After a manual **Live view**, the service stays stopped until you start it.

### Yellow and black stripes appeared

The host stopped sending without saying goodbye: it crashed, was killed, or
hung. The pad switches to the regular effect after 10 seconds. If it was a
one-off (you ended the process in Task Manager, say), restart the service and
move on. If it happens repeatedly, the log says why.

An **older host version** didn't say goodbye at all, so every stop or
shutdown showed the stripes. If you see them at every shutdown, update the
host.

### The pad is completely dark

- **The lights are switched off.** Turn NumLock off and press the
  **top-left key**. VIA's All Off does the same thing, and the same key
  undoes it. The self-test's *sysmon firmware* line and the log both say when
  this is the cause.
- **The laptop is asleep**, or the lid is closed. The pad has no power then.
- **Only when the host is stopped:** the regular effect's brightness was
  stepped down to off. Turn NumLock off and press the brightness step key
  (row 5 or 6, last key).
- **The module isn't seated.** Remove it and reinsert it. The boot rainbow
  should appear.

### The whole pad flashed white for an instant

You held the top-left key for a second or more. That saves a diagnostic
recording of the LEDs to the pad, and the white flash confirms it. Nothing
else changed. See
[The top-left key](04-keys-and-lighting.md#the-top-left-key-tap-or-hold-to-save-a-trace).

### Esc (or the lights key) only acts when released, and doesn't repeat

That's by design: the top-left key waits to see whether it's a tap or a
one-second hold. See
[The top-left key](04-keys-and-lighting.md#the-top-left-key-tap-or-hold-to-save-a-trace).

### The display jumps between states

The bars flicker, rows jump, or the rainbow and the bars alternate. Two
programs are writing to the pad. The macropad has one data connection, and
the service must be the only program driving it.

- A **Live view** console, or `fw16-sysmon.exe --verbose`, running while the
  service runs. The console prints a warning. Close it, then restart the
  service.
- The old **"FW16 Macropad Sysmon" scheduled task** from before the installer
  existed. The installer removes it. Check Task Scheduler if you set it up
  again by hand.
- A development run of the host from source.

### A row stays dark

| Row | Likely cause | Check / fix |
|-----|--------------|-------------|
| Battery | No battery found, or Windows doesn't report its state | Self-test *Battery state* |
| CPU | Rare. The row shows any non-zero usage. | n/a |
| iGPU | Windows' GPU counters aren't ready yet (common for a minute after boot; the service retries every 10 s), or nothing is drawing | Self-test *GPU counters (PDH)* |
| dGPU | No dGPU fitted, or it wasn't recognised. NVIDIA cards are found through their driver. Other cards' names must match `dgpu-match`. | Self-test *NVIDIA dGPU* and *GPU counters (PDH)*, which lists the adapters. Set `dgpu-match` in [`[cli]`](06-settings.md#command-line-defaults-cli) if your card's name isn't matched. |
| Temperature | No temperature source works | Self-test *Temperature source*. Install `framework_tool` (`winget install FrameworkComputer.framework_tool`). |

A dGPU at 0 % usage looks empty too. If a game runs but the dGPU row stays
low, check whether the game is minimised or out of focus. Many games throttle
themselves then.

### The temperature row is full and blinking, but the laptop isn't hot

A temperature source is giving a wrong reading. The self-test's *Temperature
source* line shows what **every** source reads, and which one is used:

- Install `framework_tool`. It reads the embedded controller and outranks the
  processor's own reading.
- Or turn off the source that's wrong: `no-ryzenadj = true` or
  `no-lhm-lib = true` in [`[cli]`](06-settings.md#command-line-defaults-cli).
- Check the self-test's *Service's own view* line too. If it says the service
  couldn't find `framework_tool` while your session can, set its path. See
  [the recipe](06-settings.md#recipes).

### The CPU row never breathes

- **Nothing binds.** On a cool laptop under moderate load, often nothing
  does. Press the replay key (NumLock off, keypad 5). `NO LIMIT` is an honest
  answer.
- **You're on battery.** Power and clock limits are only shown on AC, by
  design.
- **ryzenadj can't load.** Usually Memory Integrity blocks its driver. Check
  the self-test's *AMD APU power limits* and *Ring0 driver* lines, and see
  [Memory Integrity](10-faq-and-glossary.md#memory-integrity). Without
  ryzenadj, only the clock limit can make the CPU row breathe.
- **It's switched off** in `LIMIT_KINDS`.

### The dGPU row breathes all the time

NVIDIA's driver reports its power limit eagerly. The host filters that out,
but you can tighten the filter: raise `NV_NEAR_CAP` (default 0.92) or
`NV_DEBOUNCE_READS` (default 3), or drop `power` from `LIMIT_KINDS`. See
[Recipes](06-settings.md#recipes).

### Messages never scroll

- `MSG_ENABLE = 0`, or the limit's kind isn't in `MSG_KINDS`.
- The limit doesn't last: a message needs it held for 3 seconds
  (`MSG_ONSET_S`), and it's named once per episode.
- The pad is dark, or showing the boot rainbow.
- The firmware is too old. The self-test's *sysmon firmware* line says so.
- To check whether messages are being sent, search `sysmon.log` for
  `Message to the pad`.

### Keypad 5 types "5", or does nothing, instead of answering

- NumLock is on. The replay key only works with NumLock off.
- The host isn't running, or the pad is dark. Then the key goes to Windows as
  usual (with NumLock off, that's "Clear", which does nothing visible).

### The battery bar is full, but the battery isn't at 100 %

That's expected. The bar is scaled to the battery charge limit set in the
BIOS or with `framework_tool`. At an 80 % limit, 80 % shows as full. If you'd
rather see the raw percentage, that's what Windows' battery icon shows.

### The battery bar never reaches full

`framework_tool` isn't installed, or the service can't find it. The bar is
then scaled to 100 %, and a battery held at an 80 % limit stops at 80 %. Check
the self-test's *framework_tool (EC)* and *Service's own view* lines.

### The battery row is amber while plugged in

The charger can't supply what the laptop is using, so the battery makes up the
difference. This is common with low-power USB-C chargers, and with a monitor's
USB-C port powering the laptop. Use a stronger charger, such as the Framework
one. Expect the CPU row to breathe slowly, or show `CPU CLK LIMITED`, at the
same time.

### The pad's brightness doesn't follow the screen

The screen brightness can't be read, so the host assumes 30 %
(`fallback-brightness`). This happens with the built-in screen turned off,
for example when only an external monitor is in use. Check the self-test's
*Screen brightness* line.

### The dGPU sleep marker never appears

Something keeps the dGPU awake: a browser, a game or an app using it, an
external monitor wired to the dGPU's video output, or a vendor overlay.
Task Manager's GPU page shows which processes use the dGPU. The marker also
needs the current firmware.

### The sleep marker keeps restarting from one key

Something wakes the dGPU briefly now and then. Each wake starts the count
over. That's by design: the marker shows how long the card *really* stayed
off. Restarting the host also wakes it once.

### I changed a setting and nothing happened

- **The service wasn't restarted.** Start menu → **Restart the service**.
- **The file didn't save.** Only administrators can write it. Save from
  Notepad started with *Run as administrator*.
- **The setting was ignored.** A typo, an out-of-range value, or the wrong
  section. The self-test's *Settings file* line names ignored entries, and
  the log says why.
- **The encoding is wrong.** The file must be UTF-8, not UTF-16.

### Flashing failed

The flasher prints what went wrong. Common cases:

- **"No Framework macropad found."** The module isn't attached, or it's the
  numpad. The flasher then explains the manual route: unplug the module, hold
  the **top-left and bottom-left keys** while inserting it, and an `RPI-RP2`
  drive appears. Run the flash again, or copy
  `C:\Program Files\FW16 Macropad Sysmon\framework_macropad_sysmon.uf2` onto
  that drive.
- **The `RPI-RP2` drive is still there afterwards.** The image wasn't accepted.
  Copy it onto the drive by hand.
- **Another RP2040 board is connected** (a Raspberry Pi Pico, say). Every
  RP2040 shows the same kind of drive, so the flasher refuses to guess.
  Disconnect the other board.

### The installer says the service couldn't be registered

Close the Services console and Task Manager, which can hold the service
open, or restart Windows, then run Setup again.

## The self-test, line by line

| Line | `[warn]` / `[FAIL]` means | What to do |
|------|---------------------------|------------|
| **Elevated** | The test runs without admin rights, so some probes can't work | Run it from the Start menu (it asks for admin), or from an elevated terminal. The service itself always has the rights. |
| **Macropad attached** | No macropad; the numpad module instead; or a pad without the data connection (non-VIA firmware) | Insert the RGB Macropad. Flash the sysmon firmware. |
| **sysmon firmware** | No answer: not sysmon firmware. *Older sysmon firmware*: from before this host version. *Lighting switched off*: the lights-off key. | **Flash firmware onto the macropad**, or switch the lights on (NumLock off, top-left key) |
| **AMD APU power limits** | ryzenadj found but can't start (usually the driver is blocked), or its files are missing | See *Ring0 driver*. Missing files: reinstall. Shows `--` on Intel. |
| **Ring0 driver** | Memory Integrity is on, which blocks the sensor driver | Your choice. See [Memory Integrity](10-faq-and-glossary.md#memory-integrity). |
| **dGPU power state** | Information only: `D0` on, `D3hot` light sleep, `D3cold` off | n/a |
| **NVIDIA dGPU** | The driver didn't answer. *Powered down*: normal on hybrid graphics, as the service connects when the card wakes. | Repair the NVIDIA driver if it says init failed. |
| **LibreHardwareMonitor lib** | The secondary sensor source can't read the processor | On AMD, ryzenadj covers it. Same driver caveat. |
| **framework_tool (EC)** | Not installed, or not elevated | `winget install FrameworkComputer.framework_tool`. Optional, but it gives the battery charge-limit scaling and a reliable temperature. |
| **Temperature source** | No source, a reading at or above 90 °C, or only the frozen ACPI zone | Compare the listed sources. See [above](#the-temperature-row-is-full-and-blinking-but-the-laptop-isnt-hot). |
| **Screen brightness** | Can't be read, so the pad uses `fallback-brightness` | Usually only with the built-in screen off |
| **Battery state** | Detailed rates unavailable, so the battery row uses a coarser reading | n/a |
| **GPU counters (PDH)** | Windows' GPU counters aren't available yet, or nothing is drawing | Wait a minute after boot, or re-run with something on screen |
| **Settings file** | It doesn't parse (the whole file is ignored), or some entries were ignored | Fix the named entries. If the whole file fails, re-save it as UTF-8 or regenerate it ([After an update](06-settings.md#after-an-update)). |
| **Service's own view** | The service sees less than your session, typically no `framework_tool` | Set `framework-tool` in `[cli]` to the path the line shows |
| **Limit log** | No sample in the last 2 minutes | Is the service running? Check the log for `Limit log` lines. |
| **Service** | Not installed, or not running | **Restart the service**, or reinstall |

## Reporting a problem

Useful things to include:

- the **self-test** output (or `fw16-sysmon.exe --selftest --json`);
- the end of **`sysmon.log`**, from the service start to the problem;
- a few seconds of **live view** output while the problem shows (stop the
  service first);
- what the pad showed, ideally a short phone video, because LEDs are hard to
  describe.

Remove anything you'd rather not share. The log contains paths and adapter
names, and the limit log contains process names.

---

Previous: [7. The limit log](07-limit-log.md) · Next: [9. Installing, updating, flashing, removing](09-install-update-remove.md)
