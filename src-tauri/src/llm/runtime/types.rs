use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum HealthState {
    Stopped,
    Starting,
    Loading,
    Ready,
    NoSlots,
    Failed,
    Exited,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManagedProcess {
    pub pid: u32,
    pub runtime_id: String,
    pub port: u16,
    pub started_at: DateTime<Utc>,
    pub command_hash: String,
    pub executable: String,
    pub profile_id: String,
    pub model_id: String,
    pub instance_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeInfo {
    pub runtime_id: String,
    pub profile_id: String,
    pub model_id: String,
    pub port: u16,
    pub pid: Option<u32>,
    pub status: HealthState,
    pub started_at: Option<DateTime<Utc>>,
    pub executable: String,
    pub exit_code: Option<i32>,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeStateFile {
    pub instance_id: String,
    pub runtimes: Vec<ManagedProcess>,
    pub updated_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum LogStream { Stdout, Stderr }
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum LogLevel { Info, Warn, Error, Debug, Unknown }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogLine {
    pub ts: DateTime<Utc>,
    pub runtime_id: String,
    pub stream: LogStream,
    pub level: LogLevel,
    pub text: String,
    pub timestamp: DateTime<Utc>,
    // legacy alias for compat
    pub source: String,
    pub line: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HealthPolicy {
    pub poll_interval_ms: u64,
    pub startup_timeout_ms: u64,
    pub request_timeout_ms: u64,
    pub consecutive_failures: u32,
    pub shutdown_grace_ms: u64,
}
impl Default for HealthPolicy {
    fn default() -> Self {
        Self { poll_interval_ms: 500, startup_timeout_ms: 120_000, request_timeout_ms: 1500, consecutive_failures: 3, shutdown_grace_ms: 5000 }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum RestartPolicy { Never, OnCrash }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExitInfo {
    pub runtime_id: String,
    pub pid: u32,
    pub exit_code: Option<i32>,
    pub signal: Option<i32>,
    pub at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeEvent {
    pub runtime_id: String,
    pub timestamp: DateTime<Utc>,
    pub state: Option<HealthState>,
    pub health: Option<HealthState>,
    pub log: Option<LogLine>,
    pub error: Option<String>,
}
