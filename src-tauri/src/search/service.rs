use super::types::*;
use super::SearchBackend;
use async_trait::async_trait;
use std::sync::Arc;

pub struct SearchService {
    lexical: Arc<dyn SearchBackend>,
    semantic: Option<Arc<dyn SearchBackend>>,
    fallback: FallbackPolicy,
}

impl SearchService {
    pub fn new(
        lexical: Arc<dyn SearchBackend>,
        semantic: Option<Arc<dyn SearchBackend>>,
        fallback: FallbackPolicy,
    ) -> Self {
        Self {
            lexical,
            semantic,
            fallback,
        }
    }

    pub fn with_lexical(lexical: Arc<dyn SearchBackend>) -> Self {
        Self {
            lexical,
            semantic: None,
            fallback: FallbackPolicy::Lexical,
        }
    }

    pub async fn search(
        &self,
        query: SearchQuery,
        mode: SearchMode,
    ) -> Result<SearchResponse, SearchError> {
        query.validate()?;
        match mode {
            SearchMode::Lexical => self.lexical_search(query).await,
            SearchMode::Semantic => self.semantic_search(query).await,
            SearchMode::Hybrid => Err(SearchError::SemanticUnavailable {
                reason: FallbackReason::UnsupportedSemanticSearch,
                message: "hybrid search not implemented in Stage 6".to_string(),
            }),
            SearchMode::Auto => self.auto_search(query).await,
        }
    }

    async fn lexical_search(&self, query: SearchQuery) -> Result<SearchResponse, SearchError> {
        let mut res = self.lexical.search(query).await?;
        res.mode = SearchMode::Lexical;
        res.degraded = false;
        res.fallback_reason = None;
        // Ensure source is Lexical
        for r in &mut res.results {
            r.source = SearchSource::Lexical;
        }
        Ok(res)
    }

    async fn semantic_search(&self, query: SearchQuery) -> Result<SearchResponse, SearchError> {
        if let Some(sem) = &self.semantic {
            match sem.search(query.clone()).await {
                Ok(mut res) => {
                    res.mode = SearchMode::Semantic;
                    res.degraded = false;
                    res.fallback_reason = None;
                    for r in &mut res.results {
                        r.source = SearchSource::Semantic;
                    }
                    Ok(res)
                }
                Err(e) => match &self.fallback {
                    FallbackPolicy::Deny => {
                        // Normalize to SemanticUnavailable
                        let msg = e.to_string();
                        Err(SearchError::SemanticUnavailable {
                            reason: FallbackReason::UnsupportedSemanticSearch,
                            message: msg,
                        })
                    }
                    FallbackPolicy::Lexical => {
                        let reason = map_semantic_err_to_reason(&e);
                        let mut lex = self.lexical.search(query).await?;
                        lex.mode = SearchMode::Semantic;
                        lex.degraded = true;
                        lex.fallback_reason = Some(reason);
                        for r in &mut lex.results {
                            r.source = SearchSource::Lexical;
                        }
                        Ok(lex)
                    }
                },
            }
        } else {
            // No semantic backend
            match &self.fallback {
                FallbackPolicy::Deny => Err(SearchError::SemanticUnavailable {
                    reason: FallbackReason::UnsupportedSemanticSearch,
                    message: "semantic backend not configured".to_string(),
                }),
                FallbackPolicy::Lexical => {
                    let mut lex = self.lexical.search(query).await?;
                    lex.mode = SearchMode::Semantic;
                    lex.degraded = true;
                    lex.fallback_reason = Some(FallbackReason::UnsupportedSemanticSearch);
                    for r in &mut lex.results {
                        r.source = SearchSource::Lexical;
                    }
                    Ok(lex)
                }
            }
        }
    }

    async fn auto_search(&self, query: SearchQuery) -> Result<SearchResponse, SearchError> {
        // Try semantic if available, else lexical degraded
        if self.semantic.is_some() {
            // Attempt semantic with fallback
            let res = self.semantic_search(query.clone()).await;
            match res {
                Ok(r) => Ok(r),
                Err(SearchError::SemanticUnavailable { .. }) => {
                    // Already handled fallback in semantic_search if Lexical, but if Deny we get error
                    // For Auto, we always fallback to lexical if semantic fails
                    let mut lex = self.lexical.search(query).await?;
                    lex.mode = SearchMode::Auto;
                    lex.degraded = true;
                    lex.fallback_reason = Some(FallbackReason::UnsupportedSemanticSearch);
                    for r in &mut lex.results {
                        r.source = SearchSource::Lexical;
                    }
                    Ok(lex)
                }
                Err(e) => Err(e),
            }
        } else {
            let mut lex = self.lexical.search(query).await?;
            // Auto with no semantic -> degraded lexical
            // If lexical has results, degraded=true indicates fallback
            // If we want to distinguish, we set degraded true
            lex.mode = SearchMode::Auto;
            // Only mark degraded if semantic was expected but missing
            // For now, mark degraded true to indicate not semantic
            lex.degraded = true;
            lex.fallback_reason = Some(FallbackReason::UnsupportedSemanticSearch);
            for r in &mut lex.results {
                r.source = SearchSource::Lexical;
            }
            Ok(lex)
        }
    }
}

fn map_semantic_err_to_reason(err: &SearchError) -> FallbackReason {
    match err {
        SearchError::SemanticUnavailable { reason, .. } => reason.clone(),
        SearchError::IndexUnavailable(msg) => {
            if msg.to_lowercase().contains("model") {
                FallbackReason::EmbeddingModelMissing
            } else if msg.to_lowercase().contains("index") {
                FallbackReason::EmbeddingIndexMissing
            } else {
                FallbackReason::EmbeddingRequestFailed
            }
        }
        SearchError::Internal(msg) => {
            if msg.to_lowercase().contains("provider") {
                FallbackReason::EmbeddingProviderUnavailable
            } else {
                FallbackReason::EmbeddingRequestFailed
            }
        }
        _ => FallbackReason::EmbeddingRequestFailed,
    }
}

/// Backend that always fails — used for Stage 5 to simulate unavailable semantic search
pub struct UnavailableSemanticBackend {
    reason: FallbackReason,
    message: String,
}

impl UnavailableSemanticBackend {
    pub fn new(reason: FallbackReason, message: String) -> Self {
        Self { reason, message }
    }
}

#[async_trait]
impl SearchBackend for UnavailableSemanticBackend {
    async fn search(&self, _query: SearchQuery) -> Result<SearchResponse, SearchError> {
        Err(SearchError::SemanticUnavailable {
            reason: self.reason.clone(),
            message: self.message.clone(),
        })
    }
}
