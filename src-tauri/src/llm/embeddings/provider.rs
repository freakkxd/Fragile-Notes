use super::types::{EmbeddingError, EmbeddingRequest, EmbeddingResponse};
use async_trait::async_trait;
use tokio_util::sync::CancellationToken;

/// EmbeddingProvider is separate from ChatGateway.
/// Request is pure data, cancellation is control channel.
#[async_trait]
pub trait EmbeddingProvider: Send + Sync {
    async fn embed(
        &self,
        request: EmbeddingRequest,
        cancel: CancellationToken,
    ) -> Result<EmbeddingResponse, EmbeddingError>;

    /// Convenience without cancellation.
    async fn embed_without_cancel(
        &self,
        request: EmbeddingRequest,
    ) -> Result<EmbeddingResponse, EmbeddingError> {
        self.embed(request, CancellationToken::new()).await
    }
}

/// Helper to check cancellation before/after operations.
pub fn check_cancelled(cancel: &CancellationToken) -> Result<(), EmbeddingError> {
    if cancel.is_cancelled() {
        Err(EmbeddingError::Cancelled)
    } else {
        Ok(())
    }
}

/// Redact secrets from error/log strings.
pub fn redact_secrets(s: &str) -> String {
    let mut out = s.to_string();
    // Mask Authorization: Bearer ...
    for pat in ["Authorization", "authorization", "x-api-key", "x_api_key"] {
        if let Some(idx) = out.to_lowercase().find(&pat.to_lowercase()) {
            let start = idx;
            let end = out[start..].find('\n').map(|i| start + i).unwrap_or(out.len());
            let masked = format!("{}: ***REDACTED***", pat);
            out.replace_range(start..end.min(start + 128), &masked);
        }
    }
    // Mask ?key= or &key=. NOTE: the `?key=`/`&key=` prefix itself is kept,
    // so the search cursor must advance past each replacement — otherwise
    // `find` re-matches the same prefix forever (infinite loop).
    for key_pat in ["?key=", "&key=", "?api_key=", "&api_key="] {
        let mut search_from = 0usize;
        while let Some(rel) = out.to_lowercase().get(search_from..).and_then(|s| s.find(key_pat)) {
            let start = search_from + rel + key_pat.len();
            let end = out[start..].find(|c| c == '&' || c == ' ' || c == '"' || c == '\'').map(|i| start + i).unwrap_or(out.len().min(start + 64));
            out.replace_range(start..end, "***REDACTED***");
            search_from = start + "***REDACTED***".len();
        }
    }
    // Mask Bearer token
    if let Some(idx) = out.find("Bearer ") {
        let start = idx + "Bearer ".len();
        let end = out[start..].find(|c| c == ' ' || c == '"' || c == '\'' || c == '\n').map(|i| start + i).unwrap_or(out.len().min(start + 64));
        out.replace_range(start..end, "***REDACTED***");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redact_bearer() {
        let s = "Authorization: Bearer sk-1234567890abcdef";
        let r = redact_secrets(s);
        assert!(!r.contains("sk-1234"));
        assert!(r.contains("REDACTED"));
    }

    #[test]
    fn redact_key_url() {
        let s = "https://api.openai.com/v1/embeddings?key=sk-abc123&foo=bar";
        let r = redact_secrets(s);
        assert!(!r.contains("sk-abc123"));
        assert!(r.contains("REDACTED"));
    }
}
