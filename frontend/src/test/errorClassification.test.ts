import { describe, expect, it } from 'vitest'
import { classifyError } from '../components/AIChatFull'

describe('error_classification_safe', () => {
  it('maps missing credential to actionable text without secret', () => {
    const out = classifyError(new Error('no api_key in keyring'))
    expect(out).toMatch(/Нет ключа/)
    expect(out).not.toMatch(/sk-[A-Za-z0-9]{8,}|AIza|keychain:\/\//)
  })

  it('maps unauthorized/invalid key without echoing the key', () => {
    for (const raw of ['unauthorized', 'invalid_key: bad', 'HTTP 401 Unauthorized']) {
      const out = classifyError(new Error(raw))
      expect(out).toMatch(/отклонён|unauthorized/i)
    }
    // Backend message that accidentally contains key-looking text is capped,
    // never passed through verbatim beyond the safe prefix.
    const out = classifyError(new Error('unauthorized for sk-ant-secretvalue123'))
    expect(out).not.toContain('sk-ant-secretvalue123')
  })

  it('maps network/timeout and privacy violations', () => {
    expect(classifyError(new Error('LLM offline http://x: timeout'))).toMatch(/Сеть\/таймаут/)
    expect(classifyError(new Error('PrivacyPolicyViolation: local-only'))).toMatch(/приватности/)
  })

  it('maps unknown provider/model with bounded raw text', () => {
    const out = classifyError(new Error('provider not found'))
    expect(out).toMatch(/Провайдер\/модель/)
    const long = classifyError(new Error('model ' + 'x'.repeat(5000)))
    expect(long.length).toBeLessThanOrEqual(250)
  })

  it('passes through unknown errors capped, never throws', () => {
    expect(classifyError('plain string failure')).toBe('plain string failure')
    expect(classifyError(null)).toBe('null')
    expect(classifyError(undefined)).toBe('undefined')
  })
})
