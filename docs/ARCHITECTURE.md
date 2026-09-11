# ARCHITECTURE — подготовка к портированию (v0.2.1)

## Цель
Подготовить почву для быстрого переноса на другие языки (C++/Qt, Rust/Tauri, Go, TypeScript).
Принцип: `core/services` — чистый Python без GTK, `ui` — тонкий GTK-слой.

## Текущие слои
```
main.py -> fragilenotes/__init__.py (APP_ID, __version__)
  core/       # чистый python, stdlib + PyYAML/cryptography, БЕЗ gi
    runner, crdt, srs, tasks, vault, workspaces, snippets, templates, fast_scan
  services/   # I/O адаптеры, зависят от core, НЕ от ui
    engine, fts, embeddings, llm, git_sync, tray, web_clipper, vault_service
  ui/         # GTK4/libadwaita, зависит от core/services, НЕ наоборот
    app.py (FragileWindow), 26 views, widgets, theme_manager
  data/       # registry + plugins (изолированы через PluginManager)
```

## Границы (зафиксировано в 0.2.1)
- `core/` не импортирует `gi`, `ui`, `services` (проверяется `tools/portability_check.py`).
- `services/` не импортирует `ui`.
- `ui/` общается с core только через `VaultService`, `RunnerController`, `PluginManager`, `config`.
- Конфиг: `config.py` + `paths.py` + `settings.json` — единственный источник `vault_root`.

## Что сделано для портирования
1. Фикс фатального `Adw.HeaderBar` -> только на win32 (`ui/app.py:114`), Linux — нативный декор.
2. Версионирование единое: `pyproject.toml` + `fragilenotes/__init__.py` + `installer.iss` -> `0.2.1`.
3. Artefact: `dist/FragileNotes-Setup-v0.2.1.exe` (+ AllInOne alias).
4. Этот документ + `portability_check.py` для CI.

## Порт-стратегия по языкам
| Язык | UI | Что переиспользовать |
|------|----|----------------------|
| **C++ Qt6 / gtkmm4** | Qt Widgets / gtkmm | Порт `core/` 1:1, `services` через QtNetwork/libgit2, `crdt.py` -> header-only |
| **Rust Tauri** | WebView (React/Svelte) | `core` -> Rust crate, `services/fts` -> tantivy, `srs/crdt` -> wasm |
| **Go + Fyne/Wails** | Fyne | `core` -> Go structs, `vault` -> os/fs |
| **TypeScript Electron** | Electron | Прямой порт `core` -> TS, `services/engine` уже Node |

## Интерфейсы для портирования (протоколы)
Будущие порты реализуют те же протоколы (см. `core/interfaces.py` TODO):
- `VaultProtocol`: `ensure_file_tree()`, `read_note()`, `write_note()`, `search()`
- `RunnerProtocol`: `get_status()`, `run_task()`
- `SyncProtocol`: `FileSync`, `NetworkSync` (crdt)
- `PluginProtocol`: `load_plugins()`, `hook(event, ctx)`

## Чеклист перед портом
- [ ] `python tools/portability_check.py` — 0 нарушений
- [ ] `pytest -q` — 93 passed
- [ ] `core/` покрыт тестами без GTK (headless)
- [ ] `services` с моком `engine/llm` (offline ок)
- [ ] Версия бампнута синхронно (3 файла)

## Следующий шаг (0.2.2)
- Вынести `core/interfaces.py` с `typing.Protocol`
- Добавить `fragilenotes/core/api.py` — фасад для FFI (pyo3/cffi)
- CI: `portability_check` в `.github/workflows/ci.yml`
