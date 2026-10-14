import { describe, expect, it } from 'vitest'
import { parseConfigSafe } from '../lib/llm'
import { installTauriMock } from './tauriMock'

function baseCfg(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 2,
    providers: [],
    models: [],
    runtime_profiles: [],
    task_profiles: [],
    local: {
      binary_path: 'llama-server', models_dir: '/tmp/Models', active_model: '',
      n_ctx: 8192, n_threads: 0, n_gpu_layers: 0, temp: 0.7, top_p: 0.9,
      top_k: 40, repeat_penalty: 1.1, port: 8010, auto_start: false, use_mmap: true,
    },
    pipelines: [],
    active_pipeline: 'enrich',
    ...overrides,
  }
}

function provider(id: string, kind: string, extra: Record<string, unknown> = {}) {
  return {
    id, name: id, kind, enabled: true, endpoint: '', auth: null,
    default_model: '', extra: {},
    ...extra,
  }
}

describe('config_parsing_tolerant_safe', () => {
  it('keeps valid providers and quarantines unknown kinds', () => {
    const raw = baseCfg({
      providers: [
        provider('local', 'local_llama_cpp'),
        { id: 'weird', name: 'Weird', kind: 'quantum_ai', enabled: false },
      ],
    })
    const res = parseConfigSafe(raw)
    expect(res.config).not.toBeNull()
    expect(res.config!.providers.map((p) => p.id)).toEqual(['local'])
    expect(res.recovered).toBe(true)
    expect(res.issues.some((i) => i.code === 'unknown_kind')).toBe(true)
    // Raw unknown entry never leaks secrets-shaped data into messages.
    for (const i of res.issues) {
      expect(i.message).not.toMatch(/sk-|AIza|keychain:\/\//)
    }
  })

  it('rejects non-object root without throwing', () => {
    for (const bad of [null, 42, 'str', []]) {
      const res = parseConfigSafe(bad)
      expect(res.config).toBeNull()
      expect(res.issues.length).toBeGreaterThan(0)
    }
  })

  it('accepts a fully valid config with no recovery flag', () => {
    const raw = baseCfg({ providers: [provider('local', 'local_llama_cpp')] })
    const res = parseConfigSafe(raw)
    expect(res.config).not.toBeNull()
    expect(res.recovered).toBe(false)
    expect(res.issues).toEqual([])
  })

  it('merges backend report issues through the mocked invoke', async () => {
    const { llmGetConfigSafe } = await import('../lib/llm')
    installTauriMock({
      llm_get_config_report: JSON.stringify({
        status: 'degraded',
        reason: null,
        issues: [{ code: 'unknown_kind', index: 0, provider_id: 'x', detail: 'quantum_ai' }],
        active_path: '/tmp/llm.json',
        candidates: [],
        config: baseCfg({ providers: [provider('local', 'local_llama_cpp')] }),
      }),
    })
    const res = await llmGetConfigSafe()
    expect(res.reportStatus).toBe('degraded')
    expect(res.recovered).toBe(true)
    expect(res.config).not.toBeNull()
  })
})

// The report path is typed and only rejects at the IPC edge. These tests pin
// the real contract of llmGetConfigSafe: legacy fallback, all-fail, malformed
// embedded config, and absent Tauri. None of them may throw a raw error.
describe('llm_get_config_safe_typed_contract', () => {
  it('falls back to the legacy raw config when the report command is unavailable', async () => {
    const { llmGetConfigSafe } = await import('../lib/llm')
    // Legacy backend: no llm_get_config_report command, only llm_get_config.
    installTauriMock({
      llm_get_config: JSON.stringify(baseCfg({ providers: [provider('local', 'local_llama_cpp')] })),
    })
    const res = await llmGetConfigSafe()
    expect(res.config).not.toBeNull()
    expect(res.config!.providers.map((p) => p.id)).toEqual(['local'])
    // Legacy path carries no report classification.
    expect(res.reportStatus).toBeUndefined()
    expect(res.recovered).toBe(false)
    expect(res.issues).toEqual([])
  })

  it('returns a typed backend_unavailable result when both commands fail', async () => {
    const { llmGetConfigSafe } = await import('../lib/llm')
    installTauriMock({
      llm_get_config_report: () => Promise.reject(new Error('report down')),
      llm_get_config: () => Promise.reject(new Error('legacy down')),
    })
    const res = await llmGetConfigSafe()
    expect(res.config).toBeNull()
    expect(res.recovered).toBe(false)
    expect(res.issues.some((i) => i.code === 'backend_unavailable')).toBe(true)
    // User-facing message is present and free of raw payload / secrets.
    for (const i of res.issues) {
      expect(i.message).not.toMatch(/sk-|AIza|keychain:\/\//)
    }
  })

  it('never turns a malformed embedded report config into a false ready', async () => {
    const { llmGetConfigSafe } = await import('../lib/llm')
    // Report succeeds, but the embedded config is structurally unusable
    // (no providers array). This must be null + invalid_root, not ready.
    installTauriMock({
      llm_get_config_report: JSON.stringify({
        status: 'valid',
        reason: null,
        issues: [],
        active_path: '/tmp/llm.json',
        candidates: [],
        config: { schema_version: 2 },
      }),
    })
    const res = await llmGetConfigSafe()
    expect(res.config).toBeNull()
    expect(res.recovered).toBe(true)
    expect(res.reportStatus).toBe('valid')
    expect(res.issues.some((i) => i.code === 'invalid_root')).toBe(true)
  })

  it('returns a typed backend_unavailable result when Tauri IPC is absent', async () => {
    const { llmGetConfigSafe } = await import('../lib/llm')
    // No installTauriMock(): window.__TAURI__ is absent, as in a browser preview.
    const res = await llmGetConfigSafe()
    expect(res.config).toBeNull()
    expect(res.recovered).toBe(false)
    expect(res.issues).toHaveLength(1)
    expect(res.issues[0].code).toBe('backend_unavailable')
  })
})
