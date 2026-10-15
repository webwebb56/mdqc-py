; MD QC Agent - Windows setup program (Inno Setup 6).
;
; Built by scripts/build_installer.py, which stages MDQC with its own Python
; in build\installer\app and passes AppVersion and SourceDir:
;
;   ISCC /DAppVersion=1.2.1 /DSourceDir=...\build\installer\app installer\mdqc.iss
;
; The result needs no Python, no PowerShell and no internet on the target PC,
; so it can be copied to a USB stick. By default it installs for the current
; user without administrator rights; "Install for all users" (admin) is
; offered on the first page. Silent install for IT: /VERYSILENT /CURRENTUSER
; or /VERYSILENT /ALLUSERS.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\installer\app"
#endif

#define AppName "MD QC Agent"
#define RunValue "MassDynamicsQC"
#define Aumid "MassDynamics.QCAgent"

[Setup]
; Never change AppId: upgrades and the uninstall entry are keyed on it.
AppId={{6F1B6C1E-3A57-4C4B-9E0C-5D2F8A7B1C40}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Mass Dynamics
AppPublisherURL=https://massdynamics.com
AppSupportURL=https://github.com/MassDynamics/md-qc
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\Mass Dynamics\MDQC
DefaultGroupName=Mass Dynamics QC
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=MDQC-Setup-{#AppVersion}
SetupIconFile={#SourceDir}\mdqc.ico
UninstallDisplayIcon={app}\mdqc.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The agent and tray run from {app}; the [Code] below stops them cleanly
; first, and Restart Manager closes anything left.
CloseApplications=force
RestartApplications=no
#ifdef Sign
SignTool=mdqcsign
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; Configuration, results and logs. Users must be able to write here even when
; an administrator installed for all users.
Name: "{commonappdata}\MassDynamics\QC"; Permissions: users-modify

[Icons]
Name: "{group}\MD QC Dashboard"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m mdqc open"; WorkingDir: "{app}"; IconFilename: "{app}\mdqc.ico"; Comment: "Open the MD QC dashboard"
; Windows shows the agent's notifications only if a Start-menu shortcut
; carries the app id it sends them under.
Name: "{group}\MD QC Agent"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m mdqc start"; WorkingDir: "{app}"; IconFilename: "{app}\mdqc.ico"; AppUserModelID: "{#Aumid}"; Comment: "Start the MD QC agent and tray icon"
Name: "{group}\MD QC Diagnostics"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m mdqc open /diagnostics"; WorkingDir: "{app}"; IconFilename: "{app}\mdqc.ico"; Comment: "Check the MD QC installation"
Name: "{group}\Uninstall MD QC Agent"; Filename: "{uninstallexe}"

[Registry]
; Start at login: for this user, or for every user when installed for all.
Root: HKA; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#RunValue}"; ValueData: """{app}\python\pythonw.exe"" -m mdqc start"; Flags: uninsdeletevalue
; The PowerShell installer registered the tray under this name; this
; replaces it.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "MassDynamicsQCTray"; Flags: deletevalue dontcreatekey

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-m mdqc start --open"; WorkingDir: "{app}"; Description: "Start MD QC Agent and open the dashboard"; Flags: postinstall nowait skipifsilent
; Silent installs (IT rollouts) start the agent without opening a browser.
Filename: "{app}\python\pythonw.exe"; Parameters: "-m mdqc start"; WorkingDir: "{app}"; Flags: nowait; Check: WizardSilent

[UninstallDelete]
; Python writes __pycache__ folders at run time, which the uninstaller
; didn't install and so wouldn't remove. Configuration and results live in
; ProgramData and are kept.
Type: filesandordirs; Name: "{app}\python"

[UninstallRun]
Filename: "{app}\python\python.exe"; Parameters: "-m mdqc stop"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "StopMDQC"

[Code]
function SkylineFound(): Boolean;
begin
  Result :=
    FileExists(ExpandConstant('{commonpf64}\Skyline\SkylineCmd.exe')) or
    FileExists(ExpandConstant('{commonpf64}\Skyline-daily\SkylineCmd.exe')) or
    FileExists(ExpandConstant('{commonpf32}\Skyline\SkylineCmd.exe'));
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not SkylineFound() and not WizardSilent() then
    Result := MsgBox(
      'Skyline was not found on this PC.' + #13#10#13#10 +
      'MD QC Agent uses Skyline to read raw files. If Skyline was installed ' +
      'from its website for this user only, this check can miss it. You can ' +
      'also install Skyline afterwards; nothing is processed until it is there.' + #13#10#13#10 +
      'Continue installing MD QC Agent?',
      mbConfirmation, MB_YESNO) = IDYES;
end;

// Stop a running agent (it finishes the file in hand) before files are replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  Py: String;
begin
  Result := '';
  Py := ExpandConstant('{app}\python\python.exe');
  if FileExists(Py) then
    Exec(Py, '-m mdqc stop', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
end;

// An empty configuration, so a fresh PC's agent can start and be set up from
// Settings. An existing one is never touched.
procedure CurStepChanged(CurStep: TSetupStep);
var
  Cfg: String;
begin
  if CurStep = ssPostInstall then
  begin
    // MDQC_DATA_DIR moves the data folder, as it does for the agent (tests).
    Cfg := GetEnv('MDQC_DATA_DIR');
    if Cfg = '' then
      Cfg := ExpandConstant('{commonappdata}\MassDynamics\QC');
    ForceDirectories(Cfg);
    Cfg := AddBackslash(Cfg) + 'config.toml';
    if not FileExists(Cfg) then
      SaveStringToFile(Cfg,
        '# MD QC Agent configuration, created by the setup program.' + #13#10 +
        '# Set up instruments and the cloud token in the dashboard''s Settings page,' + #13#10 +
        '# or by hand: see docs/INSTALL.md section 4b.' + #13#10,
        False);
  end;
end;
