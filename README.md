# Fragile Notes — v0.4.3

> **Подпись v0.4.3 — почему отделили эту версию:**
> - **Сделано в 0.4.2:** полный отказ от Python (70613 deletions), но два бага: 1) окно Tauri закрывалось сразу — `distDir: ../fragilenotes/ui/react_dist` не существовал + `icons/icon.png` 0 байт, 2) `CI tauri cargo check` фейлил — `libwebkit2gtk-4.0-dev` не существует на `ubuntu-latest` 24.04 (теперь 4.1/soups 3.0).
> - **Почему 0.4.3 отдельно:** фикс обоих багов без фич — `distDir → ../frontend/dist` (чистый `frontend/dist`), валидная 512px иконка, `main.rs` `WalkDir::filter_entry` + `setup` vault + `fs::read` 1k null-check, `CI` `webkit 4.1/soup3` fallback `4.0/soup2.4` и `frontend build` перед `cargo check`.
> - **Цель 0.4.3:** окно не вылетает, `CI` зелёный.

**Obsidian-like vault для Linux/Windows.** Ядро `C++` + `Tauri (Rust)` + `React`.

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Актуальный стек v0.4.3

| Слой | Технология | Где |
|------|------------|-----|
| **UI** | React 18 + TypeScript 5.5, Zustand, marked+DOMPurify, Vite 5.4 | `frontend/src/` — Ribbon, FileTree, TabBar, Editor, Preview, Graph, Canvas, CommandPalette, StatusBar |
| **Bridge** | Tauri 1.6 IPC + zod runtime validation | `frontend/src/lib/bridge.ts` + `src-tauri/src/main.rs` |
| **Core** | C++20, OpenSSL (AES-GCM/PBKDF2), SQLite FTS5, CRDT (LWW/RGA) | `cpp/` — vault, fts, crypto, crdt, tasks, srs, link_index, dataview, media |
| **Backend** | Rust + Tauri, walkdir, rusqlite, regex | `src-tauri/` — `list_notes/read_note/write_note/fts_search/get_links` |
| **Build** | CMake 3.20, Vite, Tauri bundler | `frontend/dist` + `src-tauri/tauri.conf.json` |
| **CI** | tsc --noEmit, vite build, ctest, cargo check/build (Ubuntu 24.04/22.04 compat) | `.github/workflows/ci.yml` |

**Obsidian UX:** `Ribbon (44px)` | `File Explorer` (vault как корень) | `Center Tabs` | `Split Edit/Preview` | `Command Palette Ctrl+P` | `Graph/Canvas` | `Right Sidebar` | `StatusBar`.

## Быстрый старт

### Windows — Tauri bundle
Скачай `FragileNotes-Setup-v0.4.3.exe` из **Releases** → https://github.com/freakkxd/Fragile-Notes/releases/latest
Волт `~/Documents/FragileNotesVault` создастся сам.

### Linux / macOS (из исходников — Tauri)
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
cmake -S cpp -B build/cpp && cmake --build build/cpp && ./build/cpp/fragile_tests
npm --prefix frontend install && npm --prefix frontend run build
cargo install tauri-cli
cargo tauri dev          # dev (http://localhost:5173)
cargo tauri build        # bundle -> src-tauri/target/release/bundle
```

**Системные зависимости Linux (для Tauri):**
```bash
# Arch / CachyOS
sudo pacman -S webkit2gtk libsoup gtk3 nodejs npm rust
# Ubuntu 24.04 (webkit 4.1)
sudo apt install libwebkit2gtk-4.1-dev libjavascriptcoregtk-4.1-dev libsoup-3.0-dev libgtk-3-dev librsvg2-dev patchelf nodejs npm cargo -y
# Ubuntu 22.04 (webkit 4.0 fallback — CI делает автоматом)
sudo apt install libwebkit2gtk-4.0-dev libjavascriptcoregtk-4.0-dev libsoup2.4-dev
```

## Безопасность волта
Волт `~/Documents/FragileNotesVault` (`~/.config/fragile-notes/settings.json` `vault_root`).

## Разработка v0.4.3
```bash
npm --prefix frontend run typecheck
npm --prefix frontend run build # -> frontend/dist
cmake -S cpp -B build/cpp && ctest --test-dir build/cpp
cargo check --manifest-path src-tauri/Cargo.toml
```

Структура:
```
cpp/                 # C++20 core
frontend/            # React 18 + TS (dist -> frontend/dist)
src-tauri/           # Rust Tauri backend
```

## Версионирование
- `v0.3.10` — последний Python GTK
- `v0.4.1` — C++/Tauri гибрид (Python deprecated)
- `v0.4.2` — Full C++/Tauri — Python удалён
- `v0.4.3` — **фикс вылета окна + CI** (текущий)

## Лицензия
MIT — `LICENSE`
