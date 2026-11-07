#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Install-layout helpers shared by the sysmon host, service, and installer.

The host runs in two very different layouts and every path in the program has
to resolve correctly in both:

  development   host/fw16_sysmon.py run from a venv; DLLs live in host/vendor/
                and host/lib/, config and log sit next to the script.
  installed     a PyInstaller *onedir* bundle in
                C:\\Program Files\\FW16 Macropad Sysmon\\, config and log in
                C:\\ProgramData\\FW16Sysmon\\.

`__file__` must never be used to anchor a runtime path: under PyInstaller it
points into the per-run temp extraction directory (sys._MEIPASS), which is a
different path on every start and is deleted at exit. Everything anchors on
app_dir() instead.

Two placement rules are load-bearing rather than cosmetic:

  * WinRing0x64.sys must sit in the *executable's* directory. WinRing0 (used by
    libryzenadj and by LibreHardwareMonitorLib) resolves its kernel driver
    relative to GetModuleFileName(NULL), i.e. the host process image, not the
    DLL. That is why vendor_dir() == app_dir() when frozen: the installer drops
    the DLLs beside the exe and nothing needs copying at runtime, so the service
    never has to write into Program Files.
  * The data directory must be machine-scope, not user-scope. The service runs
    as LocalSystem, whose %USERPROFILE% is
    C:\\Windows\\system32\\config\\systemprofile -- anything written per-user
    would be invisible to the human editing the config.
"""

import os
import sys

APP_NAME = "FW16Sysmon"


def is_frozen():
    """True when running from a PyInstaller bundle rather than source."""
    return bool(getattr(sys, "frozen", False))


def app_dir():
    """Directory holding the executable (frozen) or this source tree (dev)."""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def process_image_dir():
    """Directory of the running process image (what GetModuleFileName(NULL) sees).

    Frozen: the bundle's exe directory, same as app_dir(). Source run: the
    *interpreter's* directory, which is NOT app_dir() -- and from a venv, not
    sys.executable's either: a Windows venv's python.exe is a small launcher
    that starts the base interpreter (sys._base_executable) as the real
    process, so that is the image WinRing0 resolves its kernel driver against.

    This is where WinRing0x64.sys has to go. The host copies it there only
    when frozen (RyzenAdjSource._ensure_driver): from source this is a shared
    Python installation, and ryzenadj fails to open there past the driver
    anyway (inpoutx64 reported "Unable to get memory access"), so the SMU path
    is tested with the frozen build.
    """
    exe = sys.executable
    if not is_frozen():
        exe = getattr(sys, "_base_executable", None) or exe
    return os.path.dirname(os.path.abspath(exe))


def data_dir():
    """Machine-scope directory for config and logs.

    Frozen: %ProgramData%\\FW16Sysmon (readable by the service as LocalSystem
    and editable by an admin). Dev: next to the script, so a source checkout
    stays self-contained and never litters ProgramData.
    """
    if not is_frozen():
        return app_dir()
    base = os.environ.get("ProgramData") or r"C:\ProgramData"
    return os.path.join(base, APP_NAME)


def ensure_data_dir():
    """Create data_dir() if missing; return it. Best-effort, never raises."""
    d = data_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def config_path():
    """Path of the tunables INI."""
    return os.path.join(data_dir(), "sysmon.ini")


def log_path():
    """Path of the rotating service log."""
    return os.path.join(data_dir(), "sysmon.log")


def service_state_path():
    """Where the service publishes what IT can actually see.

    A self-test run by a human inherits that human's environment; the service
    runs as LocalSystem with a different PATH and a different profile. Those
    diverge in ways that silently degrade the service while an interactive
    check reports everything healthy -- so the service writes its own view here
    and --selftest reads it back rather than assuming the two match.
    """
    return os.path.join(data_dir(), "service-state.json")


def vendor_dir():
    """Directory holding libryzenadj.dll + WinRing0x64.dll/.sys + inpoutx64.dll.

    Frozen: beside the exe, which is exactly where WinRing0 needs its .sys, so
    RyzenAdjSource._ensure_driver() becomes a no-op. Dev: host/vendor/, which
    packaging/fetch-vendor.ps1 populates and .gitignore excludes.
    """
    if is_frozen():
        return app_dir()
    return os.path.join(app_dir(), "vendor")


def lhm_lib_dir():
    """Directory holding LibreHardwareMonitorLib.dll + HidSharp + BCL deps.

    Loaded through .NET Assembly.LoadFrom with an explicit path, so this can be
    a plain subdirectory in both layouts.
    """
    return os.path.join(app_dir(), "lib")


def engine_module():
    """Return the one live fw16_sysmon module object.

    The engine is the program's entry script, so it is registered as __main__ --
    both when run as `python fw16_sysmon.py` and inside the PyInstaller bundle.
    A plain `import fw16_sysmon` from a sibling module would therefore not find
    it and would execute a *second*, independent copy: two sets of module
    globals, only one of which the config layer ever rebinds.

    Prefer the already-loaded entry module, and fall back to a real import for
    the case where the engine genuinely was imported as a library.
    """
    main_mod = sys.modules.get("__main__")
    if main_mod is not None and hasattr(main_mod, "build_parser") \
            and hasattr(main_mod, "SYSMON_CHANNEL"):
        return main_mod
    import fw16_sysmon
    return fw16_sysmon


def bundled_uf2():
    """Path of the firmware image shipped alongside the host, or None.

    The installer places framework_macropad_sysmon.uf2 next to the exe so
    `--flash` works with no arguments. In a source checkout, fall back to the
    QMK build output at the repo root.
    """
    cand = [os.path.join(app_dir(), "framework_macropad_sysmon.uf2")]
    if not is_frozen():
        # host/ -> sysmon/ -> keymaps/ -> macropad/ -> framework/ -> keyboards/ -> repo
        repo = os.path.abspath(os.path.join(app_dir(), *([os.pardir] * 6)))
        cand.append(os.path.join(repo, ".build", "framework_macropad_sysmon.uf2"))
    for p in cand:
        if os.path.isfile(p):
            return p
    return None
