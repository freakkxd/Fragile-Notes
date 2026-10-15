// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import AIChatFull from '../components/AIChatFull'
import { installTauriMock, tauriMockCalls } from './tauriMock'

afterEach(() => {
  cleanup()
})

function providers() {
  return [
    {
      id: 'local', name: 'Local', kind: 'local_llama_cpp', enabled: true,
      endpoint: 'http://127.0.0.1:8010', auth: null, default_model: 'qwen.gguf',
      model: '', extra: {},
    },
    {
      id: 'openai', name: 'OpenAI', kind: 'open_ai', enabled: true,
      endpoint: 'https://api.openai.com/v1',
      auth: { method: 'api_key', secret_ref: '' },
      default_model: 'gpt-4o', model: '', extra: {},
    },
  ]
}

function cfg(privacy = 'cloud_allowed') {
  return {
    status: 'valid', reason: null, issues: [], active_path: '/tmp/llm.json',
    candidates: [],
    config: {
      schema_version: 2,
      providers: providers(),
      models: [],
      runtime_profiles: [],
      task_profiles: [{ id: 'task-chat', name: 'Chat', model_ref: '', privacy }],
      local: {
        binary_path: 'llama-server', models_dir: '/tmp/Models', active_model: '',
        n_ctx: 8192, n_threads: 0, n_gpu_layers: 0, temp: 0.7, top_p: 0.9,
        top_k: 40, repeat_penalty: 1.1, port: 8010, auto_start: false, use_mmap: true,
      },
      pipelines: [], active_pipeline: 'enrich',
    },
  }
}

function consentFixture(required: boolean) {
  return JSON.stringify({
    provider_id: 'openai', task_profile_id: 'task-chat',
    destination: 'https://api.openai.com/v1', granted: !required, required,
  })
}

async function selectCloudAndType(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.click(await screen.findByLabelText('Провайдер'))
  await user.click(await screen.findByRole('option', { name: /OpenAI \(cloud\)/ }))
  await user.type(screen.getByPlaceholderText('Сообщение провайдеру…'), text)
}

describe('cloud_consent_dialog', () => {
  it('cancel keeps the draft, sends nothing and creates no message', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: JSON.stringify(cfg()),
      llm_models_list: JSON.stringify([]),
      llm_cloud_consent_status: consentFixture(true),
      llm_cloud_consent_grant: 'granted',
      llm_chat_universal: () => { throw new Error('must not send on cancel') },
    })
    render(<AIChatFull />)
    await selectCloudAndType(user, 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    // Dialog appears; draft is preserved and nothing is dispatched.
    expect(await screen.findByRole('dialog', { name: /Согласие на отправку в облако/ })).toBeTruthy()
    expect((screen.getByPlaceholderText('Сообщение провайдеру…') as HTMLTextAreaElement).value).toBe('hi')
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_chat_universal')).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Отмена' }))
    expect(screen.queryByRole('dialog', { name: /Согласие на отправку в облако/ })).toBeNull()
    // Draft remains for a retry; no message was appended.
    expect((screen.getByPlaceholderText('Сообщение провайдеру…') as HTMLTextAreaElement).value).toBe('hi')
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_chat_universal')).toBe(false)
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_cloud_consent_grant')).toBe(false)
  })

  it('confirm grants once and sends exactly one message', async () => {
    const user = userEvent.setup()
    let chatCalls = 0
    installTauriMock({
      llm_get_config_report: JSON.stringify(cfg()),
      llm_models_list: JSON.stringify([]),
      llm_cloud_consent_status: consentFixture(true),
      llm_cloud_consent_grant: 'granted',
      llm_chat_universal: () => { chatCalls += 1; return 'cloud answer' },
    })
    render(<AIChatFull />)
    await selectCloudAndType(user, 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    await screen.findByRole('dialog', { name: /Согласие на отправку в облако/ })
    await user.click(screen.getByRole('button', { name: 'Разрешить и отправить' }))
    expect(await screen.findByText('cloud answer')).toBeTruthy()
    expect(chatCalls).toBe(1)
    const grants = tauriMockCalls().filter((c) => c.cmd === 'llm_cloud_consent_grant')
    expect(grants).toHaveLength(1)
    expect(grants[0].args?.['providerId']).toBe('openai')
    expect(grants[0].args?.['taskProfileId']).toBe('task-chat')
    // Exactly one user message ('hi') plus the assistant reply.
    const sends = tauriMockCalls().filter((c) => c.cmd === 'llm_chat_universal')
    expect(sends).toHaveLength(1)
    expect((screen.getByPlaceholderText('Сообщение провайдеру…') as HTMLTextAreaElement).value).toBe('')
  })

  it('does not show the dialog when consent is not required', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: JSON.stringify(cfg()),
      llm_models_list: JSON.stringify([]),
      llm_cloud_consent_status: consentFixture(false),
      llm_chat_universal: 'cloud answer',
    })
    render(<AIChatFull />)
    await selectCloudAndType(user, 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByText('cloud answer')).toBeTruthy()
    expect(screen.queryByRole('dialog', { name: /Согласие на отправку в облако/ })).toBeNull()
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_cloud_consent_grant')).toBe(false)
  })

  it('blocks honestly when the consent status cannot be read', async () => {
    const user = userEvent.setup()
    installTauriMock({
      llm_get_config_report: JSON.stringify(cfg()),
      llm_models_list: JSON.stringify([]),
      llm_cloud_consent_status: () => { throw new Error('backend down') },
      llm_chat_universal: () => { throw new Error('must not send') },
    })
    render(<AIChatFull />)
    await selectCloudAndType(user, 'hi')
    await user.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByText(/Не удалось проверить согласие/)).toBeTruthy()
    expect(tauriMockCalls().some((c) => c.cmd === 'llm_chat_universal')).toBe(false)
    // Draft is untouched so the user can retry.
    expect((screen.getByPlaceholderText('Сообщение провайдеру…') as HTMLTextAreaElement).value).toBe('hi')
  })
})
