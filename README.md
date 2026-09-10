# Fragile Notes — v0.1

Нативный аналог Obsidian: ядро = **AO Runner** + **Fragilich Suite**.

> **Важно:** волт (`~/desktop`) никогда не коммитится в этот репозиторий. Код и личные заметки разделены.

## Что внутри v0.1
- **Core** (`fragilenotes/core/`): `runner`, `crdt`, `crypto`, `dataview`, `fast_scan`, `media`, `pdf_export`, `plugins`, `publish`, `snippets`, `srs`, `tasks`, `templates`, `workspaces`
- **Services** (`fragilenotes/services/`): `engine`, `fts`, `llm`, `embeddings`, `ocr`, `p2p`, `git_sync`, `canvas_collab` и др.
- **UI** (`fragilenotes/ui/`): 18 вьюх — editor, board, whiteboard, video, voice, graph, kanban, database и т.д. + `theme_manager`
- **Packaging**: `pyproject.toml` `0.1.0` `python>=3.11`, `packaging/dev.fragilich.fragile-notes.desktop`
- **Tests**: 12 suites, CI `ruff / mypy / pytest` (`.github/workflows/ci.yml`)

## Установка
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
pip install -e ".[dev]"
./run.sh  # или python main.py
```

## Безопасность волта
Волт по умолчанию `~/desktop` (`~/.config/fragile-notes/settings.json` `vault_root`).
В `.gitignore` и `tools/vault_guard.py` (pre-commit) заблокированы:
`01 Home/`, `02 Daily/`, `_System/`, `Secret/`, `*.md` кроме `README.md`, `*.enc`, `settings.json`.

Проверка:
```bash
git ls-files | grep -E "01 Home|_System"  # пусто
pre-commit run --all  # vault_guard pass
```

## Версионирование
- `v0.1.0` — первый публичный релиз, без личных данных.
- Личные заметки хранятся только локально в `~/desktop/.git` (отдельный приват если нужен).

## Лицензия
MIT (если нужна другая — укажи)
