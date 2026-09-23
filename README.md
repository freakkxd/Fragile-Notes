# Fragile Notes — v0.5.7 (P1 local model lifecycle)

> **Подпись v0.5.6 — почему отделили эту версию:**
> - **Сделано в 0.5.5:** `LLM Hub` прототип с `base64` ключами, `extra_args String`, `blocking reqwest`, `single active_model`, `fake download`.
> - **Почему 0.5.6 отдельно:** **Foundation Fix P0** — контракт и миграции: `schema v2` `Provider/Model/RuntimeProfile/TaskProfile`, `atomic save + backup`, `keyring Standard` без `base64`, `typed LlamaSettings`, `async Gateway + RuntimeManager (N=1 task-exclusive)`, `real connection test`, `privacy guard`, `fake download → NotImplemented`. Без новых фич — стабилизация.
> - **Цель 0.5.6:** не потерять настройки и ключи при миграции, единый транспорт, фундамент для `P1`.

> **Подпись v0.5.7 — почему отделили эту версию от v0.5.6:**
> - **Сделано в P1.1–P1.6:** `Runtime Core`, `Health+Logs`, `Executable Resolver`, `TaskExecutor`, `Model Registry`, `Downloader` (bytes/installer/registry). P0 дал контракт, P1 дал локальный lifecycle, но без реального E2E не было гарантии, что `ResolvedModel.path` попадёт в `spawn`.
> - **Почему 0.5.7 отдельно:** ручной E2E с реальной GGUF `Qwen2-0.5B` (397M) выявил 3 бага: `spawn --model` как forbidden, `single-flight Ready` hang, `executor` возвращал `profile_id` вместо `runtime_id` и `Missing`/`Changed` для >50MB. Исправлено и покрыто 60 тестами.
> - **Цель 0.5.7:** зафиксировать проверенный локальный цикл `scan → registry → ResolvedModel → RuntimeManager --model → Ready → Gateway → Stop` как релиз, без embeddings.

**Obsidian-like vault.** `Task Manager v2` + `Tauri` `C++` + `LLM Hub v2` + `P1 E2E готов (v0.5.7)`.

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
- `v0.5.7` — **P1 local model lifecycle: scan → registry → ResolvedModel → RuntimeManager → Ready → Gateway** (текущий, E2E пройден)
- `v0.5.8` — **Local AI Embeddings and RAG Foundation** (planned, tag отсутствует)

## Unreleased — v0.5.8 scope (без tag)

RAG foundation с lexical E2E, mock-покрытием semantic path и поддержкой OpenAI-compatible embedding provider.

Включено: `EmbeddingProvider` contract, deterministic Markdown chunking, incremental chunk/vector persistence (`LE f32 BLOB`, fingerprint isolation), FTS5 lexical fallback, fingerprint-aware cosine search, RRF hybrid, bounded RAG retrieval с `NoEvidence` policy, generation settings из `TaskProfile`.

Явно НЕ production-ready: полноценный semantic E2E на реальной embedding-модели, production cloud E2E, streaming UI, OAuth/account login, tools/agents, reranker, sqlite-vec, query rewriting, автоматическая индексация, on-save/scheduled pipelines, background indexing, parallel runtimes, auto-update моделей, удаление моделей через UI, vision/audio workflows.

Детали — `CHANGELOG.md` (раздел `Unreleased`).

## Лицензия
MIT — `LICENSE`
