pub mod types;
pub mod validation;
pub mod chunker;
pub mod store;
#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_persistence;

pub use types::{
    ChunkDiff, EmbeddingError, EmbeddingLimits, EmbeddingModelRef, EmbeddingRecord, EmbeddingRequest, EmbeddingResponse, IndexRun, IndexStatus, NoteChunk,
};
pub use validation::{validate_embedding_request, validate_embedding_response, content_hash_for, sha256_hex};
pub use chunker::{ChunkingConfig, ChunkingError, ChunkedNote, chunk_markdown, normalize_for_hash};
pub use store::{ChunkStore, EmbeddingStore, SqliteStore};

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
