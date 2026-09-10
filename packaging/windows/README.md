# Windows Installer — Fragile Notes

## Варианты

| Файл | Что делает | Требует админа |
|------|------------|----------------|
| `FragileNotes-Portable-v0.1.1.zip` | Распаковал → `FragileNotes.exe` | Нет |
| `FragileNotes-Setup-v0.1.1.exe` | Инсталлер Inno Setup (Пуск, удаление) | Нет (per-user) |

## Сборка на Windows

```powershell
# 1. Зависимости (один раз)
# MSYS2 GTK4 + libadwaita (для PyGObject):
# Скачай MSYS2 https://www.msys2.org/ → в UCRT64 shell:
#   pacman -S mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-libadwaita mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-python-pip

# 2. Сборка portable + zip
powershell -ExecutionPolicy Bypass -File packaging/windows/build.ps1
# Только portable без инсталлера:
# packaging/windows/build.ps1 -PortableOnly

# 3. Сборка EXE инсталлера (требует Inno Setup 6 https://jrsoftware.org/isdl.php)
iscc packaging/windows/installer.iss
# → dist/FragileNotes-Setup-v0.1.1.exe
```

## Ручная установка без сборки (из коробки)

Если не хочешь собирать exe:

```powershell
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
pip install -e .
fragile-notes  # или python main.py — создаст %USERPROFILE%\desktop
```

## Что происходит при первом запуске на Windows

- Волт: `%USERPROFILE%\desktop` (создается `01 Home\Home.md` и т.д. если нет)
- Настройки: `%APPDATA%\fragile-notes\settings.json`
- AO Engine: `%USERPROFILE%\desktop\_System\ArchiveOrganism\ao-engine\dist\cli\cli.js` — если нет, UI покажет `offline` (редактор работает)
- LLM: `%USERPROFILE%\LLM\models\` — если нет, чат `offline`

Все опционально — ядро (редактор, задачи, файлы) работает сразу.

## Проверка

```powershell
.\scripts\bootstrap.sh  # или bootstrap.ps1 аналог — покажет ✓/⚠
fragile-notes --help
```

## Подпись и SmartScreen

Для обхода SmartScreen подпиши exe (опционально):
```powershell
signtool sign /fd SHA256 /a dist\FragileNotes-Setup-v0.1.1.exe
```
