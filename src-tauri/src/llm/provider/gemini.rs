//! Stage 3.2 (v0.5.9): native Gemini adapter — `generateContent` only.
//!
//! Auth uses the `x-goog-api-key` header (provider-approved scheme), never a
//! `?key=` query parameter, so full URLs are safe to handle. `embedContent`
//! has a different API shape and stays a separate future stage (3.2b).

use super::openai::{
    join_api_path, map_http_status_for, map_reqwest_err_for, race_with, truncate_capped,
    ChatMessage, ChatRequest, ChatResponse, ChatRole, ChatUsage, HealthReport, SecretStore,
};
use super::types::{AuthReference, ProviderConnection, ProviderError, ProviderModel};
use crate::llm::embeddings::provider::redact_secrets;
use crate::llm::task::policy::ProviderScope;
use crate::llm::{Capability, CapabilitySource, ProviderKind};
use crate::llm::models::types::ModelSource;
use async_trait::async_trait;
use std::sync::Arc;
use std::time::Duration;
use tokio_util::sync::CancellationToken;

/// Mask a bare `AIza…` key without regex and without loops that can hang:
/// single pass, cut at the first delimiter.
fn mask_aiza(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    loop {
        match rest.find("AIza") {
            None => {
                out.push_str(rest);
                break;
            }
            Some(i) => {
                out.push_str(&rest[..i]);
                let after = &rest[i + 4..];
                let end = after
                    .find(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '-'))
                    .map(|e| i + 4 + e)
                    .unwrap_or(rest.len());
                out.push_str("***REDACTED***");
                rest = &rest[end..];
            }
        }
    }
    out
}

fn redact_gemini(s: &str) -> String {
    mask_aiza(&redact_secrets(s))
}

// ---------------------------------------------------------------------------
// Native Gemini adapter (generateContent)
// ---------------------------------------------------------------------------

pub struct GeminiNativeAdapter {
    connection: ProviderConnection,
    secrets: Arc<dyn SecretStore>,
    http: reqwest::Client,
    timeout: Duration,
}

/// Manual `Debug`: no transient secrets stored.
impl std::fmt::Debug for GeminiNativeAdapter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("GeminiNativeAdapter")
            .field("connection", &self.connection)
            .field("timeout", &self.timeout)
            .finish()
    }
}

impl GeminiNativeAdapter {
    pub fn new(
        connection: ProviderConnection,
        secrets: Arc<dyn SecretStore>,
        timeout: Duration,
    ) -> Result<Self, ProviderError> {
        connection.validate()?;
        if connection.kind != ProviderKind::Gemini {
            return Err(ProviderError::InvalidConfig(format!(
                "native Gemini adapter requires kind Gemini, got {:?}",
                connection.kind
            )));
        }
        if connection.scope != ProviderScope::Cloud {
            return Err(ProviderError::InvalidConfig(format!(
                "native Gemini adapter requires Cloud scope, got {:?}",
                connection.scope
            )));
        }
        let http = reqwest::Client::builder()
            .timeout(timeout + Duration::from_secs(5))
            .build()
            .map_err(|e| ProviderError::InvalidConfig(e.to_string()))?;
        Ok(Self {
            connection,
            secrets,
            http,
            timeout,
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

    /// API key strictly from keyring, sent only as `x-goog-api-key`.
    fn resolve_key(&self, cancel: &CancellationToken) -> Result<String, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        match &self.connection.auth {
            None => Err(ProviderError::CredentialsMissing(format!(
                "provider {} has no auth configured",
                self.connection.id
            ))),
            Some(AuthReference { secret_ref }) => {
                let key = self.secrets.get(secret_ref)?;
                if key.trim().is_empty() {
                    return Err(ProviderError::CredentialsMissing(format!(
                        "empty credential for provider {}",
                        self.connection.id
                    )));
                }
                Ok(key)
            }
        }
    }

    fn check_model(&self, model: &ProviderModel, cap: &Capability) -> Result<(), ProviderError> {
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
        if !model.supports(cap) {
            return Err(ProviderError::UnsupportedCapability(format!("{:?}", cap)));
        }
        Ok(())
    }

    fn model_path(&self, model: &ProviderModel) -> Result<String, ProviderError> {
        // Remote names are "models/<name>"; the URL needs the full path.
        // Validated strictly: a remote id must never reshape the URL path.
        let full = if model.remote_id.starts_with("models/") {
            model.remote_id.clone()
        } else {
            format!("models/{}", model.remote_id)
        };
        validate_remote_path(&full)
    }
}

/// Strict shape check for a model path used in URL construction:
/// exactly `models/<name>`, name charset `[A-Za-z0-9._-]`, no traversal.
fn validate_remote_path(full: &str) -> Result<String, ProviderError> {
    let name = full.strip_prefix("models/").ok_or_else(|| {
        ProviderError::InvalidConfig(format!("remote model path must start with models/: {}", full))
    })?;
    if name.is_empty() || name.len() > 128 {
        return Err(ProviderError::InvalidConfig(
            "remote model name is empty or too long".to_string(),
        ));
    }
    let ok = name
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.');
    if !ok {
        return Err(ProviderError::InvalidConfig(format!(
            "remote model name has illegal characters: {}",
            name
        )));
    }
    Ok(full.to_string())
}

// ---------------------------------------------------------------------------
// Embeddings (batchEmbedContent) — Stage 3.2b
// ---------------------------------------------------------------------------

/// embedContent task types. Exhaustive enum — no free-form strings.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GeminiTaskType {
    SemanticSimilarity,
    Classification,
    Clustering,
    RetrievalDocument,
    RetrievalQuery,
    QuestionAnswering,
    FactVerification,
    CodeRetrievalQuery,
}

impl GeminiTaskType {
    fn as_str(&self) -> &'static str {
        match self {
            Self::SemanticSimilarity => "SEMANTIC_SIMILARITY",
            Self::Classification => "CLASSIFICATION",
            Self::Clustering => "CLUSTERING",
            Self::RetrievalDocument => "RETRIEVAL_DOCUMENT",
            Self::RetrievalQuery => "RETRIEVAL_QUERY",
            Self::QuestionAnswering => "QUESTION_ANSWERING",
            Self::FactVerification => "FACT_VERIFICATION",
            Self::CodeRetrievalQuery => "CODE_RETRIEVAL_QUERY",
        }
    }
}

/// Optional embedding parameters. `None` = omitted from the request
/// (never `null`). A RAG-query default belongs to Stage 4, not here.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct GeminiEmbedParams {
    pub task_type: Option<GeminiTaskType>,
    pub output_dimensionality: Option<u32>,
}

impl GeminiEmbedParams {
    fn validate(&self) -> Result<(), ProviderError> {
        if let Some(0) = self.output_dimensionality {
            return Err(ProviderError::InvalidRequest(
                "outputDimensionality must be > 0".to_string(),
            ));
        }
        Ok(())
    }
}

/// Build the `generateContent` body.
///
/// Role mapping: system → `systemInstruction`, user → `user`,
/// assistant → `model`. Empty-text parts are dropped; a message left with
/// zero parts is rejected. `systemInstruction` is omitted when empty;
/// no OpenAI `messages`, no `max_tokens`, no `tools`.
pub fn build_gemini_body(
    request: &ChatRequest,
) -> Result<serde_json::Value, ProviderError> {
    if request.messages.is_empty() {
        return Err(ProviderError::InvalidRequest("messages is empty".to_string()));
    }
    for m in [&request.temperature, &request.top_p].into_iter().flatten() {
        if !m.is_finite() {
            return Err(ProviderError::InvalidRequest(
                "temperature/top_p must be finite".to_string(),
            ));
        }
    }
    if request.response_format.is_some() {
        return Err(ProviderError::UnsupportedCapability(
            "StructuredOutput (native Gemini responseMimeType is out of scope)".to_string(),
        ));
    }
    let mut system_parts: Vec<serde_json::Value> = Vec::new();
    let mut contents: Vec<serde_json::Value> = Vec::new();
    for m in &request.messages {
        // Preserve message order; drop empty text parts.
        let text = m.content.trim();
        if text.is_empty() {
            continue;
        }
        match m.role {
            ChatRole::System => {
                system_parts.push(serde_json::json!({"text": m.content}));
            }
            ChatRole::User => {
                contents.push(
                    serde_json::json!({"role": "user", "parts": [{"text": m.content}]}),
                );
            }
            ChatRole::Assistant => {
                contents.push(
                    serde_json::json!({"role": "model", "parts": [{"text": m.content}]}),
                );
            }
        }
    }
    if contents.is_empty() {
        return Err(ProviderError::InvalidRequest(
            "no non-empty user/assistant message".to_string(),
        ));
    }
    let mut body = serde_json::json!({"contents": contents});
    if !system_parts.is_empty() {
        body["systemInstruction"] = serde_json::json!({"parts": system_parts});
    }
    let mut gen_config = serde_json::Map::new();
    if let Some(t) = request.temperature {
        gen_config.insert("temperature".to_string(), serde_json::json!(t));
    }
    if let Some(p) = request.top_p {
        gen_config.insert("topP".to_string(), serde_json::json!(p));
    }
    if let Some(n) = request.max_tokens {
        gen_config.insert("maxOutputTokens".to_string(), serde_json::json!(n));
    }
    if !gen_config.is_empty() {
        body["generationConfig"] = serde_json::Value::Object(gen_config);
    }
    Ok(body)
}

fn parse_gemini_chat(body: &[u8]) -> Result<ChatResponse, ProviderError> {
    let json: serde_json::Value = serde_json::from_slice(body)
        .map_err(|e| ProviderError::MalformedResponse(redact_gemini(&e.to_string())))?;
    // Safety blocks surface without usable candidates.
    let blocked = json
        .get("promptFeedback")
        .and_then(|v| v.get("blockReason"))
        .and_then(|v| v.as_str())
        .filter(|s| *s != "BLOCK_REASON_UNSPECIFIED");
    if let Some(reason) = blocked {
        return Err(ProviderError::ContentBlocked(format!(
            "prompt blocked: {}",
            reason
        )));
    }
    let candidates = json.get("candidates").and_then(|v| v.as_array()).ok_or_else(|| {
        ProviderError::InvalidResponse("missing candidates".to_string())
    })?;
    if candidates.is_empty() {
        return Err(ProviderError::InvalidResponse("empty candidates".to_string()));
    }
    let first = &candidates[0];
    if let Some(reason) = first
        .get("finishReason")
        .and_then(|v| v.as_str())
        .filter(|s| *s == "SAFETY")
    {
        return Err(ProviderError::ContentBlocked(format!(
            "finish reason: {}",
            reason
        )));
    }
    let parts = first
        .get("content")
        .and_then(|v| v.get("parts"))
        .and_then(|v| v.as_array())
        .ok_or_else(|| ProviderError::InvalidResponse("missing content.parts".to_string()))?;
    // Concatenate text parts in order; skip non-text parts (function calls…).
    let mut text = String::new();
    for p in parts {
        if let Some(t) = p.get("text").and_then(|v| v.as_str()) {
            text.push_str(t);
        }
    }
    if text.trim().is_empty() {
        return Err(ProviderError::InvalidResponse(
            "no text in candidate parts".to_string(),
        ));
    }
    let finish = first
        .get("finishReason")
        .and_then(|v| v.as_str())
        .map(String::from);
    let usage = json.get("usageMetadata").map(|u| super::openai::ChatUsage {
        prompt_tokens: u.get("promptTokenCount").and_then(|v| v.as_u64()),
        completion_tokens: u.get("candidatesTokenCount").and_then(|v| v.as_u64()),
        total_tokens: u.get("totalTokenCount").and_then(|v| v.as_u64()),
    });
    Ok(ChatResponse {
        content: text,
        model: String::new(), // generateContent echoes no model id; caller knows remote_id
        finish_reason: finish,
        usage,
    })
}

/// Explicit mapping of Gemini `supportedGenerationMethods` → capabilities.
/// Unknown methods are ignored, never guessed.
pub fn capabilities_from_methods(methods: &[String]) -> Vec<Capability> {
    let mut out = Vec::new();
    for m in methods {
        match m.as_str() {
            "generateContent" => {
                if !out.contains(&Capability::Chat) {
                    out.push(Capability::Chat);
                }
            }
            "embedContent" => {
                if !out.contains(&Capability::Embeddings) {
                    out.push(Capability::Embeddings);
                }
            }
            _ => {}
        }
    }
    out
}

fn parse_models_json(
    connection_id: &str,
    bytes: &[u8],
) -> Result<Vec<ProviderModel>, ProviderError> {
    let json: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|e| ProviderError::MalformedResponse(redact_gemini(&e.to_string())))?;
    let data = json.get("models").and_then(|v| v.as_array()).ok_or_else(|| {
        ProviderError::MalformedResponse("missing models array".to_string())
    })?;
    let mut out = Vec::with_capacity(data.len());
    for item in data {
        let name = item
            .get("name")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ProviderError::MalformedResponse("model entry missing name".to_string()))?;
        if name.trim().is_empty() {
            return Err(ProviderError::MalformedResponse("empty remote model name".to_string()));
        }
        let display = item
            .get("displayName")
            .and_then(|v| v.as_str())
            .unwrap_or(name)
            .to_string();
        let methods: Vec<String> = item
            .get("supportedGenerationMethods")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        let caps = capabilities_from_methods(&methods);
        out.push(ProviderModel {
            id: format!("{}:{}", connection_id, name),
            provider_id: connection_id.to_string(),
            remote_id: name.to_string(),
            display_name: display,
            capabilities: caps,
            capability_source: Some(CapabilitySource::Probed),
            context_length: None,
            dimensions: None,
            pricing: None,
            source: ModelSource::Manual,
        });
    }
    Ok(out)
}

#[async_trait]
impl super::openai::ProviderAdapter for GeminiNativeAdapter {
    async fn health(&self, cancel: CancellationToken) -> Result<HealthReport, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        let t0 = std::time::Instant::now();
        let out = race_with(
            self.http.clone(),
            self.timeout,
            cancel.clone(),
            |client| async move {
                let url = join_api_path(&base, "/v1beta/models")?;
                let resp = client
                    .get(&url)
                    .header("x-goog-api-key", key)
                    .send()
                    .await
                    .map_err(map_reqwest_err_for)?;
                if cancel.is_cancelled() {
                    return Err(ProviderError::Cancelled);
                }
                match resp.status().as_u16() {
                    200..=299 => Ok((true, Some(true))),
                    401 | 403 => Ok((true, Some(false))),
                    s => {
                        let b = resp.text().await.unwrap_or_default();
                        Err(map_http_status_for(s, &b))
                    }
                }
            },
        )
        .await?;
        let (reachable, authenticated) = out;
        Ok(HealthReport {
            reachable,
            authenticated,
            latency_ms: t0.elapsed().as_millis() as u64,
            capabilities: vec![Capability::Chat],
            capability_source: CapabilitySource::StaticProvider,
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
        race_with(
            self.http.clone(),
            self.timeout,
            cancel.clone(),
            |client| async move {
                let url = join_api_path(&base, "/v1beta/models")?;
                let resp = client
                    .get(&url)
                    .header("x-goog-api-key", key)
                    .send()
                    .await
                    .map_err(map_reqwest_err_for)?;
                if cancel.is_cancelled() {
                    return Err(ProviderError::Cancelled);
                }
                let status = resp.status().as_u16();
                if !resp.status().is_success() {
                    let b = resp.text().await.unwrap_or_default();
                    return Err(map_http_status_for(status, &b));
                }
                let bytes = resp.bytes().await.map_err(map_reqwest_err_for)?;
                parse_models_json(&self.connection.id, &truncate_capped(&bytes)?)
            },
        )
        .await
    }

    async fn chat(
        &self,
        model: &ProviderModel,
        request: super::openai::ChatRequest,
        cancel: CancellationToken,
    ) -> Result<ChatResponse, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        self.check_model(model, &Capability::Chat)?;
        let body = build_gemini_body(&request)?;
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        let model_path = self.model_path(model)?;
        let path = format!("/v1beta/{}:generateContent", model_path);
        race_with(
            self.http.clone(),
            self.timeout,
            cancel.clone(),
            |client| async move {
                let url = join_api_path(&base, &path)?;
                let resp = client
                    .post(&url)
                    .header("Content-Type", "application/json")
                    .header("x-goog-api-key", key)
                    .json(&body)
                    .send()
                    .await
                    .map_err(map_reqwest_err_for)?;
                if cancel.is_cancelled() {
                    return Err(ProviderError::Cancelled);
                }
                let status = resp.status().as_u16();
                if !resp.status().is_success() {
                    let b = resp.text().await.unwrap_or_default();
                    return Err(map_http_status_for(status, &b));
                }
                let bytes = resp.bytes().await.map_err(map_reqwest_err_for)?;
                parse_gemini_chat(truncate_capped(&bytes)?)
            },
        )
        .await
    }

    async fn embed(
        &self,
        model: &ProviderModel,
        request: crate::llm::embeddings::types::EmbeddingRequest,
        cancel: CancellationToken,
    ) -> Result<crate::llm::embeddings::types::EmbeddingResponse, ProviderError> {
        self.embed_content(
            model,
            request.inputs.clone(),
            GeminiEmbedParams::default(),
            cancel,
        )
        .await
    }
}

impl GeminiNativeAdapter {
    /// Native `batchEmbedContent` for 1..N inputs through one request shape.
    /// Positional mapping: `embeddings[i]` belongs to `inputs[i]`.
    pub async fn embed_content(
        &self,
        model: &ProviderModel,
        inputs: Vec<String>,
        params: GeminiEmbedParams,
        cancel: CancellationToken,
    ) -> Result<crate::llm::embeddings::types::EmbeddingResponse, ProviderError> {
        use crate::llm::embeddings::types::{EmbeddingLimits, EmbeddingRequest, EmbeddingResponse};
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        self.check_model(model, &Capability::Embeddings)?;
        params.validate()?;
        if inputs.is_empty() || inputs.iter().any(|s| s.trim().is_empty()) {
            return Err(ProviderError::InvalidRequest(
                "inputs is empty or contains empty string".to_string(),
            ));
        }
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        let model_path = self.model_path(model)?;
        let url_path = format!("/v1beta/{}:batchEmbedContent", model_path);
        let requests: Vec<serde_json::Value> = inputs
            .iter()
            .map(|text| {
                let mut r = serde_json::json!({
                    "model": model.remote_id,
                    "content": {"parts": [{"text": text}]},
                });
                if let Some(t) = params.task_type {
                    r["taskType"] = serde_json::json!(t.as_str());
                }
                if let Some(d) = params.output_dimensionality {
                    r["outputDimensionality"] = serde_json::json!(d);
                }
                r
            })
            .collect();
        let body = serde_json::json!({"requests": requests});
        let limits = EmbeddingLimits::default();
        let inputs_for_req = inputs.clone();
        let vectors = race_with(
            self.http.clone(),
            self.timeout,
            cancel.clone(),
            |client| async move {
                let url = join_api_path(&base, &url_path)?;
                let resp = client
                    .post(&url)
                    .header("Content-Type", "application/json")
                    .header("x-goog-api-key", key)
                    .json(&body)
                    .send()
                    .await
                    .map_err(map_reqwest_err_for)?;
                if cancel.is_cancelled() {
                    return Err(ProviderError::Cancelled);
                }
                let status = resp.status().as_u16();
                if !resp.status().is_success() {
                    let b = resp.text().await.unwrap_or_default();
                    return Err(map_http_status_for(status, &b));
                }
                let bytes = resp.bytes().await.map_err(map_reqwest_err_for)?;
                parse_embed_content(&truncate_capped(&bytes)?, inputs.len())
            },
        )
        .await?;
        // Requested dimensionality must hold for every returned vector.
        if let Some(d) = params.output_dimensionality {
            if vectors.iter().any(|v| v.len() != d as usize) {
                return Err(ProviderError::InvalidResponse(format!(
                    "requested dimensionality {} not met",
                    d
                )));
            }
        }
        let dimensions = vectors.first().map(Vec::len).unwrap_or(0);
        let app_request = EmbeddingRequest::new(model.id.clone(), inputs_for_req);
        let response = EmbeddingResponse {
            model_id: model.id.clone(),
            dimensions,
            vectors,
        };
        response
            .validate_for(&app_request, &limits)
            .map_err(|e| ProviderError::InvalidResponse(format!("{:?}", e)))?;
        Ok(response)
    }
}

/// Transport parse for `batchEmbedContent`: structure + positional mapping.
/// Non-empty/finite/dimensions are re-checked by `validate_for` downstream.
fn parse_embed_content(
    bytes: &[u8],
    expected: usize,
) -> Result<Vec<Vec<f32>>, ProviderError> {
    let json: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|e| ProviderError::MalformedResponse(redact_gemini(&e.to_string())))?;
    let items = json.get("embeddings").and_then(|v| v.as_array()).ok_or_else(|| {
        ProviderError::MalformedResponse("missing embeddings array".to_string())
    })?;
    if items.len() != expected {
        return Err(ProviderError::MalformedResponse(format!(
            "count mismatch: expected {} got {}",
            expected,
            items.len()
        )));
    }
    if items.is_empty() {
        return Err(ProviderError::MalformedResponse("empty embeddings".to_string()));
    }
    let mut out = Vec::with_capacity(items.len());
    for (i, item) in items.iter().enumerate() {
        let values = item
            .get("embedding")
            .and_then(|v| v.get("values"))
            .and_then(|v| v.as_array())
            .ok_or_else(|| {
                ProviderError::MalformedResponse(format!("missing embedding.values at {}", i))
            })?;
        if values.is_empty() {
            return Err(ProviderError::MalformedResponse(format!(
                "empty values at {}",
                i
            )));
        }
        let mut vec = Vec::with_capacity(values.len());
        for val in values {
            let f = val.as_f64().ok_or_else(|| {
                ProviderError::MalformedResponse(format!("non-number at {}", i))
            })? as f32;
            if !f.is_finite() {
                return Err(ProviderError::MalformedResponse(format!(
                    "non-finite value at {}",
                    i
                )));
            }
        vec.push(f);
        }
        out.push(vec);
    }
    Ok(out)
}

/// [`crate::llm::embeddings::provider::EmbeddingProvider`] bridge bound to
/// one catalog model (remote_id known — never guessed).
pub struct GeminiEmbedBridge<'a> {
    pub adapter: &'a GeminiNativeAdapter,
    pub model: ProviderModel,
    pub params: GeminiEmbedParams,
}

#[async_trait::async_trait]
impl<'a> crate::llm::embeddings::provider::EmbeddingProvider for GeminiEmbedBridge<'a> {
    async fn embed(
        &self,
        request: crate::llm::embeddings::types::EmbeddingRequest,
        cancel: CancellationToken,
    ) -> Result<
        crate::llm::embeddings::types::EmbeddingResponse,
        crate::llm::embeddings::types::EmbeddingError,
    > {
        use crate::llm::embeddings::types::EmbeddingError;
        self.adapter
            .embed_content(&self.model, request.inputs.clone(), self.params.clone(), cancel)
            .await
            .map_err(|e| match e {
                ProviderError::Cancelled => EmbeddingError::Cancelled,
                ProviderError::Timeout(m) => EmbeddingError::Timeout(m),
                other => EmbeddingError::Provider(format!("{:?}", other)),
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::embeddings::types::EmbeddingRequest;

    fn conn(port: u16) -> ProviderConnection {
        ProviderConnection {
            id: "gemini".to_string(),
            kind: ProviderKind::Gemini,
            name: "Gemini".to_string(),
            enabled: true,
            scope: ProviderScope::Cloud,
            endpoint: Some(format!("http://127.0.0.1:{}", port)),
            auth: Some(AuthReference::new("keychain://fragile-notes/gem".to_string()).unwrap()),
        }
    }

    fn secrets() -> Arc<dyn SecretStore> {
        struct Map;
        impl SecretStore for Map {
            fn get(&self, r: &str) -> Result<String, ProviderError> {
                assert_eq!(r, "keychain://fragile-notes/gem");
                Ok("AIza-test-key-12345".to_string())
            }
        }
        Arc::new(Map)
    }

    fn model() -> ProviderModel {
        ProviderModel {
            id: "gemini:models/gemini-2.0-flash".to_string(),
            provider_id: "gemini".to_string(),
            remote_id: "models/gemini-2.0-flash".to_string(),
            display_name: "Gemini 2.0 Flash".to_string(),
            capabilities: vec![Capability::Chat],
            capability_source: Some(CapabilitySource::Manual),
            context_length: None,
            dimensions: None,
            pricing: None,
            source: ModelSource::Manual,
        }
    }

    fn adapter_for(port: u16, timeout: Duration) -> GeminiNativeAdapter {
        GeminiNativeAdapter::new(conn(port), secrets(), timeout).unwrap()
    }

    fn req() -> ChatRequest {
        ChatRequest {
            messages: vec![
                ChatMessage {
                    role: ChatRole::System,
                    content: "sys".to_string(),
                },
                ChatMessage {
                    role: ChatRole::User,
                    content: "hi".to_string(),
                },
                ChatMessage {
                    role: ChatRole::Assistant,
                    content: "yo".to_string(),
                },
            ],
            temperature: Some(0.7),
            top_p: Some(0.9),
            max_tokens: Some(2048),
            response_format: None,
        }
    }

    // -- mock server (mirrors tests_transport wire format) --

    async fn read_request(stream: &mut tokio::net::TcpStream) -> String {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let mut buf = vec![0u8; 65536];
        let mut acc = Vec::new();
        loop {
            let n = tokio::time::timeout(Duration::from_secs(5), stream.read(&mut buf))
                .await
                .unwrap()
                .unwrap();
            if n == 0 {
                break;
            }
            acc.extend_from_slice(&buf[..n]);
            if acc.windows(4).any(|w| w == b"\r\n\r\n") {
                break;
            }
        }
        String::from_utf8_lossy(&acc).to_string()
    }

    fn respond(status: u16, body: &str) -> Vec<u8> {
        let reason = match status {
            200 => "OK",
            400 => "Bad Request",
            401 => "Unauthorized",
            403 => "Forbidden",
            404 => "Not Found",
            429 => "Too Many Requests",
            500 => "Internal Server Error",
            _ => "Error",
        };
        format!(
            "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            status, reason, body.len(), body
        )
        .into_bytes()
    }

    struct Wire {
        port: u16,
        captured: Arc<std::sync::Mutex<Vec<String>>>,
    }

    async fn spawn_wire(status: u16, body: String, delay_ms: u64) -> Wire {
        use tokio::io::AsyncWriteExt;
        let captured = Arc::new(std::sync::Mutex::new(Vec::new()));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let c2 = captured.clone();
        tokio::spawn(async move {
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    break;
                };
                let c3 = c2.clone();
                let body = body.clone();
                tokio::spawn(async move {
                    let raw = read_request(&mut stream).await;
                    c3.lock().unwrap().push(raw);
                    if delay_ms > 0 {
                        tokio::time::sleep(Duration::from_millis(delay_ms)).await;
                    }
                    let _ = stream.write_all(&respond(status, &body)).await;
                });
            }
        });
        Wire { port, captured }
    }

    fn chat_ok() -> String {
        r#"{"candidates":[{"content":{"role":"model","parts":[{"text":"he"},{"text":"llo"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":20,"totalTokenCount":30}}"#.to_string()
    }

    // -- body mapping --

    #[test]
    fn body_mapping() {
        let body = build_gemini_body(&req()).unwrap();
        assert_eq!(body["contents"][0]["role"], "user");
        assert_eq!(body["contents"][1]["role"], "model");
        assert_eq!(body["systemInstruction"]["parts"][0]["text"], "sys");
        let t = body["generationConfig"]["temperature"].as_f64().unwrap();
        assert!((t - 0.7).abs() < 1e-6);
        let p = body["generationConfig"]["topP"].as_f64().unwrap();
        assert!((p - 0.9).abs() < 1e-6);
        assert_eq!(body["generationConfig"]["maxOutputTokens"], 2048);
        assert!(body.get("messages").is_none(), "no OpenAI messages");
        assert!(body.get("max_tokens").is_none(), "no max_tokens");
        assert!(body.get("tools").is_none());
        assert!(!body.to_string().contains("gemini:models"));
    }

    #[test]
    fn null_fields_and_empty_system_omitted() {
        let req = ChatRequest {
            messages: vec![ChatMessage {
                role: ChatRole::User,
                content: "hi".to_string(),
            }],
            ..Default::default()
        };
        let body = build_gemini_body(&req).unwrap();
        assert!(body.get("systemInstruction").is_none());
        assert!(body.get("generationConfig").is_none());
        assert!(!body.to_string().contains("null"));
    }

    #[test]
    fn empty_parts_rejected() {
        let req = ChatRequest {
            messages: vec![ChatMessage {
                role: ChatRole::User,
                content: "   ".to_string(),
            }],
            ..Default::default()
        };
        assert!(build_gemini_body(&req).is_err());
        assert!(build_gemini_body(&ChatRequest::default()).is_err());
    }

    #[test]
    fn model_mapping_uses_remote_only() {
        let m = spawn_path_capture();
        assert!(m.contains("models/gemini-2.0-flash"));
    }

    fn spawn_path_capture() -> String {
        // Pure check: model_path keeps the full "models/..." remote name.
        let a = GeminiNativeAdapter::new(conn(9), secrets(), Duration::from_secs(1)).unwrap();
        a.model_path(&model()).unwrap()
    }

    // -- response parsing --

    #[test]
    fn candidates_concatenated_in_order() {
        let r = parse_gemini_chat(chat_ok().as_bytes()).unwrap();
        assert_eq!(r.content, "hello");
        assert_eq!(r.finish_reason.as_deref(), Some("STOP"));
        let u = r.usage.unwrap();
        assert_eq!(u.prompt_tokens, Some(10));
        assert_eq!(u.completion_tokens, Some(20));
        assert_eq!(u.total_tokens, Some(30));
    }

    #[test]
    fn blocked_and_empty_candidates() {
        let blocked = r#"{"promptFeedback":{"blockReason":"SAFETY"},"candidates":[]}"#;
        assert_eq!(
            parse_gemini_chat(blocked.as_bytes()).unwrap_err().code(),
            "content_blocked"
        );
        let safety = r#"{"candidates":[{"content":{"role":"model","parts":[{"text":"x"}]},"finishReason":"SAFETY"}]}"#;
        assert_eq!(
            parse_gemini_chat(safety.as_bytes()).unwrap_err().code(),
            "content_blocked"
        );
        let empty = r#"{"candidates":[]}"#;
        assert_eq!(
            parse_gemini_chat(empty.as_bytes()).unwrap_err().code(),
            "invalid_response"
        );
        let no_text = r#"{"candidates":[{"content":{"role":"model","parts":[{"functionCall":{"name":"f"}}]},"finishReason":"STOP"}]}"#;
        assert_eq!(
            parse_gemini_chat(no_text.as_bytes()).unwrap_err().code(),
            "invalid_response"
        );
        assert!(parse_gemini_chat(b"not json").unwrap_err().code() == "malformed_response");
    }

    // -- catalog --

    #[test]
    fn capabilities_from_methods_explicit() {
        let caps = capabilities_from_methods(
            &["generateContent".to_string(), "embedContent".to_string(), "weirdFuture".to_string()],
        );
        assert!(caps.contains(&Capability::Chat));
        assert!(caps.contains(&Capability::Embeddings));
        assert_eq!(caps.len(), 2, "unknown methods ignored, never guessed");
        assert!(capabilities_from_methods(&[]).is_empty());
    }

    // -- transport --

    #[tokio::test]
    async fn chat_roundtrip_header_auth() {
        use super::super::openai::ProviderAdapter;
        let m = spawn_wire(200, chat_ok(), 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        let r = a
            .chat(&model(), req(), CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(r.content, "hello");
        let raw = m.captured.lock().unwrap().join("\n");
        let lower = raw.to_lowercase();
        // Key only in the approved header, never in URL or body.
        assert_eq!(lower.matches("x-goog-api-key: aiza-test-key-12345").count(), 1);
        assert!(!raw.contains("?key="), "no query-key auth");
        assert!(!raw.contains("gemini:models"), "no internal id on wire");
        assert!(raw.contains("/v1beta/models/gemini-2.0-flash:generateContent"));
    }

    #[tokio::test]
    async fn missing_key_no_network() {
        use super::super::openai::ProviderAdapter;
        struct Empty;
        impl SecretStore for Empty {
            fn get(&self, _: &str) -> Result<String, ProviderError> {
                Err(ProviderError::KeyringUnavailable("locked".to_string()))
            }
        }
        let a =
            GeminiNativeAdapter::new(conn(9), Arc::new(Empty), Duration::from_secs(2)).unwrap();
        let err = a
            .chat(&model(), req(), CancellationToken::new())
            .await
            .unwrap_err();
        assert_eq!(err.code(), "keyring_unavailable");
    }

    #[tokio::test]
    async fn error_matrix() {
        use super::super::openai::ProviderAdapter;
        for (status, code) in [
            (400u16, "invalid_request"),
            (401, "unauthorized"),
            (403, "unauthorized"),
            (404, "not_found"),
            (429, "rate_limited"),
            (500, "server_error"),
        ] {
            let m = spawn_wire(status, r#"{"error":{"message":"e"}}"#.to_string(), 0).await;
            let a = adapter_for(m.port, Duration::from_secs(5));
            let err = a
                .chat(&model(), req(), CancellationToken::new())
                .await
                .unwrap_err();
            assert_eq!(err.code(), code, "status {}", status);
            assert!(!redact_gemini(&format!("{:?}", err)).contains("test-key-12345"));
        }
    }

    #[tokio::test]
    async fn timeout_and_cancel() {
        use super::super::openai::ProviderAdapter;
        let m = spawn_wire(200, chat_ok(), 2000).await;
        let a = adapter_for(m.port, Duration::from_millis(200));
        assert_eq!(
            a.chat(&model(), req(), CancellationToken::new())
                .await
                .unwrap_err()
                .code(),
            "timeout"
        );
        let cancel = CancellationToken::new();
        cancel.cancel();
        assert_eq!(
            a.chat(&model(), req(), cancel)
                .await
                .unwrap_err()
                .code(),
            "cancelled"
        );
    }

    #[tokio::test]
    async fn list_models_deterministic() {
        use super::super::openai::ProviderAdapter;
        let body = r#"{"models":[{"name":"models/gemini-2.0-flash","displayName":"Gemini 2.0 Flash","supportedGenerationMethods":["generateContent","embedContent","mystery"]},{"name":"models/other","displayName":"Other"}]}"#.to_string();
        let m = spawn_wire(200, body, 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        let first = a.list_models(CancellationToken::new()).await.unwrap();
        let second = a.list_models(CancellationToken::new()).await.unwrap();
        assert_eq!(first, second);
        assert_eq!(first[0].id, "gemini:models/gemini-2.0-flash");
        assert_eq!(first[0].remote_id, "models/gemini-2.0-flash");
        assert_eq!(first[0].display_name, "Gemini 2.0 Flash");
        assert!(first[0].capabilities.contains(&Capability::Chat));
        assert!(first[0].capabilities.contains(&Capability::Embeddings));
        assert_eq!(first[1].capabilities.len(), 0, "no methods → no caps");
        use crate::llm::CapabilitySource as CS;
        assert!(matches!(
            first[0].capability_source,
            Some(CS::Probed)
        ));
    }

    #[tokio::test]
    async fn health_reports() {
        use super::super::openai::ProviderAdapter;
        let m = spawn_wire(200, r#"{"models":[]}"#.to_string(), 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        let h = a.health(CancellationToken::new()).await.unwrap();
        assert!(h.reachable);
        assert_eq!(h.authenticated, Some(true));
        let m2 = spawn_wire(401, "no".to_string(), 0).await;
        let a2 = adapter_for(m2.port, Duration::from_secs(5));
        assert_eq!(
            a2.health(CancellationToken::new()).await.unwrap().authenticated,
            Some(false)
        );
    }

    #[tokio::test]
    async fn embed_unsupported_in_chat_stage() {
        use super::super::openai::ProviderAdapter;
        let m = spawn_wire(200, "{}".to_string(), 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        let req = EmbeddingRequest::new("x".to_string(), vec!["a".to_string()]);
        assert_eq!(
            a.embed(&model(), req, CancellationToken::new())
                .await
                .unwrap_err()
                .code(),
            "unsupported_capability"
        );
    }

    #[test]
    fn query_key_redaction_terminates() {
        // Regression: redactor must terminate on repeated ?key= patterns
        // and mask Aiza keys without loops.
        let evil = "https://x.example.com/?key=AIzaAAA111&next=1?key=AIzaBBB222 end".to_string();
        let out = redact_gemini(&evil);
        assert!(!out.contains("AIzaAAA111"));
        assert!(!out.contains("AIzaBBB222"));
        assert!(out.contains("?key="), "prefix kept, value masked");
        let bare = "key=AIzaXYZ_123-abc next";
        assert!(!redact_gemini(bare).contains("AIzaXYZ_123-abc"));
    }

    #[test]
    fn debug_hides_key() {
        let a = adapter_for(9, Duration::from_secs(1));
        assert!(!format!("{:?}", a).contains("test-key-12345"));
        assert!(!format!("{:?}", a).contains("keychain://"));
    }

    #[test]
    fn v1beta_base_no_doubling() {
        // BLOCKED-gate regression: a base already ending in /v1beta must not
        // produce /v1beta/v1beta. Longest-prefix dedup in join_api_path.
        use super::super::openai::join_api_path;
        assert_eq!(
            join_api_path(
                "https://host.example.com/v1beta",
                "/v1beta/models/gemini-embedding-001:batchEmbedContent"
            )
            .unwrap(),
            "https://host.example.com/v1beta/models/gemini-embedding-001:batchEmbedContent"
        );
        // Bare host unchanged.
        assert_eq!(
            join_api_path(
                "https://host.example.com",
                "/v1beta/models/gemini-embedding-001:batchEmbedContent"
            )
            .unwrap(),
            "https://host.example.com/v1beta/models/gemini-embedding-001:batchEmbedContent"
        );
    }

    // -- embedContent (Stage 3.2b) --

    fn emb_model() -> ProviderModel {
        ProviderModel {
            id: "gemini:models/gemini-embedding-001".to_string(),
            provider_id: "gemini".to_string(),
            remote_id: "models/gemini-embedding-001".to_string(),
            display_name: "Gemini Embedding".to_string(),
            capabilities: vec![Capability::Embeddings],
            capability_source: Some(CapabilitySource::Manual),
            context_length: None,
            dimensions: None,
            pricing: None,
            source: ModelSource::Manual,
        }
    }

    fn emb_ok_2() -> String {
        r#"{"embeddings":[{"embedding":{"values":[0.1,0.2]}},{"embedding":{"values":[0.3,0.4]}}]}"#.to_string()
    }

    #[test]
    fn remote_path_validation() {
        let a = adapter_for(9, Duration::from_secs(1));
        assert!(a.model_path(&model()).is_ok());
        let mut bad = model();
        for remote in [
            "models/evil/../x",
            "models/a/b",
            "models/",
            "models/has space",
            "models/ctrl\x01x",
            "models/semicolon;x",
            "other/gemini-2.0-flash",
            "",
        ] {
            bad.remote_id = remote.to_string();
            assert!(a.model_path(&bad).is_err(), "must reject {:?}", remote);
        }
    }

    #[test]
    fn task_type_exhaustive_mapping() {
        use GeminiTaskType::*;
        let pairs = [
            (SemanticSimilarity, "SEMANTIC_SIMILARITY"),
            (Classification, "CLASSIFICATION"),
            (Clustering, "CLUSTERING"),
            (RetrievalDocument, "RETRIEVAL_DOCUMENT"),
            (RetrievalQuery, "RETRIEVAL_QUERY"),
            (QuestionAnswering, "QUESTION_ANSWERING"),
            (FactVerification, "FACT_VERIFICATION"),
            (CodeRetrievalQuery, "CODE_RETRIEVAL_QUERY"),
        ];
        for (t, s) in pairs {
            assert_eq!(t.as_str(), s);
        }
    }

    #[tokio::test]
    async fn embed_single_and_batch_order() {
        use super::super::openai::ProviderAdapter;
        let m = spawn_wire(200, emb_ok_2(), 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        let r = a
            .embed_content(
                &emb_model(),
                vec!["a".to_string(), "b".to_string()],
                GeminiEmbedParams::default(),
                CancellationToken::new(),
            )
            .await
            .unwrap();
        assert_eq!(r.dimensions, 2);
        assert_eq!(r.model_id, "gemini:models/gemini-embedding-001");
        assert_eq!(r.vectors.len(), 2);
        // Positional mapping preserved.
        assert!((r.vectors[0][0] - 0.1).abs() < 1e-6);
        assert!((r.vectors[1][0] - 0.3).abs() < 1e-6);
        let raw = m.captured.lock().unwrap().join("\n");
        // Exact URL: no doubled segments, full models/ path.
        assert!(raw.contains("POST /v1beta/models/gemini-embedding-001:batchEmbedContent "));
        assert!(!raw.contains("/v1beta/v1beta"));
        assert!(!raw.contains("gemini:models"), "no internal id on wire");
        // No taskType/outputDimensionality when unset — never null.
        assert!(!raw.contains("taskType"));
        assert!(!raw.contains("outputDimensionality"));
        assert!(!raw.contains("null"));
        let lower = raw.to_lowercase();
        assert_eq!(lower.matches("x-goog-api-key: aiza-test-key-12345").count(), 1);
        assert!(!raw.contains("?key="));
    }

    #[tokio::test]
    async fn embed_options_mapping() {
        use super::super::openai::ProviderAdapter;
        let m = spawn_wire(200, emb_ok_2(), 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        let r = a
            .embed_content(
                &emb_model(),
                vec!["a".to_string(), "b".to_string()],
                GeminiEmbedParams {
                    task_type: Some(GeminiTaskType::RetrievalQuery),
                    output_dimensionality: Some(2),
                },
                CancellationToken::new(),
            )
            .await
            .unwrap();
        assert_eq!(r.dimensions, 2);
        let raw = m.captured.lock().unwrap().join("\n");
        assert!(raw.contains("\"taskType\":\"RETRIEVAL_QUERY\""));
        assert!(raw.contains("\"outputDimensionality\":2"));
    }

    #[tokio::test]
    async fn embed_option_validation() {
        use super::super::openai::ProviderAdapter;
        let m = spawn_wire(200, emb_ok_2(), 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        // outputDimensionality 0 rejected before network.
        let err = a
            .embed_content(
                &emb_model(),
                vec!["a".to_string()],
                GeminiEmbedParams {
                    task_type: None,
                    output_dimensionality: Some(0),
                },
                CancellationToken::new(),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code(), "invalid_request");
        // Requested 3 but server returns 2 → InvalidResponse.
        let err2 = a
            .embed_content(
                &emb_model(),
                vec!["a".to_string(), "b".to_string()],
                GeminiEmbedParams {
                    task_type: None,
                    output_dimensionality: Some(3),
                },
                CancellationToken::new(),
            )
            .await
            .unwrap_err();
        assert_eq!(err2.code(), "invalid_response");
        // Empty inputs rejected before network.
        let err3 = a
            .embed_content(&emb_model(), vec![], GeminiEmbedParams::default(), CancellationToken::new())
            .await
            .unwrap_err();
        assert_eq!(err3.code(), "invalid_request");
    }

    #[tokio::test]
    async fn embed_malformed_matrix() {
        use super::super::openai::ProviderAdapter;
        for (body, _why) in [
            (r#"{"nope":true}"#.to_string(), "no embeddings"),
            (r#"{"embeddings":[]}"#.to_string(), "empty"),
            (
                r#"{"embeddings":[{"embedding":{"values":[]}}]}"#.to_string(),
                "empty values",
            ),
            (
                r#"{"embeddings":[{"embedding":{"values":[1.0]}},{"embedding":{"values":[2.0,3.0]}}]}"#.to_string(),
                "count vs dims",
            ),
            (r#"{"embeddings":[{"nope":1}]}"#.to_string(), "no values"),
            (r#"not json"#.to_string(), "not json"),
        ] {
            let m = spawn_wire(200, body, 0).await;
            let a = adapter_for(m.port, Duration::from_secs(5));
            let err = a
                .embed_content(
                    &emb_model(),
                    vec!["a".to_string()],
                    GeminiEmbedParams::default(),
                    CancellationToken::new(),
                )
                .await
                .unwrap_err();
            assert!(
                err.code() == "malformed_response" || err.code() == "invalid_response",
                "got {}",
                err.code()
            );
        }
        // Count mismatch (2 inputs, 1 embedding).
        let m = spawn_wire(
            200,
            r#"{"embeddings":[{"embedding":{"values":[0.1]}}]}"#.to_string(),
            0,
        )
        .await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        assert_eq!(
            a.embed_content(
                &emb_model(),
                vec!["a".to_string(), "b".to_string()],
                GeminiEmbedParams::default(),
                CancellationToken::new()
            )
            .await
            .unwrap_err()
            .code(),
            "malformed_response"
        );
    }

    #[tokio::test]
    async fn embed_error_status_and_cancel() {
        use super::super::openai::ProviderAdapter;
        for (status, code) in [
            (401u16, "unauthorized"),
            (403, "unauthorized"),
            (404, "not_found"),
            (429, "rate_limited"),
            (500, "server_error"),
        ] {
            let m = spawn_wire(status, r#"{"error":{"message":"e"}}"#.to_string(), 0).await;
            let a = adapter_for(m.port, Duration::from_secs(5));
            let err = a
                .embed_content(
                    &emb_model(),
                    vec!["a".to_string()],
                    GeminiEmbedParams::default(),
                    CancellationToken::new(),
                )
                .await
                .unwrap_err();
            assert_eq!(err.code(), code, "status {}", status);
            assert!(!redact_gemini(&format!("{:?}", err)).contains("test-key-12345"));
        }
        // Timeout + cancel.
        let m = spawn_wire(200, emb_ok_2(), 2000).await;
        let a = adapter_for(m.port, Duration::from_millis(200));
        assert_eq!(
            a.embed_content(
                &emb_model(),
                vec!["a".to_string(), "b".to_string()],
                GeminiEmbedParams::default(),
                CancellationToken::new()
            )
            .await
            .unwrap_err()
            .code(),
            "timeout"
        );
        let cancel = CancellationToken::new();
        cancel.cancel();
        assert_eq!(
            a.embed_content(
                &emb_model(),
                vec!["a".to_string()],
                GeminiEmbedParams::default(),
                cancel
            )
            .await
            .unwrap_err()
            .code(),
            "cancelled"
        );
    }

    #[tokio::test]
    async fn embed_capability_gate_no_network() {
        use super::super::openai::ProviderAdapter;
        let m = spawn_wire(200, "{}".to_string(), 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        let req = crate::llm::embeddings::types::EmbeddingRequest::new(
            "x".to_string(),
            vec!["a".to_string()],
        );
        // Chat-only model via the trait path → gate before network.
        assert_eq!(
            a.embed(&model(), req, CancellationToken::new())
                .await
                .unwrap_err()
                .code(),
            "unsupported_capability"
        );
    }

    #[tokio::test]
    async fn embed_bridge_roundtrip() {
        use crate::llm::embeddings::provider::EmbeddingProvider as Emb;
        let m = spawn_wire(200, emb_ok_2(), 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        let bridge = super::GeminiEmbedBridge {
            adapter: &a,
            model: emb_model(),
            params: GeminiEmbedParams::default(),
        };
        let req = crate::llm::embeddings::types::EmbeddingRequest::new(
            "gemini:models/gemini-embedding-001".to_string(),
            vec!["a".to_string(), "b".to_string()],
        );
        let r = bridge.embed(req, CancellationToken::new()).await.unwrap();
        assert_eq!(r.dimensions, 2);
        assert_eq!(r.model_id, "gemini:models/gemini-embedding-001");
    }
}
