//! Stage 0 (v0.5.9): pure chat request builder extracted from `gateway_chat`.
//!
//! Byte-identical behavior to the inline `match prov.kind` branches that lived
//! in `llm.rs`. No network, no config, no keyring access here — the caller
//! resolves `secret` (possibly empty) beforehand. Snapshot tests pin the exact
//! URL/headers/body per provider kind so the Stage 2+ adapters cannot drift
//! silently.

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
) -> ChatHttpRequest {
    let (url, headers) = match kind {
        ProviderKind::LocalLlamaCpp
        | ProviderKind::Ollama
        | ProviderKind::CustomOpenAI
        | ProviderKind::OpenAI
        | ProviderKind::DeepSeek
        | ProviderKind::OpenRouter => {
            let base = if endpoint.is_empty() {
                format!("http://127.0.0.1:{}", default_port)
            } else {
                endpoint.to_string()
            };
            let u = format!("{}/v1/chat/completions", base.trim_end_matches('/'));
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
            let base = if endpoint.is_empty() {
                "https://generativelanguage.googleapis.com".to_string()
            } else {
                endpoint.to_string()
            };
            let mdl = if !model_ref.is_empty() {
                model_ref.to_string()
            } else {
                default_model.to_string()
            };
            let u = format!(
                "{}/v1beta/models/{}:generateContent?key={}",
                base.trim_end_matches('/'),
                mdl,
                secret
            );
            (u, HashMap::new())
        }
        ProviderKind::Claude => {
            let base = if endpoint.is_empty() {
                "https://api.anthropic.com".to_string()
            } else {
                endpoint.to_string()
            };
            let u = format!("{}/v1/messages", base.trim_end_matches('/'));
            let mut h = HashMap::new();
            if !secret.is_empty() {
                h.insert("x-api-key".to_string(), secret.to_string());
            }
            h.insert("anthropic-version".to_string(), "2023-06-01".to_string());
            (u, h)
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
    ChatHttpRequest { url, headers, body }
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
        );
        // NOTE: Stage 0 freezes existing behavior verbatim, including the
        // doubled `/v1` segment when the endpoint already ends with `/v1`.
        assert_eq!(r.url, "https://api.openai.com/v1/v1/chat/completions");
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
        );
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
        );
        assert_eq!(
            r.url,
            "https://generativelanguage.googleapis.com/v1beta/models/custom-model:generateContent?key=key-123"
        );
        assert!(r.headers.is_empty());
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
        );
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
        );
        assert_eq!(r.url, "http://127.0.0.1:11434/v1/chat/completions");
        assert_eq!(r.body["model"], "llama3.1");
    }
}
