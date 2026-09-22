//! Answer generation flow (Stage 10B, no tools/agents):
//!
//! ```text
//! user query
//!   -> RagRetriever (bounded context + references)
//!   -> privacy gates (retrieval scope, then chat task policy)
//!   -> PromptAssembler (system/user roles, untrusted data in user only)
//!   -> ChatGateway (task profile selects provider/model/generation)
//!   -> RagAnswer (references copied from retrieval, never from LLM text)
//! ```
//!
//! Rules:
//! - One `CancellationToken` across retrieval -> assembly -> chat. After
//!   cancellation no successful `RagAnswer` is returned, and no fallback or
//!   gateway call happens past the cancel point.
//! - `refuse_without_evidence + empty references` -> `NoEvidence` WITHOUT
//!   calling the gateway. Without refusal, the gateway IS called and the
//!   answer is returned with empty references (explicitly no evidence).
//! - References are cloned from the retrieval result. Answer text is never
//!   parsed for citations; a model-hallucinated source is not valid.
//! - Degraded retrieval propagates (`degraded`, typed `fallback_reason`).
//!   Failure reasons carry codes/enums only — no raw bodies, URLs, headers.
//! - Generation settings come from the `TaskProfile`, never from frontend
//!   raw input. Invalid values are ignored (model default applies).

use super::prompt::{PromptAssembler, PromptError, RagPromptPolicy};
use super::retrieval::{RagRetriever, RagSearchBackend};
use super::types::{RagReference, RagRequest};
use crate::llm::task::policy::ProviderScope;
use crate::llm::{Capability, LlmConfig};
use crate::search::types::{FallbackReason, SearchError, SearchMode};
use std::sync::Arc;
use tokio_util::sync::CancellationToken;

/// Chat message roles allowed in RAG prompts. No tool roles exist here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChatRole {
    System,
    User,
}

/// A single assembled chat message (content already built, no raw context).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChatMessage {
    pub role: ChatRole,
    pub content: String,
}

/// Dispatch envelope for the chat gateway. Provider/model/privacy were
/// resolved from the `TaskProfile` (source of truth); temperature/max_tokens
/// were parsed from its generation map (invalid values -> None).
#[derive(Debug, Clone, PartialEq)]
pub struct ChatDispatch {
    pub provider_id: String,
    pub model_id: String,
    pub task_profile_id: String,
    pub messages: Vec<ChatMessage>,
    pub temperature: Option<f32>,
    pub max_tokens: Option<u32>,
}

/// Gateway failures. Strings are pre-redacted + length-capped by the
/// production adapter; fakes use canned values.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChatGatewayError {
    Transport(String),
    Cancelled,
}

impl std::fmt::Display for ChatGatewayError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Transport(msg) => write!(f, "chat transport failed: {}", msg),
            Self::Cancelled => write!(f, "cancelled"),
        }
    }
}

/// Chat transport seam. Production implements it over `gateway_chat`;
/// tests use fakes. Cancellation is cooperative via the token.
#[async_trait::async_trait]
pub trait ChatGateway: Send + Sync {
    async fn chat(
        &self,
        dispatch: ChatDispatch,
        cancel: CancellationToken,
    ) -> Result<String, ChatGatewayError>;
}

/// Final RAG answer. References come ONLY from the retriever.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct RagAnswer {
    pub answer: String,
    pub references: Vec<RagReference>,
    pub search_mode: SearchMode,
    pub degraded: bool,
    pub fallback_reason: Option<FallbackReason>,
}

/// Typed answer failures (codes/ids only, no raw provider data).
#[derive(Debug, Clone, PartialEq)]
pub enum RagError {
    InvalidRequest(String),
    TaskNotFound(String),
    ModelNotFound(String),
    ProviderNotFound(String),
    PrivacyViolation(String),
    UnsupportedCapability(String),
    Retrieval(SearchError),
    Cancelled,
    NoEvidence,
    Prompt(PromptError),
    PromptTooLarge { chars: usize, max: usize },
    ChatFailed(String),
}

impl std::fmt::Display for RagError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidRequest(m) => write!(f, "invalid request: {}", m),
            Self::TaskNotFound(id) => write!(f, "task profile not found: {}", id),
            Self::ModelNotFound(id) => write!(f, "model not found: {}", id),
            Self::ProviderNotFound(id) => write!(f, "provider not found: {}", id),
            Self::PrivacyViolation(m) => write!(f, "{}", m),
            Self::UnsupportedCapability(m) => write!(f, "{}", m),
            Self::Retrieval(e) => write!(f, "retrieval failed: {}", e),
            Self::Cancelled => write!(f, "cancelled"),
            Self::NoEvidence => write!(f, "no evidence: retrieval returned no references"),
            Self::Prompt(e) => write!(f, "prompt assembly failed: {}", e),
            Self::PromptTooLarge { chars, max } => {
                write!(f, "prompt too large: {} chars exceed max {}", chars, max)
            }
            Self::ChatFailed(m) => write!(f, "chat failed: {}", m),
        }
    }
}

impl std::error::Error for RagError {}

/// Parse generation settings from a task profile map.
/// Invalid/absent values -> None (model default applies), documented.
pub fn generation_from_profile(generation: &std::collections::HashMap<String, String>) -> (Option<f32>, Option<u32>) {
    let temperature = generation
        .get("temperature")
        .and_then(|s| s.parse::<f32>().ok())
        .filter(|t| t.is_finite() && *t >= 0.0 && *t <= 2.0);
    let max_tokens = generation
        .get("max_tokens")
        .and_then(|s| s.parse::<u32>().ok())
        .filter(|n| *n > 0 && *n <= 1_000_000);
    (temperature, max_tokens)
}

pub struct RagAnswerService<S, G>
where
    S: RagSearchBackend,
    G: ChatGateway,
{
    retriever: RagRetriever<S>,
    chat: Arc<G>,
}

impl<S, G> RagAnswerService<S, G>
where
    S: RagSearchBackend,
    G: ChatGateway,
{
    pub fn new(retrieval: Arc<S>, chat: Arc<G>) -> Self {
        Self {
            retriever: RagRetriever::new(retrieval),
            chat,
        }
    }

    /// Full answer flow. `retrieval_scope` describes the embedding path:
    /// `None` = local-only retrieval (e.g. lexical FTS, nothing leaves the
    /// box); `Some(scope)` = scope of the configured embedding provider.
    pub async fn answer(
        &self,
        config: &LlmConfig,
        task_profile_id: &str,
        rag_req: RagRequest,
        policy: &RagPromptPolicy,
        retrieval_scope: Option<ProviderScope>,
        cancel: CancellationToken,
    ) -> Result<RagAnswer, RagError> {
        rag_req
            .validate()
            .map_err(|e| RagError::InvalidRequest(e))?;
        policy.validate().map_err(|e| RagError::InvalidRequest(e))?;
        if cancel.is_cancelled() {
            return Err(RagError::Cancelled);
        }

        // Task profile is the source of truth for chat provider/model/privacy.
        let sel = crate::llm::task::selection::resolve_selection(config, task_profile_id)
            .map_err(|e| {
                if e.starts_with("task_not_found") {
                    RagError::TaskNotFound(task_profile_id.to_string())
                } else if e.starts_with("model_not_found") {
                    RagError::ModelNotFound(sel_model_hint(&e))
                } else {
                    RagError::ProviderNotFound(e)
                }
            })?;

        // Gate 1: retrieval scope against the task privacy.
        // `None` = local-only retrieval (lexical FTS, nothing leaves the
        // box). Each side (retrieval, chat) is gated independently because
        // embedding and chat providers may legitimately differ.
        void_retrieval_scope_check(&sel.task.privacy, retrieval_scope.as_ref())
            .map_err(RagError::PrivacyViolation)?;

        // Gate 2: chat provider against the task privacy.
        crate::llm::embeddings::wiring::validate_task_policy(
            &sel.task.privacy,
            &sel.provider.kind,
            &sel.provider.endpoint,
        )
        .map_err(|e| RagError::PrivacyViolation(e.to_string()))?;

        // Chat capability (mirrors TaskExecutor: empty = unknown = allow).
        if !sel.model.capabilities.is_empty()
            && !sel.model.capabilities.contains(&Capability::Chat)
        {
            return Err(RagError::UnsupportedCapability(format!(
                "model {} cannot chat",
                sel.model.id
            )));
        }

        // Retrieval (single token; retriever maps cancel, never falls back
        // after explicit cancellation, never yields partial context on error).
        let retrieval = tokio::select! {
            _ = cancel.cancelled() => return Err(RagError::Cancelled),
            r = self.retriever.retrieve(rag_req.clone(), cancel.clone()) => r,
        };
        let retrieval = match retrieval {
            Ok(v) => v,
            Err(SearchError::Cancelled) => return Err(RagError::Cancelled),
            Err(e) => return Err(RagError::Retrieval(e)),
        };

        if cancel.is_cancelled() {
            return Err(RagError::Cancelled);
        }

        let references = retrieval.context.references.clone();
        if references.is_empty() && policy.refuse_without_evidence {
            return Err(RagError::NoEvidence);
        }

        // Prompt assembly (pure; references copied, never parsed from text).
        let per_chunk = rag_req.context.max_chars_per_chunk;
        let previews = PromptAssembler::previews_for(
            &retrieval.search.results.iter().map(|r| r.content.clone()).collect::<Vec<_>>(),
            per_chunk,
        );
        let query = rag_req.search.text.clone();
        let prompt =
            PromptAssembler::assemble(&query, &references, &previews, policy).map_err(|e| {
                match e {
                    PromptError::PromptTooLarge { chars, max } => {
                        RagError::PromptTooLarge { chars, max }
                    }
                    other => RagError::Prompt(other),
                }
            })?;

        if cancel.is_cancelled() {
            return Err(RagError::Cancelled);
        }

        let (temperature, max_tokens) = generation_from_profile(&sel.task.generation);
        let wire_model = if sel.model.remote_id.trim().is_empty() {
            sel.model.id.clone()
        } else {
            sel.model.remote_id.clone()
        };
        let dispatch = ChatDispatch {
            provider_id: sel.provider.id.clone(),
            model_id: wire_model,
            task_profile_id: sel.task.id.clone(),
            messages: vec![
                ChatMessage {
                    role: ChatRole::System,
                    content: prompt.system,
                },
                ChatMessage {
                    role: ChatRole::User,
                    content: prompt.user,
                },
            ],
            temperature,
            max_tokens,
        };

        let answer = tokio::select! {
            _ = cancel.cancelled() => return Err(RagError::Cancelled),
            r = self.chat.chat(dispatch, cancel.clone()) => r,
        };
        let answer = match answer {
            Ok(text) => text,
            Err(ChatGatewayError::Cancelled) => return Err(RagError::Cancelled),
            Err(ChatGatewayError::Transport(msg)) => return Err(RagError::ChatFailed(msg)),
        };

        Ok(RagAnswer {
            answer,
            references,
            search_mode: retrieval.search.mode.clone(),
            degraded: retrieval.degraded,
            fallback_reason: retrieval.fallback_reason.clone(),
        })
    }
}

fn sel_model_hint(msg: &str) -> String {
    msg.rsplit(": ").next().unwrap_or(msg).trim().to_string()
}

/// Authoritative retrieval-scope gate: `LocalOnly` tasks may only use
/// local retrieval (`None` = lexical FTS, or Local scopes). Cloud retrieval
/// under `LocalOnly` is a privacy violation even when chat itself is local.
fn void_retrieval_scope_check(
    privacy: &Option<crate::llm::Privacy>,
    scope: Option<&ProviderScope>,
) -> Result<(), String> {
    if let Some(crate::llm::Privacy::LocalOnly) = privacy {
        match scope {
            None | Some(ProviderScope::LocalManaged) | Some(ProviderScope::LocalExternal) => Ok(()),
            Some(ProviderScope::Cloud) => Err(
                "PrivacyPolicyViolation: local-only task cannot use cloud embeddings".to_string(),
            ),
        }
    } else {
        Ok(())
    }
}
