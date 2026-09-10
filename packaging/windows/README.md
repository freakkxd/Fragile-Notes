# Windows Installer — Fragile Notes v0.1.8+

> **Актуально:** только **Full Offline 33M** `FragileNotes-Setup-v0.1.1.exe` (внутри `Python 3.11` + `GTK4` + `PyGObject` + все deps). `Portable ZIP` и `AllInOne 3.4M` (исходники + `pip` на машине) — **депрекейтед**, остались в истории `5beb7bb`, больше не собираются.

## Варианты (актуально)

| Файл | Что внутри | Требует | Где брать |
|------|------------|---------|-----------|
| `FragileNotes-Setup-v0.1.1.exe` **33M** | Python + GTK4/libadwaita + PyGObject + PyYAML + cryptography (PyInstaller `UCRT64` + `Inno Setup 6`) | Ничего, один exe | `Releases` → `v0.1.6+` `https://github.com/freakkxd/Fragile-Notes/releases/latest` |

## Сборка на Windows (для контрибьюторов)

```powershell
# 1. Зависимости (один раз) — MSYS2 UCRT64
# Скачай MSYS2 https://www.msys2.org/ → в UCRT64 shell:
#   pacman -S mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-libadwaita mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-python-pip mingw-w64-ucrt-x86_64-python-cryptography mingw-w64-ucrt-x86_64-python-yaml

# 2. Сборка Full Offline (единственный)
powershell -ExecutionPolicy Bypass -File packaging/windows/build.ps1
# → PyInstaller --collect-all gi + hidden-imports → dist/windows/FragileNotes/FragileNotes.exe
# → Inno Setup → dist/FragileNotes-Setup-v0.1.1.exe 33M

# 3. Проверка (без установки)
# dist/windows/FragileNotes/FragileNotes.exe --help
```

## Сборка на Linux (CI, через Docker)

```bash
# AllInOne 3.4M (депрекейтед) — собирался так:
docker run --rm -v $PWD:/work amake/innosetup packaging/windows/installer-allinone.iss
# Full Offline собирается только на windows-latest (MSYS2) — см. .github/workflows/build-windows-full.yml
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
