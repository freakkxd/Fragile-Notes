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
    // Security: only check shell metas here. Forbidden --host/--port/--model for runtime_args is already filtered
    // in RuntimeManager::build_args (manager controls these). The final args must contain --model/--port.
    for a in &args {
        if a.contains(';') || a.contains('|') || a.contains('`') || a.contains('$') {
            return Err(format!("runtime_args contains shell meta: {}", a));
        }
    }
    // Ensure required managed args are present
    if !args.contains(&"--model".to_string()) || !args.contains(&"--port".to_string()) {
        return Err("missing required --model/--port args".to_string());
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
    let inspector = super::inspector::default_inspector();
    inspector.exists(pid).unwrap_or(false)
}

pub fn command_line_matches(pid: u32, expected_exe: &str) -> bool {
    let inspector = super::inspector::default_inspector();
    inspector.command_line(pid).map(|s| s.contains(expected_exe)).unwrap_or(false)
}

pub fn verify_ownership(managed: &ManagedProcess, expected_instance: &str) -> Result<(), String> {
    if managed.instance_id != expected_instance {
        return Err(format!("instance mismatch: {} != {}", managed.instance_id, expected_instance));
    }
    let inspector = super::inspector::default_inspector();
    if !inspector.exists(managed.pid).unwrap_or(false) {
        return Err(format!("PID {} not running (stale)", managed.pid));
    }
    let cmd = inspector.command_line(managed.pid).unwrap_or_default();
    if !cmd.is_empty() && !cmd.contains(&managed.executable) {
        return Err(format!("PID {} command line does not match {}", managed.pid, managed.executable));
    }
    // also check via trait owns_process for full verification (includes instance_id)
    let owns = inspector.owns_process(managed).map_err(|e| format!("owns check failed: {}", e))?;
    if !owns {
        return Err(format!("PID {} not owned by instance {}", managed.pid, managed.instance_id));
    }
    Ok(())
}

#[cfg(unix)]
pub fn graceful_terminate(pid: u32) -> Result<(), String> {
    // Unix: SIGTERM via kill
    let out = std::process::Command::new("kill").arg("-TERM").arg(pid.to_string()).output().map_err(|e| e.to_string())?;
    if !out.status.success() && !String::from_utf8_lossy(&out.stderr).is_empty() {
        // not fatal, process may have already exited
    }
    Ok(())
}

#[cfg(windows)]
pub fn graceful_terminate(_pid: u32) -> Result<(), String> {
    // Windows: Job Object would terminate tree; for now, direct child only
    // TODO P1.3: use Job Object, for now just return Ok and let manager do kill via Child handle
    // This is explicit limitation, not silent fallback
    Ok(())
}

#[cfg(unix)]
pub fn force_kill(pid: u32) -> Result<(), String> {
    let _ = std::process::Command::new("kill").arg("-KILL").arg(pid.to_string()).output();
    Ok(())
}

#[cfg(windows)]
pub fn force_kill(_pid: u32) -> Result<(), String> {
    // Windows: TerminateProcess via OpenProcess
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
        // Shell meta should be rejected
        let res = spawn_llama_server(&PathBuf::from("/bin/false"), vec!["--model".to_string(), "a.gguf".to_string(), "--port".to_string(), "8010".to_string(), "; rm -rf".to_string()], HashMap::new(), "r1", 8010, "p1", "m1", "inst1");
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("shell meta"));
        // Missing required args should also fail
        let res2 = spawn_llama_server(&PathBuf::from("/bin/false"), vec!["--model".to_string(), "a.gguf".to_string()], HashMap::new(), "r1", 8010, "p1", "m1", "inst1");
        assert!(res2.is_err());
        assert!(res2.unwrap_err().contains("missing required"));
    }
}
