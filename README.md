# Fragile Notes — v0.4.13

> **Подпись v0.4.13 — почему отделили эту версию:**
> - **Сделано в 0.4.12:** авто без кликов (`Tauri updater` `autoInstall`), но локально `cargo tauri build` падал — `allowlist.updater` не существует в `Tauri 1.x` + `beforeBuild` `../frontend` давал `ENOENT`.
> - **Почему 0.4.13 отдельно:** фикс `allowlist` (убрал `updater`) + `beforeBuild` (`frontend` без `../`) чтобы `AppImage` собирался и открывался на этом компе.
> - **Цель 0.4.13:** локальная сборка без ошибок, `AppImage` открывается.

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
- `v0.4.12` — авто без кликов
- `v0.4.13` — **фикс локальной сборки** (текущий)

## Лицензия
MIT — `LICENSE`
