//! v0.5.9 provider hub contracts (Stage 1).
//!
//! [`ProviderRegistry`] owns connections and the model catalog.
//! Adapters (Stage 2+) consume these contracts; no transport here.

pub mod types;

pub use types::{
    AuthReference, PricingInfo, ProviderConnection, ProviderError, ProviderModel, ProviderRegistry,
    default_endpoint,
};

#[cfg(test)]
mod tests;
