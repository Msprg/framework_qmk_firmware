<#
.SYNOPSIS
    Download the redistributable native dependencies the installer bundles.

.DESCRIPTION
    The service runs as LocalSystem, and the script's original ryzenadj
    discovery (shutil.which + %USERPROFILE%\scoop\apps\...) is user-scoped:
    under LocalSystem, %USERPROFILE% is C:\Windows\system32\config\systemprofile
    and a user's scoop shims are not on the machine PATH. So the DLLs are
    shipped rather than discovered.

    Two sets are fetched:

      host\vendor\   libryzenadj.dll + WinRing0x64.dll/.sys + inpoutx64.dll,
                     from the RyzenAdj release. The installer places these
                     *beside the exe*, which is where WinRing0 looks for its
                     kernel driver (GetModuleFileName(NULL)).
      host\lib\      LibreHardwareMonitorLib + HidSharp + the netstandard BCL
                     shims, via the host's own --fetch-lhm (it already picks
                     the correct RID and target framework).

    Both directories are gitignored: these are third-party binaries, and the
    licences that permit redistribution are recorded in THIRD-PARTY-NOTICES.md.

.PARAMETER RyzenAdjVersion
    RyzenAdj release tag to pin. Pinned rather than "latest" so a build is
    reproducible; bump it deliberately.

.PARAMETER Python
    Interpreter used for the --fetch-lhm step. Defaults to the host venv.
#>
[CmdletBinding()]
param(
    [string]$RyzenAdjVersion = "v0.19.0",
    [string]$Python = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$PackagingDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$HostDir      = Split-Path -Parent $PackagingDir
$VendorDir    = Join-Path $HostDir "vendor"
$LibDir       = Join-Path $HostDir "lib"

# Exactly the files RyzenAdjSource needs: the library, the ring0 driver it
# loads, and the two shims that driver stack pulls in.
$RyzenAdjFiles = @(
    "libryzenadj.dll",
    "WinRing0x64.dll",
    "WinRing0x64.sys",
    "inpoutx64.dll"
)

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor DarkGray }

# See the note in build.ps1: PowerShell 5.1 turns native stderr into a
# terminating error under $ErrorActionPreference = "Stop", so exit codes are
# checked explicitly instead.
function Invoke-Native {
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @(), [string]$What = "")
    if (-not $What) { $What = Split-Path -Leaf $Exe }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

# --- RyzenAdj -------------------------------------------------------------
Write-Step "Fetching RyzenAdj $RyzenAdjVersion"
New-Item -ItemType Directory -Force -Path $VendorDir | Out-Null

$missing = $RyzenAdjFiles | Where-Object { -not (Test-Path (Join-Path $VendorDir $_)) }
if ($missing.Count -eq 0 -and -not $Force) {
    Write-Ok "all present, skipping download (use -Force to refresh)"
} else {
    $url = "https://github.com/FlyGoat/RyzenAdj/releases/download/$RyzenAdjVersion/libryzenadj-win64.zip"
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "libryzenadj-$RyzenAdjVersion.zip"
    $extract = Join-Path ([System.IO.Path]::GetTempPath()) "libryzenadj-$RyzenAdjVersion"

    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing -TimeoutSec 120
    if (Test-Path $extract) { Remove-Item -Recurse -Force $extract }
    Expand-Archive -Path $tmp -DestinationPath $extract -Force

    foreach ($f in $RyzenAdjFiles) {
        $src = Join-Path $extract $f
        $dst = Join-Path $VendorDir $f
        if (-not (Test-Path $src)) {
            throw "$f is missing from libryzenadj-win64.zip -- has the release layout changed?"
        }
        if ((Test-Path $dst) -and -not $Force) { Write-Ok "have    $f"; continue }
        try {
            Copy-Item $src $dst -Force
        } catch [System.IO.IOException] {
            # A running sysmon service holds libryzenadj.dll mapped in-process.
            throw "Cannot write $f -- a running sysmon service or scheduled task " +
                  "still has it loaded. Stop it (sc stop FW16Sysmon) and retry. ($_)"
        }
        Write-Ok "$f  ($([math]::Round((Get-Item $src).Length / 1KB)) KB)"
    }
    Remove-Item -Recurse -Force $extract, $tmp -ErrorAction SilentlyContinue
}

# The driver is the piece most likely to be blocked on an end user's machine
# (Memory Integrity / the vulnerable-driver blocklist). Report what we shipped
# so a signature problem is visible at build time rather than at install time.
$sig = Get-AuthenticodeSignature (Join-Path $VendorDir "WinRing0x64.sys")
Write-Ok "WinRing0x64.sys signature: $($sig.Status)"

# --- LibreHardwareMonitorLib ---------------------------------------------
Write-Step "Fetching LibreHardwareMonitorLib + deps"

if (-not $Python) {
    $venvPy = Join-Path $HostDir ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { $Python = $venvPy } else { $Python = "python" }
}
Write-Ok "using $Python"

$lhmArgs = @((Join-Path $HostDir "fw16_sysmon.py"), "--fetch-lhm")
if ($Force) { $lhmArgs += "--force" }
Invoke-Native $Python $lhmArgs "--fetch-lhm"

# --- summary --------------------------------------------------------------
Write-Step "Vendored payload"
foreach ($d in @($VendorDir, $LibDir)) {
    Get-ChildItem $d -File -Exclude ".gitignore" |
        ForEach-Object { Write-Ok ("{0,-42} {1,7} KB" -f $_.FullName.Replace("$HostDir\", ""), [math]::Round($_.Length / 1KB)) }
}
Write-Host "Done." -ForegroundColor Green
