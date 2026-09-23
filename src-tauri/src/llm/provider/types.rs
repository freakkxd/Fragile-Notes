//! Stage 1 (v0.5.9): provider registry contracts.
//!
//! A [`ProviderConnection`] carries identity, kind, explicit [`ProviderScope`],
//! an optional endpoint override, and a keyring secret reference — never a raw
//! key. [`ProviderModel`] separates application id, provider id, and the
//! remote provider model id. [`ProviderRegistry`] persists both atomically.

use crate::llm::task::policy::{scope_of, ProviderScope};
use crate::llm::{Capability, CapabilitySource, ProviderKind};
use crate::llm::models::types::ModelSource;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// Keyring reference — `keychain://...`. Raw tokens are rejected.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuthReference {
    pub secret_ref: String,
}

impl AuthReference {
    pub fn new(secret_ref: String) -> Result<Self, ProviderError> {
        if secret_ref.is_empty() {
            return Err(ProviderError::InvalidConfig(
                "secret_ref is empty".to_string(),
            ));
        }
        if !secret_ref.starts_with("keychain://") {
            return Err(ProviderError::InvalidConfig(
                "secret_ref must be keychain://..., raw token not allowed".to_string(),
            ));
        }
        if secret_ref.contains('\n') || secret_ref.contains(' ') || secret_ref.len() > 256 {
            return Err(ProviderError::InvalidConfig(
                "invalid secret_ref".to_string(),
            ));
        }
        Ok(Self { secret_ref })
    }
}

/// A configured provider connection. No raw keys, no model payload,
/// no runtime-specific settings, no chat-only parameters.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProviderConnection {
    pub id: String,
    pub kind: ProviderKind,
    pub name: String,
    pub enabled: bool,
    pub scope: ProviderScope,
    /// Base URL override. `None` = built-in default for the kind.
    pub endpoint: Option<String>,
    /// Credential reference. `None` for providers that need no auth
    /// (local managed runtimes).
    pub auth: Option<AuthReference>,
}

/// Manual `Debug`: the secret reference is presence-only, never printed.
impl std::fmt::Debug for ProviderConnection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ProviderConnection")
            .field("id", &self.id)
            .field("kind", &self.kind)
            .field("name", &self.name)
            .field("enabled", &self.enabled)
            .field("scope", &self.scope)
            .field("endpoint", &self.endpoint)
            .field("auth", &self.auth.as_ref().map(|_| "***"))
            .finish()
    }
}

impl ProviderConnection {
    pub fn validate(&self) -> Result<(), ProviderError> {
        if self.id.trim().is_empty() {
            return Err(ProviderError::InvalidConfig(
                "provider id is empty".to_string(),
            ));
        }
        if self.name.trim().is_empty() {
            return Err(ProviderError::InvalidConfig(format!(
                "provider {} has empty name",
                self.id
            )));
        }
        let ep = self.endpoint.as_deref().unwrap_or("");
        validate_endpoint_for_scope(&self.scope, ep)?;
        // Stored scope must agree with the derived one — explicit but consistent.
        let derived = scope_of(&self.kind, ep);
        if derived != self.scope {
            return Err(ProviderError::InvalidConfig(format!(
                "provider {} scope {:?} disagrees with kind+endpoint ({:?})",
                self.id, self.scope, derived
            )));
        }
        Ok(())
    }

    /// Effective base URL: override or built-in default (may be `None`
    /// for port-assigned local runtimes).
    pub fn effective_endpoint(&self) -> Option<String> {
        match &self.endpoint {
            Some(e) if !e.trim().is_empty() => Some(e.clone()),
            _ => default_endpoint(&self.kind),
        }
    }
}

/// Built-in base URLs. `None` = assigned at runtime (local managed server).
pub fn default_endpoint(kind: &ProviderKind) -> Option<String> {
    match kind {
        ProviderKind::LocalLlamaCpp => None,
        ProviderKind::Ollama => Some("http://127.0.0.1:11434".to_string()),
        ProviderKind::CustomOpenAI => None,
        ProviderKind::OpenAI => Some("https://api.openai.com/v1".to_string()),
        ProviderKind::DeepSeek => Some("https://api.deepseek.com/v1".to_string()),
        ProviderKind::OpenRouter => Some("https://openrouter.ai/api/v1".to_string()),
        ProviderKind::Gemini => Some("https://generativelanguage.googleapis.com".to_string()),
        ProviderKind::Claude => Some("https://api.anthropic.com".to_string()),
    }
}

fn is_loopback(url: &str) -> bool {
    url.contains("127.0.0.1") || url.contains("localhost") || url.contains("::1")
}

fn validate_endpoint_for_scope(scope: &ProviderScope, endpoint: &str) -> Result<(), ProviderError> {
    if endpoint.trim().is_empty() {
        return Ok(());
    }
    if endpoint.contains(' ') || endpoint.contains('\n') {
        return Err(ProviderError::InvalidConfig(
            "endpoint contains whitespace".to_string(),
        ));
    }
    match scope {
        ProviderScope::Cloud => {
            if !endpoint.starts_with("https://") {
                return Err(ProviderError::InvalidConfig(
                    "cloud provider requires https endpoint".to_string(),
                ));
            }
        }
        ProviderScope::LocalManaged | ProviderScope::LocalExternal => {
            // Local scope with a non-loopback endpoint would bypass
            // local-only policy — reject regardless of scheme.
            if !is_loopback(endpoint) {
                return Err(ProviderError::InvalidConfig(
                    "local provider requires loopback endpoint".to_string(),
                ));
            }
            if !(endpoint.starts_with("https://") || endpoint.starts_with("http://")) {
                return Err(ProviderError::InvalidConfig(
                    "local provider requires http(s) endpoint".to_string(),
                ));
            }
        }
    }
    Ok(())
}

/// Pricing metadata (USD per million tokens). Informational only.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PricingInfo {
    pub input_usd_per_mtok: f64,
    pub output_usd_per_mtok: f64,
}

impl PricingInfo {
    pub fn validate(&self) -> Result<(), ProviderError> {
        if !self.input_usd_per_mtok.is_finite() || !self.output_usd_per_mtok.is_finite() {
            return Err(ProviderError::InvalidConfig(
                "pricing must be finite".to_string(),
            ));
        }
        if self.input_usd_per_mtok < 0.0 || self.output_usd_per_mtok < 0.0 {
            return Err(ProviderError::InvalidConfig(
                "pricing must be non-negative".to_string(),
            ));
        }
        Ok(())
    }
}

/// A model in the provider catalog.
///
/// Three identities stay separate: application `id`, owning `provider_id`,
/// and the provider-side `remote_id` (plus local GGUF record ids elsewhere —
/// never stored here).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProviderModel {
    pub id: String,
    pub provider_id: String,
    pub remote_id: String,
    pub display_name: String,
    pub capabilities: Vec<Capability>,
    pub capability_source: Option<CapabilitySource>,
    pub context_length: Option<u32>,
    pub dimensions: Option<usize>,
    pub pricing: Option<PricingInfo>,
    pub source: ModelSource,
}

impl ProviderModel {
    pub fn validate(&self) -> Result<(), ProviderError> {
        if self.id.trim().is_empty() {
            return Err(ProviderError::InvalidConfig(
                "model id is empty".to_string(),
            ));
        }
        if self.provider_id.trim().is_empty() {
            return Err(ProviderError::InvalidConfig(format!(
                "model {} has empty provider_id",
                self.id
            )));
        }
        if self.remote_id.trim().is_empty() {
            return Err(ProviderError::InvalidConfig(format!(
                "model {} has empty remote_id",
                self.id
            )));
        }
        if let Some(d) = self.dimensions {
            if d == 0 {
                return Err(ProviderError::InvalidConfig(format!(
                    "model {} has zero dimensions",
                    self.id
                )));
            }
        }
        if let Some(p) = &self.pricing {
            p.validate()?;
        }
        Ok(())
    }

    pub fn supports(&self, cap: &Capability) -> bool {
        self.capabilities.contains(cap)
    }
}

/// Normalized provider failures. No secret material in any variant.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ProviderError {
    UnsupportedCapability(String),
    Unauthorized(String),
    NotFound(String),
    RateLimited(String),
    Timeout(String),
    Server { status: u16, message: String },
    Network(String),
    InvalidConfig(String),
    DuplicateId(String),
    InvalidRequest(String),
    MalformedResponse(String),
    CredentialsMissing(String),
    KeyringUnavailable(String),
    Cancelled,
}

impl ProviderError {
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            Self::RateLimited(_) | Self::Timeout(_) | Self::Server { .. } | Self::Network(_)
        )
    }

    pub fn code(&self) -> &'static str {
        match self {
            Self::UnsupportedCapability(_) => "unsupported_capability",
            Self::Unauthorized(_) => "unauthorized",
            Self::NotFound(_) => "not_found",
            Self::RateLimited(_) => "rate_limited",
            Self::Timeout(_) => "timeout",
            Self::Server { .. } => "server_error",
            Self::Network(_) => "network",
            Self::InvalidConfig(_) => "invalid_config",
            Self::DuplicateId(_) => "duplicate_id",
            Self::InvalidRequest(_) => "invalid_request",
            Self::MalformedResponse(_) => "malformed_response",
            Self::CredentialsMissing(_) => "credentials_missing",
            Self::KeyringUnavailable(_) => "keyring_unavailable",
            Self::Cancelled => "cancelled",
        }
    }
}

impl std::fmt::Display for ProviderError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnsupportedCapability(c) => write!(f, "unsupported capability: {}", c),
            Self::Unauthorized(m) => write!(f, "unauthorized: {}", m),
            Self::NotFound(m) => write!(f, "not found: {}", m),
            Self::RateLimited(m) => write!(f, "rate limited: {}", m),
            Self::Timeout(m) => write!(f, "timeout: {}", m),
            Self::Server { status, message } => write!(f, "server error {}: {}", status, message),
            Self::Network(m) => write!(f, "network: {}", m),
            Self::InvalidConfig(m) => write!(f, "invalid config: {}", m),
            Self::DuplicateId(id) => write!(f, "duplicate id: {}", id),
            Self::InvalidRequest(m) => write!(f, "invalid request: {}", m),
            Self::MalformedResponse(m) => write!(f, "malformed response: {}", m),
            Self::CredentialsMissing(m) => write!(f, "credentials missing: {}", m),
            Self::KeyringUnavailable(m) => write!(f, "keyring unavailable: {}", m),
            Self::Cancelled => write!(f, "cancelled"),
        }
    }
}

impl std::error::Error for ProviderError {}

/// Persistent provider + catalog store. Atomic JSON file, no raw secrets
/// by construction ([`AuthReference`] rejects them at the boundary).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
struct RegistryFile {
    #[serde(default)]
    connections: Vec<ProviderConnection>,
    #[serde(default)]
    models: Vec<ProviderModel>,
}

pub struct ProviderRegistry {
    path: PathBuf,
    connections: HashMap<String, ProviderConnection>,
    models: HashMap<String, ProviderModel>,
}

impl ProviderRegistry {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            connections: HashMap::new(),
            models: HashMap::new(),
        }
    }

    pub fn load(&mut self) -> Result<(), ProviderError> {
        if !self.path.exists() {
            return Ok(());
        }
        let txt =
            std::fs::read_to_string(&self.path).map_err(|e| ProviderError::Network(e.to_string()))?;
        let file: RegistryFile =
            serde_json::from_str(&txt).map_err(|e| ProviderError::InvalidConfig(e.to_string()))?;
        let mut connections = HashMap::new();
        for c in file.connections {
            c.validate()?;
            if connections.insert(c.id.clone(), c).is_some() {
                return Err(ProviderError::DuplicateId(
                    "duplicate connection in file".to_string(),
                ));
            }
        }
        let mut models = HashMap::new();
        for m in file.models {
            m.validate()?;
            if !connections.contains_key(&m.provider_id) {
                return Err(ProviderError::InvalidConfig(format!(
                    "model {} references unknown provider {}",
                    m.id, m.provider_id
                )));
            }
            if models.insert(m.id.clone(), m).is_some() {
                return Err(ProviderError::DuplicateId(
                    "duplicate model in file".to_string(),
                ));
            }
        }
        self.connections = connections;
        self.models = models;
        Ok(())
    }

    pub fn save(&self) -> Result<(), ProviderError> {
        let file = RegistryFile {
            connections: self.connections.values().cloned().collect(),
            models: self.models.values().cloned().collect(),
        };
        let txt = serde_json::to_string_pretty(&file)
            .map_err(|e| ProviderError::InvalidConfig(e.to_string()))?;
        if let Some(parent) = self.path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)
                    .map_err(|e| ProviderError::Network(e.to_string()))?;
            }
        }
        // Atomic write: tmp + rename. No secrets exist by construction,
        // but the ref allowlist is re-checked by tests on the file bytes.
        let tmp = self.path.with_extension("tmp");
        std::fs::write(&tmp, txt).map_err(|e| ProviderError::Network(e.to_string()))?;
        std::fs::rename(&tmp, &self.path).map_err(|e| ProviderError::Network(e.to_string()))?;
        Ok(())
    }

    pub fn register(&mut self, conn: ProviderConnection) -> Result<(), ProviderError> {
        conn.validate()?;
        if self.connections.contains_key(&conn.id) {
            return Err(ProviderError::DuplicateId(conn.id));
        }
        self.connections.insert(conn.id.clone(), conn);
        Ok(())
    }

    pub fn remove(&mut self, id: &str) -> Result<(), ProviderError> {
        if self.connections.remove(id).is_none() {
            return Err(ProviderError::NotFound(id.to_string()));
        }
        // Catalog entries of a removed provider must not linger.
        self.models.retain(|_, m| m.provider_id != id);
        Ok(())
    }

    pub fn set_enabled(&mut self, id: &str, enabled: bool) -> Result<(), ProviderError> {
        match self.connections.get_mut(id) {
            Some(c) => {
                c.enabled = enabled;
                Ok(())
            }
            None => Err(ProviderError::NotFound(id.to_string())),
        }
    }

    pub fn get(&self, id: &str) -> Option<&ProviderConnection> {
        self.connections.get(id)
    }

    pub fn all(&self) -> Vec<&ProviderConnection> {
        self.connections.values().collect()
    }

    pub fn add_model(&mut self, model: ProviderModel) -> Result<(), ProviderError> {
        model.validate()?;
        if !self.connections.contains_key(&model.provider_id) {
            return Err(ProviderError::NotFound(format!(
                "provider {} for model {}",
                model.provider_id, model.id
            )));
        }
        if self.models.contains_key(&model.id) {
            return Err(ProviderError::DuplicateId(model.id));
        }
        self.models.insert(model.id.clone(), model);
        Ok(())
    }

    pub fn get_model(&self, id: &str) -> Option<&ProviderModel> {
        self.models.get(id)
    }

    pub fn models_for_provider(&self, provider_id: &str) -> Vec<&ProviderModel> {
        self.models
            .values()
            .filter(|m| m.provider_id == provider_id)
            .collect()
    }

    pub fn models_with_capability(&self, cap: &Capability) -> Vec<&ProviderModel> {
        self.models.values().filter(|m| m.supports(cap)).collect()
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}
