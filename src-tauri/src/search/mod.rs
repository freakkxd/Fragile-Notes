pub mod lexical;
pub mod service;
pub mod types;
pub mod vector;

#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_vector;

pub use lexical::LexicalSearchBackend;
pub use service::{FallbackPolicy, SearchService, UnavailableSemanticBackend};
pub use types::{
    FallbackReason, SearchError, SearchMode, SearchQuery, SearchQueryMode, SearchResponse, SearchResult,
    SearchSource, VectorModelFilter, VectorQuery, VectorSearchLimits, VectorSearchResult,
};
pub use vector::{SqliteVectorStore, VectorStore};

use async_trait::async_trait;
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
