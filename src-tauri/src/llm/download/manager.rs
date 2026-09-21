use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicBool, Ordering};
use uuid::Uuid;
use chrono::Utc;

use super::types::{DownloadJob, DownloadSource, DownloadStatus, DownloadError, DownloadEvent};
use super::downloader::Downloader;
use super::installer::install_downloaded;

pub struct DownloadManager {
    state_path: PathBuf,
    models_dir: PathBuf,
    jobs: Arc<Mutex<HashMap<Uuid, DownloadJob>>>,
    cancels: Arc<Mutex<HashMap<Uuid, Arc<AtomicBool>>>>,
}

impl DownloadManager {
    pub fn new(state_path: PathBuf, models_dir: PathBuf) -> Self {
        let mgr = Self { state_path: state_path.clone(), models_dir, jobs: Arc::new(Mutex::new(HashMap::new())), cancels: Arc::new(Mutex::new(HashMap::new())) };
        // Load existing jobs and handle lifecycle after restart: Downloading/Installing -> Paused
        if state_path.exists() {
            if let Ok(txt) = std::fs::read_to_string(&state_path) {
                if let Ok(list) = serde_json::from_str::<Vec<DownloadJob>>(&txt) {
                    let mut jobs = mgr.jobs.lock().unwrap();
                    for mut job in list {
                        match job.status {
                            DownloadStatus::Downloading | DownloadStatus::Installing => {
                                // If Installing and part exists but final absent, keep Paused for resume
                                if job.part_path.exists() && !job.destination.exists() {
                                    job.status = DownloadStatus::Paused;
                                } else if job.status == DownloadStatus::Installing && !job.part_path.exists() {
                                    job.status = DownloadStatus::Failed;
                                    job.error = Some(super::types::DownloadError{ code: "install_failed".to_string(), message: "Installing but no part file after restart".to_string(), retryable: false });
                                } else {
                                    job.status = DownloadStatus::Paused;
                                }
                                job.updated_at = Utc::now();
                            },
                            _ => {}
                        }
                        jobs.insert(job.id, job);
                    }
                }
            }
        }
        mgr
    }

    fn sanitize_filename(name: &str) -> Result<String, String> {
        if name.contains('/') || name.contains('\\') || name.contains("..") {
            return Err("path_traversal".to_string());
        }
        if name.is_empty() || name.len() > 255 { return Err("invalid filename".to_string()); }
        Ok(name.to_string())
    }

    fn destination_for(&self, filename: &str) -> Result<PathBuf, String> {
        let safe = Self::sanitize_filename(filename)?;
        let dest = self.models_dir.join(safe);
        // Ensure inside models_dir
        let canon_base = self.models_dir.canonicalize().unwrap_or(self.models_dir.clone());
        let canon_dest = dest.parent().unwrap().canonicalize().unwrap_or(dest.parent().unwrap().to_path_buf());
        // Use Path::starts_with component-wise
        if !canon_dest.starts_with(&canon_base) && canon_dest != canon_base {
            return Err("path_traversal: destination outside models_dir".to_string());
        }
        Ok(dest)
    }

    pub fn start(&self, source: DownloadSource) -> Result<Uuid, String> {
        // Harden: reject raw HF token, only allow secret ref
        source.validate_token_ref()?;
        let id = Uuid::new_v4();
        let dest = self.destination_for(&source.filename)?;
        let part = dest.with_extension("gguf.part");
        // Check disk space
        let needed = source.expected_size.unwrap_or(1024*1024*100);
        check_disk_space(dest.parent().unwrap_or(Path::new(".")), needed)?;
        if dest.exists() {
            return Err("already exists".to_string());
        }
        let job = DownloadJob{
            id,
            source: source.clone(),
            destination: dest.clone(),
            part_path: part.clone(),
            bytes_downloaded: 0,
            total_bytes: source.expected_size,
            status: DownloadStatus::Queued,
            sha256: source.sha256.clone(),
            error: None,
            created_at: Utc::now(),
            updated_at: Utc::now(),
        };
        {
            let mut jobs = self.jobs.lock().unwrap();
            jobs.insert(id, job);
        }
        // Spawn download task
        let jobs_clone = Arc::clone(&self.jobs);
        let cancels_clone = Arc::clone(&self.cancels);
        let cancel_flag = Arc::new(AtomicBool::new(false));
        {
            let mut cancels = self.cancels.lock().unwrap();
            cancels.insert(id, Arc::clone(&cancel_flag));
        }
        let state_path = self.state_path.clone();
        tokio::spawn(async move {
            // Update status to Downloading
            {
                let mut jobs = jobs_clone.lock().unwrap();
                if let Some(j) = jobs.get_mut(&id) { j.status = DownloadStatus::Downloading; j.updated_at = Utc::now(); }
            }
            let downloader = Downloader::new().unwrap();
            let mut job_clone = {
                let jobs = jobs_clone.lock().unwrap();
                jobs.get(&id).cloned().unwrap()
            };
            let res = downloader.download(&mut job_clone, cancel_flag.clone(), |ev| {
                // Throttled progress already in downloader, here we just update job
                let mut jobs = jobs_clone.lock().unwrap();
                if let Some(j) = jobs.get_mut(&id) {
                    // If pause was requested, downloader keeps checking cancel flag and will exit next chunk.
                    // Do not overwrite Paused status with progress.
                    if j.status == DownloadStatus::Paused || j.status == DownloadStatus::Cancelled { return; }
                    j.bytes_downloaded = ev.downloaded;
                    j.total_bytes = ev.total;
                    j.updated_at = Utc::now();
                }
            }).await;
            {
                let mut jobs = jobs_clone.lock().unwrap();
                if let Some(j) = jobs.get_mut(&id) {
                    // If user paused/cancelled during download, job already has Paused/Cancelled — keep it, do not overwrite with Failed/Cancelled.
                    // downloader returns Err("cancelled") on flag; we preserve the earlier user-initiated state and leave .part for resume.
                    if j.status == DownloadStatus::Paused || j.status == DownloadStatus::Cancelled {
                        j.updated_at = Utc::now();
                    } else {
                    match res {
                        Ok(_) => {
                            j.status = DownloadStatus::Verifying;
                            j.updated_at = Utc::now();
                            // Verify and install
                            let install_res = install_downloaded(&*j, j.sha256.clone());
                            match install_res {
                                Ok(final_path) => {
                                    j.status = DownloadStatus::Completed;
                                    j.destination = final_path;
                                    // Remove part (already renamed)
                                },
                                Err(e) => {
                                    j.status = DownloadStatus::Failed;
                                    j.error = Some(DownloadError{ code: if e.contains("checksum") {"checksum_mismatch".to_string()} else if e.contains("size") {"size_mismatch".to_string()} else {"install_failed".to_string()}, message: e, retryable: false });
                                }
                            }
                        },
                        Err(e) if e == "cancelled" => {
                            // Flag set but status not yet Paused/Cancelled — treat as Paused (preserve .part) if job was Downloading
                            j.status = DownloadStatus::Paused;
                            j.error = Some(DownloadError{ code: "paused".to_string(), message: e, retryable: true });
                        },
                        Err(e) => {
                            j.status = DownloadStatus::Failed;
                            j.error = Some(DownloadError{ code: "network".to_string(), message: e, retryable: true });
                        }
                    }
                    j.updated_at = Utc::now();
                    }
                }
            }
            // Persist state
            let _ = save_jobs(&state_path, &jobs_clone);
            // Cleanup cancels
            let mut cancels = cancels_clone.lock().unwrap();
            cancels.remove(&id);
        });
        Ok(id)
    }

     pub fn pause(&self, id: Uuid) -> Result<(), String> {
        let flag = { self.cancels.lock().unwrap().get(&id).cloned().ok_or("job not found")? };
        flag.store(true, Ordering::Relaxed);
        {
            let mut jobs = self.jobs.lock().unwrap();
            if let Some(job) = jobs.get_mut(&id) {
                // Only pause if currently Downloading/Queued; do not overwrite Completed/Failed
                if job.status == DownloadStatus::Downloading || job.status == DownloadStatus::Queued || job.status == DownloadStatus::Verifying || job.status == DownloadStatus::Installing {
                    job.status = DownloadStatus::Paused;
                    job.updated_at = Utc::now();
                    job.error = Some(DownloadError{ code: "paused".to_string(), message: "paused by user".to_string(), retryable: true });
                }
            }
        }
        // Persist paused state immediately so UI shows pause even if bytes_stream still draining
        let _ = save_jobs(&self.state_path, &self.jobs);
        Ok(())
    }

    pub fn resume(&self, id: Uuid) -> Result<(), String> {
        // For P1, resume is same as start with Range: we just restart download (it will resume via .part)
        let job = self.jobs.lock().unwrap().get(&id).cloned().ok_or("job not found")?;
        if job.status != DownloadStatus::Paused && job.status != DownloadStatus::Failed {
            return Err("can only resume Paused/Failed".to_string());
        }
        let cancel_flag = Arc::new(AtomicBool::new(false));
        {
            let mut cancels = self.cancels.lock().unwrap();
            cancels.insert(id, Arc::clone(&cancel_flag));
        }
        {
            let mut jobs = self.jobs.lock().unwrap();
            if let Some(j) = jobs.get_mut(&id) { j.status = DownloadStatus::Queued; }
        }
        let jobs_clone = Arc::clone(&self.jobs);
        let cancels_clone = Arc::clone(&self.cancels);
        let state_path = self.state_path.clone();
        tokio::spawn(async move {
            let downloader = Downloader::new().unwrap();
            let mut job_clone = {
                let jobs = jobs_clone.lock().unwrap();
                jobs.get(&id).cloned().unwrap()
            };
            let res = downloader.download(&mut job_clone, cancel_flag.clone(), |ev| {
                let mut jobs = jobs_clone.lock().unwrap();
                if let Some(j) = jobs.get_mut(&id) {
                    if j.status == DownloadStatus::Paused || j.status == DownloadStatus::Cancelled { return; }
                    j.bytes_downloaded = ev.downloaded;
                    j.total_bytes = ev.total;
                    j.updated_at = Utc::now();
                }
            }).await;
            {
                let mut jobs = jobs_clone.lock().unwrap();
                if let Some(j) = jobs.get_mut(&id) {
                    if j.status == DownloadStatus::Paused || j.status == DownloadStatus::Cancelled {
                        j.updated_at = Utc::now();
                    } else {
                    match res {
                        Ok(_) => { j.status = DownloadStatus::Verifying; let install_res = install_downloaded(&*j, j.sha256.clone()); match install_res {
                            Ok(p) => { j.status = DownloadStatus::Completed; j.destination = p; },
                            Err(e) => { j.status = DownloadStatus::Failed; j.error = Some(DownloadError{ code: "install_failed".to_string(), message: e, retryable: false}); }
                        }},
                        Err(e) if e=="cancelled" => {
                            j.status = DownloadStatus::Paused;
                            j.error = Some(DownloadError{ code: "paused".to_string(), message: e, retryable: true });
                        },
                        Err(e) => { j.status = DownloadStatus::Failed; j.error = Some(DownloadError{ code: "network".to_string(), message: e, retryable: true}); }
                    }
                    j.updated_at = Utc::now();
                    }
                }
            }
            let _ = save_jobs(&state_path, &jobs_clone);
            let mut cancels = cancels_clone.lock().unwrap(); cancels.remove(&id);
        });
        Ok(())
    }

    pub fn cancel(&self, id: Uuid) -> Result<(), String> {
        let flag = { self.cancels.lock().unwrap().get(&id).cloned().ok_or("job not found")? };
        flag.store(true, Ordering::Relaxed);
        {
            let mut jobs = self.jobs.lock().unwrap();
            if let Some(job) = jobs.get_mut(&id) {
                job.status = DownloadStatus::Cancelled;
                job.updated_at = Utc::now();
                job.error = Some(DownloadError{ code: "cancelled".to_string(), message: "cancelled by user".to_string(), retryable: false });
            }
        }
        let _ = save_jobs(&self.state_path, &self.jobs);
        Ok(())
    }

    pub fn status(&self, id: Uuid) -> Result<DownloadJob, String> {
        let jobs = self.jobs.lock().unwrap();
        jobs.get(&id).cloned().map(|j| j.sanitized_for_api()).ok_or("job not found".to_string())
    }

    pub fn list(&self) -> Vec<DownloadJob> {
        let jobs = self.jobs.lock().unwrap();
        jobs.values().cloned().map(|j| j.sanitized_for_api()).collect()
    }

    /// Internal: raw status without masking (for installer/worker only)
    pub fn status_raw(&self, id: Uuid) -> Result<DownloadJob, String> {
        let jobs = self.jobs.lock().unwrap();
        jobs.get(&id).cloned().ok_or("job not found".to_string())
    }
}

fn save_jobs(path: &Path, jobs: &Arc<Mutex<HashMap<Uuid, DownloadJob>>>) -> Result<(), String> {
    let jobs = jobs.lock().unwrap();
    // Persist sanitized: ensure raw token never hits disk (validate already, but double-mask)
    let list: Vec<DownloadJob> = jobs.values().map(|j| {
        let mut sanitized = j.clone();
        // Keep token_ref only if it's a valid keychain ref; otherwise strip
        if let Some(r) = &j.source.token_ref {
            if !r.starts_with("keychain://") {
                sanitized.source.token_ref = None;
            }
        }
        sanitized
    }).collect();
    let txt = serde_json::to_string_pretty(&list).map_err(|e| e.to_string())?;
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, txt).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, path).map_err(|e| e.to_string())?;
    Ok(())
}

fn check_disk_space(dir: &Path, needed: u64) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::ffi::CString;
        let c_path = CString::new(dir.to_string_lossy().as_bytes()).map_err(|e| e.to_string())?;
        let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
        let ret = unsafe { libc::statvfs(c_path.as_ptr(), &mut stat as *mut _) };
        if ret == 0 {
            let avail = stat.f_bavail as u64 * stat.f_frsize as u64;
            if avail < needed + 1024*1024 {
                return Err(format!("insufficient disk space: need {}, avail {}", needed, avail));
            }
        }
    }
    Ok(())
}

use once_cell::sync::Lazy;
static GLOBAL: Lazy<Arc<DownloadManager>> = Lazy::new(|| {
    let cfg = crate::llm::load_config_inner();
    let state_path = crate::llm::config_path().with_file_name("downloads.json");
    let models_dir = PathBuf::from(cfg.local.models_dir);
    Arc::new(DownloadManager::new(state_path, models_dir))
});
pub fn global() -> Arc<DownloadManager> { GLOBAL.clone() }

#[tauri::command]
pub fn llm_download_start(source: DownloadSource) -> Result<String, String> {
    let mgr = global();
    let id = mgr.start(source)?;
    Ok(id.to_string())
}
#[tauri::command]
pub fn llm_download_pause(job_id: String) -> Result<String, String> {
    let mgr = global();
    let id = uuid::Uuid::parse_str(&job_id).map_err(|e| e.to_string())?;
    mgr.pause(id)?;
    Ok("paused".to_string())
}
#[tauri::command]
pub fn llm_download_resume(job_id: String) -> Result<String, String> {
    let mgr = global();
    let id = uuid::Uuid::parse_str(&job_id).map_err(|e| e.to_string())?;
    mgr.resume(id)?;
    Ok("resumed".to_string())
}
#[tauri::command]
pub fn llm_download_cancel(job_id: String) -> Result<String, String> {
    let mgr = global();
    let id = uuid::Uuid::parse_str(&job_id).map_err(|e| e.to_string())?;
    mgr.cancel(id)?;
    Ok("cancelled".to_string())
}
#[tauri::command]
pub fn llm_download_status(job_id: String) -> Result<String, String> {
    let mgr = global();
    let id = uuid::Uuid::parse_str(&job_id).map_err(|e| e.to_string())?;
    let job = mgr.status(id)?;
    serde_json::to_string(&job).map_err(|e| e.to_string())
}
#[tauri::command]
pub fn llm_download_list() -> Result<String, String> {
    let mgr = global();
    let list = mgr.list();
    serde_json::to_string(&list).map_err(|e| e.to_string())
}
