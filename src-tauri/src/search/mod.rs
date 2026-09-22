pub mod lexical;
pub mod query;
pub mod semantic;
pub mod service;
pub mod types;
pub mod vector;

#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_vector;
#[cfg(test)]
mod tests_query;

pub use lexical::LexicalSearchBackend;
pub use query::TextSearchRequest;
pub use semantic::SemanticSearchService;
pub use service::{SearchService, UnavailableSemanticBackend};
pub use types::{
    FallbackPolicy, FallbackReason, SearchError, SearchMode, SearchQuery, SearchQueryMode, SearchResponse,
    SearchResult, SearchSource, VectorModelFilter, VectorQuery, VectorSearchLimits, VectorSearchResult,
};
pub use vector::{SqliteVectorStore, VectorStore};

use async_trait::async_trait;
use std::path::PathBuf;
use types::{SearchError as SE, SearchQuery as SQ, SearchResponse as SR};

#[async_trait]
pub trait SearchBackend: Send + Sync {
    async fn search(&self, query: SQ) -> Result<SR, SE>;
}

#[tauri::command]
pub async fn search_vector(
    vector: Vec<f32>,
    model_id: String,
    model_fingerprint: String,
    limit: Option<usize>,
    note_filter: Option<String>,
    min_score: Option<f32>,
) -> Result<String, String> {
    use crate::search::types::{VectorModelFilter, VectorQuery};
    use crate::search::vector::{SqliteVectorStore, VectorStore};
    use std::path::PathBuf;

    // Validate size before Rust
    if vector.is_empty() || vector.len() > 16384 {
        return Err("invalid vector dimensions".to_string());
    }
    let lim = limit.unwrap_or(10).clamp(1, 100);
    let model = VectorModelFilter {
        model_id: model_id.clone(),
        model_fingerprint: model_fingerprint.clone(),
    };
    let q = VectorQuery {
        vector,
        model,
        limit: lim,
        note_filter,
        min_score,
    };
    // Reuse same DB as embeddings (vault/.fragile/embeddings.db or fallback)
    let vault = {
        if let Ok(custom) = std::env::var("FRAGILE_VAULT") {
            PathBuf::from(custom)
        } else {
            let home = std::env::var_os("HOME")
                .map(PathBuf::from)
                .or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from))
                .unwrap_or_else(|| PathBuf::from("."));
            home.join("Documents").join("FragileNotesVault")
        }
    };
    let db_path = vault.join(".fragile").join("embeddings.db");
    // If DB doesn't exist, return empty (no index)
    if !db_path.exists() {
        return Ok(serde_json::to_string(&serde_json::json!({
            "results": [],
            "model": model_id,
            "degraded": false
        }))
        .unwrap());
    }
    let store = SqliteVectorStore::new(db_path, crate::search::types::VectorSearchLimits::default());
    let results = store.search(q).await.map_err(|e| e.to_string())?;
    // Hybrid not implemented in Stage 6
    serde_json::to_string(&results).map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn search_hybrid(
    _query: String,
    _limit: Option<usize>,
) -> Result<String, String> {
    Err("hybrid search not implemented in Stage 6 — use search_lexical or search_vector".to_string())
}

#[tauri::command]
pub async fn search_text(
    text: String,
    embedding_model_id: String,
    embedding_model_fingerprint: String,
    limit: Option<usize>,
    note_filter: Option<String>,
    min_score: Option<f32>,
    mode: Option<String>,
    fallback: Option<String>,
) -> Result<String, String> {
    use crate::search::semantic::SemanticSearchService;
    use crate::search::types::{FallbackPolicy, SearchMode, TextSearchRequest, VectorModelFilter};
    use crate::search::{LexicalSearchBackend, SearchBackend};
    use std::path::PathBuf;
    use std::sync::Arc;
    use tokio_util::sync::CancellationToken;

    // Validate no raw secrets
    if text.chars().count() > 2000 {
        return Err("text too long".to_string());
    }
    let lim = limit.unwrap_or(10).clamp(1, 100);
    let smode = match mode.as_deref() {
        Some("lexical") => SearchMode::Lexical,
        Some("semantic") => SearchMode::Semantic,
        Some("hybrid") => SearchMode::Hybrid,
        Some("auto") | None => SearchMode::Auto,
        Some(_) => SearchMode::Auto,
    };
    let fpolicy = match fallback.as_deref() {
        Some("deny") => FallbackPolicy::Deny,
        Some("lexical") | None => FallbackPolicy::Lexical,
        _ => FallbackPolicy::Lexical,
    };
    let req = TextSearchRequest {
        text: text.clone(),
        embedding_model: VectorModelFilter {
            model_id: embedding_model_id.clone(),
            model_fingerprint: embedding_model_fingerprint.clone(),
        },
        limit: lim,
        note_filter: note_filter.clone(),
        min_score,
        mode: smode.clone(),
        fallback: fpolicy.clone(),
    };
    req.validate().map_err(|e| e.to_string())?;

    // If lexical mode, skip embedding
    if smode == SearchMode::Lexical {
        let vault = vault_root_for_search();
        let lexical = Arc::new(LexicalSearchBackend::from_vault(&vault));
        let q = crate::search::types::SearchQuery {
            text,
            limit: lim,
            note_filter,
            mode: crate::search::types::SearchQueryMode::Literal,
        };
        let res = lexical.search(q).await.map_err(|e| e.to_string())?;
        return serde_json::to_string(&res).map_err(|e| e.to_string());
    }

    // For semantic/auto: try to use real embedding provider if available, else fallback
    // For Stage 7, we use a provider that will be unavailable if no sidecar, so fallback demonstrates.
    // Create a dummy provider that returns provider error to trigger lexical fallback when needed.
    struct UnavailableProvider;
    #[async_trait::async_trait]
    impl crate::llm::embeddings::provider::EmbeddingProvider for UnavailableProvider {
        async fn embed(
            &self,
            _request: crate::llm::embeddings::types::EmbeddingRequest,
            _cancel: CancellationToken,
        ) -> Result<crate::llm::embeddings::types::EmbeddingResponse, crate::llm::embeddings::types::EmbeddingError> {
            Err(crate::llm::embeddings::types::EmbeddingError::Provider(
                "embedding provider not configured".to_string(),
            ))
        }
    }

    let vault = vault_root_for_search();
    let lexical = Arc::new(LexicalSearchBackend::from_vault(&vault));
    let db_path = vault.join(".fragile").join("embeddings.db");
    let vector_store = Arc::new(crate::search::vector::SqliteVectorStore::new(
        db_path,
        crate::search::types::VectorSearchLimits::default(),
    ));
    let provider = Arc::new(UnavailableProvider);
    let svc = SemanticSearchService::new(provider, vector_store, lexical);
    let cancel = CancellationToken::new();
    let res = svc.search_text(req, cancel).await.map_err(|e| e.to_string())?;
    serde_json::to_string(&res).map_err(|e| e.to_string())
}

fn vault_root_for_search() -> PathBuf {
    if let Ok(custom) = std::env::var("FRAGILE_VAULT") {
        PathBuf::from(custom)
    } else {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from))
            .unwrap_or_else(|| PathBuf::from("."));
        home.join("Documents").join("FragileNotesVault")
    }
}
