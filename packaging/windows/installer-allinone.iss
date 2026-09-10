; installer-allinone.iss — All-in-One EXE, делает всё за юзера из коробки
; Собирается на Linux: docker run --rm -v $PWD:/work amake/innosetup packaging/windows/installer-allinone.iss
; Не требует предсборки PyInstaller — пакует исходники и ставит pip deps на машине юзера

#define MyAppName "Fragile Notes"
#define MyAppVersion "0.1.1"
#define MyAppPublisher "fragilich"
#define MyAppURL "https://github.com/freakkxd/Fragile-Notes"
#define MyAppExeName "fragile-notes.exe"

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
OutputBaseFilename=FragileNotes-AllInOne-Setup-v{#MyAppVersion}
;SetupIconFile=..\fragile-notes.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Исходники — всё что нужно для pip install -e .
Source: "..\..\fragilenotes\*"; DestDir: "{app}\fragilenotes"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\main.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\run.sh"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\scripts\bootstrap.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\..\scripts\bootstrap.sh"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\..\packaging\fragile-notes.svg"; DestDir: "{app}\packaging"; Flags: ignoreversion
Source: "..\..\packaging\dev.fragilich.fragile-notes.desktop"; DestDir: "{app}\packaging"; Flags: ignoreversion
; Скрипт установки
Source: "install-helper.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\packaging\fragile-notes.svg"; Comment: "Нативный аналог Obsidian"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 1. Ставим зависимости из коробки (Python должен быть, если нет — покажем ссылку)
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\install-helper.ps1"" -Install"; StatusMsg: "Устанавливаю зависимости (pip install)..."; Flags: runhidden waituntilterminated
; 2. Запуск после установки
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\fragilenotes\__pycache__"

[Code]
function InitializeSetup(): Boolean;
begin
  // Проверку Python и установку делаем в install-helper.ps1 после копирования файлов
  Result := True;
end;
