//! v0.5.9 provider hub contracts (Stage 1).
//!
//! [`ProviderRegistry`] owns connections and the model catalog.
//! Adapters (Stage 2+) consume these contracts; no transport here.

pub mod openai;
pub mod openai_native;
pub mod types;

pub use types::{
    AuthReference, PricingInfo, ProviderConnection, ProviderError, ProviderModel, ProviderRegistry,
    default_endpoint,
};
pub use openai::{
    AdapterChatBridge, AdapterEmbedBridge, ChatMessage, ChatRequest, ChatResponse, ChatRole,
    ChatUsage, EndpointLayout, HealthReport, KeyringSecretStore, NoSecretStore, OpenAiCompatibleAdapter,
    ProviderAdapter, ResponseFormat, SecretStore, build_chat_body, detect_layout, join_api_path,
};
pub use openai_native::{OpenAiNativeAdapter, build_native_chat_body};

#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_native;
#[cfg(test)]
mod tests_transport;
