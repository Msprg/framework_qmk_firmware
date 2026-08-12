<#
.SYNOPSIS
    Build FW16-Sysmon-Setup-<version>.exe from a clean checkout.

.DESCRIPTION
    One command, end to end:

      1. build venv       an isolated venv with the host's runtime deps plus
                          PyInstaller. Kept separate from host\.venv, which is
                          manifested requireAdministrator and cannot be launched
                          from a normal shell.
      2. vendor           fetch-vendor.ps1 (libryzenadj + WinRing0, LHM DLLs)
      3. firmware         reuse .build\framework_macropad_sysmon.uf2, or rebuild
                          it with -BuildFirmware (needs QMK MSYS)
      4. freeze           PyInstaller onedir -> dist\fw16-sysmon\
      5. installer        Inno Setup -> dist\FW16-Sysmon-Setup-<version>.exe

    Anything already satisfied is skipped, so re-runs are cheap. Use -Clean to
    force a rebuild from scratch.

.PARAMETER BuildFirmware
    Rebuild the .uf2 through QMK MSYS instead of reusing the existing build.
    This fork predates GNU Make 4.4, so the top-level keyboard:keymap target
    does not work and the build makefile is invoked directly.

.PARAMETER SkipVendor
    Reuse whatever is already in host\vendor and host\lib.

.PARAMETER Clean
    Delete build\ and dist\ first.

.EXAMPLE
    .\build.ps1
.EXAMPLE
    .\build.ps1 -Clean -BuildFirmware
#>
[CmdletBinding()]
param(
    [switch]$BuildFirmware,
    [switch]$SkipVendor,
    [switch]$SkipSmokeTest,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$PackagingDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$HostDir      = Split-Path -Parent $PackagingDir
$RepoRoot     = (Resolve-Path (Join-Path $HostDir "..\..\..\..\..\..")).Path
$BuildVenv    = Join-Path $PackagingDir ".buildvenv"
$DistDir      = Join-Path $HostDir "dist"
$WorkDir      = Join-Path $HostDir "build"
$Uf2Name      = "framework_macropad_sysmon.uf2"

function Write-Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "    $m" -ForegroundColor DarkGray }

# Windows PowerShell 5.1 turns a native command's stderr into an ErrorRecord,
# and with $ErrorActionPreference = "Stop" that aborts the build even when the
# tool exited 0. uv, pip and PyInstaller all write progress to stderr, so every
# native call goes through here: preference relaxed for the call, success judged
# by the exit code alone.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @(),
        [string]$What = "",
        [switch]$Quiet
    )
    if (-not $What) { $What = Split-Path -Leaf $Exe }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Quiet) {
            & $Exe @Arguments 2>&1 | Out-Null
        } else {
            & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        }
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

# --- version --------------------------------------------------------------
$verLine = Select-String -Path (Join-Path $HostDir "fw16_sysmon.py") `
                         -Pattern '^__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $verLine) { throw "Could not read __version__ from fw16_sysmon.py" }
$Version = $verLine.Matches[0].Groups[1].Value
Write-Host "FW16 Macropad Sysmon $Version" -ForegroundColor Green
Write-Ok "repo: $RepoRoot"

if ($Clean) {
    Write-Step "Cleaning"
    foreach ($d in @($DistDir, $WorkDir)) {
        if (Test-Path $d) { Remove-Item -Recurse -Force $d; Write-Ok "removed $d" }
    }
}

# --- 1. build venv --------------------------------------------------------
Write-Step "Build environment"

# uv ships with QMK MSYS on this machine and is dramatically faster than pip;
# fall back to whatever python is on PATH.
$uv = "C:\QMK_MSYS\opt\uv\uv.exe"
$haveUv = Test-Path $uv

$py = Join-Path $BuildVenv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    # A half-created venv from an interrupted run has the directory but no
    # interpreter, and both uv and venv refuse to write into it.
    if (Test-Path $BuildVenv) {
        Write-Ok "removing incomplete $BuildVenv"
        Remove-Item -Recurse -Force $BuildVenv
    }
    Write-Ok "creating $BuildVenv"
    if ($haveUv) {
        Invoke-Native $uv @("venv", "--python", "3.12", $BuildVenv) "uv venv"
    } else {
        $base = (Get-Command python -ErrorAction Stop).Source
        Invoke-Native $base @("-m", "venv", $BuildVenv) "python -m venv"
    }
}
if (-not (Test-Path $py)) { throw "Build venv creation failed ($py missing)" }

$requirements = @(
    "pyinstaller",
    "hidapi", "psutil", "wmi", "pywin32", "nvidia-ml-py", "pythonnet"
)
Write-Ok "installing: $($requirements -join ', ')"
if ($haveUv) {
    Invoke-Native $uv (@("pip", "install", "--python", $py) + $requirements) "uv pip install"
} else {
    Invoke-Native $py (@("-m", "pip", "install", "--upgrade") + $requirements) "pip install"
}

# pywin32 drops its DLLs in site-packages\pywin32_system32; without this
# postinstall they are not where the loader expects and the frozen service
# entry point fails at import time.
$pwi = Join-Path $BuildVenv "Scripts\pywin32_postinstall.py"
if (Test-Path $pwi) {
    Invoke-Native $py @($pwi, "-install", "-silent", "-quiet") "pywin32_postinstall" -Quiet
}

# --- 2. vendored natives --------------------------------------------------
if ($SkipVendor) {
    Write-Step "Vendored natives (skipped)"
} else {
    Write-Step "Vendored natives"
    & (Join-Path $PackagingDir "fetch-vendor.ps1") -Python $py
}

foreach ($f in @("libryzenadj.dll", "WinRing0x64.dll", "WinRing0x64.sys", "inpoutx64.dll")) {
    $p = Join-Path $HostDir "vendor\$f"
    if (-not (Test-Path $p)) { throw "missing vendored file: $p (run without -SkipVendor)" }
}
if (-not (Test-Path (Join-Path $HostDir "lib\LibreHardwareMonitorLib.dll"))) {
    throw "missing lib\LibreHardwareMonitorLib.dll (run without -SkipVendor)"
}

# --- 3. firmware ----------------------------------------------------------
Write-Step "Firmware image"
$builtUf2 = Join-Path $RepoRoot ".build\$Uf2Name"

if ($BuildFirmware) {
    Write-Ok "rebuilding through QMK MSYS"
    # This fork's vintage predates GNU Make 4.4, whose parser rejects QMK's
    # keyboard:keymap targets, so build_keyboard.mk is invoked directly.
    # USERPROFILE must be set: the native qmk.exe calls Path.home(), and the
    # MSYS login shell leaves USERPROFILE empty while setting only a Unix HOME.
    $msysRepo = "/" + $RepoRoot.Replace(":", "").Replace("\", "/")
    $cmd = "export USERPROFILE='$($RepoRoot.Replace('\','/'))' && cd $msysRepo && " +
           "make -r -R -f builddefs/build_keyboard.mk -j8 " +
           "KEYBOARD=framework/macropad KEYMAP=sysmon " +
           "TARGET=framework_macropad_sysmon QMK_BIN=qmk SILENT=false"
    $env:MSYSTEM = "MINGW64"
    Invoke-Native "C:\QMK_MSYS\usr\bin\bash.exe" @("-l", "-c", $cmd) "firmware build"
}

if (-not (Test-Path $builtUf2)) {
    throw "No firmware at $builtUf2. Build it once (-BuildFirmware) or copy a .uf2 there."
}
Copy-Item $builtUf2 (Join-Path $PackagingDir $Uf2Name) -Force
Write-Ok "$Uf2Name  ($([math]::Round((Get-Item $builtUf2).Length / 1KB)) KB, built $((Get-Item $builtUf2).LastWriteTime))"

# --- 4. version resource + freeze ----------------------------------------
Write-Step "Version resource"
$v4 = ($Version -split '\.') + @("0", "0", "0", "0") | Select-Object -First 4
$vtuple = "($($v4 -join ', '))"
@"
# Generated by build.ps1 -- do not edit.
VSVersionInfo(
  ffi=FixedFileInfo(filevers=$vtuple, prodvers=$vtuple, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Msprg'),
      StringStruct('FileDescription', 'FW16 Macropad Sysmon host'),
      StringStruct('FileVersion', '$Version'),
      StringStruct('InternalName', 'fw16-sysmon'),
      StringStruct('LegalCopyright', 'Copyright 2026 Msprg. GPL-2.0-or-later.'),
      StringStruct('OriginalFilename', 'fw16-sysmon.exe'),
      StringStruct('ProductName', 'FW16 Macropad Sysmon'),
      StringStruct('ProductVersion', '$Version')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Set-Content -Path (Join-Path $PackagingDir "version_info.txt") -Encoding utf8

# Inno needs a .txt licence file; reuse the repo's GPL text.
Copy-Item (Join-Path $RepoRoot "LICENSE") (Join-Path $PackagingDir "LICENSE.txt") -Force

Write-Step "Freezing with PyInstaller"
Push-Location $HostDir
try {
    # log-level WARN: the INFO stream is thousands of lines of module analysis
    # and drowns everything else this script reports.
    Invoke-Native $py @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
        "--distpath", $DistDir, "--workpath", $WorkDir,
        (Join-Path $PackagingDir "fw16_sysmon.spec")
    ) "PyInstaller"
} finally {
    Pop-Location
}

$BundleDir = Join-Path $DistDir "fw16-sysmon"
$exe = Join-Path $BundleDir "fw16-sysmon.exe"
if (-not (Test-Path $exe)) { throw "PyInstaller produced no exe at $exe" }

# --- 4b. stage the payload PyInstaller does not own ----------------------
# Everything the installed program needs is assembled here rather than by the
# Inno script, so that dist\fw16-sysmon IS the installed layout: it can be run
# and self-tested directly, and Inno just copies the tree. Splitting the
# placement across two tools is how you get a bundle that passes its build and
# then cannot find its own DLLs.
#
# The native libraries go beside the exe, NOT under _internal: WinRing0
# resolves its kernel driver relative to the process image.
Write-Step "Staging bundled payload"
foreach ($f in @("libryzenadj.dll", "WinRing0x64.dll", "WinRing0x64.sys", "inpoutx64.dll")) {
    Copy-Item (Join-Path $HostDir "vendor\$f") $BundleDir -Force
    Write-Ok "$f"
}
$bundleLib = Join-Path $BundleDir "lib"
New-Item -ItemType Directory -Force -Path $bundleLib | Out-Null
Copy-Item (Join-Path $HostDir "lib\*.dll") $bundleLib -Force
Write-Ok "lib\ ($((Get-ChildItem $bundleLib -Filter *.dll).Count) .NET assemblies)"

Copy-Item (Join-Path $PackagingDir $Uf2Name) $BundleDir -Force
Copy-Item (Join-Path $PackagingDir "THIRD-PARTY-NOTICES.md") $BundleDir -Force
Copy-Item (Join-Path $PackagingDir "LICENSE.txt") $BundleDir -Force
Copy-Item (Join-Path $HostDir "..\readme.md") $BundleDir -Force
Write-Ok "$Uf2Name, notices, licence, readme"

$bundleSize = (Get-ChildItem $BundleDir -Recurse -File |
               Measure-Object -Property Length -Sum).Sum
Write-Ok "bundle: $([math]::Round($bundleSize / 1MB, 1)) MB"

# Smoke-test the frozen exe before wrapping it in an installer: --version
# exercises the whole import graph, which is where a missing hidden import
# (win32timezone is the classic) shows up.
# Smoke-test the frozen exe before wrapping it in an installer. --selftest
# walks the entire import graph and touches every sensor, which is where a
# missing hidden import shows up (win32timezone is the classic: it is imported
# by name inside pywin32 and otherwise only fails when the SCM starts the
# service). The exe is manifested requireAdministrator because the ring0 sensors
# need it, so an unelevated shell cannot launch it directly.
Write-Step "Smoke-testing the frozen executable"
$elevated = ([Security.Principal.WindowsPrincipal] `
             [Security.Principal.WindowsIdentity]::GetCurrent()
            ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($SkipSmokeTest) {
    Write-Host "    skipped (-SkipSmokeTest)" -ForegroundColor Yellow
} elseif ($elevated) {
    Invoke-Native $exe @("--version") "frozen exe --version"
    Write-Ok "running --selftest"
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & $exe --selftest 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }
    finally { $ErrorActionPreference = $prev }
} else {
    Write-Ok "not elevated; re-launching the check with a UAC prompt"
    $log = Join-Path ([System.IO.Path]::GetTempPath()) "fw16-sysmon-smoke.txt"
    if (Test-Path $log) { Remove-Item $log -Force }
    $p = Start-Process -FilePath $env:ComSpec `
            -ArgumentList @("/c", "`"`"$exe`" --version && `"$exe`" --selftest`" > `"$log`" 2>&1") `
            -Verb RunAs -Wait -PassThru -ErrorAction SilentlyContinue
    if ($null -eq $p) {
        Write-Host "    UAC declined -- smoke test skipped." -ForegroundColor Yellow
        Write-Host "    Verify after installing with: fw16-sysmon.exe --selftest" -ForegroundColor Yellow
    } else {
        if (Test-Path $log) {
            Get-Content $log | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        }
        # --selftest exits 1 when something on this machine needs attention,
        # which is a fact about the machine, not a build failure. Only a crash
        # (no output at all) means the bundle is broken.
        if (-not (Test-Path $log) -or ((Get-Content $log -Raw).Trim() -eq "")) {
            throw "the frozen executable produced no output -- the bundle is broken"
        }
        if ((Get-Content $log -Raw) -match "ModuleNotFoundError|ImportError|Failed to execute script") {
            throw "the frozen executable failed to import; see $log"
        }
    }
}

# --- 5. installer ---------------------------------------------------------
Write-Step "Building the installer"

# winget installs Inno Setup per-user under %LOCALAPPDATA%\Programs when it is
# not elevated, and machine-wide under Program Files when it is, so both have to
# be probed. The registry entry is checked too, in case of a custom location.
function Find-Iscc {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($key in @(
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1")) {
        if (Test-Path $key) {
            $loc = (Get-ItemProperty $key -ErrorAction SilentlyContinue).InstallLocation
            if ($loc) { $candidates += (Join-Path $loc "ISCC.exe") }
        }
    }
    $onPath = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

$iscc = Find-Iscc
if (-not $iscc) {
    Write-Host "Inno Setup not found; installing via winget..." -ForegroundColor Yellow
    Invoke-Native "winget.exe" @(
        "install", "--id", "JRSoftware.InnoSetup", "--silent",
        "--accept-source-agreements", "--accept-package-agreements") "winget install Inno Setup"
    $iscc = Find-Iscc
    if (-not $iscc) {
        throw "Inno Setup installed but ISCC.exe was not found. Install it manually " +
              "from https://jrsoftware.org/isdl.php and re-run."
    }
}
Write-Ok "using $iscc"

Invoke-Native $iscc @("/DAppVersion=$Version", (Join-Path $PackagingDir "installer.iss")) "Inno Setup"

$setup = Join-Path $DistDir "FW16-Sysmon-Setup-$Version.exe"
if (-not (Test-Path $setup)) { throw "installer missing at $setup" }

Write-Host ""
Write-Host "Built $setup" -ForegroundColor Green
Write-Ok "$([math]::Round((Get-Item $setup).Length / 1MB, 1)) MB"
Write-Host ""
Write-Host "The installer is unsigned, so SmartScreen will warn on first run." -ForegroundColor Yellow
Write-Host "Install with:  `"$setup`"" -ForegroundColor Gray
