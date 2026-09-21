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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogLine {
    pub ts: DateTime<Utc>,
    pub runtime_id: String,
    pub source: String, // stdout | stderr
    pub line: String,
}
