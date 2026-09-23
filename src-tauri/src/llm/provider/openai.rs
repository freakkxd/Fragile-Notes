//! Stage 2 (v0.5.9): OpenAI-compatible provider adapter.
//!
//! One adapter covers llama.cpp, Ollama-compatible endpoints, LM Studio,
//! vLLM, LocalAI, OpenRouter, DeepSeek, and custom OpenAI-compatible servers.
//! It consumes [`ProviderConnection`] + [`ProviderModel`] only — no parallel
//! config model. Raw keys live in the keyring; the adapter holds just the
//! `secret_ref` (inside the connection) and resolves the key per request.

use super::types::{
    AuthReference, ProviderConnection, ProviderError, ProviderModel,
};
use crate::llm::embeddings::provider::{check_cancelled, redact_secrets};
use crate::llm::embeddings::types::{EmbeddingLimits, EmbeddingRequest, EmbeddingResponse};
use crate::llm::{Capability, CapabilitySource};
use crate::llm::models::types::ModelSource;
use async_trait::async_trait;
use std::sync::Arc;
use std::time::Duration;
use tokio_util::sync::CancellationToken;

const ERROR_BODY_LIMIT: usize = 8192;

// ---------------------------------------------------------------------------
// Endpoint normalization
// ---------------------------------------------------------------------------

/// How a base URL is laid out. Detected explicitly — no `format!` magic.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EndpointLayout {
    /// Bare host (`https://api.example.com`, `/`).
    Root,
    /// Already carries `/v1` (`https://api.example.com/v1`, `/v1/`).
    OpenAiV1,
    /// Any other path prefix (`https://host/custom`).
    Custom,
}

/// Classify `base` (trailing slashes ignored).
pub fn detect_layout(base: &str) -> EndpointLayout {
    let t = base.trim_end_matches('/');
    if t.ends_with("/v1") {
        EndpointLayout::OpenAiV1
    } else if t.find("://").map(|i| t[i + 3..].contains('/')).unwrap_or(false) {
        EndpointLayout::Custom
    } else {
        EndpointLayout::Root
    }
}

/// Join `base` with an operation path (`/v1/chat/completions`,
/// `/v1/embeddings`, `/v1/models`).
///
/// * `OpenAiV1` bases already end with `/v1` — the operation's `/v1` prefix
///   is replaced, never doubled (legacy gateway doubled it; frozen there).
/// * `Root`/`Custom` bases get the full operation path appended.
pub fn join_api_path(base: &str, op: &str) -> Result<String, ProviderError> {
    if base.trim().is_empty() {
        return Err(ProviderError::InvalidConfig("base endpoint is empty".to_string()));
    }
    if !op.starts_with('/') {
        return Err(ProviderError::InvalidConfig(
            "operation path must start with /".to_string(),
        ));
    }
    let b = base.trim_end_matches('/');
    let path = match detect_layout(b) {
        EndpointLayout::OpenAiV1 => op.strip_prefix("/v1").unwrap_or(op),
        EndpointLayout::Root | EndpointLayout::Custom => op,
    };
    Ok(format!("{}{}", b, path))
}

// ---------------------------------------------------------------------------
// Secrets
// ---------------------------------------------------------------------------

/// Credential source. Synchronous: keyring reads are local and fast.
pub trait SecretStore: Send + Sync {
    fn get(&self, secret_ref: &str) -> Result<String, ProviderError>;
}

/// No credentials — for local providers without auth.
pub struct NoSecretStore;

impl SecretStore for NoSecretStore {
    fn get(&self, _secret_ref: &str) -> Result<String, ProviderError> {
        Err(ProviderError::CredentialsMissing(
            "no secret store configured".to_string(),
        ))
    }
}

/// OS keyring (`com.fragilich.notes`, `keychain://fragile-notes/<key>`).
/// Errors never carry secret material.
pub struct KeyringSecretStore;

impl SecretStore for KeyringSecretStore {
    fn get(&self, secret_ref: &str) -> Result<String, ProviderError> {
        let key = secret_ref
            .strip_prefix("keychain://fragile-notes/")
            .ok_or_else(|| {
                ProviderError::InvalidConfig(
                    "secret_ref must be keychain://fragile-notes/...".to_string(),
                )
            })?;
        if key.is_empty() {
            return Err(ProviderError::CredentialsMissing(
                "empty key name".to_string(),
            ));
        }
        let entry = keyring::Entry::new("com.fragilich.notes", key)
            .map_err(|e| ProviderError::KeyringUnavailable(e.to_string()))?;
        match entry.get_password() {
            Ok(pw) if !pw.is_empty() => Ok(pw),
            Ok(_) => Err(ProviderError::CredentialsMissing(format!(
                "empty credential for {}",
                redact_id(key)
            ))),
            Err(_) => Err(ProviderError::CredentialsMissing(format!(
                "no credential for {}",
                redact_id(key)
            ))),
        }
    }
}

fn redact_id(s: &str) -> String {
    // Key names are not secrets, but keep messages short and stable.
    s.chars().take(64).collect()
}

// ---------------------------------------------------------------------------
// Chat / health types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChatRole {
    System,
    User,
    Assistant,
}

impl ChatRole {
    pub(crate) fn as_str(&self) -> &'static str {
        match self {
            Self::System => "system",
            Self::User => "user",
            Self::Assistant => "assistant",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ChatMessage {
    pub role: ChatRole,
    pub content: String,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct ResponseFormat {
    /// e.g. `"json_object"`. Sent only when the model allows StructuredOutput.
    pub kind: String,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct ChatRequest {
    pub messages: Vec<ChatMessage>,
    pub temperature: Option<f32>,
    pub top_p: Option<f32>,
    pub max_tokens: Option<u32>,
    pub response_format: Option<ResponseFormat>,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct ChatUsage {
    pub prompt_tokens: Option<u64>,
    pub completion_tokens: Option<u64>,
    pub total_tokens: Option<u64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ChatResponse {
    pub content: String,
    pub model: String,
    pub finish_reason: Option<String>,
    pub usage: Option<ChatUsage>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct HealthReport {
    pub reachable: bool,
    pub authenticated: Option<bool>,
    pub latency_ms: u64,
    /// Declared capabilities only — never inferred from names/responses.
    pub capabilities: Vec<Capability>,
    pub capability_source: CapabilitySource,
}

// ---------------------------------------------------------------------------
// Adapter trait
// ---------------------------------------------------------------------------

#[async_trait]
pub trait ProviderAdapter: Send + Sync {
    async fn health(&self, cancel: CancellationToken) -> Result<HealthReport, ProviderError>;
    async fn list_models(
        &self,
        cancel: CancellationToken,
    ) -> Result<Vec<ProviderModel>, ProviderError>;
    async fn chat(
        &self,
        model: &ProviderModel,
        request: ChatRequest,
        cancel: CancellationToken,
    ) -> Result<ChatResponse, ProviderError>;
    async fn embed(
        &self,
        model: &ProviderModel,
        request: EmbeddingRequest,
        cancel: CancellationToken,
    ) -> Result<EmbeddingResponse, ProviderError>;
}

// ---------------------------------------------------------------------------
// OpenAI-compatible adapter
// ---------------------------------------------------------------------------

pub struct OpenAiCompatibleAdapter {
    connection: ProviderConnection,
    secrets: Arc<dyn SecretStore>,
    http: reqwest::Client,
    timeout: Duration,
    /// Explicitly declared capabilities (registry/catalog); echoed by health,
    /// never guessed.
    capabilities: Vec<Capability>,
    capability_source: CapabilitySource,
}

/// Manual `Debug`: connection already redacts; no transient secrets stored.
impl std::fmt::Debug for OpenAiCompatibleAdapter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OpenAiCompatibleAdapter")
            .field("connection", &self.connection)
            .field("timeout", &self.timeout)
            .field("capabilities", &self.capabilities)
            .finish()
    }
}

impl OpenAiCompatibleAdapter {
    pub fn new(
        connection: ProviderConnection,
        secrets: Arc<dyn SecretStore>,
        timeout: Duration,
        capabilities: Vec<Capability>,
        capability_source: CapabilitySource,
    ) -> Result<Self, ProviderError> {
        connection.validate()?;
        let http = reqwest::Client::builder()
            .timeout(timeout + Duration::from_secs(5))
            .build()
            .map_err(|e| ProviderError::InvalidConfig(e.to_string()))?;
        Ok(Self {
            connection,
            secrets,
            http,
            timeout,
            capabilities,
            capability_source,
        })
    }

    pub fn connection(&self) -> &ProviderConnection {
        &self.connection
    }

    fn base_url(&self) -> Result<String, ProviderError> {
        self.connection.effective_endpoint().ok_or_else(|| {
            ProviderError::InvalidConfig(format!(
                "provider {} has no endpoint",
                self.connection.id
            ))
        })
    }

    /// Resolve the API key per request. `None` auth → no header, no lookup.
    fn resolve_key(&self, cancel: &CancellationToken) -> Result<Option<String>, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        match &self.connection.auth {
            None => Ok(None),
            Some(AuthReference { secret_ref }) => {
                let key = self.secrets.get(secret_ref)?;
                if key.trim().is_empty() {
                    return Err(ProviderError::CredentialsMissing(format!(
                        "empty credential for provider {}",
                        self.connection.id
                    )));
                }
                Ok(Some(key))
            }
        }
    }

    fn check_model(&self, model: &ProviderModel) -> Result<(), ProviderError> {
        if model.provider_id != self.connection.id {
            return Err(ProviderError::InvalidConfig(format!(
                "model {} belongs to {}, not {}",
                model.id, model.provider_id, self.connection.id
            )));
        }
        if model.remote_id.trim().is_empty() {
            return Err(ProviderError::InvalidConfig(format!(
                "model {} has empty remote_id (never send internal id upstream)",
                model.id
            )));
        }
        Ok(())
    }

    async fn race<T, F>(&self, cancel: CancellationToken, fut: F) -> Result<T, ProviderError>
    where
        F: std::future::Future<Output = Result<T, ProviderError>>,
    {
        tokio::select! {
            _ = cancel.cancelled() => Err(ProviderError::Cancelled),
            res = tokio::time::timeout(self.timeout, fut) => match res {
                Ok(r) => r,
                Err(_) => Err(ProviderError::Timeout(format!("timeout after {:?}", self.timeout))),
            },
        }
    }
}

/// Build the `/v1/chat/completions` body. `model.remote_id` is the only
/// model identifier sent upstream. Optional fields are skipped when `None`;
/// `response_format` requires `StructuredOutput` capability.
pub fn build_chat_body(
    model: &ProviderModel,
    request: &ChatRequest,
) -> Result<serde_json::Value, ProviderError> {
    if request.messages.is_empty() {
        return Err(ProviderError::InvalidRequest(
            "messages is empty".to_string(),
        ));
    }
    for m in [&request.temperature, &request.top_p].into_iter().flatten() {
        if !m.is_finite() {
            return Err(ProviderError::InvalidRequest(
                "temperature/top_p must be finite".to_string(),
            ));
        }
    }
    if let Some(rf) = &request.response_format {
        if !model.supports(&Capability::StructuredOutput) {
            return Err(ProviderError::UnsupportedCapability(
                "StructuredOutput".to_string(),
            ));
        }
        if rf.kind.trim().is_empty() {
            return Err(ProviderError::InvalidRequest(
                "empty response_format".to_string(),
            ));
        }
    }
    let messages: Vec<serde_json::Value> = request
        .messages
        .iter()
        .map(|m| serde_json::json!({"role": m.role.as_str(), "content": m.content}))
        .collect();
    let mut body = serde_json::json!({"model": model.remote_id, "messages": messages});
    if let Some(t) = request.temperature {
        body["temperature"] = serde_json::json!(t);
    }
    if let Some(p) = request.top_p {
        body["top_p"] = serde_json::json!(p);
    }
    if let Some(n) = request.max_tokens {
        body["max_tokens"] = serde_json::json!(n);
    }
    if let Some(rf) = &request.response_format {
        body["response_format"] = serde_json::json!({"type": rf.kind});
    }
    Ok(body)
}

fn truncate_body(s: &str) -> String {
    if s.len() <= ERROR_BODY_LIMIT {
        s.to_string()
    } else {
        format!(
            "{}...[truncated {} bytes]",
            &s[..ERROR_BODY_LIMIT],
            s.len() - ERROR_BODY_LIMIT
        )
    }
}

fn map_http_status(status: u16, body: &str) -> ProviderError {
    let msg = format!("HTTP {}: {}", status, redact_secrets(&truncate_body(body)));
    match status {
        400 => ProviderError::InvalidRequest(msg),
        401 | 403 => ProviderError::Unauthorized(msg),
        404 => ProviderError::NotFound(msg),
        408 => ProviderError::Timeout(msg),
        429 => ProviderError::RateLimited(msg),
        500..=599 => ProviderError::Server {
            status,
            message: msg,
        },
        _ => ProviderError::Network(msg),
    }
}

fn map_reqwest_err(e: reqwest::Error) -> ProviderError {
    let msg = redact_secrets(&e.to_string());
    if e.is_timeout() {
        return ProviderError::Timeout(msg);
    }
    if e.is_connect() {
        return ProviderError::Network(msg);
    }
    if e.is_request() && msg.to_lowercase().contains("cancel") {
        return ProviderError::Cancelled;
    }
    ProviderError::Network(msg)
}

// Shared with the native adapter (same normalization, one definition).
pub(crate) fn map_http_status_for(status: u16, body: &str) -> ProviderError {
    map_http_status(status, body)
}

pub(crate) fn map_reqwest_err_for(e: reqwest::Error) -> ProviderError {
    map_reqwest_err(e)
}

/// Reject oversized success bodies before parsing.
pub(crate) fn truncate_capped(bytes: &[u8]) -> Result<&[u8], ProviderError> {
    if bytes.len() > ERROR_BODY_LIMIT * 128 {
        return Err(ProviderError::MalformedResponse(
            "response too large".to_string(),
        ));
    }
    Ok(bytes)
}

/// Race a request future against cancellation and timeout.
/// No auto-retry — only classification via `retryable`.
pub(crate) async fn race_with<T, F, Fut>(
    client: reqwest::Client,
    timeout: Duration,
    cancel: CancellationToken,
    f: F,
) -> Result<T, ProviderError>
where
    F: FnOnce(reqwest::Client) -> Fut,
    Fut: std::future::Future<Output = Result<T, ProviderError>>,
{
    tokio::select! {
        _ = cancel.cancelled() => Err(ProviderError::Cancelled),
        res = tokio::time::timeout(timeout, f(client)) => match res {
            Ok(r) => r,
            Err(_) => Err(ProviderError::Timeout(format!("timeout after {:?}", timeout))),
        },
    }
}

#[async_trait]
impl ProviderAdapter for OpenAiCompatibleAdapter {
    async fn health(&self, cancel: CancellationToken) -> Result<HealthReport, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        let t0 = std::time::Instant::now();
        let out = self
            .race(cancel.clone(), async {
                // Local servers may expose /health; fall back to /v1/models.
                let local = matches!(
                    self.connection.scope,
                    crate::llm::task::policy::ProviderScope::LocalManaged
                        | crate::llm::task::policy::ProviderScope::LocalExternal
                );
                if local {
                    let url = join_api_path(&base, "/health").unwrap_or_else(|_| format!("{}/health", base.trim_end_matches('/')));
                    let mut req = self.http.get(&url);
                    if let Some(k) = &key {
                        req = req.header("Authorization", format!("Bearer {}", k));
                    }
                    if let Ok(resp) = req.send().await {
                        if resp.status().is_success() {
                            return Ok((true, key.is_some().then_some(true)));
                        }
                    }
                    if cancel.is_cancelled() {
                        return Err(ProviderError::Cancelled);
                    }
                }
                let url = join_api_path(&base, "/v1/models")?;
                let mut req = self.http.get(&url);
                if let Some(k) = &key {
                    req = req.header("Authorization", format!("Bearer {}", k));
                }
                let resp = req.send().await.map_err(map_reqwest_err)?;
                let status = resp.status().as_u16();
                if cancel.is_cancelled() {
                    return Err(ProviderError::Cancelled);
                }
                match status {
                    200..=299 => Ok((true, key.is_some().then_some(true))),
                    401 | 403 => Ok((true, Some(false))),
                    _ => Ok((true, None)),
                }
            })
            .await?;
        let (reachable, authenticated) = out;
        Ok(HealthReport {
            reachable,
            authenticated,
            latency_ms: t0.elapsed().as_millis() as u64,
            capabilities: self.capabilities.clone(),
            capability_source: self.capability_source.clone(),
        })
    }

    async fn list_models(
        &self,
        cancel: CancellationToken,
    ) -> Result<Vec<ProviderModel>, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        self.race(cancel.clone(), async {
            let url = join_api_path(&base, "/v1/models")?;
            let mut req = self.http.get(&url);
            if let Some(k) = &key {
                req = req.header("Authorization", format!("Bearer {}", k));
            }
            let resp = req.send().await.map_err(map_reqwest_err)?;
            if cancel.is_cancelled() {
                return Err(ProviderError::Cancelled);
            }
            let status = resp.status().as_u16();
            if !resp.status().is_success() {
                let body = resp.text().await.unwrap_or_default();
                return Err(map_http_status(status, &body));
            }
            let bytes = resp.bytes().await.map_err(map_reqwest_err)?;
            if bytes.len() > ERROR_BODY_LIMIT * 128 {
                return Err(ProviderError::MalformedResponse(
                    "models response too large".to_string(),
                ));
            }
            let json: serde_json::Value = serde_json::from_slice(&bytes).map_err(|e| {
                ProviderError::MalformedResponse(redact_secrets(&e.to_string()))
            })?;
            let data = json.get("data").and_then(|v| v.as_array()).ok_or_else(|| {
                ProviderError::MalformedResponse("missing data array".to_string())
            })?;
            let mut out = Vec::with_capacity(data.len());
            for item in data {
                let remote = item
                    .get("id")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| {
                        ProviderError::MalformedResponse("model entry missing id".to_string())
                    })?;
                if remote.trim().is_empty() {
                    return Err(ProviderError::MalformedResponse(
                        "empty remote model id".to_string(),
                    ));
                }
                // Deterministic internal id — no random UUID per refresh.
                // Capabilities stay unknown (never guessed from names).
                out.push(ProviderModel {
                    id: format!("{}:{}", self.connection.id, remote),
                    provider_id: self.connection.id.clone(),
                    remote_id: remote.to_string(),
                    display_name: remote.to_string(),
                    capabilities: Vec::new(),
                    capability_source: None,
                    context_length: None,
                    dimensions: None,
                    pricing: None,
                    source: ModelSource::Manual,
                });
            }
            Ok(out)
        })
        .await
    }

    async fn chat(
        &self,
        model: &ProviderModel,
        request: ChatRequest,
        cancel: CancellationToken,
    ) -> Result<ChatResponse, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        self.check_model(model)?;
        if !model.supports(&Capability::Chat) {
            return Err(ProviderError::UnsupportedCapability("Chat".to_string()));
        }
        let body = build_chat_body(model, &request)?;
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        self.race(cancel.clone(), async {
            let url = join_api_path(&base, "/v1/chat/completions")?;
            let mut req = self
                .http
                .post(&url)
                .header("Content-Type", "application/json");
            if let Some(k) = &key {
                req = req.header("Authorization", format!("Bearer {}", k));
            }
            let resp = req
                .json(&body)
                .send()
                .await
                .map_err(map_reqwest_err)?;
            if cancel.is_cancelled() {
                return Err(ProviderError::Cancelled);
            }
            let status = resp.status().as_u16();
            if !resp.status().is_success() {
                let b = resp.text().await.unwrap_or_default();
                return Err(map_http_status(status, &b));
            }
            let bytes = resp.bytes().await.map_err(map_reqwest_err)?;
            if bytes.len() > ERROR_BODY_LIMIT * 128 {
                return Err(ProviderError::MalformedResponse(
                    "chat response too large".to_string(),
                ));
            }
            let json: serde_json::Value = serde_json::from_slice(&bytes).map_err(|e| {
                ProviderError::MalformedResponse(redact_secrets(&e.to_string()))
            })?;
            let content = json
                .get("choices")
                .and_then(|v| v.as_array())
                .and_then(|a| a.first())
                .and_then(|c| c.get("message"))
                .and_then(|m| m.get("content"))
                .and_then(|c| c.as_str())
                .ok_or_else(|| {
                    ProviderError::MalformedResponse("missing choices[0].message.content".to_string())
                })?
                .to_string();
            let resp_model = json
                .get("model")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let finish = json
                .get("choices")
                .and_then(|v| v.as_array())
                .and_then(|a| a.first())
                .and_then(|c| c.get("finish_reason"))
                .and_then(|v| v.as_str())
                .map(String::from);
            let usage = json.get("usage").map(|u| ChatUsage {
                prompt_tokens: u.get("prompt_tokens").and_then(|v| v.as_u64()),
                completion_tokens: u.get("completion_tokens").and_then(|v| v.as_u64()),
                total_tokens: u.get("total_tokens").and_then(|v| v.as_u64()),
            });
            Ok(ChatResponse {
                content,
                model: resp_model,
                finish_reason: finish,
                usage,
            })
        })
        .await
    }

    async fn embed(
        &self,
        model: &ProviderModel,
        request: EmbeddingRequest,
        cancel: CancellationToken,
    ) -> Result<EmbeddingResponse, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        self.check_model(model)?;
        if !model.supports(&Capability::Embeddings) {
            return Err(ProviderError::UnsupportedCapability(
                "Embeddings".to_string(),
            ));
        }
        // Domain validation before any network (default limits).
        let limits = EmbeddingLimits::default();
        request
            .validate(&limits)
            .map_err(|e| ProviderError::InvalidRequest(format!("{:?}", e)))?;
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        self.race(cancel.clone(), async {
            let url = join_api_path(&base, "/v1/embeddings")?;
            let mut req = self
                .http
                .post(&url)
                .header("Content-Type", "application/json");
            if let Some(k) = &key {
                req = req.header("Authorization", format!("Bearer {}", k));
            }
            // remote_id is the only model identifier sent upstream.
            let body = serde_json::json!({
                "model": model.remote_id,
                "input": request.inputs,
                "encoding_format": "float"
            });
            let resp = req
                .json(&body)
                .send()
                .await
                .map_err(map_reqwest_err)?;
            if cancel.is_cancelled() {
                return Err(ProviderError::Cancelled);
            }
            let status = resp.status().as_u16();
            if !resp.status().is_success() {
                let b = resp.text().await.unwrap_or_default();
                return Err(map_http_status(status, &b));
            }
            let bytes = resp.bytes().await.map_err(map_reqwest_err)?;
            if bytes.len() > limits.max_vector_bytes + 1024 * 1024 {
                return Err(ProviderError::MalformedResponse(
                    "embeddings response too large".to_string(),
                ));
            }
            parse_embeddings_json(&bytes, model, &request, &limits)
        })
        .await
    }
}

/// Transport-level parse: structure, index ordering, per-vector shape.
/// Domain semantics (count/dims/finite) are re-checked by
/// [`EmbeddingResponse::validate_for`] — single source of truth.
fn parse_embeddings_json(
    bytes: &[u8],
    model: &ProviderModel,
    request: &EmbeddingRequest,
    limits: &EmbeddingLimits,
) -> Result<EmbeddingResponse, ProviderError> {
    let json: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|e| ProviderError::MalformedResponse(redact_secrets(&e.to_string())))?;
    let data = json.get("data").and_then(|v| v.as_array()).ok_or_else(|| {
        ProviderError::MalformedResponse("missing data array".to_string())
    })?;
    if data.len() != request.inputs.len() {
        return Err(ProviderError::MalformedResponse(format!(
            "count mismatch: expected {} got {}",
            request.inputs.len(),
            data.len()
        )));
    }
    if data.is_empty() {
        return Err(ProviderError::MalformedResponse("empty data".to_string()));
    }
    let mut indexed: Vec<(usize, Vec<f32>)> = Vec::with_capacity(data.len());
    let mut seen = std::collections::HashSet::new();
    for item in data {
        let idx = item.get("index").and_then(|v| v.as_u64()).ok_or_else(|| {
            ProviderError::MalformedResponse("missing index".to_string())
        })? as usize;
        if !seen.insert(idx) || idx >= request.inputs.len() {
            return Err(ProviderError::MalformedResponse(format!(
                "bad index {}",
                idx
            )));
        }
        let emb = item.get("embedding").and_then(|v| v.as_array()).ok_or_else(|| {
            ProviderError::MalformedResponse("missing embedding array".to_string())
        })?;
        let mut vec = Vec::with_capacity(emb.len());
        for val in emb {
            let f = val.as_f64().ok_or_else(|| {
                ProviderError::MalformedResponse("non-number in embedding".to_string())
            })? as f32;
            vec.push(f);
        }
        indexed.push((idx, vec));
    }
    indexed.sort_by_key(|(idx, _)| *idx);
    let vectors: Vec<Vec<f32>> = indexed.into_iter().map(|(_, v)| v).collect();
    let dimensions = vectors.first().map(Vec::len).unwrap_or(0);
    let app_request =
        EmbeddingRequest::new(model.id.clone(), request.inputs.clone());
    let response = EmbeddingResponse {
        model_id: model.id.clone(),
        dimensions,
        vectors,
    };
    response
        .validate_for(&app_request, limits)
        .map_err(|e| ProviderError::MalformedResponse(format!("{:?}", e)))?;
    Ok(response)
}

// ---------------------------------------------------------------------------
// Bridges to existing contracts (no breakage; Stage 4 routes through these)
// ---------------------------------------------------------------------------

/// [`crate::llm::embeddings::provider::EmbeddingProvider`] over an adapter
/// bound to one catalog model.
pub struct AdapterEmbedBridge<'a> {
    pub adapter: &'a OpenAiCompatibleAdapter,
    pub model: ProviderModel,
}

#[async_trait]
impl<'a> crate::llm::embeddings::provider::EmbeddingProvider for AdapterEmbedBridge<'a> {
    async fn embed(
        &self,
        request: EmbeddingRequest,
        cancel: CancellationToken,
    ) -> Result<EmbeddingResponse, crate::llm::embeddings::types::EmbeddingError> {
        use crate::llm::embeddings::types::EmbeddingError;
        self.adapter
            .embed(&self.model, request, cancel)
            .await
            .map_err(|e| match e {
                ProviderError::Cancelled => EmbeddingError::Cancelled,
                ProviderError::Timeout(m) => EmbeddingError::Timeout(m),
                other => EmbeddingError::Provider(format!("{:?}", other)),
            })
    }
}

/// [`crate::rag::answer::ChatGateway`] over an adapter bound to one model.
pub struct AdapterChatBridge<'a> {
    pub adapter: &'a OpenAiCompatibleAdapter,
    pub model: ProviderModel,
}

#[async_trait]
impl<'a> crate::rag::answer::ChatGateway for AdapterChatBridge<'a> {
    async fn chat(
        &self,
        dispatch: crate::rag::answer::ChatDispatch,
        cancel: CancellationToken,
    ) -> Result<String, crate::rag::answer::ChatGatewayError> {
        use crate::rag::answer::{ChatGatewayError, ChatRole as RagRole};
        let messages = dispatch
            .messages
            .into_iter()
            .map(|m| ChatMessage {
                role: match m.role {
                    RagRole::System => ChatRole::System,
                    RagRole::User => ChatRole::User,
                },
                content: m.content,
            })
            .collect();
        let req = ChatRequest {
            messages,
            temperature: dispatch.temperature,
            top_p: None,
            max_tokens: dispatch.max_tokens,
            response_format: None,
        };
        self.adapter
            .chat(&self.model, req, cancel)
            .await
            .map(|r| r.content)
            .map_err(|e| match e {
                ProviderError::Cancelled => ChatGatewayError::Cancelled,
                other => ChatGatewayError::Transport(
                    redact_secrets(&format!("{:?}", other)).chars().take(600).collect(),
                ),
            })
    }
}

#[cfg(test)]
mod unit_tests {
    use super::*;

    fn local_conn() -> ProviderConnection {
        ProviderConnection {
            id: "local".to_string(),
            kind: crate::llm::ProviderKind::LocalLlamaCpp,
            name: "Local".to_string(),
            enabled: true,
            scope: crate::llm::task::policy::ProviderScope::LocalManaged,
            endpoint: Some("http://127.0.0.1:8010".to_string()),
            auth: None,
        }
    }

    fn chat_model() -> ProviderModel {
        ProviderModel {
            id: "local:qwen".to_string(),
            provider_id: "local".to_string(),
            remote_id: "qwen2".to_string(),
            display_name: "Qwen".to_string(),
            capabilities: vec![Capability::Chat],
            capability_source: Some(CapabilitySource::Manual),
            context_length: None,
            dimensions: None,
            pricing: None,
            source: ModelSource::Manual,
        }
    }

    #[test]
    fn endpoint_layout_detection() {
        assert_eq!(detect_layout("https://api.example.com"), EndpointLayout::Root);
        assert_eq!(detect_layout("https://api.example.com/"), EndpointLayout::Root);
        assert_eq!(detect_layout("https://api.example.com/v1"), EndpointLayout::OpenAiV1);
        assert_eq!(detect_layout("https://api.example.com/v1/"), EndpointLayout::OpenAiV1);
        assert_eq!(
            detect_layout("https://host.example.com/custom"),
            EndpointLayout::Custom
        );
    }

    #[test]
    fn endpoint_join_matrix() {
        assert_eq!(
            join_api_path("https://api.example.com", "/v1/chat/completions").unwrap(),
            "https://api.example.com/v1/chat/completions"
        );
        assert_eq!(
            join_api_path("https://api.example.com/v1", "/v1/chat/completions").unwrap(),
            "https://api.example.com/v1/chat/completions"
        );
        assert_eq!(
            join_api_path("http://127.0.0.1:8010/", "/v1/embeddings").unwrap(),
            "http://127.0.0.1:8010/v1/embeddings"
        );
        assert_eq!(
            join_api_path("https://api.example.com/v1/", "/v1/models").unwrap(),
            "https://api.example.com/v1/models"
        );
        assert_eq!(
            join_api_path("https://host.example.com/custom", "/v1/chat/completions").unwrap(),
            "https://host.example.com/custom/v1/chat/completions"
        );
        assert!(join_api_path("", "/v1/models").is_err());
        assert!(join_api_path("https://x.example.com", "v1/models").is_err());
    }

    #[test]
    fn chat_body_uses_remote_id_only() {
        let m = chat_model();
        let req = ChatRequest {
            messages: vec![ChatMessage {
                role: ChatRole::User,
                content: "hi".to_string(),
            }],
            temperature: Some(0.2),
            top_p: None,
            max_tokens: Some(64),
            response_format: None,
        };
        let body = build_chat_body(&m, &req).unwrap();
        assert_eq!(body["model"], "qwen2");
        assert!(!body.to_string().contains("local:qwen"), "internal id never sent");
        let t = body["temperature"].as_f64().unwrap();
        assert!((t - 0.2).abs() < 1e-6, "{}", t);
        assert!(body.get("top_p").is_none(), "None fields omitted, no null");
        assert_eq!(body["max_tokens"], 64);
        assert!(body.get("response_format").is_none());
        assert!(body.get("stream").is_none());
        assert!(body.get("tools").is_none());
    }

    #[test]
    fn response_format_gated_by_capability() {
        let m = chat_model();
        let req = ChatRequest {
            messages: vec![ChatMessage {
                role: ChatRole::User,
                content: "hi".to_string(),
            }],
            temperature: None,
            top_p: None,
            max_tokens: None,
            response_format: Some(ResponseFormat {
                kind: "json_object".to_string(),
            }),
        };
        assert!(build_chat_body(&m, &req).is_err());
        let mut m2 = m;
        m2.capabilities.push(Capability::StructuredOutput);
        let body = build_chat_body(&m2, &req).unwrap();
        assert_eq!(body["response_format"]["type"], "json_object");
    }

    #[test]
    fn chat_body_rejects_empty_and_nonfinite() {
        let m = chat_model();
        assert!(build_chat_body(&m, &ChatRequest::default()).is_err());
        let bad = ChatRequest {
            messages: vec![ChatMessage {
                role: ChatRole::User,
                content: "hi".to_string(),
            }],
            temperature: Some(f32::NAN),
            ..Default::default()
        };
        assert!(build_chat_body(&m, &bad).is_err());
    }

    #[test]
    fn error_mapping_matrix() {
        assert_eq!(map_http_status(400, "bad").code(), "invalid_request");
        assert_eq!(map_http_status(401, "x").code(), "unauthorized");
        assert_eq!(map_http_status(403, "x").code(), "unauthorized");
        assert_eq!(map_http_status(404, "x").code(), "not_found");
        assert_eq!(map_http_status(408, "x").code(), "timeout");
        assert_eq!(map_http_status(429, "x").code(), "rate_limited");
        assert!(matches!(
            map_http_status(500, "x"),
            ProviderError::Server { status: 500, .. }
        ));
        assert!(!map_http_status(401, "x").is_retryable());
        assert!(map_http_status(429, "x").is_retryable());
        assert!(!ProviderError::Cancelled.is_retryable(), "cancelled never retryable");
    }

    #[test]
    fn error_body_capped_and_redacted() {
        let big = format!("Authorization: Bearer sk-secret {}\n", "y".repeat(9000));
        let err = map_http_status(500, &big);
        let s = format!("{:?}", err);
        assert!(s.len() < 9000);
        assert!(!s.contains("sk-secret"));
    }

    #[test]
    fn adapter_rejects_wrong_provider_model() {
        let a = OpenAiCompatibleAdapter::new(
            local_conn(),
            Arc::new(NoSecretStore),
            Duration::from_secs(5),
            vec![Capability::Chat],
            CapabilitySource::Manual,
        )
        .unwrap();
        let mut m = chat_model();
        m.provider_id = "other".to_string();
        assert!(a.check_model(&m).is_err());
        m.provider_id = "local".to_string();
        m.remote_id = String::new();
        assert!(a.check_model(&m).is_err());
    }
}
