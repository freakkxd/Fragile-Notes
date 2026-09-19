# Fragile Notes — v0.4.6

> **Подпись v0.4.6 — почему отделили эту версию:**
> - **Сделано в 0.4.5:** вернулся `Windows NSIS` инсталлер (`targets nsis/msi`, `Russian/English`, `both` `perMachine+perUser`), но UI был базовым (`textarea`, простой `FileTree`, без `fuzzy`).
> - **Почему 0.4.6 отдельно:** **полная полировка UI до релизного вида** — токены `Inter + JetBrains Mono`, `FileTree` collapsible + иконки + `empty` CTA, `Editor` тулбар + `Tab=2` + `Ctrl+S`, `TabBar` `pin/dirty` + `CommandPalette` `fuzzy` `↑↓ Enter` + `StatusBar` `words/chars` + `Graph/Canvas/Search` релизные. Без изменения ядра `C++/Tauri`.
> - **Цель 0.4.6:** `Obsidian` уровень полировки — готов к релизу.

**Obsidian-like vault для Linux/Windows.** Ядро `C++` + `Tauri (Rust)` + `React`.

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Актуальный стек v0.4.6

| Слой | Технология | Где |
|------|------------|-----|
| **UI** | React 18 + TypeScript 5.5, Zustand, marked+DOMPurify, Vite 5.4 | `frontend/src/` — Ribbon, FileTree (collapsible+icons), Editor (toolbar), TabBar, CommandPalette (fuzzy), StatusBar, Graph/Canvas/Search |
| **Bridge** | Tauri 1.6 IPC + zod | `frontend/src/lib/bridge.ts` + `src-tauri/src/main.rs` |
| **Core** | C++20, OpenSSL, SQLite FTS5, CRDT | `cpp/` |
| **Backend** | Rust + Tauri, walkdir, rusqlite | `src-tauri/` |
| **Build** | CMake, Vite, Tauri NSIS/MSI | `frontend/dist` + `src-tauri/target/release/bundle` |
| **CI** | tsc, vite, ctest, cargo check/build + Windows NSIS | `.github/workflows/` |

**UX:** `Ribbon 44px` `hover/active` | `File Explorer` collapsible + `icons` + `+ Новая` | `Tabs pin/dirty` | `Editor toolbar` | `Split` | `Ctrl+P fuzzy` | `Ctrl+B` | `StatusBar words/chars` | `Graph/Canvas/Search`.

## Быстрый старт

### Windows — один файл (рекомендуется)
Скачай **FragileNotes-Setup-v0.4.6.exe** из **Releases** → https://github.com/freakkxd/Fragile-Notes/releases/latest
Двойной клик → `Установить` → `Запустить` — всё ставится само (`Program Files\Fragile Notes` + `Start Menu`), `vault` `~/Documents/FragileNotesVault` создастся автоматом. Тихая: `exe /S`.

> Также `*.msi` для `GPO`/`winget`.

### Linux / macOS (из исходников)
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
cmake -S cpp -B build/cpp && cmake --build build/cpp && ./build/cpp/fragile_tests
npm --prefix frontend install && npm --prefix frontend run build
cargo install tauri-cli --version "^1.6"
cargo tauri dev    # dev http://localhost:5173
cargo tauri build  # bundle -> src-tauri/target/release/bundle
```

## Безопасность волта
Волт `~/Documents/FragileNotesVault`. В `.gitignore` заблокированы личные заметки.

## Разработка
```bash
npm --prefix frontend run typecheck && npm --prefix frontend run build # 282kB (10.26kB css)
cmake -S cpp -B build/cpp && ctest --test-dir build/cpp
cargo check --manifest-path src-tauri/Cargo.toml
```

## Версионирование
- `v0.4.5` — Windows инсталлер с автоустановкой
- `v0.4.6` — **UI полировка до релизного вида** (текущий)

## Лицензия
MIT — `LICENSE`
