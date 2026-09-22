//! Stage 10A.1 tests: production embedding wiring from config.
//!
//! Fake credential loader only — no OS keyring, no network, no vault.

use super::wiring::{
    find_record_for_model, resolve_embedding_provider, resolve_model_fingerprint,
    validate_task_policy, EmbeddingWiringError,
};
use crate::llm::models::registry::Registry;
use crate::llm::models::types::{
    IdentityStatus, ModelFormat, ModelMetadata, ModelRecord, ModelRole, ModelSource, ModelState,
};
use crate::llm::task::policy::ProviderScope;
use crate::llm::{
    AuthMethod, AuthRef, Capability, LlmConfig, Model, Privacy, Provider, ProviderKind,
};
use std::collections::HashMap;

// ---------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------

fn provider(id: &str, kind: ProviderKind, endpoint: &str, auth: Option<AuthMethod>) -> Provider {
    Provider {
        id: id.to_string(),
        name: id.to_string(),
        kind,
        enabled: true,
        endpoint: endpoint.to_string(),
        auth: auth.map(|method| AuthRef {
            method,
            secret_ref: None,
            account_id: None,
        }),
        default_model: String::new(),
        extra: HashMap::new(),
    }
}

fn model(id: &str, provider_id: &str, remote_id: &str, caps: Vec<Capability>) -> Model {
    Model {
        id: id.to_string(),
        provider_id: provider_id.to_string(),
        name: id.to_string(),
        path: String::new(),
        remote_id: remote_id.to_string(),
        capabilities: caps,
        capability_source: None,
        metadata: crate::llm::ModelMetadata::default(),
    }
}

fn config_with(p: Provider, m: Model) -> LlmConfig {
    let mut cfg = LlmConfig::default();
    cfg.providers = vec![p];
    cfg.models = vec![m];
    cfg
}

fn record(id: &str, sha: Option<&str>) -> ModelRecord {
    ModelRecord {
        id: id.to_string(),
        provider_id: "local".to_string(),
        path: std::path::PathBuf::from("/models/emb.gguf"),
        canonical_path: "/models/emb.gguf".to_string(),
        filename: "emb.gguf".to_string(),
        format: ModelFormat::Gguf,
        size_bytes: 1024,
        sha256: sha.map(|s| s.to_string()),
        metadata: ModelMetadata::default(),
        capabilities: vec![Capability::Embeddings],
        roles: vec![ModelRole::Embedding],
        source: ModelSource::LocalFile,
        state: ModelState::Present,
        identity_status: IdentityStatus::Verified,
        first_seen_at: chrono::Utc::now(),
        last_seen_at: chrono::Utc::now(),
        diagnostics: vec![],
    }
}

fn no_credential(_id: &str) -> Result<String, String> {
    Err("keyring get failed".to_string())
}

// ---------------------------------------------------------------------------
// Resolution
// ---------------------------------------------------------------------------

#[test]
fn real_provider_resolved_from_config() {
    let p = provider("openai", ProviderKind::OpenAI, "https://api.openai.com", None);
    let m = model(
        "emb-app-id",
        "openai",
        "text-embedding-3-small",
        vec![Capability::Embeddings],
    );
    let cfg = config_with(p, m);
    let rec = record("model-abc", Some("deadbeefcafebabe0123456789abcdef"));
    let out = resolve_embedding_provider(&cfg, "emb-app-id", Some(&rec), &None, &no_credential)
        .expect("must resolve");
    // Application model_id is preserved for requests ...
    assert_eq!(out.model_ref.model_id, "emb-app-id");
    // ... while the wire model differs (remote mapping) ...
    assert_eq!(out.remote_model, "text-embedding-3-small");
    // ... and the fingerprint follows the installed bytes.
    assert_eq!(out.model_ref.model_fingerprint, "model-sha256-deadbeefcafebabe");
    assert_eq!(out.scope, ProviderScope::Cloud);
    assert_eq!(out.endpoint, "https://api.openai.com");
}

#[test]
fn keyring_credential_used_without_leaking() {
    let p = provider(
        "openai",
        ProviderKind::OpenAI,
        "https://api.openai.com",
        Some(AuthMethod::ApiKey),
    );
    let m = model("e1", "openai", "text-embedding-3-small", vec![Capability::Embeddings]);
    let cfg = config_with(p, m);
    let loader = |id: &str| {
        assert_eq!(id, "openai"); // keyed by provider id only
        Ok("sk-test-secret-value".to_string())
    };
    let out =
        resolve_embedding_provider(&cfg, "e1", None, &None, &loader).expect("must resolve");
    // Debug must not contain the raw key ...
    let dbg = format!("{:?}", out);
    assert!(!dbg.contains("sk-test-secret-value"));
    assert!(dbg.contains("REDACTED") || dbg.contains("***"));
    // ... and neither do typed error Displays.
    let err = EmbeddingWiringError::MissingCredential("openai".to_string()).to_string();
    assert!(!err.contains("sk-test"));
}

#[test]
fn missing_model_provider_disabled_capability_errors_are_typed() {
    let p = provider("openai", ProviderKind::OpenAI, "https://api.openai.com", None);
    let m = model("e1", "openai", "r1", vec![Capability::Embeddings]);
    let cfg = config_with(p, m);
    assert_eq!(
        resolve_embedding_provider(&cfg, "nope", None, &None, &no_credential).unwrap_err(),
        EmbeddingWiringError::ModelNotFound("nope".to_string())
    );

    // Chat-only model is rejected for embeddings.
    let p2 = provider("openai", ProviderKind::OpenAI, "https://api.openai.com", None);
    let m2 = model("chat", "openai", "gpt-x", vec![Capability::Chat]);
    let cfg2 = config_with(p2, m2);
    assert!(matches!(
        resolve_embedding_provider(&cfg2, "chat", None, &None, &no_credential).unwrap_err(),
        EmbeddingWiringError::MissingCapability(_)
    ));

    // Disabled provider is rejected.
    let mut p3 = provider("openai", ProviderKind::OpenAI, "https://api.openai.com", None);
    p3.enabled = false;
    let m3 = model("e1", "openai", "r1", vec![Capability::Embeddings]);
    let cfg3 = config_with(p3, m3);
    assert_eq!(
        resolve_embedding_provider(&cfg3, "e1", None, &None, &no_credential).unwrap_err(),
        EmbeddingWiringError::ProviderDisabled("openai".to_string())
    );

    // ApiKey without keyring entry is rejected (no silent unauthenticated call).
    let p4 = provider(
        "openai",
        ProviderKind::OpenAI,
        "https://api.openai.com",
        Some(AuthMethod::ApiKey),
    );
    let m4 = model("e1", "openai", "r1", vec![Capability::Embeddings]);
    let cfg4 = config_with(p4, m4);
    assert_eq!(
        resolve_embedding_provider(&cfg4, "e1", None, &None, &no_credential).unwrap_err(),
        EmbeddingWiringError::MissingCredential("openai".to_string())
    );

    // OAuth is explicitly unsupported for embeddings.
    let p5 = provider(
        "claude",
        ProviderKind::Claude,
        "https://api.anthropic.com",
        Some(AuthMethod::OAuth),
    );
    let m5 = model("e1", "claude", "r1", vec![Capability::Embeddings]);
    let cfg5 = config_with(p5, m5);
    assert!(matches!(
        resolve_embedding_provider(&cfg5, "e1", None, &None, &no_credential).unwrap_err(),
        EmbeddingWiringError::UnsupportedAuth(_)
    ));
}

#[test]
fn local_only_blocks_cloud_embeddings() {
    let p = provider("openai", ProviderKind::OpenAI, "https://api.openai.com", None);
    let m = model("e1", "openai", "r1", vec![Capability::Embeddings]);
    let cfg = config_with(p, m);
    let err = resolve_embedding_provider(
        &cfg,
        "e1",
        None,
        &Some(Privacy::LocalOnly),
        &no_credential,
    )
    .unwrap_err();
    assert!(matches!(err, EmbeddingWiringError::PrivacyViolation(_)));
    assert!(err.to_string().contains("PrivacyPolicyViolation"));
}

#[test]
fn local_only_allows_local_providers() {
    // Ollama / localhost custom endpoints are local — allowed under LocalOnly.
    for (kind, endpoint) in [
        (ProviderKind::Ollama, "http://127.0.0.1:11434"),
        (ProviderKind::CustomOpenAI, "http://localhost:8010"),
        (ProviderKind::LocalLlamaCpp, ""),
    ] {
        let mut p = provider("loc", kind, endpoint, Some(AuthMethod::None));
        if matches!(p.kind, ProviderKind::LocalLlamaCpp) {
            p.endpoint = "http://127.0.0.1:8010".to_string();
        }
        let m = model("e1", "loc", "", vec![Capability::Embeddings]);
        let cfg = config_with(p, m);
        let out = resolve_embedding_provider(
            &cfg,
            "e1",
            None,
            &Some(Privacy::LocalOnly),
            &no_credential,
        );
        // LocalLlamaCpp without record/remote_id has no fingerprint -> unavailable,
        // but it must NOT fail with PrivacyViolation.
        match out {
            Ok(_) => {}
            Err(e) => assert!(
                !matches!(e, EmbeddingWiringError::PrivacyViolation(_)),
                "local provider blocked: {:?}",
                e
            ),
        }
    }
}

#[test]
fn shared_policy_gate_blocks_cloud_chat_too() {
    // Same validate_task_policy is reused for the 10B chat path.
    let scope = validate_task_policy(
        &Some(Privacy::LocalOnly),
        &ProviderKind::OpenAI,
        "https://api.openai.com",
    )
    .unwrap_err();
    assert!(matches!(scope, EmbeddingWiringError::PrivacyViolation(_)));
    let scope = validate_task_policy(
        &Some(Privacy::LocalOnly),
        &ProviderKind::Ollama,
        "http://127.0.0.1:11434",
    )
    .expect("local chat allowed");
    assert_eq!(scope, ProviderScope::LocalExternal);
}

// ---------------------------------------------------------------------------
// Fingerprints
// ---------------------------------------------------------------------------

#[test]
fn fingerprint_prefers_verified_sha_then_stable_then_remote() {
    let m = model("e1", "openai", "text-embedding-3-small", vec![Capability::Embeddings]);
    let rec = record("model-abc", Some("0123456789abcdef0123456789abcdef"));
    assert_eq!(
        resolve_model_fingerprint(&m, Some(&rec)).unwrap(),
        "model-sha256-0123456789abcdef"
    );
    let rec2 = record("model-stable-9", None);
    assert_eq!(
        resolve_model_fingerprint(&m, Some(&rec2)).unwrap(),
        "model-stable-9"
    );
    // Cloud model without a local record: deterministic remote fingerprint.
    assert_eq!(
        resolve_model_fingerprint(&m, None).unwrap(),
        "remote:text-embedding-3-small"
    );
    // Local file model, never scanned, no remote id -> explicit error.
    let m_local = model("loc", "local", "", vec![Capability::Embeddings]);
    assert_eq!(
        resolve_model_fingerprint(&m_local, None).unwrap_err(),
        EmbeddingWiringError::FingerprintUnavailable("loc".to_string())
    );
}

#[test]
fn fingerprint_passed_to_retrieval_model_ref() {
    // The resolved model_ref is what retrieval uses for has_vectors() and
    // vector search (model_id + fingerprint isolation).
    let p = provider("o", ProviderKind::Ollama, "http://127.0.0.1:11434", None);
    let m = model("emb", "o", "nomic-embed", vec![]);
    let cfg = config_with(p, m);
    let rec = record("model-xyz", Some("aabbccddeeff00112233445566778899"));
    let out =
        resolve_embedding_provider(&cfg, "emb", Some(&rec), &None, &no_credential).unwrap();
    assert_eq!(out.model_ref.model_id, "emb");
    assert_eq!(out.model_ref.model_fingerprint, "model-sha256-aabbccddeeff0011");
}

#[test]
fn find_record_matches_by_path() {
    use crate::llm::models::types::ScanResult;
    let mut reg = Registry::new(std::path::PathBuf::from("/tmp/10a-test-registry.json"));
    let rec = record("model-abc", Some("fff"));
    reg.update_with_scan(ScanResult {
        discovered: vec![rec],
        added: vec![],
        updated: vec![],
        missing: vec![],
        invalid: vec![],
    });
    let mut m = model("e1", "local", "", vec![Capability::Embeddings]);
    m.path = "/models/emb.gguf".to_string();
    let found = find_record_for_model(&reg, &m).expect("record must match by path");
    assert_eq!(found.id, "model-abc");
    m.path = "/models/other.gguf".to_string();
    assert!(find_record_for_model(&reg, &m).is_none());
    m.path = String::new();
    assert!(find_record_for_model(&reg, &m).is_none());
}

#[test]
fn scope_resolved_without_credentials() {
    use super::wiring::resolve_scope_for_model;
    use crate::llm::task::policy::ProviderScope;
    // Cloud model with ApiKey but no keyring entry: scope still resolves
    // (credential errors surface only at full resolution time).
    let p = provider(
        "openai",
        ProviderKind::OpenAI,
        "https://api.openai.com",
        Some(AuthMethod::ApiKey),
    );
    let m = model("e1", "openai", "r1", vec![Capability::Embeddings]);
    let cfg = config_with(p, m);
    assert_eq!(
        resolve_scope_for_model(&cfg, "e1"),
        Some(ProviderScope::Cloud)
    );
    assert_eq!(resolve_scope_for_model(&cfg, "missing"), None);
    let mut p2 = provider("o", ProviderKind::Ollama, "http://127.0.0.1:11434", None);
    p2.enabled = false;
    let m2 = model("e2", "o", "", vec![Capability::Embeddings]);
    let cfg2 = config_with(p2, m2);
    assert_eq!(resolve_scope_for_model(&cfg2, "e2"), None);
}
