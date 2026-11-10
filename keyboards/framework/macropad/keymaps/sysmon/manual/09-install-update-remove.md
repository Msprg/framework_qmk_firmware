# 9. Installing, updating, flashing, removing

[Manual](README.md) › Installing, updating, flashing, removing

## What you need

- A **Framework Laptop 16** with the **RGB Macropad** input module. The
  numpad module (white backlight) isn't supported.
- **Windows 10 version 2004 or newer, or Windows 11**, 64-bit, with
  administrator rights.
- AMD Ryzen models get every feature. Intel models work too, without the
  AMD-specific power limit detection.
- Optional: **`framework_tool`** (`winget install FrameworkComputer.framework_tool`).
  It scales the battery bar to your charge limit, and gives a reliable
  temperature reading. The installer offers to install it.

## Getting the installer

The installer is `FW16-Sysmon-Setup-<version>.exe`. No release is published on
GitHub. It's built from this repository with one command,
`host\packaging\build.ps1`, which puts it in `host\dist\`. See
[Building the installer](../readme.md#building-the-installer) in the technical
readme. If someone built it for you, that file is all you need. It bundles
everything, so no Python or other downloads are required.

## Installing

1. **Run `FW16-Sysmon-Setup-<version>.exe`.** It isn't code-signed, so Windows
   SmartScreen says *"Windows protected your PC"*. Choose **More info → Run
   anyway**. Then confirm the administrator prompt.
2. **Licence.** Accept it.
3. **System check.** Before installing anything, Setup says what will work on
   this machine:
   - *RGB macropad module detected*, or a warning if it isn't attached or
     the numpad is fitted instead. Installing without the pad is fine: the
     service picks it up whenever it appears.
   - *AMD CPU* (power limit detection through the processor's power table), or
     *non-AMD* (a fallback is used).
   - *Memory Integrity*: if it's on, the processor sensors are blocked. See
     [Memory Integrity](10-faq-and-glossary.md#memory-integrity).
   - *framework_tool* found or not.
   - An old *"FW16 Macropad Sysmon" scheduled task*, if present, will be
     removed. It's from before the installer existed, and two copies would
     fight over the pad.
   - A previously installed version, which will be stopped and replaced.
4. **Choose what to do after installing:**
   - **Start the service now** (on by default). It also starts at every boot
     from then on.
   - **Flash the bundled firmware onto the macropad now** (off by default).
     Tick it on a first install, or when the update includes new firmware.
     See [Flashing](#flashing-the-firmware).
   - **Install framework_tool via winget.** Only offered when it's missing and
     winget is available.
5. **Install.** Setup copies the program, writes a fully commented
   `sysmon.ini` (unless one exists), registers the service, flashes if you
   asked, and starts the service.
6. **Finish.** Leave *Show what works on this machine (self-test)* ticked to
   see the [self-test](05-the-host-program.md#self-test) right away.

Within a few seconds of the service starting, the bars appear on the pad.

### What gets installed

| What | Where |
|------|-------|
| The program and its libraries | `C:\Program Files\FW16 Macropad Sysmon\` |
| Settings, log, limit log | `C:\ProgramData\FW16Sysmon\` |
| The `FW16Sysmon` service | LocalSystem, starts automatically at boot |
| Start menu folder | *FW16 Macropad Sysmon* (see [Start menu](05-the-host-program.md#start-menu)) |

### Silent install

Setup accepts the standard Inno Setup switches, for example
`/VERYSILENT /SUPPRESSMSGBOXES`. To pick the after-install steps, pass
`/TASKS="startservice,flashfirmware"`. Task names: `startservice`,
`flashfirmware`, `frameworktool`. A silent flash runs hidden and asks no
questions.

## Updating

Run the newer Setup over the installed one. It:

- stops the service, replaces the program, re-registers the service and
  starts it again;
- **keeps your `sysmon.ini`** as it is. New settings run at their defaults
  until you add them. See [After an update](06-settings.md#after-an-update).
- keeps the log and the limit log.

If the new version includes new firmware, tick **Flash the bundled firmware**,
or flash it afterwards from the Start menu. The self-test's *sysmon firmware*
line tells you when the pad's firmware is older than the host.

## Flashing the firmware

The installed program carries the matching firmware image. To put it on the
pad, use **Start menu → Flash firmware onto the macropad**, or run
`fw16-sysmon.exe --flash` in an elevated terminal. Then:

1. The service is paused, since the flasher needs the pad's data connection.
2. The pad restarts into its bootloader, and a drive named **`RPI-RP2`**
   appears.
3. The image is copied onto it.
4. The pad restarts with the new firmware, showing the boot rainbow.
5. The service is started again, and the bars return.

The window says each step and waits for Enter at the end. To flash a
different image, run `fw16-sysmon.exe --flash path\to\image.uf2`.

**By hand**, if the automatic route fails or the pad runs firmware that can't
take the command:

1. Remove the macropad module.
2. Hold the **top-left and bottom-left keys** while inserting it. An
   `RPI-RP2` drive appears.
3. Copy `framework_macropad_sysmon.uf2` (in
   `C:\Program Files\FW16 Macropad Sysmon\`) onto that drive. The pad
   restarts by itself when the copy finishes.

Flashing replaces only the firmware. The pad's stored settings (lighting
effect, lights on/off, VIA remaps) usually survive.

## Uninstalling

**Settings → Apps → FW16 Macropad Sysmon → Uninstall**, or the *Uninstall*
item in the Start menu. It stops and removes the service and the program.
At the end it asks:

> Remove your settings and log as well?

- **Yes** deletes `C:\ProgramData\FW16Sysmon\`: settings, log **and the limit
  log**.
- **No** keeps them, so a reinstall picks up your settings.

**The pad keeps the sysmon firmware.** Without the host it works as an
ordinary macropad: the boot rainbow at power-up until a key is pressed, then
the regular RGB effect. To return to Framework's stock firmware, flash
Framework's macropad firmware the same way, by the
[manual route](#flashing-the-firmware) or with Framework's own updater.

---

Previous: [8. Troubleshooting](08-troubleshooting.md) · Next: [10. FAQ, limitations and glossary](10-faq-and-glossary.md)
