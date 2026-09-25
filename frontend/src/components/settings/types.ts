import type * as llm from '../../lib/llm'

export type SettingsMode = 'simple' | 'default' | 'expert'
export type SettingsSection =
  | 'general'
  | 'providers'
  | 'models'
  | 'runtime'
  | 'privacy'
  | 'pipelines'
  | 'diagnostics'

export const MODE_LABELS: Record<SettingsMode, string> = {
  simple: 'Простой',
  default: 'Обычный',
  expert: 'Эксперт',
}

export const SECTION_LABELS: Record<SettingsSection, string> = {
  general: 'Общее',
  providers: 'Провайдеры',
  models: 'Модели',
  runtime: 'Рантайм',
  privacy: 'Приватность',
  pipelines: 'Пайплайны',
  diagnostics: 'Диагностика',
}

const MODE_KEY = 'fragile-settings-mode'

export function loadMode(): SettingsMode {
  try {
    const v = localStorage.getItem(MODE_KEY)
    if (v === 'simple' || v === 'default' || v === 'expert') return v
  } catch {}
  return 'default'
}

export function saveMode(m: SettingsMode) {
  try {
    localStorage.setItem(MODE_KEY, m)
  } catch {}
}

/** Snapshot-based dirty tracking: stable stringify of the editable config. */
export function snapshotConfig(cfg: llm.LlmConfig): string {
  return JSON.stringify(cfg)
}

export function isDirty(snapshot: string, cfg: llm.LlmConfig): boolean {
  return snapshot !== snapshotConfig(cfg)
}

/** Cloud scope for UI purposes: anything not locally managed. */
export function isLocalKind(kind: string): boolean {
  return kind === 'local_llama_cpp' || kind === 'ollama'
}

/** Never expose raw secrets: masked display for keyring state. */
export function keyringLabel(hasSecret: boolean | undefined): string {
  if (hasSecret === true) return 'Ключ сохранён в системном хранилище'
  if (hasSecret === false) return 'Ключ не сохранён'
  return 'Состояние ключа неизвестно'
}

/** Redacted diagnostics copy: providers without api_key, masked refs. */
export function redactedDiagnostics(cfg: llm.LlmConfig): object {
  return {
    schema_version: cfg.schema_version,
    providers: cfg.providers.map((p) => ({
      id: p.id,
      name: p.name,
      kind: p.kind,
      enabled: p.enabled,
      endpoint: p.endpoint || p.api_url || '',
      default_model: p.default_model || p.model || '',
      hasSecret: p.hasSecret ?? undefined,
      auth: p.auth
        ? {
            method: p.auth.method ?? undefined,
            secret_ref: p.auth.secret_ref ? '***' : undefined,
          }
        : undefined,
    })),
    models: cfg.models.map((m) => ({
      id: m.id,
      provider_id: m.provider_id,
      name: m.name,
      capabilities: m.capabilities,
    })),
    task_profiles: cfg.task_profiles.map((t) => ({
      id: t.id,
      name: t.name,
      model_ref: t.model_ref,
      privacy: t.privacy ?? undefined,
    })),
  }
}
