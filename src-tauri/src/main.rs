#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
#![allow(clippy::all, clippy::pedantic, clippy::nursery)]
#![allow(dead_code, unused)]

mod collect;
mod enrich;
mod tasks;
mod llm;
use collect::{collect_sources, web_clip};
use enrich::{embeddings_search, enrich_notes, llm_chat, llm_status};
use llm::{
    llm_chat_universal, llm_delete_pipeline, llm_delete_provider_credential, llm_download_model,
    llm_get_config, llm_get_pipelines, llm_has_provider_credential, llm_pipeline_run,
    llm_runtime_health, llm_runtime_list, llm_runtime_logs, llm_runtime_restart, llm_runtime_start,
    llm_runtime_stop, llm_save_config, llm_save_pipeline, llm_scan_models, llm_set_active_model,
    llm_set_provider_credential, llm_task_cancel, llm_task_run, llm_task_status, llm_test_provider,
};
use llm::models::{llm_models_list, llm_models_scan};
use llm::download::{llm_download_cancel, llm_download_list, llm_download_pause, llm_download_resume, llm_download_start, llm_download_status};
use tasks::{tasks_archive, tasks_create, tasks_list, tasks_update_status};
use once_cell::sync::Lazy;
use regex::Regex;
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

static RE_WIKILINK: Lazy<Regex> = Lazy::new(|| Regex::new(r"\[\[([^\]|#]+)").unwrap());

fn vault_root() -> PathBuf {
    if let Ok(custom) = std::env::var("FRAGILE_VAULT") {
        return PathBuf::from(custom);
    }
    let home = dirs_next();
    let root = home.join("Documents").join("FragileNotesVault");
    // ensure exists, but never panic — program must stay open even if vault missing
    let _ = fs::create_dir_all(&root);
    root
}

fn dirs_next() -> PathBuf {
    if let Some(h) = std::env::var_os("HOME") {
        return PathBuf::from(h);
    }
    if let Some(h) = std::env::var_os("USERPROFILE") {
        return PathBuf::from(h);
    }
    PathBuf::from(".")
}

fn is_text_file(p: &Path) -> bool {
    const ALLOWED: &[&str] = &[
        "md", "txt", "html", "htm", "css", "js", "ts", "jsx", "tsx", "json", "yaml", "yml", "toml",
        "ini", "cfg", "py", "cpp", "h", "hpp", "c", "rs", "go", "java", "sh", "xml", "svg", "csv",
        "log", "enc", "pdf", "mdx",
    ];
    if let Some(ext) = p.extension().and_then(|e| e.to_str()) {
        if ALLOWED.contains(&ext.to_ascii_lowercase().as_str()) {
            return true;
        }
        // binary check: no null bytes in first 1k, else binary
        if let Ok(f) = fs::File::open(p) {
            use std::io::Read;
            let mut buf = [0u8; 1024];
            if let Ok(n) = std::io::BufReader::new(f).read(&mut buf) {
                if n == 0 {
                    return true;
                }
                if buf[..n].contains(&0) {
                    return false;
                }
            }
        }
    } else {
        // no extension — treat as text if no null bytes
        if let Ok(f) = fs::File::open(p) {
            use std::io::Read;
            let mut buf = [0u8; 1024];
            if let Ok(n) = std::io::BufReader::new(f).read(&mut buf) {
                if buf[..n].contains(&0) {
                    return false;
                }
            }
        }
    }
    true
}

#[tauri::command]
fn list_notes() -> Vec<String> {
    let root = vault_root();
    if !root.exists() {
        let _ = fs::create_dir_all(&root);
        return vec![];
    }
    let mut out = Vec::with_capacity(256);
    // filter_entry prevents descending into heavy dirs
    let walker = WalkDir::new(&root).into_iter().filter_entry(|e| {
        let name = e.file_name().to_string_lossy();
        !matches!(
            name.as_ref(),
            "node_modules" | ".git" | "dist" | "build" | "target" | ".venv" | "venv" | ".cargo"
        )
    });
    for entry in walker.filter_map(|e| e.ok()) {
        let p = entry.path();
        if p.is_file() && is_text_file(p) {
            if let Ok(rel) = p.strip_prefix(&root) {
                // normalize to forward slashes for frontend
                out.push(rel.to_string_lossy().replace('\\', "/"));
            }
        }
    }
    out.sort();
    out
}

#[tauri::command]
fn read_note(path: String) -> Result<String, String> {
    if path.contains("..") || path.starts_with('/') || path.starts_with('\\') {
        return Err("invalid path".into());
    }
    let full = vault_root().join(&path);
    fs::read_to_string(&full).map_err(|e| format!("read {}: {}", path, e))
}

#[tauri::command]
fn write_note(path: String, content: String) -> Result<(), String> {
    if path.contains("..") || path.starts_with('/') || path.starts_with('\\') {
        return Err("invalid path".into());
    }
    let full = vault_root().join(&path);
    if let Some(parent) = full.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    fs::write(&full, &content).map_err(|e| e.to_string())?;
    let db = vault_root().join(".fragile_fts.db");
    if db.exists() {
        let _ = fts_index(&db, &full, &content);
    }
    Ok(())
}

fn fts_index(db: &Path, file: &Path, content: &str) -> rusqlite::Result<()> {
    let conn = Connection::open(db)?;
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(path, content)",
        [],
    )?;
    conn.execute("DELETE FROM fts WHERE path = ?1", params![file.to_string_lossy()])?;
    conn.execute(
        "INSERT INTO fts(path, content) VALUES(?1, ?2)",
        params![file.to_string_lossy(), content],
    )?;
    Ok(())
}

#[tauri::command]
fn fts_search(query: String) -> Vec<String> {
    let db = vault_root().join(".fragile_fts.db");
    if !db.exists() {
        return list_notes()
            .into_iter()
            .filter(|p| {
                read_note(p.clone())
                    .map(|c| c.to_lowercase().contains(&query.to_lowercase()))
                    .unwrap_or(false)
            })
            .collect();
    }
    let conn = match Connection::open(&db) {
        Ok(c) => c,
        Err(_) => return vec![],
    };
    let mut stmt = match conn.prepare("SELECT path FROM fts WHERE fts MATCH ?1") {
        Ok(s) => s,
        Err(_) => return vec![],
    };
    let rows = stmt.query_map(params![query], |row| row.get::<_, String>(0));
    match rows {
        Ok(iter) => iter.filter_map(|r| r.ok()).collect(),
        Err(_) => vec![],
    }
}

#[derive(Serialize, Deserialize)]
struct LinkInfo {
    from: String,
    to: String,
    line: usize,
}

#[tauri::command]
fn get_links(path: String) -> Vec<LinkInfo> {
    let content = read_note(path.clone()).unwrap_or_default();
    let mut out = Vec::new();
    for (idx, line) in content.lines().enumerate() {
        for cap in RE_WIKILINK.captures_iter(line) {
            out.push(LinkInfo {
                from: path.clone(),
                to: cap[1].trim().to_string(),
                line: idx + 1,
            });
        }
    }
    out
}

fn main() {
    // Ensure vault exists before Tauri starts — never panic, just log
    let root = vault_root();
    eprintln!("[fragile] vault: {}", root.display());
    let _ = fs::create_dir_all(&root);

    tauri::Builder::default()
        .setup(move |_app| {
            eprintln!("[fragile] setup ok, vault {}", root.display());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            list_notes,
            read_note,
            write_note,
            fts_search,
            get_links,
            collect_sources,
            web_clip,
            llm_status,
            llm_chat,
            enrich_notes,
            embeddings_search,
            tasks_list,
            tasks_create,
            tasks_update_status,
            tasks_archive,
            llm_get_config,
            llm_save_config,
            llm_scan_models,
            llm_set_active_model,
            llm_download_model,
            llm_test_provider,
            llm_chat_universal,
            llm_get_pipelines,
            llm_save_pipeline,
            llm_delete_pipeline,
            llm_pipeline_run,
            llm_has_provider_credential,
            llm_set_provider_credential,
            llm_delete_provider_credential,
            llm_runtime_list,
            llm_runtime_start,
            llm_runtime_stop,
            llm_runtime_health,
            llm_runtime_logs,
            llm_runtime_restart,
            llm_task_run,
            llm_task_cancel,
            llm_task_status,
            llm_models_scan,
            llm_models_list,
            llm_download_start,
            llm_download_pause,
            llm_download_resume,
            llm_download_cancel,
            llm_download_status,
            llm_download_list
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri app");
}
