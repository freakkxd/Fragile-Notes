# E2E P1.6 — Ручной gate (обязателен перед embeddings)

Дата: 2026-09-22
Версия кода: `d46c31c` (+ `2e7347e`) — P1.6 hardening, базис `v0.5.6`
GGUF модель для теста: **1–4 GB** валидная (например, `tiny-qwen-1.5B-Q4_K_M.gguf` или `phi-2.gguf`)

## Жизненный цикл

```text
scan → registry record → select model → TaskProfile.model_id → ResolvedModel.path → RuntimeProfile → RuntimeManager → health Ready → Gateway chat → Stop
```

## Отчёт (заполняется вручную)

```yaml
model_id: ""            # Registry stable id
path: ""                # ResolvedModel.path (absolute)
sha256: ""              # из Registry или compute
provider_id: "local"
runtime_profile_id: ""  # must match TaskProfile.runtime_profile_id и иметь model_id == model_id
executable: ""          # resolve_executable (SystemPath/BundledSidecar)
port: 8010
health_transitions: "Stopped → Starting → Loading → Ready"  # из llm_runtime_health / logs
chat_latency_ms: 0
exit_code: 0
final_runtime_state: "Stopped"  # после Stop свободен, list пуст
```

## Критические проверки

### Модель действительно выбранная
```bash
tr '\0' ' ' </proc/<pid>/cmdline
# Ожидается: --model <ResolvedModel.path>  (точный путь из Registry)
# Не active_model, не первый файл из models_dir, не старое значение v0.5.5
```

### Изменённая модель
1. Первый успешный `scan` → `state = Present`
2. Изменить файл (touch или `echo x >> model.gguf`)
3. `scan` → `state = Changed`
4. `llm_task_run` → `TaskErrorDto code=model_unavailable` (или `model_unavailable` в executor)
5. **Процесс не запускается** — проверить `llm_runtime_list` пуст и `is_process_alive(pid)==false`

### Миграция файла
- `model.gguf.part` — **не** отображается как `Present` (scanner игнорирует `.part`)
- После установки: `part → checksum (sha256) → GGUF parse → atomic rename → Present` (см. `download/installer.rs`)

### Downloader (6 существующих тестов — нормально для первой вертикали)
Перед embeddings расширить до 13:
- [ ] resume 206
- [ ] 200 after Range restarts, not appends
- [ ] 416 handling
- [ ] cancellation leaves .part
- [ ] checksum mismatch
- [ ] size mismatch
- [ ] disconnect/retry
- [ ] timeout
- [ ] chunked response
- [ ] progress throttling 200ms/1MB
- [ ] disk-space failure
- [ ] redirect HTTPS policy
- [ ] corrupted GGUF

Особо: `pause` должен ставить флаг `AtomicBool` и выходить из `bytes_stream` на следующей chunk (не только UI флаг).

### Hugging Face token
- Не сериализуется в `downloads.json` (только `keychain://` ref)
- Не возвращается в `llm_download_status/list` (masked)
- Не попадает в `DownloadEvent`
- Не попадает в логи (masked в `runtime/logs.rs`)
- Не хранится в Zustand
- Модель: `HuggingFace { repo_id, revision, filename, token_ref: Option<String> }` — downloader получает raw via `SecretStore` перед `Authorization: Bearer`

### DownloadManager lifecycle
- `download started → app crash/kill → .part remains → app starts → downloads.json loaded → job Paused/Interrupted → resume works`
- Не восстанавливать в `Downloading` — безопасно `Downloading → Paused`, требовать явный `Resume`
- `Installing + .part exists + final absent → Paused/Failed` (не оставлять `Installing`)

## Ручной E2E gate — условия успеха

- [ ] маленькая GGUF найдена scanner
- [ ] registry сохранил record (stable id)
- [ ] `model_id` стабилен
- [ ] `TaskProfile.model_ref` ссылается на `model_id`
- [ ] `RuntimeProfile` ссылается на тот же `model_id`
- [ ] выбранный `path` попал в `spawn` (`--model`)
- [ ] `port` выдал allocator (`8010` или следующий свободный)
- [ ] `Starting → Loading → Ready` (health)
- [ ] `chat` вернул ответ (Gateway)
- [ ] `Stop` освободил runtime (`Stopped`, порт свободен)
- [ ] повторный запуск использует тот же `model_id`
- [ ] `Changed` блокирует запуск (нет spawn)
- [ ] cloud fallback отсутствует (`Privacy LocalOnly → cloud Deny`)
- [ ] `.part` не виден как `Present`

После прохождения — переход к embeddings: начать с абстракции

```rust
pub trait EmbeddingProvider {
    async fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>>;
}
```

Порядок: `chunking → embedding provider → local persistence → lexical-only fallback` → затем `vector search`. Не добавлять embeddings в `TaskExecutor` как разновидность chat без отдельного capability и отдельного response type.

## Как запустить вручную

```bash
cargo tauri dev
# 1. Поместить GGUF 1–4GB в ~/Models или ~/Documents/FragileNotesVault/.fragile/models
# 2. invoke llm_models_scan -> проверить registry record
# 3. Создать/обновить TaskProfile + RuntimeProfile с model_id
# 4. llm_task_run(taskProfileId, [{role:"user", content:"ping"}])
# 5. Проверить cmdline, health, latency, затем Stop
# 6. Изменить GGUF -> scan -> Changed -> повторный run -> ModelUnavailable
```
