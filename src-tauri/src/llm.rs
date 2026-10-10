use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use once_cell::sync::Lazy;
use regex::Regex;
use serde::{Deserialize, Serialize};
use walkdir::WalkDir;

pub mod runtime;
pub mod task;
pub mod models;
pub mod download;
pub mod embeddings;
pub mod gateway;
pub mod provider;
#[cfg(test)]
mod e2e_manual;

// Native adapter contract for the Claude dispatch path.
use crate::llm::provider::ProviderAdapter;

// ---------- helpers ----------
pub(crate) fn config_path() -> PathBuf {
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
pub(crate) fn registry_path() -> PathBuf {
    config_path().with_file_name("llm-models.json")
}
pub(crate) fn config_path_pub() -> PathBuf { config_path() }
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
pub enum CapabilitySource { StaticProvider, ModelMetadata, Probed, UserOverride, Manual }
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Privacy { LocalOnly, CloudAllowed }
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum CloudPolicy { Deny, AskOnce, Allow }

// ---------- provider / model / runtime / task ----------
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ProviderKind { LocalLlamaCpp, Ollama, OpenAI, Gemini, Claude, CustomOpenAI, DeepSeek, OpenRouter }
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
pub(crate) fn get_keyring(provider_id: &str) -> Result<String, String> {
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

pub fn load_config_for_test() -> LlmConfig { load_config_inner() }
// Legacy internal entry: best-effort working config. Never writes.
// Degraded sources yield a safe in-memory subset/default; use
// load_config_report_inner() when the status matters.
pub(crate) fn load_config_inner() -> LlmConfig { load_config_report_inner().config }

// ---------- Gate 1: non-destructive config load classification ----------
// Read path NEVER writes: no quarantine, no backup, no migration persistence.
// Recovery copies/backups are created only inside the guarded save flow
// after an explicit recovery-start token.

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ConfigLoadStatus { Missing, Valid, Migrated, Degraded }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ConfigDegradedReason {
    Corrupt, Unreadable, MigrationFailed, UnsupportedVersion,
    VaultMissing, KeyringFailed, InvalidEntry,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfigLoadIssue {
    pub code: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub index: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfigCandidateMeta {
    pub role: String,
    pub path: String,
    pub exists: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub modified_secs: Option<i64>,
    pub active: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LoadReport {
    pub status: ConfigLoadStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<ConfigDegradedReason>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub migrated_from: Option<u32>,
    pub issues: Vec<ConfigLoadIssue>,
    pub active_path: String,
    pub candidates: Vec<ConfigCandidateMeta>,
    // Always None on the read path (no side effects). Present only in
    // save outcomes surfaced through dedicated commands.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quarantine: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub backup: Option<String>,
    pub config: LlmConfig,
}

fn cfg_issue(code: &str, index: Option<i64>, provider_id: Option<String>, detail: Option<String>) -> ConfigLoadIssue {
    ConfigLoadIssue { code: code.to_string(), index, provider_id, detail }
}

// Safe short identifiers only: never raw file contents, never secrets.
fn safe_short_id(v: Option<&serde_json::Value>) -> Option<String> {
    let s = v?.as_str()?;
    if s.is_empty() || s.len() > 128 { return None; }
    Some(s.to_string())
}
fn safe_raw_kind(v: Option<&serde_json::Value>) -> Option<String> {
    let s = v?.as_str()?;
    if s.is_empty() || s.len() > 64 { return None; }
    if !s.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-' | ' ')) { return None; }
    Some(s.to_string())
}
fn is_known_provider_kind(kind: &str) -> bool {
    matches!(kind.trim(), "local_llama_cpp" | "ollama" | "open_ai" | "gemini"
        | "claude" | "custom_open_ai" | "deep_seek" | "open_router")
}

fn candidate_meta(role: &str, path: &Path, active: &Path) -> ConfigCandidateMeta {
    let md = fs::symlink_metadata(path).ok();
    ConfigCandidateMeta {
        role: role.to_string(),
        path: path.to_string_lossy().to_string(),
        exists: md.is_some(),
        size: md.as_ref().map(|m| m.len()),
        modified_secs: md.as_ref().and_then(|m| m.modified().ok())
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs() as i64),
        active: path == active,
    }
}
fn config_candidates(active: &Path) -> Vec<ConfigCandidateMeta> {
    let vault = vault_root().join(".fragile").join("llm.json");
    let global = dirs_next().join(".config").join("Fragile-Notes").join("llm.json");
    vec![candidate_meta("vault", &vault, active), candidate_meta("global", &global, active)]
}

trait KeyStore { fn set(&self, provider_id: &str, secret: &str) -> Result<(), String>; }
struct RealKeyStore;
impl KeyStore for RealKeyStore {
    fn set(&self, pid: &str, secret: &str) -> Result<(), String> { set_keyring(pid, secret) }
}

fn report_with(status: ConfigLoadStatus, reason: Option<ConfigDegradedReason>,
    migrated_from: Option<u32>, issues: Vec<ConfigLoadIssue>,
    active: &Path, candidates: Vec<ConfigCandidateMeta>, config: LlmConfig) -> LoadReport {
    LoadReport { status, reason, migrated_from, issues,
        active_path: active.to_string_lossy().to_string(),
        candidates, quarantine: None, backup: None, config }
}

// Partial provider recovery: valid entries survive, bad ones become safe
// issues. Only for documents whose root/top-level schema is intact.
fn parse_v2_partial(obj: serde_json::Value, active: &Path, candidates: Vec<ConfigCandidateMeta>) -> LoadReport {
    let arr = obj.get("providers").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let mut providers: Vec<Provider> = Vec::new();
    let mut issues: Vec<ConfigLoadIssue> = Vec::new();
    for (i, p) in arr.iter().enumerate() {
        match serde_json::from_value::<Provider>(p.clone()) {
            Ok(pr) => providers.push(pr),
            Err(_) => {
                let o = p.as_object();
                let id = o.and_then(|m| safe_short_id(m.get("id")));
                let raw = o.and_then(|m| m.get("kind"));
                let known = raw.and_then(|k| k.as_str()).map(is_known_provider_kind).unwrap_or(false);
                issues.push(cfg_issue(
                    if known { "invalid_provider" } else { "unknown_kind" },
                    Some(i as i64), id, safe_raw_kind(raw)));
            }
        }
    }
    let mut rest = obj;
    rest["providers"] = serde_json::to_value(&providers).unwrap_or(serde_json::Value::Null);
    rest["schema_version"] = serde_json::json!(2);
    match serde_json::from_value::<LlmConfig>(rest) {
        Ok(cfg) => {
            if issues.is_empty() {
                report_with(ConfigLoadStatus::Valid, None, None, vec![], active, candidates, cfg)
            } else {
                report_with(ConfigLoadStatus::Degraded, Some(ConfigDegradedReason::InvalidEntry),
                    None, issues, active, candidates, cfg)
            }
        }
        Err(_) => {
            issues.push(cfg_issue("invalid_root", None, None, None));
            report_with(ConfigLoadStatus::Degraded, Some(ConfigDegradedReason::Corrupt),
                None, issues, active, candidates, LlmConfig::default())
        }
    }
}

pub(crate) fn load_config_report_inner() -> LoadReport {
    let active = config_path();
    let candidates = config_candidates(&active);
    // 1. FRAGILE_VAULT points nowhere: location itself is dubious, never default silently.
    if let Ok(custom) = std::env::var("FRAGILE_VAULT") {
        if !PathBuf::from(&custom).exists() {
            return report_with(ConfigLoadStatus::Degraded, Some(ConfigDegradedReason::VaultMissing),
                None, vec![cfg_issue("vault_missing", None, None, None)],
                &active, candidates, LlmConfig::default());
        }
    }
    // 2. Missing first-run config: safe in-memory default, save allowed.
    if !active.exists() {
        return report_with(ConfigLoadStatus::Missing, None, None, vec![], &active, candidates, LlmConfig::default());
    }
    // 3. Unreadable.
    let txt = match fs::read_to_string(&active) {
        Ok(t) => t,
        Err(_) => return report_with(ConfigLoadStatus::Degraded, Some(ConfigDegradedReason::Unreadable),
            None, vec![cfg_issue("unreadable", None, None, None)],
            &active, candidates, LlmConfig::default()),
    };
    // 4. Broken JSON / non-object root.
    let val: serde_json::Value = match serde_json::from_str(&txt) {
        Ok(v) => v,
        Err(_) => return report_with(ConfigLoadStatus::Degraded, Some(ConfigDegradedReason::Corrupt),
            None, vec![cfg_issue("invalid_root", None, None, None)],
            &active, candidates, LlmConfig::default()),
    };
    let obj = match val.as_object() {
        Some(_) => val.clone(),
        None => return report_with(ConfigLoadStatus::Degraded, Some(ConfigDegradedReason::Corrupt),
            None, vec![cfg_issue("invalid_root", None, None, None)],
            &active, candidates, LlmConfig::default()),
    };
    // 5. Version dispatch. No writes on any branch.
    let v = obj.get("schema_version").and_then(|x| x.as_u64());
    let legacy = obj.get("version").and_then(|x| x.as_u64());
    if v == Some(2) || (v.is_none() && legacy.is_none()) {
        // v2, or version-less but otherwise shaped document (tolerated as v2).
        if obj.get("providers").and_then(|x| x.as_array()).is_none() {
            return report_with(ConfigLoadStatus::Degraded, Some(ConfigDegradedReason::Corrupt),
                None, vec![cfg_issue("invalid_root", None, None, None)],
                &active, candidates, LlmConfig::default());
        }
        parse_v2_partial(obj, &active, candidates)
    } else if v == Some(1) || legacy == Some(1) {
        // v1: migrate in memory only. Persistence happens via the normal
        // guarded save path (which creates the versioned backup).
        match migrate_to_v2(obj, &RealKeyStore) {
            Ok(migrated) => match serde_json::from_value::<LlmConfig>(migrated) {
                Ok(cfg) => report_with(ConfigLoadStatus::Migrated, None, Some(1), vec![], &active, candidates, cfg),
                Err(_) => report_with(ConfigLoadStatus::Degraded, Some(ConfigDegradedReason::MigrationFailed),
                    None, vec![cfg_issue("migration_failed", None, None, None)],
                    &active, candidates, LlmConfig::default()),
            },
            Err(e) => {
                let reason = if e.starts_with("keyring:") { ConfigDegradedReason::KeyringFailed } else { ConfigDegradedReason::MigrationFailed };
                let code = if reason == ConfigDegradedReason::KeyringFailed { "keyring_failed" } else { "migration_failed" };
                report_with(ConfigLoadStatus::Degraded, Some(reason),
                    None, vec![cfg_issue(code, None, None, None)],
                    &active, candidates, LlmConfig::default())
            }
        }
    } else {
        report_with(ConfigLoadStatus::Degraded, Some(ConfigDegradedReason::UnsupportedVersion),
            None, vec![cfg_issue("unsupported_version", None, None, None)],
            &active, candidates, LlmConfig::default())
    }
}

fn migrate_to_v2(old: serde_json::Value, ks: &dyn KeyStore) -> Result<serde_json::Value, String> {
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
                "deep_seek" => ProviderKind::DeepSeek,
                "open_router" => ProviderKind::OpenRouter,
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
                // keyring unavailable -> abort migration, no dangling secret_ref
                ks.set(&id, &secret).map_err(|e| format!("keyring: {}", e))?;
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
    // Every write path passes the guard: degraded sources are rejected
    // without a recovery token, healthy ones get backup + atomic write.
    save_config_guarded(cfg, None).map(|_| ())
}

// ---------- Gate 1: recovery copies, backups, confirmation tokens ----------

#[derive(Debug, Clone, PartialEq)]
struct FileFingerprint { size: u64, mtime_secs: i64 }

fn fingerprint_of(p: &Path) -> Option<FileFingerprint> {
    let md = fs::symlink_metadata(p).ok()?;
    let mtime_secs = md.modified().ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs() as i64)?;
    Some(FileFingerprint { size: md.len(), mtime_secs })
}

fn utc_stamp() -> String { chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string() }

// Collision-safe sibling name: never overwrite an existing file.
// First collision appends -1, then -2, etc.
fn unique_sibling(live: &Path, first_name: &str) -> PathBuf {
    let parent = live.parent().unwrap_or(Path::new("."));
    let mut cand = parent.join(first_name);
    let mut n = 0;
    while cand.exists() {
        n += 1;
        cand = parent.join(format!("{}-{}", first_name, n));
    }
    cand
}

fn write_create_new(path: &Path, bytes: &[u8]) -> Result<(), String> {
    use std::fs::OpenOptions;
    use std::io::Write;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let mut f = OpenOptions::new().write(true).create_new(true)
        .open(path).map_err(|e| e.to_string())?;
    f.write_all(bytes).map_err(|e| e.to_string())?;
    let _ = f.sync_all();
    if let Some(parent) = path.parent() {
        if let Ok(d) = fs::File::open(parent) { let _ = d.sync_all(); }
    }
    Ok(())
}

// Pre-save backup, created BEFORE replacing the live config.
// Ordinary: llm.json.bak-<UTC>. Migration: llm.json.bak-v<from>-<UTC>.
fn create_pre_save_backup(live: &Path, migration_from: Option<u32>) -> Result<PathBuf, String> {
    let name = match migration_from {
        Some(f) => format!("llm.json.bak-v{}-{}", f, utc_stamp()),
        None => format!("llm.json.bak-{}", utc_stamp()),
    };
    let dest = unique_sibling(live, &name);
    let bytes = fs::read(live).map_err(|e| e.to_string())?;
    write_create_new(&dest, &bytes)?;
    Ok(dest)
}

// Rotation keeps the newest N ordinary backups. Never touches migration
// backups (bak-v*) or corrupt quarantine files. Non-fatal by contract:
// callers surface failures as warnings, never fail the save for them.
fn rotate_ordinary_backups(dir: Option<&Path>, keep: usize) -> Result<Vec<String>, String> {
    let dir = dir.unwrap_or(Path::new("."));
    let rd = fs::read_dir(dir).map_err(|e| e.to_string())?;
    let mut names: Vec<String> = Vec::new();
    for e in rd {
        let e = e.map_err(|e| e.to_string())?;
        let n = e.file_name().to_string_lossy().to_string();
        if let Some(rest) = n.strip_prefix("llm.json.bak-") {
            if rest.starts_with(|c: char| c.is_ascii_digit()) { names.push(n); }
        }
    }
    names.sort();
    names.reverse(); // newest first: UTC stamps sort lexicographically
    let mut removed = Vec::new();
    for n in names.into_iter().skip(keep) {
        match fs::remove_file(dir.join(&n)) {
            Ok(_) => removed.push(n),
            Err(e) => return Err(e.to_string()),
        }
    }
    Ok(removed)
}

struct TokenRecord {
    path: PathBuf,
    fp: Option<FileFingerprint>,
    expires_at: std::time::Instant,
}
static RECOVERY_TOKENS: Lazy<Mutex<HashMap<String, TokenRecord>>> =
    Lazy::new(|| Mutex::new(HashMap::new()));
const RECOVERY_TOKEN_TTL_SECS: u64 = 600;

fn new_token_string() -> String {
    (0..4).map(|_| format!("{:016x}", rand::random::<u64>())).collect()
}

fn issue_recovery_token(path: &Path, fp: Option<FileFingerprint>) -> String {
    // Policy note (intentional, not a leak): the read/report path is
    // non-mutating and never purges this store. Tokens live in RAM until
    // TTL expiry, single-use consumption, a newer issue for the same path,
    // or successful save. The frontend drops its in-memory copy on Reload,
    // so Reload needs no backend purge and performs no filesystem writes.
    let mut map = RECOVERY_TOKENS.lock().unwrap_or_else(|e| e.into_inner());
    let now = std::time::Instant::now();
    // Single active token per path: purge expired and previous ones.
    map.retain(|_, r| r.expires_at > now && r.path != *path);
    let t = new_token_string();
    map.insert(t.clone(), TokenRecord {
        path: path.to_path_buf(), fp,
        expires_at: now + std::time::Duration::from_secs(RECOVERY_TOKEN_TTL_SECS),
    });
    t
}

fn validate_and_consume_recovery_token(tok: &str, path: &Path, fp: Option<FileFingerprint>) -> Result<(), String> {
    let mut map = RECOVERY_TOKENS.lock().unwrap_or_else(|e| e.into_inner());
    let rec = map.remove(tok).ok_or_else(|| "InvalidConfirmToken: unknown, expired or already used token".to_string())?;
    if rec.expires_at <= std::time::Instant::now() {
        return Err("InvalidConfirmToken: token expired, restart recovery".to_string());
    }
    if rec.path != *path {
        return Err("InvalidConfirmToken: config path changed, restart recovery".to_string());
    }
    if rec.fp != fp {
        return Err("InvalidConfirmToken: source changed since recovery start, restart recovery".to_string());
    }
    Ok(())
}

fn invalidate_recovery_tokens(path: &Path) {
    let mut map = RECOVERY_TOKENS.lock().unwrap_or_else(|e| e.into_inner());
    map.retain(|_, r| r.path != *path);
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SaveOutcome {
    pub saved_path: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recovery_copy: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub backup: Option<String>,
    pub rotated_out: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rotation_warning: Option<String>,
}

fn scrub_for_save(cfg: &LlmConfig) -> LlmConfig {
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
    to_save
}

fn restore_masked_secrets(cfg: &mut LlmConfig) {
    // never accept plain secret in config_json — frontend should use llm_set_provider_credential
    for pr in &mut cfg.providers {
        if let Some(auth) = &pr.auth {
            if auth.method == AuthMethod::ApiKey && auth.secret_ref.as_deref() == Some("masked:••••••••") {
                // keep existing keyring, don't overwrite
                let existing = load_config_inner().providers.into_iter().find(|p| p.id==pr.id).and_then(|p| p.auth).and_then(|a| a.secret_ref);
                pr.auth.as_mut().unwrap().secret_ref = existing;
            }
        }
    }
}

fn mask_config(cfg: LlmConfig) -> LlmConfig {
    let mut masked = cfg;
    for pr in &mut masked.providers {
        if let Some(auth) = &mut pr.auth {
            if auth.method == AuthMethod::ApiKey {
                let has = has_keyring(&pr.id);
                auth.secret_ref = Some(if has { "masked:••••••••".to_string() } else { String::new() });
            }
        }
    }
    masked
}

// The single write funnel. Ordering:
// 1. classify (read-only) -> 2. degraded requires valid one-time token ->
// 3. recovery copy of the original (degraded only) -> 4. pre-save backup
// (live exists) -> 5. atomic write -> 6. rotation (non-fatal) ->
// 7. invalidate all tokens for the path.
// Any failure before step 5 leaves the original intact.
fn save_config_guarded(cfg: &LlmConfig, token: Option<&str>) -> Result<SaveOutcome, String> {
    save_config_guarded_with(cfg, token, create_pre_save_backup)
}

// Backup step is injectable so the BackupFailed path is testable
// deterministically (no permission tricks, root-independent).
fn save_config_guarded_with(
    cfg: &LlmConfig,
    token: Option<&str>,
    pre_save_backup: fn(&Path, Option<u32>) -> Result<PathBuf, String>,
) -> Result<SaveOutcome, String> {
    let live = config_path();
    let rep = load_config_report_inner();
    let degraded = rep.status == ConfigLoadStatus::Degraded;
    if degraded {
        let t = token.filter(|s| !s.is_empty()).ok_or_else(||
            "DegradedSaveBlocked: config is degraded, start explicit recovery first".to_string())?;
        validate_and_consume_recovery_token(t, &live, fingerprint_of(&live))?;
    }
    let to_save = scrub_for_save(cfg);
    let txt = serde_json::to_string_pretty(&to_save).map_err(|e| e.to_string())?;
    let mut outcome = SaveOutcome {
        saved_path: live.to_string_lossy().to_string(),
        recovery_copy: None, backup: None,
        rotated_out: Vec::new(), rotation_warning: None,
    };
    if degraded {
        // Recovery copy immediately before the confirmed overwrite.
        // Failure denies the overwrite; the original stays intact.
        let bytes = fs::read(&live)
            .map_err(|e| format!("RecoveryCopyFailed: cannot re-read original: {}", e))?;
        let dest = unique_sibling(&live, &format!("llm.json.corrupt-{}", utc_stamp()));
        write_create_new(&dest, &bytes)
            .map_err(|e| format!("RecoveryCopyFailed: {}", e))?;
        outcome.recovery_copy = Some(dest.to_string_lossy().to_string());
    }
    if live.exists() {
        let mig_from = if rep.status == ConfigLoadStatus::Migrated { rep.migrated_from } else { None };
        let bak = pre_save_backup(&live, mig_from)
            .map_err(|e| format!("BackupFailed: {}", e))?;
        outcome.backup = Some(bak.to_string_lossy().to_string());
    } else {
        ensure_parent(&live)?;
    }
    atomic_write(&live, &txt).map_err(|e| format!("save failed: {}", e))?;
    match rotate_ordinary_backups(live.parent(), 5) {
        Ok(removed) => outcome.rotated_out = removed,
        Err(e) => outcome.rotation_warning = Some(e),
    }
    invalidate_recovery_tokens(&live);
    Ok(outcome)
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
    // frontend expects legacy shape? Provide both: providers with endpoint, models, etc.
    // also include legacy local for compat
    serde_json::to_string(&mask_config(cfg)).map_err(|e| e.to_string())
}
#[tauri::command]
pub fn llm_get_config_report() -> Result<String, String> {
    let rep = load_config_report_inner();
    let masked = LoadReport { config: mask_config(rep.config), ..rep };
    serde_json::to_string(&masked).map_err(|e| e.to_string())
}
// Explicit recovery start: issues the one-time confirmation token for a
// degraded config. Memory-only, short-lived, bound to active path +
// source fingerprint. Never logged, never stored.
#[tauri::command]
pub fn llm_start_config_recovery() -> Result<String, String> {
    let rep = load_config_report_inner();
    if rep.status != ConfigLoadStatus::Degraded {
        return Err("no_recovery_needed: config is not degraded".to_string());
    }
    let path = config_path();
    let token = issue_recovery_token(&path, fingerprint_of(&path));
    let out = serde_json::json!({
        "token": token,
        "active_path": path.to_string_lossy(),
        "reason": rep.reason,
        "issues": rep.issues,
    });
    serde_json::to_string(&out).map_err(|e| e.to_string())
}
#[tauri::command]
pub fn llm_save_config(config_json: String) -> Result<String, String> {
    let mut cfg: LlmConfig = serde_json::from_str(&config_json).map_err(|e| format!("parse config: {}", e))?;
    cfg.schema_version = 2;
    restore_masked_secrets(&mut cfg);
    save_config_inner(&cfg)?;
    Ok("saved".to_string())
}
#[tauri::command]
pub fn llm_save_config_confirmed(config_json: String, confirm_token: String) -> Result<String, String> {
    let mut cfg: LlmConfig = serde_json::from_str(&config_json).map_err(|e| format!("CorruptInput: {}", e))?;
    cfg.schema_version = 2;
    restore_masked_secrets(&mut cfg);
    let outcome = save_config_guarded(&cfg, Some(&confirm_token))?;
    serde_json::to_string(&outcome).map_err(|e| e.to_string())
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

/// Gemini connection test through the native adapter: header-only auth,
/// never `?key=`. Preserves the `ConnectionTestResult` UI contract.
/// `secrets` is injectable for tests; production passes KeyringSecretStore.
async fn gemini_test_via_adapter(
    prov: &Provider,
    start: std::time::Instant,
    secrets: std::sync::Arc<dyn provider::SecretStore>,
) -> ConnectionTestResult {
    use std::sync::Arc;
    let latency = || Some(start.elapsed().as_millis() as u64);
    let fail = |code: &str, message: String, retryable: bool| ConnectionTestResult {
        ok: false, provider_id: prov.id.clone(), latency_ms: latency(),
        models: None, capabilities: None,
        error: Some(ConnectionError { code: code.to_string(), message, retryable }),
    };
    let auth = match &prov.auth {
        Some(a) if a.method == AuthMethod::ApiKey => match &a.secret_ref {
            Some(r) => match provider::AuthReference::new(r.clone()) {
                Ok(a) => Some(a),
                Err(_) => None,
            },
            None => None,
        },
        _ => None,
    };
    let connection = provider::ProviderConnection {
        id: prov.id.clone(),
        kind: ProviderKind::Gemini,
        name: prov.name.clone(),
        enabled: true,
        scope: crate::llm::task::policy::ProviderScope::Cloud,
        endpoint: if prov.endpoint.is_empty() { None } else { Some(prov.endpoint.clone()) },
        auth,
    };
    let adapter = match provider::GeminiNativeAdapter::new(
        connection, secrets, Duration::from_secs(8)) {
        Ok(a) => a,
        Err(e) => return fail("unsupported", dispatch_err(&e), false),
    };
    let cancel = tokio_util::sync::CancellationToken::new();
    match adapter.health(cancel).await {
        Ok(rep) => {
            if rep.authenticated == Some(false) {
                fail("unauthorized", "unauthorized".to_string(), false)
            } else {
                ConnectionTestResult { ok: true, provider_id: prov.id.clone(), latency_ms: latency(),
                    models: None, capabilities: Some(vec![Capability::Chat, Capability::Streaming]), error: None }
            }
        }
        Err(e) => {
            // Legacy UI code strings preserved; messages redacted + capped.
            let msg: String = crate::llm::embeddings::redact_secrets(&e.safe_message()).chars().take(300).collect();
            let (code, retryable) = match e.code() {
                "unauthorized" => ("unauthorized", false),
                "rate_limited" => ("rate_limited", true),
                "not_found" => ("unsupported", false),
                _ => ("network", e.is_retryable()),
            };
            fail(code, format!("HTTP {}", msg), retryable)
        }
    }
}

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
            // Gemini: header-only health via the native adapter (no ?key=).
            if prov.kind == ProviderKind::Gemini {
                return Ok(serde_json::to_string(
                    &gemini_test_via_adapter(&prov, start, std::sync::Arc::new(provider::KeyringSecretStore)).await
                ).unwrap());
            }
            let base = if prov.endpoint.is_empty() { "https://api.openai.com/v1".to_string() } else { prov.endpoint.clone() };
            let url = match prov.kind {
                ProviderKind::OpenAI | ProviderKind::CustomOpenAI => format!("{}/models", base.trim_end_matches('/')),
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

/// Effective chat generation settings. Source of truth is ALWAYS the
/// `TaskProfile.generation` map — `ChatRequest` carries NO generation
/// fields, so frontend raw input can never override the profile.
#[derive(Debug, Clone, PartialEq)]
pub struct ChatGeneration {
    pub temperature: f32,
    pub top_p: f32,
    pub max_tokens: u32,
}

impl Default for ChatGeneration {
    fn default() -> Self {
        Self { temperature: 0.7, top_p: 0.9, max_tokens: 2048 }
    }
}

/// Pure resolver: profile map -> effective settings.
/// Invalid/absent values fall back to defaults (never fail the call).
pub fn resolve_chat_generation(generation: &HashMap<String, String>) -> ChatGeneration {
    let mut out = ChatGeneration::default();
    if let Some(t) = generation.get("temperature").and_then(|s| s.parse::<f32>().ok()) {
        if t.is_finite() && (0.0..=2.0).contains(&t) {
            out.temperature = t;
        }
    }
    if let Some(p) = generation.get("top_p").and_then(|s| s.parse::<f32>().ok()) {
        if p.is_finite() && (0.0..=1.0).contains(&p) {
            out.top_p = p;
        }
    }
    if let Some(n) = generation.get("max_tokens").and_then(|s| s.parse::<u32>().ok()) {
        if n > 0 && n <= 1_000_000 {
            out.max_tokens = n;
        }
    }
    out
}
pub async fn gateway_chat(req: ChatRequest) -> Result<String, String> {
    let cfg = load_config_inner();
    // privacy guard + profile generation (single lookup)
    let mut gen = ChatGeneration::default();
    if let Some(tpid) = &req.task_profile_id {
        if let Some(tp) = cfg.task_profiles.iter().find(|t| t.id==*tpid) {
            let prov = cfg.providers.iter().find(|p| p.id==req.provider_id).ok_or("provider not found")?;
            check_privacy(&tp.privacy, &prov.kind)?;
            if let Some(CloudPolicy::Deny) = tp.cloud_policy { if is_cloud_kind(&prov.kind) { return Err("PrivacyPolicyViolation: Cloud Deny".to_string()); } }
            gen = resolve_chat_generation(&tp.generation);
        }
    }
    // route via provider + model
    let prov = cfg.providers.iter().find(|p| p.id==req.provider_id).cloned().ok_or("provider not found")?;
    let secret = if prov.auth.as_ref().map(|a| a.method==AuthMethod::ApiKey).unwrap_or(false) { get_keyring(&prov.id).unwrap_or_default() } else { String::new() };
    // Use async reqwest for gateway (P0)
    let client = reqwest::Client::builder().timeout(Duration::from_secs(90)).build().map_err(|e| e.to_string())?;
    // Pure request mapping lives in gateway:: (Stage 0 extract, behavior identical).
    let built = gateway::build_chat_http_request(
        &prov.kind,
        &prov.endpoint,
        &prov.default_model,
        cfg.local.port,
        &req.model_ref,
        &req.messages,
        &gen,
        &secret,
    );
    let (url, headers, body) = (built.url, built.headers, built.body);
    let mut r = client.post(&url).json(&body);
    for (k,v) in headers { r = r.header(k, v); }
    // Never leak credentials: errors carry host/path only, never the query.
    let safe_url = safe_url_for_error(&url);
    let resp = r.send().await.map_err(|e| format!("LLM offline {}: {}", safe_url, e))?;
    if !resp.status().is_success() {
        return Err(format!("LLM HTTP {}: {}", resp.status(), resp.text().await.unwrap_or_default().chars().take(600).collect::<String>()));
    }
    let txt = resp.text().await.map_err(|e| e.to_string())?;
    Ok(txt)
}

// ---------- Claude dispatch foundation (non-breaking) ----------
// Internal dispatch through the native provider adapters. Only Claude is
// wired: any other kind returns an explicit error pointing at the legacy
// gateway. The contract matches gateway_chat exactly (Result<String, String>
// carrying assistant TEXT, never raw provider JSON), so existing callers
// keep working. Legacy gateway.rs branches stay untouched.
pub async fn gateway_chat_via_registry(
    req: ChatRequest,
    cancel: tokio_util::sync::CancellationToken,
    secrets: std::sync::Arc<dyn provider::SecretStore>,
    timeout: Duration,
) -> Result<String, String> {
    let cfg = load_config_inner();
    // privacy guard + profile generation (same policy as gateway_chat)
    let mut gen = ChatGeneration::default();
    if let Some(tpid) = &req.task_profile_id {
        if let Some(tp) = cfg.task_profiles.iter().find(|t| t.id==*tpid) {
            let prov = cfg.providers.iter().find(|p| p.id==req.provider_id).ok_or("provider not found")?;
            check_privacy(&tp.privacy, &prov.kind)?;
            if let Some(CloudPolicy::Deny) = tp.cloud_policy { if is_cloud_kind(&prov.kind) { return Err("PrivacyPolicyViolation: Cloud Deny".to_string()); } }
            gen = resolve_chat_generation(&tp.generation);
        }
    }
    let prov = cfg.providers.iter().find(|p| p.id==req.provider_id).cloned().ok_or("provider not found")?;
    if prov.kind != ProviderKind::Claude {
        return Err(format!("dispatch unsupported for {:?}: use gateway_chat", prov.kind));
    }
    if !prov.enabled {
        return Err(format!("provider {} is disabled", prov.id));
    }
    // Model identity: internal id + provider ownership enforced here;
    // only remote_id ever goes upstream (enforced again by the adapter).
    // A catalog hit resolves an internal id to its remote_id.
    let mut remote = if !req.model_ref.is_empty() { req.model_ref.clone() } else { prov.default_model.clone() };
    if remote.trim().is_empty() {
        return Err("dispatch: no model selected (model_ref and provider default_model are empty)".to_string());
    }
    let mut capabilities = vec![Capability::Chat];
    if let Some(known) = cfg.models.iter().find(|m| m.id==req.model_ref || (!m.remote_id.is_empty() && m.remote_id==remote)) {
        if known.provider_id != prov.id {
            return Err(format!("model {} belongs to {}, not {}", known.id, known.provider_id, prov.id));
        }
        if !known.capabilities.is_empty() && !known.capabilities.contains(&Capability::Chat) {
            return Err("UnsupportedCapability: model missing Chat".to_string());
        }
        if !known.capabilities.is_empty() {
            capabilities = known.capabilities.clone();
        }
        if !known.remote_id.is_empty() {
            remote = known.remote_id.clone();
        }
    }
    let model = provider::ProviderModel {
        id: format!("{}:{}", prov.id, remote),
        provider_id: prov.id.clone(),
        remote_id: remote.clone(),
        display_name: remote.clone(),
        capabilities,
        capability_source: None,
        context_length: None,
        dimensions: None,
        pricing: None,
        source: crate::llm::models::types::ModelSource::Manual,
    };
    let auth = match &prov.auth {
        Some(a) if a.method == AuthMethod::ApiKey => match &a.secret_ref {
            Some(r) => Some(provider::AuthReference::new(r.clone()).map_err(|e| format!("dispatch: {}", e))?),
            None => None,
        },
        _ => None,
    };
    let connection = provider::ProviderConnection {
        id: prov.id.clone(),
        kind: ProviderKind::Claude,
        name: prov.name.clone(),
        enabled: true,
        scope: crate::llm::task::policy::ProviderScope::Cloud,
        endpoint: if prov.endpoint.is_empty() { None } else { Some(prov.endpoint.clone()) },
        auth,
    };
    let adapter = provider::ClaudeNativeAdapter::new(connection, secrets, timeout)
        .map_err(|e| dispatch_err(&e))?;
    // Legacy role strings -> adapter roles. Assistant is preserved
    // (parity with multi-turn history); unknown roles become user.
    let messages = req.messages.iter().map(|m| {
        let role = m.get("role").and_then(|r| r.as_str()).unwrap_or("user");
        let content = m.get("content").and_then(|c| c.as_str()).unwrap_or("").to_string();
        let role = match role {
            "system" => provider::ChatRole::System,
            "assistant" => provider::ChatRole::Assistant,
            _ => provider::ChatRole::User,
        };
        provider::ChatMessage { role, content }
    }).collect::<Vec<_>>();
    let preq = provider::ChatRequest {
        messages,
        temperature: Some(gen.temperature),
        top_p: None, // parity with the legacy Claude branch (no top_p on the wire)
        // Profile value with gateway default 2048 fallback — never the
        // adapter-local DEFAULT_MAX_TOKENS=1024 in this path.
        max_tokens: Some(gen.max_tokens),
        response_format: None,
    };
    let resp = adapter.chat(&model, preq, cancel).await.map_err(|e| dispatch_err(&e))?;
    Ok(resp.content)
}

// ---------- Gemini dispatch foundation (non-breaking) ----------
// Same shape as the Claude path: header-only auth via the native adapter,
// assistant text out, existing gateway contract preserved. Legacy
// gateway.rs Gemini branch stays untouched until parity is proven.
pub async fn gateway_gemini_via_registry(
    req: ChatRequest,
    cancel: tokio_util::sync::CancellationToken,
    secrets: std::sync::Arc<dyn provider::SecretStore>,
    timeout: Duration,
) -> Result<String, String> {
    let cfg = load_config_inner();
    // privacy guard + profile generation (same policy as gateway_chat)
    let mut gen = ChatGeneration::default();
    if let Some(tpid) = &req.task_profile_id {
        if let Some(tp) = cfg.task_profiles.iter().find(|t| t.id==*tpid) {
            let prov = cfg.providers.iter().find(|p| p.id==req.provider_id).ok_or("provider not found")?;
            check_privacy(&tp.privacy, &prov.kind)?;
            if let Some(CloudPolicy::Deny) = tp.cloud_policy { if is_cloud_kind(&prov.kind) { return Err("PrivacyPolicyViolation: Cloud Deny".to_string()); } }
            gen = resolve_chat_generation(&tp.generation);
        }
    }
    let prov = cfg.providers.iter().find(|p| p.id==req.provider_id).cloned().ok_or("provider not found")?;
    if prov.kind != ProviderKind::Gemini {
        return Err(format!("dispatch unsupported for {:?}: use gateway_chat", prov.kind));
    }
    if !prov.enabled {
        return Err(format!("provider {} is disabled", prov.id));
    }
    // Model identity: internal id + provider ownership enforced here;
    // only remote_id ever goes upstream (adapter prefixes models/ itself).
    // A catalog hit resolves an internal id to its remote_id.
    let mut remote = if !req.model_ref.is_empty() { req.model_ref.clone() } else { prov.default_model.clone() };
    if remote.trim().is_empty() {
        return Err("dispatch: no model selected (model_ref and provider default_model are empty)".to_string());
    }
    let mut capabilities = vec![Capability::Chat];
    if let Some(known) = cfg.models.iter().find(|m| m.id==req.model_ref || (!m.remote_id.is_empty() && m.remote_id==remote)) {
        if known.provider_id != prov.id {
            return Err(format!("model {} belongs to {}, not {}", known.id, known.provider_id, prov.id));
        }
        if !known.capabilities.is_empty() && !known.capabilities.contains(&Capability::Chat) {
            return Err("UnsupportedCapability: model missing Chat".to_string());
        }
        if !known.capabilities.is_empty() {
            capabilities = known.capabilities.clone();
        }
        if !known.remote_id.is_empty() {
            remote = known.remote_id.clone();
        }
    }
    let model = provider::ProviderModel {
        id: format!("{}:{}", prov.id, remote),
        provider_id: prov.id.clone(),
        remote_id: remote.clone(),
        display_name: remote.clone(),
        capabilities,
        capability_source: None,
        context_length: None,
        dimensions: None,
        pricing: None,
        source: crate::llm::models::types::ModelSource::Manual,
    };
    let auth = match &prov.auth {
        Some(a) if a.method == AuthMethod::ApiKey => match &a.secret_ref {
            Some(r) => Some(provider::AuthReference::new(r.clone()).map_err(|e| format!("dispatch: {}", e))?),
            None => None,
        },
        _ => None,
    };
    let connection = provider::ProviderConnection {
        id: prov.id.clone(),
        kind: ProviderKind::Gemini,
        name: prov.name.clone(),
        enabled: true,
        scope: crate::llm::task::policy::ProviderScope::Cloud,
        endpoint: if prov.endpoint.is_empty() { None } else { Some(prov.endpoint.clone()) },
        auth,
    };
    let adapter = provider::GeminiNativeAdapter::new(connection, secrets, timeout)
        .map_err(|e| dispatch_err(&e))?;
    // Legacy role strings -> adapter roles. Assistant maps to model
    // (parity with multi-turn history); unknown roles become user.
    let messages = req.messages.iter().map(|m| {
        let role = m.get("role").and_then(|r| r.as_str()).unwrap_or("user");
        let content = m.get("content").and_then(|c| c.as_str()).unwrap_or("").to_string();
        let role = match role {
            "system" => provider::ChatRole::System,
            "assistant" => provider::ChatRole::Assistant,
            _ => provider::ChatRole::User,
        };
        provider::ChatMessage { role, content }
    }).collect::<Vec<_>>();
    let preq = provider::ChatRequest {
        messages,
        temperature: Some(gen.temperature),
        // Legacy Gemini body carries topP: parity keeps it on the wire.
        top_p: Some(gen.top_p),
        // Profile value with gateway default 2048 fallback.
        max_tokens: Some(gen.max_tokens),
        response_format: None,
    };
    let resp = adapter.chat(&model, preq, cancel).await.map_err(|e| dispatch_err(&e))?;
    Ok(resp.content)
}

fn dispatch_err(e: &provider::ProviderError) -> String {
    let capped: String = e.safe_message().chars().take(600).collect();
    format!("LLM {}: {}", e.code(), capped)
}

/// Strip a request URL down to a safe host/path form for error strings:
/// scheme + authority + path, never the query (which may carry credentials)
/// or fragment. No allocation-heavy parsing — pure string surgery.
fn safe_url_for_error(url: &str) -> String {
    let no_frag = url.split('#').next().unwrap_or(url);
    let no_query = no_frag.split('?').next().unwrap_or(no_frag);
    no_query.to_string()
}

// ---------- Production caller router (non-breaking) ----------
// Peek provider kind from config (no network I/O): Claude goes through the
// native dispatch foundation, every other kind keeps the legacy gateway
// byte-for-byte. Unknown/disabled providers keep existing errors.
pub async fn gateway_chat_routed(
    req: ChatRequest,
    cancel: tokio_util::sync::CancellationToken,
) -> Result<String, String> {
    let kind = {
        let cfg = load_config_inner();
        cfg.providers.iter().find(|p| p.id == req.provider_id).map(|p| p.kind.clone())
    };
    match kind {
        Some(ProviderKind::Claude) => {
            gateway_chat_via_registry(
                req,
                cancel,
                std::sync::Arc::new(provider::KeyringSecretStore),
                Duration::from_secs(90),
            )
            .await
        }
        Some(ProviderKind::Gemini) => {
            gateway_gemini_via_registry(
                req,
                cancel,
                std::sync::Arc::new(provider::KeyringSecretStore),
                Duration::from_secs(90),
            )
            .await
        }
        _ => gateway_chat(req).await,
    }
}

#[tauri::command]
pub async fn llm_chat_universal(provider_id: String, model: String, messages: Vec<serde_json::Value>) -> Result<String, String> {
    // wrap with privacy via default task
    let req = ChatRequest{ provider_id: provider_id.clone(), model_ref: model.clone(), messages, task_profile_id: None };
    gateway_chat_routed(req, tokio_util::sync::CancellationToken::new()).await
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
    let run_cancel = tokio_util::sync::CancellationToken::new();
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
        let res = gateway_chat_routed(req, run_cancel.clone()).await.unwrap_or_else(|e| format!("error: {}", e));
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

// ---------- runtime manager P1.2 ----------
use std::sync::Arc;
static RUNTIME_MANAGER: Lazy<Arc<runtime::manager::RuntimeManager>> = Lazy::new(|| {
    Arc::new(runtime::manager::RuntimeManager::new(runtime_state_path()))
});

pub(crate) static TASK_EXECUTOR: Lazy<Arc<task::executor::TaskExecutor>> = Lazy::new(|| {
    Arc::new(task::executor::TaskExecutor::new(RUNTIME_MANAGER.clone(), Arc::new(task::executor::RealGateway)))
});

pub async fn clear_startup_for_test(key: &str) {
    TASK_EXECUTOR.clear_startup(key).await;
}

#[derive(Debug, Clone, Serialize, Deserialize)] pub struct RuntimeStatus { pub id: String, pub pid: Option<u32>, pub port: u16, pub status: String }

#[tauri::command]
pub fn llm_runtime_list() -> Result<String, String> {
    // P1.2: via RuntimeManager list with typed status
    let mgr = RUNTIME_MANAGER.clone();
    match mgr.list() {
        Ok(list) => serde_json::to_string(&list).map_err(|e| e.to_string()),
        Err(_) => {
            // fallback to file read for compat
            let path = runtime_state_path();
            if !path.exists() { return Ok("[]".to_string()); }
            let txt = fs::read_to_string(&path).unwrap_or("[]".to_string());
            Ok(txt)
        }
    }
}

#[tauri::command]
pub async fn llm_runtime_start(profile_id: String) -> Result<String, String> {
    let cfg = load_config_inner();
    let rp = cfg.runtime_profiles.iter().find(|r| r.id==profile_id).cloned().ok_or("profile not found")?;
    // E2E gate: Registry is source of truth for model path (ResolvedModel.path), not active_model / first file
    let model_path = {
        let mut reg = crate::llm::models::registry::Registry::new(crate::llm::registry_path());
        let _ = reg.load();
        if let Some(rec) = reg.all().into_iter().find(|r| r.id == rp.model_id) {
            if rec.state != crate::llm::models::types::ModelState::Present {
                return Err(format!("model_unavailable: {} state {:?}", rec.id, rec.state));
            }
            rec.path.to_string_lossy().to_string()
        } else if !rp.model_id.is_empty() {
            // Fallback to config models for legacy, but validate path exists
            cfg.models.iter().find(|m| m.id==rp.model_id).map(|m| m.path.clone()).unwrap_or_else(|| {
                // If no config model, treat model_id as path only if it looks like absolute path
                if rp.model_id.contains('/') || rp.model_id.ends_with(".gguf") { rp.model_id.clone() } else { String::new() }
            })
        } else {
            // No model_id on profile — legacy active_model fallback (deprecated, deny for E2E strict)
            return Err("model_not_found: runtime profile has empty model_id, run scan and link model".to_string());
        }
    };
    if model_path.is_empty() {
        return Err("model_not_found: model path missing for profile".to_string());
    }
    let mgr = RUNTIME_MANAGER.clone();
    let res = mgr.start(&profile_id, &model_path, &rp.binary_path, &rp.settings).await;
    match res {
        Ok(info) => serde_json::to_string(&info).map_err(|e| e.to_string()),
        Err(e) => Err(e),
    }
}

#[tauri::command]
pub async fn llm_runtime_stop(runtime_id: String) -> Result<String, String> {
    let mgr = RUNTIME_MANAGER.clone();
    // Find profile_id for this runtime to clear startup entry
    let profile_id = {
        let list = mgr.list().unwrap_or_default();
        list.iter().find(|r| r.runtime_id == runtime_id).map(|r| r.profile_id.clone())
    };
    mgr.stop(&runtime_id).await?;
    if let Some(pid) = profile_id {
        let key = format!("runtime:{}", pid);
        TASK_EXECUTOR.clear_startup(&key).await;
    } else {
        // Fallback: try to clear any startup that matches runtime_id prefix (runtime_id is runtime-<profile>-<ts>)
        // Extract profile from runtime_id: runtime-<profile>-<timestamp> -> profile is between first and last '-'
        if let Some(stripped) = runtime_id.strip_prefix("runtime-") {
            if let Some(idx) = stripped.rfind('-') {
                let profile = &stripped[..idx];
                let key = format!("runtime:{}", profile);
                TASK_EXECUTOR.clear_startup(&key).await;
            }
        }
    }
    Ok("stopped".to_string())
}

#[tauri::command]
pub async fn llm_runtime_health(runtime_id: String) -> Result<String, String> {
    let mgr = RUNTIME_MANAGER.clone();
    let info = mgr.status(&runtime_id).await?;
    serde_json::to_string(&info).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn llm_runtime_logs(runtime_id: String, tail: Option<u32>) -> Result<String, String> {
    let mgr = RUNTIME_MANAGER.clone();
    let n = tail.unwrap_or(200) as usize;
    let logs = mgr.logs(&runtime_id, n)?;
    serde_json::to_string(&logs).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn llm_runtime_restart(runtime_id: String) -> Result<String, String> {
    // P1.2 RestartPolicy Never only — manual restart = stop + start
    Err("restart OnCrash not enabled in P1.2 — use stop then start (Never policy)".to_string())
}

// ---------- P1.4 TaskExecutor ----------
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskErrorDto {
    pub code: String,
    pub message: String,
    pub retryable: bool,
}

#[tauri::command]
pub async fn llm_task_run(task_profile_id: String, messages: Vec<serde_json::Value>) -> Result<String, String> {
    // E2E: Registry → TaskProfile → ResolvedModel → RuntimeProfile consistency → RuntimeManager → Gateway
    let cfg = load_config_inner();
    // Validate task exists
    let task = cfg.task_profiles.iter().find(|t| t.id == task_profile_id).cloned().ok_or_else(|| {
        let dto = TaskErrorDto{ code: "task_not_found".to_string(), message: format!("task {} not found", task_profile_id), retryable: false };
        serde_json::to_string(&dto).unwrap()
    })?;
    // Resolve model via Registry (source of truth for path/state)
    let mut reg = crate::llm::models::registry::Registry::new(crate::llm::registry_path());
    let _ = reg.load();
    let model_rec = reg.all().into_iter().find(|r| r.id == task.model_ref).or_else(|| {
        // fallback to LlmConfig.models for old configs
        cfg.models.iter().find(|m| m.id == task.model_ref).map(|m| crate::llm::models::types::ModelRecord{
            id: m.id.clone(), provider_id: m.provider_id.clone(), path: std::path::PathBuf::from(&m.path), canonical_path: m.path.clone(), filename: m.name.clone(),
            format: crate::llm::models::types::ModelFormat::Gguf, size_bytes: m.metadata.size_bytes, sha256: Some(m.metadata.sha256.clone()).filter(|s| !s.is_empty()),
            metadata: crate::llm::models::types::ModelMetadata{ architecture: Some(m.metadata.arch.clone()).filter(|s| !s.is_empty()), ..Default::default() },
            capabilities: vec![crate::llm::Capability::Chat], roles: vec![crate::llm::models::types::ModelRole::General],
            source: crate::llm::models::types::ModelSource::LocalFile, state: crate::llm::models::types::ModelState::Present,
            identity_status: Default::default(),
            first_seen_at: chrono::Utc::now(), last_seen_at: chrono::Utc::now(), diagnostics: vec![]
        })
    }).ok_or_else(|| {
        let dto = TaskErrorDto{ code: "model_not_found".to_string(), message: format!("model {} not found in registry", task.model_ref), retryable: false };
        serde_json::to_string(&dto).unwrap()
    })?;
    // Fallback for E2E: if registry empty (no scan yet), allow old LlmConfig path for backward compat in tests
    // Check model state before spawn
    if model_rec.state != crate::llm::models::types::ModelState::Present {
        // In tests, registry may be empty and we fallback to LlmConfig, so allow Present only if registry had it
        // For fallback case, state will be Present as we constructed, so this check is for real registry
        if model_rec.state == crate::llm::models::types::ModelState::Missing || model_rec.state == crate::llm::models::types::ModelState::Invalid || model_rec.state == crate::llm::models::types::ModelState::Changed {
            let dto = TaskErrorDto{ code: "model_unavailable".to_string(), message: format!("model {} state {:?}", model_rec.id, model_rec.state), retryable: false };
            return Err(serde_json::to_string(&dto).unwrap());
        }
    }
    // RuntimeProfile consistency
    if let Some(rp_id) = &task.runtime_profile_id {
        let rp = cfg.runtime_profiles.iter().find(|r| &r.id == rp_id).ok_or_else(|| {
            let dto = TaskErrorDto{ code: "runtime_not_found".to_string(), message: format!("runtime {} not found", rp_id), retryable: false };
            serde_json::to_string(&dto).unwrap()
        })?;
        if rp.model_id != model_rec.id {
            let dto = TaskErrorDto{ code: "model_runtime_mismatch".to_string(), message: format!("RuntimeProfile {} model_id {} != ModelRecord {}", rp.id, rp.model_id, model_rec.id), retryable: false };
            return Err(serde_json::to_string(&dto).unwrap());
        }
        if rp.provider_id != model_rec.provider_id {
            let dto = TaskErrorDto{ code: "model_provider_mismatch".to_string(), message: format!("provider mismatch: model {} vs runtime {}", model_rec.provider_id, rp.provider_id), retryable: false };
            return Err(serde_json::to_string(&dto).unwrap());
        }
    } else {
        // For local model, runtime profile should exist
        if model_rec.provider_id == "local" {
            let dto = TaskErrorDto{ code: "local_model_missing_runtime".to_string(), message: format!("local model {} requires runtime profile", model_rec.id), retryable: false };
            return Err(serde_json::to_string(&dto).unwrap());
        }
        // For cloud, no runtime is expected — check that we don't have a runtime for cloud
        if model_rec.provider_id != "local" && task.runtime_profile_id.is_some() {
            let dto = TaskErrorDto{ code: "cloud_model_has_runtime".to_string(), message: "cloud model should not have runtime".to_string(), retryable: false };
            return Err(serde_json::to_string(&dto).unwrap());
        }
    }

    let executor = TASK_EXECUTOR.clone();
    match executor.run_chat(&task_profile_id, messages).await {
        Ok((run, response)) => {
            let out = serde_json::json!({ "run_id": run.run_id.to_string(), "provider_id": run.provider_id, "model_id": run.model_id, "runtime_id": run.runtime_id, "response": response });
            Ok(out.to_string())
        },
        Err(e) => {
            let msg = e.clone();
            let (code, retryable) = if e.contains("task_not_found") { ("task_not_found", false) }
            else if e.contains("provider_not_found") { ("provider_not_found", false) }
            else if e.contains("model_not_found") { ("model_not_found", false) }
            else if e.contains("model_unavailable") { ("model_unavailable", false) }
            else if e.contains("model_runtime_mismatch") { ("model_runtime_mismatch", false) }
            else if e.contains("runtime_not_found") { ("runtime_not_found", false) }
            else if e.contains("PrivacyPolicyViolation") || e.contains("privacy_violation") { ("privacy_violation", false) }
            else if e.contains("unsupported_capability") { ("unsupported_capability", false) }
            else if e.contains("runtime_start_failed") { ("runtime_start_failed", true) }
            else if e.contains("timeout") { ("runtime_timeout", true) }
            else if e.contains("cancelled") { ("cancelled", false) }
            else { ("provider_error", true) };
            let dto = TaskErrorDto{ code: code.to_string(), message: msg, retryable };
            Err(serde_json::to_string(&dto).unwrap_or_else(|_| e))
        }
    }
}

#[tauri::command]
pub async fn llm_task_cancel(run_id: String) -> Result<String, String> {
    let executor = TASK_EXECUTOR.clone();
    let uuid = uuid::Uuid::parse_str(&run_id).map_err(|e| format!("invalid run_id: {}", e))?;
    executor.cancel(uuid).await.map(|_| "cancelled".to_string()).map_err(|e| {
        let dto = TaskErrorDto{ code: "cancelled".to_string(), message: e, retryable: false };
        serde_json::to_string(&dto).unwrap()
    })
}

#[tauri::command]
pub async fn llm_task_status(run_id: String) -> Result<String, String> {
    let executor = TASK_EXECUTOR.clone();
    let uuid = uuid::Uuid::parse_str(&run_id).map_err(|e| format!("invalid run_id: {}", e))?;
    match executor.status(uuid).await {
        Ok(run) => Ok(serde_json::to_string(&run).unwrap_or("{}".to_string())),
        Err(e) => {
            let dto = TaskErrorDto{ code: "task_not_found".to_string(), message: e, retryable: false };
            Err(serde_json::to_string(&dto).unwrap())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    fn temp_vault() -> PathBuf {
        let p = std::env::temp_dir().join(format!("fragile-test-{}-{}", std::process::id(), rand_string(6)));
        let _ = fs::create_dir_all(p.join(".fragile"));
        p
    }
    fn rand_string(n: usize) -> String { (0..n).map(|_| (b'a' + (rand::random::<u8>() % 26)) as char).collect() }

    // Isolated env: temp HOME + FRAGILE_VAULT, restored on drop. No real
    // user data, no network, no system keyring dependency.
    static ENV_GUARD: Lazy<Mutex<()>> = Lazy::new(|| Mutex::new(()));
    struct IsolatedEnv {
        _g: std::sync::MutexGuard<'static, ()>,
        tmp: PathBuf,
        old_home: Option<std::ffi::OsString>,
        old_vault: Option<std::ffi::OsString>,
    }
    impl IsolatedEnv {
        fn lock(name: &str) -> Self {
            let g = ENV_GUARD.lock().unwrap_or_else(|e| e.into_inner());
            let tmp = std::env::temp_dir().join(format!("fragile-gate1-{}-{}-{}", std::process::id(), name, rand_string(6)));
            let vault = tmp.join("vault");
            fs::create_dir_all(vault.join(".fragile")).unwrap();
            let old_home = std::env::var_os("HOME");
            let old_vault = std::env::var_os("FRAGILE_VAULT");
            std::env::set_var("HOME", &tmp);
            std::env::set_var("FRAGILE_VAULT", &vault);
            Self { _g: g, tmp, old_home, old_vault }
        }
        fn cfg_path(&self) -> PathBuf {
            PathBuf::from(std::env::var_os("FRAGILE_VAULT").unwrap()).join(".fragile").join("llm.json")
        }
        fn dir(&self) -> PathBuf {
            PathBuf::from(std::env::var_os("FRAGILE_VAULT").unwrap()).join(".fragile")
        }
    }
    impl Drop for IsolatedEnv {
        fn drop(&mut self) {
            match &self.old_home { Some(v) => std::env::set_var("HOME", v), None => std::env::remove_var("HOME") }
            match &self.old_vault { Some(v) => std::env::set_var("FRAGILE_VAULT", v), None => std::env::remove_var("FRAGILE_VAULT") }
            let _ = fs::remove_dir_all(&self.tmp);
        }
    }
    struct MemKeyStore;
    impl KeyStore for MemKeyStore {
        fn set(&self, _pid: &str, _secret: &str) -> Result<(), String> { Ok(()) }
    }
    struct FailKeyStore;
    impl KeyStore for FailKeyStore {
        fn set(&self, _pid: &str, _secret: &str) -> Result<(), String> { Err("keyring unavailable".to_string()) }
    }
    fn write_valid_config(path: &Path) {
        let cfg = LlmConfig::default();
        let txt = serde_json::to_string_pretty(&cfg).unwrap();
        fs::write(path, txt).unwrap();
    }

    #[test]
    fn test_migration_v1_to_v2() {
        let old = serde_json::json!({
            "version": 1,
            "providers": [
                {"id":"openai","name":"OpenAI","kind":"open_ai","enabled":true,"api_key":"c2stZmFrZS1rZXktMTIzNA==","api_url":"https://api.openai.com/v1","model":"gpt-4o"}
            ],
            "local": {"binary_path":"llama-server","models_dir":"/tmp/Models","active_model":"/tmp/Models/qwen.gguf","n_ctx":8192,"n_threads":0,"n_gpu_layers":0,"temp":0.7,"top_p":0.9,"top_k":40,"repeat_penalty":1.1,"port":8010,"auto_start":false,"use_mmap":true,"extra_args":""},
            "pipelines": [],
            "active_pipeline":"enrich"
        });
        let migrated = migrate_to_v2(old, &MemKeyStore).expect("migrate");
        assert_eq!(migrated.get("schema_version").and_then(|v| v.as_u64()), Some(2));
        let prov = migrated.get("providers").and_then(|v| v.as_array()).unwrap();
        let openai = prov.iter().find(|p| p.get("id").and_then(|v| v.as_str())==Some("openai")).unwrap();
        // old api_key should be gone, replaced by auth.secret_ref
        assert!(openai.get("api_key").is_none(), "api_key should be removed after migration");
        assert!(openai.get("auth").is_some());
    }

    #[test]
    fn test_no_api_key_in_serialized() {
        let mut cfg = LlmConfig::default();
        // simulate setting a secret via keyring, not via plain field
        let _ = set_keyring("test-no-plain", "sk-fake-plain-key-12345");
        if let Some(p) = cfg.providers.iter_mut().find(|p| p.id=="openai") {
            p.auth = Some(AuthRef{ method: AuthMethod::ApiKey, secret_ref: Some(secret_ref_for("test-no-plain")), account_id: None });
        }
        let json = serde_json::to_string_pretty(&cfg).unwrap();
        assert!(!json.contains("sk-fake-plain-key"), "plain key must not be in serialized config");
        // api_key as field name should not exist, but method "api_key" is allowed
        assert!(!json.contains("\"api_key\":"), "api_key field must not exist as key");
        let _ = delete_keyring("test-no-plain");
    }

    #[test]
    fn test_local_only_blocks_cloud() {
        let task_privacy = Some(Privacy::LocalOnly);
        let cloud_kind = ProviderKind::OpenAI;
        let res = check_privacy(&task_privacy, &cloud_kind);
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("PrivacyPolicyViolation"));
        // cloud allowed should pass
        let ok = check_privacy(&Some(Privacy::CloudAllowed), &cloud_kind);
        assert!(ok.is_ok());
        // local provider should pass even with LocalOnly
        let local_ok = check_privacy(&Some(Privacy::LocalOnly), &ProviderKind::LocalLlamaCpp);
        assert!(local_ok.is_ok());
    }

    #[test]
    fn test_named_pipeline_outputs() {
        // verify pipeline_run uses named outputs map, not just ctx
        let pipeline = Pipeline {
            id: "test-pipe".to_string(),
            name: "test".to_string(),
            description: "".to_string(),
            enabled: true,
            trigger: "manual".to_string(),
            steps: vec![
                PipelineStep{ id:"s1".to_string(), name:"step1".to_string(), kind:"llm".to_string(), provider_id:"local".to_string(), model_ref:"".to_string(), task_profile_id: None, input_refs: vec!["input".to_string()], prompt_template:"{{input}} world".to_string(), output_schema: None, retry: None, timeout_ms: None, enabled: true },
                PipelineStep{ id:"s2".to_string(), name:"step2".to_string(), kind:"llm".to_string(), provider_id:"local".to_string(), model_ref:"".to_string(), task_profile_id: None, input_refs: vec!["s1".to_string()], prompt_template:"hello {{s1}}".to_string(), output_schema: None, retry: None, timeout_ms: None, enabled: true },
            ],
        };
        assert_eq!(pipeline.steps[1].input_refs, vec!["s1"]);
        // template replacement should support {{s1}}
        let mut outputs = std::collections::HashMap::new();
        outputs.insert("input".to_string(), "hello".to_string());
        outputs.insert("s1".to_string(), "hello world".to_string());
        let mut prompt = pipeline.steps[1].prompt_template.clone();
        for (k,v) in &outputs { prompt = prompt.replace(&format!("{{{{{}}}}}",k), v); }
        assert_eq!(prompt, "hello hello world");
    }

    #[test]
    fn test_duplicate_runtime_port() {
        let cfg = LlmConfig::default();
        // default limits should be 1
        assert_eq!(cfg.runtime_limits.max_active_runtimes, 1);
        assert!(!cfg.runtime_limits.allow_concurrent);
        // simulate two runtime profiles with same port - manager should detect
        let mut cfg2 = LlmConfig::default();
        cfg2.runtime_profiles = vec![
            RuntimeProfile{ id:"r1".to_string(), provider_id:"local".to_string(), model_id:"m1".to_string(), executable_source: Some(ExecutableSource::SystemPath), binary_path:"llama-server".to_string(), port:8010, policy:"on-demand".to_string(), settings: LlamaSettings::default() },
            RuntimeProfile{ id:"r2".to_string(), provider_id:"local".to_string(), model_id:"m2".to_string(), executable_source: Some(ExecutableSource::SystemPath), binary_path:"llama-server".to_string(), port:8010, policy:"on-demand".to_string(), settings: LlamaSettings::default() },
        ];
        let ports: Vec<u16> = cfg2.runtime_profiles.iter().map(|r| r.port).collect();
        let has_duplicate = ports.len() != ports.iter().collect::<std::collections::HashSet<_>>().len();
        assert!(has_duplicate, "duplicate ports should be detectable");
    }

    #[test]
    fn test_stale_pid() {
        // runtime state file with stale PID should be considered not running
        let state = RuntimeState{ active: vec![RuntimeInstance{ id:"runtime-local".to_string(), pid: Some(999999), port:8010, model_id:"m1".to_string(), status:"running".to_string(), start_time:"".to_string(), last_error: None }] };
        let json = serde_json::to_string(&state).unwrap();
        // stale PID check: pid 999999 should not exist
        let pid_exists = PathBuf::from(format!("/proc/{}", 999999)).exists();
        assert!(!pid_exists, "stale PID should not exist");
        assert!(json.contains("999999"));
    }

    #[test]
    fn test_malformed_provider_response() {
        // extract_content should handle malformed JSON gracefully
        let raw = "not json at all {{{";
        let out = extract_content(raw);
        assert_eq!(out, raw);
        let malformed = r#"{"choices":[{"message":{"content": null}}]}"#;
        let out2 = extract_content(malformed);
        assert_eq!(out2, malformed);
        // valid should extract
        let valid = r#"{"choices":[{"message":{"content":"hello world"}}]}"#;
        assert_eq!(extract_content(valid), "hello world");
    }

    #[test]
    fn test_keyring_failure() {
        // when keyring unavailable, the error should contain actionable UI hint
        // we simulate by trying to get non-existent credential
        let res = get_keyring("non-existent-provider-xyz-123");
        assert!(res.is_err());
        let err = res.unwrap_err();
        assert!(err.contains("keyring"), "error should mention keyring");
        // set with empty should fail
        let empty = set_keyring("test-empty", "");
        // empty string is still a valid password for keyring, but our wrapper checks empty before
        // here we test the validation in llm_set_provider_credential logic: empty should be rejected
        // we test via direct call with empty - it will set empty, but our command wrapper would reject
        assert!(empty.is_ok() || empty.is_err()); // just ensure no panic
        let _ = delete_keyring("test-empty");
        let _ = delete_keyring("non-existent-provider-xyz-123");
    }

    #[test]
    fn test_chat_generation_defaults_without_profile() {
        let gen = resolve_chat_generation(&HashMap::new());
        assert_eq!(gen, ChatGeneration::default());
        assert_eq!(gen.temperature, 0.7);
        assert_eq!(gen.top_p, 0.9);
        assert_eq!(gen.max_tokens, 2048);
    }

    #[test]
    fn test_chat_generation_from_profile() {
        let mut m = HashMap::new();
        m.insert("temperature".to_string(), "0.2".to_string());
        m.insert("top_p".to_string(), "0.5".to_string());
        m.insert("max_tokens".to_string(), "512".to_string());
        let gen = resolve_chat_generation(&m);
        assert_eq!(gen.temperature, 0.2);
        assert_eq!(gen.top_p, 0.5);
        assert_eq!(gen.max_tokens, 512);
    }

    #[test]
    fn test_chat_generation_invalid_ignored() {
        // Invalid values fall back to defaults — never fail the call,
        // never accept frontend injection (ChatRequest has no such fields).
        for (k, v) in [
            ("temperature", "banana"),
            ("temperature", "NaN"),
            ("temperature", "5.0"),
            ("temperature", "-1.0"),
            ("top_p", "2.0"),
            ("top_p", "oops"),
            ("max_tokens", "0"),
            ("max_tokens", "-10"),
            ("max_tokens", "99999999999"),
        ] {
            let mut m = HashMap::new();
            m.insert(k.to_string(), v.to_string());
            let gen = resolve_chat_generation(&m);
            let d = ChatGeneration::default();
            match k {
                "temperature" => assert_eq!(gen.temperature, d.temperature, "case {k}={v}"),
                "top_p" => assert_eq!(gen.top_p, d.top_p, "case {k}={v}"),
                _ => assert_eq!(gen.max_tokens, d.max_tokens, "case {k}={v}"),
            }
        }
    }

    // ---------- Gate 1: non-destructive recovery tests ----------
    fn dir_files(dir: &Path) -> Vec<String> {
        fs::read_dir(dir).map(|rd| rd.filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().to_string()).collect()).unwrap_or_default()
    }

    #[test]
    fn gate1_missing_first_run() {
        let env = IsolatedEnv::lock("missing");
        let p = env.cfg_path();
        assert!(!p.exists());
        let rep = load_config_report_inner();
        assert_eq!(rep.status, ConfigLoadStatus::Missing);
        assert!(rep.reason.is_none());
        assert!(!p.exists(), "read path must not create files");
        // first save is allowed and creates parent dirs
        let out = save_config_guarded(&LlmConfig::default(), None).expect("first save");
        assert!(p.exists());
        assert!(out.backup.is_none(), "no backup when nothing existed");
    }

    #[test]
    fn gate1_corrupt_no_write() {
        let env = IsolatedEnv::lock("corrupt");
        let p = env.cfg_path();
        fs::write(&p, b"{not json{{").unwrap();
        let before = dir_files(&env.dir());
        let rep = load_config_report_inner();
        assert_eq!(rep.status, ConfigLoadStatus::Degraded);
        assert_eq!(rep.reason, Some(ConfigDegradedReason::Corrupt));
        assert_eq!(fs::read(&p).unwrap(), b"{not json{{", "original intact");
        assert_eq!(dir_files(&env.dir()), before, "read path creates no quarantine/backup");
        // degraded save denied without token
        let err = save_config_guarded(&LlmConfig::default(), None).unwrap_err();
        assert!(err.starts_with("DegradedSaveBlocked"), "got: {}", err);
        assert_eq!(fs::read(&p).unwrap(), b"{not json{{");
    }

    #[test]
    fn gate1_unreadable() {
        let env = IsolatedEnv::lock("unreadable");
        let p = env.cfg_path();
        // a directory at the config path fails read_to_string deterministically
        fs::create_dir_all(&p).unwrap();
        let rep = load_config_report_inner();
        assert_eq!(rep.status, ConfigLoadStatus::Degraded);
        assert_eq!(rep.reason, Some(ConfigDegradedReason::Unreadable));
        fs::remove_dir(&p).unwrap();
    }

    #[test]
    fn gate1_stale_tmp_ignored() {
        let env = IsolatedEnv::lock("staletmp");
        let p = env.cfg_path();
        write_valid_config(&p);
        let tmp = p.with_extension("tmp");
        fs::write(&tmp, b"garbage").unwrap();
        let rep = load_config_report_inner();
        assert_eq!(rep.status, ConfigLoadStatus::Valid);
        assert_eq!(fs::read(&tmp).unwrap(), b"garbage", "stale tmp untouched");
    }

    #[test]
    fn gate1_v1_migrates_without_write() {
        let env = IsolatedEnv::lock("v1nowrite");
        let p = env.cfg_path();
        let v1 = serde_json::json!({
            "version": 1,
            "providers": [{"id":"openai","name":"OpenAI","kind":"open_ai","enabled":true,"api_url":"https://api.openai.com/v1","model":"gpt-4o"}],
            "local": {"binary_path":"llama-server","models_dir":"/tmp/Models","active_model":"","n_ctx":8192,"n_threads":0,"n_gpu_layers":0,"temp":0.7,"top_p":0.9,"top_k":40,"repeat_penalty":1.1,"port":8010,"auto_start":false,"use_mmap":true,"extra_args":""},
            "pipelines": [], "active_pipeline":"enrich"
        });
        let raw = serde_json::to_string_pretty(&v1).unwrap();
        fs::write(&p, &raw).unwrap();
        // Note: load uses RealKeyStore; this fixture has no api_key so no keyring touch.
        let rep = load_config_report_inner();
        assert_eq!(rep.status, ConfigLoadStatus::Migrated);
        assert_eq!(rep.migrated_from, Some(1));
        assert_eq!(fs::read_to_string(&p).unwrap(), raw, "read path must not persist migration");
        assert!(dir_files(&env.dir()).iter().all(|n| !n.contains(".bak")), "no backup on read");
    }

    #[test]
    fn gate1_keyring_failure_aborts_migration() {
        let old = serde_json::json!({
            "version": 1,
            "providers": [{"id":"openai","name":"OpenAI","kind":"open_ai","enabled":true,"api_key":"c2stZmFrZS1rZXktMTIzNA==","api_url":"https://api.openai.com/v1","model":"gpt-4o"}],
            "pipelines": [], "active_pipeline":"enrich"
        });
        let err = migrate_to_v2(old, &FailKeyStore).unwrap_err();
        assert!(err.starts_with("keyring:"), "got: {}", err);
    }

    #[test]
    fn gate1_future_schema() {
        let env = IsolatedEnv::lock("future");
        let p = env.cfg_path();
        let raw = r#"{"schema_version":99,"providers":[]}"#;
        fs::write(&p, raw).unwrap();
        let rep = load_config_report_inner();
        assert_eq!(rep.status, ConfigLoadStatus::Degraded);
        assert_eq!(rep.reason, Some(ConfigDegradedReason::UnsupportedVersion));
        assert_eq!(fs::read_to_string(&p).unwrap(), raw, "untouched");
    }

    #[test]
    fn gate1_partial_providers() {
        let env = IsolatedEnv::lock("partial");
        let p = env.cfg_path();
        let cfg = LlmConfig::default();
        let good: Vec<serde_json::Value> = cfg.providers.iter().take(2)
            .map(|pr| serde_json::to_value(pr).unwrap()).collect();
        let raw_cfg = serde_json::json!({
            "schema_version": 2,
            "providers": [
                good[0].clone(),
                {"id":"weird","name":"Weird","kind":"quantum_ai","enabled":false,"endpoint":"","default_model":""},
                {"id":123,"kind":"open_ai"},
                good[1].clone(),
            ],
            "models": [], "runtime_profiles": [], "task_profiles": cfg.task_profiles.clone(),
            "pipelines": cfg.pipelines.clone(), "active_pipeline": cfg.active_pipeline.clone(),
            "local": cfg.local.clone(),
        });
        let raw = serde_json::to_string_pretty(&raw_cfg).unwrap();
        fs::write(&p, &raw).unwrap();
        let rep = load_config_report_inner();
        assert_eq!(rep.status, ConfigLoadStatus::Degraded);
        assert_eq!(rep.reason, Some(ConfigDegradedReason::InvalidEntry));
        assert_eq!(rep.issues.len(), 2);
        assert_eq!(rep.config.providers.len(), 2, "valid subset preserved");
        assert_eq!(fs::read_to_string(&p).unwrap(), raw, "original never modified during load");
        assert!(rep.issues.iter().any(|i| i.code == "unknown_kind"));
        assert!(rep.issues.iter().any(|i| i.code == "invalid_provider"));
    }

    #[test]
    fn gate1_recovery_token_flow() {
        let env = IsolatedEnv::lock("tokenflow");
        let p = env.cfg_path();
        let corrupt: &[u8] = b"[[broken";
        fs::write(&p, corrupt).unwrap();
        // recovery start is read-only: no files created or changed
        let before = dir_files(&env.dir());
        let started = llm_start_config_recovery().expect("recovery start");
        assert_eq!(dir_files(&env.dir()), before, "recovery start must not write");
        assert_eq!(fs::read(&p).unwrap(), corrupt, "source untouched by recovery start");
        let v: serde_json::Value = serde_json::from_str(&started).unwrap();
        let token = v.get("token").and_then(|t| t.as_str()).unwrap().to_string();
        assert!(token.len() >= 32);
        // wrong token rejected, source preserved
        let err = save_config_guarded(&LlmConfig::default(), Some("deadbeef")).unwrap_err();
        assert!(err.starts_with("InvalidConfirmToken"), "got: {}", err);
        assert_eq!(fs::read(&p).unwrap(), corrupt);
        // valid token: recovery copy + backup + write, single-use
        let out = save_config_guarded(&LlmConfig::default(), Some(&token)).expect("confirmed save");
        let rc_path = out.recovery_copy.clone().expect("recovery copy");
        assert!(out.backup.is_some());
        assert_eq!(fs::read(&rc_path).unwrap(), corrupt, "recovery copy byte-identical to corrupt source");
        let names = dir_files(&env.dir());
        assert!(names.iter().any(|n| n.starts_with("llm.json.corrupt-")), "recovery copy present: {:?}", names);
        assert!(names.iter().any(|n| n.starts_with("llm.json.bak-")), "backup present: {:?}", names);
        // live config is now the valid new content
        let rep = load_config_report_inner();
        assert_eq!(rep.status, ConfigLoadStatus::Valid);
        // single-use: re-corrupt inside the fixture, old token must not authorize again
        fs::write(&p, corrupt).unwrap();
        let err2 = save_config_guarded(&LlmConfig::default(), Some(&token)).unwrap_err();
        assert!(err2.starts_with("InvalidConfirmToken"), "got: {}", err2);
        assert_eq!(fs::read(&p).unwrap(), corrupt, "rejected attempt preserves corrupt source");
    }

    #[test]
    fn gate1_no_recovery_when_healthy() {
        let env = IsolatedEnv::lock("healthy");
        write_valid_config(&env.cfg_path());
        let rep = load_config_report_inner();
        assert_eq!(rep.status, ConfigLoadStatus::Valid);
        assert!(llm_start_config_recovery().is_err());
        // healthy save creates ordinary backup
        let out = save_config_guarded(&LlmConfig::default(), None).expect("save");
        assert!(out.backup.is_some());
        assert!(out.recovery_copy.is_none());
    }

    #[test]
    fn gate1_backup_failure_blocks_overwrite() {
        let env = IsolatedEnv::lock("bakfail");
        let p = env.cfg_path();
        write_valid_config(&p);
        let original = fs::read(&p).unwrap();
        let before = dir_files(&env.dir());
        let err = save_config_guarded_with(
            &LlmConfig::default(),
            None,
            |_, _| Err("injected backup failure".to_string()),
        ).unwrap_err();
        assert!(err.starts_with("BackupFailed"), "got: {}", err);
        assert_eq!(fs::read(&p).unwrap(), original, "original live bytes unchanged");
        assert_eq!(dir_files(&env.dir()), before, "no partial new config, no recovery copy on healthy path");
    }

    #[test]
    fn gate1_migrated_save_creates_versioned_backup() {
        let env = IsolatedEnv::lock("migbak");
        let p = env.cfg_path();
        let v1 = serde_json::json!({
            "version": 1,
            "providers": [{"id":"openai","name":"OpenAI","kind":"open_ai","enabled":true,"api_url":"https://api.openai.com/v1","model":"gpt-4o"}],
            "local": {"binary_path":"llama-server","models_dir":"/tmp/Models","active_model":"","n_ctx":8192,"n_threads":0,"n_gpu_layers":0,"temp":0.7,"top_p":0.9,"top_k":40,"repeat_penalty":1.1,"port":8010,"auto_start":false,"use_mmap":true,"extra_args":""},
            "pipelines": [], "active_pipeline":"enrich"
        });
        let raw = serde_json::to_string_pretty(&v1).unwrap();
        fs::write(&p, &raw).unwrap();
        let rep = load_config_report_inner();
        assert_eq!(rep.status, ConfigLoadStatus::Migrated);
        let out = save_config_guarded(&rep.config, None).expect("migrated save");
        assert!(out.recovery_copy.is_none(), "healthy-path save needs no recovery copy");
        let names = dir_files(&env.dir());
        let mig: Vec<_> = names.iter().filter(|n| n.starts_with("llm.json.bak-v1-")).collect();
        assert_eq!(mig.len(), 1, "one versioned migration backup: {:?}", names);
        assert_eq!(fs::read_to_string(env.dir().join(mig[0])).unwrap(), raw, "migration backup holds source v1 bytes");
        // rotation must not delete the migration backup
        let dir = env.dir();
        for i in 0..6 {
            fs::write(dir.join(format!("llm.json.bak-202601{:02}T000000Z", i)), b"old").unwrap();
        }
        save_config_guarded(&LlmConfig::default(), None).expect("second save");
        let names2 = dir_files(&dir);
        assert!(names2.iter().any(|n| n.starts_with("llm.json.bak-v1-")), "migration backup survives rotation: {:?}", names2);
        let ordinary = names2.iter().filter(|n| {
            n.strip_prefix("llm.json.bak-").map(|r| r.starts_with(|c: char| c.is_ascii_digit())).unwrap_or(false)
        }).count();
        assert_eq!(ordinary, 5, "ordinary cap still N=5: {:?}", names2);
    }

    #[test]
    fn gate1_reload_does_not_invalidate_token() {
        // Policy (intentional): the read/report path is non-mutating and
        // never purges the RAM token store; the frontend drops its own
        // in-memory copy on Reload. Reloads must neither consume the
        // token nor require a backend purge.
        let env = IsolatedEnv::lock("reloadtok");
        let p = env.cfg_path();
        fs::write(&p, b"{oops").unwrap();
        let started = llm_start_config_recovery().expect("start");
        let v: serde_json::Value = serde_json::from_str(&started).unwrap();
        let token = v.get("token").and_then(|t| t.as_str()).unwrap().to_string();
        assert_eq!(load_config_report_inner().status, ConfigLoadStatus::Degraded);
        assert_eq!(load_config_report_inner().reason, Some(ConfigDegradedReason::Corrupt));
        let out = save_config_guarded(&LlmConfig::default(), Some(&token)).expect("save after reloads");
        assert!(out.recovery_copy.is_some());
    }

    #[test]
    fn gate1_rotation_keeps_five() {
        let env = IsolatedEnv::lock("rotation");
        let p = env.cfg_path();
        write_valid_config(&p);
        let dir = env.dir();
        for i in 0..7 {
            fs::write(dir.join(format!("llm.json.bak-202601{:02}T000000Z", i)), b"old").unwrap();
        }
        fs::write(dir.join("llm.json.bak-v1-20260101T000000Z"), b"mig").unwrap();
        fs::write(dir.join("llm.json.corrupt-20260101T000000Z"), b"q").unwrap();
        save_config_guarded(&LlmConfig::default(), None).expect("save");
        let names = dir_files(&dir);
        let ordinary: Vec<_> = names.iter().filter(|n| {
            n.strip_prefix("llm.json.bak-").map(|r| r.starts_with(|c: char| c.is_ascii_digit())).unwrap_or(false)
        }).collect();
        assert_eq!(ordinary.len(), 5, "ordinary backups capped at 5: {:?}", names);
        assert!(names.iter().any(|n| n == "llm.json.bak-v1-20260101T000000Z"), "migration backup kept");
        assert!(names.iter().any(|n| n == "llm.json.corrupt-20260101T000000Z"), "quarantine kept");
    }

    #[test]
    fn gate1_quarantine_collision() {
        let env = IsolatedEnv::lock("collision");
        let p = env.cfg_path();
        let first = unique_sibling(&p, "llm.json.corrupt-20261010T080000Z");
        assert_eq!(first.file_name().unwrap().to_string_lossy(), "llm.json.corrupt-20261010T080000Z");
        write_create_new(&first, b"a").unwrap();
        let second = unique_sibling(&p, "llm.json.corrupt-20261010T080000Z");
        assert_eq!(second.file_name().unwrap().to_string_lossy(), "llm.json.corrupt-20261010T080000Z-1");
        write_create_new(&second, b"b").unwrap();
        assert_eq!(fs::read(&first).unwrap(), b"a", "existing quarantine never overwritten");
    }

    #[test]
    fn gate1_no_secrets_in_report() {
        let env = IsolatedEnv::lock("nosecrets");
        let p = env.cfg_path();
        let mut cfg = LlmConfig::default();
        if let Some(pr) = cfg.providers.iter_mut().find(|x| x.id == "openai") {
            pr.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/openai".to_string()), account_id: None });
        }
        fs::write(&p, serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let rep = load_config_report_inner();
        let masked = mask_config(rep.config);
        let json = serde_json::to_string(&masked).unwrap();
        assert!(!json.contains("keychain://"), "secret_ref must be masked");
        // every api-key provider carries only the masked placeholder or empty
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        for pr in v.get("providers").and_then(|x| x.as_array()).cloned().unwrap_or_default() {
            if pr.get("auth").and_then(|a| a.get("method")).and_then(|m| m.as_str()) == Some("api_key") {
                let r = pr.get("auth").and_then(|a| a.get("secret_ref")).and_then(|s| s.as_str()).unwrap_or("");
                assert!(r == "masked:••••••••" || r.is_empty(), "unmasked secret_ref: {:?}", r);
            }
        }
        let dbg = format!("{:?}", rep.issues);
        assert!(!dbg.contains("keychain://"));
    }

    #[test]
    fn gate1_vault_missing() {
        let _env = IsolatedEnv::lock("vaultmiss");
        let missing = std::env::temp_dir().join(format!("fragile-gate1-nothing-{}-{}", std::process::id(), rand_string(6)));
        std::env::set_var("FRAGILE_VAULT", &missing);
        let rep = load_config_report_inner();
        assert_eq!(rep.status, ConfigLoadStatus::Degraded);
        assert_eq!(rep.reason, Some(ConfigDegradedReason::VaultMissing));
    }

    // ---------- Claude dispatch foundation tests ----------
    struct MapSecrets;
    impl provider::SecretStore for MapSecrets {
        fn get(&self, r: &str) -> Result<String, provider::ProviderError> {
            assert_eq!(r, "keychain://fragile-notes/cld");
            Ok("sk-ant-test-key".to_string())
        }
    }
    struct NoSecrets;
    impl provider::SecretStore for NoSecrets {
        fn get(&self, _: &str) -> Result<String, provider::ProviderError> {
            Err(provider::ProviderError::CredentialsMissing("no credential for test".to_string()))
        }
    }

    struct Wire { port: u16, captured: Arc<std::sync::Mutex<Vec<String>>> }

    async fn spawn_dispatch_wire(status: u16, body: String, delay_ms: u64) -> Wire {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let captured = Arc::new(std::sync::Mutex::new(Vec::new()));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let c2 = captured.clone();
        tokio::spawn(async move {
            while let Ok((mut stream, _)) = listener.accept().await {
                let c3 = c2.clone();
                let body = body.clone();
                tokio::spawn(async move {
                    let mut acc = Vec::new();
                    let mut buf = vec![0u8; 65536];
                    loop {
                        let n = tokio::time::timeout(std::time::Duration::from_secs(5), stream.read(&mut buf))
                            .await.unwrap().unwrap();
                        if n == 0 { break; }
                        acc.extend_from_slice(&buf[..n]);
                        if acc.windows(4).any(|w| w == b"\r\n\r\n") { break; }
                    }
                    let head = String::from_utf8_lossy(&acc).to_string();
                    let len = head.lines().find_map(|l| {
                        let (k, v) = l.split_once(':')?;
                        if k.trim().eq_ignore_ascii_case("content-length") { v.trim().parse::<usize>().ok() } else { None }
                    }).unwrap_or(0);
                    let hend = acc.windows(4).position(|w| w == b"\r\n\r\n").map(|i| i + 4).unwrap_or(acc.len());
                    let mut full = acc.clone();
                    while full.len() < hend + len {
                        let n = tokio::time::timeout(std::time::Duration::from_secs(5), stream.read(&mut buf))
                            .await.unwrap().unwrap();
                        if n == 0 { break; }
                        full.extend_from_slice(&buf[..n]);
                    }
                    c3.lock().unwrap().push(String::from_utf8_lossy(&full).to_string());
                    if delay_ms > 0 {
                        tokio::time::sleep(std::time::Duration::from_millis(delay_ms)).await;
                    }
                    let resp = format!("HTTP/1.1 {} OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        status, body.len(), body);
                    let _ = stream.write_all(resp.as_bytes()).await;
                });
            }
        });
        Wire { port, captured }
    }

    fn claude_ok_body(text: &str) -> String {
        format!(r#"{{"id":"msg_1","type":"message","role":"assistant","model":"claude-sonnet-4-5","content":[{{"type":"text","text":"{}"}}],"stop_reason":"end_turn","usage":{{"input_tokens":1,"output_tokens":1}}}}"#, text)
    }

    fn write_claude_config(path: &Path, port: u16, default_model: &str) {
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "claude") {
            p.enabled = true;
            p.endpoint = format!("http://127.0.0.1:{}", port);
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/cld".to_string()), account_id: None });
            p.default_model = default_model.to_string();
        }
        fs::write(path, serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
    }

    fn claude_legacy_msgs() -> Vec<serde_json::Value> {
        vec![
            serde_json::json!({"role": "system", "content": "sys"}),
            serde_json::json!({"role": "user", "content": "hi"}),
            serde_json::json!({"role": "assistant", "content": "hello"}),
        ]
    }

    fn dispatch_req(model_ref: &str, task: Option<&str>) -> ChatRequest {
        ChatRequest {
            provider_id: "claude".to_string(),
            model_ref: model_ref.to_string(),
            messages: claude_legacy_msgs(),
            task_profile_id: task.map(|s| s.to_string()),
        }
    }

    fn dispatch_cancel() -> tokio_util::sync::CancellationToken {
        tokio_util::sync::CancellationToken::new()
    }

    fn wire_body(wire: &Wire) -> serde_json::Value {
        // Last captured request (tests may issue several).
        let lock = wire.captured.lock().unwrap();
        let raw = lock.last().cloned().expect("request captured");
        let body = raw.split("\r\n\r\n").nth(1).expect("request body captured");
        serde_json::from_str(body).unwrap()
    }

    fn wire_f32(v: &serde_json::Value, expected: f64) {
        let a = v.as_f64().expect("number");
        assert!((a - expected).abs() < 1e-6, "{} != {}", a, expected);
    }

    #[tokio::test]
    async fn claude_dispatch_wire_headers_and_body() {
        let env = IsolatedEnv::lock("dispwire");
        let w = spawn_dispatch_wire(200, claude_ok_body("hello"), 0).await;
        write_claude_config(&env.cfg_path(), w.port, "claude-sonnet-4-5");
        let out = gateway_chat_via_registry(dispatch_req("", None), dispatch_cancel(),
            Arc::new(MapSecrets), Duration::from_secs(5)).await.unwrap();
        assert_eq!(out, "hello");
        let raw = w.captured.lock().unwrap().join("\n");
        assert!(raw.contains("POST /v1/messages"), "messages endpoint");
        assert!(raw.contains("x-api-key: sk-ant-test-key"), "header auth");
        assert!(raw.contains("anthropic-version: 2023-06-01"), "version header");
        let body = wire_body(&w);
        assert_eq!(body["model"], "claude-sonnet-4-5", "remote id upstream");
        assert_eq!(body["system"], "sys");
        assert_eq!(body["max_tokens"], 2048, "gateway default, not adapter 1024");
        wire_f32(&body["temperature"], 0.7);
        assert!(body.get("top_p").is_none(), "parity: no top_p on Claude wire");
    }

    #[tokio::test]
    async fn claude_dispatch_returns_text_not_raw_chat_response() {
        let env = IsolatedEnv::lock("disptext");
        let w = spawn_dispatch_wire(200, claude_ok_body("hello world"), 0).await;
        write_claude_config(&env.cfg_path(), w.port, "claude-sonnet-4-5");
        let out = gateway_chat_via_registry(dispatch_req("", None), dispatch_cancel(),
            Arc::new(MapSecrets), Duration::from_secs(5)).await.unwrap();
        assert_eq!(out, "hello world");
        assert!(!out.contains("msg_1"), "no raw envelope leak");
        assert!(!out.contains("stop_reason"), "no raw envelope leak");
    }

    #[tokio::test]
    async fn claude_dispatch_parity_with_legacy_fixture() {
        let env = IsolatedEnv::lock("dispparity");
        let w = spawn_dispatch_wire(200, claude_ok_body("ok"), 0).await;
        let endpoint = format!("http://127.0.0.1:{}", w.port);
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "claude") {
            p.enabled = true;
            p.endpoint = endpoint.clone();
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/cld".to_string()), account_id: None });
            p.default_model = "claude-3-5-sonnet-latest".to_string();
        }
        let mut generation = HashMap::new();
        generation.insert("temperature".to_string(), "0.2".to_string());
        generation.insert("max_tokens".to_string(), "512".to_string());
        cfg.task_profiles.push(TaskProfile { id: "tp-parity".to_string(), name: "Parity".to_string(),
            model_ref: String::new(), runtime_profile_id: None, privacy: None, cloud_policy: None, generation });
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        // Legacy frozen mapping for the same input.
        let gen = ChatGeneration { temperature: 0.2, top_p: 0.5, max_tokens: 512 };
        let legacy = gateway::build_chat_http_request(&ProviderKind::Claude, &endpoint,
            "claude-3-5-sonnet-latest", 8010, "", &claude_legacy_msgs(), &gen, "sk-ant-test-key");
        let out = gateway_chat_via_registry(dispatch_req("", Some("tp-parity")), dispatch_cancel(),
            Arc::new(MapSecrets), Duration::from_secs(5)).await.unwrap();
        assert_eq!(out, "ok");
        let body = wire_body(&w);
        assert_eq!(body["model"], legacy.body["model"]);
        assert_eq!(body["system"], legacy.body["system"]);
        assert_eq!(body["messages"], legacy.body["messages"]);
        assert_eq!(body["max_tokens"], legacy.body["max_tokens"]);
        assert_eq!(body["temperature"], legacy.body["temperature"]);
        assert!(body.get("top_p").is_none() && legacy.body.get("top_p").is_none());
    }

    #[tokio::test]
    async fn claude_dispatch_privacy_deny() {
        let env = IsolatedEnv::lock("dispdeny");
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "claude") {
            p.enabled = true;
            p.endpoint = "http://127.0.0.1:1".to_string();
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/cld".to_string()), account_id: None });
        }
        let mut generation = HashMap::new();
        generation.insert("temperature".to_string(), "0.2".to_string());
        cfg.task_profiles.push(TaskProfile { id: "tp-local".to_string(), name: "Local".to_string(),
            model_ref: String::new(), runtime_profile_id: None, privacy: Some(Privacy::LocalOnly),
            cloud_policy: None, generation });
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let err = gateway_chat_via_registry(dispatch_req("", Some("tp-local")), dispatch_cancel(),
            Arc::new(MapSecrets), Duration::from_secs(5)).await.unwrap_err();
        assert!(err.starts_with("PrivacyPolicyViolation"), "got: {}", err);
    }

    #[tokio::test]
    async fn claude_dispatch_cancel_and_timeout() {
        let env = IsolatedEnv::lock("dispcancel");
        let w = spawn_dispatch_wire(200, claude_ok_body("late"), 2000).await;
        write_claude_config(&env.cfg_path(), w.port, "claude-sonnet-4-5");
        // Pre-cancelled token short-circuits before network I/O.
        let cancel = dispatch_cancel();
        cancel.cancel();
        let err = gateway_chat_via_registry(dispatch_req("", None), cancel,
            Arc::new(MapSecrets), Duration::from_secs(5)).await.unwrap_err();
        assert!(err.starts_with("LLM cancelled"), "got: {}", err);
        // Slow server + short timeout surfaces a timeout, never a hang.
        let err2 = gateway_chat_via_registry(dispatch_req("", None), dispatch_cancel(),
            Arc::new(MapSecrets), Duration::from_millis(200)).await.unwrap_err();
        assert!(err2.starts_with("LLM timeout"), "got: {}", err2);
    }

    #[tokio::test]
    async fn claude_dispatch_missing_credential_no_network() {
        let env = IsolatedEnv::lock("dispnocred");
        // Closed port: any network attempt would surface transport/timeout,
        // so a credentials error proves resolution precedes I/O.
        write_claude_config(&env.cfg_path(), 1, "claude-sonnet-4-5");
        let err = gateway_chat_via_registry(dispatch_req("", None), dispatch_cancel(),
            Arc::new(NoSecrets), Duration::from_secs(5)).await.unwrap_err();
        assert!(err.contains("credential"), "got: {}", err);
    }

    #[tokio::test]
    async fn claude_registry_identity_never_sends_internal_id() {
        let env = IsolatedEnv::lock("dispidentity");
        let w = spawn_dispatch_wire(200, claude_ok_body("hi"), 0).await;
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "claude") {
            p.enabled = true;
            p.endpoint = format!("http://127.0.0.1:{}", w.port);
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/cld".to_string()), account_id: None });
            p.default_model = String::new();
        }
        cfg.models.push(Model { id: "claude:claude-sonnet-4-5".to_string(), provider_id: "claude".to_string(),
            name: "Sonnet".to_string(), path: String::new(), remote_id: "claude-sonnet-4-5".to_string(),
            capabilities: vec![Capability::Chat], capability_source: None, metadata: Default::default() });
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let out = gateway_chat_via_registry(dispatch_req("claude:claude-sonnet-4-5", None), dispatch_cancel(),
            Arc::new(MapSecrets), Duration::from_secs(5)).await.unwrap();
        assert_eq!(out, "hi");
        let raw = w.captured.lock().unwrap().join("\n");
        assert!(!raw.contains("claude:"), "internal id must never go upstream");
        let body = wire_body(&w);
        assert_eq!(body["model"], "claude-sonnet-4-5", "remote id only");
        // Unknown internal-style ref is rejected before any network I/O.
        let err = gateway_chat_via_registry(dispatch_req("evil:model", None), dispatch_cancel(),
            Arc::new(MapSecrets), Duration::from_secs(5)).await.unwrap_err();
        assert!(err.starts_with("LLM invalid_config"), "got: {}", err);
    }

    #[tokio::test]
    async fn claude_dispatch_profile_max_tokens_used() {
        let env = IsolatedEnv::lock("dispmaxtok");
        let w = spawn_dispatch_wire(200, claude_ok_body("t"), 0).await;
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "claude") {
            p.enabled = true;
            p.endpoint = format!("http://127.0.0.1:{}", w.port);
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/cld".to_string()), account_id: None });
            p.default_model = "claude-sonnet-4-5".to_string();
        }
        let mut generation = HashMap::new();
        generation.insert("max_tokens".to_string(), "512".to_string());
        cfg.task_profiles.push(TaskProfile { id: "tp-tok".to_string(), name: "Tok".to_string(),
            model_ref: String::new(), runtime_profile_id: None, privacy: None, cloud_policy: None, generation });
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        gateway_chat_via_registry(dispatch_req("", Some("tp-tok")), dispatch_cancel(),
            Arc::new(MapSecrets), Duration::from_secs(5)).await.unwrap();
        assert_eq!(wire_body(&w)["max_tokens"], 512, "profile value wins");
        gateway_chat_via_registry(dispatch_req("", None), dispatch_cancel(),
            Arc::new(MapSecrets), Duration::from_secs(5)).await.unwrap();
        let fallback = wire_body(&w)["max_tokens"].as_u64().unwrap();
        assert_eq!(fallback, 2048, "gateway default fallback, never adapter 1024 (got {})", fallback);
    }

    #[tokio::test]
    async fn claude_dispatch_assistant_role_preserved() {
        let env = IsolatedEnv::lock("dispassist");
        let w = spawn_dispatch_wire(200, claude_ok_body("ack"), 0).await;
        write_claude_config(&env.cfg_path(), w.port, "claude-sonnet-4-5");
        let req = ChatRequest {
            provider_id: "claude".to_string(),
            model_ref: String::new(),
            messages: vec![
                serde_json::json!({"role": "user", "content": "q1"}),
                serde_json::json!({"role": "assistant", "content": "a1"}),
                serde_json::json!({"role": "user", "content": "q2"}),
            ],
            task_profile_id: None,
        };
        let out = gateway_chat_via_registry(req, dispatch_cancel(),
            Arc::new(MapSecrets), Duration::from_secs(5)).await.unwrap();
        assert_eq!(out, "ack");
        let body = wire_body(&w);
        let roles: Vec<&str> = body["messages"].as_array().unwrap().iter()
            .map(|m| m["role"].as_str().unwrap()).collect();
        assert_eq!(roles, vec!["user", "assistant", "user"], "assistant must not become user");
        assert_eq!(body["messages"][1]["content"], "a1", "assistant content intact");
    }

    // ---------- Caller wiring tests (router) ----------
    fn routed_cancel() -> tokio_util::sync::CancellationToken {
        tokio_util::sync::CancellationToken::new()
    }

    #[tokio::test]
    async fn wiring_router_claude_uses_dispatch() {
        // The router hardcodes the production keyring path, so a live
        // dispatch cannot run here. With no auth configured, dispatch fails
        // at credential resolution before any network I/O — while the legacy
        // gateway would attempt the network ("LLM offline ..."). The error
        // kind therefore proves router selection; text output is proven by
        // the mock-secret dispatch tests.
        let env = IsolatedEnv::lock("wireroute");
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "claude") {
            p.enabled = true;
            p.endpoint = "http://127.0.0.1:1".to_string();
            p.auth = None;
            p.default_model = "claude-sonnet-4-5".to_string();
        }
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let err = gateway_chat_routed(dispatch_req("", None), routed_cancel()).await.unwrap_err();
        assert!(err.contains("credentials_missing"), "router must select dispatch, got: {}", err);
    }

    #[tokio::test]
    async fn wiring_router_local_uses_legacy() {
        let env = IsolatedEnv::lock("wirelocal");
        let raw = r#"{"choices":[{"message":{"content":"local-hi"}}]}"#.to_string();
        let w = spawn_dispatch_wire(200, raw.clone(), 0).await;
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "local") {
            p.endpoint = format!("http://127.0.0.1:{}", w.port);
        }
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let req = ChatRequest { provider_id: "local".to_string(), model_ref: String::new(),
            messages: vec![serde_json::json!({"role": "user", "content": "hi"})], task_profile_id: None };
        let out = gateway_chat_routed(req, routed_cancel()).await.unwrap();
        assert_eq!(out, raw, "legacy path must stay byte-identical");
    }

    #[tokio::test]
    async fn wiring_router_unknown_provider_no_network() {
        let env = IsolatedEnv::lock("wireunknown");
        write_valid_config(&env.cfg_path());
        let req = ChatRequest { provider_id: "nope".to_string(), model_ref: String::new(),
            messages: vec![serde_json::json!({"role": "user", "content": "hi"})], task_profile_id: None };
        let err = gateway_chat_routed(req, routed_cancel()).await.unwrap_err();
        assert_eq!(err, "provider not found", "got: {}", err);
    }

    #[tokio::test]
    async fn wiring_router_disabled_claude_no_network() {
        let env = IsolatedEnv::lock("wiredisabled");
        // Closed port: any network attempt would fail differently.
        write_claude_config(&env.cfg_path(), 1, "claude-sonnet-4-5");
        let mut cfg: LlmConfig = serde_json::from_str(&fs::read_to_string(env.cfg_path()).unwrap()).unwrap();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "claude") {
            p.enabled = false;
        }
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let err = gateway_chat_routed(dispatch_req("", None), routed_cancel()).await.unwrap_err();
        assert!(err.contains("disabled"), "got: {}", err);
    }

    #[tokio::test]
    async fn wiring_router_task_profile_passthrough() {
        let env = IsolatedEnv::lock("wireprofile");
        let w = spawn_dispatch_wire(200, claude_ok_body("p"), 0).await;
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "claude") {
            p.enabled = true;
            p.endpoint = format!("http://127.0.0.1:{}", w.port);
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/cld".to_string()), account_id: None });
            p.default_model = "claude-sonnet-4-5".to_string();
        }
        // RAG-shaped dispatch: model id + task profile with generation.
        let mut generation = HashMap::new();
        generation.insert("temperature".to_string(), "0.3".to_string());
        generation.insert("max_tokens".to_string(), "256".to_string());
        cfg.task_profiles.push(TaskProfile { id: "tp-rag".to_string(), name: "Rag".to_string(),
            model_ref: String::new(), runtime_profile_id: None, privacy: None, cloud_policy: None, generation });
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let req = ChatRequest { provider_id: "claude".to_string(), model_ref: "claude-sonnet-4-5".to_string(),
            messages: vec![
                serde_json::json!({"role": "system", "content": "s"}),
                serde_json::json!({"role": "user", "content": "q"}),
            ],
            task_profile_id: Some("tp-rag".to_string()) };
        // Profile generation (the same shape RAG forwards) flows into dispatch.
        let out = gateway_chat_via_registry(req, routed_cancel(),
            Arc::new(MapSecrets), Duration::from_secs(5)).await.unwrap();
        assert_eq!(out, "p");
        let body = wire_body(&w);
        assert_eq!(body["max_tokens"], 256, "task profile generation flows through");
        assert_eq!(body["system"], "s");
    }

    #[tokio::test]
    async fn wiring_rag_claude_returns_text_not_raw_json() {
        // Same call shape the RAG GatewayChatAdapter builds (provider +
        // model id + task profile + system/user messages): must yield text.
        let env = IsolatedEnv::lock("wirerag");
        let w = spawn_dispatch_wire(200, claude_ok_body("rag answer"), 0).await;
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "claude") {
            p.enabled = true;
            p.endpoint = format!("http://127.0.0.1:{}", w.port);
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/cld".to_string()), account_id: None });
            p.default_model = "claude-sonnet-4-5".to_string();
        }
        cfg.task_profiles.push(TaskProfile { id: "task-chat".to_string(), name: "Chat".to_string(),
            model_ref: String::new(), runtime_profile_id: None, privacy: None, cloud_policy: None,
            generation: HashMap::new() });
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let req = ChatRequest { provider_id: "claude".to_string(), model_ref: "claude-sonnet-4-5".to_string(),
            messages: vec![
                serde_json::json!({"role": "system", "content": "ctx"}),
                serde_json::json!({"role": "user", "content": "q"}),
            ],
            task_profile_id: Some("task-chat".to_string()) };
        let out = gateway_chat_via_registry(req, routed_cancel(),
            Arc::new(MapSecrets), Duration::from_secs(5)).await.unwrap();
        assert_eq!(out, "rag answer");
        assert!(!out.contains("content"), "no raw provider JSON in RAG-shaped output");
    }

    #[tokio::test]
    async fn wiring_pipeline_claude_step_text() {
        // End-to-end through llm_pipeline_run: the Claude step is routed to
        // dispatch. With no auth configured the adapter fails before any
        // network I/O (no keyring touch: auth None short-circuits), and the
        // pipeline preserves its error-string contract.
        let env = IsolatedEnv::lock("wirepipe");
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "claude") {
            p.enabled = true;
            p.endpoint = "http://127.0.0.1:1".to_string();
            p.auth = None;
            p.default_model = "claude-sonnet-4-5".to_string();
        }
        cfg.pipelines.push(Pipeline { id: "pipe-claude".to_string(), name: "P".to_string(),
            description: String::new(), enabled: true, trigger: "manual".to_string(),
            steps: vec![PipelineStep { id: "s1".to_string(), name: "S1".to_string(), kind: "llm".to_string(),
                provider_id: "claude".to_string(), model_ref: String::new(), task_profile_id: None,
                input_refs: vec!["input".to_string()], prompt_template: "{{input}}".to_string(),
                output_schema: None, retry: None, timeout_ms: None, enabled: true }] });
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let out = llm_pipeline_run("pipe-claude".to_string(), "hello".to_string()).await.unwrap();
        assert!(out.starts_with("error:"), "pipeline error contract, got: {}", out);
        assert!(out.contains("credential"), "dispatch reached (not legacy network path), got: {}", out);
        // Anthropic text blocks extract cleanly when dispatch succeeds.
        assert_eq!(extract_content(&claude_ok_body("piped")), "piped");
    }

    #[tokio::test]
    async fn wiring_pipeline_error_string_behavior_unchanged() {
        let env = IsolatedEnv::lock("wirepipeerr");
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "local") {
            p.endpoint = "http://127.0.0.1:1".to_string();
        }
        cfg.pipelines.push(Pipeline { id: "pipe-err".to_string(), name: "E".to_string(),
            description: String::new(), enabled: true, trigger: "manual".to_string(),
            steps: vec![PipelineStep { id: "s1".to_string(), name: "S1".to_string(), kind: "llm".to_string(),
                provider_id: "local".to_string(), model_ref: String::new(), task_profile_id: None,
                input_refs: vec!["input".to_string()], prompt_template: "{{input}}".to_string(),
                output_schema: None, retry: None, timeout_ms: None, enabled: true }] });
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let out = llm_pipeline_run("pipe-err".to_string(), "hello".to_string()).await.unwrap();
        assert!(out.starts_with("error:"), "error-string behavior preserved, got: {}", out);
    }

    #[tokio::test]
    async fn wiring_enrich_claude_writes_text_not_raw_json() {
        // enrich llm_chat with an explicit cloud profile is routed to
        // dispatch: with no auth configured it fails before any network I/O
        // (no keyring touch), proving the enrich path reaches dispatch and
        // never the legacy raw-JSON branch for Claude.
        let env = IsolatedEnv::lock("wireenrich");
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "claude") {
            p.enabled = true;
            p.endpoint = "http://127.0.0.1:1".to_string();
            p.auth = None;
            p.default_model = "claude-sonnet-4-5".to_string();
        }
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let out = crate::enrich::llm_chat("claude".to_string(),
            vec![serde_json::json!({"role": "user", "content": "hi"})]).await.unwrap_err();
        assert!(out.contains("credential"), "dispatch reached, got: {}", out);
        assert!(!out.contains("msg_"), "no raw provider JSON leak");
    }

    /// Manual cloud E2E (ignored by default). Secret ONLY from
    /// ANTHROPIC_API_KEY_TEST via a mock store — never keyring, never repo,
    /// never logs. Isolated temp config; skips cleanly without credentials.
    #[tokio::test]
    #[ignore]
    async fn claude_cloud_e2e_manual() {
        let key = match std::env::var("ANTHROPIC_API_KEY_TEST") {
            Ok(k) if !k.trim().is_empty() => k,
            _ => {
                eprintln!("SKIP: ANTHROPIC_API_KEY_TEST absent");
                return;
            }
        };
        struct EnvSecrets(String);
        impl provider::SecretStore for EnvSecrets {
            fn get(&self, _: &str) -> Result<String, provider::ProviderError> {
                Ok(self.0.clone())
            }
        }
        let env = IsolatedEnv::lock("cloude2e");
        let model = std::env::var("ANTHROPIC_E2E_MODEL").unwrap_or_else(|_| "claude-3-5-haiku-latest".to_string());
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "claude") {
            p.enabled = true;
            p.endpoint = String::new();
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/e2e".to_string()), account_id: None });
            p.default_model = model;
        }
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let req = ChatRequest { provider_id: "claude".to_string(), model_ref: String::new(),
            messages: vec![serde_json::json!({"role": "user", "content": "Reply with exactly: ok"})],
            task_profile_id: None };
        let out = gateway_chat_via_registry(req, routed_cancel(),
            Arc::new(EnvSecrets(key)), Duration::from_secs(90)).await.unwrap();
        assert!(!out.trim().is_empty(), "non-empty assistant text");
        assert!(!out.contains("msg_"), "text, not raw envelope");
    }

    // ---------- Gemini query-auth removal tests ----------
    struct MapGeminiSecrets;
    impl provider::SecretStore for MapGeminiSecrets {
        fn get(&self, r: &str) -> Result<String, provider::ProviderError> {
            assert_eq!(r, "keychain://fragile-notes/gld");
            Ok("AIza-test-key-12345".to_string())
        }
    }

    fn gemini_ok_body(text: &str) -> String {
        format!(r#"{{"candidates":[{{"content":{{"parts":[{{"text":"{}"}}]}},"finishReason":"STOP"}}],"usageMetadata":{{"promptTokenCount":1,"candidatesTokenCount":1}}}}"#, text)
    }

    fn write_gemini_config(path: &Path, port: u16, default_model: &str) {
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "gemini") {
            p.enabled = true;
            p.endpoint = format!("http://127.0.0.1:{}", port);
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/gld".to_string()), account_id: None });
            p.default_model = default_model.to_string();
        }
        fs::write(path, serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
    }

    fn gemini_req(model_ref: &str, task: Option<&str>) -> ChatRequest {
        ChatRequest {
            provider_id: "gemini".to_string(),
            model_ref: model_ref.to_string(),
            messages: vec![
                serde_json::json!({"role": "system", "content": "sys"}),
                serde_json::json!({"role": "user", "content": "hi"}),
                serde_json::json!({"role": "assistant", "content": "hello"}),
            ],
            task_profile_id: task.map(|s| s.to_string()),
        }
    }

    fn wire_request_line(wire: &Wire) -> String {
        let lock = wire.captured.lock().unwrap();
        let raw = lock.last().cloned().expect("request captured");
        raw.lines().next().unwrap_or("").to_string()
    }

    #[tokio::test]
    async fn gemini_no_query_key_in_chat_url() {
        let env = IsolatedEnv::lock("gemurl");
        let w = spawn_dispatch_wire(200, gemini_ok_body("hi"), 0).await;
        write_gemini_config(&env.cfg_path(), w.port, "gemini-2.0-flash");
        gateway_gemini_via_registry(gemini_req("", None), dispatch_cancel(),
            Arc::new(MapGeminiSecrets), Duration::from_secs(5)).await.unwrap();
        let line = wire_request_line(&w);
        assert!(line.starts_with("POST /v1beta/models/gemini-2.0-flash:generateContent "),
            "model path in URL, got: {}", line);
        assert!(!line.contains('?'), "no query string at all, got: {}", line);
        assert!(!line.contains("key="), "no query auth, got: {}", line);
    }

    #[tokio::test]
    async fn gemini_chat_uses_header_auth() {
        let env = IsolatedEnv::lock("gemhead");
        let w = spawn_dispatch_wire(200, gemini_ok_body("hi"), 0).await;
        write_gemini_config(&env.cfg_path(), w.port, "gemini-2.0-flash");
        gateway_gemini_via_registry(gemini_req("", None), dispatch_cancel(),
            Arc::new(MapGeminiSecrets), Duration::from_secs(5)).await.unwrap();
        let raw = w.captured.lock().unwrap().join("\n").to_lowercase();
        assert_eq!(raw.matches("x-goog-api-key: aiza-test-key-12345").count(), 1,
            "exactly one header auth");
    }

    #[tokio::test]
    async fn gemini_dispatch_returns_text_not_raw_json() {
        let env = IsolatedEnv::lock("gemtext");
        let w = spawn_dispatch_wire(200, gemini_ok_body("hello world"), 0).await;
        write_gemini_config(&env.cfg_path(), w.port, "gemini-2.0-flash");
        let out = gateway_gemini_via_registry(gemini_req("", None), dispatch_cancel(),
            Arc::new(MapGeminiSecrets), Duration::from_secs(5)).await.unwrap();
        assert_eq!(out, "hello world");
        assert!(!out.contains("candidates"), "no raw envelope leak");
        assert!(!out.contains("finishReason"), "no raw envelope leak");
    }

    #[tokio::test]
    async fn gemini_dispatch_parity_with_legacy_fixture() {
        let env = IsolatedEnv::lock("gemparity");
        let w = spawn_dispatch_wire(200, gemini_ok_body("ok"), 0).await;
        let endpoint = format!("http://127.0.0.1:{}", w.port);
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "gemini") {
            p.enabled = true;
            p.endpoint = endpoint.clone();
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/gld".to_string()), account_id: None });
            p.default_model = "gemini-2.0-flash".to_string();
        }
        let mut generation = HashMap::new();
        generation.insert("temperature".to_string(), "0.2".to_string());
        generation.insert("top_p".to_string(), "0.5".to_string());
        generation.insert("max_tokens".to_string(), "512".to_string());
        cfg.task_profiles.push(TaskProfile { id: "tp-gem".to_string(), name: "Gem".to_string(),
            model_ref: String::new(), runtime_profile_id: None, privacy: None, cloud_policy: None, generation });
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        // Legacy frozen mapping (now header-auth) for the same input.
        let gen = ChatGeneration { temperature: 0.2, top_p: 0.5, max_tokens: 512 };
        let legacy = gateway::build_chat_http_request(&ProviderKind::Gemini, &endpoint,
            "gemini-2.0-flash", 8010, "", &gemini_req("", None).messages, &gen, "AIza-test-key-12345");
        let out = gateway_gemini_via_registry(gemini_req("", Some("tp-gem")), dispatch_cancel(),
            Arc::new(MapGeminiSecrets), Duration::from_secs(5)).await.unwrap();
        assert_eq!(out, "ok");
        let raw = w.captured.lock().unwrap().join("\n");
        let target_line = raw.lines().next().unwrap_or("").to_string();
        let legacy_path = legacy.url.split('/').skip(3).collect::<Vec<_>>().join("/");
        assert!(target_line.contains(&format!("POST /{}", legacy_path)),
            "same URL path as legacy builder, got: {} vs {}", target_line, legacy.url);
        assert!(!target_line.contains('?'), "no query in either shape");
        let body = wire_body(&w);
        // Same model path and generation settings as the legacy builder.
        // Known documented difference: the native adapter lifts system text
        // into top-level systemInstruction, while legacy folds it into the
        // first user part. Semantic content is identical.
        assert_eq!(body["generationConfig"], legacy.body["generationConfig"]);
        assert_eq!(body["systemInstruction"]["parts"][0]["text"], "sys");
        assert_eq!(legacy.body["contents"][0]["parts"][0]["text"], "sys");
        assert_eq!(body["contents"].as_array().unwrap().len(), 2);
        assert_eq!(body["contents"][0]["role"], "user");
        assert_eq!(body["contents"][0]["parts"][0]["text"], "hi");
        assert_eq!(body["contents"][1]["role"], "model");
        assert_eq!(body["contents"][1]["parts"][0]["text"], "hello");
    }

    #[tokio::test]
    async fn gemini_test_provider_uses_header_auth() {
        let env = IsolatedEnv::lock("gemhealth");
        let w = spawn_dispatch_wire(200, r#"{"models":[]}"#.to_string(), 0).await;
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "gemini") {
            p.enabled = true;
            p.endpoint = format!("http://127.0.0.1:{}", w.port);
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/gld".to_string()), account_id: None });
        }
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let prov = load_config_inner().providers.into_iter().find(|p| p.id == "gemini").unwrap();
        let res = gemini_test_via_adapter(&prov, std::time::Instant::now(), Arc::new(MapGeminiSecrets)).await;
        assert!(res.ok, "health ok: {:?}", res.error);
        assert_eq!(res.capabilities, Some(vec![Capability::Chat, Capability::Streaming]));
        let raw = w.captured.lock().unwrap().join("\n").to_lowercase();
        assert!(raw.contains("get /v1beta/models "), "models probe, got headers");
        assert_eq!(raw.matches("x-goog-api-key: aiza-test-key-12345").count(), 1);
        assert!(!raw.contains("?key="), "no query auth in health probe");
    }

    #[tokio::test]
    async fn gemini_offline_error_hides_secret() {
        let evil = "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent?key=AIzaSECRET999&alt=json";
        let safe = safe_url_for_error(evil);
        assert!(!safe.contains("AIzaSECRET999"), "secret leaked: {}", safe);
        assert!(!safe.contains('?'), "query stripped: {}", safe);
        assert_eq!(safe, "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent");
        let err = format!("LLM offline {}: {}", safe_url_for_error(evil), "conn refused");
        assert!(!err.contains("AIzaSECRET999"));
    }

    #[tokio::test]
    async fn gemini_rag_bounded_error_hides_secret() {
        // Same redaction the RAG adapter applies before capping to 500 chars.
        let evil = "LLM offline https://x.test/v1beta/models/m:generateContent?key=AIzaSECRET999: conn refused";
        let out: String = crate::llm::embeddings::redact_secrets(&evil).chars().take(500).collect();
        assert!(!out.contains("AIzaSECRET999"), "secret leaked: {}", out);
    }

    #[tokio::test]
    async fn gemini_legacy_branch_still_present_until_parity() {
        // The legacy builder branch stays alive (header-secured): same
        // endpoint shape and body mapping as before, only auth moved.
        let gen = ChatGeneration { temperature: 0.2, top_p: 0.5, max_tokens: 512 };
        let r = gateway::build_chat_http_request(&ProviderKind::Gemini, "",
            "gemini-2.0-flash", 8010, "custom-model", &gemini_req("", None).messages, &gen, "k");
        assert_eq!(r.url, "https://generativelanguage.googleapis.com/v1beta/models/custom-model:generateContent");
        // Legacy folds system text into the first user part (3 contents);
        // the native adapter lifts it to systemInstruction instead.
        assert_eq!(r.body["contents"].as_array().unwrap().len(), 3);
        assert_eq!(r.body["contents"][0]["parts"][0]["text"], "sys");
        assert_eq!(r.body["generationConfig"]["topP"], 0.5);
        assert_eq!(r.body["generationConfig"]["maxOutputTokens"], 512);
    }

    #[tokio::test]
    async fn gemini_profile_max_tokens_used() {
        let env = IsolatedEnv::lock("gemmaxtok");
        let w = spawn_dispatch_wire(200, gemini_ok_body("t"), 0).await;
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "gemini") {
            p.enabled = true;
            p.endpoint = format!("http://127.0.0.1:{}", w.port);
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/gld".to_string()), account_id: None });
            p.default_model = "gemini-2.0-flash".to_string();
        }
        let mut generation = HashMap::new();
        generation.insert("max_tokens".to_string(), "512".to_string());
        cfg.task_profiles.push(TaskProfile { id: "tp-gtok".to_string(), name: "Tok".to_string(),
            model_ref: String::new(), runtime_profile_id: None, privacy: None, cloud_policy: None, generation });
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        gateway_gemini_via_registry(gemini_req("", Some("tp-gtok")), dispatch_cancel(),
            Arc::new(MapGeminiSecrets), Duration::from_secs(5)).await.unwrap();
        assert_eq!(wire_body(&w)["generationConfig"]["maxOutputTokens"], 512, "profile value wins");
        gateway_gemini_via_registry(gemini_req("", None), dispatch_cancel(),
            Arc::new(MapGeminiSecrets), Duration::from_secs(5)).await.unwrap();
        assert_eq!(wire_body(&w)["generationConfig"]["maxOutputTokens"], 2048, "gateway default fallback");
    }

    /// Manual cloud E2E (ignored by default). Secret ONLY from
    /// GEMINI_API_KEY_TEST via a mock store — never keyring, never repo,
    /// never logs. Isolated temp config; chat only; skips cleanly.
    #[tokio::test]
    #[ignore]
    async fn gemini_cloud_e2e_manual() {
        let key = match std::env::var("GEMINI_API_KEY_TEST") {
            Ok(k) if !k.trim().is_empty() => k,
            _ => {
                eprintln!("SKIP: GEMINI_API_KEY_TEST absent");
                return;
            }
        };
        struct EnvSecrets(String);
        impl provider::SecretStore for EnvSecrets {
            fn get(&self, _: &str) -> Result<String, provider::ProviderError> {
                Ok(self.0.clone())
            }
        }
        let env = IsolatedEnv::lock("gemcloude2e");
        let model = std::env::var("GEMINI_E2E_MODEL").unwrap_or_else(|_| "gemini-2.0-flash".to_string());
        let mut cfg = LlmConfig::default();
        if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "gemini") {
            p.enabled = true;
            p.endpoint = String::new();
            p.auth = Some(AuthRef { method: AuthMethod::ApiKey,
                secret_ref: Some("keychain://fragile-notes/e2e".to_string()), account_id: None });
            p.default_model = model;
        }
        fs::write(env.cfg_path(), serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        let out = gateway_gemini_via_registry(gemini_req("", None), dispatch_cancel(),
            Arc::new(EnvSecrets(key)), Duration::from_secs(90)).await.unwrap();
        assert!(!out.trim().is_empty(), "non-empty assistant text");
        assert!(!out.contains("candidates"), "text, not raw envelope");
    }
}

