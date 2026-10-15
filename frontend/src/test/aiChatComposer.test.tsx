// @vitest-environment jsdom
// Behaviour of the ported reference composer: draft retention on blocked
// sends, Enter vs Shift+Enter, IME guard, keyboard menus, exact identifiers
// and the absence of unsupported (voice / attachment / effort) controls.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import AIChatFull from '../components/AIChatFull'
import { installTauriMock, tauriMockCalls } from './tauriMock'

afterEach(() => {
  cleanup()
})

function provider(over: Record<string, unknown> = {}) {
  return {
    id: 'local', name: 'Local', kind: 'local_llama_cpp', enabled: true,
    endpoint: 'http://127.0.0.1:8010', auth: null, default_model: '', model: '', extra: {},
    ...over,
  }
}

function reportCfg(providers: unknown[], privacy?: string) {
  return {
    schema_version: 2,
    providers,
    models: [],
    runtime_profiles: [],
    task_profiles: privacy ? [{ id: 'task-chat', name: 'Chat', model_ref: '', privacy }] : [],
    local: {
      binary_path: 'llama-server', models_dir: '/tmp/Models', active_model: '',
      n_ctx: 8192, n_threads: 0, n_gpu_layers: 0, temp: 0.7, top_p: 0.9,
      top_k: 40, repeat_penalty: 1.1, port: 8010, auto_start: false, use_mmap: true,
    },
    pipelines: [],
    active_pipeline: 'enrich',
  }
}

function report(providers: unknown[], privacy?: string) {
  return JSON.stringify({ status: 'valid', reason: null, issues: [], active_path: '/tmp/llm.json', candidates: [], config: reportCfg(providers, privacy) })
}

const CHAT_INPUT = 'Сообщение провайдеру…'

describe('composer_draft_retention', () => {
  it('keeps the typed draft when the send is blocked by the privacy guard', async () => {
    const user = userEvent.setup()
    let invoked = false
    installTauriMock({
      llm_get_config_report: report([
        provider(),
        provider({ id: 'openai', name: 'OpenAI', kind: 'open_ai', endpoint: 'https://api.openai.com/v1' }),
      ], 'local_only'),
      llm_models_list: JSON.stringify([{ id: 'm1', provider_id: 'local', filename: 'a.gguf', state: 'Present', size_bytes: 1 }]),
      llm_chat_universal: () => { invoked = true; return 'must not happen' },
    })
    render(<AIChatFull />)
    await user.click(await screen.findByLabelText('Провайдер'))
    await user.click(await screen.findByRole('option', { name: /OpenAI \(cloud\)/ }))

    const ta = screen.getByPlaceholderText(CHAT_INPUT) as HTMLTextAreaElement
    await user.type(ta, 'keep me')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))

    expect(await screen.findByText(/Отправка заблокирована/)).toBeTruthy()
    expect(ta.value).toBe('keep me')
    expect(invoked).toBe(false)
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_chat_universal')).toBe(false)
  })
})

describe('composer_enter_semantics', () => {
  it('sends on Enter', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: report([provider()]),
      llm_models_list: JSON.stringify([{ id: 'm1', provider_id: 'local', filename: 'a.gguf', state: 'Present', size_bytes: 1 }]),
      llm_chat_universal: () => 'pong',
    })
    render(<AIChatFull />)
    const ta = await screen.findByPlaceholderText(CHAT_INPUT)
    await user.type(ta, 'ping{Enter}')
    expect(await screen.findByText('pong')).toBeTruthy()
  })

  it('Shift+Enter inserts a newline instead of sending', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: report([provider()]),
      llm_models_list: JSON.stringify([{ id: 'm1', provider_id: 'local', filename: 'a.gguf', state: 'Present', size_bytes: 1 }]),
      llm_chat_universal: () => 'should not run',
    })
    render(<AIChatFull />)
    const ta = screen.getByPlaceholderText(CHAT_INPUT) as HTMLTextAreaElement
    await user.type(ta, 'a{Shift>}{Enter}{/Shift}b')
    expect(ta.value).toBe('a\nb')
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_chat_universal')).toBe(false)
  })
})

describe('composer_ime_guard', () => {
  it('does not send while an IME composition is active', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: report([provider()]),
      llm_models_list: JSON.stringify([{ id: 'm1', provider_id: 'local', filename: 'a.gguf', state: 'Present', size_bytes: 1 }]),
      llm_chat_universal: () => 'late',
    })
    render(<AIChatFull />)
    const ta = await screen.findByPlaceholderText(CHAT_INPUT)
    await user.type(ta, 'draft')

    fireEvent.keyDown(ta, { key: 'Enter', isComposing: true })
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_chat_universal')).toBe(false)

    fireEvent.keyDown(ta, { key: 'Enter', isComposing: false })
    await waitFor(() => expect(tauriMockCalls().some((c) => c.cmd === 'llm_chat_universal')).toBe(true))
  })
})

describe('composer_menu_keyboard', () => {
  it('opens with ArrowDown, moves and commits with Enter, returns focus', async () => {
    installTauriMock({
      llm_get_config_report: report([
        provider(),
        provider({ id: 'openai', name: 'OpenAI', kind: 'open_ai', endpoint: 'https://api.openai.com/v1' }),
      ]),
      llm_models_list: JSON.stringify([{ id: 'm1', provider_id: 'local', filename: 'a.gguf', state: 'Present', size_bytes: 1 }]),
    })
    render(<AIChatFull />)
    const trigger = await screen.findByLabelText('Провайдер')
    trigger.focus()

    await userEvent.setup().keyboard('{ArrowDown}')
    expect(screen.getByRole('option', { name: /Local \(local\)/ })).toBeTruthy()
    await userEvent.setup().keyboard('{ArrowDown}{Enter}')
    expect(trigger.textContent).toContain('OpenAI')
    expect(screen.queryByRole('option')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('closes on Escape and returns focus to the trigger', async () => {
    installTauriMock({
      llm_get_config_report: report([provider()]),
      llm_models_list: JSON.stringify([{ id: 'm1', provider_id: 'local', filename: 'a.gguf', state: 'Present', size_bytes: 1 }]),
    })
    render(<AIChatFull />)
    const trigger = await screen.findByLabelText('Провайдер')
    trigger.focus()
    const user = userEvent.setup()
    await user.keyboard('{Enter}')
    expect(screen.getByRole('option', { name: /Local \(local\)/ })).toBeTruthy()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('option')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })
})

describe('composer_exact_local_id_sent', () => {
  it('sends the registry id of the model chosen in the menu', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: report([provider()]),
      llm_models_list: JSON.stringify([
        { id: 'model-aaa', provider_id: 'local', filename: 'aaa.gguf', state: 'Present', size_bytes: 1 },
        { id: 'model-bbb', provider_id: 'local', filename: 'bbb.gguf', state: 'Present', size_bytes: 1 },
      ]),
      llm_chat_universal: () => 'ok',
    })
    render(<AIChatFull />)
    await user.click(await screen.findByRole('combobox', { name: 'Модель' }))
    await user.click(await screen.findByRole('option', { name: 'bbb.gguf' }))
    await user.type(screen.getByPlaceholderText(CHAT_INPUT), 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))

    const call = tauriMockCalls().find((c) => c.cmd === 'llm_chat_universal')
    expect(call?.args?.model).toBe('model-bbb')
  })
})

describe('composer_layout_structure', () => {
  // Structural regression for the layout fix: the chat root fills the flex
  // parent (flex:1/min-height:0) instead of a fixed 100% that overflowed the
  // status bar, and the card keeps its natural height (no inline pixel height
  // that could clip the textarea / send button). Geometry itself is verified
  // in a real engine, not here.
  it('roots the chat in a flex-filling container and keeps the card natural-height', async () => {
    installTauriMock({
      llm_get_config_report: report([provider()]),
      llm_models_list: JSON.stringify([{ id: 'm1', provider_id: 'local', filename: 'a.gguf', state: 'Present', size_bytes: 1 }]),
    })
    render(<AIChatFull />)
    await screen.findByLabelText('Провайдер')

    const card = document.querySelector('.ai-card') as HTMLElement
    expect(card).toBeTruthy()
    expect(card.style.height).toBe('')

    const root = document.querySelector('.ai-chat-head')?.parentElement as HTMLElement
    expect(root.style.flex).toContain('1')
    expect(root.style.height).toBe('')
    expect(/^0/.test(root.style.minHeight)).toBe(true)
  })

  it('keeps provider, model and send controls inside the card', async () => {
    installTauriMock({
      llm_get_config_report: report([provider()]),
      llm_models_list: JSON.stringify([{ id: 'm1', provider_id: 'local', filename: 'a.gguf', state: 'Present', size_bytes: 1 }]),
    })
    render(<AIChatFull />)
    await screen.findByLabelText('Провайдер')

    const card = document.querySelector('.ai-card') as HTMLElement
    expect(card.contains(screen.getByLabelText('Провайдер'))).toBe(true)
    expect(card.contains(screen.getByLabelText('Модель'))).toBe(true)
    expect(card.contains(screen.getByRole('button', { name: 'Отправить' }))).toBe(true)
  })
})

describe('composer_unsupported_controls_absent', () => {
  it('renders no voice, attachment or effort controls', async () => {
    installTauriMock({
      llm_get_config_report: report([provider()]),
      llm_models_list: JSON.stringify([{ id: 'm1', provider_id: 'local', filename: 'a.gguf', state: 'Present', size_bytes: 1 }]),
    })
    render(<AIChatFull />)
    await screen.findByLabelText('Провайдер')
    expect(screen.queryByRole('button', { name: /голос|микрофон|voice|mic|запис/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /прикреп|вложение|attach/i })).toBeNull()
    expect(document.querySelector('input[type="file"]')).toBeNull()
    expect(screen.queryByText(/усилие|усилия|effort/i)).toBeNull()
  })
})
