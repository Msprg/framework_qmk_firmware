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
  3. Wait for the RP2040's RPI-RP2 mass-storage volume. Matched by its
     INFO_UF2.TXT marker rather than only by volume label, because the label can
     be absent while the drive is still settling.
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


def _drive_letters():
    import ctypes
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase)
            if mask & (1 << i)]


def find_rp2_drive():
    """Return the mount point of an attached RP2040 bootloader, or None."""
    import ctypes
    for root in _drive_letters():
        try:
            if os.path.isfile(os.path.join(root, RPI_MARKER)):
                return root
        except OSError:
            continue
        # Fall back to the volume label for a drive that is mounted but whose
        # directory listing is not readable yet.
        try:
            name = ctypes.create_unicode_buffer(261)
            if ctypes.windll.kernel32.GetVolumeInformationW(
                    ctypes.c_wchar_p(root), name, 261,
                    None, None, None, None, 0):
                if name.value.upper() == RPI_LABEL:
                    return root
        except Exception:
            continue
    return None


def _wait_for_rp2(timeout=BOOTLOADER_WAIT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        drive = find_rp2_drive()
        if drive:
            time.sleep(0.5)   # let the volume settle before writing to it
            return drive
        time.sleep(0.25)
    return None


def _macropad_present():
    try:
        import hid
        return bool(hid.enumerate(0x32AC, 0x0013))
    except Exception:
        return False


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
        was_running = service.is_running()
        if was_running:
            print(f"Stopping {service.SERVICE_NAME} while flashing...")
            service.stop(quiet=True)
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
    drive = find_rp2_drive()
    if drive:
        print(f"Macropad is already in its bootloader ({drive})")
    else:
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
        drive = _wait_for_rp2()
        if drive is None:
            print(f"The {RPI_LABEL} drive never appeared.", file=sys.stderr)
            _manual_instructions()
            return 1
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
    print("Waiting for the macropad to restart...")
    if _wait_for_macropad():
        print("Macropad re-enumerated; firmware flashed.")
        return 0

    if copy_error is not None:
        print(f"Copy failed: {copy_error}", file=sys.stderr)
    else:
        print("The macropad did not re-enumerate within "
              f"{REENUMERATE_WAIT_S}s.", file=sys.stderr)
    print("If the RPI-RP2 drive is still present, the image was not accepted; "
          "try again or copy the .uf2 onto the drive by hand.", file=sys.stderr)
    return 1
