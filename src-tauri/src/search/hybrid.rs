use super::types::{
    FallbackPolicy, FallbackReason, HybridResult, HybridSearchConfig, SearchError, SearchMode, SearchResponse,
    SearchResult, SearchSource, TextSearchRequest, VectorModelFilter,
};
use super::vector::VectorStore;
use crate::llm::embeddings::provider::EmbeddingProvider;
use crate::llm::embeddings::types::{EmbeddingRequest, EmbeddingResponse};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use tokio_util::sync::CancellationToken;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RankedId {
    pub chunk_id: String,
    pub rank: usize,
}

pub fn reference_rrf(
    lexical: &[RankedId],
    semantic: &[RankedId],
    config: &HybridSearchConfig,
) -> Vec<(String, f32, Option<usize>, Option<usize>)> {
    let mut scores: HashMap<String, (f32, Option<usize>, Option<usize>)> = HashMap::new();
    for r in lexical {
        let entry = scores.entry(r.chunk_id.clone()).or_insert((0.0, None, None));
        entry.0 += config.lexical_weight * (1.0 / (config.rrf_k as f32 + r.rank as f32 + 1.0));
        entry.1 = Some(r.rank);
    }
    for r in semantic {
        let entry = scores.entry(r.chunk_id.clone()).or_insert((0.0, None, None));
        entry.0 += config.semantic_weight * (1.0 / (config.rrf_k as f32 + r.rank as f32 + 1.0));
        entry.2 = Some(r.rank);
    }
    let mut fused: Vec<(String, f32, Option<usize>, Option<usize>)> = scores
        .into_iter()
        .map(|(id, (score, lr, sr))| (id, score, lr, sr))
        .collect();
    fused.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    fused
}

pub struct HybridSearchService<P: ?Sized, V, L>
where
    P: EmbeddingProvider,
    V: VectorStore,
    L: super::SearchBackend,
{
    embedding_provider: Arc<P>,
    vector_store: Arc<V>,
    lexical: Arc<L>,
}

impl<P: ?Sized, V, L> HybridSearchService<P, V, L>
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

    pub async fn search_hybrid(
        &self,
        req: TextSearchRequest,
        config: HybridSearchConfig,
        cancel: CancellationToken,
    ) -> Result<SearchResponse, SearchError> {
        config.validate()?;
        req.validate()?;
        if cancel.is_cancelled() {
            return Err(SearchError::Cancelled);
        }

        // Preflight: check if semantic index exists to avoid embedding if not
        let has_vectors = self
            .vector_store
            .has_vectors(&req.embedding_model)
            .await
            .unwrap_or(false);

        // Fetch lexical candidates (always)
        let lexical_candidates = {
            if cancel.is_cancelled() {
                return Err(SearchError::Cancelled);
            }
            let q = crate::search::types::SearchQuery {
                text: req.text.clone(),
                limit: config.candidate_limit,
                note_filter: req.note_filter.clone(),
                mode: crate::search::types::SearchQueryMode::Literal,
            };
            // Lexical is not cancellation-aware via token, but we race
            let res = tokio::select! {
                _ = cancel.cancelled() => Err(SearchError::Cancelled),
                r = self.lexical.search(q) => r,
            };
            match res {
                Ok(r) => Ok(r),
                Err(e) => Err(e),
            }
        };

        // Fetch semantic candidates (if has_vectors or we still try)
        let semantic_candidates = {
            if cancel.is_cancelled() {
                return Err(SearchError::Cancelled);
            }
            // If no vectors and we know, skip embedding to save request, but still need to report fallback
            // For hybrid, if has_vectors is false, we consider semantic unavailable
            if !has_vectors {
                Err(SearchError::IndexUnavailable(
                    "no vectors for model".to_string(),
                ))
            } else {
                // Embed single text
                let emb_req = EmbeddingRequest::new(
                    req.embedding_model.model_id.clone(),
                    vec![req.text.clone()],
                );
                let emb_res = tokio::select! {
                    _ = cancel.cancelled() => Err(SearchError::Cancelled),
                    r = self.embedding_provider.embed(emb_req, cancel.clone()) => r.map_err(|e| {
                        if matches!(e, crate::llm::embeddings::types::EmbeddingError::Cancelled) {
                            SearchError::Cancelled
                        } else {
                            SearchError::Internal(format!("embedding: {}", e.code()))
                        }
                    }),
                };
                match emb_res {
                    Ok(resp) => {
                        if resp.vectors.len() != 1 {
                            return Err(SearchError::Internal("expected 1 vector".to_string()));
                        }
                        if resp.model_id != req.embedding_model.model_id {
                            return Err(SearchError::Internal("model mismatch".to_string()));
                        }
                        let vec = resp.vectors.into_iter().next().unwrap();
                        // Validate finite
                        for &v in &vec {
                            if !v.is_finite() {
                                return Err(SearchError::InvalidVector("non-finite".to_string()));
                            }
                        }
                        // Vector search
                        let vq = crate::search::types::VectorQuery {
                            vector: vec,
                            model: req.embedding_model.clone(),
                            limit: config.candidate_limit,
                            note_filter: req.note_filter.clone(),
                            min_score: req.min_score,
                        };
                        let vs_res = tokio::select! {
                            _ = cancel.cancelled() => Err(SearchError::Cancelled),
                            r = self.vector_store.search(vq) => r,
                        };
                        vs_res
                    }
                    Err(e) => Err(e),
                }
            }
        };

        // Decide fallback semantics per table
        let lexical_res = lexical_candidates;
        let semantic_res = semantic_candidates;

        match (lexical_res, semantic_res) {
            (Ok(lex), Ok(sem)) => {
                // Both available -> Hybrid
                self.fuse_results(lex, sem, &req, config).await
            }
            (Ok(lex), Err(sem_err)) => {
                if sem_err.is_cancelled() {
                    return Err(SearchError::Cancelled);
                }
                if is_semantic_fallback_eligible(&sem_err) {
                    // Check if invalid semantic should not fallback: already handled in is_eligible
                    // For hybrid, if semantic unavailable and lexical available, return lexical degraded
                    let reason = map_semantic_err_to_reason(&sem_err);
                    let mut degraded = lex;
                    degraded.mode = SearchMode::Lexical;
                    degraded.degraded = true;
                    degraded.fallback_reason = Some(reason);
                    for r in &mut degraded.results {
                        r.source = SearchSource::Lexical;
                    }
                    degraded.results.truncate(config.result_limit);
                    Ok(degraded)
                } else {
                    // Non-eligible -> error
                    Err(sem_err)
                }
            }
            (Err(lex_err), Ok(sem)) => {
                if lex_err.is_cancelled() {
                    return Err(SearchError::Cancelled);
                }
                // Lexical unavailable, semantic available -> degraded semantic
                if is_lexical_fallback_eligible(&lex_err) {
                    let mut degraded = self.semantic_to_response(sem, &req).await?;
                    degraded.mode = SearchMode::Semantic;
                    degraded.degraded = true;
                    degraded.fallback_reason = Some(FallbackReason::LexicalIndexUnavailable);
                    Ok(degraded)
                } else {
                    Err(lex_err)
                }
            }
            (Err(lex_err), Err(sem_err)) => {
                if lex_err.is_cancelled() || sem_err.is_cancelled() {
                    return Err(SearchError::Cancelled);
                }
                // Both unavailable
                Err(SearchError::Internal(format!(
                    "both backends unavailable: lexical {:?}, semantic {:?}",
                    lex_err, sem_err
                )))
            }
        }
    }

    async fn fuse_results(
        &self,
        lexical: SearchResponse,
        semantic: Vec<crate::search::types::VectorSearchResult>,
        req: &TextSearchRequest,
        config: HybridSearchConfig,
    ) -> Result<SearchResponse, SearchError> {
        // Build ranked ids
        let lexical_ranked: Vec<RankedId> = lexical
            .results
            .iter()
            .enumerate()
            .map(|(rank, r)| RankedId {
                chunk_id: r.chunk_id.clone().unwrap_or_else(|| r.note_id.clone()),
                rank,
            })
            .collect();
        let semantic_ranked: Vec<RankedId> = semantic
            .iter()
            .enumerate()
            .map(|(rank, r)| RankedId {
                chunk_id: r.chunk_id.clone(),
                rank,
            })
            .collect();

        let fused = reference_rrf(&lexical_ranked, &semantic_ranked, &config);

        // Build id -> result maps
        let mut lex_map: HashMap<String, SearchResult> = HashMap::new();
        for r in lexical.results {
            let id = r.chunk_id.clone().unwrap_or_else(|| r.note_id.clone());
            lex_map.insert(id, r);
        }
        let mut sem_map: HashMap<String, crate::search::types::VectorSearchResult> = HashMap::new();
        for r in semantic {
            sem_map.insert(r.chunk_id.clone(), r);
        }

        let mut hybrid_results: Vec<HybridResult> = Vec::new();
        for (chunk_id, fused_score, lex_rank, sem_rank) in fused {
            let base = if let Some(sr) = sem_map.get(&chunk_id) {
                SearchResult {
                    note_id: sr.note_id.clone(),
                    path: Some(sr.note_id.clone()),
                    title: Some(sr.note_id.clone()),
                    content: sr.content.clone(),
                    heading_path: sr.heading_path.clone(),
                    chunk_id: Some(sr.chunk_id.clone()),
                    rank: 0, // will be overwritten after sort
                    raw_score: fused_score,
                    normalized_score: Some(fused_score),
                    start_offset: sr.start_offset,
                    end_offset: sr.end_offset,
                    source: SearchSource::Hybrid,
                }
            } else if let Some(lr) = lex_map.get(&chunk_id) {
                SearchResult {
                    note_id: lr.note_id.clone(),
                    path: lr.path.clone(),
                    title: lr.title.clone(),
                    content: lr.content.clone(),
                    heading_path: lr.heading_path.clone(),
                    chunk_id: lr.chunk_id.clone(),
                    rank: 0,
                    raw_score: fused_score,
                    normalized_score: Some(fused_score),
                    start_offset: lr.start_offset,
                    end_offset: lr.end_offset,
                    source: SearchSource::Hybrid,
                }
            } else {
                continue;
            };
            hybrid_results.push(HybridResult {
                result: base,
                lexical_rank: lex_rank,
                semantic_rank: sem_rank,
                fused_score,
            });
        }

        // Already sorted by fused_score, but need stable sort again (reference already sorted)
        hybrid_results.sort_by(|a, b| {
            b.fused_score
                .partial_cmp(&a.fused_score)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.result.chunk_id.cmp(&b.result.chunk_id))
        });
        hybrid_results.truncate(config.result_limit);

        let mut results: Vec<SearchResult> = Vec::new();
        for (rank, hr) in hybrid_results.into_iter().enumerate() {
            let mut r = hr.result;
            r.rank = rank;
            r.raw_score = hr.fused_score;
            r.normalized_score = Some(hr.fused_score);
            r.source = SearchSource::Hybrid;
            results.push(r);
        }

        // Filter by note_filter already done upstream, but ensure
        Ok(SearchResponse {
            results,
            mode: SearchMode::Hybrid,
            degraded: false,
            fallback_reason: None,
        })
    }

    async fn semantic_to_response(
        &self,
        semantic: Vec<crate::search::types::VectorSearchResult>,
        _req: &TextSearchRequest,
    ) -> Result<SearchResponse, SearchError> {
        let results: Vec<SearchResult> = semantic
            .into_iter()
            .enumerate()
            .map(|(rank, r)| SearchResult {
                note_id: r.note_id,
                path: Some(r.chunk_id.clone()),
                title: None,
                content: r.content,
                heading_path: r.heading_path,
                chunk_id: Some(r.chunk_id),
                rank,
                raw_score: r.score,
                normalized_score: None,
                start_offset: r.start_offset,
                end_offset: r.end_offset,
                source: SearchSource::Semantic,
            })
            .collect();
        Ok(SearchResponse {
            results,
            mode: SearchMode::Semantic,
            degraded: false,
            fallback_reason: None,
        })
    }
}

fn is_semantic_fallback_eligible(err: &SearchError) -> bool {
    match err {
        SearchError::SemanticUnavailable { .. } => true,
        SearchError::IndexUnavailable(_) => true,
        SearchError::Internal(msg) => {
            msg.contains("embedding") || msg.contains("provider") || msg.contains("timeout") || msg.contains("rate")
        }
        _ => false,
    }
}

fn is_lexical_fallback_eligible(err: &SearchError) -> bool {
    matches!(err, SearchError::IndexUnavailable(_) | SearchError::Internal(_))
}

fn map_semantic_err_to_reason(err: &SearchError) -> FallbackReason {
    match err {
        SearchError::SemanticUnavailable { reason, .. } => reason.clone(),
        SearchError::IndexUnavailable(msg) => {
            if msg.to_lowercase().contains("model") {
                FallbackReason::EmbeddingModelMissing
            } else {
                FallbackReason::EmbeddingIndexMissing
            }
        }
        SearchError::Internal(msg) if msg.contains("provider") => {
            FallbackReason::EmbeddingProviderUnavailable
        }
        _ => FallbackReason::EmbeddingRequestFailed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hybrid_config_validate() {
        let mut cfg = HybridSearchConfig::default();
        assert!(cfg.validate().is_ok());
        cfg.lexical_weight = -1.0;
        assert!(cfg.validate().is_err());
        cfg.lexical_weight = 0.0;
        cfg.semantic_weight = 0.0;
        assert!(cfg.validate().is_err());
        cfg.lexical_weight = 0.5;
        cfg.semantic_weight = 0.5;
        cfg.candidate_limit = 5;
        cfg.result_limit = 10;
        assert!(cfg.validate().is_err()); // candidate < result
    }
}
