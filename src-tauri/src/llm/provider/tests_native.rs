//! Stage 3.1 native OpenAI tests — local mock HTTP only.

use super::openai::{
    ChatMessage, ChatRequest, ChatRole, NoSecretStore, ProviderAdapter, ResponseFormat,
    SecretStore,
};use super::openai_native::{OpenAiNativeAdapter, build_native_chat_body};
use super::types::*;
use crate::llm::embeddings::types::EmbeddingRequest;
use crate::llm::task::policy::ProviderScope;
use crate::llm::{Capability, CapabilitySource, ProviderKind};
use crate::llm::models::types::ModelSource;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio_util::sync::CancellationToken;

struct MapSecrets {
    map: HashMap<String, String>,
}

impl SecretStore for MapSecrets {
    fn get(&self, secret_ref: &str) -> Result<String, ProviderError> {
        self.map.get(secret_ref).cloned().ok_or_else(|| {
            ProviderError::CredentialsMissing(format!("no entry for {}", secret_ref))
        })
    }
}

fn secrets() -> Arc<MapSecrets> {
    Arc::new(MapSecrets {
        map: [("keychain://fragile-notes/oai".to_string(), "test-openai-key".to_string())]
            .into_iter()
            .collect(),
    })
}

fn conn(port: u16) -> ProviderConnection {
    ProviderConnection {
        id: "openai".to_string(),
        kind: ProviderKind::OpenAI,
        name: "OpenAI".to_string(),
        enabled: true,
        scope: ProviderScope::Cloud,
        endpoint: Some(format!("http://127.0.0.1:{}", port)),
        auth: Some(AuthReference::new("keychain://fragile-notes/oai".to_string()).unwrap()),
    }
}

fn chat_model() -> ProviderModel {
    ProviderModel {
        id: "openai:gpt-4o".to_string(),
        provider_id: "openai".to_string(),
        remote_id: "gpt-4o".to_string(),
        display_name: "GPT-4o".to_string(),
        capabilities: vec![Capability::Chat],
        capability_source: Some(CapabilitySource::StaticProvider),
        context_length: Some(128000),
        dimensions: None,
        pricing: None,
        source: ModelSource::Manual,
    }
}

fn emb_model() -> ProviderModel {
    ProviderModel {
        id: "openai:emb".to_string(),
        provider_id: "openai".to_string(),
        remote_id: "text-embedding-3-small".to_string(),
        display_name: "Emb".to_string(),
        capabilities: vec![Capability::Embeddings],
        capability_source: Some(CapabilitySource::StaticProvider),
        context_length: None,
        dimensions: Some(1536),
        pricing: None,
        source: ModelSource::Manual,
    }
}

fn adapter_for(port: u16, timeout: Duration) -> OpenAiNativeAdapter {
    // NOTE: Cloud scope with loopback http endpoint passes validation
    // (loopback allowed for tests); https rule applies to non-loopback.
    let mut c = conn(port);
    c.scope = ProviderScope::Cloud;
    OpenAiNativeAdapter::new(c, secrets(), timeout).unwrap()
}

fn chat_req() -> ChatRequest {
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
        ],
        temperature: Some(0.7),
        top_p: Some(0.9),
        max_tokens: Some(32),
        response_format: None,
    }
}

struct WireMock {
    port: u16,
    captured: Arc<Mutex<Vec<String>>>,
}

async fn read_request(stream: &mut tokio::net::TcpStream) -> String {
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

async fn spawn_wire(status: u16, body: String, delay_ms: u64) -> WireMock {
    let captured = Arc::new(Mutex::new(Vec::new()));
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
    WireMock { port, captured }
}

fn chat_ok() -> String {
    r#"{"id":"c1","object":"chat.completion","model":"gpt-4o","choices":[{"index":0,"message":{"role":"assistant","content":"hello"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}"#.to_string()
}

#[test]
fn kind_and_scope_gating() {
    // Non-OpenAI kind rejected.
    let mut c = conn(9);
    c.kind = ProviderKind::DeepSeek;
    c.scope = ProviderScope::Cloud;
    assert!(OpenAiNativeAdapter::new(c, secrets(), Duration::from_secs(1)).is_err());
    // Local scope rejected.
    let mut c2 = conn(9);
    c2.scope = ProviderScope::LocalManaged;
    c2.kind = ProviderKind::OpenAI;
    c2.endpoint = Some("http://127.0.0.1:9".to_string());
    assert!(OpenAiNativeAdapter::new(c2, secrets(), Duration::from_secs(1)).is_err());
}

#[test]
fn native_body_mapping() {
    let body = build_native_chat_body(&chat_model(), &chat_req()).unwrap();
    assert_eq!(body["model"], "gpt-4o");
    assert!(!body.to_string().contains("openai:gpt-4o"));
    assert_eq!(body["messages"][0]["role"], "system");
    let t = body["temperature"].as_f64().unwrap();
    assert!((t - 0.7).abs() < 1e-6);
    assert_eq!(body["max_tokens"], 32);
    assert!(body.get("stream").is_none(), "no stream field");
    assert!(body.get("tools").is_none());
    assert!(body.get("response_format").is_none());
}

#[test]
fn native_response_format_gate() {
    let req = ChatRequest {
        response_format: Some(ResponseFormat {
            kind: "json_object".to_string(),
        }),
        ..chat_req()
    };
    assert!(build_native_chat_body(&chat_model(), &req).is_err());
    let mut m = chat_model();
    m.capabilities.push(Capability::StructuredOutput);
    assert!(build_native_chat_body(&m, &req).is_ok());
}

#[tokio::test]
async fn native_chat_roundtrip() {
    let m = spawn_wire(200, chat_ok(), 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let r = a
        .chat(&chat_model(), chat_req(), CancellationToken::new())
        .await
        .unwrap();
    assert_eq!(r.content, "hello");
    assert_eq!(r.model, "gpt-4o");
    assert_eq!(r.usage.unwrap().total_tokens, Some(12));
    let raw = m.captured.lock().unwrap().join("\n").to_lowercase();
    assert!(raw.contains("\"model\":\"gpt-4o\""));
    assert!(!raw.contains("openai:gpt-4o"));
    assert_eq!(raw.matches("bearer test-openai-key").count(), 1);
}

#[tokio::test]
async fn native_key_only_from_keyring() {
    // NoSecretStore → CredentialsMissing before any network (closed port).
    let a = OpenAiNativeAdapter::new(conn(9), Arc::new(NoSecretStore), Duration::from_secs(2))
        .unwrap();
    let err = a
        .chat(&chat_model(), chat_req(), CancellationToken::new())
        .await
        .unwrap_err();
    assert_eq!(err.code(), "credentials_missing");
    // Debug/Display carry no key.
    let m = spawn_wire(500, "boom".to_string(), 0).await;
    let a2 = adapter_for(m.port, Duration::from_secs(5));
    let err2 = a2
        .chat(&chat_model(), chat_req(), CancellationToken::new())
        .await
        .unwrap_err();
    assert!(!format!("{:?}", err2).contains("test-openai-key"));
    assert!(!format!("{:?}", a2).contains("test-openai-key"));
    assert!(!format!("{:?}", a2).contains("keychain://"));
}

#[tokio::test]
async fn native_error_matrix() {
    for (status, code) in [
        (400u16, "invalid_request"),
        (401, "unauthorized"),
        (403, "unauthorized"),
        (404, "not_found"),
        (408, "timeout"),
        (429, "rate_limited"),
        (500, "server_error"),
    ] {
        let m = spawn_wire(status, r#"{"error":"e"}"#.to_string(), 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        let err = a
            .chat(&chat_model(), chat_req(), CancellationToken::new())
            .await
            .unwrap_err();
        assert_eq!(err.code(), code, "status {}", status);
    }
}

#[tokio::test]
async fn native_timeout_cancel() {
    let m = spawn_wire(200, chat_ok(), 2000).await;
    let a = adapter_for(m.port, Duration::from_millis(200));
    assert_eq!(
        a.chat(&chat_model(), chat_req(), CancellationToken::new())
            .await
            .unwrap_err()
            .code(),
        "timeout"
    );
    let cancel = CancellationToken::new();
    cancel.cancel();
    assert_eq!(
        a.chat(&chat_model(), chat_req(), cancel)
            .await
            .unwrap_err()
            .code(),
        "cancelled"
    );
}

#[tokio::test]
async fn native_list_models() {
    let body = r#"{"object":"list","data":[{"id":"gpt-4o","object":"model"},{"id":"gpt-4o-mini","object":"model"}]}"#.to_string();
    let m = spawn_wire(200, body, 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let models = a.list_models(CancellationToken::new()).await.unwrap();
    assert_eq!(models.len(), 2);
    assert_eq!(models[0].id, "openai:gpt-4o");
    assert_eq!(models[0].remote_id, "gpt-4o");
    assert!(models[0].capabilities.is_empty(), "never guessed");
    assert!(models[0].capability_source.is_none());
    // Bearer auth on discovery too.
    let raw = m.captured.lock().unwrap().join("\n").to_lowercase();
    assert_eq!(raw.matches("bearer test-openai-key").count(), 1);
}

#[tokio::test]
async fn native_list_models_malformed() {
    let m = spawn_wire(200, r#"{"object":"NeXT","data":[]}"#.to_string(), 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    assert_eq!(
        a.list_models(CancellationToken::new())
            .await
            .unwrap_err()
            .code(),
        "malformed_response"
    );
}

#[tokio::test]
async fn native_health() {
    let m = spawn_wire(200, r#"{"object":"list","data":[]}"#.to_string(), 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let h = a.health(CancellationToken::new()).await.unwrap();
    assert!(h.reachable);
    assert_eq!(h.authenticated, Some(true));
    // Static capability declaration echoed, nothing probed.
    assert!(h.capabilities.contains(&Capability::Chat));

    let m2 = spawn_wire(401, "no".to_string(), 0).await;
    let a2 = adapter_for(m2.port, Duration::from_secs(5));
    let h2 = a2.health(CancellationToken::new()).await.unwrap();
    assert!(h2.reachable);
    assert_eq!(h2.authenticated, Some(false));
}

#[tokio::test]
async fn native_embeddings_roundtrip() {
    let body = r#"{"object":"list","model":"text-embedding-3-small","data":[{"index":0,"embedding":[0.1,0.2,0.3]}]}"#.to_string();
    let m = spawn_wire(200, body, 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let req = EmbeddingRequest::new(
        "openai:emb".to_string(),
        vec!["hello".to_string()],
    );
    let r = a
        .embed(&emb_model(), req, CancellationToken::new())
        .await
        .unwrap();
    assert_eq!(r.dimensions, 3);
    assert_eq!(r.model_id, "openai:emb");
    let raw = m.captured.lock().unwrap().join("\n");
    assert!(raw.contains("\"model\":\"text-embedding-3-small\""));
    assert!(!raw.contains("openai:emb"));
}

#[tokio::test]
async fn native_embeddings_capability_gated() {
    let m = spawn_wire(200, "{}".to_string(), 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let req = EmbeddingRequest::new("x".to_string(), vec!["a".to_string()]);
    assert_eq!(
        a.embed(&chat_model(), req, CancellationToken::new())
            .await
            .unwrap_err()
            .code(),
        "unsupported_capability"
    );
}
