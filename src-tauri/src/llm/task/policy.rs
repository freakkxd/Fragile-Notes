use crate::llm::{Capability, Privacy, ProviderKind};

#[derive(Debug, Clone, PartialEq)]
pub enum ProviderScope { LocalManaged, LocalExternal, Cloud }

#[derive(Debug, Clone, PartialEq)]
pub enum RuntimeSwitchPolicy { RejectIfBusy, StopAndSwitch, Queue }

#[derive(Debug, Clone, PartialEq)]
pub enum RuntimeRetention { StopAfterRun, KeepWarm { idle_for: std::time::Duration }, KeepAlive }

impl Default for RuntimeRetention {
    fn default() -> Self { RuntimeRetention::KeepWarm { idle_for: std::time::Duration::from_secs(300) } }
}

pub fn scope_of(kind: &ProviderKind, endpoint: &str) -> ProviderScope {
    match kind {
        ProviderKind::LocalLlamaCpp => ProviderScope::LocalManaged,
        ProviderKind::Ollama => ProviderScope::LocalExternal,
        ProviderKind::CustomOpenAI => {
            // Custom must be explicitly marked; for P1.4 default to Cloud unless endpoint is localhost
            if endpoint.contains("127.0.0.1") || endpoint.contains("localhost") {
                ProviderScope::LocalExternal
            } else {
                ProviderScope::Cloud
            }
        },
        ProviderKind::OpenAI | ProviderKind::Gemini | ProviderKind::Claude => ProviderScope::Cloud,
    }
}

pub fn validate_privacy(privacy: &Option<Privacy>, scope: &ProviderScope) -> Result<(), String> {
    if let Some(Privacy::LocalOnly) = privacy {
        match scope {
            ProviderScope::LocalManaged | ProviderScope::LocalExternal => Ok(()),
            ProviderScope::Cloud => Err("PrivacyPolicyViolation: local-only task cannot use cloud provider".to_string()),
        }
    } else {
        Ok(())
    }
}

pub fn ensure_capability(model_caps: &[Capability], required: Capability) -> Result<(), String> {
    if model_caps.contains(&required) || model_caps.is_empty() {
        // empty means unknown, allow for P1.4
        Ok(())
    } else {
        Err(format!("UnsupportedCapability: model missing {:?}", required))
    }
}
