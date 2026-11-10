# 4. Keys and lighting controls

[Manual](README.md) › Keys and lighting controls

The sysmon firmware keeps the macropad's **default Framework keymap**. It's a
numeric keypad with a second layer for the lighting controls. The layer
follows **NumLock**: NumLock on gives the numbers, NumLock off gives the
lighting controls and navigation keys. NumLock is shared with the main
keyboard, so pressing NumLock on either one switches the pad's layer.

> If you remapped keys with VIA, your remapping is stored on the pad and the
> tables below may not match it. The [replay key](#keypad-5-the-replay-key)
> is recognised by what the key *sends* (keypad 5), not by its position, so
> it follows your remapping.

## NumLock on: the keypad

| | 1 | 2 | 3 | 4 |
|---|---|---|---|---|
| **Row 1** | Esc¹ | Calculator | = | Backspace |
| **Row 2** | Num Lock | / | * | − |
| **Row 3** | 7 | 8 | 9 | + |
| **Row 4** | 4 | 5 | 6 | + |
| **Row 5** | 1 | 2 | 3 | Enter |
| **Row 6** | 0 | 0 | . | Enter |

¹ With recent firmware, acts when released, and holding it for a second saves
a diagnostic trace instead. See
[The top-left key](#the-top-left-key-tap-or-hold-to-save-a-trace).

## NumLock off: lighting and navigation

| | 1 | 2 | 3 | 4 |
|---|---|---|---|---|
| **Row 1** | **Lights on/off**¹ | Effect speed + | Hue + | Saturation + |
| **Row 2** | Num Lock | Effect speed − | Hue − | Saturation − |
| **Row 3** | Home | ↑ | Page Up | **Next effect** |
| **Row 4** | ← | **Sysmon: what's limiting?** | → | **Previous effect** |
| **Row 5** | End | ↓ | Page Down | Effect brightness step |
| **Row 6** | Insert | Insert | Delete | Effect brightness step |

The navigation keys (Home, arrows, Page Up/Down, End, Insert, Delete) are the
keypad keys as Windows reads them with NumLock off. The rest are the pad's own
functions. The ¹ on the top-left key means the same as in the table above.

## What each lighting key does to the sysmon display

The bars are drawn by the host and ignore the regular effect's settings. Most
lighting keys therefore change only the **regular RGB effect**, which you see
when the host isn't running. One key affects everything:

| Key (NumLock off) | Effect on the bars | Effect on the regular RGB effect |
|-------------------|--------------------|----------------------------------|
| **Lights on/off** (top-left) | **Switches everything off and on**: bars, messages, warning, rainbow | Same |
| Next / previous effect | None, except that it switches the lights back on if they were off | Changes the effect |
| Hue +/−, saturation +/− | None | Changes the colour |
| Effect speed +/− | None | Changes the speed |
| Effect brightness step | None | Steps the brightness: off → ⅓ → ⅔ → full → off |

The pad remembers all of these across restarts.

Two things are easy to mix up:

- **Lights off** (the top-left key, or VIA's All Off) makes the pad completely
  dark, bars included. See [Lights off](03-what-the-pad-shows-when.md#lights-off).
- **Effect brightness at "off"** (the brightness step key) makes only the
  regular effect dark. The bars still show while the host runs. When the host
  stops, the pad looks off, but it isn't. Press the brightness step key to
  bring the effect back.

The pad's brightness while it shows the bars follows your screen. To change
it, see [Brightness follows your screen](01-reading-the-pad.md#brightness-follows-your-screen).

## Keypad 5: the replay key

With NumLock off, **keypad 5** (row 4, key 2) asks the pad to say what holds
the CPU and dGPU back right now, or `NO LIMIT`. See
[Asking: the replay key](02-limits-and-messages.md#asking-the-replay-key).

This only happens while the host is running and the pad is lit. Then the key
press doesn't reach Windows. At other times, and always with NumLock on, the
key is an ordinary keypad 5.

## The top-left key: tap, or hold to save a trace

The top-left key (Esc with NumLock on, lights on/off with NumLock off) has a
second job. **Holding it for one second** saves a diagnostic recording to the
pad's own memory, and **the whole pad flashes white** for a moment when the
save is done. The recording is the last ~13 seconds of what the pad's LEDs
were sent.

It's for reporting a display glitch seen at a time when no tool on the laptop
can run, such as while Windows boots. The pad keeps the last four saves,
through power-off and reflashing, and a developer tool on the laptop reads
them out later (`bootanim_preview.py trace saved`, see the technical
[readme](https://github.com/Msprg/framework_qmk_firmware/blob/macropad-sysmon/keyboards/framework/macropad/keymaps/sysmon/readme.md)). It works in both layers, whatever the keymap. The
pad records only while its lights are on, so a save made with the lights off
holds nothing from the dark stretch.

> **Needs recent firmware**, from 10 November 2026 on. With older sysmon
> firmware, the top-left key acts on press as usual, Esc auto-repeats, and
> holding it does nothing more. If the pad never flashes white, flash the
> firmware that came with your host.

Because of this, the key acts **when you release it**:

- A tap (shorter than a second) sends Esc, or toggles the lights, on release.
- A hold of a second or more saves a trace and sends nothing. Holding Esc
  doesn't auto-repeat.
- A hold doesn't dismiss the boot rainbow. A tap does.

If the pad flashed white and you didn't expect it, you held this key a bit
long. Nothing changed apart from the saved recording.

## Any key dismisses the boot rainbow

During the boot rainbow (from power-up until the host connects), the first
key press of any kind ends it. The pad switches to the regular effect, and
the key still does its normal job. The one exception is a
[long hold of the top-left key](#the-top-left-key-tap-or-hold-to-save-a-trace).

## VIA

The pad still works with [VIA](https://usevia.app), the web tool for
remapping keys and choosing lighting effects. The sysmon data
travels on a private channel that VIA doesn't use. Lighting changes made in
VIA apply to the regular effect, just like the keys above, and VIA's All Off
is the same as the lights-off key.

The macropad has one data connection for VIA and the host. VIA and the
running service normally coexist. If VIA behaves oddly while the service is
running, stop the service while you use VIA (Start menu, or
`sc.exe stop FW16Sysmon`), then start it again.

## Entering the bootloader by hand

Firmware is normally flashed from the Start menu (see
[Flashing](09-install-update-remove.md#flashing-the-firmware)). To put the pad
into its bootloader manually, **hold the top-left and bottom-left keys while
inserting the module**. A drive named `RPI-RP2` appears, and you can copy a
`.uf2` firmware file onto it.

---

Previous: [3. What the pad shows, and when](03-what-the-pad-shows-when.md) · Next: [5. The host program](05-the-host-program.md)
