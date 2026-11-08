#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Stream system stats to a Framework 16 RGB Macropad running the `sysmon` keymap.

Protocol v2: the host is the producer of *all* monitoring policy. The firmware
is a generic render engine that draws six 4-segment bars from per-row 8-byte
descriptors plus a globals block. Every threshold, color, cadence and mapping
that used to live in the firmware now lives here as a tunable module-level
constant. LED brightness is derived from the laptop screen brightness so the
macropad dims with the screen.

Each tick sends THREE 32-byte VIA "custom set value" raw-HID reports on a
private VIA channel (0x42):
    value_id 0x01 = FRAME A: rows 0,1,2
    value_id 0x02 = FRAME B: rows 3,4,5 + globals
    value_id 0x03 = FRAME C: optional per-row ghost bars (the NPU on the iGPU
                    row, the dGPU sleep marker); older firmware ignores it

Dependencies (Windows):
    pip install hidapi psutil wmi
    optional:
      pynvml    -- NVIDIA dGPU utilization (preferred over the PDH 3D-engine
                   counter: matches nvidia-smi, counts compute/encode/copy
                   engines too) + power/throttle reasons (authoritative)
      ryzenadj  -- AMD APU STAPM/PPT power limits + Tctl read straight from the
                   SMU power-metrics table via its libryzenadj.dll (the same
                   source HWiNFO reads for "APU STAPM Limit"). Install the
                   `ryzenadj` tool (e.g. `scoop install ryzenadj`); the DLL is
                   auto-discovered, or point --ryzenadj-dir at it.
      pythonnet -- in-process LibreHardwareMonitorLib for AMD APU package power
                   + temperature (run --fetch-lhm once)

APU limit detection (CPU-row breathe, and the message naming the limit):
    1. ryzenadj / libryzenadj SMU table: STAPM/PPT/TDC/EDC/THM value-vs-limit
       (authoritative)
    2. LibreHardwareMonitor package-power plateau heuristic (fallback)
    3. Windows' per-CPU clock counters (CpuClock): held at the base clock under
       load while neither of the above sees a limit -- boost switched off
The dGPU's come from NVML's throttle reasons. See LIMIT_KINDS, LimitTracker.

Limit log (limitlog.py, LIMITLOG_*): the loop also hands every tick's readings
to a recorder that keeps a long-term SQLite record of each limit that binds,
raw and unfiltered, with the context it bound in -- pad or no pad. Tallied by
--limit-report (limitreport.py).

Temperature sources, tried in order (gather(); --selftest's temperature probe
walks the same chain):
    1. LibreHardwareMonitor: the in-process LibreHardwareMonitorLib via
       pythonnet (run --fetch-lhm), else its WMI provider (only while the LHM
       app is running)
    2. Framework EC via framework_tool --thermal (throttled, TTL_EC_THERMAL)
    3. ryzenadj Tctl (in-process; free once libryzenadj is loaded for limits).
       Deliberately after the EC: a wrong SMU-table decode returns a plausible
       number instead of failing (see the note in gather())
    4. ACPI thermal zone (MSAcpi_ThermalZoneTemperature, cached TTL_TEMP;
       static on the FW16, so only a last resort)

The ring0 sources (ryzenadj, LibreHardwareMonitorLib) need the script elevated.

Installed, it runs as the FW16Sysmon boot-start service (service.py, set up by
the installer or --install-service). Run it in an elevated console to try it
out; stop the service first, as the macropad has a single raw-HID endpoint.
"""

import argparse
import os
import re
import subprocess
import sys
import threading
import time
import winreg

import hid  # hidapi package
import psutil

import paths
from applog import log, pause_if_own_console

__version__ = "1.0.0"

try:
    import wmi as wmi_module
except ImportError:
    wmi_module = None

try:
    import win32pdh
except ImportError:
    win32pdh = None

try:
    import pynvml  # nvidia-ml-py / pynvml; NVIDIA dGPU power & throttle reasons
except ImportError:
    pynvml = None

VID = 0x32AC
PID_MACROPAD = 0x0013
RAW_USAGE_PAGE = 0xFF60  # QMK raw HID (VIA) interface
RAW_USAGE = 0x61
REPORT_LEN = 32

# ---------------------------------------------------------------------------
# FROZEN WIRE PROTOCOL v2 (sysmon) — both sides MUST match byte-for-byte.
#
# Transport: VIA "custom set value" raw-HID report on private channel 0x42.
# Each report is 32 bytes. Host writes b"\x00" + 32-byte report (the leading
# 0x00 is the HID report id; the firmware never sees it).
#
# Report header (every report):
#   data[0] = 0x07   (VIA id_custom_set_value)
#   data[1] = 0x42   (SYSMON_CHANNEL)
#   data[2] = value_id
#   data[3..] = payload
#
# Three FRAME reports per tick:
#   value_id 0x01 = FRAME A : rows 0,1,2          (24-byte payload, 5 pad bytes)
#   value_id 0x02 = FRAME B : rows 3,4,5 + globals (24-byte payload + globals)
#   value_id 0x03 = FRAME C : ghost bars (optional; described below)
#
# Per-row descriptor = EXACTLY 8 bytes:
#   +0 level         uint8 0..255 fraction of the 4-segment bar (0=empty,255=full)
#   +1 row_val       uint8 0..255 base row brightness (normally 255)
#   +2 hue0          uint8 0..255 QMK hue: solid color, or gradient start
#   +3 hue1          uint8 0..255 gradient end hue (ignored unless gradient bit)
#   +4 flags         uint8 bit0    color_gradient: 0=solid hue0, 1=hue0->hue1
#                          bits1-2 envelope_type: 0=none,1=blink,2=breathe
#                          bits3-4 overlay_shape: 0=none,1=trap-rise,2=trap-fall,3=sine
#                          bit5    overlay_dir: 0=boundary upward, 1=downward
#                          bits6-7 reserved (0)
#   +5 overlay_count uint8 0=overlay off; 1..4 = segments from the boundary
#   +6 period        uint8 units of 32ms; period of this row's timed effect
#   +7 floor         uint8 0..255 breathe trough / sine amplitude floor
#
# FRAME A payload (data[3..26]) = row0[8] ++ row1[8] ++ row2[8] (24 bytes;
#   data[27..31] unused/zero).
# FRAME B payload (data[3..26]) = row3[8] ++ row4[8] ++ row5[8] (24 bytes),
#   then globals:
#     data[27] = brightness uint8 0..255 (overall; drives GCC via the slew)
#     data[28] = glide      uint8 units of 4ms   (default 300ms => 75)
#     data[29] = timeout    uint8 units of 100ms (default 5000ms => 50)
#
# FRAME C (value_id 0x03, optional) = six 4-byte ghost descriptors, row0..row5
#   (data[3..26]; data[27..31] unused/zero). A ghost is a second, dimmer bar
#   drawn behind the main one, visible only where it reaches past the main fill:
#   +0 level  uint8 0..255 fraction of the 4-segment bar (0 = no ghost)
#   +1 hue    uint8 0..255 QMK hue of the ghost
#   +2 val    uint8 0..255 ghost brightness (the row's envelope scales it too)
#   +3 flags  uint8 bit0 = anchored at the right end (fills leftward from the
#             last key; level 63 = the last key alone), bits1-7 reserved (0)
#   Firmware without ghost support ignores value_id 0x03; firmware with it
#   fades the ghosts out if FRAME C stops arriving within the timeout, and sets
#   data[27] = 0x01 in its FRAME C echo (older firmware echoes it unchanged).
#   Firmware that reads the flags also sets data[28] = 0x01; a right-anchored
#   ghost is only sent once that has been seen (without it, it would show at
#   the left end). --selftest reads features from the BOOT REPLAY capability
#   byte instead, and sends no frames.
#
# MESSAGE (value_id 0x05, sent only when there is something to say): a line
#   of text the firmware scrolls over the bars -- the bars dim out, the text
#   scrolls through on dark, the bars fade back in:
#   data[3] style (0 = ticker), data[4] hue, data[5] saturation (0 = white),
#   data[6] speed in columns/s (0 = firmware default), data[7] sequence
#   number (1..255, 0 = none; reported back in the FRAME B echo),
#   data[8..30] text, NUL-terminated (up to 23 chars). Empty text is a probe:
#   message-capable firmware sets data[31] = 0x01 in the echo (exactly: older
#   hosts compare it for equality), and firmware that reports message outcomes
#   by sequence number also data[9] = 0x02 in the probe's echo.
#   The FRAME B echo's data[31] reports bit0 = a message is waiting or
#   playing, bit1 = the replay key was pressed (answer with a MESSAGE),
#   bit2 = the pad's RGB lighting is switched off (nothing sent shows),
#   bit4 = the pad is suspended (lid closed): nothing shows. data[30] is the
#   sequence number (MESSAGE data[7]) of the last message played to its end,
#   data[29] that of the last one dropped unplayed (0 = none).
#
# BOOT REPLAY (value_id 0x10) with data[3] = 0xFD is a status query that
# changes nothing; its echo carries data[23] = capability bits (bit7 set on
# firmware that has them) and data[24] = 1 while the RGB lighting is on.
# ---------------------------------------------------------------------------

VIA_CMD_CUSTOM_SET_VALUE = 0x07
SYSMON_CHANNEL = 0x42
SYSMON_VALUE_FRAME_A = 0x01  # rows 0,1,2
SYSMON_VALUE_FRAME_B = 0x02  # rows 3,4,5 + globals
SYSMON_VALUE_FRAME_C = 0x03  # ghost bars, rows 0..5
SYSMON_VALUE_GOODBYE = 0x04  # host stopping on purpose: no disconnect warning
SYSMON_VALUE_MESSAGE = 0x05  # a line of text scrolled over the bars
SYSMON_VALUE_BOOT_REPLAY = 0x10  # boot animation replay / status (not sent by the loop)
BOOT_STATUS_QUERY = 0xFD     # BOOT REPLAY data[3]: report status, change nothing

ROW_DESC_LEN = 8
GHOST_DESC_LEN = 4
GHOST_FLAGS_ACK_OFFSET = 28  # FRAME C echo byte the firmware marks when it reads the ghost flags
GHOST_FLAGS_ACK = 0x01
GHOST_FLAG_RIGHT = 0x01      # ghost flags bit0: anchored at the row's right end
GHOST_LEVEL_KEYS = (0, 63, 127, 191, 255)  # a ghost covering exactly 0..4
                             # segments: n*255//4, as one more (64 for one key)
                             # would spill 1/255 onto the next key, which the
                             # firmware's visibility lift can make glow
MSG_ACK_OFFSET = 31     # MESSAGE echo byte the firmware marks when it can show one
MSG_ACK = 0x01
MSG_SEQ_ACK = 0x02      # the probe's echo at MSG_SEQ_PROBE_OFFSET: FRAME B
MSG_SEQ_PROBE_OFFSET = 9  # reports message outcomes (inside the empty text)
MSG_SEQ_OFFSET = 7      #   MESSAGE data[7]: the sequence number (echoed back)
MSG_TEXT_OFFSET = 8     #   MESSAGE data[8..30]: the text (empty: the probe)
MSG_DROPPED_OFFSET = 29 #   FRAME B echo: the last message dropped unplayed
MSG_PLAYED_OFFSET = 30  #   ...and the last one played to its end (sequence numbers)
STATUS_OFFSET = 31      # FRAME B echo: the firmware's status bits
STATUS_MSG = 0x01       #   a message is waiting or playing
STATUS_ASK = 0x02       #   the replay key was pressed
STATUS_DARK = 0x04      #   the pad's RGB lighting is off (RGB_TOG, or VIA's All Off)
STATUS_SUSPENDED = 0x10 #   the pad is suspended (lid closed): nothing shows
# The BOOT REPLAY status echo (a 0xFD query) says what the firmware can do, so a
# self-test needn't send frames, which would show on the pad.
CAPS_OFFSET = 23        # capability bits; bit7 = the byte is set (older: 0)
CAPS_VALID = 0x80       # (its feature bits: keymap.c SYSMON_CAPS; firmware with
                        # the byte is this version's and sets them all, so the
                        # self-test needs only this one)
LIGHTING_OFFSET = 24    # 1 while the pad's RGB lighting is on (with CAPS_VALID)
MSG_TEXT_MAX = 23
# The pad's RGB toggle is its lights-off switch, and it hides the bars too.
LIGHTING_OFF_NOTE = ("The macropad's RGB lighting is switched off (its RGB "
                     "toggle, or VIA's All Off), so the bars don't show: with "
                     "NumLock off, press its top-left key to switch it back on")

# Fixed row order (matches sysmon_row_leds; do NOT change).
ROW_BATTERY = 0
ROW_CPU = 1
ROW_MEM = 2
ROW_IGPU = 3
ROW_DGPU = 4
ROW_TEMP = 5

# flags byte bit fields ------------------------------------------------------
FLAG_GRADIENT = 1 << 0  # bit0

ENV_NONE = 0
ENV_BLINK = 1
ENV_BREATHE = 2
ENV_SHIFT = 1  # bits1-2

OVL_NONE = 0
OVL_TRAP_RISE = 1
OVL_TRAP_FALL = 2
OVL_SINE = 3
OVL_SHIFT = 3  # bits3-4

OVL_DIR_UP = 0
OVL_DIR_DOWN = 1
OVL_DIR_SHIFT = 5  # bit5

# Globals defaults -----------------------------------------------------------
GLIDE_MS_DEFAULT = 300         # per-row level glide on device
TIMEOUT_MS_DEFAULT = 5000      # no-data fallback to stock RGB effect

# Period encoding reference: every period byte is in units of 32ms, so the
# values below are the pre-encoded byte values (e.g. 32 -> 1024ms full cycle).
PERIOD_BLINK = 32          # ~1Hz full-cycle 1024ms (reproduces timer&512)
PERIOD_BREATHE_FAST = 48   # ~1.5s, power/TDP-cap cadence
PERIOD_BREATHE_SLOW = 128  # ~4s, power-source cadence
PERIOD_PULSE = 128         # charge/discharge trapezoid, 4096ms
PERIOD_SINE = 128          # drift sine tell, 4096ms

# ---------------------------------------------------------------------------
# Render policy constants (moved from firmware keymap.c)
# ---------------------------------------------------------------------------

# QMK hues (0-255)
HUE_RED = 0
HUE_AMBER = 28      # AC battery-assist discharge ("plugged but draining")
HUE_GOLD = 36       # memory
HUE_GREEN = 85      # full battery / dGPU
HUE_TEMP_LO = 85    # temperature gradient start (green, cool)
HUE_TEMP_HI = 0     # temperature gradient end (red, hot)
HUE_CHARGE_FULL = 85  # charging hue at 100% (pct*85/100, red->green)
HUE_NPU = 128       # NPU ghost bar on the iGPU row (cyan)
HUE_DGPU_OFF = 140  # dGPU powered off (D3cold): the dGPU row's keys from the
                    # right (light blue/teal; kept off HUE_NPU, the row above)

# Brightness levels (base row_val)
ROW_VAL_FULL = 255
ROW_VAL_BATT_IDLE = 80   # AC-idle dim green; firmware lifts via visible_pwm
ROW_VAL_NPU_GHOST = 96   # NPU ghost bar: dim, so the bright iGPU bar reads first
ROW_VAL_DGPU_OFF = 80    # the D3cold marker: steady and dim, like the idle battery

# Battery policy
BATT_CRITICAL_PCT = 15   # raw % below which a discharging battery blinks red
ASSIST_MIN_W = 10        # battery-assist drain meaning a real power deficit;
                         # below this on AC is just EC cap-drift housekeeping
DRIFT_MIN_W = 0.1        # smallest AC drain that counts as *any* flow out of
                         # the pack. The EC leaves the ACPI discharging bit
                         # asserted at a flat 0 W while it floats a full pack
                         # (visible with the charge cap at 100%), and that is
                         # idle, not drift -- no tell.

# Battery drift breathe tell (the faint AC cap-drift sine). The firmware's old
# tell breathed 68..92 around the dim-80 fill; encode that as a sine overlay
# whose floor is the trough. Floor maps onto the 0..row_val sine range.
BATT_DRIFT_FLOOR = 68    # trough of the faint drift breathe (peak = row_val 80)

# Memory / temperature critical blink thresholds (gated on RAW value)
MEM_CRITICAL_PCT = 90
TEMP_CRITICAL_C = 90

# Temperature bar scaling: clamp(t,30,90) -> 0..255 level.
TEMP_MIN_C = 30
TEMP_MAX_C = 90

# The D3cold marker grows: the last key lights as the dGPU powers off, and one
# more for each further DGPU_OFF_STEP_S it stays off without a break, up to 4.
DGPU_OFF_STEP_S = 900.0

# Breathe overlay floor for power-limited rows (~43% of 255: dim but readable).
LIMIT_FLOOR = 110

# APU power-limit detection via the SMU table (libryzenadj; authoritative).
APU_LIMIT_NEAR = 0.95   # a STAPM/PPT limit counts as binding when its live
                        # value reaches this fraction of the limit (the value
                        # sits right at the limit when the chip is power-capped)
APU_LIMIT_SIGNALS = ("stapm", "slow")  # which SMU *power* limits count (when
                        # LIMIT_KINDS has "power"). STAPM (skin-temp-aware
                        # sustained budget -- the "APU STAPM Limit" HWiNFO shows)
                        # and the slow PPT are sustained, so they give a steady
                        # signal; the fast PPT is the short boost window and
                        # would flicker, so it is read for diagnostics but not
                        # used to trigger. Add "fast" here to include it.

# Which kinds of limit make the CPU / dGPU row breathe:
#   power   -- APU STAPM / PPT (APU_LIMIT_SIGNALS), dGPU power cap and power
#              brake; on AC only (on battery the limits drop by design and the
#              battery row already says so)
#   current -- APU TDC / EDC, core and SoC
#   thermal -- APU Tctl at its THM limit (APU_THM_MARGIN_C), dGPU thermal
#              slowdown
#   clock   -- the CPU held at its base clock under load (boost off) with no
#              SMU limit near; on AC only (see CpuClock)
#   charger -- a component held back while the battery assists an overloaded
#              charger: breathes slowly (PERIOD_BREATHE_SLOW) -- the cadence
#              itself says "the charger", next to the battery row's amber
LIMIT_KINDS = ("power", "current", "thermal", "clock", "charger")
# ...and which of them the pad also names on its own, in a message. The replay
# key names whatever makes a row breathe, these or not.
MSG_KINDS = ("power", "current", "thermal", "clock")
APU_THM_MARGIN_C = 3.0  # Tctl within this many degrees of the THM limit binds
LIMIT_RELEASE_S = 8.0   # a limit stays "binding" until it has been clear this
                        # long: a capped value hovers around the threshold
                        # (STAPM crossed 95% every 1-3 s under an all-core load
                        # here), which flickered the breathe on and off
CLOCK_BUSY_PCT = 80     # a core (both its threads together) this busy wants to
                        # boost...
CLOCK_BASE_MARGIN = 1.05  # ...so its CPU is held back when no busy logical CPU
                        # runs faster than this times the base clock
CLOCK_DEBOUNCE_READS = 3  # consecutive reads that must agree before it counts

# Limit messages: when a limit (of MSG_KINDS) has held for MSG_ONSET_S, the pad
# scrolls the component, the limit and its current value ("CPU STAPM 28W") over
# the bars, once per episode -- again only when the reason changes (at most
# every MSG_MIN_GAP_S), or after the limit has been clear for MSG_REARM_S.
# Keypad 5 with NumLock off names what makes each row breathe, on demand.
MSG_ENABLE = 1          # 0: no automatic messages (the replay key still works)
MSG_ONSET_S = 3.0
MSG_MIN_GAP_S = 30.0
MSG_REARM_S = 120.0
MSG_SPEED = 8           # ticker speed, columns per second
MSG_SHOW_VALUE = 1      # 0: leave out the limit's value
MSG_NAME_CPU = "CPU"    # the component names (the CPU row is the whole APU)
MSG_NAME_GPU = "GPU"
MSG_NO_LIMIT = "NO LIMIT"  # the replay key's answer when nothing binds
# The word for each limit. Uppercase W and M are the tall letter forms.
MSG_WORD_STAPM = "STAPM"
MSG_WORD_SLOW = "PPT"
MSG_WORD_FAST = "FAST PPT"
MSG_WORD_TDC = "TDC"
MSG_WORD_TDC_SOC = "SOC TDC"
MSG_WORD_EDC = "EDC"
MSG_WORD_EDC_SOC = "SOC EDC"
MSG_WORD_THM = "HOT"
MSG_WORD_POWER = "POWER"    # dGPU power cap; APU package-power heuristic
MSG_WORD_BRAKE = "BRAKE"    # dGPU hardware power brake
MSG_WORD_THERMAL = "HOT"    # dGPU thermal slowdown
MSG_WORD_CLOCK = "CLK LIMITED"  # CPU held at its base clock (boost off)
MSG_WORD_CHARGER = "CHARGER"

# Limit log (limitlog.py): a long-term record of every limit that binds, when,
# and what the machine was doing then, in limitlog.sqlite in the data
# directory. Read it with --limit-report (or limitreport.py on a copy).
LIMITLOG_ENABLE = 1
LIMITLOG_ACTIVE_S = 2.0      # sample interval while a limit binds or a component
                             # is busy (and for LIMITLOG_LINGER_S after)
LIMITLOG_IDLE_S = 30.0       # ...and otherwise
LIMITLOG_LINGER_S = 30.0
LIMITLOG_CPU_ACTIVE_PCT = 25  # total CPU load that counts as busy for sampling
LIMITLOG_ONSET_S = 2.0       # a limit held this long becomes an episode (its
                             # time counts in the tallies from the first read)
LIMITLOG_EC_S = 5.0          # framework_tool --thermal (EC temperatures, fans,
LIMITLOG_EC_IDLE_S = 30.0    # the EC's own APU throttle flags): busy / idle
LIMITLOG_PROC_S = 10.0       # top CPU processes, while busy (6x that idle)
LIMITLOG_PMTABLE_S = 600.0   # raw SMU table every this often (and at each CPU
                             # episode's start); 0: only at episode starts
LIMITLOG_RETENTION_DAYS = 90  # samples and raw tables older than this go;
                              # episodes, events and tallies are kept for good
LIMITLOG_MAX_MB = 1024        # ...and the oldest samples go past this size

# Inference thresholds (the LHM package-power fallback used only when ryzenadj
# is unavailable; all heuristic; tune as needed).
CPU_LOAD_HIGH = 80      # % CPU load above which APU power limits are evaluated
CPU_POWER_FLOOR_W = 15  # ignore APU power-cap inference below this package power
NV_ACTIVE_MIN_W = 15    # dGPU draw above which it counts as actively loaded
CEIL_NEAR = 0.95        # fraction of the (known/observed) ceiling read as "at it"
NV_NEAR_CAP = 0.92      # dGPU draw as a fraction of its enforced limit to count
                        # as genuinely power-capped -- filters NVIDIA's eager
                        # SwPowerCap (set even near idle), which made the dGPU
                        # breathe flicker on/off every few seconds
NV_DEBOUNCE_READS = 3   # consecutive evaluations a dGPU limit flag must hold
                        # before it flips, so a single-tick NVML blip can't flash
                        # the breathe on and off

# Charge/discharge overlay segment counts by power flow (watts).
RATE_SEG_W1 = 20
RATE_SEG_W2 = 45
RATE_SEG_W3 = 75

# Source cache TTLs (seconds). Most ticks reuse cached slow readings so the
# ~0.4s loop stays light. Brightness is kept short so the LEDs track a screen
# brightness change promptly (a longer TTL makes the macropad visibly lag the
# screen); set it to 0 to re-read every tick.
TTL_BRIGHTNESS = 0.5
TTL_BATTERY = 1.0
TTL_RYZENADJ = 1.0       # one ryzenadj SMU refresh feeding APU limits + Tctl
TTL_TEMP = 2.0           # the last-resort ACPI thermal-zone read (a WMI query)
TTL_LHM_SNAPSHOT = 1.0   # one LHM enumeration feeding both temp and cpu power
TTL_EC_THERMAL = 2.5     # throttled framework_tool --thermal fallback

# LibreHardwareMonitorLib: the .NET engine inside LibreHardwareMonitor, loaded
# in-process via pythonnet so the APU package-power/load/temperature sensors can
# be read directly -- no separate LHM app and no WMI provider. The native DLLs
# are fetched into lib/ with `--fetch-lhm` (that directory is gitignored).
#
# Resolved through paths.lhm_lib_dir() rather than __file__: when frozen,
# __file__ points into PyInstaller's per-run temp extraction directory, which is
# a different path on every start and is deleted at exit.
LHM_DLL = "LibreHardwareMonitorLib.dll"
HIDSHARP_DLL = "HidSharp.dll"
# NuGet packages the DLLs come from (flat v2 API gives the latest stable).
LHM_NUGET = "LibreHardwareMonitorLib"
HIDSHARP_NUGET = "HidSharp"
# LibreHardwareMonitorLib's net472 build also needs the netstandard2.0 BCL-compat
# assemblies (Span<T> etc.) that aren't in-box on .NET Framework; fetch them too
# and resolve them from lib/ via an AssemblyResolve handler at load time.
LHM_DEP_NUGETS = (
    "System.Memory",
    "System.Buffers",
    "System.Runtime.CompilerServices.Unsafe",
    "System.Numerics.Vectors",
    "System.Threading.Tasks.Extensions",
)


def lhm_reading(lhmlib, sources):
    """(temp, pkg_w, load) from LibreHardwareMonitor: the in-process library
    (no app, no WMI), else -- unavailable, or reading nothing -- the LHM app's
    WMI provider. gather() and --selftest's temperature probe both use this."""
    if lhmlib.available:
        snap = lhmlib.snapshot()
        if snap != (None, None, None):
            return snap
    return sources.lhm_snapshot()


def clamp(value, lo, hi):
    return max(lo, min(hi, int(round(value))))


def clamp8(value):
    return clamp(value, 0, 255)


def pct_to_level(pct):
    """Map a 0..100 metric percentage onto the firmware's 0..255 bar fraction.

    The wire `level` byte is a full-scale 0..255 fraction of the 4-segment bar
    (the firmware fills level*4 across the 0..1020 segment range), so a raw
    0..100 percentage must be rescaled here or a "full" bar only fills ~40%.
    """
    return clamp(pct * 255 / 100, 0, 255)


class Cached:
    """Memoize a zero-arg callable for `ttl` seconds (time.monotonic based).

    Keeps slow sources off the hot path: the first call after expiry fetches,
    every call within the TTL returns the cached value. A fetch that raises is
    not cached; the previous value (if any) is returned instead.
    """

    def __init__(self, fn, ttl):
        self._fn = fn
        self._ttl = ttl
        self._value = None
        self._expires = 0.0
        self._primed = False

    def __call__(self):
        now = time.monotonic()
        if self._primed and now < self._expires:
            return self._value
        try:
            self._value = self._fn()
        except Exception:
            if self._primed:
                return self._value
            raise
        self._primed = True
        self._expires = now + self._ttl
        return self._value


def find_device_path():
    for info in hid.enumerate(VID, PID_MACROPAD):
        if info["usage_page"] == RAW_USAGE_PAGE and info["usage"] == RAW_USAGE:
            return info["path"]
    return None


def open_device():
    path = find_device_path()
    if path is None:
        return None
    dev = hid.device()
    dev.open_path(path)
    dev.set_nonblocking(True)
    return dev


def adapter_luid_names():
    """Map adapter LUID -> GPU name from the DirectX registry key.

    GPU engine counter instances only carry the adapter LUID; this is the
    cheapest way to resolve which LUID is which GPU. Entries from previous
    boots linger, but we only look up LUIDs seen in live counters.
    """
    names = {}
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\DirectX")
    except OSError:
        return names
    with root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            try:
                with winreg.OpenKey(root, winreg.EnumKey(root, i)) as key:
                    desc, _ = winreg.QueryValueEx(key, "Description")
                    luid, _ = winreg.QueryValueEx(key, "AdapterLuid")
                    names[int(luid)] = desc
            except OSError:
                continue
    return names


def compute_only_adapter_luids():
    """LUIDs of the compute-only (MCDM) adapters, i.e. NPUs, via D3DKMT.

    An NPU is not a display adapter: it is missing from the DirectX registry
    key adapter_luid_names() reads, so it has no name to match a regex
    against. The kernel does flag it, though -- D3DKMT_ADAPTERTYPE.ComputeOnly
    -- and D3DKMTEnumAdapters3 only lists such adapters when asked to with
    IncludeComputeOnly. That flag is what Task Manager keys its NPU graph on,
    so it is vendor-neutral (AMD XDNA, Intel AI Boost, ...) with no name
    matching. Verified non-elevated; expected to work from session 0 as well
    (a dxgkrnl call, not session-scoped) -- the service records `npu_present`
    in service-state.json, which is the ground truth for that.

    Returns a set (empty if there is no NPU), or None if the API is missing
    (Windows older than 10 2004), so the caller can stop asking.
    """
    import ctypes
    from ctypes import wintypes as wt

    try:
        gdi = ctypes.WinDLL("gdi32")
        enum3 = gdi.D3DKMTEnumAdapters3
        query_info = gdi.D3DKMTQueryAdapterInfo
        close = gdi.D3DKMTCloseAdapter
    except (OSError, AttributeError):
        return None

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]

    class ADAPTERINFO(ctypes.Structure):  # D3DKMT_ADAPTERINFO
        _fields_ = [("hAdapter", wt.UINT), ("AdapterLuid", LUID),
                    ("NumOfSources", wt.ULONG),
                    ("bPrecisePresentRegionsPreferred", wt.BOOL)]

    class ENUMADAPTERS3(ctypes.Structure):  # D3DKMT_ENUMADAPTERS3
        _fields_ = [("Filter", ctypes.c_uint64), ("NumAdapters", wt.ULONG),
                    ("pAdapters", ctypes.POINTER(ADAPTERINFO))]

    class QUERYADAPTERINFO(ctypes.Structure):  # D3DKMT_QUERYADAPTERINFO
        _fields_ = [("hAdapter", wt.UINT), ("Type", ctypes.c_int),
                    ("pPrivateDriverData", ctypes.c_void_p),
                    ("PrivateDriverDataSize", wt.UINT)]

    class CLOSEADAPTER(ctypes.Structure):
        _fields_ = [("hAdapter", wt.UINT)]

    FILTER_INCLUDE_COMPUTE_ONLY = 1 << 0
    KMTQAITYPE_ADAPTERTYPE = 15
    ADAPTERTYPE_COMPUTE_ONLY = 1 << 11

    # First call (no buffer) reports how many entries to allocate; the second
    # fills them in and sets NumAdapters to the real count. Every returned
    # handle is open and must be closed.
    req = ENUMADAPTERS3(FILTER_INCLUDE_COMPUTE_ONLY, 0, None)
    if enum3(ctypes.byref(req)) != 0 or req.NumAdapters == 0:
        return set()
    arr = (ADAPTERINFO * req.NumAdapters)()
    req.pAdapters = arr
    if enum3(ctypes.byref(req)) != 0:
        return set()

    found = set()
    for info in arr[:req.NumAdapters]:
        try:
            kind = wt.UINT(0)
            q = QUERYADAPTERINFO(info.hAdapter, KMTQAITYPE_ADAPTERTYPE,
                                 ctypes.cast(ctypes.byref(kind), ctypes.c_void_p),
                                 ctypes.sizeof(kind))
            if query_info(ctypes.byref(q)) == 0 and kind.value & ADAPTERTYPE_COMPUTE_ONLY:
                luid = info.AdapterLuid
                found.add(((luid.HighPart & 0xFFFFFFFF) << 32) | luid.LowPart)
        finally:
            close(ctypes.byref(CLOSEADAPTER(info.hAdapter)))
    return found


class GpuMonitor:
    """3D engine utilization per adapter via a persistent PDH query.

    Roughly Task Manager's headline GPU number, and ~1ms per read instead
    of the seconds a Win32_PerfFormattedData WMI query takes.

    When launched at boot (Task Scheduler) the graphics stack may not be up
    yet: the "GPU Engine" counter set can be unregistered, and the DirectX
    registry key still holds only the previous boot's adapter LUIDs (LUIDs
    are per-boot, so the stale entries never match live counters). Both are
    transient, so failures here are retried from read() instead of disabling
    GPU reporting for the life of the process.

    NPU utilization rides the same query. The NPU publishes its engines under
    the same "GPU Engine" counter set, but has no 3D engine (an AMD XDNA NPU
    exposes a single engtype_Compute engine) and no DirectX registry entry, so
    it is found by LUID instead -- compute_only_adapter_luids() -- and read
    through a counter scoped to that LUID. That keeps the per-tick cost to the
    NPU's own handful of instances rather than widening the 3D counter to every
    engine of every adapter (roughly double the instances).
    """

    RETRY_S = 10  # min seconds between PDH-open / registry-rescan attempts
    # An NPU driver can register after a boot-start service is already
    # running, so keep looking for this long before deciding there is none.
    NPU_DISCOVERY_S = 300

    def __init__(self, igpu_pattern, dgpu_pattern):
        self.igpu_re = re.compile(igpu_pattern, re.IGNORECASE)
        self.dgpu_re = re.compile(dgpu_pattern, re.IGNORECASE)
        self.luid_names = adapter_luid_names()
        self.dgpu_present = any(self.dgpu_re.search(n) for n in self.luid_names.values())
        self.query = None
        self._open_failed = False
        self._next_open = 0.0
        self._next_rescan = 0.0
        self.npu_luids = set()
        self._npu_counters = []
        self._npu_searching = True
        self._npu_deadline = time.monotonic() + self.NPU_DISCOVERY_S
        self._next_npu_scan = 0.0
        if win32pdh is None:
            print("win32pdh not available; GPU usage disabled", file=sys.stderr)
            return
        self._discover_npu()
        self._open_query()

    @property
    def npu_present(self):
        return bool(self.npu_luids)

    def _open_query(self):
        self._next_open = time.monotonic() + self.RETRY_S
        query = None
        try:
            query = win32pdh.OpenQuery()
            self.counter = win32pdh.AddEnglishCounter(
                query, r"\GPU Engine(*engtype_3D)\Utilization Percentage")
            self.query = query
            self._npu_counters = []
            self._add_npu_counters()
            win32pdh.CollectQueryData(query)  # prime; % counters need two samples
            if self._open_failed:
                print("GPU counters now available", file=sys.stderr)
        except Exception as err:
            self.query = None
            # A query opened but whose counter failed to add (the GPU Engine
            # set isn't registered yet at boot) must be closed, or every retry
            # leaks one PDH query handle until the counters appear.
            if query is not None:
                try:
                    win32pdh.CloseQuery(query)
                except Exception:
                    pass
            if not self._open_failed:
                print(f"GPU counters unavailable (will retry): {err}", file=sys.stderr)
                self._open_failed = True

    def _discover_npu(self):
        """Look for an NPU; once found (or proven absent) stop looking."""
        now = time.monotonic()
        self._next_npu_scan = now + self.RETRY_S
        try:
            luids = compute_only_adapter_luids()
        except Exception:
            luids = set()
        if luids is None:
            self._npu_searching = False  # no D3DKMTEnumAdapters3 on this OS
            return
        if luids:
            self.npu_luids = luids
            self._npu_searching = False
            log("NPU found: " + ", ".join(f"luid 0x{l:X}" for l in sorted(luids)))
            if self.query is not None:
                self._add_npu_counters()
        elif now >= self._npu_deadline:
            self._npu_searching = False

    def _add_npu_counters(self):
        for luid in sorted(self.npu_luids):
            path = (r"\GPU Engine(*luid_0x%08X_0x%08X*)\Utilization Percentage"
                    % (luid >> 32, luid & 0xFFFFFFFF))
            try:
                self._npu_counters.append(
                    win32pdh.AddEnglishCounter(self.query, path))
            except Exception as err:
                log(f"NPU counter for luid 0x{luid:X} unavailable: {err}")

    def _read_npu(self):
        """NPU busy % from the last collect: the busiest engine, summed over
        processes -- the same reduction Task Manager uses for its NPU graph."""
        per_engine = {}
        for counter in self._npu_counters:
            try:
                items = win32pdh.GetFormattedCounterArray(
                    counter, win32pdh.PDH_FMT_DOUBLE)
            except Exception:
                continue  # first sample after adding, or no instances yet
            for name, value in items.items():
                m = re.search(r"luid_0x([0-9A-Fa-f]+)_0x([0-9A-Fa-f]+)_phys_(\d+)_eng_(\d+)",
                              name)
                if m:
                    key = m.groups()
                    per_engine[key] = per_engine.get(key, 0.0) + value
        return min(100, max(per_engine.values(), default=0.0))

    def _rescan_adapters(self):
        """Re-read the LUID->name map; drivers register adapters after boot."""
        self._next_rescan = time.monotonic() + self.RETRY_S
        fresh = adapter_luid_names()
        if fresh:
            self.luid_names.update(fresh)
            self.dgpu_present = any(
                self.dgpu_re.search(n) for n in self.luid_names.values())

    def read(self):
        """Return (igpu_pct, dgpu_pct, dgpu_present, npu_pct).

        npu_pct is None when the machine has no NPU (as opposed to 0 = idle).
        """
        if (self._npu_searching and win32pdh is not None
                and time.monotonic() >= self._next_npu_scan):
            self._discover_npu()
        npu_idle = 0 if self.npu_present else None
        if self.query is None:
            if win32pdh is not None and time.monotonic() >= self._next_open:
                self._open_query()
            return 0, 0, self.dgpu_present, npu_idle
        try:
            win32pdh.CollectQueryData(self.query)
            items = win32pdh.GetFormattedCounterArray(self.counter, win32pdh.PDH_FMT_DOUBLE)
        except Exception:
            return 0, 0, self.dgpu_present, npu_idle
        npu = self._read_npu() if self.npu_present else None

        per_luid = {}
        for name, value in items.items():
            m = re.search(r"luid_0x([0-9A-Fa-f]+)_0x([0-9A-Fa-f]+)", name)
            if not m:
                continue
            luid = (int(m.group(1), 16) << 32) | int(m.group(2), 16)
            per_luid[luid] = per_luid.get(luid, 0.0) + value

        if (any(l not in self.luid_names for l in per_luid)
                and time.monotonic() >= self._next_rescan):
            self._rescan_adapters()

        igpu = dgpu = 0.0
        for luid, total in per_luid.items():
            desc = self.luid_names.get(luid, "")
            if self.dgpu_re.search(desc):
                dgpu += total
            elif self.igpu_re.search(desc):
                igpu += total
        return min(100, igpu), min(100, dgpu), self.dgpu_present, npu


class DgpuPower:
    """The dGPU's device power state from Plug and Play, without waking it.

    On a hybrid-graphics laptop the dGPU powers off when nothing uses it. PnP
    keeps each device's most recent D-state (DEVPKEY_Device_PowerData, the
    "Power data" Device Manager shows), and reading it is a cfgmgr32 property
    lookup: no I/O to the device, so it can't wake it -- unlike NVML, whose
    calls power the GPU up (about 2 s) and keep it up while they continue.

    PnP says D3 for D3hot and D3cold alike. The difference is the PCIe port
    above the GPU: for D3cold Windows takes the link down and powers the port
    to D3 as well. Measured on the FW16's RTX 5070: an idle GPU drops to D3
    within ~1 s with its port still at D0 (D3hot), and the port follows 6-8 s
    later (D3cold). Short wakes from background activity bring both to D0.

    The device is found by name among the present display adapters (the same
    regex as the PDH dGPU match), then re-found by its instance ID on every
    read, so a driver reinstall or a disabled device is picked up again.
    """

    RETRY_S = 10  # min seconds between scans while no dGPU devnode is found
    DISPLAY_CLASS = "{4d36e968-e325-11ce-bfc1-08002be10318}"

    def __init__(self, dgpu_pattern, announce=False):
        self.dgpu_re = re.compile(dgpu_pattern, re.IGNORECASE)
        self.announce = announce  # log the device once it is found
        self.device_id = None
        self.name = None
        self._next_scan = 0.0
        self._cfg = None
        try:
            import ctypes
            from ctypes import wintypes as wt
            cfg = ctypes.WinDLL("cfgmgr32")
            pdword = ctypes.POINTER(wt.DWORD)
            cfg.CM_Locate_DevNodeW.argtypes = (pdword, wt.LPCWSTR, wt.ULONG)
            cfg.CM_Get_Parent.argtypes = (pdword, wt.DWORD, wt.ULONG)
            cfg.CM_Get_DevNode_PropertyW.argtypes = (
                wt.DWORD, ctypes.c_void_p, ctypes.POINTER(wt.ULONG),
                ctypes.c_void_p, ctypes.POINTER(wt.ULONG), wt.ULONG)
            cfg.CM_Get_Device_ID_List_SizeW.argtypes = (
                ctypes.POINTER(wt.ULONG), wt.LPCWSTR, wt.ULONG)
            cfg.CM_Get_Device_ID_ListW.argtypes = (
                wt.LPCWSTR, ctypes.c_void_p, wt.ULONG, wt.ULONG)
            for fn in (cfg.CM_Locate_DevNodeW, cfg.CM_Get_Parent,
                       cfg.CM_Get_DevNode_PropertyW,
                       cfg.CM_Get_Device_ID_List_SizeW, cfg.CM_Get_Device_ID_ListW):
                fn.restype = wt.DWORD  # CONFIGRET; 0 = CR_SUCCESS
        except (OSError, AttributeError):
            return
        self._ctypes, self._wt, self._cfg = ctypes, wt, cfg

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD),
                        ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class DEVPROPKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", wt.ULONG)]

        # {a45c254e-df1c-4efd-8020-67d146a850e0}: the DEVPKEY_Device_* set
        dev = GUID(0xA45C254E, 0xDF1C, 0x4EFD,
                   (ctypes.c_ubyte * 8)(0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0))
        self._key_desc = DEVPROPKEY(dev, 2)       # DEVPKEY_Device_DeviceDesc
        self._key_friendly = DEVPROPKEY(dev, 14)  # DEVPKEY_Device_FriendlyName
        self._key_power = DEVPROPKEY(dev, 32)     # DEVPKEY_Device_PowerData

    def _prop(self, inst, key):
        """A devnode property's raw bytes, or None."""
        ctypes, wt = self._ctypes, self._wt
        kind, size = wt.ULONG(), wt.ULONG(0)
        self._cfg.CM_Get_DevNode_PropertyW(inst, ctypes.byref(key), ctypes.byref(kind),
                                           None, ctypes.byref(size), 0)
        if not size.value:
            return None
        buf = (ctypes.c_ubyte * size.value)()
        if self._cfg.CM_Get_DevNode_PropertyW(inst, ctypes.byref(key), ctypes.byref(kind),
                                              buf, ctypes.byref(size), 0):
            return None
        return bytes(buf)

    def _locate(self, device_id):
        inst = self._wt.DWORD()
        if self._cfg.CM_Locate_DevNodeW(self._ctypes.byref(inst), device_id, 0):
            return None
        return inst.value

    def _d_state(self, inst):
        """0..3 for D0..D3 (CM_POWER_DATA.PD_MostRecentPowerState), or None."""
        data = self._prop(inst, self._key_power)
        if not data or len(data) < 8:
            return None
        state = int.from_bytes(data[4:8], "little")  # PowerDeviceD0 = 1 .. D3 = 4
        return state - 1 if 1 <= state <= 4 else None

    def _scan(self):
        """Find the present display adapter whose name matches the dGPU regex."""
        ctypes, wt = self._ctypes, self._wt
        self._next_scan = time.monotonic() + self.RETRY_S
        flags = 0x200 | 0x100  # CM_GETIDLIST_FILTER_CLASS | _PRESENT
        size = wt.ULONG()
        if self._cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), self.DISPLAY_CLASS, flags):
            return
        buf = ctypes.create_unicode_buffer(size.value)
        if self._cfg.CM_Get_Device_ID_ListW(self.DISPLAY_CLASS, buf, size, flags):
            return
        for device_id in buf[:size.value].split("\0"):
            inst = self._locate(device_id) if device_id else None
            if inst is None:
                continue
            raw = self._prop(inst, self._key_friendly) or self._prop(inst, self._key_desc)
            name = raw.decode("utf-16-le", "replace").rstrip("\0") if raw else ""
            if self.dgpu_re.search(name):
                if self.announce and device_id != self.device_id:
                    log(f"dGPU power state from PnP: {name}")
                self.device_id, self.name = device_id, name
                return

    def state(self):
        """'D0'..'D2', 'D3hot', 'D3cold', or None (no dGPU found / unreadable)."""
        if self._cfg is None:
            return None
        inst = self._locate(self.device_id) if self.device_id else None
        if inst is None:
            if time.monotonic() < self._next_scan:
                return None
            self._scan()
            inst = self._locate(self.device_id) if self.device_id else None
            if inst is None:
                return None
        d = self._d_state(inst)
        if d is None:
            return None
        if d < 3:
            return f"D{d}"
        parent = self._wt.DWORD()
        if self._cfg.CM_Get_Parent(self._ctypes.byref(parent), inst, 0):
            return "D3hot"
        return "D3cold" if self._d_state(parent.value) == 3 else "D3hot"


class DgpuSleepWatch:
    """How long the dGPU has stayed powered off (D3cold) without a break.

    A thread of its own looks at DgpuPower every POLL_S, apart from the main
    loop: a background wake lasts a second or two, and a main-loop tick can
    stall about that long (WMI, framework_tool) or stop for seconds while the
    pad is away. A look that isn't D3cold ends the streak, and so does a gap
    between two looks longer than GAP_S (the machine slept, the process was
    frozen): the GPU could have woken unseen. The next D3cold look starts over.
    """

    POLL_S = 0.25
    GAP_S = 3.0

    def __init__(self, power):
        self.power = power
        # (state, since, seen): the last look's state, when the streak it
        # belongs to started (None = not in D3cold) and when that look was
        # taken, all monotonic. One tuple, so a reader never sees half a look.
        self._look = (None, None, None)
        self._failed = False

    def observe(self, now):
        try:
            state = self.power.state()
        except Exception as err:  # keep looking; a dead thread would freeze the marker
            if not self._failed:
                self._failed = True
                log(f"dGPU power state could not be read: {err!r}")
            state = None
        _, since, seen = self._look
        if state != "D3cold" or seen is None or now - seen > self.GAP_S:
            since = None
        if state == "D3cold" and since is None:
            since = now
        self._look = (state, since, now)

    def start(self, stop_event):
        def loop():
            while True:
                self.observe(time.monotonic())
                if stop_event.wait(self.POLL_S):
                    return
        threading.Thread(target=loop, name="dgpu-sleep-watch", daemon=True).start()

    def snapshot(self):
        """(state, seconds in D3cold so far or None): 'D0'..'D3cold' or None.

        The time runs to the last look, not to now, so a stale look (the
        watcher not yet past a gap) can't count time nobody saw."""
        state, since, seen = self._look
        return state, (None if since is None else seen - since)


class Reason:
    """One limit that binds right now: what it is and how far in.

    key names the limit (it picks the message's word, see REASON_WORDS), kind
    is its LIMIT_KINDS group, ratio how close the live value is to the limit
    (it ranks limits binding at once), and value + unit what the message shows
    (the limit itself, or for the dGPU's thermal slowdown its temperature).
    """
    __slots__ = ("key", "kind", "ratio", "value", "unit")

    def __init__(self, key, kind, ratio=1.0, value=None, unit=""):
        self.key = key
        self.kind = kind
        self.ratio = ratio
        self.value = value
        self.unit = unit

    def word(self):
        return globals()[REASON_WORDS[self.key]]  # read now: the INI rebinds them

    def __repr__(self):
        v = "" if self.value is None else f" {self.value:.1f}{self.unit}"
        return f"{self.key}({self.ratio:.2f}{v})"


# Message word for each reason key: the *name* of the constant, looked up when
# the message is built, so sysmon.ini can change it.
REASON_WORDS = {
    "stapm": "MSG_WORD_STAPM", "slow": "MSG_WORD_SLOW", "fast": "MSG_WORD_FAST",
    "tdc": "MSG_WORD_TDC", "tdc_soc": "MSG_WORD_TDC_SOC",
    "edc": "MSG_WORD_EDC", "edc_soc": "MSG_WORD_EDC_SOC",
    "thm": "MSG_WORD_THM", "power": "MSG_WORD_POWER", "brake": "MSG_WORD_BRAKE",
    "thermal": "MSG_WORD_THERMAL", "clock": "MSG_WORD_CLOCK",
    "charger": "MSG_WORD_CHARGER",
}


class NvGpuLimits:
    """dGPU power & throttle state for NVIDIA cards via NVML.

    NVIDIA exposes authoritative throttle reasons (no inference needed):
    nvmlDeviceGetCurrentClocksThrottleReasons returns a bitmask, and the
    enforced power limit drops on battery. Disables itself if pynvml is
    missing or there's no NVIDIA GPU; an NVML init failure is retried from
    evaluate() because at boot (Task Scheduler) the NVIDIA driver service
    may simply not be running yet.
    """

    RETRY_S = 10  # min seconds between NVML init attempts

    def __init__(self, enabled=True):
        self.available = False
        self.handle = None
        self.last = None   # the last read's raw values, for the limit log
        self._streak = {}  # reason key -> consecutive reads it has been seen
        self._retryable = False
        self._init_failed = False
        self._next_init = 0.0
        if not enabled:
            return
        if pynvml is None:
            print("pynvml not installed; dGPU limit detection disabled",
                  file=sys.stderr)
            return
        self._retryable = True
        self._try_init()

    def _try_init(self):
        self._next_init = time.monotonic() + self.RETRY_S
        try:
            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() < 1:
                self._retryable = False  # driver is up but no NVIDIA GPU
                return
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            # constant names were renamed Throttle -> Event across versions
            self._sw_power = getattr(
                pynvml, "nvmlClocksThrottleReasonSwPowerCap",
                getattr(pynvml, "nvmlClocksEventReasonSwPowerCap", 0))
            self._hw_brake = getattr(
                pynvml, "nvmlClocksThrottleReasonHwPowerBrakeSlowdown",
                getattr(pynvml, "nvmlClocksEventReasonHwPowerBrakeSlowdown", 0))
            self._thermal = (
                getattr(pynvml, "nvmlClocksThrottleReasonSwThermalSlowdown",
                        getattr(pynvml, "nvmlClocksEventReasonSwThermalSlowdown", 0x20))
                | getattr(pynvml, "nvmlClocksThrottleReasonHwThermalSlowdown",
                          getattr(pynvml, "nvmlClocksEventReasonHwThermalSlowdown", 0x40)))
            self._reasons_fn = (
                getattr(pynvml, "nvmlDeviceGetCurrentClocksThrottleReasons", None)
                or getattr(pynvml, "nvmlDeviceGetCurrentClocksEventReasons", None))
            self.available = self._reasons_fn is not None
            self._retryable = False
            if self.available and self._init_failed:
                print("NVML now initialized; dGPU limit detection active",
                      file=sys.stderr)
        except Exception as err:
            self.available = False
            if not self._init_failed:
                print(f"NVML init failed; dGPU limit detection "
                      f"{'will retry' if self._retryable else 'disabled'}: {err}",
                      file=sys.stderr)
                self._init_failed = True

    def utilization(self):
        """Device-wide dGPU busy percentage via NVML, or None if unavailable.

        Preferred over the PDH 3D-engine sum for the dGPU row: it is what
        nvidia-smi reports, counts every engine (compute/encode/copy, which
        engtype_3D misses), and needs no LUID->adapter mapping.
        """
        if not self.available:
            if self._retryable and time.monotonic() >= self._next_init:
                self._try_init()
            if not self.available:
                return None
        try:
            return pynvml.nvmlDeviceGetUtilizationRates(self.handle).gpu
        except Exception:
            return None

    def evaluate(self, on_ac, weak_charger):
        """Return {key: Reason} for the dGPU limits that bind right now.

        power (the driver's power cap, believed only near the enforced limit)
        and brake (the hardware power brake) count on AC only; thermal (the
        driver's or the hardware's thermal slowdown) anywhere. With "charger"
        in LIMIT_KINDS, a loaded GPU held back while the battery assists an
        overloaded charger is the charger instead. Each must hold for
        NV_DEBOUNCE_READS reads in a row before it counts, so one NVML blip
        can't start the breathe; LimitTracker holds it on the way out.
        """
        self.last = None
        if not self.available:
            if self._retryable and time.monotonic() >= self._next_init:
                self._try_init()
            if not self.available:
                return self._debounce({})
        try:
            reasons = self._reasons_fn(self.handle)
            draw = pynvml.nvmlDeviceGetPowerUsage(self.handle) / 1000.0
            enforced = pynvml.nvmlDeviceGetEnforcedPowerLimit(self.handle) / 1000.0
            try:
                default = pynvml.nvmlDeviceGetPowerManagementDefaultLimit(
                    self.handle) / 1000.0
            except Exception:
                default = enforced
        except Exception:
            return self._debounce({})
        # raw, unfiltered: the limit log keeps every bit (limitlog.py)
        self.last = {"reasons": int(reasons), "draw": draw,
                     "enforced": enforced, "default": default}

        sw_cap = bool(reasons & self._sw_power)
        hw_brake = bool(reasons & self._hw_brake)
        thermal = bool(reasons & self._thermal)
        capped_below_stock = default > 0 and enforced < 0.9 * default
        # NVIDIA sets SwPowerCap eagerly -- it can be set even near idle, when the
        # GPU is nowhere near its limit, which made the breathe flicker. Only
        # honour it as a real power cap when the GPU is actually drawing near its
        # enforced limit. HwPowerBrake is a genuine hardware brake; keep it as-is.
        near_cap = enforced > 0 and draw >= NV_NEAR_CAP * enforced
        found = {}
        if (weak_charger and "charger" in LIMIT_KINDS and draw >= NV_ACTIVE_MIN_W
                and (hw_brake or (sw_cap and near_cap) or capped_below_stock)):
            # supply-bound: a loaded GPU capped while the charger can't keep up
            found["charger"] = Reason("charger", "charger")
        else:
            if on_ac and "power" in LIMIT_KINDS:
                if sw_cap and near_cap:
                    found["power"] = Reason("power", "power", draw / enforced,
                                            enforced, "W")
                if hw_brake:
                    found["brake"] = Reason("brake", "power")
            if thermal and "thermal" in LIMIT_KINDS:
                try:
                    temp = float(pynvml.nvmlDeviceGetTemperature(
                        self.handle, pynvml.NVML_TEMPERATURE_GPU))
                except Exception:
                    temp = None
                found["thermal"] = Reason("thermal", "thermal", 1.0, temp, "C")
        return self._debounce(found)

    def _debounce(self, found):
        """Keep only reasons seen NV_DEBOUNCE_READS evaluations in a row."""
        self._streak = {k: self._streak.get(k, 0) + 1 for k in found}
        return {k: r for k, r in found.items()
                if self._streak[k] >= NV_DEBOUNCE_READS}


class ApuLimits:
    """Infer APU (CPU+iGPU) power state from LibreHardwareMonitor.

    AMD exposes no throttle flags, so "at the power cap" is inferred: under
    sustained high load the package power plateaus at the chip's sustained
    PPT. The ceiling is taken from an explicit cap when given, otherwise
    auto-calibrated as the highest package power seen on AC (approximate; the
    explicit cap is the reliable path). Fails safe -- asserts nothing when the
    reading is missing or the chip clearly isn't being pushed.
    """

    def __init__(self, cap_watts=None):
        self.cap = cap_watts
        self.ceiling = 0.0
        self.prev = 0.0

    def reasons(self, reading, on_ac, weak_charger):
        """The heuristic's verdict as {key: Reason}: "power" (with the package
        power) or, with "charger" in LIMIT_KINDS, "charger"."""
        power, psrc = self.evaluate(reading, on_ac, weak_charger)
        if psrc and "charger" in LIMIT_KINDS:
            return {"charger": Reason("charger", "charger")}
        if power and "power" in LIMIT_KINDS:
            return {"power": Reason("power", "power", 1.0, reading[0], "W")}
        return {}

    def evaluate(self, reading, on_ac, weak_charger):
        """Return (power_limited, psrc_limited) for the APU, inferred.

        The fallback when ryzenadj can't read the SMU table: the cap is
        inferred from LHM `reading` = (package_watts, load_pct). Only the
        weak-charger (battery-assisting-on-AC) case is surfaced as a
        power-source limit; plain battery operation is left to the battery row.
        """
        if reading is None:
            return False, False
        pkg_w, load = reading
        if load is None or load < CPU_LOAD_HIGH:
            self.prev = pkg_w or 0.0
            return False, False
        if weak_charger:
            self.prev = pkg_w or 0.0
            return False, True
        if not on_ac:
            # plain battery: not surfaced here (the battery row covers it)
            self.prev = pkg_w or 0.0
            return False, False
        # on AC under high load: power-capped if package power has plateaued
        # at (near) the ceiling rather than still climbing toward it
        if pkg_w is None or pkg_w < CPU_POWER_FLOOR_W:
            self.prev = pkg_w or 0.0
            return False, False
        prev_ceiling = self.cap if self.cap else self.ceiling
        self.ceiling = max(self.ceiling, pkg_w)
        climbing = pkg_w > self.prev + 1.0
        self.prev = pkg_w
        power = prev_ceiling > 0 and not climbing and pkg_w >= CEIL_NEAR * prev_ceiling
        return power, False


FRAMEWORK_TOOL_EXE = "framework_tool.exe"
_framework_tool_path = None    # None = not resolved yet, False = not found
# One framework_tool at a time: each opens the EC, and the limit log's thread
# calls it too (limitlog.py).
_framework_tool_lock = threading.Lock()


def find_framework_tool(explicit=None):
    """Locate framework_tool.exe, without relying on PATH.

    PATH is the wrong thing to trust here, and this cost a real bug: winget
    installs a portable package into the *user's* WinGet\\Links unless it was
    run elevated, and that directory is only ever on the user's PATH. The
    LocalSystem service gets the machine PATH, which has no WinGet entry at all,
    so it silently lost the EC temperature source and the battery charge cap --
    while an interactive --selftest, inheriting the user's PATH, found the tool
    and reported everything healthy.

    %LOCALAPPDATA% is no help either: for LocalSystem it resolves to
    C:\\Windows\\System32\\config\\systemprofile\\AppData\\Local, which has no
    WinGet directory. So the user profiles are searched explicitly -- the
    service runs as SYSTEM and can read them.

    Resolved once and cached; pass an explicit path (--framework-tool) to skip
    the search entirely.
    """
    import glob
    import shutil

    cands = []
    if explicit:
        # An explicit setting that does not exist must not fail silently: it is
        # almost always a typo or a moved install, and quietly searching on
        # would leave the user editing a path that is never consulted.
        if os.path.isfile(explicit):
            return explicit
        log(f"framework-tool is set to {explicit}, which does not exist; "
            f"searching the usual locations instead")
    found = shutil.which("framework_tool")
    if found:
        cands.append(found)
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    cands.append(os.path.join(program_files, "WinGet", "Links", FRAMEWORK_TOOL_EXE))
    cands.append(os.path.join(program_files, "Framework Computer", FRAMEWORK_TOOL_EXE))
    # Every user profile's per-user winget install.
    users = os.environ.get("PUBLIC")
    users = os.path.dirname(users) if users else r"C:\Users"
    for sub in (r"AppData\Local\Microsoft\WinGet\Links",
                r"AppData\Local\Microsoft\WindowsApps"):
        cands.extend(sorted(glob.glob(os.path.join(users, "*", sub,
                                                   FRAMEWORK_TOOL_EXE))))
    for path in cands:
        try:
            if path and os.path.isfile(path):
                return path
        except OSError:
            continue
    return None


def set_framework_tool(explicit=None):
    """Resolve framework_tool once at start-up; log where it was found."""
    global _framework_tool_path
    path = find_framework_tool(explicit)
    _framework_tool_path = path or False
    if path:
        log(f"framework_tool: {path}")
    else:
        log("framework_tool not found (EC temperature and battery charge cap "
            "unavailable); install it with "
            "`winget install FrameworkComputer.framework_tool`, or set "
            "framework-tool in sysmon.ini")
    return path


def run_framework_tool(*tool_args, timeout=2, wait=1.0):
    """Run framework_tool and return its stdout, or None on transient failure.

    Raises FileNotFoundError if the tool is not installed, so callers can
    disable themselves permanently. Calls are serialised (one EC client at a
    time): a caller waits up to `wait` seconds for another's call to finish,
    then gives up (None). The poll loop keeps the short default, so a slow
    call on the limit log's thread can't stall the frames past the pad's
    timeout.
    """
    global _framework_tool_path
    if _framework_tool_path is None:
        set_framework_tool()
    if not _framework_tool_path:
        raise FileNotFoundError(FRAMEWORK_TOOL_EXE)
    if not _framework_tool_lock.acquire(timeout=wait):
        return None
    try:
        # stdin must be DEVNULL: under CREATE_NO_WINDOW framework_tool
        # sees a fresh console and waits for ENTER before exiting
        return subprocess.run(
            [_framework_tool_path, *tool_args],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except (subprocess.TimeoutExpired, OSError) as err:
        if isinstance(err, FileNotFoundError):
            raise
        return None
    finally:
        _framework_tool_lock.release()


class EcThermal:
    """CPU temperature from the Framework EC via framework_tool.

    Fallback source: used only when LibreHardwareMonitor's temperature is
    unavailable. The EC reports live die temperatures; the FW16's ACPI thermal
    zones return frozen values. Needs framework_tool on PATH and an elevated
    process for the EC driver. Each call spawns a subprocess, so it is wrapped
    in a TTL cache by the caller; this class additionally holds the last good
    reading across transient failures.
    """

    _LINE_RE = re.compile(r"^\s*(?P<name>[^:]+?):\s+(?P<val>-?\d+)\s*C\s*$")
    _HOLD_SECS = 15

    def __init__(self):
        self.available = True
        self._last = None  # (monotonic timestamp, value)

    def temperature(self):
        """Return temperature in degrees C, or None if unavailable."""
        if not self.available:
            return None
        try:
            out = run_framework_tool("--thermal")
        except FileNotFoundError:
            print("framework_tool not found; EC temperature disabled",
                  file=sys.stderr)
            self.available = False
            return None
        if out is not None:
            temps = {}
            for line in out.splitlines():
                m = self._LINE_RE.match(line)
                # EC reports 255 (or negative) for absent/unpowered sensors
                if m and 0 < int(m.group("val")) < 120:
                    temps[m.group("name").strip()] = int(m.group("val"))
            if temps:
                val = temps.get("APU", max(temps.values()))
                self._last = (time.monotonic(), val)
                return val
        # transient failure: hold the last good reading briefly rather
        # than dropping to the WMI fallbacks (static-on-FW16 ACPI zones)
        if self._last and time.monotonic() - self._last[0] < self._HOLD_SECS:
            return self._last[1]
        return None


class EcChargeLimit:
    """Battery max-charge cap from the Framework EC via framework_tool.

    `framework_tool --charge-limit` prints "Minimum N%, Maximum N%". The cap
    rarely changes, so it is re-read every _REFRESH_SECS rather than every
    packet (each read spawns a process). Defaults to 100 (uncapped) when
    unreadable, e.g. when the tool is missing or the process isn't elevated.
    """

    _MAX_RE = re.compile(r"Maximum\s+(\d+)\s*%")
    _REFRESH_SECS = 60

    def __init__(self):
        self.available = True
        self._cap = 100
        self._next_read = 0.0

    def cap(self):
        """Return the max charge percentage, 1-100."""
        if not self.available or time.monotonic() < self._next_read:
            return self._cap
        self._next_read = time.monotonic() + self._REFRESH_SECS
        try:
            out = run_framework_tool("--charge-limit")
        except FileNotFoundError:
            print("framework_tool not found; charge cap assumed 100%",
                  file=sys.stderr)
            self.available = False
            return self._cap
        m = self._MAX_RE.search(out or "")
        if m and 0 < int(m.group(1)) <= 100:
            self._cap = int(m.group(1))
        return self._cap


class WmiSources:
    """Lazily connected WMI namespaces; every query degrades gracefully.

    The two LibreHardwareMonitor enumerations (temperature + CPU power) are
    collapsed into a single `lhm_snapshot()` walk so a tick touches the WMI
    provider once, not twice.
    """

    def __init__(self):
        self._wmi_ns = None
        self._lhm = None
        if wmi_module is None:
            print("wmi module not available; temperature and screen "
                  "brightness readings disabled", file=sys.stderr)

    def _connect(self, attr, namespace):
        if wmi_module is None:
            return None
        if getattr(self, attr) is None:
            try:
                setattr(self, attr, wmi_module.WMI(namespace=namespace))
            except Exception:
                return None
        return getattr(self, attr)

    def lhm_snapshot(self):
        """One LibreHardwareMonitor walk -> (temp_c, cpu_pkg_w, cpu_load_pct).

        Any field may be None if that sensor is absent or LHM is unavailable.
        This replaces the two separate Sensor() enumerations that previously
        backed temperature() and cpu_power_state(): a single pass extracts the
        CPU temperature, CPU package power and total CPU load together. Wrapped
        in a TTL cache by the caller so it runs at most once per snapshot
        interval regardless of how many consumers read it.
        """
        lhm = self._connect("_lhm", "root\\LibreHardwareMonitor")
        if lhm is None:
            return None, None, None
        try:
            sensors = lhm.Sensor()
        except Exception:
            return None, None, None

        temps = []          # CPU/general temperature sensors (Value, is_preferred)
        pkg_w = None
        load = None
        for s in sensors:
            stype = s.SensorType
            name = s.Name or ""
            parent = (s.Parent or "").lower()
            if stype == "Temperature":
                preferred = name in ("CPU Package", "Core (Tctl/Tdie)", "Tctl")
                try:
                    temps.append((float(s.Value), preferred))
                except (TypeError, ValueError):
                    pass
            elif "cpu" in parent and stype == "Power" and "package" in name.lower():
                pkg_w = s.Value
            elif "cpu" in parent and stype == "Load" and name == "CPU Total":
                load = s.Value

        temp_c = None
        if temps:
            preferred = [v for v, p in temps if p]
            temp_c = max(preferred) if preferred else max(v for v, _ in temps)
        return temp_c, pkg_w, load

    def acpi_temperature(self):
        """Last-resort ACPI thermal-zone temperature (static on the FW16)."""
        ns = self._connect("_wmi_ns", "root\\wmi")
        if ns is None:
            return None
        try:
            zones = ns.MSAcpi_ThermalZoneTemperature()
            if zones:
                # value is tenths of Kelvin
                return max(z.CurrentTemperature for z in zones) / 10.0 - 273.15
        except Exception:
            pass
        return None

    def battery_state(self):
        """Return (charging, discharging, rate_watts), or None if unavailable.

        Both booleans False means the battery is idle — typically plugged in
        and held at a charge cap. rate_watts is the active charge or
        discharge power (ACPI reports it in mW). The BatteryStatus class
        gives all of this directly, unlike psutil which only sees
        plugged/unplugged.
        """
        ns = self._connect("_wmi_ns", "root\\wmi")
        if ns is None:
            return None
        try:
            statuses = ns.BatteryStatus()
            if statuses:
                charge_mw = sum(s.ChargeRate or 0 for s in statuses)
                discharge_mw = sum(s.DischargeRate or 0 for s in statuses)
                charging = any(bool(s.Charging) for s in statuses) or charge_mw > 0
                discharging = (any(bool(s.Discharging) for s in statuses)
                               or discharge_mw > 0)
                rate_mw = discharge_mw if discharging else charge_mw
                # watts as a float: rounded to whole watts, the sub-watt cap
                # drift DRIFT_MIN_W (0.1 W) is there to catch read as 0
                return charging, discharging, max(0.0, min(255.0, rate_mw / 1000))
        except Exception:
            pass
        return None

    def screen_brightness(self):
        """Return screen brightness 0-100, or None if unavailable."""
        ns = self._connect("_wmi_ns", "root\\wmi")
        if ns is None:
            return None
        try:
            monitors = ns.WmiMonitorBrightness()
            if monitors:
                return int(monitors[0].CurrentBrightness)
        except Exception:
            pass
        return None


def fetch_lhm_dlls(dest=None, force=False):
    """Download LibreHardwareMonitorLib + HidSharp DLLs from NuGet into dest.

    A NuGet .nupkg is a zip; we pull the latest stable of each package and copy
    out the .NET Framework build of each DLL. Run once via `--fetch-lhm`.

    Existing files are left alone unless `force`. That keeps re-runs cheap, and
    avoids the common failure of trying to overwrite a DLL that a running
    sysmon service still has mapped in-process.
    """
    import urllib.request
    import zipfile
    import tempfile
    import glob
    import shutil

    if dest is None:
        dest = paths.lhm_lib_dir()
    os.makedirs(dest, exist_ok=True)

    def grab(pkg, dll):
        target = os.path.join(dest, dll)
        if os.path.exists(target) and not force:
            print(f"  have    {dll}")
            return
        with tempfile.TemporaryDirectory() as tmp:
            nupkg = os.path.join(tmp, pkg + ".nupkg")
            urllib.request.urlretrieve(
                f"https://www.nuget.org/api/v2/package/{pkg}", nupkg)
            with zipfile.ZipFile(nupkg) as zf:
                zf.extractall(tmp)
            hits = glob.glob(os.path.join(tmp, "**", dll), recursive=True)
            # Pick the runtime implementation, never a ref/ assembly (metadata
            # only -> BadImageFormat), and never a wrong-architecture build.
            # Newer LibreHardwareMonitorLib ships per-RID DLLs under
            # runtimes/win-<arch>/lib/<tfm>/; match this process's architecture.
            import platform
            want_rid = "win-arm64" if "arm" in platform.machine().lower() else "win-x64"
            bad_tfm = ("netcoreapp", "/netstandard1.", "/net5", "/net6",
                       "/net7", "/net8", "/net9")
            impl = []
            for h in hits:
                n = h.replace("\\", "/").lower()
                if "/ref/" in n:
                    continue
                if "/runtimes/" in n and f"/runtimes/{want_rid}/" not in n:
                    continue  # a different architecture's runtime build
                if any(b in n for b in bad_tfm):
                    continue  # needs .NET Core / .NET 5+, not .NET Framework
                if "/lib/" in n or f"/runtimes/{want_rid}/" in n:
                    impl.append(h)
            candidates = impl or hits
            pick = None
            for pref in ("net472", "net471", "net47", "net462", "net461",
                         "net46", "net452", "net45", "net48", "netstandard2.0"):
                pick = next((h for h in candidates
                             if pref + "/" in h.replace("\\", "/").lower()), None)
                if pick:
                    break
            pick = pick or (candidates[0] if candidates else None)
            if not pick:
                raise RuntimeError(f"{dll} not found in the {pkg} package")
            try:
                shutil.copy2(pick, target)
            except PermissionError as err:
                raise RuntimeError(
                    f"cannot write {target}: {err}. A running sysmon service or "
                    f"scheduled task still has this DLL loaded -- stop it "
                    f"(sc stop FW16Sysmon) and retry.") from err
            print(f"  fetched {dll}  <- {os.path.relpath(pick, tmp)}")

    grab(LHM_NUGET, LHM_DLL)
    grab(HIDSHARP_NUGET, HIDSHARP_DLL)
    for dep in LHM_DEP_NUGETS:
        grab(dep, dep + ".dll")
    print(f"LibreHardwareMonitorLib + deps installed in {dest}")


class LhmLibSource:
    """APU sensors via LibreHardwareMonitorLib loaded in-process (pythonnet).

    Reads CPU package power, total load and temperature straight from the
    LibreHardwareMonitor engine -- no separate app, no WMI provider. Needs the
    DLLs in lib/ (run `--fetch-lhm`) and an elevated process, since the engine
    loads a ring0 driver to read the CPU MSRs/power. Degrades to `available =
    False` (and the caller falls back to the WMI provider / EC) otherwise.

    Open the driver once at start-up and Close() it at exit; snapshot() only
    calls Update(), so it is cheap enough to sit behind the same TTL cache as
    the WMI snapshot.
    """

    def __init__(self, enabled=True, lib_dir=None):
        self.available = False
        self._computer = None
        self._cpu_hw = []
        self._resolver = None
        if lib_dir is None:
            lib_dir = paths.lhm_lib_dir()
        lhm_dll = os.path.join(lib_dir, LHM_DLL)
        hid_dll = os.path.join(lib_dir, HIDSHARP_DLL)
        if not enabled:
            return
        if not os.path.exists(lhm_dll):
            print(f"LibreHardwareMonitorLib not found in {lib_dir}; run "
                  f"`python fw16_sysmon.py --fetch-lhm` (or enable the LHM WMI "
                  f"provider). In-process APU detection disabled.", file=sys.stderr)
            return
        try:
            import clr  # pythonnet
            from System import AppDomain, ResolveEventHandler
            from System.Reflection import Assembly

            # Resolve LibreHardwareMonitorLib's transitive deps (System.Memory
            # and friends, fetched into lib/) by simple name, sidestepping .NET
            # Framework's strict version binding (AssemblyResolve results bypass
            # the version check, so any compatible build in lib/ satisfies them).
            def _resolve(sender, ev_args):
                short = ev_args.Name.split(",", 1)[0]
                cand = os.path.join(lib_dir, short + ".dll")
                if os.path.exists(cand):
                    return Assembly.LoadFrom(cand)
                return None

            self._resolver = ResolveEventHandler(_resolve)
            AppDomain.CurrentDomain.AssemblyResolve += self._resolver

            if os.path.exists(hid_dll):
                clr.AddReference(hid_dll)
            clr.AddReference(lhm_dll)
            from LibreHardwareMonitor.Hardware import Computer

            comp = Computer()
            comp.IsCpuEnabled = True
            comp.Open()
            self._computer = comp
            self._cpu_hw = [hw for hw in comp.Hardware
                            if str(hw.HardwareType) == "Cpu"]
            self.available = bool(self._cpu_hw)
            if not self.available:
                print("LibreHardwareMonitorLib opened but exposed no CPU hardware "
                      "(elevated? supported CPU?); in-process APU detection "
                      "disabled.", file=sys.stderr)
                self.close()
        except Exception as err:
            print(f"LibreHardwareMonitorLib init failed ({err}); in-process APU "
                  f"detection disabled.", file=sys.stderr)
            self.close()

    def snapshot(self):
        """Return (temp_c, cpu_pkg_w, cpu_load); any field None if absent.

        Same shape as WmiSources.lhm_snapshot() so it is a drop-in source.
        """
        if not self.available:
            return None, None, None
        temps = []
        pkg_w = None
        load = None
        try:
            for hw in self._cpu_hw:
                hw.Update()
                for s in hw.Sensors:
                    val = s.Value
                    if val is None:
                        continue
                    st = str(s.SensorType)
                    name = s.Name or ""
                    if st == "Temperature":
                        # a live CPU is never at 0 C; 0 means the ring0 driver
                        # is not loaded (process not elevated) -> no reading
                        if val > 0:
                            preferred = name in ("CPU Package", "Core (Tctl/Tdie)", "Tctl")
                            temps.append((float(val), preferred))
                    elif st == "Power" and "package" in name.lower():
                        if val > 0:  # 0 W package power == no driver reading
                            pkg_w = float(val)
                    elif st == "Load" and name == "CPU Total":
                        load = float(val)
        except Exception:
            return None, None, None
        temp_c = None
        if temps:
            preferred = [v for v, p in temps if p]
            temp_c = max(preferred) if preferred else max(v for v, _ in temps)
        return temp_c, pkg_w, load

    def close(self):
        try:
            if self._computer is not None:
                self._computer.Close()
        except Exception:
            pass
        self._computer = None
        self._cpu_hw = []
        self.available = False


# RyzenAdj / libryzenadj: the AMD SMU power-metrics table -- the same source
# HWiNFO reads for "APU STAPM Limit" et al. The DLL ships with the `ryzenadj`
# tool (e.g. a scoop install) alongside its WinRing0 ring0 driver.
RYZENADJ_DLL = "libryzenadj.dll"
RYZENADJ_DRIVER = "WinRing0x64.sys"


def find_ryzenadj_dir(explicit=None):
    """Locate the directory holding libryzenadj.dll (+ its WinRing0 driver).

    Order: an explicit --ryzenadj-dir; the bundled copy shipped beside the
    executable; the real install dir behind a `ryzenadj` shim on PATH (scoop
    writes a `.shim` sidecar whose `path =` points at the actual exe, which is
    NOT where the shim lives); then the conventional scoop location. Returns
    None if the DLL isn't found anywhere.

    The bundled copy must be tried before the PATH/scoop probes, because those
    two are inherently user-scoped and both fail under the service: as
    LocalSystem, %USERPROFILE% is C:\\Windows\\system32\\config\\systemprofile
    and a user's scoop shims are not on the machine PATH. Shipping the DLL is
    what makes the APU power-limit breathe work from session 0.
    """
    import shutil
    cands = []
    if explicit:
        cands.append(explicit)
    cands.append(paths.vendor_dir())
    exe = shutil.which("ryzenadj")
    if exe:
        cands.append(os.path.dirname(exe))
        shim = (exe[:-4] if exe.lower().endswith(".exe") else exe) + ".shim"
        try:
            with open(shim, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.strip().lower().startswith("path"):
                        real = line.split("=", 1)[1].strip().strip('"')
                        cands.append(os.path.dirname(real))
        except OSError:
            pass
    profile = os.environ.get("USERPROFILE")
    if profile:
        cands.append(os.path.join(profile, "scoop", "apps", "ryzenadj",
                                  "current"))
    for d in cands:
        if d and os.path.isfile(os.path.join(d, RYZENADJ_DLL)):
            return d
    return None


class RyzenAdjSource:
    """AMD APU power-limit + die-temperature from libryzenadj (the SMU table).

    Reads the same STAPM / PPT-fast / PPT-slow limit+value pairs HWiNFO shows,
    so "the APU is at a power limit" is authoritative rather than inferred from
    a package-power plateau: a limit is binding when its live value reaches
    (within APU_LIMIT_NEAR of) the limit. Also exposes the Tctl die temperature
    as an in-process temperature source. libryzenadj is loaded once via ctypes
    with a persistent handle; it loads the WinRing0 ring0 driver to read the
    SMU, so the process must be elevated. Degrades to available=False (the
    caller then falls back to the LHM package-power heuristic / other temp
    sources) when the DLL, driver, or CPU isn't supported.

    Open the handle once at start-up and cleanup() at exit; snapshot() only
    refreshes the table, so it is cheap enough to sit behind a TTL cache.
    """

    # (value getter, limit getter) per SMU limit; HWiNFO / `ryzenadj -i` names:
    # STAPM, PPT FAST/SLOW, TDC/EDC VDD and SOC, THM CORE. Note get_tctl_temp
    # is the THM *limit*; the temperature is get_tctl_temp_value.
    PAIRS = {
        "stapm": ("get_stapm_value", "get_stapm_limit"),
        "fast": ("get_fast_value", "get_fast_limit"),
        "slow": ("get_slow_value", "get_slow_limit"),
        "tdc": ("get_vrm_current_value", "get_vrm_current"),
        "tdc_soc": ("get_vrmsoc_current_value", "get_vrmsoc_current"),
        "edc": ("get_vrmmax_current_value", "get_vrmmax_current"),
        "edc_soc": ("get_vrmsocmax_current_value", "get_vrmsocmax_current"),
        "thm": ("get_tctl_temp_value", "get_tctl_temp"),
    }

    # More of the table, for the limit log only (limitlog.py), as `ryzenadj
    # -i` names them: the STAPM / slow PPT time constants, the APU-only slow
    # PPT, the skin-temperature (STT) limits STAPM is built on, the boost
    # setpoint, and power, clocks and temperatures. Each one optional: a getter
    # this libryzenadj lacks, or a NaN (not decoded for this table version),
    # is left out. Units as libryzenadj gives them.
    EXTRAS = {
        "stapm_time": "get_stapm_time", "slow_time": "get_slow_time",
        "apu_slow_v": "get_apu_slow_value", "apu_slow_l": "get_apu_slow_limit",
        "stt_apu_v": "get_apu_skin_temp_value",
        "stt_apu_l": "get_apu_skin_temp_limit",
        "stt_dgpu_v": "get_dgpu_skin_temp_value",
        "stt_dgpu_l": "get_dgpu_skin_temp_limit",
        "cclk_setpoint": "get_cclk_setpoint", "cclk_busy": "get_cclk_busy_value",
        "socket_w": "get_socket_power", "soc_w": "get_soc_power",
        "gfx_clk": "get_gfx_clk", "gfx_temp": "get_gfx_temp",
        "fclk": "get_fclk", "mclk": "get_mem_clk", "l3_clk": "get_l3_clk",
    }
    CORE_EXTRAS = {"core_clk": "get_core_clk", "core_power": "get_core_power",
                   "core_temp": "get_core_temp"}  # (ry, core index)
    # Set by the limit log, class-wide so a source rebuilt after a resume
    # keeps them: read EXTRAS into each snapshot's "_extra"; and once, copy the
    # raw table into the next snapshot's "_table" as (version, bytes).
    want_extras = False
    want_table = False

    def __init__(self, enabled=True, dll_dir=None):
        self.available = False
        self._lib = None
        self._ry = None
        self._pairs = {}
        self._extras = {}
        self._core_fns = {}
        self._table_fns = None
        self._ncores = psutil.cpu_count(logical=False) or 0
        self._tctl_moved = False  # two different Tctl readings seen
        self._tctl_last = None
        self._tctl_repeats = 0
        self._tctl_since = 0.0    # when Tctl last changed (monotonic)
        self._tctl_stuck_logged = False
        if not enabled:
            return
        d = find_ryzenadj_dir(dll_dir)
        if d is None:
            print("libryzenadj.dll not found (install the `ryzenadj` tool or "
                  "pass --ryzenadj-dir); authoritative APU STAPM/PPT limit "
                  "detection disabled -- falling back to the LHM heuristic.",
                  file=sys.stderr)
            return
        try:
            from ctypes import cdll, c_void_p, c_float, c_int

            # WinRing0x64.dll / inpoutx64.dll sit beside the lib; the ring0
            # driver (.sys) is loaded relative to the host executable, so make
            # sure a copy is next to it (mirrors ryzenadj's pmtable-example.py).
            os.add_dll_directory(d)
            self._ensure_driver(d)
            lib = cdll.LoadLibrary(os.path.join(d, RYZENADJ_DLL))

            lib.init_ryzenadj.restype = c_void_p
            lib.cleanup_ryzenadj.argtypes = [c_void_p]
            lib.init_table.argtypes = [c_void_p]
            lib.init_table.restype = c_int
            lib.refresh_table.argtypes = [c_void_p]
            lib.refresh_table.restype = c_int
            # STAPM/PPT are required; the rest are read when this libryzenadj
            # has them (a pair whose getters are missing is simply not read).
            self._pairs = {}
            for key, (vname, lname) in self.PAIRS.items():
                fns = [getattr(lib, n, None) for n in (vname, lname)]
                if None in fns:
                    if key in ("stapm", "fast", "slow"):
                        raise AttributeError(f"{vname}/{lname} missing")
                    continue
                for fn in fns:
                    fn.restype = c_float
                    fn.argtypes = [c_void_p]
                self._pairs[key] = fns
            self._bind_extras(lib)

            ry = lib.init_ryzenadj()
            if not ry:
                print("init_ryzenadj failed (elevated? supported CPU?); "
                      "authoritative APU STAPM/PPT limit detection disabled.",
                      file=sys.stderr)
                return
            self._lib = lib
            self._ry = ry
            # init_table maps the SMU power table; some libryzenadj versions
            # return nonzero here yet still work, so verify via a real refresh
            # rather than trusting its result.
            try:
                lib.init_table(ry)
            except Exception:
                pass
            if lib.refresh_table(ry) != 0:
                print("ryzenadj refresh_table failed; authoritative APU "
                      "STAPM/PPT limit detection disabled.", file=sys.stderr)
                self.close()
                return
            self.available = True
        except Exception as err:
            print(f"libryzenadj init failed ({err}); authoritative APU "
                  f"STAPM/PPT limit detection disabled.", file=sys.stderr)
            self.close()

    def _bind_extras(self, lib):
        """Set up the optional EXTRAS / CORE_EXTRAS / raw-table getters."""
        from ctypes import c_void_p, c_float, c_int, c_size_t, c_uint32, POINTER
        for key, name in self.EXTRAS.items():
            fn = getattr(lib, name, None)
            if fn is not None:
                fn.restype = c_float
                fn.argtypes = [c_void_p]
                self._extras[key] = fn
        for key, name in self.CORE_EXTRAS.items():
            fn = getattr(lib, name, None)
            if fn is not None:
                fn.restype = c_float
                fn.argtypes = [c_void_p, c_int]
                self._core_fns[key] = fn
        values, size, ver = (getattr(lib, n, None) for n in
                             ("get_table_values", "get_table_size", "get_table_ver"))
        if values is not None and size is not None:
            values.restype = POINTER(c_float)
            values.argtypes = [c_void_p]
            size.restype = c_size_t
            size.argtypes = [c_void_p]
            if ver is not None:
                ver.restype = c_uint32
                ver.argtypes = [c_void_p]
            self._table_fns = (values, size, ver)

    @staticmethod
    def _num(v):
        """A getter's float, or None for NaN / nonsense."""
        return round(float(v), 3) if v == v and -1e7 < v < 1e7 else None

    def _read_extras(self, ry):
        """EXTRAS (+ per-core lists) from the table refresh_table just read.
        Same thread, right after the refresh: the getters read the table
        buffer, which the next refresh overwrites."""
        ex = {}
        for key, fn in self._extras.items():
            v = self._num(fn(ry))
            if v is not None:
                ex[key] = v
        for key, fn in self._core_fns.items():
            vals = [self._num(fn(ry, i)) for i in range(self._ncores)]
            if any(v is not None for v in vals):
                ex[key] = vals
        return ex

    def _read_table(self, ry):
        """(table version, raw bytes) of the PM table, copied out at once."""
        import ctypes
        values, size, ver = self._table_fns
        n = int(size(ry))
        ptr = values(ry)
        if not ptr or not 0 < n <= 0x10000:
            return None
        data = ctypes.string_at(ctypes.cast(ptr, ctypes.c_void_p), n)
        return (int(ver(ry)) if ver is not None else None), data

    @staticmethod
    def _ensure_driver(dll_dir):
        """Best-effort: place WinRing0x64.sys next to the host executable.

        WinRing0 loads its kernel driver from a path relative to the process
        image. Copy the .sys there if it's missing (if the copy can't happen,
        init just fails and the caller falls back).

        Frozen only. The target is the *process image* directory, the
        bundle's exe directory: in the installed layout that makes this a
        no-op by design -- paths.vendor_dir() already is the exe's directory,
        so src == dst, the installer's copy is in the right place, and the
        service never writes into Program Files -- and in a dev build's dist\\
        it puts the driver where the exe will look.

        A source run's process image is the base Python interpreter (a venv's
        python.exe is only a launcher; see paths.process_image_dir), whose
        directory is a shared Python installation: no place to leave a kernel
        driver that nothing would update or remove. ryzenadj doesn't open from
        a source run past that point anyway, so test it with the frozen build.
        """
        dst = os.path.join(paths.process_image_dir(), RYZENADJ_DRIVER)
        if not paths.is_frozen():
            if not os.path.isfile(dst) and not RyzenAdjSource._driver_noted:
                RyzenAdjSource._driver_noted = True  # once, not per resume
                print(f"Source run: ryzenadj would need {dst} (beside the base "
                      f"interpreter), and a driver is not copied into a Python "
                      f"installation; test the SMU path with the frozen build.",
                      file=sys.stderr)
            return
        try:
            import shutil
            src = os.path.join(dll_dir, RYZENADJ_DRIVER)
            if os.path.isfile(src) and not os.path.isfile(dst):
                shutil.copyfile(src, dst)
        except OSError:
            pass

    def snapshot(self):
        """Return {'stapm': (value, limit), 'fast': .., ..., 'tctl': C or None}.

        One (value, limit) pair per PAIRS key this libryzenadj can read: watts
        for STAPM/PPT, amps for TDC/EDC, degrees C for THM. Pairs whose limit
        is non-positive or NaN (unsupported on this CPU) are dropped so they
        can't read as a binding limit. Returns None on a refresh failure
        (caller falls back). One SMU refresh, so it sits behind the snapshot
        TTL cache.
        """
        if not self.available:
            return None
        ry = self._ry
        extra = table = None
        try:
            if self._lib.refresh_table(ry) != 0:
                return None
            raw = {key: (fv(ry), fl(ry)) for key, (fv, fl) in self._pairs.items()}
            if RyzenAdjSource.want_extras:
                extra = self._read_extras(ry)
            if RyzenAdjSource.want_table and self._table_fns:
                RyzenAdjSource.want_table = False
                table = self._read_table(ry)
        except Exception:
            return None
        out = {}
        for key, (val, lim) in raw.items():
            # x == x is False only for NaN (unsupported sensor); a 0/negative
            # limit means "not reported" and must not count as binding.
            if lim == lim and val == val and lim > 0:
                out[key] = (float(val), float(lim))
        if extra:
            out["_extra"] = extra
        if table:
            out["_table"] = table
        tctl = out["thm"][0] if "thm" in out else None
        out["tctl"] = self._check_stuck(tctl if tctl and tctl > 0 else None)
        if out["tctl"] is None or not self._tctl_moved:
            # a dead temperature must not read as at its limit; nor one not
            # yet seen to move (a dead one reads as live for STUCK_READS)
            out.pop("thm", None)
        return out

    # A Tctl that never changes is not a temperature. The temperature used to
    # be read with get_tctl_temp, which is libryzenadj's THM *limit* getter
    # (the "THM LIMIT CORE" line of `ryzenadj -i`): on a Ryzen AI 9 HX 370 it
    # read a flat 100.0 while the die swung 42->59 C, which looked like a dead
    # table field. The value getter (get_tctl_temp_value, "THM VALUE CORE") is
    # live and what is read now; this check stays as a guard for a table
    # layout libryzenadj gets wrong on some other CPU.
    #
    # Left undetected this is the worst possible failure: 100.0 is above
    # TEMP_CRITICAL_C, so a dead field renders as a permanent maximum-severity
    # thermal alarm. A live sensor jitters (Tctl has 0.125 C granularity and a
    # control loop behind it), so a bit-identical reading means the slot is not
    # being written at all -- but how long counts depends on what is known:
    #
    #   * never seen to move: STUCK_READS repeats (about 6 s at TTL_RYZENADJ
    #     1 s). A dead field never moves, and this keeps its false alarm short,
    #     at start and after every resume (which opens a fresh source).
    #   * seen to move: also STUCK_S of the same value. A live idle die can hold
    #     one 0.125 C step for several seconds; six reads would call it dead,
    #     and the temperature would flip to the frozen ACPI zone and back.
    #   * at or above TEMP_CRITICAL_C: STUCK_READS again, moved or not: a
    #     frozen value there is the full, blinking alarm, the case not to
    #     leave up for a minute. The cost: a die the SMU holds at
    #     its THM limit on one exact value for that long is taken for dead too
    #     (temperature falls back, the THM limit isn't named) -- rarely, as a
    #     die that hot is under load and its reading moves.
    #
    # A live sensor that happens to sit still right at start can read as dead
    # until its first move; that falls back for those seconds, nothing worse.
    STUCK_READS = 5
    STUCK_S = 60.0

    _driver_noted = False  # the source-run driver note was printed

    @staticmethod
    def temperature(snap):
        """Tctl die temperature from a snapshot, or None."""
        return snap.get("tctl") if snap else None

    def _check_stuck(self, tctl, now=None):
        """Return tctl, or None once it has proven to be a dead constant."""
        if now is None:
            now = time.monotonic()
        if tctl is None:
            # A missing read (the pair dropped as NaN, say) is no evidence
            # either way: keep what is known, or a dead constant would come
            # back as live after it.
            return None
        if tctl == self._tctl_last:
            self._tctl_repeats += 1
        else:
            self._tctl_moved |= self._tctl_last is not None
            self._tctl_repeats = 0
            self._tctl_last = tctl
            self._tctl_since = now
        patient = self._tctl_moved and tctl < TEMP_CRITICAL_C
        if (self._tctl_repeats < self.STUCK_READS
                or (patient and now - self._tctl_since < self.STUCK_S)):
            return tctl
        # Once per source: a sensor flipping between stuck and moving would
        # otherwise log on every flip.
        if not self._tctl_stuck_logged:
            self._tctl_stuck_logged = True
            log(f"ryzenadj Tctl is stuck at {tctl:.1f} C for "
                f"{now - self._tctl_since:.0f} s; ignoring it for "
                f"temperature while it stays so")
        return None

    def close(self):
        try:
            if self._lib is not None and self._ry:
                self._lib.cleanup_ryzenadj(self._ry)
        except Exception:
            pass
        self._lib = None
        self._ry = None
        self.available = False


# ---------------------------------------------------------------------------
# Limit reasons: which limit holds a component back, held steady, and when to
# say so on the pad
# ---------------------------------------------------------------------------

# SMU table key -> (LIMIT_KINDS group, unit of its value and limit)
APU_SIGNALS = {
    "stapm": ("power", "W"), "slow": ("power", "W"), "fast": ("power", "W"),
    "tdc": ("current", "A"), "edc": ("current", "A"),
    "tdc_soc": ("current", "A"), "edc_soc": ("current", "A"),
    "thm": ("thermal", "C"),
}


def apu_binding(snap):
    """{key: Reason} for every SMU limit at its threshold in a ryzenadj
    snapshot, whatever LIMIT_KINDS says: a power or current limit when its
    value reaches APU_LIMIT_NEAR of the limit, THM when Tctl is within
    APU_THM_MARGIN_C of it. The message shows the limit (STAPM's moves: the
    SMU lowers it as the chassis warms)."""
    binding = {}
    for key, (kind, unit) in APU_SIGNALS.items():
        pair = snap.get(key)
        if not pair:
            continue
        val, lim = pair
        if kind == "thermal":
            hit = val >= lim - APU_THM_MARGIN_C
        else:
            hit = val >= APU_LIMIT_NEAR * lim
        if hit:
            binding[key] = Reason(key, kind, val / lim, lim, unit)
    return binding


def apu_reasons(snap, on_ac, weak_charger):
    """{key: Reason} for the SMU limits that bind in a ryzenadj snapshot and
    count: those of apu_binding(), power limits on AC only and only those in
    APU_LIMIT_SIGNALS. While the battery assists an overloaded charger, a
    binding power limit is the charger's doing (the EC lowered it):
    "charger", as it always was.
    """
    binding = apu_binding(snap)
    capped = any(k in APU_LIMIT_SIGNALS for k in binding)
    if weak_charger and capped and "charger" in LIMIT_KINDS:
        return {"charger": Reason("charger", "charger")}
    return {k: r for k, r in binding.items()
            if r.kind in LIMIT_KINDS
            and (r.kind != "power" or (on_ac and k in APU_LIMIT_SIGNALS))}


class CpuClock:
    """Is the CPU held at its base clock while it has work?

    The SMU limits cover power, current and temperature, but a CPU can also be
    held at (or under) its base clock with none of them near: boost switched
    off. Measured on this FW16 running from a 29 W USB-C source (a monitor's
    port), with the battery making up the rest: every core of the Ryzen AI 9
    HX 370 stayed at 2.0 GHz under an all-core load -- 27 W, the slow PPT at
    90% -- and even a single busy thread never passed 2.02 GHz, in every
    Windows power mode. Nothing in the SMU table binds, so without this the
    CPU row showed nothing at all.

    Windows reports each logical CPU's clock relative to base (% Processor
    Performance, measured while it runs) and how busy it is (% Processor
    Time). A core with work and boost available runs well above base, so the
    CPU counts as held back when a core is busy (its threads' time summed: a
    single thread hops between a core's two threads) yet no busy logical CPU
    runs faster than CLOCK_BASE_MARGIN times base, CLOCK_DEBOUNCE_READS reads
    in a row.
    """

    RETRY_S = 30.0       # while the counters can't be opened
    ACTIVE_PCT = 20      # a logical CPU this busy shows how fast it can run

    def __init__(self):
        self.query = None
        self._next_open = 0.0
        self._failed = False
        self._reads = 0  # consecutive reads that found the CPU held back
        self.stats = None  # the last read's detail, for the limit log
        logical = psutil.cpu_count(logical=True) or 0
        physical = psutil.cpu_count(logical=False) or 0
        self._smt = bool(physical) and logical == 2 * physical
        if win32pdh is not None:
            self._open()

    def _open(self):
        self._next_open = time.monotonic() + self.RETRY_S
        query = None
        try:
            query = win32pdh.OpenQuery()
            self._time = win32pdh.AddEnglishCounter(
                query, r"\Processor Information(*)\% Processor Time")
            self._perf = win32pdh.AddEnglishCounter(
                query, r"\Processor Information(*)\% Processor Performance")
            self._base = win32pdh.AddEnglishCounter(
                query, r"\Processor Information(_Total)\Processor Frequency")
            win32pdh.CollectQueryData(query)  # prime: rates need two samples
            self.query = query
        except Exception as err:
            if query is not None:
                try:
                    win32pdh.CloseQuery(query)
                except Exception:
                    pass
            if not self._failed:
                log(f"CPU clock counters unavailable (will retry): {err}")
                self._failed = True

    def read(self):
        """(held, ghz, busy): held once CLOCK_DEBOUNCE_READS reads in a row
        found the CPU held back; ghz the busy CPUs' fastest clock (None:
        unknown), busy the busiest core's %. None when the counters can't be
        read."""
        if self.query is None:
            if win32pdh is not None and time.monotonic() >= self._next_open:
                self._open()
            return None
        try:
            win32pdh.CollectQueryData(self.query)
            busy_t = win32pdh.GetFormattedCounterArray(self._time,
                                                       win32pdh.PDH_FMT_DOUBLE)
            perf = win32pdh.GetFormattedCounterArray(self._perf,
                                                     win32pdh.PDH_FMT_DOUBLE)
            base = win32pdh.GetFormattedCounterValue(self._base,
                                                     win32pdh.PDH_FMT_DOUBLE)[1]
        except Exception:
            self._reads = 0
            self.stats = None
            return None
        cores = {}
        fastest = None
        active = []  # % Processor Performance of the busy logical CPUs
        for name, t in busy_t.items():
            m = re.fullmatch(r"(\d+),(\d+)", name)  # not "_Total", "0,_Total"
            if not m:
                continue
            group, num = int(m.group(1)), int(m.group(2))
            core = (group, num // 2 if self._smt else num)
            cores[core] = cores.get(core, 0.0) + t
            p = perf.get(name)
            if t >= self.ACTIVE_PCT and p is not None:
                fastest = p if fastest is None else max(fastest, p)
                active.append(p)
        busiest = min(100.0, max(cores.values(), default=0.0))
        held = (busiest >= CLOCK_BUSY_PCT and fastest is not None
                and fastest <= 100.0 * CLOCK_BASE_MARGIN)
        self._reads = self._reads + 1 if held else 0
        ghz = base * fastest / 100000.0 if (fastest and base > 0) else None
        self.stats = {
            "ghz_avg": (base * sum(active) / len(active) / 100000.0
                        if active and base > 0 else None),
            "busy_cores": sum(1 for v in cores.values() if v >= CLOCK_BUSY_PCT),
            "base_ghz": base / 1000.0 if base > 0 else None,
        }
        return self._reads >= CLOCK_DEBOUNCE_READS, ghz, busiest


def clock_reasons(clock, on_ac, weak_charger):
    """{key: Reason} for a CPU held at its base clock (CpuClock.read()), for
    when nothing else explains it: the caller asks only when no SMU limit is
    at its threshold and no other limit binds. On AC only (on battery a low
    clock is the power plan's choice). While the battery assists an
    overloaded charger, the charger is the likely cause (boost was off on the
    29 W source above): "charger", the slow breathe."""
    if not (clock and clock[0] and on_ac):
        return {}
    if weak_charger and "charger" in LIMIT_KINDS:
        return {"charger": Reason("charger", "charger")}
    if "clock" in LIMIT_KINDS:
        return {"clock": Reason("clock", "clock", 1.0, clock[1], "GHZ")}
    return {}


class LimitTracker:
    """One component's limit: which one binds, held through flicker, and when
    the pad should name it.

    A capped value hovers around the threshold, so a limit counts as binding
    until it has been clear for LIMIT_RELEASE_S. Of several, the one named
    stays named while it holds; when it lets go the closest of the rest takes
    over. Messages follow episodes: one once the limit has held for
    MSG_ONSET_S, another only when the reason changes (and MSG_MIN_GAP_S has
    passed since the last), and a new episode only after MSG_REARM_S clear.
    """

    def __init__(self, name_const, hue_const):
        self.name_const = name_const  # constant names: the INI rebinds them
        self.hue_const = hue_const
        self.seen = {}        # key -> (time last binding, Reason)
        self.current = None   # key of the limit named
        self.live = False     # ...and it binds this tick (not just held)
        self.since = 0.0      # when the component became limited
        self.clear_at = 0.0   # when it stopped
        self.announced = None # key named last in this episode
        self.last_msg = None  # when a message last named this component
        self.pending = 0      # messages about it queued or playing, not yet shown
        self.episode = 0      # counts the episodes that ended
        self.ended = False    # ...and this one has (until the next limit)

    @property
    def reason(self):
        return self.seen[self.current][1] if self.current else None

    @property
    def limited(self):
        return self.current is not None

    def update(self, binding, now):
        for key, r in binding.items():
            self.seen[key] = (now, r)
        self.seen = {k: v for k, v in self.seen.items()
                     if now - v[0] <= LIMIT_RELEASE_S}
        was = self.limited
        if self.current not in self.seen:
            pool = binding or {k: v[1] for k, v in self.seen.items()}
            self.current = (max(pool, key=lambda k: pool[k].ratio)
                            if pool else None)
        self.live = self.current in binding  # binds this tick, not just held
        if self.limited and not was:
            self.since = now
        elif was and not self.limited:
            self.clear_at = now
        if (not self.limited and not self.ended
                and now - self.clear_at >= MSG_REARM_S):
            self.announced = None  # the episode is over
            self.episode += 1      # (a message of it still pending names nothing)
            self.ended = True
        if self.limited:
            self.ended = False

    def due(self, now):
        """True when the pad should name the limit now.

        The limit must bind on this very tick too: the release hold keeps one
        stray read "limited" for LIMIT_RELEASE_S, which must not be enough to
        scroll a message MSG_ONSET_S later. Kinds outside MSG_KINDS (the
        charger, by default) only breathe. Not while a message about this
        component waits to be sent or plays.
        """
        return (not self.pending and self.live
                and self.reason.kind in MSG_KINDS
                and now - self.since >= MSG_ONSET_S
                and self.current != self.announced
                and (self.last_msg is None
                     or now - self.last_msg >= MSG_MIN_GAP_S))

    # A message counts as naming its limit only once the pad has played it.
    # Until then it is pending: due() holds off another, and one that never
    # plays (dropped as the pad went dark, lost with the pad's power) has
    # named nothing, so its limit is named again.
    def queued(self):
        """A message naming the current limit is queued; returns what shown()
        needs: the limit's key, and the episode it belongs to."""
        self.pending += 1
        return self.current, self.episode

    def shown(self, key, now):
        """The pad played the message `key` (from queued()) names. One from
        an episode that has ended since doesn't count against the next."""
        self.pending = max(self.pending - 1, 0)
        self.last_msg = now
        limit, episode = key
        if episode == self.episode:
            self.announced = limit

    def lost(self, sent_at=None):
        """A queued message will never play: it named nothing. One that was
        sent still counts toward MSG_MIN_GAP_S, so a limit whose messages keep
        being replaced (another client's text) isn't re-sent in a tight loop."""
        self.pending = max(self.pending - 1, 0)
        if sent_at is not None:
            self.last_msg = sent_at

    def still(self, key):
        """Does the limit `key` (from queued()) still hold, in its episode?"""
        limit, episode = key
        return episode == self.episode and self.limited and self.current == limit

    def message(self):
        """(text, hue, saturation) naming the limit, e.g. "CPU STAPM 28W"."""
        r = self.reason
        text = f"{globals()[self.name_const]} {r.word()}"
        if MSG_SHOW_VALUE and r.value is not None:
            if r.unit == "GHZ":  # 2.0 GHz -> "2GHZ", 2.6 GHz -> "2.6GHZ"
                num = f"{r.value:.1f}".rstrip("0").rstrip(".")
            else:
                num = str(int(r.value + 0.5))
            text += f" {num}{r.unit}"
        hue = HUE_AMBER if r.key == "charger" else globals()[self.hue_const]
        return text[:MSG_TEXT_MAX], hue, 255


class _Inflight:
    """The message sent last, until the pad says what became of it."""
    __slots__ = ("tracker", "key", "seq", "sent_at", "armed")

    def __init__(self, tracker, key, seq, sent_at):
        self.tracker = tracker
        self.key = key
        self.seq = seq
        self.sent_at = sent_at
        self.armed = False  # its own echo is back: echoes after it count


class Messenger:
    """Sends limit messages to the pad, one at a time.

    Learns from a probe whether the firmware can show them at all, and from
    each FRAME B echo whether one is still waiting or playing, whether the
    replay key was pressed, and whether the pad is dark. Echoes reach every
    open handle, so the preview tool's own messages hold this one back too.

    A limit counts as named once the pad has played its message
    (LimitTracker.shown). Each message carries a sequence number, and the
    FRAME B echo reports the last one the pad played to its end and the last
    one it dropped unplayed (replaced, dropped as it went dark or the host
    went away): the message in flight is settled by what the pad says, not
    guessed from timing. The next is sent only once it is settled.

    Only an echo that came after the message's own echo counts (echoes come
    in order): one sent before it carries the pad's state before it, where a
    number can still be reported from an earlier message -- a restarted host
    numbers from 1 again. Once armed, an echo with the pad idle that reports
    neither outcome means the pad holds no such message: it lost power.

    Firmware without the numbers (its probe echo lacks MSG_SEQ_ACK) keeps the
    old rule: a limit is named as its message is sent.
    """

    SETTLE_S = 1.2    # after sending, until the echoes surely tell its state
    RESOLVE_S = 90.0  # a message the pad never reports on, and isn't busy
                      # with, is taken as lost after this long (its echo
                      # went missing); longer than any message takes to play

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.queue = []       # ((text, hue, sat), tracker or None, key, replay)
        self.inflight = None  # _Inflight: the message sent last
        self.seq = 0          # the last sequence number used
        self.lights_off = False  # the lighting switched off; kept across
                                 # reconnects, so a still-dark pad isn't re-logged
        self.on_sent = None   # called with (text, replay) per message sent
        self.reset()

    def reset(self):
        # A (re)connect: what waited to be sent never will. The message in
        # flight stays, armed -- every echo on the new handle comes after the
        # pad got it (or after it lost power with it, which reads as lost): a
        # pad that kept its power still reports what became of it.
        self._drop_queue()
        if self.inflight is not None:
            self.inflight.armed = True
        self.capable = False
        self.seq_capable = False  # the pad reports message outcomes
        self.busy = False
        self.asked = False
        self.dark = False         # nothing shows: lighting off, or suspended
        self.sent_at = -1e9

    def probe(self, dev):
        """Ask (empty text) whether the firmware shows messages; see on_echo."""
        self._write(dev, "", 0, 0)

    def on_echo(self, rep):
        if len(rep) < REPORT_LEN or rep[1] != SYSMON_CHANNEL:
            return
        if rep[2] == SYSMON_VALUE_MESSAGE and rep[MSG_ACK_OFFSET] == MSG_ACK:
            if not self.capable and self.verbose:
                print("firmware shows limit messages")
            self.capable = True
            if not rep[MSG_TEXT_OFFSET]:  # the probe's echo (empty text)
                self.seq_capable = rep[MSG_SEQ_PROBE_OFFSET] == MSG_SEQ_ACK
                if not self.seq_capable and self.inflight is not None:
                    # reconnected to firmware that can't report on it
                    self._settle(played=False)
            if self.inflight is not None and rep[MSG_SEQ_OFFSET] == self.inflight.seq:
                self.inflight.armed = True  # its own echo: what follows is after it
        elif rep[2] == SYSMON_VALUE_FRAME_B:
            self.busy = bool(rep[STATUS_OFFSET] & STATUS_MSG)
            lights_off = bool(rep[STATUS_OFFSET] & STATUS_DARK)
            if lights_off != self.lights_off:
                log(LIGHTING_OFF_NOTE if lights_off else
                    "The macropad's RGB lighting is back on")
            self.lights_off = lights_off
            self.dark = bool(rep[STATUS_OFFSET] & (STATUS_DARK | STATUS_SUSPENDED))
            if self.seq_capable and self.inflight is not None and self.inflight.armed:
                seq = self.inflight.seq
                if rep[MSG_PLAYED_OFFSET] == seq:
                    self._settle(played=True)
                elif rep[MSG_DROPPED_OFFSET] == seq or not self.busy:
                    # dropped -- or the pad, idle, holds nothing and reports
                    # nothing of it: it lost power (replugged, rebooted)
                    self._settle(played=False)
            if rep[STATUS_OFFSET] & STATUS_ASK:
                self.asked = True

    def tick(self, dev, now, trackers):
        if not self.capable:
            return
        if (self.inflight is not None and not self.busy
                and now - self.inflight.sent_at >= self.RESOLVE_S):
            # never reported on, and the pad holds no message (one waiting
            # there long -- behind a preview playlist, say -- may yet play)
            self._settle(played=False)
        if self.dark:
            # Nothing can show while the pad is dark (it drops, and reports,
            # what it held). Queueing anyway would replay a backlog of stale
            # limits once it is lit again: what waits is dropped, and named
            # afresh then if it still binds.
            self._drop_queue()
            self.asked = False
            return
        if self.asked:
            # the replay key: what binds now, or that nothing does
            self.asked = False
            self._drop_queue()
            for t in trackers:
                if t.limited:
                    self.queue.append((t.message(), t, t.queued(), True))
            if not self.queue:
                self.queue.append(((MSG_NO_LIMIT, 0, 0), None, None, True))
        elif MSG_ENABLE:
            for t in trackers:
                if t.due(now):
                    self.queue.append((t.message(), t, t.queued(), False))
        while (self.queue and self.inflight is None and not self.busy
                and now - self.sent_at >= self.SETTLE_S):
            msg, tracker, key, replay = self.queue.pop(0)
            if tracker is not None and not tracker.still(key):
                tracker.lost()
                if not (replay and tracker.limited):
                    continue  # its limit cleared while it waited: stale
                # the replay key asked what binds: say what binds now
                msg, key = tracker.message(), tracker.queued()
            text, hue, sat = msg
            seq = 0
            if self.seq_capable:
                self.seq = self.seq % 255 + 1  # 1..255; 0 means none
                seq = self.seq
            try:
                self._write(dev, text, hue, sat, seq)
            except Exception:
                # it never reached the pad (the run loop reconnects): it
                # named nothing, or the tracker would wait on it for good
                if tracker is not None:
                    tracker.lost()
                raise
            self.sent_at = now
            if tracker is not None:
                if self.seq_capable:
                    self.inflight = _Inflight(tracker, key, seq, now)
                else:
                    tracker.shown(key, now)  # older firmware: named as sent
            # into sysmon.log: the only record of what the pad was told
            log(f"Message to the pad: {text}"
                + (" (replay key)" if replay else ""))
            if self.on_sent is not None:
                self.on_sent(text, replay)  # the limit log; never raises
            break  # one per tick

    def _drop_queue(self):
        """Drop what waits to be sent: none of it will name anything."""
        for entry in self.queue:
            if entry[1] is not None:
                entry[1].lost()
        self.queue = []

    def _settle(self, played):
        """The message in flight has played (as of when it was sent: the gap
        between messages counts from their starts), or never will."""
        if self.inflight is not None:
            m = self.inflight
            if played:
                m.tracker.shown(m.key, m.sent_at)
            else:
                m.tracker.lost(m.sent_at)
            self.inflight = None

    @staticmethod
    def _write(dev, text, hue, sat, seq=0):
        rep = bytearray(REPORT_LEN)
        rep[0] = VIA_CMD_CUSTOM_SET_VALUE
        rep[1] = SYSMON_CHANNEL
        rep[2] = SYSMON_VALUE_MESSAGE
        rep[3] = 0  # ticker
        rep[4] = clamp8(hue)
        rep[5] = clamp8(sat)
        rep[6] = clamp(MSG_SPEED, 0, 255)
        rep[MSG_SEQ_OFFSET] = seq  # the pad reports it back as played or dropped
        raw = text.encode("ascii", "replace")[:MSG_TEXT_MAX]
        rep[MSG_TEXT_OFFSET:MSG_TEXT_OFFSET + len(raw)] = raw
        dev.write(b"\x00" + bytes(rep))


# ---------------------------------------------------------------------------
# Descriptor builder + per-row render policy
# ---------------------------------------------------------------------------

class RowDesc:
    """An 8-byte per-row descriptor matching the frozen wire layout.

    Fields default to "off" / "no effect"; the row policy functions set only
    what they need. pack() emits the bytes in the documented order.

    The ghost_* fields are the row's optional FRAME C ghost bar (a dimmer
    second bar behind the main one); pack_ghost() emits its 4 bytes.
    """

    def __init__(self, level=0, row_val=ROW_VAL_FULL, hue0=0, hue1=0,
                 gradient=False, envelope=ENV_NONE,
                 overlay_shape=OVL_NONE, overlay_dir=OVL_DIR_UP,
                 overlay_count=0, period=0, floor=0,
                 ghost_level=0, ghost_hue=0, ghost_val=0, ghost_right=False):
        self.level = level
        self.row_val = row_val
        self.hue0 = hue0
        self.hue1 = hue1
        self.gradient = gradient
        self.envelope = envelope
        self.overlay_shape = overlay_shape
        self.overlay_dir = overlay_dir
        self.overlay_count = overlay_count
        self.period = period
        self.floor = floor
        self.ghost_level = ghost_level
        self.ghost_hue = ghost_hue
        self.ghost_val = ghost_val
        self.ghost_right = ghost_right

    def flags_byte(self):
        f = 0
        if self.gradient:
            f |= FLAG_GRADIENT
        f |= (self.envelope & 0x3) << ENV_SHIFT
        f |= (self.overlay_shape & 0x3) << OVL_SHIFT
        f |= (self.overlay_dir & 0x1) << OVL_DIR_SHIFT
        return f & 0xFF

    def pack(self):
        return bytes((
            clamp8(self.level),
            clamp8(self.row_val),
            clamp8(self.hue0),
            clamp8(self.hue1),
            self.flags_byte(),
            clamp8(self.overlay_count),
            clamp8(self.period),
            clamp8(self.floor),
        ))

    def pack_ghost(self):
        return bytes((
            clamp8(self.ghost_level),
            clamp8(self.ghost_hue),
            clamp8(self.ghost_val),
            GHOST_FLAG_RIGHT if self.ghost_right else 0,
        ))

    def __repr__(self):
        env = {ENV_NONE: "none", ENV_BLINK: "blink", ENV_BREATHE: "breathe"}[self.envelope]
        ovl = {OVL_NONE: "none", OVL_TRAP_RISE: "rise",
               OVL_TRAP_FALL: "fall", OVL_SINE: "sine"}[self.overlay_shape]
        grad = f" grad->{self.hue1}" if self.gradient else ""
        ovls = "" if self.overlay_shape == OVL_NONE else (
            f" ovl={ovl}x{self.overlay_count}"
            f"{'down' if self.overlay_dir else 'up'}")
        envs = "" if self.envelope == ENV_NONE else f" env={env}@{self.period}"
        flr = f" floor={self.floor}" if (self.envelope == ENV_BREATHE
                                         or self.overlay_shape == OVL_SINE) else ""
        ghost = (f" ghost={self.ghost_level}@hue{self.ghost_hue}/val{self.ghost_val}"
                 f"{' right' if self.ghost_right else ''}"
                 if self.ghost_level else "")
        return (f"lvl={self.level} val={self.row_val} hue={self.hue0}{grad}"
                f"{envs}{ovls}{flr}{ghost}")


def rate_segments(rate_w):
    """Overlay segment count from charge/discharge power: 1 + (>=20)+(>=45)+(>=75)."""
    return 1 + (rate_w >= RATE_SEG_W1) + (rate_w >= RATE_SEG_W2) + (rate_w >= RATE_SEG_W3)


def breathe_period(psrc):
    """Breathe cadence: psrc wins (slow ~4s), else power-cap (fast ~1.5s).

    Only psrc decides the cadence: when both power and psrc are set, psrc wins
    (the supply is the bigger story), mirroring the old firmware behaviour.
    """
    return PERIOD_BREATHE_SLOW if psrc else PERIOD_BREATHE_FAST


def build_battery_row(m):
    """row0: battery level + charge/discharge/idle/critical effects.

    m carries: has_batt, ac, charging, discharging, raw_pct, cap, rate_w.
    """
    if not m["has_batt"]:
        return RowDesc(level=0)

    ac = m["ac"]
    charging = m["charging"]
    discharging = m["discharging"]
    raw_pct = m["raw_pct"]
    rate_w = m["rate_w"]

    # Unplugged always drains.
    if not ac and not charging:
        discharging = True

    # Cap rescale FIRST: charged-to-cap reads as a full (green) bar.
    cap = m["cap"]
    if cap <= 0 or cap > 100:
        cap = 100
    pct = min(100, raw_pct * 100 // cap)

    # Drift fold: on AC, a sub-ASSIST_MIN_W drain is the EC holding the cap,
    # not a real discharge. Treat it as idle; remember it for the faint tell.
    # The tell needs a *measured* drain: the discharging bit alone means little
    # on AC, since the EC keeps it asserted at a flat 0 W while it floats a
    # pack it is done charging, and that is plain idle with nothing to tell.
    drift = False
    if ac and discharging and rate_w < ASSIST_MIN_W:
        discharging = False
        drift = rate_w >= DRIFT_MIN_W

    # Critical (gated on RAW %, not rescaled): full red row blinking ~1Hz.
    if discharging and raw_pct < BATT_CRITICAL_PCT:
        return RowDesc(level=255, row_val=ROW_VAL_FULL, hue0=HUE_RED,
                       envelope=ENV_BLINK, period=PERIOD_BLINK)

    if charging:
        # red->green by charge level; trap-rise overlay grows from the boundary
        # upward, more segments the faster the charge.
        hue = raw_pct_to_charge_hue(pct)
        return RowDesc(level=pct_to_level(pct), row_val=ROW_VAL_FULL, hue0=hue,
                       overlay_shape=OVL_TRAP_RISE, overlay_dir=OVL_DIR_UP,
                       overlay_count=rate_segments(rate_w), period=PERIOD_PULSE)

    if discharging:
        # Real discharge: unplugged (red) or AC battery-assist (amber).
        # trap-fall overlay drains from the boundary downward.
        hue = HUE_AMBER if ac else HUE_RED
        return RowDesc(level=pct_to_level(pct), row_val=ROW_VAL_FULL, hue0=hue,
                       overlay_shape=OVL_TRAP_FALL, overlay_dir=OVL_DIR_DOWN,
                       overlay_count=rate_segments(rate_w), period=PERIOD_PULSE)

    # AC idle / held at cap: whole row steady dim green. If this is drift, add
    # a faint sine overlay on the boundary segment (the cap-drift tell).
    desc = RowDesc(level=255, row_val=ROW_VAL_BATT_IDLE, hue0=HUE_GREEN)
    if drift:
        desc.overlay_shape = OVL_SINE
        desc.overlay_dir = OVL_DIR_UP
        desc.overlay_count = 1
        desc.period = PERIOD_SINE
        desc.floor = BATT_DRIFT_FLOOR
    return desc


def raw_pct_to_charge_hue(pct):
    """Charging hue: red (0) at empty -> green (85) at full."""
    return clamp(pct * HUE_CHARGE_FULL // 100, 0, HUE_CHARGE_FULL)


def limit_breathe(desc, reason):
    """Breathe a row held back by a limit (see LIMIT_KINDS)."""
    if reason is not None:
        desc.envelope = ENV_BREATHE
        desc.floor = LIMIT_FLOOR
        desc.period = breathe_period(reason.key == "charger")
    return desc


def build_cpu_row(m):
    """row1: CPU usage, solid red; breathe when an APU limit binds."""
    return limit_breathe(RowDesc(level=pct_to_level(m["cpu"]), hue0=HUE_RED),
                         m.get("cpu_limit"))


def build_mem_row(m):
    """row2: memory usage, solid gold; blink when RAW mem >= 90."""
    desc = RowDesc(level=pct_to_level(m["mem"]), hue0=HUE_GOLD)
    if m["raw_mem"] >= MEM_CRITICAL_PCT:
        desc.envelope = ENV_BLINK
        desc.period = PERIOD_BLINK
    return desc


def build_igpu_row(m):
    """row3: iGPU usage, solid red; NPU usage as a dim cyan ghost bar.

    The NPU sits on the same die as the iGPU and is idle most of the time, so
    it shares this row rather than claiming one: the ghost is invisible while
    the NPU is idle, and shows only where the NPU is busier than the iGPU.
    """
    desc = RowDesc(level=pct_to_level(m["igpu"]), hue0=HUE_RED)
    if m["npu"] is not None:
        desc.ghost_level = pct_to_level(m["npu"])
        desc.ghost_hue = HUE_NPU
        desc.ghost_val = ROW_VAL_NPU_GHOST
    return desc


def dgpu_off_keys(off_s):
    """Keys of the D3cold marker for a streak off_s seconds long (None = none):
    1 at once, one more at the start of each DGPU_OFF_STEP_S, at most 4."""
    if off_s is None:
        return 0
    return min(4, 1 + int(off_s // DGPU_OFF_STEP_S))


def build_dgpu_row(m):
    """row4: dGPU usage, solid green; breathe when a dGPU limit binds.

    While the dGPU is powered off (D3cold) the row glows dim HUE_DGPU_OFF from
    its last key: a right-anchored ghost, so it glides in and out and stays
    clear of the bar. One key at once, then one more per DGPU_OFF_STEP_S the
    GPU stays off without a break (dgpu_off_keys). Not tied to dgpu_present:
    finding the device in D3cold is proof enough that there is one. It needs
    firmware that reads the ghost flags (m["ghost_flags"]); without them it
    would show at the left end.
    """
    if m["dgpu_present"]:
        desc = limit_breathe(RowDesc(level=pct_to_level(m["dgpu"]), hue0=HUE_GREEN),
                             m.get("dgpu_limit"))
    else:
        desc = RowDesc(level=0)
    if m.get("ghost_flags"):
        desc.ghost_level = GHOST_LEVEL_KEYS[dgpu_off_keys(m["dgpu_off_s"])]
        desc.ghost_hue = HUE_DGPU_OFF
        desc.ghost_val = ROW_VAL_DGPU_OFF
        desc.ghost_right = True
    return desc


def build_temp_row(m):
    """row5: temperature bar, green->red gradient; blink when RAW temp > 90."""
    if not m["has_temp"]:
        return RowDesc(level=0)
    t = m["temp_c"]
    # TEMP_MIN_C < TEMP_MAX_C holds: config.py reverts a pair that isn't
    # (config.ORDERED_PAIRS), so the division is safe.
    tc = max(TEMP_MIN_C, min(TEMP_MAX_C, t))
    level = clamp((tc - TEMP_MIN_C) * 255 // (TEMP_MAX_C - TEMP_MIN_C), 0, 255)
    desc = RowDesc(level=level, hue0=HUE_TEMP_LO, hue1=HUE_TEMP_HI, gradient=True)
    if m["raw_temp"] > TEMP_CRITICAL_C:
        # bar still shows its level, but the whole row blinks as an alert
        desc.envelope = ENV_BLINK
        desc.period = PERIOD_BLINK
    return desc


ROW_BUILDERS = [
    build_battery_row,   # 0
    build_cpu_row,       # 1
    build_mem_row,       # 2
    build_igpu_row,      # 3
    build_dgpu_row,      # 4
    build_temp_row,      # 5
]


def build_frame(rows, brightness, glide_ms=None, timeout_ms=None):
    """Build the three 32-byte reports for one tick.

    rows is the list of six RowDesc objects in fixed row order. Returns
    [frame_a, frame_b, frame_c]; the caller writes each as b"\\x00" + report.
    FRAME C (the ghost bars) is sent every tick, all-zero when no row has a
    ghost: the firmware fades ghosts out when FRAME C goes stale, so a steady
    stream is what keeps a live ghost on screen.

    The two globals resolve from the module constants in the body, not as
    default arguments: default arguments bind once at import, which would
    freeze them before config.py has had a chance to apply the INI.
    """
    assert len(rows) == 6
    if glide_ms is None:
        glide_ms = GLIDE_MS_DEFAULT
    if timeout_ms is None:
        timeout_ms = TIMEOUT_MS_DEFAULT

    frame_a = bytearray(REPORT_LEN)
    frame_a[0] = VIA_CMD_CUSTOM_SET_VALUE
    frame_a[1] = SYSMON_CHANNEL
    frame_a[2] = SYSMON_VALUE_FRAME_A
    off = 3
    for r in rows[0:3]:
        frame_a[off:off + ROW_DESC_LEN] = r.pack()
        off += ROW_DESC_LEN
    # data[27..31] unused/zero (already zero)

    frame_b = bytearray(REPORT_LEN)
    frame_b[0] = VIA_CMD_CUSTOM_SET_VALUE
    frame_b[1] = SYSMON_CHANNEL
    frame_b[2] = SYSMON_VALUE_FRAME_B
    off = 3
    for r in rows[3:6]:
        frame_b[off:off + ROW_DESC_LEN] = r.pack()
        off += ROW_DESC_LEN
    # globals
    frame_b[27] = clamp8(brightness)
    frame_b[28] = clamp(glide_ms / 4, 0, 255)      # 4ms units
    frame_b[29] = clamp(timeout_ms / 100, 0, 255)  # 100ms units

    frame_c = bytearray(REPORT_LEN)
    frame_c[0] = VIA_CMD_CUSTOM_SET_VALUE
    frame_c[1] = SYSMON_CHANNEL
    frame_c[2] = SYSMON_VALUE_FRAME_C
    off = 3
    for r in rows:
        frame_c[off:off + GHOST_DESC_LEN] = r.pack_ghost()
        off += GHOST_DESC_LEN
    # data[27..31] unused/zero

    return [bytes(frame_a), bytes(frame_b), bytes(frame_c)]


def gather(sources, gpu, charge_limit, apu, nv, ryzen, args, cache):
    """Collect metrics and turn them into six row descriptors + globals.

    cache is the dict of Cached wrappers around the slow sources.
    """
    flags_ac = charging = discharging = has_batt = False
    raw_pct = 0
    batt_rate = 0
    batt_cap = 100
    batt = psutil.sensors_battery()
    if batt is not None:
        has_batt = True
        raw_pct = clamp(batt.percent, 0, 100)
        batt_cap = charge_limit.cap()
        flags_ac = bool(batt.power_plugged)
        state = cache["battery"]()
        if state is None:
            # crude fallback: can't tell "charging" from "held at a cap",
            # and the rate is unknown (0 = minimal animation on the pad)
            state = (batt.power_plugged and raw_pct < 100,
                     not batt.power_plugged, 0)
        charging = bool(state[0])
        discharging = bool(state[1])
        batt_rate = state[2]

    cpu = clamp(psutil.cpu_percent(interval=None), 0, 100)
    raw_mem = psutil.virtual_memory().percent
    mem = clamp(raw_mem, 0, 100)

    igpu, dgpu, dgpu_present, npu = gpu.read()

    # dGPU: prefer NVML's device-wide utilization over the PDH 3D-engine sum
    # (see NvGpuLimits.utilization). PDH stays as the fallback and still
    # covers the iGPU, which NVML can't see.
    nv_util = nv.utilization()
    if nv_util is not None:
        dgpu, dgpu_present = nv_util, True

    # One LHM snapshot feeds both temperature and CPU power this tick.
    lhm_temp, cpu_pkg_w, cpu_load = cache["lhm"]()

    # One ryzenadj SMU refresh feeds both the authoritative APU power-limit
    # verdict and an in-process Tctl temperature fallback.
    rj = cache["ryzenadj"]() if ryzen.available else None

    # Temperature: prefer LHM (validated), then the EC via framework_tool, then
    # ryzenadj Tctl, then static ACPI.
    #
    # The EC outranks Tctl on purpose. Tctl is decoded from the AMD SMU power
    # table, whose layout depends on the CPU model, and a wrong decode does not
    # fail -- it returns a plausible-looking number. Observed on a clean Windows
    # install on this very machine: Tctl pinned at exactly 100.0 C while the EC
    # read 41 C, which drove the temperature row to full and blinking. (That
    # was this host reading libryzenadj's THM *limit* getter instead of the
    # value -- fixed, see RyzenAdjSource.PAIRS -- but a different CPU's table
    # can still decode wrong.) The EC sensor is the Framework firmware's own,
    # and is the source this hardware is actually specified around.
    #
    # The cost is a throttled subprocess (TTL_EC_THERMAL) instead of a free
    # in-process read, and it is only paid when LHM is unavailable -- which is
    # exactly the case where Tctl proved untrustworthy.
    temp_c = lhm_temp
    temp_src = "lhm"
    if temp_c is None:
        temp_c, temp_src = cache["ec_temp"](), "ec"
    if temp_c is None:
        temp_c, temp_src = RyzenAdjSource.temperature(rj), "ryzenadj"
    if temp_c is None:
        temp_c, temp_src = cache["acpi_temp"](), "acpi"
    has_temp = temp_c is not None
    if not has_temp:
        temp_src = None
    raw_temp = temp_c if has_temp else 0

    # Brightness -> LED global current value (host-side gamma).
    brightness_pct = cache["brightness"]()
    if brightness_pct is None:
        brightness_pct = args.fallback_brightness
    frac = (brightness_pct / 100.0) ** args.led_gamma
    led_val = clamp(
        args.min_led + (args.max_led - args.min_led) * frac,
        args.min_led, args.max_led)

    # Weak / overloaded charger: battery assisting on AC (a genuine deficit,
    # not the sub-ASSIST_MIN_W cap drift). The only power-source state surfaced
    # on the component rows; it now selects a breathe cadence, not a flag.
    on_ac = flags_ac
    weak_charger = on_ac and discharging and batt_rate >= ASSIST_MIN_W

    # The limits binding this tick (LimitTracker holds them steady): the APU's
    # from the ryzenadj SMU table when it reads, else from the LHM
    # package-power plateau heuristic; the dGPU's from NVML.
    smu_binding = apu_binding(rj) if rj is not None else {}
    if rj is not None:
        cpu_binding = apu_reasons(rj, on_ac, weak_charger)
    else:
        cpu_binding = apu.reasons(
            (cpu_pkg_w, cpu_load) if (cpu_pkg_w is not None or cpu_load is not None)
            else None, on_ac, weak_charger)
    # Held at the base clock with nothing else to explain it: boost is off. A
    # limit at its threshold (even one LIMIT_KINDS leaves out) slows the clock
    # itself, and is the reason then.
    cpu_clock = cache["clock"]()
    if not cpu_binding and not smu_binding:
        cpu_binding = clock_reasons(cpu_clock, on_ac, weak_charger)
    dgpu_binding = nv.evaluate(on_ac, weak_charger)
    if not dgpu_present:
        dgpu_binding = {}

    return {
        "has_batt": has_batt,
        "ac": flags_ac,
        "charging": charging,
        "discharging": discharging,
        "raw_pct": raw_pct,
        "cap": batt_cap,
        "rate_w": batt_rate,

        "cpu": cpu,
        "cpu_binding": cpu_binding,  # {key: Reason} binding this tick
        "cpu_limit": None,           # the Reason named; set by LimitTracker
        # raw limit-detection inputs, surfaced for --verbose diagnostics
        "cpu_pkg_w": cpu_pkg_w,
        "cpu_load": cpu_load,
        "cpu_limits": rj,  # ryzenadj SMU snapshot (value, limit pairs) or None
        "cpu_clock": cpu_clock,  # CpuClock.read(): (held, ghz, busy) or None
        "weak_charger": weak_charger,

        "raw_mem": raw_mem,
        "mem": mem,

        "igpu": clamp(igpu, 0, 100),
        "npu": None if npu is None else clamp(npu, 0, 100),  # None = no NPU

        "dgpu": clamp(dgpu, 0, 100),
        "dgpu_present": dgpu_present,
        "dgpu_src": "nvml" if nv_util is not None else "pdh",
        "dgpu_binding": dgpu_binding,
        "dgpu_limit": None,

        "has_temp": has_temp,
        "temp_c": clamp(raw_temp, 0, 254) if has_temp else 0,
        "raw_temp": raw_temp,
        "temp_src": temp_src,

        "brightness": led_val,
    }


def build_parser():
    """Build the CLI parser.

    Split out of main() so config.py can rewrite the defaults from the INI
    before parsing (an explicit flag then still wins over the file) and so the
    service and installer entry points can reuse the exact same options.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interval", type=float, default=0.4,
                        help="seconds between updates (default: 0.4)")
    parser.add_argument("--min-led", type=int, default=8,
                        help="LED brightness at 0%% screen brightness (default: 8)")
    parser.add_argument("--max-led", type=int, default=150,
                        help="LED brightness at 100%% screen brightness, "
                             "0-255 (default: 150; full 255 is blinding)")
    parser.add_argument("--fallback-brightness", type=int, default=30,
                        help="assumed screen brightness %% if unreadable (default: 30)")
    parser.add_argument("--led-gamma", type=float, default=2.0,
                        help="curve between min/max LED brightness; higher "
                             "dims the mid-range, 1.0 is linear (default: 2.0)")
    parser.add_argument("--igpu-match", default=r"Intel|Iris|UHD|Radeon.*(Graphics|M\b)",
                        help="regex matching the iGPU adapter name")
    parser.add_argument("--dgpu-match", default=r"NVIDIA|GeForce|RTX|GTX|Radeon RX",
                        help="regex matching the dGPU adapter name")
    parser.add_argument("--cpu-power-cap", type=float, default=None,
                        help="APU sustained power ceiling in watts for "
                             "power-limit detection; auto-calibrated (approx) "
                             "if unset")
    parser.add_argument("--no-nvml", action="store_true",
                        help="disable NVIDIA dGPU power/throttle detection")
    parser.add_argument("--no-ryzenadj", action="store_true",
                        help="disable the in-process libryzenadj APU STAPM/PPT "
                             "power-limit + Tctl source (fall back to the LHM "
                             "package-power heuristic)")
    parser.add_argument("--ryzenadj-dir", default=None,
                        help="directory containing libryzenadj.dll + "
                             "WinRing0x64.sys (auto-discovered from a ryzenadj "
                             "install / scoop shim if unset)")
    parser.add_argument("--no-lhm-lib", action="store_true",
                        help="disable the in-process LibreHardwareMonitorLib APU "
                             "source (fall back to the LHM WMI provider / EC)")
    parser.add_argument("--framework-tool", default=None, metavar="PATH",
                        help="full path to framework_tool.exe (searched on PATH, "
                             "in Program Files and in every user's winget "
                             "install if unset; the service cannot rely on PATH)")
    parser.add_argument("--fetch-lhm", action="store_true",
                        help="download LibreHardwareMonitorLib + HidSharp DLLs "
                             "into lib/ and exit (needs pythonnet at runtime); "
                             "files already present are kept")
    parser.add_argument("--force", action="store_true",
                        help="with --fetch-lhm: re-download even if the DLLs "
                             "are already there")
    parser.add_argument("--verbose", action="store_true",
                        help="print each tick's per-row descriptors")
    parser.add_argument("--bootloader", action="store_true",
                        help="reboot the macropad into its UF2 bootloader "
                             "(RPI-RP2 drive) and exit")
    parser.add_argument("--version", action="version",
                        version=f"fw16-sysmon {__version__}")

    deployment = parser.add_argument_group(
        "deployment", "packaging, service and firmware management")
    deployment.add_argument("--config", default=None, metavar="PATH",
                            help="tunables INI to load (default: "
                                 "%%ProgramData%%\\FW16Sysmon\\sysmon.ini when "
                                 "installed, else sysmon.ini beside the script)")
    deployment.add_argument("--write-default-config", action="store_true",
                            help="write a fully commented default INI to the "
                                 "config path and exit (never overwrites)")
    deployment.add_argument("--selftest", action="store_true",
                            help="probe every data source and dependency, "
                                 "report what works and how to fix what does not")
    deployment.add_argument("--json", action="store_true",
                            help="with --selftest: emit machine-readable JSON "
                                 "(consumed by the installer)")
    deployment.add_argument("--flash", nargs="?", const=True, default=None,
                            metavar="UF2",
                            help="flash firmware onto the macropad: jump to the "
                                 "bootloader, wait for the RPI-RP2 drive, copy "
                                 "the image, wait for it to come back "
                                 "(default: the bundled .uf2)")
    deployment.add_argument("--service", action="store_true",
                            help=argparse.SUPPRESS)  # SCM entry point
    deployment.add_argument("--install-service", action="store_true",
                            help="register the boot-start LocalSystem service")
    deployment.add_argument("--uninstall-service", action="store_true",
                            help="stop and deregister the service")
    deployment.add_argument("--start-service", action="store_true",
                            help="start the installed service")
    deployment.add_argument("--stop-service", action="store_true",
                            help="stop the installed service")
    deployment.add_argument("--restart-service", action="store_true",
                            help="restart the service (apply sysmon.ini changes)")

    limitlog = parser.add_argument_group(
        "limit log", "the long-term record of what held the CPU and dGPU back")
    limitlog.add_argument("--limit-report", action="store_true",
                          help="tally the limit log: which limits bound, how "
                               "long, under what conditions, and exit")
    limitlog.add_argument("--report-days", type=float, default=None,
                          metavar="N",
                          help="with --limit-report: only the last N days")
    limitlog.add_argument("--export-csv", default=None, metavar="DIR",
                          help="with --limit-report: also write every table "
                               "as CSV into DIR")
    limitlog.add_argument("--limitlog-db", default=None, metavar="PATH",
                          help="with --limit-report: read this copy of "
                               "limitlog.sqlite instead")
    return parser


def send_goodbye(dev):
    """Tell the firmware this host is stopping on purpose.

    Without it the firmware can't tell a clean stop from a crash: both just
    stop sending. A crash should be noticed -- the module shows a warning
    triangle for a while before falling back to the regular RGB effect -- but a
    service stop, a shutdown or Ctrl+C should hand over to the RGB effect
    quietly and at once. Best effort: a write error here changes nothing.
    """
    rep = bytes([VIA_CMD_CUSTOM_SET_VALUE, SYSMON_CHANNEL, SYSMON_VALUE_GOODBYE])
    try:
        dev.write(b"\x00" + rep + bytes(REPORT_LEN - len(rep)))
    except (OSError, ValueError):
        pass


def bootloader_jump(dev):
    """Reboot the macropad into its UF2 bootloader over the open raw-HID handle.

    VIA id_bootloader_jump; the 0xFE padding doubles as Framework's factory
    f_bootloader command, the same trick their official updater uses.
    """
    dev.write(b"\x00\x0b" + b"\xfe" * (REPORT_LEN - 1))


def main(argv=None, stop_event=None, resume_check=None):
    # Redirected (Setup's [Run] steps, a pipe), stdout is in the ANSI code
    # page: a path or name outside it would raise mid-command -- after the
    # work is done, failing the exit code callers check. Replace instead.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    # --service is dispatched before anything else: the SCM expects the process
    # to hand itself to the control dispatcher promptly, and the engine run it
    # subsequently starts loads the config itself.
    if "--service" in (argv if argv is not None else sys.argv[1:]):
        import service
        return service.run_dispatcher()

    parser = build_parser()
    import config
    # Pass this module object explicitly: run as a script it is registered as
    # __main__, so config could not find it by name without importing a second,
    # unused copy whose rebound constants nobody would read.
    config_file = config.apply(parser, argv, sys.modules[__name__])
    args = parser.parse_args(argv)

    if args.write_default_config:
        path = config.write_default(args.config, sys.modules[__name__])
        print(f"Wrote default config to {path}")
        return

    if args.fetch_lhm:
        fetch_lhm_dlls(force=args.force)
        return

    if args.limit_report:
        import limitreport
        rc = limitreport.report(args.limitlog_db, days=args.report_days,
                                csv_dir=args.export_csv)
        pause_if_own_console()
        return rc

    # The one-shot actions below are what shortcuts and the installer launch.
    # Each ends with pause_if_own_console() so the window sticks around long
    # enough to read: an elevated child always gets a fresh console of its own,
    # which Windows destroys the moment the process returns.
    if args.selftest:
        import deps
        rc = deps.run_selftest(args)
        if not args.json:
            pause_if_own_console()
        return rc

    if args.install_service or args.uninstall_service or \
            args.start_service or args.stop_service or args.restart_service:
        import service
        rc = service.handle_cli(args)
        # Only --restart-service is user-facing (it is a Start-menu item, and
        # is how an edited sysmon.ini gets applied). The other four are driven
        # by Setup, which runs them hidden and waits for them: pausing there
        # would stall the installer behind an invisible prompt.
        if args.restart_service:
            pause_if_own_console()
        return rc

    if args.flash is not None:
        import flash
        rc = flash.flash(None if args.flash is True else args.flash)
        pause_if_own_console()
        return rc

    if args.bootloader:
        dev = open_device()
        if dev is None:
            print("Macropad not found", file=sys.stderr)
            pause_if_own_console()
            sys.exit(1)
        bootloader_jump(dev)
        dev.close()
        print("Sent bootloader jump; RPI-RP2 drive should appear, then copy "
              "the .uf2 onto it")
        pause_if_own_console()
        return

    if config_file:
        log(f"Loaded config from {config_file}")
    return run(args, stop_event, resume_check)


def _publish_service_state(m, lhmlib, ryzen):
    """Write the service's own view of its sensors for --selftest to read.

    Deliberately records the *resolved* framework_tool path and the temperature
    source that actually won, because those are precisely the things that differ
    between a LocalSystem service and the interactive session someone runs a
    diagnostic in.
    """
    import json
    try:
        paths.ensure_data_dir()
        state = {
            "version": __version__,
            "written": time.strftime("%Y-%m-%d %H:%M:%S"),
            "framework_tool": _framework_tool_path or None,
            "temp_src": m["temp_src"],
            "temp_c": m["raw_temp"] if m["has_temp"] else None,
            "lhm_available": bool(lhmlib.available),
            "ryzenadj_available": bool(ryzen.available),
            "dgpu_present": bool(m["dgpu_present"]),
            "npu_present": m["npu"] is not None,
            "brightness": m["brightness"],
        }
        tmp = paths.service_state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, paths.service_state_path())
    except Exception as err:
        log(f"Could not publish service state: {err}")


def run(args, stop_event=None, resume_check=None):
    """The poll loop: gather, build six rows, send three frames, repeat.

    stop_event is a threading.Event the service sets from SvcStop; in console
    mode it is an Event that is never set, so waiting on it behaves exactly like
    the time.sleep() this replaced. Blocking on the event rather than sleeping
    is what makes a service stop prompt instead of taking up to a full interval.

    resume_check, when given, is polled once per tick and returns True after the
    machine has woken from sleep. The macropad re-enumerates across suspend (so
    the old handle is stale) and the ryzenadj SMU table mapping does not reliably
    survive S0ix, so both are torn down and rebuilt rather than waited out.
    """
    if stop_event is None:
        stop_event = threading.Event()

    # The macropad has a single raw-HID endpoint. A console run while the
    # service is also running means two processes interleaving frames at
    # slightly different rates, which looks like the display "behaving oddly"
    # rather than like a conflict -- so say so plainly instead of letting it be
    # diagnosed as a bug in the readings.
    import applog
    if not applog.is_service_mode():
        try:
            import service
            if service.is_running():
                print(f"WARNING: the {service.SERVICE_NAME} service is also "
                      f"running and writing to the macropad.", file=sys.stderr)
                print(f"         Both will fight over the single raw-HID "
                      f"endpoint and the display will look erratic.",
                      file=sys.stderr)
                print(f"         Stop it first:  sc stop {service.SERVICE_NAME}"
                      f"   (or use host\\dev.ps1, which does this for you)",
                      file=sys.stderr)
        except Exception:
            pass

    # Resolve framework_tool up front and log the result: it is the difference
    # between a working EC temperature source and a silent fallback, and the
    # service's environment is not the one anybody tests interactively.
    set_framework_tool(args.framework_tool)

    sources = WmiSources()
    ec = EcThermal()
    charge_limit = EcChargeLimit()
    gpu = GpuMonitor(args.igpu_match, args.dgpu_match)
    dwatch = DgpuSleepWatch(DgpuPower(args.dgpu_match, announce=True))
    dwatch.start(stop_event)
    apu = ApuLimits(cap_watts=args.cpu_power_cap)
    clock = CpuClock()
    nv = NvGpuLimits(enabled=not args.no_nvml)
    lhmlib = LhmLibSource(enabled=not args.no_lhm_lib)
    ryzen = RyzenAdjSource(enabled=not args.no_ryzenadj,
                           dll_dir=args.ryzenadj_dir)

    # Slow sources behind TTL caches so most ~0.4s ticks stay light. The LHM
    # snapshot is shared between temperature and CPU power (one read).
    cache = {
        "brightness": Cached(sources.screen_brightness, TTL_BRIGHTNESS),
        "battery": Cached(sources.battery_state, TTL_BATTERY),
        "lhm": Cached(lambda: lhm_reading(lhmlib, sources), TTL_LHM_SNAPSHOT),
        "ryzenadj": Cached(ryzen.snapshot, TTL_RYZENADJ),
        "ec_temp": Cached(ec.temperature, TTL_EC_THERMAL),
        # the last-resort ACPI zone: a WMI query, so not every tick
        "acpi_temp": Cached(sources.acpi_temperature, TTL_TEMP),
        # every tick (it debounces by reads): one PDH collect, about a ms
        "clock": Cached(clock.read, 0.0),
    }

    if args.verbose:
        for luid, name in gpu.luid_names.items():
            kind = "dGPU" if gpu.dgpu_re.search(name) else \
                   "iGPU" if gpu.igpu_re.search(name) else "ignored"
            print(f"adapter luid 0x{luid:X}: {name} [{kind}]")
        for luid in sorted(gpu.npu_luids):
            print(f"adapter luid 0x{luid:X}: compute-only [NPU -> iGPU row ghost]")
    psutil.cpu_percent(interval=None)  # prime; first call always returns 0

    cpu_track = LimitTracker("MSG_NAME_CPU", "HUE_RED")
    gpu_track = LimitTracker("MSG_NAME_GPU", "HUE_GREEN")
    messenger = Messenger(verbose=args.verbose)

    # The limit log records from the same readings, pad or no pad. It must
    # never take the loop down: every call into it catches its own errors.
    recorder = None
    if LIMITLOG_ENABLE:
        try:
            import limitlog
            recorder = limitlog.Recorder(sys.modules[__name__], clock=clock,
                                         nv=nv, dwatch=dwatch)
            messenger.on_sent = recorder.message
            print(f"Limit log: {recorder.path}")
        except Exception as err:
            log(f"Limit log disabled: {err}")
            recorder = None

    dev = None
    next_open = 0.0      # when to look for the pad again while it is absent
    ghost_flags = False  # the firmware reads ghost flags (seen in a FRAME C echo)
    published_state = False
    while not stop_event.is_set():
        try:
            if resume_check is not None and resume_check():
                if recorder is not None:
                    recorder.resumed()
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass
                    dev = None
                    next_open = 0.0
                if ryzen.available:
                    ryzen.close()
                    ryzen = RyzenAdjSource(enabled=not args.no_ryzenadj,
                                           dll_dir=args.ryzenadj_dir)
                    cache["ryzenadj"] = Cached(ryzen.snapshot, TTL_RYZENADJ)
            if dev is None and (recorder is None or time.monotonic() >= next_open):
                next_open = time.monotonic() + 5
                dev = open_device()
                if dev is not None:
                    print("Connected to macropad")
                    ghost_flags = False  # a reflash may have changed the firmware
                    messenger.reset()
                    messenger.probe(dev)
                    if recorder is not None:
                        recorder.pad(True)
            if dev is None and recorder is None:
                # module unplugged / not enumerated yet (the service starts
                # before the USB stack has necessarily settled); with the limit
                # log on, the loop runs on without it and looks again every 5 s
                stop_event.wait(5)
                continue

            m = gather(sources, gpu, charge_limit, apu, nv, ryzen, args, cache)
            now = time.monotonic()
            cpu_track.update(m["cpu_binding"], now)
            gpu_track.update(m["dgpu_binding"], now)
            m["cpu_limit"] = cpu_track.reason
            m["dgpu_limit"] = gpu_track.reason
            m["ghost_flags"] = ghost_flags
            # whether the dGPU is powered off, and for how long without a
            # break, from PnP (never wakes it; see DgpuPower, DgpuSleepWatch)
            m["dgpu_power"], m["dgpu_off_s"] = dwatch.snapshot()
            rows = [ROW_BUILDERS[i](m) for i in range(6)]
            if not published_state:
                # First complete tick: record what this process can actually
                # see. Only the service's view is worth publishing -- a console
                # run would overwrite it with the user's environment, which is
                # exactly the confusion this file exists to prevent.
                published_state = True
                if applog.is_service_mode():
                    _publish_service_state(m, lhmlib, ryzen)
            if args.verbose:
                names = ["batt", "cpu", "mem", "igpu", "dgpu", "temp"]
                print(f"bright={m['brightness']}")
                for name, r in zip(names, rows):
                    print(f"  {name:5s} {r!r}")
                # limit-detection diagnostics: shows which source drove the APU
                # verdict. With ryzenadj it prints the SMU value/limit pairs
                # (the * marks one binding this tick); otherwise it shows the
                # LHM heuristic inputs (pkg_w/load = None means LHM isn't
                # running -> no APU breathe). clock[] is CpuClock: the busy
                # CPUs' fastest clock, the busiest core, HELD = at base under
                # load. The dGPU's need pynvml. -> names the limit the row
                # breathes for (held LIMIT_RELEASE_S).
                rj = m["cpu_limits"]
                if rj:
                    units = {k: u for k, (_, u) in APU_SIGNALS.items()}
                    pairs = " ".join(
                        f"{k}={rj[k][0]:.1f}/{rj[k][1]:.1f}{units[k]}"
                        f"{'*' if k in m['cpu_binding'] else ''}"
                        for k in APU_SIGNALS if k in rj)
                    cpu_src = f"ryzenadj[{pairs}]"
                else:
                    cpu_src = f"heuristic(pkg_w={m['cpu_pkg_w']} load={m['cpu_load']})"
                clk = m["cpu_clock"]
                if clk:
                    ghz = "?" if clk[1] is None else f"{clk[1]:.2f}GHz"
                    cpu_src += (f" clock[{ghz} busy={clk[2]:.0f}%"
                                f"{' HELD' if clk[0] else ''}]")
                dgpu_now = " ".join(map(repr, m["dgpu_binding"].values())) or "-"
                print(f"  limits ac={m['ac']} weak={m['weak_charger']} "
                      f"cpu {cpu_src} -> {m['cpu_limit']!r} "
                      f"dgpu[{m['dgpu_src']}] {dgpu_now} -> {m['dgpu_limit']!r} "
                      f"power={m['dgpu_power']}"
                      + (f" for {m['dgpu_off_s']:.0f}s" if m["dgpu_off_s"] is not None else ""))
            if recorder is not None:
                recorder.observe(m, now, cpu_track, gpu_track)
            if dev is None:
                stop_event.wait(args.interval)
                continue
            for rep in build_frame(rows, m["brightness"]):
                dev.write(b"\x00" + rep)
            # drain the VIA echoes so the report buffer doesn't fill up; they
            # carry the firmware's message status and the replay key
            while True:
                rep = dev.read(REPORT_LEN)
                if not rep:
                    break
                if (len(rep) > GHOST_FLAGS_ACK_OFFSET and rep[1] == SYSMON_CHANNEL
                        and rep[2] == SYSMON_VALUE_FRAME_C):
                    ghost_flags = rep[GHOST_FLAGS_ACK_OFFSET] == GHOST_FLAGS_ACK
                messenger.on_echo(rep)
            messenger.tick(dev, now, (cpu_track, gpu_track))
        except KeyboardInterrupt:
            break
        except OSError as err:
            print(f"Device error ({err}); reconnecting...", file=sys.stderr)
            try:
                dev.close()
            except Exception:
                pass
            if dev is not None and recorder is not None:
                recorder.pad(False)
            dev = None
            next_open = 0.0
            stop_event.wait(2)
            continue
        try:
            stop_event.wait(args.interval)
        except KeyboardInterrupt:  # Ctrl+C usually lands here, mid-sleep
            break

    if dev is not None:
        send_goodbye(dev)
        dev.close()
    if recorder is not None:
        recorder.close()  # bounded: the SCM doesn't wait long for a stop
    lhmlib.close()
    ryzen.close()
    # A clean exit says goodbye (above), so the firmware hands over to the
    # regular RGB effect at once. A crash or an abrupt power loss sends
    # nothing: the firmware's no-data timeout (TIMEOUT_MS_DEFAULT) then shows a
    # disconnect warning for a while before falling back -- deliberately, as
    # that case is worth noticing.


if __name__ == "__main__":
    # main()'s return is the process exit code: the CLI actions return one
    # (--install-service's is what the installer checks), the loop None = 0.
    sys.exit(main())
