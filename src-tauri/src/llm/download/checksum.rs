use std::path::Path;
use sha2::{Sha256, Digest};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};

pub fn sha256_of_file(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 8192];
    loop {
        let n = file.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 { break; }
        hasher.update(&buf[..n]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

pub fn sha256_of_part_with_existing(part_path: &Path, existing_len: u64) -> Result<String, String> {
    // For resume: hash existing .part before appending, then continue
    // For simplicity, recompute whole file after download (for 30GB, this is expensive but correct)
    // For P1, we do full recompute only at verify stage, not per chunk
    sha256_of_file(part_path)
}

pub fn verify_checksum(path: &Path, expected: &str) -> Result<(), String> {
    let actual = sha256_of_file(path)?;
    if actual.to_lowercase() != expected.to_lowercase() {
        return Err(format!("checksum_mismatch: expected {}, got {}", expected, actual));
    }
    Ok(())
}
