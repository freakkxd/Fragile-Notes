# Fragile Notes — v0.4.15

> **Подпись v0.4.15 — почему отделили эту версию:**
> - **Сделано в 0.4.14:** фикс `AppImage` (`allowlist` + `beforeBuild` + `updater`).
> - **Почему 0.4.15 отдельно:** **визуально богаче** — `glass` + `градиенты` + `glow` + `3D hover` без изменения ядра.
> - **Цель 0.4.15:** интерфейс уровня `Obsidian` `+`.

**Obsidian-like vault.** `C++` + `Tauri` + `React` — богатая визуально.

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Визуально богаче
- `Glass` `blur 16px`, `ambient glow` `radial`, `ribbon` `gradient` + `scale`, `panels` `inset`, `tabs` `shadow`, `editor` `1.8`, `markdown` `gradient h1`, `file-tree` `translateX`, `palette` `18px`, `graph` `600x300`, `cards` `14px`

## Быстрый старт
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
npm --prefix frontend install && npm --prefix frontend run build
cargo tauri dev
```

## Версионирование
- `v0.4.14` — фикс `AppImage`
- `v0.4.15` — **богаче визуально** (текущий)

## Лицензия
MIT — `LICENSE`
