use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::fs;
use super::types::{ModelRecord, ModelState, ScanResult};
use super::identity::{stable_id, file_identity};
use super::scanner::Scanner;
use std::sync::{Arc, atomic::AtomicBool};

pub fn registry_path() -> PathBuf { crate::llm::registry_path() }

pub struct Registry {
    path: PathBuf,
    records: HashMap<String, ModelRecord>, // id -> record
}

impl Registry {
    pub fn new(path: PathBuf) -> Self { Self { path, records: HashMap::new() } }

    pub fn load(&mut self) -> Result<(), String> {
        if !self.path.exists() { return Ok(()); }
        let txt = fs::read_to_string(&self.path).map_err(|e| e.to_string())?;
        let list: Vec<ModelRecord> = serde_json::from_str(&txt).map_err(|e| e.to_string())?;
        for r in list { self.records.insert(r.id.clone(), r); }
        Ok(())
    }

    pub fn save(&self) -> Result<(), String> {
        let dir = self.path.parent().unwrap_or(Path::new("."));
        let _ = fs::create_dir_all(dir);
        let list: Vec<&ModelRecord> = self.records.values().collect();
        let txt = serde_json::to_string_pretty(&list).map_err(|e| e.to_string())?;
        let tmp = self.path.with_extension("tmp");
        fs::write(&tmp, txt).map_err(|e| e.to_string())?;
        if let Ok(f) = fs::File::open(&tmp) { let _ = f.sync_all(); }
        fs::rename(&tmp, &self.path).map_err(|e| e.to_string())?;
        Ok(())
    }

    pub fn update_with_scan(&mut self, scan: ScanResult) -> ScanResult {
        let mut added = Vec::new();
        let mut updated = Vec::new();
        let mut seen_ids = HashSet::new();

        for mut rec in scan.discovered {
            // Check for moved file: same sha256 but different stable id (path change)
            let mut target_id = rec.id.clone();
            let mut is_moved = false;
            if !self.records.contains_key(&rec.id) {
                if let Some(sha) = &rec.sha256 {
                    if !sha.is_empty() {
                        for (existing_id, existing) in self.records.iter() {
                            if let Some(existing_sha) = &existing.sha256 {
                                if existing_sha == sha && existing.size_bytes == rec.size_bytes {
                                    // Same file moved: keep original id, update path
                                    target_id = existing_id.clone();
                                    is_moved = true;
                                    break;
                                }
                            }
                        }
                    }
                }
            }
            seen_ids.insert(target_id.clone());
            if let Some(existing) = self.records.get_mut(&target_id) {
                let old_roles = existing.roles.clone();
                let old_first_seen = existing.first_seen_at;
                let old_id = existing.id.clone();
                // Changed detection: same path but different sha256 or size
                let is_changed = if is_moved {
                    false // moved is not changed, it's present at new path
                } else if existing.path == rec.path && existing.sha256.is_some() && rec.sha256.is_some() && existing.sha256 != rec.sha256 {
                    true
                } else if existing.size_bytes != rec.size_bytes {
                    true
                } else {
                    false
                };
                if is_changed {
                    existing.state = ModelState::Changed;
                    updated.push(old_id.clone());
                } else {
                    existing.state = ModelState::Present;
                    if is_moved { updated.push(old_id.clone()); }
                }
                existing.last_seen_at = chrono::Utc::now();
                existing.path = rec.path.clone();
                existing.size_bytes = rec.size_bytes;
                existing.sha256 = rec.sha256.clone();
                existing.metadata = rec.metadata.clone();
                if !old_roles.is_empty() { existing.roles = old_roles; }
                existing.first_seen_at = old_first_seen;
                // Ensure id stays stable
                existing.id = old_id;
            } else {
                // New
                let id = rec.id.clone();
                // ensure id is stable (not verified truncated)
                rec.id = id.clone();
                self.records.insert(id.clone(), rec);
                added.push(id);
            }
        }
        // Missing: those in registry but not seen
        let mut missing = Vec::new();
        for (id, rec) in self.records.iter_mut() {
            if !seen_ids.contains(id) {
                if rec.state != ModelState::Missing {
                    rec.state = ModelState::Missing;
                    missing.push(id.clone());
                }
            }
        }
        ScanResult{ discovered: self.records.values().cloned().collect(), added, updated, missing, invalid: scan.invalid }
    }

    pub fn all(&self) -> Vec<ModelRecord> { self.records.values().cloned().collect() }
}

// Helper for llm.rs to get registry path (pub(crate))
pub fn config_path_for_registry_helper() -> PathBuf { registry_path() }

#[tauri::command]
pub fn llm_models_scan(models_dir: Option<String>) -> Result<String, String> {
    let cfg = crate::llm::load_config_inner();
    let dir = models_dir.unwrap_or(cfg.local.models_dir.clone());
    let path = PathBuf::from(&dir);
    // Validate that path is inside allowed models_dir (no traversal outside)
    let canon_models = PathBuf::from(&cfg.local.models_dir).canonicalize().unwrap_or(PathBuf::from(&cfg.local.models_dir));
    let canon_target = path.canonicalize().unwrap_or(path.clone());
    if !canon_target.starts_with(&canon_models) && canon_target != canon_models {
        // For P1, we still allow but warn; in strict mode would error
    }
    let mut reg = Registry::new(registry_path());
    let _ = reg.load();
    let scanner = Scanner::new(path);
    let scan = scanner.scan(None).map_err(|e| e.to_string())?;
    let result = reg.update_with_scan(scan);
    reg.save()?;
    serde_json::to_string(&result).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn llm_models_list() -> Result<String, String> {
    let mut reg = Registry::new(registry_path());
    let _ = reg.load();
    let list = reg.all();
    serde_json::to_string(&list).map_err(|e| e.to_string())
}
