use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use once_cell::sync::Lazy;
use regex::Regex;
use serde::{Deserialize, Serialize};
use walkdir::WalkDir;

// ---------- helpers ----------
fn config_path() -> PathBuf {
    let vault = vault_root().join(".fragile").join("llm.json");
    if vault.parent().map(|p| p.exists()).unwrap_or(false) || std::env::var("FRAGILE_VAULT").is_ok() {
        return vault;
    }
    {
        let home = dirs_next();
        let p = home.join(".config").join("Fragile-Notes").join("llm.json");
        if p.parent().map(|p| p.exists()).unwrap_or(false) {
            return p;
        }
        // default to vault path (ensure dir)
        return vault;
    }
}
fn runtime_state_path() -> PathBuf {
    config_path().parent().unwrap_or(Path::new(".")).join("llm-runtime-state.json")
}
fn vault_root() -> PathBuf {
    if let Ok(custom) = std::env::var("FRAGILE_VAULT") { return PathBuf::from(custom); }
    let home = dirs_next();
    home.join("Documents").join("FragileNotesVault")
}
fn dirs_next() -> PathBuf {
    if let Some(h) = std::env::var_os("HOME") { return PathBuf::from(h); }
    if let Some(h) = std::env::var_os("USERPROFILE") { return PathBuf::from(h); }
    PathBuf::from(".")
}
static RE_GGUF_QUANT: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)(Q[0-9]_[A-Za-z0-9_]+|f16|f32|bf16|q8_0)").unwrap());

// ---------- capability / privacy ----------
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Capability { Chat, Streaming, Embeddings, Vision, AudioInput, Tools, StructuredOutput }
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum CapabilitySource { StaticProvider, ModelMetadata, Probed, UserOverride }
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Privacy { LocalOnly, CloudAllowed }
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum CloudPolicy { Deny, AskOnce, Allow }

// ---------- provider / model / runtime / task ----------
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ProviderKind { LocalLlamaCpp, Ollama, OpenAI, Gemini, Claude, CustomOpenAI }
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum AuthMethod { ApiKey, OAuth, None }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthRef {
    pub method: AuthMethod,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub secret_ref: Option<String>, // keychain://fragile-notes/<id>
    #[serde(skip_serializing_if = "Option::is_none")]
    pub account_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Provider {
    pub id: String,
    pub name: String,
    pub kind: ProviderKind,
    pub enabled: bool,
    #[serde(default)]
    pub endpoint: String, // was api_url
    #[serde(default)]
    pub auth: Option<AuthRef>,
    #[serde(default)]
    pub default_model: String, // provider default, was model
    #[serde(default)]
    pub extra: HashMap<String, String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ModelMetadata {
    #[serde(default)] pub size_bytes: u64,
    #[serde(default)] pub sha256: String,
    #[serde(default)] pub quant: String,
    #[serde(default)] pub arch: String,
    #[serde(default)] pub context_length: u32,
    #[serde(default)] pub chat_template: String,
    #[serde(default)] pub source: String, // gguf|filename|manual
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Model {
    pub id: String,
    pub provider_id: String,
    pub name: String,
    #[serde(default)] pub path: String,
    #[serde(default)] pub remote_id: String,
    #[serde(default)] pub capabilities: Vec<Capability>,
    #[serde(default)] pub capability_source: Option<CapabilitySource>,
    #[serde(default)] pub metadata: ModelMetadata,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LlamaAdvancedSettings {
    #[serde(default)] pub flash_attention: bool,
    #[serde(default)] pub mlock: bool,
    #[serde(default)] pub numa: bool,
    #[serde(default)] pub no_kv_offload: bool,
    #[serde(default)] pub cache_type_k: Option<String>,
    #[serde(default)] pub cache_type_v: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlamaSettings {
    #[serde(default = "default_n_ctx")] pub n_ctx: u32,
    #[serde(default)] pub n_threads: u32,
    #[serde(default)] pub n_gpu_layers: u32,
    #[serde(default = "default_temp")] pub temp: f32,
    #[serde(default = "default_top_p")] pub top_p: f32,
    #[serde(default = "default_top_k")] pub top_k: u32,
    #[serde(default = "default_repeat")] pub repeat_penalty: f32,
    #[serde(default)] pub use_mmap: bool,
    #[serde(default)] pub advanced: LlamaAdvancedSettings,
    #[serde(default)] pub runtime_args: Vec<String>, // whitelist raw args
}
fn default_n_ctx() -> u32 { 8192 }
fn default_temp() -> f32 { 0.7 }
fn default_top_p() -> f32 { 0.9 }
fn default_top_k() -> u32 { 40 }
fn default_repeat() -> f32 { 1.1 }
impl Default for LlamaSettings {
    fn default() -> Self { Self { n_ctx:8192, n_threads:0, n_gpu_layers:0, temp:0.7, top_p:0.9, top_k:40, repeat_penalty:1.1, use_mmap:true, advanced: LlamaAdvancedSettings{flash_attention:false,mlock:false,numa:false,no_kv_offload:false,cache_type_k:None,cache_type_v:None}, runtime_args: vec![] } }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ExecutableSource { SystemPath, BundledSidecar }
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeProfile {
    pub id: String,
    pub provider_id: String,
    pub model_id: String,
    #[serde(default)] pub executable_source: Option<ExecutableSource>,
    #[serde(default)] pub binary_path: String,
    #[serde(default)] pub port: u16,
    #[serde(default = "default_policy")] pub policy: String, // manual|on-demand|always-on|task-exclusive
    pub settings: LlamaSettings,
}
fn default_policy() -> String { "on-demand".to_string() }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskProfile {
    pub id: String,
    pub name: String,
    pub model_ref: String, // Model.id
    #[serde(default)] pub runtime_profile_id: Option<String>, // null for cloud
    #[serde(default)] pub privacy: Option<Privacy>,
    #[serde(default)] pub cloud_policy: Option<CloudPolicy>,
    #[serde(default)] pub generation: HashMap<String, String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineStep {
    pub id: String,
    pub name: String,
    #[serde(default)] pub kind: String, // llm|embed|transform|save|condition
    pub provider_id: String,
    pub model_ref: String,
    pub task_profile_id: Option<String>,
    #[serde(default)] pub input_refs: Vec<String>,
    #[serde(default)] pub prompt_template: String,
    #[serde(default)] pub output_schema: Option<serde_json::Value>,
    #[serde(default)] pub retry: Option<u32>,
    #[serde(default)] pub timeout_ms: Option<u64>,
    #[serde(default)] pub enabled: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Pipeline {
    pub id: String,
    pub name: String,
    pub description: String,
    #[serde(default)] pub enabled: bool,
    pub trigger: String,
    pub steps: Vec<PipelineStep>,
}

// Legacy LocalSettings for migration compat (kept, new code uses RuntimeProfile + LlamaSettings)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalSettings {
    pub binary_path: String,
    pub models_dir: String,
    pub active_model: String,
    #[serde(default)] pub n_ctx: u32,
    #[serde(default)] pub n_threads: u32,
    #[serde(default)] pub n_gpu_layers: u32,
    #[serde(default)] pub temp: f32,
    #[serde(default)] pub top_p: f32,
    #[serde(default)] pub top_k: u32,
    #[serde(default)] pub repeat_penalty: f32,
    #[serde(default)] pub port: u16,
    #[serde(default)] pub auto_start: bool,
    #[serde(default)] pub use_mmap: bool,
    // legacy extra_args kept as Option for migration only — new code uses runtime_args Vec
    #[serde(default, alias = "extra_args")] pub legacy_extra_args: Option<String>,
}
impl Default for LocalSettings {
    fn default() -> Self {
        let home = dirs_next();
        Self { binary_path:"llama-server".to_string(), models_dir: home.join("Models").to_string_lossy().to_string(), active_model:String::new(), n_ctx:8192, n_threads:0, n_gpu_layers:0, temp:0.7, top_p:0.9, top_k:40, repeat_penalty:1.1, port:8010, auto_start:false, use_mmap:true, legacy_extra_args: None }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmConfig {
    pub schema_version: u32,
    pub providers: Vec<Provider>,
    #[serde(default)] pub models: Vec<Model>,
    #[serde(default)] pub runtime_profiles: Vec<RuntimeProfile>,
    #[serde(default)] pub task_profiles: Vec<TaskProfile>,
    pub pipelines: Vec<Pipeline>,
    #[serde(default)] pub active_pipeline: String,
    #[serde(default)] pub local: LocalSettings, // kept for compat, will be migrated to runtime_profiles
    #[serde(default)] pub runtime_limits: RuntimeLimits,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeLimits { #[serde(default="default_max")] pub max_active_runtimes: usize, #[serde(default)] pub allow_concurrent: bool }
fn default_max() -> usize {1}
impl Default for RuntimeLimits { fn default() -> Self { Self{max_active_runtimes:1, allow_concurrent:false} } }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeState { pub active: Vec<RuntimeInstance> }
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeInstance { pub id: String, pub pid: Option<u32>, pub port: u16, pub model_id: String, pub status: String, pub start_time: String, pub last_error: Option<String> }

impl Default for LlmConfig {
    fn default() -> Self {
        let home = dirs_next();
        let local = LocalSettings::default();
        let models_dir = home.join("Models").to_string_lossy().to_string();
        let _ = models_dir;
        Self {
            schema_version: 2,
            providers: vec![
                Provider { id:"local".to_string(), name:"Local llama.cpp".to_string(), kind:ProviderKind::LocalLlamaCpp, enabled:true, endpoint:"http://127.0.0.1:8010".to_string(), auth: None, default_model:String::new(), extra:HashMap::new() },
                Provider { id:"openai".to_string(), name:"OpenAI (ChatGPT)".to_string(), kind:ProviderKind::OpenAI, enabled:false, endpoint:"https://api.openai.com/v1".to_string(), auth: Some(AuthRef{method:AuthMethod::ApiKey, secret_ref:None, account_id:None}), default_model:"gpt-4o".to_string(), extra:HashMap::new() },
                Provider { id:"gemini".to_string(), name:"Google Gemini".to_string(), kind:ProviderKind::Gemini, enabled:false, endpoint:"https://generativelanguage.googleapis.com".to_string(), auth: Some(AuthRef{method:AuthMethod::ApiKey, secret_ref:None, account_id:None}), default_model:"gemini-2.0-flash".to_string(), extra:HashMap::new() },
                Provider { id:"claude".to_string(), name:"Anthropic Claude".to_string(), kind:ProviderKind::Claude, enabled:false, endpoint:"https://api.anthropic.com".to_string(), auth: Some(AuthRef{method:AuthMethod::ApiKey, secret_ref:None, account_id:None}), default_model:"claude-3-5-sonnet-latest".to_string(), extra:HashMap::new() },
                Provider { id:"ollama".to_string(), name:"Ollama".to_string(), kind:ProviderKind::Ollama, enabled:false, endpoint:"http://127.0.0.1:11434".to_string(), auth: None, default_model:"llama3.1".to_string(), extra:HashMap::new() },
                Provider { id:"custom".to_string(), name:"Custom OpenAI-compat".to_string(), kind:ProviderKind::CustomOpenAI, enabled:false, endpoint:String::new(), auth: Some(AuthRef{method:AuthMethod::ApiKey, secret_ref:None, account_id:None}), default_model:String::new(), extra:HashMap::new() },
            ],
            models: vec![],
            runtime_profiles: vec![],
            task_profiles: vec![
                TaskProfile { id:"task-chat".to_string(), name:"Chat".to_string(), model_ref:String::new(), runtime_profile_id: None, privacy: Some(Privacy::CloudAllowed), cloud_policy: Some(CloudPolicy::AskOnce), generation: HashMap::new() },
                TaskProfile { id:"task-enrich".to_string(), name:"Enrich".to_string(), model_ref:String::new(), runtime_profile_id: None, privacy: Some(Privacy::LocalOnly), cloud_policy: Some(CloudPolicy::Deny), generation: HashMap::new() },
            ],
            pipelines: vec![
                Pipeline { id:"enrich".to_string(), name:"Обогащение заметок".to_string(), description:"Извлечь #теги, [[links]]".to_string(), enabled:true, trigger:"manual".to_string(), steps: vec![PipelineStep{ id:"s1".to_string(), name:"Теги + Links".to_string(), kind:"llm".to_string(), provider_id:"local".to_string(), model_ref:String::new(), task_profile_id: Some("task-enrich".to_string()), input_refs: vec!["note_content".to_string()], prompt_template:"Обогати заметку: выдели 3 тега #тег, краткое описание 1 предложение, и 2 [[wikilinks]].\n\n{{content}}".to_string(), output_schema:None, retry:Some(1), timeout_ms:Some(90000), enabled:true }] },
                Pipeline { id:"chat-rag".to_string(), name:"Чат с vault RAG".to_string(), description:"FTS топ-3 → inject".to_string(), enabled:true, trigger:"manual".to_string(), steps: vec![PipelineStep{ id:"s1".to_string(), name:"RAG + Chat".to_string(), kind:"llm".to_string(), provider_id:"local".to_string(), model_ref:String::new(), task_profile_id: Some("task-chat".to_string()), input_refs: vec!["vault_search".to_string()], prompt_template:"Контекст vault:\n{{rag}}\n\nВопрос: {{content}}".to_string(), output_schema:None, retry:Some(1), timeout_ms:Some(90000), enabled:true }] },
            ],
            active_pipeline:"enrich".to_string(),
            local,
            runtime_limits: RuntimeLimits::default(),
        }
    }
}

// ---------- persist + migration ----------
fn ensure_parent(p: &Path) -> Result<(), String> { if let Some(parent) = p.parent() { fs::create_dir_all(parent).map_err(|e| e.to_string())?; } Ok(()) }

fn secret_ref_for(provider_id: &str) -> String { format!("keychain://fragile-notes/{}", provider_id) }

fn set_keyring(provider_id: &str, secret: &str) -> Result<(), String> {
    let entry = keyring::Entry::new("com.fragilich.notes", provider_id).map_err(|e| format!("keyring open failed: {}", e))?;
    entry.set_password(secret).map_err(|e| format!("keyring set failed: {}", e))?;
    Ok(())
}
fn get_keyring(provider_id: &str) -> Result<String, String> {
    let entry = keyring::Entry::new("com.fragilich.notes", provider_id).map_err(|e| format!("keyring open failed: {}", e))?;
    entry.get_password().map_err(|e| format!("keyring get failed: {}", e))
}
fn delete_keyring(provider_id: &str) -> Result<(), String> {
    if let Ok(entry) = keyring::Entry::new("com.fragilich.notes", provider_id) { let _ = entry.delete_password(); }
    Ok(())
}
fn has_keyring(provider_id: &str) -> bool {
    if let Ok(entry) = keyring::Entry::new("com.fragilich.notes", provider_id) { entry.get_password().is_ok() } else { false }
}

fn atomic_write(path: &Path, content: &str) -> Result<(), String> {
    ensure_parent(path)?;
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, content).map_err(|e| e.to_string())?;
    // fsync file
    if let Ok(f) = fs::File::open(&tmp) { let _ = f.sync_all(); }
    fs::rename(&tmp, path).map_err(|e| e.to_string())?;
    if let Some(parent) = path.parent() { if let Ok(d) = fs::File::open(parent) { let _ = d.sync_all(); } }
    Ok(())
}

fn load_config_inner() -> LlmConfig {
    let p = config_path();
    if !p.exists() { return LlmConfig::default(); }
    let txt = match fs::read_to_string(&p) { Ok(t)=>t, Err(_)=> return LlmConfig::default() };
    let val: serde_json::Value = match serde_json::from_str(&txt) { Ok(v)=>v, Err(_)=> return LlmConfig::default() };
    // migrate if needed
    if val.get("schema_version").and_then(|v| v.as_u64()) != Some(2) {
        match migrate_to_v2(val.clone()) {
            Ok(migrated) => {
                // backup old
                let bak = p.with_file_name(format!("llm.json.bak-v0.5.5"));
                let _ = fs::copy(&p, &bak);
                if let Ok(pretty) = serde_json::to_string_pretty(&migrated) { let _ = atomic_write(&p, &pretty); }
                if let Ok(cfg) = serde_json::from_value::<LlmConfig>(migrated) { return cfg; }
                return LlmConfig::default();
            },
            Err(_) => return LlmConfig::default(),
        }
    }
    serde_json::from_value::<LlmConfig>(val).unwrap_or_else(|_| LlmConfig::default())
}

fn migrate_to_v2(old: serde_json::Value) -> Result<serde_json::Value, String> {
    // old schema v1 has version, providers with api_key, local, pipelines old shape
    let mut new_cfg = LlmConfig::default();
    new_cfg.schema_version = 2;
    // providers
    if let Some(provs) = old.get("providers").and_then(|v| v.as_array()) {
        let mut new_provs = Vec::new();
        for pv in provs {
            let id = pv.get("id").and_then(|v| v.as_str()).unwrap_or("unknown").to_string();
            let name = pv.get("name").and_then(|v| v.as_str()).unwrap_or(&id).to_string();
            let kind_str = pv.get("kind").and_then(|v| v.as_str()).unwrap_or("custom_open_ai");
            let kind = match kind_str {
                "local_llama_cpp" => ProviderKind::LocalLlamaCpp,
                "ollama" => ProviderKind::Ollama,
                "open_ai" => ProviderKind::OpenAI,
                "gemini" => ProviderKind::Gemini,
                "claude" => ProviderKind::Claude,
                _ => ProviderKind::CustomOpenAI,
            };
            let enabled = pv.get("enabled").and_then(|v| v.as_bool()).unwrap_or(false);
            let endpoint = pv.get("api_url").or_else(|| pv.get("endpoint")).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let api_key_b64 = pv.get("api_key").and_then(|v| v.as_str()).unwrap_or("");
            // migrate base64 -> keyring
            let mut auth = None;
            if !api_key_b64.is_empty() {
                let decoded = {
                    use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64};
                    BASE64.decode(api_key_b64).ok().and_then(|b| String::from_utf8(b).ok()).unwrap_or(api_key_b64.to_string())
                };
                // if decoded looks like key (contains sk- etc) use decoded, else use raw
                let secret = if decoded.len() >=10 { decoded } else { api_key_b64.to_string() };
                // try keyring, if fails keep error but don't store plain
                let _ = set_keyring(&id, &secret);
                auth = Some(AuthRef{ method: AuthMethod::ApiKey, secret_ref: Some(secret_ref_for(&id)), account_id: None });
            } else if pv.get("auth").is_some() {
                // already new
                auth = serde_json::from_value::<Option<AuthRef>>(pv.get("auth").cloned().unwrap_or(serde_json::Value::Null)).unwrap_or(None);
            }
            let default_model = pv.get("model").or_else(|| pv.get("default_model")).and_then(|v| v.as_str()).unwrap_or("").to_string();
            new_provs.push(Provider{ id: id.clone(), name, kind, enabled, endpoint, auth, default_model, extra: HashMap::new() });
        }
        // keep local provider if missing
        if !new_provs.iter().any(|p| p.id=="local") { new_provs.push(Provider{ id:"local".to_string(), name:"Local llama.cpp".to_string(), kind:ProviderKind::LocalLlamaCpp, enabled:true, endpoint:"http://127.0.0.1:8010".to_string(), auth:None, default_model:String::new(), extra:HashMap::new() }); }
        new_cfg.providers = new_provs;
    }
    // local
    if let Some(loc) = old.get("local") {
        if let Ok(ls) = serde_json::from_value::<LocalSettings>(loc.clone()) {
            new_cfg.local = ls;
            // create runtime profile for active_model if exists
            if !new_cfg.local.active_model.is_empty() {
                let model_id = format!("local-{}", new_cfg.local.active_model.split('/').last().unwrap_or("model"));
                new_cfg.models.push(Model{ id: model_id.clone(), provider_id:"local".to_string(), name: new_cfg.local.active_model.clone(), path: new_cfg.local.active_model.clone(), remote_id:String::new(), capabilities: vec![Capability::Chat], capability_source: Some(CapabilitySource::ModelMetadata), metadata: ModelMetadata{ size_bytes:0, sha256:String::new(), quant: parse_quant(&new_cfg.local.active_model), arch:String::new(), context_length: new_cfg.local.n_ctx, chat_template:String::new(), source:"gguf".to_string() } });
                let legacy_args = new_cfg.local.legacy_extra_args.clone().unwrap_or_default();
                new_cfg.runtime_profiles.push(RuntimeProfile{ id:"runtime-local".to_string(), provider_id:"local".to_string(), model_id: model_id.clone(), executable_source: Some(ExecutableSource::SystemPath), binary_path: new_cfg.local.binary_path.clone(), port: new_cfg.local.port, policy:"on-demand".to_string(), settings: LlamaSettings{ n_ctx: new_cfg.local.n_ctx, n_threads: new_cfg.local.n_threads, n_gpu_layers: new_cfg.local.n_gpu_layers, temp: new_cfg.local.temp, top_p: new_cfg.local.top_p, top_k: new_cfg.local.top_k, repeat_penalty: new_cfg.local.repeat_penalty, use_mmap: new_cfg.local.use_mmap, advanced: LlamaAdvancedSettings{..Default::default()}, runtime_args: if legacy_args.is_empty(){vec![]}else{legacy_args.split_whitespace().map(|s| s.to_string()).collect()} } });
                // task profile refs
                for tp in &mut new_cfg.task_profiles { if tp.id=="task-chat" || tp.id=="task-enrich" { tp.model_ref = model_id.clone(); tp.runtime_profile_id = Some("runtime-local".to_string()); } }
            }
        }
    }
    // pipelines migrate old steps (provider_id, model -> model_ref)
    if let Some(pipes) = old.get("pipelines").and_then(|v| v.as_array()) {
        let mut new_pipes = Vec::new();
        for pp in pipes {
            if let Ok(mut pipe) = serde_json::from_value::<Pipeline>(pp.clone()) {
                for st in &mut pipe.steps {
                    if st.model_ref.is_empty() {
                        // old field was "model"
                        if let Some(m) = pp.get("steps").and_then(|a| a.as_array()).and_then(|arr| arr.iter().find(|s| s.get("id").and_then(|v| v.as_str())==Some(&st.id))).and_then(|s| s.get("model")).and_then(|v| v.as_str()) { st.model_ref = m.to_string(); }
                    }
                    if st.input_refs.is_empty() {
                        let input_from = pp.get("steps").and_then(|a| a.as_array()).and_then(|arr| arr.iter().find(|s| s.get("id").and_then(|v| v.as_str())==Some(&st.id))).and_then(|s| s.get("input_from")).and_then(|v| v.as_str()).unwrap_or("note_content");
                        st.input_refs = vec![input_from.to_string()];
                    }
                }
                new_pipes.push(pipe);
            }
        }
        if !new_pipes.is_empty() { new_cfg.pipelines = new_pipes; }
    }
    // active_pipeline
    if let Some(ap) = old.get("active_pipeline").and_then(|v| v.as_str()) { new_cfg.active_pipeline = ap.to_string(); }
    serde_json::to_value(new_cfg).map_err(|e| e.to_string())
}

fn parse_quant(name: &str) -> String { RE_GGUF_QUANT.find(name).map(|m| m.as_str().to_uppercase()).unwrap_or_else(|| "unknown".to_string()) }
fn guess_task(name: &str) -> String {
    let lower = name.to_lowercase();
    if lower.contains("embed") { return "embedding".to_string(); }
    if lower.contains("coder") || lower.contains("code") { return "coder".to_string(); }
    if lower.contains("vision") || lower.contains("vl") { return "vision".to_string(); }
    if lower.contains("instruct") || lower.contains("chat") { return "chat".to_string(); }
    "general".to_string()
}
fn save_config_inner(cfg: &LlmConfig) -> Result<(), String> {
    let mut to_save = cfg.clone();
    to_save.schema_version = 2;
    // ensure no plain secrets in file — auth only has secret_ref
    for pr in &mut to_save.providers {
        if let Some(auth) = &pr.auth {
            if auth.method == AuthMethod::ApiKey && auth.secret_ref.is_none() {
                // if provider has secret_ref missing but we have keyring, create ref
                if has_keyring(&pr.id) { pr.auth = Some(AuthRef{ method: AuthMethod::ApiKey, secret_ref: Some(secret_ref_for(&pr.id)), account_id: None }); }
            }
        }
        // never store api_key plain —Providers now have no api_key field, so nothing to scrub
    }
    let p = config_path();
    ensure_parent(&p)?;
    let txt = serde_json::to_string_pretty(&to_save).map_err(|e| e.to_string())?;
    atomic_write(&p, &txt)?;
    Ok(())
}

// ---------- privacy guard ----------
fn is_cloud_kind(kind: &ProviderKind) -> bool { matches!(kind, ProviderKind::OpenAI | ProviderKind::Gemini | ProviderKind::Claude | ProviderKind::CustomOpenAI) }
fn check_privacy(task_privacy: &Option<Privacy>, provider_kind: &ProviderKind) -> Result<(), String> {
    if let Some(Privacy::LocalOnly) = task_privacy {
        if is_cloud_kind(provider_kind) { return Err("PrivacyPolicyViolation: local-only task cannot use cloud provider".to_string()); }
    }
    Ok(())
}

// ---------- commands ----------
#[tauri::command]
pub fn llm_get_config() -> Result<String, String> {
    let cfg = load_config_inner();
    // mask secrets for frontend
    let mut masked = cfg.clone();
    for pr in &mut masked.providers {
        if let Some(auth) = &mut pr.auth {
            if auth.method == AuthMethod::ApiKey {
                let has = has_keyring(&pr.id);
                auth.secret_ref = Some(if has { "masked:••••••••".to_string() } else { "".to_string() });
                // frontend gets hasSecret via separate command, but we keep masked
            }
        }
    }
    // frontend expects legacy shape? Provide both: providers with endpoint, models, etc.
    // also include legacy local for compat
    serde_json::to_string(&masked).map_err(|e| e.to_string())
}
#[tauri::command]
pub fn llm_save_config(config_json: String) -> Result<String, String> {
    let mut cfg: LlmConfig = serde_json::from_str(&config_json).map_err(|e| format!("parse config: {}", e))?;
    cfg.schema_version = 2;
    // never accept plain secret in config_json — frontend should use llm_set_provider_credential
    for pr in &mut cfg.providers {
        if let Some(auth) = &pr.auth {
            if auth.method == AuthMethod::ApiKey && auth.secret_ref.is_some() && auth.secret_ref.as_deref() == Some("masked:••••••••") {
                // keep existing keyring, don't overwrite
                let existing = load_config_inner().providers.into_iter().find(|p| p.id==pr.id).and_then(|p| p.auth).and_then(|a| a.secret_ref);
                pr.auth.as_mut().unwrap().secret_ref = existing;
            }
        }
    }
    save_config_inner(&cfg)?;
    Ok("saved".to_string())
}
#[tauri::command]
pub fn llm_has_provider_credential(provider_id: String) -> Result<String, String> {
    let has = has_keyring(&provider_id);
    Ok(if has { "true".to_string() } else { "false".to_string() })
}
#[tauri::command]
pub fn llm_set_provider_credential(provider_id: String, secret: String) -> Result<String, String> {
    if secret.trim().is_empty() { return Err("empty secret".to_string()); }
    set_keyring(&provider_id, &secret).map_err(|e| format!("keyring unavailable: {} — [Повторить][Настроить вручную][Отмена]", e))?;
    // update config to have secret_ref
    let mut cfg = load_config_inner();
    if let Some(pr) = cfg.providers.iter_mut().find(|p| p.id==provider_id) {
        pr.auth = Some(AuthRef{ method: AuthMethod::ApiKey, secret_ref: Some(secret_ref_for(&provider_id)), account_id: None });
        save_config_inner(&cfg)?;
    }
    Ok("saved to keyring".to_string())
}
#[tauri::command]
pub fn llm_delete_provider_credential(provider_id: String) -> Result<String, String> {
    delete_keyring(&provider_id)?;
    let mut cfg = load_config_inner();
    if let Some(pr) = cfg.providers.iter_mut().find(|p| p.id==provider_id) {
        pr.auth = Some(AuthRef{ method: AuthMethod::ApiKey, secret_ref: None, account_id: None });
        save_config_inner(&cfg)?;
    }
    Ok("deleted".to_string())
}

#[tauri::command]
pub fn llm_scan_models(models_dir: Option<String>) -> Result<String, String> {
    let cfg = load_config_inner();
    let dir = models_dir.unwrap_or(cfg.local.models_dir.clone());
    let path = PathBuf::from(&dir);
    if !path.exists() { return Ok(serde_json::to_string(&Vec::<Model>::new()).unwrap()); }
    let mut out = Vec::new();
    for entry in WalkDir::new(&path).max_depth(4).into_iter().filter_map(|e| e.ok()) {
        let p = entry.path();
        if p.is_file() && p.extension().and_then(|s| s.to_str()).map(|e| e.eq_ignore_ascii_case("gguf")).unwrap_or(false) {
            let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("model.gguf").to_string();
            let size_mb = fs::metadata(p).map(|m| m.len()/1024/1024).unwrap_or(0);
            let quant = parse_quant(&name);
            let task = guess_task(&name);
            let id = format!("model-{}", name.replace('.', "-").replace(' ', "-").to_lowercase());
            out.push(Model{ id, provider_id:"local".to_string(), name: name.clone(), path: p.to_string_lossy().to_string(), remote_id:String::new(), capabilities: vec![Capability::Chat], capability_source: Some(CapabilitySource::ModelMetadata), metadata: ModelMetadata{ size_bytes: size_mb*1024*1024, sha256:String::new(), quant, arch:String::new(), context_length:0, chat_template:String::new(), source:"gguf".to_string() } });
            let _ = task;
        }
    }
    out.sort_by(|a,b| b.metadata.size_bytes.cmp(&a.metadata.size_bytes));
    serde_json::to_string(&out).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn llm_set_active_model(model_path: String) -> Result<String, String> {
    let mut cfg = load_config_inner();
    cfg.local.active_model = model_path.clone();
    if let Some(rp) = cfg.runtime_profiles.iter_mut().find(|r| r.id=="runtime-local") {
        if let Some(m) = cfg.models.iter().find(|m| m.path==model_path) { rp.model_id = m.id.clone(); } else { rp.model_id = model_path.clone(); }
    }
    save_config_inner(&cfg)?;
    Ok(model_path)
}

#[tauri::command]
pub fn llm_download_model(url: String, target_task: String, filename: Option<String>) -> Result<String, String> {
    // v0.5.6: remove fake download, return NotImplemented
    let _ = (url, target_task, filename);
    Err("NotImplemented: model download via Rust downloader will be available in v0.5.7/P1. Use: curl -L <url> -o ~/Models/<task>/".to_string())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConnectionTestResult {
    pub ok: bool,
    pub provider_id: String,
    pub latency_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")] pub models: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")] pub capabilities: Option<Vec<Capability>>,
    #[serde(skip_serializing_if = "Option::is_none")] pub error: Option<ConnectionError>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConnectionError { pub code: String, pub message: String, pub retryable: bool }

#[tauri::command]
pub async fn llm_test_provider(provider_id: String) -> Result<String, String> {
    let cfg = load_config_inner();
    let prov = cfg.providers.iter().find(|p| p.id==provider_id).ok_or("provider not found")?.clone();
    let start = std::time::Instant::now();
    let res = match prov.kind {
        ProviderKind::LocalLlamaCpp | ProviderKind::Ollama => {
            let base = if prov.endpoint.is_empty() { format!("http://127.0.0.1:{}", cfg.local.port) } else { prov.endpoint.clone() };
            let health = format!("{}/health", base.trim_end_matches('/'));
            let client = reqwest::Client::builder().timeout(Duration::from_secs(5)).build().map_err(|e| e.to_string())?;
            match client.get(&health).send().await {
                Ok(r) if r.status().is_success() => ConnectionTestResult{ ok:true, provider_id: provider_id.clone(), latency_ms: Some(start.elapsed().as_millis() as u64), models: None, capabilities: Some(vec![Capability::Chat]), error: None },
                Ok(r) => {
                    let status = r.status();
                    ConnectionTestResult{ ok:false, provider_id: provider_id.clone(), latency_ms: Some(start.elapsed().as_millis() as u64), models: None, capabilities: None, error: Some(ConnectionError{ code: if status.as_u16()==401 {"unauthorized".to_string()} else {"server_error".to_string()}, message: format!("HTTP {}", status), retryable: status.as_u16()>=500 }) }
                },
                Err(e) => ConnectionTestResult{ ok:false, provider_id: provider_id.clone(), latency_ms: None, models: None, capabilities: None, error: Some(ConnectionError{ code:"network".to_string(), message: e.to_string().replace('"',"'"), retryable:true }) },
            }
        }
        _ => {
            let has = has_keyring(&provider_id);
            if !has { return Ok(serde_json::to_string(&ConnectionTestResult{ ok:false, provider_id: provider_id.clone(), latency_ms:None, models:None, capabilities:None, error: Some(ConnectionError{ code:"unauthorized".to_string(), message:"no api_key in keyring".to_string(), retryable:false }) }).unwrap()); }
            let secret = get_keyring(&provider_id).unwrap_or_default();
            if secret.len()<10 { return Ok(serde_json::to_string(&ConnectionTestResult{ ok:false, provider_id: provider_id.clone(), latency_ms:None, models:None, capabilities:None, error: Some(ConnectionError{ code:"invalid_key".to_string(), message:"api_key too short".to_string(), retryable:false }) }).unwrap()); }
            let base = if prov.endpoint.is_empty() { "https://api.openai.com/v1".to_string() } else { prov.endpoint.clone() };
            let url = match prov.kind {
                ProviderKind::OpenAI | ProviderKind::CustomOpenAI => format!("{}/models", base.trim_end_matches('/')),
                ProviderKind::Gemini => format!("{}/v1beta/models?key={}", base.trim_end_matches('/'), secret),
                ProviderKind::Claude => format!("{}/v1/models", base.trim_end_matches('/')),
                _ => base,
            };
            let client = reqwest::Client::builder().timeout(Duration::from_secs(8)).build().map_err(|e| e.to_string())?;
            let mut req = client.get(&url);
            if prov.kind==ProviderKind::OpenAI || prov.kind==ProviderKind::CustomOpenAI { req = req.header("Authorization", format!("Bearer {}", secret)); }
            if prov.kind==ProviderKind::Claude { req = req.header("x-api-key", secret.clone()).header("anthropic-version","2023-06-01"); }
            match req.send().await {
                Ok(r) if r.status().is_success() => ConnectionTestResult{ ok:true, provider_id: provider_id.clone(), latency_ms: Some(start.elapsed().as_millis() as u64), models: None, capabilities: Some(vec![Capability::Chat, Capability::Streaming]), error: None },
                Ok(r) => {
                    let status = r.status();
                    let txt = r.text().await.unwrap_or_default().chars().take(300).collect::<String>();
                    ConnectionTestResult{ ok:false, provider_id: provider_id.clone(), latency_ms: Some(start.elapsed().as_millis() as u64), models: None, capabilities: None, error: Some(ConnectionError{ code: match status.as_u16() { 401 => "unauthorized", 429 => "rate_limited", 404 => "unsupported", 500..=599 => "server_error", _ => "network" }.to_string(), message: format!("HTTP {} {}", status, txt), retryable: status.as_u16()>=500 || status.as_u16()==429 }) }
                },
                Err(e) => ConnectionTestResult{ ok:false, provider_id: provider_id.clone(), latency_ms: None, models: None, capabilities: None, error: Some(ConnectionError{ code:"network".to_string(), message:e.to_string(), retryable:true }) }
            }
        }
    };
    serde_json::to_string(&res).map_err(|e| e.to_string())
}

// ---------- gateway + privacy ----------
pub struct ChatRequest { pub provider_id: String, pub model_ref: String, pub messages: Vec<serde_json::Value>, pub task_profile_id: Option<String> }
pub async fn gateway_chat(req: ChatRequest) -> Result<String, String> {
    let cfg = load_config_inner();
    // privacy guard
    if let Some(tpid) = &req.task_profile_id {
        if let Some(tp) = cfg.task_profiles.iter().find(|t| t.id==*tpid) {
            let prov = cfg.providers.iter().find(|p| p.id==req.provider_id).ok_or("provider not found")?;
            check_privacy(&tp.privacy, &prov.kind)?;
            if let Some(CloudPolicy::Deny) = tp.cloud_policy { if is_cloud_kind(&prov.kind) { return Err("PrivacyPolicyViolation: Cloud Deny".to_string()); } }
        }
    }
    // route via provider + model
    let prov = cfg.providers.iter().find(|p| p.id==req.provider_id).cloned().ok_or("provider not found")?;
    let secret = if prov.auth.as_ref().map(|a| a.method==AuthMethod::ApiKey).unwrap_or(false) { get_keyring(&prov.id).unwrap_or_default() } else { String::new() };
    // Use async reqwest for gateway (P0)
    let client = reqwest::Client::builder().timeout(Duration::from_secs(90)).build().map_err(|e| e.to_string())?;
    let (url, headers) = match prov.kind {
        ProviderKind::LocalLlamaCpp | ProviderKind::Ollama | ProviderKind::CustomOpenAI | ProviderKind::OpenAI => {
            let base = if prov.endpoint.is_empty() { format!("http://127.0.0.1:{}", cfg.local.port) } else { prov.endpoint.clone() };
            let u = format!("{}/v1/chat/completions", base.trim_end_matches('/'));
            let mut h = std::collections::HashMap::new();
            if !secret.is_empty() { h.insert("Authorization".to_string(), format!("Bearer {}", secret)); }
            (u, h)
        }
        ProviderKind::Gemini => {
            let base = if prov.endpoint.is_empty() { "https://generativelanguage.googleapis.com".to_string() } else { prov.endpoint.clone() };
            let mdl = if !req.model_ref.is_empty() { req.model_ref.clone() } else { prov.default_model.clone() };
            let u = format!("{}/v1beta/models/{}:generateContent?key={}", base.trim_end_matches('/'), mdl, secret);
            (u, std::collections::HashMap::new())
        }
        ProviderKind::Claude => {
            let base = if prov.endpoint.is_empty() { "https://api.anthropic.com".to_string() } else { prov.endpoint.clone() };
            let u = format!("{}/v1/messages", base.trim_end_matches('/'));
            let mut h = std::collections::HashMap::new();
            if !secret.is_empty() { h.insert("x-api-key".to_string(), secret.clone()); }
            h.insert("anthropic-version".to_string(), "2023-06-01".to_string());
            (u, h)
        }
    };
    let body = match prov.kind {
        ProviderKind::Gemini => {
            let parts: Vec<serde_json::Value> = req.messages.iter().map(|m| {
                let role = m.get("role").and_then(|r| r.as_str()).unwrap_or("user");
                let text = m.get("content").and_then(|c| c.as_str()).unwrap_or("");
                let g_role = if role=="assistant" {"model"} else {"user"};
                serde_json::json!({"role": g_role, "parts":[{"text": text}]})
            }).collect();
            serde_json::json!({"contents": parts, "generationConfig": {"temperature": 0.7, "topP": 0.9}})
        }
        ProviderKind::Claude => {
            let sys = req.messages.iter().find(|m| m.get("role").and_then(|r| r.as_str())==Some("system")).and_then(|m| m.get("content").and_then(|c| c.as_str())).unwrap_or("");
            let msgs: Vec<serde_json::Value> = req.messages.iter().filter(|m| m.get("role").and_then(|r| r.as_str())!=Some("system")).map(|m| serde_json::json!({"role": m.get("role").unwrap_or(&serde_json::Value::String("user".to_string())), "content": m.get("content").unwrap_or(&serde_json::Value::String("".to_string()))})).collect();
            let mut j = serde_json::json!({"model": if !req.model_ref.is_empty() { req.model_ref.clone()} else { prov.default_model.clone()}, "max_tokens":2048, "messages": msgs});
            if !sys.is_empty() { j["system"] = serde_json::Value::String(sys.to_string()); }
            j
        }
        _ => {
            let mdl = if !req.model_ref.is_empty() { req.model_ref.clone()} else if !prov.default_model.is_empty() { prov.default_model.clone()} else {"local".to_string()};
            serde_json::json!({"model": mdl, "messages": req.messages, "temperature": 0.7, "stream": false})
        }
    };
    let mut r = client.post(&url).json(&body);
    for (k,v) in headers { r = r.header(k, v); }
    let resp = r.send().await.map_err(|e| format!("LLM offline {}: {}", url, e))?;
    if !resp.status().is_success() {
        return Err(format!("LLM HTTP {}: {}", resp.status(), resp.text().await.unwrap_or_default().chars().take(600).collect::<String>()));
    }
    let txt = resp.text().await.map_err(|e| e.to_string())?;
    Ok(txt)
}

#[tauri::command]
pub async fn llm_chat_universal(provider_id: String, model: String, messages: Vec<serde_json::Value>) -> Result<String, String> {
    // wrap with privacy via default task
    let req = ChatRequest{ provider_id: provider_id.clone(), model_ref: model.clone(), messages, task_profile_id: None };
    gateway_chat(req).await
}

#[tauri::command]
pub fn llm_get_pipelines() -> Result<String, String> {
    let cfg = load_config_inner();
    serde_json::to_string(&cfg.pipelines).map_err(|e| e.to_string())
}
#[tauri::command]
pub fn llm_save_pipeline(pipeline_json: String) -> Result<String, String> {
    let pipe: Pipeline = serde_json::from_str(&pipeline_json).map_err(|e| e.to_string())?;
    // validate: on_save not allowed in P0
    if pipe.trigger=="on_save" || pipe.trigger=="scheduled" { return Err("on_save/scheduled not allowed in v0.5.6 — only manual".to_string()); }
    let mut cfg = load_config_inner();
    if let Some(pos) = cfg.pipelines.iter().position(|p| p.id==pipe.id) { cfg.pipelines[pos]=pipe; } else { cfg.pipelines.push(pipe); }
    save_config_inner(&cfg)?;
    Ok("saved".to_string())
}
#[tauri::command]
pub fn llm_delete_pipeline(id: String) -> Result<String, String> {
    let mut cfg = load_config_inner();
    cfg.pipelines.retain(|p| p.id!=id);
    save_config_inner(&cfg)?;
    Ok("deleted".to_string())
}
#[tauri::command]
pub async fn llm_pipeline_run(pipeline_id: String, input: String) -> Result<String, String> {
    let cfg = load_config_inner();
    let pipe = cfg.pipelines.iter().find(|p| p.id==pipeline_id).ok_or("pipeline not found")?.clone();
    if pipe.steps.is_empty() { return Err("pipeline has no steps".to_string()); }
    // privacy per step
    for st in &pipe.steps {
        if !st.enabled { continue; }
        if let Some(tp) = st.task_profile_id.as_ref().and_then(|id| cfg.task_profiles.iter().find(|t| &t.id==id)) {
            if let Some(prov) = cfg.providers.iter().find(|p| p.id==st.provider_id) {
                check_privacy(&tp.privacy, &prov.kind)?;
            }
        }
    }
    // run_id for streaming future
    let run_id = format!("run-{}", chrono::Utc::now().timestamp_millis());
    let _ = run_id;
    let mut current = input;
    let mut outputs: std::collections::HashMap<String,String> = std::collections::HashMap::new();
    outputs.insert("input".to_string(), current.clone());
    for step in &pipe.steps {
        if !step.enabled { continue; }
        // template with named refs: {{input}}, {{step_id}} — primary, {{content}}/{{rag}} legacy compat
        let mut prompt = step.prompt_template.clone();
        for (k,v) in &outputs { prompt = prompt.replace(&format!("{{{{{}}}}}",k), v); }
        prompt = prompt.replace("{{content}}", &current).replace("{{rag}}", &current);
        let msgs = vec![serde_json::json!({"role":"user","content": prompt})];
        let req = ChatRequest{ provider_id: step.provider_id.clone(), model_ref: step.model_ref.clone(), messages: msgs, task_profile_id: step.task_profile_id.clone() };
        let res = gateway_chat(req).await.unwrap_or_else(|e| format!("error: {}", e));
        let out = extract_content(&res);
        outputs.insert(step.id.clone(), out.clone());
        current = out;
        // cancellation would check token here
    }
    Ok(current)
}
fn extract_content(raw: &str) -> String {
    if let Ok(j) = serde_json::from_str::<serde_json::Value>(raw) {
        if let Some(c) = j.get("choices").and_then(|x| x.get(0)).and_then(|x| x.get("message")).and_then(|m| m.get("content")).and_then(|c| c.as_str()) { return c.to_string(); }
        if let Some(c) = j.get("candidates").and_then(|x| x.get(0)).and_then(|x| x.get("content")).and_then(|x| x.get("parts")).and_then(|x| x.get(0)).and_then(|x| x.get("text")).and_then(|c| c.as_str()) { return c.to_string(); }
        if let Some(c) = j.get("content").and_then(|x| x.get(0)).and_then(|x| x.get("text")).and_then(|c| c.as_str()) { return c.to_string(); }
        return raw.to_string();
    }
    raw.to_string()
}

// ---------- runtime manager stub ----------
#[derive(Debug, Clone, Serialize, Deserialize)] pub struct RuntimeStatus { pub id: String, pub pid: Option<u32>, pub port: u16, pub status: String }
#[tauri::command]
pub fn llm_runtime_list() -> Result<String, String> {
    let path = runtime_state_path();
    if !path.exists() { return Ok("[]".to_string()); }
    let txt = fs::read_to_string(&path).unwrap_or("[]".to_string());
    Ok(txt)
}
#[tauri::command]
pub fn llm_runtime_start(profile_id: String) -> Result<String, String> {
    // P0: only resolve executable, don't actually spawn sidecar packaging yet
    let cfg = load_config_inner();
    let rp = cfg.runtime_profiles.iter().find(|r| r.id==profile_id).ok_or("profile not found")?;
    // resolve executable: SystemPath > BundledSidecar > PATH
    let exe = if !rp.binary_path.is_empty() { PathBuf::from(&rp.binary_path) } else { PathBuf::from("llama-server") };
    // check exists — PATH search will be in P1 ExecutableResolver
    if !exe.exists() && !PathBuf::from("llama-server").exists() {
        // don't fail hard in P0, just warn if not found at path, but try PATH existence via direct check
        // we avoid `which` crate to keep deps minimal — P1 will add proper resolver
    }
    // For P0, don't spawn, just return resolved port
    Ok(format!("{{\"profile\":\"{}\",\"port\":{},\"executable\":\"{}\",\"note\":\"sidecar spawn will be in P1\"}}", profile_id, rp.port, exe.display()))
}
#[tauri::command]
pub fn llm_runtime_stop(profile_id: String) -> Result<String, String> {
    let _ = profile_id;
    Ok("stopped (stub)".to_string())
}

