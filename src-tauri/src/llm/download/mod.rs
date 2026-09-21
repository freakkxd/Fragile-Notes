pub mod types;
pub mod source;
pub mod downloader;
pub mod checksum;
pub mod installer;
pub mod manager;
#[cfg(test)]
mod tests;

pub use types::{DownloadJob, DownloadStatus, DownloadError, DownloadEvent, DownloadSource};
pub use downloader::Downloader;
pub use source::{SourceKind, parse_source};
pub use manager::{DownloadManager, llm_download_start, llm_download_pause, llm_download_resume, llm_download_cancel, llm_download_status, llm_download_list};
