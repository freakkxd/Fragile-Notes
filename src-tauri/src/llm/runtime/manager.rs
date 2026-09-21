use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use chrono::Utc;

use super::allocator::PortAllocator;
use super::executable::resolve_executable;
use super::health::check_health;
use super::logs::LogSink;
use super::process::{spawn_llama_server, verify_ownership};
use super::types::{HealthState as TypesHealthState, ManagedProcess, RuntimeInfo};

pub struct RuntimeManager {
    state_path: PathBuf,
    logs_base: PathBuf,
    instance_id: String,
    children: Arc<Mutex<HashMap<String, Child>>>,
    sinks: Arc<Mutex<HashMap<String, Arc<LogSink>>>>,
    allocator: PortAllocator,
    health_policy: super::types::HealthPolicy,
}

impl RuntimeManager {
    pub fn new(state_path: PathBuf) -> Self {
        let instance_id = uuid::Uuid::new_v4().to_string();
        let logs_base = state_path.parent().unwrap_or(Path::new(".")).to_path_buf();
        Self {
            state_path,
            logs_base,
            instance_id,
            children: Arc::new(Mutex::new(HashMap::new())),
            sinks: Arc::new(Mutex::new(HashMap::new())),
            allocator: PortAllocator::new(8010),
            health_policy: super::types::HealthPolicy::default(),
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
        let mut runtimes = self.load_state();
        if runtimes.len() >= 1 {
            return Err("max_active_runtimes=1: stop existing runtime before starting new (task-exclusive)".to_string());
        }
        let occupied: HashSet<u16> = runtimes.iter().map(|r| r.port).collect();
        let port = self.allocator.allocate(&occupied)?;
        let resolved = resolve_executable(binary_path)?;
        let args = build_args(model_path, port, settings);
        let runtime_id = format!("runtime-{}-{}", profile_id, Utc::now().timestamp_millis());
        let envs: HashMap<String, String> = HashMap::new();
        let (mut child, managed) = spawn_llama_server(&resolved.path, args, envs, &runtime_id, port, profile_id, model_path, &self.instance_id)?;
        // Setup logs: take stdout/stderr and drain
        let sink = Arc::new(LogSink::new(&runtime_id, &self.logs_base));
        if let Some(stdout) = child.stdout.take() {
            sink.drain_stdout(stdout);
        }
        if let Some(stderr) = child.stderr.take() {
            sink.drain_stderr(stderr);
        }
        if let Ok(mut m) = self.sinks.lock() { m.insert(runtime_id.clone(), sink.clone()); }
        let pid = managed.pid;
        runtimes.push(managed.clone());
        self.save_state(&runtimes)?;
        if let Ok(mut map) = self.children.lock() { map.insert(runtime_id.clone(), child); }
        // Spawn exit watcher (poll /proc, no Child lock across await)
        let pid_for_watcher = pid;
        let state_path_clone = self.state_path.clone();
        let instance_id_clone = self.instance_id.clone();
        let rid_clone = runtime_id.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_millis(500)).await;
                let alive = PathBuf::from(format!("/proc/{}", pid_for_watcher)).exists();
                if !alive { break; }
            }
            // Update state file to mark Exited if still present
            if state_path_clone.exists() {
                if let Ok(txt) = std::fs::read_to_string(&state_path_clone) {
                    if let Ok(mut val) = serde_json::from_str::<serde_json::Value>(&txt) {
                        if let Some(arr) = val.get_mut("runtimes").and_then(|v| v.as_array_mut()) {
                            for v in arr.iter_mut() {
                                if v.get("runtime_id").and_then(|x| x.as_str()) == Some(&rid_clone) {
                                    // would update status to Exited
                                }
                            }
                        }
                    }
                }
            }
            let _ = instance_id_clone;
        });
        // Wait health
        let base_url = format!("http://127.0.0.1:{}", port);
        match self.wait_health_with_policy(&base_url).await {
            Ok(health) if health == TypesHealthState::Ready => {
                Ok(RuntimeInfo{
                    runtime_id: managed.runtime_id,
                    profile_id: profile_id.to_string(),
                    model_id: model_path.to_string(),
                    port,
                    pid: Some(pid),
                    status: TypesHealthState::Ready,
                    started_at: Some(managed.started_at),
                    executable: resolved.path.to_string_lossy().to_string(),
                    exit_code: None,
                    last_error: None,
                })
            },
            Ok(state) => {
                let _ = self.stop(&runtime_id).await;
                Err(format!("health not ready: {:?}", state))
            },
            Err(e) => {
                let _ = self.stop(&runtime_id).await;
                Err(e)
            }
        }
    }

    async fn wait_health_with_policy(&self, base_url: &str, ) -> Result<TypesHealthState, String> {
        let policy = &self.health_policy;
        let start = std::time::Instant::now();
        let mut consecutive_failures = 0;
        loop {
            if start.elapsed() > Duration::from_millis(policy.startup_timeout_ms) {
                return Err(format!("health timeout after {}ms for {}", policy.startup_timeout_ms, base_url));
            }
            let res = check_health(base_url).await;
            match res.state {
                TypesHealthState::Ready => return Ok(TypesHealthState::Ready),
                TypesHealthState::NoSlots => return Ok(TypesHealthState::NoSlots),
                TypesHealthState::Failed => {
                    consecutive_failures += 1;
                    if consecutive_failures >= policy.consecutive_failures {
                        return Err(format!("health failed consecutive {}: {:?}", consecutive_failures, res.error));
                    }
                },
                TypesHealthState::Exited => return Err("process exited".to_string()),
                _ => {
                    consecutive_failures = 0;
                }
            }
            tokio::time::sleep(Duration::from_millis(policy.poll_interval_ms)).await;
        }
    }

    pub async fn stop(&self, runtime_id: &str) -> Result<(), String> {
        let mut runtimes = self.load_state();
        let pos = runtimes.iter().position(|r| r.runtime_id == runtime_id).ok_or("runtime not found")?;
        let managed = runtimes.remove(pos);
        verify_ownership(&managed, &self.instance_id)?;
        // Graceful: try SIGTERM, wait grace, then SIGKILL
        let grace = Duration::from_millis(self.health_policy.shutdown_grace_ms);
        let pid = managed.pid;
        // Try graceful termination via kill -TERM
        let _ = std::process::Command::new("kill").arg("-TERM").arg(pid.to_string()).output();
        let start = std::time::Instant::now();
        while start.elapsed() < grace {
            if !PathBuf::from(format!("/proc/{}", pid)).exists() { break; }
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
        // Force kill if still alive and owned
        if PathBuf::from(format!("/proc/{}", pid)).exists() {
            // Verify still our process before force
            if verify_ownership(&managed, &self.instance_id).is_ok() {
                let _ = std::process::Command::new("kill").arg("-KILL").arg(pid.to_string()).output();
                // Also try via Child handle
                if let Ok(mut map) = self.children.lock() {
                    if let Some(mut child) = map.remove(runtime_id) {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                }
            }
        } else {
            // Already exited, remove from children
            if let Ok(mut map) = self.children.lock() { map.remove(runtime_id); }
        }
        // Wait for exit watcher to update, then save state
        self.save_state(&runtimes)?;
        // Keep logs, but we could rotate
        Ok(())
    }

    pub async fn status(&self, runtime_id: &str) -> Result<RuntimeInfo, String> {
        let runtimes = self.load_state();
        let mp = runtimes.iter().find(|r| r.runtime_id == runtime_id).ok_or("runtime not found")?;
        let base_url = format!("http://127.0.0.1:{}", mp.port);
        let health = check_health(&base_url).await;
        let process_state = if PathBuf::from(format!("/proc/{}", mp.pid)).exists() { "running" } else { "exited" };
        let combined = super::health::combine_health(process_state, &health);
        Ok(RuntimeInfo{
            runtime_id: mp.runtime_id.clone(),
            profile_id: mp.profile_id.clone(),
            model_id: mp.model_id.clone(),
            port: mp.port,
            pid: Some(mp.pid),
            status: combined,
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
            let alive = PathBuf::from(format!("/proc/{}", mp.pid)).exists();
            let status = if !alive { TypesHealthState::Exited } else { TypesHealthState::Unknown };
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
                if !PathBuf::from(format!("/proc/{}", mp.pid)).exists() { return false; }
                // also check command line matches
                if !crate::llm::runtime::process::command_line_matches(mp.pid, &mp.executable) { return false; }
            }
            if !PathBuf::from(format!("/proc/{}", mp.pid)).exists() { return false; }
            true
        });
        let removed = before - runtimes.len();
        if removed > 0 { let _ = self.save_state(&runtimes); }
        Ok(removed)
    }

    pub fn logs(&self, runtime_id: &str, tail: usize) -> Result<Vec<super::types::LogLine>, String> {
        let sinks = self.sinks.lock().map_err(|_| "lock failed".to_string())?;
        if let Some(sink) = sinks.get(runtime_id) {
            Ok(sink.tail(tail))
        } else {
            // Try reading from file
            let path = self.logs_base.join("runtime-logs").join(format!("{}.log", runtime_id));
            if path.exists() {
                if let Ok(content) = std::fs::read_to_string(&path) {
                    let lines: Vec<super::types::LogLine> = content.lines().rev().take(tail).filter_map(|l| {
                        Some(super::types::LogLine{
                            ts: Utc::now(), timestamp: Utc::now(), runtime_id: runtime_id.to_string(),
                            stream: super::types::LogStream::Stdout, level: super::types::LogLevel::Info,
                            text: l.to_string(), source: "file".to_string(), line: l.to_string()
                        })
                    }).collect();
                    return Ok(lines);
                }
            }
            Err("no logs for runtime".to_string())
        }
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
    if !settings.use_mmap { args.push("--no-mmap".to_string()); }
    if settings.advanced.mlock { args.push("--mlock".to_string()); }
    if settings.advanced.flash_attention { args.push("--flash-attn".to_string()); }
    for a in &settings.runtime_args {
        if a == "--host" || a == "--port" || a == "--model" { continue; }
        args.push(a.clone());
    }
    args
}
