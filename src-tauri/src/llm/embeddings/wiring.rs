//! Production wiring: configured `Provider`/`Model` -> live embedding provider.
//!
//! Stage 10A.1. Before this module, production search commands always used
//! `UnavailableProvider` (lexical fallback only). This module resolves a real
//! [`OpenAiCompatibleEmbeddingProvider`] from the current [`LlmConfig`]:
//!
//! ```text
//! model_id (+ optional registry record)
//!   -> Model (capability Embeddings)
//!   -> Provider (enabled, endpoint)
//!   -> ProviderScope + privacy check (local-only blocks cloud)
//!   -> credential via keyring ONLY (never config/frontend/logs)
//!   -> Bearer provider for OpenAI-compatible kinds, native header-auth
//!      provider for Gemini + EmbeddingModelRef{model_id, fingerprint}
//! ```
//!
//! Security rules:
//! - `api_key` comes only from `load_credential` (production: OS keyring).
//!   It is moved into the provider config, never cloned into errors,
//!   `Debug` output, responses, or frontend payloads.
//! - [`ResolvedEmbedding`] has a redacting `Debug` impl; there is NO getter
//!   for the raw key.
//! - Error messages carry ids/endpoints only — never secret material.

use super::openai_compatible::{OpenAiCompatibleConfig, OpenAiCompatibleEmbeddingProvider};
use super::provider::EmbeddingProvider;
use super::types::EmbeddingModelRef;
use crate::llm::models::identity::verified_id;
use crate::llm::models::registry::Registry;
use crate::llm::models::types::ModelRecord;
use crate::llm::provider::{
    AuthReference, GeminiEmbedParams, GeminiNativeAdapter, OwnedGeminiEmbeddingProvider,
    ProviderConnection, ProviderError, ProviderModel, SecretStore,
};
use crate::llm::task::policy::{ensure_capability, scope_of, validate_privacy, ProviderScope};
use crate::llm::{AuthMethod, Capability, LlmConfig, Model, Privacy, Provider, ProviderKind};

/// Typed wiring failures. Display strings contain ids/endpoints only.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EmbeddingWiringError {
    ModelNotFound(String),
    ProviderNotFound(String),
    ProviderDisabled(String),
    MissingCapability(String),
    PrivacyViolation(String),
    MissingCredential(String),
    FingerprintUnavailable(String),
    InvalidEndpoint(String),
    UnsupportedAuth(String),
}

impl std::fmt::Display for EmbeddingWiringError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ModelNotFound(id) => write!(f, "embedding model not found: {}", id),
            Self::ProviderNotFound(id) => write!(f, "embedding provider not found: {}", id),
            Self::ProviderDisabled(id) => write!(f, "embedding provider disabled: {}", id),
            Self::MissingCapability(msg) => write!(f, "{}", msg),
            Self::PrivacyViolation(msg) => write!(f, "{}", msg),
            Self::MissingCredential(id) => {
                write!(f, "missing credential in keyring for provider: {}", id)
            }
            Self::FingerprintUnavailable(id) => {
                write!(f, "model fingerprint unavailable (model never scanned): {}", id)
            }
            Self::InvalidEndpoint(msg) => write!(f, "invalid embedding endpoint: {}", msg),
            Self::UnsupportedAuth(msg) => write!(f, "unsupported embedding auth: {}", msg),
        }
    }
}

impl std::error::Error for EmbeddingWiringError {}

/// A resolved, ready-to-use production embedding provider.
///
/// The concrete provider is boxed behind [`EmbeddingProvider`]: Bearer-based
/// [`OpenAiCompatibleEmbeddingProvider`] for OpenAI-compatible kinds,
/// header-auth native providers (Gemini) otherwise. Credentials live inside
/// the provider and are intentionally NOT exposed: no getter, redacted
/// `Debug`.
pub struct ResolvedEmbedding {
    pub provider: Box<dyn EmbeddingProvider>,
    pub model_ref: EmbeddingModelRef,
    pub scope: ProviderScope,
    pub endpoint: String,
    pub remote_model: String,
}

impl std::fmt::Debug for ResolvedEmbedding {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ResolvedEmbedding")
            .field("model_ref", &self.model_ref)
            .field("scope", &self.scope)
            .field("endpoint", &self.endpoint)
            .field("remote_model", &self.remote_model)
            .field("provider", &"embedding provider (api_key: ***)")
            .finish()
    }
}

/// Memory-only [`SecretStore`] holding an already-resolved key.
///
/// The key itself comes from the injected credential loader (production:
/// OS keyring). The reference is validated for shape only; the key is
/// never logged, never cloned beyond the request, never exposed.
struct KeySecretStore(String);

impl SecretStore for KeySecretStore {
    fn get(&self, secret_ref: &str) -> Result<String, ProviderError> {
        if secret_ref.trim().is_empty() {
            return Err(ProviderError::CredentialsMissing(
                "empty secret reference".to_string(),
            ));
        }
        if self.0.is_empty() {
            return Err(ProviderError::CredentialsMissing(
                "empty credential".to_string(),
            ));
        }
        Ok(self.0.clone())
    }
}

/// Shared policy gate for embedding AND chat providers (10B reuses this).
/// Returns the computed scope on success so callers can record it.
pub fn validate_task_policy(
    privacy: &Option<Privacy>,
    kind: &ProviderKind,
    endpoint: &str,
) -> Result<ProviderScope, EmbeddingWiringError> {
    let scope = scope_of(kind, endpoint);
    validate_privacy(privacy, &scope)
        .map_err(EmbeddingWiringError::PrivacyViolation)?;
    Ok(scope)
}

/// Deterministic fingerprint for an embedding model.
///
/// - Registry record WITH `sha256` -> `verified_id` (`model-sha256-<16>`).
///   Survives file moves; invalidates vectors when bytes change.
/// - Record WITHOUT sha -> persisted stable `record.id`.
/// - No record + `remote_id` set (cloud model, nothing to scan) ->
///   `remote:<remote_id>`.
/// - Otherwise -> `FingerprintUnavailable` (local file never scanned;
///   scan it before indexing so vectors stay attributable).
pub fn resolve_model_fingerprint(
    model: &Model,
    record: Option<&ModelRecord>,
) -> Result<String, EmbeddingWiringError> {
    if let Some(rec) = record {
        if let Some(sha) = rec.sha256.as_ref().filter(|s| !s.is_empty()) {
            return Ok(verified_id(sha));
        }
        if !rec.id.is_empty() {
            return Ok(rec.id.clone());
        }
    }
    if !model.remote_id.trim().is_empty() {
        return Ok(format!("remote:{}", model.remote_id.trim()));
    }
    Err(EmbeddingWiringError::FingerprintUnavailable(model.id.clone()))
}

/// Scope of the configured embedding model WITHOUT touching credentials.
/// Used by commands to feed the retrieval privacy gate (`Some` = cloud or
/// local embedding path, `None` = model not configured -> lexical only).
/// Never fails on missing credentials — credential errors surface at
/// `resolve_embedding_provider` time.
pub fn resolve_scope_for_model(
    config: &LlmConfig,
    model_id: &str,
) -> Option<ProviderScope> {
    let model = config.models.iter().find(|m| m.id == model_id)?;
    let provider = config.providers.iter().find(|p| p.id == model.provider_id)?;
    if !provider.enabled {
        return None;
    }
    Some(scope_of(&provider.kind, &provider.endpoint))
}

/// Find the registry record belonging to a config model (by path identity).
pub fn find_record_for_model(registry: &Registry, model: &Model) -> Option<ModelRecord> {
    if model.path.trim().is_empty() {
        return None;
    }
    registry.all().into_iter().find(|rec| {
        rec.canonical_path == model.path
            || rec.path.to_string_lossy() == model.path
            || rec.provider_id == model.provider_id
                && rec.path.to_string_lossy().as_ref() == model.path
    })
}

/// Resolve a production embedding provider from config.
///
/// `load_credential` is injected for testability; production passes
/// `crate::llm::get_keyring` (OS keyring only — never config/frontend).
pub fn resolve_embedding_provider(
    config: &LlmConfig,
    model_id: &str,
    record: Option<&ModelRecord>,
    privacy: &Option<Privacy>,
    load_credential: &dyn Fn(&str) -> Result<String, String>,
) -> Result<ResolvedEmbedding, EmbeddingWiringError> {
    let model = config
        .models
        .iter()
        .find(|m| m.id == model_id)
        .ok_or_else(|| EmbeddingWiringError::ModelNotFound(model_id.to_string()))?;

    ensure_capability(&model.capabilities, Capability::Embeddings)
        .map_err(EmbeddingWiringError::MissingCapability)?;

    let provider = config
        .providers
        .iter()
        .find(|p| p.id == model.provider_id)
        .ok_or_else(|| EmbeddingWiringError::ProviderNotFound(model.provider_id.clone()))?;
    if !provider.enabled {
        return Err(EmbeddingWiringError::ProviderDisabled(provider.id.clone()));
    }

    let scope = validate_task_policy(privacy, &provider.kind, &provider.endpoint)?;

    if provider.endpoint.trim().is_empty() {
        return Err(EmbeddingWiringError::InvalidEndpoint(
            "endpoint is empty".to_string(),
        ));
    }

    // Credential ONLY via injected loader (keyring in production).
    let api_key: Option<String> = match provider.auth.as_ref().map(|a| &a.method) {
        None | Some(AuthMethod::None) => None,
        Some(AuthMethod::ApiKey) => {
            let secret = load_credential(&provider.id)
                .map_err(|_| EmbeddingWiringError::MissingCredential(provider.id.clone()))?;
            if secret.is_empty() {
                return Err(EmbeddingWiringError::MissingCredential(provider.id.clone()));
            }
            Some(secret)
        }
        Some(AuthMethod::OAuth) => {
            return Err(EmbeddingWiringError::UnsupportedAuth(
                "OAuth is not supported for embeddings".to_string(),
            ))
        }
    };

    // Application model_id vs remote model name: the request carries the
    // application id; the wire carries remote_model when configured.
    let remote_model = model.remote_id.clone();
    let fingerprint = resolve_model_fingerprint(model, record)?;
    let model_ref = EmbeddingModelRef {
        model_id: model.id.clone(),
        model_fingerprint: fingerprint,
    };

    // Gemini: native header-auth path (never Bearer, never ?key=).
    // Every other kind keeps the Bearer path below, byte-for-byte.
    let endpoint = provider.endpoint.clone();
    if provider.kind == ProviderKind::Gemini {
        return resolve_gemini_embedding(
            model,
            provider,
            api_key,
            remote_model,
            model_ref,
            scope,
            endpoint,
        );
    }

    let cfg = OpenAiCompatibleConfig {
        endpoint: provider.endpoint.clone(),
        api_key,
        remote_model: remote_model.clone(),
        timeout: std::time::Duration::from_secs(30),
        limits: super::types::EmbeddingLimits::default(),
        supports_embeddings: true,
        extra_headers: vec![],
    };
    let endpoint = cfg.endpoint.clone();
    let provider = OpenAiCompatibleEmbeddingProvider::new(cfg)
        .map_err(EmbeddingWiringError::InvalidEndpoint)?;

    Ok(ResolvedEmbedding {
        provider: Box::new(provider),
        model_ref,
        scope,
        endpoint,
        remote_model,
    })
}

/// Native Gemini embedding resolution: header-only auth through
/// [`GeminiNativeAdapter::embed_content`], same [`EmbeddingResponse`]
/// contract, same fingerprint rules. Task-type separation is out of scope:
/// [`GeminiEmbedParams::default`] applies to query and index alike.
#[allow(clippy::too_many_arguments)]
fn resolve_gemini_embedding(
    model: &Model,
    provider: &Provider,
    api_key: Option<String>,
    remote_model: String,
    model_ref: EmbeddingModelRef,
    scope: ProviderScope,
    endpoint: String,
) -> Result<ResolvedEmbedding, EmbeddingWiringError> {
    use crate::llm::models::types::ModelSource;
    use std::sync::Arc;
    let key = api_key
        .filter(|k| !k.is_empty())
        .ok_or_else(|| EmbeddingWiringError::MissingCredential(provider.id.clone()))?;
    let secret_ref = provider
        .auth
        .as_ref()
        .and_then(|a| a.secret_ref.clone())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| EmbeddingWiringError::MissingCredential(provider.id.clone()))?;
    if remote_model.trim().is_empty() {
        return Err(EmbeddingWiringError::InvalidEndpoint(format!(
            "gemini embedding model {} has no remote model name",
            model.id
        )));
    }
    let auth = AuthReference::new(secret_ref)
        .map_err(|_| EmbeddingWiringError::MissingCredential(provider.id.clone()))?;
    let connection = ProviderConnection {
        id: provider.id.clone(),
        kind: ProviderKind::Gemini,
        name: provider.name.clone(),
        enabled: true,
        scope: ProviderScope::Cloud,
        endpoint: Some(endpoint.clone()),
        auth: Some(auth),
    };
    let catalog_model = ProviderModel {
        id: format!("{}:{}", provider.id, remote_model.trim()),
        provider_id: provider.id.clone(),
        remote_id: remote_model.trim().to_string(),
        display_name: model.name.clone(),
        capabilities: model.capabilities.clone(),
        capability_source: None,
        context_length: None,
        dimensions: None,
        pricing: None,
        source: ModelSource::Manual,
    };
    let adapter = GeminiNativeAdapter::new(
        connection,
        Arc::new(KeySecretStore(key)),
        std::time::Duration::from_secs(30),
    )
    .map_err(|e| EmbeddingWiringError::InvalidEndpoint(e.to_string()))?;
    let provider =
        OwnedGeminiEmbeddingProvider::new(adapter, catalog_model, GeminiEmbedParams::default());
    Ok(ResolvedEmbedding {
        provider: Box::new(provider),
        model_ref,
        scope,
        endpoint,
        remote_model,
    })
}
