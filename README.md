# Fragile Notes — v0.3.4

> **Подпись v0.2.0 — почему отделили эту версию:**
> - **Сделано в 0.1.0-0.1.21:** успешный **порт на ПК** — `Full Offline` `33-59M` `FragileNotes-Setup-v0.1.20.exe` (один файл, внутри `Python 3.11`+`GTK4`+`PyGObject`+`libstdc++`/`GdkPixbuf`/`Gsk`), фикс 6 топ-багов (окно `Adw.HeaderBar` с кнопками, волт `~/desktop`→`~/Documents/FragileNotesVault`, боковая панель `44↔232` + `hover`, `Daily/Заметки/Шаблоны` + `AI Чат`/`Магазин`/`Презентация` без обрезки), `CI` зеленый (`ruff`/`mypy`/`pytest` `xvfb`), `vault_guard` защита
> - **Почему 0.2.0 отдельно:** первая **стабильная** после 3 дней порта — `ValueError: Namespace Gtk/Gsk/PangoCairo/GdkPixbuf` + `libstdc++-6.dll` закрыты, `Full` запускается по двойному клику (`PE32` 4.4M, не `bat`), `AllInOne` 3.4M депрекейтед и удален (`f447673`)
> - **В работе теперь:** фокус на багах, которые были до порта и подготовка **порта на C++** (`Qt`/`gtkmm`) — вручную, без спешки, `0.2.x` без breaking changes
> - **Цель 0.2:** стабильная база для `C++` порта, `Full Offline` 59M как `latest` на `https://github.com/freakkxd/Fragile-Notes/releases/latest`

**Нативный аналог Obsidian для Linux/Windows.** Ядро = **AO Runner** (Node) + **Fragilich Suite** (Python) — всё в одном процессе, без Electron.

> Работает **из коробки** — без волта, AO Engine и LLM тоже запускается. Волт (`~/desktop`) никогда не коммитится.

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)

## Актуальный стек

| Слой | Технология | Где |
|------|------------|-----|
| **UI** | GTK4 4.14 + libadwaita 1.5, PyGObject 3.46, Adw.Application | `fragilenotes/ui/` 18 вьюх |
| **Core** | Python 3.11, PyYAML, cryptography, SQLite FTS5 | `fragilenotes/core/`, `services/` |
| **AO Engine** | Node 20 + TypeScript, CLI `ao-engine/dist/cli/cli.js` | `~/desktop/_System/ArchiveOrganism` (опционально) |
| **LLM** | llama.cpp `llama-server` + Qwen3-14B / Gemma 26B | `~/LLM/models/*.gguf` (опционально) |
| **Build** | PyInstaller 6, Inno Setup 6, MSYS2 UCRT64 | `packaging/windows/` |
| **CI** | ruff 0.9.6, mypy 1.13, pytest 9, xvfb | `.github/workflows/` |

**18 вьюх:** `editor` `files` `board` `whiteboard` `canvas` `graph` `kanban` `database` `slides` `mindmap` `mermaid_live` `latex_live` `media` `video` `voice` `srs` `tasks` `habits` `pomodoro` `calendar` `templates` `analytics` `plugin_store` `theme_editor` `ai_chat` + `quick_capture` `quick_switcher`.

**Core:** `runner` `vault` `crdt` `srs` `tasks` `snippets` `templates` `crypto` `e2e` `fast_scan` `workspaces` + `services: engine, fts, embeddings, llm, ocr, p2p, git_sync, tray`.

## Быстрый старт

### Windows — один файл (рекомендуется)

Скачай **FragileNotes-Setup-v0.2.0.exe (33M)** из **Releases** → https://github.com/freakkxd/Fragile-Notes/releases/latest

Двойной клик → Далее → Установить → Запустить. **Внутри уже** `Python 3.11` + `GTK4` + `PyGObject` + `PyYAML` + `cryptography` — интернет и `MSYS2` не нужны. Волт `~/desktop` (`%USERPROFILE%\desktop`) создастся сам.

> `AllInOne 3.4M` (исходники + `pip install` на машине) — **депрекейтед**, оставлен в git истории (`5beb7bb`), больше не собирается.

### Linux / macOS (из исходников)

```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
./scripts/bootstrap.sh          # проверка: Python, GTK, Node, vault
pip install -e .                # или pip install -e ".[dev]" для разработки
fragile-notes                   # или ./run.sh / python main.py
# Первый запуск создаст ~/desktop (01 Home, 02 Daily, _System)
```

**Windows для разработчиков:**
```powershell
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
.\scripts\bootstrap.ps1
pip install -e .
fragile-notes
```

Сборка своего exe: `powershell -ExecutionPolicy Bypass -File packaging/windows/build.ps1` (требует `MSYS2` + `Inno Setup 6`, см. `packaging/windows/README.md`).

## Системные зависимости (если ставишь из исходников)

**Arch / CachyOS:**
```bash
sudo pacman -S gtk4 libadwaita gobject-introspection python-gobject nodejs npm
```

**Ubuntu 24.04:**
```bash
sudo apt update && sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0 libcairo2-dev libgirepository1.0-dev pkg-config python3-dev nodejs npm
```

**Fedora:**
```bash
sudo dnf install gtk4 libadwaita gobject-introspection python3-gobject nodejs npm
```

Проверка:
```bash
python -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Adw; print('OK')"
node --version  # опционально
./scripts/bootstrap.sh  # покажет ✓/⚠
```

## Что работает без чего

| Компонент | Ожидается | Если нет |
|-----------|-----------|----------|
| **Волт** | `~/desktop` | Создается автоматически (`01 Home/Home.md` и т.д.) |
| **AO Engine** | `~/desktop/_System/ArchiveOrganism/ao-engine/dist/cli/cli.js` | UI `offline`, кнопки `Enrich` неактивны, редактор/задачи работают (`engine.py:98` `except OSError → ok:False`) |
| **LLM** | `~/LLM/models/*.gguf` + `manage-llm.sh` | `LlmService` `online: False`, чат `LLM offline` (`llm.py:65`) |
| **GTK** | `PyGObject` + `libadwaita` | Full Offline exe уже внутри, AllInOne ставит `MSYS2` автоматом (`install-helper.ps1:38`) |

## Безопасность волта

Волт по умолчанию `~/desktop` (`~/.config/fragile-notes/settings.json` `vault_root`).

В `.gitignore:1` и `tools/vault_guard.py:1` (pre-commit) заблокированы:
`01 Home/`, `02 Daily/`, `_System/`, `Secret/`, `*.md` кроме `README.md`, `*.enc`, `settings.json`, `*.db`.

```bash
git ls-files | grep -E "01 Home|_System"  # пусто
pre-commit run --all  # vault_guard Passed
```

Волт живет отдельно `~/desktop/.git` — код и заметки разделены.

## Разработка

```bash
pip install -e ".[dev]"
ruff check .          # 0.9.6, All checks passed
mypy fragilenotes     # continue-on-error в CI, локально 1 warning в numpy
pytest -q             # 93 passed (xvfb-run на CI)
pre-commit install    # vault_guard + ruff + mypy
```

Структура:
```
fragilenotes/
  core/       # vault, runner, crdt, srs, tasks, crypto, e2e, publish
  services/   # engine, fts, embeddings, llm, git_sync, tray
  ui/         # 18 views + widgets, theme_manager
  data/       # plugins registry
packaging/
  windows/    # build.ps1, installer.iss (Full), FragileNotes.spec
scripts/
  bootstrap.sh / bootstrap.ps1
```

## Версионирование

- `v0.1.0` — initial core
- `v0.1.1` — out-of-box (`bootstrap.sh`, `Exec=fragile-notes`, `LICENSE`)
- `v0.1.2` — Windows AllInOne 3.4M (депрекейтед)
- `v0.1.6` — Full Offline 33M (PyInstaller + GTK, один exe)
- `v0.1.7` — Windows - only Full
- `v0.1.8` — Full Offline fix `collect-all gi`
- `v0.2.0` — Full Offline 33M самодостаточный, один exe (текущий)

## Лицензия

MIT — `LICENSE:1`

## Скриншоты

> TODO: добавить `screenshots/` (каталог B2B/EdTech не относится, это `Test-Works` — веб демо)

## FAQ

**Q: `Namespace Gtk not available` на Windows?**  
A: Старая AllInOne 3.4M требовала `MSYS2` — скачай **Full 33M** из `Releases → v0.2.0` (внутри GTK). Или запусти `AllInOne` с интернетом — он сам скачает `MSYS2` (70M).

**Q: Где волт?**  
A: `~/desktop` — можно поменять в `Настройки → Волт` (`settings.json` `vault_root`).

**Q: Нужен ли Node/LLM?**  
A: Нет, опционально. Без них `Enrich`/`AI чат` покажет `offline`, остальное работает.
