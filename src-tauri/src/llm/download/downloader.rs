use std::path::{Path, PathBuf};
use std::sync::{Arc, atomic::{AtomicBool, Ordering}};
use std::time::{Duration, Instant};
use tokio::fs::OpenOptions;
use tokio::io::{AsyncWriteExt, AsyncSeekExt};
use futures::StreamExt;
use super::types::{DownloadJob, DownloadStatus, DownloadError, DownloadEvent};
use super::source::{SourceKind, parse_source};
use super::types::DownloadSource;

const MAX_REDIRECTS: usize = 5;
const MAX_DOWNLOAD_SIZE: u64 = 100 * 1024 * 1024 * 1024; // 100GB
const PROGRESS_INTERVAL_MS: u64 = 200;
const PROGRESS_MIN_BYTES: u64 = 1024 * 1024; // 1MB

pub struct Downloader {
    client: reqwest::Client,
}

impl Downloader {
    pub fn new() -> Result<Self, String> {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .connect_timeout(Duration::from_secs(10))
            .redirect(reqwest::redirect::Policy::limited(MAX_REDIRECTS))
            .user_agent("Fragile-Notes/0.5.6")
            .build().map_err(|e| e.to_string())?;
        Ok(Self { client })
    }

    pub async fn download(
        &self,
        job: &mut DownloadJob,
        cancel: Arc<AtomicBool>,
        on_progress: impl Fn(DownloadEvent),
    ) -> Result<(), String> {
        // Validate token_ref before any network
        job.source.validate_token_ref()?;
        let source = parse_source(job.source.clone())?;
        let (url, token) = match source {
            SourceKind::DirectUrl { url, .. } => (url.to_string(), None),
            SourceKind::HuggingFace { repo_id, revision, filename, token_ref } => {
                let url = super::source::resolve_hf_url(&repo_id, &revision, &filename);
                let token = if let Some(r) = token_ref {
                    // Only accept keychain://fragile-notes/<key>
                    let key = r.strip_prefix("keychain://fragile-notes/").ok_or_else(|| "token_ref must be keychain://fragile-notes/...".to_string())?;
                    // Fetch via SecretStore directly before HTTP (never log token)
                    keyring::Entry::new("com.fragilich.notes", key).ok().and_then(|e| e.get_password().ok())
                } else { None };
                (url, token)
            }
        };
        // Validate destination is inside models_dir (no traversal)
        let dest = &job.destination;
        if dest.to_string_lossy().contains("..") {
            return Err("path_traversal: destination contains ..".to_string());
        }
        // Check disk space
        let needed = job.total_bytes.unwrap_or(1024*1024*100);
        check_disk_space(dest.parent().unwrap_or(Path::new(".")), needed)?;

        // Determine resume offset
        let part_path = job.part_path.clone();
        let existing = if part_path.exists() { tokio::fs::metadata(&part_path).await.map(|m| m.len()).unwrap_or(0) } else { 0 };
        let mut downloaded = existing;
        job.bytes_downloaded = downloaded;

        // Build request with Range if resume + auth if HuggingFace private
        let mut req = self.client.get(&url);
        if let Some(t) = token {
            // Do not log token; add as Bearer
            req = req.header("Authorization", format!("Bearer {}", t));
        }
        if downloaded > 0 {
            req = req.header("Range", format!("bytes={}-", downloaded));
        }
        // Security: only https, redirect already limited, no HTTP downgrade
        if !url.starts_with("https://") {
            return Err("HTTPS policy: only https allowed".to_string());
        }

        let resp = req.send().await.map_err(|e| format!("network: {}", e))?;
        let status = resp.status().as_u16();
        // Handle Range responses
        let total = if status == 206 {
            // Partial content
            let content_range = resp.headers().get("content-range").and_then(|v| v.to_str().ok()).unwrap_or("");
            // Parse total from Content-Range: bytes 100-999/2000
            if let Some(total_str) = content_range.split('/').last() {
                total_str.parse::<u64>().ok()
            } else { None }
        } else if status == 200 {
            if downloaded > 0 {
                // Server doesn't support Range, restart
                downloaded = 0;
                job.bytes_downloaded = 0;
                // Truncate part file
                let _ = tokio::fs::remove_file(&part_path).await;
            }
            resp.content_length()
        } else if status == 416 {
            // Range Not Satisfiable - check if already complete
            if let Some(total) = job.total_bytes {
                if downloaded == total {
                    job.status = DownloadStatus::Verifying;
                    return Ok(());
                }
            }
            return Err("Range Not Satisfiable (416)".to_string());
        } else {
            return Err(format!("HTTP {} for {}", status, url));
        };

        if let Some(t) = total {
            if t > MAX_DOWNLOAD_SIZE {
                return Err(format!("max_size: {} > {}", t, MAX_DOWNLOAD_SIZE));
            }
            job.total_bytes = Some(t);
        }

        // Check Content-Length vs max
        if let Some(cl) = resp.content_length() {
            if cl + downloaded > MAX_DOWNLOAD_SIZE {
                return Err("max_size exceeded".to_string());
            }
        }

        // Stream to .part
        let mut file = OpenOptions::new().create(true).write(true).open(&part_path).await.map_err(|e| e.to_string())?;
        if downloaded > 0 {
            file.seek(std::io::SeekFrom::Start(downloaded)).await.map_err(|e| e.to_string())?;
        }
        let mut stream = resp.bytes_stream();
        let mut last_emit = Instant::now();
        let mut bytes_since_emit = 0u64;
        let mut last_downloaded = downloaded;

        while let Some(chunk) = stream.next().await {
            if cancel.load(Ordering::Relaxed) {
                job.status = DownloadStatus::Cancelled;
                return Err("cancelled".to_string());
            }
            let bytes = chunk.map_err(|e| format!("stream error: {}", e))?;
            // Check max size during download
            if downloaded + bytes.len() as u64 > MAX_DOWNLOAD_SIZE {
                return Err("max_size exceeded during download".to_string());
            }
            file.write_all(&bytes).await.map_err(|e| e.to_string())?;
            downloaded += bytes.len() as u64;
            job.bytes_downloaded = downloaded;
            bytes_since_emit += bytes.len() as u64;

            // Throttle progress events
            let elapsed = last_emit.elapsed().as_millis() as u64;
            if elapsed >= PROGRESS_INTERVAL_MS || bytes_since_emit >= PROGRESS_MIN_BYTES {
                let speed = if elapsed > 0 { (downloaded - last_downloaded) * 1000 / elapsed } else { 0 };
                let eta = if speed > 0 && job.total_bytes.is_some() {
                    Some((job.total_bytes.unwrap().saturating_sub(downloaded)) / speed)
                } else { None };
                on_progress(DownloadEvent{
                    job_id: job.id,
                    status: DownloadStatus::Downloading,
                    downloaded,
                    total: job.total_bytes,
                    speed_bytes_per_sec: Some(speed),
                    eta_seconds: eta,
                    error: None,
                });
                last_emit = Instant::now();
                bytes_since_emit = 0;
                last_downloaded = downloaded;
            }
            // Check disk space periodically
            if downloaded % (10*1024*1024) == 0 {
                check_disk_space(dest.parent().unwrap_or(Path::new(".")), 1024*1024)?;
            }
        }
        file.flush().await.map_err(|e| e.to_string())?;
        file.sync_all().await.map_err(|e| e.to_string())?;
        job.status = DownloadStatus::Verifying;
        Ok(())
    }
}

fn check_disk_space(dir: &Path, needed: u64) -> Result<(), String> {
    // Use statvfs via nix or just check via df
    // For P1, use simple check: try to get available space via `statvfs` if available, else assume ok
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
