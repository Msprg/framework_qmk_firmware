#!/usr/bin/env python3
"""Replay macropad boot animations and try out text (VIA channel 0x42, value_id 0x10).

  bootanim_preview.py              demo of every variant, each named by dots first
  bootanim_preview.py N [xR]       variant N (R times), without the naming dots
  bootanim_preview.py N-M [xR]     variants N..M, each named by dots, R rounds
      --speed X                    (with any of the above, and with trace N) play
                                   the animations X times as fast: 0.25 = at a
                                   quarter of the speed, 2 = twice as fast
                                   (0.1 to 10)
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
  bootanim_preview.py trace [N [xR]]
                                   save what the LED chip was sent, frame by frame:
                                   the last ~13 s, or (with N) animation N (R
                                   times; the recording keeps the last ~13 s) and
                                   the fade into the bars after it. Writes a CSV,
                                   a summary and a timeline PNG to host/traces/
  bootanim_preview.py trace saved [N|last]
                                   list the traces saved in the macropad's flash
                                   (holding the top-left key for a second saves
                                   one), or write save N's files like trace does
  bootanim_preview.py trace save   save one from here, as the key does
  bootanim_preview.py text TEXT [TEXT ...] [options]
                                   show text; several texts play one after another
                                   (A/B testing). See `text --help`.
  bootanim_preview.py msg TEXT [TEXT ...] [options]
                                   scroll a limit message over the live bars, as
                                   the host does ("CPU STAPM 28W"); needs the
                                   service running. See `msg --help`.

Letter case picks the letter form: uppercase W and M are tall (they reach into
the small top row), lowercase w and m are short. Every other letter has a
single form, so its case doesn't matter. Up to 23 characters: A-Z, 0-9,
space, - ! and .

Safe to run while the FW16Sysmon service is running: it only writes, and the
service just drains the extra VIA echo.
"""
import argparse
import math
import os
import struct
import sys
import time
import zlib

import hid

VID, PID = 0x32AC, 0x0013
RAW_USAGE_PAGE, RAW_USAGE = 0xFF60, 0x61
MODES = {0: "rgb-effect", 1: "boot-rainbow", 2: "sysmon", 3: "warning",
         4: "nothing (lighting off, or suspended)"}
STYLES = {"flash": 0, "ticker": 1, "pop": 2}
COLORS = {"rainbow": 0, "letters": 1, "white": 2}
TEXT_MAX = 23
NAMES = ["Wipe", "Shine", "Spin", "Bloom", "Twinkle", "Rotate", "Twinkle, scatter",
         "Fire", "Converge", "Flicker", "Lock-in", "Fireworks", "Lightning", "Stack",
         "Spotlight", "Glitch", "FW16 pop", "FW16 scroll", "FRAMEWORK scroll",
         "FRAMEWORK flash", "FRAMEWORK scroll, letter colours",
         "FRAMEWORK16 scroll, letter colours"]


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


def wait_done(dev, timeout=70.0, byte=21):
    """Block until the macropad's playback (byte 22: message) has finished."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rep = replay(dev, [0xFD])
        if rep is not None and not rep[byte]:
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


# --- LED trace (TRACE, value_id 0x12) ---------------------------------------

TRACE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traces")
TRACE_FRAME = 76  # u16 ms, GCC, info, then 6 rows x 4 keys x RGB PWM
MODE_RGB = {0: (90, 90, 90), 1: (150, 70, 210), 2: (40, 170, 90), 3: (230, 190, 0)}


def trace_cmd(dev, sub, offset=0):
    """Send a TRACE command; return its echo (matched on header, sub, offset)."""
    payload = bytes([sub]) + offset.to_bytes(4, "little")
    dev.write(b"\x00" + bytes([0x07, 0x42, 0x12]) + payload + bytes(29 - len(payload)))
    for _ in range(50):
        rep = bytes(dev.read(32, 100))
        if rep[:4] == bytes([0x07, 0x42, 0x12, sub]) and (sub not in (2, 4) or rep[4:8] == payload[1:5]):
            return rep
    return None


def trace_read(dev, sub, start, total):
    """Read total bytes from start, 24 at a time (sub 2: the frozen ring, 4: the saved traces)."""
    data, off = bytearray(), start
    while off < start + total:
        rep = None
        for _ in range(3):
            rep = trace_cmd(dev, sub, off)
            if rep is not None:
                break
        if rep is None:
            sys.exit(f"trace read failed at byte {off}")
        data += rep[8:32]
        off += 24
    return bytes(data[:total])


def trace_download(dev):
    """Freeze the recorder and read out its frames, oldest first."""
    st = trace_cmd(dev, 1)
    if st is None:
        sys.exit("no TRACE answer (firmware without the LED trace?)")
    count, size = st[8] | st[9] << 8, st[10]
    if size != TRACE_FRAME:
        sys.exit(f"unexpected trace frame size {size}")
    started = time.monotonic()
    data = trace_read(dev, 2, 0, count * size)
    print(f"read {count} frames ({len(data)} bytes) in {time.monotonic() - started:.1f} s")
    return trace_frames(data, count, size)


def trace_frames(data, count, size=TRACE_FRAME):
    """Frames from their bytes, oldest first; t in ms from the first, unwrapped."""
    frames, t, last = [], 0, None
    for k in range(count):
        b = data[k * size:(k + 1) * size]
        raw_t = b[0] | b[1] << 8
        t += 0 if last is None else (raw_t - last) % 65536
        last = raw_t
        frames.append({"t": t, "gcc": b[2], "mode": b[3] & 3, "change": (b[3] >> 2) & 3,
                       "fade": bool(b[3] & 0x10), "px": bytes(b[4:76])})
    return frames


def png_write(path, w, h, rows):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


# A 3x5 pixel font for the PNG's labels: rows top to bottom, bit 2 = left.
FONT = {
    "0": (7, 5, 5, 5, 7), "1": (2, 6, 2, 2, 7), "2": (7, 1, 7, 4, 7), "3": (7, 1, 7, 1, 7),
    "4": (5, 5, 7, 1, 1), "5": (7, 4, 7, 1, 7), "6": (7, 4, 7, 5, 7), "7": (7, 1, 1, 1, 1),
    "8": (7, 5, 7, 5, 7), "9": (7, 5, 7, 1, 7), "A": (2, 5, 7, 5, 5), "B": (6, 5, 6, 5, 6),
    "C": (3, 4, 4, 4, 3), "D": (6, 5, 5, 5, 6), "E": (7, 4, 6, 4, 7), "G": (3, 4, 5, 5, 3),
    "M": (5, 7, 7, 5, 5), "P": (6, 5, 6, 4, 4), "S": (3, 4, 2, 1, 6), "T": (7, 2, 2, 2, 2),
    "U": (5, 5, 5, 5, 7), " ": (0, 0, 0, 0, 0),
}
ROW_NAMES = ["BAT", "CPU", "MEM", "GPU", "DGPU", "TEMP"]


def trace_png(frames, path):
    """Timeline, a column per frame, times in ms from the first frame. Top:
    display mode (fading frames pale; current changes marked, white rising,
    red falling) and the global current as a bar; then a band per key, pad
    rows top to bottom (named on the left), keys left to right, colored by
    light (PWM x current) relative to the trace's brightest, gamma 2.2; a
    ruler (ticks every 50 ms, tall and labeled every 250 ms, or every 500 ms
    or more where the labels would run together) at the bottom."""
    n = len(frames)
    fw = max(1, min(6, 1800 // max(n, 1)))
    left, sc = 38, 2  # label margin; font scale
    w = left + n * fw
    lmax = max(1, max(max(f["px"]) * f["gcc"] for f in frames))
    gmax = max(1, max(f["gcc"] for f in frames))
    bands = [("mode", 8), ("gap", 2), ("gcc", 16), ("gap", 4)]
    for r in range(6):
        for c in range(4):
            bands.append((("key", r, c), 7))
        bands.append(("gap", 4))
    bands += [("ruler", 7), ("labels", 5 * sc + 4)]
    h = sum(b[1] for b in bands)
    rows = [bytearray(w * 3) for _ in range(h)]

    def put(x, y, rgb):
        if 0 <= x < w and 0 <= y < h:
            rows[y][x * 3:x * 3 + 3] = bytes(rgb)

    def text(x, y, txt, rgb=(220, 220, 220)):
        for ch in txt:
            for gy, bits in enumerate(FONT.get(ch, FONT[" "])):
                for gx in range(3):
                    if bits >> (2 - gx) & 1:
                        for dy in range(sc):
                            for dx in range(sc):
                                put(x + gx * sc + dx, y + gy * sc + dy, rgb)
            x += 4 * sc

    t0 = frames[0]["t"]
    y, labels = 0, []
    for kind, bh in bands:
        if isinstance(kind, tuple) and kind[2] == 0:
            text(2, y + (4 * 7 + 3 - 5 * sc) // 2, ROW_NAMES[kind[1]])
        if kind == "gcc":
            text(2, y + 3, "GCC")
        for i, f in enumerate(frames):
            for dy in range(bh):
                if kind == "mode":
                    rgb = MODE_RGB[f["mode"]]
                    if f["fade"] and dy < 4:
                        rgb = tuple(min(255, v + 90) for v in rgb)
                    if f["change"] and dy >= 4:
                        rgb = (255, 255, 255) if f["change"] == 1 else (255, 40, 40)
                elif kind == "gcc":
                    rgb = (200, 200, 200) if bh - dy <= round(f["gcc"] / gmax * bh) else (25, 25, 25)
                elif kind in ("ruler", "labels"):
                    rgb = (0, 0, 0)
                elif kind == "gap":
                    rgb = (15, 15, 15)
                else:
                    k = (kind[1] * 4 + kind[2]) * 3
                    rgb = tuple(int(255 * ((f["px"][k + j] * f["gcc"]) / lmax) ** (1 / 2.2) + 0.5) for j in range(3))
                for dx in range(fw):
                    put(left + i * fw + dx, y + dy, rgb)
        if kind == "ruler":
            # labels every 250 ms, or less often where they would run together
            span = max(frames[-1]["t"] - t0, 1)
            wide = (len(str(span)) * 4 * sc + 8) * span / (n * fw)  # ms one label needs
            every = next((e for e in (250, 500, 1000, 2000, 5000) if e >= wide), 10000)
            mark = 0
            for i, f in enumerate(frames):
                while f["t"] - t0 >= mark:
                    tall = mark % every == 0
                    for dy in range(bh if tall else 3):
                        put(left + i * fw, y + dy, (255, 255, 255) if tall else (130, 130, 130))
                    if tall:
                        label = str(mark)
                        labels.append((left + i * fw - len(label) * 2 * sc + sc, y + bh + 2, label))
                    mark += 50
        y += bh
    for x, ly, label in labels:  # last: the label band is painted black above
        text(x, ly, label)
    png_write(path, w, h, rows)


def trace_load_csv(path):
    """Frames back from a trace's CSV (for re-rendering its PNG)."""
    import csv
    frames = []
    modes = {v: k for k, v in MODES.items()}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            frames.append({"t": int(r["t_ms"]), "gcc": int(r["gcc"]), "mode": modes.get(r["display"], 0),
                           "change": int(r["gcc_change"]), "fade": r["fading"] == "1",
                           "px": bytes(int(r[f"r{a}c{b}_{c}"]) for a in range(6) for b in range(4) for c in "rgb")})
    return frames


def trace_summary(frames):
    out = []
    t0 = frames[0]["t"]
    span = frames[-1]["t"] - t0
    gaps = [b["t"] - a["t"] for a, b in zip(frames, frames[1:])] or [0]
    out.append(f"{len(frames)} frames over {span} ms; frame interval avg "
               f"{span / max(len(frames) - 1, 1):.1f} ms, max {max(gaps)} ms")
    out.append("display (t ms from the first frame):")
    segs = []
    for f in frames:
        key = (f["mode"], f["fade"])
        if segs and segs[-1][0] == key:
            segs[-1][2] = f["t"]
        else:
            segs.append([key, f["t"], f["t"]])
    for (mode, fade), a, b in segs:
        out.append(f"  {a - t0:6d} - {b - t0:6d}  {MODES.get(mode, mode)}{' (fading in)' if fade else ''}")
    changes = [(f["t"] - t0, a["gcc"], f["gcc"], f["change"]) for a, f in zip(frames, frames[1:]) if a["gcc"] != f["gcc"]]
    out.append(f"global current: {frames[0]['gcc']} -> {frames[-1]['gcc']}, {len(changes)} changes"
               + (":" if changes else ""))
    for t, a, b, ch in changes[:40]:
        order = {1: "PWM first", 2: "GCC first"}.get(ch, "?")
        out.append(f"  {t:6d} ms  {a:3d} -> {b:3d}  ({order})")
    if len(changes) > 40:
        out.append(f"  ... {len(changes) - 40} more")

    def light(f, k):
        return sum(f["px"][k * 3 + j] for j in range(3)) * f["gcc"]
    jumps = []
    for a, b in zip(frames, frames[1:]):
        for k in range(24):
            la, lb = light(a, k), light(b, k)
            if max(la, lb) >= 300 and min(la, lb) > 0:
                jumps.append((lb / la, b["t"] - t0, k, la, lb))
    jumps.sort(key=lambda j: -abs(j[0] if j[0] >= 1 else 1 / j[0]))
    out.append("biggest frame-to-frame light changes on a key (light = PWM sum x current):")
    for ratio, t, k, la, lb in jumps[:10]:
        out.append(f"  {t:6d} ms  row {k // 4} key {k % 4}: {la} -> {lb} (x{ratio:.2f})")
    fading = [i for i, f in enumerate(frames) if f["fade"]]
    if fading:
        def lum(f, k):
            p = f["px"][k * 3:k * 3 + 3]
            return round((0.3 * p[0] + 0.6 * p[1] + 0.1 * p[2]) * f["gcc"])
        out.extend(fade_checks(frames, fading[0], fading[-1], lum))
    low = [f["t"] - t0 for f in frames if f["gcc"] >= 16 and any(0 < v < 16 for v in f["px"])]
    if low:
        out.append(f"PWM below 16 at a current of 16+ (light may not follow PWM x current): "
                   f"{len(low)} frames, {low[0]} - {low[-1]} ms")
    return "\n".join(out)


def fade_checks(frames, a, b, light):
    """Check a fade into the bars (frames a..b fading) for what has looked
    wrong before (brightness as luminance: PWM x current, green weighted
    most): a key jumping from one frame to the next -- a step much bigger
    than the ones either side of it, as when the current stepped under a key
    held at full PWM, or just a big one -- a key going brighter or dimmer
    than both where it started and where it ends up, a key dipping after it
    had (nearly) reached its final brightness, and big steps of the current
    late in the fade. A live bar (the CPU's, say) moves on its own and can
    trip the first three."""
    t0, end = frames[0]["t"], frames[b]["t"]
    window = list(range(a, b + 1)) + [i for i in range(b + 1, len(frames)) if frames[i]["t"] - end <= 300]
    start, ref = frames[max(a - 1, 0)], frames[window[-1]]
    problems, rounding = [], []
    for k in range(24):
        final, first = light(ref, k), light(start, k)
        vals = [light(frames[i], k) for i in window]
        top = max(vals + [first])
        floor = max(50, 0.02 * top)  # below that the eye can't follow a step
        steps = [math.log(v1 / v0) if min(v0, v1) >= floor else 0.0 for v0, v1 in zip(vals, vals[1:])]
        for n, d in enumerate(steps):
            side = max(abs(steps[n - 1]) if n else 0.0, abs(steps[n + 1]) if n + 1 < len(steps) else 0.0)
            if abs(d) > math.log(1.5) or (abs(d) > math.log(1.12) and abs(d) > 2.5 * side):
                f0, f1 = frames[window[n]], frames[window[n + 1]]
                pwm = max(abs(f1["px"][k * 3 + c] - f0["px"][k * 3 + c]) for c in range(3))
                top_pwm = max(f0["px"][k * 3:k * 3 + 3] + f1["px"][k * 3:k * 3 + 3])
                line = f"  row {k // 4} key {k % 4} jumps {vals[n]} -> {vals[n + 1]} at {f1['t'] - t0} ms"
                if pwm <= 2 and top_pwm <= 12:  # whole PWM steps, all it can do down there
                    rounding.append(f"{line} (PWM {top_pwm} or less, current {f0['gcc']} -> {f1['gcc']})")
                else:
                    problems.append(line)
                break
        if min(first, final) >= 50:
            lo_, hi_ = min(first, final), max(first, final)
            for i, v in zip(window, vals):
                if v < 0.9 * lo_ or v > 1.1 * hi_:
                    problems.append(f"  row {k // 4} key {k % 4} goes to {v} at {frames[i]['t'] - t0} ms, "
                                    f"outside its start {first} and end {final}")
                    break
        if final < 50:
            continue
        near, peak = False, 0
        for i, v in zip(window, vals):
            if not near and abs(v - final) <= 0.15 * final:
                near = True
            if near:
                peak = max(peak, v)
            if near and v < 0.85 * final and v < 0.88 * peak:  # not just a rounding wobble
                problems.append(f"  row {k // 4} key {k % 4} dips to {v} at {frames[i]['t'] - t0} ms "
                                f"after nearing its final {final}")
                break
    notes = rounding
    for i in window[1:]:
        g0, g1 = frames[i - 1]["gcc"], frames[i]["gcc"]
        if g0 == g1 or max(g0, g1) <= 1.1 * min(g0, g1):
            continue  # small steps go by unseen while the keys move
        t = frames[i]["t"]
        if t > end:
            notes.append(f"  the current steps {g0} -> {g1} {t - end} ms after the fade (bars dimmer than "
                         f"the rainbow: it holds the rainbow's for the fade)")
        elif end - t < 150:
            problems.append(f"  the current steps {g0} -> {g1} only {end - t} ms before the fade ends")
    return (["fade checks: " + ("OK" if not problems else f"{len(problems)} problem(s)")] + problems
            + (["fade notes (rounding at a very low PWM; the current stepping after the fade):"] + notes
               if notes else []))


SAVE_HEADER = struct.Struct("<IIIIHBB")  # magic, save number, ms after power-up, power-up counter, frames, frame size, why
SAVE_MAGIC = 0x31435254  # "TRC1"
SAVED_BY = {1: "the save key", 2: "the laptop (trace save)"}


def saved_list(dev):
    """The traces saved in the macropad's flash, oldest save first."""
    st = trace_cmd(dev, 0)
    if st is None:
        sys.exit("no TRACE answer (firmware without the LED trace?)")
    slots, slot_size = st[14], st[16] * 1024
    if not slots:
        sys.exit("this firmware can't save traces (flash the current one)")
    out = []
    for slot in range(slots):
        magic, seq, at, boot, frames, size, why = SAVE_HEADER.unpack(
            trace_read(dev, 4, slot * slot_size, SAVE_HEADER.size))
        if magic == SAVE_MAGIC:
            gap = None
            if frames:  # how long before the save its newest frame was
                raw = trace_read(dev, 4, slot * slot_size + 256 + (frames - 1) * size, 2)
                gap = (at - (raw[0] | raw[1] << 8)) % 65536
            out.append({"slot": slot, "base": slot * slot_size, "seq": seq, "at": at, "boot": boot,
                        "frames": frames, "size": size, "why": why, "gap": gap})
    return sorted(out, key=lambda e: e["seq"]), st


SAVE_GAP_NOTE_MS = 500  # normally the newest frame is a few ms before the save


def saved_line(e):
    line = (f"save {e['seq']}: {e['frames']} frames, saved {e['at'] / 1000:.1f} s after power-up "
            f"(power-up counter {e['boot']}), by {SAVED_BY.get(e['why'], e['why'])}")
    # Frames are recorded only while something shows: saved with the lights
    # off (or the pad suspended), a trace ends where they went off. The
    # frames' times are 16-bit, so the gap is known modulo 65.5 s: at least.
    if e.get("gap") is not None and e["gap"] >= SAVE_GAP_NOTE_MS:
        line += (f"; nothing recorded in at least its last {e['gap'] / 1000:.1f} s "
                 f"(the lights were off, or the pad suspended)")
    return line


def saved_main(dev, argv):
    if argv[:1] == ["save"]:
        st = trace_cmd(dev, 5)
        if st is None or not st[14]:
            sys.exit("this firmware can't save traces (flash the current one)")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(0.2)
            st = trace_cmd(dev, 0)
            if st is not None and not st[15]:
                break
        else:
            sys.exit("the save didn't finish within 15 s")
        saves, _ = saved_list(dev)
        print("saved:", saved_line(saves[-1]) if saves else "?")
        return
    saves, _ = saved_list(dev)  # "saved", with or without N
    if len(argv) < 2:
        if not saves:
            print("no saved traces (hold the top-left key for a second to save one)")
        for e in saves:
            print(saved_line(e))
        return
    want = argv[1]
    pick = saves[-1:] if want == "last" else [e for e in saves if str(e["seq"]) == want]
    if not pick:
        sys.exit(f"no save {want} (trace saved lists them)")
    e = pick[0]
    if e["size"] != TRACE_FRAME:
        sys.exit(f"unexpected trace frame size {e['size']}")
    data = trace_read(dev, 4, e["base"] + 256, e["frames"] * e["size"])
    frames = trace_frames(data, e["frames"], e["size"])
    if not frames:
        sys.exit("that save holds no frames")
    # when the frames were, after power-up: the newest one was just before the save
    raw_last = data[-e["size"]] | data[-e["size"] + 1] << 8
    last = e["at"] - ((e["at"] - raw_last) % 65536)
    first = last - (frames[-1]["t"] - frames[0]["t"])
    trace_write(frames, f"saved{e['seq']}", [
        saved_line(e),
        f"its frames run from {first / 1000:.2f} s to {last / 1000:.2f} s after power-up "
        f"(t below is ms from the first)",
    ])


def trace_write(frames, label, notes=()):
    """Write a trace's CSV, summary and PNG to host/traces/, and print the summary."""
    os.makedirs(TRACE_DIR, exist_ok=True)
    base = os.path.join(TRACE_DIR, time.strftime("trace-%Y%m%d-%H%M%S") + (f"-{label}" if label else ""))
    with open(base + ".csv", "w", encoding="utf-8") as f:
        keys = [f"r{r}c{c}_{ch}" for r in range(6) for c in range(4) for ch in "rgb"]
        f.write(",".join(["frame", "t_ms", "gcc", "display", "gcc_change", "fading"] + keys) + "\n")
        t0 = frames[0]["t"]
        for i, fr in enumerate(frames):
            f.write(",".join(map(str, [i, fr["t"] - t0, fr["gcc"], MODES.get(fr["mode"], fr["mode"]),
                                       fr["change"], int(fr["fade"])] + list(fr["px"]))) + "\n")
    summary = "\n".join(list(notes) + [trace_summary(frames)])
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(summary + "\n")
    trace_png(frames, base + ".png")
    print(summary)
    print(f"saved {base}.csv / .txt / .png")


def trace_main(dev, argv, speed=None):
    label = ""
    if argv[:1] in (["saved"], ["save"]):
        saved_main(dev, argv)
        return
    if argv[:1] == ["--render"]:  # redraw the PNG (and summary) of saved traces
        for path in argv[1:]:
            frames = trace_load_csv(path)
            base = os.path.splitext(path)[0]
            trace_png(frames, base + ".png")
            with open(base + ".txt", "w", encoding="utf-8") as f:
                f.write(trace_summary(frames) + "\n")
            print(f"redrew {base}.png / .txt")
        return
    if argv:
        anim = int(argv[0])
        rounds = int(argv[1].lstrip("x")) if len(argv) > 1 else 1
        label = f"anim{anim}"
        trace_cmd(dev, 3)  # start a fresh recording
        pct = speed or 100
        replay(dev, [anim, 0, rounds, pct & 0xFF, pct >> 8])
        print(f"recording animation {anim}" + (f" x{rounds}" if rounds > 1 else "")
              + (f" at {pct / 100:g}x speed" if pct != 100 else "") + " and the fade after it")
        time.sleep(0.5)
        wait_done(dev, 70.0 * rounds * 100 / pct)
        time.sleep(1.5)  # the fade into the bars, and a little after
    frames = trace_download(dev)
    trace_cmd(dev, 3)  # record again
    if not frames:
        sys.exit("no frames recorded")
    trace_write(frames, label)


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


MSG_COLORS = {"red": (0, 255), "green": (85, 255), "amber": (28, 255), "white": (0, 0)}


def msg_main(dev, argv):
    ap = argparse.ArgumentParser(
        prog="bootanim_preview.py msg",
        description="Scroll a limit message over the live bars, exactly as the "
                    "host sends one: the bars dim out, the text scrolls through, "
                    "the bars fade back in. Only plays while the bars show (the "
                    "service running). Several texts play one after another.")
    ap.add_argument("texts", nargs="+", metavar="TEXT")
    ap.add_argument("-c", "--color", default="red",
                    help="red (the CPU row's, default), green (the dGPU's), "
                         "amber, white, or a QMK hue 0-255")
    ap.add_argument("-v", "--speed", type=int, default=0,
                    help="columns per second (default: the firmware's, 8)")
    ap.add_argument("-r", "--repeat", type=int, default=1,
                    help="play the whole list this many times")
    a = ap.parse_args(argv)
    hue, sat = MSG_COLORS.get(a.color, (None, 255))
    if hue is None:
        try:
            hue = int(a.color)
        except ValueError:
            sys.exit(f"--color: {a.color!r} is not one of {', '.join(MSG_COLORS)} or a hue")
    for t in a.texts:
        if len(t) > TEXT_MAX:
            sys.exit(f"{t!r} is longer than {TEXT_MAX} characters")
    status = replay(dev, [0xFD])
    if status is not None and status[20] != 2:
        print(f"note: the pad shows {MODES.get(status[20], status[20])}, not the bars; "
              "a message only plays over the bars")
    try:
        for _ in range(a.repeat):
            for t in a.texts:
                print(f"scrolling {t!r}")
                raw = t.encode("ascii", "replace")
                dev.write(b"\x00" + bytes([0x07, 0x42, 0x05, 0, hue & 0xFF, sat,
                                           max(0, min(a.speed, 255)), 0])
                          + raw + bytes(24 - len(raw)))
                time.sleep(0.3)
                wait_done(dev, 60.0, byte=22)
                time.sleep(0.8)  # the bars fading back in
    except KeyboardInterrupt:
        print("stopped (the message plays out)")


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


def take_speed(argv):
    """Pull --speed X out of argv: (the rest, percent for BOOT REPLAY or None)."""
    if "--speed" not in argv:
        return argv, None
    i = argv.index("--speed")
    try:
        x = float(argv[i + 1])
    except (IndexError, ValueError):
        sys.exit("--speed needs a number: 0.25 = a quarter of the speed, 2 = twice as fast")
    if not 0.1 <= x <= 10:
        sys.exit("--speed goes from 0.1 to 10")
    return argv[:i] + argv[i + 2:], round(x * 100)


def main():
    argv, speed = sys.argv[1:], None
    if argv[:1] not in (["text"], ["msg"]):  # their own -v/--speed is letters / columns
        argv, speed = take_speed(argv)
    if speed is not None and argv and not (argv[0] == "trace" or argv[0][0].isdigit()):
        sys.exit("--speed works with replays of animations (N, N-M, all) and trace N")
    if argv[:2] == ["trace", "--render"]:  # no macropad needed
        trace_main(None, argv[1:])
        return
    dev = open_dev()
    try:
        if argv[:1] == ["text"]:
            text_main(dev, argv[1:])
            return
        if argv[:1] == ["msg"]:
            msg_main(dev, argv[1:])
            return
        if argv[:1] == ["trace"]:
            trace_main(dev, argv[1:], speed)
            return
        if argv[:1] == ["reboot"]:
            dev = reboot_main(dev, argv[1:])
            return
        if argv[:1] == ["goodbye"]:
            dev.write(b"\x00" + bytes([0x07, 0x42, 0x04]) + bytes(29))
            print("sent goodbye")
            return
        cmd = parse(argv)
        if speed is not None:
            cmd += [speed & 0xFF, speed >> 8]
        print(describe(replay(dev, cmd)))
        print(f"sent {cmd}")
    finally:
        dev.close()


if __name__ == "__main__":
    main()
