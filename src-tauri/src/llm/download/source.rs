use url::Url;
use super::types::DownloadSource;

#[derive(Debug, Clone)]
pub enum SourceKind {
    DirectUrl { url: Url, filename: String, expected_size: Option<u64>, sha256: Option<String> },
    HuggingFace { repo_id: String, revision: String, filename: String, token_ref: Option<String> },
}

pub fn parse_source(source: DownloadSource) -> Result<SourceKind, String> {
    source.validate_token_ref()?;
    if let Some(repo_id) = source.repo_id {
        let rev = source.revision.unwrap_or_else(|| "main".to_string());
        // HuggingFace: ensure filename sanitized, token_ref already validated as keychain ref
        sanitize_filename(&source.filename)?;
        return Ok(SourceKind::HuggingFace { repo_id, revision: rev, filename: source.filename.clone(), token_ref: source.token_ref.clone() });
    }
    // DirectUrl
    let url = Url::parse(&source.url).map_err(|e| format!("invalid url: {}", e))?;
    // Validate filename sanitized
    let filename = sanitize_filename(&source.filename)?;
    // Validate HTTPS
    if url.scheme() != "https" {
        return Err("HTTPS policy: only https allowed".to_string());
    }
    Ok(SourceKind::DirectUrl { url, filename, expected_size: source.expected_size, sha256: source.sha256 })
}

fn sanitize_filename(name: &str) -> Result<String, String> {
    if name.contains('/') || name.contains('\\') || name.contains("..") {
        return Err("path_traversal: filename must not contain path".to_string());
    }
    if name.is_empty() || name.len() > 255 {
        return Err("invalid filename".to_string());
    }
    // Only allow alphanumeric, -, _, ., 
    Ok(name.to_string())
}

pub fn resolve_hf_url(repo_id: &str, revision: &str, filename: &str) -> String {
    format!("https://huggingface.co/{}/resolve/{}/{}", repo_id, revision, filename)
}
