use super::types::ManagedProcess;

pub trait ProcessInspector: Send + Sync {
    fn exists(&self, pid: u32) -> Result<bool, String>;
    fn command_line(&self, pid: u32) -> Result<String, String>;
    fn owns_process(&self, managed: &ManagedProcess) -> Result<bool, String>;
}

#[cfg(unix)]
pub struct UnixInspector;

#[cfg(unix)]
impl ProcessInspector for UnixInspector {
    fn exists(&self, pid: u32) -> Result<bool, String> {
        Ok(std::path::Path::new(&format!("/proc/{}", pid)).exists())
    }
    fn command_line(&self, pid: u32) -> Result<String, String> {
        let path = format!("/proc/{}/cmdline", pid);
        let content = std::fs::read(&path).map_err(|e| e.to_string())?;
        Ok(String::from_utf8_lossy(&content).to_string())
    }
    fn owns_process(&self, managed: &ManagedProcess) -> Result<bool, String> {
        if !self.exists(managed.pid)? {
            return Ok(false);
        }
        let cmd = self.command_line(managed.pid)?;
        Ok(cmd.contains(&managed.executable) && managed.instance_id == managed.instance_id)
    }
}

#[cfg(windows)]
pub struct WindowsInspector;

#[cfg(windows)]
impl ProcessInspector for WindowsInspector {
    fn exists(&self, _pid: u32) -> Result<bool, String> {
        // TODO P1.2: Use OpenProcess + GetExitCodeProcess + Job Object for tree
        // For now, stub — direct child only, real impl in P1.3 with winapi Job Object
        Err("Windows process inspector not yet implemented — use Job Object in P1.3".to_string())
    }
    fn command_line(&self, _pid: u32) -> Result<String, String> {
        Err("Windows command_line not yet implemented".to_string())
    }
    fn owns_process(&self, _managed: &ManagedProcess) -> Result<bool, String> {
        Err("Windows owns_process not yet implemented".to_string())
    }
}

pub fn default_inspector() -> Box<dyn ProcessInspector> {
    #[cfg(unix)]
    { Box::new(UnixInspector) }
    #[cfg(windows)]
    { Box::new(WindowsInspector) }
    #[cfg(not(any(unix, windows)))]
    { Box::new(UnixInspector) }
}
