pub mod hybrid;
pub mod lexical;
pub mod query;
pub mod semantic;
pub mod service;
pub mod types;
pub mod vector;

#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_hybrid;
#[cfg(test)]
mod tests_query;
#[cfg(test)]
mod tests_vector;

pub use hybrid::{HybridSearchService, RankedId, reference_rrf};
pub use lexical::LexicalSearchBackend;
pub use semantic::SemanticSearchService;
pub use service::{SearchService, UnavailableSemanticBackend};
pub use types::{
    FallbackPolicy, FallbackReason, HybridResult, HybridSearchConfig, SearchError, SearchMode, SearchQuery,
    SearchQueryMode, SearchResponse, SearchResult, SearchSource, TextSearchRequest, VectorModelFilter,
    VectorQuery, VectorSearchLimits, VectorSearchResult,
};
pub use vector::{SqliteVectorStore, VectorStore};

use async_trait::async_trait;
use std::path::PathBuf;
use types::{SearchError as SE, SearchQuery as SQ, SearchResponse as SR};

/// Try to resolve a production embedding provider from the current config.
///
/// Returns the provider plus the AUTHORITATIVE model filter (application
/// `model_id` + resolved fingerprint). Callers must use the returned filter
/// for retrieval so `has_vectors` / vector search hit the index written
/// under the installed model's fingerprint — not a stale frontend value.
///
/// Returns `None` when no configured model matches (command keeps the
/// `UnavailableProvider` path with its typed degraded fallback).
/// Privacy note: Tauri search commands carry no task context, so resolution
/// passes `privacy = None`. Explicit local-only enforcement happens in flows
/// that own a task profile (10B chat/RAG) via `validate_task_policy`.
pub(crate) fn production_embedding_provider(
    model_id: &str,
) -> Option<(
    std::sync::Arc<dyn crate::llm::embeddings::provider::EmbeddingProvider>,
    types::VectorModelFilter,
)> {
    use crate::llm::embeddings::wiring;
    let cfg = crate::llm::load_config_inner();
    let model = cfg.models.iter().find(|m| m.id == model_id)?;
    // Best-effort registry record for fingerprint (missing -> fallback rules).
    let record = (|| {
        let mut reg = crate::llm::models::registry::Registry::new(crate::llm::registry_path());
        reg.load().ok()?;
        wiring::find_record_for_model(&reg, model)
    })();
    let resolved = wiring::resolve_embedding_provider(
        &cfg,
        model_id,
        record.as_ref(),
        &None,
        &crate::llm::get_keyring,
    )
    .ok()?;
    let filter = types::VectorModelFilter {
        model_id: resolved.model_ref.model_id,
        model_fingerprint: resolved.model_ref.model_fingerprint,
    };
    let provider: std::sync::Arc<dyn crate::llm::embeddings::provider::EmbeddingProvider> =
        std::sync::Arc::new(resolved.provider);
    Some((provider, filter))
}

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
    text: String,
    embedding_model_id: String,
    embedding_model_fingerprint: String,
    limit: Option<usize>,
    candidate_limit: Option<usize>,
    lexical_weight: Option<f32>,
    semantic_weight: Option<f32>,
    rrf_k: Option<usize>,
    note_filter: Option<String>,
    min_score: Option<f32>,
) -> Result<String, String> {
    use crate::search::hybrid::HybridSearchService;
    use crate::search::types::{HybridSearchConfig, TextSearchRequest, VectorModelFilter, FallbackPolicy, SearchMode};
    use std::sync::Arc;
    use tokio_util::sync::CancellationToken;

    let lim = limit.unwrap_or(10).clamp(1, 100);
    let cand = candidate_limit.unwrap_or(20).clamp(lim, 100);
    let lw = lexical_weight.unwrap_or(0.5);
    let sw = semantic_weight.unwrap_or(0.5);
    let k = rrf_k.unwrap_or(60);
    let cfg = HybridSearchConfig {
        lexical_weight: lw,
        semantic_weight: sw,
        candidate_limit: cand,
        result_limit: lim,
        rrf_k: k,
    };
    cfg.validate().map_err(|e| e.to_string())?;
    let mut req = TextSearchRequest {
        text: text.clone(),
        embedding_model: VectorModelFilter {
            model_id: embedding_model_id.clone(),
            model_fingerprint: embedding_model_fingerprint.clone(),
        },
        limit: lim,
        note_filter: note_filter.clone(),
        min_score,
        mode: SearchMode::Hybrid,
        fallback: FallbackPolicy::Lexical,
    };
    req.validate().map_err(|e| e.to_string())?;

    let vault = vault_root_for_search();
    let lexical = Arc::new(crate::search::LexicalSearchBackend::from_vault(&vault));
    let db_path = vault.join(".fragile").join("embeddings.db");
    let vector_store = Arc::new(crate::search::vector::SqliteVectorStore::new(
        db_path,
        crate::search::types::VectorSearchLimits::default(),
    ));
    // Production provider when configured; otherwise the unavailable stub
    // (typed error -> existing degraded lexical fallback).
    struct UnavailableProvider;
    #[async_trait::async_trait]
    impl crate::llm::embeddings::provider::EmbeddingProvider for UnavailableProvider {
        async fn embed(
            &self,
            _request: crate::llm::embeddings::types::EmbeddingRequest,
            _cancel: CancellationToken,
        ) -> Result<crate::llm::embeddings::types::EmbeddingResponse, crate::llm::embeddings::types::EmbeddingError> {
            Err(crate::llm::embeddings::types::EmbeddingError::Provider(
                "hybrid provider not configured".to_string(),
            ))
        }
    }
    let provider = Arc::new(UnavailableProvider);
    let hybrid_provider: Arc<dyn crate::llm::embeddings::provider::EmbeddingProvider> =
        match production_embedding_provider(&embedding_model_id) {
            Some((p, filter)) => {
                req.embedding_model = filter;
                p
            }
            None => provider,
        };
    let svc = HybridSearchService::new(hybrid_provider, vector_store, lexical);
    let cancel = CancellationToken::new();
    let res = svc
        .search_hybrid(req, cfg, cancel)
        .await
        .map_err(|e| e.to_string())?;
    serde_json::to_string(&res).map_err(|e| e.to_string())
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
    let mut req = TextSearchRequest {
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

    // For semantic/auto: production provider when configured, else the
    // unavailable stub (typed error -> existing degraded lexical fallback).
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
    let text_provider: Arc<dyn crate::llm::embeddings::provider::EmbeddingProvider> =
        match production_embedding_provider(&embedding_model_id) {
            Some((p, filter)) => {
                req.embedding_model = filter;
                p
            }
            None => provider,
        };
    let svc = SemanticSearchService::new(text_provider, vector_store, lexical);
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
