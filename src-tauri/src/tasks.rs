use std::fs;
use std::path::{Path, PathBuf};
use walkdir::WalkDir;
use serde::{Deserialize, Serialize};
use chrono::{NaiveDate, Local};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Task {
    pub id: String,
    pub title: String,
    pub status: String, // todo, doing, done, cancelled
    pub task_type: String, // normal, routine, bill
    pub due: String, // YYYY-MM-DD
    pub scheduled: String,
    pub priority: String,
    pub project: String,
    pub path: String,
    pub created: String,
}

fn vault_root() -> PathBuf {
    if let Ok(custom) = std::env::var("FRAGILE_VAULT") { return PathBuf::from(custom); }
    let home = std::env::var_os("HOME").map(PathBuf::from).or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from)).unwrap_or_else(|| PathBuf::from("."));
    home.join("Documents").join("FragileNotesVault")
}

fn is_terminal(status: &str) -> bool {
    matches!(status, "done" | "cancelled" | "archived")
}

fn parse_ymd(s: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(s.trim(), "%Y-%m-%d").ok()
}

fn is_relevant_on_day(task: &Task, day: &str) -> bool {
    if is_terminal(&task.status) { return false; }
    let day_date = match parse_ymd(day) { Some(d) => d, None => return false };
    if task.task_type == "routine" {
        if let Some(next) = parse_ymd(&task.scheduled).or_else(|| parse_ymd(&task.due)) {
            return next <= day_date;
        }
        return false;
    }
    if let Some(due) = parse_ymd(&task.due) { if due <= day_date { return true; } }
    if let Some(sched) = parse_ymd(&task.scheduled) { if sched <= day_date { return true; } }
    false
}

fn tasks_root(vault: &Path) -> PathBuf {
    // Try Tasks folder, fallback to 05 Sort/Daily
    let candidate = vault.join("Tasks");
    if candidate.exists() { candidate } else { vault.join("05 Sort") }
}

fn load_tasks_from_vault(vault: &Path) -> Vec<Task> {
    let root = tasks_root(vault);
    if !root.exists() { return vec![]; }
    let mut out = Vec::new();
    for entry in WalkDir::new(&root).into_iter().filter_map(|e| e.ok()) {
        let p = entry.path();
        if !p.is_file() || p.extension().and_then(|e| e.to_str()) != Some("md") { continue; }
        let content = match fs::read_to_string(p) { Ok(c) => c, Err(_) => continue };
        // frontmatter parse simplified
        let fm = if content.starts_with("---") {
            content[3..].find("\n---").map(|end| content[3..3+end].to_string()).unwrap_or_default()
        } else { "".to_string() };
        let get = |k: &str| -> String {
            for line in fm.lines() {
                if line.trim_start().starts_with(&format!("{}:", k)) {
                    let v = line.splitn(2, ':').nth(1).unwrap_or("").trim().trim_matches('"').trim_matches('\'').to_string();
                    return v;
                }
            }
            "".to_string()
        };
        let title = get("title").trim().to_string();
        let title = if title.is_empty() {
            p.file_stem().and_then(|s| s.to_str()).unwrap_or("Untitled").replace('-', " ")
        } else { title };
        let status = {
            let s = get("status").to_lowercase();
            if s.is_empty() {
                if content.contains("- [x]") { "done".to_string() } else if content.contains("- [ ]") { "todo".to_string() } else { "todo".to_string() }
            } else { s }
        };
        let task = Task {
            id: get("id").trim().to_string().chars().take(40).collect::<String>().trim().to_string(),
            title: title.chars().take(120).collect(),
            status: if status.is_empty() { "todo".to_string() } else { status },
            task_type: { let t = get("task_type").to_lowercase(); if ["normal","routine","bill"].contains(&t.as_str()) { t } else { "normal".to_string() } },
            due: get("due"),
            scheduled: get("scheduled"),
            priority: get("priority"),
            project: get("project"),
            path: p.strip_prefix(vault).unwrap_or(p).to_string_lossy().replace('\\', "/"),
            created: get("created"),
        };
        let id = if task.id.is_empty() { format!("task:{}", out.len()) } else { task.id.clone() };
        let mut t = task;
        t.id = id;
        out.push(t);
    }
    out
}

#[tauri::command]
pub fn tasks_list(filter: String) -> Result<String, String> {
    let vault = vault_root();
    let tasks = load_tasks_from_vault(&vault);
    let today = Local::now().format("%Y-%m-%d").to_string();
    let filtered: Vec<Task> = match filter.as_str() {
        "today" => tasks.into_iter().filter(|t| is_relevant_on_day(t, &today) || t.due == today).collect(),
        "board" => tasks.into_iter().filter(|t| !is_terminal(&t.status)).collect(),
        "completed" => tasks.into_iter().filter(|t| is_terminal(&t.status)).collect(),
        "workspace" => tasks,
        _ => tasks,
    };
    Ok(serde_json::to_string(&filtered).unwrap())
}

#[tauri::command]
pub fn tasks_create(title: String, project: String, due: String) -> Result<String, String> {
    let vault = vault_root();
    let root = tasks_root(&vault);
    fs::create_dir_all(&root).map_err(|e| e.to_string())?;
    let id = format!("task-{}", chrono::Utc::now().timestamp_millis());
    let date = Local::now().format("%Y-%m-%d").to_string();
    let safe_title = title.replace(|c: char| !c.is_alphanumeric() && c != ' ', "-").replace(' ', "-").chars().take(40).collect::<String>();
    let path = root.join(format!("{}-{}.md", date, safe_title));
    let content = format!("---\ntitle: \"{}\"\nstatus: todo\ntask_type: normal\nproject: \"{}\"\ndue: \"{}\"\ncreated: \"{}\"\nid: \"{}\"\n---\n\n# {}\n\n- [ ] {}\n", title.replace('"', "'"), project, due, date, id, title, title);
    fs::write(&path, content).map_err(|e| e.to_string())?;
    let task = Task { id: id.clone(), title, status: "todo".to_string(), task_type: "normal".to_string(), due, scheduled: "".to_string(), priority: "".to_string(), project, path: path.strip_prefix(&vault).unwrap_or(&path).to_string_lossy().to_string(), created: date };
    Ok(serde_json::to_string(&task).unwrap())
}

#[tauri::command]
pub fn tasks_update_status(id: String, status: String) -> Result<String, String> {
    let vault = vault_root();
    let tasks = load_tasks_from_vault(&vault);
    for t in tasks {
        if t.id == id {
            let full = vault.join(&t.path);
            let content = fs::read_to_string(&full).map_err(|e| e.to_string())?;
            let new_content = if content.contains("status:") {
                content.lines().map(|l| if l.trim_start().starts_with("status:") { format!("status: {}", status) } else { l.to_string() }).collect::<Vec<_>>().join("\n")
            } else {
                content.replace("---", &format!("---\nstatus: {}", status))
            };
            // also update - [ ] to - [x] if done
            let final_content = if status == "done" {
                new_content.replace("- [ ]", "- [x]")
            } else if status == "todo" {
                new_content.replace("- [x]", "- [ ]")
            } else { new_content };
            fs::write(&full, final_content).map_err(|e| e.to_string())?;
            return Ok(format!("{{\"id\":\"{}\",\"status\":\"{}\"}}", id, status));
        }
    }
    Err(format!("task {} not found", id))
}

#[tauri::command]
pub fn tasks_archive() -> Result<String, String> {
    let vault = vault_root();
    let tasks = load_tasks_from_vault(&vault);
    let mut archived = 0;
    for t in tasks.iter().filter(|t| is_terminal(&t.status)) {
        let full = vault.join(&t.path);
        if full.exists() {
            let archive_dir = vault.join("Archive/Tasks");
            let _ = fs::create_dir_all(&archive_dir);
            let dest = archive_dir.join(full.file_name().unwrap_or_default());
            let _ = fs::rename(&full, dest);
            archived += 1;
        }
    }
    Ok(format!("{{\"archived\":{}}}", archived))
}
