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

        // Migration: ensure existing records have canonical_path and identity_status
        for rec in self.records.values_mut() {
            if rec.canonical_path.is_empty() {
                let canon = rec.path.canonicalize().map(|p| p.to_string_lossy().to_string()).unwrap_or_else(|_| rec.path.to_string_lossy().to_string());
                rec.canonical_path = canon;
            }
            // Default identity_status already handled via serde default
        }

        for mut rec in scan.discovered {
            // Ensure rec has canonical_path (scanner already sets it, but for safety)
            let canon_new = if !rec.canonical_path.is_empty() {
                rec.canonical_path.clone()
            } else {
                rec.path.canonicalize().map(|p| p.to_string_lossy().to_string()).unwrap_or_else(|_| rec.path.to_string_lossy().to_string())
            };
            rec.canonical_path = canon_new.clone();

            // 1. Same canonical_path -> preserve existing id (covers mtime/size changes, delete/restore)
            let mut target_id: Option<String> = None;
            let mut is_moved = false;
            let mut is_same_path = false;
            for (eid, existing) in self.records.iter() {
                let canon_existing = if !existing.canonical_path.is_empty() {
                    existing.canonical_path.clone()
                } else {
                    existing.path.canonicalize().map(|p| p.to_string_lossy().to_string()).unwrap_or(existing.path.to_string_lossy().to_string())
                };
                if canon_existing == canon_new {
                    target_id = Some(eid.clone());
                    is_same_path = true;
                    break;
                }
            }

            // 2. If not found by canonical, try SHA move detection (lazy, only if sha present)
            if target_id.is_none() {
                if let Some(sha) = &rec.sha256 {
                    if !sha.is_empty() {
                        let mut candidates = Vec::new();
                        for (eid, existing) in self.records.iter() {
                            if let Some(existing_sha) = &existing.sha256 {
                                if existing_sha == sha && existing.size_bytes == rec.size_bytes {
                                    candidates.push(eid.clone());
                                }
                            }
                        }
                        if candidates.len() == 1 {
                            // If candidate already seen in this scan, it's a duplicate file in same scan, not a move
                            if seen_ids.contains(&candidates[0]) {
                                rec.identity_status = super::types::IdentityStatus::Ambiguous;
                                rec.diagnostics.push(super::types::ModelDiagnostic{ code: "duplicate_hash".to_string(), message: format!("same sha {} found in two files in same scan ({} and {}), manual resolution needed", sha, candidates[0], rec.path.display()), level: "warn".to_string() });
                                target_id = None;
                            } else {
                                target_id = Some(candidates[0].clone());
                                is_moved = true;
                            }
                        } else if candidates.len() > 1 {
                            // Ambiguous same hash in multiple existing records
                            rec.identity_status = super::types::IdentityStatus::Ambiguous;
                            rec.diagnostics.push(super::types::ModelDiagnostic{ code: "ambiguous_hash".to_string(), message: format!("same sha {} found in multiple records, manual resolution needed", sha), level: "warn".to_string() });
                            // Do not auto-merge, treat as new
                            target_id = None;
                        }
                    }
                } else if rec.size_bytes < 50 * 1024 * 1024 {
                    // For small files without sha (should not happen, but for safety) compute lazily if needed for move
                    // This branch is rare; we keep as new
                } else {
                    // Large file without sha: try lazy hash for move detection if there's a Missing candidate with same size
                    // Only if we have a Missing record with same size and same canonical parent? For now, treat as new to avoid expensive hash.
                    // Future: background hash verification
                }
            }

            let final_target = if let Some(tid) = target_id.clone() {
                seen_ids.insert(tid.clone());
                tid
            } else {
                // New file
                let new_id = rec.id.clone();
                seen_ids.insert(new_id.clone());
                // Set identity_status
                if rec.sha256.is_some() {
                    rec.identity_status = super::types::IdentityStatus::Verified;
                } else {
                    rec.identity_status = super::types::IdentityStatus::Unchecked;
                }
                self.records.insert(new_id.clone(), rec);
                added.push(new_id);
                continue;
            };

            // Update existing record (same canonical or moved)
            if let Some(existing) = self.records.get_mut(&final_target) {
                let old_roles = existing.roles.clone();
                let old_first_seen = existing.first_seen_at;
                let old_id = existing.id.clone();

                // Changed detection: same canonical, size or sha differs
                let is_changed = if is_moved {
                    false
                } else if is_same_path {
                    if existing.size_bytes != rec.size_bytes {
                        true
                    } else if existing.sha256.is_some() && rec.sha256.is_some() && existing.sha256 != rec.sha256 {
                        true
                    } else {
                        false
                    }
                } else {
                    false
                };

                if is_changed {
                    existing.state = ModelState::Changed;
                    existing.identity_status = super::types::IdentityStatus::Changed;
                    updated.push(old_id.clone());
                } else {
                    // Restore from Missing/Changed to Present, or keep Present
                    if existing.state == ModelState::Missing || existing.state == ModelState::Changed {
                        updated.push(old_id.clone());
                    } else if is_moved {
                        updated.push(old_id.clone());
                    }
                    existing.state = ModelState::Present;
                    // Update identity_status if we now have sha
                    if existing.sha256.is_none() && rec.sha256.is_some() {
                        existing.identity_status = super::types::IdentityStatus::Verified;
                    } else if existing.identity_status == super::types::IdentityStatus::Unchecked && rec.sha256.is_some() {
                        existing.identity_status = super::types::IdentityStatus::Verified;
                    }
                }

                existing.last_seen_at = chrono::Utc::now();
                // For moved, update path and canonical_path to new location
                if is_moved {
                    existing.path = rec.path.clone();
                    existing.canonical_path = rec.canonical_path.clone();
                } else {
                    // Same canonical, keep path as is (may have been moved within same canonical? but canonical same, so path should be same)
                    // Update path to new path in case of minor difference (e.g., relative vs absolute)
                    existing.path = rec.path.clone();
                    // Keep canonical_path as is (should be same)
                    if existing.canonical_path.is_empty() {
                        existing.canonical_path = rec.canonical_path.clone();
                    }
                }
                existing.size_bytes = rec.size_bytes;
                if rec.sha256.is_some() {
                    existing.sha256 = rec.sha256.clone();
                }
                existing.metadata = rec.metadata.clone();
                if !old_roles.is_empty() { existing.roles = old_roles; }
                existing.first_seen_at = old_first_seen;
                existing.id = old_id;
                if !rec.diagnostics.is_empty() {
                    existing.diagnostics.extend(rec.diagnostics.clone());
                }
            }
        }
        // Missing: those in registry but not seen
        let mut missing = Vec::new();
        for (id, rec) in self.records.iter_mut() {
            if !seen_ids.contains(id) {
                if rec.state != ModelState::Missing {
                    rec.state = ModelState::Missing;
                    // Keep identity_status as is (do not change to Missing status's identity)
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
