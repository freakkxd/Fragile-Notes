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

fn sanitize_runtime_id(id: &str) -> String {
    // prevent path traversal: only allow alphanumeric, -, _, .
    id.chars().map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' }).collect::<String>().chars().take(64).collect()
}

impl LogSink {
    pub fn new(runtime_id: &str, base_dir: &Path) -> Self {
        let safe_id = sanitize_runtime_id(runtime_id);
        let dir = base_dir.join("runtime-logs");
        let _ = std::fs::create_dir_all(&dir);
        // ensure runtime_id does not contain path separators
        let file_path = dir.join(format!("{}.log", safe_id));
        // verify file_path is still under dir (prevent traversal)
        let file_path = if file_path.starts_with(&dir) { file_path } else { dir.join("invalid.log") };
        Self { runtime_id: safe_id, ring: Arc::new(Mutex::new(VecDeque::with_capacity(RING_CAP))), file_path }
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
        if n == 0 { return vec![]; }
        let n = n.min(RING_CAP);
        if let Ok(ring) = self.ring.lock() {
            let len = ring.len();
            let start = len.saturating_sub(n);
            ring.iter().skip(start).cloned().collect()
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

pub(crate) fn mask_secrets(s: &str) -> String {
    const MASK: &str = "••••••••";
    const MASK_LEN: usize = 24; // "••••••••".len() 8*3
    let mut out = s.to_string();
    // Cover all secret patterns: Bearer, x-api-key, api_key, sk-, sk-ant-, AIza, key=
    for pat in ["Bearer ", "x-api-key: ", "x-api-key=", "key=", "api_key=", "api_key: ", "Authorization:"] {
        let mut search_from = 0;
        while let Some(start) = out[search_from..].find(pat).map(|i| search_from + i) {
            let after = start + pat.len();
            let end = out[after..].find(|c: char| c.is_whitespace() || c == '"' || c == '\'' || c == ',' || c == '}').map(|i| after + i).unwrap_or(out.len());
            let len = end - after;
            if len > 8 {
                out.replace_range(after..end, MASK);
                search_from = after + MASK_LEN;
            } else {
                search_from = end;
            }
            if search_from >= out.len() { break; }
        }
    }
    // Direct token patterns without prefix: sk-..., sk-ant-..., AIza...
    for prefix in ["sk-", "sk-ant-", "AIza"] {
        let mut search_from = 0;
        while let Some(start) = out[search_from..].find(prefix).map(|i| search_from + i) {
            let end = out[start..].find(|c: char| c.is_whitespace() || c == '"' || c == '\'' || c == ',' || c == '}').map(|i| start + i).unwrap_or(out.len());
            let token = &out[start..end];
            if token.len() > 12 {
                out.replace_range(start..end, MASK);
                search_from = start + MASK_LEN;
            } else {
                search_from = end;
            }
            if search_from >= out.len() { break; }
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
