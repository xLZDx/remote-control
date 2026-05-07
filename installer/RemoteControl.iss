; Inno Setup script for RemoteControl
;
; Build prerequisites:
;   - Run build.ps1 first, which produces dist\RemoteControl\
;   - Inno Setup 6 installed (https://jrsoftware.org/isinfo.php)
;
; Compile this script with:
;   iscc installer\RemoteControl.iss
;
; Output: installer\Output\RemoteControlSetup.exe

#define MyAppName       "RemoteControl"
#define MyAppVersion    "0.1.0"
#define MyAppPublisher  "RemoteControl"
#define MyAppExeName    "RemoteControl.exe"
#define DefaultPort     "7777"

[Setup]
AppId={{6D8B3C9E-2D44-4E83-9F0A-3B1E7A4F9ABC}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=RemoteControlSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin           ; needed for firewall rule
PrivilegesRequiredOverridesAllowed=commandline

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "autostart"; Description: "Start RemoteControl with Windows (host mode)"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\RemoteControl\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Share this PC";    Filename: "{app}\{#MyAppExeName}"; Parameters: "host"
Name: "{group}\Connect to a PC";  Filename: "{app}\{#MyAppExeName}"; Parameters: "client"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Optional autostart in host mode
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "RemoteControl"; ValueData: """{app}\{#MyAppExeName}"" host"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
; Add Windows Firewall inbound rule for the host port
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""RemoteControl Host"" dir=in action=allow protocol=TCP localport={#DefaultPort} program=""{app}\{#MyAppExeName}"" enable=yes"; Flags: runhidden

; Offer to launch after install
Filename: "{app}\{#MyAppExeName}"; Description: "Launch RemoteControl"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""RemoteControl Host"""; Flags: runhidden
