use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq)]
pub enum ExecutableSource {
    SystemPath(PathBuf),
    BundledSidecar,
}

#[derive(Debug, Clone)]
pub struct ResolvedExecutable {
    pub path: PathBuf,
    pub source: ExecutableSource,
}

pub fn resolve_executable(user_path: &str) -> Result<ResolvedExecutable, String> {
    // 1. explicit user path
    if !user_path.is_empty() && user_path != "llama-server" {
        let p = PathBuf::from(user_path);
        if p.exists() {
            return Ok(ResolvedExecutable { path: p.clone(), source: ExecutableSource::SystemPath(p) });
        } else {
            return Err(format!("executable not found at explicit path: {} — install llama.cpp: https://github.com/ggerganov/llama.cpp", p.display()));
        }
    }
    // 2. bundled sidecar — check src-tauri/binaries or bundled location
    // In P1.1 sidecar packaging not ready, so we check common bundled path
    let bundled = PathBuf::from("src-tauri/binaries/llama-server");
    if bundled.exists() {
        return Ok(ResolvedExecutable { path: bundled.clone(), source: ExecutableSource::BundledSidecar });
    }
    // 3. PATH lookup via `which` logic without external crate — try which via PATH env
    if let Some(found) = find_in_path("llama-server") {
        return Ok(ResolvedExecutable { path: found.clone(), source: ExecutableSource::SystemPath(found) });
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
    // Run `llama-server --version` via direct Command, not shell
    let out = std::process::Command::new(path)
        .arg("--version")
        .output()
        .map_err(|e| format!("failed to run --version: {}", e))?;
    if out.status.success() {
        Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
    } else {
        Err(format!("--version failed: {}", String::from_utf8_lossy(&out.stderr)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn test_resolve_explicit_missing() {
        let res = resolve_executable("/nonexistent/path/llama-server");
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("explicit path"));
    }
}
