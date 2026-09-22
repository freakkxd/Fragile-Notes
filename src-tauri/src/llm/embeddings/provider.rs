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

    /// Regression test for the Stage 9 hang: `?key=` survives redaction as a
    /// prefix, so the match loop MUST terminate (cursor advances past each
    /// replacement). This test completing at all IS the assertion; the
    /// output checks below pin the masking behaviour.
    #[test]
    fn redact_key_url_terminates_and_masks() {
        let s = "https://api.openai.com/v1/embeddings?key=sk-abc123&foo=bar";
        let r = redact_secrets(s);
        assert!(!r.contains("sk-abc123"), "secret leaked: {}", r);
        assert!(r.contains("?key=***REDACTED***"), "prefix kept, value masked: {}", r);
        assert!(r.contains("&foo=bar"), "trailing params preserved: {}", r);
    }

    #[test]
    fn redact_all_key_variants() {
        for (input, secret) in [
            ("POST /x?key=sk-111&y=1", "sk-111"),
            ("POST /x?a=1&key=sk-222", "sk-222"),
            ("POST /x?api_key=sk-333&y=1", "sk-333"),
            ("POST /x?a=1&api_key=sk-444", "sk-444"),
        ] {
            let r = redact_secrets(input);
            assert!(!r.contains(secret), "secret leaked for {:?}: {}", input, r);
            assert!(r.contains("REDACTED"), "no mask for {:?}: {}", input, r);
        }
    }

    #[test]
    fn redact_multiple_keys_all_masked() {
        let s = "?key=sk-aaa1&x=1&key=sk-bbb2";
        let r = redact_secrets(s);
        assert!(!r.contains("sk-aaa1"), "first key leaked: {}", r);
        assert!(!r.contains("sk-bbb2"), "second key leaked: {}", r);
    }

    #[test]
    fn redact_x_api_key_header() {
        let s = "x-api-key: sk-xyz789\nnext-line kept";
        let r = redact_secrets(s);
        assert!(!r.contains("sk-xyz789"), "header key leaked: {}", r);
        assert!(r.contains("next-line kept"), "unrelated line damaged: {}", r);
    }

    #[test]
    fn redact_bearer_standalone() {
        let s = "request failed with Bearer sk-ant-aaa321, retry later";
        let r = redact_secrets(s);
        assert!(!r.contains("sk-ant-aaa321"), "bearer token leaked: {}", r);
    }

    #[test]
    fn redact_bare_provider_key_prefixes_via_log_mask() {
        // Bare sk-/sk-ant-/AIza patterns (no header/query prefix) are masked
        // by the runtime log masker, which shares the secret boundary duty
        // for log sinks. Pin that coverage here so the pattern matrix in the
        // 10A checklist stays green in one place.
        let m = crate::llm::runtime::logs::mask_secrets(
            "saw sk-abc123DEF456ghi789jkl and sk-ant-xyz1234567890abcdef and AIzaSyD-test1234567890 in output",
        );
        assert!(!m.contains("sk-abc123DEF456ghi789jkl"), "bare sk- leaked: {}", m);
        assert!(!m.contains("sk-ant-xyz1234567890abcdef"), "bare sk-ant- leaked: {}", m);
        assert!(!m.contains("AIzaSyD-test1234567890"), "bare AIza leaked: {}", m);
    }
}
