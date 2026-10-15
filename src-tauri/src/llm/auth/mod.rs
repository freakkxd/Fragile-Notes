//! Browser-based credential acquisition (OAuth 2.0 + PKCE).
//!
//! This module never hands a code, verifier, or issued token to the frontend.
//! The backend owns the whole transaction: it binds a loopback listener, the
//! browser authorizes against the provider, the callback is validated, and the
//! resulting credential is written through the F-9 keyring funnel. The frontend
//! only sees a safe status (`pending`/`connected`/`cancelled`/`failed`/
//! `expired`) plus the authorization URL it must open in the system browser.
pub mod openrouter;

use once_cell::sync::Lazy;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use std::collections::HashMap;
use std::sync::Mutex;

use crate::llm::{LlmConfig, ProviderKind, Provider, TaskProfile};

/// How long a pending authorization transaction stays valid. Matches the
/// expected human interaction window; expired transactions are pruned and
/// reported as `expired`.
pub const TX_TTL_MS: i64 = 5 * 60 * 1000;
/// Upper bound on concurrent pending transactions. Oldest finished entries are
/// evicted first, so a stuck flow cannot grow the store without limit.
pub const MAX_TXS: usize = 8;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OAuthTxStatus {
    Pending,
    Processing,
    Connected,
    Cancelled,
    Failed,
    Expired,
}

/// A single browser-authorization transaction. Never serialized to the
/// frontend as-is: the status command projects only safe fields.
#[derive(Debug, Clone)]
pub struct PendingTx {
    pub id: String,
    pub state: String,
    pub verifier: String,
    pub challenge: String,
    pub provider_id: String,
    pub provider_kind: ProviderKind,
    /// Canonical provider endpoint (query/userinfo stripped). Safe to show.
    pub destination: String,
    /// Fingerprint of the provider+task binding captured at `start`. Used to
    /// refuse persisting a credential into a provider that changed mid-flow.
    pub config_fingerprint: String,
    pub port: u16,
    pub created_at_ms: i64,
    pub expires_at_ms: i64,
    pub status: OAuthTxStatus,
    /// Safe, secret-free error text (never a code/verifier/key).
    pub error: Option<String>,
}

pub static OAUTH_TXS: Lazy<Mutex<HashMap<String, PendingTx>>> =
    Lazy::new(|| Mutex::new(HashMap::new()));

pub fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

/// CSPRNG bytes, URL-safe base64 without padding.
pub fn random_b64url(nbytes: usize) -> String {
    use rand::RngCore;
    let mut buf = vec![0u8; nbytes];
    rand::rngs::OsRng.fill_bytes(&mut buf);
    URL_SAFE_NO_PAD.encode(buf)
}

/// PKCE S256 challenge: `base64url(SHA256(ascii(verifier)))`.
pub fn s256_challenge(verifier: &str) -> String {
    let digest = Sha256::digest(verifier.as_bytes());
    URL_SAFE_NO_PAD.encode(digest)
}

/// Canonical destination used for consent binding and display: scheme, host,
/// port and path only. Drops query, fragment and userinfo so a rotating query
/// (?api-version=...) or an embedded credential can never affect the binding.
pub fn canonical_destination(endpoint: &str) -> String {
    match url::Url::parse(endpoint.trim()) {
        Ok(mut u) => {
            let _ = u.set_query(None);
            let _ = u.set_fragment(None);
            let _ = u.set_username("");
            let _ = u.set_password(None);
            u.to_string()
        }
        Err(_) => endpoint.trim().to_string(),
    }
}

/// Stable fingerprint of a provider identity (id + kind + canonical endpoint).
/// Captured when a browser-authorization transaction starts so a credential is
/// never written into a provider that was deleted, retyped or repointed
/// mid-flow.
pub fn provider_fingerprint(provider: &Provider) -> String {
    let canonical = format!(
        "provider={}\u{1f}kind={:?}\u{1f}dest={}",
        provider.id,
        provider.kind,
        canonical_destination(&provider.endpoint),
    );
    URL_SAFE_NO_PAD.encode(Sha256::digest(canonical.as_bytes()))
}

/// Stable fingerprint of the (provider endpoint+kind, task privacy+cloud
/// policy) binding. Any change to endpoint, provider identity, task privacy or
/// cloud policy changes the fingerprint, which invalidates a previously granted
/// AskOnce consent and aborts a mid-flight credential write.
pub fn cloud_binding_fingerprint(
    provider: &Provider,
    profile: &TaskProfile,
) -> String {
    let canonical = format!(
        "provider={}\u{1f}kind={:?}\u{1f}dest={}\u{1f}privacy={:?}\u{1f}policy={:?}",
        provider.id,
        provider.kind,
        canonical_destination(&provider.endpoint),
        profile.privacy,
        profile.cloud_policy,
    );
    URL_SAFE_NO_PAD.encode(Sha256::digest(canonical.as_bytes()))
}

/// Resolve the provider+profile binding for a given (provider_id, profile_id)
/// against the live config. `None` when either side is missing.
pub fn binding_fingerprint(
    cfg: &LlmConfig,
    provider_id: &str,
    profile_id: &str,
) -> Option<String> {
    let prov = cfg.providers.iter().find(|p| p.id == provider_id)?;
    let prof = cfg.task_profiles.iter().find(|t| t.id == profile_id)?;
    Some(cloud_binding_fingerprint(prov, prof))
}

/// Mark every `pending`/`processing` transaction past its deadline as
/// `expired`, then bound the store to [`MAX_TXS`] by evicting the oldest
/// finished entries.
pub fn prune_txs() {
    let now = now_ms();
    let mut map = OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner());
    for tx in map.values_mut() {
        if now > tx.expires_at_ms
            && matches!(tx.status, OAuthTxStatus::Pending | OAuthTxStatus::Processing)
        {
            tx.status = OAuthTxStatus::Expired;
        }
    }
    if map.len() > MAX_TXS {
        let mut ids: Vec<(String, i64)> =
            map.iter().map(|(k, t)| (k.clone(), t.created_at_ms)).collect();
        ids.sort_by_key(|(_, created)| *created);
        let excess = map.len() - MAX_TXS;
        for (id, _) in ids.into_iter().take(excess) {
            // Never evict a live in-flight transaction; only finished ones.
            if let Some(t) = map.get(&id) {
                if matches!(
                    t.status,
                    OAuthTxStatus::Pending | OAuthTxStatus::Processing
                ) {
                    continue;
                }
            }
            map.remove(&id);
        }
    }
}

/// Project a transaction to the only fields the frontend may see. Deliberately
/// omits `state`, `verifier`, `challenge` and any credential material.
pub fn safe_tx_json(tx: &PendingTx) -> serde_json::Value {
    serde_json::json!({
        "tx_id": tx.id,
        "status": tx.status,
        "provider_id": tx.provider_id,
        "destination": tx.destination,
        "error": tx.error,
    })
}

#[cfg(test)]
pub(crate) fn clear_txs_for_test() {
    OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner()).clear();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pkce_verifier_and_challenge_have_expected_shape() {
        let v = random_b64url(32);
        assert_eq!(v.len(), 43, "32 CSPRNG bytes -> 43 base64url chars");
        assert!(v
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'));
        let c = s256_challenge(&v);
        // S256 of 32 bytes -> 32 bytes -> 43 base64url chars.
        assert_eq!(c.len(), 43);
        // Deterministic and different from the verifier.
        assert_eq!(c, s256_challenge(&v));
        assert_ne!(c, v);
    }

    #[test]
    fn canonical_destination_strips_query_fragment_and_userinfo() {
        assert_eq!(
            canonical_destination("https://user:pass@openrouter.ai/api/v1?x=1#frag"),
            "https://openrouter.ai/api/v1"
        );
        assert_eq!(
            canonical_destination("  https://api.openai.com/v1/  "),
            "https://api.openai.com/v1/"
        );
    }

    #[test]
    fn fingerprint_changes_with_endpoint_and_policy() {
        let mut cfg = crate::llm::LlmConfig::default();
        let base = binding_fingerprint(&cfg, "claude", "task-chat").unwrap();
        // Endpoint change invalidates the binding.
        cfg.providers.iter_mut().find(|p| p.id == "claude").unwrap().endpoint =
            "https://api.anthropic.com/v2".to_string();
        assert_ne!(base, binding_fingerprint(&cfg, "claude", "task-chat").unwrap());
        // Cloud policy change invalidates the binding.
        let mut cfg2 = crate::llm::LlmConfig::default();
        cfg2.task_profiles
            .iter_mut()
            .find(|t| t.id == "task-chat")
            .unwrap()
            .cloud_policy = Some(crate::llm::CloudPolicy::Allow);
        assert_ne!(base, binding_fingerprint(&cfg2, "claude", "task-chat").unwrap());
    }

    #[test]
    fn prune_expires_pending_and_bounds_finished() {
        clear_txs_for_test();
        let mk = |id: &str, status: OAuthTxStatus, created: i64, expires: i64| PendingTx {
            id: id.to_string(),
            state: "s".to_string(),
            verifier: "v".to_string(),
            challenge: "c".to_string(),
            provider_id: "openrouter".to_string(),
            provider_kind: ProviderKind::OpenRouter,
            destination: "https://openrouter.ai/api/v1".to_string(),
            config_fingerprint: "fp".to_string(),
            port: 0,
            created_at_ms: created,
            expires_at_ms: expires,
            status,
            error: None,
        };
        let now = now_ms();
        {
            let mut m = OAUTH_TXS.lock().unwrap();
            m.insert("old-pending".into(), mk("old-pending", OAuthTxStatus::Pending, now - 1000, now - 1));
            m.insert("live".into(), mk("live", OAuthTxStatus::Pending, now, now + 100_000));
        }
        prune_txs();
        let m = OAUTH_TXS.lock().unwrap();
        assert_eq!(m.get("old-pending").unwrap().status, OAuthTxStatus::Expired);
        assert_eq!(m.get("live").unwrap().status, OAuthTxStatus::Pending);
        drop(m);
        clear_txs_for_test();
    }

    #[test]
    fn safe_projection_omits_secret_material() {
        let tx = PendingTx {
            id: "t".into(),
            state: "STATE".into(),
            verifier: "VERIFIER".into(),
            challenge: "CHALLENGE".into(),
            provider_id: "openrouter".into(),
            provider_kind: ProviderKind::OpenRouter,
            destination: "https://openrouter.ai/api/v1".into(),
            config_fingerprint: "fp".into(),
            port: 1234,
            created_at_ms: 0,
            expires_at_ms: now_ms() + 1000,
            status: OAuthTxStatus::Pending,
            error: None,
        };
        let s = safe_tx_json(&tx).to_string();
        assert!(!s.contains("STATE"));
        assert!(!s.contains("VERIFIER"));
        assert!(!s.contains("CHALLENGE"));
        assert!(s.contains("openrouter"));
    }
}
