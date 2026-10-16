//! OpenRouter browser authorization (OAuth 2.0 + PKCE, official flow).
//!
//! Flow (per <https://openrouter.ai/docs/guides/overview/auth/oauth>):
//!   1. `start` generates a PKCE verifier/challenge + CSRF `state`, binds a
//!      loopback listener on an ephemeral port, and returns the authorization
//!      URL. The frontend opens it in the system browser.
//!   2. The browser redirects back to `http://localhost:<port>/callback`.
//!      The listener validates method, path, Host and `state`; a callback
//!      without a matching `state` is rejected and never attributed to a tx.
//!   3. The backend exchanges the code at
//!      `POST https://openrouter.ai/api/v1/auth/keys` and persists the issued
//!      key through the F-9 keyring funnel.
//!
//! The frontend never receives the code, verifier or key — only a safe status.
use std::future::Future;

use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::llm::auth::{
    canonical_destination, now_ms, prune_txs, random_b64url, register_wake, s256_challenge,
    unregister_wake, wake_listener, PendingTx, OAUTH_TXS, TX_TTL_MS,
};
use crate::llm::{cred_guard, load_config_inner, provider_is_cloud, MutationState, ProviderKind};

const OPENROUTER_EXCHANGE_URL: &str = "https://openrouter.ai/api/v1/auth/keys";
/// Path component of the official exchange endpoint. Shared with the contract
/// tests so they exercise the exact production path against a local mock.
const OPENROUTER_EXCHANGE_PATH: &str = "/api/v1/auth/keys";
const OPENROUTER_AUTH_URL: &str = "https://openrouter.ai/auth";
const CALLBACK_PATH: &str = "/callback";
/// Cap on the HTTP request we read from a single loopback connection.
const MAX_REQUEST_BYTES: usize = 8 * 1024;
const CONN_READ_TIMEOUT_MS: u64 = 5_000;

/// Errors in this module never carry credential material. Any upstream message
/// is reduced to a short, safe description before it reaches the frontend.
fn sanitize_error(raw: &str) -> String {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return "authorization failed".to_string();
    }
    // Keep it short and never echo potential token-shaped payloads back.
    trimmed.chars().take(200).collect()
}

fn urlencode(s: &str) -> String {
    url::form_urlencoded::byte_serialize(s.as_bytes()).collect()
}

/// Build the OpenRouter authorization URL. `callback_url` is the literal
/// `127.0.0.1` loopback form so the callback host matches the IPv4 listener
/// exactly (see [`loopback_callback_url`]).
fn build_auth_url(callback_url: &str, challenge: &str, state: &str, key_label: &str) -> String {
    format!(
        "{}?callback_url={}&code_challenge={}&code_challenge_method=S256&state={}&key_label={}",
        OPENROUTER_AUTH_URL,
        urlencode(callback_url),
        urlencode(challenge),
        urlencode(state),
        urlencode(key_label),
    )
}

/// The callback URL registered with OpenRouter for a given loopback `port`.
///
/// The official docs accept `localhost` *or* `127.0.0.1` on any port for local
/// CLI tools. We use the literal `127.0.0.1` form because the listener binds
/// IPv4 loopback only: if the browser resolved `localhost` to `::1` while no
/// IPv6 listener is present, the callback would fail. The literal IPv4 address
/// and the IPv4 listener always agree.
fn loopback_callback_url(port: u16) -> String {
    format!("http://127.0.0.1:{}{}", port, CALLBACK_PATH)
}

/// Extract the path component of a request target for an **exact**
/// [`CALLBACK_PATH`] comparison.
///
/// Handles the origin form (`/callback?x=1`) and the absolute form
/// (`http://host/callback?x=1`), and drops any fragment. A prefix comparison
/// would wrongly accept `/callbackevil` or `/callback/anything`.
fn request_path(target: &str) -> &str {
    let no_fragment = target.split('#').next().unwrap_or(target);
    let after_scheme = match no_fragment.find("://") {
        Some(i) => &no_fragment[i + 3..],
        None => no_fragment,
    };
    let path = match after_scheme.find('/') {
        Some(i) => &after_scheme[i..],
        None => "/",
    };
    path.split('?').next().unwrap_or(path)
}

/// Parse an HTTP request line + headers, returning `(method, target, host)`.
fn parse_request_line_and_host(req: &str) -> (String, String, String) {
    let mut method = String::new();
    let mut target = String::new();
    let mut host = String::new();
    for (i, line) in req.lines().enumerate() {
        if i == 0 {
            let mut parts = line.split_whitespace();
            method = parts.next().unwrap_or("").to_string();
            target = parts.next().unwrap_or("").to_string();
        } else if let Some(rest) = line.to_ascii_lowercase().strip_prefix("host:") {
            host = rest.trim().to_string();
        }
    }
    (method, target, host)
}

/// A valid loopback callback has `Host: localhost:<port>` or
/// `127.0.0.1:<port>` (the exact port we bound). Anything else is rejected so a
/// hostile local page cannot spoof the callback.
fn host_is_loopback(host: &str, port: u16) -> bool {
    let expected = [format!("localhost:{}", port), format!("127.0.0.1:{}", port)];
    expected.iter().any(|e| e == host.trim())
}

/// Write a minimal HTTP/1.1 response with `body` and close the connection.
/// Best-effort: write errors on a dead socket are intentionally ignored.
async fn write_and_close(sock: &mut tokio::net::TcpStream, status: &str, body: &str) {
    let resp = format!(
        "HTTP/1.1 {}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        status,
        body.len(),
        body
    );
    let _ = sock.write_all(resp.as_bytes()).await;
    let _ = sock.flush().await;
    let _ = sock.shutdown().await;
}

/// Outcome of handling one loopback connection.
enum ConnOutcome {
    /// Valid authorization callback; carries the code to exchange.
    Code(String),
    /// Not the callback (e.g. favicon) or rejected; keep waiting.
    Ignored,
    /// A valid-`state` provider denial callback; fail this transaction only.
    Denied,
    /// Fatal protocol error for this listener.
    Fatal(String),
}

/// Read and validate a single callback request from `sock`. Bounded in size and
/// time so a hung or oversized connection can never block the transaction
/// indefinitely. Returns the auth `code` only when method, path, Host and
/// `state` all match.
async fn handle_callback_conn(
    sock: &mut tokio::net::TcpStream,
    expected_state: &str,
    port: u16,
) -> ConnOutcome {
    let mut buf: Vec<u8> = Vec::with_capacity(1024);
    let mut chunk = [0u8; 512];
    let read = tokio::time::timeout(
        std::time::Duration::from_millis(CONN_READ_TIMEOUT_MS),
        async {
            loop {
                let n = match sock.read(&mut chunk).await {
                    Ok(n) => n,
                    Err(e) => return Err(format!("read: {}", e)),
                };
                if n == 0 {
                    break;
                }
                buf.extend_from_slice(&chunk[..n]);
                if buf.len() > MAX_REQUEST_BYTES {
                    return Err("request too large".to_string());
                }
                if buf.windows(4).any(|w| w == b"\r\n\r\n") {
                    break;
                }
            }
            Ok(())
        },
    )
    .await;
    match read {
        Ok(Ok(())) => {}
        Ok(Err(e)) => return ConnOutcome::Fatal(e),
        Err(_) => return ConnOutcome::Fatal("read timeout".to_string()),
    }

    let req = String::from_utf8_lossy(&buf).to_string();
    let (method, target, host) = parse_request_line_and_host(&req);

    // Only an exact `GET /callback` is a valid callback target. A prefix match
    // would wrongly accept `/callbackevil` or `/callback/anything`.
    if method != "GET" || request_path(&target) != CALLBACK_PATH {
        write_and_close(sock, "404 Not Found", "").await;
        return ConnOutcome::Ignored;
    }
    if !host_is_loopback(&host, port) {
        write_and_close(sock, "400 Bad Request", "").await;
        return ConnOutcome::Ignored;
    }

    let query = target.splitn(2, '?').nth(1).unwrap_or("");
    // Keep every occurrence of every key: a `HashMap` would silently collapse a
    // repeated `state`/`code`/`error` to its last value and hide a parameter
    // pollution attempt.
    let pairs: Vec<(String, String)> = url::form_urlencoded::parse(query.as_bytes())
        .into_owned()
        .collect();

    // Security-sensitive parameters must appear at most once. A duplicate is
    // rejected outright and MUST NOT consume the pending transaction, so a
    // hostile local page cannot burn a legitimate authorization.
    for sensitive in ["state", "code", "error"] {
        if pairs.iter().filter(|(k, _)| k == sensitive).count() > 1 {
            write_and_close(sock, "400 Bad Request", "").await;
            return ConnOutcome::Ignored;
        }
    }
    let get = |k: &str| {
        pairs
            .iter()
            .find(|(key, _)| key == k)
            .map(|(_, v)| v.as_str())
            .unwrap_or("")
    };
    let state = get("state");
    let code = get("code");
    let error = get("error");

    // A callback without a matching `state` must never complete — or consume —
    // a transaction.
    if state != expected_state {
        write_and_close(sock, "400 Bad Request", "").await;
        return ConnOutcome::Ignored;
    }

    // Provider-reported denial (e.g. the user clicked "Deny"): with a valid
    // `state` this genuinely belongs to *this* transaction, so it fails the tx
    // rather than being ignored. The raw provider message is never surfaced.
    if code.is_empty() {
        if !error.is_empty() {
            let body = "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">\
<title>Fragile Notes</title></head><body style=\"font-family:sans-serif;padding:2rem\">\
<h2>Авторизация отклонена</h2><p>Возврат в приложение Fragile Notes. \
Окно можно закрыть.</p></body></html>";
            write_and_close(sock, "200 OK", body).await;
            return ConnOutcome::Denied;
        }
        write_and_close(sock, "400 Bad Request", "").await;
        return ConnOutcome::Ignored;
    }

    // Neutral intermediate page: authorization was received, but persistence
    // has NOT happened yet, so it must not claim the account is connected.
    let body = "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">\
<title>Fragile Notes</title></head><body style=\"font-family:sans-serif;padding:2rem\">\
<h2>Авторизация получена</h2><p>Вернитесь в приложение Fragile Notes. \
Окно можно закрыть.</p></body></html>";
    write_and_close(sock, "200 OK", body).await;
    ConnOutcome::Code(code.to_string())
}

fn fail_tx(tx_id: &str, raw_err: &str) {
    let mut m = OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(tx) = m.get_mut(tx_id) {
        // Never overwrite a terminal success from a late failure.
        if tx.status != crate::llm::auth::OAuthTxStatus::Connected {
            tx.status = crate::llm::auth::OAuthTxStatus::Failed;
            tx.error = Some(sanitize_error(raw_err));
        }
    }
}

/// POST the authorization code to `url` and parse the issued key.
///
/// `url` is a test seam only: production always calls this with
/// [`OPENROUTER_EXCHANGE_URL`]. No environment variable or frontend-supplied
/// value can redirect the production exchange. No `client_id`/`redirect_uri` is
/// sent — only the code, verifier and method, per the official flow.
async fn exchange_code_at(url: &str, code: &str, verifier: &str) -> Result<String, String> {
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|_| "http client init failed".to_string())?;
    let resp = client
        .post(url)
        .header("Content-Type", "application/json")
        .json(&serde_json::json!({
            "code": code,
            "code_verifier": verifier,
            "code_challenge_method": "S256",
        }))
        .send()
        .await
        .map_err(|e| format!("exchange request failed: {}", e))?;
    if !resp.status().is_success() {
        // Only the status is surfaced; never the response body.
        return Err(format!("exchange HTTP {}", resp.status().as_u16()));
    }
    // Parse as a generic value first so a malformed body (not JSON) and a
    // well-formed body missing `key` yield distinct, safe errors.
    let value: serde_json::Value = resp
        .json()
        .await
        .map_err(|_| "exchange response was not valid JSON".to_string())?;
    let key = value
        .get("key")
        .and_then(|k| k.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if key.is_empty() {
        return Err("exchange returned an empty key".to_string());
    }
    Ok(key)
}

/// Production exchange: fixed official destination, no override.
async fn exchange_code(code: &str, verifier: &str) -> Result<String, String> {
    exchange_code_at(OPENROUTER_EXCHANGE_URL, code, verifier).await
}

/// Drive one transaction from received code to a persisted credential.
///
/// Lifecycle:
///   1. `Pending -> Processing` (under `OAUTH_TXS` only), capturing the
///      immutable authorization binding: provider id, config fingerprint and
///      the provider's `auth_rev` at start.
///   2. Network exchange with **no lock held**, so a slow/failed call cannot
///      block cancel, status polling or any config writer.
///   3. Commit under `OAUTH_TXS -> CRED_MUTEX`: re-check `Processing`, the
///      `auth_rev` and the fingerprint, then persist. Holding the credential
///      guard across the check-and-persist means a disconnect that lands first
///      makes us reject, while one that lands after us deletes the key we wrote
///      — either way we never leave a credential behind a confirmed disconnect.
///
/// The `exchange` and `persist` closures are injected so the whole state
/// machine (including the cancel-vs-persist and disconnect-vs-commit races) is
/// testable without network or a real keyring. No `await` occurs while a lock
/// guard is held, so the returned future stays `Send`.
pub async fn exchange_and_persist<F, Fut, P>(
    tx_id: &str,
    code: String,
    exchange: F,
    persist: P,
) where
    F: FnOnce(String, String) -> Fut,
    Fut: Future<Output = Result<String, String>>,
    P: FnOnce(&mut MutationState, &str, &str) -> Result<(), String>,
{
    use crate::llm::auth::OAuthTxStatus;

    // 1. Pending -> Processing. Captures the binding to re-check at commit.
    let (verifier, provider_id, config_fingerprint, auth_rev) = {
        let mut m = OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner());
        let tx = match m.get_mut(tx_id) {
            Some(t) => t,
            None => return,
        };
        match tx.status {
            OAuthTxStatus::Pending => tx.status = OAuthTxStatus::Processing,
            // A cancelled/expired/terminal tx must never proceed.
            _ => return,
        }
        (
            tx.verifier.clone(),
            tx.provider_id.clone(),
            tx.config_fingerprint.clone(),
            tx.auth_rev,
        )
    };

    // 2. Network exchange, no lock held.
    let key = match exchange(code, verifier).await {
        Ok(k) => k,
        Err(e) => {
            fail_tx(tx_id, &e);
            return;
        }
    };

    // 3. Commit. Lock order OAUTH_TXS -> CRED_MUTEX; nothing takes the reverse.
    let mut m = OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner());
    let mut cred = cred_guard();
    let tx = match m.get_mut(tx_id) {
        Some(t) => t,
        None => return,
    };
    // Cancelled/expired between the exchange and the commit.
    if tx.status != OAuthTxStatus::Processing {
        return;
    }
    // A later credential change (e.g. disconnect) invalidates this flow.
    if cred.auth_rev_of(&provider_id) != auth_rev {
        tx.status = OAuthTxStatus::Failed;
        tx.error =
            Some("authorization was invalidated by a later credential change".to_string());
        return;
    }
    // The provider must still exist and be unchanged since `start`.
    let cfg = load_config_inner();
    let still_matches = cfg
        .providers
        .iter()
        .find(|p| p.id == provider_id)
        .map(|p| crate::llm::auth::provider_fingerprint(p) == config_fingerprint)
        .unwrap_or(false);
    if !still_matches {
        tx.status = OAuthTxStatus::Failed;
        tx.error =
            Some("provider configuration changed during authorization".to_string());
        return;
    }
    match persist(&mut cred, &provider_id, &key) {
        Ok(()) => {
            tx.status = OAuthTxStatus::Connected;
            tx.error = None;
        }
        Err(e) => {
            tx.status = OAuthTxStatus::Failed;
            tx.error = Some(sanitize_error(&e));
        }
    }
}

/// Accept loopback callbacks until the tx is completed, cancelled or expired.
/// Each accepted connection is handled independently; a bad connection does not
/// tear down the listener.
///
/// The loop `select!`s between the socket, the per-tx `wake` handle and the TTL
/// deadline. A cancel/expiry calls `wake.notify_one()`, so the listener closes
/// promptly instead of waiting out the remaining 5-minute deadline.
async fn run_callback_listener(
    tx_id: String,
    listener: tokio::net::TcpListener,
    expected_state: String,
    port: u16,
    wake: std::sync::Arc<tokio::sync::Notify>,
) {
    use crate::llm::auth::OAuthTxStatus;
    let deadline =
        tokio::time::Instant::now() + std::time::Duration::from_millis(TX_TTL_MS as u64);
    loop {
        // Stop early if the tx left Pending/Processing (cancelled, expired,
        // connected, failed). The guard is dropped before any `await`.
        {
            let m = OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner());
            match m.get(&tx_id) {
                Some(t)
                    if matches!(
                        t.status,
                        OAuthTxStatus::Pending | OAuthTxStatus::Processing
                    ) => {}
                _ => {
                    unregister_wake(&tx_id);
                    return;
                }
            }
        }
        // `notify_one` stores a permit when no waiter is parked yet, so a wake
        // that fired before this `select!` is consumed immediately — no lost
        // wakeup, no waiting out the TTL after a cancel.
        let accepted = tokio::select! {
            _ = wake.notified() => continue,
            accepted = tokio::time::timeout_at(deadline, listener.accept()) => accepted,
        };
        let (mut sock, _) = match accepted {
            Ok(Ok(pair)) => pair,
            Ok(Err(_)) => continue,
            Err(_) => {
                // TTL elapsed with no callback.
                fail_tx(&tx_id, "authorization timed out");
                unregister_wake(&tx_id);
                return;
            }
        };
        match handle_callback_conn(&mut sock, &expected_state, port).await {
            ConnOutcome::Code(code) => {
                let tid = tx_id.clone();
                exchange_and_persist(
                    &tid,
                    code,
                    |c, v| async move { exchange_code(&c, &v).await },
                    |state, pid, key| {
                        crate::llm::set_provider_credential_locked(state, pid, key)
                    },
                )
                .await;
                unregister_wake(&tx_id);
                return;
            }
            ConnOutcome::Ignored => continue,
            ConnOutcome::Denied => {
                fail_tx(&tx_id, "authorization was denied");
                unregister_wake(&tx_id);
                return;
            }
            ConnOutcome::Fatal(e) => {
                fail_tx(&tx_id, &e);
                unregister_wake(&tx_id);
                return;
            }
        }
    }
}

/// Begin a browser authorization transaction for an OpenRouter provider.
/// Returns `{ tx_id, auth_url, port }`; no secret ever crosses this boundary.
#[tauri::command]
pub async fn llm_openrouter_oauth_start(provider_id: String) -> Result<String, String> {
    prune_txs();
    // Read the provider identity AND its current auth revision under the shared
    // credential guard, so the captured `(fingerprint, auth_rev)` pair is
    // coherent: no later credential change can slip between the two reads. The
    // guard is dropped before any `await`, keeping this future `Send`.
    let (prov, auth_rev) = {
        let guard = cred_guard();
        let cfg = load_config_inner();
        let prov = cfg
            .providers
            .iter()
            .find(|p| p.id == provider_id)
            .ok_or_else(|| "provider not found".to_string())?
            .clone();
        if prov.kind != ProviderKind::OpenRouter {
            return Err("provider is not an OpenRouter provider".to_string());
        }
        if !provider_is_cloud(&prov.kind, &prov.endpoint) {
            return Err("provider is not a cloud provider".to_string());
        }
        let rev = guard.auth_rev_of(&provider_id);
        (prov, rev)
    };

    let state = random_b64url(32);
    let verifier = random_b64url(32);
    let challenge = s256_challenge(&verifier);
    let destination = canonical_destination(&prov.endpoint);
    let fingerprint = crate::llm::auth::provider_fingerprint(&prov);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .map_err(|e| format!("loopback bind failed: {}", e))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("loopback addr failed: {}", e))?
        .port();

    // Literal IPv4 loopback callback (see `loopback_callback_url`): matches the
    // IPv4 listener exactly and is accepted by OpenRouter for local CLI tools.
    let callback_url = loopback_callback_url(port);
    let auth_url = build_auth_url(&callback_url, &challenge, &state, "Fragile Notes");

    let tx_id = format!("orx-{}", random_b64url(12));
    let created = now_ms();
    let tx = PendingTx {
        id: tx_id.clone(),
        state: state.clone(),
        verifier,
        challenge,
        provider_id: provider_id.clone(),
        provider_kind: prov.kind.clone(),
        destination,
        config_fingerprint: fingerprint,
        auth_rev,
        port,
        created_at_ms: created,
        expires_at_ms: created + TX_TTL_MS,
        status: crate::llm::auth::OAuthTxStatus::Pending,
        error: None,
    };
    {
        let mut m = OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner());
        m.insert(tx_id.clone(), tx);
    }
    // Register the wake handle *after* releasing OAUTH_TXS (lock order is
    // OAUTH_TXS -> OAUTH_WAKE and wake registration only takes OAUTH_WAKE).
    let wake = register_wake(&tx_id);
    tokio::spawn(run_callback_listener(
        tx_id.clone(),
        listener,
        state,
        port,
        wake,
    ));

    Ok(serde_json::json!({
        "tx_id": tx_id,
        "auth_url": auth_url,
        "port": port,
    })
    .to_string())
}

/// Safe status projection. Never returns code/verifier/key/state.
#[tauri::command]
pub fn llm_openrouter_oauth_status(tx_id: String) -> Result<String, String> {
    prune_txs();
    let m = OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner());
    let tx = m.get(&tx_id).ok_or_else(|| "unknown oauth transaction".to_string())?;
    Ok(crate::llm::auth::safe_tx_json(tx).to_string())
}

/// Cancel a pending/processing transaction. Idempotent; a `connected`
/// transaction is never reverted (the credential is already persisted).
#[tauri::command]
pub fn llm_openrouter_oauth_cancel(tx_id: String) -> Result<String, String> {
    use crate::llm::auth::OAuthTxStatus;
    let result = {
        let mut m = OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner());
        let tx = m
            .get_mut(&tx_id)
            .ok_or_else(|| "unknown oauth transaction".to_string())?;
        match tx.status {
            OAuthTxStatus::Pending | OAuthTxStatus::Processing => {
                tx.status = OAuthTxStatus::Cancelled;
                Ok("cancelled".to_string())
            }
            OAuthTxStatus::Connected => Ok("connected".to_string()),
            OAuthTxStatus::Cancelled => Ok("cancelled".to_string()),
            OAuthTxStatus::Failed => Ok("failed".to_string()),
            OAuthTxStatus::Expired => Ok("expired".to_string()),
        }
    };
    // Wake the listener (if any) *after* releasing OAUTH_TXS, so a parked
    // callback listener closes its socket immediately rather than waiting out
    // the remaining TTL. The `?` above returns before this line when the tx is
    // unknown, which is correct: there is no listener to wake.
    wake_listener(&tx_id);
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::auth::{clear_txs_for_test, OAuthTxStatus};

    fn openrouter_cfg() -> crate::llm::LlmConfig {
        let mut cfg = crate::llm::LlmConfig::default();
        cfg.providers.push(crate::llm::Provider {
            id: "openrouter".to_string(),
            name: "OpenRouter".to_string(),
            kind: ProviderKind::OpenRouter,
            enabled: true,
            endpoint: "https://openrouter.ai/api/v1".to_string(),
            auth: None,
            default_model: String::new(),
            extra: Default::default(),
        });
        cfg
    }

    #[test]
    fn auth_url_contains_required_params_and_never_client_secret() {
        let u = build_auth_url(
            "http://localhost:12345/callback",
            "CH",
            "ST",
            "Fragile Notes",
        );
        assert!(u.starts_with("https://openrouter.ai/auth?"));
        assert!(u.contains("callback_url=http%3A%2F%2Flocalhost%3A12345%2Fcallback"));
        assert!(u.contains("code_challenge=CH"));
        assert!(u.contains("code_challenge_method=S256"));
        assert!(u.contains("state=ST"));
        assert!(!u.contains("client_secret"));
        assert!(!u.contains("redirect_uri"));
    }

    #[test]
    fn host_validation_only_accepts_expected_loopback() {
        assert!(host_is_loopback("localhost:5000", 5000));
        assert!(host_is_loopback("127.0.0.1:5000", 5000));
        assert!(!host_is_loopback("localhost:5001", 5000));
        assert!(!host_is_loopback("evil.example:5000", 5000));
        assert!(!host_is_loopback("[::1]:5000", 5000));
    }

    fn seed_tx(state: &str) -> String {
        let cfg = openrouter_cfg();
        let prov = cfg.providers.iter().find(|p| p.id == "openrouter").unwrap();
        let id = format!("test-{}", random_b64url(6));
        let created = now_ms();
        let verifier = random_b64url(32);
        let auth_rev = crate::llm::cred_guard().auth_rev_of("openrouter");
        let tx = PendingTx {
            id: id.clone(),
            state: state.to_string(),
            verifier: verifier.clone(),
            challenge: s256_challenge(&verifier),
            provider_id: "openrouter".to_string(),
            provider_kind: ProviderKind::OpenRouter,
            destination: canonical_destination(&prov.endpoint),
            config_fingerprint: crate::llm::auth::provider_fingerprint(prov),
            auth_rev,
            port: 0,
            created_at_ms: created,
            expires_at_ms: created + TX_TTL_MS,
            status: OAuthTxStatus::Pending,
            error: None,
        };
        OAUTH_TXS.lock().unwrap().insert(id.clone(), tx);
        id
    }

    fn status_of(id: &str) -> OAuthTxStatus {
        OAUTH_TXS.lock().unwrap().get(id).unwrap().status
    }

    /// Spawn a one-shot mock exchange server. Returns the bound port and a
    /// handle yielding the raw request bytes it received, so a contract test
    /// can assert the exact method/path/body without a live OpenRouter call.
    async fn spawn_exchange_mock(
        status: &'static str,
        body: &'static str,
    ) -> (u16, tokio::task::JoinHandle<String>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let handle = tokio::spawn(async move {
            let (mut sock, _) = listener.accept().await.unwrap();
            let mut buf: Vec<u8> = Vec::new();
            let mut chunk = [0u8; 1024];
            loop {
                let n = sock.read(&mut chunk).await.unwrap_or(0);
                if n == 0 {
                    break;
                }
                buf.extend_from_slice(&chunk[..n]);
                let s = String::from_utf8_lossy(&buf).to_string();
                if let Some(idx) = s.find("\r\n\r\n") {
                    let header_len = idx + 4;
                    let cl = s[..idx]
                        .to_ascii_lowercase()
                        .lines()
                        .find_map(|l| l.strip_prefix("content-length:"))
                        .and_then(|v| v.trim().parse::<usize>().ok())
                        .unwrap_or(0);
                    if buf.len() >= header_len + cl {
                        break;
                    }
                }
            }
            let resp = format!(
                "HTTP/1.1 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                status,
                body.len(),
                body
            );
            let _ = sock.write_all(resp.as_bytes()).await;
            let _ = sock.flush().await;
            let _ = sock.shutdown().await;
            String::from_utf8_lossy(&buf).to_string()
        });
        (port, handle)
    }

    #[test]
    fn production_exchange_target_is_fixed_official_url() {
        assert_eq!(
            OPENROUTER_EXCHANGE_URL,
            "https://openrouter.ai/api/v1/auth/keys"
        );
        // The shared path constant must match the production URL exactly, so a
        // contract test against a local mock exercises the real path.
        assert!(OPENROUTER_EXCHANGE_URL.ends_with(OPENROUTER_EXCHANGE_PATH));
    }

    #[test]
    fn callback_url_uses_ipv4_loopback() {
        let u = loopback_callback_url(4242);
        assert_eq!(u, "http://127.0.0.1:4242/callback");
        assert!(!u.contains("localhost"));
    }

    #[test]
    fn request_path_matches_only_exact_callback() {
        assert_eq!(request_path("/callback"), "/callback");
        assert_eq!(request_path("/callback?code=x&state=y"), "/callback");
        assert_eq!(request_path("http://127.0.0.1:5000/callback?x=1"), "/callback");
        assert_eq!(request_path("/callback#frag"), "/callback");
        assert_ne!(request_path("/callback/extra"), "/callback");
        assert_ne!(request_path("/callbackevil"), "/callback");
        assert_ne!(request_path("/foo/callback"), "/callback");
        assert_ne!(request_path("callback"), "/callback");
    }

    // The exchange must POST to the exact production path with the documented
    // body shape and parse the issued key.
    #[tokio::test]
    async fn exchange_posts_official_path_and_parses_key() {
        let (port, handle) = spawn_exchange_mock("200 OK", "{\"key\":\"sk-contract\"}").await;
        let url = format!("http://127.0.0.1:{}{}", port, OPENROUTER_EXCHANGE_PATH);
        let key = exchange_code_at(&url, "THE_CODE", "THE_VERIFIER").await.unwrap();
        assert_eq!(key, "sk-contract");
        let req = handle.await.unwrap();
        let request_line = req.lines().next().unwrap_or("");
        assert!(
            request_line.starts_with("POST /api/v1/auth/keys "),
            "unexpected request line: {}",
            request_line
        );
        assert!(req.contains("\"code\":\"THE_CODE\""));
        assert!(req.contains("\"code_verifier\":\"THE_VERIFIER\""));
        assert!(req.contains("\"code_challenge_method\":\"S256\""));
    }

    #[tokio::test]
    async fn exchange_non_success_is_safe_and_bodyless() {
        let (port, _handle) =
            spawn_exchange_mock("401 Unauthorized", "{\"error\":\"SECRET-LEAK\"}").await;
        let url = format!("http://127.0.0.1:{}{}", port, OPENROUTER_EXCHANGE_PATH);
        let err = exchange_code_at(&url, "c", "v").await.unwrap_err();
        assert!(err.contains("401"));
        assert!(!err.contains("SECRET-LEAK"));
    }

    #[tokio::test]
    async fn exchange_missing_key_is_rejected() {
        let (port, _handle) = spawn_exchange_mock("200 OK", "{}").await;
        let url = format!("http://127.0.0.1:{}{}", port, OPENROUTER_EXCHANGE_PATH);
        assert!(exchange_code_at(&url, "c", "v").await.is_err());
    }

    // A credential change (disconnect) landing while the exchange is in flight
    // must invalidate the commit: no key is persisted and the tx fails.
    #[tokio::test]
    async fn disconnect_during_exchange_aborts_commit() {
        clear_txs_for_test();
        let _env = crate::llm::tests::IsolatedEnv::lock("oauth-disconnect-race");
        crate::llm::save_config_inner(&openrouter_cfg()).unwrap();
        let id = seed_tx("S");
        let persisted = std::sync::Arc::new(std::sync::Mutex::new(0usize));
        let p2 = persisted.clone();
        exchange_and_persist(
            &id,
            "CODE".to_string(),
            |_c, _v| async {
                // Simulate a disconnect that lands during the network call.
                crate::llm::cred_guard().bump_auth_rev("openrouter");
                Ok("sk-late".to_string())
            },
            move |_state, _pid, _key| {
                *p2.lock().unwrap() += 1;
                Ok(())
            },
        )
        .await;
        assert_eq!(status_of(&id), OAuthTxStatus::Failed);
        assert_eq!(
            *persisted.lock().unwrap(),
            0,
            "must not persist after a disconnect during exchange"
        );
        clear_txs_for_test();
    }

    // Duplicate security-sensitive params are rejected without consuming the tx.
    #[tokio::test]
    async fn callback_rejects_duplicate_sensitive_params() {
        clear_txs_for_test();
        let _env = crate::llm::tests::IsolatedEnv::lock("oauth-dup-params");
        let cases = [
            "GET /callback?state=RIGHT&state=EVIL&code=X HTTP/1.1",
            "GET /callback?state=RIGHT&code=X&code=Y HTTP/1.1",
            "GET /callback?state=RIGHT&error=a&error=b HTTP/1.1",
        ];
        for request_line in cases {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let port = listener.local_addr().unwrap().port();
            let line = request_line.to_string();
            let client = tokio::spawn(async move {
                let mut s = tokio::net::TcpStream::connect(("127.0.0.1", port))
                    .await
                    .unwrap();
                let req = format!("{}\r\nHost: 127.0.0.1:{}\r\n\r\n", line, port);
                s.write_all(req.as_bytes()).await.unwrap();
                let mut out = Vec::new();
                let _ = s.read_to_end(&mut out).await;
                String::from_utf8_lossy(&out).to_string()
            });
            let (mut sock, _) = listener.accept().await.unwrap();
            match handle_callback_conn(&mut sock, "RIGHT", port).await {
                ConnOutcome::Ignored => {}
                _ => panic!("duplicate sensitive param must be Ignored: {}", request_line),
            }
            assert!(
                client.await.unwrap().contains("400"),
                "duplicate must yield 400: {}",
                request_line
            );
        }
    }

    // A provider denial with the correct state belongs to this tx and fails it;
    // the same denial with a wrong state is ignored (never consumes a tx).
    #[tokio::test]
    async fn callback_provider_denial_is_attributed_only_with_valid_state() {
        clear_txs_for_test();
        let _env = crate::llm::tests::IsolatedEnv::lock("oauth-denied");

        let good = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let gport = good.local_addr().unwrap().port();
        let gclient = tokio::spawn(async move {
            let mut s = tokio::net::TcpStream::connect(("127.0.0.1", gport)).await.unwrap();
            let req = format!(
                "GET /callback?state=RIGHT&error=access_denied HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n\r\n",
                gport
            );
            s.write_all(req.as_bytes()).await.unwrap();
            let mut out = Vec::new();
            let _ = s.read_to_end(&mut out).await;
            String::from_utf8_lossy(&out).to_string()
        });
        let (mut sock, _) = good.accept().await.unwrap();
        match handle_callback_conn(&mut sock, "RIGHT", gport).await {
            ConnOutcome::Denied => {}
            _ => panic!("valid-state denial must be Denied"),
        }
        assert!(gclient.await.unwrap().contains("200"));

        let bad = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let bport = bad.local_addr().unwrap().port();
        let bclient = tokio::spawn(async move {
            let mut s = tokio::net::TcpStream::connect(("127.0.0.1", bport)).await.unwrap();
            let req = format!(
                "GET /callback?state=WRONG&error=access_denied HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n\r\n",
                bport
            );
            s.write_all(req.as_bytes()).await.unwrap();
            let mut out = Vec::new();
            let _ = s.read_to_end(&mut out).await;
            String::from_utf8_lossy(&out).to_string()
        });
        let (mut sock, _) = bad.accept().await.unwrap();
        match handle_callback_conn(&mut sock, "RIGHT", bport).await {
            ConnOutcome::Ignored => {}
            _ => panic!("wrong-state denial must be Ignored"),
        }
        assert!(bclient.await.unwrap().contains("400"));
    }

    // The wake handle must interrupt a parked listener promptly on cancel,
    // long before the TTL deadline.
    #[tokio::test]
    async fn cancel_wakes_listener_promptly() {
        clear_txs_for_test();
        let _env = crate::llm::tests::IsolatedEnv::lock("oauth-wake");
        crate::llm::save_config_inner(&openrouter_cfg()).unwrap();
        let cfg = openrouter_cfg();
        let prov = cfg.providers.iter().find(|p| p.id == "openrouter").unwrap();
        let state = random_b64url(16);
        let verifier = random_b64url(32);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let id = format!("test-wake-{}", random_b64url(6));
        let created = now_ms();
        let tx = PendingTx {
            id: id.clone(),
            state: state.clone(),
            verifier,
            challenge: "c".to_string(),
            provider_id: "openrouter".to_string(),
            provider_kind: ProviderKind::OpenRouter,
            destination: canonical_destination(&prov.endpoint),
            config_fingerprint: crate::llm::auth::provider_fingerprint(prov),
            auth_rev: 0,
            port,
            created_at_ms: created,
            expires_at_ms: created + TX_TTL_MS,
            status: OAuthTxStatus::Pending,
            error: None,
        };
        OAUTH_TXS.lock().unwrap().insert(id.clone(), tx);
        let wake = register_wake(&id);
        let task = tokio::spawn(run_callback_listener(
            id.clone(),
            listener,
            state,
            port,
            wake,
        ));
        // `Notify::notify_one` stores a permit, so a cancel landing before the
        // listener parks is still observed; no sleep/synchronisation needed.
        llm_openrouter_oauth_cancel(id.clone()).unwrap();
        // It must finish well before the 5-minute TTL.
        tokio::time::timeout(std::time::Duration::from_secs(2), task)
            .await
            .expect("listener must close promptly after cancel")
            .unwrap();
        assert_eq!(status_of(&id), OAuthTxStatus::Cancelled);
        clear_txs_for_test();
    }


    // The exchange must reach the persist step and mark the tx connected.
    #[tokio::test]
    async fn exchange_then_persist_marks_connected() {
        clear_txs_for_test();
        let _env = crate::llm::tests::IsolatedEnv::lock("oauth-connect");
        // The live config must contain the provider so the fingerprint matches.
        crate::llm::save_config_inner(&openrouter_cfg()).unwrap();
        let id = seed_tx("S");
        let persisted = std::sync::Arc::new(std::sync::Mutex::new(Vec::<(String, String)>::new()));
        let p2 = persisted.clone();
        exchange_and_persist(
            &id,
            "THE_CODE".to_string(),
            |_c, _v| async { Ok("sk-or-secret".to_string()) },
            move |_state, pid, key| {
                p2.lock().unwrap().push((pid.to_string(), key.to_string()));
                Ok(())
            },
        )
        .await;
        assert_eq!(status_of(&id), OAuthTxStatus::Connected);
        assert_eq!(
            persisted.lock().unwrap().as_slice(),
            &[("openrouter".to_string(), "sk-or-secret".to_string())]
        );
        // The status projection never leaks the key.
        let m = OAUTH_TXS.lock().unwrap();
        let s = crate::llm::auth::safe_tx_json(m.get(&id).unwrap()).to_string();
        assert!(!s.contains("sk-or-secret"));
        drop(m);
        clear_txs_for_test();
    }

    // A cancelled tx must not exchange nor persist.
    #[tokio::test]
    async fn cancel_before_exchange_never_persists() {
        clear_txs_for_test();
        let _env = crate::llm::tests::IsolatedEnv::lock("oauth-cancel-first");
        let id = seed_tx("S");
        llm_openrouter_oauth_cancel(id.clone()).unwrap();
        let persisted = std::sync::Arc::new(std::sync::Mutex::new(0usize));
        let p2 = persisted.clone();
        let exchanged = std::sync::Arc::new(std::sync::Mutex::new(0usize));
        let e2 = exchanged.clone();
        exchange_and_persist(
            &id,
            "CODE".to_string(),
            move |_c, _v| {
                let e3 = e2.clone();
                async move {
                    *e3.lock().unwrap() += 1;
                    Ok("sk".to_string())
                }
            },
            move |_state, _pid, _key| {
                *p2.lock().unwrap() += 1;
                Ok(())
            },
        )
        .await;
        assert_eq!(status_of(&id), OAuthTxStatus::Cancelled);
        assert_eq!(*exchanged.lock().unwrap(), 0, "cancelled tx must not exchange");
        assert_eq!(*persisted.lock().unwrap(), 0, "cancelled tx must not persist");
        clear_txs_for_test();
    }

    // Cancel arriving *during* the exchange must prevent persistence, and the
    // tx must report cancelled.
    #[tokio::test]
    async fn cancel_during_exchange_prevents_persist() {
        clear_txs_for_test();
        let _env = crate::llm::tests::IsolatedEnv::lock("oauth-cancel-race");
        let id = seed_tx("S");
        let id_for_exchange = id.clone();
        let persisted = std::sync::Arc::new(std::sync::Mutex::new(0usize));
        let p2 = persisted.clone();
        exchange_and_persist(
            &id,
            "CODE".to_string(),
            move |_c, _v| {
                // Cancel lands while the (simulated) network exchange is in
                // flight, i.e. after Processing was set.
                llm_openrouter_oauth_cancel(id_for_exchange.clone()).unwrap();
                async { Ok("sk".to_string()) }
            },
            move |_state, _pid, _key| {
                *p2.lock().unwrap() += 1;
                Ok(())
            },
        )
        .await;
        assert_eq!(status_of(&id), OAuthTxStatus::Cancelled);
        assert_eq!(*persisted.lock().unwrap(), 0, "must not persist after cancel");
        clear_txs_for_test();
    }

    // Once connected, a cancel must not revert to cancelled.
    #[tokio::test]
    async fn cancel_after_connect_is_a_noop() {
        clear_txs_for_test();
        let _env = crate::llm::tests::IsolatedEnv::lock("oauth-cancel-after");
        crate::llm::save_config_inner(&openrouter_cfg()).unwrap();
        let id = seed_tx("S");
        exchange_and_persist(
            &id,
            "CODE".to_string(),
            |_c, _v| async { Ok("sk".to_string()) },
            |_state, _pid, _key| Ok(()),
        )
        .await;
        assert_eq!(status_of(&id), OAuthTxStatus::Connected);
        let r = llm_openrouter_oauth_cancel(id.clone()).unwrap();
        assert_eq!(r, "connected");
        assert_eq!(status_of(&id), OAuthTxStatus::Connected);
        clear_txs_for_test();
    }

    // A failed exchange (or persist) surfaces a safe failure without leaking.
    #[tokio::test]
    async fn failed_exchange_marks_failed_and_safe() {
        clear_txs_for_test();
        let _env = crate::llm::tests::IsolatedEnv::lock("oauth-fail");
        let id = seed_tx("S");
        exchange_and_persist(
            &id,
            "CODE".to_string(),
            |_c, _v| async { Err("exchange HTTP 401".to_string()) },
            |_state, _pid, _key| Ok(()),
        )
        .await;
        assert_eq!(status_of(&id), OAuthTxStatus::Failed);
        let m = OAUTH_TXS.lock().unwrap();
        let err = m.get(&id).unwrap().error.clone().unwrap();
        assert!(err.contains("401"));
        drop(m);
        clear_txs_for_test();
    }

    // A credential must not be written into a provider removed mid-flow.
    #[tokio::test]
    async fn provider_changed_during_flow_aborts_persist() {
        clear_txs_for_test();
        let env = crate::llm::tests::IsolatedEnv::lock("oauth-provider-changed");
        let _ = env;
        // Seed a tx against a provider fingerprint that will not match the live
        // config (which, in the isolated env, has no `openrouter` provider).
        let id = seed_tx("S");
        let persisted = std::sync::Arc::new(std::sync::Mutex::new(0usize));
        let p2 = persisted.clone();
        exchange_and_persist(
            &id,
            "CODE".to_string(),
            |_c, _v| async { Ok("sk".to_string()) },
            move |_state, _pid, _key| {
                *p2.lock().unwrap() += 1;
                Ok(())
            },
        )
        .await;
        assert_eq!(status_of(&id), OAuthTxStatus::Failed);
        assert_eq!(*persisted.lock().unwrap(), 0, "must not persist into missing provider");
        clear_txs_for_test();
    }

    // Full loopback callback round-trip over a real TCP socket: a good callback
    // yields the code, a bad state is rejected.
    #[tokio::test]
    async fn loopback_listener_validates_state() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();

        // Bad state first.
        let bad = tokio::spawn(async move {
            let mut s = tokio::net::TcpStream::connect(("127.0.0.1", port)).await.unwrap();
            let req = format!(
                "GET /callback?code=LEAK&state=WRONG HTTP/1.1\r\nHost: localhost:{}\r\n\r\n",
                port
            );
            s.write_all(req.as_bytes()).await.unwrap();
            let mut out = Vec::new();
            let _ = s.read_to_end(&mut out).await;
            String::from_utf8_lossy(&out).to_string()
        });
        let (mut sock, _) = listener.accept().await.unwrap();
        match handle_callback_conn(&mut sock, "RIGHT", port).await {
            ConnOutcome::Ignored => {}
            _ => panic!("bad state must be ignored"),
        }
        let bad_resp = bad.await.unwrap();
        assert!(bad_resp.contains("400"), "bad state must get 400, got {}", bad_resp);

        // Good state next.
        let good = tokio::spawn(async move {
            let mut s = tokio::net::TcpStream::connect(("127.0.0.1", port)).await.unwrap();
            let req = format!(
                "GET /callback?code=GOODCODE&state=RIGHT HTTP/1.1\r\nHost: localhost:{}\r\n\r\n",
                port
            );
            s.write_all(req.as_bytes()).await.unwrap();
            let mut out = Vec::new();
            let _ = s.read_to_end(&mut out).await;
            String::from_utf8_lossy(&out).to_string()
        });
        let (mut sock, _) = listener.accept().await.unwrap();
        match handle_callback_conn(&mut sock, "RIGHT", port).await {
            ConnOutcome::Code(c) => assert_eq!(c, "GOODCODE"),
            _ => panic!("good state must return the code"),
        }
        let good_resp = good.await.unwrap();
        assert!(good_resp.contains("200"));
        // Intermediate page must not claim the account is connected.
        assert!(good_resp.contains("Авторизация получена"));
    }

    // A rejected/wrong-path connection must not be attributed to any tx.
    #[tokio::test]
    async fn listener_rejects_wrong_path_and_host() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let client = tokio::spawn(async move {
            let mut s = tokio::net::TcpStream::connect(("127.0.0.1", port)).await.unwrap();
            // Wrong path.
            let req = format!(
                "GET /favicon.ico HTTP/1.1\r\nHost: localhost:{}\r\n\r\n",
                port
            );
            s.write_all(req.as_bytes()).await.unwrap();
            let mut out = Vec::new();
            let _ = s.read_to_end(&mut out).await;
            String::from_utf8_lossy(&out).to_string()
        });
        let (mut sock, _) = listener.accept().await.unwrap();
        match handle_callback_conn(&mut sock, "S", port).await {
            ConnOutcome::Ignored => {}
            _ => panic!("wrong path must be ignored"),
        }
        assert!(client.await.unwrap().contains("404"));
    }
}
