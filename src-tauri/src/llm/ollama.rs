//! Minimal Ollama-native helpers.
//!
//! Scope: model discovery via the native `GET /api/tags` endpoint only.
//! Chat generation intentionally keeps the existing OpenAI-compatible legacy
//! gateway path (`POST /v1/chat/completions`) — this module does not touch it.
//!
//! Security: discovery never substitutes a different endpoint, never falls
//! back to another provider, and never leaks raw response bodies or the
//! request URL into error messages.

use std::time::Duration;

/// Default Ollama endpoint, used when the provider has no endpoint configured.
pub const DEFAULT_OLLAMA_ENDPOINT: &str = "http://127.0.0.1:11434";

/// Build the native tags URL for `endpoint`.
/// Empty endpoint → default loopback endpoint. Trailing slashes are trimmed.
pub fn tags_url(endpoint: &str) -> String {
    let base = if endpoint.trim().is_empty() {
        DEFAULT_OLLAMA_ENDPOINT.to_string()
    } else {
        endpoint.trim().to_string()
    };
    format!("{}/api/tags", base.trim_end_matches('/'))
}

/// Parse an Ollama `/api/tags` body into normalized model identifiers.
///
/// Returns `Err("malformed_response")` for anything that is not a JSON object
/// carrying a `models` array. The error never contains body content.
pub fn parse_tags(body: &str) -> Result<Vec<String>, String> {
    let v: serde_json::Value =
        serde_json::from_str(body).map_err(|_| "malformed_response".to_string())?;
    let arr = v
        .get("models")
        .and_then(|m| m.as_array())
        .ok_or_else(|| "malformed_response".to_string())?;
    let mut out: Vec<String> = Vec::with_capacity(arr.len());
    for m in arr {
        // Ollama returns both `name` and `model` (usually equal); prefer `name`.
        let id = m
            .get("name")
            .and_then(|n| n.as_str())
            .filter(|s| !s.trim().is_empty())
            .or_else(|| m.get("model").and_then(|n| n.as_str()))
            .unwrap_or("");
        let id = id.trim();
        if id.is_empty() {
            continue;
        }
        if !out.iter().any(|x| x == id) {
            out.push(id.to_string());
        }
    }
    Ok(out)
}

/// Discovery failure. `message` is always safe for UI display.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiscoverError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
}

impl DiscoverError {
    fn malformed() -> Self {
        Self {
            code: "malformed_response".to_string(),
            message: "Сервер Ollama вернул некорректный ответ.".to_string(),
            retryable: false,
        }
    }
    fn network(e: &reqwest::Error) -> Self {
        if e.is_timeout() {
            Self {
                code: "network".to_string(),
                message: "Таймаут подключения к серверу Ollama.".to_string(),
                retryable: true,
            }
        } else if e.is_connect() {
            Self {
                code: "network".to_string(),
                message: "Сервер Ollama недоступен.".to_string(),
                retryable: true,
            }
        } else {
            Self {
                code: "network".to_string(),
                message: "Ошибка сети при обращении к серверу Ollama.".to_string(),
                retryable: true,
            }
        }
    }
}

/// Fetch model identifiers from `{endpoint}/api/tags`.
///
/// No fallback: a non-success status, malformed body, or transport error is
/// reported as-is. HTTP status codes are mapped to stable UI codes.
pub async fn discover_models(
    endpoint: &str,
    timeout: Duration,
) -> Result<Vec<String>, DiscoverError> {
    let url = tags_url(endpoint);
    let client = reqwest::Client::builder()
        .timeout(timeout)
        .build()
        .map_err(|_| DiscoverError {
            code: "network".to_string(),
            message: "Не удалось создать HTTP-клиент.".to_string(),
            retryable: false,
        })?;
    match client.get(&url).send().await {
        Ok(resp) => {
            let status = resp.status();
            if status.is_success() {
                let body = resp.text().await.unwrap_or_default();
                parse_tags(&body).map_err(|_| DiscoverError::malformed())
            } else {
                let (code, retryable) = match status.as_u16() {
                    401 | 403 => ("unauthorized", false),
                    404 => ("unsupported", false),
                    500..=599 => ("server_error", true),
                    _ => ("http_error", false),
                };
                Err(DiscoverError {
                    code: code.to_string(),
                    message: format!("HTTP {}", status),
                    retryable,
                })
            }
        }
        Err(e) => Err(DiscoverError::network(&e)),
    }
}

/// Small helper so the command layer does not re-implement the timeout.
pub fn discovery_timeout() -> Duration {
    Duration::from_secs(8)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[test]
    fn tags_url_default_when_empty() {
        assert_eq!(tags_url(""), "http://127.0.0.1:11434/api/tags");
        assert_eq!(tags_url("   "), "http://127.0.0.1:11434/api/tags");
    }

    #[test]
    fn tags_url_trims_trailing_slash() {
        assert_eq!(
            tags_url("http://127.0.0.1:11434/"),
            "http://127.0.0.1:11434/api/tags"
        );
        assert_eq!(
            tags_url("http://example:1234"),
            "http://example:1234/api/tags"
        );
    }

    #[test]
    fn parse_tags_names() {
        let body = r#"{"models":[{"name":"llama3.1:latest","model":"llama3.1:latest"},{"name":"qwen2.5:7b"}]}"#;
        assert_eq!(
            parse_tags(body).unwrap(),
            vec!["llama3.1:latest".to_string(), "qwen2.5:7b".to_string()]
        );
    }

    #[test]
    fn parse_tags_prefers_name_then_model() {
        let body = r#"{"models":[{"model":"fallback:tag"},{"name":"","model":"second"}]}"#;
        assert_eq!(
            parse_tags(body).unwrap(),
            vec!["fallback:tag".to_string(), "second".to_string()]
        );
    }

    #[test]
    fn parse_tags_empty_list_is_ok() {
        assert_eq!(parse_tags(r#"{"models":[]}"#).unwrap(), Vec::<String>::new());
    }

    #[test]
    fn parse_tags_malformed_is_safe() {
        for body in ["not json", "{}", r#"{"models":{}}"#, "[]"] {
            let err = parse_tags(body).unwrap_err();
            assert_eq!(err, "malformed_response");
            assert!(!err.contains(body), "error never echoes body");
        }
    }

    #[test]
    fn parse_tags_dedups_and_skips_blank() {
        let body = r#"{"models":[{"name":"a"},{"name":"a"},{"name":""},{"name":"b"}]}"#;
        assert_eq!(parse_tags(body).unwrap(), vec!["a".to_string(), "b".to_string()]);
    }

    // --- mock HTTP ---

    fn respond(status: u16, body: &str) -> Vec<u8> {
        let reason = match status {
            200 => "OK",
            404 => "Not Found",
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

    struct Mock {
        port: u16,
        captured: Arc<Mutex<Vec<String>>>,
    }

    async fn spawn_mock(status: u16, body: &'static str) -> Mock {
        let captured = Arc::new(Mutex::new(Vec::new()));
        let c2 = captured.clone();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            while let Ok((mut stream, _)) = listener.accept().await {
                let c3 = c2.clone();
                tokio::spawn(async move {
                    let mut buf = [0u8; 2048];
                    let mut raw = String::new();
                    if let Ok(n) = stream.read(&mut buf).await {
                        raw.push_str(&String::from_utf8_lossy(&buf[..n]));
                    }
                    c3.lock().unwrap().push(raw);
                    let _ = stream.write_all(&respond(status, body)).await;
                });
            }
        });
        Mock { port, captured }
    }

    #[tokio::test]
    async fn discover_ok_uses_api_tags_path() {
        let m = spawn_mock(200, r#"{"models":[{"name":"llama3.1:latest"}]}"#).await;
        let models = discover_models(&format!("http://127.0.0.1:{}", m.port), Duration::from_secs(5))
            .await
            .unwrap();
        assert_eq!(models, vec!["llama3.1:latest".to_string()]);
        let raw = m.captured.lock().unwrap().join("\n");
        assert!(raw.contains("GET /api/tags"), "native tags path: {}", raw);
    }

    #[tokio::test]
    async fn discover_http_error_is_mapped() {
        let m = spawn_mock(500, "boom").await;
        let err = discover_models(&format!("http://127.0.0.1:{}", m.port), Duration::from_secs(5))
            .await
            .unwrap_err();
        assert_eq!(err.code, "server_error");
        assert!(err.retryable);
        assert!(!err.message.contains("boom"), "no raw body");
    }

    #[tokio::test]
    async fn discover_malformed_is_not_retryable() {
        let m = spawn_mock(200, r#"{"nope":true}"#).await;
        let err = discover_models(&format!("http://127.0.0.1:{}", m.port), Duration::from_secs(5))
            .await
            .unwrap_err();
        assert_eq!(err.code, "malformed_response");
        assert!(!err.retryable);
    }

    #[tokio::test]
    async fn discover_network_error_is_retryable() {
        // Port 9 is closed in practice; connect must fail quickly.
        let err = discover_models("http://127.0.0.1:9", Duration::from_secs(2))
            .await
            .unwrap_err();
        assert_eq!(err.code, "network");
        assert!(err.retryable);
        assert!(!err.message.contains("127.0.0.1"), "no endpoint in message");
    }
}
