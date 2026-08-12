# -*- mode: python ; coding: utf-8 -*-
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""PyInstaller spec for the sysmon host.

Deliberately **onedir**, not onefile:

  * A onefile bundle extracts ~40 MB to %TEMP% on every start. This runs as a
    boot-start service, so that cost is paid on every boot and on every service
    restart, and a crashing service leaves _MEI directories behind.
  * WinRing0 loads its kernel driver from a path relative to the process image.
    With onedir the exe's directory is stable and is exactly where the installer
    puts WinRing0x64.sys, so nothing has to be copied at runtime and the service
    never writes into Program Files.
  * Antivirus heuristics treat self-extracting executables far more harshly, and
    this installer is unsigned.

The vendored native DLLs are intentionally **not** listed in `datas`: with
onedir, PyInstaller 6 puts data files under `_internal\\`, and WinRing0x64.sys
must sit next to the exe itself. The Inno Setup script places them in `{app}`
instead. Keeping one owner for that placement avoids two half-correct copies.
"""

import os
import re

from PyInstaller.utils.hooks import collect_all

HOST_DIR = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# Read the version textually rather than importing the module: evaluating the
# spec should not require the engine's native dependencies to be importable,
# and should have no side effects.
with open(os.path.join(HOST_DIR, "fw16_sysmon.py"), encoding="utf-8") as _fh:
    VERSION = re.search(r'^__version__\s*=\s*"([^"]+)"', _fh.read(), re.M).group(1)

# pythonnet needs its runtime assemblies and clr_loader data collected; the
# rest are modules PyInstaller's static analysis cannot see because they are
# imported lazily or by name.
pythonnet_datas, pythonnet_binaries, pythonnet_hidden = collect_all("pythonnet")
clr_datas, clr_binaries, clr_hidden = collect_all("clr_loader")

hiddenimports = [
    # pywin32 service plumbing. win32timezone in particular is imported *by
    # name* deep inside pywin32 and is the classic missing-module failure for
    # frozen services -- it surfaces only when the SCM starts the service.
    "win32timezone",
    "servicemanager",
    "win32serviceutil",
    "win32service",
    "win32event",
    "pythoncom",
    "pywintypes",
    "win32com",
    "win32com.client",
    "win32pdh",
    "wmi",
    # imported lazily by the engine
    "pynvml",
    # the host's own modules, reached via `import x` inside functions
    "paths",
    "applog",
    "config",
    "deps",
    "service",
    "flash",
] + pythonnet_hidden + clr_hidden

excludes = [
    # Present in the venv (pywin32 pulls them in) and never used here. Dropping
    # them keeps the bundle honest and noticeably smaller.
    "tkinter",
    "pythonwin",
    "adodbapi",
    "isapi",
    "win32com.server",
    "pydoc_data",
    "unittest",
]

a = Analysis(
    [os.path.join(HOST_DIR, "fw16_sysmon.py")],
    pathex=[HOST_DIR],
    binaries=pythonnet_binaries + clr_binaries,
    datas=pythonnet_datas + clr_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="fw16-sysmon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed binaries trip antivirus; not worth it
    console=True,       # a console app: --selftest/--flash are terminal tools,
                        # and the service host never shows one anyway
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # The ring0 sources need elevation for a manual console run. The SCM ignores
    # manifests for LocalSystem services, so this costs the service nothing.
    uac_admin=True,
    version=os.path.join(SPECPATH, "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="fw16-sysmon",
)
