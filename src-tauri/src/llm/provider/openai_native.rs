//! Stage 3.1 (v0.5.9): native OpenAI adapter — Chat Completions only.
//!
//! Uses [`ProviderConnection`] with `kind == OpenAI` and `Cloud` scope only.
//! Responses API is a separate future mode and is NOT mixed in here.
//! Embeddings go through the native `/v1/embeddings` endpoint with the same
//! domain validation as the generic adapter.

use super::openai::{
    join_api_path, map_http_status_for, map_reqwest_err_for, race_with, truncate_capped,
};
use super::types::{AuthReference, ProviderConnection, ProviderError, ProviderModel};
use crate::llm::embeddings::provider::redact_secrets;
use crate::llm::embeddings::types::{EmbeddingLimits, EmbeddingRequest, EmbeddingResponse};
use crate::llm::task::policy::ProviderScope;
use crate::llm::{Capability, CapabilitySource, ProviderKind};
use crate::llm::models::types::ModelSource;
use async_trait::async_trait;
use std::sync::Arc;
use std::time::Duration;
use tokio_util::sync::CancellationToken;

pub use super::openai::{
    ChatMessage, ChatRequest, ChatResponse, ChatRole, ChatUsage, HealthReport, ResponseFormat,
};

// ---------------------------------------------------------------------------
// Native OpenAI adapter (Chat Completions + /v1/embeddings)
// ---------------------------------------------------------------------------

pub struct OpenAiNativeAdapter {
    connection: ProviderConnection,
    secrets: Arc<dyn super::openai::SecretStore>,
    http: reqwest::Client,
    timeout: Duration,
}

/// Manual `Debug`: no transient secrets stored.
impl std::fmt::Debug for OpenAiNativeAdapter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OpenAiNativeAdapter")
            .field("connection", &self.connection)
            .field("timeout", &self.timeout)
            .finish()
    }
}

impl OpenAiNativeAdapter {
    pub fn new(
        connection: ProviderConnection,
        secrets: Arc<dyn super::openai::SecretStore>,
        timeout: Duration,
    ) -> Result<Self, ProviderError> {
        connection.validate()?;
        if connection.kind != ProviderKind::OpenAI {
            return Err(ProviderError::InvalidConfig(format!(
                "native OpenAI adapter requires kind OpenAI, got {:?}",
                connection.kind
            )));
        }
        if connection.scope != ProviderScope::Cloud {
            return Err(ProviderError::InvalidConfig(format!(
                "native OpenAI adapter requires Cloud scope, got {:?}",
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

    /// API key strictly from keyring. No key → CredentialsMissing (no network).
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
}

/// Build the Chat Completions body. `model.remote_id` is the only model
/// identifier sent upstream; optional fields are skipped when `None`.
pub fn build_native_chat_body(
    model: &ProviderModel,
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
    if let Some(rf) = &request.response_format {
        if !model.supports(&Capability::StructuredOutput) {
            return Err(ProviderError::UnsupportedCapability(
                "StructuredOutput".to_string(),
            ));
        }
        if rf.kind.trim().is_empty() {
            return Err(ProviderError::InvalidRequest("empty response_format".to_string()));
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

fn parse_chat_json(body: &[u8]) -> Result<ChatResponse, ProviderError> {
    let json: serde_json::Value = serde_json::from_slice(body)
        .map_err(|e| ProviderError::MalformedResponse(redact_secrets(&e.to_string())))?;
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
    let resp_model = json.get("model").and_then(|v| v.as_str()).unwrap_or("").to_string();
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
}

fn parse_models_json(
    connection_id: &str,
    bytes: &[u8],
) -> Result<Vec<ProviderModel>, ProviderError> {
    let json: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|e| ProviderError::MalformedResponse(redact_secrets(&e.to_string())))?;
    // Native OpenAI shape: {"object":"list","data":[{"id",...}]}.
    if let Some(obj) = json.get("object").and_then(|v| v.as_str()) {
        if obj != "list" {
            return Err(ProviderError::MalformedResponse(format!(
                "expected object=list got {}",
                obj
            )));
        }
    }
    let data = json.get("data").and_then(|v| v.as_array()).ok_or_else(|| {
        ProviderError::MalformedResponse("missing data array".to_string())
    })?;
    let mut out = Vec::with_capacity(data.len());
    for item in data {
        let remote = item
            .get("id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ProviderError::MalformedResponse("model entry missing id".to_string()))?;
        if remote.trim().is_empty() {
            return Err(ProviderError::MalformedResponse("empty remote model id".to_string()));
        }
        out.push(ProviderModel {
            id: format!("{}:{}", connection_id, remote),
            provider_id: connection_id.to_string(),
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
}

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
            return Err(ProviderError::MalformedResponse(format!("bad index {}", idx)));
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
    let app_request = EmbeddingRequest::new(model.id.clone(), request.inputs.clone());
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

#[async_trait]
impl super::openai::ProviderAdapter for OpenAiNativeAdapter {
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
                let url = join_api_path(&base, "/v1/models")?;
                let resp = client
                    .get(&url)
                    .header("Authorization", format!("Bearer {}", key))
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
                let url = join_api_path(&base, "/v1/models")?;
                let resp = client
                    .get(&url)
                    .header("Authorization", format!("Bearer {}", key))
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
                parse_models_json(&self.connection.id, &bytes)
            },
        )
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
        self.check_model(model, &Capability::Chat)?;
        let body = build_native_chat_body(model, &request)?;
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        race_with(
            self.http.clone(),
            self.timeout,
            cancel.clone(),
            |client| async move {
                let url = join_api_path(&base, "/v1/chat/completions")?;
                let resp = client
                    .post(&url)
                    .header("Content-Type", "application/json")
                    .header("Authorization", format!("Bearer {}", key))
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
                parse_chat_json(&truncate_capped(&bytes)?)
            },
        )
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
        self.check_model(model, &Capability::Embeddings)?;
        let limits = EmbeddingLimits::default();
        request
            .validate(&limits)
            .map_err(|e| ProviderError::InvalidRequest(format!("{:?}", e)))?;
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        race_with(
            self.http.clone(),
            self.timeout,
            cancel.clone(),
            |client| async move {
                let url = join_api_path(&base, "/v1/embeddings")?;
                let body = serde_json::json!({
                    "model": model.remote_id,
                    "input": request.inputs,
                    "encoding_format": "float"
                });
                let resp = client
                    .post(&url)
                    .header("Content-Type", "application/json")
                    .header("Authorization", format!("Bearer {}", key))
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
                if bytes.len() > limits.max_vector_bytes + 1024 * 1024 {
                    return Err(ProviderError::MalformedResponse(
                        "embeddings response too large".to_string(),
                    ));
                }
                parse_embeddings_json(&bytes, model, &request, &limits)
            },
        )
        .await
    }
}
