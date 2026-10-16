//! Stage 0 (v0.5.9): pure chat request builder extracted from `gateway_chat`.
//!
//! URL/headers/body per provider kind. No network, no config, no keyring
//! access here — the caller resolves `secret` (possibly empty) beforehand.
//! Snapshot tests pin the exact URL/headers/body per provider kind so the
//! Stage 2+ adapters cannot drift silently.
//!
//! v0.5.9 (Package 9): version prefixes are joined via
//! [`join_api_path`] — an endpoint already ending in `/v1` no longer yields
//! `/v1/v1/...`. Empty endpoints resolve per kind: local runtimes keep the
//! assigned port, cloud kinds use their documented bases, and CustomOpenAI
//! requires an explicit endpoint (validation error, never a silent fallback).

use crate::llm::provider::openai::join_api_path;
use crate::llm::provider::types::default_endpoint;
use crate::llm::{ChatGeneration, ProviderKind};
use std::collections::HashMap;

/// Fully materialized HTTP request for a chat call (before transport).
#[derive(Debug, Clone, PartialEq)]
pub struct ChatHttpRequest {
    pub url: String,
    pub headers: HashMap<String, String>,
    pub body: serde_json::Value,
}

/// Build URL/headers/body for `kind`.
///
/// * `endpoint` — provider base URL, or empty for built-in defaults.
/// * `default_model` — provider default when `model_ref` is empty.
/// * `default_port` — `cfg.local.port` fallback for local endpoints.
/// * `secret` — pre-resolved credential; empty means "no auth".
#[allow(clippy::too_many_arguments)]
pub fn build_chat_http_request(
    kind: &ProviderKind,
    endpoint: &str,
    default_model: &str,
    default_port: u16,
    model_ref: &str,
    messages: &[serde_json::Value],
    gen: &ChatGeneration,
    secret: &str,
) -> Result<ChatHttpRequest, String> {
    let (url, headers) = match kind {
        ProviderKind::LocalLlamaCpp
        | ProviderKind::Ollama
        | ProviderKind::CustomOpenAI
        | ProviderKind::OpenAI
        | ProviderKind::DeepSeek
        | ProviderKind::OpenRouter => {
            let base = if endpoint.trim().is_empty() {
                match kind {
                    // Local runtime: the assigned port on loopback. Never
                    // applies to cloud kinds (F-7: no localhost guessing).
                    ProviderKind::LocalLlamaCpp => format!("http://127.0.0.1:{}", default_port),
                    // Custom gateways must be explicit: no silent fallback
                    // to localhost or a vendor default.
                    ProviderKind::CustomOpenAI => {
                        return Err(
                            "endpoint is required for custom OpenAI-compatible providers"
                                .to_string(),
                        );
                    }
                    // Cloud kinds use their documented base URLs.
                    _ => default_endpoint(kind).unwrap_or_default(),
                }
            } else {
                endpoint.to_string()
            };
            // Shared layout detection: `/v1` in the endpoint and `/v1/...` in
            // the operation are joined, never doubled.
            let u = join_api_path(&base, "/v1/chat/completions")
                .map_err(|e| e.safe_message())?;
            let mut h = HashMap::new();
            if !secret.is_empty() {
                h.insert(
                    "Authorization".to_string(),
                    format!("Bearer {}", secret),
                );
            }
            (u, h)
        }
        ProviderKind::Gemini => {
            let base = if endpoint.trim().is_empty() {
                default_endpoint(&ProviderKind::Gemini)
                    .unwrap_or_else(|| "https://generativelanguage.googleapis.com".to_string())
            } else {
                endpoint.to_string()
            };
            let mdl = if !model_ref.is_empty() {
                model_ref.to_string()
            } else {
                default_model.to_string()
            };
            // Header-only auth (security: no ?key= in production URLs).
            // The legacy branch shape (endpoint/body) is preserved; only the
            // credential transport changed. Production chat goes through the
            // native dispatch foundation; this builder remains for parity.
            let u = format!(
                "{}/v1beta/models/{}:generateContent",
                base.trim_end_matches('/'),
                mdl
            );
            let mut h = HashMap::new();
            if !secret.is_empty() {
                h.insert("x-goog-api-key".to_string(), secret.to_string());
            }
            (u, h)
        }
        ProviderKind::Claude => {
            let base = if endpoint.trim().is_empty() {
                default_endpoint(&ProviderKind::Claude)
                    .unwrap_or_else(|| "https://api.anthropic.com".to_string())
            } else {
                endpoint.to_string()
            };
            let u = join_api_path(&base, "/v1/messages").map_err(|e| e.safe_message())?;
            let mut h = HashMap::new();
            if !secret.is_empty() {
                h.insert("x-api-key".to_string(), secret.to_string());
            }
            h.insert("anthropic-version".to_string(), "2023-06-01".to_string());
            (u, h)
        }
        // Perplexity has no OpenAI-chat-completions shape; it is served by the
        // dedicated Agent API adapter. Never build a fallback request here —
        // a silent `/v1/chat/completions` call would lose sources and is wrong.
        ProviderKind::Perplexity => {
            return Err(
                "perplexity uses the Agent API dispatch (gateway_perplexity_via_registry), \
                 not the OpenAI-compatible gateway builder"
                    .to_string(),
            );
        }
    };
    let body = match kind {
        ProviderKind::Gemini => {
            let parts: Vec<serde_json::Value> = messages
                .iter()
                .map(|m| {
                    let role = m.get("role").and_then(|r| r.as_str()).unwrap_or("user");
                    let text = m.get("content").and_then(|c| c.as_str()).unwrap_or("");
                    let g_role = if role == "assistant" { "model" } else { "user" };
                    serde_json::json!({"role": g_role, "parts":[{"text": text}]})
                })
                .collect();
            serde_json::json!({"contents": parts, "generationConfig": {"temperature": gen.temperature, "topP": gen.top_p, "maxOutputTokens": gen.max_tokens}})
        }
        ProviderKind::Claude => {
            let sys = messages
                .iter()
                .find(|m| {
                    m.get("role").and_then(|r| r.as_str()) == Some("system")
                })
                .and_then(|m| m.get("content").and_then(|c| c.as_str()))
                .unwrap_or("");
            let msgs: Vec<serde_json::Value> = messages
                .iter()
                .filter(|m| {
                    m.get("role").and_then(|r| r.as_str()) != Some("system")
                })
                .map(|m| {
                    serde_json::json!({"role": m.get("role").unwrap_or(&serde_json::Value::String("user".to_string())), "content": m.get("content").unwrap_or(&serde_json::Value::String("".to_string()))})
                })
                .collect();
            let mut j = serde_json::json!({"model": if !model_ref.is_empty() { model_ref.to_string() } else { default_model.to_string() }, "max_tokens": gen.max_tokens, "temperature": gen.temperature, "messages": msgs});
            if !sys.is_empty() {
                j["system"] = serde_json::Value::String(sys.to_string());
            }
            j
        }
        _ => {
            let mdl = if !model_ref.is_empty() {
                model_ref.to_string()
            } else if !default_model.is_empty() {
                default_model.to_string()
            } else {
                "local".to_string()
            };
            serde_json::json!({"model": mdl, "messages": messages, "temperature": gen.temperature, "top_p": gen.top_p, "max_tokens": gen.max_tokens, "stream": false})
        }
    };
    Ok(ChatHttpRequest { url, headers, body })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gen() -> ChatGeneration {
        ChatGeneration {
            temperature: 0.2,
            top_p: 0.5,
            max_tokens: 512,
        }
    }

    fn msgs() -> Vec<serde_json::Value> {
        vec![
            serde_json::json!({"role": "system", "content": "sys"}),
            serde_json::json!({"role": "user", "content": "hi"}),
            serde_json::json!({"role": "assistant", "content": "hello"}),
        ]
    }

    fn f32eq(v: &serde_json::Value, expected: f64) {
        let a = v.as_f64().expect("number");
        assert!((a - expected).abs() < 1e-6, "{} != {}", a, expected);
    }

    #[test]
    fn openai_compatible_shape() {
        let r = build_chat_http_request(
            &ProviderKind::OpenAI,
            "https://api.openai.com/v1",
            "gpt-4o",
            8010,
            "",
            &msgs(),
            &gen(),
            "sk-test",
        )
        .unwrap();
        // Endpoint already ends with `/v1`: the operation prefix is joined,
        // not doubled (F-1 — the old builder produced /v1/v1/... here).
        assert_eq!(r.url, "https://api.openai.com/v1/chat/completions");
        assert_eq!(
            r.headers.get("Authorization").map(String::as_str),
            Some("Bearer sk-test")
        );
        assert_eq!(r.body["model"], "gpt-4o");
        f32eq(&r.body["temperature"], 0.2);
        f32eq(&r.body["top_p"], 0.5);
        assert_eq!(r.body["max_tokens"], 512);
        assert_eq!(r.body["stream"], false);
        assert_eq!(r.body["messages"].as_array().unwrap().len(), 3);
    }

    #[test]
    fn openai_empty_endpoint_uses_documented_default() {
        let r = build_chat_http_request(
            &ProviderKind::OpenAI,
            "",
            "gpt-4o",
            8010,
            "",
            &msgs(),
            &gen(),
            "sk-test",
        )
        .unwrap();
        // F-7: cloud kinds never fall back to the local llama.cpp port.
        assert_eq!(r.url, "https://api.openai.com/v1/chat/completions");
    }

    #[test]
    fn cloud_empty_endpoints_use_documented_defaults() {
        let ds = build_chat_http_request(
            &ProviderKind::DeepSeek,
            "",
            "deepseek-chat",
            8010,
            "",
            &msgs(),
            &gen(),
            "sk-x",
        )
        .unwrap();
        assert_eq!(ds.url, "https://api.deepseek.com/v1/chat/completions");
        let or = build_chat_http_request(
            &ProviderKind::OpenRouter,
            "",
            "openrouter/model",
            8010,
            "",
            &msgs(),
            &gen(),
            "sk-x",
        )
        .unwrap();
        assert_eq!(or.url, "https://openrouter.ai/api/v1/chat/completions");
    }

    #[test]
    fn perplexity_has_no_openai_gateway_builder_fallback() {
        // The Agent API is not OpenAI chat-completions compatible; the builder
        // must refuse rather than silently emit a `/v1/chat/completions` request.
        let err = build_chat_http_request(
            &ProviderKind::Perplexity,
            "",
            "low",
            8010,
            "",
            &msgs(),
            &gen(),
            "pplx-x",
        )
        .unwrap_err();
        assert!(err.contains("Perplexity") || err.contains("perplexity"), "{}", err);
    }

    #[test]
    fn custom_openai_empty_endpoint_is_validation_error() {
        let err = build_chat_http_request(
            &ProviderKind::CustomOpenAI,
            "",
            "m",
            8010,
            "",
            &msgs(),
            &gen(),
            "sk-x",
        )
        .unwrap_err();
        // Explicit endpoint required — never a silent localhost/OpenAI fallback.
        assert!(err.contains("endpoint is required"), "{}", err);
    }

    #[test]
    fn custom_prefix_endpoint_appends_operation() {
        let r = build_chat_http_request(
            &ProviderKind::CustomOpenAI,
            "https://gw.example.com/openai",
            "m",
            8010,
            "",
            &msgs(),
            &gen(),
            "sk-x",
        )
        .unwrap();
        assert_eq!(r.url, "https://gw.example.com/openai/v1/chat/completions");
    }

    #[test]
    fn local_defaults_and_no_auth_header() {
        let r = build_chat_http_request(
            &ProviderKind::LocalLlamaCpp,
            "",
            "",
            8010,
            "",
            &msgs(),
            &ChatGeneration::default(),
            "",
        )
        .unwrap();
        assert_eq!(r.url, "http://127.0.0.1:8010/v1/chat/completions");
        assert!(r.headers.get("Authorization").is_none());
        assert_eq!(r.body["model"], "local");
        f32eq(&r.body["temperature"], 0.7);
    }

    #[test]
    fn gemini_shape() {
        let r = build_chat_http_request(
            &ProviderKind::Gemini,
            "",
            "gemini-2.0-flash",
            8010,
            "custom-model",
            &msgs(),
            &gen(),
            "key-123",
        )
        .unwrap();
        // Header-only auth: no ?key= in the URL (security invariant).
        assert_eq!(
            r.url,
            "https://generativelanguage.googleapis.com/v1beta/models/custom-model:generateContent"
        );
        assert!(!r.url.contains("?key="), "no query auth");
        assert!(!r.url.contains("key-123"), "no secret in URL");
        assert_eq!(
            r.headers.get("x-goog-api-key").map(String::as_str),
            Some("key-123")
        );
        assert_eq!(r.body["contents"][0]["role"], "user", "system maps to user");
        assert_eq!(r.body["contents"][1]["role"], "user");
        assert_eq!(r.body["contents"][2]["role"], "model", "assistant maps to model");
        f32eq(&r.body["generationConfig"]["temperature"], 0.2);
        f32eq(&r.body["generationConfig"]["topP"], 0.5);
        assert_eq!(r.body["generationConfig"]["maxOutputTokens"], 512);
    }

    #[test]
    fn claude_shape_with_system_split() {
        let r = build_chat_http_request(
            &ProviderKind::Claude,
            "",
            "claude-3-5-sonnet-latest",
            8010,
            "",
            &msgs(),
            &gen(),
            "ant-key",
        )
        .unwrap();
        assert_eq!(r.url, "https://api.anthropic.com/v1/messages");
        assert_eq!(
            r.headers.get("x-api-key").map(String::as_str),
            Some("ant-key")
        );
        assert_eq!(
            r.headers.get("anthropic-version").map(String::as_str),
            Some("2023-06-01")
        );
        assert_eq!(r.body["model"], "claude-3-5-sonnet-latest");
        assert_eq!(r.body["max_tokens"], 512);
        f32eq(&r.body["temperature"], 0.2);
        assert!(r.body.get("top_p").is_none(), "claude gets no top_p");
        assert_eq!(r.body["system"], "sys");
        assert_eq!(r.body["messages"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn trailing_slash_endpoint_trimmed() {
        let r = build_chat_http_request(
            &ProviderKind::Ollama,
            "http://127.0.0.1:11434/",
            "",
            8010,
            "llama3.1",
            &msgs(),
            &gen(),
            "",
        )
        .unwrap();
        assert_eq!(r.url, "http://127.0.0.1:11434/v1/chat/completions");
        assert_eq!(r.body["model"], "llama3.1");
    }
}
