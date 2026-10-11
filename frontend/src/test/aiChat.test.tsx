// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
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
  if (chatImpl) fixtures['llm_chat_universal'] = chatImpl
  installTauriMock(fixtures)
}

describe('ai_chat_renders_enabled_providers', () => {
  it('lists enabled providers and hides disabled ones', async () => {
    mockConfig()
    render(<AIChatFull />)
    expect(await screen.findByRole('option', { name: /Local \(local\)/ })).toBeTruthy()
    expect(screen.getByRole('option', { name: /OpenAI \(cloud\)/ })).toBeTruthy()
    expect(screen.queryByRole('option', { name: /Dead/ })).toBeNull()
  })
})

describe('ai_chat_default_local_selection', () => {
  it('selects local provider and first Present registry model by default', async () => {
    mockConfig()
    render(<AIChatFull />)
    const combo = (await screen.findByLabelText('Провайдер')) as HTMLSelectElement
    expect(combo.value).toBe('local')
    const model = screen.getByLabelText('Модель') as HTMLSelectElement
    expect(model.value).toBe('model-abc')
    expect(screen.getByRole('option', { name: 'qwen.gguf' })).toBeTruthy()
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
    const combo = (await screen.findByLabelText('Провайдер')) as HTMLSelectElement
    await user.selectOptions(combo, 'openai')
    await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByText('cloud plain answer')).toBeTruthy()
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
    const combo = (await screen.findByLabelText('Провайдер')) as HTMLSelectElement
    await user.selectOptions(combo, 'openai')
    await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByText(/Отправка заблокирована/)).toBeTruthy()
    expect(invoked).toBe(false)
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_chat_universal')).toBe(false)
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
