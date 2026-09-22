pub mod context;
pub mod retrieval;
pub mod types;
pub mod untrusted;

#[cfg(test)]
mod tests;

pub use context::ContextBuilder;
pub use retrieval::{RagRetriever, RagSearchBackend};
pub use types::{ContextLimits, RagContext, RagReference, RagRequest, RagRetrievalResult};
pub use untrusted::{serialize_untrusted_context, untrusted_blocks, UntrustedBlock, UntrustedContext};

/// Demo Tauri flow: lexical-only retrieval.
///
/// STAGE 9 NOTE (do not misrepresent as production E2E):
/// the live [`crate::llm::embeddings::openai_compatible::OpenAiCompatibleEmbeddingProvider`]
/// is NOT wired into this demo command. Retrieval here runs through a
/// lexical adapter only (`SearchQueryMode::Literal`), the `embedding_model`
/// filter is accepted for API shape compatibility and validated, but no
/// embedding request is issued. Semantic/hybrid paths are covered by
/// `RagRetriever` unit tests via fake [`RagSearchBackend`] implementations
/// and by the already-verified hybrid search service (Stage 8).
/// Wiring the real provider into the production search flow is a separate
/// step (pre-Stage-10) and requires explicit secret handling review.
#[tauri::command]
pub async fn rag_retrieve(
    query: String,
    embedding_model_id: String,
    embedding_model_fingerprint: String,
    limit: Option<usize>,
    max_chunks: Option<usize>,
    max_chars: Option<usize>,
    max_chars_per_chunk: Option<usize>,
    note_filter: Option<String>,
    include_sources: Option<bool>,
) -> Result<String, String> {
    use crate::rag::retrieval::RagSearchBackend;
    use crate::search::types::{FallbackPolicy, SearchMode, TextSearchRequest, VectorModelFilter};
    use std::sync::Arc;

    // Validate search limits strictly — never clamp-mask invalid input.
    // `limit` defaults when omitted; out-of-range values are rejected.
    let lim = limit.unwrap_or(10);
    let req = TextSearchRequest {
        text: query.clone(),
        embedding_model: VectorModelFilter {
            model_id: embedding_model_id,
            model_fingerprint: embedding_model_fingerprint,
        },
        limit: lim,
        note_filter: note_filter.clone(),
        min_score: None,
        mode: SearchMode::Auto,
        fallback: FallbackPolicy::Lexical,
    };
    req.validate().map_err(|e| e.to_string())?;

    // Validate context limits strictly — never clamp-mask invalid input.
    let ctx_limits = ContextLimits {
        max_chunks: max_chunks.unwrap_or(10),
        max_chars: max_chars.unwrap_or(8000),
        max_chars_per_chunk: max_chars_per_chunk.unwrap_or(2000),
    };
    ctx_limits.validate().map_err(|e| e.to_string())?;

    let rag_req = RagRequest {
        search: req,
        context: ctx_limits,
        include_sources: include_sources.unwrap_or(true),
    };

    let vault = {
        if let Ok(custom) = std::env::var("FRAGILE_VAULT") {
            std::path::PathBuf::from(custom)
        } else {
            let home = std::env::var_os("HOME")
                .map(std::path::PathBuf::from)
                .or_else(|| std::env::var_os("USERPROFILE").map(std::path::PathBuf::from))
                .unwrap_or_else(|| std::path::PathBuf::from("."));
            home.join("Documents").join("FragileNotesVault")
        }
    };
    let lexical = Arc::new(crate::search::LexicalSearchBackend::from_vault(&vault));
    // Lexical-only adapter for the demo flow (see note above).
    struct LexicalRagAdapter {
        backend: Arc<dyn crate::search::SearchBackend>,
    }
    #[async_trait::async_trait]
    impl RagSearchBackend for LexicalRagAdapter {
        async fn search(
            &self,
            req: TextSearchRequest,
            cancel: tokio_util::sync::CancellationToken,
        ) -> Result<crate::search::types::SearchResponse, crate::search::types::SearchError> {
            if cancel.is_cancelled() {
                return Err(crate::search::types::SearchError::Cancelled);
            }
            let q = crate::search::types::SearchQuery {
                text: req.text.clone(),
                limit: req.limit,
                note_filter: req.note_filter.clone(),
                mode: crate::search::types::SearchQueryMode::Literal,
            };
            tokio::select! {
                _ = cancel.cancelled() => Err(crate::search::types::SearchError::Cancelled),
                r = self.backend.search(q) => r,
            }
        }
    }
    let adapter = Arc::new(LexicalRagAdapter { backend: lexical });
    let retriever = RagRetriever::new(adapter);
    let cancel = tokio_util::sync::CancellationToken::new();
    let res = retriever
        .retrieve(rag_req, cancel)
        .await
        .map_err(|e| e.to_string())?;
    serde_json::to_string(&res).map_err(|e| e.to_string())
}
