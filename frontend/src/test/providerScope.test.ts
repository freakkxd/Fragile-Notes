import { describe, expect, it } from 'vitest'
import { isLoopbackEndpoint, providerIsCloud } from '../components/settings/types'

// Mirrors the backend `endpoint_scope`: local kinds are local only with an
// empty/loopback endpoint, otherwise cloud; non-local kinds are always cloud.
describe('provider_is_cloud_matches_backend_endpoint_scope', () => {
  it('treats non-local kinds as cloud', () => {
    expect(providerIsCloud('open_ai', 'https://api.openai.com/v1')).toBe(true)
    expect(providerIsCloud('open_router', 'https://openrouter.ai/api/v1')).toBe(true)
    expect(providerIsCloud('claude', 'https://api.anthropic.com')).toBe(true)
  })

  it('treats local kinds with empty/loopback endpoints as local', () => {
    expect(providerIsCloud('local_llama_cpp', '')).toBe(false)
    expect(providerIsCloud('local_llama_cpp', undefined)).toBe(false)
    expect(providerIsCloud('ollama', 'http://127.0.0.1:11434')).toBe(false)
    expect(providerIsCloud('ollama', 'http://localhost:11434')).toBe(false)
    expect(providerIsCloud('ollama', 'http://[::1]:11434')).toBe(false)
  })

  it('treats a local kind with a remote endpoint as cloud', () => {
    expect(providerIsCloud('ollama', 'https://gpu.example.com:11434')).toBe(true)
    expect(providerIsCloud('local_llama_cpp', 'http://192.168.1.5:8010')).toBe(true)
  })

  it('isLoopbackEndpoint mirrors the backend check', () => {
    expect(isLoopbackEndpoint('')).toBe(true)
    expect(isLoopbackEndpoint('   ')).toBe(true)
    expect(isLoopbackEndpoint('http://127.0.0.1:1234')).toBe(true)
    expect(isLoopbackEndpoint('https://example.com')).toBe(false)
  })
})
