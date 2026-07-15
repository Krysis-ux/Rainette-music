; Build with:
; iscc /DAppVersion=1.0.0 /DSourceDir="C:\path\to\release\stage\RainetteMusic" RainetteMusic.iss
#ifndef AppVersion
  #define AppVersion "0.1.0-local"
#endif
#ifndef SourceDir
  #error SourceDir must point to the PyInstaller --onedir output.
#endif

[Setup]
AppId={{B0A4DB26-D2D5-42C4-A71C-FF869B6CB429}
AppName=Rainette Music
AppVersion={#AppVersion}
AppPublisher=Rainette Music
DefaultDirName={autopf}\Rainette Music
DefaultGroupName=Rainette Music
DisableProgramGroupPage=yes
OutputDir=..\release\out
OutputBaseFilename=RainetteMusicSetup
SetupIconFile={#SourceDir}\_internal\web\assets\rainette-icon.ico
UninstallDisplayIcon={app}\_internal\web\assets\rainette-icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
; The in-app updater installs over a running copy, so close it first to release
; the file locks on RainetteMusic.exe and _internal\*.
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Rainette Music"; Filename: "{app}\RainetteMusic.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Rainette Music"; Filename: "{app}\RainetteMusic.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
; Pairing is a TLS listener reachable only from the local network.  Restrict
; the exception to this signed application, TCP, Private profiles, and the
; local subnet instead of opening a machine-wide port or Python interpreter.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Rainette Music Companion"" program=""{app}\RainetteMusic.exe"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Rainette Music Companion"" dir=in action=allow program=""{app}\RainetteMusic.exe"" enable=yes profile=private protocol=TCP remoteip=LocalSubnet"; Flags: runhidden waituntilterminated; StatusMsg: "Allowing secure phone pairing on private networks..."
Filename: "{app}\RainetteMusic.exe"; Description: "Launch Rainette Music"; Flags: nowait postinstall skipifsilent
; The in-app updater runs this silently with /autorelaunch=1 so the app it just
; replaced comes back on its own. Gated on that flag so an ordinary /SILENT
; install (e.g. a scripted deploy) does not unexpectedly launch the app.
Filename: "{app}\RainetteMusic.exe"; Flags: nowait; Check: WantsAutoRelaunch

[Code]
function WantsAutoRelaunch(): Boolean;
begin
  Result := ExpandConstant('{param:autorelaunch|0}') = '1';
end;

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Rainette Music Companion"" program=""{app}\RainetteMusic.exe"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveRainetteCompanionFirewallRule"
