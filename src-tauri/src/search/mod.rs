pub mod lexical;
pub mod service;
pub mod types;

#[cfg(test)]
mod tests;

pub use lexical::LexicalSearchBackend;
pub use service::{FallbackPolicy, SearchService, UnavailableSemanticBackend};
pub use types::{
    FallbackReason, SearchError, SearchMode, SearchQuery, SearchQueryMode, SearchResponse, SearchResult,
    SearchSource,
};

use async_trait::async_trait;
use types::{SearchError as SE, SearchQuery as SQ, SearchResponse as SR};

#[async_trait]
pub trait SearchBackend: Send + Sync {
    async fn search(&self, query: SQ) -> Result<SR, SE>;
}
