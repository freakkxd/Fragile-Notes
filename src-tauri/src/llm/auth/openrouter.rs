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

use serde::Deserialize;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::llm::auth::{
    canonical_destination, now_ms, prune_txs, random_b64url, s256_challenge, PendingTx, OAUTH_TXS,
    TX_TTL_MS,
};
use crate::llm::{is_cloud_kind, load_config_inner, ProviderKind};

const OPENROUTER_EXCHANGE_URL: &str = "https://openrouter.ai/api/v1/auth/keys";
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

/// Build the OpenRouter authorization URL. `callback_url` is intentionally the
/// documented `localhost` form (the docs guarantee localhost callbacks on any
/// port), while the listener itself binds IPv4 loopback.
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

/// Outcome of handling one loopback connection.
enum ConnOutcome {
    /// Valid authorization callback; carries the code to exchange.
    Code(String),
    /// Not the callback (e.g. favicon) or rejected; keep waiting.
    Ignored,
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

    // Only GET /callback is a valid callback target.
    if method != "GET" || !target.starts_with(CALLBACK_PATH) {
        let _ = sock
            .write_all(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
            .await;
        let _ = sock.shutdown().await;
        return ConnOutcome::Ignored;
    }
    if !host_is_loopback(&host, port) {
        let _ = sock
            .write_all(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            .await;
        let _ = sock.shutdown().await;
        return ConnOutcome::Ignored;
    }

    let query = target.splitn(2, '?').nth(1).unwrap_or("");
    let params: std::collections::HashMap<String, String> =
        url::form_urlencoded::parse(query.as_bytes())
            .into_owned()
            .collect();
    let state = params.get("state").cloned().unwrap_or_default();
    let code = params.get("code").cloned().unwrap_or_default();

    // A callback without a matching state must never complete a transaction.
    if state != expected_state || code.is_empty() {
        let _ = sock
            .write_all(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            .await;
        let _ = sock.shutdown().await;
        return ConnOutcome::Ignored;
    }

    // Neutral intermediate page: authorization was received, but persistence
    // has NOT happened yet, so it must not claim the account is connected.
    let body = "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">\
<title>Fragile Notes</title></head><body style=\"font-family:sans-serif;padding:2rem\">\
<h2>Авторизация получена</h2><p>Вернитесь в приложение Fragile Notes. \
Окно можно закрыть.</p></body></html>";
    let resp = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    let _ = sock.write_all(resp.as_bytes()).await;
    let _ = sock.flush().await;
    let _ = sock.shutdown().await;
    ConnOutcome::Code(code)
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

/// Real exchange against OpenRouter. No `client_id`/`redirect_uri` is sent —
/// only the code, verifier and method, per the official flow.
async fn exchange_code(code: &str, verifier: &str) -> Result<String, String> {
    #[derive(Deserialize)]
    struct ExchangeResponse {
        key: String,
    }
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|_| "http client init failed".to_string())?;
    let resp = client
        .post(OPENROUTER_EXCHANGE_URL)
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
        return Err(format!("exchange HTTP {}", resp.status().as_u16()));
    }
    let body: ExchangeResponse = resp
        .json()
        .await
        .map_err(|_| "exchange response was not valid JSON".to_string())?;
    if body.key.trim().is_empty() {
        return Err("exchange returned an empty key".to_string());
    }
    Ok(body.key)
}

/// Drive one transaction from received code to a persisted credential.
///
/// The exchange closure and the persist closure are injected so the state
/// machine (including the cancel-vs-persist race) is testable without network
/// or a real keyring.
pub async fn exchange_and_persist<F, Fut, P>(
    tx_id: &str,
    code: String,
    exchange: F,
    persist: P,
) where
    F: FnOnce(String, String) -> Fut,
    Fut: Future<Output = Result<String, String>>,
    P: FnOnce(&str, &str) -> Result<(), String>,
{
    use crate::llm::auth::OAuthTxStatus;

    // 1. Pending -> Processing. Cancellable while the network exchange runs.
    let verifier = {
        let mut m = OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner());
        let tx = match m.get_mut(tx_id) {
            Some(t) => t,
            None => return,
        };
        match tx.status {
            OAuthTxStatus::Pending => tx.status = OAuthTxStatus::Processing,
            // A cancelled tx must never proceed to exchange or persistence.
            _ => return,
        }
        tx.verifier.clone()
    };

    let key = match exchange(code, verifier).await {
        Ok(k) => k,
        Err(e) => {
            fail_tx(tx_id, &e);
            return;
        }
    };

    // 2. Commit point. Hold the tx lock across the not-cancelled check AND the
    //    keyring write, so a concurrent cancel either lands first (we skip) or
    //    waits until we are done (then observes `Connected`, a no-op). This
    //    guarantees we never persist after a confirmed cancel and never report
    //    `cancelled` once the credential is saved. Lock order is
    //    OAUTH_TXS -> CRED_MUTEX; nothing takes the reverse.
    let mut m = OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner());
    let tx = match m.get_mut(tx_id) {
        Some(t) => t,
        None => return,
    };
    if tx.status == OAuthTxStatus::Cancelled {
        return;
    }
    // The provider must still exist and be unchanged since `start`.
    let cfg = load_config_inner();
    let still_matches = cfg
        .providers
        .iter()
        .find(|p| p.id == tx.provider_id)
        .map(|p| crate::llm::auth::provider_fingerprint(p) == tx.config_fingerprint)
        .unwrap_or(false);
    if !still_matches {
        tx.status = OAuthTxStatus::Failed;
        tx.error =
            Some("provider configuration changed during authorization".to_string());
        return;
    }
    match persist(&tx.provider_id, &key) {
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
async fn run_callback_listener(
    tx_id: String,
    listener: tokio::net::TcpListener,
    expected_state: String,
    port: u16,
) {
    use crate::llm::auth::OAuthTxStatus;
    let deadline =
        tokio::time::Instant::now() + std::time::Duration::from_millis(TX_TTL_MS as u64);
    loop {
        // Stop early if the tx left Pending/Processing (cancelled, expired...).
        {
            let m = OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner());
            match m.get(&tx_id) {
                Some(t)
                    if matches!(
                        t.status,
                        OAuthTxStatus::Pending | OAuthTxStatus::Processing
                    ) => {}
                _ => return,
            }
        }
        let accepted = tokio::time::timeout_at(deadline, listener.accept()).await;
        let (mut sock, _) = match accepted {
            Ok(Ok(pair)) => pair,
            Ok(Err(_)) => continue,
            Err(_) => {
                // TTL elapsed: expire the tx if still waiting.
                fail_tx(&tx_id, "authorization timed out");
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
                    |pid, key| {
                        crate::llm::llm_set_provider_credential(pid.to_string(), key.to_string())
                            .map(|_| ())
                    },
                )
                .await;
                return;
            }
            ConnOutcome::Ignored => continue,
            ConnOutcome::Fatal(e) => {
                fail_tx(&tx_id, &e);
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
    if !is_cloud_kind(&prov.kind) {
        return Err("provider is not a cloud provider".to_string());
    }

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

    // Documented localhost callback form; listener binds IPv4 loopback.
    let callback_url = format!("http://localhost:{}{}", port, CALLBACK_PATH);
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
    tokio::spawn(run_callback_listener(
        tx_id.clone(),
        listener,
        state,
        port,
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
    let mut m = OAUTH_TXS.lock().unwrap_or_else(|e| e.into_inner());
    let tx = m.get_mut(&tx_id).ok_or_else(|| "unknown oauth transaction".to_string())?;
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
        let tx = PendingTx {
            id: id.clone(),
            state: state.to_string(),
            verifier: verifier.clone(),
            challenge: s256_challenge(&verifier),
            provider_id: "openrouter".to_string(),
            provider_kind: ProviderKind::OpenRouter,
            destination: canonical_destination(&prov.endpoint),
            config_fingerprint: crate::llm::auth::provider_fingerprint(prov),
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
            move |pid, key| {
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
            move |_pid, _key| {
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
            move |_pid, _key| {
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
            |_pid, _key| Ok(()),
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
            |_pid, _key| Ok(()),
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
            move |_pid, _key| {
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
