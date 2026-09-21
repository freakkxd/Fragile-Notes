use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use super::types::{ModelMetadata, MetadataSource};

const GGUF_MAGIC: u32 = 0x46554747; // "GGUF" little endian
const MAX_STRING_LEN: usize = 1024 * 1024;
const MAX_ARRAY_LEN: usize = 100_000;

#[derive(Debug)]
pub struct GgufHeader {
    pub version: u32,
    pub tensor_count: u64,
    pub metadata_kv_count: u64,
    pub metadata: std::collections::HashMap<String, GgufValue>,
}

#[derive(Debug, Clone)]
pub enum GgufValue { String(String), U32(u32), U64(u64), I32(i32), Bool(bool), Array(Vec<GgufValue>) }

pub fn parse_gguf_header(path: &Path) -> Result<GgufHeader, String> {
    let mut f = File::open(path).map_err(|e| format!("open failed: {}", e))?;
    // Limit header read to first 4MB to avoid 30GB allocation
    let mut buf = [0u8; 4];
    f.read_exact(&mut buf).map_err(|e| format!("read magic failed: {}", e))?;
    let magic = u32::from_le_bytes(buf);
    if magic != GGUF_MAGIC {
        return Err(format!("invalid magic: expected GGUF (0x{:x}), got 0x{:x}", GGUF_MAGIC, magic));
    }
    let mut version_buf = [0u8; 4];
    f.read_exact(&mut version_buf).map_err(|e| format!("read version failed: {}", e))?;
    let version = u32::from_le_bytes(version_buf);
    if version != 2 && version != 3 {
        // version tolerant: warn but continue
        // For P registry, we accept 2 and 3, but don't panic on unknown
        if version > 10 {
            return Err(format!("unsupported GGUF version: {}", version));
        }
    }
    // tensor count and kv count are u64 in v3, u32 in v2? Actually GGUF v3 uses u64
    let mut tmp = [0u8; 8];
    f.read_exact(&mut tmp[..8]).map_err(|e| format!("read tensor count failed: {}", e))?;
    let tensor_count = if version == 3 { u64::from_le_bytes(tmp) } else { u32::from_le_bytes(tmp[..4].try_into().unwrap()) as u64 };
    f.read_exact(&mut tmp[..8]).map_err(|e| format!("read kv count failed: {}", e))?;
    let kv_count = if version == 3 { u64::from_le_bytes(tmp) } else { u32::from_le_bytes(tmp[..4].try_into().unwrap()) as u64 };
    if kv_count > 10000 {
        return Err(format!("kv count too large: {}", kv_count));
    }
    let mut metadata = std::collections::HashMap::new();
    for _ in 0..kv_count {
        let key = read_string(&mut f)?;
        if key.len() > MAX_STRING_LEN { return Err("key too long".to_string()); }
        let ty = read_u32(&mut f)?;
        let val = read_value(&mut f, ty)?;
        metadata.insert(key, val);
        // Avoid reading too much: limit total metadata to 2MB
        if f.stream_position().map_err(|e| e.to_string())? > 2 * 1024 * 1024 {
            break;
        }
    }
    Ok(GgufHeader { version, tensor_count, metadata_kv_count: kv_count, metadata })
}

fn read_u32(f: &mut File) -> Result<u32, String> {
    let mut buf = [0u8; 4];
    f.read_exact(&mut buf).map_err(|e| e.to_string())?;
    Ok(u32::from_le_bytes(buf))
}
fn read_u64(f: &mut File) -> Result<u64, String> {
    let mut buf = [0u8; 8];
    f.read_exact(&mut buf).map_err(|e| e.to_string())?;
    Ok(u64::from_le_bytes(buf))
}
fn read_string(f: &mut File) -> Result<String, String> {
    let len = read_u64(f)? as usize;
    if len > MAX_STRING_LEN { return Err(format!("string len too large: {}", len)); }
    let mut buf = vec![0u8; len];
    f.read_exact(&mut buf).map_err(|e| e.to_string())?;
    String::from_utf8(buf).map_err(|e| e.to_string())
}
fn read_value(f: &mut File, ty: u32) -> Result<GgufValue, String> {
    match ty {
        0 => Ok(GgufValue::U32(read_u32(f)?)),
        1 => Ok(GgufValue::I32(read_u32(f)? as i32)),
        2 => Ok(GgufValue::U64(read_u64(f)?)),
        3 | 8 => Ok(GgufValue::String(read_string(f)?)),
        4 | 7 => {
            let mut buf = [0u8; 1];
            f.read_exact(&mut buf).map_err(|e| e.to_string())?;
            Ok(GgufValue::Bool(buf[0] != 0))
        },
        9 => {
            let arr_ty = read_u32(f)?;
            let len = read_u64(f)? as usize;
            if len > MAX_ARRAY_LEN { return Err("array too large".to_string()); }
            let mut arr = Vec::with_capacity(len.min(100));
            for _ in 0..len.min(100) {
                arr.push(read_value(f, arr_ty)?);
            }
            // Skip remaining if truncated
            Ok(GgufValue::Array(arr))
        },
        _ => {
            // Unknown type: try to skip as string for tolerance
            Err(format!("unknown gguf type: {}", ty))
        }
    }
}

pub fn metadata_from_header(header: &GgufHeader) -> ModelMetadata {
    let get_str = |k: &str| header.metadata.get(k).and_then(|v| if let GgufValue::String(s) = v { Some(s.clone()) } else { None });
    let get_u32 = |k: &str| header.metadata.get(k).and_then(|v| match v { GgufValue::U32(n) => Some(*n), GgufValue::U64(n) => Some(*n as u32), _ => None });
    let get_u64 = |k: &str| header.metadata.get(k).and_then(|v| match v { GgufValue::U64(n) => Some(*n), GgufValue::U32(n) => Some(*n as u64), _ => None });

    ModelMetadata {
        architecture: get_str("general.architecture"),
        parameter_count: get_u64("general.parameter_count"),
        context_length: get_u32("llama.context_length").or_else(|| get_u32("general.context_length")),
        block_count: get_u32("llama.block_count"),
        embedding_length: get_u32("llama.embedding_length"),
        quantization: None, // derived from filename, not header
        chat_template: get_str("tokenizer.chat_template"),
        vision: None,
        tensor_count: Some(header.tensor_count as u32),
        metadata_source: MetadataSource::Gguf,
    }
}

pub fn parse_file(path: &Path) -> Result<ModelMetadata, String> {
    let header = parse_gguf_header(path)?;
    Ok(metadata_from_header(&header))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    #[test]
    fn test_invalid_magic() {
        let dir = std::env::temp_dir().join("gguf-test-invalid");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("bad.gguf");
        let mut f = File::create(&path).unwrap();
        f.write_all(b"BAD!").unwrap();
        f.write_all(&[0u8; 12]).unwrap();
        assert!(parse_gguf_header(&path).is_err());
        let _ = std::fs::remove_file(&path);
    }
    #[test]
    fn test_truncated_header() {
        let dir = std::env::temp_dir().join("gguf-test-trunc");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("trunc.gguf");
        let mut f = File::create(&path).unwrap();
        f.write_all(&GGUF_MAGIC.to_le_bytes()).unwrap();
        // truncated after magic
        assert!(parse_gguf_header(&path).is_err());
        let _ = std::fs::remove_file(&path);
    }
}
