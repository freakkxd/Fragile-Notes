use crate::search::types::{FallbackReason, SearchMode, SearchResponse, TextSearchRequest};
use serde::{Deserialize, Serialize};

/// Bounded context limits. Validated BEFORE any allocation.
///
/// Units:
/// - `max_chunks`: count of context blocks, `1..=100`.
/// - `max_chars`: total budget in **Unicode scalar values** (`char` count),
///   `1..=1_000_000`. NOT bytes — safe for CJK/emoji.
/// - `max_chars_per_chunk`: per-chunk preview budget, same unit,
///   `1..=max_chars`.
///
/// Offsets (`start_offset`/`end_offset` in references) are separate:
/// **UTF-8 byte offsets** into the original full chunk content.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextLimits {
    pub max_chunks: usize,
    pub max_chars: usize,
    pub max_chars_per_chunk: usize,
}

impl Default for ContextLimits {
    fn default() -> Self {
        Self {
            max_chunks: 10,
            max_chars: 8000,
            max_chars_per_chunk: 2000,
        }
    }
}

impl ContextLimits {
    pub fn validate(&self) -> Result<(), String> {
        if self.max_chunks == 0 || self.max_chunks > 100 {
            return Err("max_chunks must be 1..100".to_string());
        }
        if self.max_chars == 0 || self.max_chars > 1_000_000 {
            return Err("max_chars must be 1..1000000".to_string());
        }
        if self.max_chars_per_chunk == 0 || self.max_chars_per_chunk > self.max_chars {
            return Err("max_chars_per_chunk must be 1..max_chars".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct RagRequest {
    pub search: TextSearchRequest,
    pub context: ContextLimits,
    pub include_sources: bool,
}

impl RagRequest {
    pub fn validate(&self) -> Result<(), String> {
        self.search.validate().map_err(|e| e.to_string())?;
        self.context.validate()?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagReference {
    pub chunk_id: String,
    pub note_id: String,
    pub path: Option<String>,
    pub heading_path: Vec<String>,
    pub start_offset: usize,
    pub end_offset: usize,
    pub score: Option<f32>,
    pub source: crate::search::types::SearchSource,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagContext {
    pub text: String,
    pub references: Vec<RagReference>,
    pub truncated: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RagRetrievalResult {
    pub context: RagContext,
    pub search: SearchResponse,
    pub degraded: bool,
    pub fallback_reason: Option<FallbackReason>,
    pub search_mode: SearchMode,
}
