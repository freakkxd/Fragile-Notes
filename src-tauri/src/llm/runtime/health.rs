use std::time::Duration;
use super::types::HealthState;

#[derive(Debug, Clone)]
pub struct HealthCheckResult {
    pub state: HealthState,
    pub http_status: Option<u16>,
    pub body: Option<String>,
    pub error: Option<String>,
}

pub async fn check_health(base_url: &str) -> HealthCheckResult {
    let url = format!("{}/health", base_url.trim_end_matches('/'));
    let client = reqwest::Client::builder().timeout(Duration::from_secs(3)).build();
    let client = match client {
        Ok(c) => c,
        Err(e) => return HealthCheckResult{ state: HealthState::Unknown, http_status: None, body: None, error: Some(e.to_string()) },
    };
    match client.get(&url).send().await {
        Ok(resp) => {
            let status = resp.status().as_u16();
            let text = resp.text().await.unwrap_or_default();
            // llama.cpp /health returns JSON with status, slots, etc.
            // We distinguish: 200 with {"status":"ok"} -> Ready, 503 -> Loading/NoSlots, 500 -> Failed
            if status == 200 {
                if text.contains("no slots") || text.contains("NoSlots") {
                    HealthCheckResult{ state: HealthState::NoSlots, http_status: Some(status), body: Some(text), error: None }
                } else if text.contains("loading") || text.contains("Loading") {
                    HealthCheckResult{ state: HealthState::Loading, http_status: Some(status), body: Some(text), error: None }
                } else {
                    HealthCheckResult{ state: HealthState::Ready, http_status: Some(status), body: Some(text), error: None }
                }
            } else if status == 503 {
                // often loading
                HealthCheckResult{ state: HealthState::Loading, http_status: Some(status), body: Some(text), error: None }
            } else if status >= 500 {
                HealthCheckResult{ state: HealthState::Failed, http_status: Some(status), body: Some(text), error: Some(format!("HTTP {}", status)) }
            } else {
                HealthCheckResult{ state: HealthState::Unknown, http_status: Some(status), body: Some(text), error: Some(format!("HTTP {}", status)) }
            }
        }
        Err(e) => {
            // connection refused -> Starting/Loading or Exited
            let msg = e.to_string();
            if msg.contains("Connection refused") || msg.contains(" refused") {
                HealthCheckResult{ state: HealthState::Starting, http_status: None, body: None, error: Some(msg) }
            } else {
                HealthCheckResult{ state: HealthState::Unknown, http_status: None, body: None, error: Some(msg) }
            }
        }
    }
}

pub async fn wait_for_ready(base_url: &str, timeout: Duration, interval: Duration) -> Result<HealthCheckResult, String> {
    let start = std::time::Instant::now();
    loop {
        if start.elapsed() > timeout {
            return Err(format!("health timeout after {:?} for {}", timeout, base_url));
        }
        let res = check_health(base_url).await;
        match res.state {
            HealthState::Ready => return Ok(res),
            HealthState::Failed | HealthState::Exited => return Err(format!("health failed: {:?}", res)),
            _ => {
                tokio::time::sleep(interval).await;
                continue;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn test_health_transitions() {
        // Check that check_health returns a state, not panic, for invalid URL
        let res = check_health("http://127.0.0.1:65535").await;
        assert!(matches!(res.state, HealthState::Starting | HealthState::Unknown));
    }
}
