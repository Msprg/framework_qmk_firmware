#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Draw the user manual's figures: schematic pictures of the macropad's keys.

    python figures.py            (writes the .svg files next to this script)

Standard library only. The pictures are schematic: colours come from the
same QMK hues the host sends (see sysmon.ini), brightness is drawn as a mix
of the lit colour into the dark key, and keys that animate are marked with a
dashed outline. Real LEDs look different from a screen -- greens are much
stronger, and dim keys read dimmer -- so these show *which* keys light and
roughly in what colour, not exactly how it looks.

Geometry follows keyboards/framework/macropad/info.json: four columns, six
rows, the top row's keys shorter than the rest. Bars fill from the left key,
as in keymap.c (sysmon_row_leds).
"""

import colorsys
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# --- palette ----------------------------------------------------------------
PANEL = "#16181c"
KEY_OFF = (0x2a, 0x2d, 0x33)
TEXT = "#e6e6e6"
MUTED = "#9aa0a6"
FONT = "Segoe UI, Helvetica, Arial, sans-serif"

# QMK hues used by the host's defaults (fw16_sysmon.py / sysmon.ini)
RED, AMBER, GOLD, GREEN, NPU, DGPU_OFF, YELLOW = 0, 28, 36, 85, 128, 140, 40
TEMP_LO, TEMP_HI = 85, 0

# key geometry (px)
KW, GAP = 50, 10
TOP_H, KH = 30, 50


def rgb_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def lit(hue, v=1.0, sat=1.0):
    """The colour of a key lit at QMK hue `hue` (0-255), brightness v (0-1)."""
    r, g, b = colorsys.hsv_to_rgb((hue % 256) / 256.0, sat, 1.0)
    full = (r * 255, g * 255, b * 255)
    # a dim key: mostly the dark key with a tint of its colour
    v = max(0.0, min(1.0, v))
    return tuple(o + (f - o) * v for o, f in zip(KEY_OFF, full))


class Key:
    """One key: its fill colour, and whether it animates (dashed outline)."""

    def __init__(self, rgb=None, mark=False):
        self.rgb = rgb or KEY_OFF
        self.mark = mark


def off():
    return Key()


def bar(level_pct, hue, v=1.0, gradient=None):
    """A row's four keys for a bar at level_pct, as the firmware fills it:
    whole keys up to the level, the next one partly lit for the remainder."""
    keys = []
    for i in range(4):
        part = max(0.0, min(1.0, level_pct / 25.0 - i))
        h = hue if gradient is None else gradient[i]
        keys.append(Key(lit(h, v * part)) if part > 0 else off())
    return keys


def temp_gradient():
    return [TEMP_LO + (TEMP_HI - TEMP_LO) * i / 3.0 for i in range(4)]


# --- drawing ------------------------------------------------------------------

def key_rect(x, y, w, h, key):
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" '
           f'fill="{rgb_hex(key.rgb)}"/>']
    if key.mark:
        out.append(f'<rect x="{x - 3}" y="{y - 3}" width="{w + 6}" height="{h + 6}" '
                   f'rx="9" fill="none" stroke="{TEXT}" stroke-opacity="0.75" '
                   f'stroke-width="1.6" stroke-dasharray="4 3"/>')
    return "".join(out)


def text(x, y, s, size=14, color=TEXT, anchor="start", weight="normal"):
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
            f'fill="{color}" text-anchor="{anchor}" font-weight="{weight}">{s}</text>')


def svg(width, height, body, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="{title}">'
            f'<title>{title}</title>'
            f'<rect width="{width}" height="{height}" rx="12" fill="{PANEL}"/>'
            + "".join(body) + "</svg>\n")


def pad(x0, y0, rows, labels=None, label_x=None):
    """A whole pad (six rows of four keys) at x0, y0; optional row labels to
    the right. Returns (svg pieces, height)."""
    body, y = [], y0
    for r, keys in enumerate(rows):
        h = TOP_H if r == 0 else KH
        for c, key in enumerate(keys):
            body.append(key_rect(x0 + c * (KW + GAP), y, KW, h, key))
        if labels:
            lx = label_x if label_x is not None else x0 + 4 * (KW + GAP) + 14
            main, sub = (labels[r] + "|").split("|")[:2]
            if sub:
                body.append(text(lx, y + h / 2 - 2, main, 15, weight="600"))
                body.append(text(lx, y + h / 2 + 15, sub, 12.5, MUTED))
            else:
                body.append(text(lx, y + h / 2 + 5, main, 15, weight="600"))
        y += h + GAP
    return body, y - y0 - GAP


def strips(title, items, label_w=330, note=None):
    """Single rows with a caption each: [(caption, subcaption, keys, tall)]."""
    width = label_w + 4 * (KW + GAP) + 30
    body, y = [], 24
    for caption, sub, keys, tall in items:
        h = KH if tall else TOP_H
        for c, key in enumerate(keys):
            body.append(key_rect(label_w + c * (KW + GAP), y, KW, h, key))
        if sub:
            body.append(text(20, y + h / 2 - 3, caption, 15, weight="600"))
            body.append(text(20, y + h / 2 + 14, sub, 12.5, MUTED))
        else:
            body.append(text(20, y + h / 2 + 5, caption, 15, weight="600"))
        y += h + 18
    if note:
        body.append(text(20, y + 6, note, 12.5, MUTED))
        y += 22
    return svg(width, y + 6, body, title)


# --- figures ------------------------------------------------------------------

def fig_pad_map():
    rows = [
        [Key(lit(GREEN, 0.38)) for _ in range(4)],          # battery idle on AC
        bar(40, RED),                                         # CPU
        bar(60, GOLD),                                        # memory
        [Key(lit(RED)), Key(lit(NPU, 0.45)), off(), off()],   # iGPU 25 %, NPU 50 %
        [off(), off(), off(), Key(lit(DGPU_OFF, 0.38))],      # dGPU asleep
        bar(55, None, gradient=temp_gradient()),              # temperature ~63 C
    ]
    labels = ["Battery|charge, charging / draining",
              "CPU|usage; breathes when held back",
              "Memory|usage; blinks at 90 %+",
              "iGPU|usage, with the NPU behind it",
              "dGPU|usage; teal from the right = asleep",
              "Temperature|30-90 °C; blinks above 90 °C"]
    body, h = pad(24, 24, rows, labels)
    return svg(560, h + 48, body, "The macropad's six rows")


def fig_bar_levels():
    items = [(f"{p} %", sub, bar(p, RED), True) for p, sub in (
        (0, "nothing lit"),
        (10, "the first key, partly lit"),
        (25, "one key"),
        (60, "two keys, the third partly lit"),
        (100, "all four keys"))]
    return strips("Reading a bar", items, label_w=270)


def fig_battery():
    charge_hue = 50 * 85 // 100
    charging = bar(50, charge_hue)
    charging[2] = Key(lit(charge_hue, 0.7), mark=True)
    charging[3] = Key(lit(charge_hue, 0.7), mark=True)
    draining = bar(60, RED)
    draining[2] = Key(lit(RED, 0.75), mark=True)
    assist = bar(60, AMBER)
    assist[2] = Key(lit(AMBER, 0.75), mark=True)
    items = [
        ("Charging, at 50 %", "red → green as it charges; keys past "
         "the edge pulse", charging, False),
        ("On battery, at 60 %", "red; the edge key flares and fades",
         draining, False),
        ("On AC, battery helping the charger", "amber: the charger can't keep up",
         assist, False),
        ("On AC, battery idle", "the whole row steady dim green",
         [Key(lit(GREEN, 0.38)) for _ in range(4)], False),
        ("On battery, below 15 %", "the whole row red, blinking",
         [Key(lit(RED), mark=True) for _ in range(4)], False),
    ]
    return strips("The battery row", items, label_w=420,
                  note="Dashed outline = the key animates. More keys pulse the "
                       "more power flows: 1 key below 20 W, all 4 from 75 W.")


def fig_npu():
    items = [
        ("NPU idle", "just the iGPU's red bar (here 50 %)", bar(50, RED), True),
        ("NPU busier than the iGPU", "dim cyan past the red bar (iGPU 25 %, NPU 75 %)",
         [Key(lit(RED)), Key(lit(NPU, 0.45)), Key(lit(NPU, 0.45)), off()], True),
        ("iGPU busier than the NPU", "the cyan is hidden behind the red bar",
         bar(75, RED), True),
    ]
    return strips("The iGPU row and its NPU ghost bar", items, label_w=400)


def fig_dgpu_sleep():
    t = lambda: Key(lit(DGPU_OFF, 0.38))
    items = [
        ("dGPU awake, 40 % busy", "the green usage bar", bar(40, GREEN), True),
        ("Powered off (D3cold)", "the last key glows dim teal", [off(), off(), off(), t()], True),
        ("Off for 15 minutes", "a second key", [off(), off(), t(), t()], True),
        ("Off for 30 minutes", "a third key", [off(), t(), t(), t()], True),
        ("Off for 45 minutes or more", "all four keys", [t(), t(), t(), t()], True),
    ]
    return strips("The dGPU sleep marker", items, label_w=330,
                  note="Any wake, even a second long, fades it out; it starts "
                       "again from one key.")


def fig_temperature():
    g = temp_gradient()
    hot = bar(100, None, gradient=g)
    for k in hot:
        k.mark = True
    items = [
        ("45 °C", "a quarter of the bar, green", bar(25, None, gradient=g), True),
        ("70 °C", "into yellow and orange", bar(67, None, gradient=g), True),
        ("Above 90 °C", "the full bar, blinking", hot, True),
    ]
    return strips("The temperature row", items, label_w=300,
                  note="The colours are fixed per key, green on the left to red "
                       "on the right; the length says how hot.")


def fig_warning():
    rows = []
    for r in range(6):
        row = []
        for c in range(4):
            on = (((r + c) // 2) & 1) == 0
            row.append(Key(lit(YELLOW)) if on else off())
        rows.append(row)
    body, h = pad(24, 24, rows)
    body.append(text(24 + 4 * (KW + GAP) + 14, 24 + 40, "Disconnect warning", 15, weight="600"))
    for i, line in enumerate(("Black and yellow caution stripes,",
                              "shown for 10 s when the host program",
                              "stopped sending without saying",
                              "goodbye (a crash, a hang). Then the",
                              "pad falls back to its regular",
                              "RGB effect.")):
        body.append(text(24 + 4 * (KW + GAP) + 14, 24 + 66 + i * 19, line, 13, MUTED))
    return svg(560, h + 48, body, "The disconnect warning")


def fig_rainbow():
    rows = []
    for r in range(6):
        row = []
        for c in range(4):
            pos = (c / 3.0 + r / 5.0) / 2.0
            row.append(Key(lit(0 + 205 * pos, 0.8)))
        rows.append(row)
    body, h = pad(24, 24, rows)
    x = 24 + 4 * (KW + GAP) + 14
    body.append(text(x, 24 + 40, "Boot rainbow", 15, weight="600"))
    for i, line in enumerate(("Dim and still, red at the top-left",
                              "to violet at the bottom-right,",
                              "from power-up until the host",
                              "program first connects. A short",
                              "animation introduces it, a",
                              "different one each power-up.")):
        body.append(text(x, 24 + 66 + i * 19, line, 13, MUTED))
    return svg(560, h + 48, body, "The boot rainbow")


def fig_rhythms():
    """Brightness over 6 s for each of the pad's rhythms."""
    W, H = 760, 470
    x0, plot_w, lane_h, lane_gap = 250, 480, 52, 22
    seconds = 6.0

    def steady(t):
        return 1.0

    def blink(t):
        return 1.0 if (t % 1.024) < 0.512 else 0.0

    def breathe(period, floor):
        def f(t):
            ph = (t % period) / period
            env = (math.sin(2 * math.pi * ph - math.pi / 2) + 1) / 2
            return floor + (1 - floor) * env
        return f

    def pulse(t):
        p = 4.096
        q = (t % p) / (p / 4)
        s = q if q < 1 else 1 if q < 2 else 3 - q if q < 3 else 0
        return math.sqrt(max(0.0, s))

    lanes = [
        ("Steady", "a bar showing a level", steady),
        ("Pulse, ~4 s", "battery: power flowing in or out", pulse),
        ("Brisk breathe, ~1.5 s", "CPU / dGPU at one of its own limits",
         breathe(1.536, 110 / 255)),
        ("Slow breathe, ~4 s", "CPU / dGPU held back by the charger",
         breathe(4.096, 110 / 255)),
        ("Blink, ~1 Hz", "critical: battery, memory, temperature", blink),
    ]
    body, y = [], 28
    for name, sub, f in lanes:
        body.append(text(20, y + lane_h / 2 - 2, name, 15, weight="600"))
        body.append(text(20, y + lane_h / 2 + 15, sub, 12.5, MUTED))
        body.append(f'<rect x="{x0}" y="{y}" width="{plot_w}" height="{lane_h}" '
                    f'rx="6" fill="#202329"/>')
        pts = []
        n = 300
        for i in range(n + 1):
            t = seconds * i / n
            v = f(t)
            pts.append(f"{x0 + plot_w * i / n:.1f},{y + lane_h - 4 - (lane_h - 8) * v:.1f}")
        body.append(f'<polyline points="{" ".join(pts)}" fill="none" '
                    f'stroke="#f2c14e" stroke-width="2"/>')
        y += lane_h + lane_gap
    for s in range(int(seconds) + 1):
        x = x0 + plot_w * s / seconds
        body.append(f'<line x1="{x}" y1="{y - lane_gap + 4}" x2="{x}" y2="{y - lane_gap + 10}" '
                    f'stroke="{MUTED}"/>')
        body.append(text(x, y - lane_gap + 26, f"{s} s", 12, MUTED, anchor="middle"))
    body.append(text(x0, 18, "brightness ↑   time →", 12, MUTED))
    return svg(W, y + 18, body, "The pad's rhythms")


FIGURES = {
    "pad-map.svg": fig_pad_map,
    "bar-levels.svg": fig_bar_levels,
    "battery.svg": fig_battery,
    "npu-ghost.svg": fig_npu,
    "dgpu-sleep.svg": fig_dgpu_sleep,
    "temperature.svg": fig_temperature,
    "rhythms.svg": fig_rhythms,
    "warning.svg": fig_warning,
    "rainbow.svg": fig_rainbow,
}


def main():
    for name, fn in FIGURES.items():
        with open(os.path.join(HERE, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(fn())
        print(name)


if __name__ == "__main__":
    main()
