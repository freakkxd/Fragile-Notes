use super::context::ContextBuilder;
use super::types::{RagRequest, RagRetrievalResult};
use crate::search::types::SearchError;
use std::sync::Arc;
use tokio_util::sync::CancellationToken;

#[async_trait::async_trait]
pub trait RagSearchBackend: Send + Sync {
    async fn search(
        &self,
        req: crate::search::types::TextSearchRequest,
        cancel: CancellationToken,
    ) -> Result<crate::search::types::SearchResponse, SearchError>;
}

/// RagRetriever — applies RAG policy on top of search.
/// No LLM generation, no prompt assembly beyond bounded context.

pub struct RagRetriever<S>
where
    S: RagSearchBackend,
{
    search_backend: Arc<S>,
}

impl<S> RagRetriever<S>
where
    S: RagSearchBackend,
{
    pub fn new(search_backend: Arc<S>) -> Self {
        Self { search_backend }
    }

    pub async fn retrieve(
        &self,
        req: RagRequest,
        cancel: CancellationToken,
    ) -> Result<RagRetrievalResult, SearchError> {
        req.validate().map_err(|e| SearchError::InvalidQuery(e))?;
        if cancel.is_cancelled() {
            return Err(SearchError::Cancelled);
        }

        let search_req = req.search.clone();
        let limits = req.context.clone();
        let include_sources = req.include_sources;

        // Perform search via backend (which handles mode/fallback)
        let search_res = tokio::select! {
            _ = cancel.cancelled() => return Err(SearchError::Cancelled),
            r = self.search_backend.search(search_req, cancel.clone()) => r,
        };

        match search_res {
            Ok(search) => {
                if cancel.is_cancelled() {
                    return Err(SearchError::Cancelled);
                }
                // Build bounded context
                let ctx = if include_sources {
                    ContextBuilder::build(&search.results, &limits)
                } else {
                    super::types::RagContext {
                        text: String::new(),
                        references: vec![],
                        truncated: false,
                    }
                };
                let degraded = search.degraded;
                let fallback_reason = search.fallback_reason.clone();
                let mode = search.mode.clone();
                Ok(RagRetrievalResult {
                    context: ctx,
                    search: search.clone(),
                    degraded,
                    fallback_reason,
                    search_mode: mode,
                })
            }
            Err(e) => {
                if e.is_cancelled() {
                    return Err(SearchError::Cancelled);
                }
                // Do not produce partial context on error (CorruptVectorBlob, Cancelled, etc.)
                Err(e)
            }
        }
    }
}
