// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import LLMSettings from '../components/LLMSettings'
import { installTauriMock, tauriMockCalls } from './tauriMock'

afterEach(() => {
  cleanup()
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function report() {
  return JSON.stringify({
    status: 'valid', reason: null, issues: [], active_path: '/tmp/llm.json',
    config: {
      schema_version: 2,
      providers: [
        {
          id: 'or', name: 'OpenRouter', kind: 'open_router', enabled: true,
          endpoint: 'https://openrouter.ai/api/v1',
          auth: { method: 'api_key', secret_ref: '' },
          default_model: 'openai/gpt-4o', model: '', extra: {},
        },
      ],
      models: [],
      runtime_profiles: [],
      task_profiles: [{ id: 'task-chat', name: 'Chat', model_ref: '', privacy: 'cloud_allowed' }],
      local: {
        binary_path: 'llama-server', models_dir: '/tmp/Models', active_model: '',
        n_ctx: 8192, n_threads: 0, n_gpu_layers: 0, temp: 0.7, top_p: 0.9,
        top_k: 40, repeat_penalty: 1.1, port: 8010, auto_start: false, use_mmap: true,
      },
      pipelines: [], active_pipeline: 'enrich',
    },
  })
}

const baseFixtures = () => ({
  llm_get_config_report: report(),
  llm_scan_models: '[]',
  llm_has_provider_credential: 'false',
})

async function openOauthCard(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'Провайдеры' }))
  await user.click(await screen.findByRole('button', { name: 'Настроить ›' }))
  await screen.findByRole('button', { name: 'Подключить через браузер' })
}

describe('openrouter_oauth_poll_lifecycle', () => {
  it('never polls a superseded start transaction', async () => {
    const user = userEvent.setup()
    const first = deferred<string>()
    const second = deferred<string>()
    let startCalls = 0
    const statusTx: string[] = []
    installTauriMock({
      ...baseFixtures(),
      llm_openrouter_oauth_start: () => {
        startCalls += 1
        return startCalls === 1 ? first.promise : second.promise
      },
      llm_openrouter_oauth_status: (_cmd: string, args?: Record<string, unknown>) => {
        statusTx.push(String(args?.['txId']))
        return JSON.stringify({ tx_id: args?.['txId'], status: 'pending', provider_id: 'or', destination: '', error: null })
      },
    })
    render(<LLMSettings />)
    await openOauthCard(user)

    // Two rapid connects: the second must invalidate the first.
    await user.click(screen.getByRole('button', { name: 'Подключить через браузер' }))
    await user.click(screen.getByRole('button', { name: 'Подключить через браузер' }))
    expect(startCalls).toBe(2)

    // Resolve the superseded start while the live one is still pending. If the
    // generation guard were absent this would schedule a poll for tx-stale.
    await act(async () => { first.resolve(JSON.stringify({ tx_id: 'tx-stale', auth_url: 'https://a', port: 1 })) })
    // Bounded wait past the poll interval to prove the stale poll never fires.
    await new Promise((r) => setTimeout(r, 1500))
    expect(statusTx).not.toContain('tx-stale')

    await act(async () => { second.resolve(JSON.stringify({ tx_id: 'tx-live', auth_url: 'https://a', port: 1 })) })
    // The live transaction starts polling; the stale one was never polled.
    await waitFor(() => expect(statusTx).toContain('tx-live'), { timeout: 4000 })
    expect(statusTx).not.toContain('tx-stale')
  })

  it('cancel invalidates an in-flight status so a late result cannot connect', async () => {
    const user = userEvent.setup()
    const pending = deferred<string>()
    let statusCalls = 0
    installTauriMock({
      ...baseFixtures(),
      llm_openrouter_oauth_start: JSON.stringify({ tx_id: 'tx-1', auth_url: 'https://a', port: 1 }),
      llm_openrouter_oauth_status: () => { statusCalls += 1; return pending.promise },
      llm_openrouter_oauth_cancel: 'cancelled',
    })
    render(<LLMSettings />)
    await openOauthCard(user)

    await user.click(screen.getByRole('button', { name: 'Подключить через браузер' }))
    // Wait for the poll tick to fire and issue an in-flight status request.
    await waitFor(() => expect(statusCalls).toBe(1), { timeout: 4000 })

    // Cancel while the status request is still unresolved.
    await user.click(screen.getByRole('button', { name: 'Отмена' }))
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_openrouter_oauth_cancel')).toBe(true)

    // A late "connected" for the cancelled tx must be dropped by the gen guard.
    await act(async () => {
      pending.resolve(JSON.stringify({ tx_id: 'tx-1', status: 'connected', provider_id: 'or', destination: '', error: null }))
    })
    await waitFor(() => expect(screen.queryByText('Подключено')).toBeNull())
    expect(screen.getByText('Не подключено')).toBeTruthy()
  })
})
