use std::path::PathBuf;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum DownloadStatus { Queued, Downloading, Paused, Verifying, Installing, Completed, Cancelled, Failed }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DownloadSource {
    pub url: String,
    pub filename: String,
    pub expected_size: Option<u64>,
    pub sha256: Option<String>,
    pub repo_id: Option<String>,
    pub revision: Option<String>,
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
