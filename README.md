# Fragile Notes — v0.5.4

> **Подпись v0.5.4 — почему отделили эту версию:**
> - **Сделано в 0.5.3:** фикс `tauri.conf` `icon` + `targets` для сборки `Windows/Linux/updater`.
> - **Почему 0.5.4 отдельно:** `фоном 5-6МБ → оптимизация до ~3.5МБ` без ломки фич: `Rust` переиспользуемый `reqwest Client` + `Lazy Regex` + `profile.release opt-level z/lto/strip`, `React.lazy` для тяжелых вьюх + `updater 5мин→15мин` + `chunk split`.
> - **Цель 0.5.4:** меньше RAM/CPU фоном, та же функциональность.

**Obsidian-like vault.** `Task Manager v2` + `Tauri` `C++`.

## Быстрый старт
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cargo tauri dev
```

## Версионирование
- `v0.5.1` — `Task Manager v2`
- `v0.5.2` — фикс `icon.ico` файл
- `v0.5.3` — фикс `tauri.conf` bundle
- `v0.5.4` — **оптимизация RAM 5-6МБ→3.5МБ** `lazy + reuse Client` (текущий)

## Лицензия
MIT — `LICENSE`
