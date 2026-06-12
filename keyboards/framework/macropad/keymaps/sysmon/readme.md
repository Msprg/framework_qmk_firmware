# sysmon — system monitor lighting for the Framework 16 RGB Macropad

Turns the macropad's six key rows into live bar graphs, driven by a small
host-side Python script over the VIA raw HID endpoint (VIA keeps working
normally — the stats ride on a private custom-value channel, `0x42`):

| Row | Indicator | Colors |
|-----|-----------|--------|
| 1 (top, small keys) | Battery | charging: level bar (red→green by level) with a pulse on the filling segment; discharging: red bar with a draining pulse, blinks below 15%; idle at the charge cap: whole row steady dim green |
| 2 | CPU usage | red bar |
| 3 | Memory usage | gold bar |
| 4 | iGPU usage | red bar |
| 5 | dGPU usage | green bar (dark if no dGPU) |
| 6 | System temperature | fixed green→deep-red gradient across the row; bar length scales 30–85 °C |

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
pip install hidapi psutil wmi
python host\fw16_sysmon.py --verbose
```

Useful flags:

- `--max-led 150` — LED brightness at 100% screen brightness (0–255)
- `--min-led 8` — LED brightness at 0% screen brightness
- `--interval 1.0` — update period in seconds
- `--igpu-match / --dgpu-match` — regexes that classify GPU adapters by
  name (run with `--verbose` to see the detected adapters; defaults handle
  AMD iGPU + NVIDIA or Radeon RX dGPU)

Temperature needs one of (tried in this order):

- [framework_tool](https://github.com/FrameworkComputer/framework-system)
  on PATH (`winget install FrameworkComputer.framework_tool`) — reads the
  APU die temperature straight from the EC; needs the script to run
  elevated (best source),
- [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
  running with *Options → Remote Web Server / WMI provider* enabled, or
- a working ACPI thermal zone — beware: the FW16 exposes zones that
  *look* valid but report frozen values (a constant ~63 °C), and reading
  them needs admin rights anyway. The row stays dark if no source is
  available.

### Start at login (no service required)

Task Scheduler → Create Task:

- Trigger: *At log on*
- Action: `pythonw.exe C:\path\to\fw16_sysmon.py`
- Check *Run only when user is logged on* (WMI brightness/GPU counters are
  per-session)

## Protocol

One 32-byte VIA packet per interval:

```
[0]  0x07  id_custom_set_value
[1]  0x42  sysmon channel
[2]  0x01  stats value id
[3]  flags: bit0 AC connected, bit1 battery present, bit2 dGPU present,
     bit3 temp valid, bit4 charging, bit5 discharging
     (bit4+bit5 both clear while on AC = battery idle / held at a cap)
[4]  battery %   [5] cpu %   [6] mem %   [7] igpu %   [8] dgpu %
[9]  temperature °C
[10] LED brightness 0-255
```

The firmware ACKs by VIA's normal echo of the buffer.
