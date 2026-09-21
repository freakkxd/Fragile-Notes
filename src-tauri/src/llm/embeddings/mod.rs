pub mod types;
pub mod validation;
#[cfg(test)]
mod tests;

pub use types::{
    EmbeddingError, EmbeddingLimits, EmbeddingRequest, EmbeddingResponse, NoteChunk,
};
pub use validation::{validate_embedding_request, validate_embedding_response, sha256_hex, content_hash_for};

use async_trait::async_trait;

// TODO: cancellation for batch embeddings — choose one:
// 1) async fn embed(&self, request: EmbeddingRequest, cancel: tokio_util::sync::CancellationToken) -> ...
// 2) request contains CancellationToken
// Preferred: request is data, cancellation is control, so option 1.
// For stage 1, cancellation is not required in trait; will be added in stage 4 provider.

#[async_trait]
pub trait EmbeddingProvider: Send + Sync {
    async fn embed(&self, request: EmbeddingRequest) -> Result<EmbeddingResponse, EmbeddingError>;
}
