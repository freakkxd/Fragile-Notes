use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq)]
pub struct EmbeddingLimits {
    pub max_inputs: usize,
    pub max_input_chars: usize,
    pub max_dimensions: usize,
    pub max_vector_bytes: usize,
}

impl Default for EmbeddingLimits {
    fn default() -> Self {
        Self {
            max_inputs: 64,
            max_input_chars: 32_000,
            max_dimensions: 16_384,
            max_vector_bytes: 50 * 1024 * 1024, // 50 MB per response
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum EmbeddingError {
    EmptyModelId,
    EmptyInput,
    BatchTooLarge { max: usize, actual: usize },
    InputTooLarge { index: usize, max: usize, actual: usize },
    EmptyResponse,
    CountMismatch { expected: usize, actual: usize },
    InvalidDimensions { dimensions: usize },
    DimensionMismatch { expected: usize, actual: usize, index: usize },
    NonFiniteValue { index: usize, pos: usize },
    VectorTooLarge { max_bytes: usize, actual_bytes: usize },
    ModelMismatch { expected: String, actual: String },
    MaxDimensionsExceeded { max: usize, actual: usize },
    CorruptVectorBlob(String),
    Unauthorized(String),
    NotFound(String),
    RateLimited(String),
    Timeout(String),
    Server { status: u16, message: String },
    MalformedResponse(String),
    InvalidResponse(String),
    UnsupportedCapability(String),
    Provider(String),
    Cancelled,
}

impl EmbeddingError {
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            Self::RateLimited(_) | Self::Timeout(_) | Self::Server { .. }
        )
    }
    pub fn code(&self) -> &'static str {
        match self {
            Self::EmptyModelId => "empty_model_id",
            Self::EmptyInput => "empty_input",
            Self::BatchTooLarge { .. } => "batch_too_large",
            Self::InputTooLarge { .. } => "input_too_large",
            Self::EmptyResponse => "empty_response",
            Self::CountMismatch { .. } => "count_mismatch",
            Self::InvalidDimensions { .. } => "invalid_dimensions",
            Self::DimensionMismatch { .. } => "dimension_mismatch",
            Self::NonFiniteValue { .. } => "non_finite",
            Self::VectorTooLarge { .. } => "vector_too_large",
            Self::ModelMismatch { .. } => "model_mismatch",
            Self::MaxDimensionsExceeded { .. } => "max_dimensions_exceeded",
            Self::CorruptVectorBlob(_) => "corrupt_vector_blob",
            Self::Unauthorized(_) => "unauthorized",
            Self::NotFound(_) => "not_found",
            Self::RateLimited(_) => "rate_limited",
            Self::Timeout(_) => "timeout",
            Self::Server { .. } => "server_error",
            Self::MalformedResponse(_) => "malformed_response",
            Self::InvalidResponse(_) => "invalid_response",
            Self::UnsupportedCapability(_) => "unsupported_capability",
            Self::Provider(_) => "provider_error",
            Self::Cancelled => "cancelled",
        }
    }
}

impl std::fmt::Display for EmbeddingError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyModelId => write!(f, "model_id is empty"),
            Self::EmptyInput => write!(f, "inputs is empty or contains empty string"),
            Self::BatchTooLarge { max, actual } => write!(f, "batch too large: max {} actual {}", max, actual),
            Self::InputTooLarge { index, max, actual } => write!(f, "input {} too large: max {} chars actual {}", index, max, actual),
            Self::EmptyResponse => write!(f, "response is empty"),
            Self::CountMismatch { expected, actual } => write!(f, "count mismatch: expected {} actual {}", expected, actual),
            Self::InvalidDimensions { dimensions } => write!(f, "invalid dimensions: {}", dimensions),
            Self::DimensionMismatch { expected, actual, index } => write!(f, "dimension mismatch at vector {}: expected {} actual {}", index, expected, actual),
            Self::NonFiniteValue { index, pos } => write!(f, "non-finite value at vector {} pos {}", index, pos),
            Self::VectorTooLarge { max_bytes, actual_bytes } => write!(f, "vector too large: max {} bytes actual {}", max_bytes, actual_bytes),
            Self::ModelMismatch { expected, actual } => write!(f, "model mismatch: expected {} actual {}", expected, actual),
            Self::MaxDimensionsExceeded { max, actual } => write!(f, "max dimensions exceeded: max {} actual {}", max, actual),
            Self::CorruptVectorBlob(reason) => write!(f, "corrupt vector blob: {}", reason),
            Self::Unauthorized(msg) => write!(f, "unauthorized: {}", msg),
            Self::NotFound(msg) => write!(f, "not found: {}", msg),
            Self::RateLimited(msg) => write!(f, "rate limited: {}", msg),
            Self::Timeout(msg) => write!(f, "timeout: {}", msg),
            Self::Server { status, message } => write!(f, "server error {}: {}", status, message),
            Self::MalformedResponse(msg) => write!(f, "malformed response: {}", msg),
            Self::InvalidResponse(msg) => write!(f, "invalid response: {}", msg),
            Self::UnsupportedCapability(cap) => write!(f, "unsupported capability: {}", cap),
            Self::Provider(msg) => write!(f, "provider error: {}", msg),
            Self::Cancelled => write!(f, "cancelled"),
        }
    }
}

impl std::error::Error for EmbeddingError {}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EmbeddingRequest {
    pub model_id: String,
    pub inputs: Vec<String>,
}

impl EmbeddingRequest {
    pub fn new(model_id: String, inputs: Vec<String>) -> Self {
        Self { model_id, inputs }
    }

    pub fn validate(&self, limits: &EmbeddingLimits) -> Result<(), EmbeddingError> {
        if self.model_id.trim().is_empty() {
            return Err(EmbeddingError::EmptyModelId);
        }
        if self.inputs.is_empty() {
            return Err(EmbeddingError::EmptyInput);
        }
        if self.inputs.len() > limits.max_inputs {
            return Err(EmbeddingError::BatchTooLarge { max: limits.max_inputs, actual: self.inputs.len() });
        }
        for (idx, inp) in self.inputs.iter().enumerate() {
            if inp.trim().is_empty() {
                return Err(EmbeddingError::EmptyInput);
            }
            if inp.chars().count() > limits.max_input_chars {
                return Err(EmbeddingError::InputTooLarge { index: idx, max: limits.max_input_chars, actual: inp.chars().count() });
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct EmbeddingResponse {
    pub model_id: String,
    pub dimensions: usize,
    pub vectors: Vec<Vec<f32>>,
}

impl EmbeddingResponse {
    pub fn validate_for(&self, request: &EmbeddingRequest, limits: &EmbeddingLimits) -> Result<(), EmbeddingError> {
        if self.model_id.trim().is_empty() {
            return Err(EmbeddingError::EmptyModelId);
        }
        if self.model_id != request.model_id {
            return Err(EmbeddingError::ModelMismatch { expected: request.model_id.clone(), actual: self.model_id.clone() });
        }
        if self.dimensions == 0 {
            return Err(EmbeddingError::InvalidDimensions { dimensions: 0 });
        }
        if self.dimensions > limits.max_dimensions {
            return Err(EmbeddingError::MaxDimensionsExceeded { max: limits.max_dimensions, actual: self.dimensions });
        }
        if self.vectors.is_empty() {
            return Err(EmbeddingError::EmptyResponse);
        }
        if self.vectors.len() != request.inputs.len() {
            return Err(EmbeddingError::CountMismatch { expected: request.inputs.len(), actual: self.vectors.len() });
        }
        // Check each vector dimensions and finiteness, and total bytes
        let mut total_bytes = 0usize;
        for (idx, vec) in self.vectors.iter().enumerate() {
            if vec.len() != self.dimensions {
                return Err(EmbeddingError::DimensionMismatch { expected: self.dimensions, actual: vec.len(), index: idx });
            }
            for (pos, val) in vec.iter().enumerate() {
                if !val.is_finite() {
                    return Err(EmbeddingError::NonFiniteValue { index: idx, pos });
                }
            }
            total_bytes += vec.len() * std::mem::size_of::<f32>();
        }
        if total_bytes > limits.max_vector_bytes {
            return Err(EmbeddingError::VectorTooLarge { max_bytes: limits.max_vector_bytes, actual_bytes: total_bytes });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct NoteChunk {
    pub id: String,
    pub note_id: String,
    pub content: String,
    pub content_hash: String, // SHA-256 hex lowercase of normalized content
    pub heading_path: Vec<String>,
    pub start_offset: usize,
    pub end_offset: usize,
}

impl NoteChunk {
    pub fn validate(&self) -> Result<(), String> {
        if self.id.trim().is_empty() {
            return Err("chunk id is empty".to_string());
        }
        if self.note_id.trim().is_empty() {
            return Err("note_id is empty".to_string());
        }
        if self.content.trim().is_empty() {
            return Err("content is empty".to_string());
        }
        if self.start_offset > self.end_offset {
            return Err(format!("invalid offsets: start {} > end {}", self.start_offset, self.end_offset));
        }
        if self.content_hash.trim().is_empty() {
            return Err("content_hash is empty".to_string());
        }
        // Verify content_hash matches SHA-256 hex lowercase of normalized content (CRLF -> LF)
        let normalized = self.content.replace("\r\n", "\n");
        let expected = {
            use sha2::{Sha256, Digest};
            let mut hasher = Sha256::new();
            hasher.update(normalized.as_bytes());
            format!("{:x}", hasher.finalize())
        };
        if self.content_hash.to_lowercase() != expected {
            return Err(format!("content_hash mismatch: expected {} got {}", expected, self.content_hash));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct EmbeddingRecord {
    pub chunk_id: String,
    pub model_id: String,
    pub model_fingerprint: String,
    pub content_hash: String,
    pub dimensions: usize,
    pub vector: Vec<f32>,
}

impl EmbeddingRecord {
    pub fn validate(&self, limits: &EmbeddingLimits) -> Result<(), EmbeddingError> {
        if self.chunk_id.trim().is_empty() {
            return Err(EmbeddingError::EmptyInput);
        }
        if self.model_id.trim().is_empty() {
            return Err(EmbeddingError::EmptyModelId);
        }
        if self.content_hash.trim().is_empty() {
            return Err(EmbeddingError::EmptyInput);
        }
        if self.dimensions == 0 {
            return Err(EmbeddingError::InvalidDimensions { dimensions: 0 });
        }
        if self.dimensions > limits.max_dimensions {
            return Err(EmbeddingError::MaxDimensionsExceeded { max: limits.max_dimensions, actual: self.dimensions });
        }
        if self.vector.len() != self.dimensions {
            return Err(EmbeddingError::DimensionMismatch { expected: self.dimensions, actual: self.vector.len(), index: 0 });
        }
        for (pos, v) in self.vector.iter().enumerate() {
            if !v.is_finite() {
                return Err(EmbeddingError::NonFiniteValue { index: 0, pos });
            }
        }
        let bytes = self.vector.len() * std::mem::size_of::<f32>();
        if bytes > limits.max_vector_bytes {
            return Err(EmbeddingError::VectorTooLarge { max_bytes: limits.max_vector_bytes, actual_bytes: bytes });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmbeddingModelRef {
    pub model_id: String,
    pub model_fingerprint: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChunkDiff {
    pub added: Vec<NoteChunk>,
    pub changed: Vec<NoteChunk>,
    pub unchanged: Vec<NoteChunk>,
    pub deleted_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum IndexStatus {
    Pending,
    Running,
    Completed,
    Failed,
    Cancelled,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct IndexRun {
    pub id: String,
    pub note_id: String,
    pub source_hash: String,
    pub status: IndexStatus,
    pub processed_chunks: usize,
    pub total_chunks: usize,
    pub error: Option<String>,
}
