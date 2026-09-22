pub mod answer;
pub mod context;
pub mod prompt;
pub mod retrieval;
pub mod types;
pub mod untrusted;

#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_answer;

pub use answer::{ChatDispatch, ChatGateway, ChatGatewayError, ChatMessage, ChatRole, RagAnswer, RagAnswerService, RagError};
pub use context::ContextBuilder;
pub use prompt::{PromptAssembler, PromptError, RagPrompt, RagPromptPolicy};
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

/// Production answer flow (Stage 10B):
/// task_profile_id selects chat provider/model/privacy/generation;
/// retrieval runs semantic-first with degraded lexical fallback.
#[tauri::command]
pub async fn rag_answer(
    task_profile_id: String,
    query: String,
    embedding_model_id: String,
    embedding_model_fingerprint: String,
    limit: Option<usize>,
    max_chunks: Option<usize>,
    max_chars: Option<usize>,
    max_chars_per_chunk: Option<usize>,
    note_filter: Option<String>,
    cite_sources: Option<bool>,
    refuse_without_evidence: Option<bool>,
    answer_language: Option<String>,
    max_prompt_chars: Option<usize>,
) -> Result<String, String> {
    use crate::rag::answer::{ChatDispatch, ChatGateway, ChatGatewayError, RagAnswerService};
    use crate::rag::prompt::RagPromptPolicy;
    use crate::search::types::{FallbackPolicy, SearchMode, TextSearchRequest, VectorModelFilter};
    use std::sync::Arc;

    if task_profile_id.trim().is_empty() {
        return Err("task_profile_id is empty".to_string());
    }
    // Strict validation, never clamp-masked.
    let req = TextSearchRequest {
        text: query.clone(),
        embedding_model: VectorModelFilter {
            model_id: embedding_model_id.clone(),
            model_fingerprint: embedding_model_fingerprint.clone(),
        },
        limit: limit.unwrap_or(10),
        note_filter: note_filter.clone(),
        min_score: None,
        mode: SearchMode::Auto,
        fallback: FallbackPolicy::Lexical,
    };
    req.validate().map_err(|e| e.to_string())?;
    let mut search = req;

    let ctx_limits = ContextLimits {
        max_chunks: max_chunks.unwrap_or(10),
        max_chars: max_chars.unwrap_or(8000),
        max_chars_per_chunk: max_chars_per_chunk.unwrap_or(2000),
    };
    ctx_limits.validate().map_err(|e| e.to_string())?;

    let policy = RagPromptPolicy {
        cite_sources: cite_sources.unwrap_or(true),
        refuse_without_evidence: refuse_without_evidence.unwrap_or(true),
        answer_language,
        max_prompt_chars: max_prompt_chars.unwrap_or(24_000),
    };
    policy.validate().map_err(|e| e.to_string())?;

    let rag_req = RagRequest {
        search: search.clone(),
        context: ctx_limits,
        include_sources: true, // answers require attributable references
    };

    let cfg = crate::llm::load_config_inner();

    // Embedding side: production provider when configured (authoritative
    // fingerprint + scope for the privacy gate), else lexical-only.
    let (provider, retrieval_scope): (
        Arc<dyn crate::llm::embeddings::provider::EmbeddingProvider>,
        Option<crate::llm::task::policy::ProviderScope>,
    ) = match crate::search::production_embedding_provider(&embedding_model_id) {
        Some((p, filter)) => {
            search.embedding_model = filter;
            let scope = crate::llm::embeddings::wiring::resolve_scope_for_model(&cfg, &embedding_model_id);
            (p, scope)
        }
        None => {
            struct UnavailableProvider;
            #[async_trait::async_trait]
            impl crate::llm::embeddings::provider::EmbeddingProvider for UnavailableProvider {
                async fn embed(
                    &self,
                    _request: crate::llm::embeddings::types::EmbeddingRequest,
                    _cancel: tokio_util::sync::CancellationToken,
                ) -> Result<crate::llm::embeddings::types::EmbeddingResponse, crate::llm::embeddings::types::EmbeddingError> {
                    Err(crate::llm::embeddings::types::EmbeddingError::Provider(
                        "embedding provider not configured".to_string(),
                    ))
                }
            }
            (Arc::new(UnavailableProvider), None)
        }
    };
    let rag_req = RagRequest {
        search,
        context: rag_req.context,
        include_sources: true,
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
    let db_path = vault.join(".fragile").join("embeddings.db");
    let vector_store = Arc::new(crate::search::vector::SqliteVectorStore::new(
        db_path,
        crate::search::types::VectorSearchLimits::default(),
    ));
    let semantic = Arc::new(crate::search::SemanticSearchService::new(
        provider, vector_store, lexical,
    ));
    struct SemanticRagAdapter {
        svc: Arc<
            crate::search::SemanticSearchService<
                dyn crate::llm::embeddings::provider::EmbeddingProvider,
                crate::search::vector::SqliteVectorStore,
                crate::search::LexicalSearchBackend,
            >,
        >,
    }
    #[async_trait::async_trait]
    impl RagSearchBackend for SemanticRagAdapter {
        async fn search(
            &self,
            req: TextSearchRequest,
            cancel: tokio_util::sync::CancellationToken,
        ) -> Result<crate::search::types::SearchResponse, crate::search::types::SearchError> {
            self.svc.search_text(req, cancel).await
        }
    }

    // Chat side: production gateway_chat with task profile passthrough
    // (gateway re-applies its privacy guard; errors redacted + capped).
    struct GatewayChatAdapter;
    #[async_trait::async_trait]
    impl ChatGateway for GatewayChatAdapter {
        async fn chat(
            &self,
            dispatch: ChatDispatch,
            cancel: tokio_util::sync::CancellationToken,
        ) -> Result<String, ChatGatewayError> {
            use crate::llm::embeddings::redact_secrets;
            let messages: Vec<serde_json::Value> = dispatch
                .messages
                .iter()
                .map(|m| {
                    let role = match m.role {
                        crate::rag::answer::ChatRole::System => "system",
                        crate::rag::answer::ChatRole::User => "user",
                    };
                    serde_json::json!({"role": role, "content": m.content})
                })
                .collect();
            let req = crate::llm::ChatRequest {
                provider_id: dispatch.provider_id.clone(),
                model_ref: dispatch.model_id.clone(),
                messages,
                task_profile_id: Some(dispatch.task_profile_id.clone()),
            };
            tokio::select! {
                _ = cancel.cancelled() => Err(ChatGatewayError::Cancelled),
                r = crate::llm::gateway_chat(req) => r.map_err(|e| {
                    let redacted = redact_secrets(&e);
                    let capped: String = redacted.chars().take(500).collect();
                    ChatGatewayError::Transport(capped)
                }),
            }
        }
    }

    let svc = RagAnswerService::new(Arc::new(SemanticRagAdapter { svc: semantic }), Arc::new(GatewayChatAdapter));
    let cancel = tokio_util::sync::CancellationToken::new();
    let res = svc
        .answer(&cfg, &task_profile_id, rag_req, &policy, retrieval_scope, cancel)
        .await
        .map_err(|e| e.to_string())?;
    serde_json::to_string(&res).map_err(|e| e.to_string())
}
