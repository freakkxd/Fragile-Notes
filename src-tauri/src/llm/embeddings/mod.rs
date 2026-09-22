pub mod types;
pub mod validation;
pub mod chunker;
pub mod store;
pub mod provider;
pub mod openai_compatible;
#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_persistence;
#[cfg(test)]
mod tests_provider;

pub use types::{
    ChunkDiff, EmbeddingError, EmbeddingLimits, EmbeddingModelRef, EmbeddingRecord, EmbeddingRequest, EmbeddingResponse, IndexRun, IndexStatus, NoteChunk,
};
pub use validation::{validate_embedding_request, validate_embedding_response, content_hash_for, sha256_hex};
pub use chunker::{ChunkingConfig, ChunkingError, ChunkedNote, chunk_markdown, normalize_for_hash};
pub use store::{ChunkStore, EmbeddingStore, SqliteStore};
pub use provider::{EmbeddingProvider, redact_secrets};
pub use openai_compatible::{OpenAiCompatibleConfig, OpenAiCompatibleEmbeddingProvider};
