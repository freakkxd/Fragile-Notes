# Fragile Notes — v0.5.3

> **Подпись v0.5.3 — почему отделили эту версию:**
> - **Сделано в 0.5.2:** добавлен `icon.ico` `362K` для `Windows`, но `tauri.conf` всё ещё имел `icon: ["icon.png"]` и `targets: ["nsis","msi"]` без `icon.ico` в массиве и без `Linux` целей — `Windows build` падал `the bundle config must have a .ico icon`, `Linux bundle` не генерировался `No such file bundle/`.
> - **Почему 0.5.3 отдельно:** фикс `tauri.conf.json` `bundle`: `icon` → `["icon.ico","icon.png"]`, `installerIcon` → `icon.ico`, `targets` → `["appimage","deb","nsis","msi","updater"]` — теперь `Windows` + `Linux` `AppImage/deb` + `updater` артефакты генерируются.
> - **Цель 0.5.3:** `GitHub Release` собирается на обеих платформах с `updater.json`.

**Obsidian-like vault.** `Task Manager v2` + `Tauri` `C++`.

## Быстрый старт
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cargo tauri dev
```

## Версионирование
- `v0.5.1` — `Task Manager v2`
- `v0.5.2` — фикс `icon.ico` файл
- `v0.5.3` — **фикс `tauri.conf` bundle** `ico` + `targets` (текущий)

## Лицензия
MIT — `LICENSE`
