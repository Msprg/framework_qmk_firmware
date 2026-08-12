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
Name: "frameworktool"; Description: "Install framework_tool via winget (battery charge-cap scaling and a temperature fallback)"; GroupDescription: "Optional dependencies:"; Check: NeedsFrameworkTool

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
Name: "{group}\Flash firmware onto the macropad";       Filename: "{app}\{#ExeName}"; Parameters: "--flash"
Name: "{group}\Restart the service";                    Filename: "{app}\{#ExeName}"; Parameters: "--restart-service"; Comment: "Apply changes made to sysmon.ini"
Name: "{group}\Third-party notices";                    Filename: "{app}\THIRD-PARTY-NOTICES.md"
Name: "{group}\Uninstall {#AppName}";                   Filename: "{uninstallexe}"

[Run]
; Order matters: config first (so the service reads it on its very first start),
; then registration, then the optional flash, then the start.
Filename: "{app}\{#ExeName}"; Parameters: "--write-default-config"; StatusMsg: "Writing the default configuration..."; Flags: runhidden waituntilterminated
Filename: "{app}\{#ExeName}"; Parameters: "--install-service";      StatusMsg: "Registering the boot-start service...";  Flags: runhidden waituntilterminated

Filename: "winget.exe"; Parameters: "install --id FrameworkComputer.framework_tool --silent --accept-source-agreements --accept-package-agreements"; StatusMsg: "Installing framework_tool via winget..."; Flags: runhidden waituntilterminated skipifdoesntexist; Tasks: frameworktool

; Flashing is interactive by nature (it waits for the module to re-enumerate),
; so it gets a visible console rather than running hidden, and pauses itself at
; the end so the result can be read.
Filename: "{app}\{#ExeName}"; Parameters: "--flash"; StatusMsg: "Flashing the macropad..."; Flags: waituntilterminated; Tasks: flashfirmware

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

function FrameworkToolPresent: Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf}\WinGet\Links\framework_tool.exe'))
         or FileExists(ExpandConstant('{commonpf}\Framework Computer\framework_tool.exe'));
end;

function NeedsFrameworkTool: Boolean;
begin
  Result := not FrameworkToolPresent;
end;

function WingetPresent: Boolean;
var
  Dummy: String;
begin
  Result := RegQueryStringValue(HKEY_LOCAL_MACHINE,
    'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\winget.exe', '', Dummy);
  if not Result then
    Result := FileExists(ExpandConstant('{commonpf}\WinGet\Links\winget.exe'));
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
