# Fragile Notes — v0.5.6

> **Подпись v0.5.6 — почему отделили эту версию:**
> - **Сделано в 0.5.5:** `LLM Hub` прототип с `base64` ключами, `extra_args String`, `blocking reqwest`, `single active_model`, `fake download`.
> - **Почему 0.5.6 отдельно:** **Foundation Fix P0** — контракт и миграции: `schema v2` `Provider/Model/RuntimeProfile/TaskProfile`, `atomic save + backup`, `keyring Standard` без `base64`, `typed LlamaSettings`, `async Gateway + RuntimeManager (N=1 task-exclusive)`, `real connection test`, `privacy guard`, `fake download → NotImplemented`. Без новых фич — стабилизация.
> - **Цель 0.5.6:** не потерять настройки и ключи при миграции, единый транспорт, фундамент для `P1`.

**Obsidian-like vault.** `Task Manager v2` + `Tauri` `C++` + `LLM Hub v2`.

## Быстрый старт
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cargo tauri dev
# 🧠 Нейросети — теперь с keyring и typed llama.cpp
```

## Версионирование
- `v0.5.3` — фикс `tauri.conf` bundle
- `v0.5.4` — оптимизация RAM `lazy + reuse`
- `v0.5.5` — LLM Hub прототип
- `v0.5.6` — **Foundation P0: schema v2 + keyring + Gateway + RuntimeManager** (текущий)

## Лицензия
MIT — `LICENSE`
