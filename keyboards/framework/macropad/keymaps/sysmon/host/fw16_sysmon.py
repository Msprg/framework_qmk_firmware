#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Stream system stats to a Framework 16 RGB Macropad running the `sysmon` keymap.

Sends one 32-byte packet per interval over the VIA raw HID endpoint, using a
private VIA "custom value" channel the firmware listens on. LED brightness is
derived from the laptop screen brightness so the macropad dims with the screen.

Dependencies (Windows):
    pip install hidapi psutil wmi

Temperature sources, tried in order:
    1. Framework EC via framework_tool --thermal (APU die temperature)
    2. LibreHardwareMonitor (if running with its WMI provider enabled)
    3. ACPI thermal zone (MSAcpi_ThermalZoneTemperature; static on the FW16,
       so only a last resort)

Run it in a console first to verify, then schedule with Task Scheduler
(pythonw.exe) to have it start at login. No service needed.
"""

import argparse
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

VID = 0x32AC
PID_MACROPAD = 0x0013
RAW_USAGE_PAGE = 0xFF60  # QMK raw HID (VIA) interface
RAW_USAGE = 0x61
REPORT_LEN = 32

VIA_CMD_CUSTOM_SET_VALUE = 0x07
SYSMON_CHANNEL = 0x42
SYSMON_VALUE_STATS = 0x01

FLAG_AC_CONNECTED = 1 << 0
FLAG_BATTERY = 1 << 1
FLAG_DGPU = 1 << 2
FLAG_TEMP = 1 << 3
FLAG_CHARGING = 1 << 4
FLAG_DISCHARGING = 1 << 5


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


class EcThermal:
    """CPU temperature from the Framework EC via framework_tool.

    The EC reports live die temperatures; the FW16's ACPI thermal zones
    return frozen values, so this is the only trustworthy source without
    LibreHardwareMonitor running. Needs framework_tool on PATH and an
    elevated process for the EC driver.
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
        out = None
        try:
            # stdin must be DEVNULL: under CREATE_NO_WINDOW framework_tool
            # sees a fresh console and waits for ENTER before exiting
            out = subprocess.run(
                ["framework_tool", "--thermal"],
                capture_output=True, text=True, timeout=2,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW).stdout
        except FileNotFoundError:
            print("framework_tool not found; EC temperature disabled",
                  file=sys.stderr)
            self.available = False
            return None
        except (subprocess.TimeoutExpired, OSError):
            pass
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


class WmiSources:
    """Lazily connected WMI namespaces; every query degrades gracefully."""

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

    def temperature(self):
        """Return temperature in degrees C, or None if unavailable."""
        lhm = self._connect("_lhm", "root\\LibreHardwareMonitor")
        if lhm is not None:
            try:
                sensors = [s for s in lhm.Sensor() if s.SensorType == "Temperature"]
                if sensors:
                    preferred = [s for s in sensors
                                 if s.Name in ("CPU Package", "Core (Tctl/Tdie)", "Tctl")]
                    pick = preferred or sensors
                    return max(s.Value for s in pick)
            except Exception:
                pass
        ns = self._connect("_wmi_ns", "root\\wmi")
        if ns is not None:
            try:
                zones = ns.MSAcpi_ThermalZoneTemperature()
                if zones:
                    # value is tenths of Kelvin
                    return max(z.CurrentTemperature for z in zones) / 10.0 - 273.15
            except Exception:
                pass
        return None

    def battery_state(self):
        """Return (charging, discharging) booleans, or None if unavailable.

        Both False means the battery is idle — typically plugged in and held
        at a charge cap. The ACPI BatteryStatus class reports this directly,
        unlike psutil which only sees plugged/unplugged.
        """
        ns = self._connect("_wmi_ns", "root\\wmi")
        if ns is None:
            return None
        try:
            statuses = ns.BatteryStatus()
            if statuses:
                charging = any(bool(s.Charging) or (s.ChargeRate or 0) > 0
                               for s in statuses)
                discharging = any(bool(s.Discharging) or (s.DischargeRate or 0) > 0
                                  for s in statuses)
                return charging, discharging
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


def clamp(value, lo, hi):
    return max(lo, min(hi, int(round(value))))


def build_packet(stats):
    data = bytearray(REPORT_LEN)
    data[0] = VIA_CMD_CUSTOM_SET_VALUE
    data[1] = SYSMON_CHANNEL
    data[2] = SYSMON_VALUE_STATS
    data[3] = stats["flags"]
    data[4] = stats["battery"]
    data[5] = stats["cpu"]
    data[6] = stats["mem"]
    data[7] = stats["igpu"]
    data[8] = stats["dgpu"]
    data[9] = stats["temp"]
    data[10] = stats["brightness"]
    return bytes(data)


def gather(sources, gpu, ec, args):
    flags = 0

    battery = 0
    batt = psutil.sensors_battery()
    if batt is not None:
        flags |= FLAG_BATTERY
        battery = clamp(batt.percent, 0, 100)
        if batt.power_plugged:
            flags |= FLAG_AC_CONNECTED
        state = sources.battery_state()
        if state is None:
            # crude fallback: can't tell "charging" from "held at a cap"
            state = (batt.power_plugged and battery < 100, not batt.power_plugged)
        if state[0]:
            flags |= FLAG_CHARGING
        if state[1]:
            flags |= FLAG_DISCHARGING

    cpu = clamp(psutil.cpu_percent(interval=None), 0, 100)
    mem = clamp(psutil.virtual_memory().percent, 0, 100)

    igpu, dgpu, dgpu_present = gpu.read()
    if dgpu_present:
        flags |= FLAG_DGPU

    temp = ec.temperature()
    if temp is None:
        temp = sources.temperature()
    if temp is not None:
        flags |= FLAG_TEMP
        temp = clamp(temp, 0, 254)
    else:
        temp = 0

    brightness_pct = sources.screen_brightness()
    if brightness_pct is None:
        brightness_pct = args.fallback_brightness
    led_val = clamp(
        args.min_led + (args.max_led - args.min_led) * brightness_pct / 100.0,
        args.min_led, args.max_led)

    return {
        "flags": flags,
        "battery": battery,
        "cpu": cpu,
        "mem": mem,
        "igpu": clamp(igpu, 0, 100),
        "dgpu": clamp(dgpu, 0, 100),
        "temp": temp,
        "brightness": led_val,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between updates (default: 1.0)")
    parser.add_argument("--min-led", type=int, default=8,
                        help="LED brightness at 0%% screen brightness (default: 8)")
    parser.add_argument("--max-led", type=int, default=150,
                        help="LED brightness at 100%% screen brightness, "
                             "0-255 (default: 150; full 255 is blinding)")
    parser.add_argument("--fallback-brightness", type=int, default=30,
                        help="assumed screen brightness %% if unreadable (default: 30)")
    parser.add_argument("--igpu-match", default=r"Intel|Iris|UHD|Radeon.*(Graphics|M\b)",
                        help="regex matching the iGPU adapter name")
    parser.add_argument("--dgpu-match", default=r"NVIDIA|GeForce|RTX|GTX|Radeon RX",
                        help="regex matching the dGPU adapter name")
    parser.add_argument("--verbose", action="store_true",
                        help="print each packet's values")
    parser.add_argument("--bootloader", action="store_true",
                        help="reboot the macropad into its UF2 bootloader "
                             "(RPI-RP2 drive) and exit")
    args = parser.parse_args()

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
    gpu = GpuMonitor(args.igpu_match, args.dgpu_match)
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

            stats = gather(sources, gpu, ec, args)
            if args.verbose:
                print(stats)
            dev.write(b"\x00" + build_packet(stats))
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


if __name__ == "__main__":
    main()
