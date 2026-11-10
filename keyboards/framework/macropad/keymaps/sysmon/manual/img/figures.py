#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Draw the user manual's still figures (the animated ones are animations.py's),
and the drawing helpers both share.

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

def fig_bar_levels():
    items = [(f"{p} %", sub, bar(p, RED), True) for p, sub in (
        (0, "nothing lit"),
        (10, "the first key, partly lit"),
        (25, "one key"),
        (60, "two keys, the third partly lit"),
        (100, "all four keys"))]
    return strips("Reading a bar", items, label_w=270)


FIGURES = {
    # The rest are animated, rendered by the firmware itself: animations.py.
    "bar-levels.svg": fig_bar_levels,
}


def main():
    for name, fn in FIGURES.items():
        with open(os.path.join(HERE, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(fn())
        print(name)


if __name__ == "__main__":
    main()
