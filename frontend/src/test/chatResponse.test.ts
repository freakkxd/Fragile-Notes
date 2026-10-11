import { describe, expect, it } from 'vitest'
import { extractChatText } from '../components/AIChatFull'

describe('chat_response_json_and_text_handling', () => {
  it('extracts content from legacy OpenAI-shape JSON', () => {
    const raw = JSON.stringify({ choices: [{ message: { content: 'hello world' } }] })
    expect(extractChatText(raw)).toBe('hello world')
  })

  it('passes normalized cloud text through unchanged', () => {
    expect(extractChatText('plain assistant text')).toBe('plain assistant text')
  })

  it('falls back to raw text when JSON has no content path', () => {
    const raw = JSON.stringify({ choices: [{ message: {} }] })
    expect(extractChatText(raw)).toBe(raw)
    expect(extractChatText('not json at all {{{')).toBe('not json at all {{{')
  })

  it('never throws on garbage input', () => {
    expect(() => extractChatText('')).not.toThrow()
    expect(() => extractChatText(null)).not.toThrow()
    expect(() => extractChatText(undefined)).not.toThrow()
    expect(() => extractChatText(42)).not.toThrow()
  })
})
