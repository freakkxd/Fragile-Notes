use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use walkdir::WalkDir;
use super::types::{ModelRecord, ModelFormat, ModelState, ModelSource, ScanResult, ModelDiagnostic};
use super::identity::{file_identity, stable_id};
use super::gguf;

pub struct Scanner {
    models_dir: PathBuf,
    max_depth: usize,
}

impl Scanner {
    pub fn new(models_dir: PathBuf) -> Self { Self { models_dir: models_dir.canonicalize().unwrap_or(models_dir), max_depth: 4 } }

    pub fn scan(&self, cancel: Option<Arc<AtomicBool>>) -> Result<ScanResult, String> {
        if !self.models_dir.exists() {
            return Ok(ScanResult{ discovered: vec![], added: vec![], updated: vec![], missing: vec![], invalid: vec![] });
        }
        let mut discovered = Vec::new();
        let mut invalid = Vec::new();
        for entry in WalkDir::new(&self.models_dir).max_depth(self.max_depth).follow_links(false).into_iter().filter_map(|e| e.ok()) {
            if let Some(flag) = &cancel { if flag.load(Ordering::Relaxed) { return Err("cancelled".to_string()); } }
            let p = entry.path();
            // Ensure path is inside models_dir (no symlink escape)
            if let Ok(canon) = p.canonicalize() {
                if !canon.starts_with(&self.models_dir) {
                    invalid.push(ModelDiagnostic{ code: "outside_root".to_string(), message: format!("symlink outside: {}", p.display()), level: "warn".to_string() });
                    continue;
                }
            }
            // Only scan files, not vault
            if p.is_file() {
                if let Some(ext) = p.extension().and_then(|s| s.to_str()) {
                    if ext.eq_ignore_ascii_case("gguf") {
                        match self.scan_single(p) {
                            Ok(rec) => discovered.push(rec),
                            Err(e) => invalid.push(ModelDiagnostic{ code: "invalid_gguf".to_string(), message: format!("{}: {}", p.display(), e), level: "warn".to_string() }),
                        }
                    }
                }
            }
        }
        Ok(ScanResult{ discovered, added: vec![], updated: vec![], missing: vec![], invalid })
    }

    fn scan_single(&self, path: &Path) -> Result<ModelRecord, String> {
        let identity = file_identity(path)?;
        let id = stable_id(&identity);
        let filename = path.file_name().and_then(|n| n.to_str()).unwrap_or("model.gguf").to_string();
        let format = ModelFormat::Gguf;
        // Try GGUF header, fallback to filename
        let (metadata, source) = match gguf::parse_file(path) {
            Ok(meta) => (meta, super::types::MetadataSource::Gguf),
            Err(_) => {
                // fallback: guess from filename (quant via regex)
                let mut meta = super::types::ModelMetadata::default();
                let quant = {
                    let re = regex::Regex::new(r"(?i)(Q[0-9]_[A-Za-z0-9_]+|f16|f32|bf16|q8_0)").unwrap();
                    re.find(&filename).map(|m| m.as_str().to_uppercase()).unwrap_or_else(|| "unknown".to_string())
                };
                meta.quantization = Some(quant);
                meta.metadata_source = super::types::MetadataSource::Filename;
                (meta, super::types::MetadataSource::Filename)
            }
        };
        let now = chrono::Utc::now();
        Ok(ModelRecord{
            id,
            provider_id: "local".to_string(),
            path: path.to_path_buf(),
            filename,
            format,
            size_bytes: identity.file_size,
            sha256: identity.sha256,
            metadata,
            capabilities: vec![crate::llm::Capability::Chat],
            roles: vec![super::types::ModelRole::General],
            source: ModelSource::LocalFile,
            state: ModelState::Present,
            first_seen_at: now,
            last_seen_at: now,
            diagnostics: vec![],
        })
    }
}
