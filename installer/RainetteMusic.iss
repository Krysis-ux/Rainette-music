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

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Rainette Music"; Filename: "{app}\RainetteMusic.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Rainette Music"; Filename: "{app}\RainetteMusic.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\RainetteMusic.exe"; Description: "Launch Rainette Music"; Flags: nowait postinstall skipifsilent
