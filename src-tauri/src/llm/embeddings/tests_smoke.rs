//! Stage 1 real-endpoint smoke — MANUAL ONLY, never in default CI.
//!
//! Run explicitly with a local embedding runtime:
//!
//! ```bash
//! FRAGILE_REAL_EMBEDDINGS=1 \
//!   cargo test real_local_embedding_smoke -- --ignored --nocapture
//! ```
//!
//! Optional self-managed runtime (started and stopped by the test):
//!
//! ```bash
//! FRAGILE_REAL_EMBEDDINGS=1 FRAGILE_E2E_MODEL=/path/to/embed.gguf \
//!   FRAGILE_LLAMA_SERVER=/path/to/llama-server \
//!   cargo test real_local_embedding_smoke -- --ignored --nocapture
//! ```
//!
//! Rules: loopback endpoints only, hard timeout, runtime cleanup,
//! skip (not fail) when no model/server is available, no secrets,
//! never touches the user vault.

use super::openai_compatible::{OpenAiCompatibleConfig, OpenAiCompatibleEmbeddingProvider};
use super::provider::EmbeddingProvider;
use super::types::EmbeddingRequest;
use std::time::Duration;
use tokio_util::sync::CancellationToken;

fn enabled() -> bool {
    std::env::var("FRAGILE_REAL_EMBEDDINGS").unwrap_or_default() == "1"
}

fn skip(msg: &str) {
    eprintln!("SMOKE SKIP: {}", msg);
}

#[ignore]
#[tokio::test]
async fn real_local_embedding_smoke() {
    if !enabled() {
        eprintln!("SMOKE SKIP: set FRAGILE_REAL_EMBEDDINGS=1 to run");
        return;
    }
    tokio::time::timeout(Duration::from_secs(180), run())
        .await
        .expect("smoke timed out after 180s")
        .expect("smoke failed");
}

async fn run() -> Result<(), String> {
    // Self-managed runtime if a model path is provided, else probe existing server.
    let model_path = std::env::var("FRAGILE_E2E_MODEL").unwrap_or_default();
    let managed: Option<ManagedRuntime> = if !model_path.trim().is_empty() {
        Some(ManagedRuntime::start(&model_path).await?)
    } else {
        None
    };
    let endpoint = managed
        .as_ref()
        .map(|m| m.endpoint.clone())
        .or_else(|| std::env::var("FRAGILE_REAL_EMBEDDINGS_URL").ok())
        .unwrap_or_else(|| "http://127.0.0.1:8010".to_string());

    let out = probe_endpoint(&endpoint).await;
    // Cleanup before reporting, so failures never leak a runtime.
    if let Some(m) = managed {
        m.stop().await;
    }
    out
}

struct ManagedRuntime {
    endpoint: String,
    manager: crate::llm::runtime::manager::RuntimeManager,
    runtime_id: String,
    _state_dir: tempfile::TempDir,
}

impl ManagedRuntime {
    async fn start(model_path: &str) -> Result<Self, String> {
        let mp = std::path::Path::new(model_path);
        if !mp.exists() {
            return Err("SKIP: FRAGILE_E2E_MODEL does not exist".to_string());
        }
        if mp.extension().and_then(|e| e.to_str()) != Some("gguf") {
            return Err("refusing non-gguf model".to_string());
        }
        let binary = std::env::var("FRAGILE_LLAMA_SERVER")
            .unwrap_or_else(|_| "llama-server".to_string());
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let manager =
            crate::llm::runtime::manager::RuntimeManager::new(dir.path().join("runtime.json"));
        let mut settings = crate::llm::LlamaSettings::default();
        settings.n_ctx = 2048;
        settings.runtime_args = vec!["--embedding".to_string()];
        let info = manager.start("smoke-emb", model_path, &binary, &settings).await?;
        Ok(Self {
            endpoint: format!("http://127.0.0.1:{}", info.port),
            manager,
            runtime_id: info.runtime_id,
            _state_dir: dir,
        })
    }

    async fn stop(self) {
        let _ = self.manager.stop(&self.runtime_id).await;
    }
}

async fn probe_endpoint(endpoint: &str) -> Result<(), String> {
    // Loopback only — never point this harness at a remote endpoint.
    let url: url::Url = endpoint.parse().map_err(|e| format!("bad endpoint: {}", e))?;
    if url.username() != "" || url.password().is_some() {
        return Err("refusing endpoint with credentials".to_string());
    }
    match url.host_str() {
        Some("127.0.0.1") | Some("localhost") | Some("::1") => {}
        other => return Err(format!("refusing non-loopback endpoint: {:?}", other)),
    }
    if url.scheme() != "http" {
        return Err("smoke expects plain local http".to_string());
    }

    // No server → skip, not fail.
    let health_url = format!("{}/health", endpoint.trim_end_matches('/'));
    let probe = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(|e| e.to_string())?;
    match probe.get(&health_url).send().await {
        Ok(r) if r.status().is_success() => {}
        other => {
            skip(&format!("no local server at {} ({:?})", health_url, other.map(|r| r.status())));
            return Ok(());
        }
    }

    let provider = OpenAiCompatibleEmbeddingProvider::new(OpenAiCompatibleConfig {
        endpoint: endpoint.to_string(),
        api_key: None, // local server, no secrets
        remote_model: std::env::var("FRAGILE_SMOKE_MODEL").unwrap_or_else(|_| "smoke".to_string()),
        timeout: Duration::from_secs(30),
        ..Default::default()
    })
    .map_err(|e| e.to_string())?;

    let cancel = CancellationToken::new();
    let req = EmbeddingRequest::new(
        "smoke".to_string(),
        vec!["Fragile Notes smoke test.".to_string()],
    );
    let resp = provider.embed(req, cancel).await.map_err(|e| format!("{:?}", e))?;
    if resp.dimensions == 0 {
        return Err("zero dimensions".to_string());
    }
    if !resp.vectors.iter().flatten().all(|v| v.is_finite()) {
        return Err("non-finite values".to_string());
    }
    let norm: f32 = resp.vectors[0].iter().map(|v| v * v).sum::<f32>().sqrt();
    if norm <= 0.0 {
        return Err("zero norm".to_string());
    }
    eprintln!(
        "SMOKE ok: endpoint={} dimensions={} norm={:.4}",
        endpoint, resp.dimensions, norm
    );
    Ok(())
}
