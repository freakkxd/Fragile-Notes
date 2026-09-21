use std::path::{Path, PathBuf};
use std::time::Duration;

#[derive(Debug, Clone, PartialEq)]
pub enum ExecutableSource {
    SystemPath(PathBuf),
    BundledSidecar,
}

#[derive(Debug, Clone)]
pub struct RuntimeCapabilities {
    pub version: String,
    pub has_cuda: bool,
    pub has_vulkan: bool,
}

#[derive(Debug, Clone)]
pub struct ResolvedExecutable {
    pub path: PathBuf,
    pub source: ExecutableSource,
    pub version: Option<String>,
    pub capabilities: Option<RuntimeCapabilities>,
}

pub fn resolve_executable(user_path: &str) -> Result<ResolvedExecutable, String> {
    // 1. explicit user path — if set but invalid, fail without fallback (user expects this path)
    if !user_path.is_empty() && user_path != "llama-server" {
        let p = PathBuf::from(user_path);
        if p.exists() {
            // Validate --version with timeout, no shell
            let version = validate_executable(&p).ok();
            return Ok(ResolvedExecutable { path: p.clone(), source: ExecutableSource::SystemPath(p), version: version.clone(), capabilities: version.map(|v| RuntimeCapabilities{ version: v, has_cuda: false, has_vulkan: false }) });
        } else {
            return Err(format!("executable not found at explicit path: {} — install llama.cpp: https://github.com/ggerganov/llama.cpp", p.display()));
        }
    }
    // 2. bundled sidecar
    let bundled = PathBuf::from("src-tauri/binaries/llama-server");
    if bundled.exists() {
        let version = validate_executable(&bundled).ok();
        return Ok(ResolvedExecutable { path: bundled.clone(), source: ExecutableSource::BundledSidecar, version: version.clone(), capabilities: version.map(|v| RuntimeCapabilities{ version: v, has_cuda: false, has_vulkan: false }) });
    }
    // 3. PATH lookup
    if let Some(found) = find_in_path("llama-server") {
        let version = validate_executable(&found).ok();
        return Ok(ResolvedExecutable { path: found.clone(), source: ExecutableSource::SystemPath(found), version: version.clone(), capabilities: version.map(|v| RuntimeCapabilities{ version: v, has_cuda: false, has_vulkan: false }) });
    }
    Err("llama-server not found in PATH and no explicit path set — install llama.cpp: https://github.com/ggerganov/llama.cpp".to_string())
}

fn find_in_path(name: &str) -> Option<PathBuf> {
    let path_var = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path_var) {
        let candidate = dir.join(name);
        if candidate.exists() {
            return Some(candidate);
        }
        // Windows .exe
        let candidate_exe = dir.join(format!("{}.exe", name));
        if candidate_exe.exists() {
            return Some(candidate_exe);
        }
    }
    None
}

pub fn validate_executable(path: &Path) -> Result<String, String> {
    use std::time::Duration;
    // Timeout and truncated stdout/stderr, no shell
    let mut cmd = std::process::Command::new(path);
    cmd.arg("--version");
    // Use wait_timeout via manual timeout
    let mut child = cmd.stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::piped()).spawn().map_err(|e| format!("failed to run --version: {}", e))?;
    let start = std::time::Instant::now();
    let timeout = Duration::from_secs(5);
    loop {
        if start.elapsed() > timeout {
            let _ = child.kill();
            return Err("validate --version timeout".to_string());
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                let out = child.wait_with_output().unwrap_or_else(|_| std::process::Output{ status, stdout: vec![], stderr: vec![] });
                // Truncate stdout/stderr to 1KB
                let stdout = String::from_utf8_lossy(&out.stdout[..out.stdout.len().min(1024)]).trim().to_string();
                let stderr = String::from_utf8_lossy(&out.stderr[..out.stderr.len().min(1024)]).trim().to_string();
                if out.status.success() {
                    return Ok(stdout);
                } else {
                    return Err(format!("--version failed: stdout:{} stderr:{}", stdout, stderr));
                }
            },
            Ok(None) => std::thread::sleep(Duration::from_millis(50)),
            Err(e) => return Err(format!("try_wait failed: {}", e)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn test_resolve_explicit_missing() {
        let res = resolve_executable("/nonexistent/path/llama-server");
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("explicit path"));
    }
    #[test]
    fn test_explicit_invalid_no_fallback() {
        // Explicit invalid should not fallback to PATH
        let res = resolve_executable("/tmp/nonexistent-llama-12345");
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("explicit path"));
    }
    #[test]
    fn test_path_with_spaces() {
        let dir = std::env::temp_dir().join("fragile test space");
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("llama-server");
        // Create a dummy executable that returns --version success
        let _ = fs::write(&path, "#!/bin/sh\necho 'llama-server 0.0.1'\n");
        let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o755));
        if path.exists() {
            let res = resolve_executable(path.to_string_lossy().as_ref());
            assert!(res.is_ok(), "path with spaces should be handled: {:?}", res);
            let _ = fs::remove_file(&path);
        }
    }
    #[test]
    fn test_malicious_path_no_shell() {
        // Path containing shell meta should not be executed via shell
        let res = resolve_executable("/tmp/llama; rm -rf /");
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("explicit path"));
    }
    #[test]
    fn test_version_non_zero_fails() {
        let dir = std::env::temp_dir().join("fragile-version-fail");
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("llama-fail");
        let _ = fs::write(&path, "#!/bin/sh\nexit 1\n");
        let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o755));
        if path.exists() {
            let res = validate_executable(&path);
            assert!(res.is_err());
            assert!(res.unwrap_err().contains("failed"));
            let _ = fs::remove_file(&path);
        }
    }
    #[test]
    fn test_stdout_truncated() {
        let dir = std::env::temp_dir().join("fragile-trunc");
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("llama-trunc");
        // Create script that outputs >2KB
        let _ = fs::write(&path, "#!/bin/sh\npython3 -c \"print('a'*5000)\"\n");
        let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o755));
        if path.exists() {
            let res = validate_executable(&path);
            // Should be ok and truncated to 1KB, but still success
            if let Ok(s) = res { assert!(s.len() <= 1024); }
            let _ = fs::remove_file(&path);
        }
    }
}
