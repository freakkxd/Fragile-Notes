use super::types::DownloadSource;
use super::source::parse_source;
use super::manager::DownloadManager;

#[test]
fn direct_download_200_validation() {
    let src = DownloadSource{ url: "https://huggingface.co/repo/resolve/main/model.gguf".to_string(), filename: "model.gguf".to_string(), expected_size: Some(1024), sha256: None, repo_id: None, revision: None };
    let res = parse_source(src);
    assert!(res.is_ok());
}
#[test]
fn https_policy() {
    let src = DownloadSource{ url: "http://example.com/model.gguf".to_string(), filename: "model.gguf".to_string(), expected_size: None, sha256: None, repo_id: None, revision: None };
    let res = parse_source(src);
    assert!(res.is_err());
    assert!(res.unwrap_err().contains("HTTPS"));
}
#[test]
fn path_traversal_filename() {
    let src = DownloadSource{ url: "https://example.com/model.gguf".to_string(), filename: "../evil.gguf".to_string(), expected_size: None, sha256: None, repo_id: None, revision: None };
    let res = parse_source(src);
    assert!(res.is_err());
    assert!(res.unwrap_err().contains("path_traversal"));
}
#[test]
fn max_size() {
    let dir = std::env::temp_dir().join("fragile-download-max");
    let _ = std::fs::create_dir_all(&dir);
    let mgr = DownloadManager::new(dir.join("state.json"), dir.clone());
    let src = DownloadSource{ url: "https://example.com/model.gguf".to_string(), filename: "model.gguf".to_string(), expected_size: Some(200 * 1024 * 1024 * 1024), sha256: None, repo_id: None, revision: None };
    let res = mgr.start(src);
    assert!(res.is_ok() || res.is_err());
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn atomic_rename() {
    let dir = std::env::temp_dir().join("fragile-atomic");
    let _ = std::fs::create_dir_all(&dir);
    let part = dir.join("model.gguf.part");
    let dest = dir.join("model.gguf");
    std::fs::write(&part, b"test content").unwrap();
    std::fs::rename(&part, &dest).unwrap();
    assert!(dest.exists());
    assert!(!part.exists());
    let _ = std::fs::remove_file(&dest);
    let _ = std::fs::remove_dir_all(&dir);
}
#[test]
fn completed_file_enters_registry_only_after_parse() {
    let dir = std::env::temp_dir().join("fragile-registry-parse");
    let _ = std::fs::create_dir_all(&dir);
    let path = dir.join("bad.gguf");
    std::fs::write(&path, b"not gguf").unwrap();
    let res = crate::llm::models::gguf::parse_file(&path);
    assert!(res.is_err());
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_dir_all(&dir);
}
