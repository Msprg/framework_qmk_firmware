#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Flash firmware onto the macropad, end to end.

`--bootloader` already sent the VIA jump packet; everything after it -- waiting
for the drive, copying the image, checking it came back -- was manual. The
installer needs the whole thing to be one unattended step, so it lives here.

The sequence, and why each part is the way it is:

  1. Stop the service if it is running. The macropad has a single raw-HID
     endpoint; a service writing frames at 2.5 Hz while the flasher tries to
     talk to the same handle is a race with no upside.
  2. Send VIA's id_bootloader_jump (0x0B) padded with 0xFE. The padding doubles
     as Framework's factory f_bootloader command, which is the same trick their
     official updater uses, so this works on stock firmware too -- someone
     installing for the first time has not flashed sysmon yet.
  3. Wait for the macropad to drop off USB and the RP2040's RPI-RP2
     mass-storage volume to appear. Matched by its INFO_UF2.TXT marker rather
     than only by volume label, because the label can be absent while the
     drive is still settling. Every RP2040 shows the same drive (a Raspberry
     Pi Pico in BOOTSEL too), so only a drive that was not there before the
     jump counts, and only once the macropad has actually left: anything else
     could be another board, and writing the macropad's image onto it would
     then read as success. (The one exception, the manual route: with the
     macropad not running and a single drive attached, that drive is used if
     the person at the console says it is the macropad.)
  4. Copy the .uf2. The bootloader reboots itself the moment the transfer
     completes, so the copy "failing" at the very end is normal and expected --
     which is why a write error is only believed if the device does not come
     back.
  5. Wait for the macropad to re-enumerate, then restart the service.

If the module is not attached at all, the manual route (the key combo) is
printed instead of hanging: the macropad is hot-swappable, so "not plugged in
right now" is an ordinary state, not an error.
"""

import os
import shutil
import string
import sys
import time

import paths

RPI_MARKER = "INFO_UF2.TXT"
RPI_LABEL = "RPI-RP2"

BOOTLOADER_WAIT_S = 30
REENUMERATE_WAIT_S = 30


DRIVE_REMOVABLE = 2  # GetDriveTypeW: what an RPI-RP2 mass-storage drive is


def _drive_letters():
    """Removable drives only: a bootloader drive is one, and probing the rest
    (an offline network share, an empty optical drive) can take seconds each."""
    import ctypes
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    return [root for root in (f"{letter}:\\" for i, letter
                              in enumerate(string.ascii_uppercase) if mask & (1 << i))
            if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) == DRIVE_REMOVABLE]


def _is_rp2(root):
    """Is the drive at root an RP2040 bootloader (any RP2040's)?"""
    import ctypes
    try:
        if os.path.isfile(os.path.join(root, RPI_MARKER)):
            return True
    except OSError:
        return False
    # Fall back to the volume label for a drive that is mounted but whose
    # directory listing is not readable yet.
    try:
        name = ctypes.create_unicode_buffer(261)
        if ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(root), name, 261,
                None, None, None, None, 0):
            return name.value.upper() == RPI_LABEL
    except Exception:
        pass
    return False


def find_rp2_drives():
    """Mount points of every attached RP2040 bootloader drive."""
    return [root for root in _drive_letters() if _is_rp2(root)]


def _wait_for_rp2(before, timeout=BOOTLOADER_WAIT_S):
    """The drives that appeared after the jump: those not in `before`, once
    the macropad itself is no longer seen running (`[]` if that doesn't
    happen in time), and whether HID enumeration was failing at the last
    look. Exactly one is the macropad's; the caller refuses to guess among
    more.

    A letter in `before` never counts, even after its drive goes away: a
    board that resets or is replugged comes back at its letter, and taking it
    then would write the macropad's image onto it. The price is the rare case
    of the macropad's drive mounting at a letter a vanished board held: that
    reads as 'never appeared', and a re-run, with the macropad now waiting in
    its bootloader, asks before using the drive. Likewise the pad must be seen
    gone: while HID enumeration fails it is not known to have left, so a new
    drive could be another board's -- keep waiting (a timeout is the safe
    failure)."""
    def new_drives():
        return [d for d in find_rp2_drives() if d not in before]

    hid_failed = False  # as of the latest look (also the answer if none comes)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        present = _macropad_present() if new_drives() else True
        hid_failed = present is None
        if present is False:
            time.sleep(0.5)   # let the volume settle before writing to it
            settled = new_drives()
            if settled:
                return settled, hid_failed
        time.sleep(0.25)
    return [], hid_failed


def _macropad_present():
    """True / False, or None when USB HID can't be enumerated at all (which
    must not read as 'the macropad is gone')."""
    try:
        import hid
        return bool(hid.enumerate(0x32AC, 0x0013))
    except Exception:
        return None


def _confirm_drive(drive):
    """Ask whether a bootloader drive found already there is the macropad.

    Every RP2040 shows the same drive, so with the macropad not running there
    is no telling it from, say, a Raspberry Pi Pico in BOOTSEL. Only a person
    can: without one to ask (no console they can see -- a silent Setup runs
    this hidden), or with nobody answering within a minute, the answer is no.
    """
    from applog import ask_yes, can_prompt
    print(f"An {RPI_LABEL} bootloader drive is attached at {drive} and the "
          "macropad is not running.")
    if not can_prompt():
        print(f"Not writing to {drive} without confirmation: every RP2040 board "
              "shows this same drive. Run --flash from a console to confirm, or "
              "copy the .uf2 onto the macropad's drive by hand.",
              file=sys.stderr)
        return False
    print("Every RP2040 board (a Raspberry Pi Pico in BOOTSEL, say) shows this "
          "same drive.")
    return ask_yes(f"Is {drive} the macropad, put into its bootloader by hand?")


def _wait_for_macropad(timeout=REENUMERATE_WAIT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _macropad_present():
            return True
        time.sleep(0.5)
    return False


def _manual_instructions():
    print()
    print("To enter the bootloader by hand:")
    print("  unplug the macropad module, then hold the top-left and")
    print("  bottom-left keys while inserting it. An RPI-RP2 drive appears.")
    print("Then re-run with --flash, or copy the .uf2 onto that drive.")


def flash(uf2=None, restart_service=True):
    """Flash `uf2` (default: the bundled image). Returns a process exit code."""
    if uf2 is None:
        uf2 = paths.bundled_uf2()
        if uf2 is None:
            print("No firmware image bundled and none given.", file=sys.stderr)
            print("Pass one: --flash path\\to\\framework_macropad_sysmon.uf2",
                  file=sys.stderr)
            return 1
    if not os.path.isfile(uf2):
        print(f"Firmware image not found: {uf2}", file=sys.stderr)
        return 1

    size = os.path.getsize(uf2)
    print(f"Firmware: {uf2} ({size // 1024} KiB)")

    # --- 1. get the service out of the way --------------------------------
    was_running = False
    try:
        import service
        # Stop whatever may hold the endpoint ("unknown": the SCM couldn't be
        # asked, so it may well be running), but start again afterwards only
        # what was running -- not one the user, or Setup, was stopping.
        state = service.query_state()
        was_running = state in ("running", "starting")
        if state in ("running", "starting", "stopping", "paused", "unknown"):
            print(f"Stopping {service.SERVICE_NAME} while flashing...")
            if service.stop(quiet=True) != 0:
                # hidapi handles are shared, so the jump still gets through
                # (the service reconnects once the pad is back): go ahead
                print(f"(could not stop {service.SERVICE_NAME}; flashing "
                      "anyway)", file=sys.stderr)
                was_running = False  # nothing to restart
    except Exception as err:
        print(f"(could not check the service: {err})", file=sys.stderr)

    try:
        return _flash_inner(uf2)
    finally:
        if was_running and restart_service:
            try:
                import service
                print(f"Restarting {service.SERVICE_NAME}...")
                service.start()
            except Exception as err:
                print(f"Could not restart the service: {err}", file=sys.stderr)


def _flash_inner(uf2):
    # --- 2. already in the bootloader? ------------------------------------
    # Only when the macropad is not running: then a single bootloader drive
    # is taken to be it, put there by hand (the key combo). While it runs,
    # any drive already present belongs to some other RP2040 board.
    before = find_rp2_drives()
    running = _macropad_present()
    if running is None:
        print("Could not enumerate USB HID devices, so whether the macropad is "
              "running can't be told; not flashing.", file=sys.stderr)
        return 1
    assumed = not running and len(before) == 1
    if assumed:
        drive = before[0]
        if not _confirm_drive(drive):
            return 1
    elif not running and len(before) > 1:
        print(f"Several {RPI_LABEL} drives are attached "
              f"({', '.join(before)}) and the macropad is not running, so "
              "which one is the macropad can't be told. Detach the other "
              "boards, or start the macropad normally, and try again.",
              file=sys.stderr)
        return 1
    else:
        if before:
            print(f"(ignoring {', '.join(before)}: another board's "
                  f"{RPI_LABEL} drive; the macropad is running)")
        eng = paths.engine_module()
        dev = eng.open_device()
        if dev is None:
            if not _macropad_present():
                print("No Framework macropad found.", file=sys.stderr)
                try:
                    import hid
                    if hid.enumerate(0x32AC, 0x0018):
                        print("(a Framework numpad module is attached instead; "
                              "sysmon needs the RGB macropad)", file=sys.stderr)
                except Exception:
                    pass
            else:
                print("Macropad found but its raw-HID endpoint did not open.",
                      file=sys.stderr)
            _manual_instructions()
            return 1

        print("Sending bootloader jump...")
        try:
            eng.bootloader_jump(dev)
        except OSError as err:
            # The device often resets mid-write, which surfaces as a write
            # error even though the jump was accepted. Only the drive appearing
            # (or not) settles it.
            print(f"(write returned {err}; checking for the bootloader anyway)")
        finally:
            try:
                dev.close()
            except Exception:
                pass

        # --- 3. wait for the drive ----------------------------------------
        print(f"Waiting for the {RPI_LABEL} drive (up to {BOOTLOADER_WAIT_S}s)...")
        new, hid_failed = _wait_for_rp2(before)
        if not new and hid_failed:
            print(f"A new {RPI_LABEL} drive appeared, but USB HID enumeration "
                  "kept failing, so whether the macropad left for its "
                  "bootloader can't be told and the drive isn't trusted. "
                  "Once USB HID works again, run --flash again (it asks "
                  "before writing to a lone drive), or copy the .uf2 onto "
                  "the macropad's drive by hand.",
                  file=sys.stderr)
            return 1
        if len(new) > 1:
            # The macropad is in its bootloader now, not running, so a plain
            # re-run would find several drives and refuse too.
            print(f"More than one new {RPI_LABEL} drive appeared "
                  f"({', '.join(new)}), so which is the macropad's can't be "
                  "told. Detach the other RP2040 boards, then run --flash "
                  "again (it asks before writing to the drive that is left), "
                  "or copy the .uf2 onto the macropad's drive by hand.",
                  file=sys.stderr)
            return 1
        if not new:
            print(f"The macropad's {RPI_LABEL} drive never appeared.",
                  file=sys.stderr)
            _manual_instructions()
            return 1
        drive = new[0]
        print(f"Bootloader mounted at {drive}")

    # --- 4. copy ----------------------------------------------------------
    dest = os.path.join(drive, os.path.basename(uf2))
    print(f"Copying firmware to {dest}...")
    copy_error = None
    try:
        shutil.copyfile(uf2, dest)
    except OSError as err:
        # Expected: the RP2040 reboots the instant the last block lands, so the
        # handle can die before close() returns. Treated as fatal only if the
        # macropad does not come back below.
        copy_error = err

    # --- 5. wait for it to come back --------------------------------------
    # The macropad was off USB (in its bootloader) when the copy began, so
    # seeing it now means it restarted, not that it never left.
    print("Waiting for the macropad to restart...")
    if _wait_for_macropad():
        print("Macropad re-enumerated; firmware flashed.")
        if assumed:
            # A board flashed with this image enumerates as the macropad too,
            # so this can't tell -- only the confirmation did.
            print(f"(As confirmed, {drive} was the macropad's drive. Had it "
                  "been another RP2040 board's, that board now runs the "
                  "macropad image and is what re-enumerated.)")
        return 0

    if copy_error is not None:
        print(f"Copy failed: {copy_error}", file=sys.stderr)
    else:
        print("The macropad did not re-enumerate within "
              f"{REENUMERATE_WAIT_S}s.", file=sys.stderr)
    print("If the RPI-RP2 drive is still present, the image was not accepted; "
          "try again or copy the .uf2 onto the drive by hand.", file=sys.stderr)
    if assumed:
        print(f"If {drive} belonged to another RP2040 board rather than the "
              "macropad, the image went onto that board instead.",
              file=sys.stderr)
    return 1
