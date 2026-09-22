use super::provider::{check_cancelled, redact_secrets, EmbeddingProvider};
use super::types::{EmbeddingError, EmbeddingLimits, EmbeddingRequest, EmbeddingResponse};
use async_trait::async_trait;
use reqwest::StatusCode;
use std::time::Duration;
use tokio_util::sync::CancellationToken;

const ERROR_BODY_LIMIT: usize = 8192;

/// NOTE: manual `Debug` — `api_key` is NEVER printed (presence only).
/// Debug output may land in logs; raw secrets must not.
impl std::fmt::Debug for OpenAiCompatibleConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OpenAiCompatibleConfig")
            .field("endpoint", &self.endpoint)
            .field("api_key", &self.api_key.as_ref().map(|_| "***REDACTED***"))
            .field("remote_model", &self.remote_model)
            .field("timeout", &self.timeout)
            .field("limits", &self.limits)
            .field("supports_embeddings", &self.supports_embeddings)
            .field("extra_headers", &self.extra_headers)
            .finish()
    }
}

#[derive(Clone)]
pub struct OpenAiCompatibleConfig {
    pub endpoint: String,
    pub api_key: Option<String>,
    /// Remote model name to send in API (may differ from application model_id)
    pub remote_model: String,
    pub timeout: Duration,
    pub limits: EmbeddingLimits,
    /// Whether this provider supports embeddings capability
    pub supports_embeddings: bool,
    /// Optional extra headers (not used for secrets)
    pub extra_headers: Vec<(String, String)>,
}

impl Default for OpenAiCompatibleConfig {
    fn default() -> Self {
        Self {
            endpoint: "http://127.0.0.1:8010".to_string(),
            api_key: None,
            remote_model: String::new(),
            timeout: Duration::from_secs(30),
            limits: EmbeddingLimits::default(),
            supports_embeddings: true,
            extra_headers: vec![],
        }
    }
}

pub struct OpenAiCompatibleEmbeddingProvider {
    config: OpenAiCompatibleConfig,
    client: reqwest::Client,
}

impl OpenAiCompatibleEmbeddingProvider {
    pub fn new(config: OpenAiCompatibleConfig) -> Result<Self, String> {
        if config.endpoint.trim().is_empty() {
            return Err("endpoint is empty".to_string());
        }
        // Validate endpoint is valid URL
        let _url: url::Url = config.endpoint.parse().map_err(|e| format!("invalid endpoint: {}", e))?;

        let client = reqwest::Client::builder()
            .timeout(config.timeout + Duration::from_secs(5)) // reqwest timeout as fallback, we use tokio::timeout for precise
            .build()
            .map_err(|e| e.to_string())?;
        Ok(Self { config, client })
    }

    fn remote_model_for(&self, request: &EmbeddingRequest) -> String {
        if !self.config.remote_model.trim().is_empty() {
            self.config.remote_model.clone()
        } else {
            request.model_id.clone()
        }
    }

    fn endpoint_url(&self) -> String {
        let base = self.config.endpoint.trim_end_matches('/');
        if base.ends_with("/v1") {
            format!("{}/embeddings", base)
        } else if base.ends_with("/v1/embeddings") {
            base.to_string()
        } else {
            format!("{}/v1/embeddings", base)
        }
    }
}

#[async_trait]
impl EmbeddingProvider for OpenAiCompatibleEmbeddingProvider {
    async fn embed(
        &self,
        request: EmbeddingRequest,
        cancel: CancellationToken,
    ) -> Result<EmbeddingResponse, EmbeddingError> {
        check_cancelled(&cancel)?;
        if !self.config.supports_embeddings {
            return Err(EmbeddingError::UnsupportedCapability(
                "Embeddings".to_string(),
            ));
        }
        request.validate(&self.config.limits)?;

        let remote_model = self.remote_model_for(&request);
        if remote_model.trim().is_empty() {
            return Err(EmbeddingError::EmptyModelId);
        }

        let url = self.endpoint_url();
        let body = serde_json::json!({
            "model": remote_model,
            "input": request.inputs,
            "encoding_format": "float"
        });
        let body_bytes = serde_json::to_vec(&body).map_err(|e| {
            EmbeddingError::Provider(format!("serialize failed: {}", redact_secrets(&e.to_string())))
        })?;

        // Build request
        let mut req = self.client.post(&url).header("Content-Type", "application/json");
        if let Some(key) = &self.config.api_key {
            if !key.trim().is_empty() {
                req = req.header("Authorization", format!("Bearer {}", key));
            }
        }
        for (k, v) in &self.config.extra_headers {
            req = req.header(k.as_str(), v.as_str());
        }
        // Never log raw key
        let req_body_json = serde_json::to_string(&body).unwrap_or_default();
        debug_assert!(
            !req_body_json.contains(self.config.api_key.as_deref().unwrap_or("__no_key__"))
                || self.config.api_key.is_none(),
            "api key must not be in JSON body"
        );

        req = req.body(body_bytes);

        // Race between request and cancellation/timeout
        let timeout = self.config.timeout;
        let fut = async {
            check_cancelled(&cancel)?;
            let resp = self.client.execute(req.build().map_err(|e| {
                EmbeddingError::Provider(redact_secrets(&e.to_string()))
            })?).await.map_err(|e| map_reqwest_err(e))?;
            check_cancelled(&cancel)?;
            parse_response(resp, &request, &self.config.limits, &remote_model).await
        };

        tokio::select! {
            _ = cancel.cancelled() => Err(EmbeddingError::Cancelled),
            res = tokio::time::timeout(timeout, fut) => {
                match res {
                    Ok(r) => r,
                    Err(_) => Err(EmbeddingError::Timeout(format!("timeout after {:?}", timeout))),
                }
            }
        }
    }
}

fn map_reqwest_err(e: reqwest::Error) -> EmbeddingError {
    let msg = redact_secrets(&e.to_string());
    if e.is_timeout() {
        return EmbeddingError::Timeout(msg);
    }
    if e.is_connect() {
        return EmbeddingError::Provider(msg);
    }
    if e.is_request() && msg.to_lowercase().contains("cancel") {
        return EmbeddingError::Cancelled;
    }
    EmbeddingError::Provider(msg)
}

async fn parse_response(
    resp: reqwest::Response,
    request: &EmbeddingRequest,
    limits: &EmbeddingLimits,
    expected_model: &str,
) -> Result<EmbeddingResponse, EmbeddingError> {
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        let truncated = truncate_body(&body);
        let redacted = redact_secrets(&truncated);
        return Err(map_http_status(status, &redacted));
    }

    let bytes = resp.bytes().await.map_err(|e| {
        if e.is_timeout() {
            EmbeddingError::Timeout(redact_secrets(&e.to_string()))
        } else {
            EmbeddingError::Provider(redact_secrets(&e.to_string()))
        }
    })?;

    if bytes.len() > limits.max_vector_bytes + 1024 * 1024 {
        return Err(EmbeddingError::VectorTooLarge {
            max_bytes: limits.max_vector_bytes,
            actual_bytes: bytes.len(),
        });
    }

    let json: serde_json::Value = serde_json::from_slice(&bytes)
        .map_err(|e| EmbeddingError::MalformedResponse(redact_secrets(&e.to_string())))?;

    parse_openai_json(&json, request, limits, expected_model)
}

fn map_http_status(status: StatusCode, body: &str) -> EmbeddingError {
    let msg = format!("HTTP {}: {}", status.as_u16(), body);
    match status.as_u16() {
        401 | 403 => EmbeddingError::Unauthorized(msg),
        404 => EmbeddingError::NotFound(msg),
        408 => EmbeddingError::Timeout(msg),
        429 => EmbeddingError::RateLimited(msg),
        500..=599 => EmbeddingError::Server {
            status: status.as_u16(),
            message: msg,
        },
        _ => EmbeddingError::Provider(msg),
    }
}

fn truncate_body(s: &str) -> String {
    if s.len() <= ERROR_BODY_LIMIT {
        s.to_string()
    } else {
        format!("{}...[truncated {} bytes]", &s[..ERROR_BODY_LIMIT], s.len() - ERROR_BODY_LIMIT)
    }
}

fn parse_openai_json(
    json: &serde_json::Value,
    request: &EmbeddingRequest,
    limits: &EmbeddingLimits,
    expected_model: &str,
) -> Result<EmbeddingResponse, EmbeddingError> {
    // object == "list"
    if let Some(obj) = json.get("object").and_then(|v| v.as_str()) {
        if obj != "list" {
            return Err(EmbeddingError::InvalidResponse(format!(
                "expected object=list got {}",
                obj
            )));
        }
    }
    let data = json
        .get("data")
        .and_then(|v| v.as_array())
        .ok_or_else(|| EmbeddingError::MalformedResponse("missing data array".to_string()))?;

    if data.len() != request.inputs.len() {
        return Err(EmbeddingError::CountMismatch {
            expected: request.inputs.len(),
            actual: data.len(),
        });
    }
    if data.is_empty() {
        return Err(EmbeddingError::EmptyResponse);
    }

    // Collect by index
    let mut indexed: Vec<(usize, Vec<f32>)> = Vec::with_capacity(data.len());
    let mut seen = std::collections::HashSet::new();
    let mut dimensions: Option<usize> = None;
    for item in data {
        let idx = item
            .get("index")
            .and_then(|v| v.as_u64())
            .ok_or_else(|| EmbeddingError::MalformedResponse("missing index".to_string()))? as usize;
        if !seen.insert(idx) {
            return Err(EmbeddingError::InvalidResponse(format!("duplicate index {}", idx)));
        }
        if idx >= request.inputs.len() {
            return Err(EmbeddingError::InvalidResponse(format!(
                "index {} out of bounds for inputs len {}",
                idx,
                request.inputs.len()
            )));
        }
        let emb = item
            .get("embedding")
            .and_then(|v| v.as_array())
            .ok_or_else(|| EmbeddingError::MalformedResponse("missing embedding array".to_string()))?;
        if emb.is_empty() {
            return Err(EmbeddingError::InvalidResponse(format!("empty embedding at index {}", idx)));
        }
        let mut vec = Vec::with_capacity(emb.len());
        for (pos, val) in emb.iter().enumerate() {
            let f = val.as_f64().ok_or_else(|| {
                EmbeddingError::MalformedResponse(format!("non-number at index {} pos {}", idx, pos))
            })? as f32;
            if !f.is_finite() {
                return Err(EmbeddingError::NonFiniteValue { index: idx, pos });
            }
            vec.push(f);
        }
        // dimensions consistency
        if let Some(d) = dimensions {
            if vec.len() != d {
                return Err(EmbeddingError::DimensionMismatch {
                    expected: d,
                    actual: vec.len(),
                    index: idx,
                });
            }
        } else {
            if vec.is_empty() {
                return Err(EmbeddingError::InvalidDimensions { dimensions: 0 });
            }
            if vec.len() > limits.max_dimensions {
                return Err(EmbeddingError::MaxDimensionsExceeded {
                    max: limits.max_dimensions,
                    actual: vec.len(),
                });
            }
            dimensions = Some(vec.len());
        }
        // bytes limit per vector already checked via total below
        indexed.push((idx, vec));
    }

    // Check indices cover 0..len
    if seen.len() != request.inputs.len() {
        return Err(EmbeddingError::InvalidResponse(format!(
            "indices missing: expected {} got {}",
            request.inputs.len(),
            seen.len()
        )));
    }
    for i in 0..request.inputs.len() {
        if !seen.contains(&i) {
            return Err(EmbeddingError::InvalidResponse(format!("missing index {}", i)));
        }
    }
    // Sort by index
    indexed.sort_by_key(|(idx, _)| *idx);
    let dims = dimensions.unwrap();
    let vectors: Vec<Vec<f32>> = indexed.into_iter().map(|(_, v)| v).collect();

    // Model check
    let resp_model = json
        .get("model")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if !resp_model.is_empty() && resp_model != expected_model && resp_model != request.model_id {
        // Allow either remote_model or application model_id
        // If mismatch, report
        if resp_model != expected_model {
            return Err(EmbeddingError::ModelMismatch {
                expected: expected_model.to_string(),
                actual: resp_model.to_string(),
            });
        }
    }

    // Total bytes check
    let total_bytes: usize = vectors.iter().map(|v| v.len() * 4).sum();
    if total_bytes > limits.max_vector_bytes {
        return Err(EmbeddingError::VectorTooLarge {
            max_bytes: limits.max_vector_bytes,
            actual_bytes: total_bytes,
        });
    }

    let response = EmbeddingResponse {
        model_id: request.model_id.clone(),
        dimensions: dims,
        vectors,
    };
    response.validate_for(request, limits)?;
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::types::{EmbeddingRequest, EmbeddingLimits};

    fn req(model: &str, inputs: Vec<&str>) -> EmbeddingRequest {
        EmbeddingRequest::new(model.to_string(), inputs.into_iter().map(|s| s.to_string()).collect())
    }

    #[test]
    fn endpoint_url_variants() {
        let c = OpenAiCompatibleConfig { endpoint: "http://127.0.0.1:8010".to_string(), ..Default::default() };
        let p = OpenAiCompatibleEmbeddingProvider::new(c).unwrap();
        assert_eq!(p.endpoint_url(), "http://127.0.0.1:8010/v1/embeddings");
        let c2 = OpenAiCompatibleConfig { endpoint: "http://127.0.0.1:8010/v1".to_string(), ..Default::default() };
        let p2 = OpenAiCompatibleEmbeddingProvider::new(c2).unwrap();
        assert_eq!(p2.endpoint_url(), "http://127.0.0.1:8010/v1/embeddings");
        let c3 = OpenAiCompatibleConfig { endpoint: "http://127.0.0.1:8010/v1/embeddings".to_string(), ..Default::default() };
        let p3 = OpenAiCompatibleEmbeddingProvider::new(c3).unwrap();
        assert_eq!(p3.endpoint_url(), "http://127.0.0.1:8010/v1/embeddings");
    }

    #[test]
    fn redact_in_provider_error() {
        let s = "failed with Authorization: Bearer sk-abc123";
        assert!(!redact_secrets(s).contains("sk-abc123"));
    }
}
