# Fragile Notes — v0.5.8 (Local AI Embeddings and RAG Foundation)

> **Подпись v0.5.6 — почему отделили эту версию:**
> - **Сделано в 0.5.5:** `LLM Hub` прототип с `base64` ключами, `extra_args String`, `blocking reqwest`, `single active_model`, `fake download`.
> - **Почему 0.5.6 отдельно:** **Foundation Fix P0** — контракт и миграции: `schema v2` `Provider/Model/RuntimeProfile/TaskProfile`, `atomic save + backup`, `keyring Standard` без `base64`, `typed LlamaSettings`, `async Gateway + RuntimeManager (N=1 task-exclusive)`, `real connection test`, `privacy guard`, `fake download → NotImplemented`. Без новых фич — стабилизация.
> - **Цель 0.5.6:** не потерять настройки и ключи при миграции, единый транспорт, фундамент для `P1`.

> **Подпись v0.5.7 — почему отделили эту версию от v0.5.6:**
> - **Сделано в P1.1–P1.6:** `Runtime Core`, `Health+Logs`, `Executable Resolver`, `TaskExecutor`, `Model Registry`, `Downloader` (bytes/installer/registry). P0 дал контракт, P1 дал локальный lifecycle, но без реального E2E не было гарантии, что `ResolvedModel.path` попадёт в `spawn`.
> - **Почему 0.5.7 отдельно:** ручной E2E с реальной GGUF `Qwen2-0.5B` (397M) выявил 3 бага: `spawn --model` как forbidden, `single-flight Ready` hang, `executor` возвращал `profile_id` вместо `runtime_id` и `Missing`/`Changed` для >50MB. Исправлено и покрыто 60 тестами.
> - **Цель 0.5.7:** зафиксировать проверенный локальный цикл `scan → registry → ResolvedModel → RuntimeManager --model → Ready → Gateway → Stop` как релиз, без embeddings.

**Obsidian-like vault.** `Task Manager v2` + `Tauri` `C++` + `LLM Hub v2` + `local AI search и RAG foundation (v0.5.8)`.

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
- `v0.5.7` — **P1 local model lifecycle: scan → registry → ResolvedModel → RuntimeManager → Ready → Gateway** (E2E пройден)
- `v0.5.8` — **Local AI Embeddings and RAG Foundation** (текущий релиз)

## Latest release

[Fragile Notes v0.5.8](https://github.com/freakkxd/Fragile-Notes/releases/tag/v0.5.8)

v0.5.8 — Local AI Embeddings and RAG Foundation: управляемый lifecycle локальных моделей, embeddings pipeline, lexical/semantic/hybrid search и безопасный bounded RAG context. RAG foundation с lexical E2E, mock-покрытием semantic path и поддержкой OpenAI-compatible embedding provider.

### Установка

Linux (AppImage):

```bash
chmod +x fragile-notes_0.5.8_amd64.AppImage
./fragile-notes_0.5.8_amd64.AppImage
```

- [Download AppImage](https://github.com/freakkxd/Fragile-Notes/releases/download/v0.5.8/fragile-notes_0.5.8_amd64.AppImage)
- [Download .deb](https://github.com/freakkxd/Fragile-Notes/releases/download/v0.5.8/fragile-notes_0.5.8_amd64.deb) (`sudo apt install ./fragile-notes_0.5.8_amd64.deb`)

Windows:

- [Fragile.Notes_0.5.8_x64-setup.exe](https://github.com/freakkxd/Fragile-Notes/releases/download/v0.5.8/Fragile.Notes_0.5.8_x64-setup.exe)
- [Fragile.Notes_0.5.8_x64_en-US.msi](https://github.com/freakkxd/Fragile-Notes/releases/download/v0.5.8/Fragile.Notes_0.5.8_x64_en-US.msi)
- [Fragile.Notes_0.5.8_x64_ru-RU.msi](https://github.com/freakkxd/Fragile-Notes/releases/download/v0.5.8/Fragile.Notes_0.5.8_x64_ru-RU.msi)

Платформы: Linux x86_64, Windows x64. macOS и ARM builds отсутствуют. `updater.json` в assets — только update metadata, не installer.

### Локальный AI setup

1. Установить или собрать совместимый `llama-server`.
2. Открыть LLM settings в Fragile Notes и указать runtime executable.
3. Просканировать GGUF models, выбрать model/task profile.
4. Для embeddings использовать отдельную embedding-capable модель с поддерживаемым `/v1/embeddings` endpoint (проверено: `nomic-embed-text-v1.5` GGUF + `llama-server --embedding`, 768 dims).
5. Credentials для cloud providers хранить только через системный keyring.

> Chat model не обязательно является embedding model. Для semantic search нужна embedding-capable модель и поддерживаемый `/v1/embeddings` endpoint.

> 🔒 Privacy: API keys хранятся только в системном keyring и никогда не попадают в config, logs, frontend state или export. Проверяйте `TaskProfile` privacy перед облачными вызовами.

### Ограничения

> ⚠️ v0.5.8 — foundation-релиз. Semantic E2E подтверждён локально (Stage 1: `nomic-embed-text-v1.5` Q4_K_M, 768 dims, `llama-server --embedding` — см. `docs/E2E-Stage1-embeddings-report.md`); production cloud E2E не подтверждён. Отсутствуют: streaming UI, OAuth, tools/agents, reranker, sqlite-vec, query rewriting, background indexing, on-save/scheduled pipelines, parallel runtimes, удаление моделей через UI, vision/audio workflows.

Детали — [CHANGELOG.md](CHANGELOG.md) (раздел `[0.5.8]`) и [GitHub Release](https://github.com/freakkxd/Fragile-Notes/releases/tag/v0.5.8).

## Лицензия
MIT — `LICENSE`
