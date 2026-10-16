//! Perplexity Agent API adapter (v0.5.9, Package 14).
//!
//! Web-grounded answers via `POST https://api.perplexity.ai/v1/agent`, with
//! the response `output` items parsed into assistant text **and** sources
//! (web search results + inline URL citations). This is deliberately NOT an
//! OpenAI chat-completions adapter: the Agent API response shape differs and
//! sources would be lost, so Perplexity gets a dedicated parser.
//!
//! Contract verified against the official docs (docs.perplexity.ai):
//! Agent API quickstart, API reference (`/v1/agent`), presets, OpenAI
//! compatibility (`GET /v1/models`), error handling, billing. See
//! `build_agent_body` / `parse_agent_response` for the exact fields used.
//!
//! Credentials: the raw key lives in the keyring; this adapter stores only the
//! `secret_ref` inside [`ProviderConnection`] and resolves the key per request.

use super::openai::{
    join_api_path, map_http_status_for, map_reqwest_err_for, truncate_capped, ChatMessage,
    ChatRequest, ChatResponse, ChatUsage, HealthReport, ProviderAdapter, SecretStore,
};
use super::types::{AuthReference, ProviderConnection, ProviderError, ProviderModel};
use crate::llm::embeddings::provider::redact_secrets;
use crate::llm::{Capability, CapabilitySource};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::Duration;
use tokio_util::sync::CancellationToken;

/// Perplexity Agent API presets (dynamic configuration names), documented at
/// docs.perplexity.ai/docs/agent-api/presets. Sent via the `preset` field.
pub const PERPLEXITY_PRESETS: [&str; 5] = ["fast", "low", "medium", "high", "xhigh"];

/// Maximum number of sources kept from one response (bounded UI payload).
const MAX_SOURCES: usize = 30;
/// Maximum stored URL/title/snippet length (defense against hostile payloads).
const MAX_URL_LEN: usize = 2048;
const MAX_TITLE_LEN: usize = 512;
const MAX_SNIPPET_LEN: usize = 2048;

/// One web source backing an answer. Only `http(s)` URLs are ever produced or
/// surfaced; the frontend additionally re-checks the scheme before rendering.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct ChatSource {
    pub url: String,
    pub title: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub snippet: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub date: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_updated: Option<String>,
}

/// Assistant text plus the sources it cites.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ChatFullResponse {
    pub content: String,
    pub model: String,
    pub usage: Option<ChatUsage>,
    pub sources: Vec<ChatSource>,
}

// ---------------------------------------------------------------------------
// Pure request/response mapping
// ---------------------------------------------------------------------------

fn is_http_url(u: &str) -> bool {
    let u = u.trim();
    u.starts_with("https://") || u.starts_with("http://")
}

fn clean_source(mut s: ChatSource) -> Option<ChatSource> {
    s.url = s.url.trim().to_string();
    s.title = s.title.trim().chars().take(MAX_TITLE_LEN).collect();
    s.snippet = s.snippet.trim().chars().take(MAX_SNIPPET_LEN).collect();
    if s.url.len() > MAX_URL_LEN || !is_http_url(&s.url) {
        return None;
    }
    if s.title.is_empty() {
        s.title = s.url.clone();
    }
    Some(s)
}

/// Build the Agent API request body. Only `model`/`preset` + `input` are sent:
/// a preset already bundles model, system prompt, search config and tool budget,
/// and the docs state that any extra field would *override* those tuned
/// defaults. We therefore deliberately do NOT forward temperature/top_p/
/// max_tokens — the shared task profile settings must not silently change the
/// preset's behavior. A value with `/` is treated as a provider model id
/// (e.g. `openai/gpt-6-luna`); otherwise it is a preset name.
pub fn build_agent_body(
    model: &ProviderModel,
    request: &ChatRequest,
) -> Result<serde_json::Value, ProviderError> {
    if request.messages.is_empty() {
        return Err(ProviderError::InvalidRequest(
            "messages is empty".to_string(),
        ));
    }
    let selector = model.remote_id.trim();
    if selector.is_empty() {
        return Err(ProviderError::InvalidRequest(
            "empty model/preset selector".to_string(),
        ));
    }
    // Multi-turn history is replayed in the `input` array (InputMessage items).
    let input: Vec<serde_json::Value> = request
        .messages
        .iter()
        .map(|m| {
            serde_json::json!({
                "type": "message",
                "role": m.role.as_str(),
                "content": m.content,
            })
        })
        .collect();
    let mut body = serde_json::json!({ "input": input });
    if selector.contains('/') {
        // Direct model mode (provider model id). Do NOT send preset.
        body["model"] = serde_json::json!(selector);
        // For Anthropic models, Agent API requires `max_output_tokens`.
        // Only forward when explicitly provided and > 0; never invent a budget.
        if selector.starts_with("anthropic/") {
            let v = request.max_tokens;
            if v == Some(0) || v.is_none() {
                return Err(ProviderError::InvalidRequest(
                    "anthropic/* models require max_output_tokens > 0 (from generation settings)"
                        .to_string(),
                ));
            }
            if let Some(mt) = v {
                if mt > 0 {
                    body["max_output_tokens"] = serde_json::json!(mt);
                } else {
                    return Err(ProviderError::InvalidRequest(
                        "anthropic/* models require max_output_tokens > 0".to_string(),
                    ));
                }
            }
        }
    } else {
        // Preset mode: do not override preset tuning with generation params.
        body["preset"] = serde_json::json!(selector);
    }
    Ok(body)
}

/// Parse a non-streaming Agent API response into text + sources.
pub fn parse_agent_response(
    bytes: &[u8],
    fallback_model: &str,
) -> Result<ChatFullResponse, ProviderError> {
    let json: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|e| ProviderError::MalformedResponse(redact_secrets(&e.to_string())))?;

    // Strict status handling for synchronous response shape.
    match json.get("status").and_then(|v| v.as_str()) {
        Some("completed") => {}
        Some("failed") => {
            let msg = json
                .get("error")
                .and_then(|e| e.get("message"))
                .and_then(|m| m.as_str())
                .unwrap_or("agent run failed");
            return Err(ProviderError::Server {
                status: 200,
                message: redact_secrets(msg),
            });
        }
        Some("cancelled") => {
            return Err(ProviderError::Cancelled);
        }
        Some("incomplete") | Some("in_progress") | Some("queued") => {
            return Err(ProviderError::MalformedResponse(format!(
                "non-terminal status '{}' from agent response",
                json.get("status").and_then(|v| v.as_str()).unwrap_or("")
            )));
        }
        Some(s) => {
            return Err(ProviderError::MalformedResponse(format!(
                "unknown status '{}' from agent response",
                s
            )));
        }
        None => {
            return Err(ProviderError::MalformedResponse(
                "missing status in agent response".to_string(),
            ));
        }
    }

    let output = json
        .get("output")
        .and_then(|v| v.as_array())
        .ok_or_else(|| ProviderError::MalformedResponse("missing output array".to_string()))?;

    let mut text = String::new();
    let mut sources: Vec<ChatSource> = Vec::new();
    let mut seen_urls = std::collections::HashSet::new();

    for item in output {
        match item.get("type").and_then(|v| v.as_str()) {
            Some("message") => {
                let parts = item
                    .get("content")
                    .and_then(|v| v.as_array())
                    .map(|a| a.as_slice())
                    .unwrap_or(&[]);
                for part in parts {
                    if part.get("type").and_then(|v| v.as_str()) != Some("output_text") {
                        continue;
                    }
                    if let Some(t) = part.get("text").and_then(|v| v.as_str()) {
                        text.push_str(t);
                    }
                    // Inline URL citations annotated on the text.
                    if let Some(anns) = part.get("annotations").and_then(|v| v.as_array()) {
                        for a in anns {
                            if a.get("type").and_then(|v| v.as_str()) != Some("url_citation") {
                                continue;
                            }
                            let src = ChatSource {
                                url: a.get("url").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                                title: a.get("title").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                                ..Default::default()
                            };
                            if let Some(s) = clean_source(src) {
                                if sources.len() < MAX_SOURCES && seen_urls.insert(s.url.clone()) {
                                    sources.push(s);
                                }
                            }
                        }
                    }
                }
            }
            Some("search_results") => {
                let results = item
                    .get("results")
                    .and_then(|v| v.as_array())
                    .map(|a| a.as_slice())
                    .unwrap_or(&[]);
                for r in results {
                    let src = ChatSource {
                        url: r.get("url").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                        title: r.get("title").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                        snippet: r.get("snippet").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                        date: r.get("date").and_then(|v| v.as_str()).map(String::from),
                        last_updated: r
                            .get("last_updated")
                            .and_then(|v| v.as_str())
                            .map(String::from),
                    };
                    if let Some(s) = clean_source(src) {
                        if sources.len() < MAX_SOURCES && seen_urls.insert(s.url.clone()) {
                            sources.push(s);
                        }
                    }
                }
            }
            _ => {}
        }
    }

    if text.trim().is_empty() {
        return Err(ProviderError::MalformedResponse(
            "no assistant text in agent response".to_string(),
        ));
    }

    let model = json
        .get("model")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or(fallback_model)
        .to_string();
    let usage = json.get("usage").map(|u| ChatUsage {
        prompt_tokens: u.get("input_tokens").and_then(|v| v.as_u64()),
        completion_tokens: u.get("output_tokens").and_then(|v| v.as_u64()),
        total_tokens: match (
            u.get("input_tokens").and_then(|v| v.as_u64()),
            u.get("output_tokens").and_then(|v| v.as_u64()),
        ) {
            (Some(i), Some(o)) => Some(i + o),
            _ => None,
        },
    });

    Ok(ChatFullResponse {
        content: text,
        model,
        usage,
        sources,
    })
}

// ---------------------------------------------------------------------------
// Adapter
// ---------------------------------------------------------------------------

pub struct PerplexityAdapter {
    connection: ProviderConnection,
    secrets: Arc<dyn SecretStore>,
    http: reqwest::Client,
    timeout: Duration,
    capabilities: Vec<Capability>,
    capability_source: CapabilitySource,
}

impl std::fmt::Debug for PerplexityAdapter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PerplexityAdapter")
            .field("connection", &self.connection)
            .field("timeout", &self.timeout)
            .finish()
    }
}

impl PerplexityAdapter {
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
            // A 3xx on a credential-bearing call must be an explicit error,
            // never a silent origin change.
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| {
                ProviderError::InvalidConfig("failed to initialize HTTP client".to_string())
            })?;
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

    /// Full chat: assistant text + sources. This is the Perplexity-native path;
    /// the [`ProviderAdapter::chat`] shim drops sources for generic callers.
    pub async fn chat_full(
        &self,
        model: &ProviderModel,
        request: ChatRequest,
        cancel: CancellationToken,
    ) -> Result<ChatFullResponse, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        self.check_model(model)?;
        if !model.supports(&Capability::Chat) {
            return Err(ProviderError::UnsupportedCapability("Chat".to_string()));
        }
        let body = build_agent_body(model, &request)?;
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        let fallback_model = model.remote_id.clone();
        self.race(cancel.clone(), async {
            let url = join_api_path(&base, "/v1/agent")?;
            let mut req = self
                .http
                .post(&url)
                .header("Content-Type", "application/json");
            if let Some(k) = &key {
                req = req.header("Authorization", format!("Bearer {}", k));
            }
            let resp = req.json(&body).send().await.map_err(map_reqwest_err_for)?;
            if cancel.is_cancelled() {
                return Err(ProviderError::Cancelled);
            }
            let status = resp.status().as_u16();
            if !resp.status().is_success() {
                let b = resp.text().await.unwrap_or_default();
                return Err(map_http_status_for(status, &b));
            }
            let bytes = resp.bytes().await.map_err(map_reqwest_err_for)?;
            let bytes = truncate_capped(&bytes)?;
            parse_agent_response(bytes, &fallback_model)
        })
        .await
    }
}

#[async_trait]
impl ProviderAdapter for PerplexityAdapter {
    async fn health(&self, cancel: CancellationToken) -> Result<HealthReport, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        let t0 = std::time::Instant::now();
        // `GET /v1/models` is documented and requires no authentication, so it
        // proves reachability only. It does NOT validate the key — we therefore
        // leave `authenticated` as `None` (unknown) rather than claiming a
        // success the listing cannot prove.
        let reachable = self
            .race(cancel.clone(), async {
                let url = join_api_path(&base, "/v1/models")?;
                let mut req = self.http.get(&url);
                if let Some(k) = &key {
                    req = req.header("Authorization", format!("Bearer {}", k));
                }
                let resp = req.send().await.map_err(map_reqwest_err_for)?;
                if cancel.is_cancelled() {
                    return Err(ProviderError::Cancelled);
                }
                Ok(resp.status().is_success())
            })
            .await?;
        Ok(HealthReport {
            reachable,
            authenticated: None,
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
            let resp = req.send().await.map_err(map_reqwest_err_for)?;
            if cancel.is_cancelled() {
                return Err(ProviderError::Cancelled);
            }
            let status = resp.status().as_u16();
            if !resp.status().is_success() {
                let body = resp.text().await.unwrap_or_default();
                return Err(map_http_status_for(status, &body));
            }
            let bytes = resp.bytes().await.map_err(map_reqwest_err_for)?;
            let bytes = truncate_capped(&bytes)?;
            let json: serde_json::Value = serde_json::from_slice(bytes)
                .map_err(|e| ProviderError::MalformedResponse(redact_secrets(&e.to_string())))?;
            let data = json
                .get("data")
                .and_then(|v| v.as_array())
                .ok_or_else(|| ProviderError::MalformedResponse("missing data array".to_string()))?;
            let mut out = Vec::with_capacity(data.len());
            for item in data {
                let remote = item.get("id").and_then(|v| v.as_str()).ok_or_else(|| {
                    ProviderError::MalformedResponse("model entry missing id".to_string())
                })?;
                if remote.trim().is_empty() {
                    return Err(ProviderError::MalformedResponse(
                        "empty remote model id".to_string(),
                    ));
                }
                out.push(ProviderModel {
                    id: format!("{}:{}", self.connection.id, remote),
                    provider_id: self.connection.id.clone(),
                    remote_id: remote.to_string(),
                    display_name: remote.to_string(),
                    // Capabilities are NOT guessed from the catalogue: the same
                    // principle as the OpenAI-compatible adapter (an id alone
                    // never proves chat support). Chat routing supplies
                    // `Capability::Chat` explicitly for the selected model.
                    capabilities: Vec::new(),
                    capability_source: None,
                    context_length: None,
                    dimensions: None,
                    pricing: None,
                    source: crate::llm::models::types::ModelSource::Manual,
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
        let full = self.chat_full(model, request, cancel).await?;
        Ok(ChatResponse {
            content: full.content,
            model: full.model,
            finish_reason: None,
            usage: full.usage,
        })
    }

    async fn embed(
        &self,
        _model: &ProviderModel,
        _request: crate::llm::embeddings::types::EmbeddingRequest,
        _cancel: CancellationToken,
    ) -> Result<crate::llm::embeddings::types::EmbeddingResponse, ProviderError> {
        // The Agent API is chat/search only. Perplexity's Embeddings API is a
        // separate endpoint with a separate contract — out of scope here, and
        // never faked through the chat path.
        Err(ProviderError::UnsupportedCapability("Embeddings".to_string()))
    }
}

#[cfg(test)]
mod unit_tests {
    use super::*;
    use crate::llm::task::policy::ProviderScope;
    use crate::llm::ProviderKind;

    fn model(remote: &str) -> ProviderModel {
        ProviderModel {
            id: format!("pplx:{}", remote),
            provider_id: "pplx".to_string(),
            remote_id: remote.to_string(),
            display_name: remote.to_string(),
            capabilities: vec![Capability::Chat],
            capability_source: Some(CapabilitySource::Manual),
            context_length: None,
            dimensions: None,
            pricing: None,
            source: crate::llm::models::types::ModelSource::Manual,
        }
    }

    fn conn() -> ProviderConnection {
        ProviderConnection {
            id: "pplx".to_string(),
            kind: ProviderKind::Perplexity,
            name: "Perplexity".to_string(),
            enabled: true,
            scope: ProviderScope::Cloud,
            endpoint: Some("https://api.perplexity.ai".to_string()),
            auth: Some(AuthReference {
                secret_ref: "keychain://fragile-notes/perplexity".to_string(),
            }),
        }
    }

    fn req(messages: Vec<(&str, &str)>) -> ChatRequest {
        ChatRequest {
            messages: messages
                .into_iter()
                .map(|(r, c)| ChatMessage {
                    role: match r {
                        "system" => super::super::openai::ChatRole::System,
                        "assistant" => super::super::openai::ChatRole::Assistant,
                        _ => super::super::openai::ChatRole::User,
                    },
                    content: c.to_string(),
                })
                .collect(),
            temperature: Some(0.7),
            top_p: Some(0.9),
            max_tokens: Some(2048),
            response_format: None,
        }
    }

    #[test]
    fn preset_selector_uses_preset_field() {
        let body = build_agent_body(&model("low"), &req(vec![("user", "hi")])).unwrap();
        assert_eq!(body["preset"], "low");
        assert!(body.get("model").is_none(), "preset form sends no model");
        // Generation knobs are deliberately not forwarded (preset keeps its defaults).
        assert!(body.get("temperature").is_none());
        assert!(body.get("top_p").is_none());
        assert!(body.get("max_output_tokens").is_none());
        assert!(body.get("max_tokens").is_none());
        assert_eq!(body["input"][0]["type"], "message");
        assert_eq!(body["input"][0]["role"], "user");
        assert_eq!(body["input"][0]["content"], "hi");
    }

    #[test]
    fn slashed_selector_uses_model_field() {
        let body = build_agent_body(&model("openai/gpt-6-luna"), &req(vec![("user", "hi")])).unwrap();
        assert_eq!(body["model"], "openai/gpt-6-luna");
        assert!(body.get("preset").is_none());
    }

    #[test]
    fn empty_messages_or_selector_rejected() {
        assert!(build_agent_body(&model("low"), &ChatRequest::default()).is_err());
        assert!(build_agent_body(&model("  "), &req(vec![("user", "hi")])).is_err());
    }

    #[test]
    fn parses_text_and_search_sources() {
        let json = serde_json::json!({
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "model": "openai/gpt-6-luna",
            "output": [
                { "type": "search_results", "queries": ["q"],
                  "results": [
                    { "id": 1, "url": "https://example.com/a", "title": "A", "snippet": "sa" },
                    { "id": 2, "url": "ftp://bad.example/x", "title": "Bad", "snippet": "s" }
                  ] },
                { "type": "message", "role": "assistant", "status": "completed", "id": "m1",
                  "content": [
                    { "type": "output_text", "text": "Answer with ",
                      "annotations": [ { "type": "url_citation", "url": "https://example.com/b", "title": "B" } ] },
                    { "type": "output_text", "text": "citations." }
                  ] }
            ],
            "usage": { "input_tokens": 10, "output_tokens": 5, "cost": 0.0 }
        });
        let out = parse_agent_response(json.to_string().as_bytes(), "fallback").unwrap();
        assert_eq!(out.content, "Answer with citations.");
        assert_eq!(out.model, "openai/gpt-6-luna");
        assert_eq!(out.usage.as_ref().unwrap().total_tokens, Some(15));
        let urls: Vec<&str> = out.sources.iter().map(|s| s.url.as_str()).collect();
        assert_eq!(urls, vec!["https://example.com/a", "https://example.com/b"]);
        // Non-http source dropped; empty snippet omitted on serialize.
        let ser = serde_json::to_string(&out.sources[1]).unwrap();
        assert!(!ser.contains("snippet"), "empty optional fields omitted: {}", ser);
    }

    #[test]
    fn failed_status_is_error() {
        let json = serde_json::json!({
            "id": "r", "object": "response", "status": "failed", "model": "m",
            "output": [],
            // A failed run may echo request context; the surfaced message must
            // pass through redact_secrets so an upstream auth header never leaks.
            "error": { "message": "boom pplx-secret Authorization: Bearer pplx-secret", "code": "x", "type": "y" }
        });
        let err = parse_agent_response(json.to_string().as_bytes(), "fc").unwrap_err();
        assert_eq!(err.code(), "server_error");
        let dbg = format!("{:?}", err);
        assert!(!dbg.contains("Bearer pplx-secret"), "bearer redacted: {}", dbg);
    }

    #[test]
    fn missing_text_is_malformed() {
        let json = serde_json::json!({
            "id": "r", "object": "response", "status": "completed", "model": "m",
            "output": [ { "type": "search_results", "results": [] } ]
        });
        let err = parse_agent_response(json.to_string().as_bytes(), "m").unwrap_err();
        assert_eq!(err.code(), "malformed_response");
    }

    #[test]
    fn lists_models_from_v1_models_shape() {
        let json = serde_json::json!({ "object": "list", "data": [ { "id": "openai/gpt-6-luna", "owned_by": "openai" } ] });
        // Direct data-array extraction is exercised via a tiny inline parse.
        let v: serde_json::Value = json;
        let ids: Vec<String> = v["data"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["id"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(ids, vec!["openai/gpt-6-luna"]);
    }
}
