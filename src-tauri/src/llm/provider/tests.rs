//! Stage 1 registry tests: load/save, duplicates, enable/disable,
//! policy, catalog, capabilities, secret hygiene. No network, no keyring.

use super::types::*;
use crate::llm::task::policy::{validate_privacy, ProviderScope};
use crate::llm::{Capability, CapabilitySource, ProviderKind};
use crate::llm::models::types::ModelSource;

fn local_conn() -> ProviderConnection {
    ProviderConnection {
        id: "local".to_string(),
        kind: ProviderKind::LocalLlamaCpp,
        name: "Local".to_string(),
        enabled: true,
        scope: ProviderScope::LocalManaged,
        endpoint: None,
        auth: None,
    }
}

fn cloud_conn() -> ProviderConnection {
    ProviderConnection {
        id: "openai".to_string(),
        kind: ProviderKind::OpenAI,
        name: "OpenAI".to_string(),
        enabled: true,
        scope: ProviderScope::Cloud,
        endpoint: None,
        auth: Some(AuthReference::new("keychain://fragile-notes/openai".to_string()).unwrap()),
    }
}

fn chat_model() -> ProviderModel {
    ProviderModel {
        id: "gpt-4o-app".to_string(),
        provider_id: "openai".to_string(),
        remote_id: "gpt-4o".to_string(),
        display_name: "GPT-4o".to_string(),
        capabilities: vec![Capability::Chat],
        capability_source: Some(CapabilitySource::StaticProvider),
        context_length: Some(128000),
        dimensions: None,
        pricing: Some(PricingInfo {
            input_usd_per_mtok: 2.5,
            output_usd_per_mtok: 10.0,
        }),
        source: ModelSource::Manual,
    }
}

#[test]
fn registry_load_save_roundtrip() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("providers.json");
    let mut reg = ProviderRegistry::new(path.clone());
    reg.register(local_conn()).unwrap();
    reg.register(cloud_conn()).unwrap();
    reg.add_model(chat_model()).unwrap();
    reg.save().unwrap();

    let mut reg2 = ProviderRegistry::new(path);
    reg2.load().unwrap();
    assert_eq!(reg2.all().len(), 2);
    assert_eq!(reg2.models_for_provider("openai").len(), 1);
    let m = reg2.get_model("gpt-4o-app").unwrap();
    assert_eq!(m.remote_id, "gpt-4o");
    // Three identities stay separate.
    assert_ne!(m.id, m.remote_id);
}

#[test]
fn duplicate_connection_ids_rejected() {
    let dir = tempfile::tempdir().unwrap();
    let mut reg = ProviderRegistry::new(dir.path().join("p.json"));
    reg.register(local_conn()).unwrap();
    let err = reg.register(local_conn()).unwrap_err();
    assert_eq!(err.code(), "duplicate_id");
    assert!(!err.is_retryable());
}

#[test]
fn duplicate_model_ids_rejected() {
    let dir = tempfile::tempdir().unwrap();
    let mut reg = ProviderRegistry::new(dir.path().join("p.json"));
    reg.register(cloud_conn()).unwrap();
    reg.add_model(chat_model()).unwrap();
    let err = reg.add_model(chat_model()).unwrap_err();
    assert_eq!(err.code(), "duplicate_id");
}

#[test]
fn model_requires_known_provider() {
    let dir = tempfile::tempdir().unwrap();
    let mut reg = ProviderRegistry::new(dir.path().join("p.json"));
    let err = reg.add_model(chat_model()).unwrap_err();
    assert_eq!(err.code(), "not_found");
}

#[test]
fn raw_secret_ref_rejected() {
    assert!(AuthReference::new("hf_abc123rawtoken".to_string()).is_err());
    assert!(AuthReference::new("sk-abc".to_string()).is_err());
    assert!(AuthReference::new(String::new()).is_err());
    assert!(AuthReference::new("keychain://fragile-notes/x".to_string()).is_ok());
}

#[test]
fn no_raw_secrets_in_serialization() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("p.json");
    let mut reg = ProviderRegistry::new(path.clone());
    reg.register(cloud_conn()).unwrap();
    reg.save().unwrap();
    let txt = std::fs::read_to_string(&path).unwrap();
    assert!(txt.contains("keychain://fragile-notes/openai"));
    for needle in ["sk-", "hf_", "Bearer ", "api_key\""] {
        assert!(!txt.contains(needle), "leak: {}", needle);
    }
    // Debug never prints the ref.
    let dbg = format!("{:?}", cloud_conn());
    assert!(!dbg.contains("keychain://"));
    assert!(dbg.contains("***"));
}

#[test]
fn enable_disable() {
    let dir = tempfile::tempdir().unwrap();
    let mut reg = ProviderRegistry::new(dir.path().join("p.json"));
    reg.register(cloud_conn()).unwrap();
    reg.set_enabled("openai", false).unwrap();
    assert!(!reg.get("openai").unwrap().enabled);
    assert!(reg.set_enabled("missing", true).is_err());
}

#[test]
fn scope_mismatch_rejected() {
    let dir = tempfile::tempdir().unwrap();
    let mut reg = ProviderRegistry::new(dir.path().join("p.json"));
    let mut bad = cloud_conn();
    bad.scope = ProviderScope::LocalManaged; // disagrees with OpenAI kind
    assert!(reg.register(bad).is_err());
}

#[test]
fn endpoint_rules() {
    let dir = tempfile::tempdir().unwrap();
    let mut reg = ProviderRegistry::new(dir.path().join("p.json"));
    // Cloud requires https.
    let mut c = cloud_conn();
    c.endpoint = Some("http://insecure.example.com".to_string());
    assert!(reg.register(c).is_err());
    // Local requires loopback http(s).
    let mut l = local_conn();
    l.endpoint = Some("https://cloud.example.com".to_string());
    assert!(reg.register(l).is_err());
    let mut l2 = local_conn();
    l2.id = "local2".to_string();
    l2.scope = ProviderScope::LocalExternal;
    l2.kind = ProviderKind::CustomOpenAI;
    l2.endpoint = Some("http://127.0.0.1:8080".to_string());
    assert!(reg.register(l2).is_ok());
}

#[test]
fn local_only_cloud_policy_denies() {
    let dir = tempfile::tempdir().unwrap();
    let mut reg = ProviderRegistry::new(dir.path().join("p.json"));
    reg.register(cloud_conn()).unwrap();
    let scope = reg.get("openai").unwrap().scope.clone();
    assert!(validate_privacy(&Some(crate::llm::Privacy::LocalOnly), &scope).is_err());
    assert!(validate_privacy(&Some(crate::llm::Privacy::CloudAllowed), &scope).is_ok());
}

#[test]
fn remove_drops_catalog_entries() {
    let dir = tempfile::tempdir().unwrap();
    let mut reg = ProviderRegistry::new(dir.path().join("p.json"));
    reg.register(cloud_conn()).unwrap();
    reg.add_model(chat_model()).unwrap();
    reg.remove("openai").unwrap();
    assert!(reg.get_model("gpt-4o-app").is_none());
    assert!(reg.remove("openai").is_err());
}

#[test]
fn capability_filtering() {
    let dir = tempfile::tempdir().unwrap();
    let mut reg = ProviderRegistry::new(dir.path().join("p.json"));
    reg.register(cloud_conn()).unwrap();
    reg.add_model(chat_model()).unwrap();
    assert_eq!(reg.models_with_capability(&Capability::Chat).len(), 1);
    assert!(reg.models_with_capability(&Capability::Embeddings).is_empty());
    // No guessing: empty capabilities means unsupported.
    let m = reg.get_model("gpt-4o-app").unwrap();
    assert!(!m.supports(&Capability::Vision));
}

#[test]
fn default_endpoints() {
    assert_eq!(default_endpoint(&ProviderKind::LocalLlamaCpp), None);
    assert_eq!(
        default_endpoint(&ProviderKind::OpenAI).as_deref(),
        Some("https://api.openai.com/v1")
    );
    assert_eq!(
        default_endpoint(&ProviderKind::DeepSeek).as_deref(),
        Some("https://api.deepseek.com/v1")
    );
    assert_eq!(
        default_endpoint(&ProviderKind::OpenRouter).as_deref(),
        Some("https://openrouter.ai/api/v1")
    );
}

#[test]
fn error_codes_not_retryable_config() {
    assert!(!ProviderError::InvalidConfig("x".to_string()).is_retryable());
    assert!(!ProviderError::UnsupportedCapability("x".to_string()).is_retryable());
    assert!(ProviderError::RateLimited("x".to_string()).is_retryable());
    assert_eq!(ProviderError::Cancelled.code(), "cancelled");
}

#[test]
fn error_status_and_safe_message() {
    let srv = ProviderError::Server {
        status: 503,
        message: "boom".to_string(),
    };
    assert_eq!(srv.status(), Some(503));
    assert!(srv.is_retryable());
    assert!(ProviderError::NotFound("x".to_string()).status().is_none());
    // safe_message re-redacts even a hostile payload.
    let hostile = ProviderError::Network("Authorization: Bearer sk-live-123".to_string());
    assert!(!hostile.safe_message().contains("sk-live-123"));
}
