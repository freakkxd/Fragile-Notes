use crate::llm::{LlmConfig, Model, Provider, RuntimeProfile, TaskProfile};

#[derive(Debug)]
pub struct ResolvedSelection {
    pub task: TaskProfile,
    pub provider: Provider,
    pub model: Model,
    pub runtime_profile: Option<RuntimeProfile>,
}

pub fn resolve_selection(config: &LlmConfig, task_profile_id: &str) -> Result<ResolvedSelection, String> {
    let task = config.task_profiles.iter().find(|t| t.id == task_profile_id)
        .ok_or_else(|| format!("task_not_found: {}", task_profile_id))?.clone();
    let model = if task.model_ref.is_empty() {
        return Err(format!("model_not_found: task {} has empty model_ref", task_profile_id));
    } else {
        config.models.iter().find(|m| m.id == task.model_ref)
            .ok_or_else(|| format!("model_not_found: {}", task.model_ref))?.clone()
    };
    let provider = config.providers.iter().find(|p| p.id == model.provider_id)
        .ok_or_else(|| format!("provider_not_found: {}", model.provider_id))?.clone();
    let runtime_profile = if let Some(rid) = &task.runtime_profile_id {
        Some(config.runtime_profiles.iter().find(|r| &r.id == rid)
            .ok_or_else(|| format!("runtime_not_found: {}", rid))?.clone())
    } else {
        // For cloud, runtime is None; for local, try to find runtime for model
        if provider.kind == crate::llm::ProviderKind::LocalLlamaCpp {
            config.runtime_profiles.iter().find(|r| r.model_id == model.id).cloned()
        } else { None }
    };
    Ok(ResolvedSelection { task, provider, model, runtime_profile })
}
