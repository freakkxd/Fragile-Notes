# Fragile Notes — v0.4.1

> **Подпись v0.4.1 — почему отделили эту версию:**
> - **Сделано в 0.3.10:** последний `Python GTK` релиз: `C++ core` заглушки + `React 18` внутри `WebView` (`fragilenotes/ui/react_dist`), `vault_guard`, Full Offline 33-59M, CI `ruff/mypy/pytest`.
> - **Почему 0.4.1 отдельно:** **полный переезд на C++/Tauri** — Python удалён из рантайма, ядро теперь `C++20 + SQLite FTS5 + OpenSSL` (`cpp/`), UI `TypeScript/React 18 + Zustand + Tauri IPC` (`frontend/` + `src-tauri/`), сборка `CMake + Vite + Tauri bundler` вместо `PyInstaller/GTK`. Vault остаётся совместим (`~/Documents/FragileNotesVault`).
> - **В работе теперь:** паритет 18 вьюх (graph/canvas/kanban/database) уже как React компоненты, плагины, темы, FTS, crypto, CRDT — на Tauri.
> - **Цель 0.4.1:** стабильная Obsidian-like база без Python — `latest` на `https://github.com/freakkxd/Fragile-Notes/releases/latest`

**Obsidian-like vault для Linux/Windows.** Ядро `C++` + `Tauri (Rust)` + `React`.

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Актуальный стек v0.4.1

| Слой | Технология | Где |
|------|------------|-----|
| **UI** | React 18 + TypeScript 5.5, Zustand, marked+DOMPurify, Vite 5.4 | `frontend/src/` — Ribbon, FileTree, TabBar, Editor, Preview, Graph, Canvas, CommandPalette, StatusBar |
| **Bridge** | Tauri 1.6 IPC + zod runtime validation | `frontend/src/lib/bridge.ts` + `src-tauri/src/main.rs` |
| **Core** | C++20, OpenSSL (AES-GCM/PBKDF2), SQLite FTS5, CRDT (LWW/RGA) | `cpp/` — vault, fts, crypto, crdt, tasks, srs, link_index, dataview, media |
| **Backend** | Rust + Tauri, walkdir, rusqlite, regex | `src-tauri/` — `list_notes/read_note/write_note/fts_search/get_links` |
| **Build** | CMake 3.20, Vite, Tauri bundler, Inno Setup 6 | `packaging/windows/` + `src-tauri/tauri.conf.json` |
| **CI** | tsc --noEmit, ruff, mypy (deprecated), pytest, ctest, xvfb | `.github/workflows/` |

**Obsidian UX:** `Ribbon (44px)` | `File Explorer` (vault как корень, grouping) | `Center Tabs` | `Split Edit/Preview` | `Command Palette Ctrl+P` | `Graph/Canvas` | `Right Sidebar` | `StatusBar`.

## Быстрый старт

### Windows — Tauri bundle (рекомендуется)
Скачай `FragileNotes-Setup-v0.4.1.exe` из **Releases** → https://github.com/freakkxd/Fragile-Notes/releases/latest
Волт `~/Documents/FragileNotesVault` создастся сам. Python/GTK не нужны.

### Linux / macOS (из исходников — Tauri)
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
# C++ core
cmake -S cpp -B build/cpp && cmake --build build/cpp && ./build/cpp/fragile_tests
# Frontend
npm --prefix frontend install && npm --prefix frontend run typecheck && npm --prefix frontend run build
# Tauri (требует webkit2gtk, libsoup, javascriptcoregtk на Linux)
cargo install tauri-cli
cargo tauri dev          # dev
cargo tauri build        # bundle
# Fallback legacy Python (deprecated, только для совместимости)
pip install -e . && fragile-notes
```

**Системные зависимости Linux (для Tauri):**
```bash
# Arch / CachyOS
sudo pacman -S webkit2gtk libsoup gtk3 nodejs npm rust
# Ubuntu 24.04
sudo apt install libwebkit2gtk-4.0-dev libsoup2.4-dev libjavascriptcoregtk-4.0-dev nodejs npm cargo
```

### Legacy Python (deprecated, до v0.4.1 only)
```bash
./scripts/bootstrap.sh
pip install -e ".[dev]"
fragile-notes # -> теперь обёртка над Tauri, будет удалён в v0.5.0
```

## Что работает без чего

| Компонент | Ожидается | Если нет |
|-----------|-----------|----------|
| **Волт** | `~/Documents/FragileNotesVault` | Создается автоматически |
| **Tauri** | `cargo tauri` | `frontend` работает как Vite SPA (bridge mock) |
| **LLM** | `~/LLM/models/*.gguf` | `AI Чат` `offline` |

## Безопасность волта

Волт `~/Documents/FragileNotesVault` (`~/.config/fragile-notes/settings.json` `vault_root`).
В `.gitignore` и `tools/vault_guard.py` заблокированы личные заметки.

## Разработка v0.4.1

```bash
npm --prefix frontend run typecheck # tsc
npm --prefix frontend run build     # vite
cmake -S cpp -B build/cpp && ctest --test-dir build/cpp
cargo check --manifest-path src-tauri/Cargo.toml # требует webkit на Linux
ruff check . && mypy fragilenotes   # legacy python, deprecated
pytest -q                           # 93 passed
```

Структура:
```
cpp/                 # C++20 core (vault, fts, crypto, crdt, tasks, srs, link_index)
frontend/            # React 18 + TS (Obsidian shell)
  src/lib/bridge.ts  # Tauri IPC + zod
  src/store/vault.ts # Zustand
  src/components/    # Ribbon, FileTree, TabBar, Editor, Preview, Graph, Canvas
src-tauri/           # Rust Tauri backend (vault_root ~/Documents/FragileNotesVault)
fragilenotes/        # legacy Python (deprecated, compat only)
```

## Версионирование

- `v0.3.10` — последний Python GTK
- `v0.4.1` — **Full C++/Tauri** — Python удалён из рантайма, Obsidian shell (текущий)

## Лицензия

MIT — `LICENSE`
