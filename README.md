# Fragile Notes — v0.4.2

> **Подпись v0.4.2 — почему отделили эту версию:**
> - **Сделано в 0.4.1:** `C++20 core` + `Tauri 1.6` + `React 18 TS` уже собраны, но `Python` (`fragilenotes/`, `main.py`, `pyproject.toml`, `packaging/windows` PyInstaller) ещё лежал в репо как deprecated.
> - **Почему 0.4.2 отдельно:** **полный отказ от Python** — удалены `fragilenotes/`, `main.py`, `run.sh`, `scripts/bootstrap.*`, `pyproject.toml`, `tests/`, `tools/`, `packaging/` (PyInstaller/Inno), вычищен `CI` от `ruff/mypy/pytest`. Релиз собирается **только** `CMake + Vite + Tauri bundler`. Vault `~/Documents/FragileNotesVault` без изменений.
> - **Цель 0.4.2:** чистый `C++/Rust/TypeScript` без Python — как и задумывалось для `v0.4`.

**Obsidian-like vault для Linux/Windows.** Ядро `C++` + `Tauri (Rust)` + `React`.

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Актуальный стек v0.4.2

| Слой | Технология | Где |
|------|------------|-----|
| **UI** | React 18 + TypeScript 5.5, Zustand, marked+DOMPurify, Vite 5.4 | `frontend/src/` — Ribbon, FileTree, TabBar, Editor, Preview, Graph, Canvas, CommandPalette, StatusBar |
| **Bridge** | Tauri 1.6 IPC + zod runtime validation | `frontend/src/lib/bridge.ts` + `src-tauri/src/main.rs` |
| **Core** | C++20, OpenSSL (AES-GCM/PBKDF2), SQLite FTS5, CRDT (LWW/RGA) | `cpp/` — vault, fts, crypto, crdt, tasks, srs, link_index, dataview, media |
| **Backend** | Rust + Tauri, walkdir, rusqlite, regex | `src-tauri/` — `list_notes/read_note/write_note/fts_search/get_links` |
| **Build** | CMake 3.20, Vite, Tauri bundler | `src-tauri/tauri.conf.json` |
| **CI** | tsc --noEmit, vite build, ctest, cargo check/build | `.github/workflows/ci.yml` |

**Obsidian UX:** `Ribbon (44px)` | `File Explorer` (vault как корень) | `Center Tabs` | `Split Edit/Preview` | `Command Palette Ctrl+P` | `Graph/Canvas` | `Right Sidebar` | `StatusBar`.

## Быстрый старт

### Windows — Tauri bundle
Скачай `FragileNotes-Setup-v0.4.2.exe` из **Releases** → https://github.com/freakkxd/Fragile-Notes/releases/latest
Волт `~/Documents/FragileNotesVault` создастся сам. Python не нужен — его нет в системе.

### Linux / macOS (из исходников — Tauri)
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
# C++ core
cmake -S cpp -B build/cpp && cmake --build build/cpp && ./build/cpp/fragile_tests
# Frontend
npm --prefix frontend install && npm --prefix frontend run typecheck && npm --prefix frontend run build
# Tauri
cargo install tauri-cli
cargo tauri dev          # dev (http://localhost:5173)
cargo tauri build        # bundle -> src-tauri/target/release/bundle
```

**Системные зависимости Linux (для Tauri):**
```bash
# Arch / CachyOS
sudo pacman -S webkit2gtk libsoup gtk3 nodejs npm rust
# Ubuntu 24.04
sudo apt install libwebkit2gtk-4.0-dev libsoup2.4-dev libjavascriptcoregtk-4.0-dev nodejs npm cargo
```

## Безопасность волта
Волт `~/Documents/FragileNotesVault` (`~/.config/fragile-notes/settings.json` `vault_root`).
В `.gitignore` заблокированы личные заметки — `vault_guard` больше не нужен (Python удалён).

## Разработка v0.4.2
```bash
npm --prefix frontend run typecheck
npm --prefix frontend run build
cmake -S cpp -B build/cpp && ctest --test-dir build/cpp
cargo check --manifest-path src-tauri/Cargo.toml
```

Структура:
```
cpp/                 # C++20 core (vault, fts, crypto, crdt, tasks, srs, link_index)
frontend/            # React 18 + TS (Obsidian shell)
  src/lib/bridge.ts  # Tauri IPC + zod
  src/store/vault.ts # Zustand
  src/components/    # Ribbon, FileTree, TabBar, Editor, Preview, Graph, Canvas
src-tauri/           # Rust Tauri backend (vault_root ~/Documents/FragileNotesVault)
```

## Версионирование
- `v0.3.10` — последний Python GTK
- `v0.4.1` — C++/Tauri гибрид (Python deprecated)
- `v0.4.2` — **Full C++/Tauri** — Python полностью удалён (текущий)

## Лицензия
MIT — `LICENSE`
