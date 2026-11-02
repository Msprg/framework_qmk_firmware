#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Logging for the sysmon host in its two run modes.

Console runs must keep behaving exactly as they always have: the engine's
diagnostics go to stderr unadorned, so `fw16-sysmon.exe --verbose` reads the
same as `python fw16_sysmon.py --verbose` always did.

Service runs have no console at all, and that is a crash risk rather than a
cosmetic one: in a PyInstaller bundle with no attached console `sys.stdout` and
`sys.stderr` can be None, and the engine's ~15 `print(..., file=sys.stderr)`
diagnostics would then raise AttributeError from inside the poll loop.

Rather than rewrite every one of those call sites, service mode *redirects the
streams*: sys.stdout/sys.stderr become adapters that funnel whole lines into a
size-rotating log file. Every existing print keeps working, unexpected
tracebacks land in the log instead of vanishing, and the engine stays free of
logging plumbing.
"""

import logging
import logging.handlers
import os
import sys

from paths import ensure_data_dir, log_path

MAX_BYTES = 1 << 20   # 1 MiB per file
BACKUP_COUNT = 3      # sysmon.log + .1 .2 .3

_logger = None


def _get_logger():
    """Create (once) the rotating file logger used by service mode."""
    global _logger
    if _logger is not None:
        return _logger
    ensure_data_dir()
    lg = logging.getLogger("fw16sysmon")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    try:
        handler = logging.handlers.RotatingFileHandler(
            log_path(), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
            encoding="utf-8", delay=True)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S"))
        lg.addHandler(handler)
    except OSError:
        # No writable data dir (should not happen as LocalSystem, but never let
        # logging setup be the thing that stops the service from running).
        lg.addHandler(logging.NullHandler())
    _logger = lg
    return lg


class _StreamToLog:
    """Minimal write/flush file object that logs complete lines.

    Buffers partial writes because `print` emits the text and the newline as
    separate write() calls, which would otherwise produce two log records per
    message.
    """

    def __init__(self, logger):
        self._logger = logger
        self._buf = ""

    def write(self, text):
        if not text:
            return 0
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._logger.info(line.rstrip())
        return len(text)

    def flush(self):
        if self._buf.strip():
            self._logger.info(self._buf.rstrip())
        self._buf = ""

    def isatty(self):
        return False


_service_mode = False


def start_service_logging():
    """Switch to file logging and capture stdout/stderr. Idempotent."""
    global _service_mode
    if _service_mode:
        return
    lg = _get_logger()
    sink = _StreamToLog(lg)
    sys.stdout = sink
    sys.stderr = sink
    # A frozen process with no console has sys.__stdout__/__stderr__ set to
    # None, and anything that flushes the "original" streams at interpreter
    # shutdown -- pywin32's service teardown does -- then dies with
    # "'NoneType' object has no attribute 'flush'" in the log, right after the
    # service has otherwise stopped cleanly. Point them at the sink so there is
    # always something flushable.
    if sys.__stdout__ is None:
        sys.__stdout__ = sink
    if sys.__stderr__ is None:
        sys.__stderr__ = sink
    _service_mode = True


def pause_if_own_console():
    """Wait for Enter when this process owns the console it is printing to.

    Launching the exe from a shortcut or the installer gives it a console of its
    own, which Windows destroys the instant the process exits -- the report
    scrolls past and the window vanishes before it can be read. Worse, the exe
    is manifested requireAdministrator, so even when started from an existing
    `cmd /k` the elevated child gets its OWN new console and closes with it,
    leaving the parent window behind empty.

    GetConsoleProcessList reporting exactly one attached process means this
    process is the only thing keeping that console alive, so the output is about
    to be lost. Requiring a tty as well keeps every automated caller unaffected:
    under a pipe (psexec, Tee-Object, a CI capture) there is nothing to pause
    for and hanging there would be a bug.

    A process launched with SW_HIDE -- which is how Setup runs the service
    commands -- still gets a real console, so isatty() is true and the process
    count is one, yet nobody can see the prompt or type into it: pausing there
    hangs the installer on a window that was never drawn. That case is detected
    from the creator's own STARTUPINFO rather than by asking whether the console
    window is visible, because under ConPTY (Windows Terminal, the default on
    Windows 11) GetConsoleWindow returns a hidden pseudo-console window even
    though the user is looking right at the output -- testing visibility would
    silently bring back the vanishing-window bug on most Win11 machines.
    """
    try:
        if not sys.stdout.isatty():
            return
        import ctypes
        from ctypes import wintypes

        buf = (ctypes.c_uint * 8)()
        if ctypes.windll.kernel32.GetConsoleProcessList(buf, 8) != 1:
            return

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        STARTF_USESHOWWINDOW = 0x0001
        SW_HIDE = 0
        info = STARTUPINFOW()
        info.cb = ctypes.sizeof(STARTUPINFOW)
        ctypes.windll.kernel32.GetStartupInfoW(ctypes.byref(info))
        if (info.dwFlags & STARTF_USESHOWWINDOW) and info.wShowWindow == SW_HIDE:
            return
    except Exception:
        return
    try:
        print()
        print("Press Enter to close this window...", end="", flush=True)
        input()
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def is_service_mode():
    """True when running under the SCM rather than in a console."""
    return _service_mode


def log(msg):
    """Emit one diagnostic line to wherever this run's output belongs."""
    if _service_mode:
        _get_logger().info(msg)
    else:
        try:
            print(msg, file=sys.stderr)
        except Exception:
            pass


def event_log(msg, error=False):
    """Record a service lifecycle event in the Windows Application event log.

    Start/stop/fatal only -- routine diagnostics belong in the log file. Silent
    no-op when pywin32's event log support is unavailable.
    """
    log(msg)
    try:
        import servicemanager
        if error:
            servicemanager.LogErrorMsg(msg)
        else:
            servicemanager.LogInfoMsg(msg)
    except Exception:
        pass


def tail(lines=40):
    """Return the last `lines` lines of the log, for --selftest output."""
    path = log_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-lines:]
    except OSError:
        return []
