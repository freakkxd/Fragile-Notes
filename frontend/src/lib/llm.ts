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
  auth_method: z.enum(['api_key', 'o_auth', 'none']),
  api_key: z.string(),
  api_url: z.string(),
  model: z.string(),
  extra: z.record(z.string()),
})
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
  extra_args: z.string(),
})
export type LocalSettings = z.infer<typeof LocalSettingsSchema>

export const PipelineStepSchema = z.object({
  id: z.string(),
  name: z.string(),
  provider_id: z.string(),
  model: z.string(),
  prompt_template: z.string(),
  input_from: z.string(),
  output_to: z.string(),
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
  version: z.number(),
  providers: z.array(ProviderSchema),
  local: LocalSettingsSchema,
  pipelines: z.array(PipelineSchema),
  active_pipeline: z.string(),
})
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
