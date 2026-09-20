# Fragile Notes — v0.4.9

> **Подпись v0.4.9 — почему отделили эту версию:**
> - **Сделано в 0.4.8:** `display:none` для `React Tauri` (`left/right-panel.collapsed`), но на скринах `Python GTK` — полоса осталась (пустой `side_column` 280px как на скрине 1, видно `Настройки` + `WORKSPACES`).
> - **Почему 0.4.9 отдельно:** хотфикс `Python GTK` `WorkspaceMixin`: `top.set_position(44)` + `timeout_add` + `side_column.set_size_request(44)` — теперь `Ctrl+B` / клик `◧` полностью схлопывает без полосы. Восстановлен `Python` runtime (гибрид) для текущего инсталла.
> - **Цель 0.4.9:** обе платформы без полосы.

**Obsidian-like vault.** Гибрид `C++` + `Tauri (Rust/React)` + `Python GTK` (hotfix).

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Актуальный стек v0.4.9

| Слой | Технология |
|------|------------|
| **UI Python** | GTK4 4.14 + libadwaita 1.5, `Revealer` + `Paned` (hotfix) | 
| **UI Tauri** | React 18 + TS, `display:none` |
| **Core** | C++20, Tauri Rust, Python (transition) |
| **Build** | CMake, Vite, Tauri NSIS + PyInstaller (hybrid) |

## Быстрый старт

### Windows — Tauri
Скачай `FragileNotes-Setup-v0.4.9.exe` → https://github.com/freakkxd/Fragile-Notes/releases/latest — всё само.

### Linux — Python (текущий инсталл на скринах)
```bash
git pull
pip install -e . # обновит 0.4.9
fragile-notes # или python main.py — теперь без полосы
```
Tauri: `npm --prefix frontend run build && cargo tauri dev`

## Версионирование
- `v0.4.8` — `display:none` React
- `v0.4.9` — **Python GTK hotfix + гибрид** (текущий)

## Лицензия
MIT — `LICENSE`
