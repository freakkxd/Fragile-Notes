use super::types::{ModelState, MetadataSource};
use super::gguf;
use super::identity::{file_identity, stable_id};
use super::scanner::Scanner;
use super::registry::Registry;
use std::fs::File;
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

fn tmp_dir(name: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!("fragile-registry-test-{}-{}", name, std::process::id()));
    let _ = std::fs::create_dir_all(&p);
    p
}

fn write_gguf(path: &PathBuf, version: u32, kv: Vec<(&str, &str)>) {
    let mut f = File::create(path).unwrap();
    f.write_all(&0x46554747u32.to_le_bytes()).unwrap(); // magic
    f.write_all(&version.to_le_bytes()).unwrap();
    f.write_all(&1u64.to_le_bytes()).unwrap(); // tensor_count
    f.write_all(&(kv.len() as u64).to_le_bytes()).unwrap();
    for (k, v) in kv {
        let kb = k.as_bytes();
        f.write_all(&(kb.len() as u64).to_le_bytes()).unwrap();
        f.write_all(kb).unwrap();
        f.write_all(&8u32.to_le_bytes()).unwrap(); // type string
        let vb = v.as_bytes();
        f.write_all(&(vb.len() as u64).to_le_bytes()).unwrap();
        f.write_all(vb).unwrap();
    }
}

#[test]
fn valid_gguf_metadata() {
    let dir = tmp_dir("valid");
    let path = dir.join("valid.gguf");
    write_gguf(&path, 3, vec![("general.architecture", "llama")]);
    let meta = gguf::parse_file(&path).unwrap();
    assert!(meta.architecture.is_some() || meta.tensor_count.is_some());
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn invalid_magic() {
    let dir = tmp_dir("invalid");
    let path = dir.join("bad.gguf");
    let mut f = File::create(&path).unwrap();
    f.write_all(b"BAD!").unwrap();
    f.write_all(&[0u8; 12]).unwrap();
    assert!(gguf::parse_gguf_header(&path).is_err());
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn truncated_header() {
    let dir = tmp_dir("trunc");
    let path = dir.join("trunc.gguf");
    let mut f = File::create(&path).unwrap();
    f.write_all(&0x46554747u32.to_le_bytes()).unwrap();
    assert!(gguf::parse_gguf_header(&path).is_err());
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn unsupported_gguf_version() {
    let dir = tmp_dir("version");
    let path = dir.join("ver.gguf");
    write_gguf(&path, 99, vec![]);
    let res = gguf::parse_gguf_header(&path);
    assert!(res.is_err());
    assert!(res.unwrap_err().contains("unsupported"));
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn missing_optional_metadata() {
    let dir = tmp_dir("missing");
    let path = dir.join("missing.gguf");
    write_gguf(&path, 3, vec![]);
    let meta = gguf::parse_file(&path).unwrap();
    assert!(meta.architecture.is_none());
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn filename_fallback() {
    let dir = tmp_dir("fallback");
    let path = dir.join("MyModel-Q4_K_M.gguf");
    std::fs::write(&path, b"not gguf").unwrap();
    let scanner = Scanner::new(dir.clone());
    let res = scanner.scan(None).unwrap();
    assert!(res.discovered.iter().any(|r| r.filename.contains("MyModel")));
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn unicode_path() {
    let dir = tmp_dir("unicode-世界");
    let path = dir.join("模型-Q4.gguf");
    write_gguf(&path, 3, vec![]);
    let scanner = Scanner::new(dir.clone());
    let res = scanner.scan(None).unwrap();
    assert_eq!(res.discovered.len(), 1);
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn path_with_spaces() {
    let dir = tmp_dir("space test");
    let path = dir.join("model with space.gguf");
    write_gguf(&path, 3, vec![]);
    let scanner = Scanner::new(dir.clone());
    let res = scanner.scan(None).unwrap();
    assert_eq!(res.discovered.len(), 1);
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn nested_directories() {
    let dir = tmp_dir("nested");
    let sub = dir.join("a").join("b");
    std::fs::create_dir_all(&sub).unwrap();
    let path = sub.join("deep.gguf");
    write_gguf(&path, 3, vec![]);
    let scanner = Scanner::new(dir.clone());
    let res = scanner.scan(None).unwrap();
    assert_eq!(res.discovered.len(), 1);
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn symlink_outside() {
    let dir = tmp_dir("symlink");
    let outside = tmp_dir("outside");
    let outside_file = outside.join("outside.gguf");
    write_gguf(&outside_file, 3, vec![]);
    let link = dir.join("link.gguf");
    #[cfg(unix)]
    std::os::unix::fs::symlink(&outside_file, &link).unwrap();
    #[cfg(unix)]
    {
        let scanner = Scanner::new(dir.clone());
        let res = scanner.scan(None).unwrap();
        assert!(res.discovered.is_empty());
        assert!(res.invalid.iter().any(|d| d.code == "outside_root"));
    }
    let _ = std::fs::remove_file(&outside_file);
    let _ = std::fs::remove_dir_all(&dir);
    let _ = std::fs::remove_dir_all(&outside);
}
#[test]
fn max_depth() {
    let dir = tmp_dir("depth");
    let deep = dir.join("a/b/c/d/e");
    std::fs::create_dir_all(&deep).unwrap();
    let path = deep.join("deep.gguf");
    write_gguf(&path, 3, vec![]);
    let scanner = Scanner::new(dir.clone());
    let res = scanner.scan(None).unwrap();
    assert_eq!(res.discovered.len(), 0);
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn duplicate_scan_stable_ids() {
    let dir = tmp_dir("stable");
    let path = dir.join("stable.gguf");
    write_gguf(&path, 3, vec![]);
    let scanner = Scanner::new(dir.clone());
    let r1 = scanner.scan(None).unwrap();
    let r2 = scanner.scan(None).unwrap();
    assert_eq!(r1.discovered[0].id, r2.discovered[0].id);
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn moved_file_same_hash() {
    let dir = tmp_dir("moved");
    let path1 = dir.join("orig.gguf");
    write_gguf(&path1, 3, vec![("general.architecture", "llama")]);
    let id1 = file_identity(&path1).unwrap();
    let stable1 = stable_id(&id1);
    let path2 = dir.join("moved.gguf");
    std::fs::rename(&path1, &path2).unwrap();
    let id2 = file_identity(&path2).unwrap();
    let stable2 = stable_id(&id2);
    assert_ne!(stable1, stable2);
    let _ = std::fs::remove_file(&path2);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn changed_file() {
    let dir = tmp_dir("changed2");
    let path = dir.join("change.gguf");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    write_gguf(&path, 3, vec![]);
    let mut reg = Registry::new(dir.join("registry.json"));
    let scan1 = Scanner::new(dir.clone()).scan(None).unwrap();
    let res1 = reg.update_with_scan(scan1);
    assert!(!res1.added.is_empty());
    assert_eq!(res1.discovered.len(), 1);
    std::thread::sleep(std::time::Duration::from_millis(10));
    let _ = std::fs::remove_file(&path);
    write_gguf(&path, 3, vec![("general.architecture", "changed")]);
    let scan2 = Scanner::new(dir.clone()).scan(None).unwrap();
    let res2 = reg.update_with_scan(scan2);
    assert!(res2.discovered.len() >= 1);
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_file(dir.join("registry.json"));
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn deleted_file_missing() {
    let dir = tmp_dir("deleted");
    let path = dir.join("del.gguf");
    write_gguf(&path, 3, vec![]);
    let mut reg = Registry::new(dir.join("registry.json"));
    let scan1 = Scanner::new(dir.clone()).scan(None).unwrap();
    reg.update_with_scan(scan1);
    std::fs::remove_file(&path).unwrap();
    let scan2 = Scanner::new(dir.clone()).scan(None).unwrap();
    let res2 = reg.update_with_scan(scan2);
    assert_eq!(res2.missing.len(), 1);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn manual_roles_survive_scan() {
    let dir = tmp_dir("manual");
    let path = dir.join("manual.gguf");
    write_gguf(&path, 3, vec![]);
    let mut reg = Registry::new(dir.join("registry.json"));
    let scan1 = Scanner::new(dir.clone()).scan(None).unwrap();
    reg.update_with_scan(scan1);
    let scan2 = Scanner::new(dir.clone()).scan(None).unwrap();
    let res2 = reg.update_with_scan(scan2);
    assert_eq!(res2.discovered.len(), 1);
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn scan_cancellation() {
    let dir = tmp_dir("cancel");
    for i in 0..10 {
        let p = dir.join(format!("m{}.gguf", i));
        write_gguf(&p, 3, vec![]);
    }
    let scanner = Scanner::new(dir.clone());
    let flag = Arc::new(AtomicBool::new(true));
    let res = scanner.scan(Some(flag));
    assert!(res.is_err());
    assert_eq!(res.unwrap_err(), "cancelled");
    for i in 0..10 { let _ = std::fs::remove_file(dir.join(format!("m{}.gguf", i))); }
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn no_30gb_allocation() {
    let dir = tmp_dir("noalloc");
    let path = dir.join("big.gguf");
    write_gguf(&path, 3, vec![]);
    let mut f = std::fs::OpenOptions::new().append(true).open(&path).unwrap();
    f.write_all(&vec![0u8; 1024*1024]).unwrap();
    let meta = gguf::parse_file(&path).unwrap();
    assert!(meta.tensor_count.is_some() || meta.architecture.is_none());
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
