#!/usr/bin/env python3
"""Replay macropad boot animations (VIA channel 0x42, value_id 0x10).

  bootanim_preview.py              numbered demo of every variant
  bootanim_preview.py N            variant N, numbered
  bootanim_preview.py N-M [xR]     variants N..M, numbered, R rounds
  bootanim_preview.py boot         the next power-up pick, exactly as a boot plays it
  bootanim_preview.py stop         stop a running replay

Safe to run while the FW16Sysmon service is running: it only writes, and the
service just drains the extra VIA echo.
"""
import sys

import hid

VID, PID = 0x32AC, 0x0013
RAW_USAGE_PAGE, RAW_USAGE = 0xFF60, 0x61


def parse(argv):
    arg = argv[0] if argv else "0"
    rounds = int(argv[1].lstrip("x")) if len(argv) > 1 else 1
    if arg == "status":
        return 0xFD, 0, 0
    if arg == "stop":
        return 0xFF, 0, 0
    if arg == "boot":
        return 0xFE, 0, 0
    if "-" in arg:
        first, last = (int(p) for p in arg.split("-", 1))
        return first, last, rounds
    return int(arg), 0, rounds


def main():
    first, last, rounds = parse(sys.argv[1:])
    path = next((d["path"] for d in hid.enumerate(VID, PID)
                 if d["usage_page"] == RAW_USAGE_PAGE and d["usage"] == RAW_USAGE), None)
    if path is None:
        sys.exit("macropad not found")
    dev = hid.device()
    dev.open_path(path)
    try:
        dev.write(b"\x00" + bytes([0x07, 0x42, 0x10, first, last, rounds]) + bytes(26))
        # The echo carries status; the service's handle gets its own copy.
        for _ in range(20):
            rep = bytes(dev.read(32, 100))
            if rep[:3] == b"\x07\x42\x10":
                ram = int.from_bytes(rep[11:15], "little")
                stored = int.from_bytes(rep[16:20], "little")
                print(f"counter ram={ram} eeprom={stored} power-up pick={rep[15] or 'none'}")
                break
        else:
            print("(no echo; firmware without status support?)")
    finally:
        dev.close()
    print(f"sent first={first} last={last} rounds={rounds}")


if __name__ == "__main__":
    main()
