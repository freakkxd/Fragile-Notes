# Fragile Notes — v0.4.5

> **Подпись v0.4.5 — почему отделили эту версию:**
> - **Сделано в 0.4.4:** `CI` зелёный (`webkit/jsc/soup` симлинки + `Cargo` `fs-all/dialog-all` + `move` closure + `RGBA` иконка), но `exe` инсталлер пропал — `packaging/` и `build-windows.yml` удалены в `v0.4.2`.
> - **Почему 0.4.5 отдельно:** возвращает **полноценный Windows инсталлер с автоустановкой** — `Tauri NSIS` `both` (`perMachine`+`perUser`), `Russian/English`, `512px` иконка, один клик `Установить` без действий юзера. Vault `~/Documents/FragileNotesVault` создаётся сам при первом запуске.
> - **Цель 0.4.5:** `exe` `FragileNotes-Setup-v0.4.5.exe` снова в `Releases/latest` — как в `v0.4.1`, но теперь `Tauri`.

**Obsidian-like vault для Linux/Windows.** Ядро `C++` + `Tauri (Rust)` + `React`.

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Актуальный стек v0.4.5

| Слой | Технология | Где |
|------|------------|-----|
| **UI** | React 18 + TypeScript 5.5, Zustand, marked+DOMPurify, Vite 5.4 | `frontend/src/` |
| **Bridge** | Tauri 1.6 IPC + zod | `frontend/src/lib/bridge.ts` + `src-tauri/src/main.rs` |
| **Core** | C++20, OpenSSL, SQLite FTS5, CRDT | `cpp/` |
| **Backend** | Rust + Tauri, walkdir, rusqlite | `src-tauri/` |
| **Build** | CMake, Vite, Tauri NSIS/MSI | `frontend/dist` + `src-tauri/target/release/bundle` |
| **CI** | tsc, vite, ctest, cargo check/build + Windows NSIS build | `.github/workflows/` |

**UX:** `Ribbon` | `File Explorer` | `Tabs` | `Split Edit/Preview` | `Ctrl+P Palette` | `Graph/Canvas` | `StatusBar`.

## Быстрый старт

### Windows — один файл (рекомендуется)
Скачай **FragileNotes-Setup-v0.4.5.exe** из **Releases** → https://github.com/freakkxd/Fragile-Notes/releases/latest
Двойной клик → `Установить` → `Запустить` — **всё ставится само** (`Program Files\Fragile Notes`, `Start Menu`), `vault` `~/Documents/FragileNotesVault` создастся автоматом, `Next→Next` не нужен. Тихая установка: `FragileNotes-Setup-v0.4.5.exe /S` (NSIS silent).

> Также доступен `*.msi` (Wix) — для `GPO`/`winget` деплоя.

### Linux / macOS (из исходников)
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
cmake -S cpp -B build/cpp && cmake --build build/cpp && ./build/cpp/fragile_tests
npm --prefix frontend install && npm --prefix frontend run build
cargo install tauri-cli
cargo tauri dev    # dev
cargo tauri build  # bundle -> src-tauri/target/release/bundle/{nsis,msi,appimage,deb}
```

**Сборка своего exe (Windows):** `gh workflow run "Build Tauri Windows Installer"` → `Actions` → `windows-installers` artifact, или локально `cargo tauri build` (требует `NSIS` + `WiX`).

## Безопасность волта
Волт `~/Documents/FragileNotesVault`. В `.gitignore` заблокированы личные заметки.

## Разработка
```bash
npm --prefix frontend run typecheck && npm --prefix frontend run build
cmake -S cpp -B build/cpp && ctest --test-dir build/cpp
cargo check --manifest-path src-tauri/Cargo.toml
```

## Версионирование
- `v0.4.4` — CI зелёный
- `v0.4.5` — **Windows инсталлер с автоустановкой** (текущий)

## Лицензия
MIT — `LICENSE`
