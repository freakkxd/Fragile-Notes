use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use chrono::Utc;

use super::allocator::PortAllocator;
use super::executable::resolve_executable;
use super::health::check_health;
use super::process::{spawn_llama_server, verify_ownership};
use super::types::{HealthState, ManagedProcess, RuntimeInfo};

pub struct RuntimeManager {
    state_path: PathBuf,
    instance_id: String,
    children: Arc<Mutex<HashMap<String, Child>>>,
    allocator: PortAllocator,
}

impl RuntimeManager {
    pub fn new(state_path: PathBuf) -> Self {
        let instance_id = uuid::Uuid::new_v4().to_string();
        Self {
            state_path,
            instance_id,
            children: Arc::new(Mutex::new(HashMap::new())),
            allocator: PortAllocator::new(8010),
        }
    }

    pub fn instance_id(&self) -> &str { &self.instance_id }

    fn load_state(&self) -> Vec<ManagedProcess> {
        if !self.state_path.exists() { return vec![]; }
        if let Ok(txt) = std::fs::read_to_string(&self.state_path) {
            if let Ok(state) = serde_json::from_str::<serde_json::Value>(&txt) {
                if let Some(arr) = state.get("runtimes").and_then(|v| v.as_array()) {
                    let mut out = vec![];
                    for v in arr {
                        if let Ok(mp) = serde_json::from_value::<ManagedProcess>(v.clone()) {
                            out.push(mp);
                        }
                    }
                    return out;
                }
                // fallback: try direct Vec<ManagedProcess>
                if let Ok(list) = serde_json::from_str::<Vec<ManagedProcess>>(&txt) {
                    return list;
                }
            }
        }
        vec![]
    }

    fn save_state(&self, runtimes: &[ManagedProcess]) -> Result<(), String> {
        let dir = self.state_path.parent().unwrap_or(Path::new("."));
        let _ = std::fs::create_dir_all(dir);
        let state = serde_json::json!({
            "instance_id": self.instance_id,
            "runtimes": runtimes,
            "updated_at": Utc::now().to_rfc3339()
        });
        let tmp = self.state_path.with_extension("tmp");
        let txt = serde_json::to_string_pretty(&state).map_err(|e| e.to_string())?;
        std::fs::write(&tmp, txt).map_err(|e| e.to_string())?;
        if let Ok(f) = std::fs::File::open(&tmp) { let _ = f.sync_all(); }
        std::fs::rename(&tmp, &self.state_path).map_err(|e| e.to_string())?;
        Ok(())
    }

    pub async fn start(&self, profile_id: &str, model_path: &str, binary_path: &str, settings: &crate::llm::LlamaSettings) -> Result<RuntimeInfo, String> {
        // Check max_active =1
        let mut runtimes = self.load_state();
        if runtimes.len() >= 1 {
            return Err("max_active_runtimes=1: stop existing runtime before starting new (task-exclusive)".to_string());
        }
        // Allocate port
        let occupied: HashSet<u16> = runtimes.iter().map(|r| r.port).collect();
        let port = self.allocator.allocate(&occupied)?;
        // Resolve executable
        let resolved = resolve_executable(binary_path)?;
        let args = build_args(model_path, port, settings);
        let runtime_id = format!("runtime-{}-{}", profile_id, Utc::now().timestamp_millis());
        let envs: HashMap<String, String> = HashMap::new();
        let (child, managed) = spawn_llama_server(&resolved.path, args, envs, &runtime_id, port, profile_id, model_path, &self.instance_id)?;
        let pid = managed.pid;
        runtimes.push(managed.clone());
        self.save_state(&runtimes)?;
        // Track child for logs/exit
        if let Ok(mut map) = self.children.lock() {
            map.insert(runtime_id.clone(), child);
        }
        // Wait for health with timeout and cancellation
        let base_url = format!("http://127.0.0.1:{}", port);
        let health = self.wait_health_with_timeout(&base_url, Duration::from_secs(60), Duration::from_millis(500)).await;
        match health {
            Ok(h) if h == HealthState::Ready => {
                Ok(RuntimeInfo{
                    runtime_id: managed.runtime_id,
                    profile_id: profile_id.to_string(),
                    model_id: model_path.to_string(),
                    port,
                    pid: Some(pid),
                    status: HealthState::Ready,
                    started_at: Some(managed.started_at),
                    executable: resolved.path.to_string_lossy().to_string(),
                    exit_code: None,
                    last_error: None,
                })
            },
            Ok(state) => {
                // NoSlots etc still considered not ready
                Err(format!("health not ready: {:?}", state))
            },
            Err(e) => {
                // cleanup on failure
                let _ = self.stop(&runtime_id).await;
                Err(e)
            }
        }
    }

    async fn wait_health_with_timeout(&self, base_url: &str, timeout: Duration, interval: Duration) -> Result<HealthState, String> {
        let start = std::time::Instant::now();
        // simple loop with timeout (cancellation via timeout, P1.1 no explicit token)
        loop {
            if start.elapsed() > timeout {
                return Err(format!("health timeout after {:?}", timeout));
            }
            let res = check_health(base_url).await;
            match res.state {
                HealthState::Ready => return Ok(HealthState::Ready),
                HealthState::Failed | HealthState::Exited => return Err(format!("health failed: {:?}", res)),
                _ => {
                    tokio::time::sleep(interval).await;
                    continue;
                }
            }
        }
    }

    pub async fn stop(&self, runtime_id: &str) -> Result<(), String> {
        let mut runtimes = self.load_state();
        let pos = runtimes.iter().position(|r| r.runtime_id == runtime_id).ok_or("runtime not found")?;
        let managed = runtimes.remove(pos);
        // Verify ownership before kill
        verify_ownership(&managed, &self.instance_id)?;
        // Kill process if still alive
        if let Ok(mut map) = self.children.lock() {
            if let Some(mut child) = map.remove(runtime_id) {
                let _ = child.kill();
                let _ = child.wait();
            }
        } else {
            // Fallback: kill via PID
            let _ = std::process::Command::new("kill").arg(managed.pid.to_string()).output();
        }
        self.save_state(&runtimes)?;
        Ok(())
    }

    pub async fn status(&self, runtime_id: &str) -> Result<RuntimeInfo, String> {
        let runtimes = self.load_state();
        let mp = runtimes.iter().find(|r| r.runtime_id == runtime_id).ok_or("runtime not found")?;
        let base_url = format!("http://127.0.0.1:{}", mp.port);
        let health = check_health(&base_url).await;
        Ok(RuntimeInfo{
            runtime_id: mp.runtime_id.clone(),
            profile_id: mp.profile_id.clone(),
            model_id: mp.model_id.clone(),
            port: mp.port,
            pid: Some(mp.pid),
            status: health.state,
            started_at: Some(mp.started_at),
            executable: mp.executable.clone(),
            exit_code: None,
            last_error: health.error,
        })
    }

    pub fn list(&self) -> Result<Vec<RuntimeInfo>, String> {
        let runtimes = self.load_state();
        let mut out = Vec::new();
        for mp in runtimes {
            // stale check
            let alive = PathBuf::from(format!("/proc/{}", mp.pid)).exists();
            let status = if !alive { HealthState::Exited } else { HealthState::Unknown };
            out.push(RuntimeInfo{
                runtime_id: mp.runtime_id.clone(),
                profile_id: mp.profile_id.clone(),
                model_id: mp.model_id.clone(),
                port: mp.port,
                pid: Some(mp.pid),
                status,
                started_at: Some(mp.started_at),
                executable: mp.executable.clone(),
                exit_code: None,
                last_error: None,
            });
        }
        Ok(out)
    }

    pub fn cleanup_stale(&self) -> Result<usize, String> {
        let mut runtimes = self.load_state();
        let before = runtimes.len();
        runtimes.retain(|mp| {
            if mp.instance_id != self.instance_id {
                // different instance, check if PID still alive and belongs to us
                // if not alive, it's stale
                if !PathBuf::from(format!("/proc/{}", mp.pid)).exists() {
                    return false;
                }
            }
            // also check if PID is stale (not alive)
            if !std::path::Path::new(&format!("/proc/{}", mp.pid)).exists() {
                return false;
            }
            true
        });
        let removed = before - runtimes.len();
        if removed > 0 {
            let _ = self.save_state(&runtimes);
        }
        Ok(removed)
    }
}

fn build_args(model_path: &str, port: u16, settings: &crate::llm::LlamaSettings) -> Vec<String> {
    let mut args = vec![
        "--model".to_string(), model_path.to_string(),
        "--port".to_string(), port.to_string(),
        "--ctx-size".to_string(), settings.n_ctx.to_string(),
        "--temp".to_string(), settings.temp.to_string(),
    ];
    if settings.n_threads != 0 {
        args.push("--threads".to_string());
        args.push(settings.n_threads.to_string());
    }
    if settings.n_gpu_layers != 0 {
        args.push("--n-gpu-layers".to_string());
        args.push(settings.n_gpu_layers.to_string());
    }
    if !settings.use_mmap {
        args.push("--no-mmap".to_string());
    }
    if settings.advanced.mlock { args.push("--mlock".to_string()); }
    if settings.advanced.flash_attention { args.push("--flash-attn".to_string()); }
    // whitelist runtime_args
    for a in &settings.runtime_args {
        if a == "--host" || a == "--port" || a == "--model" { continue; }
        args.push(a.clone());
    }
    args
}
