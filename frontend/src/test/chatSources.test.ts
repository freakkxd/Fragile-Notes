import { describe, expect, it } from 'vitest'
import { parseChatResult, KNOWN_PROVIDER_KINDS, PERPLEXITY_PRESETS, ProviderKindSchema, ChatSourceSchema, type ChatSource } from '../lib/llm'
import { extractChatText, safeSourceLinks } from '../components/AIChatFull'

describe('perplexity_kind_registered', () => {
  it('is a known provider kind accepted by the schema', () => {
    expect(KNOWN_PROVIDER_KINDS).toContain('perplexity')
    expect(ProviderKindSchema.safeParse('perplexity').success).toBe(true)
  })

  it('exposes the five documented presets', () => {
    expect([...PERPLEXITY_PRESETS]).toEqual(['fast', 'low', 'medium', 'high', 'xhigh'])
  })
})

describe('parse_chat_result_envelope', () => {
  it('reads content and sources from the JSON envelope', () => {
    const raw = JSON.stringify({
      content: 'Answer text',
      sources: [
        { url: 'https://example.com/a', title: 'A' },
        { url: 'https://example.com/b', title: 'B', snippet: 's' },
      ],
    })
    const r = parseChatResult(raw)
    expect(r.content).toBe('Answer text')
    expect(r.sources.map(s => s.url)).toEqual(['https://example.com/a', 'https://example.com/b'])
  })

  it('accepts a legacy plain string as text with no sources', () => {
    const r = parseChatResult('plain assistant text')
    expect(r.content).toBe('plain assistant text')
    expect(r.sources).toEqual([])
  })

  it('keeps a legacy raw provider body intact so extractChatText still normalizes it', () => {
    const legacy = JSON.stringify({ choices: [{ message: { content: 'hello world' } }] })
    const r = parseChatResult(legacy)
    expect(r.content).toBe(legacy)
    expect(extractChatText(r.content)).toBe('hello world')
  })

  it('handles an envelope whose content is itself a legacy provider body', () => {
    const legacy = JSON.stringify({ choices: [{ message: { content: 'from local' } }] })
    const r = parseChatResult(JSON.stringify({ content: legacy, sources: [] }))
    expect(extractChatText(r.content)).toBe('from local')
    expect(r.sources).toEqual([])
  })

  it('drops malformed or missing source entries instead of throwing', () => {
    const raw = JSON.stringify({
      content: 'ok',
      sources: [
        { url: 'https://ok.example', title: 'ok' },
        { url: 123, title: 'bad type' },
        'not-an-object',
        { url: '' },
      ],
    })
    const r = parseChatResult(raw)
    expect(r.content).toBe('ok')
    expect(r.sources).toHaveLength(1)
    expect(r.sources[0].url).toBe('https://ok.example')
    expect(r.sources[0].snippet).toBe('')
  })

  it('never throws on garbage input', () => {
    for (const g of ['', null, undefined, 42, '{}', '{"content":42}']) {
      expect(() => parseChatResult(g)).not.toThrow()
    }
    expect(parseChatResult(null).content).toBe('')
    expect(parseChatResult(42).content).toBe('42')
  })
})

describe('safe_source_links', () => {
  const src = (url: string, title: string): ChatSource => ({ url, title, snippet: '' })

  it('keeps http(s) links and rejects dangerous or malformed URLs', () => {
    const links = safeSourceLinks([
      src('https://a.example', 'a'),
      src('http://b.example', 'b'),
      src('javascript:alert(1)', 'xss'),
      src('data:text/html,<script>', 'data'),
      src('ftp://c.example', 'ftp'),
      src('not a url', 'bad'),
    ])
    expect(links.map(l => l.url)).toEqual(['https://a.example', 'http://b.example'])
  })

  it('returns an empty array for undefined/empty input', () => {
    expect(safeSourceLinks(undefined)).toEqual([])
    expect(safeSourceLinks([])).toEqual([])
  })
})

describe('chat_source_schema', () => {
  it('fills defaults for a minimal source and drops undefined optionals on serialize', () => {
    const p = ChatSourceSchema.parse({ url: 'https://a.example' })
    expect(p.title).toBe('')
    expect(p.snippet).toBe('')
    expect(p.last_updated).toBeUndefined()
    const ser = JSON.stringify(p)
    expect(ser).toContain('https://a.example')
    expect(ser).not.toContain('last_updated')
  })
})
