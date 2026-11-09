#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Limit log: a long-term record of what held the CPU and dGPU back.

The pad shows *that* a limit binds and names it once per episode. This keeps
the evidence: which limits bound, when, for how long, and what the machine was
doing at the time (power source and charger contract, Windows power mode,
temperatures and fans, clocks, load, the processes behind it), so the reasons
can be tallied later and conclusions drawn from data rather than from
impressions.

It is fed by the engine's poll loop (fw16_sysmon.run) once per tick, with the
readings the pad is drawn from: one set of sensors, one SMU handle, the same
verdicts. Slow readings (framework_tool, process list, power mode, extra
NVML/PDH counters) are polled on a thread of their own; the database is
written on another. Nothing here may stop the service: every entry point
catches its own errors and logs them, rate-limited.

Where: limitlog.sqlite in the data directory (%ProgramData%\\FW16Sysmon when
installed, beside the script from source). One SQLite file with a rollback
journal, not WAL: the directory gives Users read-only access, and a WAL
database can't be read without write access to its -shm file. Read it with
`fw16-sysmon.exe --limit-report`, or limitreport.py on a copy.

Tables
  meta      key/value: schema version, machine (CPU, BIOS), thresholds in force
  samples   one wide row of readings: every LIMITLOG_ACTIVE_S while a limit
            binds or a component is busy, every LIMITLOG_IDLE_S otherwise, and
            at every episode start/end and event. Pruned after
            LIMITLOG_RETENTION_DAYS, and oldest-first past LIMITLOG_MAX_MB.
  episodes  one row per limit episode, per component and reason: start, end,
            how close to the limit, the limit's own range (STAPM moves), clocks,
            temperatures, the power context at its start, the other limits
            that bound alongside, whether the pad showed it, the processes
            busy during it. Written when it starts, completed when it ends.
  events    state changes: start/stop, suspend/resume and gaps, AC and
            charger contract, power mode, charge cap, the EC's own throttle
            flags, the pad connecting, messages sent -- and every change of an
            SMU limit (STAPM, PPT, ...) with the full context it changed in.
  tally     seconds bound / primary / bound-while-busy per day, component,
            reason, power source and power mode.
  pmtable   the raw SMU power-metrics table (zlib'd float32s) at each CPU
            episode start and every LIMITLOG_PMTABLE_S: the fields libryzenadj
            doesn't name, for decoding later.
  Episodes, events and tallies are kept for good; they are small.

Reasons are logged raw: every limit at its threshold, on battery as well as on
AC, whatever LIMIT_KINDS says. The pad's own (filtered, held) verdict is kept
beside them (samples.cpu_pad / gpu_pad, episodes.pad).

  cpu  stapm fast slow tdc tdc_soc edc edc_soc thm  -- SMU table at its threshold
                                                      (APU_LIMIT_NEAR, APU_THM_MARGIN_C)
       stt_apu      skin temperature at its STT limit (when the table has it)
       base_clock   held at the base clock under load, no SMU limit near (CpuClock)
       pkg_power    the LHM package-power heuristic (only without ryzenadj)
       ec_sppt      the EC asserts its soft APU throttle (AMD SPPT)
       prochot      the EC asserts PROCHOT#
  gpu  power        NVML SwPowerCap, drawing near the enforced limit (NV_NEAR_CAP)
       brake        NVML HwPowerBrakeSlowdown
       sw_thermal   NVML SwThermalSlowdown
       hw_thermal   NVML HwThermalSlowdown
       hw_slowdown  NVML HwSlowdown with neither brake nor thermal set
"""

import ctypes
import json
import math
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import uuid
import zlib

import psutil

import paths
from applog import log

SCHEMA_VERSION = 1
DB_NAME = "limitlog.sqlite"

FLUSH_S = 10.0     # the writer commits at least this often while it has rows
GAP_S = 5.0        # a tick this late means time went missing (sleep, a stall)
CLOSE_S = 4.0      # close() waits at most this long for the threads in all

# Hard throttles outrank the SMU's own limits when both bind at once: the EC
# asserting SPPT or PROCHOT# is the cause; the SMU limit it lowers, the effect.
EC_RATIO = 1.05
PROCHOT_RATIO = 1.1

NV_SW_POWER, NV_HW_SLOWDOWN, NV_SW_THERMAL, NV_HW_THERMAL, NV_HW_BRAKE = (
    0x04, 0x08, 0x20, 0x40, 0x80)

# Moves of these are logged as "smu_limits" events: once one is more than its
# tolerance away from where the last event left it (not from the previous
# read). Some limits drift: STAPM on Strix Point floats between 35 and 63 W,
# a fraction of a watt per read, so a read-to-read compare saw only jitter.
# A drifting limit logs one event per SMU_LIMIT_TOL moved. Not stt_dgpu_l: on
# the FW16's Strix Point table (0x5d000b) libryzenadj decodes it from a field
# that moves every read (1.3 -> 1.0 -> 0.8), which would bury the real
# changes; the samples still keep it.
SMU_LIMIT_KEYS = ("stapm_l", "fast_l", "slow_l", "apu_slow_l", "tdc_l",
                  "tdc_soc_l", "edc_l", "edc_soc_l", "thm_l", "stt_apu_l",
                  "stapm_time", "slow_time")
SMU_LIMIT_TOL = 2.0  # W, A or C; idle STAPM wobbles about 1 W at its ceiling
SMU_LIMIT_TOL_KEY = {"stapm_time": 0.2, "slow_time": 0.2}  # seconds

# Windows power modes (the overlay the Settings slider picks) and plans.
POWER_MODES = {
    "961cc777-2547-4f9d-8174-7d86181b8a7a": "efficiency",
    "00000000-0000-0000-0000-000000000000": "balanced",
    "3af9b8d9-7c97-431d-ad78-34a8bfea439f": "better_perf",
    "ded574b5-45a0-4f42-8737-46345c09c238": "performance",
}
POWER_PLANS = {
    "381b4222-f694-41f0-9685-ff5bb260df2e": "balanced",
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "high_perf",
    "a1841308-3541-4fab-bc81-f71556f20b4a": "power_saver",
    "e9a42b02-d5df-448d-aa00-03f14749eb61": "ultimate",
}

# framework_tool --thermal sensor names -> sample columns.
EC_TEMPS = {"ambient": "ec_ambient", "charger": "ec_charger", "apu": "ec_apu",
            "cpu": "ec_cpu", "gpu_amb": "ec_gpu_amb", "gpu_vr": "ec_gpu_vr",
            "gpu_vram": "ec_gpu_vram", "gpu_temp": "ec_gpu"}
EC_FANS = {"left fan": "fan_l", "right fan": "fan_r"}

R, I, T = "REAL", "INTEGER", "TEXT"
SAMPLE_COLS = [
    ("ts", R), ("why", T),
    # power source
    ("ac", I), ("power_src", T), ("batt_pct", I), ("batt_state", T),
    ("batt_rate_w", R), ("charge_cap", I),
    ("pd_w", R), ("pd_v", R), ("pd_a", R), ("pd_port", I),
    ("chg_input_ma", I), ("chg_ma", I), ("batt_v", R), ("batt_ma", I),
    ("power_mode", T), ("power_plan", T), ("energy_saver", I),
    # CPU load and clocks (Windows' view)
    ("cpu_util", R), ("cpu_busiest", R), ("cpu_busy_cores", I),
    ("cpu_ghz_max", R), ("cpu_ghz_avg", R), ("cpu_base_ghz", R),
    ("clock_held", I), ("perf_pct", R), ("perf_limit_pct", R),
    ("perf_limit_flags", I), ("utility_pct", R),
    # SMU table (value / limit)
    ("stapm_v", R), ("stapm_l", R), ("fast_v", R), ("fast_l", R),
    ("slow_v", R), ("slow_l", R), ("apu_slow_v", R), ("apu_slow_l", R),
    ("tdc_v", R), ("tdc_l", R), ("tdc_soc_v", R), ("tdc_soc_l", R),
    ("edc_v", R), ("edc_l", R), ("edc_soc_v", R), ("edc_soc_l", R),
    ("thm_v", R), ("thm_l", R), ("stt_apu_v", R), ("stt_apu_l", R),
    ("stt_dgpu_v", R), ("stt_dgpu_l", R), ("stapm_time", R), ("slow_time", R),
    ("cclk_setpoint", R), ("cclk_busy", R), ("socket_w", R), ("soc_w", R),
    ("gfx_clk", R), ("gfx_temp", R), ("fclk", R), ("mclk", R), ("l3_clk", R),
    ("core_clk_max", R), ("core_clk_avg", R), ("core_power_sum", R),
    ("core_temp_max", R),
    # LibreHardwareMonitor
    ("pkg_w", R),
    # temperatures, fans, the EC's throttle flags
    ("temp_c", R), ("temp_src", T),
    ("ec_ambient", R), ("ec_charger", R), ("ec_apu", R), ("ec_cpu", R),
    ("ec_gpu_amb", R), ("ec_gpu_vr", R), ("ec_gpu_vram", R), ("ec_gpu", R),
    ("fan_l", R), ("fan_r", R), ("ec_sppt", I), ("ec_prochot", I),
    # GPUs, memory
    ("igpu", R), ("npu", R), ("dgpu", R), ("dgpu_state", T),
    ("nv_w", R), ("nv_limit_w", R), ("nv_default_w", R), ("nv_reasons", I),
    ("nv_temp", R), ("nv_gfx_clk", R), ("nv_mem_clk", R), ("nv_pstate", I),
    ("mem_pct", R),
    # verdicts: raw (closest first), the closest, and what the pad named
    ("cpu_bound", T), ("cpu_primary", T), ("cpu_pad", T),
    ("gpu_bound", T), ("gpu_primary", T), ("gpu_pad", T),
    ("top_procs", T), ("extra", T),
]
SAMPLE_NAMES = [c for c, _ in SAMPLE_COLS]

EPISODE_COLS = [
    ("id", "INTEGER PRIMARY KEY"), ("component", T), ("reason", T),
    ("start", R), ("end", R), ("duration", R), ("bound_s", R), ("primary_s", R),
    ("peak_ratio", R), ("mean_ratio", R), ("unit", T),
    ("limit_first", R), ("limit_min", R), ("limit_max", R),
    ("cpu_util_mean", R), ("cpu_ghz_mean", R), ("cpu_ghz_min", R),
    ("socket_w_mean", R), ("pkg_w_mean", R),
    ("ec_apu_max", R), ("ec_ambient_max", R), ("ec_cpu_max", R), ("fan_max", R),
    ("dgpu_mean", R), ("nv_w_mean", R),
    # the context at the start
    ("ac", I), ("power_src", T), ("power_mode", T), ("power_plan", T),
    ("pd_w", R), ("batt_pct", I), ("batt_state", T), ("batt_rate_w", R),
    ("stapm_l", R), ("slow_l", R), ("fast_l", R), ("stapm_time", R),
    ("ec_apu_start", R), ("ec_ambient_start", R),
    ("others", T), ("pad", T), ("top_procs", T), ("end_cause", T),
]
EPISODE_NAMES = [c for c, _ in EPISODE_COLS]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS samples ({", ".join(f"{c} {t}" for c, t in SAMPLE_COLS)});
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
CREATE TABLE IF NOT EXISTS episodes ({", ".join(f'"{c}" {t}' for c, t in EPISODE_COLS)});
CREATE INDEX IF NOT EXISTS episodes_start ON episodes (start);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS tally (day TEXT, component TEXT, reason TEXT,
    power_src TEXT, power_mode TEXT, bound_s REAL, primary_s REAL, loaded_s REAL,
    PRIMARY KEY (day, component, reason, power_src, power_mode));
CREATE TABLE IF NOT EXISTS pmtable (id INTEGER PRIMARY KEY, ts REAL, version INTEGER,
    size INTEGER, episode INTEGER, why TEXT, data BLOB);
CREATE INDEX IF NOT EXISTS pmtable_ts ON pmtable (ts);
"""


def db_path():
    return os.path.join(paths.data_dir(), DB_NAME)


def _round(v, nd=3):
    return None if v is None else round(float(v), nd)


def _machine():
    """CPU, BIOS and board, from the registry: a BIOS update moves limits."""
    import winreg
    info = {}
    for key, names in (
            (r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
             ("ProcessorNameString",)),
            (r"HARDWARE\DESCRIPTION\System\BIOS",
             ("BIOSVersion", "BIOSReleaseDate", "SystemManufacturer",
              "SystemProductName", "SystemSKU", "BaseBoardProduct"))):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                for n in names:
                    try:
                        info[n] = str(winreg.QueryValueEx(k, n)[0]).strip()
                    except OSError:
                        pass
        except OSError:
            pass
    info["logical_cpus"] = psutil.cpu_count(logical=True)
    info["physical_cpus"] = psutil.cpu_count(logical=False)
    return info


class _Reason:
    """A raw limit at its threshold this tick: key, ratio, value, unit."""
    __slots__ = ("key", "ratio", "value", "unit")

    def __init__(self, key, ratio=1.0, value=None, unit=""):
        self.key = key
        self.ratio = ratio
        self.value = value
        self.unit = unit


_ntquery = None


def _process_times():
    """{pid: (image name, CPU seconds)} for every process, from one
    NtQuerySystemInformation(SystemProcessInformation) snapshot: about a
    millisecond. psutil takes seconds for the same, as it falls back to a
    whole-system query per process it may not open (x64 layout below)."""
    global _ntquery
    if _ntquery is None:
        fn = ctypes.WinDLL("ntdll").NtQuerySystemInformation
        fn.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                       ctypes.POINTER(ctypes.c_ulong)]
        fn.restype = ctypes.c_ulong
        _ntquery = [fn, 1 << 20]
    fn, size = _ntquery
    for _ in range(5):
        buf = ctypes.create_string_buffer(size)
        need = ctypes.c_ulong(0)
        status = fn(5, buf, size, ctypes.byref(need))  # SystemProcessInformation
        if status == 0:
            break
        if status != 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
            raise OSError(f"NtQuerySystemInformation: 0x{status:08X}")
        size = _ntquery[1] = max(size * 2, need.value + 65536)
    else:
        raise OSError("NtQuerySystemInformation: the process list kept growing")
    raw = buf.raw
    base = ctypes.addressof(buf)
    out = {}
    off = 0
    while True:
        nxt = int.from_bytes(raw[off:off + 4], "little")
        user = int.from_bytes(raw[off + 40:off + 48], "little", signed=True)
        kern = int.from_bytes(raw[off + 48:off + 56], "little", signed=True)
        nlen = int.from_bytes(raw[off + 56:off + 58], "little")
        nptr = int.from_bytes(raw[off + 64:off + 72], "little")
        pid = int.from_bytes(raw[off + 80:off + 88], "little")
        if pid and nptr and base <= nptr < base + size:
            name = raw[nptr - base:nptr - base + nlen].decode("utf-16-le", "replace")
            out[pid] = (name, (user + kern) / 1e7)
        if not nxt:
            break
        off += nxt
    return out


def _primary(reasons):
    return max(reasons, key=lambda k: reasons[k].ratio) if reasons else None


def _bound_text(reasons):
    """Raw reasons, closest to their limit first: 'stapm,slow'."""
    return ",".join(sorted(reasons, key=lambda k: -reasons[k].ratio)) or None


# ---------------------------------------------------------------------------
# Slow readings, on a thread of their own
# ---------------------------------------------------------------------------

class _Extras:
    """Readings too slow (or too costly) for the ~0.4 s loop, each polled at
    its own pace, faster while the machine is busy. snapshot() hands the loop
    the latest of each; a reading can be a few seconds old."""

    def __init__(self, eng, nv, dwatch):
        self.eng = eng
        self.nv = nv
        self.dwatch = dwatch
        self.active = False   # set by the recorder each tick
        self.pd_now = False   # the recorder asks for a PD re-read (AC changed)
        self._lock = threading.Lock()
        self._state = {}
        self._stop = threading.Event()
        self._due = {}
        self._failed = {}     # task -> error text logged
        self._dead = set()    # tasks that can't work here (no framework_tool)
        self._pdh = None
        self._procs_prev = None
        self._was_active = False
        self._thread = threading.Thread(target=self._run, name="limitlog-extras",
                                        daemon=True)

    def start(self):
        self._thread.start()

    def stop(self, timeout):
        self._stop.set()
        self._thread.join(timeout)

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def _put(self, **kv):
        with self._lock:
            self._state.update(kv)

    def _run(self):
        E = self.eng
        tasks = (
            ("power_mode", self._power_mode, lambda a: 5.0),
            ("pdh", self._read_pdh, lambda a: 2.0),
            ("nvml", self._nvml, lambda a: 2.0),
            ("ec_thermal", self._ec_thermal,
             lambda a: E.LIMITLOG_EC_S if a else E.LIMITLOG_EC_IDLE_S),
            ("ec_power", self._ec_power, lambda a: 30.0 if a else 120.0),
            ("pdports", self._pdports, lambda a: 60.0 if a else 300.0),
            ("procs", self._procs,
             lambda a: E.LIMITLOG_PROC_S * (1 if a else 6)),
        )
        while not self._stop.is_set():
            active = self.active
            if active and not self._was_active:
                # just got busy: read the fast-moving ones now, not at the
                # idle pace's next turn
                for name in ("ec_thermal", "procs"):
                    self._due[name] = 0.0
            self._was_active = active
            if self.pd_now:
                self.pd_now = False
                self._due["pdports"] = self._due["ec_power"] = 0.0
            for name, fn, period in tasks:
                if self._stop.is_set():
                    break
                if name in self._dead or time.monotonic() < self._due.get(name, 0.0):
                    continue
                self._due[name] = time.monotonic() + max(0.5, period(active))
                try:
                    fn()
                    self._failed.pop(name, None)
                except FileNotFoundError:
                    self._dead.add(name)  # framework_tool isn't installed
                except Exception as err:
                    text = f"{type(err).__name__}: {err}"
                    if self._failed.get(name) != text:
                        self._failed[name] = text
                        log(f"Limit log: {name} reading failed ({text})")
            self._stop.wait(0.5)

    # --- the readings ------------------------------------------------------

    _powrprof = None

    def _power_mode(self):
        if self._powrprof is None:
            pp = ctypes.WinDLL("powrprof")
            pp.PowerGetActiveScheme.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_void_p)]
            self._powrprof = pp
        pp = self._powrprof
        mode = plan = None
        buf = (ctypes.c_ubyte * 16)()
        if pp.PowerGetEffectiveOverlayScheme(ctypes.byref(buf)) == 0:
            g = str(uuid.UUID(bytes_le=bytes(buf)))
            mode = POWER_MODES.get(g, g)
        else:
            # fallback: the registry's per-source overlay
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    r"SYSTEM\CurrentControlSet\Control\Power"
                                    r"\User\PowerSchemes") as k:
                    batt = psutil.sensors_battery()
                    name = ("ActiveOverlayDcPowerScheme"
                            if batt is not None and not batt.power_plugged
                            else "ActiveOverlayAcPowerScheme")
                    g = str(winreg.QueryValueEx(k, name)[0]).lower()
                    mode = POWER_MODES.get(g, g)
            except OSError:
                pass
        ptr = ctypes.c_void_p()
        if pp.PowerGetActiveScheme(None, ctypes.byref(ptr)) == 0 and ptr.value:
            g = str(uuid.UUID(bytes_le=ctypes.string_at(ptr.value, 16)))
            ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(ptr.value))
            plan = POWER_PLANS.get(g, g)

        class _SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                        ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte),
                        ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong),
                        ("BatteryFullLifeTime", ctypes.c_ulong)]
        sps = _SPS()
        saver = None
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
            saver = int(sps.SystemStatusFlag & 1)
        self._put(power_mode=mode, power_plan=plan, energy_saver=saver)

    def _read_pdh(self):
        pdh = self.eng.win32pdh
        if pdh is None:
            raise FileNotFoundError("win32pdh")
        if self._pdh is None:
            q = pdh.OpenQuery()
            ctrs = {}
            for key, path in (
                    ("perf_pct", r"\Processor Information(_Total)\% Processor Performance"),
                    ("perf_limit_pct", r"\Processor Information(_Total)\% Performance Limit"),
                    ("perf_limit_flags", r"\Processor Information(_Total)\Performance Limit Flags"),
                    ("utility_pct", r"\Processor Information(_Total)\% Processor Utility")):
                try:
                    ctrs[key] = pdh.AddEnglishCounter(q, path)
                except Exception:
                    pass
            pdh.CollectQueryData(q)  # rates need two samples
            self._pdh = (q, ctrs)
            return
        q, ctrs = self._pdh
        pdh.CollectQueryData(q)
        vals = {}
        for key, c in ctrs.items():
            try:
                vals[key] = round(pdh.GetFormattedCounterValue(
                    c, pdh.PDH_FMT_DOUBLE)[1], 2)
            except Exception:
                vals[key] = None
        if vals.get("perf_limit_flags") is not None:
            vals["perf_limit_flags"] = int(vals["perf_limit_flags"])
        self._put(**vals)

    def _nvml(self):
        """dGPU temperature, clocks, P-state: only while it is awake (D0), as
        NVML calls can power a sleeping one up."""
        nv, pynvml = self.nv, self.eng.pynvml
        if nv is None or pynvml is None or not nv.available:
            return
        state = self.dwatch.snapshot()[0] if self.dwatch is not None else None
        if state != "D0":
            self._put(nv_temp=None, nv_gfx_clk=None, nv_mem_clk=None, nv_pstate=None)
            return
        h = nv.handle
        vals = {}
        for key, fn in (
                ("nv_temp", lambda: pynvml.nvmlDeviceGetTemperature(
                    h, pynvml.NVML_TEMPERATURE_GPU)),
                ("nv_gfx_clk", lambda: pynvml.nvmlDeviceGetClockInfo(
                    h, pynvml.NVML_CLOCK_GRAPHICS)),
                ("nv_mem_clk", lambda: pynvml.nvmlDeviceGetClockInfo(
                    h, pynvml.NVML_CLOCK_MEM)),
                ("nv_pstate", lambda: pynvml.nvmlDeviceGetPerformanceState(h))):
            try:
                vals[key] = fn()
            except Exception:
                vals[key] = None
        self._put(**vals)

    def _ft(self, *args, timeout=4):
        # this thread can wait its turn (the poll loop's own calls can't)
        return self.eng.run_framework_tool(*args, timeout=timeout, wait=10.0)

    _EC_VALUE = re.compile(r"^\s*(?P<name>[^:]+?):\s+(?P<val>-?\d+)\s*(?P<unit>C|RPM)\s*$")
    _EC_FLAG = re.compile(r"^\s*(?P<name>Soft|Hard)\b[^:]*:\s*(?P<val>true|false)\s*$", re.I)

    def _ec_thermal(self):
        """EC temperatures, fan speeds, and the EC's APU throttle flags."""
        out = self._ft("--thermal")
        if out is None:
            return
        vals = {}
        extra = {}
        for line in out.splitlines():
            m = self._EC_VALUE.match(line)
            if m:
                name = m.group("name").strip()
                val = int(m.group("val"))
                if m.group("unit") == "RPM":
                    col = EC_FANS.get(name.lower())
                    (vals if col else extra)[col or name] = val
                elif 0 < val < 150:  # 255 / negative: absent sensor
                    short = name.split("@")[0].replace("_f75303", "")
                    col = EC_TEMPS.get(short)
                    (vals if col else extra)[col or short] = val
                continue
            m = self._EC_FLAG.match(line)
            if m:
                key = "ec_sppt" if m.group("name").lower() == "soft" else "ec_prochot"
                vals[key] = int(m.group("val").lower() == "true")
        if vals or extra:
            self._put(ec=vals, ec_extra=extra or None, ec_t=time.time())

    _KV = re.compile(r"^\s*(?P<k>[A-Za-z][A-Za-z .()#/-]*?):\s*(?P<v>\S.*?)\s*$")

    @staticmethod
    def _int(text):
        m = re.match(r"-?\d+", text)
        return int(m.group(0)) if m else None

    def _ec_power(self):
        """The charger's and battery's electrical state from the EC."""
        out = self._ft("--power", "-vv")
        if out is None:
            return
        vals = {}
        for line in out.splitlines():
            m = self._KV.match(line)
            if not m:
                continue
            k, v = m.group("k").strip(), m.group("v")
            if k == "Chg Input Current":
                vals["chg_input_ma"] = self._int(v)
            elif k == "Charger Current":
                vals["chg_ma"] = self._int(v)
            elif k == "Present Voltage":
                try:
                    vals["batt_v"] = float(v.split()[0])
                except (ValueError, IndexError):
                    pass
            elif k == "Present Rate":
                vals["batt_ma"] = self._int(v)
        self._put(**vals)

    def _pdports(self):
        """The USB-PD contract the laptop sinks power through."""
        out = self._ft("--pdports", timeout=6)
        if out is None:
            return
        ports, cur = [], None
        for line in out.splitlines():
            m = re.match(r"^\s*USB-C Port (\d+):", line)
            if m:
                cur = {"port": int(m.group(1))}
                ports.append(cur)
                continue
            m = self._KV.match(line)
            if cur is None or not m:
                continue
            k, v = m.group("k").strip(), m.group("v")
            if k == "PD Contract":
                cur["contract"] = v.lower().startswith("y")
            elif k == "Power Role":
                cur["role"] = v
            elif k == "Port Partner":
                cur["partner"] = v
            elif k == "EPR":
                cur["epr"] = v
            elif k == "Negotiated":
                n = re.match(r"([\d.]+)\s*V,\s*(\d+)\s*mA,\s*([\d.]+)\s*W", v)
                if n:
                    cur["v"], cur["ma"], cur["w"] = (float(n.group(1)),
                                                     int(n.group(2)),
                                                     float(n.group(3)))
        sinks = [p for p in ports if p.get("contract") and p.get("w")
                 and str(p.get("role", "")).lower() == "sink"]
        best = max(sinks, key=lambda p: p["w"]) if sinks else None
        self._put(pd_ports=ports or None,
                  pd_w=best["w"] if best else None,
                  pd_v=best["v"] if best else None,
                  pd_a=best["ma"] / 1000.0 if best else None,
                  pd_port=best["port"] if best else None)

    def _procs(self):
        """The processes with the most CPU time since the last look, summed
        by name, as % of the whole machine: 'cinebench.exe:93.1;...'."""
        now = time.monotonic()
        cur = _process_times()
        prev, self._procs_prev = self._procs_prev, (now, cur)
        if prev is None or now <= prev[0]:
            return
        span = now - prev[0]
        ncpu = psutil.cpu_count() or 1
        busy = {}
        for pid, (name, t) in cur.items():
            old = prev[1].get(pid)
            if old is None or old[0] != name:
                continue  # new since the last look (or a reused pid)
            d = t - old[1]
            if d > 0:
                busy[name] = busy.get(name, 0.0) + d
        top = sorted(busy.items(), key=lambda kv: -kv[1])[:5]
        text = ";".join(f"{n}:{100.0 * d / span / ncpu:.1f}" for n, d in top
                        if 100.0 * d / span / ncpu >= 0.5)
        self._put(top_procs=text or None, procs_t=time.time())


# ---------------------------------------------------------------------------
# The database, on a thread of its own
# ---------------------------------------------------------------------------

class _Writer:
    """Owns the SQLite connection; batches what the recorder hands it."""

    def __init__(self, path, eng):
        self.path = path
        self.eng = eng
        self.q = queue.Queue(maxsize=50000)
        self.dropped = 0
        self.machine = {}
        self._ready = threading.Event()
        self._err = None
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._fail_logged = None
        self._thread = threading.Thread(target=self._run, name="limitlog-writer",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(15):
            raise RuntimeError(f"{path} did not open within 15 s")
        if self._err is not None:
            raise self._err

    def new_id(self):
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def put(self, op):
        try:
            self.q.put_nowait(op)
        except queue.Full:
            self.dropped += 1

    def close(self, timeout):
        try:
            self.q.put(("close",), timeout=timeout)
        except queue.Full:
            pass
        self._thread.join(timeout)

    # --- writer thread -----------------------------------------------------

    def _open(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        new = not os.path.exists(self.path)
        # 4 s a try while opening (three tries fit Recorder's 15 s wait), 10 s
        # for the batches after
        db = sqlite3.connect(self.path, timeout=4, isolation_level=None,
                             check_same_thread=False)
        if new:
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")  # before any table
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript(SCHEMA)
        # columns added by a later version: ALTER TABLE the missing ones in
        for table, cols in (("samples", SAMPLE_COLS), ("episodes", EPISODE_COLS)):
            have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            for c, t in cols:
                if c not in have:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN "{c}" {t}')
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        db.execute("INSERT OR IGNORE INTO meta VALUES ('created', ?)", (now,))
        try:
            self.machine = _machine()
        except Exception:
            self.machine = {}
        for k, v in (("schema_version", SCHEMA_VERSION),
                     ("utc_offset_s", time.localtime().tm_gmtoff),
                     ("machine", json.dumps(self.machine))):
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, str(v)))
        self._next_id = db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM episodes").fetchone()[0]
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def _run(self):
        for attempt in range(3):
            try:
                db = self._open()
                break
            except sqlite3.OperationalError as err:
                # a reader holding the file (a long report) as the service
                # starts: wait it out rather than log nothing until a restart
                if attempt == 2 or "locked" not in str(err):
                    self._err = err
                    self._ready.set()
                    return
            except Exception as err:
                self._err = err
                self._ready.set()
                return
        self._ready.set()
        pending = []
        last_commit = time.monotonic()
        next_prune = time.monotonic() + 120.0
        closing = False
        while True:
            try:
                op = self.q.get(timeout=1.0)
            except queue.Empty:
                op = None
            if op is not None:
                if op[0] == "close":
                    closing = True
                else:
                    pending.append(op)
                # take what else is waiting without blocking
                while len(pending) < 2000:
                    try:
                        op = self.q.get_nowait()
                    except queue.Empty:
                        break
                    if op[0] == "close":
                        closing = True
                    else:
                        pending.append(op)
            now = time.monotonic()
            if pending and (closing or now - last_commit >= FLUSH_S
                            or len(pending) >= 1000):
                if self._apply(db, pending):
                    pending = []
                elif len(pending) > 20000:
                    # the file stays locked or unwritable: keep the newest
                    self.dropped += len(pending) - 10000
                    pending = pending[-10000:]
                last_commit = now
            if closing:
                try:
                    db.close()
                except Exception:
                    pass
                return
            if now >= next_prune:
                next_prune = now + 3600.0
                self._prune(db)

    def _apply(self, db, ops):
        """Write a batch in one transaction; False to retry it later."""
        try:
            db.execute("BEGIN IMMEDIATE")
            for op in ops:
                kind = op[0]
                if kind == "sample":
                    row = op[1]
                    db.execute(self._sample_sql, [row.get(c) for c in SAMPLE_NAMES])
                elif kind == "episode":
                    row = op[1]
                    db.execute(self._episode_sql, [row.get(c) for c in EPISODE_NAMES])
                elif kind == "event":
                    db.execute("INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
                               op[1:4])
                elif kind == "tally":
                    db.executemany(
                        "INSERT INTO tally VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (day, component, reason, power_src, power_mode) "
                        "DO UPDATE SET bound_s = bound_s + excluded.bound_s, "
                        "primary_s = primary_s + excluded.primary_s, "
                        "loaded_s = loaded_s + excluded.loaded_s",
                        [k + tuple(v) for k, v in op[1].items()])
                elif kind == "pmtable":
                    ts, ver, data, episode, why = op[1:6]
                    db.execute("INSERT INTO pmtable (ts, version, size, episode, why, data) "
                               "VALUES (?, ?, ?, ?, ?, ?)",
                               (ts, ver, len(data), episode, why, zlib.compress(data, 6)))
            db.execute("COMMIT")
            self._fail_logged = None
            return True
        except Exception as err:
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
            text = f"{type(err).__name__}: {err}"
            if self._fail_logged != text:
                self._fail_logged = text
                log(f"Limit log: write failed, will retry ({text})")
            return False

    _sample_sql = (f"INSERT INTO samples ({', '.join(SAMPLE_NAMES)}) "
                   f"VALUES ({', '.join('?' * len(SAMPLE_NAMES))})")
    _episode_sql = (f"INSERT OR REPLACE INTO episodes "
                    f"({', '.join(chr(34) + c + chr(34) for c in EPISODE_NAMES)}) "
                    f"VALUES ({', '.join('?' * len(EPISODE_NAMES))})")

    def _prune(self, db):
        """Samples and raw tables past the retention, then oldest-first past
        the size cap. Episodes, events and tallies stay."""
        E = self.eng
        try:
            cutoff = time.time() - E.LIMITLOG_RETENTION_DAYS * 86400.0
            db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            db.execute("DELETE FROM pmtable WHERE ts < ?", (cutoff,))
            page = db.execute("PRAGMA page_size").fetchone()[0]
            for _ in range(20):
                used = (db.execute("PRAGMA page_count").fetchone()[0]
                        - db.execute("PRAGMA freelist_count").fetchone()[0]) * page
                if used <= E.LIMITLOG_MAX_MB * 1048576:
                    break
                n = db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
                if n < 1000:
                    break
                db.execute("DELETE FROM samples WHERE rowid IN (SELECT rowid "
                           "FROM samples ORDER BY ts LIMIT ?)", (n // 10,))
                db.execute("DELETE FROM pmtable WHERE ts < (SELECT MIN(ts) FROM samples)")
            db.execute("PRAGMA incremental_vacuum")
        except Exception as err:
            log(f"Limit log: pruning failed ({type(err).__name__}: {err})")


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------

class _Mean:
    __slots__ = ("s", "n", "lo", "hi")

    def __init__(self):
        self.s = 0.0
        self.n = 0
        self.lo = None
        self.hi = None

    def add(self, v):
        if v is None:
            return
        v = float(v)
        self.s += v
        self.n += 1
        self.lo = v if self.lo is None else min(self.lo, v)
        self.hi = v if self.hi is None else max(self.hi, v)

    @property
    def mean(self):
        return self.s / self.n if self.n else None


class _Episode:
    """One reason's run, from its first read to LIMIT_RELEASE_S after its last."""

    def __init__(self, comp, key, wall, now, ctx):
        self.comp = comp
        self.key = key
        self.first = wall          # wall time first seen
        self.last = wall           # ...and last seen
        self.first_mono = now
        self.last_mono = now
        self.open = False          # written as an episode (lasted ONSET)
        self.id = None
        self.bound_s = 0.0
        self.primary_s = 0.0
        self.peak = 0.0
        self.ratio = _Mean()
        self.limit = _Mean()
        self.limit_first = None
        self.unit = ""
        self.stats = {k: _Mean() for k in (
            "cpu_util", "cpu_ghz", "socket_w", "pkg_w", "ec_apu", "ec_ambient",
            "ec_cpu", "fan", "dgpu", "nv_w")}
        self.ctx = ctx
        self.others = set()
        self.pad = set()
        self.procs = {}            # name -> summed %
        self.procs_n = 0
        self._procs_t = None

    def see(self, wall, now, dt, r, primary, others, pad_key, s, ex):
        self.last = wall
        self.last_mono = now
        self.bound_s += dt
        if primary:
            self.primary_s += dt
        self.peak = max(self.peak, r.ratio)
        self.ratio.add(r.ratio)
        if r.value is not None:
            self.limit.add(r.value)
            if self.limit_first is None:
                self.limit_first = r.value
        self.unit = r.unit or self.unit
        self.others.update(others)
        if pad_key:
            self.pad.add(pad_key)
        st = self.stats
        st["cpu_util"].add(s.get("cpu_util"))
        st["cpu_ghz"].add(s.get("cpu_ghz_avg"))
        st["socket_w"].add(s.get("socket_w"))
        st["pkg_w"].add(s.get("pkg_w"))
        st["ec_apu"].add(s.get("ec_apu"))
        st["ec_ambient"].add(s.get("ec_ambient"))
        st["ec_cpu"].add(s.get("ec_cpu"))
        fans = [v for v in (s.get("fan_l"), s.get("fan_r")) if v is not None]
        st["fan"].add(max(fans) if fans else None)
        st["dgpu"].add(s.get("dgpu"))
        st["nv_w"].add(s.get("nv_w"))
        if ex.get("procs_t") != self._procs_t:
            self._procs_t = ex.get("procs_t")
            if ex.get("top_procs"):
                self.procs_n += 1
                for part in ex["top_procs"].split(";"):
                    name, _, pct = part.rpartition(":")
                    try:
                        self.procs[name] = self.procs.get(name, 0.0) + float(pct)
                    except ValueError:
                        pass

    def row(self, end_cause=None):
        st, c = self.stats, self.ctx
        procs = ""
        if self.procs_n:
            top = sorted(self.procs.items(), key=lambda kv: -kv[1])[:5]
            procs = ";".join(f"{n}:{v / self.procs_n:.1f}" for n, v in top)
        ended = end_cause is not None
        return {
            "id": self.id, "component": self.comp, "reason": self.key,
            "start": self.first, "end": self.last if ended else None,
            "duration": _round(self.last - self.first, 2),
            "bound_s": _round(self.bound_s, 2), "primary_s": _round(self.primary_s, 2),
            "peak_ratio": _round(self.peak), "mean_ratio": _round(self.ratio.mean),
            "unit": self.unit or None,
            "limit_first": _round(self.limit_first), "limit_min": _round(self.limit.lo),
            "limit_max": _round(self.limit.hi),
            "cpu_util_mean": _round(st["cpu_util"].mean, 1),
            "cpu_ghz_mean": _round(st["cpu_ghz"].mean), "cpu_ghz_min": _round(st["cpu_ghz"].lo),
            "socket_w_mean": _round(st["socket_w"].mean, 2),
            "pkg_w_mean": _round(st["pkg_w"].mean, 2),
            "ec_apu_max": st["ec_apu"].hi, "ec_ambient_max": st["ec_ambient"].hi,
            "ec_cpu_max": st["ec_cpu"].hi, "fan_max": st["fan"].hi,
            "dgpu_mean": _round(st["dgpu"].mean, 1), "nv_w_mean": _round(st["nv_w"].mean, 2),
            "ac": c.get("ac"), "power_src": c.get("power_src"),
            "power_mode": c.get("power_mode"), "power_plan": c.get("power_plan"),
            "pd_w": c.get("pd_w"), "batt_pct": c.get("batt_pct"),
            "batt_state": c.get("batt_state"), "batt_rate_w": c.get("batt_rate_w"),
            "stapm_l": c.get("stapm_l"), "slow_l": c.get("slow_l"),
            "fast_l": c.get("fast_l"), "stapm_time": c.get("stapm_time"),
            "ec_apu_start": c.get("ec_apu"), "ec_ambient_start": c.get("ec_ambient"),
            "others": ",".join(sorted(self.others)) or None,
            "pad": ",".join(sorted(self.pad)) or None,
            "top_procs": procs or c.get("top_procs"),
            "end_cause": end_cause,
        }


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------

class Recorder:
    """Fed once per poll-loop tick by fw16_sysmon.run(). See the module doc."""

    def __init__(self, eng, clock=None, nv=None, dwatch=None, path=None):
        self.eng = eng
        self.clock = clock
        self.nv = nv
        self.path = path or db_path()
        self._writer = _Writer(self.path, eng)
        self._extras = _Extras(eng, nv, dwatch)
        self._extras.start()
        eng.RyzenAdjSource.want_extras = True
        self._eps = {"cpu": {}, "gpu": {}}
        self._tally = {}
        self._tally_at = time.monotonic()
        self._last_tick = None
        self._last_wall = None
        self._next_sample = 0.0
        self._active_until = 0.0
        self._prev = {}
        self._anchor = {}          # SMU limit -> its value at the last smu_limits event
        self._busiest_ema = None   # the busiest core's %, smoothed (LIMITLOG_BUSY_TAU_S)
        self._last_rj = None
        self._next_table = time.monotonic() + 30.0
        self._table_for = None     # (episode id, why) the requested table is for
        self._resumed = False
        self._closed = False
        self._errors = {}          # where -> (count, last logged monotonic)
        self.event("start", version=getattr(eng, "__version__", "?"),
                   pid=os.getpid(), db=self.path, machine=self._writer.machine,
                   thresholds=dict({k: getattr(eng, k, None) for k in (
                       "APU_LIMIT_NEAR", "APU_THM_MARGIN_C", "LIMIT_RELEASE_S",
                       "NV_NEAR_CAP", "CLOCK_BUSY_PCT", "CLOCK_BASE_MARGIN",
                       "LIMITLOG_ONSET_S", "LIMITLOG_ACTIVE_S", "LIMITLOG_IDLE_S",
                       "LIMITLOG_BUSY_TAU_S")},
                       SMU_LIMIT_TOL=SMU_LIMIT_TOL, SMU_LIMIT_TOL_KEY=SMU_LIMIT_TOL_KEY))

    # --- entry points: none of them raises ---------------------------------

    def observe(self, m, now, cpu_track=None, gpu_track=None):
        try:
            self._observe(m, now, cpu_track, gpu_track)
        except Exception as err:
            self._error("observe", err)

    def resumed(self):
        self._resumed = True

    def pad(self, connected):
        self.event("pad", connected=bool(connected))

    def message(self, text, replay):
        self.event("message", text=text, replay=bool(replay))

    def event(self, kind, ts=None, **detail):
        try:
            self._writer.put(("event", ts or time.time(), kind,
                              json.dumps(detail, default=str) if detail else None))
        except Exception as err:
            self._error("event", err)

    def close(self, timeout=CLOSE_S):
        if self._closed:
            return
        self._closed = True
        t0 = time.monotonic()
        try:
            self.eng.RyzenAdjSource.want_extras = False
            for comp in self._eps:
                self._end_all(comp, "stop")
            self._flush_tally()
            self.event("stop", dropped=self._writer.dropped)
        except Exception as err:
            self._error("close", err)
        self._extras.stop(min(1.5, timeout))
        self._writer.close(max(0.5, timeout - (time.monotonic() - t0)))

    # --- internals ---------------------------------------------------------

    def _error(self, where, err):
        count, at = self._errors.get(where, (0, -1e9))
        count += 1
        if time.monotonic() - at >= 600.0:
            import traceback
            tb = traceback.format_exception(type(err), err, err.__traceback__)
            log(f"Limit log: {where} failed ({count}x so far): "
                f"{''.join(tb[-3:]).strip()}")
            at = time.monotonic()
        self._errors[where] = (count, at)

    def _observe(self, m, now, cpu_track, gpu_track):
        E = self.eng
        wall = time.time()
        dt = 0.0 if self._last_tick is None else now - self._last_tick
        if self._last_tick is not None and (dt > GAP_S or self._resumed):
            # time went missing (sleep, a stall): close what was running as of
            # when it was last seen, count nothing across the gap
            cause = "suspend" if self._resumed else "gap"
            for comp in self._eps:
                self._end_all(comp, cause)
            self.event("resume" if self._resumed else "gap", ts=wall,
                       since=self._last_wall, seconds=round(dt, 1))
            self._resumed = False
            self._busiest_ema = None
            dt = 0.0
        self._last_tick, self._last_wall = now, wall

        ex = self._extras.snapshot()
        rj = m.get("cpu_limits")
        new_rj = rj is not None and rj is not self._last_rj
        if rj is not None:
            self._last_rj = rj
        s = self._sample(wall, m, rj, ex, cpu_track, gpu_track)
        cpu = self._cpu_reasons(m, rj, s)
        gpu = self._gpu_reasons(m)
        s["cpu_bound"], s["cpu_primary"] = _bound_text(cpu), _primary(cpu)
        s["gpu_bound"], s["gpu_primary"] = _bound_text(gpu), _primary(gpu)

        # the busiest core, smoothed: Windows' background work puts some core
        # at 80%+ for a moment every few seconds even at idle, which held the
        # busy cadence (and the tallies' "busy" time) all night
        # (no history -- the first tick, after a gap or an unreadable counter --
        # starts from 0, so one spike then doesn't count either)
        busiest, ema = s.get("cpu_busiest"), self._busiest_ema
        tau = E.LIMITLOG_BUSY_TAU_S
        if busiest is None:
            ema = None
        elif tau <= 0:
            ema = busiest
        else:
            ema = (ema or 0.0) + (1.0 - math.exp(-dt / tau)) * (busiest - (ema or 0.0))
        self._busiest_ema = ema
        loaded = {
            "cpu": (ema is not None and ema >= E.CLOCK_BUSY_PCT)
                   or (s.get("cpu_util") or 0) >= E.CPU_LOAD_HIGH,
            "gpu": bool(m.get("dgpu_present")) and (
                (s.get("dgpu") or 0) >= 30 or (s.get("nv_w") or 0) >= E.NV_ACTIVE_MIN_W),
        }
        self._count(wall, dt, s, loaded, cpu, gpu)
        if now - self._tally_at >= FLUSH_S:
            self._flush_tally()

        why = None
        for comp, reasons, track in (("cpu", cpu, cpu_track), ("gpu", gpu, gpu_track)):
            w = self._episodes(comp, reasons, wall, now, dt, s, ex, track)
            why = why or w
        w = self._changes(wall, m, s, new_rj)
        why = why or w

        # the raw SMU table: at a CPU episode's start (asked for in
        # _episodes), and every LIMITLOG_PMTABLE_S
        if new_rj and rj.get("_table"):
            ver, data = rj["_table"]
            ep, twhy = self._table_for[:2] if self._table_for else (None, "periodic")
            self._table_for = None
            self._writer.put(("pmtable", wall, ver, data, ep, twhy))
        elif self._table_for is not None and now - self._table_for[2] > 10.0:
            self._table_for = None  # never came (no ryzenadj, or no table getter)
            E.RyzenAdjSource.want_table = False
        if (E.LIMITLOG_PMTABLE_S > 0 and now >= self._next_table
                and self._table_for is None and rj is not None):
            self._next_table = now + E.LIMITLOG_PMTABLE_S
            self._table_for = (None, "periodic", now)
            E.RyzenAdjSource.want_table = True

        busy = (bool(cpu or gpu) or loaded["cpu"] or loaded["gpu"]
                or (s.get("cpu_util") or 0) >= E.LIMITLOG_CPU_ACTIVE_PCT)
        if busy:
            self._active_until = now + E.LIMITLOG_LINGER_S
        active = now < self._active_until
        self._extras.active = active
        if why or now >= self._next_sample:
            self._next_sample = now + (E.LIMITLOG_ACTIVE_S if active
                                       else E.LIMITLOG_IDLE_S)
            s["why"] = why or ("active" if active else "idle")
            self._writer.put(("sample", s))

    def _sample(self, wall, m, rj, ex, cpu_track, gpu_track):
        """One sample row's worth of readings (the verdict columns aside)."""
        ac = bool(m.get("ac"))
        if not m.get("has_batt"):
            src = "ac"
        elif not ac:
            src = "battery"
        else:
            src = "ac_assist" if m.get("weak_charger") else "ac"
        clk = m.get("cpu_clock")
        cst = getattr(self.clock, "stats", None) or {}
        nvl = getattr(self.nv, "last", None) or {}
        s = {
            "ts": round(wall, 3),
            "ac": int(ac), "power_src": src,
            "batt_pct": m.get("raw_pct") if m.get("has_batt") else None,
            "batt_state": ("charging" if m.get("charging") else
                           "discharging" if m.get("discharging") else "idle")
                          if m.get("has_batt") else None,
            "batt_rate_w": _round(m.get("rate_w"), 2),
            "charge_cap": m.get("cap"),
            "cpu_util": m.get("cpu"),
            "cpu_busiest": _round(clk[2], 1) if clk else None,
            "cpu_ghz_max": _round(clk[1]) if clk else None,
            "clock_held": int(bool(clk[0])) if clk else None,
            "cpu_busy_cores": cst.get("busy_cores"),
            "cpu_ghz_avg": _round(cst.get("ghz_avg")),
            "cpu_base_ghz": _round(cst.get("base_ghz")),
            "pkg_w": _round(m.get("cpu_pkg_w"), 2),
            "temp_c": _round(m.get("raw_temp"), 2) if m.get("has_temp") else None,
            "temp_src": m.get("temp_src"),
            "igpu": m.get("igpu"), "npu": m.get("npu"),
            "dgpu": m.get("dgpu") if m.get("dgpu_present") else None,
            "dgpu_state": m.get("dgpu_power"),
            "mem_pct": _round(m.get("raw_mem"), 1),
            "nv_w": _round(nvl.get("draw"), 2),
            "nv_limit_w": _round(nvl.get("enforced"), 2),
            "nv_default_w": _round(nvl.get("default"), 2),
            "nv_reasons": nvl.get("reasons"),
            "cpu_pad": getattr(cpu_track, "current", None),
            "gpu_pad": getattr(gpu_track, "current", None),
        }
        for k in ("pd_w", "pd_v", "pd_a", "pd_port", "chg_input_ma", "chg_ma",
                  "batt_v", "batt_ma", "power_mode", "power_plan", "energy_saver",
                  "perf_pct", "perf_limit_pct", "perf_limit_flags", "utility_pct",
                  "nv_temp", "nv_gfx_clk", "nv_mem_clk", "nv_pstate", "top_procs"):
            s[k] = ex.get(k)
        s.update(ex.get("ec") or {})
        extra = {}
        if ex.get("ec_extra"):
            extra["ec"] = ex["ec_extra"]
        if rj:
            for k in ("stapm", "fast", "slow", "tdc", "tdc_soc", "edc", "edc_soc", "thm"):
                pair = rj.get(k)
                if pair:
                    s[k + "_v"], s[k + "_l"] = _round(pair[0]), _round(pair[1])
            if s.get("thm_v") is None and rj.get("tctl") is not None:
                s["thm_v"] = _round(rj["tctl"])
            for k, v in (rj.get("_extra") or {}).items():
                if isinstance(v, list):
                    vals = [x for x in v if x is not None]
                    if k == "core_clk" and vals:
                        s["core_clk_max"] = max(vals)
                        s["core_clk_avg"] = _round(sum(vals) / len(vals))
                    elif k == "core_power" and vals:
                        s["core_power_sum"] = _round(sum(vals), 2)
                    elif k == "core_temp" and vals:
                        s["core_temp_max"] = max(vals)
                    if k == "core_clk":
                        extra[k] = v  # the per-core clocks; the rest summed up
                elif k in SAMPLE_NAMES:
                    s[k] = v
                else:
                    extra[k] = v
        if extra:
            s["extra"] = json.dumps(extra, separators=(",", ":"))
        return s

    def _cpu_reasons(self, m, rj, s):
        E = self.eng
        out = {}
        if rj:
            for k, r in E.apu_binding(rj).items():
                out[k] = _Reason(k, r.ratio, r.value, r.unit)
            v, lim = s.get("stt_apu_v"), s.get("stt_apu_l")
            if v and lim and v > 0 and lim > 0 and v >= lim - E.APU_THM_MARGIN_C:
                out["stt_apu"] = _Reason("stt_apu", v / lim, lim, "C")
        else:
            r = (m.get("cpu_binding") or {}).get("power")
            if r is not None:
                out["pkg_power"] = _Reason("pkg_power", 1.0, r.value, "W")
        clk = m.get("cpu_clock")
        if not out and clk and clk[0]:
            out["base_clock"] = _Reason("base_clock", 1.0, clk[1], "GHZ")
        if s.get("ec_sppt"):
            out["ec_sppt"] = _Reason("ec_sppt", EC_RATIO)
        if s.get("ec_prochot"):
            out["prochot"] = _Reason("prochot", PROCHOT_RATIO)
        return out

    def _gpu_reasons(self, m):
        E = self.eng
        nvl = getattr(self.nv, "last", None)
        if not m.get("dgpu_present") or not nvl:
            return {}
        bits, draw, enf = nvl["reasons"], nvl["draw"], nvl["enforced"]
        out = {}
        if bits & NV_SW_POWER and enf > 0 and draw >= E.NV_NEAR_CAP * enf:
            out["power"] = _Reason("power", draw / enf, enf, "W")
        if bits & NV_HW_BRAKE:
            out["brake"] = _Reason("brake")
        if bits & NV_SW_THERMAL:
            out["sw_thermal"] = _Reason("sw_thermal")
        if bits & NV_HW_THERMAL:
            out["hw_thermal"] = _Reason("hw_thermal")
        if bits & NV_HW_SLOWDOWN and not bits & (NV_HW_BRAKE | NV_HW_THERMAL):
            out["hw_slowdown"] = _Reason("hw_slowdown")
        return out

    def _count(self, wall, dt, s, loaded, cpu, gpu):
        """Add this tick's time to the tallies."""
        if dt <= 0:
            return
        day = time.strftime("%Y-%m-%d", time.localtime(wall))
        src, mode = s.get("power_src") or "?", s.get("power_mode") or "?"

        def add(comp, reason, bound, primary=0.0, load=0.0):
            v = self._tally.setdefault((day, comp, reason, src, mode), [0.0, 0.0, 0.0])
            v[0] += bound
            v[1] += primary
            v[2] += load

        add("all", "_observed", dt)
        for comp, reasons in (("cpu", cpu), ("gpu", gpu)):
            ld = dt if loaded[comp] else 0.0
            if ld:
                add(comp, "_loaded", dt, 0.0, ld)
            if reasons:
                add(comp, "_limited", dt, dt, ld)
                prim = _primary(reasons)
                for k in reasons:
                    add(comp, k, dt, dt if k == prim else 0.0, ld)

    def _flush_tally(self):
        self._tally_at = time.monotonic()
        if self._tally:
            delta = {k: [round(x, 3) for x in v] for k, v in self._tally.items()}
            self._tally = {}
            self._writer.put(("tally", delta))

    def _episodes(self, comp, reasons, wall, now, dt, s, ex, track):
        """Advance this component's episodes; the sample 'why' if one started
        or ended (a sample is forced then)."""
        E = self.eng
        eps = self._eps[comp]
        prim = _primary(reasons)
        pad_key = getattr(track, "current", None)
        why = None
        for key, r in reasons.items():
            ep = eps.get(key)
            if ep is None:
                ep = eps[key] = _Episode(comp, key, wall, now, self._context(s))
            ep.see(wall, now, dt, r, key == prim, [k for k in reasons if k != key],
                   pad_key, s, ex)
            if not ep.open and now - ep.first_mono >= E.LIMITLOG_ONSET_S:
                ep.open = True
                ep.id = self._writer.new_id()
                self._writer.put(("episode", ep.row()))
                why = "episode"
                if (comp == "cpu" and self._table_for is None
                        and self._last_rj is not None):
                    self._table_for = (ep.id, "episode:" + key, now)
                    E.RyzenAdjSource.want_table = True
        for key in [k for k in eps if k not in reasons]:
            ep = eps[key]
            if now - ep.last_mono > E.LIMIT_RELEASE_S:
                del eps[key]
                if ep.open:
                    self._writer.put(("episode", ep.row("released")))
                    why = "episode"
        return why

    def _end_all(self, comp, cause):
        for ep in self._eps[comp].values():
            if ep.open:
                self._writer.put(("episode", ep.row(cause)))
        self._eps[comp] = {}

    @staticmethod
    def _context(s):
        return {k: s.get(k) for k in (
            "ac", "power_src", "power_mode", "power_plan", "pd_w", "batt_pct",
            "batt_state", "batt_rate_w", "stapm_l", "slow_l", "fast_l",
            "stapm_time", "ec_apu", "ec_ambient", "top_procs")}

    def _changes(self, wall, m, s, new_rj):
        """Events for state changes; the sample 'why' if any."""
        prev = self._prev
        why = None

        def changed(name, val, tol=0.0):
            """val differs from the last one seen (None is 'unknown', never a
            change); the first value seen is the baseline."""
            if val is None:
                return False
            old = prev.get(name)
            prev[name] = val
            if old is None:
                return False
            if isinstance(val, (int, float)) and isinstance(old, (int, float)):
                return abs(val - old) > tol
            return val != old

        ctx = {k: s.get(k) for k in (
            "power_src", "power_mode", "power_plan", "pd_w", "batt_pct",
            "batt_state", "batt_rate_w", "cpu_util", "ec_apu", "ec_ambient",
            "ec_cpu", "ec_charger", "fan_l", "fan_r", "stapm_v", "stapm_l",
            "slow_l", "fast_l", "socket_w", "stt_apu_v", "stt_apu_l", "dgpu_state")}

        old_ac = prev.get("ac")
        if changed("ac", s.get("ac")):
            self._extras.pd_now = True
            self.event("ac", ts=wall, ac=s["ac"], was=old_ac, **ctx)
            why = "event"
        old = prev.get("power_src")
        if changed("power_src", s.get("power_src")):
            self.event("power_src", ts=wall, was=old, **ctx)
            why = "event"
        for name in ("power_mode", "power_plan", "energy_saver", "charge_cap"):
            old = prev.get(name)
            if changed(name, s.get(name)):
                self.event(name, ts=wall, value=s.get(name), was=old, **ctx)
                why = "event"
        old = (prev.get("pd_w"), prev.get("pd_port"))
        a = changed("pd_w", s.get("pd_w"), 0.5)
        b = changed("pd_port", s.get("pd_port"))
        if a or b:
            self.event("pd_contract", ts=wall, port=s.get("pd_port"), w=s.get("pd_w"),
                       v=s.get("pd_v"), a=s.get("pd_a"), was=old,
                       ports=self._extras.snapshot().get("pd_ports"), **ctx)
            why = "event"
        flips = {}
        for name in ("ec_sppt", "ec_prochot"):
            old = prev.get(name)
            if changed(name, s.get(name)):
                flips[name] = [old, s.get(name)]
        if flips:
            self.event("ec_throttle", ts=wall, changed=flips, **ctx)
            why = "event"
        if new_rj:
            moved = {}
            anchor = self._anchor
            for name in SMU_LIMIT_KEYS:
                val = s.get(name)
                if val is None:
                    continue
                old = anchor.get(name)
                if old is None:
                    anchor[name] = val  # the first value seen is the baseline
                elif abs(val - old) > SMU_LIMIT_TOL_KEY.get(name, SMU_LIMIT_TOL):
                    moved[name] = [old, val]
                    anchor[name] = val
            if moved:
                # the event this log exists for: what moved a limit, and when
                self.event("smu_limits", ts=wall, changed=moved, **ctx)
                why = "limit_change"
        return why
