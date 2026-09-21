pub mod types;
pub mod identity;
pub mod gguf;
pub mod scanner;
pub mod registry;
#[cfg(test)]
mod tests;

pub use types::{ModelRecord, ScanResult, ModelState};
pub use scanner::Scanner;
pub use registry::{Registry, registry_path, llm_models_scan, llm_models_list};
