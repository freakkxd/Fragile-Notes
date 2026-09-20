# Fragile Notes — v0.4.11

> **Подпись v0.4.11 — почему отделили эту версию:**
> - **Сделано в 0.4.10:** чистый `Tauri` без `Python` (`AppImage`).
> - **Почему 0.4.11 отдельно:** **автообновление**: на этом компе после каждого `push` — `git hook` + `systemd timer` пересобирает `AppImage`; в `Tauri` — баннер `⬆ Доступно обновление` из `GitHub Releases` (`api.github.com`) с кнопкой `Скачать`.
> - **Цель 0.4.11:** не надо вручную `git pull && cargo tauri build`.

**Obsidian-like vault.** `C++` + `Tauri` + `React` + автообновление.

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Автообновление

| Канал | Как работает |
|-------|--------------|
| **Локально (этот комп)** | `post-commit`/`post-merge` + `systemd timer 5мин` → `~/bin/fragile-auto-build.sh` (`git pull` → `npm build` → `cargo tauri build` → `~/Applications/Fragile-Notes.AppImage`) |
| **Удалённо (установленный AppImage)** | `App.tsx` `checkForUpdates` (`fetch api.github.com/releases/latest`) при старте + 30мин + `focus` → баннер `⬆ Доступно обновление {tag}` → `Скачать` (`shell.open`) |

Лог локальной сборки: `~/.cache/fragile-auto-build.log`, ручной триггер: `~/bin/fragile-auto-build.sh` или `systemctl --user start fragile-auto-update.service`

## Быстрый старт

### Windows
`FragileNotes-Setup-v0.4.11.exe` → https://github.com/freakkxd/Fragile-Notes/releases/latest — автообновление через баннер.

### Linux — Tauri (без Python)
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cd Fragile-Notes
npm --prefix frontend install && npm --prefix frontend run build
cargo tauri dev    # dev
cargo tauri build  # -> AppImage
```

## Версионирование
- `v0.4.10` — чистый Tauri
- `v0.4.11` — **автообновление** (текущий)

## Лицензия
MIT — `LICENSE`
