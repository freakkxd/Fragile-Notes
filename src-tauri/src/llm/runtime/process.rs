use std::collections::HashMap;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use chrono::Utc;
use super::types::{ManagedProcess, HealthState};

pub fn hash_command(executable: &str, args: &[String]) -> String {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    let mut hasher = DefaultHasher::new();
    executable.hash(&mut hasher);
    for a in args { a.hash(&mut hasher); }
    format!("{:x}", hasher.finish())
}

pub fn spawn_llama_server(
    executable: &PathBuf,
    args: Vec<String>,
    envs: HashMap<String, String>,
    runtime_id: &str,
    port: u16,
    profile_id: &str,
    model_id: &str,
    instance_id: &str,
) -> Result<(Child, ManagedProcess), String> {
    // Whitelist: forbid --host, --port, --model override via runtime_args (manager controls)
    for a in &args {
        if a == "--host" || a == "--port" || a == "--model" {
            return Err(format!("runtime_args contains forbidden arg: {} — managed by RuntimeManager", a));
        }
        if a.contains(';') || a.contains('|') || a.contains('`') || a.contains('$') {
            return Err(format!("runtime_args contains shell meta: {}", a));
        }
    }

    let mut cmd = Command::new(executable);
    cmd.args(&args)
        .envs(&envs)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = cmd.spawn().map_err(|e| format!("spawn failed: {}", e))?;

    // Verify child still alive immediately
    match child.try_wait() {
        Ok(Some(status)) => return Err(format!("process exited immediately: {}", status)),
        Ok(None) => {},
        Err(e) => return Err(format!("try_wait failed: {}", e)),
    }

    let pid = child.id();
    let managed = ManagedProcess {
        pid,
        runtime_id: runtime_id.to_string(),
        port,
        started_at: Utc::now(),
        command_hash: hash_command(&executable.to_string_lossy(), &args),
        executable: executable.to_string_lossy().to_string(),
        profile_id: profile_id.to_string(),
        model_id: model_id.to_string(),
        instance_id: instance_id.to_string(),
    };
    Ok((child, managed))
}

pub fn is_process_alive(pid: u32) -> bool {
    PathBuf::from(format!("/proc/{}", pid)).exists()
}

pub fn command_line_matches(pid: u32, expected_exe: &str) -> bool {
    let path = format!("/proc/{}/cmdline", pid);
    if let Ok(content) = std::fs::read(path) {
        let cmdline = String::from_utf8_lossy(&content);
        return cmdline.contains(expected_exe);
    }
    false
}

pub fn verify_ownership(managed: &ManagedProcess, expected_instance: &str) -> Result<(), String> {
    if managed.instance_id != expected_instance {
        return Err(format!("instance mismatch: {} != {}", managed.instance_id, expected_instance));
    }
    if !is_process_alive(managed.pid) {
        return Err(format!("PID {} not running (stale)", managed.pid));
    }
    if !command_line_matches(managed.pid, &managed.executable) {
        return Err(format!("PID {} command line does not match {}", managed.pid, managed.executable));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn test_hash_stable() {
        let h1 = hash_command("/usr/bin/llama-server", &["--model".to_string(), "a.gguf".to_string()]);
        let h2 = hash_command("/usr/bin/llama-server", &["--model".to_string(), "a.gguf".to_string()]);
        assert_eq!(h1, h2);
    }
    #[test]
    fn test_forbidden_args() {
        let res = spawn_llama_server(&PathBuf::from("/bin/false"), vec!["--port".to_string()], HashMap::new(), "r1", 8010, "p1", "m1", "inst1");
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("forbidden"));
    }
}
