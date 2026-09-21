use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::ChildStdout;
use std::process::ChildStderr;
use std::sync::{Arc, Mutex};
use chrono::Utc;
use super::types::{LogLine, LogStream, LogLevel};

const RING_CAP: usize = 500;
const MAX_FILE_BYTES: u64 = 2 * 1024 * 1024; // 2MB

pub struct LogSink {
    runtime_id: String,
    ring: Arc<Mutex<VecDeque<LogLine>>>,
    file_path: PathBuf,
}

impl LogSink {
    pub fn new(runtime_id: &str, base_dir: &Path) -> Self {
        let dir = base_dir.join("runtime-logs");
        let _ = std::fs::create_dir_all(&dir);
        let file_path = dir.join(format!("{}.log", runtime_id));
        Self { runtime_id: runtime_id.to_string(), ring: Arc::new(Mutex::new(VecDeque::with_capacity(RING_CAP))), file_path }
    }

    pub fn push(&self, stream: LogStream, text: String) {
        let masked = mask_secrets(&text);
        let level = infer_level(&masked, &stream);
        let now = Utc::now();
        let line = LogLine {
            ts: now,
            timestamp: now,
            runtime_id: self.runtime_id.clone(),
            stream: stream.clone(),
            level,
            text: masked.clone(),
            source: match stream { LogStream::Stdout => "stdout".to_string(), LogStream::Stderr => "stderr".to_string() },
            line: masked.clone(),
        };
        // ring
        if let Ok(mut ring) = self.ring.lock() {
            if ring.len() >= RING_CAP { ring.pop_front(); }
            ring.push_back(line.clone());
        }
        // file append with rotation
        self.append_to_file(&line);
    }

    fn append_to_file(&self, line: &LogLine) {
        // rotation check
        if let Ok(meta) = std::fs::metadata(&self.file_path) {
            if meta.len() > MAX_FILE_BYTES {
                let rotated = self.file_path.with_extension("log.1");
                let _ = std::fs::rename(&self.file_path, rotated);
            }
        }
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&self.file_path) {
            let _ = writeln!(f, "[{}] {:?} {:?} {}", line.timestamp.to_rfc3339(), line.stream, line.level, line.text);
        }
    }

    pub fn tail(&self, n: usize) -> Vec<LogLine> {
        if let Ok(ring) = self.ring.lock() {
            ring.iter().rev().take(n).cloned().collect::<Vec<_>>().into_iter().rev().collect()
        } else { vec![] }
    }

    pub fn drain_stdout(&self, stdout: ChildStdout) {
        let sink = self.clone_for_thread();
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines().map_while(Result::ok) {
                sink.push(LogStream::Stdout, line);
            }
        });
    }

    pub fn drain_stderr(&self, stderr: ChildStderr) {
        let sink = self.clone_for_thread();
        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().map_while(Result::ok) {
                sink.push(LogStream::Stderr, line);
            }
        });
    }

    fn clone_for_thread(&self) -> Self {
        Self { runtime_id: self.runtime_id.clone(), ring: Arc::clone(&self.ring), file_path: self.file_path.clone() }
    }
}

fn mask_secrets(s: &str) -> String {
    let mut out = s.to_string();
    for pat in ["Bearer ", "x-api-key: ", "key=", "Authorization:"] {
        if let Some(start) = out.find(pat) {
            let after = start + pat.len();
            let end = out[after..].find(|c: char| c.is_whitespace() || c == '"' || c == '\'').map(|i| after + i).unwrap_or(out.len());
            let len = end - after;
            if len > 8 {
                out.replace_range(after..end, "••••••••");
            }
        }
    }
    out
}

fn infer_level(text: &str, stream: &LogStream) -> LogLevel {
    let lower = text.to_lowercase();
    if lower.contains("error") || lower.contains("failed") || lower.contains("panic") { LogLevel::Error }
    else if lower.contains("warn") { LogLevel::Warn }
    else if lower.contains("debug") { LogLevel::Debug }
    else if *stream == LogStream::Stderr { LogLevel::Warn }
    else { LogLevel::Info }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    #[test]
    fn test_mask() {
        let s = "Authorization: Bearer sk-1234567890abcdef and key=AIza123";
        let m = mask_secrets(s);
        assert!(!m.contains("sk-1234567890"));
        assert!(m.contains("••••••••"));
    }
    #[test]
    fn test_ring_cap() {
        let sink = LogSink::new("test-ring", &PathBuf::from("/tmp"));
        for i in 0..600 { sink.push(LogStream::Stdout, format!("line {}", i)); }
        assert_eq!(sink.tail(500).len(), 500);
        assert_eq!(sink.tail(1)[0].text, "line 599");
    }
}
