# Fragile Notes — v0.4.7

> **Подпись v0.4.7 — почему отделили эту версию:**
> - **Сделано в 0.4.6:** полировка UI (`Inter + JetBrains`, `FileTree` collapsible + иконки, `Editor` тулбар, `TabBar` `pin/dirty`, `Palette` `fuzzy`, `Graph/Canvas/Search` релизные)
> - **Почему 0.4.7 отдельно:** фикс бага **пустой полосы после закрытия сайдбара** — `left/right-panel.collapsed` оставалась `280/300px` полоса (видно на скрине), можно было убрать только ручным драгом. Теперь `flex:0 0 0` + `!important` схлопывание, `center` растягивается.
> - **Цель 0.4.7:** сайдбары закрываются без артефактов.

**Obsidian-like vault для Linux/Windows.** Ядро `C++` + `Tauri (Rust)` + `React`.

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Актуальный стек v0.4.7

| Слой | Технология | Где |
|------|------------|-----|
| **UI** | React 18 + TypeScript 5.5, Zustand, marked+DOMPurify, Vite 5.4 | `frontend/src/` — полированный Obsidian shell |
| **Bridge** | Tauri 1.6 IPC + zod | `frontend/src/lib/bridge.ts` + `src-tauri/src/main.rs` |
| **Core** | C++20, OpenSSL, SQLite FTS5, CRDT | `cpp/` |
| **Backend** | Rust + Tauri, walkdir, rusqlite | `src-tauri/` |
| **Build** | CMake, Vite, Tauri NSIS/MSI | `frontend/dist` + `src-tauri/target/release/bundle` |
| **CI** | tsc, vite, ctest, cargo check/build + Windows NSIS | `.github/workflows/` |

**UX:** `Ribbon` | `File Explorer` collapsible | `Tabs` | `Editor toolbar` | `Split` | `Ctrl+P fuzzy` | `Ctrl+B` (без полосы) | `StatusBar` | `Graph/Canvas`.

## Быстрый старт

### Windows — один файл
Скачай **FragileNotes-Setup-v0.4.7.exe** из **Releases** → https://github.com/freakkxd/Fragile-Notes/releases/latest
Двойной клик → `Установить` → всё само (`Program Files` + `Start Menu`), `vault` `~/Documents/FragileNotesVault` создастся. Тихая: `exe /S`.

### Linux / macOS
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
cmake -S cpp -B build/cpp && cmake --build build/cpp && ./build/cpp/fragile_tests
npm --prefix frontend install && npm --prefix frontend run build
cargo install tauri-cli --version "^1.6"
cargo tauri dev    # dev
cargo tauri build  # bundle
```

## Безопасность волта
Волт `~/Documents/FragileNotesVault`. В `.gitignore` заблокированы личные заметки.

## Разработка
```bash
npm --prefix frontend run typecheck && npm --prefix frontend run build # 282kB
cmake -S cpp -B build/cpp && ctest --test-dir build/cpp
cargo check --manifest-path src-tauri/Cargo.toml
```

## Версионирование
- `v0.4.6` — UI полировка
- `v0.4.7` — **фикс полосы сайдбара** (текущий)

## Лицензия
MIT — `LICENSE`
