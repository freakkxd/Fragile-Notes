import { describe, expect, it } from 'vitest'
import type { Provider } from '../lib/llm'
import { defaultModelForProvider, defaultProviderForChat } from '../components/AIChatFull'

function prov(id: string, kind: Provider['kind'], enabled: boolean, model = ''): Provider {
  return {
    id, name: id, kind, enabled, endpoint: '', auth: null,
    default_model: model, model: '', extra: {},
  } as Provider
}

describe('provider_model_defaults_from_config', () => {
  it('prefers first enabled local provider', () => {
    const list = [
      prov('openai', 'open_ai', true),
      prov('local', 'local_llama_cpp', true),
      prov('off', 'ollama', false),
    ]
    expect(defaultProviderForChat(list)?.id).toBe('local')
  })

  it('falls back to first enabled when no local exists', () => {
    const list = [prov('openai', 'open_ai', true), prov('gemini', 'gemini', true)]
    expect(defaultProviderForChat(list)?.id).toBe('openai')
  })

  it('returns null when nothing is enabled', () => {
    expect(defaultProviderForChat([prov('x', 'open_ai', false)])).toBeNull()
    expect(defaultProviderForChat([])).toBeNull()
  })

  it('takes model default from config, overridable afterwards', () => {
    expect(defaultModelForProvider(prov('o', 'open_ai', true, 'gpt-4o'))).toBe('gpt-4o')
    expect(defaultModelForProvider(prov('l', 'local_llama_cpp', true))).toBe('')
    expect(defaultModelForProvider(null)).toBe('')
  })

  it('never uses filesystem paths as identity', () => {
    // Model identifiers are remote ids; paths must not flow into selection.
    const m = defaultModelForProvider(prov('l', 'local_llama_cpp', true))
    expect(m).not.toMatch(/^\//)
  })
})
