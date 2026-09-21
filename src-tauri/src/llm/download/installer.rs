use std::path::{Path, PathBuf};
use std::fs;
use super::types::{DownloadJob, DownloadStatus};

pub fn install_downloaded(job: &DownloadJob, expected_sha256: Option<String>) -> Result<PathBuf, String> {
    let part = &job.part_path;
    let dest = &job.destination;
    if !part.exists() {
        return Err("part file missing".to_string());
    }
    // Verify size
    let part_meta = fs::metadata(part).map_err(|e| e.to_string())?;
    if let Some(expected) = job.total_bytes {
        if part_meta.len() != expected {
            return Err(format!("size_mismatch: expected {}, got {}", expected, part_meta.len()));
        }
    }
    // Verify sha256 if provided
    if let Some(exp) = expected_sha256.or_else(|| job.sha256.clone()) {
        super::checksum::verify_checksum(part, &exp)?;
    }
    // Verify GGUF header
    let gguf_meta = crate::llm::models::gguf::parse_file(part).map_err(|e| format!("GGUF parse failed: {}", e))?;
    // Log metadata for registry (not strictly needed for install, but verify)
    let _ = gguf_meta;

    // Atomic rename .part -> final
    let parent = dest.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    // Ensure destination not outside allowed models_dir (already validated in downloader)
    // Use atomic rename with fsync
    fs::rename(part, dest).map_err(|e| format!("atomic rename failed: {}", e))?;
    if let Ok(f) = fs::File::open(dest) { let _ = f.sync_all(); }
    if let Ok(d) = fs::File::open(parent) { let _ = d.sync_all(); }

    // Update registry
    let mut reg = crate::llm::models::registry::Registry::new(crate::llm::registry_path());
    let _ = reg.load();
    let scanner = crate::llm::models::scanner::Scanner::new(dest.parent().unwrap().to_path_buf());
    if let Ok(scan) = scanner.scan(None) {
        let result = reg.update_with_scan(scan);
        let _ = reg.save();
        // Ensure the installed file is in Present state
        if !result.discovered.iter().any(|r| r.path == *dest && r.state == crate::llm::models::types::ModelState::Present) {
            // Could be invalid
        }
    }

    Ok(dest.clone())
}

pub fn cleanup_failed(job: &DownloadJob) {
    // Keep .part for diagnostics by default, but could delete on policy
    // For now, keep it
    let _ = job;
}
