// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import AIChatFull from '../components/AIChatFull'
import LLMSettings from '../components/LLMSettings'
import { installTauriMock, tauriMockCalls } from './tauriMock'

// Vitest runs without globals: unmount after each test explicitly.
afterEach(() => {
  cleanup()
})

function rec(id: string, providerId: string, filename: string, state: string) {
  return { id, provider_id: providerId, filename, state, size_bytes: 1000 }
}

function mixedRegistry() {
  return [
    rec('model-abc', 'local', 'qwen.gguf', 'Present'),
    rec('model-def', 'local', 'second.gguf', 'Present'),
    rec('model-miss', 'local', 'gone.gguf', 'Missing'),
    rec('model-inv', 'local', 'broken.gguf', 'Invalid'),
    rec('model-chg', 'local', 'changed.gguf', 'Changed'),
    rec('model-unv', 'local', 'new.gguf', 'Unverified'),
    rec('other: m', 'openai', 'cloud-only.gguf', 'Present'),
  ]
}

function chatConfig() {
  return {
    schema_version: 2,
    providers: [
      {
        id: 'local', name: 'Local', kind: 'local_llama_cpp', enabled: true,
        endpoint: 'http://127.0.0.1:8010', auth: null, default_model: '',
        model: '', extra: {},
      },
      {
        id: 'openai', name: 'OpenAI', kind: 'open_ai', enabled: true,
        endpoint: 'https://api.openai.com/v1', auth: null, default_model: 'gpt-4o',
        model: '', extra: {},
      },
    ],
    models: [],
    runtime_profiles: [],
    task_profiles: [],
    local: {
      binary_path: 'llama-server', models_dir: '/tmp/Models', active_model: '',
      n_ctx: 8192, n_threads: 0, n_gpu_layers: 0, temp: 0.7, top_p: 0.9,
      top_k: 40, repeat_penalty: 1.1, port: 8010, auto_start: false, use_mmap: true,
    },
    pipelines: [],
    active_pipeline: 'enrich',
  }
}

function mockChat(registry: unknown[]) {
  installTauriMock({
    llm_get_config_report: JSON.stringify({
      status: 'valid', reason: null, issues: [], active_path: '/tmp/llm.json',
      candidates: [], config: chatConfig(),
    }),
    llm_models_list: JSON.stringify(registry),
  })
}

describe('registry_present_onlySelectable', () => {
  it('offers only Present records of the selected provider', async () => {
    mockChat(mixedRegistry())
    render(<AIChatFull />)
    expect(await screen.findByRole('option', { name: 'qwen.gguf' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'second.gguf' })).toBeTruthy()
  })
})

describe('registry_missing_invalid_unselectable', () => {
  it('hides Missing/Invalid/Changed/Unverified and foreign-provider records', async () => {
    mockChat(mixedRegistry())
    render(<AIChatFull />)
    await screen.findByRole('option', { name: 'qwen.gguf' })
    for (const name of ['gone.gguf', 'broken.gguf', 'changed.gguf', 'new.gguf', 'cloud-only.gguf']) {
      expect(screen.queryByRole('option', { name })).toBeNull()
    }
  })
})

describe('registry_empty_shows_scan_hint', () => {
  it('shows scan hint and blocks send when no eligible models exist', async () => {
    const user = userEvent.setup()
    let invoked = false
    mockChat([])
    installTauriMock({
      llm_get_config_report: JSON.stringify({
        status: 'valid', reason: null, issues: [], active_path: '/tmp/llm.json',
        candidates: [], config: chatConfig(),
      }),
      llm_models_list: JSON.stringify([]),
      llm_chat_universal: () => {
        invoked = true
        return 'must not happen'
      },
    })
    render(<AIChatFull />)
    await screen.findByLabelText('Провайдер')
    expect(await screen.findByText(/Сканировать реестр/)).toBeTruthy()
    await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    // Banner uses a colon; the header hint uses an em dash.
    expect(await screen.findByText(/Нет установленных моделей:/)).toBeTruthy()
    expect(invoked).toBe(false)
  })
})

describe('cloud_remote_not_filesystem_path', () => {
  it('passes cloud remote ids through unchanged', async () => {
    const user = userEvent.setup()
    mockChat([])
    render(<AIChatFull />)
    const combo = (await screen.findByLabelText('Провайдер')) as HTMLSelectElement
    await user.selectOptions(combo, 'openai')
    const model = screen.getByLabelText('Модель') as HTMLInputElement
    await user.clear(model)
    await user.type(model, 'models/gemini-2.0-flash')
    await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), 'hi')
    // Mock the chat call now (typed after selection to keep fixture simple).
    installTauriMock({
      llm_get_config_report: JSON.stringify({
        status: 'valid', reason: null, issues: [], active_path: '/tmp/llm.json',
        candidates: [], config: chatConfig(),
      }),
      llm_models_list: JSON.stringify([]),
      llm_chat_universal: 'cloud ok',
    })
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByText('cloud ok')).toBeTruthy()
    const call = tauriMockCalls().find((c) => c.cmd === 'llm_chat_universal')
    expect(call?.args?.['model']).toBe('models/gemini-2.0-flash')
  })
})

describe('download_button_disabled_no_legacy_invoke', () => {
  it('shows disabled manager button and never calls legacy download', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: JSON.stringify({
        status: 'valid', reason: null, issues: [], active_path: '/tmp/llm.json',
        candidates: [], config: chatConfig(),
      }),
      llm_scan_models: JSON.stringify([]),
      llm_models_scan: JSON.stringify({ discovered: [], added: [], updated: [], missing: [], invalid: [] }),
      llm_models_list: JSON.stringify([]),
    })
    render(<LLMSettings />)
    await user.click(await screen.findByRole('button', { name: 'Рантайм' }))
    const btn = (await screen.findByRole('button', { name: /Загрузка через менеджер/ })) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(btn.title).toMatch(/менеджер/)
    await user.click(btn).catch(() => {})
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_download_model')).toBe(false)
  })
})
