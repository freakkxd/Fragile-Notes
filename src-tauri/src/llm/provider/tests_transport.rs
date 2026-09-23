//! Stage 2 transport tests — local mock HTTP server only.
//! No real OpenRouter/DeepSeek/cloud calls in default tests.

use super::openai::*;
use super::types::*;
use crate::llm::embeddings::types::EmbeddingRequest;
use crate::llm::task::policy::ProviderScope;
use crate::llm::{Capability, CapabilitySource, ProviderKind};
use crate::llm::models::types::ModelSource;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicUsize, Ordering};
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
        map: [("keychain://fragile-notes/test".to_string(), "test-key-123".to_string())]
            .into_iter()
            .collect(),
    })
}

fn cloud_conn(port: u16) -> ProviderConnection {
    ProviderConnection {
        id: "test-cloud".to_string(),
        kind: ProviderKind::CustomOpenAI,
        name: "Test".to_string(),
        enabled: true,
        scope: ProviderScope::LocalExternal,
        endpoint: Some(format!("http://127.0.0.1:{}", port)),
        auth: Some(AuthReference::new("keychain://fragile-notes/test".to_string()).unwrap()),
    }
}

fn chat_model() -> ProviderModel {
    ProviderModel {
        id: "test-cloud:remote-chat".to_string(),
        provider_id: "test-cloud".to_string(),
        remote_id: "remote-chat".to_string(),
        display_name: "Remote".to_string(),
        capabilities: vec![Capability::Chat],
        capability_source: Some(CapabilitySource::Manual),
        context_length: None,
        dimensions: None,
        pricing: None,
        source: ModelSource::Manual,
    }
}

fn emb_model() -> ProviderModel {
    ProviderModel {
        id: "test-cloud:remote-emb".to_string(),
        provider_id: "test-cloud".to_string(),
        remote_id: "remote-emb".to_string(),
        display_name: "RemoteEmb".to_string(),
        capabilities: vec![Capability::Embeddings],
        capability_source: Some(CapabilitySource::Manual),
        context_length: None,
        dimensions: Some(3),
        pricing: None,
        source: ModelSource::Manual,
    }
}

fn adapter_for(port: u16, timeout: Duration) -> OpenAiCompatibleAdapter {
    OpenAiCompatibleAdapter::new(
        cloud_conn(port),
        secrets(),
        timeout,
        vec![Capability::Chat],
        CapabilitySource::Manual,
    )
    .unwrap()
}

fn chat_req() -> ChatRequest {
    ChatRequest {
        messages: vec![ChatMessage {
            role: ChatRole::User,
            content: "hello".to_string(),
        }],
        temperature: Some(0.5),
        top_p: None,
        max_tokens: Some(16),
        response_format: None,
    }
}

async fn read_request(stream: &mut tokio::net::TcpStream) -> String {
    let mut buf = vec![0u8; 65536];
    let mut acc = Vec::new();
    // Read until end of headers; body may follow in same packet.
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
        if acc.len() > 1_000_000 {
            break;
        }
    }
    // Best-effort body: content-length bytes if already buffered are enough
    // for our assertions (small JSON bodies arrive in one packet).
    String::from_utf8_lossy(&acc).to_string()
}

fn respond(status: u16, body: &str) -> Vec<u8> {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        _ => "Error",
    };
    format!(
        "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        status,
        reason,
        body.len(),
        body
    )
    .into_bytes()
}

// Single-purpose mock with capture + hit counting.
struct WireMock {
    port: u16,
    captured: Arc<Mutex<Vec<String>>>,
    hits: Arc<AtomicUsize>,
}

async fn spawn_wire(
    status: u16,
    body: String,
    delay_ms: u64,
) -> WireMock {
    let captured = Arc::new(Mutex::new(Vec::new()));
    let hits = Arc::new(AtomicUsize::new(0));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let c2 = captured.clone();
    let h2 = hits.clone();
    tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else {
                break;
            };
            let c3 = c2.clone();
            let h3 = h2.clone();
            let body = body.clone();
            tokio::spawn(async move {
                h3.fetch_add(1, Ordering::SeqCst);
                let raw = read_request(&mut stream).await;
                c3.lock().unwrap().push(raw);
                if delay_ms > 0 {
                    tokio::time::sleep(Duration::from_millis(delay_ms)).await;
                }
                let _ = stream.write_all(&respond(status, &body)).await;
            });
        }
    });
    WireMock {
        port,
        captured,
        hits,
    }
}

fn chat_ok_body() -> String {
    r#"{"id":"c1","object":"chat.completion","model":"remote-chat","choices":[{"index":0,"message":{"role":"assistant","content":"hi there"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}"#.to_string()
}

#[tokio::test]
async fn valid_chat_request_response() {
    let m = spawn_wire(200, chat_ok_body(), 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let r = a
        .chat(&chat_model(), chat_req(), CancellationToken::new())
        .await
        .unwrap();
    assert_eq!(r.content, "hi there");
    assert_eq!(r.model, "remote-chat");
    assert_eq!(r.finish_reason.as_deref(), Some("stop"));
    assert_eq!(r.usage.unwrap().total_tokens, Some(5));
    // Wire assertions: remote_id sent, internal id absent, Bearer present once.
    // (HTTP wire lowercases header names; the secret value keeps its case.)
    let raw = m.captured.lock().unwrap().join("\n");
    assert!(raw.contains("\"model\":\"remote-chat\""));
    assert!(!raw.contains("test-cloud:remote-chat"), "internal id never sent");
    let lower = raw.to_lowercase();
    assert!(lower.contains("authorization:"));
    assert_eq!(lower.matches("bearer test-key-123").count(), 1);
}

#[tokio::test]
async fn auth_none_omits_header() {
    let m = spawn_wire(200, chat_ok_body(), 0).await;
    let conn = ProviderConnection {
        id: "local".to_string(),
        kind: ProviderKind::CustomOpenAI,
        name: "L".to_string(),
        enabled: true,
        scope: ProviderScope::LocalExternal,
        endpoint: Some(format!("http://127.0.0.1:{}", m.port)),
        auth: None,
    };
    let a = OpenAiCompatibleAdapter::new(
        conn,
        Arc::new(NoSecretStore),
        Duration::from_secs(5),
        vec![Capability::Chat],
        CapabilitySource::Manual,
    )
    .unwrap();
    a.chat(&ProviderModel {
        id: "local:m".to_string(),
        provider_id: "local".to_string(),
        remote_id: "m".to_string(),
        display_name: "M".to_string(),
        capabilities: vec![Capability::Chat],
        capability_source: None,
        context_length: None,
        dimensions: None,
        pricing: None,
        source: ModelSource::Manual,
    }, chat_req(), CancellationToken::new())
        .await
        .unwrap();
    let raw = m.captured.lock().unwrap().join("\n");
    assert!(
        !raw.to_lowercase().contains("authorization:"),
        "no header without auth"
    );
}

#[tokio::test]
async fn missing_credential_no_network() {
    // Closed port: any network attempt would fail differently.
    let a = OpenAiCompatibleAdapter::new(
        cloud_conn(9),
        Arc::new(NoSecretStore),
        Duration::from_secs(5),
        vec![Capability::Chat],
        CapabilitySource::Manual,
    )
    .unwrap();
    let err = a
        .chat(&chat_model(), chat_req(), CancellationToken::new())
        .await
        .unwrap_err();
    assert_eq!(err.code(), "credentials_missing");
}

#[tokio::test]
async fn error_mapping_matrix_transport() {
    for (status, code) in [
        (400u16, "invalid_request"),
        (401, "unauthorized"),
        (404, "not_found"),
        (429, "rate_limited"),
        (500, "server_error"),
    ] {
        let m = spawn_wire(status, format!(r#"{{"error":"e{}"}}"#, status), 0).await;
        let a = adapter_for(m.port, Duration::from_secs(5));
        let err = a
            .chat(&chat_model(), chat_req(), CancellationToken::new())
            .await
            .unwrap_err();
        assert_eq!(err.code(), code, "status {}", status);
    }
}

#[tokio::test]
async fn secret_absent_from_errors_and_debug() {
    let m = spawn_wire(500, "boom".to_string(), 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let err = a
        .chat(&chat_model(), chat_req(), CancellationToken::new())
        .await
        .unwrap_err();
    assert!(!format!("{:?}", err).contains("test-key-123"));
    assert!(!format!("{:?}", a).contains("test-key-123"));
    assert!(!format!("{:?}", a).contains("keychain://"));
}

#[tokio::test]
async fn embeddings_ordering_and_validation() {
    // Out-of-order indices must be sorted; remote_id sent, never internal id.
    let body = r#"{"object":"list","model":"remote-emb","data":[{"index":1,"embedding":[0.0,1.0,0.0]},{"index":0,"embedding":[1.0,0.0,0.0]}]}"#.to_string();
    let m = spawn_wire(200, body, 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let req = EmbeddingRequest::new("test-cloud:remote-emb".to_string(), vec!["a".to_string(), "b".to_string()]);
    let r = a
        .embed(&emb_model(), req, CancellationToken::new())
        .await
        .unwrap();
    assert_eq!(r.dimensions, 3);
    assert_eq!(r.vectors[0], vec![1.0, 0.0, 0.0]);
    assert_eq!(r.model_id, "test-cloud:remote-emb");
    let raw = m.captured.lock().unwrap().join("\n");
    assert!(raw.contains("\"model\":\"remote-emb\""));
    assert!(!raw.contains("test-cloud:remote-emb"));
}

#[tokio::test]
async fn embeddings_capability_gated() {
    let m = spawn_wire(200, "{}".to_string(), 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let req = EmbeddingRequest::new("x".to_string(), vec!["a".to_string()]);
    let err = a
        .embed(&chat_model(), req, CancellationToken::new())
        .await
        .unwrap_err();
    assert_eq!(err.code(), "unsupported_capability");
    assert_eq!(m.hits.load(Ordering::SeqCst), 0, "no network on gate");
}

#[tokio::test]
async fn list_models_deterministic_ids() {
    let body = r#"{"object":"list","data":[{"id":"alpha"},{"id":"beta"}]}"#.to_string();
    let m = spawn_wire(200, body, 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let first = a.list_models(CancellationToken::new()).await.unwrap();
    let second = a.list_models(CancellationToken::new()).await.unwrap();
    assert_eq!(first, second, "no random ids per refresh");
    assert_eq!(first[0].id, "test-cloud:alpha");
    assert_eq!(first[0].remote_id, "alpha");
    // Capabilities unknown — never guessed from names.
    assert!(first[0].capabilities.is_empty());
    assert!(first[0].capability_source.is_none());
}

#[tokio::test]
async fn malformed_models_response() {
    let m = spawn_wire(200, r#"{"nope":true}"#.to_string(), 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let err = a
        .list_models(CancellationToken::new())
        .await
        .unwrap_err();
    assert_eq!(err.code(), "malformed_response");
}

#[tokio::test]
async fn health_local_and_cloud() {
    // Local /health 200.
    let m = spawn_wire(200, "ok".to_string(), 0).await;
    let conn = ProviderConnection {
        id: "local".to_string(),
        kind: ProviderKind::LocalLlamaCpp,
        name: "L".to_string(),
        enabled: true,
        scope: ProviderScope::LocalManaged,
        endpoint: Some(format!("http://127.0.0.1:{}", m.port)),
        auth: None,
    };
    let a = OpenAiCompatibleAdapter::new(
        conn,
        Arc::new(NoSecretStore),
        Duration::from_secs(5),
        vec![Capability::Chat],
        CapabilitySource::Manual,
    )
    .unwrap();
    let h = a.health(CancellationToken::new()).await.unwrap();
    assert!(h.reachable);
    assert_eq!(h.authenticated, None, "anonymous local");
    assert_eq!(h.capabilities, vec![Capability::Chat]);

    // Cloud 401 → reachable, not authenticated.
    let m2 = spawn_wire(401, "no".to_string(), 0).await;
    let a2 = adapter_for(m2.port, Duration::from_secs(5));
    let h2 = a2.health(CancellationToken::new()).await.unwrap();
    assert!(h2.reachable);
    assert_eq!(h2.authenticated, Some(false));
}

#[tokio::test]
async fn health_unreachable_is_network_error() {
    let a = adapter_for(9, Duration::from_secs(2));
    let err = a.health(CancellationToken::new()).await.unwrap_err();
    assert_eq!(err.code(), "network");
    assert!(err.is_retryable());
}

#[tokio::test]
async fn timeout_classified() {
    let m = spawn_wire(200, chat_ok_body(), 2000).await;
    let a = adapter_for(m.port, Duration::from_millis(200));
    let err = a
        .chat(&chat_model(), chat_req(), CancellationToken::new())
        .await
        .unwrap_err();
    assert_eq!(err.code(), "timeout");
    assert!(err.is_retryable());
}

#[tokio::test]
async fn cancel_before_request_no_network() {
    let m = spawn_wire(200, chat_ok_body(), 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let cancel = CancellationToken::new();
    cancel.cancel();
    let err = a
        .chat(&chat_model(), chat_req(), cancel)
        .await
        .unwrap_err();
    assert_eq!(err.code(), "cancelled");
    assert!(!err.is_retryable(), "cancelled never retryable");
    // Give the loop a beat; server must see zero connections.
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(m.hits.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn cancel_during_body() {
    // Slow drip: headers + partial body, then stall.
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else {
                break;
            };
            tokio::spawn(async move {
                let _ = read_request(&mut stream).await;
                let head = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 5000\r\nConnection: close\r\n\r\n{\"choices\":[";
                let _ = stream.write_all(head.as_bytes()).await;
                tokio::time::sleep(Duration::from_secs(10)).await;
            });
        }
    });
    let a = adapter_for(port, Duration::from_secs(10));
    let cancel = CancellationToken::new();
    let c2 = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(300)).await;
        c2.cancel();
    });
    let err = a
        .chat(&chat_model(), chat_req(), cancel)
        .await
        .unwrap_err();
    assert_eq!(err.code(), "cancelled");
}

#[tokio::test]
async fn oversized_success_body_rejected() {
    let big = format!(r#"{{"choices":[{{"message":{{"content":"{}"}}}}]}}"#, "z".repeat(2_000_000));
    let m = spawn_wire(200, big, 0).await;
    let a = adapter_for(m.port, Duration::from_secs(5));
    let err = a
        .chat(&chat_model(), chat_req(), CancellationToken::new())
        .await
        .unwrap_err();
    assert_eq!(err.code(), "malformed_response");
}
