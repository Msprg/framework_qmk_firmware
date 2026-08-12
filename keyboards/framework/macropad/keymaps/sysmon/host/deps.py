#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Capability and dependency probe behind `--selftest`.

Two audiences, one probe:

  * the installer runs `--selftest --json` before copying files, so it can show
    the user what will and will not work on *their* machine and offer to install
    the missing optional pieces;
  * the user runs `--selftest` from the Start menu when a row is dark and they
    want to know why.

Almost everything the host reads is optional and degrades on its own, which is
a virtue at runtime and a problem when diagnosing: a dark dGPU row is correct on
a machine with no dGPU, expected on an AMD-only Framework 16, and a bug if an
RTX card is fitted. So each probe reports one of

    ok        working
    n/a       legitimately absent on this hardware -- not a problem to fix
    degraded  working, but a better source exists
    missing   should work here and does not, with a remedy

and the summary only counts `missing` as something the user has to act on.

Probes must be cheap, must never hang, and must never raise: this runs inside an
installer wizard page.
"""

import ctypes
import json
import os
import subprocess
import sys

import paths

OK, NA, DEGRADED, MISSING = "ok", "n/a", "degraded", "missing"

_ICON = {OK: "[ ok ]", NA: "[ -- ]", DEGRADED: "[warn]", MISSING: "[FAIL]"}


class Result:
    def __init__(self, key, title, status, detail="", remedy=""):
        self.key = key
        self.title = title
        self.status = status
        self.detail = detail
        self.remedy = remedy

    def as_dict(self):
        return {"key": self.key, "title": self.title, "status": self.status,
                "detail": self.detail, "remedy": self.remedy}


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class _quiet:
    """Capture everything a probe writes to stderr, including from C code.

    Two separate streams have to be caught, and missing either one leaves stray
    text in the middle of the formatted report:

      * Python-level `sys.stderr` -- the engine's own diagnostics, printed while
        constructing RyzenAdjSource / LhmLibSource.
      * File descriptor 2 -- libryzenadj and WinRing0 are native code writing to
        the C runtime's stderr, which does not go through sys.stderr at all.
        Rebinding the Python object alone leaves messages like "Unable to get
        memory access" landing on the console whenever they feel like it,
        typically after the report has already finished printing.

    The captured text is kept so a probe can fold the useful part into its
    detail line rather than discarding a real diagnostic.
    """

    def __init__(self):
        self.text = ""
        self._buf = None
        self._saved = None
        self._tmp = None
        self._saved_fd = None

    def __enter__(self):
        import io
        import tempfile
        self._buf = io.StringIO()
        self._saved = sys.stderr
        sys.stderr = self._buf
        try:
            self._tmp = tempfile.TemporaryFile(mode="w+b")
            self._saved_fd = os.dup(2)
            # sys.__stderr__ is None in a frozen process with no console.
            if sys.__stderr__ is not None:
                sys.__stderr__.flush()
            os.dup2(self._tmp.fileno(), 2)
        except Exception:
            # No usable fd 2 (pythonw, a service with no console): the Python
            # capture above is still in place, so carry on rather than failing
            # the probe.
            self._release_fd()
        return self

    def _release_fd(self):
        if self._saved_fd is not None:
            try:
                os.dup2(self._saved_fd, 2)
                os.close(self._saved_fd)
            except Exception:
                pass
            self._saved_fd = None

    def __exit__(self, *exc):
        native = ""
        if self._tmp is not None:
            try:
                os.fsync(2)
            except Exception:
                pass
            self._release_fd()
            try:
                self._tmp.seek(0)
                native = self._tmp.read().decode("utf-8", "replace")
            except Exception:
                pass
            try:
                self._tmp.close()
            except Exception:
                pass
            self._tmp = None
        sys.stderr = self._saved
        self.text = "\n".join(
            p.strip() for p in (self._buf.getvalue(), native) if p.strip())
        return False

    def first_line(self):
        return self.text.splitlines()[0] if self.text else ""


def _cpu_vendor():
    """AMD / Intel / unknown, from the registry (no WMI round-trip)."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
        low = name.lower()
        if "amd" in low or "ryzen" in low:
            return "AMD", name.strip()
        if "intel" in low:
            return "Intel", name.strip()
        return "unknown", name.strip()
    except Exception:
        return "unknown", ""


# --- individual probes -----------------------------------------------------

def probe_privileges():
    if _is_admin():
        return Result("privileges", "Elevated", OK,
                      "running with administrator rights")
    return Result(
        "privileges", "Elevated", MISSING, "running unelevated",
        "The ring0 sources (ryzenadj, LibreHardwareMonitor) need elevation. "
        "The installed service runs as LocalSystem so this is automatic; only "
        "a manual console run needs an elevated prompt.")


def probe_device():
    """Is the macropad module attached, and is it the macropad?"""
    try:
        import hid
    except Exception as err:
        return Result("device", "Macropad attached", MISSING,
                      f"hidapi unavailable ({err})",
                      "Reinstall -- the bundle is incomplete.")
    try:
        raw = [d for d in hid.enumerate(0x32AC, 0x0013)
               if d.get("usage_page") == 0xFF60 and d.get("usage") == 0x61]
        if raw:
            return Result("device", "Macropad attached", OK,
                          "raw HID endpoint present (VID 32AC PID 0013)")
        if hid.enumerate(0x32AC, 0x0013):
            return Result(
                "device", "Macropad attached", MISSING,
                "macropad found but it exposes no raw-HID (VIA) endpoint",
                "The module is running firmware without VIA/raw-HID support. "
                "Flash the sysmon firmware.")
        if hid.enumerate(0x32AC, 0x0018):
            return Result(
                "device", "Macropad attached", MISSING,
                "a Framework input module is attached, but it is the numpad "
                "(PID 0018), not the RGB macropad (PID 0013)",
                "Swap in the RGB macropad module. sysmon drives that module "
                "only.")
        return Result("device", "Macropad attached", MISSING,
                      "no Framework macropad found",
                      "Plug the RGB macropad module in. Installation can "
                      "continue -- the service waits for it and picks it up "
                      "when it appears.")
    except Exception as err:
        return Result("device", "Macropad attached", MISSING, str(err),
                      "Could not enumerate USB HID devices.")


def probe_firmware():
    """Does the attached module answer on the sysmon channel?

    Sends one FRAME A with everything zeroed -- a legal, invisible frame -- and
    reads VIA's echo back. Stock Framework firmware ignores an unknown custom
    channel, so a silent device means 'not running sysmon', not 'broken'.
    """
    try:
        import hid
        eng = paths.engine_module()
    except Exception as err:
        return Result("firmware", "sysmon firmware", MISSING, str(err))
    path = None
    try:
        path = eng.find_device_path()
    except Exception:
        pass
    if path is None:
        return Result("firmware", "sysmon firmware", NA,
                      "skipped: no macropad attached")
    dev = None
    try:
        dev = hid.device()
        dev.open_path(path)
        dev.set_nonblocking(True)
        probe = bytearray(eng.REPORT_LEN)
        probe[0] = eng.VIA_CMD_CUSTOM_SET_VALUE
        probe[1] = eng.SYSMON_CHANNEL
        probe[2] = eng.SYSMON_VALUE_FRAME_A
        dev.write(b"\x00" + bytes(probe))
        import time
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            reply = dev.read(eng.REPORT_LEN)
            if reply:
                if len(reply) > 1 and reply[1] == eng.SYSMON_CHANNEL:
                    return Result("firmware", "sysmon firmware", OK,
                                  "device acknowledged channel 0x42")
                return Result(
                    "firmware", "sysmon firmware", DEGRADED,
                    "device answered VIA but not on the sysmon channel",
                    "Flash the bundled firmware (--flash).")
            time.sleep(0.02)
        return Result(
            "firmware", "sysmon firmware", MISSING,
            "no reply on channel 0x42",
            "The macropad is not running sysmon firmware. Flash the bundled "
            "image (--flash), or tick the flashing option in the installer.")
    except Exception as err:
        return Result("firmware", "sysmon firmware", MISSING, str(err),
                      "Could not talk to the macropad's raw-HID endpoint.")
    finally:
        try:
            if dev is not None:
                dev.close()
        except Exception:
            pass


def probe_ryzenadj():
    """libryzenadj: the authoritative APU STAPM/PPT limits + Tctl."""
    vendor, name = _cpu_vendor()
    try:
        eng = paths.engine_module()
    except Exception as err:
        return Result("ryzenadj", "AMD APU power limits", MISSING, str(err))
    dll_dir = eng.find_ryzenadj_dir()
    if dll_dir is None:
        if vendor == "Intel":
            return Result("ryzenadj", "AMD APU power limits", NA,
                          f"not applicable on {name}")
        return Result(
            "ryzenadj", "AMD APU power limits", MISSING,
            "libryzenadj.dll not found",
            "Reinstall -- libryzenadj.dll should ship beside the executable.")
    if vendor == "Intel":
        return Result("ryzenadj", "AMD APU power limits", NA,
                      f"not applicable on {name}")
    with _quiet() as captured:
        src = eng.RyzenAdjSource(enabled=True, dll_dir=dll_dir)
    try:
        if not src.available:
            hint = ("Run elevated -- libryzenadj loads a ring0 driver."
                    if not _is_admin() else
                    "Usually the WinRing0 ring0 driver being blocked -- see the "
                    "'Ring0 driver' line -- or an unsupported CPU.")
            return Result(
                "ryzenadj", "AMD APU power limits", MISSING,
                f"found in {dll_dir} but would not initialise"
                + (f": {captured.first_line()}" if captured.text else ""),
                hint)
        snap = src.snapshot() or {}
        pairs = ", ".join(
            f"{k}={snap[k][0]:.1f}/{snap[k][1]:.1f}W"
            for k in ("stapm", "fast", "slow") if k in snap)
        return Result("ryzenadj", "AMD APU power limits", OK,
                      pairs or "SMU table readable")
    finally:
        src.close()


def probe_ring0():
    """Can a ring0 monitoring driver load at all on this machine?

    Memory Integrity (HVCI) and the vulnerable-driver blocklist both refuse
    WinRing0, which is what libryzenadj and LibreHardwareMonitorLib use to read
    the CPU. When blocked, the CPU-row breathe and the in-process temperature
    are impossible and only the EC path is left -- worth saying plainly rather
    than letting the user wonder why one row never animates.
    """
    try:
        import winreg
        hvci = 0
        try:
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios"
                    r"\HypervisorEnforcedCodeIntegrity") as key:
                hvci = winreg.QueryValueEx(key, "Enabled")[0]
        except OSError:
            pass
        blocklist = 1
        try:
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\CI\Config") as key:
                blocklist = winreg.QueryValueEx(
                    key, "VulnerableDriverBlocklistEnable")[0]
        except OSError:
            pass
    except Exception as err:
        return Result("ring0", "Ring0 driver", DEGRADED, str(err))

    if hvci:
        return Result(
            "ring0", "Ring0 driver", MISSING,
            "Memory Integrity (HVCI) is enabled",
            "HVCI blocks WinRing0, so the CPU-row power-limit breathe and the "
            "in-process temperature cannot work. Temperature still comes from "
            "framework_tool (the EC). To get the rest, turn off Windows "
            "Security > Device security > Core isolation > Memory integrity "
            "and reboot -- your call, it is a real security tradeoff.")
    return Result(
        "ring0", "Ring0 driver", OK,
        f"Memory Integrity off, vulnerable-driver blocklist "
        f"{'on' if blocklist else 'off'} (WinRing0 loads on this machine)")


def probe_nvml():
    """NVIDIA dGPU utilisation + authoritative power/throttle reasons."""
    try:
        import pynvml
    except ImportError:
        return Result("nvml", "NVIDIA dGPU", MISSING, "pynvml not bundled",
                      "Reinstall -- the bundle is incomplete.")
    try:
        pynvml.nvmlInit()
    except Exception as err:
        if not os.path.exists(os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"),
                "System32", "nvml.dll")):
            return Result("nvml", "NVIDIA dGPU", NA,
                          "no NVIDIA driver present (no dGPU, or an AMD one)")
        # On a hybrid-graphics laptop the dGPU is powered off when nothing is
        # using it, and NVML then reports the driver as not loaded. That is the
        # normal idle state, not a fault: the engine retries every 10 s and
        # picks the card up as soon as something wakes it.
        if "Driver Not Loaded" in str(err):
            return Result(
                "nvml", "NVIDIA dGPU", DEGRADED,
                "dGPU is currently powered down (hybrid graphics)",
                "Normal when nothing is using the dGPU. The service retries "
                "every 10 s and connects when the card wakes. To confirm now, "
                "start something that uses the dGPU and re-run.")
        return Result("nvml", "NVIDIA dGPU", MISSING, f"NVML init failed: {err}",
                      "Update or repair the NVIDIA driver.")
    try:
        count = pynvml.nvmlDeviceGetCount()
        if count < 1:
            return Result("nvml", "NVIDIA dGPU", NA,
                          "NVIDIA driver present but no GPU visible")
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        return Result("nvml", "NVIDIA dGPU", OK, name)
    except Exception as err:
        return Result("nvml", "NVIDIA dGPU", DEGRADED, str(err))
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass


def probe_lhm():
    """In-process LibreHardwareMonitorLib (pythonnet) for APU sensors."""
    lib_dir = paths.lhm_lib_dir()
    dll = os.path.join(lib_dir, "LibreHardwareMonitorLib.dll")
    if not os.path.isfile(dll):
        return Result("lhm", "LibreHardwareMonitor lib", MISSING,
                      f"not found in {lib_dir}",
                      "Reinstall, or run --fetch-lhm to download it.")
    try:
        import clr  # noqa: F401  (pythonnet)
    except Exception as err:
        return Result("lhm", "LibreHardwareMonitor lib", MISSING,
                      f"pythonnet unavailable ({err})",
                      "Reinstall -- the bundle is incomplete.")
    try:
        eng = paths.engine_module()
    except Exception as err:
        return Result("lhm", "LibreHardwareMonitor lib", MISSING, str(err))
    with _quiet():
        src = eng.LhmLibSource(enabled=True, lib_dir=lib_dir)
    try:
        if not src.available:
            return Result(
                "lhm", "LibreHardwareMonitor lib", DEGRADED,
                "engine loaded but exposed no CPU sensors",
                "Needs elevation and a loadable ring0 driver. On AMD this is "
                "only a secondary source -- ryzenadj covers both power limits "
                "and temperature.")
        temp, pkg_w, load = src.snapshot()
        return Result("lhm", "LibreHardwareMonitor lib", OK,
                      f"temp={temp} pkg_w={pkg_w} load={load}")
    finally:
        src.close()


def probe_framework_tool():
    """EC access: temperature fallback and the battery max-charge cap."""
    try:
        out = subprocess.run(
            ["framework_tool", "--charge-limit"], capture_output=True,
            text=True, timeout=5, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except FileNotFoundError:
        return Result(
            "framework_tool", "framework_tool (EC)", MISSING, "not on PATH",
            "winget install FrameworkComputer.framework_tool -- gives the "
            "battery charge-cap scaling and a temperature fallback. Optional.")
    except OSError as err:
        if getattr(err, "winerror", None) == 740:  # ERROR_ELEVATION_REQUIRED
            return Result(
                "framework_tool", "framework_tool (EC)", DEGRADED,
                "installed, but this process is not elevated",
                "framework_tool needs administrator rights to reach the EC. "
                "The service runs as LocalSystem, so this resolves itself once "
                "installed.")
        return Result("framework_tool", "framework_tool (EC)", DEGRADED,
                      str(err))
    except Exception as err:
        return Result("framework_tool", "framework_tool (EC)", DEGRADED,
                      str(err))
    if out and "Maximum" in out:
        # Pick the line that actually carries the data. framework_tool appends
        # a "Press ENTER to exit..." trailer when it thinks it owns the console
        # (it does, under CREATE_NO_WINDOW), so the last line is the wrong one.
        line = next((l.strip() for l in out.splitlines() if "Maximum" in l),
                    "charge limit readable")
        return Result("framework_tool", "framework_tool (EC)", OK, line)
    return Result("framework_tool", "framework_tool (EC)", DEGRADED,
                  "installed but returned no charge-limit data",
                  "Usually means the process is not elevated.")


def probe_brightness():
    """Screen brightness -- the source most at risk in a session-0 service."""
    try:
        import wmi
        ns = wmi.WMI(namespace="root\\wmi")
        monitors = ns.WmiMonitorBrightness()
        if monitors:
            return Result("brightness", "Screen brightness", OK,
                          f"{int(monitors[0].CurrentBrightness)}%")
        return Result(
            "brightness", "Screen brightness", DEGRADED,
            "WmiMonitorBrightness returned no instances",
            "LED brightness falls back to --fallback-brightness and no longer "
            "tracks the screen.")
    except Exception as err:
        return Result(
            "brightness", "Screen brightness", DEGRADED, str(err),
            "LED brightness falls back to --fallback-brightness and no longer "
            "tracks the screen.")


def probe_battery():
    try:
        import wmi
        ns = wmi.WMI(namespace="root\\wmi")
        statuses = ns.BatteryStatus()
        if statuses:
            s = statuses[0]
            return Result("battery", "Battery state", OK,
                          f"charge={s.ChargeRate}mW discharge={s.DischargeRate}mW")
        return Result("battery", "Battery state", DEGRADED,
                      "no BatteryStatus instances",
                      "The battery row falls back to a coarse psutil reading.")
    except Exception as err:
        return Result("battery", "Battery state", DEGRADED, str(err),
                      "The battery row falls back to a coarse psutil reading.")


def probe_gpu_counters():
    """PDH GPU Engine counters: the iGPU row, and the dGPU fallback."""
    try:
        import win32pdh
    except ImportError:
        return Result("gpu", "GPU counters (PDH)", MISSING,
                      "win32pdh unavailable",
                      "Reinstall -- the bundle is incomplete.")
    try:
        import time
        query = win32pdh.OpenQuery()
        counter = win32pdh.AddEnglishCounter(
            query, r"\GPU Engine(*engtype_3D)\Utilization Percentage")
        win32pdh.CollectQueryData(query)
        time.sleep(0.3)                      # % counters need two samples
        win32pdh.CollectQueryData(query)
        items = win32pdh.GetFormattedCounterArray(
            counter, win32pdh.PDH_FMT_DOUBLE)
        win32pdh.CloseQuery(query)
    except Exception as err:
        return Result(
            "gpu", "GPU counters (PDH)", DEGRADED, str(err),
            "The iGPU row stays empty. The counter set can be unregistered "
            "early in boot; the service retries every 10 s, so this often "
            "resolves itself.")
    if not items:
        return Result(
            "gpu", "GPU counters (PDH)", DEGRADED,
            "counter set present but no 3D engine instances",
            "Normal when nothing is drawing. Re-run with something on screen.")
    try:
        eng = paths.engine_module()
        names = eng.adapter_luid_names()
        detail = f"{len(items)} engine instance(s), " \
                 f"{len(names)} adapter(s): {', '.join(sorted(set(names.values())))}"
    except Exception:
        detail = f"{len(items)} engine instance(s)"
    return Result("gpu", "GPU counters (PDH)", OK, detail)


def probe_config():
    """Does the tunables INI parse, and do its values survive validation?

    Worth its own probe because a bad config fails *quietly* by design: the
    service must never refuse to boot over a typo, so it logs and falls back to
    built-in defaults. Without this line the user's only symptom is "my edit did
    nothing", with the explanation buried in sysmon.log.
    """
    path = paths.config_path()
    if not os.path.isfile(path):
        return Result("config", "Settings file", NA,
                      "not created yet; using built-in defaults")
    try:
        import config
        ini = config._read(path)
    except Exception as err:
        return Result(
            "config", "Settings file", MISSING, f"{path} will not parse: {err}",
            "The whole file is being ignored and built-in defaults used. If it "
            "was saved as 'UTF-8 with BOM' or in another encoding, re-save it "
            "as plain UTF-8 -- or delete it and run --write-default-config to "
            "regenerate.")

    known = {s.name: s for s in config.SCHEMA}
    applied = 0
    bad = []
    for section in ini.sections():
        for key, raw in ini.items(section):
            if section == "cli":
                applied += 1
                continue
            setting = known.get(key)
            if setting is None:
                bad.append(f"{key} (unknown)")
                continue
            try:
                config._parse(setting, raw)
                applied += 1
            except ValueError as err:
                bad.append(f"{key} ({err})")
    if bad:
        return Result("config", "Settings file", DEGRADED,
                      f"{applied} setting(s) OK, {len(bad)} ignored: "
                      + "; ".join(bad[:3]) + ("..." if len(bad) > 3 else ""),
                      "Those entries keep their built-in defaults. Fix or "
                      "delete them.")
    return Result("config", "Settings file", OK, f"{applied} setting(s), all valid")


def probe_service():
    try:
        import service
        state = service.query_state()
    except Exception as err:
        return Result("service", "Service", DEGRADED, str(err))
    if state is None:
        return Result("service", "Service", NA, "not installed")
    return Result("service", "Service", OK if state == "running" else DEGRADED,
                  f"{service.SERVICE_NAME} is {state}")


PROBES = [
    probe_privileges,
    probe_device,
    probe_firmware,
    probe_ring0,
    probe_ryzenadj,
    probe_nvml,
    probe_lhm,
    probe_framework_tool,
    probe_brightness,
    probe_battery,
    probe_gpu_counters,
    probe_config,
    probe_service,
]


def collect():
    results = []
    for probe in PROBES:
        try:
            results.append(probe())
        except Exception as err:  # a probe must never take the report down
            results.append(Result(probe.__name__, probe.__name__, DEGRADED,
                                  f"probe raised: {err}"))
    return results


def run_selftest(args):
    results = collect()
    if getattr(args, "json", False):
        payload = {
            "version": _version(),
            "frozen": paths.is_frozen(),
            "app_dir": paths.app_dir(),
            "data_dir": paths.data_dir(),
            "config": paths.config_path(),
            "elevated": _is_admin(),
            "results": [r.as_dict() for r in results],
            "blocking": [r.key for r in results if r.status == MISSING],
        }
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        # Same exit convention as the human report: non-zero means "something
        # needs attention". The installer reads the `blocking` list rather than
        # the exit code, so a machine missing an optional piece never blocks it.
        return 1 if payload["blocking"] else 0

    print(f"fw16-sysmon {_version()} self-test")
    print(f"  program : {paths.app_dir()}")
    print(f"  data    : {paths.data_dir()}")
    print(f"  config  : {paths.config_path()}"
          f"{'' if os.path.isfile(paths.config_path()) else '  (not created yet)'}")
    print()
    width = max(len(r.title) for r in results)
    for r in results:
        print(f"  {_ICON[r.status]} {r.title.ljust(width)}  {r.detail}")
        if r.remedy:
            for line in _indent(r.remedy, width + 12):
                print(line)
    blocking = [r for r in results if r.status == MISSING]
    print()
    if blocking:
        print(f"  {len(blocking)} item(s) need attention: "
              f"{', '.join(r.title for r in blocking)}")
    else:
        print("  Everything needed is in place.")
    return 1 if blocking else 0


def _indent(text, pad):
    import textwrap
    return [" " * pad + line for line in textwrap.wrap(text, 78 - pad)]


def _version():
    try:
        return paths.engine_module().__version__
    except Exception:
        return "?"
