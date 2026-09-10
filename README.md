# Fragile Notes — v0.1

Нативный аналог Obsidian: ядро = **AO Runner** + **Fragilich Suite**. Работает **из коробки** — без волта и LLM тоже запускается.

> **Важно:** волт (`~/desktop`) никогда не коммитится. Код и личные заметки разделены (`tools/vault_guard.py`).

## Что внутри v0.1
- **Core** (`fragilenotes/core/`): `runner`, `crdt`, `crypto`, `dataview`, `fast_scan`, `media`, `pdf_export`, `plugins`, `publish`, `snippets`, `srs`, `tasks`, `templates`, `workspaces`
- **Services** (`fragilenotes/services/`): `engine` (опционально), `fts`, `llm` (опционально), `embeddings`, `ocr`, `p2p`, `git_sync` и др. - **все падают грациозно в offline если нет Node/LLM**
- **UI** (`fragilenotes/ui/`): 18 вьюх — editor, board, whiteboard, video, voice, graph, kanban, database и т.д.
- **Packaging**: `pyproject.toml` `0.1.0` `python>=3.11`, `packaging/dev.fragilich.fragile-notes.desktop` (`Exec=fragile-notes`)
- **Tests**: 93 теста, CI `ruff / mypy / pytest`

## Быстрый старт (из коробки)

**Linux / macOS:**
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
./scripts/bootstrap.sh   # проверка зависимостей
pip install -e .         # или pip install -e ".[dev]" для разработки
fragile-notes            # или ./run.sh / python main.py
# Первый запуск создаст ~/desktop с 01 Home, 02 Daily и т.д. автоматически
```

**Windows (из коробки):**
```powershell
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
.\scripts\bootstrap.ps1          # проверка
pip install -e .
fragile-notes
# Или portable без установки Python:
# Скачай FragileNotes-Portable-v0.1.1.zip из Releases → распаковал → FragileNotes.exe
# Или инсталлер: FragileNotes-Setup-v0.1.1.exe
# Сборка инсталлера: packaging/windows/build.ps1 (требует MSYS2 GTK4 + Inno Setup 6) — см. packaging/windows/README.md
```

Открой `http://localhost:5173`? Нет, это отдельный проект - FragileNotes это GTK4 приложение, не веб.

## Системные зависимости

**Arch / CachyOS:**
```bash
sudo pacman -S gtk4 libadwaita gobject-introspection python-gobject nodejs npm
pip install -e .
```

**Ubuntu 22.04/24.04:**
```bash
sudo apt update && sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adwaita-1 libgirepository1.0-dev nodejs npm
pip install -e .
```

**Fedora:**
```bash
sudo dnf install gtk4 libadwaita gobject-introspection python3-gobject nodejs npm
pip install -e .
```

Проверка:
```bash
python -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Adw; print('OK')"
node --version  # опционально, для AO Engine
./scripts/bootstrap.sh  # покажет что OK, а что offline
```

## Опциональные компоненты (не требуются для запуска)

| Компонент | Где ожидается | Если нет |
|-----------|---------------|----------|
| **AO Engine** | `~/desktop/_System/ArchiveOrganism/ao-engine/dist/cli/cli.js` | UI покажет `offline`, `Enrich`/`Inbox` кнопки неактивны, но редактор/задачи работают |
| **LLM** | `~/LLM/models/*.gguf` + `manage-llm.sh` | `LlmService.ping()` → `online: False`, чат покажет `LLM offline` |
| **Волт** | `~/desktop` | Создастся автоматически при первом `load_settings()` |

Все сервисы (`fragilenotes/services/engine.py:98` `except OSError`, `llm.py:65` `except OSError`) возвращают `ok: False` вместо краша.

## Безопасность волта

Волт по умолчанию `~/desktop` (`~/.config/fragile-notes/settings.json` `vault_root`).
Заблокированы в `.gitignore:1` и `tools/vault_guard.py:1` (pre-commit):
`01 Home/`, `02 Daily/`, `_System/`, `Secret/`, `*.md` кроме `README.md`, `*.enc`, `settings.json`.

```bash
git ls-files | grep -E "01 Home|_System"  # пусто
pre-commit run --all  # vault_guard pass
```

## Разработка

```bash
pip install -e ".[dev]"
ruff check .; mypy fragilenotes; pytest -q
pre-commit install  # включает vault_guard
```

## Версионирование

- `v0.1.0` — первый публичный релиз, без личных данных.
- Личные заметки только локально в `~/desktop/.git` (отдельный приват если нужен).

## Лицензия

MIT — `LICENSE:1`
