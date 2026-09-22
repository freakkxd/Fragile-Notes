use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum SearchQueryMode {
    Literal,
    Advanced,
}

impl Default for SearchQueryMode {
    fn default() -> Self {
        Self::Literal
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SearchQuery {
    pub text: String,
    pub limit: usize,
    pub note_filter: Option<String>,
    pub mode: SearchQueryMode,
}

impl SearchQuery {
    pub fn new(text: String, limit: usize) -> Self {
        Self {
            text,
            limit,
            note_filter: None,
            mode: SearchQueryMode::Literal,
        }
    }

    pub fn with_filter(mut self, filter: Option<String>) -> Self {
        self.note_filter = filter;
        self
    }

    pub fn with_mode(mut self, mode: SearchQueryMode) -> Self {
        self.mode = mode;
        self
    }

    pub fn validate(&self) -> Result<(), SearchError> {
        if self.limit == 0 {
            return Err(SearchError::InvalidQuery("limit must be > 0".to_string()));
        }
        if self.limit > 100 {
            return Err(SearchError::InvalidQuery(
                "limit must be <= 100".to_string(),
            ));
        }
        if self.text.trim().is_empty() {
            return Err(SearchError::InvalidQuery("query is empty".to_string()));
        }
        if self.text.chars().count() > 1000 {
            return Err(SearchError::InvalidQuery(
                "query too long (max 1000 chars)".to_string(),
            ));
        }
        if let Some(f) = &self.note_filter {
            if f.contains("..") || f.contains('\0') {
                return Err(SearchError::InvalidQuery("invalid note_filter".to_string()));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SearchResult {
    pub note_id: String,
    /// path relative to vault, e.g. "notes/foo.md"
    pub path: Option<String>,
    pub title: Option<String>,
    pub content: String,
    pub heading_path: Vec<String>,
    pub chunk_id: Option<String>,
    /// rank 0 is best, higher is worse (ORDER BY bm25)
    pub rank: usize,
    /// raw bm25 score (negative in SQLite, more negative = more relevant)
    pub raw_score: f32,
    /// normalized 0..1 if available (not promised), for now None
    pub normalized_score: Option<f32>,
    pub source: SearchSource,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum SearchSource {
    Lexical,
    Semantic,
    Hybrid,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum SearchMode {
    Lexical,
    Semantic,
    Hybrid,
    Auto,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum FallbackPolicy {
    Deny,
    Lexical,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum FallbackReason {
    EmbeddingProviderUnavailable,
    EmbeddingModelMissing,
    EmbeddingIndexMissing,
    EmbeddingRequestFailed,
    UnsupportedSemanticSearch,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResponse {
    pub results: Vec<SearchResult>,
    pub mode: SearchMode,
    pub degraded: bool,
    pub fallback_reason: Option<FallbackReason>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum SearchError {
    InvalidQuery(String),
    IndexUnavailable(String),
    Internal(String),
    SemanticUnavailable { reason: FallbackReason, message: String },
    InvalidVector(String),
    DimensionMismatch { expected: usize, actual: usize },
    CorruptVectorBlob(String),
    LimitExceeded(String),
    Cancelled,
}

impl SearchError {
    pub fn is_cancelled(&self) -> bool {
        matches!(self, Self::Cancelled)
    }
    pub fn is_fallback_eligible(&self) -> bool {
        matches!(
            self,
            Self::SemanticUnavailable { .. } | Self::IndexUnavailable(_)
        )
    }
}

impl std::fmt::Display for SearchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidQuery(msg) => write!(f, "invalid query: {}", msg),
            Self::IndexUnavailable(msg) => write!(f, "index unavailable: {}", msg),
            Self::Internal(msg) => write!(f, "internal: {}", msg),
            Self::SemanticUnavailable { reason, message } => {
                write!(f, "semantic unavailable {:?}: {}", reason, message)
            }
            Self::InvalidVector(msg) => write!(f, "invalid vector: {}", msg),
            Self::DimensionMismatch { expected, actual } => {
                write!(f, "dimension mismatch: expected {} actual {}", expected, actual)
            }
            Self::CorruptVectorBlob(msg) => write!(f, "corrupt vector blob: {}", msg),
            Self::LimitExceeded(msg) => write!(f, "limit exceeded: {}", msg),
            Self::Cancelled => write!(f, "cancelled"),
        }
    }
}

impl std::error::Error for SearchError {}

// Text query orchestration (Stage 7)

#[derive(Debug, Clone, PartialEq)]
pub struct TextSearchRequest {
    pub text: String,
    pub embedding_model: VectorModelFilter,
    pub limit: usize,
    pub note_filter: Option<String>,
    pub min_score: Option<f32>,
    pub mode: SearchMode,
    pub fallback: FallbackPolicy,
}

impl TextSearchRequest {
    pub fn validate(&self) -> Result<(), SearchError> {
        if self.text.trim().is_empty() {
            return Err(SearchError::InvalidQuery("text is empty".to_string()));
        }
        if self.text.chars().count() > 2000 {
            return Err(SearchError::InvalidQuery("text too long".to_string()));
        }
        if self.limit == 0 || self.limit > 100 {
            return Err(SearchError::InvalidQuery("limit must be 1..100".to_string()));
        }
        if self.embedding_model.model_id.trim().is_empty() {
            return Err(SearchError::InvalidQuery("model_id is empty".to_string()));
        }
        if self.embedding_model.model_fingerprint.trim().is_empty() {
            return Err(SearchError::InvalidQuery(
                "model_fingerprint is empty".to_string(),
            ));
        }
        if let Some(f) = &self.note_filter {
            if f.contains("..") || f.contains('\0') {
                return Err(SearchError::InvalidQuery("invalid note_filter".to_string()));
            }
        }
        if let Some(min) = self.min_score {
            if !min.is_finite() || min < -1.0 || min > 1.0 {
                return Err(SearchError::InvalidQuery(
                    "min_score must be in [-1,1]".to_string(),
                ));
            }
        }
        Ok(())
    }
}

// Vector search types (Stage 6)

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VectorModelFilter {
    pub model_id: String,
    pub model_fingerprint: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct VectorQuery {
    pub vector: Vec<f32>,
    pub model: VectorModelFilter,
    pub limit: usize,
    pub note_filter: Option<String>,
    pub min_score: Option<f32>,
}

impl VectorQuery {
    pub fn validate(&self) -> Result<(), SearchError> {
        if self.vector.is_empty() {
            return Err(SearchError::InvalidVector("query vector is empty".to_string()));
        }
        if self.limit == 0 || self.limit > 100 {
            return Err(SearchError::InvalidQuery(
                "limit must be 1..100".to_string(),
            ));
        }
        if self.model.model_id.trim().is_empty() {
            return Err(SearchError::InvalidQuery("model_id is empty".to_string()));
        }
        if self.model.model_fingerprint.trim().is_empty() {
            return Err(SearchError::InvalidQuery(
                "model_fingerprint is empty".to_string(),
            ));
        }
        for (i, v) in self.vector.iter().enumerate() {
            if !v.is_finite() {
                return Err(SearchError::InvalidVector(format!(
                    "non-finite at pos {}",
                    i
                )));
            }
        }
        let norm: f32 = self.vector.iter().map(|x| x * x).sum::<f32>().sqrt();
        if norm == 0.0 || !norm.is_finite() {
            return Err(SearchError::InvalidVector("zero-norm vector".to_string()));
        }
        if let Some(f) = &self.note_filter {
            if f.contains("..") || f.contains('\0') {
                return Err(SearchError::InvalidQuery("invalid note_filter".to_string()));
            }
        }
        if let Some(min) = self.min_score {
            if !min.is_finite() || min < -1.0 || min > 1.0 {
                return Err(SearchError::InvalidQuery(
                    "min_score must be finite in [-1,1]".to_string(),
                ));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VectorSearchResult {
    pub chunk_id: String,
    pub note_id: String,
    pub content: String,
    pub heading_path: Vec<String>,
    pub score: f32,
    pub distance: f32,
    pub model: VectorModelFilter,
}

#[derive(Debug, Clone)]
pub struct VectorSearchLimits {
    pub max_candidates: usize,
    pub max_vector_bytes: usize,
}

impl Default for VectorSearchLimits {
    fn default() -> Self {
        Self {
            max_candidates: 10000,
            max_vector_bytes: 50 * 1024 * 1024,
        }
    }
}
