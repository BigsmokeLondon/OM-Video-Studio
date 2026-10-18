; Inno Setup script - builds OM-Video-Studio-Setup.exe (per-user install, no admin needed)
#ifndef AppVersion
  #define AppVersion "1.0"
#endif

[Setup]
AppId={{6B0F6C3E-2A57-4D0B-9C1E-0A4E7F3B1D52}
AppName=O.M. Video Studio
AppVersion={#AppVersion}
AppPublisher=M.A. Industries Inc
DefaultDirName={autopf}\O.M. Video Studio
DefaultGroupName=O.M. Video Studio
UninstallDisplayIcon={app}\OM-Video-Studio.exe
SetupIconFile=..\app.ico
OutputDir=..\Output
OutputBaseFilename=OM-Video-Studio-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\OM-Video-Studio\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs

[Icons]
Name: "{group}\O.M. Video Studio"; Filename: "{app}\OM-Video-Studio.exe"
Name: "{autodesktop}\O.M. Video Studio"; Filename: "{app}\OM-Video-Studio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\OM-Video-Studio.exe"; Description: "Launch O.M. Video Studio"; Flags: nowait postinstall skipifsilent

[InstallDelete]
; clean up leftovers from versions 1.0.x (named "O.M. FFmpeg Studio")
Type: files; Name: "{app}\OM-FFmpeg-Studio.exe"
Type: files; Name: "{group}\O.M. FFmpeg Studio.lnk"
Type: files; Name: "{autodesktop}\O.M. FFmpeg Studio.lnk"
Type: files; Name: "{autoprograms}\O.M. FFmpeg Studio.lnk"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\O.M. Video Studio"
