use std::fs;
use std::path::PathBuf;
use walkdir::WalkDir;
use regex::Regex;

fn vault_root() -> PathBuf {
    if let Ok(custom) = std::env::var("FRAGILE_VAULT") { return PathBuf::from(custom); }
    let home = std::env::var_os("HOME").map(PathBuf::from).or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from)).unwrap_or_else(|| PathBuf::from("."));
    home.join("Documents").join("FragileNotesVault")
}

fn llm_base_url(profile: &str) -> String {
    // env override or default ports: day 8010, archive 8011
    let port = std::env::var(format!("LLM_{}_PORT", profile.to_uppercase())).unwrap_or_else(|_| if profile=="day" {"8010".to_string()} else {"8011".to_string()});
    format!("http://127.0.0.1:{}", port)
}

pub fn llm_ping(profile: &str) -> String {
    let url = format!("{}/health", llm_base_url(profile));
    let client = reqwest::blocking::Client::builder().timeout(std::time::Duration::from_secs(5)).build().unwrap();
    match client.get(&url).send() {
        Ok(resp) if resp.status().is_success() => {
            let txt = resp.text().unwrap_or_default();
            format!("{{\"profile\":\"{}\",\"online\":true,\"health\":{}}}", profile, txt)
        },
        Ok(resp) => format!("{{\"profile\":\"{}\",\"online\":false,\"error\":\"HTTP {}\"}}", profile, resp.status()),
        Err(e) => format!("{{\"profile\":\"{}\",\"online\":false,\"error\":\"{}\"}}", profile, e.to_string().replace('"', "'")),
    }
}

#[tauri::command]
pub fn llm_status(profile: String) -> Result<String, String> {
    Ok(llm_ping(&profile))
}

#[tauri::command]
pub fn llm_chat(profile: String, messages: Vec<serde_json::Value>) -> Result<String, String> {
    let url = format!("{}/v1/chat/completions", llm_base_url(&profile));
    let client = reqwest::blocking::Client::builder().timeout(std::time::Duration::from_secs(90)).build().map_err(|e| e.to_string())?;
    let body = serde_json::json!({
        "model": "qwen3-14b",
        "messages": messages,
        "temperature": 0.7,
        "stream": false
    });
    let resp = client.post(&url).json(&body).send().map_err(|e| format!("LLM offline {}: {}", url, e))?;
    if !resp.status().is_success() {
        return Err(format!("LLM HTTP {}: {}", resp.status(), resp.text().unwrap_or_default()));
    }
    let txt = resp.text().map_err(|e| e.to_string())?;
    Ok(txt)
}

#[tauri::command]
pub fn enrich_notes(limit: usize, profile: String) -> Result<String, String> {
    let vault = vault_root();
    let sort_dir = vault.join("05 Sort");
    if !sort_dir.exists() { return Err("05 Sort not found".to_string()); }
    let mut enriched = 0;
    let mut errors = Vec::new();
    let re_front = Regex::new(r"(?s)^---\n(.*?)\n---\n").unwrap();
    for entry in WalkDir::new(&sort_dir).into_iter().filter_map(|e| e.ok()).take(limit) {
        let p = entry.path();
        if !p.is_file() || p.extension().and_then(|e| e.to_str()) != Some("md") { continue; }
        let content = match fs::read_to_string(p) { Ok(c) => c, Err(e) => { errors.push(e.to_string()); continue; } };
        // skip already enriched
        if content.contains("enriched: true") || content.contains("ao_enriched:") { continue; }
        let body = if let Some(cap) = re_front.captures(&content) {
            content[cap.get(0).unwrap().end()..].to_string()
        } else { content.clone() };
        let snippet = body.chars().take(1500).collect::<String>();
        let prompt = format!("Обогати заметку: выдели 3 тега #тег, краткое описание 1 предложение, и 2 связанные [[wikilinks]]. Заметка:\n\n{}\n\nОтвет JSON: {{\"tags\":[\"#...\"],\"description\":\"...\",\"links\":[\"[[...\"]]\"]}}", snippet);
        let messages = vec![
            serde_json::json!({"role":"system","content":"Ты — помощник Archive Organism. Отвечай JSON."}),
            serde_json::json!({"role":"user","content": prompt}),
        ];
        let llm_result = llm_chat(profile.clone(), messages);
        match llm_result {
            Ok(resp) => {
                // try to extract JSON tags
                let tags = extract_tags(&resp);
                let enriched_content = format!("{}\n\n---\nenriched: true\ntags: [{}]\nllm_raw: {}\n---\n", content.trim(), tags.join(", "), resp.chars().take(500).collect::<String>().replace('\n', " "));
                // append enrichment frontmatter at end (simplified)
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
    let re = Regex::new(r"#([A-Za-z0-9/_\-]+)").unwrap();
    re.captures_iter(resp).map(|c| format!("#{}", &c[1])).take(3).collect()
}

#[tauri::command]
pub fn embeddings_search(query: String, limit: usize) -> Result<String, String> {
    // simple TF-IDF fallback via FTS5 already in fts_search, here we just call fts
    let vault = vault_root();
    let db = vault.join(".fragile_fts.db");
    if !db.exists() {
        return Ok("{\"results\":[],\"note\":\"fts db not found, run FTS index\"}".to_string());
    }
    // reuse fts_search logic via direct rusqlite
    let conn = rusqlite::Connection::open(&db).map_err(|e| e.to_string())?;
    let mut stmt = conn.prepare("SELECT path FROM fts WHERE fts MATCH ?1 LIMIT ?2").map_err(|e| e.to_string())?;
    let rows = stmt.query_map(rusqlite::params![query, limit as i64], |row| row.get::<_, String>(0)).map_err(|e| e.to_string())?;
    let mut results = Vec::new();
    for r in rows { if let Ok(p) = r { results.push(p); } }
    Ok(serde_json::to_string(&serde_json::json!({"results": results})).unwrap())
}
