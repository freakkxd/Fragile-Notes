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
pub enum GgufValue {
    String(String),
    U32(u32),
    U64(u64),
    I32(i32),
    I64(i64),
    F32(f32),
    F64(f64),
    Bool(bool),
    Array(Vec<GgufValue>),
}

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
fn read_u8(f: &mut File) -> Result<u8, String> {
    let mut buf = [0u8; 1];
    f.read_exact(&mut buf).map_err(|e| e.to_string())?;
    Ok(buf[0])
}
fn read_u16(f: &mut File) -> Result<u16, String> {
    let mut buf = [0u8; 2];
    f.read_exact(&mut buf).map_err(|e| e.to_string())?;
    Ok(u16::from_le_bytes(buf))
}
fn read_value(f: &mut File, ty: u32) -> Result<GgufValue, String> {
    // Type ids per ggml/include/gguf.h (verified against local llama.cpp):
    // 0=U8 1=I8 2=U16 3=I16 4=U32 5=I32 6=F32 7=BOOL 8=STRING
    // 9=ARRAY 10=U64 11=I64 12=F64.
    match ty {
        0 => Ok(GgufValue::U32(read_u8(f)? as u32)),
        1 => Ok(GgufValue::I32(read_u8(f)? as i8 as i32)),
        2 => Ok(GgufValue::U32(read_u16(f)? as u32)),
        3 => Ok(GgufValue::I32(i16::from_le_bytes({ let mut b = [0u8; 2]; f.read_exact(&mut b).map_err(|e| e.to_string())?; b }) as i32)),
        4 => Ok(GgufValue::U32(read_u32(f)?)),
        5 => Ok(GgufValue::I32(read_u32(f)? as i32)),
        6 => Ok(GgufValue::F32(f32::from_bits(read_u32(f)?))),
        7 => Ok(GgufValue::Bool(read_u8(f)? != 0)),
        8 => Ok(GgufValue::String(read_string(f)?)),        9 => {
            let arr_ty = read_u32(f)?;
            let len = read_u64(f)? as usize;
            if len > MAX_ARRAY_LEN { return Err("array too large".to_string()); }
            // Read first 100 for metadata, SEEK past the rest.
            // Truncating without seeking misaligns the stream and corrupts
            // every subsequent KV (Stage 1: nomic-bert vocab arrays).
            let mut arr = Vec::with_capacity(len.min(100));
            for i in 0..len {
                if i < 100 {
                    arr.push(read_value(f, arr_ty)?);
                } else {
                    skip_value(f, arr_ty)?;
                }
            }
            Ok(GgufValue::Array(arr))
        },
        10 => Ok(GgufValue::U64(read_u64(f)?)),
        11 => Ok(GgufValue::I64(read_u64(f)? as i64)),
        12 => Ok(GgufValue::F64(f64::from_bits(read_u64(f)?))),
        _ => {
            Err(format!("unknown gguf type: {}", ty))
        }
    }
}

/// Seek past one value of type `ty` without materializing it.
/// Used to skip truncated array tails while keeping stream alignment.
/// Widths per ggml/include/gguf.h.
fn skip_value(f: &mut File, ty: u32) -> Result<(), String> {
    match ty {
        0 | 1 | 7 => {
            f.seek(SeekFrom::Current(1)).map_err(|e| e.to_string())?;
            Ok(())
        }
        2 | 3 => {
            f.seek(SeekFrom::Current(2)).map_err(|e| e.to_string())?;
            Ok(())
        }
        4 | 5 | 6 => {
            f.seek(SeekFrom::Current(4)).map_err(|e| e.to_string())?;
            Ok(())
        }
        10 | 11 | 12 => {
            f.seek(SeekFrom::Current(8)).map_err(|e| e.to_string())?;
            Ok(())
        }
        8 => {
            let len = read_u64(f)? as i64;
            if len < 0 { return Err("negative string len".to_string()); }
            f.seek(SeekFrom::Current(len)).map_err(|e| e.to_string())?;
            Ok(())
        }
        9 => {
            let arr_ty = read_u32(f)?;
            let len = read_u64(f)? as usize;
            if len > MAX_ARRAY_LEN { return Err("array too large".to_string()); }
            for _ in 0..len {
                skip_value(f, arr_ty)?;
            }
            Ok(())
        }
        _ => Err(format!("unknown gguf type: {}", ty)),
    }
}

pub fn metadata_from_header(header: &GgufHeader) -> ModelMetadata {
    let get_str = |k: &str| header.metadata.get(k).and_then(|v| if let GgufValue::String(s) = v { Some(s.clone()) } else { None });
    let get_u32 = |k: &str| header.metadata.get(k).and_then(|v| match v { GgufValue::U32(n) => Some(*n), GgufValue::U64(n) => Some(*n as u32), _ => None });
    let get_u64 = |k: &str| header.metadata.get(k).and_then(|v| match v { GgufValue::U64(n) => Some(*n), GgufValue::U32(n) => Some(*n as u64), _ => None });

    // Architecture-prefixed keys: GGUF stores per-arch values under
    // `{arch}.{name}` (e.g. `nomic-bert.embedding_length`), not only `llama.*`.
    let arch = get_str("general.architecture");
    let arch_get_u32 = |base: &str| {
        let mut keys = vec![format!("llama.{}", base)];
        if let Some(a) = &arch {
            keys.push(format!("{}.{}", a, base));
        }
        keys.push(format!("general.{}", base));
        keys.into_iter().find_map(|k| get_u32(&k))
    };

    ModelMetadata {
        architecture: arch.clone(),
        parameter_count: get_u64("general.parameter_count"),
        context_length: arch_get_u32("context_length"),
        block_count: arch_get_u32("block_count"),
        embedding_length: arch_get_u32("embedding_length"),
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

    fn write_gguf_string(buf: &mut Vec<u8>, s: &str) {
        buf.extend_from_slice(&(s.len() as u64).to_le_bytes());
        buf.extend_from_slice(s.as_bytes());
    }

    /// Stage 1 regression: large arrays (>100 entries, e.g. vocab) must be
    /// seek-skipped, not truncated in place — otherwise the stream misaligns
    /// and later KVs parse as garbage ("string len too large", nomic-bert).
    #[test]
    fn test_large_array_keeps_stream_aligned() {
        let dir = std::env::temp_dir().join("gguf-test-align");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("align.gguf");
        let mut buf = Vec::new();
        buf.extend_from_slice(&GGUF_MAGIC.to_le_bytes());
        buf.extend_from_slice(&3u32.to_le_bytes()); // version 3
        buf.extend_from_slice(&1u64.to_le_bytes()); // tensor count
        buf.extend_from_slice(&3u64.to_le_bytes()); // kv count
        // KV1: general.architecture = "nomic-bert"
        write_gguf_string(&mut buf, "general.architecture");
        buf.extend_from_slice(&8u32.to_le_bytes());
        write_gguf_string(&mut buf, "nomic-bert");
        // KV2: tokenizer.tokens = 150 strings (forces skip path)
        write_gguf_string(&mut buf, "tokenizer.tokens");
        buf.extend_from_slice(&9u32.to_le_bytes());
        buf.extend_from_slice(&8u32.to_le_bytes()); // array of strings
        buf.extend_from_slice(&150u64.to_le_bytes());
        for i in 0..150 {
            write_gguf_string(&mut buf, &format!("tok{}", i));
        }
        // KV3 after the large array: must still parse correctly
        write_gguf_string(&mut buf, "nomic-bert.embedding_length");
        buf.extend_from_slice(&4u32.to_le_bytes()); // UINT32
        buf.extend_from_slice(&768u32.to_le_bytes());
        std::fs::write(&path, &buf).unwrap();

        let header = parse_gguf_header(&path).expect("aligned parse must succeed");
        let meta = metadata_from_header(&header);
        assert_eq!(meta.architecture.as_deref(), Some("nomic-bert"));
        assert_eq!(meta.embedding_length, Some(768));
        // First 100 array entries retained
        match header.metadata.get("tokenizer.tokens") {
            Some(GgufValue::Array(a)) => assert_eq!(a.len(), 100),
            other => panic!("expected array, got {:?}", other.is_some()),
        }
        let _ = std::fs::remove_file(&path);
    }
}
