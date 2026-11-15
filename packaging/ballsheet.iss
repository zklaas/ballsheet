; Inno Setup script: the Windows installer for BallSheet.
; Built by .github/workflows/release.yml from PyInstaller's dist\BallSheet folder:
;   iscc /DAppVersion=1.0.0 packaging\ballsheet.iss
; Installs for the current user only (no administrator rights), adds a Start-menu
; entry and an uninstaller (Settings > Apps).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{B3CD6680-728D-5158-9216-EBDE9E7D567C}
AppName=BallSheet
AppVersion={#AppVersion}
AppPublisher=Zachary Klaas
AppPublisherURL=https://github.com/zklaas/ballsheet
DefaultDirName={localappdata}\Programs\BallSheet
DefaultGroupName=BallSheet
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=BallSheet-Setup-Windows
SetupIconFile=..\icons\ballsheet.ico
UninstallDisplayIcon={app}\BallSheet.exe
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\dist\BallSheet\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\BallSheet"; Filename: "{app}\BallSheet.exe"
Name: "{userdesktop}\BallSheet"; Filename: "{app}\BallSheet.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\BallSheet.exe"; Description: "Start BallSheet"; Flags: nowait postinstall skipifsilent
