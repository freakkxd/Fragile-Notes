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

export const ProviderSchema = z.object({
  id: z.string(),
  name: z.string(),
  kind: z.enum(['local_llama_cpp', 'ollama', 'open_ai', 'gemini', 'claude', 'custom_open_ai']),
  enabled: z.boolean(),
  // v2: endpoint + auth, v1 compat: api_url/api_key/auth_method/model
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
