#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tunables INI for the sysmon host.

The v2 protocol refactor deliberately moved *all* monitoring policy out of the
firmware and into module-level constants here on the host, so thresholds,
colours and cadences could be retuned by editing a file and restarting -- no
recompile, no reflash. Freezing the host into an executable would have taken
that straight back: the constants would be baked into the bundle.

This module gives them back. It mirrors every tunable constant into a commented
INI, and on start-up applies the file by *rebinding the module globals* before
any row builder runs.

Rebinding works because of how the engine is written: every consumer reads its
constants inside a function, at call time (`build_temp_row` reads TEMP_MIN_C
when it runs, not when it is defined), so replacing the module attribute is
enough and none of the ~35 use sites need to change. The only exceptions were
the handful of constants used as *default arguments*, which bind at import;
those were changed to None sentinels resolved in the function body.

Precedence: an explicit command-line flag beats the INI, which beats the
built-in default. Bad values are reported and ignored rather than fatal -- a
typo in a config file must never be the reason a boot-start service fails to
come up.
"""

import configparser
import math
import os

import paths
from applog import log

# kind: how the raw string is parsed and what a legal value looks like.
#   int   / float : optional lo/hi bounds, inclusive
#   csv           : comma-separated list, validated against `choices`; when
#                   "none" is one of them, it stands for an empty list
#   text          : text for the macropad's font (TEXT_CHARS), up to hi chars
INT, FLOAT, CSV, TEXT = "int", "float", "csv", "text"
TEXT_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 -!.")


class Setting:
    __slots__ = ("name", "section", "kind", "lo", "hi", "choices", "comment")

    def __init__(self, name, section, kind, comment, lo=None, hi=None,
                 choices=None):
        self.name = name
        self.section = section
        self.kind = kind
        self.comment = comment
        self.lo = lo
        self.hi = hi
        self.choices = choices


# Ordered: this is also the order the generated file is written in.
SCHEMA = [
    Setting("GLIDE_MS_DEFAULT", "globals", INT, lo=0, hi=1020,
            comment="Per-row bar glide, milliseconds. The firmware eases each\n"
                    "row towards its new level over this long, so the bars stay\n"
                    "smooth between host updates. Sent in 4 ms units, so the\n"
                    "usable maximum is 1020."),
    Setting("TIMEOUT_MS_DEFAULT", "globals", INT, lo=100, hi=25500,
            comment="How long the firmware waits with no frames before giving\n"
                    "up and restoring the stock RGB effect. Covers service\n"
                    "stop, reboot and crashes alike. Sent in 100 ms units."),

    Setting("PERIOD_BLINK", "cadence", INT, lo=1, hi=255,
            comment="Critical blink period (memory >= MEM_CRITICAL_PCT,\n"
                    "temperature > TEMP_CRITICAL_C, battery below\n"
                    "BATT_CRITICAL_PCT). 32 ms units, so 32 = ~1 Hz."),
    Setting("PERIOD_BREATHE_FAST", "cadence", INT, lo=1, hi=255,
            comment="Brisk breathe: a limit (LIMIT_KINDS) holds the CPU or\n"
                    "dGPU back. 32 ms units, so 48 = ~1.5 s."),
    Setting("PERIOD_BREATHE_SLOW", "cadence", INT, lo=1, hi=255,
            comment="Slow breathe: the power *source* is the bottleneck (the\n"
                    "battery is assisting an overloaded charger), with 'charger'\n"
                    "in LIMIT_KINDS. 128 = ~4 s."),
    Setting("PERIOD_PULSE", "cadence", INT, lo=1, hi=255,
            comment="Battery charge/discharge trapezoid pulse. 128 = ~4 s."),
    Setting("PERIOD_SINE", "cadence", INT, lo=1, hi=255,
            comment="The faint AC cap-drift 'tell' sine on the battery row."),

    Setting("HUE_RED", "colours", INT, lo=0, hi=255,
            comment="QMK hue for the CPU and iGPU bars, and a discharging\n"
                    "battery on its own power."),
    Setting("HUE_AMBER", "colours", INT, lo=0, hi=255,
            comment="Battery discharging *while on AC* -- the charger is\n"
                    "overloaded and the battery is making up the difference."),
    Setting("HUE_GOLD", "colours", INT, lo=0, hi=255,
            comment="Memory bar."),
    Setting("HUE_GREEN", "colours", INT, lo=0, hi=255,
            comment="Full battery and the dGPU bar."),
    Setting("HUE_TEMP_LO", "colours", INT, lo=0, hi=255,
            comment="Temperature gradient, cool end (85 = green)."),
    Setting("HUE_TEMP_HI", "colours", INT, lo=0, hi=255,
            comment="Temperature gradient, hot end (0 = red)."),
    Setting("HUE_CHARGE_FULL", "colours", INT, lo=0, hi=255,
            comment="Charging hue at 100%; the bar interpolates red -> this."),
    Setting("HUE_NPU", "colours", INT, lo=0, hi=255,
            comment="NPU ghost bar on the iGPU row (128 = cyan). Pick a hue\n"
                    "well away from HUE_RED: a segment the two bars share mixes\n"
                    "both colors."),
    Setting("HUE_DGPU_OFF", "colours", INT, lo=0, hi=255,
            comment="The dGPU row's keys from the right while the dGPU is\n"
                    "powered off (D3cold): 140 = light blue/teal, kept off\n"
                    "HUE_NPU (the row above). Always fully saturated (the marker\n"
                    "is a ghost bar, which has no saturation of its own)."),

    Setting("ROW_VAL_FULL", "levels", INT, lo=1, hi=255,
            comment="Base brightness of a normal row, before the global\n"
                    "screen-brightness scaling."),
    Setting("ROW_VAL_BATT_IDLE", "levels", INT, lo=1, hi=255,
            comment="Dim green shown when on AC and the battery is idle (held\n"
                    "at its charge cap)."),
    Setting("ROW_VAL_NPU_GHOST", "levels", INT, lo=1, hi=255,
            comment="Brightness of the NPU ghost bar behind the iGPU bar. Kept\n"
                    "dim so the bright iGPU bar reads first; the ghost only\n"
                    "shows where the NPU is busier than the iGPU. Needs the\n"
                    "ghost-bar firmware (FRAME C); older firmware ignores it."),
    Setting("ROW_VAL_DGPU_OFF", "levels", INT, lo=1, hi=255,
            comment="Brightness of the dGPU row's keys from the right while the\n"
                    "dGPU is powered off (D3cold): steady and dim, like the idle\n"
                    "battery row. Needs firmware that reads the ghost flags;\n"
                    "older firmware shows no marker."),
    Setting("BATT_DRIFT_FLOOR", "levels", INT, lo=0, hi=255,
            comment="Trough of the faint cap-drift breathe; the peak is\n"
                    "ROW_VAL_BATT_IDLE. Keep the gap small -- this is meant to\n"
                    "be a barely-there tell, not an alert."),
    Setting("LIMIT_FLOOR", "levels", INT, lo=0, hi=255,
            comment="Trough of the power-limit breathe. Deliberately not 0:\n"
                    "dipping fully off is the critical-blink language, so a\n"
                    "power-limited row must stay readable throughout."),

    Setting("BATT_CRITICAL_PCT", "thresholds", INT, lo=0, hi=100,
            comment="Raw battery % below which a *discharging* battery blinks."),
    Setting("ASSIST_MIN_W", "thresholds", FLOAT, lo=0, hi=255,
            comment="Battery-assist drain (watts, on AC) that counts as a real\n"
                    "power deficit. Below this, a trickle out of the battery on\n"
                    "AC is just EC charge-cap housekeeping and is shown as idle."),
    Setting("DRIFT_MIN_W", "thresholds", FLOAT, lo=0, hi=255,
            comment="Smallest drain (watts, on AC) that counts as real cap\n"
                    "drift and earns the faint boundary-segment tell. The EC\n"
                    "leaves the ACPI discharging bit asserted at a flat 0 W\n"
                    "while it floats a pack it is done charging (what you get\n"
                    "with the charge cap at 100%), so a bit with no measured\n"
                    "flow behind it is idle, not drift."),
    Setting("MEM_CRITICAL_PCT", "thresholds", INT, lo=0, hi=100,
            comment="Memory usage % at or above which the whole row blinks."),
    Setting("TEMP_CRITICAL_C", "thresholds", FLOAT, lo=0, hi=150,
            comment="Temperature above which the whole row blinks. This is the\n"
                    "temperature row's own alert; a CPU or dGPU slowed down by a\n"
                    "thermal limit breathes its row instead (LIMIT_KINDS)."),
    Setting("TEMP_MIN_C", "thresholds", FLOAT, lo=0, hi=150,
            comment="Temperature mapped to an empty bar. Must be below\n"
                    "TEMP_MAX_C (if not, both keep their defaults)."),
    Setting("TEMP_MAX_C", "thresholds", FLOAT, lo=0, hi=150,
            comment="Temperature mapped to a full bar."),
    Setting("DGPU_OFF_STEP_S", "thresholds", FLOAT, lo=1, hi=86400,
            comment="The dGPU sleep marker grows from the right: its last key\n"
                    "lights as the dGPU powers off (D3cold), and one more after\n"
                    "each further this many seconds it stays off, up to all 4\n"
                    "(900 = every 15 minutes). Any wake starts it over."),

    Setting("RATE_SEG_W1", "battery", FLOAT, lo=0, hi=255,
            comment="Charge/discharge watts above which 2 segments animate."),
    Setting("RATE_SEG_W2", "battery", FLOAT, lo=0, hi=255,
            comment="... 3 segments."),
    Setting("RATE_SEG_W3", "battery", FLOAT, lo=0, hi=255,
            comment="... all 4 segments. Below RATE_SEG_W1 a single segment\n"
                    "animates, so a trickle still reads as movement."),

    Setting("LIMIT_KINDS", "power_limits", CSV,
            choices=("power", "current", "thermal", "clock", "charger", "none"),
            comment="Which kinds of limit make the CPU or dGPU row breathe\n"
                    "(MSG_KINDS says which of them the pad also names):\n"
                    "  power   - APU STAPM/PPT (APU_LIMIT_SIGNALS), dGPU power cap\n"
                    "            and power brake; on AC only (on battery the\n"
                    "            limits drop by design; the battery row says so)\n"
                    "  current - APU TDC/EDC, core and SoC\n"
                    "  thermal - APU Tctl at its THM limit, dGPU thermal slowdown\n"
                    "  clock   - the CPU held at its base clock under load (boost\n"
                    "            off) with no SMU limit near; on AC only\n"
                    "  charger - a component held back while the battery assists\n"
                    "            an overloaded charger: breathes slowly\n"
                    "            (PERIOD_BREATHE_SLOW), next to the battery row's\n"
                    "            amber; leave it out to breathe for the limit the\n"
                    "            EC lowered instead\n"
                    "  none    - no limit indication at all"),
    Setting("APU_LIMIT_NEAR", "power_limits", FLOAT, lo=0.1, hi=1.0,
            comment="How close to an SMU power or current limit counts as\n"
                    "*binding*: the live value must reach this fraction of the\n"
                    "limit. The value sits right at the limit when the chip is\n"
                    "genuinely capped."),
    Setting("APU_LIMIT_SIGNALS", "power_limits", CSV,
            choices=("stapm", "fast", "slow"),
            comment="Which SMU *power* limits count (with power in\n"
                    "LIMIT_KINDS). stapm (the skin-temperature-aware sustained\n"
                    "budget HWiNFO shows as 'APU STAPM Limit') and slow are\n"
                    "sustained, so they give a steady signal. fast is the short\n"
                    "boost window: adding it makes the row flicker under bursty\n"
                    "load."),
    Setting("APU_THM_MARGIN_C", "power_limits", FLOAT, lo=0, hi=50,
            comment="Tctl within this many degrees of the APU's THM limit (100 C\n"
                    "on the HX 370) counts as a binding thermal limit."),
    Setting("LIMIT_RELEASE_S", "power_limits", FLOAT, lo=0, hi=600,
            comment="Seconds a limit must stay clear before it stops counting.\n"
                    "A capped value hovers around the threshold (STAPM crossed it\n"
                    "every 1-3 s under an all-core load), which flickered the\n"
                    "breathe on and off."),
    Setting("CLOCK_BUSY_PCT", "power_limits", FLOAT, lo=1, hi=100,
            comment="The clock limit (clock in LIMIT_KINDS): a core whose two\n"
                    "threads together are this busy (%) wants to boost..."),
    Setting("CLOCK_BASE_MARGIN", "power_limits", FLOAT, lo=0.5, hi=2.0,
            comment="... so the CPU counts as held at its base clock when no busy\n"
                    "logical CPU runs faster than this times base (Windows'\n"
                    "Processor Performance counter; a boosting core reads 1.5-2.5x)."),
    Setting("CLOCK_DEBOUNCE_READS", "power_limits", INT, lo=1, hi=100,
            comment="Consecutive reads that must find the CPU held at base before\n"
                    "it counts (one read per --interval)."),
    Setting("CPU_LOAD_HIGH", "power_limits", FLOAT, lo=0, hi=100,
            comment="Fallback heuristic only (no ryzenadj): CPU load % above\n"
                    "which APU power limits are even evaluated."),
    Setting("CPU_POWER_FLOOR_W", "power_limits", FLOAT, lo=0, hi=255,
            comment="Fallback heuristic only: ignore power-cap inference below\n"
                    "this package power."),
    Setting("CEIL_NEAR", "power_limits", FLOAT, lo=0.1, hi=1.0,
            comment="Fallback heuristic only: fraction of the observed ceiling\n"
                    "that reads as 'at it'."),
    Setting("NV_ACTIVE_MIN_W", "power_limits", FLOAT, lo=0, hi=1000,
            comment="dGPU draw above which the card counts as actively loaded."),
    Setting("NV_NEAR_CAP", "power_limits", FLOAT, lo=0.1, hi=1.0,
            comment="dGPU draw as a fraction of its enforced limit before\n"
                    "NVIDIA's SwPowerCap flag is believed. NVIDIA sets that flag\n"
                    "eagerly -- even near idle -- and without this filter the\n"
                    "dGPU row breathes on and off every few seconds."),
    Setting("NV_DEBOUNCE_READS", "power_limits", INT, lo=1, hi=100,
            comment="Consecutive reads a dGPU limit flag must hold before it\n"
                    "counts, so one NVML blip cannot flash the breathe."),

    Setting("MSG_ENABLE", "messages", INT, lo=0, hi=1,
            comment="1: when a limit holds the CPU or dGPU back, scroll which one\n"
                    "across the pad, e.g. 'CPU STAPM 28W'. 0: only on demand --\n"
                    "keypad 5 with NumLock off always says what binds right now\n"
                    "(or NO LIMIT). Needs the firmware from this version."),
    Setting("MSG_KINDS", "messages", CSV,
            choices=("power", "current", "thermal", "clock", "charger", "none"),
            comment="Which kinds of limit (see LIMIT_KINDS) the pad names on its\n"
                    "own. The others only breathe: the slow charger breathe next\n"
                    "to the amber battery row says enough by itself. The replay\n"
                    "key names whatever makes a row breathe."),
    Setting("MSG_ONSET_S", "messages", FLOAT, lo=0, hi=600,
            comment="How long a limit must have held before it is named."),
    Setting("MSG_MIN_GAP_S", "messages", FLOAT, lo=0, hi=3600,
            comment="Shortest time between two messages about one component\n"
                    "(when the limit holding it back changes)."),
    Setting("MSG_REARM_S", "messages", FLOAT, lo=0, hi=36000,
            comment="A limit is named once per episode; an episode ends when the\n"
                    "component has been free of limits this many seconds."),
    Setting("MSG_SPEED", "messages", INT, lo=2, hi=40,
            comment="Ticker speed, columns per second (a letter is about 5)."),
    Setting("MSG_SHOW_VALUE", "messages", INT, lo=0, hi=1,
            comment="1: end with the limit's value right now ('28W'; for a dGPU\n"
                    "thermal slowdown its temperature). 0: just the names."),
    Setting("MSG_NAME_CPU", "messages", TEXT, hi=8,
            comment="The words on the pad. A-Z, 0-9, space, - ! . only; uppercase\n"
                    "W and M are the tall letter forms, w and m the short ones.\n"
                    "The CPU row stands for the whole APU."),
    Setting("MSG_NAME_GPU", "messages", TEXT, hi=8, comment="The dGPU."),
    Setting("MSG_NO_LIMIT", "messages", TEXT, hi=23,
            comment="The replay key's answer when nothing binds."),
    Setting("MSG_WORD_STAPM", "messages", TEXT, hi=12,
            comment="APU sustained power budget (skin temperature)."),
    Setting("MSG_WORD_SLOW", "messages", TEXT, hi=12, comment="APU slow PPT."),
    Setting("MSG_WORD_FAST", "messages", TEXT, hi=12,
            comment="APU fast PPT (only with fast in APU_LIMIT_SIGNALS)."),
    Setting("MSG_WORD_TDC", "messages", TEXT, hi=12,
            comment="APU core current, sustained."),
    Setting("MSG_WORD_TDC_SOC", "messages", TEXT, hi=12,
            comment="APU SoC current, sustained."),
    Setting("MSG_WORD_EDC", "messages", TEXT, hi=12,
            comment="APU core current, peak."),
    Setting("MSG_WORD_EDC_SOC", "messages", TEXT, hi=12,
            comment="APU SoC current, peak."),
    Setting("MSG_WORD_THM", "messages", TEXT, hi=12,
            comment="APU at its temperature limit."),
    Setting("MSG_WORD_POWER", "messages", TEXT, hi=12,
            comment="dGPU power cap (and the APU's, when only the package-power\n"
                    "heuristic is available)."),
    Setting("MSG_WORD_BRAKE", "messages", TEXT, hi=12,
            comment="dGPU hardware power brake."),
    Setting("MSG_WORD_THERMAL", "messages", TEXT, hi=12,
            comment="dGPU thermal slowdown."),
    Setting("MSG_WORD_CLOCK", "messages", TEXT, hi=12,
            comment="CPU held at its base clock (boost off), e.g.\n"
                    "'CPU CLK LIMITED 2GHZ'."),
    Setting("MSG_WORD_CHARGER", "messages", TEXT, hi=12,
            comment="Held back by an overloaded charger (the replay key's word for\n"
                    "the slow breathe; named on its own only with charger in\n"
                    "MSG_KINDS)."),

    Setting("TTL_BRIGHTNESS", "polling", FLOAT, lo=0, hi=3600,
            comment="Screen-brightness cache lifetime, seconds. Keep this short:\n"
                    "a longer TTL makes the macropad visibly lag a brightness\n"
                    "change. 0 re-reads every tick."),
    Setting("TTL_BATTERY", "polling", FLOAT, lo=0, hi=3600,
            comment="Battery charge/discharge state cache lifetime."),
    Setting("TTL_RYZENADJ", "polling", FLOAT, lo=0, hi=3600,
            comment="One ryzenadj SMU refresh feeds both the APU limit verdict\n"
                    "and the Tctl temperature."),
    Setting("TTL_TEMP", "polling", FLOAT, lo=0, hi=3600,
            comment="ACPI thermal-zone cache lifetime: the last-resort\n"
                    "temperature source, a WMI query, used only when nothing\n"
                    "better reads."),
    Setting("TTL_LHM_SNAPSHOT", "polling", FLOAT, lo=0, hi=3600,
            comment="One LibreHardwareMonitor walk feeds temperature, package\n"
                    "power and CPU load together."),
    Setting("TTL_EC_THERMAL", "polling", FLOAT, lo=0, hi=3600,
            comment="framework_tool --thermal fallback. Each read spawns a\n"
                    "process, so do not lower this much."),
]

SECTION_HELP = {
    "globals": "Frame globals sent to the firmware every tick.",
    "cadence": "Animation periods, in units of 32 ms (the firmware's tick).",
    "colours": "QMK hues, 0-255: 0 red, 36 gold, 85 green, 170 blue.",
    "levels": "Row brightness levels and animation floors, 0-255.",
    "thresholds": "When a row switches into its alert state.",
    "battery": "Charge/discharge power -> how many bar segments animate.",
    "power_limits": "Limit detection: when the CPU and dGPU rows breathe.",
    "messages": "Limit messages: the pad names the limit a row breathes for.",
    "polling": "Per-source cache lifetimes. Most ticks reuse cached readings so\n"
               "the poll loop stays cheap; raise these to cut CPU use further,\n"
               "lower them for a more responsive display.",
    "cli": "Defaults for the command-line options. An explicitly passed flag\n"
           "always wins over the value here. Run with --help for the full list.",
}

# CLI options intentionally excluded from the [cli] section: one-shot actions and
# packaging plumbing, none of which make sense as a persistent default.
_CLI_SKIP = {
    "fetch_lhm", "force", "bootloader", "config", "write_default_config",
    "selftest", "json", "flash", "service", "install_service",
    "uninstall_service", "start_service", "stop_service", "restart_service",
    "help", "version",
}


# Pairs that only make sense in order, (low, high). Each value is range-checked
# on its own; a pair that ends up crossed or equal (which, for the temperature
# range, would divide by zero on every tick) reverts to its defaults as a whole.
ORDERED_PAIRS = (("TEMP_MIN_C", "TEMP_MAX_C"),)


_BUILTIN = {}  # ORDERED_PAIRS names -> built-in value, from before any INI


def builtin_value(name):
    """A pair member's built-in value (not whatever an INI made it); recorded
    by apply(), which runs at start-up before anything asks."""
    return _BUILTIN.get(name)


def crossed_pairs(get):
    """The ORDERED_PAIRS that `get(name)` -> value leaves crossed or equal, as
    (lo_name, hi_name, lo, hi). One rule for config.apply (which reverts them)
    and --selftest (which reports them); a value get returns None for is
    skipped."""
    out = []
    for lo_name, hi_name in ORDERED_PAIRS:
        lo, hi = get(lo_name), get(hi_name)
        if lo is not None and hi is not None and not lo < hi:  # nan too
            out.append((lo_name, hi_name, lo, hi))
    return out


def _by_name():
    return {s.name: s for s in SCHEMA}


def _parse(setting, raw):
    """Parse and range-check one raw string. Raises ValueError when invalid."""
    raw = raw.strip()
    if setting.kind == TEXT:
        if not raw:
            raise ValueError("empty text")
        bad = sorted(set(raw) - TEXT_CHARS)
        if bad:
            raise ValueError(f"the macropad can't show {''.join(bad)!r}; "
                             f"use A-Z, 0-9, space, - ! .")
        if len(raw) > setting.hi:
            raise ValueError(f"longer than {setting.hi} characters")
        return raw
    if setting.kind == CSV:
        items = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
        if not items:
            raise ValueError("empty list")
        bad = [i for i in items if i not in setting.choices]
        if bad:
            raise ValueError(f"unknown value(s) {', '.join(bad)}; "
                             f"allowed: {', '.join(setting.choices)}")
        if "none" in items:
            if len(items) > 1:
                raise ValueError("'none' can't be combined with other values")
            return ()
        return items
    value = int(raw, 0) if setting.kind == INT else float(raw)
    if setting.kind == FLOAT and not math.isfinite(value):
        # nan and inf parse as floats, and every range check is False for
        # nan: one would reach the row builders and fail on every tick
        raise ValueError(f"{raw!r} is not a finite number")
    if setting.lo is not None and value < setting.lo:
        raise ValueError(f"{value} is below the minimum {setting.lo}")
    if setting.hi is not None and value > setting.hi:
        raise ValueError(f"{value} is above the maximum {setting.hi}")
    return value


def _read(path):
    # interpolation=None: the [cli] section carries regexes (--igpu-match and
    # friends), and configparser's default interpolation would choke on a '%'
    # inside one. No value here ever wants to reference another.
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keep CONSTANT_NAME casing
    # utf-8-sig, not utf-8: this file is meant to be hand-edited, and the Start
    # menu opens it in Notepad. Anything that saves "UTF-8 with BOM" -- Notepad,
    # most Windows editors, PowerShell's Set-Content -Encoding utf8 -- prepends
    # U+FEFF, which plain utf-8 hands to configparser as part of the first line.
    # It then fails with "File contains no section headers" and the WHOLE file
    # is discarded in favour of built-in defaults: the user's edits vanish with
    # only a line in the log to show for it. utf-8-sig strips a BOM when present
    # and is identical to utf-8 when it is not.
    parser.read(path, encoding="utf-8-sig")
    return parser


def default_path(explicit=None):
    return explicit or paths.config_path()


def apply(arg_parser, argv, module):
    """Load the INI and apply it. Returns the path used, or None if absent.

    `module` is the engine module object rather than a name because when
    fw16_sysmon.py is run directly it is registered as __main__, and importing
    it by name here would create a *second* copy of the module whose constants
    nobody reads.
    """
    for pair in ORDERED_PAIRS:
        for n in pair:
            _BUILTIN.setdefault(n, getattr(module, n))  # the first call sees the built-ins
    path = default_path(_peek_config_arg(argv))
    if not path or not os.path.isfile(path):
        return None

    try:
        ini = _read(path)
    except (configparser.Error, OSError) as err:
        log(f"Config {path} could not be read ({err}); using built-in defaults")
        return None

    known = _by_name()
    cli_defaults = {}
    cli_dests = {a.dest for a in arg_parser._actions} - _CLI_SKIP

    for section in ini.sections():
        for key, raw in ini.items(section):
            if section == "cli":
                dest = key.replace("-", "_")
                if dest not in cli_dests:
                    log(f"Config {path} [cli]: unknown option '{key}' ignored")
                    continue
                cli_defaults[dest] = raw.strip()
                continue
            setting = known.get(key)
            if setting is None:
                log(f"Config {path} [{section}]: unknown setting '{key}' ignored")
                continue
            try:
                setattr(module, setting.name, _parse(setting, raw))
            except ValueError as err:
                log(f"Config {path} [{section}] {key}: {err}; "
                    f"keeping default {getattr(module, setting.name)!r}")

    for lo_name, hi_name, lo, hi in crossed_pairs(lambda n: getattr(module, n)):
        log(f"Config {path}: {lo_name} ({lo}) must be below {hi_name} "
            f"({hi}); keeping the defaults {_BUILTIN[lo_name]!r} and "
            f"{_BUILTIN[hi_name]!r}")
        setattr(module, lo_name, _BUILTIN[lo_name])
        setattr(module, hi_name, _BUILTIN[hi_name])

    if cli_defaults:
        _apply_cli_defaults(arg_parser, cli_defaults, path)
    return path


def _peek_config_arg(argv):
    """Find --config before the real parse, since it selects the defaults."""
    import sys as _sys
    items = list(argv if argv is not None else _sys.argv[1:])
    for i, item in enumerate(items):
        if item == "--config" and i + 1 < len(items):
            return items[i + 1]
        if item.startswith("--config="):
            return item.split("=", 1)[1]
    return None


def _apply_cli_defaults(arg_parser, values, path):
    """Push [cli] values in as argparse defaults, coerced to each option's type.

    Going through set_defaults (rather than parsing the value ourselves) is what
    keeps an explicit command-line flag winning: argparse only falls back to a
    default when the flag is absent.
    """
    actions = {a.dest: a for a in arg_parser._actions}
    coerced = {}
    for dest, raw in values.items():
        action = actions[dest]
        try:
            if isinstance(action.const, bool) or action.nargs == 0:
                coerced[dest] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif action.type is not None:
                coerced[dest] = action.type(raw)
            else:
                coerced[dest] = raw
        except (TypeError, ValueError) as err:
            log(f"Config {path} [cli] {dest}: {err}; keeping default")
    arg_parser.set_defaults(**coerced)


def write_default(explicit=None, module=None):
    """Write the commented default INI. Never overwrites an existing file."""
    path = default_path(explicit)
    if os.path.isfile(path):
        log(f"Config already exists at {path}; leaving it alone")
        return path
    paths.ensure_data_dir()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_default(module))
    return path


def render_default(module=None):
    """Render the default INI as text (also used to ship a reference copy)."""
    if module is None:
        import sys as _sys
        module = _sys.modules.get("fw16_sysmon") or _sys.modules["__main__"]

    out = [
        "; sysmon tunables -- Framework 16 RGB Macropad system monitor",
        ";",
        "; Every value below is a live tuning knob: edit, then restart the",
        "; service (`sc stop FW16Sysmon` / `sc start FW16Sysmon`, or the Start",
        "; menu shortcut) to apply. No rebuild and no reflash -- the firmware is",
        "; a generic render engine and holds none of this policy.",
        ";",
        "; Delete any line to fall back to its built-in default. A malformed",
        "; value is reported in sysmon.log and ignored, never fatal.",
        ";",
        "; Save as UTF-8. A leading byte-order mark is tolerated, so Notepad's",
        "; 'UTF-8 with BOM' is fine; UTF-16 is not. Run --selftest to see",
        "; whether this file is being read and how many settings it applied.",
        "",
    ]
    current = None
    for setting in SCHEMA:
        if setting.section != current:
            current = setting.section
            out.append(f"[{current}]")
            for line in SECTION_HELP.get(current, "").split("\n"):
                if line:
                    out.append(f"; {line}")
            out.append("")
        for line in setting.comment.split("\n"):
            out.append(f"; {line}")
        value = getattr(module, setting.name)
        if setting.kind == CSV:
            value = ", ".join(value) or "none"
        out.append(f"{setting.name} = {value}")
        out.append("")

    out.append("[cli]")
    for line in SECTION_HELP["cli"].split("\n"):
        out.append(f"; {line}")
    out.append("")
    for line in _cli_defaults_block(module):
        out.append(line)
    return "\n".join(out) + "\n"


def _cli_defaults_block(module):
    """Render the [cli] section from the parser's own defaults and help text."""
    lines = []
    parser = module.build_parser()
    for action in parser._actions:
        if action.dest in _CLI_SKIP or not action.option_strings:
            continue
        help_text = (action.help or "").replace("%%", "%")
        for chunk in _wrap(help_text, 68):
            lines.append(f"; {chunk}")
        default = action.default
        if isinstance(default, bool):
            default = "true" if default else "false"
        elif default is None:
            # e.g. --cpu-power-cap: no default, so ship it commented out rather
            # than inventing a value that would silently pin the auto-calibration
            lines.append(f"; {action.dest.replace('_', '-')} =")
            lines.append("")
            continue
        lines.append(f"{action.dest.replace('_', '-')} = {default}")
        lines.append("")
    return lines


def _wrap(text, width):
    import textwrap
    return textwrap.wrap(text, width) or [""]
