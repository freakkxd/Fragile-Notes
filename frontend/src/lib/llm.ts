import { z } from 'zod'

type TauriInvoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>
function getInvoke(): TauriInvoke | null {
  const w = window as unknown as { __TAURI__?: { invoke: TauriInvoke } }
  return w.__TAURI__?.invoke ?? null
}

// zod schemas — never as T
export const ModelInfoSchema = z.object({
  name: z.string(),
  path: z.string(),
  size_mb: z.number(),
  quant: z.string(),
  task: z.string(),
  installed_at: z.string(),
  source_url: z.string(),
})
export type ModelInfo = z.infer<typeof ModelInfoSchema>

// Single source of truth for backend ProviderKind values (snake_case).
// Must match Rust ProviderKind serialization. Do NOT duplicate ad hoc lists.
export const KNOWN_PROVIDER_KINDS = [
  'local_llama_cpp',
  'ollama',
  'open_ai',
  'gemini',
  'claude',
  'custom_open_ai',
  'deep_seek',
  'open_router',
] as const
export type ProviderKind = (typeof KNOWN_PROVIDER_KINDS)[number]
export const ProviderKindSchema = z.enum(KNOWN_PROVIDER_KINDS)

export const ProviderSchema = z.object({
  id: z.string(),
  name: z.string(),
  kind: ProviderKindSchema,
  enabled: z.boolean(),  // v2: endpoint + auth, v1 compat: api_url/api_key/auth_method/model
  endpoint: z.string().optional().default(''),
  api_url: z.string().optional().default(''),
  auth: z.object({ method: z.enum(['api_key','o_auth','none']).optional(), secret_ref: z.string().optional(), account_id: z.string().optional() }).optional().nullable(),
  auth_method: z.enum(['api_key', 'o_auth', 'none']).optional(),
  api_key: z.string().optional().default(''),
  model: z.string().optional().default(''),
  default_model: z.string().optional().default(''),
  extra: z.record(z.string()).optional().default({}),
  // masked helpers from backend
  hasSecret: z.boolean().optional(),
  masked: z.string().optional(),
}).passthrough()
export type Provider = z.infer<typeof ProviderSchema>

export const LocalSettingsSchema = z.object({
  binary_path: z.string(),
  models_dir: z.string(),
  active_model: z.string(),
  n_ctx: z.number(),
  n_threads: z.number(),
  n_gpu_layers: z.number(),
  temp: z.number(),
  top_p: z.number(),
  top_k: z.number(),
  repeat_penalty: z.number(),
  port: z.number(),
  auto_start: z.boolean(),
  use_mmap: z.boolean(),
  extra_args: z.string().optional().default(''),
  runtime_args: z.array(z.string()).optional(),
}).passthrough()
export type LocalSettings = z.infer<typeof LocalSettingsSchema>

export const ModelSchema = z.object({
  id: z.string(),
  provider_id: z.string(),
  name: z.string(),
  path: z.string(),
  remote_id: z.string().optional().default(''),
  capabilities: z.array(z.string()).optional().default([]),
  metadata: z.object({ size_bytes: z.number().optional(), quant: z.string().optional(), arch: z.string().optional() }).passthrough().optional(),
}).passthrough()
export type Model = z.infer<typeof ModelSchema>

export const RuntimeProfileSchema = z.object({
  id: z.string(),
  provider_id: z.string(),
  model_id: z.string(),
  binary_path: z.string().optional().default(''),
  port: z.number().optional().default(8010),
  policy: z.string().optional().default('on-demand'),
  settings: z.object({ n_ctx: z.number().optional(), n_threads: z.number().optional(), n_gpu_layers: z.number().optional(), temp: z.number().optional() }).passthrough().optional(),
}).passthrough()
export type RuntimeProfile = z.infer<typeof RuntimeProfileSchema>

export const TaskProfileSchema = z.object({
  id: z.string(),
  name: z.string(),
  model_ref: z.string().optional().default(''),
  runtime_profile_id: z.string().optional().nullable(),
  privacy: z.enum(['local_only','cloud_allowed']).optional(),
}).passthrough()
export type TaskProfile = z.infer<typeof TaskProfileSchema>

export const PipelineStepSchema = z.object({
  id: z.string(),
  name: z.string(),
  kind: z.string().optional().default('llm'),
  provider_id: z.string(),
  model: z.string().optional().default(''),
  model_ref: z.string().optional().default(''),
  prompt_template: z.string(),
  input_from: z.string().optional().default('note_content'),
  input_refs: z.array(z.string()).optional(),
  output_to: z.string().optional(),
  task_profile_id: z.string().optional().nullable(),
  enabled: z.boolean(),
})
export type PipelineStep = z.infer<typeof PipelineStepSchema>
export const PipelineSchema = z.object({
  id: z.string(),
  name: z.string(),
  description: z.string(),
  enabled: z.boolean(),
  trigger: z.string(),
  steps: z.array(PipelineStepSchema),
})
export type Pipeline = z.infer<typeof PipelineSchema>

export const LlmConfigSchema = z.object({
  version: z.number().optional(),
  schema_version: z.number().optional().default(2),
  providers: z.array(ProviderSchema),
  models: z.array(ModelSchema).optional().default([]),
  runtime_profiles: z.array(RuntimeProfileSchema).optional().default([]),
  task_profiles: z.array(TaskProfileSchema).optional().default([]),
  local: LocalSettingsSchema,
  pipelines: z.array(PipelineSchema),
  active_pipeline: z.string(),
  runtime_limits: z.object({ max_active_runtimes: z.number().optional(), allow_concurrent: z.boolean().optional() }).passthrough().optional(),
}).passthrough()
export type LlmConfig = z.infer<typeof LlmConfigSchema>

// ---------------------------------------------------------------------------
// Tolerant config recovery (Phase 2): one bad provider must not kill the
// whole config. Unknown future kinds are quarantined (excluded from the
// working config, reported as issues) — never silently deleted from disk
// (this layer never writes back automatically).
// ---------------------------------------------------------------------------

export type LlmConfigIssueCode =
  | 'unknown_kind'
  | 'invalid_provider'
  | 'invalid_root'
  | 'backend_unavailable'

export interface LlmConfigIssue {
  /** index in the raw providers array, -1 when not applicable */
  index: number
  /** provider id when it is a safe plain string, else null */
  providerId: string | null
  code: LlmConfigIssueCode
  /** user-facing message (Russian), no dumps, no secrets */
  message: string
  /** raw kind value only: short display string, never secrets */
  rawKind: string | null
  repairable: boolean
}

export interface LlmConfigLoadResult {
  /** working config, or null when nothing usable could be built */
  config: LlmConfig | null
  issues: LlmConfigIssue[]
  recovered: boolean
}

function safeId(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 && v.length <= 128 ? v : null
}

function safeRawKind(v: unknown): string | null {
  if (typeof v !== 'string' || v.length === 0 || v.length > 64) return null
  // kind identifiers only — strip anything that does not belong there
  if (!/^[A-Za-z0-9_.\- ]{1,64}$/.test(v)) return null
  return v
}

const KIND_MESSAGES: Record<string, string> = {
  deep_seek: 'Тип deep_seek поддерживается backend, обновите интерфейс.',
  open_router: 'Тип open_router поддерживается backend, обновите интерфейс.',
}

function kindIssueMessage(rawKind: string | null): string {
  if (rawKind && KIND_MESSAGES[rawKind.trim()]) return KIND_MESSAGES[rawKind.trim()]
  return 'Неподдерживаемый тип провайдера.'
}

/** Pure tolerant parser: valid providers survive, bad ones become issues. */
export function parseConfigSafe(raw: unknown): LlmConfigLoadResult {
  const issues: LlmConfigIssue[] = []
  let root: Record<string, unknown>
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return {
      config: null,
      issues: [{
        index: -1, providerId: null, code: 'invalid_root',
        message: 'Конфигурация повреждена и не может быть прочитана.',
        rawKind: null, repairable: false,
      }],
      recovered: true,
    }
  }
  root = raw as Record<string, unknown>
  const rawProviders = Array.isArray(root['providers']) ? root['providers'] as unknown[] : null
  if (!rawProviders) {
    return {
      config: null,
      issues: [{
        index: -1, providerId: null, code: 'invalid_root',
        message: 'В конфигурации отсутствует список провайдеров.',
        rawKind: null, repairable: false,
      }],
      recovered: true,
    }
  }
  const providers: Provider[] = []
  rawProviders.forEach((p, index) => {
    const parsed = ProviderSchema.safeParse(p)
    if (parsed.success) {
      providers.push(parsed.data)
      return
    }
    const obj = (typeof p === 'object' && p !== null ? p as Record<string, unknown> : {})
    const rawKind = safeRawKind(obj['kind'])
    const isKnownKind = typeof obj['kind'] === 'string'
      && (KNOWN_PROVIDER_KINDS as readonly string[]).includes((obj['kind'] as string).trim())
    if (!isKnownKind) {
      issues.push({
        index,
        providerId: safeId(obj['id']),
        code: 'unknown_kind',
        message: rawKind
          ? `Провайдер #${index + 1} пропущен: ${kindIssueMessage(rawKind)}`
          : `Провайдер #${index + 1} пропущен: некорректный тип.`,
        rawKind,
        repairable: true,
      })
    } else {
      issues.push({
        index,
        providerId: safeId(obj['id']),
        code: 'invalid_provider',
        message: `Провайдер #${index + 1} пропущен: некорректная запись.`,
        rawKind,
        repairable: true,
      })
    }
  })
  // Rest of the config parses tolerantly; providers are already filtered.
  const rest = { ...(root as object), providers }
  const parsed = LlmConfigSchema.safeParse(rest)
  if (!parsed.success) {
    return {
      config: null,
      issues: [...issues, {
        index: -1, providerId: null, code: 'invalid_root',
        message: 'Конфигурация повреждена: базовая структура не распознана.',
        rawKind: null, repairable: false,
      }],
      recovered: true,
    }
  }
  return { config: parsed.data, issues, recovered: issues.length > 0 }
}

/** Load config through the tolerant path. Never throws a raw Zod error. */
export async function llmGetConfigSafe(): Promise<LlmConfigLoadResult> {
  const inv = getInvoke()
  if (!inv) {
    return {
      config: null,
      issues: [{
        index: -1, providerId: null, code: 'backend_unavailable',
        message: 'Backend недоступен (Tauri IPC отсутствует).',
        rawKind: null, repairable: false,
      }],
      recovered: false,
    }
  }
  let raw: unknown
  try {
    raw = JSON.parse(await inv('llm_get_config') as string)
  } catch {
    return {
      config: null,
      issues: [{
        index: -1, providerId: null, code: 'backend_unavailable',
        message: 'Не удалось получить конфигурацию от backend.',
        rawKind: null, repairable: false,
      }],
      recovered: false,
    }
  }
  return parseConfigSafe(raw)
}

async function invoke<T>(cmd: string, args?: Record<string, unknown>, schema?: z.ZodSchema<T>): Promise<T> {
  const inv = getInvoke()
  if (!inv) throw new Error('Tauri not available')
  const raw = await inv(cmd, args)
  if (schema) return schema.parse(JSON.parse(raw as string))
  return raw as T
}

export async function llmGetConfig(): Promise<LlmConfig> {
  const inv = getInvoke()
  if (!inv) throw new Error('no tauri')
  const raw = await inv('llm_get_config') as string
  return LlmConfigSchema.parse(JSON.parse(raw))
}
export async function llmSaveConfig(cfg: LlmConfig): Promise<void> {
  const inv = getInvoke()
  if (!inv) throw new Error('no tauri')
  await inv('llm_save_config', { configJson: JSON.stringify(cfg) })
}
export async function llmScanModels(modelsDir?: string): Promise<ModelInfo[]> {
  const inv = getInvoke()
  if (!inv) return []
  const raw = await inv('llm_scan_models', { modelsDir: modelsDir ?? null }) as string
  return z.array(ModelInfoSchema).parse(JSON.parse(raw))
}
export async function llmSetActiveModel(path: string): Promise<void> {
  const inv = getInvoke()
  if (!inv) return
  await inv('llm_set_active_model', { modelPath: path })
}
export async function llmDownloadModel(url: string, targetTask: string, filename?: string): Promise<string> {
  const inv = getInvoke()
  if (!inv) throw new Error('no tauri')
  const raw = await inv('llm_download_model', { url, targetTask, filename: filename ?? null }) as string
  return raw
}
export async function llmTestProvider(providerId: string): Promise<string> {
  const inv = getInvoke()
  if (!inv) throw new Error('no tauri')
  return await inv('llm_test_provider', { providerId }) as string
}
export async function llmChatUniversal(providerId: string, model: string, messages: {role:string,content:string}[]): Promise<string> {
  const inv = getInvoke()
  if (!inv) throw new Error('no tauri')
  return await inv('llm_chat_universal', { providerId, model, messages }) as string
}
export async function llmGetPipelines(): Promise<Pipeline[]> {
  const inv = getInvoke()
  if (!inv) return []
  const raw = await inv('llm_get_pipelines') as string
  return z.array(PipelineSchema).parse(JSON.parse(raw))
}
export async function llmSavePipeline(p: Pipeline): Promise<void> {
  const inv = getInvoke()
  if (!inv) throw new Error('no tauri')
  await inv('llm_save_pipeline', { pipelineJson: JSON.stringify(p) })
}
export async function llmDeletePipeline(id: string): Promise<void> {
  const inv = getInvoke()
  if (!inv) throw new Error('no tauri')
  await inv('llm_delete_pipeline', { id })
}
export async function llmPipelineRun(pipelineId: string, input: string): Promise<string> {
  const inv = getInvoke()
  if (!inv) throw new Error('no tauri')
  return await inv('llm_pipeline_run', { pipelineId, input }) as string
}
export async function llmHasProviderCredential(providerId: string): Promise<boolean> {
  const inv = getInvoke(); if(!inv) return false
  const raw = await inv('llm_has_provider_credential', { providerId }) as string
  return raw === 'true'
}
export async function llmSetProviderCredential(providerId: string, secret: string): Promise<void> {
  const inv = getInvoke(); if(!inv) throw new Error('no tauri')
  await inv('llm_set_provider_credential', { providerId, secret })
}
export async function llmDeleteProviderCredential(providerId: string): Promise<void> {
  const inv = getInvoke(); if(!inv) throw new Error('no tauri')
  await inv('llm_delete_provider_credential', { providerId })
}
export async function llmRuntimeList(): Promise<string> { const inv=getInvoke(); if(!inv) return '[]'; return await inv('llm_runtime_list') as string }
export async function llmRuntimeStart(profileId: string): Promise<string> { const inv=getInvoke(); if(!inv) throw new Error('no tauri'); return await inv('llm_runtime_start', { profileId }) as string }
export async function llmRuntimeStop(runtimeId: string): Promise<string> { const inv=getInvoke(); if(!inv) throw new Error('no tauri'); return await inv('llm_runtime_stop', { runtimeId }) as string }
export async function llmRuntimeHealth(runtimeId: string): Promise<string> { const inv=getInvoke(); if(!inv) throw new Error('no tauri'); return await inv('llm_runtime_health', { runtimeId }) as string }
export async function llmRuntimeLogs(runtimeId: string, tail?: number): Promise<string> { const inv=getInvoke(); if(!inv) throw new Error('no tauri'); return await inv('llm_runtime_logs', { runtimeId, tail: tail ?? 200 }) as string }
export async function llmRuntimeRestart(runtimeId: string): Promise<string> { const inv=getInvoke(); if(!inv) throw new Error('no tauri'); return await inv('llm_runtime_restart', { runtimeId }) as string }
export async function llmTaskRun(taskProfileId: string, messages: {role:string,content:string}[]): Promise<string> { const inv=getInvoke(); if(!inv) throw new Error('no tauri'); return await inv('llm_task_run', { taskProfileId, messages }) as string }
export async function llmTaskCancel(runId: string): Promise<string> { const inv=getInvoke(); if(!inv) throw new Error('no tauri'); return await inv('llm_task_cancel', { runId }) as string }
export async function llmTaskStatus(runId: string): Promise<string> { const inv=getInvoke(); if(!inv) throw new Error('no tauri'); return await inv('llm_task_status', { runId }) as string }
