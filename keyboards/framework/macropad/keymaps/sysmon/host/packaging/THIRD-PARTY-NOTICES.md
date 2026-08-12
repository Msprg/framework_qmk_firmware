# Third-party notices — FW16 Macropad Sysmon

The installer bundles the components below. Each is redistributed under the
licence named, and each licence permits redistribution in the form used here.

sysmon itself is part of a QMK fork and is licensed **GPL-2.0-or-later**.
The "or later" matters for one of these dependencies: RyzenAdj is LGPL-3.0,
which is not compatible with GPL-2.0 *only*, but is compatible with GPL-3.0.
The combined work is therefore distributed under GPL-3.0.

Source for the sysmon host and firmware:
<https://github.com/Msprg/framework_qmk_firmware> (branch `macropad-sysmon`).

---

## Bundled native libraries

### RyzenAdj — `libryzenadj.dll`
- **Licence:** LGPL-3.0-or-later
- **Upstream:** <https://github.com/FlyGoat/RyzenAdj>
- **Used for:** reading the AMD SMU power-metrics table (STAPM / PPT limits and
  the Tctl die temperature) — the same source HWiNFO reads.
- **LGPL compliance:** loaded dynamically at runtime via `ctypes`, and shipped
  as a separate, unmodified `.dll` file rather than linked in. A user may
  replace it with their own build by overwriting the file in the install
  directory, or point the host at a different copy with `--ryzenadj-dir`.
  Unmodified upstream binary, taken from the project's `libryzenadj-win64.zip`
  release asset.

### WinRing0 — `WinRing0x64.dll`, `WinRing0x64.sys`
- **Licence:** BSD-style (OpenLibSys.org), redistribution permitted with notice
- **Upstream:** <https://openlibsys.org/> (redistributed within RyzenAdj)
- **Used for:** the ring0 access `libryzenadj` and LibreHardwareMonitorLib need
  to read CPU MSRs and the SMU.
- **Note for users:** this driver is old and widely known. Windows may refuse to
  load it when **Memory Integrity (HVCI)** or the **vulnerable-driver blocklist**
  is enforcing. That is a deliberate Windows security decision, not a fault in
  this installer. When it is blocked, sysmon loses the CPU-row power-limit
  breathe and the in-process temperature source, and falls back to reading
  temperature from the EC via `framework_tool`. `--selftest` reports which
  applies on a given machine.

### inpoutx64 — `inpoutx64.dll`
- **Licence:** freely redistributable (see upstream)
- **Upstream:** <http://www.highrez.co.uk/downloads/inpout32/>
- **Used for:** a port-I/O shim in the RyzenAdj driver stack.

### LibreHardwareMonitorLib — `LibreHardwareMonitorLib.dll`
- **Licence:** MPL-2.0
- **Upstream:** <https://github.com/LibreHardwareMonitor/LibreHardwareMonitor>
- **Used for:** in-process CPU package power, load and temperature — the
  fallback APU source when RyzenAdj is unavailable (e.g. on Intel machines).
- **MPL compliance:** unmodified binary, obtained from the official NuGet
  package. Source is available at the URL above.

### HidSharp — `HidSharp.dll`
- **Licence:** Apache-2.0
- **Upstream:** <https://www.zer7.com/software/hidsharp>
- **Used for:** a LibreHardwareMonitorLib dependency.

### .NET BCL compatibility shims
`System.Memory.dll`, `System.Buffers.dll`,
`System.Runtime.CompilerServices.Unsafe.dll`, `System.Numerics.Vectors.dll`,
`System.Threading.Tasks.Extensions.dll`
- **Licence:** MIT
- **Upstream:** <https://github.com/dotnet/runtime>
- **Used for:** LibreHardwareMonitorLib's `net472` build needs these
  netstandard2.0 assemblies, which are not in-box on .NET Framework.

---

## Bundled Python runtime and packages

The host is frozen with PyInstaller, so a Python interpreter and these packages
are embedded in the executable.

| Component | Licence | Upstream |
|---|---|---|
| CPython | PSF-2.0 | <https://www.python.org/> |
| PyInstaller bootloader | GPL-2.0-or-later **with** the PyInstaller linking exception | <https://pyinstaller.org/> |
| hidapi (`hid`) | BSD-3-Clause (tri-licensed; BSD taken) | <https://github.com/libusb/hidapi> |
| psutil | BSD-3-Clause | <https://github.com/giampaolo/psutil> |
| pywin32 | PSF-style | <https://github.com/mhammond/pywin32> |
| WMI | MIT | <https://pypi.org/project/WMI/> |
| nvidia-ml-py (`pynvml`) | BSD-3-Clause | <https://pypi.org/project/nvidia-ml-py/> |
| pythonnet | MIT | <https://github.com/pythonnet/pythonnet> |
| cffi / pycparser | MIT / BSD-3-Clause | <https://cffi.readthedocs.io/> |

---

## Not bundled

These are detected at runtime and never redistributed:

- **framework_tool** (Framework Computer, BSD-3-Clause) — optional. Provides the
  EC temperature fallback and the battery max-charge cap. Install with
  `winget install FrameworkComputer.framework_tool`.
- **NVIDIA driver / `nvml.dll`** — ships with the graphics driver; used through
  `pynvml` when an NVIDIA dGPU is present.
