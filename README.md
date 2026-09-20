# Fragile Notes — v0.4.12

> **Подпись v0.4.12 — почему отделили эту версию:**
> - **Сделано в 0.4.11:** автообновление на этом компе (`systemd timer` + `git hooks`) + баннер `GitHub Releases` с кнопкой `Скачать` (надо было кликать).
> - **Почему 0.4.12 отдельно:** **полная автоматика на любом устройстве без ручных триггеров** — `Tauri updater` сам `checkUpdate` → `installUpdate` → `relaunch` каждые `5 мин` + `focus`, без `Скачать`.
> - **Цель 0.4.12:** `AppImage`/`exe` на любом устройстве обновляются молча.

**Obsidian-like vault.** `C++` + `Tauri` + `React` + автообновление (везде).

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Автообновление (без ручных триггеров)

| Устройство | Как |
|------------|-----|
| **Любое (установленный AppImage/exe)** | `App.tsx` `autoInstallIfAvailable()` → `Tauri plugin updater` `checkUpdate()` ( `updater.json` с `pubkey` ) → `installUpdate()` → `relaunch()` каждые `5 мин` + `focus`/`visibilitychange`, без клика |
| **Этот комп (разработка)** | `post-commit`/`post-merge` + `systemd timer 5мин` → `~/bin/fragile-auto-build.sh` (для `cargo tauri dev`) |

Fallback: если `updater.json` нет — баннер `GitHub API` `⬆ Доступно обновление` (как в v0.4.11).

## Быстрый старт

### Windows / Linux — релиз
Скачай `*.AppImage` / `*.exe` → https://github.com/freakkxd/Fragile-Notes/releases/latest — обновится сам.

### Linux — из исходников
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
npm --prefix frontend install && npm --prefix frontend run build
cargo tauri dev
```

## Версионирование
- `v0.4.11` — автообновление с кликом `Скачать`
- `v0.4.12` — **авто без кликов** (текущий)

## Лицензия
MIT — `LICENSE`
