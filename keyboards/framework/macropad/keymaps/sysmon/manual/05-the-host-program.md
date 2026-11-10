# 5. The host program

[Manual](README.md) › The host program

**FW16 Macropad Sysmon** is the program on the laptop that feeds the pad. It
runs as a Windows **service** named `FW16Sysmon`:

- It **starts at boot**, before anyone signs in, so the bars are live at the
  sign-in screen. Nothing needs to stay open on the desktop.
- It runs as **LocalSystem**. Some sensors (the processor's power table, the
  embedded controller) need administrator rights, and a service has them
  without asking.
- It reads the sensors and sends the pad an update **every 0.4 seconds**
  (`interval` in the settings). Most readings are cached for a second or two,
  so the CPU cost stays small.
- It copes with the pad coming and going. Unplug the module and the service
  waits, and picks it up again within about 5 seconds of reinsertion. It also
  copes with sleep: after a wake, it rebuilds its connections.
- It keeps recording the [limit log](07-limit-log.md) even while the pad is
  unplugged.

**Only one program may drive the pad at a time.** The macropad has a single
data connection, and two programs writing to it make the display jump between
their states. The service is that one program. Before running a second copy,
such as the [live view](#live-view), stop the service first.

## Start menu

The installer adds a **FW16 Macropad Sysmon** folder to the Start menu. Each
item that runs the program asks for administrator rights (a UAC prompt),
since the program needs them.

| Item | What it does |
|------|--------------|
| **Self-test (what works on this machine)** | Checks every sensor and dependency, says what works, and tells you how to fix what doesn't. See [Self-test](#self-test). |
| **Live view (console)** | Runs the program in a console window and prints every update. Stop the service first (see [Live view](#live-view)). |
| **Edit settings (sysmon.ini)** | Opens the settings file in Notepad. See [Settings](06-settings.md). |
| **View log** | Opens the service log in Notepad. See [The log](#the-log). |
| **Limit report** | Prints the report of which limits held the CPU and dGPU back, for how long, and under what conditions. See [The limit log](07-limit-log.md). |
| **Flash firmware onto the macropad** | Puts the firmware that came with this version onto the pad. See [Flashing](09-install-update-remove.md#flashing-the-firmware). |
| **Restart the service** | Stops and starts the service, which applies changes to `sysmon.ini`. |
| **Third-party notices** | Licences of the bundled components |
| **Uninstall FW16 Macropad Sysmon** | Removes the program. See [Uninstalling](09-install-update-remove.md#uninstalling). |

The self-test, limit report, flash and restart windows wait for **Enter** at
the end, so you can read the result.

## Files

| Where | What |
|-------|------|
| `C:\Program Files\FW16 Macropad Sysmon\` | The program (`fw16-sysmon.exe`), its bundled libraries, the firmware image `framework_macropad_sysmon.uf2`, and the technical `readme.md` |
| `C:\ProgramData\FW16Sysmon\sysmon.ini` | [Settings](06-settings.md). Only administrators can change it. |
| `C:\ProgramData\FW16Sysmon\sysmon.log` | The service log, rotated at 1 MB, keeping `sysmon.log.1` to `.3` |
| `C:\ProgramData\FW16Sysmon\limitlog.sqlite` | The [limit log](07-limit-log.md). Anyone can read it, only the service writes it. |
| `C:\ProgramData\FW16Sysmon\service-state.json` | What the service itself could see at its last start, used by the self-test |

`C:\ProgramData` is hidden by default. Type the path into Explorer's address
bar.

## Controlling the service

From the Start menu: **Restart the service**. From an **elevated** terminal
(right-click Terminal → Run as administrator):

```powershell
sc.exe query FW16Sysmon      # is it running?
sc.exe stop FW16Sysmon       # stop: the pad switches to its regular effect
sc.exe start FW16Sysmon      # start: the bars come back
& "C:\Program Files\FW16 Macropad Sysmon\fw16-sysmon.exe" --restart-service
```

Type `sc.exe`, not just `sc`. In Windows PowerShell, `sc` is short for
`Set-Content`, so `sc stop FW16Sysmon` writes a file named `stop` instead of
stopping the service. (In Command Prompt, plain `sc` works.)

The Windows **Services** console (`services.msc`) lists it as `FW16Sysmon`,
set to start automatically. To stop the pad being a monitor for a while
without uninstalling, stop the service. To keep it off across restarts, set
its startup type to *Manual* in the Services console.

## Self-test

**Start menu → Self-test**, or `fw16-sysmon.exe --selftest`. It probes
everything the program uses and prints one line per item:

```text
[ ok ]  works
[ -- ]  not applicable on this machine (for example no NVIDIA card): nothing to fix
[warn]  works, but not as well as it could, or needs a look
[FAIL]  should work here and doesn't; a remedy is printed below it
```

What it checks, in order:

| Line | What it checks |
|------|----------------|
| Elevated | Whether the test runs with administrator rights. The service always has them. |
| Macropad attached | Whether the RGB Macropad is present and offers its data connection. It also catches the numpad module fitted instead. |
| sysmon firmware | Whether the pad runs the sysmon firmware, whether that firmware is as new as the host, and whether its lights are switched on |
| AMD APU power limits | Whether ryzenadj can read the processor's power table. Lists the limits with their current values. |
| Ring0 driver | Whether Windows allows the sensor driver to load, or Memory Integrity blocks it |
| dGPU power state | Whether the dGPU is on, in light sleep, or powered off |
| NVIDIA dGPU | Whether the NVIDIA driver answers |
| LibreHardwareMonitor lib | The secondary processor sensor source |
| framework_tool (EC) | Whether `framework_tool` is installed and can reach the embedded controller (charge limit, temperature) |
| Temperature source | What **every** temperature source reads, and which one wins |
| Screen brightness | Whether the screen brightness can be read |
| Battery state | Whether charge and discharge rates can be read |
| GPU counters (PDH) | Windows' GPU usage counters, the GPUs found, and the NPU |
| Settings file | Whether `sysmon.ini` parses, and how many of its settings were applied or ignored |
| Service's own view | What the **service** found at its last start. It can differ from what you see, because the service runs as a different user. |
| Limit log | Whether the service is writing the limit log, and how much it holds |
| Service | Whether the service is installed and running |

It ends with a summary of anything you need to act on. Note that the
self-test reports what the machine *can* do. It ignores switches in
`sysmon.ini` that turn a source off. Line-by-line help is in
[Troubleshooting](08-troubleshooting.md#the-self-test-line-by-line).

## Live view

The live view runs the program in a console and prints every update, which
is the quickest way to see what the host sees. **Stop the service first.**
Otherwise both write to the pad, the display jumps, and the console warns
you:

```text
WARNING: the FW16Sysmon service is also running and writing to the macropad.
```

1. Stop the service: `sc.exe stop FW16Sysmon` in an elevated terminal.
2. Start **Live view (console)** from the Start menu.
3. When done, press **Ctrl+C**. The pad switches to its regular effect.
4. Start the service again: **Restart the service**, or `sc.exe start FW16Sysmon`.

At the start it lists the graphics adapters it found and how it classified
them:

```text
adapter luid 0x2A4C1: AMD Radeon(TM) 890M Graphics [iGPU]
adapter luid 0x2A7F3: NVIDIA GeForce RTX 5070 Laptop GPU [dGPU]
adapter luid 0x2A5D0: compute-only [NPU -> iGPU row ghost]
Limit log: C:\ProgramData\FW16Sysmon\limitlog.sqlite
Connected to macropad
```

Then, every update, a block like this (an invented example):

```text
bright=96
  batt  lvl=255 val=80 hue=85
  cpu   lvl=97 val=255 hue=0 env=breathe@48 floor=110
  mem   lvl=140 val=255 hue=36
  igpu  lvl=12 val=255 hue=0 ghost=40@hue128/val96
  dgpu  lvl=0 val=255 hue=85 ghost=63@hue140/val80 right
  temp  lvl=115 val=255 hue=85 grad->0
  limits ac=True weak=False cpu ryzenadj[stapm=34.9/35.0W* slow=29.1/42.0W fast=36.0/63.0W ...] clock[3.41GHz busy=92%] -> stapm(1.00 35.0W) dgpu[nvml] - -> None power=D3cold for 412s
```

| Part | Meaning |
|------|---------|
| `bright=` | The pad brightness sent (0-255), from the screen brightness |
| `lvl=` | The bar's length, 0 (empty) to 255 (full) |
| `val=` | The row's base brightness (80 = the dim idle battery or sleep marker) |
| `hue=` | Its colour, a QMK hue (0 red, 16 orange, 36 gold, 85 green, 128 cyan, 140 teal) |
| `env=breathe@48` / `env=blink@32` | The row breathes or blinks, with its period in 32 ms units (48 = about 1.5 s) |
| `ovl=risex2up` / `ovl=fallx1down` | Battery pulse: charging (`rise`) or draining (`fall`), how many keys animate, and in which direction |
| `ghost=` | The second bar behind the main one: the NPU on the iGPU row, the sleep marker (`right`) on the dGPU row |
| `limits ac= weak=` | On AC? Is the charger too weak (battery helping)? |
| `cpu ryzenadj[...]` | Every processor limit as *value/limit*. A `*` marks one binding right now. `heuristic(...)` appears instead when ryzenadj isn't available. |
| `clock[...]` | The busy cores' fastest clock, and the busiest core's load. `HELD` means held at base clock. |
| `-> stapm(1.00 35.0W)` | The limit the row breathes for: its name, how close it is (1.00 = at the limit), and its value. `None` means no limit. |
| `dgpu[nvml]` | Where the dGPU readings come from (`nvml` = the NVIDIA driver, `pdh` = Windows counters) |
| `power=D3cold for 412s` | The dGPU's power state (`D0` on, `D3hot` light sleep, `D3cold` off), and how long it has been in that sleep state without a break |

A line `Message to the pad: CPU STAPM 35W` appears when a limit is named.

## The log

The service writes `C:\ProgramData\FW16Sysmon\sysmon.log` (**View log** in
the Start menu). Each line starts with a date and time. It's quiet in normal
operation. It records starts and stops, what the host found, and anything
that went wrong:

| Line | Meaning |
|------|---------|
| `Loaded config from C:\ProgramData\FW16Sysmon\sysmon.ini` | The settings were read |
| `Config ... [section] NAME: ...; keeping default ...` | A setting had a bad value and was ignored |
| `Config ...: unknown setting 'NAME' ignored` | A misspelt or outdated setting name |
| `framework_tool: C:\...\framework_tool.exe` | `framework_tool` was found |
| `framework_tool not found (EC temperature and battery charge cap ...` | It wasn't, so the battery bar is scaled to 100 % and one temperature source is missing |
| `Limit log: C:\ProgramData\FW16Sysmon\limitlog.sqlite` | The limit log is being written |
| `Connected to macropad` | The pad was found, at start, after reinsertion, or after a wake |
| `Device error (...); reconnecting...` | The pad went away (unplugged, sleep, a flash). The service looks for it again. |
| `Message to the pad: CPU STAPM 28W` | A limit was named on the pad |
| `The macropad's RGB lighting is switched off ...` / `... is back on` | Someone switched the pad's lights off or on |
| `NPU found: ...` | The NPU was found and feeds the ghost bar on the iGPU row |
| `dGPU power state from PnP: ...` | The dGPU whose power state drives the sleep marker |
| `ryzenadj Tctl is stuck at ... C ...` | The processor's temperature reading stopped moving, so the host stopped trusting it |
| `CPU clock counters unavailable (will retry): ...` | Windows' clock counters weren't ready yet, which is common right after boot |
| `Limit log disabled: ...` | The limit log couldn't be opened. The pad works regardless. |

A Python traceback in the log means the program hit an unexpected error.
Restart the service. If the error repeats, the traceback is what to report.

## Command line

Everything in the Start menu, and a little more, is available from an
elevated terminal. The program is
`"C:\Program Files\FW16 Macropad Sysmon\fw16-sysmon.exe"`.

| Command | Does |
|---------|------|
| `fw16-sysmon.exe --selftest` | The [self-test](#self-test). Add `--json` for machine-readable output. |
| `fw16-sysmon.exe --verbose` | The [live view](#live-view) (stop the service first) |
| `fw16-sysmon.exe --limit-report` | The [limit report](07-limit-log.md#the-report). Options: `--report-days N` (only the last N days), `--export-csv DIR` (also write every table as CSV), `--limitlog-db PATH` (read a copy of the log). |
| `fw16-sysmon.exe --flash` | Flash the bundled firmware onto the pad. `--flash path\to\file.uf2` flashes a different image. |
| `fw16-sysmon.exe --bootloader` | Only restart the pad into its bootloader (an `RPI-RP2` drive appears) |
| `fw16-sysmon.exe --start-service` / `--stop-service` / `--restart-service` | Control the service |
| `fw16-sysmon.exe --install-service` / `--uninstall-service` | Register or remove the service (the installer does this) |
| `fw16-sysmon.exe --write-default-config` | Write a fresh, fully commented `sysmon.ini`, if none exists |
| `fw16-sysmon.exe --config PATH` | Use a different settings file |
| `fw16-sysmon.exe --version` | Print the version |
| `fw16-sysmon.exe --help` | List every option |

The tuning options (`--max-led`, `--interval` and so on) also exist on the
command line. For the service, set them in the
[`[cli]` section of `sysmon.ini`](06-settings.md#command-line-defaults-cli)
instead.

---

Previous: [4. Keys and lighting controls](04-keys-and-lighting.md) · Next: [6. Settings](06-settings.md)
