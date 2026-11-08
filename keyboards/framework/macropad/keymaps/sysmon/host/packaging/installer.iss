; Copyright 2026 Msprg
; SPDX-License-Identifier: GPL-2.0-or-later
;
; Inno Setup script for FW16 Macropad Sysmon.
;
; Build with:  ISCC.exe /DAppVersion=1.0.0 installer.iss
; (build.ps1 does this, and passes the version read from fw16_sysmon.py)
;
; Design notes
; ------------
; * Capability checks happen twice, on purpose. Cheap registry-based checks run
;   *before* installing, on their own wizard page, so the user learns about a
;   blocked ring0 driver or a missing macropad while they can still back out.
;   The full self-test runs *after* installing, because it needs the installed
;   executable -- a PyInstaller onedir bundle cannot be run from {tmp}.
; * The legacy Task Scheduler job is removed rather than left alone. Two
;   processes writing the macropad's single raw-HID endpoint fight over it, and
;   the old task is how every existing user of this mod is currently running it.
; * The vendored DLLs go in {app}, next to the exe, NOT under _internal.
;   WinRing0 resolves its kernel driver relative to the process image.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName        "FW16 Macropad Sysmon"
#define AppPublisher   "Msprg"
#define AppURL         "https://github.com/Msprg/framework_qmk_firmware"
#define ExeName        "fw16-sysmon.exe"
#define ServiceName    "FW16Sysmon"
#define LegacyTaskName "FW16 Macropad Sysmon"
#define HostDir        ".."
#define DistDir        "..\dist\fw16-sysmon"

[Setup]
AppId={{7C4E0D2A-91B6-4E63-9E2E-6F0B5A4D1C88}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=..\dist
OutputBaseFilename=FW16-Sysmon-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The service is LocalSystem and the files live in Program Files. Leaving
; PrivilegesRequiredOverridesAllowed unset (its default) is what forbids the
; user from downgrading to a non-admin install -- there is no valid "none".
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; WmiMonitorBrightness, the GPU Engine counters and winget all assume a
; reasonably current Windows 10.
MinVersion=10.0.19041
LicenseFile={#HostDir}\packaging\LICENSE.txt
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#AppName}
DisableProgramGroupPage=yes
ShowLanguageDialog=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startservice";  Description: "Start the service now (it starts automatically at boot from then on)"; GroupDescription: "After installing:"
Name: "flashfirmware"; Description: "Flash the bundled firmware onto the macropad now"; GroupDescription: "After installing:"; Flags: unchecked
Name: "frameworktool"; Description: "Install framework_tool via winget (battery charge-cap scaling and a temperature fallback)"; GroupDescription: "Optional dependencies:"; Check: CanInstallFrameworkTool

[Files]
; One entry, on purpose. build.ps1 assembles dist\fw16-sysmon into exactly the
; layout the installed program expects -- the PyInstaller onedir output plus the
; vendored native libraries beside the exe, lib\ for the .NET assemblies, the
; firmware image, licences and readme -- so this just copies the tree.
;
; The alternative (Inno placing the DLLs itself) means the built bundle cannot
; be run or self-tested before packaging, because it is missing half its
; payload until Setup runs. Keeping one owner for placement avoids that.
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; Machine-scope config and log. Left behind on uninstall unless the user says
; otherwise, so tuning survives a reinstall.
Name: "{commonappdata}\FW16Sysmon"

[Icons]
; These point straight at the executable rather than wrapping it in `cmd /k`.
; The exe is manifested requireAdministrator, so launching it through cmd gets
; the elevated child its OWN new console and leaves the cmd window behind,
; empty, while the real output flashes past in the window that just closed.
; The program pauses itself instead (applog.pause_if_own_console), which is
; both correct under elevation and skipped automatically when the output is
; piped somewhere.
Name: "{group}\Self-test (what works on this machine)"; Filename: "{app}\{#ExeName}"; Parameters: "--selftest"; Comment: "Probe every sensor and dependency, and say how to fix anything missing"
Name: "{group}\Live view (console)";                    Filename: "{app}\{#ExeName}"; Parameters: "--verbose"; Comment: "Run in the foreground and print each tick"
Name: "{group}\Edit settings (sysmon.ini)";             Filename: "{win}\notepad.exe"; Parameters: """{commonappdata}\FW16Sysmon\sysmon.ini"""
Name: "{group}\View log";                               Filename: "{win}\notepad.exe"; Parameters: """{commonappdata}\FW16Sysmon\sysmon.log"""
Name: "{group}\Limit report";                           Filename: "{app}\{#ExeName}"; Parameters: "--limit-report"; Comment: "Which limits held the CPU and dGPU back, how long, and under what conditions"
Name: "{group}\Flash firmware onto the macropad";       Filename: "{app}\{#ExeName}"; Parameters: "--flash"
Name: "{group}\Restart the service";                    Filename: "{app}\{#ExeName}"; Parameters: "--restart-service"; Comment: "Apply changes made to sysmon.ini"
Name: "{group}\Third-party notices";                    Filename: "{app}\THIRD-PARTY-NOTICES.md"
Name: "{group}\Uninstall {#AppName}";                   Filename: "{uninstallexe}"

[Run]
; Order matters: config first (so the service reads it on its very first start),
; then registration, then the optional flash, then the start.
Filename: "{app}\{#ExeName}"; Parameters: "--write-default-config"; StatusMsg: "Writing the default configuration..."; Flags: runhidden waituntilterminated
; --install-service runs through cmd, like the winget step below: a hidden
; [Run] entry's exit code is thrown away, and its output with it. The marker
; file is written only when it succeeds; CurStepChanged checks for it.
Filename: "{cmd}"; Parameters: "/c """"{app}\{#ExeName}"" --install-service > ""{tmp}\install-service.log"" 2>&1 && echo ok> ""{tmp}\install-service.ok"""""; StatusMsg: "Registering the boot-start service...";  Flags: runhidden waituntilterminated

; Full resolved path, not a bare "winget.exe": the App Execution Alias is not
; on a path Inno can resolve, and with skipifdoesntexist that failed silently.
; Run through cmd with output captured, because a hidden [Run] discards
; winget's own diagnostics -- and when this step fails, that message is the
; only thing that explains why.
Filename: "{cmd}"; Parameters: "/c """"{code:WingetPath}"" install --id FrameworkComputer.framework_tool --silent --accept-source-agreements --accept-package-agreements > ""{tmp}\winget.log"" 2>&1"""; StatusMsg: "Installing framework_tool via winget..."; Flags: runhidden waituntilterminated; Tasks: frameworktool

; Flashing is interactive by nature (it waits for the module to re-enumerate),
; so it gets a visible console rather than running hidden, and pauses itself at
; the end so the result can be read.
Filename: "{app}\{#ExeName}"; Parameters: "--flash"; StatusMsg: "Flashing the macropad..."; Flags: waituntilterminated; Tasks: flashfirmware; Check: not WizardSilent
; A silent install has nobody to answer a question or close the window, so
; it runs hidden: launched hidden, the flasher asks nothing and doesn't wait
; for Enter (applog.can_prompt / pause_if_own_console).
Filename: "{app}\{#ExeName}"; Parameters: "--flash"; StatusMsg: "Flashing the macropad..."; Flags: runhidden waituntilterminated; Tasks: flashfirmware; Check: WizardSilent

Filename: "{app}\{#ExeName}"; Parameters: "--start-service"; StatusMsg: "Starting the service..."; Flags: runhidden waituntilterminated; Tasks: startservice

; Finished page. Two flags here are load-bearing:
;   shellexec - Finished-page entries are launched as the ORIGINAL user, not
;               with Setup's elevated token (unlike the [Run] entries above,
;               which is why those work). fw16-sysmon.exe is manifested
;               requireAdministrator, and plain CreateProcess cannot elevate:
;               it fails outright with "CreateProcess failed; code 740". Going
;               through ShellExecuteEx honours the manifest and prompts.
;   nowait    - the program holds its own window open until Enter is pressed.
Filename: "{app}\{#ExeName}"; Parameters: "--selftest"; Description: "Show what works on this machine (self-test)"; Flags: postinstall nowait skipifsilent shellexec

[UninstallRun]
Filename: "{app}\{#ExeName}"; Parameters: "--stop-service";      Flags: runhidden waituntilterminated; RunOnceId: "StopSysmonService"
Filename: "{app}\{#ExeName}"; Parameters: "--uninstall-service"; Flags: runhidden waituntilterminated; RunOnceId: "DelSysmonService"

[Code]
var
  CheckPage: TOutputMsgMemoWizardPage;
  LegacyTaskFound: Boolean;
  RemoveDataOnUninstall: Boolean;

function CmdOk(const Cmd, Params: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(Cmd, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
            and (ResultCode = 0);
end;

// The macropad shows up under the USB enumerator as VID_32AC&PID_0013.
// Checking the registry avoids needing any device API from Pascal.
// (Comments in [Code] use // throughout: Inno's { } comments do not nest, so a
//  brace comment containing an {app}-style constant ends at that constant's
//  closing brace and the rest becomes a syntax error.)
function MacropadPresent: Boolean;
begin
  Result := RegKeyExists(HKEY_LOCAL_MACHINE,
    'SYSTEM\CurrentControlSet\Enum\USB\VID_32AC&PID_0013');
end;

function NumpadPresent: Boolean;
begin
  Result := RegKeyExists(HKEY_LOCAL_MACHINE,
    'SYSTEM\CurrentControlSet\Enum\USB\VID_32AC&PID_0018');
end;

function MemoryIntegrityOn: Boolean;
var
  Value: Cardinal;
begin
  Result := False;
  if RegQueryDWordValue(HKEY_LOCAL_MACHINE,
      'SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity',
      'Enabled', Value) then
    Result := Value <> 0;
end;

function CpuName: String;
begin
  if not RegQueryStringValue(HKEY_LOCAL_MACHINE,
      'HARDWARE\DESCRIPTION\System\CentralProcessor\0', 'ProcessorNameString', Result) then
    Result := 'unknown';
end;

function CpuIsAmd: Boolean;
begin
  Result := (Pos('AMD', Uppercase(CpuName)) > 0) or (Pos('RYZEN', Uppercase(CpuName)) > 0);
end;

// winget installs a portable package into MACHINE scope only when it is run
// elevated; from a normal terminal it lands in the user's own WinGet\Links.
// Checking only the machine paths reported "not installed" for a tool that was
// installed and working, which then fired a bogus "could not be installed"
// dialog. Search PATH first -- that is what actually decides whether the host
// can run it -- then the known install roots.
// Mirrors fw16_sysmon.find_framework_tool(): PATH, then Program Files, then
// EVERY user profile's per-user winget install. Checking only {localappdata} is
// not enough -- for the LocalSystem service that resolves to the systemprofile,
// which has no WinGet directory at all, so a per-user install is invisible to
// the service even though it is on the installing user's PATH.
function FrameworkToolPresent: Boolean;
var
  Found: TFindRec;
  UsersDir, Candidate: String;
begin
  Result := FileSearch('framework_tool.exe', GetEnv('PATH')) <> '';
  if Result then Exit;
  Result := FileExists(ExpandConstant('{commonpf}\WinGet\Links\framework_tool.exe'))
         or FileExists(ExpandConstant('{commonpf}\Framework Computer\framework_tool.exe'));
  if Result then Exit;

  UsersDir := ExpandConstant('{sd}\Users');
  if FindFirst(UsersDir + '\*', Found) then
  begin
    try
      repeat
        if (Found.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0)
           and (Found.Name <> '.') and (Found.Name <> '..') then
        begin
          Candidate := UsersDir + '\' + Found.Name +
                       '\AppData\Local\Microsoft\WinGet\Links\framework_tool.exe';
          if FileExists(Candidate) then
          begin
            Result := True;
            Exit;
          end;
        end;
      until not FindNext(Found);
    finally
      FindClose(Found);
    end;
  end;
end;

function NeedsFrameworkTool: Boolean;
begin
  Result := not FrameworkToolPresent;
end;

// winget is normally an App Execution Alias under the user's WindowsApps
// directory -- NOT in Program Files and NOT registered under App Paths. Looking
// only in those two places reports "winget not found" on essentially every
// machine, and a bare "winget.exe" in [Run] cannot be resolved either, so the
// entry silently skipped and framework_tool was never installed.
// Returns a full path, or '' when winget genuinely is not available.
function FindWinget: String;
var
  Candidates: array[0..2] of String;
  I: Integer;
  FromAppPaths: String;
begin
  Result := '';
  Candidates[0] := ExpandConstant('{localappdata}\Microsoft\WindowsApps\winget.exe');
  Candidates[1] := ExpandConstant('{commonpf}\WinGet\Links\winget.exe');
  if RegQueryStringValue(HKEY_LOCAL_MACHINE,
      'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\winget.exe', '',
      FromAppPaths) then
    Candidates[2] := FromAppPaths
  else
    Candidates[2] := '';
  for I := 0 to 2 do
    if (Candidates[I] <> '') and FileExists(Candidates[I]) then
    begin
      Result := Candidates[I];
      Exit;
    end;
end;

// {code:WingetPath} in [Run] requires this exact signature (Inno passes the
// text after a '|', empty here).
function WingetPath(Param: String): String;
begin
  Result := FindWinget;
end;

function WingetPresent: Boolean;
begin
  Result := FindWinget <> '';
end;

// Offer the framework_tool task only when it is both needed and possible.
function CanInstallFrameworkTool: Boolean;
begin
  Result := NeedsFrameworkTool and WingetPresent;
end;

function ServiceInstalled: Boolean;
begin
  Result := RegKeyExists(HKEY_LOCAL_MACHINE,
    'SYSTEM\CurrentControlSet\Services\{#ServiceName}');
end;

// The pre-install report. Everything here is a cheap registry or file lookup;
// the expensive probing waits until the executable exists.
function BuildReport: String;
var
  S: String;
begin
  S := 'Checks run before anything is installed.' + #13#10 + #13#10;

  if MacropadPresent then
    S := S + '[ ok ]  RGB macropad module detected.' + #13#10
  else if NumpadPresent then
    S := S + '[warn]  A Framework input module is attached, but it is the numpad,' + #13#10 +
             '        not the RGB macropad. sysmon drives the macropad only.' + #13#10
  else
    S := S + '[warn]  No Framework macropad detected right now.' + #13#10 +
             '        That is fine -- the module is hot-swappable and the service' + #13#10 +
             '        picks it up whenever it appears. Installation can continue.' + #13#10;

  S := S + #13#10;

  if CpuIsAmd then
    S := S + '[ ok ]  AMD CPU (' + CpuName + ').' + #13#10 +
             '        Power-limit detection uses the SMU table via libryzenadj.' + #13#10
  else
    S := S + '[info]  Non-AMD CPU (' + CpuName + ').' + #13#10 +
             '        The AMD SMU power-limit path does not apply; sysmon falls' + #13#10 +
             '        back to LibreHardwareMonitor for CPU power and temperature.' + #13#10;

  S := S + #13#10;

  if MemoryIntegrityOn then
    S := S + '[warn]  Memory Integrity (Core Isolation) is ON.' + #13#10 +
             '        Windows will block the ring0 driver these sensors need, so' + #13#10 +
             '        the CPU-row power-limit breathe and the in-process' + #13#10 +
             '        temperature will not work. Temperature still comes from the' + #13#10 +
             '        EC via framework_tool. Everything else is unaffected.' + #13#10
  else
    S := S + '[ ok ]  Memory Integrity is off; the ring0 sensor driver can load.' + #13#10;

  S := S + #13#10;

  if FrameworkToolPresent then
    S := S + '[ ok ]  framework_tool found (battery charge cap + EC temperature).' + #13#10
  else if WingetPresent then
    S := S + '[info]  framework_tool is not installed. It is optional; it adds' + #13#10 +
             '        battery charge-cap scaling and a temperature fallback.' + #13#10 +
             '        The next page offers to install it with winget.' + #13#10
  else
    S := S + '[info]  framework_tool is not installed and winget was not found.' + #13#10 +
             '        Optional -- install it later if you want the battery bar' + #13#10 +
             '        scaled to the EC charge cap.' + #13#10;

  if LegacyTaskFound then
    S := S + #13#10 +
             '[warn]  An existing "{#LegacyTaskName}" scheduled task was found.' + #13#10 +
             '        It will be removed: two copies writing the macropad''s' + #13#10 +
             '        single raw-HID endpoint fight over it. The new service' + #13#10 +
             '        replaces it and starts earlier (at boot, not at sign-in).' + #13#10;

  if ServiceInstalled then
    S := S + #13#10 +
             '[info]  A previous version of the service is installed; it will be' + #13#10 +
             '        stopped and re-registered.' + #13#10;

  Result := S;
end;

procedure InitializeWizard;
begin
  LegacyTaskFound := CmdOk(ExpandConstant('{sys}\schtasks.exe'),
                           '/Query /TN "{#LegacyTaskName}"');
  CheckPage := CreateOutputMsgMemoPage(wpLicense,
    'System check', 'What sysmon will be able to do on this machine',
    'Everything below except the macropad itself is optional -- sysmon degrades ' +
    'one row at a time rather than refusing to run. A full self-test is ' +
    'available after installation.',
    BuildReport);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  // Stop the old task before the files move, so nothing is holding the device
  // or an old executable. Failures are ignored: a missing task is the normal
  // case for a first-time install.
  if LegacyTaskFound then
  begin
    CmdOk(ExpandConstant('{sys}\schtasks.exe'), '/End /TN "{#LegacyTaskName}"');
    CmdOk(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN "{#LegacyTaskName}" /F');
  end;
  // An upgrade over a running service cannot replace the exe in place.
  if ServiceInstalled then
    CmdOk(ExpandConstant('{sys}\net.exe'), 'stop {#ServiceName}');
end;

// The last few lines of a step's captured output in {tmp}, for its failure
// dialog (winget, --install-service).
function LogTail(const Name: String): String;
var
  Lines: TArrayOfString;
  I, First: Integer;
begin
  Result := '';
  if not LoadStringsFromFile(ExpandConstant('{tmp}\') + Name, Lines) then
    Exit;
  First := GetArrayLength(Lines) - 8;
  if First < 0 then First := 0;
  for I := First to GetArrayLength(Lines) - 1 do
    if Trim(Lines[I]) <> '' then
      Result := Result + Trim(Lines[I]) + #13#10;
end;

// The service as the SCM holds it: registered, and not just marked for
// deletion (such a service keeps its key, with DeleteFlag = 1, until the last
// handle to it closes).
function ServiceRegistered: Boolean;
var
  DeleteFlag: Cardinal;
begin
  Result := ServiceInstalled;
  if Result and RegQueryDWordValue(HKEY_LOCAL_MACHINE,
      'SYSTEM\CurrentControlSet\Services\{#ServiceName}', 'DeleteFlag',
      DeleteFlag) then
    Result := DeleteFlag = 0;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Tail: String;
begin
  // Check the --install-service step: by its own success marker, since an
  // upgrade whose reconfigure failed still leaves the old, valid service in
  // the registry, and by the registry too. Without a current service the pad
  // stays dark, or runs the old version, with nothing to say why.
  if (CurStep = ssPostInstall) and
     (not FileExists(ExpandConstant('{tmp}\install-service.ok')) or
      not ServiceRegistered) then
  begin
    Tail := LogTail('install-service.log');
    if Tail <> '' then
      Tail := #13#10 + 'It said:' + #13#10 + #13#10 + Tail;
    SuppressibleMsgBox(
      'The {#ServiceName} service could not be registered or updated, so' + #13#10 +
      'the macropad may not show the bars (or runs the old version).' + #13#10 +
      Tail + #13#10 +
      'If the Services console or Task Manager is open, close it (or' + #13#10 +
      'restart Windows), then run this setup again.',
      mbError, MB_OK, IDOK);
  end;

  // Verify the winget step actually did something. It runs hidden and its exit
  // code is not surfaced, so a failure (winget declining to run under Setup's
  // elevated token, no network, the package id having moved) would otherwise be
  // invisible until the user noticed a wrong temperature row much later.
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('frameworktool') then
  begin
    if not FrameworkToolPresent then
    begin
      Tail := LogTail('winget.log');
      if Tail <> '' then
        Tail := #13#10 + 'winget said:' + #13#10 + #13#10 + Tail;
      MsgBox('framework_tool could not be installed automatically.' + #13#10 +
             Tail + #13#10 +
             'It is optional -- it adds battery charge-cap scaling and a' + #13#10 +
             'temperature fallback. To install it yourself, open a normal' + #13#10 +
             '(non-elevated) terminal and run:' + #13#10 + #13#10 +
             '    winget install FrameworkComputer.framework_tool' + #13#10 + #13#10 +
             'then restart the service from the Start menu.',
             mbInformation, MB_OK);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{commonappdata}\FW16Sysmon');
    if DirExists(DataDir) then
    begin
      RemoveDataOnUninstall := MsgBox(
        'Remove your settings and log as well?' + #13#10 + #13#10 +
        DataDir + #13#10 + #13#10 +
        'Choose No to keep sysmon.ini, so your tuning survives a reinstall.',
        mbConfirmation, MB_YESNO) = IDYES;
      if RemoveDataOnUninstall then
        DelTree(DataDir, True, True, True);
    end;
    // The ring0 driver may have been copied beside the exe at runtime by an
    // older source-layout run, in which case Setup does not own the file.
    DeleteFile(ExpandConstant('{app}\WinRing0x64.sys'));
  end;
end;
