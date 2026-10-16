import { describe, expect, it } from 'vitest'
import type { Provider } from '../lib/llm'
import { chatPrivacyBlocked } from '../components/AIChatFull'

function prov(id: string, kind: Provider['kind']): Provider {
  return {
    id, name: id, kind, enabled: true, endpoint: '', auth: null,
    default_model: '', model: '', extra: {},
  } as Provider
}

describe('privacy_local_only_blocks_cloud', () => {
  it('blocks cloud provider under local_only with explanation', () => {
    const msg = chatPrivacyBlocked(prov('openai', 'open_ai'), 'local_only')
    expect(msg.length).toBeGreaterThan(0)
    expect(msg).toMatch(/local_only/)
    // NOTE: bare /sk-/ also matches "task-chat"; require key-like length.
    expect(msg).not.toMatch(/sk-[A-Za-z0-9]{8,}|AIza|keychain:\/\//)
  })

  it('allows local provider under local_only', () => {
    expect(chatPrivacyBlocked(prov('local', 'local_llama_cpp'), 'local_only')).toBe('')
    expect(chatPrivacyBlocked(prov('ollama', 'ollama'), 'local_only')).toBe('')
  })

  it('allows cloud when policy is cloud_allowed or unset', () => {
    expect(chatPrivacyBlocked(prov('openai', 'open_ai'), 'cloud_allowed')).toBe('')
    expect(chatPrivacyBlocked(prov('openai', 'open_ai'), undefined)).toBe('')
  })

  it('blocks send with no provider selected', () => {
    expect(chatPrivacyBlocked(null, 'local_only')).not.toBe('')
    expect(chatPrivacyBlocked(null, undefined)).not.toBe('')
  })

  it('blocks a disabled provider with a clear message regardless of policy', () => {
    const msg = chatPrivacyBlocked({ ...prov('openai', 'open_ai'), enabled: false }, 'cloud_allowed')
    expect(msg).toMatch(/выключен/)
  })

  it('blocks a local kind pointed at a remote endpoint under local_only', () => {
    const remote = { ...prov('ollama-remote', 'ollama'), endpoint: 'https://gpu.example.com:11434' }
    const msg = chatPrivacyBlocked(remote, 'local_only')
    expect(msg.length).toBeGreaterThan(0)
    expect(msg).toMatch(/local_only/)
  })

  it('allows a loopback local kind under local_only', () => {
    expect(chatPrivacyBlocked({ ...prov('ollama', 'ollama'), endpoint: 'http://127.0.0.1:11434' }, 'local_only')).toBe('')
    expect(chatPrivacyBlocked({ ...prov('llama', 'local_llama_cpp'), endpoint: 'http://localhost:8010' }, 'local_only')).toBe('')
  })
})
