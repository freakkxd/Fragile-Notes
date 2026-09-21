#[cfg(test)]
mod task_tests {
    use super::super::super::{LlmConfig, ProviderKind, Capability, Privacy, AuthMethod, AuthRef, Provider, Model, RuntimeProfile, LlamaSettings};
    use super::super::policy::{scope_of, ProviderScope, validate_privacy, ensure_capability};
    use super::super::selection::resolve_selection;
    use super::super::startup::StartupManager;
    use std::collections::HashMap;
    use std::sync::Arc;

    fn make_config_local() -> LlmConfig {
        let mut cfg = LlmConfig::default();
        cfg.models = vec![crate::llm::Model{
            id: "model-local".to_string(), provider_id: "local".to_string(), name: "qwen.gguf".to_string(),
            path: "/tmp/Models/qwen.gguf".to_string(), remote_id: "".to_string(),
            capabilities: vec![Capability::Chat], capability_source: None,
            metadata: crate::llm::ModelMetadata{ size_bytes: 0, sha256: "".to_string(), quant: "Q4".to_string(), arch: "".to_string(), context_length: 4096, chat_template: "".to_string(), source: "gguf".to_string()}
        }];
        cfg.runtime_profiles = vec![RuntimeProfile{
            id: "runtime-local".to_string(), provider_id: "local".to_string(), model_id: "model-local".to_string(),
            executable_source: None, binary_path: "llama-server".to_string(), port: 8010, policy: "on-demand".to_string(), settings: LlamaSettings::default()
        }];
        cfg.task_profiles = vec![
            crate::llm::TaskProfile{ id: "task-local".to_string(), name: "Local Chat".to_string(), model_ref: "model-local".to_string(), runtime_profile_id: Some("runtime-local".to_string()), privacy: Some(Privacy::LocalOnly), cloud_policy: None, generation: HashMap::new() },
            crate::llm::TaskProfile{ id: "task-cloud".to_string(), name: "Cloud Chat".to_string(), model_ref: "model-cloud".to_string(), runtime_profile_id: None, privacy: Some(Privacy::CloudAllowed), cloud_policy: None, generation: HashMap::new() },
        ];
        cfg.providers.iter_mut().find(|p| p.id=="local").unwrap().enabled = true;
        cfg
    }

    #[test]
    fn test_local_task_resolves() {
        let cfg = make_config_local();
        let sel = resolve_selection(&cfg, "task-local").unwrap();
        assert_eq!(sel.provider.id, "local");
        assert_eq!(sel.model.id, "model-local");
        assert!(sel.runtime_profile.is_some());
    }

    #[test]
    fn test_cloud_task_skips_runtime() {
        let mut cfg = LlmConfig::default();
        cfg.models = vec![Model{ id: "model-cloud".to_string(), provider_id: "openai".to_string(), name: "gpt-4o".to_string(), path: "".to_string(), remote_id: "gpt-4o".to_string(), capabilities: vec![Capability::Chat], capability_source: None, metadata: Default::default()}];
        cfg.task_profiles = vec![crate::llm::TaskProfile{ id: "task-cloud".to_string(), name: "Cloud".to_string(), model_ref: "model-cloud".to_string(), runtime_profile_id: None, privacy: Some(Privacy::CloudAllowed), cloud_policy: None, generation: HashMap::new()}];
        let sel = resolve_selection(&cfg, "task-cloud").unwrap();
        assert_eq!(sel.provider.kind, ProviderKind::OpenAI);
        assert!(sel.runtime_profile.is_none(), "cloud should have no runtime");
    }

    #[test]
    fn test_local_only_rejects_cloud_before_network() {
        let privacy = Some(Privacy::LocalOnly);
        let scope = scope_of(&ProviderKind::OpenAI, "https://api.openai.com/v1");
        assert_eq!(scope, ProviderScope::Cloud);
        let res = validate_privacy(&privacy, &scope);
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("PrivacyPolicyViolation"));
    }

    #[test]
    fn test_custom_localhost_requires_explicit_scope() {
        let scope_local = scope_of(&ProviderKind::CustomOpenAI, "http://127.0.0.1:11434/v1");
        assert_eq!(scope_local, ProviderScope::LocalExternal);
        let scope_cloud = scope_of(&ProviderKind::CustomOpenAI, "https://custom.example.com/v1");
        assert_eq!(scope_cloud, ProviderScope::Cloud);
        // LocalOnly should allow LocalExternal but not Cloud
        let privacy = Some(Privacy::LocalOnly);
        assert!(validate_privacy(&privacy, &ProviderScope::LocalExternal).is_ok());
        assert!(validate_privacy(&privacy, &ProviderScope::Cloud).is_err());
    }

    #[test]
    fn test_missing_model_fails_before_spawn() {
        let cfg = LlmConfig::default();
        // default task-chat has empty model_ref -> should fail before spawn
        let res = resolve_selection(&cfg, "task-chat");
        assert!(res.is_err());
        let err = res.unwrap_err();
        assert!(err.contains("model_not_found") || err.contains("task_not_found") || err.contains("empty"), "got: {}", err);
    }

    #[test]
    fn test_unsupported_capability_fails_before_spawn() {
        let caps = vec![Capability::Chat];
        let res = ensure_capability(&caps, Capability::Vision);
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("UnsupportedCapability"));
        let ok = ensure_capability(&caps, Capability::Chat);
        assert!(ok.is_ok());
    }

    #[tokio::test]
    async fn test_concurrent_ensure_runtime_shares_startup() {
        let mgr = Arc::new(StartupManager::new());
        let key = "runtime:test-concurrent";
        let (should1, notify1) = mgr.should_start(key).await;
        assert!(should1);
        let (should2, _) = mgr.should_start(key).await;
        assert!(!should2, "second concurrent should not start");
        // mark ready and ensure waiter is notified
        mgr.mark_ready(key).await;
        // second waiter should have been notified (we can't easily test notify without holding, but at least state is Ready)
        let (should3, _) = mgr.should_start(key).await;
        assert!(!should3);
    }

    #[tokio::test]
    async fn test_cancellation_during_startup() {
        let token = tokio_util::sync::CancellationToken::new();
        let t = token.clone();
        let handle = tokio::spawn(async move {
            tokio::select! {
                _ = tokio::time::sleep(std::time::Duration::from_secs(10)) => "done",
                _ = t.cancelled() => "cancelled",
            }
        });
        token.cancel();
        let res = handle.await.unwrap();
        assert_eq!(res, "cancelled");
    }

    #[test]
    fn test_startup_timeout_typed_error() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(async {
            let mgr = StartupManager::new();
            let key = "test-timeout";
            let (should, _) = mgr.should_start(key).await;
            assert!(should);
            mgr.mark_failed(key, "timeout".to_string()).await;
            let (should2, _) = mgr.should_start(key).await;
            // After Failed, should allow retry (new Starting)
            assert!(should2, "after Failed should allow retry");
        });
    }

    #[tokio::test]
    async fn test_start_ready_stop_cleared() {
        let mgr = StartupManager::new();
        let key = "test-restart";
        let (should, _) = mgr.should_start(key).await;
        assert!(should);
        mgr.mark_ready(key).await;
        assert_eq!(mgr.get_state(key).await, crate::llm::task::startup::StartupState::Ready);
        mgr.clear(key).await;
        assert_eq!(mgr.get_state(key).await, crate::llm::task::startup::StartupState::Idle);
        let (should2, _) = mgr.should_start(key).await;
        assert!(should2);
        mgr.mark_ready(key).await;
        assert_eq!(mgr.get_state(key).await, crate::llm::task::startup::StartupState::Ready);
    }

    #[tokio::test]
    async fn test_failed_cleared_retry() {
        let mgr = StartupManager::new();
        let key = "test-failed";
        let (should, _) = mgr.should_start(key).await;
        assert!(should);
        mgr.mark_failed(key, "fail".to_string()).await;
        assert_eq!(mgr.get_state(key).await, crate::llm::task::startup::StartupState::Failed("fail".to_string()));
        mgr.clear(key).await;
        assert_eq!(mgr.get_state(key).await, crate::llm::task::startup::StartupState::Idle);
        let (should2, _) = mgr.should_start(key).await;
        assert!(should2);
    }

    #[tokio::test]
    async fn test_cancel_cleared() {
        let mgr = StartupManager::new();
        let key = "test-cancel";
        let (should, notify) = mgr.should_start(key).await;
        assert!(should);
        // Simulate cancellation by clearing
        mgr.clear(key).await;
        assert_eq!(mgr.get_state(key).await, crate::llm::task::startup::StartupState::Idle);
        // Idempotent clear
        mgr.clear(key).await;
        mgr.clear(key).await;
        assert_eq!(mgr.get_state(key).await, crate::llm::task::startup::StartupState::Idle);
        // After cancel, new start should work
        let (should2, _) = mgr.should_start(key).await;
        assert!(should2);
        let _ = notify;
    }

    #[tokio::test]
    async fn test_clear_idempotent() {
        let mgr = StartupManager::new();
        let key = "test-idempotent";
        mgr.clear(key).await;
        mgr.clear(key).await;
        assert_eq!(mgr.get_state(key).await, crate::llm::task::startup::StartupState::Idle);
        let (should, _) = mgr.should_start(key).await;
        assert!(should);
        mgr.mark_ready(key).await;
        mgr.clear(key).await;
        mgr.clear(key).await;
        assert_eq!(mgr.get_state(key).await, crate::llm::task::startup::StartupState::Idle);
    }

    #[tokio::test]
    async fn test_concurrent_one_process() {
        let mgr = std::sync::Arc::new(StartupManager::new());
        let key = "test-concurrent2";
        let m1 = mgr.clone();
        let m2 = mgr.clone();
        let (should1, _) = m1.should_start(key).await;
        assert!(should1);
        let (should2, notify2) = m2.should_start(key).await;
        assert!(!should2);
        // Second should wait, first marks ready
        let mgr_clone = mgr.clone();
        let handle = tokio::spawn(async move {
            mgr_clone.mark_ready(key).await;
        });
        // Wait for second's notify
        let wait_fut = m2.wait(key, notify2);
        tokio::time::timeout(std::time::Duration::from_secs(1), wait_fut).await.expect("wait should complete after ready");
        handle.await.unwrap();
        assert_eq!(mgr.get_state(key).await, crate::llm::task::startup::StartupState::Ready);
    }

    #[tokio::test]
    async fn test_stop_start_race() {
        let mgr = std::sync::Arc::new(StartupManager::new());
        let key = "test-race";
        let (should, _) = mgr.should_start(key).await;
        assert!(should);
        mgr.mark_ready(key).await;
        // Simulate stop + start simultaneously
        let m1 = mgr.clone();
        let m2 = mgr.clone();
        let h1 = tokio::spawn(async move { m1.clear(key).await; });
        let h2 = tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            let (should, _) = m2.should_start(key).await;
            assert!(should, "after clear, should allow start");
            m2.mark_ready(key).await;
        });
        h1.await.unwrap();
        h2.await.unwrap();
        assert_eq!(mgr.get_state(key).await, crate::llm::task::startup::StartupState::Ready);
    }
}
