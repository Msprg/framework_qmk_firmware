#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Windows service wrapper for the sysmon host.

The mod used to start from a Task Scheduler logon task, which meant the
macropad stayed dark until somebody signed in. This runs it as a LocalSystem
service set to Automatic start instead, so the bars are live at the login
screen.

LocalSystem is not only about starting early -- it is also already elevated,
which is what the ring0 sources (libryzenadj, LibreHardwareMonitorLib) need.
The logon task had to request "Run with highest privileges" to get the same.

Three details here are load-bearing:

  * COM must be initialised on the worker thread. Every WMI read (brightness,
    battery, the LHM provider) goes through COM, and a service's worker thread
    is not the thread pywin32 initialised.
  * The SCM must hear back within ~30 s of start. Opening the ryzenadj SMU table
    and the LHM engine can take seconds, so the service reports RUNNING first
    and does that work inside the loop's own start-up.
  * Registration writes an explicit binPath rather than letting pywin32 derive
    one. In a PyInstaller bundle the interpreter path pywin32 would guess does
    not exist, so the service is registered as `<exe> --service` and the frozen
    exe hosts the dispatcher itself.
"""

import os
import sys
import threading

import paths
from applog import event_log, log, start_service_logging

SERVICE_NAME = "FW16Sysmon"
SERVICE_DISPLAY = "FW16 Macropad Sysmon"
SERVICE_DESCRIPTION = (
    "Streams CPU, memory, GPU, temperature and battery state to the Framework "
    "16 RGB Macropad, which renders them as bar graphs. Starts at boot so the "
    "display is live before sign-in.")

_STATE_NAMES = {}


def _svc_modules():
    import win32service
    import win32serviceutil
    return win32service, win32serviceutil


def _state_names():
    global _STATE_NAMES
    if not _STATE_NAMES:
        import win32service
        _STATE_NAMES = {
            win32service.SERVICE_STOPPED: "stopped",
            win32service.SERVICE_START_PENDING: "starting",
            win32service.SERVICE_STOP_PENDING: "stopping",
            win32service.SERVICE_RUNNING: "running",
            win32service.SERVICE_CONTINUE_PENDING: "resuming",
            win32service.SERVICE_PAUSE_PENDING: "pausing",
            win32service.SERVICE_PAUSED: "paused",
        }
    return _STATE_NAMES


def query_state():
    """Return the service state as a lowercase word, or None if not installed."""
    try:
        import win32serviceutil
        status = win32serviceutil.QueryServiceStatus(SERVICE_NAME)
    except Exception:
        return None
    return _state_names().get(status[1], "unknown")


def is_running():
    return query_state() == "running"


# ---------------------------------------------------------------------------
# The service itself
# ---------------------------------------------------------------------------

def _make_service_class():
    """Build the ServiceFramework subclass lazily.

    Defining it at import time would make every plain console run (--selftest,
    --flash, --help) pay for importing win32service and friends.
    """
    import win32service
    import win32serviceutil

    class SysmonService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = threading.Event()

        def GetAcceptedControls(self):
            # Base class only offers stop/shutdown. Power events matter here:
            # the macropad re-enumerates across sleep, and the ryzenadj SMU
            # mapping does not necessarily survive S0ix, so a resume needs to
            # force both to be re-established.
            accepted = super().GetAcceptedControls()
            accepted |= win32service.SERVICE_ACCEPT_POWEREVENT
            accepted |= win32service.SERVICE_ACCEPT_SHUTDOWN
            return accepted

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.stop_event.set()

        def SvcShutdown(self):
            self.SvcStop()

        def SvcOtherEx(self, control, event_type, data):
            if control == win32service.SERVICE_CONTROL_POWEREVENT:
                # PBT_APMRESUMEAUTOMATIC / PBT_APMRESUMESUSPEND
                if event_type in (0x12, 0x07):
                    log("Resumed from sleep; re-establishing device and sensors")
                    _request_resume()
                return
            return super().SvcOtherEx(control, event_type, data)

        def SvcDoRun(self):
            start_service_logging()
            import pythoncom
            # Every WMI source goes through COM and this is not the thread
            # pywin32 initialised, so without this each read raises
            # CoInitialize-has-not-been-called and the whole loop degrades.
            pythoncom.CoInitialize()
            try:
                self.ReportServiceStatus(win32service.SERVICE_RUNNING)
                event_log(f"{SERVICE_DISPLAY} {_version()} started")
                _run_engine(self.stop_event)
            except Exception as err:
                import traceback
                event_log(f"{SERVICE_DISPLAY} failed: {err}\n"
                          f"{traceback.format_exc()}", error=True)
                raise
            finally:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
                event_log(f"{SERVICE_DISPLAY} stopped")

    return SysmonService


_resume_flag = threading.Event()


def _request_resume():
    _resume_flag.set()


def resume_requested():
    """True once per resume; the engine polls this to force a reconnect."""
    if _resume_flag.is_set():
        _resume_flag.clear()
        return True
    return False


def _version():
    try:
        return paths.engine_module().__version__
    except Exception:
        return "?"


def _run_engine(stop_event):
    """Start the poll loop with the service's own config, honouring stop.

    argv is empty on purpose: a service's tuning comes from the INI, not from
    whatever flags happen to be baked into the registered binPath.
    """
    paths.engine_module().main(argv=[], stop_event=stop_event,
                               resume_check=resume_requested)


def run_dispatcher():
    """SCM entry point: host the service in this process.

    This is the frozen-safe sequence. `win32serviceutil.HandleCommandLine`
    assumes a python.exe it can re-invoke, which does not exist in a bundle;
    hosting the dispatcher directly does not.
    """
    import servicemanager
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(_make_service_class())
    servicemanager.StartServiceCtrlDispatcher()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _bin_path():
    """The command the SCM should run.

    Frozen: the bundle exe plus --service. Source checkout: the interpreter,
    the script and --service, so the service can be exercised before packaging.
    """
    if paths.is_frozen():
        return f'"{sys.executable}" --service'
    script = os.path.join(paths.app_dir(), "fw16_sysmon.py")
    return f'"{sys.executable}" "{script}" --service'


def install():
    win32service, win32serviceutil = _svc_modules()
    existing = query_state()
    if existing is not None:
        log(f"Service already installed ({existing}); reconfiguring")
        uninstall()

    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    try:
        handle = win32service.CreateService(
            scm,
            SERVICE_NAME,
            SERVICE_DISPLAY,
            win32service.SERVICE_ALL_ACCESS,
            win32service.SERVICE_WIN32_OWN_PROCESS,
            win32service.SERVICE_AUTO_START,
            win32service.SERVICE_ERROR_NORMAL,
            _bin_path(),
            None,     # no load-order group
            0,        # no tag id
            None,     # no dependencies: the loop already retries the HID device
                      # and the GPU counters, so starting before the USB and
                      # graphics stacks are up is handled rather than avoided
            None,     # LocalSystem
            None,
        )
    except Exception as err:
        print(f"Could not create the service: {err}", file=sys.stderr)
        return 1
    try:
        win32service.ChangeServiceConfig2(
            handle, win32service.SERVICE_CONFIG_DESCRIPTION, SERVICE_DESCRIPTION)
        # Restart on crash. A wedged sensor DLL taking the process down should
        # not mean a dark macropad until the next reboot.
        win32service.ChangeServiceConfig2(
            handle, win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
            {
                "ResetPeriod": 86400,
                "RebootMsg": None,
                "Command": None,
                "Actions": [
                    (win32service.SC_ACTION_RESTART, 10000),
                    (win32service.SC_ACTION_RESTART, 30000),
                    (win32service.SC_ACTION_RESTART, 60000),
                ],
            })
    except Exception as err:
        log(f"Service created but could not be fully configured: {err}")
    finally:
        win32service.CloseServiceHandle(handle)
        win32service.CloseServiceHandle(scm)

    print(f"Installed service '{SERVICE_NAME}' ({SERVICE_DISPLAY})")
    print(f"  binary : {_bin_path()}")
    print(f"  account: LocalSystem, start: Automatic (at boot)")
    return 0


def uninstall():
    win32service, win32serviceutil = _svc_modules()
    if query_state() is None:
        print(f"Service '{SERVICE_NAME}' is not installed")
        return 0
    stop()
    try:
        win32serviceutil.RemoveService(SERVICE_NAME)
    except Exception as err:
        print(f"Could not remove the service: {err}", file=sys.stderr)
        return 1
    print(f"Removed service '{SERVICE_NAME}'")
    return 0


def start():
    win32service, win32serviceutil = _svc_modules()
    state = query_state()
    if state is None:
        print(f"Service '{SERVICE_NAME}' is not installed", file=sys.stderr)
        return 1
    if state == "running":
        print(f"Service '{SERVICE_NAME}' is already running")
        return 0
    try:
        win32serviceutil.StartService(SERVICE_NAME)
        win32serviceutil.WaitForServiceStatus(
            SERVICE_NAME, win32service.SERVICE_RUNNING, waitSecs=30)
    except Exception as err:
        print(f"Could not start the service: {err}", file=sys.stderr)
        print(f"Check {paths.log_path()} for why.", file=sys.stderr)
        return 1
    print(f"Service '{SERVICE_NAME}' is running")
    return 0


def stop(quiet=False):
    win32service, win32serviceutil = _svc_modules()
    state = query_state()
    if state is None:
        if not quiet:
            print(f"Service '{SERVICE_NAME}' is not installed")
        return 0
    if state == "stopped":
        if not quiet:
            print(f"Service '{SERVICE_NAME}' is already stopped")
        return 0
    try:
        win32serviceutil.StopService(SERVICE_NAME)
        win32serviceutil.WaitForServiceStatus(
            SERVICE_NAME, win32service.SERVICE_STOPPED, waitSecs=30)
    except Exception as err:
        print(f"Could not stop the service: {err}", file=sys.stderr)
        return 1
    if not quiet:
        print(f"Service '{SERVICE_NAME}' stopped")
    return 0


def restart():
    """Stop then start -- what applying an edited sysmon.ini needs."""
    if query_state() is None:
        print(f"Service '{SERVICE_NAME}' is not installed", file=sys.stderr)
        return 1
    rc = stop(quiet=True)
    if rc != 0:
        return rc
    return start()


def handle_cli(args):
    if args.uninstall_service:
        return uninstall()
    if args.install_service:
        return install()
    if args.restart_service:
        return restart()
    if args.start_service:
        return start()
    if args.stop_service:
        return stop()
    return 0
