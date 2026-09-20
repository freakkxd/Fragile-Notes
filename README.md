# Fragile Notes — v0.4.10

> **Подпись v0.4.10 — почему отделили эту версию:**
> - **Сделано в 0.4.9:** хотфикс `Python GTK` (`Revealer` `Paned` 44px) — вернул `Python` для твоего `Linux` на скринах.
> - **Почему 0.4.10 отдельно:** **чистый `Tauri` без Python** — удалён `pip` `fragile-notes`, `fragilenotes/`, `main.py`, `pyproject.toml`. Как задумывалось для `v0.4`.
> - **Цель 0.4.10:** рабочая последняя версия без `Python` — только `C++/Tauri` + `React`.

**Obsidian-like vault.** Только `C++` + `Tauri (Rust)` + `React` (без `Python`).

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Актуальный стек v0.4.10

| Слой | Технология |
|------|------------|
| **UI** | React 18 + TS, `display:none` сайдбары |
| **Core** | C++20, Tauri Rust |
| **Build** | CMake, Vite, Tauri NSIS/MSI/AppImage |

## Быстрый старт

### Windows
Скачай `FragileNotes-Setup-v0.4.10.exe` → https://github.com/freakkxd/Fragile-Notes/releases/latest — всё само.

### Linux — Tauri (без Python)
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
npm --prefix frontend install && npm --prefix frontend run build
cargo install tauri-cli --version "^1.6"
cargo tauri dev    # dev http://localhost:5173
cargo tauri build  # -> src-tauri/target/release/bundle/appimage/*.AppImage
./src-tauri/target/release/bundle/appimage/*.AppImage
```

## Версионирование
- `v0.4.9` — гибрид Python+Tauri (hotfix)
- `v0.4.10` — **чистый Tauri без Python** (текущий)

## Лицензия
MIT — `LICENSE`
