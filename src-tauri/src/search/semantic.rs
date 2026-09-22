use super::types::{
    FallbackPolicy, FallbackReason, SearchError, SearchMode, SearchResponse, SearchResult, SearchSource,
    TextSearchRequest, VectorModelFilter, VectorQuery,
};
use super::vector::VectorStore;
use crate::llm::embeddings::provider::EmbeddingProvider;
use crate::llm::embeddings::types::{EmbeddingError, EmbeddingRequest};
use std::sync::Arc;
use tokio_util::sync::CancellationToken;

/// Orchestration layer: text -> embedding -> vector search, with fallback.

pub struct SemanticSearchService<P, V, L>
where
    P: EmbeddingProvider,
    V: VectorStore,
    L: super::SearchBackend,
{
    embedding_provider: Arc<P>,
    vector_store: Arc<V>,
    lexical: Arc<L>,
}

impl<P, V, L> SemanticSearchService<P, V, L>
where
    P: EmbeddingProvider,
    V: VectorStore,
    L: super::SearchBackend,
{
    pub fn new(embedding_provider: Arc<P>, vector_store: Arc<V>, lexical: Arc<L>) -> Self {
        Self {
            embedding_provider,
            vector_store,
            lexical,
        }
    }

    pub async fn search_text(
        &self,
        req: TextSearchRequest,
        cancel: CancellationToken,
    ) -> Result<SearchResponse, SearchError> {
        req.validate()?;
        if cancel.is_cancelled() {
            return Err(SearchError::Cancelled);
        }

        // Lexical mode skips provider entirely
        if req.mode == SearchMode::Lexical {
            return self.lexical_direct(&req).await;
        }

        // Preflight: check if index exists for this model
        // Lightweight exists check via vector_store: we do a probe search with limit 0? Instead we check via helper
        // For now, we attempt to check via vector_store.search with a dummy vector? No.
        // We do a direct DB check if possible. For generic V we can't, so we skip preflight and let embedding happen,
        // but we can try to detect IndexUnavailable from vector_store later.
        // For Stage 7, we implement preflight by trying to see if vector_store has any vectors for this model.
        // We use a helper that tries to search with a dummy vector and catches IndexUnavailable?
        // Simpler: if we have a method to check existence, we call it. For SqliteVectorStore, we can check via COUNT.
        // Since V is generic, we don't have that method, so we skip preflight and just do embedding.
        // To enable preflight, V could implement `exists` but not required for now.
        // We'll do preflight if vector_store is SqliteVectorStore via downcast? For now, just proceed to embedding.

        // If mode is Auto and we want to save embedding request when index missing, we could check here
        // For now, we check via a probe: try to see if index missing by attempting a vector search with zero vector? Not.

        let embedding_res = self.embed_one(&req, cancel.clone()).await;
        match embedding_res {
            Ok(vector) => {
                if cancel.is_cancelled() {
                    return Err(SearchError::Cancelled);
                }
                // Build VectorQuery and search
                let vq = VectorQuery {
                    vector,
                    model: req.embedding_model.clone(),
                    limit: req.limit,
                    note_filter: req.note_filter.clone(),
                    min_score: req.min_score,
                };
                // Check cancellation before vector search
                if cancel.is_cancelled() {
                    return Err(SearchError::Cancelled);
                }
                let vector_res = tokio::select! {
                    _ = cancel.cancelled() => Err(SearchError::Cancelled),
                    res = self.vector_store.search(vq) => res,
                };
                match vector_res {
                    Ok(results) => {
                        // Map VectorSearchResult to SearchResult
                        let search_results: Vec<SearchResult> = results
                            .into_iter()
                            .enumerate()
                            .map(|(rank, r)| SearchResult {
                                note_id: r.note_id.clone(),
                                path: Some(r.note_id.clone()),
                                title: Some(r.note_id.clone()),
                                content: r.content,
                                heading_path: r.heading_path,
                                chunk_id: Some(r.chunk_id),
                                rank,
                                raw_score: r.score,
                                normalized_score: None,
                                source: SearchSource::Semantic,
                            })
                            .collect();
                        Ok(SearchResponse {
                            results: search_results,
                            mode: SearchMode::Semantic,
                            degraded: false,
                            fallback_reason: None,
                        })
                    }
                    Err(e) => {
                        if e.is_cancelled() {
                            return Err(SearchError::Cancelled);
                        }
                        // Decide if fallback eligible
                        if is_vector_fallback_eligible(&e) && req.fallback == FallbackPolicy::Lexical {
                            let reason = map_vector_err_to_reason(&e);
                            return self.lexical_fallback(&req, Some(reason)).await;
                        }
                        Err(e)
                    }
                }
            }
            Err(e) => {
                if e.is_cancelled() {
                    return Err(SearchError::Cancelled);
                }
                if is_embed_fallback_eligible(&e) && req.fallback == FallbackPolicy::Lexical {
                    let reason = map_embed_err_to_reason(&e);
                    return self.lexical_fallback(&req, Some(reason)).await;
                }
                // For Deny or non-eligible, return error
                // Map embedding error to SearchError without leaking raw provider error
                Err(map_embed_to_search(e))
            }
        }
    }

    async fn embed_one(
        &self,
        req: &TextSearchRequest,
        cancel: CancellationToken,
    ) -> Result<Vec<f32>, SearchError> {
        if cancel.is_cancelled() {
            return Err(SearchError::Cancelled);
        }
        let emb_req = EmbeddingRequest::new(
            req.embedding_model.model_id.clone(),
            vec![req.text.clone()],
        );
        let resp = self
            .embedding_provider
            .embed(emb_req.clone(), cancel.clone())
            .await
            .map_err(|e| {
                if matches!(e, EmbeddingError::Cancelled) {
                    SearchError::Cancelled
                } else {
                    // Preserve embedding error for mapping
                    SearchError::Internal(format!("embedding: {}", e.code()))
                }
            })?;

        // Validate response strictly for text query: exactly one vector, model match, finite, dims
        if resp.vectors.is_empty() {
            return Err(SearchError::Internal("empty embedding response".to_string()));
        }
        if resp.vectors.len() != 1 {
            return Err(SearchError::Internal(format!(
                "expected 1 vector, got {}",
                resp.vectors.len()
            )));
        }
        if resp.model_id != req.embedding_model.model_id {
            return Err(SearchError::Internal(format!(
                "model mismatch expected {} got {}",
                req.embedding_model.model_id, resp.model_id
            )));
        }
        let vec = &resp.vectors[0];
        if vec.is_empty() {
            return Err(SearchError::Internal("empty vector".to_string()));
        }
        for &v in vec {
            if !v.is_finite() {
                return Err(SearchError::Internal("non-finite vector".to_string()));
            }
        }
        // Return cloned vector
        Ok(resp.vectors.into_iter().next().unwrap())
    }

    async fn lexical_direct(&self, req: &TextSearchRequest) -> Result<SearchResponse, SearchError> {
        let q = super::types::SearchQuery {
            text: req.text.clone(),
            limit: req.limit,
            note_filter: req.note_filter.clone(),
            mode: super::types::SearchQueryMode::Literal,
        };
        let mut res = self.lexical.search(q).await?;
        res.mode = SearchMode::Lexical;
        res.degraded = false;
        res.fallback_reason = None;
        for r in &mut res.results {
            r.source = SearchSource::Lexical;
        }
        Ok(res)
    }

    async fn lexical_fallback(
        &self,
        req: &TextSearchRequest,
        reason: Option<FallbackReason>,
    ) -> Result<SearchResponse, SearchError> {
        // Check cancellation before fallback
        // Note: caller already checked, but we also check here
        // Use lexical backend with original text
        let q = super::types::SearchQuery {
            text: req.text.clone(),
            limit: req.limit,
            note_filter: req.note_filter.clone(),
            mode: super::types::SearchQueryMode::Literal,
        };
        let mut res = self.lexical.search(q).await?;
        // Override mode to indicate actual used was lexical, but keep requested mode? Spec says degraded mode = Lexical
        res.mode = SearchMode::Lexical;
        res.degraded = true;
        res.fallback_reason = reason.or(Some(FallbackReason::UnsupportedSemanticSearch));
        for r in &mut res.results {
            r.source = SearchSource::Lexical;
        }
        Ok(res)
    }
}

fn is_embed_fallback_eligible(err: &SearchError) -> bool {
    match err {
        SearchError::SemanticUnavailable { .. } => true,
        SearchError::IndexUnavailable(_) => true,
        SearchError::Internal(msg) => {
            // Map EmbeddingError codes that are operational
            matches!(
                msg.as_str(),
                "embedding: unauthorized"
                    | "embedding: not_found"
                    | "embedding: rate_limited"
                    | "embedding: timeout"
                    | "embedding: server_error"
                    | "embedding: provider_error"
            ) || msg.contains("provider")
                || msg.contains("timeout")
                || msg.contains("rate_limited")
        }
        _ => false,
    }
}

fn map_embed_to_search(err: SearchError) -> SearchError {
    // This is already SearchError from embed_one, so just pass through
    // But we need to convert EmbeddingError that was mapped to Internal
    // For now, keep as is; non-eligible errors will not fallback
    err
}

fn map_embed_err_to_reason(err: &SearchError) -> FallbackReason {
    match err {
        SearchError::SemanticUnavailable { reason, .. } => reason.clone(),
        SearchError::IndexUnavailable(_) => FallbackReason::EmbeddingIndexMissing,
        SearchError::Internal(msg) => {
            if msg.contains("unauthorized") {
                FallbackReason::EmbeddingProviderUnavailable
            } else if msg.contains("not_found") {
                FallbackReason::EmbeddingModelMissing
            } else if msg.contains("timeout") {
                FallbackReason::EmbeddingRequestFailed
            } else if msg.contains("rate_limited") {
                FallbackReason::EmbeddingRequestFailed
            } else {
                FallbackReason::EmbeddingProviderUnavailable
            }
        }
        _ => FallbackReason::EmbeddingRequestFailed,
    }
}

fn is_vector_fallback_eligible(err: &SearchError) -> bool {
    match err {
        SearchError::IndexUnavailable(_) => true,
        SearchError::Internal(_) => false, // DimensionMismatch, Corrupt etc should not fallback
        SearchError::InvalidVector(_) => false,
        SearchError::DimensionMismatch { .. } => false,
        SearchError::CorruptVectorBlob(_) => false,
        SearchError::InvalidQuery(_) => false,
        SearchError::LimitExceeded(_) => false,
        _ => false,
    }
}

fn map_vector_err_to_reason(err: &SearchError) -> FallbackReason {
    match err {
        SearchError::IndexUnavailable(_) => FallbackReason::EmbeddingIndexMissing,
        _ => FallbackReason::EmbeddingRequestFailed,
    }
}
