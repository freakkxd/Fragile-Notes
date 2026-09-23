# E2E Report — Stage 1: Real Local Embeddings (2026-09-23)

Scope: stabilize after v0.5.8, verify the semantic path on a real local
embedding model. No new product features. Temporary live harnesses were
deleted before committing; only fixes, one ignored smoke test, and docs remain.

## Model

```text
repo/source:      nomic-ai/nomic-embed-text-v1.5-GGUF (public, no token)
revision:         0188c9bf409793f810680a5a431e7b899c46104c
filename:         nomic-embed-text-v1.5.Q4_K_M.gguf
size:             84106624 bytes (~80.2 MiB)
expected sha256:  d4e388894e09cf3816e8b0896d81d265b55e7a9fff9ab03fe8bf4ef5e11295ac (LFS oid, verified by installer)
model role:       Embedding (explicit test-procedure assignment; scanner does not guess)
expected dimensions: 768 (confirmed live)
license:          apache-2.0
```

Chat model (answer phase only, CPU): Qwen3-Coder-30B Q4_K_XL from the local
models dir. Never run simultaneously with the embedding runtime (VRAM).

## Downloader (dogfood through `DownloadManager`)

```text
download:          Completed, 84106624/84106624 bytes in ~16s, sha256 verified,
                   final .gguf exists, .part absent, GGUF parsed by installer
pause/resume:      Paused at ~3MB with .part kept → resume → Completed 84106624
                   (HTTP Range honored, no restart-from-zero)
checksum:          LFS sha256 verified on install; mismatch → checksum_mismatch
atomic install:    .part → final rename after verify+parse
restart recovery:  new manager on same state dir → job Paused (never Downloading)
                   → explicit resume → Completed
cancel:            Cancelled, final absent, .part kept (resume-able)
```

No token involved (public repo). `downloads.json`/`status`/`list` carry no secrets.

### Bugs found and fixed by dogfood

1. **Stale worker clobbers resumed job** (`download/manager.rs`,
   commit `fix(download)`): the pre-resume worker could exit after the resumed
   task set `Downloading` and reset shared state to `Paused`, silently
   discarding a completed download. Workers now check
   `is_current_worker` (cancel-flag identity) before touching shared state,
   progress, persistence, or flag cleanup. Regression test:
   `stale_worker_is_not_current_after_resume`.
2. **Completion byte counter lag** (`download/manager.rs`, same commit):
   throttled progress left `bytes_downloaded` short of total; the epilogue
   now syncs the exact count from the finished worker.
3. **GGUF type widths wrong** (`models/gguf.rs`, commit `fix(models)`):
   the parser used incorrect ids/widths (e.g. UINT32 read as 1 byte,
   INT16 read as string) and truncated arrays without seeking, misaligning
   every later KV. Widths corrected against `ggml/include/gguf.h`
   (0=U8 1=I8 2=U16 3=I16 4=U32 5=I32 6=F32 7=BOOL 8=STRING 9=ARRAY
   10=U64 11=I64 12=F64); array tails are seek-skipped; arch-prefixed keys
   (`{arch}.embedding_length` etc.) recognized. Regression test:
   `test_large_array_keeps_stream_aligned`.
4. **Quantization empty on parsed files** (`models/scanner.rs`, same commit):
   `quantization` is filename-derived; now filled whenever parsing leaves it
   empty (`Q4_K_M` confirmed on the nomic file).

## Registry

```text
scan:            1 record, state Present, .part not indexed
arch:            nomic-bert; embedding_length: 768; quantization: Q4_K_M
role:            Embedding + capability Embeddings assigned explicitly
                 (scanner still defaults to Chat/General — no silent guessing)
model_id:        model-<stable> (84MB > 50MB → no sha → canonical+size+mtime id)
fingerprint:     record.id (resolve_model_fingerprint, no sha available)
stable_across_rescan: yes, same id, roles preserved
```

## Embedding runtime

```text
executable: <llama.cpp build>/bin/llama-server, version 0.4.0-dev
command line: --model <nomic gguf> --port <auto> --ctx-size 2048
              --temp 0.7 --n-gpu-layers 99 --embedding
port:          auto-allocated (8010/8011/8013 across runs, reused after stop)
health:        Starting → Ready in 1–4s
startup latency: 1–4s
stop:          clean, no leaked processes after harness (verified via ps)
```

`--embedding` passes through `runtime_args` whitelist; no product change needed.

## Embeddings (real `/v1/embeddings`)

```text
provider:        OpenAiCompatibleEmbeddingProvider, no api_key
request:         {"model": "<app-id>", "input": [...], "encoding_format": "float"}
response:        1 input → 1 vector, index 0, dimensions 768, all finite,
                 stable across calls, L2 norm 1.0 (normalized)
dimensions:      768
persistence:     3 chunks → 3 vectors, model_id+fingerprint+dimensions recorded,
                 BLOB round-trip verified by re-read
reuse:           same-note reindex → 3 unchanged, 0 new provider calls
re-embed count:  middle-section edit → 1 unchanged + 2 replaced (fixed-window
                 boundaries shift after the edit point), stale vectors deleted
deleted_after_remove: all chunk vectors gone via cascade/cleanup
```

## Search

```text
semantic:        Russian query → 3 results, score descending (top 0.5647),
                 real chunk offsets, correct model_id+fingerprint,
                 wrong fingerprint → empty (isolation, no silent fallback)
hybrid:          "embedding runtime" → mode Hybrid, degraded=false,
                 4 fused results, dedup by chunk_id
fingerprint isolation: verified (Deny policy, explicit empty)
fallback:        not triggered; Deny honored
```

## RAG

```text
bounded context: 1535 chars ≤ 2000 limit, 2 references, truncated=true
references:      chunk_id+note_id from retrieval only; path/heading/offsets kept
prompt injection: adversarial note ("Ignore previous instructions…") stays
                 inside <retrieved_data>; system prompt unchanged
local answer:    Qwen3-Coder-30B on CPU (ctx 512, 7–44s load, ~22 tok/s):
                 "The runtime exposes an OpenAI-compatible endpoint [source 1]."
                 (61 chars extracted, grounded, cited)
degraded:        propagated, none triggered
```

## Generation (live)

```text
temperature: 0.0 → accepted
top_p:       0.9 → accepted
max_tokens:  64 → accepted (short grounded answer)
```

Request shape recorded redacted (no Authorization, no key, no private context).

## `rag_retrieve` decision — Option A (keep demo)

`rag_retrieve` stays a lexical-only compatibility command (`rag/mod.rs`,
`rag/tests.rs` document this by design). No silent API redesign in Stage 1.

## Checks

```text
cargo check / test (default suite, no network/vault/secrets) / clippy -D warnings
frontend typecheck + build, git diff --check — see Stage 1 final report
ignored smoke: tests_smoke::real_local_embedding_smoke
  (FRAGILE_REAL_EMBEDDINGS=1, loopback-only, 180s timeout, runtime cleanup,
   skip-without-model; validated live + skip path)
```

## Known limitations (unchanged)

Production cloud E2E, streaming UI, OAuth, tools/agents, reranker, sqlite-vec,
query rewriting, background indexing, on-save/scheduled pipelines, parallel
runtimes, model deletion UI, vision/audio — all still out of scope.
`rag_retrieve` remains lexical-only. Full cross-call cancellation registry
still not implemented.
