use std::path::{Path, PathBuf};
use std::fs;
use sha2::{Sha256, Digest};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelIdentity {
    pub canonical_path: String,
    pub file_size: u64,
    pub modified_at: i64,
    pub sha256: Option<String>,
}

use serde::{Deserialize, Serialize};

pub fn canonical_path(path: &Path) -> Result<String, String> {
    let canon = path.canonicalize().map_err(|e| e.to_string())?;
    Ok(canon.to_string_lossy().to_string())
}

pub fn file_identity(path: &Path) -> Result<ModelIdentity, String> {
    let meta = fs::metadata(path).map_err(|e| e.to_string())?;
    let size = meta.len();
    let mtime = meta.modified().map_err(|e| e.to_string())?
        .duration_since(std::time::UNIX_EPOCH).map_err(|e| e.to_string())?.as_secs() as i64;
    let canon = canonical_path(path).unwrap_or_else(|_| path.to_string_lossy().to_string());
    Ok(ModelIdentity { canonical_path: canon, file_size: size, modified_at: mtime, sha256: None })
}

pub fn stable_id(identity: &ModelIdentity) -> String {
    // stable id without full hash: canonical_path + size + mtime
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    let mut hasher = DefaultHasher::new();
    identity.canonical_path.hash(&mut hasher);
    identity.file_size.hash(&mut hasher);
    identity.modified_at.hash(&mut hasher);
    format!("model-{:x}", hasher.finish())
}

pub fn verified_id(sha256: &str) -> String {
    format!("model-sha256-{}", &sha256[..16.min(sha256.len())])
}

pub fn compute_sha256(path: &Path) -> Result<String, String> {
    let mut file = fs::File::open(path).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 8192];
    loop {
        use std::io::Read;
        let n = file.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 { break; }
        hasher.update(&buf[..n]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

pub fn is_same_file(a: &ModelIdentity, b: &ModelIdentity) -> bool {
    // if sha256 present and equal, same file even if moved
    if let (Some(sha_a), Some(sha_b)) = (&a.sha256, &b.sha256) {
        if sha_a == sha_b { return true; }
    }
    // otherwise compare stable id
    stable_id(a) == stable_id(b)
}
