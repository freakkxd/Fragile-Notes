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
        if !model.supports(&Capability::Chat) {
            return Err(ProviderError::UnsupportedCapability("Chat".to_string()));
        }
        Ok(())
    }

    fn model_path(&self, model: &ProviderModel) -> String {
        // Remote names are "models/<name>"; the URL needs the full path.
        if model.remote_id.starts_with("models/") {
            model.remote_id.clone()
        } else {
            format!("models/{}", model.remote_id)
        }
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
        self.check_model(model)?;
        let body = build_gemini_body(&request)?;
        let base = self.base_url()?;
        let key = self.resolve_key(&cancel)?;
        let path = format!("/v1beta/{}:generateContent", self.model_path(model));
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
        _model: &ProviderModel,
        _request: crate::llm::embeddings::types::EmbeddingRequest,
        cancel: CancellationToken,
    ) -> Result<crate::llm::embeddings::types::EmbeddingResponse, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        // embedContent has a different API shape; separate stage (3.2b).
        Err(ProviderError::UnsupportedCapability("Embeddings".to_string()))
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
        a.model_path(&model())
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
}
