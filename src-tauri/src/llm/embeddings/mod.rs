pub mod types;
pub mod validation;
pub mod chunker;
pub mod store;
pub mod provider;
pub mod openai_compatible;
pub mod wiring;
#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_persistence;
#[cfg(test)]
mod tests_provider;
#[cfg(test)]
mod tests_wiring;

pub use types::{
    ChunkDiff, EmbeddingError, EmbeddingLimits, EmbeddingModelRef, EmbeddingRecord, EmbeddingRequest, EmbeddingResponse, IndexRun, IndexStatus, NoteChunk,
};
pub use validation::{validate_embedding_request, validate_embedding_response, content_hash_for, sha256_hex};
pub use chunker::{ChunkingConfig, ChunkingError, ChunkedNote, chunk_markdown, normalize_for_hash};
pub use store::{ChunkStore, EmbeddingStore, SqliteStore};
pub use provider::{EmbeddingProvider, redact_secrets};
pub use openai_compatible::{OpenAiCompatibleConfig, OpenAiCompatibleEmbeddingProvider};
pub use wiring::{resolve_embedding_provider, resolve_model_fingerprint, resolve_scope_for_model, validate_task_policy, EmbeddingWiringError, ResolvedEmbedding};
