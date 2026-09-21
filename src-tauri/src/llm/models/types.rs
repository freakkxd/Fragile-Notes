use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum ModelFormat { Gguf, Safetensors, Unknown }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum ModelState { Present, Missing, Changed, Invalid, Unverified }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum ModelRole { Chat, Coding, Embedding, Vision, Reranker, Transcription, General }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum MetadataSource { Gguf, Filename, Manual }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum ModelSource { LocalFile, HuggingFace, Manual }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub enum IdentityStatus { #[default] Unchecked, Verified, Changed, Ambiguous, Error }

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ModelMetadata {
    pub architecture: Option<String>,
    pub parameter_count: Option<u64>,
    pub context_length: Option<u32>,
    pub block_count: Option<u32>,
    pub embedding_length: Option<u32>,
    pub quantization: Option<String>,
    pub chat_template: Option<String>,
    pub vision: Option<bool>,
    pub tensor_count: Option<u32>,
    pub metadata_source: MetadataSource,
}

impl Default for MetadataSource { fn default() -> Self { MetadataSource::Gguf } }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelRecord {
    pub id: String, // persisted application identity — created on first discovery, preserved by canonical_path
    pub provider_id: String,
    pub path: std::path::PathBuf,
    #[serde(default)]
    pub canonical_path: String, // canonical absolute path for identity preservation
    pub filename: String,
    pub format: ModelFormat,
    pub size_bytes: u64,
    pub sha256: Option<String>, // lazy verified
    pub metadata: ModelMetadata,
    pub capabilities: Vec<crate::llm::Capability>,
    pub roles: Vec<ModelRole>,
    pub source: ModelSource,
    pub state: ModelState,
    #[serde(default)]
    pub identity_status: IdentityStatus,
    pub first_seen_at: DateTime<Utc>,
    pub last_seen_at: DateTime<Utc>,
    pub diagnostics: Vec<ModelDiagnostic>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelDiagnostic {
    pub code: String,
    pub message: String,
    pub level: String, // error | warn | info
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScanResult {
    pub discovered: Vec<ModelRecord>,
    pub added: Vec<String>,
    pub updated: Vec<String>,
    pub missing: Vec<String>,
    pub invalid: Vec<ModelDiagnostic>,
}
