// @vitest-environment jsdom
// Ollama vertical slice: native discovery drives the chat model select and the
// settings model list. No GGUF registry is consulted for Ollama.
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import AIChatFull from '../components/AIChatFull'
import LLMSettings from '../components/LLMSettings'
import { installTauriMock, tauriMockCalls } from './tauriMock'

afterEach(() => {
  cleanup()
})

function provider(over: Record<string, unknown>) {
  return {
    id: 'provider', name: 'Provider', kind: 'ollama', enabled: true,
    endpoint: 'http://127.0.0.1:11434', auth: null, api_url: '', api_key: '',
    auth_method: 'none', model: '', default_model: '', extra: {},
    ...over,
  }
}

function reportCfg(providers: unknown[], taskPrivacy?: string) {
  return {
    status: 'valid', reason: null, issues: [], active_path: '/tmp/llm.json', candidates: [],
    config: {
      schema_version: 2,
      providers,
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

describe('ollama chat discovery', () => {
  it('selects the first discovered model and sends its identifier', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: JSON.stringify(reportCfg([provider({ id: 'ollama', name: 'Ollama' })])),
      llm_models_list: JSON.stringify([]),
      llm_discover_models: JSON.stringify({ ok: true, models: ['llama3.1:latest', 'qwen2.5:7b'] }),
      llm_chat_universal: () => JSON.stringify({ choices: [{ message: { content: 'ok' } }] }),
    })
    render(<AIChatFull />)
    const modelSelect = (await screen.findByRole('combobox', { name: 'Модель' })) as HTMLSelectElement
    expect(modelSelect.value).toBe('llama3.1:latest')
    expect(screen.getByRole('option', { name: 'qwen2.5:7b' })).toBeTruthy()
    await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    const call = tauriMockCalls().find((c) => c.cmd === 'llm_chat_universal')
    expect(call?.args?.model).toBe('llama3.1:latest')
  })

  it('does not use GGUF registry records for Ollama', async () => {
    installTauriMock({
      llm_get_config_report: JSON.stringify(reportCfg([provider({ id: 'ollama' })])),
      llm_models_list: JSON.stringify([
        { id: 'model-x', provider_id: 'ollama', filename: 'should-not-appear.gguf', state: 'Present', size_bytes: 1 },
      ]),
      llm_discover_models: JSON.stringify({ ok: true, models: ['llama3.1:latest'] }),
    })
    render(<AIChatFull />)
    await screen.findByRole('option', { name: 'llama3.1:latest' })
    expect(screen.queryByRole('option', { name: 'should-not-appear.gguf' })).toBeNull()
  })

  it('shows a safe error and retries on demand', async () => {
    const user = userEvent.setup()
    let first = true
    installTauriMock({
      llm_get_config_report: JSON.stringify(reportCfg([provider({ id: 'ollama' })])),
      llm_models_list: JSON.stringify([]),
      llm_discover_models: () => (first
        ? (first = false, JSON.stringify({ ok: false, error: { code: 'network', message: 'x', retryable: true } }))
        : JSON.stringify({ ok: true, models: ['llama3.1:latest'] })),
    })
    render(<AIChatFull />)
    await screen.findByText(/Сервер Ollama недоступен/)
    await user.click(screen.getByRole('button', { name: '↻ Обновить' }))
    expect(await screen.findByRole('option', { name: 'llama3.1:latest' })).toBeTruthy()
  })

  it('blocks send when the discovered list is empty', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: JSON.stringify(reportCfg([provider({ id: 'ollama' })])),
      llm_models_list: JSON.stringify([]),
      llm_discover_models: JSON.stringify({ ok: true, models: [] }),
    })
    render(<AIChatFull />)
    await screen.findByText(/Нет моделей —/)
    await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByText(/В Ollama нет моделей/)).toBeTruthy()
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_chat_universal')).toBe(false)
  })

  it('ignores a late discovery response after switching provider', async () => {
    const user = userEvent.setup()
    let release!: (v: string) => void
    const pending = new Promise<string>((res) => { release = res })
    installTauriMock({
      llm_get_config_report: JSON.stringify(reportCfg([
        provider({ id: 'ollama', name: 'Ollama' }),
        provider({ id: 'openai', name: 'OpenAI', kind: 'open_ai', endpoint: 'https://api.openai.com/v1' }),
      ])),
      llm_models_list: JSON.stringify([]),
      llm_discover_models: () => pending,
    })
    render(<AIChatFull />)
    const combo = (await screen.findByLabelText('Провайдер')) as HTMLSelectElement
    expect(combo.value).toBe('ollama')
    await user.selectOptions(combo, 'openai')
    release(JSON.stringify({ ok: true, models: ['late-model'] }))
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByRole('option', { name: 'late-model' })).toBeNull()
    expect(screen.getByPlaceholderText('модель (пусто = default провайдера)')).toBeTruthy()
  })
})

describe('ollama settings discovery', () => {
  it('lists discovered models and lets the user pick a default', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: JSON.stringify(reportCfg([provider({ id: 'ollama', name: 'Ollama' })])),
      llm_scan_models: JSON.stringify([]),
      llm_discover_models: JSON.stringify({ ok: true, models: ['llama3.1:latest'] }),
    })
    render(<LLMSettings />)
    await user.click(await screen.findByRole('button', { name: 'Провайдеры' }))
    await user.click(await screen.findByRole('button', { name: 'Настроить ›' }))
    await user.click(await screen.findByRole('button', { name: 'Получить модели Ollama' }))
    expect(await screen.findByText('llama3.1:latest')).toBeTruthy()
    const radio = screen.getByRole('radio') as HTMLInputElement
    await user.click(radio)
    expect(radio.checked).toBe(true)
  })

  it('surfaces a safe discovery error in settings', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: JSON.stringify(reportCfg([provider({ id: 'ollama' })])),
      llm_scan_models: JSON.stringify([]),
      llm_discover_models: JSON.stringify({ ok: false, error: { code: 'network', message: 'x', retryable: true } }),
    })
    render(<LLMSettings />)
    await user.click(await screen.findByRole('button', { name: 'Провайдеры' }))
    await user.click(await screen.findByRole('button', { name: 'Настроить ›' }))
    await user.click(await screen.findByRole('button', { name: 'Получить модели Ollama' }))
    expect(await screen.findByText(/Сервер Ollama недоступен/)).toBeTruthy()
  })
})
