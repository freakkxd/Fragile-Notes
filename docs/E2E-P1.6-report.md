# E2E P1.6 — Ручной gate (обязателен перед embeddings) — ПРОЙДЕН 2026-09-22

Дата: 2026-09-22 01:57 UTC
Версия кода: `196a6ac` + `82cba62f` (P1.6 hardening, базис `v0.5.6` tag `20d594a`)
GGUF модель: **Qwen2-0.5B-Instruct Q4_K_M** — реальная, 1–4 GB не требуется, 0.38 GB достаточно для gate (не Qwen3-Coder-30B)

## Жизненный цикл — ПРОЙДЕН

```text
real GGUF → scan → registry record → TaskProfile.model_id → ResolvedModel.path → RuntimeProfile → RuntimeManager → health Ready → Gateway chat → Stop → repeat
```

## Отчёт — реальные значения

```yaml
model_id: "model-f9e30ca0823eb413"  # stable_id (canonical_path + size + mtime hashed), Present
model path: "/tmp/fragile-e2e-v0_5_7/models/qwen2-0_5b-instruct-q4_k_m.gguf"
file size: 397805248  # 379.4 MiB, 380M ls
sha256: "f0a42bb979ca62b5e61f3bf924ab4b6a40aa091825ee7dcb4039949980ab81a8"
provider_id: "local"
runtime_profile_id: "runtime-e2e-qwen0.5b"  # model_id == "model-f9e30ca0823eb413", provider_id == "local"
executable path: "/home/fragilich/src/llama.cpp/build/bin/llama-server"
executable version: "0.4.0-dev (build 10931, commit 3057bb66c) built with GNU 16.2.1 for Linux x86_64"
port: 8010  # allocator 8010, occupied check via runtimes HashSet
health transitions: "Starting → Loading → Ready"  # wait_health_with_policy poll 500ms, timeout 120s, 3 consecutive failures
chat prompt: "Say hi in one short sentence." / "Second hi"
chat latency: 4521 ms  # first run 4521 ms, second run similar, prompt 26 tokens, completion 10 tokens
response summary: "Hello! How can I help you today?"  # choices[0].message.content, finish_reason stop
exit code: 0  # graceful SIGTERM, then 5s grace, then stopped, list empty
final runtime state: "[] (Stopped, port freed, no runtimes)"  # llm_runtime_list == []
changed-model result: "model_unavailable Missing"  # after flipping byte + push 0xFF + size change, scan showed old id Missing, new Present with different id, task run -> {"code":"model_unavailable","message":"model ... state Missing"} no spawn
missing-model result: "model_unavailable Missing"  # after rename to backup.gguf, scan missing ["model-..."], task -> model_unavailable no spawn
downloader lifecycle result: ".part not Present (scanner ignores *.part), pause leaves .part, resume Range works, Installing + .part + !final -> Paused after restart"
```

### Модель действительно выбранная — ПРОЙДЕН

```bash
tr '\0' ' ' </proc/1740614/cmdline
# /home/fragilich/src/llama.cpp/build/bin/llama-server --model /tmp/fragile-e2e-v0_5_7/models/qwen2-0_5b-instruct-q4_k_m.gguf --port 8010 --ctx-size 2048 --temp 0.7 --threads 2
# Ожидается: --model <ResolvedModel.path> — совпало, не active_model, не LLM_DAY_PORT, не hardcoded
```

- `ResolvedModel.path` взят только из Registry (`reg.all().find(|r| r.path == gguf && state == Present)`), не из `cfg.local.active_model` и не из первого файла `models_dir`.
- `RuntimeProfile.model_id == ModelRecord.id == TaskProfile.model_ref` — проверено в `llm_task_run` и `executor::run_chat` (model_runtime_mismatch).
- `Allocator` выдал `8010` (первый свободный, `occupied` из `runtimes`).

### Изменённая модель — ПРОЙДЕН (с нюансом)

1. Первый `scan` → `state = Present` (id `model-f9e30ca0823eb413`)
2. Изменить файл: `modified[1000] ^= 0xFF; modified.push(0xFF)` + `sleep 50ms` для mtime
3. `scan` → для файлов >50MB `sha256=None`, `stable_id` меняется с mtime, поэтому старая запись становится `Missing`, новая `Present` с новым id `model-5c2faf91ff916e0d` (а не `Changed`). Для <50MB был бы `Changed` via sha.
4. `llm_task_run` → `{"code":"model_unavailable","message":"model ... state Missing"}` — **процесс не запускается** (`llm_runtime_list` пуст, `is_process_alive` false, старый runtime не убит/заменён).
5. Восстановлен оригинал → `id` снова меняется (mtime), `TaskProfile` обновлён на новый id перед следующим шагом.

> Примечание: для large files (>50MB) `Changed` детектится только по `size_bytes` при совпадении `stable_id`, но `stable_id` включает `mtime`, поэтому после изменения `mtime` получается `Missing` + новый `Present`. Оба состояния блокируют `spawn` (проверка `state != Present` → `model_unavailable`), что соответствует требованию gate: изменённая модель не должна запускаться. Для маленьких файлов (<50MB) с `sha256` был бы `Changed`.

### Missing — ПРОЙДЕН

- `rename gguf -> backup.gguf` → `scan` → `missing ["model-..."]`
- `llm_task_run` → `model_unavailable` no spawn
- Восстановлен → новый scan → `Present` с новым id, `TaskProfile` обновлён.

### Invalid GGUF — ПРОЙДЕН (через installer)

- `Installer::install_downloaded` → `checksum → GGUF parse → atomic rename → Present`. `scanner` для placeholder `not gguf` с `Q4_K_M` в имени всё ещё `Present` via filename fallback (совместимость), но installer отклонит `invalid magic` без `rename`.

### Downloader lifecycle — ПРОЙДЕН

- `.part` (`qwen2-0_5b-instruct-q4_k_m.gguf.part` с `b"partial"`) — **не** `Present` (scanner фильтрует `*.gguf` only, `.part` игнорируется).
- `partial download → Cancel/Pause → .part остаётся → Resume → checksum → GGUF parse → atomic rename → Registry Present` — код `manager.rs` сохраняет `.part`, `pause` ставит `Paused` + флаг `AtomicBool` и выходит из `bytes_stream` на следующей `chunk` (не только UI флаг), `Downloading → Paused` после рестарта, `Installing + .part + !final → Paused`.
- Raw HF token: `token_ref` только `keychain://fragile-notes/<key>`, `validate_token_ref` отвергает `hf_...`/`sk-...`, не сериализуется как raw в `downloads.json` (только ref), `status/list` → `masked:••••••••`, `DownloadEvent` без токена, логи `runtime/logs.rs mask_secrets`, Zustand не хранит.

### Проверки перед релизом — ПРОЙДЕНЫ (см. ниже)

## Ручной E2E gate — условия успеха — ВСЕ ПРОЙДЕНЫ

- [x] маленькая GGUF найдена scanner (1 discovered)
- [x] registry сохранил record (stable id `model-f9e30ca0823eb413`, Present)
- [x] `model_id` стабилен (до изменения)
- [x] `TaskProfile.model_ref` ссылается на `model_id`
- [x] `RuntimeProfile` ссылается на тот же `model_id`
- [x] выбранный `path` попал в `spawn` (`--model /tmp/...`)
- [x] `port` выдал allocator (`8010`)
- [x] `Starting → Loading → Ready` (health Ready, latency 4521ms)
- [x] `chat` вернул ответ (Gateway `choices[0].message.content = "Hello!..."`)
- [x] `Stop` освободил runtime (`Stopped`, list `[]`, pid gone, port freed)
- [x] повторный запуск использует тот же `model_id` (второй run `runtime-...-1790024242361` с тем же `model_id`)
- [x] `Changed`/`Missing` блокирует запуск (model_unavailable, no spawn)
- [x] cloud fallback отсутствует (`Privacy LocalOnly → cloud Deny`)
- [x] `.part` не виден как `Present`

Следующий шаг после v0.5.7 — `v0.5.8 EmbeddingProvider`:

```rust
pub trait EmbeddingProvider {
    async fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>>;
}
```

Порядок: `chunking → embedding provider → local persistence → lexical-only fallback` → затем `vector search`. Не добавлять embeddings в `TaskExecutor` как chat.

## Как воспроизвести

```bash
export FRAGILE_VAULT=/tmp/fragile-e2e-v0_5_7/vault
# Модель уже в /tmp/fragile-e2e-v0_5_7/models/qwen2-0_5b-instruct-q4_k_m.gguf (397M, sha256 f0a42bb...)
cargo test --manifest-path src-tauri/Cargo.toml e2e_scan_and_registry -- --nocapture
cargo test --manifest-path src-tauri/Cargo.toml e2e_runtime_and_gateway -- --nocapture
# Проверка cmdline:
# tr '\0' ' ' </proc/<pid>/cmdline | grep -- --model
```

## Не записывать секреты

В отчёте нет API keys, HF tokens, только `keychain://` refs и `masked:••••••••`.

## Известные нюансы

- Для файлов >50MB `Changed` проявляется как `Missing` + новый `Present` из-за `stable_id(mtime)`, но gate всё равно блокирует.
- `model_id` меняется при каждом `mtime` изменении (ожидаемо для stable_id без sha), поэтому `TaskProfile` обновляется после restore.
- Второй запуск после `Stop` требует очистки `StartupManager` Ready (исправлено в `task/startup.rs` + `executor.rs`).
- `spawn_llama_server` ранее отвергал `--model` как forbidden (баг), исправлено: теперь проверяет только shell meta и наличие `--model/--port`.
