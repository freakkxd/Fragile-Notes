use super::types::{EmbeddingLimits, EmbeddingRequest, EmbeddingResponse, EmbeddingError};
use sha2::{Sha256, Digest};

pub fn sha256_hex(s: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(s.as_bytes());
    format!("{:x}", hasher.finalize())
}

pub fn validate_embedding_request(request: &EmbeddingRequest, limits: &EmbeddingLimits) -> Result<(), EmbeddingError> {
    request.validate(limits)
}

pub fn validate_embedding_response(response: &EmbeddingResponse, request: &EmbeddingRequest, limits: &EmbeddingLimits) -> Result<(), EmbeddingError> {
    response.validate_for(request, limits)
}

pub fn content_hash_for(content: &str) -> String {
    // CRLF -> LF normalized, as in chunker and NoteChunk::validate
    sha256_hex(&content.replace("\r\n", "\n"))
}
