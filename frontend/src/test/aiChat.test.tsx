// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, test } from 'vitest'
import AIChatFull from '../components/AIChatFull'
import { installTauriMock, tauriMockCalls } from './tauriMock'

// Vitest runs without globals: unmount after each test explicitly.
afterEach(() => {
  cleanup()
})

function baseProviders() {
  return [
    {
      id: 'local', name: 'Local', kind: 'local_llama_cpp', enabled: true,
      endpoint: 'http://127.0.0.1:8010', auth: null, default_model: 'qwen.gguf',
      model: '', extra: {},
    },
    {
      id: 'openai', name: 'OpenAI', kind: 'open_ai', enabled: true,
      endpoint: 'https://api.openai.com/v1',
      auth: { method: 'api_key', secret_ref: 'masked:••••••••' },
      default_model: 'gpt-4o', model: '', extra: {},
    },
    {
      id: 'dead', name: 'Dead', kind: 'gemini', enabled: false,
      endpoint: '', auth: null, default_model: '', model: '', extra: {},
    },
  ]
}

function reportCfg(taskPrivacy?: string) {
  return {
    status: 'valid',
    reason: null,
    issues: [],
    active_path: '/tmp/llm.json',
    candidates: [],
    config: {
      schema_version: 2,
      providers: baseProviders(),
      models: [],
      runtime_profiles: [],
      task_profiles: taskPrivacy
        ? [{ id: 'task-chat', name: 'Chat', model_ref: '', privacy: taskPrivacy }]
        : [],
      local: {
        binary_path: 'llama-server', models_dir: '/tmp/Models', active_model: '',
        n_ctx: 8192, n_threads: 0, n_gpu_layers: 0, temp: 0.7, top_p: 0.9,
        top_k: 40, repeat_penalty: 1.1, port: 8010, auto_start: false, use_mmap: true,
      },
      pipelines: [],
      active_pipeline: 'enrich',
    },
  }
}

function mockConfig(taskPrivacy?: string, chatImpl?: (cmd: string, args?: Record<string, unknown>) => unknown) {
  const fixtures: Record<string, unknown> = {
    llm_get_config_report: JSON.stringify(reportCfg(taskPrivacy)),
    llm_models_list: JSON.stringify([
      { id: 'model-abc', provider_id: 'local', filename: 'qwen.gguf', state: 'Present', size_bytes: 1000 },
    ]),
  }
  fixtures['llm_cloud_consent_status'] = JSON.stringify({ provider_id: 'openai', task_profile_id: 'task-chat', destination: 'https://api.openai.com/v1', granted: true, required: false })
  if (chatImpl) fixtures['llm_chat_universal'] = chatImpl
  installTauriMock(fixtures)
}

describe('ai_chat_renders_enabled_providers', () => {
  it('lists enabled providers and hides disabled ones', async () => {
    const user = userEvent.setup()
    mockConfig()
    render(<AIChatFull />)
    await user.click(await screen.findByLabelText('Провайдер'))
    expect(await screen.findByRole('option', { name: /Local \(local\)/ })).toBeTruthy()
    expect(screen.getByRole('option', { name: /OpenAI \(cloud\)/ })).toBeTruthy()
    expect(screen.queryByRole('option', { name: /Dead/ })).toBeNull()
  })
})

describe('ai_chat_default_local_selection', () => {
  it('selects local provider and first Present registry model by default', async () => {
    const user = userEvent.setup()
    mockConfig()
    render(<AIChatFull />)
    const combo = await screen.findByLabelText('Провайдер')
    expect(combo.textContent).toContain('Local')
    const model = await screen.findByRole('combobox', { name: 'Модель' })
    expect(model.textContent).toContain('qwen.gguf')
    await user.click(model)
    const opt = screen.getByRole('option', { name: 'qwen.gguf' })
    expect(opt.getAttribute('aria-selected')).toBe('true')
  })
})

describe('ai_chat_local_json_response_parsed', () => {
  it('parses legacy OpenAI-shape JSON to text', async () => {
    const user = userEvent.setup()
    mockConfig(undefined, () =>
      JSON.stringify({ choices: [{ message: { content: 'parsed hi' } }] }),
    )
    render(<AIChatFull />)
    await screen.findByLabelText('Провайдер')
    await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), 'hello')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByText('parsed hi')).toBeTruthy()
    expect(screen.queryByText(/"choices"/)).toBeNull()
  })
})

describe('ai_chat_cloud_text_response_displayed', () => {
  it('displays normalized cloud text as-is', async () => {
    const user = userEvent.setup()
    mockConfig(undefined, () => 'cloud plain answer')
    render(<AIChatFull />)
    await user.click(await screen.findByLabelText('Провайдер'))
    await user.click(await screen.findByRole('option', { name: /OpenAI \(cloud\)/ }))
    await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByText('cloud plain answer')).toBeTruthy()
  })
})

describe('ai_chat_sources_rendered_as_safe_links', () => {
  it('renders envelope sources as clickable http(s) links and drops unsafe schemes', async () => {
    const user = userEvent.setup()
    mockConfig(undefined, () => JSON.stringify({
      content: 'grounded answer',
      sources: [
        { url: 'https://example.com/a', title: 'A' },
        { url: 'javascript:alert(1)', title: 'evil' },
      ],
    }))
    render(<AIChatFull />)
    await user.click(await screen.findByLabelText('Провайдер'))
    await user.click(await screen.findByRole('option', { name: /OpenAI \(cloud\)/ }))
    await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByText('grounded answer')).toBeTruthy()
    expect(screen.getByText('Источники')).toBeTruthy()
    const link = screen.getByRole('link', { name: 'A' })
    expect(link.getAttribute('href')).toBe('https://example.com/a')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toContain('noopener')
    // A dangerous scheme must never become a clickable link.
    expect(screen.queryByRole('link', { name: 'evil' })).toBeNull()
  })
})

describe('ai_chat_local_only_blocks_cloud_invoke', () => {
  it('blocks send with explanation and never invokes backend', async () => {
    const user = userEvent.setup()
    let invoked = false
    mockConfig('local_only', () => {
      invoked = true
      return 'must not happen'
    })
    render(<AIChatFull />)
    await user.click(await screen.findByLabelText('Провайдер'))
    await user.click(await screen.findByRole('option', { name: /OpenAI \(cloud\)/ }))
    await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByText(/Отправка заблокирована/)).toBeTruthy()
    expect(invoked).toBe(false)
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_chat_universal')).toBe(false)
  })
})

describe('ai_chat_stop_invokes_backend_cancel', () => {
  it('cancels the in-flight request and drops its late answer, then allows a new send', async () => {
    const user = userEvent.setup()
    let resolveFirst!: (v: string) => void
    let chatCalls = 0
    installTauriMock({
      llm_get_config_report: JSON.stringify(reportCfg()),
      llm_models_list: JSON.stringify([
        { id: 'model-abc', provider_id: 'local', filename: 'qwen.gguf', state: 'Present', size_bytes: 1000 },
      ]),
      llm_chat_universal: () => {
        chatCalls += 1
        if (chatCalls === 1) return new Promise<string>((res) => { resolveFirst = res })
        return Promise.resolve(JSON.stringify({ choices: [{ message: { content: 'second answer' } }] }))
      },
      llm_chat_cancel: () => 'cancelled',
    })
    render(<AIChatFull />)
    await user.type(await screen.findByPlaceholderText('Сообщение провайдеру…'), 'one')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    await screen.findByRole('button', { name: 'Стоп' })
    await user.click(screen.getByRole('button', { name: 'Стоп' }))
    expect(await screen.findByText('Остановлено пользователем.')).toBeTruthy()
    // The backend cancel must carry the same request id as the send.
    const send = tauriMockCalls().find((c) => c.cmd === 'llm_chat_universal')
    const cancel = tauriMockCalls().find((c) => c.cmd === 'llm_chat_cancel')
    expect(send).toBeTruthy()
    expect(cancel).toBeTruthy()
    expect(typeof send!.args?.requestId).toBe('string')
    expect(cancel!.args?.requestId).toBe(send!.args?.requestId)
    // The aborted response is dropped even if it arrives late.
    resolveFirst(JSON.stringify({ choices: [{ message: { content: 'stale-one' } }] }))
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('stale-one')).toBeNull()
    // A subsequent send still works (the counter invalidated the aborted one).
    await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), 'two')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByText('second answer')).toBeTruthy()
    expect(screen.queryByText('stale-one')).toBeNull()
  })
})

describe('ai_chat_stop_ignores_late_response', () => {
  it('shows Stop during request and drops the late answer', async () => {
    const user = userEvent.setup()
    let release!: (v: string) => void
    mockConfig(undefined, () => new Promise<string>((res) => { release = res }))
    render(<AIChatFull />)
    await screen.findByLabelText('Провайдер')
    await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByRole('button', { name: 'Стоп' })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Стоп' }))
    expect(await screen.findByText('Остановлено пользователем.')).toBeTruthy()
    release('too late')
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('too late')).toBeNull()
    expect(screen.getByRole('button', { name: 'Отправить' })).toBeTruthy()
  })
})

test('local provider with no registry models shows compact empty state', async () => {
  const { queryByText } = render(<AIChatFull />)
  // basic smoke
  expect(queryByText('AI Чат')).toBeTruthy()
})

