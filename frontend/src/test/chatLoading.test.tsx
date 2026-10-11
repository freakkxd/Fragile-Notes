// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import AIChatFull from '../components/AIChatFull'
import { installTauriMock } from './tauriMock'

// Vitest runs without globals: unmount after each test explicitly.
afterEach(() => {
  cleanup()
})

function cfgWithProviders(providers: unknown[]) {
  return {
    schema_version: 2,
    providers,
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

function localProvider() {
  return {
    id: 'local', name: 'Local', kind: 'local_llama_cpp', enabled: true,
    endpoint: 'http://127.0.0.1:8010', auth: null, default_model: '',
    model: '', extra: {},
  }
}

function cloudProvider() {
  return {
    id: 'openai', name: 'OpenAI', kind: 'open_ai', enabled: true,
    endpoint: 'https://api.openai.com/v1', auth: null, default_model: 'gpt-4o',
    model: '', extra: {},
  }
}

function reportFor(providers: unknown[]) {
  return JSON.stringify({
    status: 'valid', reason: null, issues: [], active_path: '/tmp/llm.json',
    candidates: [], config: cfgWithProviders(providers),
  })
}

function deferred(): { promise: Promise<string>; resolve: (v: string) => void; reject: (e: unknown) => void } {
  let resolve!: (v: string) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<string>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

describe('chat_loading_no_premature_cloud_input', () => {
  it('shows neutral loading state before config resolves', async () => {
    const cfg = deferred()
    const reg = deferred()
    installTauriMock({
      llm_get_config_report: () => cfg.promise,
      llm_models_list: () => reg.promise,
    })
    render(<AIChatFull />)
    // Deferred promises are still pending: no provider-shaped controls.
    expect(screen.queryByPlaceholderText('модель (пусто = default провайдера)')).toBeNull()
    expect(screen.queryByRole('combobox', { name: 'Модель' })).toBeNull()
    // Both the disabled provider select and the model placeholder show it.
    expect(screen.getAllByText('Загрузка…').length).toBe(2)
    const prov = screen.getByLabelText('Провайдер') as HTMLSelectElement
    expect(prov.disabled).toBe(true)
    cfg.resolve(reportFor([localProvider()]))
    reg.resolve(JSON.stringify([]))
    // Local with empty registry: hint, still no cloud input.
    expect(await screen.findByText(/Сканировать реестр/)).toBeTruthy()
    expect(screen.queryByPlaceholderText('модель (пусто = default провайдера)')).toBeNull()
  })
})

describe('chat_loading_local_registry_select', () => {
  it('shows registry select after local config resolves, never cloud input', async () => {
    const cfg = deferred()
    const reg = deferred()
    installTauriMock({
      llm_get_config_report: () => cfg.promise,
      llm_models_list: () => reg.promise,
    })
    render(<AIChatFull />)
    expect(screen.queryByRole('combobox', { name: 'Модель' })).toBeNull()
    cfg.resolve(reportFor([localProvider()]))
    reg.resolve(JSON.stringify([
      { id: 'm1', provider_id: 'local', filename: 'a.gguf', state: 'Present', size_bytes: 1 },
    ]))
    const combo = (await screen.findByRole('combobox', { name: 'Модель' })) as HTMLSelectElement
    expect(combo.value).toBe('m1')
    expect(screen.queryByPlaceholderText('модель (пусто = default провайдера)')).toBeNull()
  })
})

describe('chat_loading_cloud_input', () => {
  it('shows cloud input after cloud config resolves', async () => {
    const cfg = deferred()
    const reg = deferred()
    installTauriMock({
      llm_get_config_report: () => cfg.promise,
      llm_models_list: () => reg.promise,
    })
    render(<AIChatFull />)
    expect(screen.queryByPlaceholderText('модель (пусто = default провайдера)')).toBeNull()
    cfg.resolve(reportFor([cloudProvider()]))
    reg.resolve(JSON.stringify([]))
    const input = (await screen.findByPlaceholderText('модель (пусто = default провайдера)')) as HTMLInputElement
    expect(input.value).toBe('gpt-4o')
    expect(screen.queryByRole('combobox', { name: 'Модель' })).toBeNull()
  })
})

describe('chat_loading_error_visible_with_retry', () => {
  it('shows error and recovers via retry, never a fake cloud control', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: () => Promise.reject(new Error('backend down')),
      llm_models_list: () => Promise.resolve(JSON.stringify([])),
    })
    render(<AIChatFull />)
    expect(await screen.findByText(/Не удалось загрузить конфигурацию/)).toBeTruthy()
    expect(screen.queryByPlaceholderText('модель (пусто = default провайдера)')).toBeNull()
    expect(screen.queryByRole('combobox', { name: 'Модель' })).toBeNull()
    // Retry with a working backend recovers to the local select.
    installTauriMock({
      llm_get_config_report: reportFor([localProvider()]),
      llm_models_list: JSON.stringify([
        { id: 'm1', provider_id: 'local', filename: 'a.gguf', state: 'Present', size_bytes: 1 },
      ]),
    })
    await user.click(screen.getByRole('button', { name: '↻ Повторить' }))
    expect(await screen.findByRole('combobox', { name: 'Модель' })).toBeTruthy()
    expect(screen.queryByText(/Не удалось загрузить конфигурацию/)).toBeNull()
  })
})
