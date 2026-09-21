use std::fs;
use std::path::PathBuf;
use walkdir::WalkDir;
use regex::Regex;
use once_cell::sync::Lazy;
use crate::llm::{gateway_chat, ChatRequest};

fn vault_root() -> PathBuf {
    if let Ok(custom) = std::env::var("FRAGILE_VAULT") { return PathBuf::from(custom); }
    let home = std::env::var_os("HOME").map(PathBuf::from).or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from)).unwrap_or_else(|| PathBuf::from("."));
    home.join("Documents").join("FragileNotesVault")
}

static RE_FRONT: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?s)^---\n(.*?)\n---\n").unwrap());
static RE_TAG: Lazy<Regex> = Lazy::new(|| Regex::new(r"#([A-Za-z0-9/_\-]+)").unwrap());

// Legacy profile -> provider mapping: day/archive both map to local provider via Gateway
fn profile_to_provider(profile: &str) -> String {
    match profile {
        "day" | "archive" | "" => "local".to_string(),
        other => other.to_string(),
    }
}

#[tauri::command]
pub async fn llm_status(profile: String) -> Result<String, String> {
    // Unified via Gateway health: use llm_test_provider
    let prov = profile_to_provider(&profile);
    crate::llm::llm_test_provider(prov).await
}

#[tauri::command]
pub async fn llm_chat(profile: String, messages: Vec<serde_json::Value>) -> Result<String, String> {
    let provider_id = profile_to_provider(&profile);
    // task-enrich for legacy enrich, task-chat for generic chat — infer from profile
    let task = if provider_id=="local" { Some("task-chat".to_string()) } else { None };
    let req = ChatRequest { provider_id, model_ref: String::new(), messages, task_profile_id: task };
    gateway_chat(req).await
}

#[tauri::command]
pub async fn enrich_notes(limit: usize, profile: String) -> Result<String, String> {
    let vault = vault_root();
    let sort_dir = vault.join("05 Sort");
    if !sort_dir.exists() { return Err("05 Sort not found".to_string()); }
    let mut enriched = 0;
    let mut errors = Vec::new();
    for entry in WalkDir::new(&sort_dir).into_iter().filter_map(|e| e.ok()).take(limit) {
        let p = entry.path();
        if !p.is_file() || p.extension().and_then(|e| e.to_str()) != Some("md") { continue; }
        let content = match fs::read_to_string(p) { Ok(c) => c, Err(e) => { errors.push(e.to_string()); continue; } };
        if content.contains("enriched: true") || content.contains("ao_enriched:") { continue; }
        let body = if let Some(cap) = RE_FRONT.captures(&content) {
            content[cap.get(0).unwrap().end()..].to_string()
        } else { content.clone() };
        let snippet = body.chars().take(1500).collect::<String>();
        let prompt = format!("Обогати заметку: выдели 3 тега #тег, краткое описание 1 предложение, и 2 связанные [[wikilinks]]. Заметка:\n\n{}\n\nОтвет JSON: {{\"tags\":[\"#...\"],\"description\":\"...\",\"links\":[\"[[...\"]]\"]}}", snippet);
        let messages = vec![
            serde_json::json!({"role":"system","content":"Ты — помощник Archive Organism. Отвечай JSON."}),
            serde_json::json!({"role":"user","content": prompt}),
        ];
        let provider_id = profile_to_provider(&profile);
        let req = ChatRequest { provider_id: provider_id.clone(), model_ref: String::new(), messages, task_profile_id: Some("task-enrich".to_string()) };
        match gateway_chat(req).await {
            Ok(resp) => {
                let tags = extract_tags(&resp);
                let enriched_content = format!("{}\n\n---\nenriched: true\ntags: [{}]\nllm_raw: {}\n---\n", content.trim(), tags.join(", "), resp.chars().take(500).collect::<String>().replace('\n', " "));
                match fs::write(p, enriched_content) {
                    Ok(_) => enriched += 1,
                    Err(e) => errors.push(e.to_string()),
                }
            },
            Err(e) => errors.push(format!("{} LLM error: {}", p.display(), e)),
        }
        if enriched >= limit { break; }
    }
    Ok(format!("{{\"enriched\":{},\"errors\":{:?}}}", enriched, errors))
}

fn extract_tags(resp: &str) -> Vec<String> {
    RE_TAG.captures_iter(resp).map(|c| format!("#{}", &c[1])).take(3).collect()
}

#[tauri::command]
pub fn embeddings_search(query: String, limit: usize) -> Result<String, String> {
    // FTS5 lexical, not embeddings — keep as SearchBackend::lexical
    let vault = vault_root();
    let db = vault.join(".fragile_fts.db");
    if !db.exists() {
        return Ok("{\"results\":[],\"note\":\"fts db not found, run FTS index\"}".to_string());
    }
    let conn = rusqlite::Connection::open(&db).map_err(|e| e.to_string())?;
    let mut stmt = conn.prepare("SELECT path FROM fts WHERE fts MATCH ?1 LIMIT ?2").map_err(|e| e.to_string())?;
    let rows = stmt.query_map(rusqlite::params![query, limit as i64], |row| row.get::<_, String>(0)).map_err(|e| e.to_string())?;
    let mut results = Vec::new();
    for r in rows { if let Ok(p) = r { results.push(p); } }
    Ok(serde_json::to_string(&serde_json::json!({"results": results})).unwrap())
}
