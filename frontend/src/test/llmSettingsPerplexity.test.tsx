// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import LLMSettings from '../components/LLMSettings'
import { installTauriMock } from './tauriMock'

afterEach(() => {
  cleanup()
})

function report() {
  return JSON.stringify({
    status: 'valid', reason: null, issues: [], active_path: '/tmp/llm.json',
    config: {
      schema_version: 2,
      providers: [
        {
          id: 'pplx', name: 'Perplexity', kind: 'perplexity', enabled: true,
          endpoint: 'https://api.perplexity.ai',
          auth: { method: 'api_key', secret_ref: '' },
          default_model: 'low', model: '', extra: {},
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

async function openCard(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'Провайдеры' }))
  await user.click(await screen.findByRole('button', { name: 'Настроить ›' }))
}

describe('perplexity_settings_card', () => {
  it('shows the Agent API card, preset radios and the separate-billing note', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: report(),
      llm_scan_models: '[]',
      llm_has_provider_credential: 'false',
    })
    render(<LLMSettings />)
    await openCard(user)

    expect(await screen.findByText('Perplexity Agent API')).toBeTruthy()
    expect(screen.getByText(/покупается отдельно/)).toBeTruthy()
    // Endpoint placeholder mirrors the backend default endpoint.
    expect(screen.getByPlaceholderText('https://api.perplexity.ai')).toBeTruthy()
    // All five documented presets are offered, current default 'low' selected.
    for (const pr of ['fast', 'low', 'medium', 'high', 'xhigh']) {
      expect(screen.getByRole('radio', { name: pr })).toBeTruthy()
    }
    expect((screen.getByRole('radio', { name: 'low' }) as HTMLInputElement).checked).toBe(true)

    // Selecting another preset patches the provider default model.
    await user.click(screen.getByRole('radio', { name: 'medium' }))
    expect((screen.getByRole('radio', { name: 'medium' }) as HTMLInputElement).checked).toBe(true)
    expect((screen.getByRole('radio', { name: 'low' }) as HTMLInputElement).checked).toBe(false)
  })
})
