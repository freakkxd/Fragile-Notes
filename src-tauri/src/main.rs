#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use regex::Regex;
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

fn vault_root() -> PathBuf {
    if let Ok(custom) = std::env::var("FRAGILE_VAULT") {
        return PathBuf::from(custom);
    }
    let home = dirs_next();
    home.join("Documents").join("FragileNotesVault")
}

fn dirs_next() -> PathBuf {
    if let Some(h) = std::env::var_os("HOME") {
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
        // binary check fallback: no null bytes in first 1k
        if let Ok(b) = fs::read(p) {
            if b.contains(&0) {
                return false;
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
    let mut out = Vec::new();
    for entry in WalkDir::new(&root).into_iter().filter_map(|e| e.ok()) {
        let p = entry.path();
        if p.is_file() && is_text_file(p) {
            if let Ok(rel) = p.strip_prefix(&root) {
                out.push(rel.to_string_lossy().to_string());
            }
        }
        // skip heavy dirs
        if p.is_dir() {
            let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if ["node_modules", ".git", "dist", "build", "target", ".venv"].contains(&name) {
                continue;
            }
        }
    }
    out.sort();
    out
}

#[tauri::command]
fn read_note(path: String) -> Result<String, String> {
    // zod-like runtime validation: path must be relative, no .. traversal
    if path.contains("..") || path.starts_with('/') || path.starts_with('\\') {
        return Err("invalid path".into());
    }
    let full = vault_root().join(&path);
    fs::read_to_string(&full).map_err(|e| e.to_string())
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
    fs::write(&full, content).map_err(|e| e.to_string())?;
    // update FTS if db exists
    let db = vault_root().join(".fragile_fts.db");
    if db.exists() {
        let _ = fts_index(&db, &full, &fs::read_to_string(&full).unwrap_or_default());
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
        // fallback: naive scan
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
    let re = Regex::new(r"\[\[([^\]|#]+)").unwrap();
    let mut out = Vec::new();
    for (idx, line) in content.lines().enumerate() {
        for cap in re.captures_iter(line) {
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
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            list_notes,
            read_note,
            write_note,
            fts_search,
            get_links
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri app");
}
