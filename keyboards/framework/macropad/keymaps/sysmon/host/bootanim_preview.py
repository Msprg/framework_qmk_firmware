#!/usr/bin/env python3
"""Replay macropad boot animations and try out text (VIA channel 0x42, value_id 0x10).

  bootanim_preview.py              numbered demo of every variant
  bootanim_preview.py N            variant N, numbered
  bootanim_preview.py N-M [xR]     variants N..M, numbered, R rounds
  bootanim_preview.py boot         the next power-up pick, exactly as a boot plays it
  bootanim_preview.py stop         stop a running replay
  bootanim_preview.py status       boot counter, this power-up's pick, display mode
  bootanim_preview.py warn [N[-M]] preview disconnect-warning styles (all, N, or N..M),
                                   each named by amber dots; queues behind a replay
  bootanim_preview.py goodbye      send the host's clean-stop notice (0x04), as a
                                   stopping host would -- for testing the warning
  bootanim_preview.py reboot [N]   restart the macropad, like replugging it, and
                                   name the animation it picked; N times in a row,
                                   each after the previous animation has finished
  bootanim_preview.py text TEXT [TEXT ...] [options]
                                   show text; several texts play one after another
                                   (A/B testing). See `text --help`.

Letter case picks the letter form: uppercase W and M are tall (they reach into
the small top row), lowercase w and m are short. Every other letter has a
single form, so its case doesn't matter. Up to 23 characters: A-Z, 0-9,
space, - ! and .

Safe to run while the FW16Sysmon service is running: it only writes, and the
service just drains the extra VIA echo.
"""
import argparse
import sys
import time

import hid

VID, PID = 0x32AC, 0x0013
RAW_USAGE_PAGE, RAW_USAGE = 0xFF60, 0x61
MODES = {0: "rgb-effect", 1: "boot-rainbow", 2: "sysmon", 3: "warning"}
STYLES = {"flash": 0, "ticker": 1, "pop": 2}
COLORS = {"rainbow": 0, "letters": 1, "white": 2}
TEXT_MAX = 23
NAMES = ["Wipe", "Shine", "Spin", "Bloom", "Twinkle", "Rotate", "Twinkle, scatter",
         "Fire", "Converge", "Flicker", "Lock-in", "Fireworks", "Lightning", "Stack",
         "Spotlight", "Glitch", "FW16 pop", "FW16 scroll", "FRAMEWORK scroll",
         "FRAMEWORK flash", "FRAMEWORK scroll, letter colours",
         "FRAMEWORK 16 scroll, letter colours"]


def raw_path():
    return next((d["path"] for d in hid.enumerate(VID, PID)
                 if d["usage_page"] == RAW_USAGE_PAGE and d["usage"] == RAW_USAGE), None)


def open_dev():
    path = raw_path()
    if path is None:
        sys.exit("macropad not found")
    dev = hid.device()
    dev.open_path(path)
    return dev


def replay(dev, args):
    """Send a BOOT REPLAY command (args = payload bytes) and return its echo.

    The echo carries status; the service's handle gets its own copy, and its
    frame echoes show up on this handle too, so match on the header.
    """
    payload = bytes(args)[:29]
    dev.write(b"\x00" + bytes([0x07, 0x42, 0x10]) + payload + bytes(29 - len(payload)))
    for _ in range(20):
        rep = bytes(dev.read(32, 100))
        if rep[:3] == b"\x07\x42\x10":
            return rep
    return None


def describe(rep):
    if rep is None:
        return "(no echo; firmware without status support?)"
    ram = int.from_bytes(rep[11:15], "little")
    stored = int.from_bytes(rep[16:20], "little")
    return (f"counter ram={ram} eeprom={stored} power-up pick={rep[15] or 'none'} "
            f"display={MODES.get(rep[20], rep[20])}")


def wait_done(dev, timeout=70.0):
    """Block until the macropad's playback has finished."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rep = replay(dev, [0xFD])
        if rep is not None and not rep[21]:
            return
        time.sleep(0.2)


def wait_for(present, timeout):
    """Poll until the macropad's raw HID interface is (or isn't) there."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (raw_path() is not None) == present:
            return True
        time.sleep(0.05)
    return False


def reboot_main(dev, argv):
    """Restart the macropad N times; returns the handle to close."""
    times = int(argv[0]) if argv else 1
    for k in range(times):
        dev.write(b"\x00" + bytes([0x07, 0x42, 0x11]) + bytes(29))
        dev.close()
        if not wait_for(False, 3):
            sys.exit("the macropad didn't restart (firmware without REBOOT?)")
        if not wait_for(True, 15):
            sys.exit("the macropad didn't come back within 15 s")
        for _ in range(40):  # Windows can list it a moment before it opens
            try:
                dev = open_dev()
                break
            except OSError:
                time.sleep(0.05)
        else:
            sys.exit("the macropad came back but can't be opened")
        rep = None
        for _ in range(20):  # the pick is drawn on the first frame
            rep = replay(dev, [0xFD])
            if rep is not None and rep[15]:
                break
            time.sleep(0.1)
        pick = rep[15] if rep is not None else 0
        name = NAMES[pick - 1] if 0 < pick <= len(NAMES) else "?"
        print(f"restart {k + 1}/{times}: playing {pick} {name}")
        if k + 1 < times:
            wait_done(dev)
            time.sleep(1.0)  # a moment on the finished rainbow
    return dev


def text_main(dev, argv):
    ap = argparse.ArgumentParser(
        prog="bootanim_preview.py text",
        description="Show text on the macropad. Several texts play one after "
                    "another, each when the previous one has finished. Uppercase "
                    "W/M are the tall forms, lowercase w/m the short ones.")
    ap.add_argument("texts", nargs="+", metavar="TEXT")
    ap.add_argument("-s", "--style", choices=STYLES, default="flash",
                    help="flash: letters one at a time, like a sign (default); "
                         "ticker: scrolling; pop: letters pop up one at a time")
    ap.add_argument("-c", "--color", choices=COLORS, default="white",
                    help="rainbow: each key in its own rainbow color; letters: "
                         "each letter its own color; white (default)")
    ap.add_argument("-v", "--speed", type=int, default=0,
                    help="flash/pop: ms per letter (default 220 / 420); "
                         "ticker: columns per second (default 7)")
    ap.add_argument("-r", "--repeat", type=int, default=1,
                    help="play the whole list this many times")
    a = ap.parse_args(argv)
    for t in a.texts:
        if len(t) > TEXT_MAX:
            sys.exit(f"{t!r} is longer than {TEXT_MAX} characters")
    speed = max(0, min(a.speed, 0xFFFF))
    try:
        for _ in range(a.repeat):
            for t in a.texts:
                print(f"showing {t!r} ({a.style}, {a.color})")
                replay(dev, [0xFB, STYLES[a.style], COLORS[a.color],
                             speed & 0xFF, speed >> 8] + list(t.encode("ascii", "replace")) + [0])
                wait_done(dev)
    except KeyboardInterrupt:
        replay(dev, [0xFF])
        print("stopped")


def parse(argv):
    arg = argv[0] if argv else "0"
    if arg == "status":
        return [0xFD]
    if arg == "stop":
        return [0xFF]
    if arg == "boot":
        return [0xFE]
    if arg == "warn":
        rng = argv[1] if len(argv) > 1 else "0"
        lo, _, hi = rng.partition("-")
        return [0xFC, int(lo), int(hi or lo)]
    rounds = int(argv[1].lstrip("x")) if len(argv) > 1 else 1
    if "-" in arg:
        first, last = (int(p) for p in arg.split("-", 1))
        return [first, last, rounds]
    return [int(arg), 0, rounds]


def main():
    argv = sys.argv[1:]
    dev = open_dev()
    try:
        if argv[:1] == ["text"]:
            text_main(dev, argv[1:])
            return
        if argv[:1] == ["reboot"]:
            dev = reboot_main(dev, argv[1:])
            return
        if argv[:1] == ["goodbye"]:
            dev.write(b"\x00" + bytes([0x07, 0x42, 0x04]) + bytes(29))
            print("sent goodbye")
            return
        cmd = parse(argv)
        print(describe(replay(dev, cmd)))
        print(f"sent {cmd}")
    finally:
        dev.close()


if __name__ == "__main__":
    main()
