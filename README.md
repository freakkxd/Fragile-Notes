# Fragile Notes — v0.5.6 (P1.6 hardening — E2E gate)

> **Подпись v0.5.6 — почему отделили эту версию:**
> - **Сделано в 0.5.5:** `LLM Hub` прототип с `base64` ключами, `extra_args String`, `blocking reqwest`, `single active_model`, `fake download`.
> - **Почему 0.5.6 отдельно:** **Foundation Fix P0** — контракт и миграции: `schema v2` `Provider/Model/RuntimeProfile/TaskProfile`, `atomic save + backup`, `keyring Standard` без `base64`, `typed LlamaSettings`, `async Gateway + RuntimeManager (N=1 task-exclusive)`, `real connection test`, `privacy guard`, `fake download → NotImplemented`. Без новых фич — стабилизация.
> - **Цель 0.5.6:** не потерять настройки и ключи при миграции, единый транспорт, фундамент для `P1`.

**Obsidian-like vault.** `Task Manager v2` + `Tauri` `C++` + `LLM Hub v2` + `P1 E2E готов`.

## Быстрый старт
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cargo tauri dev
# 🧠 Нейросети — теперь с keyring и typed llama.cpp
# P1.6: scan → registry → TaskProfile → ResolvedModel → RuntimeManager → Gateway
```

## P1.6 E2E gate (ручной, обязателен перед embeddings)

Используй GGUF 1–4 GB, проверь полный цикл (`docs/E2E-P1.6-report.md`):
`scan → registry → TaskProfile.model_id → ResolvedModel.path → RuntimeProfile → RuntimeManager --model <path> → health Starting→Loading→Ready → Gateway chat → Stop`
- `.part` **не** Present; `part → checksum → GGUF parse → atomic rename → Present`
- `Changed` → `ModelUnavailable` без spawn; `tr '\0' ' ' </proc/<pid>/cmdline` должен содержать `--model <ResolvedModel.path>`
- Pausing оставляет `.part`, resume работает; `Downloading/Installing → Paused` после рестарта

См. `docs/E2E-P1.6-report.md`.

## Версионирование
- `v0.5.3` — фикс `tauri.conf` bundle
- `v0.5.4` — оптимизация RAM `lazy + reuse`
- `v0.5.5` — LLM Hub прототип
- `v0.5.6` — **Foundation P0: schema v2 + keyring + Gateway + RuntimeManager**
- `P1.1..P1.6` — Runtime Core, Health, Resolver, TaskExecutor, Registry, Downloader (unreleased, gate E2E) → следующий релиз `v0.6.0` после прохождения E2E

## Лицензия
MIT — `LICENSE`
