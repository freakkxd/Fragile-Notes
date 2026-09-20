use std::fs;
use std::path::{Path, PathBuf};
use walkdir::WalkDir;
use chrono::Utc;
use regex::Regex;

fn vault_root() -> PathBuf {
    if let Ok(custom) = std::env::var("FRAGILE_VAULT") { return PathBuf::from(custom); }
    let home = std::env::var_os("HOME").map(PathBuf::from).or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from)).unwrap_or_else(|| PathBuf::from("."));
    home.join("Documents").join("FragileNotesVault")
}

fn parse_frontmatter(content: &str) -> (Option<String>, String) {
    if content.starts_with("---") {
        if let Some(end) = content[3..].find("\n---") {
            let fm = content[3..3+end].to_string();
            let body = content[3+end+4..].trim_start_matches('\n').to_string();
            return (Some(fm), body);
        }
    }
    (None, content.to_string())
}

fn has_inbox_frontmatter(content: &str) -> bool {
    let (fm, _) = parse_frontmatter(content);
    if let Some(f) = fm {
        return f.contains("ao_sort_status") || f.contains("source:") && f.contains("origin:")
    }
    false
}

fn slugify(s: &str) -> String {
    let re = Regex::new(r"[^\p{L}\p{N}]+").unwrap();
    let lower = s.to_lowercase();
    let slug = re.replace_all(&lower, "-");
    let slug = slug.trim_matches('-').to_string();
    if slug.is_empty() { "item".to_string() } else { slug[..slug.len().min(80)].to_string() }
}

fn first_url(text: &str) -> Option<String> {
    let re = Regex::new(r#"https?://[^\s<>"')\]]+"#).unwrap();
    re.find(text).map(|m| m.as_str().to_string())
}

pub fn wrap_web_clips(vault: &Path) -> (usize, usize, Vec<String>) {
    let web_clips_dirs = [
        vault.join("_System/ArchiveOrganism/Sources/web-clips"),
        vault.join("Sources/web-clips"),
        vault.join("_System/ArchiveOrganism/Sources/raw"),
    ];
    let mut scanned = 0;
    let mut wrapped = 0;
    let mut errors = Vec::new();
    let sort_dir = vault.join("05 Sort");
    let _ = fs::create_dir_all(&sort_dir);
    let now = Utc::now().format("%Y-%m-%d").to_string();
    for dir in web_clips_dirs.iter().filter(|d| d.exists()) {
        for entry in WalkDir::new(dir).into_iter().filter_map(|e| e.ok()) {
            let p = entry.path();
            if !p.is_file() || p.extension().and_then(|e| e.to_str()) != Some("md") { continue; }
            scanned += 1;
            let rel = p.strip_prefix(vault).unwrap_or(p).to_string_lossy().to_string();
            let content = match fs::read_to_string(p) { Ok(c) => c, Err(e) => { errors.push(format!("{} read failed: {}", rel, e)); continue; } };
            if has_inbox_frontmatter(&content) { continue; }
            // webClipToSourceItem simplified: extract url, title, body
            let (fm_opt, body) = parse_frontmatter(&content);
            let fm_str = fm_opt.unwrap_or_default();
            let url = {
                let re = Regex::new(r#"url["\s:]+([^\s\n]+)"#).unwrap();
                re.captures(&fm_str).and_then(|c| c.get(1)).map(|m| m.as_str().to_string())
                    .or_else(|| first_url(&body))
                    .or_else(|| first_url(&content))
                    .unwrap_or_default()
            };
            let title = {
                let re = Regex::new(r#"title["\s:]+(.+)"#).unwrap();
                re.captures(&fm_str).and_then(|c| c.get(1)).map(|m| m.as_str().trim().trim_matches('"').trim_matches('\'').to_string())
                    .or_else(|| body.lines().find(|l| !l.trim().is_empty()).map(|s| s.trim().to_string()))
                    .unwrap_or_else(|| "Web Clip".to_string())
            };
            let slug = slugify(&title);
            let date_prefix = now.clone();
            let inbox_path = sort_dir.join(format!("{}-{}.md", date_prefix, slug));
            // build inbox markdown
            let inbox_md = format!(
                "---\nsource: web\norigin: {}\ncreated: {}\nao_sort_status: undecided\nao_sort_decided_at: \"\"\nao_sort_reason: \"\"\ntags: [sort-inbox]\ncssclasses: [ao-sort-inbox]\ntitle: \"{}\"\nurl: \"{}\"\n---\n\n{}\n\n> Source: {}\n",
                if url.is_empty() { "web-clipper" } else { &url },
                Utc::now().to_rfc3339(),
                title.replace('"', "'"),
                url,
                body.trim(),
                url
            );
            // write inbox note (avoid overwrite)
            let mut final_path = inbox_path.clone();
            let mut counter = 1;
            while final_path.exists() {
                final_path = sort_dir.join(format!("{}-{}-{}.md", date_prefix, slug, counter));
                counter += 1;
            }
            match fs::write(&final_path, inbox_md) {
                Ok(_) => {
                    wrapped += 1;
                    // optionally delete original if different path
                    if p != &final_path {
                        let _ = fs::remove_file(p);
                    }
                }
                Err(e) => errors.push(format!("{} write failed: {}", final_path.display(), e)),
            }
        }
    }
    (scanned, wrapped, errors)
}

#[tauri::command]
pub fn collect_sources() -> Result<String, String> {
    let vault = vault_root();
    let (scanned, wrapped, errors) = wrap_web_clips(&vault);
    // Telegram RSS if enabled (try python script)
    let telegram_output = {
        let script = vault.join("_System/ArchiveOrganism/scripts/telegram_rss.py");
        if script.exists() {
            match std::process::Command::new("python").arg(&script).output() {
                Ok(o) => format!("telegram: {}", String::from_utf8_lossy(&o.stdout).chars().take(200).collect::<String>()),
                Err(e) => format!("telegram skip: {}", e),
            }
        } else {
            "telegram: script not found".to_string()
        }
    };
    Ok(format!("{{\"scanned\":{},\"wrapped\":{},\"telegram\":\"{}\",\"errors\":{:?}}}", scanned, wrapped, telegram_output.replace('"', "'"), errors))
}

#[tauri::command]
pub fn web_clip(url: String, title: String, html: String, selection: String) -> Result<String, String> {
    let vault = vault_root();
    let clips_dir = vault.join("_System/ArchiveOrganism/Sources/web-clips");
    fs::create_dir_all(&clips_dir).map_err(|e| e.to_string())?;
    let content = if !selection.trim().is_empty() { selection } else { html };
    let slug = slugify(if title.is_empty() { &url } else { &title });
    let now = Utc::now();
    let date = now.format("%Y-%m-%d").to_string();
    let path = clips_dir.join(format!("{}-{}.md", date, slug));
    let md = format!(
        "---\ntitle: \"{}\"\nurl: \"{}\"\ncreated: {}\nsource: web\n---\n\n{}\n",
        title.replace('"', "'"),
        url,
        now.to_rfc3339(),
        content.chars().take(8000).collect::<String>()
    );
    fs::write(&path, md).map_err(|e| e.to_string())?;
    // auto wrap
    let (scanned, wrapped, _) = wrap_web_clips(&vault);
    Ok(format!("saved to {} (scanned {}, wrapped {})", path.display(), scanned, wrapped))
}
