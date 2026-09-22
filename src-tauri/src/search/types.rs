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
    Auto,
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
        }
    }
}

impl std::error::Error for SearchError {}
