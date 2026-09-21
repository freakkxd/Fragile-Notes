use std::path::PathBuf;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum DownloadStatus { Queued, Downloading, Paused, Verifying, Installing, Completed, Cancelled, Failed }

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DownloadSource {
    pub url: String,
    pub filename: String,
    pub expected_size: Option<u64>,
    pub sha256: Option<String>,
    #[serde(default)]
    pub repo_id: Option<String>,
    #[serde(default)]
    pub revision: Option<String>,
    #[serde(default)]
    pub token_ref: Option<String>, // keychain://fragile-notes/... — secret reference, never raw token
}

impl DownloadSource {
    /// Validate that token_ref, if present, is a secret reference, not raw token.
    pub fn validate_token_ref(&self) -> Result<(), String> {
        if let Some(r) = &self.token_ref {
            if r.is_empty() { return Ok(()); }
            // Must be keychain:// reference; reject raw tokens like hf_... or sk-...
            if !r.starts_with("keychain://") {
                // Heuristic: raw HF tokens start with hf_ and are long, but any non-keychain is rejected
                return Err("HuggingFace token must be secret_ref keychain://..., raw token not allowed".to_string());
            }
            if r.contains('\n') || r.contains(' ') || r.len() > 256 {
                return Err("invalid token_ref".to_string());
            }
        }
        Ok(())
    }
    /// Return sanitized clone for persistence/API: never expose raw token, mask ref for external API.
    pub fn sanitized_for_persist(&self) -> Self {
        // Persist only keychain ref, not raw — validate already ensures it's a ref
        self.clone()
    }
    pub fn sanitized_for_api(&self) -> Self {
        let mut s = self.clone();
        // Never return token_ref to frontend/status — mask or hide
        if s.token_ref.is_some() {
            s.token_ref = Some("masked:••••••••".to_string());
        }
        s
    }
}

impl DownloadJob {
    pub fn sanitized_for_api(&self) -> Self {
        let mut j = self.clone();
        j.source = j.source.sanitized_for_api();
        j
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DownloadJob {
    pub id: Uuid,
    pub source: DownloadSource,
    pub destination: PathBuf,
    pub part_path: PathBuf,
    pub bytes_downloaded: u64,
    pub total_bytes: Option<u64>,
    pub status: DownloadStatus,
    pub sha256: Option<String>,
    pub error: Option<DownloadError>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DownloadError {
    pub code: String, // network, checksum_mismatch, size_mismatch, invalid_redirect, https_policy, path_traversal, max_size, disk_space, cancelled
    pub message: String,
    pub retryable: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DownloadEvent {
    pub job_id: Uuid,
    pub status: DownloadStatus,
    pub downloaded: u64,
    pub total: Option<u64>,
    pub speed_bytes_per_sec: Option<u64>,
    pub eta_seconds: Option<u64>,
    pub error: Option<DownloadError>,
}
