; installer.iss — Inno Setup 6 для Fragile Notes v0.3.9
; Собирается: iscc packaging/windows/installer.iss
; Требует: dist/windows/FragileNotes/FragileNotes.exe (после build.ps1)

#define MyAppName "Fragile Notes"
#define MyAppVersion "0.3.9"
#define MyAppPublisher "fragilich"
#define MyAppURL "https://github.com/freakkxd/Fragile-Notes"
#define MyAppExeName "FragileNotes.exe"

[Setup]
AppId={{3B9C8F7A-2A1D-4E6B-9C3F-8A2D1E5F7C9B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\Fragile Notes
DefaultGroupName=Fragile Notes
AllowNoIcons=yes
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=FragileNotes-Setup-v{#MyAppVersion}
;SetupIconFile=..\fragile-notes.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "vaultchooser"; Description: "Добавить 'Выбрать волт' в меню"; GroupDescription: "Дополнительно:"

[Files]
Source: "..\..\dist\windows\FragileNotes\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "installer_helper.exe"; DestDir: "{app}"; Flags: ignoreversion
; Vault не копируем — он создается в %USERPROFILE%\desktop

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Comment: "Нативный аналог Obsidian"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\installer_helper.exe"; Parameters: "/install"; StatusMsg: "Настраиваю vault и зависимости..."; Flags: runhidden waituntilterminated skipifdoesntexist
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent skipifdoesntexist

[UninstallDelete]
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\build"

[Code]
function InitializeUninstall(): Boolean;
begin
  Result := True;
end;
