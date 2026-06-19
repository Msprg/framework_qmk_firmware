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

Each tick sends TWO 32-byte VIA "custom set value" raw-HID reports on a private
VIA channel (0x42):
    value_id 0x01 = FRAME A: rows 0,1,2
    value_id 0x02 = FRAME B: rows 3,4,5 + globals

Dependencies (Windows):
    pip install hidapi psutil wmi
    optional:
      pynvml    -- NVIDIA dGPU power/throttle reasons (authoritative)
      ryzenadj  -- AMD APU STAPM/PPT power limits + Tctl read straight from the
                   SMU power-metrics table via its libryzenadj.dll (the same
                   source HWiNFO reads for "APU STAPM Limit"). Install the
                   `ryzenadj` tool (e.g. `scoop install ryzenadj`); the DLL is
                   auto-discovered, or point --ryzenadj-dir at it.
      pythonnet -- in-process LibreHardwareMonitorLib for AMD APU package power
                   + temperature (run --fetch-lhm once)

APU power-limit detection (CPU-row breathe), in order:
    1. ryzenadj / libryzenadj SMU table: STAPM/PPT value-vs-limit (authoritative)
    2. LibreHardwareMonitor package-power plateau heuristic (fallback)

Temperature sources, tried in order:
    1. LibreHardwareMonitorLib in-process via pythonnet (run --fetch-lhm)
    2. ryzenadj Tctl (in-process; free once libryzenadj is loaded for limits)
    3. LibreHardwareMonitor WMI provider (only if its app is running)
    4. Framework EC via framework_tool --thermal (throttled; temperature only)
    5. ACPI thermal zone (MSAcpi_ThermalZoneTemperature; static on the FW16,
       so only a last resort)

The ring0 sources (ryzenadj, LibreHardwareMonitorLib) need the script elevated.

Run it in a console first to verify, then schedule with Task Scheduler
(pythonw.exe) to have it start at login. No service needed.
"""

import argparse
import os
import re
import subprocess
import sys
import time
import winreg

import hid  # hidapi package
import psutil

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
# Two FRAME reports per tick:
#   value_id 0x01 = FRAME A : rows 0,1,2          (24-byte payload, 5 pad bytes)
#   value_id 0x02 = FRAME B : rows 3,4,5 + globals (24-byte payload + globals)
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
# ---------------------------------------------------------------------------

VIA_CMD_CUSTOM_SET_VALUE = 0x07
SYSMON_CHANNEL = 0x42
SYSMON_VALUE_FRAME_A = 0x01  # rows 0,1,2
SYSMON_VALUE_FRAME_B = 0x02  # rows 3,4,5 + globals

ROW_DESC_LEN = 8

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

# Brightness levels (base row_val)
ROW_VAL_FULL = 255
ROW_VAL_BATT_IDLE = 80   # AC-idle dim green; firmware lifts via visible_pwm

# Battery policy
BATT_CRITICAL_PCT = 15   # raw % below which a discharging battery blinks red
ASSIST_MIN_W = 10        # battery-assist drain meaning a real power deficit;
                         # below this on AC is just EC cap-drift housekeeping

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

# Breathe overlay floor for power-limited rows (~43% of 255: dim but readable).
LIMIT_FLOOR = 110

# APU power-limit detection via the SMU table (libryzenadj; authoritative).
APU_LIMIT_NEAR = 0.95   # a STAPM/PPT limit counts as binding when its live
                        # value reaches this fraction of the limit (the value
                        # sits right at the limit when the chip is power-capped)
APU_LIMIT_SIGNALS = ("stapm", "slow")  # which SMU limits trigger the CPU-row
                        # breathe. STAPM (skin-temp-aware sustained budget --
                        # the "APU STAPM Limit" HWiNFO shows) and the slow PPT
                        # are sustained, so they give a steady signal; the fast
                        # PPT is the short boost window and would flicker, so it
                        # is read for diagnostics but not used to trigger. Add
                        # "fast" here to include it.

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
TTL_TEMP = 2.0
TTL_LHM_SNAPSHOT = 1.0   # one LHM enumeration feeding both temp and cpu power
TTL_EC_THERMAL = 2.5     # throttled framework_tool --thermal fallback

# LibreHardwareMonitorLib: the .NET engine inside LibreHardwareMonitor, loaded
# in-process via pythonnet so the APU package-power/load/temperature sensors can
# be read directly -- no separate LHM app and no WMI provider. The native DLLs
# are fetched into lib/ with `--fetch-lhm` (that directory is gitignored).
LHM_LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
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


class GpuMonitor:
    """3D engine utilization per adapter via a persistent PDH query.

    Roughly Task Manager's headline GPU number, and ~1ms per read instead
    of the seconds a Win32_PerfFormattedData WMI query takes.
    """

    def __init__(self, igpu_pattern, dgpu_pattern):
        self.igpu_re = re.compile(igpu_pattern, re.IGNORECASE)
        self.dgpu_re = re.compile(dgpu_pattern, re.IGNORECASE)
        self.luid_names = adapter_luid_names()
        self.dgpu_present = any(self.dgpu_re.search(n) for n in self.luid_names.values())
        self.query = None
        if win32pdh is None:
            print("win32pdh not available; GPU usage disabled", file=sys.stderr)
            return
        try:
            self.query = win32pdh.OpenQuery()
            self.counter = win32pdh.AddEnglishCounter(
                self.query, r"\GPU Engine(*engtype_3D)\Utilization Percentage")
            win32pdh.CollectQueryData(self.query)  # prime; % counters need two samples
        except Exception as err:
            print(f"GPU counters unavailable: {err}", file=sys.stderr)
            self.query = None

    def read(self):
        """Return (igpu_pct, dgpu_pct, dgpu_present)."""
        if self.query is None:
            return 0, 0, self.dgpu_present
        try:
            win32pdh.CollectQueryData(self.query)
            items = win32pdh.GetFormattedCounterArray(self.counter, win32pdh.PDH_FMT_DOUBLE)
        except Exception:
            return 0, 0, self.dgpu_present

        per_luid = {}
        for name, value in items.items():
            m = re.search(r"luid_0x([0-9A-Fa-f]+)_0x([0-9A-Fa-f]+)", name)
            if not m:
                continue
            luid = (int(m.group(1), 16) << 32) | int(m.group(2), 16)
            per_luid[luid] = per_luid.get(luid, 0.0) + value

        igpu = dgpu = 0.0
        for luid, total in per_luid.items():
            desc = self.luid_names.get(luid, "")
            if self.dgpu_re.search(desc):
                dgpu += total
            elif self.igpu_re.search(desc):
                igpu += total
        return min(100, igpu), min(100, dgpu), self.dgpu_present


class NvGpuLimits:
    """dGPU power & throttle state for NVIDIA cards via NVML.

    NVIDIA exposes authoritative throttle reasons (no inference needed):
    nvmlDeviceGetCurrentClocksThrottleReasons returns a bitmask, and the
    enforced power limit drops on battery. Disables itself permanently if
    pynvml is missing, NVML won't init, or there's no NVIDIA GPU.
    """

    def __init__(self, enabled=True):
        self.available = False
        self.handle = None
        self._deb = [(False, 0), (False, 0)]  # debounce state for (power, psrc)
        if not enabled:
            return
        if pynvml is None:
            print("pynvml not installed; dGPU limit detection disabled",
                  file=sys.stderr)
            return
        try:
            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() < 1:
                return
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            # constant names were renamed Throttle -> Event across versions
            self._sw_power = getattr(
                pynvml, "nvmlClocksThrottleReasonSwPowerCap",
                getattr(pynvml, "nvmlClocksEventReasonSwPowerCap", 0))
            self._hw_brake = getattr(
                pynvml, "nvmlClocksThrottleReasonHwPowerBrakeSlowdown",
                getattr(pynvml, "nvmlClocksEventReasonHwPowerBrakeSlowdown", 0))
            self._reasons_fn = (
                getattr(pynvml, "nvmlDeviceGetCurrentClocksThrottleReasons", None)
                or getattr(pynvml, "nvmlDeviceGetCurrentClocksEventReasons", None))
            self.available = self._reasons_fn is not None
        except Exception as err:
            print(f"NVML init failed; dGPU limit detection disabled: {err}",
                  file=sys.stderr)
            self.available = False

    def evaluate(self, on_ac, weak_charger):
        """Return (power_limited, psrc_limited) for the dGPU.

        Only the weak-charger (battery-assisting-on-AC) case is surfaced as a
        power-source limit; plain battery operation is left to the battery row.
        """
        if not self.available:
            return self._stabilize(False, False)
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
            return self._stabilize(False, False)

        sw_cap = bool(reasons & self._sw_power)
        hw_brake = bool(reasons & self._hw_brake)
        capped_below_stock = default > 0 and enforced < 0.9 * default
        # NVIDIA sets SwPowerCap eagerly -- it can be set even near idle, when the
        # GPU is nowhere near its limit, which made the breathe flicker. Only
        # honour it as a real power cap when the GPU is actually drawing near its
        # enforced limit. HwPowerBrake is a genuine hardware brake; keep it as-is.
        near_cap = enforced > 0 and draw >= NV_NEAR_CAP * enforced
        if weak_charger:
            # supply-bound: a loaded GPU capped while the charger can't keep up
            active = draw >= NV_ACTIVE_MIN_W
            return self._stabilize(
                False,
                active and (hw_brake or (sw_cap and near_cap) or capped_below_stock))
        if on_ac:
            # chip-bound: the driver/board is actively capping power on AC
            return self._stabilize(hw_brake or (sw_cap and near_cap), False)
        # plain battery: not surfaced here (the battery row covers it)
        return self._stabilize(False, False)

    def _stabilize(self, power, psrc):
        """Debounce the (power, psrc) flags: a changed value must repeat for
        NV_DEBOUNCE_READS consecutive evaluations before it is adopted, so an
        isolated single-tick NVML blip can't flash the breathe on and off.
        """
        out = []
        for i, raw in enumerate((power, psrc)):
            cur, cnt = self._deb[i]
            if raw == cur:
                cnt = 0
            else:
                cnt += 1
                if cnt >= NV_DEBOUNCE_READS:
                    cur, cnt = raw, 0
            self._deb[i] = (cur, cnt)
            out.append(cur)
        return out[0], out[1]


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

    def evaluate(self, reading, on_ac, weak_charger, limited=None):
        """Return (power_limited, psrc_limited) for the APU.

        `limited`, when not None, is an authoritative chip-power-cap boolean
        (ryzenadj reporting a STAPM/PPT limit binding); the package-power
        plateau heuristic below is then bypassed entirely. When None (no
        ryzenadj), fall back to inferring the cap from LHM `reading` =
        (package_watts, load_pct). Either way, only the weak-charger
        (battery-assisting-on-AC) case is surfaced as a power-source limit;
        plain battery operation is left to the battery row.
        """
        if limited is not None:
            # Authoritative path: route the SMU verdict the same way the
            # heuristic result is routed -- weak charger -> power-source (slow)
            # breathe; else on AC -> chip (fast) breathe; plain battery is the
            # battery row's job.
            if weak_charger:
                return False, bool(limited)
            if on_ac:
                return bool(limited), False
            return False, False
        # ---- fallback heuristic (no ryzenadj): LHM package-power plateau ----
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


def run_framework_tool(*tool_args):
    """Run framework_tool and return its stdout, or None on transient failure.

    Raises FileNotFoundError if the tool is not installed, so callers can
    disable themselves permanently.
    """
    try:
        # stdin must be DEVNULL: under CREATE_NO_WINDOW framework_tool
        # sees a fresh console and waits for ENTER before exiting
        return subprocess.run(
            ["framework_tool", *tool_args],
            capture_output=True, text=True, timeout=2,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except (subprocess.TimeoutExpired, OSError) as err:
        if isinstance(err, FileNotFoundError):
            raise
        return None


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
                return charging, discharging, clamp(rate_mw / 1000, 0, 255)
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


def fetch_lhm_dlls(dest=LHM_LIB_DIR):
    """Download LibreHardwareMonitorLib + HidSharp DLLs from NuGet into dest.

    A NuGet .nupkg is a zip; we pull the latest stable of each package and copy
    out the .NET Framework build of each DLL. Run once via `--fetch-lhm`.
    """
    import urllib.request
    import zipfile
    import tempfile
    import glob
    import shutil

    os.makedirs(dest, exist_ok=True)

    def grab(pkg, dll):
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
            shutil.copy2(pick, os.path.join(dest, dll))
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

    def __init__(self, enabled=True, lib_dir=LHM_LIB_DIR):
        self.available = False
        self._computer = None
        self._cpu_hw = []
        self._resolver = None
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

    Order: an explicit --ryzenadj-dir; the real install dir behind a `ryzenadj`
    shim on PATH (scoop writes a `.shim` sidecar whose `path =` points at the
    actual exe, which is NOT where the shim lives); then the conventional scoop
    location. Returns None if the DLL isn't found anywhere.
    """
    import shutil
    cands = []
    if explicit:
        cands.append(explicit)
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

    def __init__(self, enabled=True, dll_dir=None):
        self.available = False
        self._lib = None
        self._ry = None
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
            for name in ("get_stapm_limit", "get_stapm_value", "get_fast_limit",
                         "get_fast_value", "get_slow_limit", "get_slow_value",
                         "get_tctl_temp"):
                fn = getattr(lib, name)
                fn.restype = c_float
                fn.argtypes = [c_void_p]

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

    @staticmethod
    def _ensure_driver(dll_dir):
        """Best-effort: place WinRing0x64.sys next to the host executable.

        WinRing0 loads its kernel driver from a path relative to the process
        image; when libryzenadj is hosted by python that's the interpreter dir.
        Copy the .sys there if it's missing (if the copy can't happen, init
        just fails and the caller falls back).
        """
        try:
            import shutil
            src = os.path.join(dll_dir, RYZENADJ_DRIVER)
            dst = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                               RYZENADJ_DRIVER)
            if os.path.isfile(src) and not os.path.isfile(dst):
                shutil.copyfile(src, dst)
        except OSError:
            pass

    def snapshot(self):
        """Return {'stapm':(val,lim), 'fast':.., 'slow':.., 'tctl':temp_or_None}.

        Each (value, limit) pair is in watts; pairs whose limit is non-positive
        or NaN (unsupported on this CPU) are dropped so they can't read as a
        binding limit. Returns None on a refresh failure (caller falls back).
        One SMU refresh, so it sits behind the snapshot TTL cache.
        """
        if not self.available:
            return None
        g, ry = self._lib, self._ry
        try:
            if g.refresh_table(ry) != 0:
                return None
            raw = {
                "stapm": (g.get_stapm_value(ry), g.get_stapm_limit(ry)),
                "fast": (g.get_fast_value(ry), g.get_fast_limit(ry)),
                "slow": (g.get_slow_value(ry), g.get_slow_limit(ry)),
            }
            tctl = g.get_tctl_temp(ry)
        except Exception:
            return None
        out = {}
        for key, (val, lim) in raw.items():
            # x == x is False only for NaN (unsupported sensor); a 0/negative
            # limit means "not reported" and must not count as binding.
            if lim == lim and val == val and lim > 0:
                out[key] = (float(val), float(lim))
        out["tctl"] = float(tctl) if (tctl == tctl and tctl > 0) else None
        return out

    @staticmethod
    def power_limited(snap):
        """True if any APU_LIMIT_SIGNALS limit is binding (value >= near*limit).

        Returns None when there's no snapshot (source unavailable or a transient
        refresh failure) so the caller can fall back to the heuristic that tick.
        """
        if snap is None:
            return None
        for key in APU_LIMIT_SIGNALS:
            pair = snap.get(key)
            if pair and pair[1] > 0 and pair[0] >= APU_LIMIT_NEAR * pair[1]:
                return True
        return False

    @staticmethod
    def temperature(snap):
        """Tctl die temperature from a snapshot, or None."""
        return snap.get("tctl") if snap else None

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
# Descriptor builder + per-row render policy
# ---------------------------------------------------------------------------

class RowDesc:
    """An 8-byte per-row descriptor matching the frozen wire layout.

    Fields default to "off" / "no effect"; the row policy functions set only
    what they need. pack() emits the bytes in the documented order.
    """

    def __init__(self, level=0, row_val=ROW_VAL_FULL, hue0=0, hue1=0,
                 gradient=False, envelope=ENV_NONE,
                 overlay_shape=OVL_NONE, overlay_dir=OVL_DIR_UP,
                 overlay_count=0, period=0, floor=0):
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
        return (f"lvl={self.level} val={self.row_val} hue={self.hue0}{grad}"
                f"{envs}{ovls}{flr}")


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
    drift = ac and discharging and rate_w < ASSIST_MIN_W
    if drift:
        discharging = False

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


def build_cpu_row(m):
    """row1: CPU usage, solid red; breathe when APU power-limited."""
    desc = RowDesc(level=pct_to_level(m["cpu"]), hue0=HUE_RED)
    if m["cpu_power"] or m["cpu_psrc"]:
        desc.envelope = ENV_BREATHE
        desc.floor = LIMIT_FLOOR
        desc.period = breathe_period(m["cpu_psrc"])
    return desc


def build_mem_row(m):
    """row2: memory usage, solid gold; blink when RAW mem >= 90."""
    desc = RowDesc(level=pct_to_level(m["mem"]), hue0=HUE_GOLD)
    if m["raw_mem"] >= MEM_CRITICAL_PCT:
        desc.envelope = ENV_BLINK
        desc.period = PERIOD_BLINK
    return desc


def build_igpu_row(m):
    """row3: iGPU usage, solid red; no envelope/overlay."""
    return RowDesc(level=pct_to_level(m["igpu"]), hue0=HUE_RED)


def build_dgpu_row(m):
    """row4: dGPU usage, solid green; breathe when dGPU power-limited."""
    if not m["dgpu_present"]:
        return RowDesc(level=0)
    desc = RowDesc(level=pct_to_level(m["dgpu"]), hue0=HUE_GREEN)
    if m["dgpu_power"] or m["dgpu_psrc"]:
        desc.envelope = ENV_BREATHE
        desc.floor = LIMIT_FLOOR
        desc.period = breathe_period(m["dgpu_psrc"])
    return desc


def build_temp_row(m):
    """row5: temperature bar, green->red gradient; blink when RAW temp > 90."""
    if not m["has_temp"]:
        return RowDesc(level=0)
    t = m["temp_c"]
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


def build_frame(rows, brightness, glide_ms=GLIDE_MS_DEFAULT,
                timeout_ms=TIMEOUT_MS_DEFAULT):
    """Build the two 32-byte reports for one tick.

    rows is the list of six RowDesc objects in fixed row order. Returns
    [frame_a, frame_b]; the caller writes each as b"\\x00" + report.
    """
    assert len(rows) == 6

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

    return [bytes(frame_a), bytes(frame_b)]


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

    igpu, dgpu, dgpu_present = gpu.read()

    # One LHM snapshot feeds both temperature and CPU power this tick.
    lhm_temp, cpu_pkg_w, cpu_load = cache["lhm"]()

    # One ryzenadj SMU refresh feeds both the authoritative APU power-limit
    # verdict and an in-process Tctl temperature fallback.
    rj = cache["ryzenadj"]() if ryzen.available else None

    # Temperature: prefer LHM (validated), then ryzenadj Tctl (in-process,
    # authoritative), then the throttled EC framework_tool, then static ACPI.
    temp_c = lhm_temp
    if temp_c is None:
        temp_c = RyzenAdjSource.temperature(rj)
    if temp_c is None:
        temp_c = cache["ec_temp"]()
    if temp_c is None:
        temp_c = sources.acpi_temperature()
    has_temp = temp_c is not None
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

    # APU power-limit: authoritative ryzenadj STAPM/PPT binding when available,
    # else the LHM package-power plateau heuristic (limited=None).
    cpu_limited = RyzenAdjSource.power_limited(rj)
    cpu_power, cpu_psrc = apu.evaluate(
        (cpu_pkg_w, cpu_load) if (cpu_pkg_w is not None or cpu_load is not None)
        else None, on_ac, weak_charger, limited=cpu_limited)
    dgpu_power, dgpu_psrc = nv.evaluate(on_ac, weak_charger)
    if not dgpu_present:
        dgpu_power = dgpu_psrc = False

    return {
        "has_batt": has_batt,
        "ac": flags_ac,
        "charging": charging,
        "discharging": discharging,
        "raw_pct": raw_pct,
        "cap": batt_cap,
        "rate_w": batt_rate,

        "cpu": cpu,
        "cpu_power": cpu_power,
        "cpu_psrc": cpu_psrc,
        # raw limit-detection inputs, surfaced for --verbose diagnostics
        "cpu_pkg_w": cpu_pkg_w,
        "cpu_load": cpu_load,
        "cpu_limits": rj,  # ryzenadj SMU snapshot (stapm/fast/slow) or None
        "weak_charger": weak_charger,

        "raw_mem": raw_mem,
        "mem": mem,

        "igpu": clamp(igpu, 0, 100),

        "dgpu": clamp(dgpu, 0, 100),
        "dgpu_present": dgpu_present,
        "dgpu_power": dgpu_power,
        "dgpu_psrc": dgpu_psrc,

        "has_temp": has_temp,
        "temp_c": clamp(raw_temp, 0, 254) if has_temp else 0,
        "raw_temp": raw_temp,

        "brightness": led_val,
    }


def main():
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
    parser.add_argument("--fetch-lhm", action="store_true",
                        help="download LibreHardwareMonitorLib + HidSharp DLLs "
                             "into lib/ and exit (needs pythonnet at runtime)")
    parser.add_argument("--verbose", action="store_true",
                        help="print each tick's per-row descriptors")
    parser.add_argument("--bootloader", action="store_true",
                        help="reboot the macropad into its UF2 bootloader "
                             "(RPI-RP2 drive) and exit")
    args = parser.parse_args()

    if args.fetch_lhm:
        fetch_lhm_dlls()
        return

    if args.bootloader:
        dev = open_device()
        if dev is None:
            print("Macropad not found", file=sys.stderr)
            sys.exit(1)
        # VIA id_bootloader_jump; 0xFE padding doubles as Framework's
        # factory f_bootloader command, same trick their updater uses
        dev.write(b"\x00\x0b" + b"\xfe" * (REPORT_LEN - 1))
        dev.close()
        print("Sent bootloader jump; RPI-RP2 drive should appear, then copy "
              "the .uf2 onto it")
        return

    sources = WmiSources()
    ec = EcThermal()
    charge_limit = EcChargeLimit()
    gpu = GpuMonitor(args.igpu_match, args.dgpu_match)
    apu = ApuLimits(cap_watts=args.cpu_power_cap)
    nv = NvGpuLimits(enabled=not args.no_nvml)
    lhmlib = LhmLibSource(enabled=not args.no_lhm_lib)
    ryzen = RyzenAdjSource(enabled=not args.no_ryzenadj,
                           dll_dir=args.ryzenadj_dir)

    def lhm_snapshot():
        # Prefer the in-process LibreHardwareMonitorLib (no app, no WMI); fall
        # back to the LHM WMI provider when the lib is unavailable or silent.
        if lhmlib.available:
            snap = lhmlib.snapshot()
            if snap != (None, None, None):
                return snap
        return sources.lhm_snapshot()

    # Slow sources behind TTL caches so most ~0.4s ticks stay light. The LHM
    # snapshot is shared between temperature and CPU power (one read).
    cache = {
        "brightness": Cached(sources.screen_brightness, TTL_BRIGHTNESS),
        "battery": Cached(sources.battery_state, TTL_BATTERY),
        "lhm": Cached(lhm_snapshot, TTL_LHM_SNAPSHOT),
        "ryzenadj": Cached(ryzen.snapshot, TTL_RYZENADJ),
        "ec_temp": Cached(ec.temperature, TTL_EC_THERMAL),
    }

    if args.verbose:
        for luid, name in gpu.luid_names.items():
            kind = "dGPU" if gpu.dgpu_re.search(name) else \
                   "iGPU" if gpu.igpu_re.search(name) else "ignored"
            print(f"adapter luid 0x{luid:X}: {name} [{kind}]")
    psutil.cpu_percent(interval=None)  # prime; first call always returns 0

    dev = None
    while True:
        try:
            if dev is None:
                dev = open_device()
                if dev is None:
                    time.sleep(5)
                    continue
                print("Connected to macropad")

            m = gather(sources, gpu, charge_limit, apu, nv, ryzen, args, cache)
            rows = [ROW_BUILDERS[i](m) for i in range(6)]
            if args.verbose:
                names = ["batt", "cpu", "mem", "igpu", "dgpu", "temp"]
                print(f"bright={m['brightness']}")
                for name, r in zip(names, rows):
                    print(f"  {name:5s} {r!r}")
                # limit-detection diagnostics: shows which source drove the APU
                # verdict. With ryzenadj it prints the SMU value/limit watt
                # pairs (the * marks a limit in APU_LIMIT_SIGNALS that is
                # binding); otherwise it shows the LHM heuristic inputs (pkg_w/
                # load = None means LHM isn't running -> no APU breathe). dgpu
                # power/psrc need pynvml.
                rj = m["cpu_limits"]
                if rj:
                    pairs = " ".join(
                        f"{k}={rj[k][0]:.1f}/{rj[k][1]:.1f}W"
                        f"{'*' if (k in APU_LIMIT_SIGNALS and rj[k][1] > 0 and rj[k][0] >= APU_LIMIT_NEAR * rj[k][1]) else ''}"
                        for k in ("stapm", "fast", "slow") if k in rj)
                    cpu_src = f"ryzenadj[{pairs}]"
                else:
                    cpu_src = f"heuristic(pkg_w={m['cpu_pkg_w']} load={m['cpu_load']})"
                print(f"  limits ac={m['ac']} weak={m['weak_charger']} "
                      f"cpu {cpu_src} ->power={m['cpu_power']},psrc={m['cpu_psrc']} "
                      f"dgpu->power={m['dgpu_power']},psrc={m['dgpu_psrc']}")
            for rep in build_frame(rows, m["brightness"]):
                dev.write(b"\x00" + rep)
            # drain the VIA echo so the report buffer doesn't fill up
            while dev.read(REPORT_LEN):
                pass
        except KeyboardInterrupt:
            break
        except OSError as err:
            print(f"Device error ({err}); reconnecting...", file=sys.stderr)
            try:
                dev.close()
            except Exception:
                pass
            dev = None
            time.sleep(2)
            continue
        time.sleep(args.interval)

    if dev is not None:
        dev.close()
    lhmlib.close()
    ryzen.close()


if __name__ == "__main__":
    main()
