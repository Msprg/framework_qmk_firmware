#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Draw the manual's animated figures, rendered by the sysmon firmware itself.

    python animations.py [--rev REV] [--only NAME ...] [--list]

How it works:

  * ../tools/fwsim/fwsim.c compiles keymap.c, unchanged, with QMK's real
    colour maths, lib8tion and LED tables into a PC program. keymap.c comes
    from a commit (--rev, default HEAD), not the working tree, so a half-done
    edit in a checkout can't leak into the pictures. The compiler is QMK
    MSYS's gcc by default; FWSIM_CC names another.
  * Each scene drives that program the way the real host does: frames from
    the host's own row builders (host/fw16_sysmon.py, imported with its
    hardware modules stubbed, so colours and timings follow sysmon.ini's
    defaults), messages from its Messenger, boot animations by BOOT REPLAY.
  * What the firmware sends to the LEDs every 16 ms -- read back from its own
    LED flight recorder -- becomes a CSS-animated SVG: one colour track per
    key, thinned where a straight line between frames does as well. A key's
    light is its PWM times the global current; it's shown on screen
    gamma-encoded, over the dark key.

Needs Python 3.8+ (standard library) and gcc; nothing to install. The output
is deterministic: the simulated clock seeds the animations' randomness, so a
rerun on unchanged firmware rewrites identical files. Readers who ask their
system for reduced motion see each figure's still frame.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import types

from figures import GAP, KEY_OFF, KH, KW, MUTED, TEXT, TOP_H, svg, text

HERE = os.path.dirname(os.path.abspath(__file__))
MANUAL = os.path.dirname(HERE)
SYSMON = os.path.dirname(MANUAL)
HOST = os.path.join(SYSMON, "host")
FWSIM = os.path.join(MANUAL, "tools", "fwsim")
REPO = os.path.abspath(os.path.join(SYSMON, *[os.pardir] * 5))
KEYMAP = "keyboards/framework/macropad/keymaps/sysmon/keymap.c"
DEFAULT_CC = r"C:\QMK_MSYS\mingw64\bin\gcc.exe"

FRAME_MS = 16
SAMPLE = 2       # keep every 2nd firmware frame: 32 ms, ~31 per second
TOLERANCE = 3    # a track's frame may be dropped if a straight line misses it by <= this (0-255)
REPLAY = 0x10    # BOOT REPLAY value id


# --- the simulator ------------------------------------------------------------

def build_fwsim(rev, workdir):
    """Compile fwsim against keymap.c as of `rev`; return the program's path."""
    km = os.path.join(workdir, "km")
    os.makedirs(km, exist_ok=True)
    src = subprocess.run(["git", "-C", REPO, "show", f"{rev}:{KEYMAP}"],
                         capture_output=True, check=True).stdout
    with open(os.path.join(km, "keymap.c"), "wb") as fh:
        fh.write(src)
    cc = os.environ.get("FWSIM_CC") or (DEFAULT_CC if os.path.exists(DEFAULT_CC) else "gcc")
    env = dict(os.environ)
    if os.path.dirname(cc):
        env["PATH"] = os.path.dirname(cc) + os.pathsep + env.get("PATH", "")
    exe = os.path.join(workdir, "fwsim.exe" if os.name == "nt" else "fwsim")
    inc = [FWSIM + "/stub", km, REPO, REPO + "/quantum", REPO + "/platforms",
           REPO + "/drivers/led/issi", REPO + "/keyboards/framework"]
    cmd = [cc, "-std=gnu11", "-O2", "-funsigned-char", "-w",
           "-DRGB_MATRIX_ENABLE", "-DIS31FL3743A", '-DQMK_KEYBOARD_H="qmk_stub.h"']
    for d in inc:
        cmd += ["-I", d]
    cmd += [os.path.join(FWSIM, "fwsim.c"), "-lm", "-o", exe]
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode:
        raise SystemExit(f"fwsim didn't compile:\n{res.stderr[-3000:]}")
    return exe


class Script:
    """Commands for one run of fwsim, and its frames afterwards."""

    def __init__(self, exe):
        self.exe = exe
        self.lines = []
        self.t = 0  # ms since the run started

    def via(self, report):
        report = bytes(report)
        if len(report) == 33:  # with the host's leading report id
            report = report[1:]
        self.lines.append("via " + report.ljust(32, b"\0").hex())

    def wait(self, ms):
        self.lines.append(f"wait {int(ms)}")
        self.t += int(ms)

    def run(self):
        out = subprocess.run([self.exe], input="\n".join(self.lines) + "\n",
                             capture_output=True, text=True, check=True).stdout
        frames, t0 = [], None
        for line in out.splitlines():
            if not line.startswith("F "):
                continue
            _, t, gcc, info, hx = line.split()
            t = int(t)
            if t0 is None:
                t0 = t - FRAME_MS  # the first frame is one frame in
            px = [tuple(int(hx[i + j:i + j + 2], 16) for j in (0, 2, 4))
                  for i in range(0, len(hx), 6)]
            frames.append((t - t0, int(gcc), int(info), px))
        return frames


def boot_replay(n):
    return bytes([0x07, 0x42, REPLAY, n, 0, 0, 0, 0])


# --- the host's side ----------------------------------------------------------

_ENGINE = None


def engine():
    """host/fw16_sysmon.py, imported with its hardware modules stubbed."""
    global _ENGINE
    if _ENGINE is None:
        sys.path.insert(0, HOST)
        for name in ("hid", "psutil"):
            sys.modules.setdefault(name, types.ModuleType(name))
        import fw16_sysmon
        _ENGINE = fw16_sysmon
    return _ENGINE


BASE = dict(has_batt=True, ac=True, charging=False, discharging=False, raw_pct=80,
            cap=80, rate_w=0, cpu=30, cpu_limit=None, mem=55, raw_mem=55, igpu=12,
            npu=0, dgpu_present=True, dgpu=0, dgpu_limit=None, ghost_flags=True,
            dgpu_power="D3cold", dgpu_sleep_s=1000, has_temp=True, temp_c=55, raw_temp=55)


def m_of(**kw):
    m = dict(BASE)
    m.update(kw)
    m["raw_mem"] = m["mem"]
    m["raw_temp"] = m["temp_c"]
    return m


def pad_rows(m):
    """The six rows the host would send for machine state m."""
    return [build(m) for build in engine().ROW_BUILDERS]


def stream(s, state, until_ms, brightness=255, timeout_ms=None, interval=400, events=None):
    """Send the host's frames every `interval` ms up to `until_ms` (script time).
    state(t) -> six RowDesc; events: {t: [report, ...]} sent just before that tick."""
    e = engine()
    events = dict(events or {})
    while s.t < until_ms:
        for t in sorted(k for k in events if k <= s.t):
            for rep in events.pop(t):
                s.via(rep)
        for rep in e.build_frame(state(s.t), brightness, timeout_ms=timeout_ms):
            s.via(rep)
        s.wait(min(interval, until_ms - s.t))


def message(text, hue, seq=1):
    class Capture:
        def write(self, data):
            self.data = bytes(data)
    cap = Capture()
    engine().Messenger._write(cap, text, hue, 255, seq=seq)
    return cap.data


def reason(key, kind, value, unit):
    return engine().Reason(key, kind, 1.0, value, unit)


# --- frames to pictures ---------------------------------------------------------

def screen(rgb, gcc, ref):
    """A key's light (PWM x current, against the scene's reference current) as
    an sRGB colour over the dark key."""
    enc = [min(1.0, c / 255.0 * gcc / ref) ** (1 / 2.2) for c in rgb]
    m = max(enc)
    return tuple(int(round(KEY_OFF[i] * (1 - m) + 255 * enc[i])) for i in range(3))


def hexc(rgb):
    return "#%02x%02x%02x" % rgb


def clip(frames, start, end, ref):
    """Frames in [start, end) of run time, every SAMPLE-th, as (t, [24 colours])
    with t from the clip's start."""
    out = []
    picked = [f for f in frames if start <= f[0] < end]
    for i, (t, gcc, info, px) in enumerate(picked):
        if i % SAMPLE == 0:
            out.append((t - start, [screen(p, gcc, ref) for p in px]))
    return out


def thin(points, tol=TOLERANCE):
    """Drop points a straight line between their neighbours reproduces within
    tol on every channel (Ramer-Douglas-Peucker on a colour-over-time track)."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ta, ca = points[a]
        tb, cb = points[b]
        worst, at = -1, None
        for i in range(a + 1, b):
            t, c = points[i]
            f = (t - ta) / (tb - ta) if tb != ta else 0
            err = max(abs(c[k] - (ca[k] + (cb[k] - ca[k]) * f)) for k in range(3))
            if err > worst:
                worst, at = err, i
        if worst > tol:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [p for p, k in zip(points, keep) if k]


class Anim:
    """An SVG whose elements animate over one loop of `dur` ms."""

    def __init__(self, dur):
        self.dur = dur
        self.body = []
        self.frames_css = []
        self.n = 0

    def _pct(self, t):
        return ("%.2f" % (100.0 * t / self.dur)).rstrip("0").rstrip(".") + "%"

    def _name(self):
        self.n += 1
        return f"a{self.n}"

    def key(self, x, y, w, h, track, poster, rx=7):
        """A key whose fill follows track [(t, rgb)]; `poster` is its still colour."""
        pts = thin(track)
        if all(c == pts[0][1] for _, c in pts):
            self.body.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
                             f'fill="{hexc(pts[0][1])}"/>')
            return
        name = self._name()
        stops, last = [], None
        for t, c in pts + [(self.dur, pts[-1][1])]:
            p = self._pct(t)
            if p != last:
                stops.append(f"{p}{{fill:{hexc(c)}}}")
                last = p
        self.frames_css.append(f"@keyframes {name}{{{''.join(stops)}}}")
        self.body.append(f'<rect class="a" style="animation-name:{name}" x="{x}" y="{y}" '
                         f'width="{w}" height="{h}" rx="{rx}" fill="{hexc(poster)}"/>')

    def phases(self, windows, poster_t, draw):
        """Show draw(i) (svg text) only during windows[i] = (t0, t1). Animates
        fill-opacity, not opacity: Chromium runs opacity on the compositor,
        where a headless browser's virtual clock (the only way to check these
        frame by frame) doesn't reach it."""
        for i, (t0, t1) in enumerate(windows):
            name = self._name()
            on = t0 <= poster_t < t1
            stops = []
            if t0 > 0:
                stops.append("0%{fill-opacity:0}")
            stops.append(f"{self._pct(t0)}{{fill-opacity:1}}")
            if t1 < self.dur:
                stops.append(f"{self._pct(t1)}{{fill-opacity:0}}")
            self.frames_css.append(f"@keyframes {name}{{{''.join(stops)}}}")
            self.body.append(f'<g class="s" style="animation-name:{name}" '
                             f'fill-opacity="{1 if on else 0}">{draw(i)}</g>')

    def cursor(self, x0, x1, y0, y1, poster_t):
        name = self._name()
        self.frames_css.append(f"@keyframes {name}{{0%{{transform:translateX(0)}}"
                               f"100%{{transform:translateX({x1 - x0}px)}}}}")
        # the transform attribute is the still frame's position; the CSS
        # animation's transform overrides it while it runs
        px = (x1 - x0) * poster_t / self.dur
        self.body.append(f'<line class="a" style="animation-name:{name}" '
                         f'transform="translate({px:.1f} 0)" x1="{x0}" y1="{y0}" x2="{x0}" '
                         f'y2="{y1}" stroke="{TEXT}" stroke-opacity="0.7" stroke-width="1.5"/>')

    def static(self, piece):
        self.body.append(piece)

    def render(self, width, height, title):
        d = "%.3fs" % (self.dur / 1000.0)
        style = ("<style>"
                 f".a{{animation:{d} linear infinite}}"
                 f".s{{animation:{d} step-end infinite}}"
                 "@media (prefers-reduced-motion:reduce){.a,.s{animation:none}}"
                 + "".join(self.frames_css) + "</style>")
        return svg(width, height, [style] + self.body, title)


def pad_keys(anim, x0, y0, track_of, poster_of, kw=KW, gap=GAP, top_h=TOP_H, kh=KH,
             rows=range(6), rx=7):
    """Animated keys for pad rows `rows`, laid out as the pad (short top row)."""
    y = y0
    for r in rows:
        h = top_h if r == 0 else kh
        for c in range(4):
            anim.key(x0 + c * (kw + gap), y, kw, h, track_of(r, c), poster_of(r, c), rx=rx)
        y += h + gap
    return y - y0 - gap


def tracks(samples):
    """samples [(t, [24 colours])] -> function (row, col) -> [(t, colour)]."""
    return lambda r, c: [(t, px[r * 4 + c]) for t, px in samples]


def poster(samples, t):
    best = min(samples, key=lambda s: abs(s[0] - t))[1]
    return lambda r, c: best[r * 4 + c]


def label(x, y, main, sub=None, size=15):
    if sub:
        return text(x, y - 2, main, size, weight="600") + text(x, y + 15, sub, 12.5, MUTED)
    return text(x, y + 5, main, size, weight="600")


# --- scenes ---------------------------------------------------------------------

def fig_pad_map(exe):
    """The six rows, live: CPU load rises into a STAPM limit (it breathes), the
    NPU wakes behind the iGPU, the temperature follows, the dGPU sleeps."""
    warm, dur = 2000, 12288  # 8 brisk breaths
    stapm = reason("stapm", "power", 28, "W")

    def state(t):
        c = t - warm
        busy = 4000 <= c < 9000
        cpu = 88 if busy else (30 + 25 * c / 4000 if 0 <= c < 4000 else 30)
        npu = 60 if 2000 <= c < 7000 else 0
        temp = 68 if 4800 <= c < 9600 else 54
        return pad_rows(m_of(cpu=cpu, cpu_limit=stapm if busy else None, npu=npu,
                             temp_c=temp, mem=58))
    s = Script(exe)
    stream(s, state, warm + dur)
    sm = clip(s.run(), warm, warm + dur, 185)
    a = Anim(dur)
    h = pad_keys(a, 24, 24, tracks(sm), poster(sm, 6000))
    labels = ["Battery|charge, charging / draining", "CPU|usage; breathes when held back",
              "Memory|usage; blinks at 90 %+", "iGPU|usage, with the NPU behind it",
              "dGPU|usage; teal from the right = powered off",
              "Temperature|30-90 \u00b0C; blinks above 90 \u00b0C"]
    y = 24
    for r, lab in enumerate(labels):
        rh = TOP_H if r == 0 else KH
        main, sub = lab.split("|")
        a.static(label(24 + 4 * (KW + GAP) + 14, y + rh / 2, main, sub))
        y += rh + GAP
    return a.render(560, h + 48, "The macropad's six rows, live")


STRIP_LABEL_W = 400


def strip_figure(title, items, samples, poster_t, dur, note=None, tall=False, label_w=STRIP_LABEL_W):
    """One row per item: (caption, sub, sim row) -> animated strip."""
    a = Anim(dur)
    tr, po = tracks(samples), poster(samples, poster_t)
    y = 24
    h = KH if tall else TOP_H
    for caption, sub, row in items:
        for c in range(4):
            a.key(label_w + c * (KW + GAP), y, KW, h, tr(row, c), po(row, c))
        a.static(label(20, y + h / 2, caption, sub))
        y += h + 18
    if note:
        a.static(text(20, y + 6, note, 12.5, MUTED))
        y += 22
    return a.render(label_w + 4 * (KW + GAP) + 30, y + 6, title)


def fig_battery(exe):
    warm, dur = 2000, 8192  # two charge pulses, eight blinks
    e = engine()
    states = [
        m_of(charging=True, raw_pct=50, cap=100, rate_w=30),                 # 2 keys pulse
        m_of(ac=False, discharging=True, raw_pct=60, cap=100, rate_w=15),    # 1 key flares
        m_of(ac=True, discharging=True, raw_pct=60, cap=100, rate_w=25),     # amber
        m_of(),                                                              # idle
        m_of(ac=True, discharging=True, raw_pct=79, cap=80, rate_w=3),      # drift tell
        m_of(ac=False, discharging=True, raw_pct=12, cap=100, rate_w=12),    # critical
    ]
    rows = [e.build_battery_row(m) for m in states]
    s = Script(exe)
    stream(s, lambda t: rows, warm + dur)
    sm = clip(s.run(), warm, warm + dur, 185)
    items = [
        ("Charging, at 50 %", "red \u2192 green as it charges; keys past the edge pulse", 0),
        ("On battery, at 60 %", "red; the edge key flares and fades", 1),
        ("On AC, battery helping the charger", "amber: the charger can't keep up", 2),
        ("On AC, battery idle", "the whole row steady dim green", 3),
        ("On AC, battery drifting", "the last key breathes, barely visibly (by design)", 4),
        ("Draining, below 15 %", "the whole row red, blinking", 5),
    ]
    return strip_figure("The battery row", items, sm, 1000, dur,
                        note="More keys pulse the more power flows: 1 key below 20 W, "
                             "all 4 from 75 W.")


def fig_rhythms(exe):
    warm, dur = 2000, 12288  # 1.536 s and 4.096 s and 1.024 s all fit
    e = engine()
    rows = [
        e.build_cpu_row(m_of(cpu=75)),
        e.build_battery_row(m_of(charging=True, raw_pct=25, cap=100, rate_w=80)),
        e.build_cpu_row(m_of(cpu=75, cpu_limit=reason("stapm", "power", 28, "W"))),
        e.build_cpu_row(m_of(cpu=75, cpu_limit=reason("charger", "charger", None, ""))),
        e.build_mem_row(m_of(mem=93)),
        e.build_temp_row(m_of(temp_c=50)),
    ]
    s = Script(exe)
    stream(s, lambda t: rows, warm + dur)
    frames = s.run()
    sm = clip(frames, warm, warm + dur, 185)
    lanes = [("Steady", "a bar showing a level", 0, 0),
             ("Pulse, ~4 s", "battery: power flowing in or out", 1, 2),
             ("Brisk breathe, ~1.5 s", "CPU / dGPU at one of its own limits", 2, 0),
             ("Slow breathe, ~4 s", "CPU / dGPU held back by the charger", 3, 0),
             ("Blink, ~1 Hz", "critical: battery, memory, temperature", 4, 0)]
    kw, gap, kh = 30, 6, 30
    row_x, plot_x, plot_w, lane_h, lane_gap = 300, 470, 420, 52, 22
    a = Anim(dur)
    tr, po = tracks(sm), poster(sm, 700)
    y = 30
    for name, sub, row, key in lanes:
        a.static(label(20, y + lane_h / 2, name, sub))
        for c in range(4):
            a.key(row_x + c * (kw + gap), y + (lane_h - kh) / 2, kw, kh, tr(row, c), po(row, c), rx=5)
        a.static(f'<rect x="{plot_x}" y="{y}" width="{plot_w}" height="{lane_h}" rx="6" fill="#202329"/>')
        # the key's PWM straight from the firmware: its brightest channel
        curve = [(t - warm, (max(px[row * 4 + key]),) * 3)
                 for t, gcc, info, px in frames if warm <= t < warm + dur]
        pts = [f"{plot_x + plot_w * t / dur:.1f},{y + lane_h - 4 - (lane_h - 8) * v[0] / 255.0:.1f}"
               for t, v in thin(curve, tol=1)]
        a.static(f'<polyline points="{" ".join(pts)}" fill="none" stroke="#f2c14e" stroke-width="1.6"/>')
        y += lane_h + lane_gap
    top, bottom = 30, y - lane_gap
    a.cursor(plot_x, plot_x + plot_w, top - 4, bottom + 4, 700)
    for sec in range(0, int(dur / 1000) + 1, 2):
        x = plot_x + plot_w * sec * 1000 / dur
        a.static(f'<line x1="{x:.1f}" y1="{bottom + 6}" x2="{x:.1f}" y2="{bottom + 12}" stroke="{MUTED}"/>')
        a.static(text(round(x, 1), bottom + 28, f"{sec} s", 12, MUTED, anchor="middle"))
    a.static(text(plot_x, 20, "brightness \u2191   time \u2192 (the firmware's own values)", 12, MUTED))
    return a.render(plot_x + plot_w + 24, bottom + 44, "The pad's rhythms")


def story_figure(title, sim_row, samples, dur, phases, poster_t, note=None, tall=True,
                 key_x=24, label_x=300, width=680):
    """One row that changes over time, with the caption of each phase beside it."""
    a = Anim(dur)
    tr, po = tracks(samples), poster(samples, poster_t)
    h = KH if tall else TOP_H
    y = 24
    for c in range(4):
        a.key(key_x + c * (KW + GAP), y, KW, h, tr(sim_row, c), po(sim_row, c))
    a.phases([(t0, t1) for t0, t1, _, _ in phases], poster_t,
             lambda i: label(label_x, y + h / 2, phases[i][2], phases[i][3]))
    y += h + 18
    if note:
        a.static(text(key_x, y + 6, note, 12.5, MUTED))
        y += 22
    return a.render(width, y + 6, title)


def fig_npu(exe):
    warm, dur = 2000, 9000
    e = engine()
    plan = [(0, 3000, 50, 0), (3000, 6000, 25, 75), (6000, 9000, 75, 25)]

    def state(t):
        c = (t - warm) % dur if t >= warm else 0
        ig, npu = next((i, n) for t0, t1, i, n in plan if t0 <= c < t1)
        return [e.build_igpu_row(m_of(igpu=ig, npu=npu))] * 6
    s = Script(exe)
    stream(s, state, warm + dur)
    sm = clip(s.run(), warm, warm + dur, 185)
    phases = [(0, 3000, "NPU idle", "just the iGPU's red bar (here 50 %)"),
              (3000, 6000, "NPU busier than the iGPU", "dim cyan past the red bar (iGPU 25 %, NPU 75 %)"),
              (6000, 9000, "iGPU busier than the NPU", "the cyan is hidden behind the red bar (75 % / 25 %)")]
    return story_figure("The iGPU row and its NPU ghost bar", 1, sm, dur, phases, 4500, width=720)


def fig_dgpu_sleep(exe):
    warm, dur = 2000, 15000
    e = engine()
    awake = dict(dgpu_power="D0", dgpu_sleep_s=None)
    # (start, end, machine state): the dGPU's power state as PnP reports it
    plan = [(0, 2000, dict(awake, dgpu=40)),
            (2000, 3500, dict(dgpu=0, dgpu_power="D3hot", dgpu_sleep_s=0)),
            (3500, 5000, dict(dgpu=0, dgpu_power="D3cold", dgpu_sleep_s=0)),
            (5000, 7000, dict(dgpu=0, dgpu_power="D3cold", dgpu_sleep_s=900)),
            (7000, 9000, dict(dgpu=0, dgpu_power="D3cold", dgpu_sleep_s=1800)),
            (9000, 11000, dict(dgpu=0, dgpu_power="D3cold", dgpu_sleep_s=2700)),
            (11000, 12000, dict(awake, dgpu=2)),
            (12000, 12800, dict(dgpu=0, dgpu_power="D3hot", dgpu_sleep_s=0)),
            (12800, 15000, dict(dgpu=0, dgpu_power="D3cold", dgpu_sleep_s=0))]
    phases = [(0, 2000, "Awake, 40 % busy", "the green usage bar"),
              (2000, 3500, "Asleep, link still up (D3hot)", "for a few seconds the last key glows dim orange"),
              (3500, 5000, "Powered off (D3cold)", "the key turns dim teal"),
              (5000, 7000, "Off for 15 minutes", "a second key"),
              (7000, 9000, "Off for 30 minutes", "a third key"),
              (9000, 11000, "Off for 45 minutes or more", "all four keys"),
              (11000, 12000, "Something wakes it", "even for a second: the marker fades"),
              (12000, 15000, "Off again", "orange for a moment, then teal from one key")]

    def state(t):
        c = (t - warm) % dur if t >= warm else 0
        kw = next(p[2] for p in plan if p[0] <= c < p[1])
        return [e.build_dgpu_row(m_of(**kw))] * 6
    s = Script(exe)
    stream(s, state, warm + dur)
    sm = clip(s.run(), warm, warm + dur, 185)
    return story_figure("The dGPU sleep marker", 1, sm, dur, phases, 8000,
                        note="Time-lapse: each step is 15 minutes in real life.", width=720)


def fig_temperature(exe):
    warm, dur = 2000, 12000
    e = engine()
    plan = [(0, 2000, 45), (2000, 4000, 60), (4000, 6000, 75), (6000, 8000, 88),
            (8000, 11000, 93), (11000, 12000, 65)]

    def state(t):
        c = (t - warm) % dur if t >= warm else 0
        tc = next(v for t0, t1, v in plan if t0 <= c < t1)
        return [e.build_temp_row(m_of(temp_c=tc))] * 6
    s = Script(exe)
    stream(s, state, warm + dur)
    sm = clip(s.run(), warm, warm + dur, 185)
    subs = {45: "a quarter of the bar, green", 60: "half the bar",
            75: "into yellow and orange", 88: "nearly full",
            93: "above 90 \u00b0C: the whole bar blinks", 65: "cooling down"}
    phases = [(t0, t1, f"{v} \u00b0C", subs[v]) for t0, t1, v in plan]
    return story_figure("The temperature row", 1, sm, dur, phases, 5000,
                        note="The colours are fixed per key, green on the left to red on the "
                             "right; the length says how hot.", width=700)


def full_pad_story(title, samples, dur, phases, poster_t, note=None):
    a = Anim(dur)
    h = pad_keys(a, 24, 24, tracks(samples), poster(samples, poster_t))
    x = 24 + 4 * (KW + GAP) + 18
    a.phases([(p[0], p[1]) for p in phases], poster_t,
             lambda i: label(x, 60, phases[i][2], phases[i][3]))
    height = h + 48
    if note:
        a.static(text(x, h + 10, note, 12.5, MUTED))
    return a.render(620, height, title)


def busy_pad(t):
    return pad_rows(m_of(cpu=86, cpu_limit=reason("stapm", "power", 28, "W"), mem=61,
                         igpu=35, temp_c=72))


def fig_message(exe):
    warm = 2000
    s = Script(exe)
    stream(s, busy_pad, warm + 1500)
    stream(s, busy_pad, warm + 16000,
           events={warm + 1500: [message("CPU STAPM 28W", engine().HUE_RED)]})
    frames = s.run()
    shown = [f[0] for f in frames if f[0] >= warm and f[2] & 0x20]
    msg_on, msg_off = shown[0], shown[-1]
    fading = [f[0] for f in frames if msg_off < f[0] < msg_off + 2000 and f[2] & 0x10]
    end = (fading[-1] if fading else msg_off + 700) + 1800
    dur = end - warm
    sm = clip(frames, warm, end, 185)
    rel = lambda t: t - warm
    phases = [(0, rel(msg_on), "A limit binds", "the CPU row breathes (STAPM)"),
              (rel(msg_on), rel(msg_on) + 300, "The bars dim out", ""),
              (rel(msg_on) + 300, rel(msg_off), "The pad names it", "CPU STAPM 28W, in the row's colour"),
              (rel(msg_off), dur, "The bars fade back in", "showing the readings of now")]
    phases = [(t0, t1, m, sb or None) for t0, t1, m, sb in phases]
    return full_pad_story("A limit message", sm, dur, phases, rel(msg_on) + 3000)


def fig_warning(exe):
    warm, timeout = 2000, 2000
    s = Script(exe)
    stream(s, busy_pad, warm + 1500, timeout_ms=timeout)
    stop = s.t
    s.wait(timeout + 10000 + 1800)
    frames = s.run()
    warn = [f[0] for f in frames if f[0] > stop and (f[2] & 3) == 3]
    end = warn[-1] + 1800
    dur = end - warm
    sm = clip(frames, warm, end, 185)
    rel = lambda t: t - warm
    phases = [(0, rel(stop), "The bars", "the host program running"),
              (rel(stop), rel(warn[0]), "Frames stop", "a crash: no goodbye"),
              (rel(warn[0]), rel(warn[-1]), "The disconnect warning", "black and yellow stripes, 10 s"),
              (rel(warn[-1]), dur, "Then the regular RGB effect", "(not drawn here)")]
    return full_pad_story("The disconnect warning", sm, dur, phases, rel(warn[0]) + 4000,
                          note="Timeout shortened to 2 s here; 5 s by default.")


def fig_rainbow(exe):
    """Power-up: animation 5 (Twinkle), the still rainbow, then the host
    connects and the bars fade in over it."""
    s = Script(exe)
    s.via(boot_replay(5))  # before the first frame: replaces the power-up pick
    s.wait(6500)           # gap 300 + Twinkle 2400 + hold 1800, then the still rainbow
    connect = s.t
    stream(s, lambda t: pad_rows(m_of(cpu=40, mem=58, igpu=20, temp_c=56)),
           connect + 3200, brightness=33)
    frames = s.run()
    end = s.t
    sm = clip(frames, 0, end, 3)
    phases = [(0, 4500, "Power-up animation", "here 5, Twinkle: one of 22"),
              (4500, connect, "The boot rainbow", "dim and still, until the host connects"),
              (connect, connect + 900, "The host connects", "the bars fade in over the rainbow"),
              (connect + 900, end, "The bars", "")]
    phases = [(t0, t1, m, sb or None) for t0, t1, m, sb in phases]
    return full_pad_story("From power-up to the bars", sm, end, phases, 5000)


def fig_boot(exe, n):
    """Boot animation n as BOOT REPLAY plays it: a dark gap, the animation, a
    moment of the finished rainbow."""
    s = Script(exe)
    s.via(boot_replay(n))
    s.wait(13500)
    frames = [f for f in s.run() if f[2] & 3 == 1]  # while it shows
    last_change = max(i for i in range(1, len(frames)) if frames[i][3] != frames[i - 1][3])
    end = frames[last_change][0] + 1200
    sm = clip(frames, 0, end, 3)
    kw, gap, top, kh, pad = 16, 4, 10, 16, 8
    a = Anim(end)
    h = pad_keys(a, pad, pad, tracks(sm), poster(sm, end - 1), kw=kw, gap=gap, top_h=top,
                 kh=kh, rx=3)
    w = 2 * pad + 4 * kw + 3 * gap
    a.static(text(w / 2, pad + h + 17, str(n), 12, MUTED, anchor="middle"))
    return a.render(w, pad + h + 26, f"Boot animation {n}")


FIGURES = {
    "pad-map.svg": fig_pad_map,
    "battery.svg": fig_battery,
    "rhythms.svg": fig_rhythms,
    "npu-ghost.svg": fig_npu,
    "dgpu-sleep.svg": fig_dgpu_sleep,
    "temperature.svg": fig_temperature,
    "message.svg": fig_message,
    "warning.svg": fig_warning,
    "rainbow.svg": fig_rainbow,
}
for _n in range(1, 23):
    FIGURES[f"boot-{_n:02d}.svg"] = (lambda n: lambda exe: fig_boot(exe, n))(_n)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rev", default="HEAD", help="commit to take keymap.c from (default HEAD)")
    ap.add_argument("--only", nargs="*", help="figure names to draw (default: all)")
    ap.add_argument("--list", action="store_true", help="list the figures and exit")
    args = ap.parse_args()
    if args.list:
        print("\n".join(FIGURES))
        return
    names = args.only or list(FIGURES)
    work = tempfile.mkdtemp(prefix="fwsim-")
    try:
        exe = build_fwsim(args.rev, work)
        for name in names:
            data = FIGURES[name](exe)
            with open(os.path.join(HERE, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(data)
            print(f"{name}  {len(data) // 1024} KB")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
