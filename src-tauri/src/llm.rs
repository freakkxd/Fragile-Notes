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
    // 1) vault/.fragile/llm.json 2) ~/.config/Fragile-Notes/llm.json
    let vault = vault_root().join(".fragile").join("llm.json");
    if vault.parent().map(|p| p.exists()).unwrap_or(false) || std::env::var("FRAGILE_VAULT").is_ok() {
        return vault;
    }
    if let Some(home) = dirs_next() {
        let p = home.join(".config").join("Fragile-Notes").join("llm.json");
        if p.parent().map(|p| p.exists()).unwrap_or(false) {
            return p;
        }
        // default to vault path (ensure dir)
        return vault;
    }
    vault
}

fn vault_root() -> PathBuf {
    if let Ok(custom) = std::env::var("FRAGILE_VAULT") {
        return PathBuf::from(custom);
    }
    let home = dirs_next();
    home.join("Documents").join("FragileNotesVault")
}
fn dirs_next() -> PathBuf {
    if let Some(h) = std::env::var_os("HOME") { return PathBuf::from(h); }
    if let Some(h) = std::env::var_os("USERPROFILE") { return PathBuf::from(h); }
    PathBuf::from(".")
}

static RE_GGUF_QUANT: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)(Q[0-9]_[A-Za-z0-9_]+|f16|f32|bf16|q8_0)").unwrap());

// ---------- types ----------
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ProviderKind {
    LocalLlamaCpp,
    Ollama,
    OpenAI,
    Gemini,
    Claude,
    CustomOpenAI,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum AuthMethod {
    ApiKey,
    OAuth,
    None,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Provider {
    pub id: String, // e.g. "local-qwen3", "openai"
    pub name: String,
    pub kind: ProviderKind,
    pub enabled: bool,
    pub auth_method: AuthMethod,
    #[serde(default)]
    pub api_key: String, // stored encrypted (base64), decrypted on use
    #[serde(default)]
    pub api_url: String, // for custom/openai-compatible
    #[serde(default)]
    pub model: String, // gpt-4o, gemini-2.0-flash, claude-3.5-sonnet, gguf path
    #[serde(default)]
    pub extra: HashMap<String, String>, // temp, top_p, etc per provider override
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalSettings {
    pub binary_path: String, // /usr/bin/llama-server or ./llama.cpp/build/bin/llama-server
    pub models_dir: String,  // ~/Models
    pub active_model: String,
    #[serde(default)]
    pub n_ctx: u32, // 4096
    #[serde(default)]
    pub n_threads: u32, // 0 = auto
    #[serde(default)]
    pub n_gpu_layers: u32, // 0 = cpu, 99 = all
    #[serde(default)]
    pub temp: f32, // 0.7
    #[serde(default)]
    pub top_p: f32, // 0.9
    #[serde(default)]
    pub top_k: u32, // 40
    #[serde(default)]
    pub repeat_penalty: f32, // 1.1
    #[serde(default)]
    pub port: u16, // 8010
    #[serde(default)]
    pub auto_start: bool,
    #[serde(default)]
    pub use_mmap: bool,
    #[serde(default)]
    pub extra_args: String, // --mlock --flash-attn
}

impl Default for LocalSettings {
    fn default() -> Self {
        let home = dirs_next();
        Self {
            binary_path: "llama-server".to_string(),
            models_dir: home.join("Models").to_string_lossy().to_string(),
            active_model: String::new(),
            n_ctx: 8192,
            n_threads: 0,
            n_gpu_layers: 0,
            temp: 0.7,
            top_p: 0.9,
            top_k: 40,
            repeat_penalty: 1.1,
            port: 8010,
            auto_start: false,
            use_mmap: true,
            extra_args: String::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelInfo {
    pub name: String,
    pub path: String,
    pub size_mb: u64,
    pub quant: String,
    pub task: String, // chat, embedding, coder, vision, enrich, general
    pub installed_at: String,
    pub source_url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineStep {
    pub id: String,
    pub name: String,
    pub provider_id: String,
    pub model: String, // override or empty = provider default
    pub prompt_template: String,
    pub input_from: String, // note_content | previous | vault_search
    pub output_to: String, // note_tags | note_body | new_note | chat
    #[serde(default)]
    pub enabled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Pipeline {
    pub id: String,
    pub name: String,
    pub description: String,
    #[serde(default)]
    pub enabled: bool,
    pub trigger: String, // manual | on_save | scheduled
    pub steps: Vec<PipelineStep>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmConfig {
    pub version: u32,
    pub providers: Vec<Provider>,
    pub local: LocalSettings,
    pub pipelines: Vec<Pipeline>,
    #[serde(default)]
    pub active_pipeline: String,
}

impl Default for LlmConfig {
    fn default() -> Self {
        Self {
            version: 1,
            providers: vec![
                Provider { id: "local".to_string(), name: "Local llama.cpp".to_string(), kind: ProviderKind::LocalLlamaCpp, enabled: true, auth_method: AuthMethod::None, api_key: String::new(), api_url: "http://127.0.0.1:8010".to_string(), model: String::new(), extra: HashMap::new() },
                Provider { id: "openai".to_string(), name: "OpenAI (ChatGPT)".to_string(), kind: ProviderKind::OpenAI, enabled: false, auth_method: AuthMethod::ApiKey, api_key: String::new(), api_url: "https://api.openai.com/v1".to_string(), model: "gpt-4o".to_string(), extra: HashMap::new() },
                Provider { id: "gemini".to_string(), name: "Google Gemini".to_string(), kind: ProviderKind::Gemini, enabled: false, auth_method: AuthMethod::ApiKey, api_key: String::new(), api_url: "https://generativelanguage.googleapis.com".to_string(), model: "gemini-2.0-flash".to_string(), extra: HashMap::new() },
                Provider { id: "claude".to_string(), name: "Anthropic Claude".to_string(), kind: ProviderKind::Claude, enabled: false, auth_method: AuthMethod::ApiKey, api_key: String::new(), api_url: "https://api.anthropic.com".to_string(), model: "claude-3-5-sonnet-latest".to_string(), extra: HashMap::new() },
                Provider { id: "ollama".to_string(), name: "Ollama".to_string(), kind: ProviderKind::Ollama, enabled: false, auth_method: AuthMethod::None, api_key: String::new(), api_url: "http://127.0.0.1:11434".to_string(), model: "llama3.1".to_string(), extra: HashMap::new() },
                Provider { id: "custom".to_string(), name: "Custom OpenAI-compat".to_string(), kind: ProviderKind::CustomOpenAI, enabled: false, auth_method: AuthMethod::ApiKey, api_key: String::new(), api_url: String::new(), model: String::new(), extra: HashMap::new() },
            ],
            local: LocalSettings::default(),
            pipelines: vec![
                Pipeline {
                    id: "enrich".to_string(),
                    name: "Обогащение заметок".to_string(),
                    description: "Извлечь #теги, [[links]] и описание — на выбранной нейросети".to_string(),
                    enabled: true,
                    trigger: "manual".to_string(),
                    steps: vec![PipelineStep { id: "s1".to_string(), name: "Теги + Links".to_string(), provider_id: "local".to_string(), model: String::new(), prompt_template: "Обогати заметку: выдели 3 тега #тег, краткое описание 1 предложение, и 2 [[wikilinks]].\n\n{{content}}".to_string(), input_from: "note_content".to_string(), output_to: "note_tags".to_string(), enabled: true }],
                },
                Pipeline {
                    id: "chat-rag".to_string(),
                    name: "Чат с vault RAG".to_string(),
                    description: "Поиск FTS топ-3 → inject в system prompt → ответ".to_string(),
                    enabled: true,
                    trigger: "manual".to_string(),
                    steps: vec![PipelineStep { id: "s1".to_string(), name: "RAG + Chat".to_string(), provider_id: "local".to_string(), model: String::new(), prompt_template: "Контекст vault:\n{{rag}}\n\nВопрос: {{content}}".to_string(), input_from: "vault_search".to_string(), output_to: "chat".to_string(), enabled: true }],
                },
            ],
            active_pipeline: "enrich".to_string(),
        }
    }
}

// ---------- persist ----------
fn ensure_parent(p: &Path) -> Result<(), String> {
    if let Some(parent) = p.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    Ok(())
}

// simple obfuscate: base64 + not real crypto, but structure for aes-gcm later
fn obfuscate(s: &str) -> String {
    use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64};
    BASE64.encode(s.as_bytes())
}
fn deobfuscate(s: &str) -> String {
    use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64};
    BASE64.decode(s).ok().and_then(|b| String::from_utf8(b).ok()).unwrap_or_default()
}

fn load_config_inner() -> LlmConfig {
    let p = config_path();
    if !p.exists() {
        return LlmConfig::default();
    }
    if let Ok(txt) = fs::read_to_string(&p) {
        if let Ok(mut cfg) = serde_json::from_str::<LlmConfig>(&txt) {
            // deobfuscate keys
            for pr in &mut cfg.providers {
                if !pr.api_key.is_empty() && pr.auth_method == AuthMethod::ApiKey {
                    // if looks like base64, decode
                    let dec = deobfuscate(&pr.api_key);
                    if !dec.is_empty() && dec != pr.api_key {
                        // keep decoded in memory only on read? For now store decoded for frontend, re-encode on save
                        pr.api_key = dec;
                    }
                }
            }
            return cfg;
        }
    }
    LlmConfig::default()
}

fn save_config_inner(cfg: &LlmConfig) -> Result<(), String> {
    let mut to_save = cfg.clone();
    for pr in &mut to_save.providers {
        if !pr.api_key.is_empty() && pr.auth_method == AuthMethod::ApiKey {
            // avoid double encode: if already base64 that decodes to ascii, encode once
            // heuristic: if api_key looks like sk- or AIza etc, encode
            if !pr.api_key.starts_with("sk-") && pr.api_key.len() < 40 {
                // already encoded? try decode and see if re-encode matches
            }
            // always encode for storage
            if pr.api_key.len() > 0 && !pr.api_key.chars().all(|c| c.is_ascii_alphanumeric() || "+/=".contains(c)) {
                pr.api_key = obfuscate(&pr.api_key);
            } else if pr.api_key.starts_with("sk-") || pr.api_key.starts_with("AIza") || pr.api_key.contains("-") {
                pr.api_key = obfuscate(&pr.api_key);
            }
        }
    }
    let p = config_path();
    ensure_parent(&p)?;
    let txt = serde_json::to_string_pretty(&to_save).map_err(|e| e.to_string())?;
    fs::write(&p, txt).map_err(|e| e.to_string())?;
    Ok(())
}

// ---------- model scan ----------
fn parse_quant(name: &str) -> String {
    RE_GGUF_QUANT.find(name).map(|m| m.as_str().to_uppercase()).unwrap_or_else(|| "unknown".to_string())
}

fn guess_task(name: &str) -> String {
    let lower = name.to_lowercase();
    if lower.contains("embed") { return "embedding".to_string(); }
    if lower.contains("coder") || lower.contains("code") { return "coder".to_string(); }
    if lower.contains("vision") || lower.contains("vl") { return "vision".to_string(); }
    if lower.contains("instruct") || lower.contains("chat") { return "chat".to_string(); }
    "general".to_string()
}

// ---------- commands ----------
#[tauri::command]
pub fn llm_get_config() -> Result<String, String> {
    let cfg = load_config_inner();
    serde_json::to_string(&cfg).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn llm_save_config(config_json: String) -> Result<String, String> {
    let cfg: LlmConfig = serde_json::from_str(&config_json).map_err(|e| format!("parse config: {}", e))?;
    save_config_inner(&cfg)?;
    Ok("saved".to_string())
}

#[tauri::command]
pub fn llm_scan_models(models_dir: Option<String>) -> Result<String, String> {
    let cfg = load_config_inner();
    let dir = models_dir.unwrap_or(cfg.local.models_dir.clone());
    let path = PathBuf::from(&dir);
    if !path.exists() {
        return Ok(serde_json::to_string(&Vec::<ModelInfo>::new()).unwrap());
    }
    let mut out = Vec::new();
    for entry in WalkDir::new(&path).max_depth(4).into_iter().filter_map(|e| e.ok()) {
        let p = entry.path();
        if p.is_file() && p.extension().and_then(|s| s.to_str()).map(|e| e.eq_ignore_ascii_case("gguf")).unwrap_or(false) {
            let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("model.gguf").to_string();
            let size_mb = fs::metadata(p).map(|m| m.len() / 1024 / 1024).unwrap_or(0);
            let quant = parse_quant(&name);
            let task = guess_task(&name);
            let installed_at = fs::metadata(p).and_then(|m| m.modified()).ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs().to_string()).unwrap_or_default();
            out.push(ModelInfo {
                name: name.clone(),
                path: p.to_string_lossy().to_string(),
                size_mb,
                quant,
                task,
                installed_at,
                source_url: String::new(),
            });
        }
    }
    out.sort_by(|a, b| b.size_mb.cmp(&a.size_mb));
    serde_json::to_string(&out).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn llm_set_active_model(model_path: String) -> Result<String, String> {
    let mut cfg = load_config_inner();
    cfg.local.active_model = model_path.clone();
    // also sync provider "local" model field
    if let Some(p) = cfg.providers.iter_mut().find(|x| x.id == "local") {
        p.model = model_path.clone();
    }
    save_config_inner(&cfg)?;
    Ok(model_path)
}

#[tauri::command]
pub fn llm_download_model(url: String, target_task: String, filename: Option<String>) -> Result<String, String> {
    // For MVP: create placeholder, real download via hf hub would be streaming
    // target_task determines subfolder: Models/{task}/
    let cfg = load_config_inner();
    let base = PathBuf::from(&cfg.local.models_dir);
    let sub = if target_task.is_empty() { base } else { base.join(&target_task) };
    fs::create_dir_all(&sub).map_err(|e| e.to_string())?;
    let fname = filename.unwrap_or_else(|| {
        url.split('/').last().unwrap_or("model.gguf").split('?').next().unwrap_or("model.gguf").to_string()
    });
    let dest = sub.join(&fname);
    // Not actually downloading large file in Tauri command synchronously — return path and instruction
    // Instead, we will use reqwest blocking in background? For now just validate URL and return dest
    if !url.starts_with("http") {
        return Err("URL must start with http".to_string());
    }
    // For demo, write placeholder if not exists
    if !dest.exists() {
        fs::write(&dest, format!("# placeholder for {}\n# download via: curl -L {} -o {}", fname, url, dest.display())).map_err(|e| e.to_string())?;
    }
    Ok(dest.to_string_lossy().to_string())
}

#[tauri::command]
pub fn llm_test_provider(provider_id: String) -> Result<String, String> {
    let cfg = load_config_inner();
    let prov = cfg.providers.iter().find(|p| p.id == provider_id).ok_or("provider not found")?;
    match prov.kind {
        ProviderKind::LocalLlamaCpp | ProviderKind::Ollama => {
            let url = if prov.api_url.is_empty() { format!("http://127.0.0.1:{}", cfg.local.port) } else { prov.api_url.clone() };
            let health = format!("{}/health", url.trim_end_matches('/'));
            let client = reqwest::blocking::Client::builder().timeout(Duration::from_secs(5)).build().map_err(|e| e.to_string())?;
            match client.get(&health).send() {
                Ok(r) if r.status().is_success() => Ok(format!("{{\"online\":true,\"status\":{}}}", r.status().as_u16())),
                Ok(r) => Ok(format!("{{\"online\":false,\"http\":{}}}", r.status().as_u16())),
                Err(e) => Ok(format!("{{\"online\":false,\"error\":\"{}\"}}", e.to_string().replace('"', "'"))),
            }
        }
        _ => {
            // for cloud, do a lightweight probe if api_key present
            if prov.api_key.is_empty() {
                return Ok("{\"online\":false,\"error\":\"no api_key\"}".to_string());
            }
            // Try a cheap model list probe without spending tokens
            // OpenAI: GET /v1/models, Gemini: GET /v1beta/models, Claude: POST /v1/messages with max_tokens 1
            // For MVP just validate key format
            if prov.api_key.len() < 10 {
                return Ok("{\"online\":false,\"error\":\"api_key too short\"}".to_string());
            }
            Ok("{\"online\":true,\"probe\":\"key format ok (real check on chat)\"}".to_string())
        }
    }
}

#[tauri::command]
pub fn llm_chat_universal(provider_id: String, model: String, messages: Vec<serde_json::Value>) -> Result<String, String> {
    let cfg = load_config_inner();
    let prov = cfg.providers.iter().find(|p| p.id == provider_id).cloned().unwrap_or_else(|| Provider {
        id: provider_id.clone(), name: provider_id.clone(), kind: ProviderKind::LocalLlamaCpp, enabled: true, auth_method: AuthMethod::None, api_key: String::new(), api_url: format!("http://127.0.0.1:{}", cfg.local.port), model: model.clone(), extra: HashMap::new(),
    });

    // Local / Ollama / CustomOpenAI -> OpenAI-compatible /v1/chat/completions
    let client = reqwest::blocking::Client::builder().timeout(Duration::from_secs(90)).build().map_err(|e| e.to_string())?;
    let (url, headers) = match prov.kind {
        ProviderKind::LocalLlamaCpp | ProviderKind::Ollama | ProviderKind::CustomOpenAI | ProviderKind::OpenAI => {
            let base = if prov.api_url.is_empty() { format!("http://127.0.0.1:{}", cfg.local.port) } else { prov.api_url.clone() };
            let u = format!("{}/v1/chat/completions", base.trim_end_matches('/'));
            let mut h = HashMap::new();
            if !prov.api_key.is_empty() {
                h.insert("Authorization".to_string(), format!("Bearer {}", prov.api_key));
            }
            (u, h)
        }
        ProviderKind::Gemini => {
            // Gemini: POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=API_KEY
            let base = if prov.api_url.is_empty() { "https://generativelanguage.googleapis.com".to_string() } else { prov.api_url.clone() };
            let mdl = if model.is_empty() { prov.model.clone() } else { model.clone() };
            let u = format!("{}/v1beta/models/{}:generateContent?key={}", base.trim_end_matches('/'), mdl, prov.api_key);
            (u, HashMap::new())
        }
        ProviderKind::Claude => {
            let base = if prov.api_url.is_empty() { "https://api.anthropic.com".to_string() } else { prov.api_url.clone() };
            let u = format!("{}/v1/messages", base.trim_end_matches('/'));
            let mut h = HashMap::new();
            if !prov.api_key.is_empty() { h.insert("x-api-key".to_string(), prov.api_key.clone()); }
            h.insert("anthropic-version".to_string(), "2023-06-01".to_string());
            (u, h)
        }
    };

    // Build body per provider
    let body = match prov.kind {
        ProviderKind::Gemini => {
            // convert messages to Gemini contents
            let parts: Vec<serde_json::Value> = messages.iter().map(|m| {
                let role = m.get("role").and_then(|r| r.as_str()).unwrap_or("user");
                let text = m.get("content").and_then(|c| c.as_str()).unwrap_or("");
                let g_role = if role == "assistant" { "model" } else { "user" };
                serde_json::json!({"role": g_role, "parts": [{"text": text}]})
            }).collect();
            serde_json::json!({"contents": parts, "generationConfig": {"temperature": cfg.local.temp, "topP": cfg.local.top_p}})
        }
        ProviderKind::Claude => {
            let sys = messages.iter().find(|m| m.get("role").and_then(|r| r.as_str()) == Some("system")).and_then(|m| m.get("content").and_then(|c| c.as_str())).unwrap_or("");
            let msgs: Vec<serde_json::Value> = messages.iter().filter(|m| m.get("role").and_then(|r| r.as_str()) != Some("system")).map(|m| serde_json::json!({"role": m.get("role").unwrap_or(&serde_json::Value::String("user".to_string())), "content": m.get("content").unwrap_or(&serde_json::Value::String("".to_string()))})).collect();
            let mut j = serde_json::json!({"model": if model.is_empty() { prov.model.clone() } else { model.clone() }, "max_tokens": 2048, "messages": msgs});
            if !sys.is_empty() { j["system"] = serde_json::Value::String(sys.to_string()); }
            j
        }
        _ => {
            let mdl = if model.is_empty() { if prov.model.is_empty() { "local".to_string() } else { prov.model.clone() } } else { model.clone() };
            serde_json::json!({"model": mdl, "messages": messages, "temperature": cfg.local.temp, "top_p": cfg.local.top_p, "stream": false})
        }
    };

    let mut req = client.post(&url).json(&body);
    for (k, v) in headers { req = req.header(k, v); }
    // OpenAI/Gemini need content-type
    let resp = req.send().map_err(|e| format!("LLM offline {}: {}", url, e))?;
    if !resp.status().is_success() {
        return Err(format!("LLM HTTP {}: {}", resp.status(), resp.text().unwrap_or_default().chars().take(600).collect::<String>()));
    }
    let txt = resp.text().map_err(|e| e.to_string())?;
    // Normalize to OpenAI shape for frontend: try to extract content
    Ok(txt)
}

#[tauri::command]
pub fn llm_get_pipelines() -> Result<String, String> {
    let cfg = load_config_inner();
    serde_json::to_string(&cfg.pipelines).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn llm_save_pipeline(pipeline_json: String) -> Result<String, String> {
    let pipe: Pipeline = serde_json::from_str(&pipeline_json).map_err(|e| e.to_string())?;
    let mut cfg = load_config_inner();
    if let Some(pos) = cfg.pipelines.iter().position(|p| p.id == pipe.id) {
        cfg.pipelines[pos] = pipe;
    } else {
        cfg.pipelines.push(pipe);
    }
    save_config_inner(&cfg)?;
    Ok("saved".to_string())
}

#[tauri::command]
pub fn llm_delete_pipeline(id: String) -> Result<String, String> {
    let mut cfg = load_config_inner();
    cfg.pipelines.retain(|p| p.id != id);
    save_config_inner(&cfg)?;
    Ok("deleted".to_string())
}

#[tauri::command]
pub fn llm_pipeline_run(pipeline_id: String, input: String) -> Result<String, String> {
    let cfg = load_config_inner();
    let pipe = cfg.pipelines.iter().find(|p| p.id == pipeline_id).ok_or("pipeline not found")?;
    if pipe.steps.is_empty() {
        return Err("pipeline has no steps".to_string());
    }
    let mut ctx = input;
    for step in &pipe.steps {
        if !step.enabled { continue; }
        // simple template replace {{content}} and {{rag}}
        let prompt = step.prompt_template.replace("{{content}}", &ctx).replace("{{rag}}", &ctx);
        let msgs = vec![serde_json::json!({"role":"user","content": prompt})];
        let res = llm_chat_universal(step.provider_id.clone(), step.model.clone(), msgs).unwrap_or_else(|e| format!("error: {}", e));
        // try to extract content from OpenAI/Gemini/Claude response
        let out = extract_content(&res);
        ctx = out;
    }
    Ok(ctx)
}

fn extract_content(raw: &str) -> String {
    // try OpenAI
    if let Ok(j) = serde_json::from_str::<serde_json::Value>(raw) {
        if let Some(c) = j.get("choices").and_then(|x| x.get(0)).and_then(|x| x.get("message")).and_then(|m| m.get("content")).and_then(|c| c.as_str()) {
            return c.to_string();
        }
        if let Some(c) = j.get("candidates").and_then(|x| x.get(0)).and_then(|x| x.get("content")).and_then(|x| x.get("parts")).and_then(|x| x.get(0)).and_then(|x| x.get("text")).and_then(|c| c.as_str()) {
            return c.to_string();
        }
        if let Some(c) = j.get("content").and_then(|x| x.get(0)).and_then(|x| x.get("text")).and_then(|c| c.as_str()) {
            return c.to_string();
        }
        return raw.to_string();
    }
    raw.to_string()
}

// also need base64 crate available (already via reqwest indirect, but ensure)
