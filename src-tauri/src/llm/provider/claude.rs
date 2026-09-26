//! Stage 3.3 (v0.5.9): native Claude adapter — Messages API only.
//!
//! Auth uses the `x-api-key` header with `anthropic-version: 2023-06-01`
//! (project policy, matching the legacy gateway). No Bearer, no query auth,
//! no streaming, no tools. Embeddings do not exist in the Claude API —
//! `embed()` permanently returns `UnsupportedCapability`.

use super::openai::{
    join_api_path, map_http_status_for, map_reqwest_err_for, race_with, truncate_capped,
    ChatMessage, ChatRequest, ChatResponse, ChatRole, ChatUsage, HealthReport, ProviderAdapter,
    SecretStore,
};use super::types::{AuthReference, ProviderConnection, ProviderError, ProviderModel};
use crate::llm::embeddings::provider::redact_secrets;
use crate::llm::task::policy::ProviderScope;
use crate::llm::{Capability, CapabilitySource, ProviderKind};
use crate::llm::models::types::ModelSource;
use async_trait::async_trait;
use std::sync::Arc;
use std::time::Duration;
use tokio_util::sync::CancellationToken;

/// Project anthropic-version policy (also used by the legacy gateway).
pub const ANTHROPIC_VERSION: &str = "2023-06-01";

/// Adapter-local default when `ChatRequest.max_tokens` is `None`.
/// The API requires `max_tokens`; this default is explicit adapter policy,
/// not a change to the common contract (which keeps the field optional).
pub const DEFAULT_MAX_TOKENS: u32 = 1024;

/// Mask a bare `sk-ant-…` key without regex: single pass, cut at the first
/// delimiter. Header lines are already covered by [`redact_secrets`].
fn mask_sk_ant(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    loop {
        match rest.find("sk-ant-") {
            None => {
                out.push_str(rest);
                break;
            }
            Some(i) => {
                out.push_str(&rest[..i]);
                let after = &rest[i + "sk-ant-".len()..];
                let end = after
                    .find(|c: char| {
                        !(c.is_ascii_alphanumeric() || c == '_' || c == '-')
                    })
                    .map(|e| i + "sk-ant-".len() + e)
                    .unwrap_or(rest.len());
                out.push_str("***REDACTED***");
                rest = &rest[end..];
            }
        }
    }
    out
}

fn redact_claude(s: &str) -> String {
    mask_sk_ant(&redact_secrets(s))
}

// ---------------------------------------------------------------------------
// Native Claude adapter (Messages API)
// ---------------------------------------------------------------------------

pub struct ClaudeNativeAdapter {
    connection: ProviderConnection,
    secrets: Arc<dyn SecretStore>,
    http: reqwest::Client,
    timeout: Duration,
}

/// Manual `Debug`: no transient secrets stored.
impl std::fmt::Debug for ClaudeNativeAdapter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ClaudeNativeAdapter")
            .field("connection", &self.connection)
            .field("timeout", &self.timeout)
            .finish()
    }
}

impl ClaudeNativeAdapter {
    pub fn new(
        connection: ProviderConnection,
        secrets: Arc<dyn SecretStore>,
        timeout: Duration,
    ) -> Result<Self, ProviderError> {
        connection.validate()?;
        if connection.kind != ProviderKind::Claude {
            return Err(ProviderError::InvalidConfig(format!(
                "native Claude adapter requires kind Claude, got {:?}",
                connection.kind
            )));
        }
        if connection.scope != ProviderScope::Cloud {
            return Err(ProviderError::InvalidConfig(format!(
                "native Claude adapter requires Cloud scope, got {:?}",
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

    /// API key strictly from keyring, sent only as `x-api-key`.
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

    fn check_model(&self, model: &ProviderModel) -> Result<(), ProviderError> {
        if model.provider_id != self.connection.id {
            return Err(ProviderError::InvalidConfig(format!(
                "model {} belongs to {}, not {}",
                model.id, model.provider_id, self.connection.id
            )));
        }
        validate_remote_id(&model.remote_id)?;
        if !model.supports(&Capability::Chat) {
            return Err(ProviderError::UnsupportedCapability("Chat".to_string()));
        }
        Ok(())
    }
}

/// Strict remote model id: non-empty, bounded, charset `[A-Za-z0-9._-]`.
/// Goes into the JSON body (not the URL), but strictness is cheap.
fn validate_remote_id(remote_id: &str) -> Result<(), ProviderError> {
    if remote_id.trim().is_empty() {
        return Err(ProviderError::InvalidConfig(
            "model has empty remote_id (never send internal id upstream)".to_string(),
        ));
    }
    if remote_id.len() > 128 {
        return Err(ProviderError::InvalidConfig(
            "remote model id too long".to_string(),
        ));
    }
    let ok = remote_id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.');
    if !ok {
        return Err(ProviderError::InvalidConfig(format!(
            "remote model id has illegal characters: {}",
            remote_id
        )));
    }
    Ok(())
}

/// Build the `/v1/messages` body.
///
/// Mapping: system messages → top-level `system` string (joined, omitted
/// when empty); user/assistant → messages in order (string shorthand);
/// `model` = `remote_id` only; `max_tokens` ALWAYS emitted
/// (`DEFAULT_MAX_TOKENS` when the request leaves it `None` — adapter-local
/// policy); temperature/top_p skip-none; no tools/stream/`null`.
pub fn build_claude_body(
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
    if request.response_format.is_some() {
        return Err(ProviderError::UnsupportedCapability(
            "StructuredOutput (no output_config in Stage 3.3)".to_string(),
        ));
    }
    let mut systems: Vec<String> = Vec::new();
    let mut messages: Vec<serde_json::Value> = Vec::new();
    for m in &request.messages {
        // Preserve order; server combines consecutive same-role turns.
        // Drop whitespace-only content; a message left empty is skipped,
        // and a request left with zero messages is rejected below.
        let text = m.content.trim();
        if text.is_empty() {
            continue;
        }
        match m.role {
            ChatRole::System => systems.push(m.content.clone()),
            ChatRole::User => {
                messages.push(serde_json::json!({"role": "user", "content": m.content}))
            }
            ChatRole::Assistant => {
                messages.push(serde_json::json!({"role": "assistant", "content": m.content}))
            }
        }
    }
    if messages.is_empty() {
        return Err(ProviderError::InvalidRequest(
            "no non-empty user/assistant message".to_string(),
        ));
    }
    let mut body = serde_json::json!({
        "model": model.remote_id,
        "max_tokens": request.max_tokens.unwrap_or(DEFAULT_MAX_TOKENS),
        "messages": messages,
    });
    if !systems.is_empty() {
        body["system"] = serde_json::Value::String(systems.join("\n\n"));
    }
    if let Some(t) = request.temperature {
        body["temperature"] = serde_json::json!(t);
    }
    if let Some(p) = request.top_p {
        body["top_p"] = serde_json::json!(p);
    }
    Ok(body)
}

fn parse_claude_chat(body: &[u8]) -> Result<ChatResponse, ProviderError> {
    let json: serde_json::Value = serde_json::from_slice(body)
        .map_err(|e| ProviderError::MalformedResponse(redact_claude(&e.to_string())))?;
    let content = json.get("content").and_then(|v| v.as_array()).ok_or_else(|| {
        ProviderError::InvalidResponse("missing content array".to_string())
    })?;
    if content.is_empty() {
        return Err(ProviderError::InvalidResponse("empty content".to_string()));
    }
    // Concatenate text blocks in order; skip non-text blocks (tool_use…).
    let mut text = String::new();
    for block in content {
        if block.get("type").and_then(|v| v.as_str()) == Some("text") {
            if let Some(t) = block.get("text").and_then(|v| v.as_str()) {
                text.push_str(t);
            }
        }
    }
    if text.trim().is_empty() {
        return Err(ProviderError::InvalidResponse(
            "no text in content blocks".to_string(),
        ));
    }
    let resp_model = json.get("model").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let finish = json
        .get("stop_reason")
        .and_then(|v| v.as_str())
        .map(String::from);
    let usage = json.get("usage").map(|u| super::openai::ChatUsage {
        prompt_tokens: u.get("input_tokens").and_then(|v| v.as_u64()),
        completion_tokens: u.get("output_tokens").and_then(|v| v.as_u64()),
        total_tokens: match (
            u.get("input_tokens").and_then(|v| v.as_u64()),
            u.get("output_tokens").and_then(|v| v.as_u64()),
        ) {
            (Some(a), Some(b)) => a.checked_add(b),
            _ => None,
        },
    });
    Ok(ChatResponse {
        content: text,
        model: resp_model,
        finish_reason: finish,
        usage,
    })
}

/// Explicit mapping of the Models API `capabilities` object.
/// `structured_outputs.supported=true` → StructuredOutput, nothing else.
/// Chat is a documented adapter-policy inference (listed ⇒ callable),
/// marked `Probed` by the caller — never presented as provider metadata.
pub fn has_structured_output(item: &serde_json::Value) -> bool {
    item.get("capabilities")
        .and_then(|c| c.get("structured_outputs"))
        .and_then(|s| s.get("supported"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}

fn parse_models_json(
    connection_id: &str,
    bytes: &[u8],
) -> Result<Vec<ProviderModel>, ProviderError> {
    let json: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|e| ProviderError::MalformedResponse(redact_claude(&e.to_string())))?;
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
        validate_remote_id(remote)?;
        let display = item
            .get("display_name")
            .and_then(|v| v.as_str())
            .unwrap_or(remote)
            .to_string();
        let mut caps = vec![Capability::Chat]; // adapter-policy inference, source Probed
        if has_structured_output(item) {
            caps.push(Capability::StructuredOutput);
        }
        out.push(ProviderModel {
            id: format!("{}:{}", connection_id, remote),
            provider_id: connection_id.to_string(),
            remote_id: remote.to_string(),
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
impl super::openai::ProviderAdapter for ClaudeNativeAdapter {
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
                // No dedicated health endpoint: GET /v1/models (no side effects).
                let url = join_api_path(&base, "/v1/models")?;
                let resp = client
                    .get(&url)
                    .header("x-api-key", key)
                    .header("anthropic-version", ANTHROPIC_VERSION)
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
                    .header("x-api-key", key)
                    .header("anthropic-version", ANTHROPIC_VERSION)
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
        request: ChatRequest,
        cancel: CancellationToken,
    ) -> Result<ChatResponse, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        self.check_model(model)?;
        let body = build_claude_body(model, &request)?;
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        race_with(
            self.http.clone(),
            self.timeout,
            cancel.clone(),
            |client| async move {
                let url = join_api_path(&base, "/v1/messages")?;
                let resp = client
                    .post(&url)
                    .header("Content-Type", "application/json")
                    .header("x-api-key", key)
                    .header("anthropic-version", ANTHROPIC_VERSION)
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
                parse_claude_chat(truncate_capped(&bytes)?)
            },
        )
        .await
    }

    async fn embed(
        &self,
        _model: &ProviderModel,
        _request: crate::llm::embeddings::types::EmbeddingRequest,
        cancel: CancellationToken,
    ) -> Result<crate::llm::embeddings::types::EmbeddingResponse, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        // The Claude API exposes no embeddings endpoint (permanent).
        Err(ProviderError::UnsupportedCapability("Embeddings".to_string()))
    }
}

/// [`crate::rag::answer::ChatGateway`] bridge bound to one catalog model.
pub struct ClaudeChatBridge<'a> {
    pub adapter: &'a ClaudeNativeAdapter,
    pub model: ProviderModel,
}

#[async_trait]
impl<'a> crate::rag::answer::ChatGateway for ClaudeChatBridge<'a> {
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
                    redact_claude(&format!("{:?}", other)).chars().take(600).collect(),
                ),
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::embeddings::types::EmbeddingRequest;

    fn conn(port: u16) -> ProviderConnection {
        ProviderConnection {
            id: "claude".to_string(),
            kind: ProviderKind::Claude,
            name: "Claude".to_string(),
            enabled: true,
            scope: ProviderScope::Cloud,
            endpoint: Some(format!("http://127.0.0.1:{}", port)),
            auth: Some(AuthReference::new("keychain://fragile-notes/cld".to_string()).unwrap()),
        }
    }

    fn secrets() -> Arc<dyn SecretStore> {
        struct Map;
        impl SecretStore for Map {
            fn get(&self, r: &str) -> Result<String, ProviderError> {
                assert_eq!(r, "keychain://fragile-notes/cld");
                Ok("sk-ant-test-key-12345".to_string())
            }
        }
        Arc::new(Map)
    }

    fn model() -> ProviderModel {
        ProviderModel {
            id: "claude:claude-sonnet-4-5".to_string(),
            provider_id: "claude".to_string(),
            remote_id: "claude-sonnet-4-5".to_string(),
            display_name: "Sonnet".to_string(),
            capabilities: vec![Capability::Chat],
            capability_source: Some(CapabilitySource::Manual),
            context_length: None,
            dimensions: None,
            pricing: None,
            source: ModelSource::Manual,
        }
    }

    fn adapter_for(port: u16, timeout: Duration) -> ClaudeNativeAdapter {
        ClaudeNativeAdapter::new(conn(port), secrets(), timeout).unwrap()
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
                    content: "hello".to_string(),
                },
                ChatMessage {
                    role: ChatRole::User,
                    content: "again".to_string(),
                },
            ],
            temperature: Some(0.2),
            top_p: Some(0.9),
            max_tokens: Some(128),
            response_format: None,
        }
    }

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
            408 => "Request Timeout",
            409 => "Conflict",
            413 => "Payload Too Large",
            429 => "Too Many Requests",
            500 => "Internal Server Error",
            504 => "Gateway Timeout",
            529 => "Overloaded",
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
        r#"{"id":"msg_1","type":"message","role":"assistant","model":"claude-sonnet-4-5","content":[{"type":"text","text":"he"},{"type":"text","text":"llo"}],"stop_reason":"end_turn","usage":{"input_tokens":10,"output_tokens":5}}"#.to_string()
    }

    #[test]
    fn kind_scope_gating() {
        let mut c = conn(9);
        c.kind = ProviderKind::OpenAI;
        c.scope = ProviderScope::Cloud;
        assert!(ClaudeNativeAdapter::new(c, secrets(), Duration::from_secs(1)).is_err());
        let mut c2 = conn(9);
        c2.scope = ProviderScope::LocalManaged;
        c2.kind = ProviderKind::Claude;
        c2.endpoint = Some("http://127.0.0.1:9".to_string());
        assert!(ClaudeNativeAdapter::new(c2, secrets(), Duration::from_secs(1)).is_err());
        assert_eq!(ANTHROPIC_VERSION, "2023-06-01", "project policy version");
        assert_eq!(DEFAULT_MAX_TOKENS, 1024, "adapter-local default pinned");
    }

    #[test]
    fn body_mapping() {
        let body = build_claude_body(&model(), &req()).unwrap();
        assert_eq!(body["model"], "claude-sonnet-4-5");
        assert!(!body.to_string().contains("claude:claude-sonnet"));
        assert_eq!(body["system"], "sys", "system top-level");
        assert_eq!(body["messages"].as_array().unwrap().len(), 3);
        assert_eq!(body["messages"][0]["role"], "user");
        assert_eq!(body["messages"][1]["role"], "assistant");
        assert_eq!(body["messages"][2]["role"], "user", "order preserved");
        assert!(body["messages"].as_array().unwrap().iter().all(|m| m.get("role").unwrap() != "system"));
        assert_eq!(body["max_tokens"], 128);
        let t = body["temperature"].as_f64().unwrap();
        assert!((t - 0.2).abs() < 1e-6);
        assert!(body.get("stream").is_none());
        assert!(body.get("tools").is_none());
        assert!(body.get("response_format").is_none());
        assert!(!body.to_string().contains("null"));
    }

    #[test]
    fn max_tokens_default_and_omissions() {
        let req = ChatRequest {
            messages: vec![ChatMessage {
                role: ChatRole::User,
                content: "hi".to_string(),
            }],
            ..Default::default()
        };
        let body = build_claude_body(&model(), &req).unwrap();
        assert_eq!(
            body["max_tokens"], 1024,
            "None → adapter-local default, always emitted"
        );
        assert!(body.get("system").is_none(), "empty system omitted");
        assert!(body.get("temperature").is_none());
        assert!(body.get("top_p").is_none());
        assert!(!body.to_string().contains("null"));
    }

    #[test]
    fn empty_and_invalid_requests_rejected() {
        assert!(build_claude_body(&model(), &ChatRequest::default()).is_err());
        let blank = ChatRequest {
            messages: vec![ChatMessage {
                role: ChatRole::User,
                content: "  ".to_string(),
            }],
            ..Default::default()
        };
        assert!(build_claude_body(&model(), &blank).is_err());
        let nan = ChatRequest {
            messages: vec![ChatMessage {
                role: ChatRole::User,
                content: "hi".to_string(),
            }],
            temperature: Some(f32::NAN),
            ..Default::default()
        };
        assert!(build_claude_body(&model(), &nan).is_err());
        let rf = ChatRequest {
            response_format: Some(super::super::openai::ResponseFormat {
                kind: "json_object".to_string(),
            }),
            ..req()
        };
        assert!(build_claude_body(&model(), &rf).is_err(), "no output_config in 3.3");
    }

    #[test]
    fn remote_id_validation() {
        let a = adapter_for(9, Duration::from_secs(1));
        for bad in ["", "models/a/b", "has space", "semi;colon", "slash/a"] {
            let mut m = model();
            m.remote_id = bad.to_string();
            assert!(a.check_model(&m).is_err(), "must reject {:?}", bad);
        }
        let long = "x".repeat(200);
        let mut m = model();
        m.remote_id = long;
        assert!(a.check_model(&m).is_err());
    }

    #[test]
    fn response_ordered_concat_and_usage() {
        let r = parse_claude_chat(chat_ok().as_bytes()).unwrap();
        assert_eq!(r.content, "hello");
        assert_eq!(r.finish_reason.as_deref(), Some("end_turn"));
        let u = r.usage.unwrap();
        assert_eq!(u.prompt_tokens, Some(10));
        assert_eq!(u.completion_tokens, Some(5));
        assert_eq!(u.total_tokens, Some(15));
        assert_eq!(r.model, "claude-sonnet-4-5");
    }

    #[test]
    fn response_empty_and_malformed() {
        assert_eq!(
            parse_claude_chat(r#"{"content":[]}"#.as_bytes()).unwrap_err().code(),
            "invalid_response"
        );
        assert_eq!(
            parse_claude_chat(r#"{"content":[{"type":"tool_use","id":"x"}]}"#.as_bytes())
                .unwrap_err()
                .code(),
            "invalid_response"
        );
        assert_eq!(
            parse_claude_chat(r#"{"nope":1}"#.as_bytes()).unwrap_err().code(),
            "invalid_response"
        );
        assert_eq!(
            parse_claude_chat(b"not json").unwrap_err().code(),
            "malformed_response"
        );
    }

    #[tokio::test]
    async fn chat_roundtrip_exact_headers() {
        use super::super::openai::ProviderAdapter;
        let m = spawn_wire(200, chat_ok(), 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        let r = a.chat(&model(), req(), CancellationToken::new()).await.unwrap();
        assert_eq!(r.content, "hello");
        let raw = m.captured.lock().unwrap().join("\n");
        assert!(raw.contains("POST /v1/messages "), "exact op path, no /v1 doubling");
        assert!(!raw.contains("/v1/v1/"));
        let lower = raw.to_lowercase();
        assert!(lower.contains("x-api-key: sk-ant-test-key-12345"));
        assert!(lower.contains("anthropic-version: 2023-06-01"));
        assert!(!raw.contains("?key="), "no query auth");
        assert!(!lower.contains("authorization:"), "no Bearer fallback");
        assert!(!raw.contains("claude:claude-sonnet"), "no internal id");
    }

    #[tokio::test]
    async fn endpoint_bare_and_v1_no_doubling() {
        use super::super::openai::{join_api_path, ProviderAdapter};
        assert_eq!(
            join_api_path("https://api.anthropic.com", "/v1/messages").unwrap(),
            "https://api.anthropic.com/v1/messages"
        );
        assert_eq!(
            join_api_path("https://api.anthropic.com/v1", "/v1/messages").unwrap(),
            "https://api.anthropic.com/v1/messages"
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let captured = Arc::new(std::sync::Mutex::new(Vec::new()));
        let c2 = captured.clone();
        tokio::spawn(async move {
            use tokio::io::AsyncWriteExt;
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    break;
                };
                let c3 = c2.clone();
                tokio::spawn(async move {
                    let raw = read_request(&mut stream).await;
                    c3.lock().unwrap().push(raw);
                    let _ = stream.write_all(&respond(200, &chat_ok())).await;
                });
            }
        });
        let mut c = conn(port);
        c.endpoint = Some(format!("http://127.0.0.1:{}/v1", port));
        let a = ClaudeNativeAdapter::new(c, secrets(), Duration::from_secs(5)).unwrap();
        a.chat(&model(), req(), CancellationToken::new()).await.unwrap();
        let raw = captured.lock().unwrap().join("\n");
        assert!(raw.contains("POST /v1/messages "), "single /v1, got: {}", &raw[..raw.find("\r\n").unwrap_or(80.min(raw.len()))]);
        assert!(!raw.contains("/v1/v1/"));
    }

    #[tokio::test]
    async fn missing_key_and_keyring_error_no_network() {
        use super::super::openai::ProviderAdapter;
        struct Empty;
        impl SecretStore for Empty {
            fn get(&self, _: &str) -> Result<String, ProviderError> {
                Err(ProviderError::KeyringUnavailable("locked".to_string()))
            }
        }
        let mut c = conn(9);
        c.auth = None;
        let a = ClaudeNativeAdapter::new(c, secrets(), Duration::from_secs(2)).unwrap();
        assert_eq!(
            a.chat(&model(), req(), CancellationToken::new())
                .await
                .unwrap_err()
                .code(),
            "credentials_missing"
        );
        let a2 = ClaudeNativeAdapter::new(conn(9), Arc::new(Empty), Duration::from_secs(2)).unwrap();
        assert_eq!(
            a2.chat(&model(), req(), CancellationToken::new())
                .await
                .unwrap_err()
                .code(),
            "keyring_unavailable"
        );
    }

    #[tokio::test]
    async fn error_matrix_full() {
        use super::super::openai::ProviderAdapter;
        for (status, code, retryable) in [
            (400u16, "invalid_request", false),
            (401, "unauthorized", false),
            (403, "unauthorized", false),
            (404, "not_found", false),
            (408, "timeout", true),
            (409, "invalid_request", false),
            (413, "invalid_request", false),
            (429, "rate_limited", true),
            (500, "server_error", true),
            (504, "timeout", true),
            (529, "server_error", true),
        ] {
            let m = spawn_wire(status, r#"{"type":"error","error":{"type":"t","message":"e"}}"#.to_string(), 0).await;
            let a = adapter_for(m.port, Duration::from_secs(5));
            let err = a
                .chat(&model(), req(), CancellationToken::new())
                .await
                .unwrap_err();
            assert_eq!(err.code(), code, "status {}", status);
            assert_eq!(err.is_retryable(), retryable, "status {}", status);
            assert!(!redact_claude(&format!("{:?}", err)).contains("sk-ant-test-key"));
        }
    }

    #[tokio::test]
    async fn timeout_cancel_no_retry() {
        use super::super::openai::ProviderAdapter;
        let m = spawn_wire(200, chat_ok(), 2000).await;
        let a = adapter_for(m.port, Duration::from_millis(200));
        let err = a
            .chat(&model(), req(), CancellationToken::new())
            .await
            .unwrap_err();
        assert_eq!(err.code(), "timeout");
        assert!(err.is_retryable());
        let cancel = CancellationToken::new();
        cancel.cancel();
        let err2 = a
            .chat(&model(), req(), cancel)
            .await
            .unwrap_err();
        assert_eq!(err2.code(), "cancelled");
        assert!(!err2.is_retryable(), "cancelled never retryable");
    }

    #[tokio::test]
    async fn oversized_body_rejected() {
        use super::super::openai::ProviderAdapter;
        let big = format!(r#"{{"content":[{{"type":"text","text":"{}"}}]}}"#, "z".repeat(2_000_000));
        let m = spawn_wire(200, big, 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        assert_eq!(
            a.chat(&model(), req(), CancellationToken::new())
                .await
                .unwrap_err()
                .code(),
            "malformed_response"
        );
    }

    #[tokio::test]
    async fn catalog_deterministic_and_explicit() {
        use super::super::openai::ProviderAdapter;
        let body = r#"{"data":[{"id":"claude-sonnet-4-5","display_name":"Sonnet","type":"model","capabilities":{"structured_outputs":{"supported":true}}},{"id":"claude-haiku-4-5","display_name":"Haiku","type":"model"}],"has_more":false,"first_id":"a","last_id":"b"}"#.to_string();
        let m = spawn_wire(200, body, 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        let first = a.list_models(CancellationToken::new()).await.unwrap();
        let second = a.list_models(CancellationToken::new()).await.unwrap();
        assert_eq!(first, second, "deterministic, no UUID per refresh");
        assert_eq!(first[0].id, "claude:claude-sonnet-4-5");
        assert_eq!(first[0].display_name, "Sonnet");
        assert!(first[0].capabilities.contains(&Capability::Chat));
        assert!(first[0].capabilities.contains(&Capability::StructuredOutput));
        use crate::llm::CapabilitySource as CS;
        assert!(matches!(first[0].capability_source, Some(CS::Probed)));
        assert_eq!(first[1].capabilities, vec![Capability::Chat]);
        let raw = m.captured.lock().unwrap().join("\n").to_lowercase();
        assert!(raw.contains("x-api-key: sk-ant-test-key-12345"));
        assert!(raw.contains("anthropic-version: 2023-06-01"));
    }

    #[tokio::test]
    async fn catalog_malformed() {
        use super::super::openai::ProviderAdapter;
        let m = spawn_wire(200, r#"{"nope":[]}"#.to_string(), 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        assert_eq!(
            a.list_models(CancellationToken::new()).await.unwrap_err().code(),
            "malformed_response"
        );
    }

    #[tokio::test]
    async fn health_reports() {
        use super::super::openai::ProviderAdapter;
        let m = spawn_wire(200, r#"{"data":[]}"#.to_string(), 0).await;
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
    async fn embed_permanently_unsupported() {
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
    fn sk_ant_redaction_terminates() {
        let evil = "x-api-key: sk-ant-aaa111  Bearer sk-ant-bbb222?key=AIzaCCC end sk-ant-".to_string();
        let out = redact_claude(&evil);
        assert!(!out.contains("sk-ant-aaa111"));
        assert!(!out.contains("sk-ant-bbb222"));
        assert!(!out.contains("AIzaCCC"));
        assert!(out.contains("x-api-key"), "header name kept, value masked");
    }

    #[test]
    fn bearer_redaction_unaffected() {
        let out = redact_claude("Authorization: Bearer sk-test-xyz\nnext");
        assert!(!out.contains("sk-test-xyz"));
    }

    #[test]
    fn debug_hides_key() {
        let a = adapter_for(9, Duration::from_secs(1));
        assert!(!format!("{:?}", a).contains("sk-ant-test-key"));
        assert!(!format!("{:?}", a).contains("keychain://"));
    }

    #[test]
    fn shared_status_mapping_preserved_and_extended() {
        let m = crate::llm::provider::openai::join_api_path("https://x.example.com", "/v1/models").unwrap();
        assert_eq!(m, "https://x.example.com/v1/models");
        let cases = [
            (400u16, "invalid_request", false),
            (401, "unauthorized", false),
            (403, "unauthorized", false),
            (404, "not_found", false),
            (408, "timeout", true),
            (409, "invalid_request", false),
            (413, "invalid_request", false),
            (429, "rate_limited", true),
            (504, "timeout", true),
            (529, "server_error", true),
        ];
        for (status, code, retryable) in cases {
            let err = crate::llm::provider::openai::map_status_for_test(status);
            assert_eq!(err.code(), code, "status {}", status);
            assert_eq!(err.is_retryable(), retryable, "status {}", status);
        }
    }
}
