; Inno Setup script - builds OM-FFmpeg-Studio-Setup.exe (per-user install, no admin needed)
#ifndef AppVersion
  #define AppVersion "1.0"
#endif

[Setup]
AppId={{6B0F6C3E-2A57-4D0B-9C1E-0A4E7F3B1D52}
AppName=O.M. FFmpeg Studio
AppVersion={#AppVersion}
AppPublisher=M.A. Industries Inc
DefaultDirName={autopf}\O.M. FFmpeg Studio
DefaultGroupName=O.M. FFmpeg Studio
UninstallDisplayIcon={app}\OM-FFmpeg-Studio.exe
SetupIconFile=..\app.ico
OutputDir=..\Output
OutputBaseFilename=OM-FFmpeg-Studio-Setup
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
Source: "..\dist\OM-FFmpeg-Studio\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs

[Icons]
Name: "{group}\O.M. FFmpeg Studio"; Filename: "{app}\OM-FFmpeg-Studio.exe"
Name: "{autodesktop}\O.M. FFmpeg Studio"; Filename: "{app}\OM-FFmpeg-Studio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\OM-FFmpeg-Studio.exe"; Description: "Launch O.M. FFmpeg Studio"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\O.M. FFmpeg Studio"
