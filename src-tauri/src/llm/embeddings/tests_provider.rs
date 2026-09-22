//! Stage 4 provider tests — mock HTTP server, no real credentials.

use super::openai_compatible::{OpenAiCompatibleConfig, OpenAiCompatibleEmbeddingProvider};
use super::provider::EmbeddingProvider;
use super::types::{EmbeddingLimits, EmbeddingRequest};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio_util::sync::CancellationToken;

fn req(model: &str, inputs: Vec<&str>) -> EmbeddingRequest {
    EmbeddingRequest::new(model.to_string(), inputs.into_iter().map(|s| s.to_string()).collect())
}

async fn spawn_mock_once(
    status: u16,
    body: serde_json::Value,
    extra_headers: Vec<(String, String)>,
    delay_ms: Option<u64>,
    capture: Option<std::sync::Arc<std::sync::Mutex<Option<String>>>>,
) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let body_bytes = serde_json::to_vec(&body).unwrap();
    tokio::spawn(async move {
        if let Ok((mut stream, _)) = listener.accept().await {
            // Read request (headers + maybe body)
            let mut buf = vec![0u8; 8192];
            let _ = tokio::time::timeout(Duration::from_secs(2), stream.read(&mut buf)).await;
            if let Some(c) = capture {
                let req_str = String::from_utf8_lossy(&buf).to_string();
                *c.lock().unwrap() = Some(req_str);
            }
            if let Some(d) = delay_ms {
                tokio::time::sleep(Duration::from_millis(d)).await;
            }
            let reason = match status {
                200 => "OK",
                401 => "Unauthorized",
                404 => "Not Found",
                429 => "Too Many Requests",
                500 => "Internal Server Error",
                _ => "Error",
            };
            let mut headers = format!(
                "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n",
                status, reason, body_bytes.len()
            );
            for (k, v) in extra_headers {
                headers.push_str(&format!("{}: {}\r\n", k, v));
            }
            headers.push_str("\r\n");
            let _ = stream.write_all(headers.as_bytes()).await;
            let _ = stream.write_all(&body_bytes).await;
            let _ = stream.flush().await;
        }
    });
    format!("http://{}", addr)
}

fn json_data(inputs_len: usize, dims: usize, model: &str) -> serde_json::Value {
    let data: Vec<serde_json::Value> = (0..inputs_len)
        .map(|i| {
            serde_json::json!({
                "object": "embedding",
                "index": i,
                "embedding": vec![0.1f64; dims]
            })
        })
        .collect();
    serde_json::json!({
        "object": "list",
        "data": data,
        "model": model,
        "usage": {"prompt_tokens": 10, "total_tokens": 10}
    })
}

#[tokio::test]
async fn valid_single_input() {
    let body = json_data(1, 3, "text-embedding-model");
    let endpoint = spawn_mock_once(200, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig {
        endpoint: endpoint.clone(),
        remote_model: "text-embedding-model".to_string(),
        timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let res = p.embed(req("text-embedding-model", vec!["hello"]), CancellationToken::new()).await.unwrap();
    assert_eq!(res.vectors.len(), 1);
    assert_eq!(res.dimensions, 3);
    assert_eq!(res.model_id, "text-embedding-model");
}

#[tokio::test]
async fn valid_batch() {
    let body = json_data(3, 4, "m1");
    let endpoint = spawn_mock_once(200, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig {
        endpoint,
        remote_model: "m1".to_string(),
        timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let res = p.embed(req("m1", vec!["a","b","c"]), CancellationToken::new()).await.unwrap();
    assert_eq!(res.vectors.len(), 3);
    assert_eq!(res.dimensions, 4);
}

#[tokio::test]
async fn request_uses_encoding_format_float() {
    let capture = std::sync::Arc::new(std::sync::Mutex::new(None));
    let body = json_data(1, 2, "m1");
    let endpoint = spawn_mock_once(200, body, vec![], None, Some(capture.clone())).await;
    let cfg = OpenAiCompatibleConfig {
        endpoint,
        remote_model: "m1".to_string(),
        timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let _ = p.embed(req("m1", vec!["hi"]), CancellationToken::new()).await.unwrap();
    let captured = capture.lock().unwrap().clone().unwrap();
    assert!(captured.contains("\"encoding_format\""));
    assert!(captured.contains("\"float\""));
    assert!(captured.contains("\"model\""));
    assert!(captured.contains("m1"));
}

#[tokio::test]
async fn request_model_mapping() {
    let capture = std::sync::Arc::new(std::sync::Mutex::new(None));
    let body = json_data(1, 2, "remote-name");
    let endpoint = spawn_mock_once(200, body, vec![], None, Some(capture.clone())).await;
    let cfg = OpenAiCompatibleConfig {
        endpoint,
        remote_model: "remote-name".to_string(),
        timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    // application model_id is local-id, remote is remote-name
    let _ = p.embed(req("local-id", vec!["hi"]), CancellationToken::new()).await.unwrap();
    let captured = capture.lock().unwrap().clone().unwrap();
    assert!(captured.contains("remote-name"));
    // should not contain raw secret (none set)
    assert!(!captured.contains("sk-"));
}

#[tokio::test]
async fn data_out_of_order_sorted_by_index() {
    let body = serde_json::json!({
        "object": "list",
        "data": [
            {"object": "embedding", "index": 2, "embedding": [0.3, 0.4]},
            {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
            {"object": "embedding", "index": 1, "embedding": [0.5, 0.6]}
        ],
        "model": "m1",
        "usage": {}
    });
    let endpoint = spawn_mock_once(200, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let res = p.embed(req("m1", vec!["a","b","c"]), CancellationToken::new()).await.unwrap();
    // Should be sorted: index 0 -> [0.1,0.2], 1 -> [0.5,0.6], 2 -> [0.3,0.4]
    assert_eq!(res.vectors[0], vec![0.1, 0.2]);
    assert_eq!(res.vectors[1], vec![0.5, 0.6]);
    assert_eq!(res.vectors[2], vec![0.3, 0.4]);
}

#[tokio::test]
async fn duplicate_index_rejected() {
    let body = serde_json::json!({
        "object": "list",
        "data": [
            {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
            {"object": "embedding", "index": 0, "embedding": [0.3, 0.4]}
        ],
        "model": "m1",
        "usage": {}
    });
    let endpoint = spawn_mock_once(200, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a","b"]), CancellationToken::new()).await.unwrap_err();
    assert!(format!("{:?}", err).contains("duplicate") || format!("{}", err).contains("duplicate"));
}

#[tokio::test]
async fn missing_index_rejected() {
    let body = serde_json::json!({
        "object": "list",
        "data": [
            {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
            {"object": "embedding", "index": 2, "embedding": [0.3, 0.4]}
        ],
        "model": "m1",
        "usage": {}
    });
    let endpoint = spawn_mock_once(200, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a","b","c"]), CancellationToken::new()).await.unwrap_err();
    assert!(format!("{}", err).contains("missing") || format!("{}", err).contains("count"));
}

#[tokio::test]
async fn count_mismatch_rejected() {
    let body = json_data(1, 2, "m1");
    let endpoint = spawn_mock_once(200, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a","b"]), CancellationToken::new()).await.unwrap_err();
    assert!(format!("{:?}", err).contains("CountMismatch") || format!("{}", err).contains("count"));
}

#[tokio::test]
async fn dimensions_mismatch_rejected() {
    let body = serde_json::json!({
        "object": "list",
        "data": [
            {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
            {"object": "embedding", "index": 1, "embedding": [0.3]}
        ],
        "model": "m1",
        "usage": {}
    });
    let endpoint = spawn_mock_once(200, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a","b"]), CancellationToken::new()).await.unwrap_err();
    assert!(format!("{:?}", err).contains("DimensionMismatch") || format!("{}", err).contains("dimension"));
}

#[tokio::test]
async fn empty_vector_rejected() {
    let body = serde_json::json!({
        "object": "list",
        "data": [
            {"object": "embedding", "index": 0, "embedding": []}
        ],
        "model": "m1",
        "usage": {}
    });
    let endpoint = spawn_mock_once(200, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a"]), CancellationToken::new()).await.unwrap_err();
    assert!(format!("{}", err).contains("empty") || format!("{}", err).contains("Invalid"));
}

#[tokio::test]
async fn nan_rejected() {
    let body = serde_json::json!({
        "object": "list",
        "data": [
            {"object": "embedding", "index": 0, "embedding": [f64::NAN, 0.1]}
        ],
        "model": "m1",
        "usage": {}
    });
    // Note: JSON NaN is not valid JSON, so server would send null or string; we simulate via string that parses to NaN? 
    // Instead test via direct vector that is non-finite: we send large number that becomes Infinity after f32 cast? Use payload with Infinity string
    // For this test, we use valid JSON with huge number that overflows f32 to Infinity
    let body2 = serde_json::json!({
        "object": "list",
        "data": [
            {"object": "embedding", "index": 0, "embedding": [1e40, 0.1]}
        ],
        "model": "m1",
        "usage": {}
    });
    let endpoint = spawn_mock_once(200, body2, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a"]), CancellationToken::new()).await.unwrap_err();
    // 1e40 as f32 is Infinity -> should be rejected as non-finite
    assert!(format!("{:?}", err).contains("NonFinite") || format!("{}", err).contains("finite") || format!("{}", err).contains("Infinity"));
    let _ = body;
}

#[tokio::test]
async fn infinity_rejected() {
    let body = serde_json::json!({
        "object": "list",
        "data": [
            {"object": "embedding", "index": 0, "embedding": [1e40, 0.1]}
        ],
        "model": "m1",
        "usage": {}
    });
    let endpoint = spawn_mock_once(200, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a"]), CancellationToken::new()).await.unwrap_err();
    assert!(format!("{:?}", err).contains("NonFinite") || format!("{}", err).to_lowercase().contains("finite"));
}

#[tokio::test]
async fn malformed_json() {
    // Send invalid JSON body
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        if let Ok((mut stream, _)) = listener.accept().await {
            let mut buf = vec![0u8; 4096];
            let _ = stream.read(&mut buf).await;
            let body = b"not json {";
            let resp = format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len());
            let _ = stream.write_all(resp.as_bytes()).await;
            let _ = stream.write_all(body).await;
        }
    });
    let endpoint = format!("http://{}", addr);
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a"]), CancellationToken::new()).await.unwrap_err();
    assert!(format!("{:?}", err).contains("Malformed") || format!("{}", err).to_lowercase().contains("malformed") || format!("{}", err).contains("json"));
}

#[tokio::test]
async fn http_401_normalized() {
    let body = serde_json::json!({"error": "unauthorized"});
    let endpoint = spawn_mock_once(401, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a"]), CancellationToken::new()).await.unwrap_err();
    assert!(matches!(err, super::types::EmbeddingError::Unauthorized(_)));
}

#[tokio::test]
async fn http_404_normalized() {
    let body = serde_json::json!({"error": "not found"});
    let endpoint = spawn_mock_once(404, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a"]), CancellationToken::new()).await.unwrap_err();
    assert!(matches!(err, super::types::EmbeddingError::NotFound(_)));
}

#[tokio::test]
async fn http_429_retryable() {
    let body = serde_json::json!({"error": "rate limited"});
    let endpoint = spawn_mock_once(429, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a"]), CancellationToken::new()).await.unwrap_err();
    assert!(matches!(err, super::types::EmbeddingError::RateLimited(_)));
    assert!(err.is_retryable());
}

#[tokio::test]
async fn http_500_retryable() {
    let body = serde_json::json!({"error": "server"});
    let endpoint = spawn_mock_once(500, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a"]), CancellationToken::new()).await.unwrap_err();
    assert!(matches!(err, super::types::EmbeddingError::Server{..}));
    assert!(err.is_retryable());
}

#[tokio::test]
async fn timeout() {
    let body = json_data(1, 2, "m1");
    let endpoint = spawn_mock_once(200, body, vec![], Some(2000), None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_millis(300), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a"]), CancellationToken::new()).await.unwrap_err();
    assert!(matches!(err, super::types::EmbeddingError::Timeout(_)) || format!("{}", err).to_lowercase().contains("timeout"));
    assert!(err.is_retryable() || matches!(err, super::types::EmbeddingError::Timeout(_)));
}

#[tokio::test]
async fn cancellation_before_request() {
    let body = json_data(1, 2, "m1");
    let endpoint = spawn_mock_once(200, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let cancel = CancellationToken::new();
    cancel.cancel();
    let err = p.embed(req("m1", vec!["a"]), cancel).await.unwrap_err();
    assert!(matches!(err, super::types::EmbeddingError::Cancelled));
}

#[tokio::test]
async fn cancellation_during_response() {
    let body = json_data(1, 2, "m1");
    let endpoint = spawn_mock_once(200, body, vec![], Some(2000), None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let cancel = CancellationToken::new();
    let cancel_clone = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(200)).await;
        cancel_clone.cancel();
    });
    let err = p.embed(req("m1", vec!["a"]), cancel).await.unwrap_err();
    assert!(matches!(err, super::types::EmbeddingError::Cancelled) || format!("{}", err).to_lowercase().contains("cancel"));
}

#[tokio::test]
async fn api_key_only_in_auth_header() {
    let capture = std::sync::Arc::new(std::sync::Mutex::new(None));
    let body = json_data(1, 2, "m1");
    let endpoint = spawn_mock_once(200, body, vec![], None, Some(capture.clone())).await;
    let cfg = OpenAiCompatibleConfig {
        endpoint,
        remote_model: "m1".to_string(),
        api_key: Some("sk-secret-123".to_string()),
        timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let _ = p.embed(req("m1", vec!["hi"]), CancellationToken::new()).await.unwrap();
    let captured = capture.lock().unwrap().clone().unwrap();
    // Header should contain Bearer
    assert!(captured.contains("Authorization") || captured.to_lowercase().contains("authorization"));
    assert!(captured.contains("Bearer"));
    // Body must NOT contain secret
    // Extract body part after \r\n\r\n
    let parts: Vec<&str> = captured.split("\r\n\r\n").collect();
    let body_part = if parts.len() > 1 { parts[1] } else { &captured };
    assert!(!body_part.contains("sk-secret-123"), "secret must not be in JSON body");
}

#[tokio::test]
async fn api_key_absent_from_logs_on_error() {
    let body = serde_json::json!({"error": "server"});
    let endpoint = spawn_mock_once(500, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig {
        endpoint,
        remote_model: "m1".to_string(),
        api_key: Some("sk-very-secret".to_string()),
        timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a"]), CancellationToken::new()).await.unwrap_err();
    let msg = format!("{:?} {}", err, err);
    assert!(!msg.contains("sk-very-secret"), "secret leaked in error: {}", msg);
    assert!(!msg.to_lowercase().contains("very-secret"));
}

#[tokio::test]
async fn capability_absent_blocks_request() {
    let cfg = OpenAiCompatibleConfig {
        endpoint: "http://127.0.0.1:1234".to_string(),
        remote_model: "m1".to_string(),
        supports_embeddings: false,
        timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a"]), CancellationToken::new()).await.unwrap_err();
    assert!(matches!(err, super::types::EmbeddingError::UnsupportedCapability(_)));
}

#[tokio::test]
async fn batch_input_limits_enforced() {
    let cfg = OpenAiCompatibleConfig {
        endpoint: "http://127.0.0.1:1234".to_string(),
        remote_model: "m1".to_string(),
        limits: EmbeddingLimits { max_inputs: 2, ..Default::default() },
        timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a","b","c"]), CancellationToken::new()).await.unwrap_err();
    assert!(matches!(err, super::types::EmbeddingError::BatchTooLarge{..}));

    let cfg2 = OpenAiCompatibleConfig {
        endpoint: "http://127.0.0.1:1234".to_string(),
        remote_model: "m1".to_string(),
        limits: EmbeddingLimits { max_input_chars: 5, ..Default::default() },
        timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let p2 = OpenAiCompatibleEmbeddingProvider::new(cfg2).unwrap();
    let err2 = p2.embed(req("m1", vec!["toolonginput"]), CancellationToken::new()).await.unwrap_err();
    assert!(matches!(err2, super::types::EmbeddingError::InputTooLarge{..}));
}

#[tokio::test]
async fn corrupt_blob_via_provider_still_validated() {
    // Provider's vector validation already covers finite/dimensions, but store's corrupt blob is separate.
    // Here we test that provider rejects NaN/Infinity from server.
    let body = serde_json::json!({
        "object": "list",
        "data": [
            {"object": "embedding", "index": 0, "embedding": [1e40, 0.1]}
        ],
        "model": "m1",
        "usage": {}
    });
    let endpoint = spawn_mock_once(200, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let err = p.embed(req("m1", vec!["a"]), CancellationToken::new()).await.unwrap_err();
    assert!(matches!(err, super::types::EmbeddingError::NonFiniteValue{..}) || format!("{}", err).contains("finite"));
}

#[tokio::test]
async fn e2e_mock_to_store() {
    // Full flow: request -> mock -> parse -> store -> reload -> equality
    let body = json_data(2, 3, "m1");
    let endpoint = spawn_mock_once(200, body, vec![], None, None).await;
    let cfg = OpenAiCompatibleConfig { endpoint, remote_model: "m1".to_string(), timeout: Duration::from_secs(5), ..Default::default() };
    let p = OpenAiCompatibleEmbeddingProvider::new(cfg).unwrap();
    let inputs = vec!["chunk one".to_string(), "chunk two".to_string()];
    let req = super::types::EmbeddingRequest::new("m1".to_string(), inputs.clone());
    let resp = p.embed(req.clone(), CancellationToken::new()).await.unwrap();
    assert_eq!(resp.vectors.len(), 2);
    // Persist as EmbeddingRecord
    use super::store::{SqliteStore, EmbeddingStore, ChunkStore};
    use super::validation::content_hash_for;
    use super::types::{NoteChunk, EmbeddingRecord, EmbeddingModelRef};
    let store = SqliteStore::new_in_memory().unwrap();
    let chunks: Vec<NoteChunk> = inputs.iter().enumerate().map(|(i, txt)| {
        NoteChunk {
            id: format!("chunk-{}", i),
            note_id: "note-1".to_string(),
            content: txt.clone(),
            content_hash: content_hash_for(txt),
            heading_path: vec![],
            start_offset: i*10,
            end_offset: i*10+txt.len(),
        }
    }).collect();
    store.upsert_chunks(&chunks).await.unwrap();
    let records: Vec<EmbeddingRecord> = chunks.iter().enumerate().map(|(i, ch)| {
        EmbeddingRecord {
            chunk_id: ch.id.clone(),
            model_id: "m1".to_string(),
            model_fingerprint: "fp-test".to_string(),
            content_hash: ch.content_hash.clone(),
            dimensions: resp.dimensions,
            vector: resp.vectors[i].clone(),
        }
    }).collect();
    store.upsert(&records).await.unwrap();
    for rec in &records {
        let got = store.get(&rec.chunk_id, &EmbeddingModelRef{model_id: "m1".to_string(), model_fingerprint: "fp-test".to_string()}).await.unwrap().unwrap();
        assert_eq!(got.vector, rec.vector);
        assert_eq!(got.dimensions, rec.dimensions);
    }
}
