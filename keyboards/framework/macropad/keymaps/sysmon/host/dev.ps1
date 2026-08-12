<#
.SYNOPSIS
    Run the sysmon host from source, with the installed service kept out of the way.

.DESCRIPTION
    The iteration loop for hacking on the host or the firmware. It handles the
    three things that otherwise have to be remembered every single time:

      * The macropad has ONE raw-HID endpoint. The installed service writes to
        it ~2.5x a second, so a source run fights it and both misbehave. This
        stops the service first and restarts it on the way out -- including on
        Ctrl+C, which is how a live view normally ends.
      * The ring0 sensors need elevation, and host\.venv\Scripts\python.exe is
        manifested requireAdministrator, so it cannot even be launched from a
        normal shell. This self-elevates instead of failing with "code 740".
      * Running from source picks up .build\framework_macropad_sysmon.uf2
        automatically, so -Flash always flashes what was just built.

    With no switches: stop service -> live view (--verbose) -> restart service.

.PARAMETER Build
    Rebuild the firmware through QMK MSYS first. This fork predates GNU Make
    4.4, so build_keyboard.mk is invoked directly (see the readme).

.PARAMETER Flash
    Flash the firmware onto the macropad (bootloader jump, wait for RPI-RP2,
    copy, wait for it to come back). Combine with -Build for edit -> flash.

.PARAMETER Bootloader
    Just send the bootloader-jump packet and exit.

.PARAMETER SelfTest
    Run the dependency/capability probe against the source tree.

.PARAMETER KeepService
    Leave the service running. Only useful for testing what two concurrent
    writers actually do to the display.

.PARAMETER Rest
    Everything else is passed through to fw16_sysmon.py.

.EXAMPLE
    .\dev.ps1
.EXAMPLE
    .\dev.ps1 -Build -Flash
.EXAMPLE
    .\dev.ps1 -- --interval 1.0 --max-led 200
#>
[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$Flash,
    [switch]$Bootloader,
    [switch]$SelfTest,
    [switch]$KeepService,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"

$HostDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $HostDir "..\..\..\..\..\..")).Path
$Script   = Join-Path $HostDir "fw16_sysmon.py"
$Python   = Join-Path $HostDir ".venv\Scripts\python.exe"
$Service  = "FW16Sysmon"

# --- self-elevate ---------------------------------------------------------
# The venv interpreter is requireAdministrator-manifested; CreateProcess cannot
# elevate, so a non-elevated launch dies with ERROR_ELEVATION_REQUIRED before
# anything useful happens. Re-launch through ShellExecute instead.
$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Elevating..." -ForegroundColor Yellow
    $fwd = @()
    foreach ($s in @("Build", "Flash", "Bootloader", "SelfTest", "KeepService")) {
        if ($PSBoundParameters[$s]) { $fwd += "-$s" }
    }
    if ($Rest) { $fwd += $Rest }
    $argList = @("-NoExit", "-ExecutionPolicy", "Bypass", "-File", "`"$($MyInvocation.MyCommand.Path)`"") + $fwd
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    return
}

function Write-Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }

if (-not (Test-Path $Python)) {
    throw "No venv at $Python. Create it, then: " +
          "uv pip install --python `"$Python`" hidapi psutil wmi pywin32 nvidia-ml-py pythonnet"
}

# --- optional firmware rebuild -------------------------------------------
if ($Build) {
    Write-Step "Building firmware"
    # USERPROFILE must be set explicitly: the native qmk.exe calls Path.home(),
    # and the MSYS login shell leaves USERPROFILE empty while setting only a
    # Unix-style HOME, which ntpath.expanduser ignores.
    $msysRepo = "/" + $RepoRoot.Replace(":", "").Replace("\", "/")
    $cmd = "export USERPROFILE='$($RepoRoot.Replace('\','/'))' && cd $msysRepo && " +
           "make -r -R -f builddefs/build_keyboard.mk -j8 " +
           "KEYBOARD=framework/macropad KEYMAP=sysmon " +
           "TARGET=framework_macropad_sysmon QMK_BIN=qmk SILENT=false"
    $env:MSYSTEM = "MINGW64"
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & C:\QMK_MSYS\usr\bin\bash.exe -l -c $cmd } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "firmware build failed (exit $LASTEXITCODE)" }
    $uf2 = Join-Path $RepoRoot ".build\framework_macropad_sysmon.uf2"
    Write-Host "    $uf2 ($([math]::Round((Get-Item $uf2).Length / 1KB)) KB)" -ForegroundColor DarkGray
}

# --- get the service out of the way --------------------------------------
$wasRunning = $false
if (-not $KeepService) {
    $svc = Get-Service -Name $Service -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") {
        Write-Step "Stopping $Service (it owns the macropad's raw-HID endpoint)"
        Stop-Service -Name $Service
        $wasRunning = $true
    }
}

try {
    $argv = @($Script)
    if     ($Bootloader) { $argv += "--bootloader" }
    elseif ($Flash)      { $argv += "--flash" }
    elseif ($SelfTest)   { $argv += "--selftest" }
    else                 { $argv += "--verbose" }
    if ($Rest) { $argv += $Rest }

    Write-Step "python $($argv -join ' ')"
    if (-not ($Bootloader -or $Flash -or $SelfTest)) {
        Write-Host "    Ctrl+C to stop; the service is restarted automatically." -ForegroundColor DarkGray
    }
    Write-Host ""

    # Native stderr must not be treated as a terminating error here: the engine
    # prints all its diagnostics there, and --verbose is the whole point.
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & $Python @argv } finally { $ErrorActionPreference = $prev }
}
finally {
    # Runs on Ctrl+C too, which is the normal way out of a live view.
    if ($wasRunning) {
        Write-Step "Restarting $Service"
        Start-Service -Name $Service
        Write-Host "    $((Get-Service -Name $Service).Status)" -ForegroundColor DarkGray
    }
}
