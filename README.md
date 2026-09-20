# Fragile Notes — v0.4.14

> **Подпись v0.4.14 — почему отделили эту версию:**
> - **Сделано в 0.4.13:** фикс `allowlist` + `beforeBuild` (`frontend` без `../`), но `cargo check` падал — `tauri-plugin-updater 1.6` не существует (доступны `2.x`/`3.x`).
> - **Почему 0.4.14 отдельно:** убран `tauri-plugin-updater` (для `Tauri 1.6` updater встроен), убран `.plugin()` — теперь `cargo tauri build` собирает `AppImage` и открывается.
> - **Цель 0.4.14:** локально открывается.

**Obsidian-like vault.** `C++` + `Tauri` + `React` (чисто).

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Быстрый старт

### Linux — Tauri
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
npm --prefix frontend install && npm --prefix frontend run build
cargo tauri dev    # dev
cargo tauri build  # -> AppImage
./src-tauri/target/release/bundle/appimage/*.AppImage
```

## Версионирование
- `v0.4.13` — фикс `allowlist` + `beforeBuild`
- `v0.4.14` — **фикс `tauri-plugin-updater`** (текущий)

## Лицензия
MIT — `LICENSE`
