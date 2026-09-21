use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::Mutex;
use uuid::Uuid;

use crate::llm::runtime::manager::RuntimeManager;
use super::policy::{validate_privacy, scope_of, RuntimeRetention, RuntimeSwitchPolicy};
use super::selection::resolve_selection;
use crate::llm::{Capability, LlmConfig};

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct TaskRun {
    pub run_id: Uuid,
    pub task_profile_id: String,
    pub provider_id: String,
    pub model_id: String,
    pub runtime_id: Option<String>,
}

#[derive(Debug, Clone)]
pub struct TaskCancellation {
    pub run_id: Uuid,
    pub token: tokio_util::sync::CancellationToken,
}

#[async_trait::async_trait]
pub trait LlmGateway: Send + Sync {
    async fn chat(&self, provider_id: &str, model: &str, messages: Vec<serde_json::Value>) -> Result<String, String>;
}

pub struct RealGateway;

#[async_trait::async_trait]
impl LlmGateway for RealGateway {
    async fn chat(&self, provider_id: &str, model: &str, messages: Vec<serde_json::Value>) -> Result<String, String> {
        crate::llm::gateway_chat(crate::llm::ChatRequest{ provider_id: provider_id.to_string(), model_ref: model.to_string(), messages, task_profile_id: None }).await
    }
}

pub struct TaskExecutor {
    runtime_manager: Arc<RuntimeManager>,
    gateway: Arc<dyn LlmGateway>,
    startup: Arc<super::startup::StartupManager>,
    runs: Arc<Mutex<HashMap<Uuid, TaskCancellation>>>,
    retention: RuntimeRetention,
    switch_policy: RuntimeSwitchPolicy,
}

impl TaskExecutor {
    pub fn new(runtime_manager: Arc<RuntimeManager>, gateway: Arc<dyn LlmGateway>) -> Self {
        Self {
            runtime_manager,
            gateway,
            startup: Arc::new(super::startup::StartupManager::new()),
            runs: Arc::new(Mutex::new(HashMap::new())),
            retention: RuntimeRetention::KeepWarm { idle_for: std::time::Duration::from_secs(300) },
            switch_policy: RuntimeSwitchPolicy::Queue,
        }
    }

    pub async fn run_chat(&self, task_profile_id: &str, messages: Vec<serde_json::Value>) -> Result<(TaskRun, String), String> {
        let run_id = Uuid::new_v4();
        let token = tokio_util::sync::CancellationToken::new();
        {
            let mut runs = self.runs.lock().await;
            runs.insert(run_id, TaskCancellation{ run_id, token: token.clone() });
        }
        // Load config
        let cfg = crate::llm::load_config_for_test();
        // Resolve
        let sel = resolve_selection(&cfg, task_profile_id).map_err(|e| format!("task_not_found: {}", e))?;
        // Privacy check BEFORE runtime and network
        let scope = scope_of(&sel.provider.kind, &sel.provider.endpoint);
        validate_privacy(&sel.task.privacy, &scope).map_err(|e| format!("privacy_violation: {}", e))?;
        // Capability
        if !sel.model.capabilities.is_empty() {
            // For P1.4, require Chat
            if !sel.model.capabilities.contains(&Capability::Chat) {
                return Err(format!("unsupported_capability: Chat for model {}", sel.model.id));
            }
        }
        // Registry is source of truth for path/state (E2E gate: must use ResolvedModel.path, not active_model or first file)
        let registry_model_path: Option<std::path::PathBuf> = {
            let mut reg = crate::llm::models::registry::Registry::new(crate::llm::registry_path());
            let _ = reg.load();
            if let Some(rec) = reg.all().into_iter().find(|r| r.id == sel.model.id) {
                // State gate: Changed/Missing/Invalid blocks spawn (no spawn, ModelUnavailable)
                if rec.state != crate::llm::models::types::ModelState::Present {
                    return Err(format!("model_unavailable: {} state {:?}", rec.id, rec.state));
                }
                // Also ensure RuntimeProfile model_id consistency if present
                if let Some(rp) = &sel.runtime_profile {
                    if rp.model_id != rec.id {
                        return Err(format!("model_runtime_mismatch: RuntimeProfile {} model_id {} != ModelRecord {}", rp.id, rp.model_id, rec.id));
                    }
                }
                Some(rec.path)
            } else { None }
        };
        let effective_model_path = registry_model_path.map(|p| p.to_string_lossy().to_string()).unwrap_or_else(|| sel.model.path.clone());
        // Ensure runtime if local
        let runtime_id = if matches!(scope, super::policy::ProviderScope::LocalManaged) {
            let rp = sel.runtime_profile.ok_or_else(|| "runtime_not_found: local task requires runtime profile".to_string())?;
            // Single-flight ensure_runtime
            let key = format!("runtime:{}", rp.id);
            let (should_start, notify) = self.startup.should_start(&key).await;
            if should_start {
                // We are the starter — use registry-resolved path, not stale active_model
                let mut model_for_runtime = sel.model.clone();
                model_for_runtime.path = effective_model_path.clone();
                let res = self.ensure_runtime(&rp, &model_for_runtime, &scope).await;
                match res {
                    Ok(rid) => {
                        self.startup.mark_ready(&key).await;
                        Some(rid)
                    },
                    Err(e) => {
                        self.startup.mark_failed(&key, e.clone()).await;
                        return Err(format!("runtime_start_failed: {}", e));
                    }
                }
            } else {
                // Wait for existing startup
                // Use timeout
                let wait_fut = self.startup.wait(&key, notify);
                tokio::select! {
                    _ = wait_fut => {
                        // Check if ready
                        // For now, assume ready and return existing runtime id
                        // In full impl, check state
                        Some(rp.id.clone())
                    },
                    _ = token.cancelled() => {
                        return Err("cancelled: startup wait cancelled".to_string());
                    }
                }
            }
        } else {
            None // cloud: no runtime
        };

        let task_run = TaskRun{ run_id, task_profile_id: task_profile_id.to_string(), provider_id: sel.provider.id.clone(), model_id: sel.model.id.clone(), runtime_id: runtime_id.clone() };

        // Call Gateway (with cancellation)
        let gateway = Arc::clone(&self.gateway);
        let provider_id = sel.provider.id.clone();
        let model_id = sel.model.id.clone();
        let chat_fut = gateway.chat(&provider_id, &model_id, messages);
        let response = tokio::select! {
            res = chat_fut => res,
            _ = token.cancelled() => {
                // Do not stop runtime automatically if other requests may be using it (max_active 1 case, but we keep alive)
                return Err("cancelled".to_string());
            }
        };

        // Release or keep alive per retention (for now KeepWarm/manual)
        // In P1.4, we keep alive; StopAfterRun would stop here
        match &self.retention {
            RuntimeRetention::StopAfterRun => {
                if let Some(rid) = &runtime_id {
                    let _ = self.runtime_manager.stop(rid).await;
                }
            },
            _ => {}
        }

        // Cleanup run
        {
            let mut runs = self.runs.lock().await;
            runs.remove(&run_id);
        }

        match response {
            Ok(txt) => Ok((task_run, txt)),
            Err(e) => Err(format!("provider_error: {}", e)),
        }
    }

    async fn ensure_runtime(&self, rp: &crate::llm::RuntimeProfile, model: &crate::llm::Model, _scope: &super::policy::ProviderScope) -> Result<String, String> {
        // Check if already running and same model
        let list = self.runtime_manager.list().map_err(|e| e.to_string())?;
        if let Some(existing) = list.iter().find(|r| r.profile_id == rp.id) {
            if existing.model_id == model.id && matches!(existing.status, crate::llm::runtime::types::HealthState::Ready) {
                return Ok(existing.runtime_id.clone());
            } else {
                // Different model or not ready - handle switch policy
                match self.switch_policy {
                    RuntimeSwitchPolicy::RejectIfBusy => {
                        return Err("runtime_busy: max_active 1".to_string());
                    },
                    RuntimeSwitchPolicy::Queue => {
                        // Wait for current to be stopped? For P1.4, queue means wait
                        // For now, stop old and start new
                        let _ = self.runtime_manager.stop(&existing.runtime_id).await;
                    },
                    RuntimeSwitchPolicy::StopAndSwitch => {
                        let _ = self.runtime_manager.stop(&existing.runtime_id).await;
                    }
                }
            }
        }
        // Start new runtime
        let info = self.runtime_manager.start(&rp.id, &model.path, &rp.binary_path, &rp.settings).await.map_err(|e| format!("runtime_start_failed: {}", e))?;
        Ok(info.runtime_id)
    }

    pub async fn cancel(&self, run_id: Uuid) -> Result<(), String> {
        let mut runs = self.runs.lock().await;
        if let Some(entry) = runs.get(&run_id) {
            entry.token.cancel();
            Ok(())
        } else {
            Err("run_not_found".to_string())
        }
    }

    pub async fn status(&self, run_id: Uuid) -> Result<TaskRun, String> {
        let runs = self.runs.lock().await;
        if let Some(entry) = runs.get(&run_id) {
            // Return a placeholder TaskRun for status
            Ok(TaskRun{ run_id: entry.run_id, task_profile_id: "".to_string(), provider_id: "".to_string(), model_id: "".to_string(), runtime_id: None })
        } else {
            Err("run_not_found".to_string())
        }
    }
}
