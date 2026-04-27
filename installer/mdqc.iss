; MD QC Agent (Python port) Installer Script for Inno Setup 6
; https://jrsoftware.org/isinfo.php
;
; Wraps the PyInstaller `mdqc.exe` + bundled assets + nssm.exe into a single
; Windows installer. Registers `MassDynamicsQC` as a service via NSSM and
; arranges the per-user tray to autostart at login.
;
; Build with: python scripts/package.py [--version X.Y.Z]

#ifndef AppVersion
#define AppVersion "0.1.0"
#endif

[Setup]
AppName=Mass Dynamics QC Agent
AppId={{C0C0C0C0-0000-0000-0000-000000000000}
AppVersion={#AppVersion}
AppVerName=Mass Dynamics QC Agent {#AppVersion}
AppPublisher=Mass Dynamics
AppPublisherURL=https://massdynamics.com
DefaultDirName={pf}\MassDynamics\QC
DefaultGroupName=Mass Dynamics QC Agent
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=mdqc-setup-py-v{#AppVersion}
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin
MinVersion=10.0
WizardStyle=modern
UninstallDisplayIcon={app}\mdqc.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\mdqc.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "nssm.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\QC_Method.sky"; DestDir: "{commonappdata}\MassDynamics\QC\methods"; Flags: ignoreversion onlyifdoesntexist
Source: "..\assets\MD_QC_Report.skyr"; DestDir: "{commonappdata}\MassDynamics\QC\methods"; Flags: ignoreversion onlyifdoesntexist
Source: "..\assets\icon.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{commonappdata}\MassDynamics\QC"; Permissions: users-modify
Name: "{commonappdata}\MassDynamics\QC\logs"; Permissions: users-modify
Name: "{commonappdata}\MassDynamics\QC\spool\pending"; Permissions: users-modify
Name: "{commonappdata}\MassDynamics\QC\spool\uploading"; Permissions: users-modify
Name: "{commonappdata}\MassDynamics\QC\spool\completed"; Permissions: users-modify
Name: "{commonappdata}\MassDynamics\QC\spool\failed"; Permissions: users-modify
Name: "{commonappdata}\MassDynamics\QC\spool\work"; Permissions: users-modify
Name: "{commonappdata}\MassDynamics\QC\methods"; Permissions: users-modify
Name: "{commonappdata}\MassDynamics\QC\templates"; Permissions: users-modify
Name: "{commonappdata}\MassDynamics\QC\crashes"; Permissions: users-modify

[Run]
; Register the service via NSSM and configure recovery + log redirection.
Filename: "{app}\nssm.exe"; Parameters: "install MassDynamicsQC ""{app}\mdqc.exe"" run --service-mode"; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set MassDynamicsQC AppDirectory ""{app}"""; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set MassDynamicsQC DisplayName ""Mass Dynamics QC Agent (Python)"""; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set MassDynamicsQC Description ""MD QC Agent: automated quality control monitoring for mass spectrometry instruments"""; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set MassDynamicsQC Start SERVICE_DELAYED_AUTO_START"; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set MassDynamicsQC AppExit Default Restart"; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set MassDynamicsQC AppRestartDelay 5000"; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set MassDynamicsQC AppThrottle 30000"; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set MassDynamicsQC AppStdout ""{commonappdata}\MassDynamics\QC\logs\service-stdout.log"""; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "set MassDynamicsQC AppStderr ""{commonappdata}\MassDynamics\QC\logs\service-stderr.log"""; Flags: runhidden
Filename: "{app}\nssm.exe"; Parameters: "start MassDynamicsQC"; Flags: runhidden

[UninstallRun]
Filename: "{app}\nssm.exe"; Parameters: "stop MassDynamicsQC"; Flags: runhidden; RunOnceId: "StopMDQC"
Filename: "{app}\nssm.exe"; Parameters: "remove MassDynamicsQC confirm"; Flags: runhidden; RunOnceId: "RemoveMDQC"
Filename: "taskkill.exe"; Parameters: "/F /IM mdqc.exe"; Flags: runhidden; RunOnceId: "KillTray"

[Registry]
; Auto-start the per-user tray at login.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "MassDynamicsQCTray"; ValueData: """{app}\mdqc.exe"" tray"; Flags: uninsdeletevalue

[Icons]
; Start Menu shortcut required for AUMID toast registration. Without an AUMID
; on a real shortcut, winsdk toasts either appear unbranded or not at all
; (see docs/AGENT_NOTES § Notifications).
Name: "{group}\Mass Dynamics QC Agent"; Filename: "{app}\mdqc.exe"; Parameters: "tray"; AppUserModelID: "MassDynamics.QCAgent"
Name: "{group}\Mass Dynamics QC Agent Diagnostics"; Filename: "{app}\mdqc.exe"; Parameters: "doctor"; AppUserModelID: "MassDynamics.QCAgent"
