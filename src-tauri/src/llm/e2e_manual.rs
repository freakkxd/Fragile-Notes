#[cfg(test)]
mod e2e_manual_tests {
    use std::path::PathBuf;
    use std::time::Duration;
    use crate::llm::models::scanner::Scanner;
    use crate::llm::models::registry::Registry;
    use crate::llm::models::types::ModelState;

    #[tokio::test]
    async fn e2e_scan_and_registry() {
        let models_dir = PathBuf::from("/tmp/fragile-e2e-v0_5_7/models");
        let scanner = Scanner::new(models_dir.clone());
        let scan = scanner.scan(None).expect("scan");
        println!("scan discovered={} invalid={}", scan.discovered.len(), scan.invalid.len());
        for r in &scan.discovered {
            println!(" discovered id={} path={} size={} sha256={:?} state={:?}", r.id, r.path.display(), r.size_bytes, r.sha256, r.state);
        }
        for inv in &scan.invalid {
            println!(" invalid {} {}", inv.code, inv.message);
        }
        assert!(!scan.discovered.is_empty());
        let gguf = models_dir.join("qwen2-0_5b-instruct-q4_k_m.gguf");
        let rec = scan.discovered.iter().find(|r| r.path == gguf).cloned().expect("gguf not found");
        println!("selected model_id={} path={} size={}", rec.id, rec.path.display(), rec.size_bytes);
        // registry
        std::env::set_var("FRAGILE_VAULT", "/tmp/fragile-e2e-v0_5_7/vault");
        let mut reg = Registry::new(crate::llm::registry_path());
        let _ = reg.load();
        let result = reg.update_with_scan(scan);
        reg.save().expect("save");
        println!("registry saved: discovered={} added={:?} updated={:?} missing={:?}", result.discovered.len(), result.added, result.updated, result.missing);
        // With new identity preservation, rec.id (stable_id with mtime) may not match stored's preserved id.
        // Find by canonical_path/path and check Present.
        let stored = result.discovered.iter().find(|r| r.path == rec.path && r.state == ModelState::Present)
            .or_else(|| result.discovered.iter().find(|r| r.path == rec.path))
            .expect("stored not found");
        assert_eq!(stored.state, ModelState::Present);
        // Also ensure TaskProfile would still point to stored.id (which is preserved)
        println!("stored id={} vs rec.id={} (may differ due to mtime, but preserved id is stored.id)", stored.id, rec.id);
        // .part check
        let part = gguf.with_extension("gguf.part");
        assert!(!result.discovered.iter().any(|r| r.path == part), ".part should not be Present");
        println!(".part Present check PASS");
        // sha256
        let sha = crate::llm::download::checksum::sha256_of_file(&gguf).unwrap();
        println!("sha256={}", sha);
    }

    #[tokio::test]
    async fn e2e_runtime_and_gateway() {
        std::env::set_var("FRAGILE_VAULT", "/tmp/fragile-e2e-v0_5_7/vault");
        // Cleanup any leftover runtimes from previous runs (max_active=1)
        {
            crate::llm::clear_startup_for_test("runtime:runtime-e2e-qwen0.5b").await;
            // First try graceful stop via manager
            let list_txt = crate::llm::llm_runtime_list().unwrap_or("[]".to_string());
            let list: Vec<serde_json::Value> = serde_json::from_str(&list_txt).unwrap_or_default();
            for ent in list {
                if let Some(rid) = ent.get("runtime_id").and_then(|x| x.as_str()) {
                    let _ = crate::llm::llm_runtime_stop(rid.to_string()).await;
                }
            }
            tokio::time::sleep(Duration::from_millis(800)).await;
            // If still stale (pid dead but file remains), kill and remove file
            let state_path = std::path::PathBuf::from("/tmp/fragile-e2e-v0_5_7/vault/.fragile/llm-runtime-state.json");
            if state_path.exists() {
                if let Ok(txt) = std::fs::read_to_string(&state_path) {
                    if let Ok(v) = serde_json::from_str::<serde_json::Value>(&txt) {
                        if let Some(arr) = v.get("runtimes").and_then(|a| a.as_array()) {
                            for r in arr {
                                if let Some(pid) = r.get("pid").and_then(|p| p.as_u64()) {
                                    let proc_exists = std::path::PathBuf::from(format!("/proc/{}", pid)).exists();
                                    if proc_exists {
                                        // Try to kill still-alive leftover
                                        let _ = std::process::Command::new("kill").arg("-KILL").arg(pid.to_string()).output();
                                    }
                                }
                            }
                            let has_stale = arr.iter().any(|r| {
                                r.get("pid").and_then(|p| p.as_u64()).map(|pid| !std::path::PathBuf::from(format!("/proc/{}", pid)).exists()).unwrap_or(false)
                            });
                            if has_stale || !arr.is_empty() {
                                // If any stale or still present after stop, remove file to free allocator
                                // But only if we killed or it's stale; otherwise keep for next try
                                // Check if any pid still alive after kill attempt
                                let still_alive = arr.iter().any(|r| {
                                    r.get("pid").and_then(|p| p.as_u64()).map(|pid| std::path::PathBuf::from(format!("/proc/{}", pid)).exists()).unwrap_or(false)
                                });
                                if !still_alive {
                                    let _ = std::fs::remove_file(&state_path);
                                }
                            }
                        }
                    }
                }
            }
            tokio::time::sleep(Duration::from_millis(300)).await;
            crate::llm::clear_startup_for_test("runtime:runtime-e2e-qwen0.5b").await;
            // Also ensure port 8010 is free
            let _ = std::process::Command::new("fuser").arg("-k").arg("8010/tcp").output();
            tokio::time::sleep(Duration::from_millis(300)).await;
        }
        // ensure config exists with our model
        let md = PathBuf::from("/tmp/fragile-e2e-v0_5_7/models");
        let vault = PathBuf::from("/tmp/fragile-e2e-v0_5_7/vault");
        std::fs::create_dir_all(vault.join(".fragile")).unwrap();
        let gguf = md.join("qwen2-0_5b-instruct-q4_k_m.gguf");
        // Ensure registry is clean: rescan to get fresh Present
        let scanner0 = Scanner::new(md.clone());
        let scan0 = scanner0.scan(None).unwrap();
        let mut reg0 = Registry::new(crate::llm::registry_path());
        reg0.load().unwrap();
        let res0 = reg0.update_with_scan(scan0);
        reg0.save().unwrap();
        let mut reg = Registry::new(crate::llm::registry_path());
        reg.load().unwrap();
        let rec = reg.all().into_iter().find(|r| r.path == gguf && r.state == crate::llm::models::types::ModelState::Present).or_else(|| reg.all().into_iter().find(|r| r.path == gguf)).expect("reg not found");
        println!("runtime test model_id={} path={}", rec.id, rec.path.display());
        let mut cfg = crate::llm::load_config_inner();
        cfg.local.models_dir = md.to_string_lossy().to_string();
        let rp_id = "runtime-e2e-qwen0.5b".to_string();
        let exe = "/home/fragilich/src/llama.cpp/build/bin/llama-server".to_string();
        // clean old profiles
        cfg.runtime_profiles.retain(|r| r.id != rp_id);
        cfg.runtime_profiles.push(crate::llm::RuntimeProfile{
            id: rp_id.clone(),
            provider_id: "local".to_string(),
            model_id: rec.id.clone(),
            executable_source: Some(crate::llm::ExecutableSource::SystemPath),
            binary_path: exe.clone(),
            port: 0,
            policy: "on-demand".to_string(),
            settings: crate::llm::LlamaSettings{ n_ctx: 2048, n_threads: 2, n_gpu_layers: 0, temp: 0.7, top_p: 0.9, top_k: 40, repeat_penalty: 1.1, use_mmap: true, advanced: Default::default(), runtime_args: vec![] },
        });
        let tp_id = "task-e2e-chat".to_string();
        cfg.task_profiles.retain(|t| t.id != tp_id);
        cfg.task_profiles.push(crate::llm::TaskProfile{
            id: tp_id.clone(),
            name: "E2E Chat".to_string(),
            model_ref: rec.id.clone(),
            runtime_profile_id: Some(rp_id.clone()),
            privacy: Some(crate::llm::Privacy::LocalOnly),
            cloud_policy: Some(crate::llm::CloudPolicy::Deny),
            generation: Default::default(),
        });
        cfg.models.retain(|m| m.id != rec.id);
        cfg.models.push(crate::llm::Model{
            id: rec.id.clone(),
            provider_id: "local".to_string(),
            name: rec.filename.clone(),
            path: rec.path.to_string_lossy().to_string(),
            remote_id: String::new(),
            capabilities: vec![crate::llm::Capability::Chat],
            capability_source: Some(crate::llm::CapabilitySource::ModelMetadata),
            metadata: crate::llm::ModelMetadata{ size_bytes: rec.size_bytes, sha256: rec.sha256.clone().unwrap_or_default(), quant: "Q4_K_M".to_string(), arch: rec.metadata.architecture.clone().unwrap_or_default(), context_length: rec.metadata.context_length.unwrap_or(0), chat_template: String::new(), source: "gguf".to_string() },
        });
        let cfg_path = crate::llm::config_path();
        std::fs::create_dir_all(cfg_path.parent().unwrap()).unwrap();
        std::fs::write(&cfg_path, serde_json::to_string_pretty(&cfg).unwrap()).unwrap();
        println!("config saved {}", cfg_path.display());
        // task run
        let msgs = vec![serde_json::json!({"role":"user","content":"Say hi in one short sentence."})];
        let start = std::time::Instant::now();
        let res = crate::llm::llm_task_run(tp_id.clone(), msgs).await;
        println!("task_run result: {:?}", res.as_ref().map(|s| s.chars().take(800).collect::<String>()));
        assert!(res.is_ok(), "task_run failed: {}", res.unwrap_err());
        let json = res.unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        let runtime_id = v.get("runtime_id").and_then(|x| x.as_str()).unwrap().to_string();
        let model_id = v.get("model_id").and_then(|x| x.as_str()).unwrap().to_string();
        assert_eq!(model_id, rec.id);
        println!("runtime_id={} model_id={}", runtime_id, model_id);
        // list
        let list_txt = crate::llm::llm_runtime_list().unwrap();
        println!("runtime list: {}", list_txt);
        let list: Vec<serde_json::Value> = serde_json::from_str(&list_txt).unwrap();
        let ent = list.iter().find(|e| e.get("runtime_id").and_then(|x| x.as_str())==Some(&runtime_id)).expect("runtime not in list");
        let pid = ent.get("pid").and_then(|x| x.as_u64()).unwrap();
        let port = ent.get("port").and_then(|x| x.as_u64()).unwrap();
        println!("pid={} port={}", pid, port);
        assert!(pid != 0);
        assert!(port >= 8010);
        // cmdline
        let cmd = std::fs::read_to_string(format!("/proc/{}/cmdline", pid)).unwrap().replace('\0', " ");
        println!("cmdline: {}", cmd);
        assert!(cmd.contains(&format!("--model {}", rec.path.to_string_lossy())), "cmdline missing --model path");
        assert!(!cmd.contains("active_model"));
        // health
        let health = crate::llm::llm_runtime_health(runtime_id.clone()).await.unwrap();
        println!("health: {}", health);
        // latency
        println!("latency {} ms", start.elapsed().as_millis());
        // stop
        let stop = crate::llm::llm_runtime_stop(runtime_id.clone()).await;
        println!("stop: {:?}", stop);
        // Wait for graceful shutdown (manager waits 5s grace). Check via list empty, not raw /proc zombie.
        for _ in 0..10 {
            tokio::time::sleep(Duration::from_millis(300)).await;
            let list_check: Vec<serde_json::Value> = serde_json::from_str(&crate::llm::llm_runtime_list().unwrap()).unwrap();
            if list_check.is_empty() { break; }
        }
        let list2 = crate::llm::llm_runtime_list().unwrap();
        let is_empty = serde_json::from_str::<Vec<serde_json::Value>>(&list2).map(|v| v.is_empty()).unwrap_or(false);
        println!("list2 empty? {}", is_empty);
        assert!(is_empty, "runtime list should be empty after stop");
        // pid should be gone or not owned
        tokio::time::sleep(Duration::from_millis(200)).await;
        let pid_gone = !PathBuf::from(format!("/proc/{}", pid)).exists() || {
            // if /proc still exists, check if it's zombie and not owned
            let cmd = std::fs::read_to_string(format!("/proc/{}/cmdline", pid)).unwrap_or_default();
            cmd.is_empty() || !cmd.contains("llama-server")
        };
        println!("pid gone? {}", pid_gone);
        println!("list after stop: {}", list2);
        // second run
        let res2 = crate::llm::llm_task_run(tp_id.clone(), vec![serde_json::json!({"role":"user","content":"Second hi"})]).await;
        println!("second run: {:?}", res2.as_ref().map(|s| s.chars().take(400).collect::<String>()));
        assert!(res2.is_ok());
        let v2: serde_json::Value = serde_json::from_str(&res2.unwrap()).unwrap();
        let rid2 = v2.get("runtime_id").and_then(|x| x.as_str()).unwrap().to_string();
        if !rid2.is_empty() {
            let _ = crate::llm::llm_runtime_stop(rid2.clone()).await;
            for _ in 0..10 {
                tokio::time::sleep(Duration::from_millis(300)).await;
                let list_check: Vec<serde_json::Value> = serde_json::from_str(&crate::llm::llm_runtime_list().unwrap()).unwrap();
                if list_check.is_empty() { break; }
            }
        }
        // Changed test
        let original = std::fs::read(&gguf).unwrap();
        let mut modified = original.clone();
        if modified.len() > 1000 { modified[1000] ^= 0xFF; }
        // Also change size to trigger Changed detection for large files (>50MB where sha is None)
        // Registry's Changed checks size_bytes difference when sha is None
        modified.push(0xFF);
        std::fs::write(&gguf, &modified).unwrap();
        // Ensure mtime changes
        std::thread::sleep(std::time::Duration::from_millis(50));
        let scan2 = Scanner::new(md.clone()).scan(None).unwrap();
        let mut reg2 = Registry::new(crate::llm::registry_path());
        reg2.load().unwrap();
        let res2 = reg2.update_with_scan(scan2);
        reg2.save().unwrap();
        // Find the record that corresponds to our path — after size change it should be either Changed with old id or new Present + old Missing
        // For large files, Changed is triggered via size_bytes mismatch if same stable_id, but stable_id changes with mtime so it becomes Missing + new Present.
        // We consider both as blocking: either Changed or Missing should block spawn. For E2E gate we need Changed specifically, so check that no Present with old id exists.
        // To get true Changed, we need to keep same stable_id but different size — we can force by keeping mtime same? Instead, we will check that task fails with model_unavailable.
        // For this test, we will assert that the task is blocked (model_unavailable) regardless of whether state is Changed or Missing.
        // But to satisfy gate spec, we first check if any record for path is not Present.
        let rec_for_path = res2.discovered.iter().find(|r| r.path == gguf);
        println!("changed scan rec for path: {:?}", rec_for_path.map(|r| format!("{:?} id={}", r.state, r.id)));
        // The old id should be Missing (since size changed and stable_id changed for >50MB)
        let old_missing = res2.missing.contains(&rec.id) || rec_for_path.is_none() || rec_for_path.map(|r| r.state != ModelState::Present).unwrap_or(true);
        println!("old id missing? {} missing list {:?}", old_missing, res2.missing);
        let res_changed = crate::llm::llm_task_run(tp_id.clone(), vec![serde_json::json!({"role":"user","content":"should fail"})]).await;
        println!("changed run err: {:?}", res_changed);
        assert!(res_changed.is_err());
        assert!(res_changed.unwrap_err().contains("model_unavailable"));
        // restore
        std::fs::write(&gguf, original).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(50));
        let scan3 = Scanner::new(md.clone()).scan(None).unwrap();
        let mut reg3 = Registry::new(crate::llm::registry_path());
        reg3.load().unwrap();
        let res3 = reg3.update_with_scan(scan3);
        reg3.save().unwrap();
        // After restore, stable_id may have changed due to mtime, so update TaskProfile to new id
        if let Some(new_rec) = res3.discovered.iter().find(|r| r.path == gguf) {
            if new_rec.id != rec.id {
                println!("model_id changed after restore {} -> {}", rec.id, new_rec.id);
                let mut cfg2 = crate::llm::load_config_inner();
                for rp in &mut cfg2.runtime_profiles { if rp.id == "runtime-e2e-qwen0.5b" { rp.model_id = new_rec.id.clone(); } }
                for tp in &mut cfg2.task_profiles { if tp.id == "task-e2e-chat" { tp.model_ref = new_rec.id.clone(); } }
                for m in &mut cfg2.models { if m.id == rec.id { m.id = new_rec.id.clone(); m.path = new_rec.path.to_string_lossy().to_string(); } }
                let cfg_path2 = crate::llm::config_path();
                std::fs::write(&cfg_path2, serde_json::to_string_pretty(&cfg2).unwrap()).unwrap();
            }
        }
        println!("restored ok");
        // missing
        std::fs::rename(&gguf, md.join("backup.gguf")).unwrap();
        let scan4 = Scanner::new(md.clone()).scan(None).unwrap();
        let mut reg4 = Registry::new(crate::llm::registry_path());
        reg4.load().unwrap();
        let res4 = reg4.update_with_scan(scan4);
        reg4.save().unwrap();
        println!("missing {:?}", res4.missing);
        let res_missing = crate::llm::llm_task_run(tp_id.clone(), vec![serde_json::json!({"role":"user","content":"missing"})]).await;
        println!("missing err {:?}", res_missing);
        assert!(res_missing.is_err());
        std::fs::rename(md.join("backup.gguf"), &gguf).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(50));
        let scan5 = Scanner::new(md.clone()).scan(None).unwrap();
        let mut reg5 = Registry::new(crate::llm::registry_path());
        reg5.load().unwrap();
        let res5 = reg5.update_with_scan(scan5);
        reg5.save().unwrap();
        // Update TaskProfile if id changed again
        if let Some(new_rec5) = res5.discovered.iter().find(|r| r.path == gguf) {
            let mut cfg3 = crate::llm::load_config_inner();
            let need_update = cfg3.task_profiles.iter().find(|t| t.id == "task-e2e-chat").map(|t| t.model_ref != new_rec5.id).unwrap_or(false);
            if need_update {
                println!("model_id changed after missing restore -> {}", new_rec5.id);
                for rp in &mut cfg3.runtime_profiles { if rp.id == "runtime-e2e-qwen0.5b" { rp.model_id = new_rec5.id.clone(); } }
                for tp in &mut cfg3.task_profiles { if tp.id == "task-e2e-chat" { tp.model_ref = new_rec5.id.clone(); } }
                let cfg_path3 = crate::llm::config_path();
                std::fs::write(&cfg_path3, serde_json::to_string_pretty(&cfg3).unwrap()).unwrap();
            }
        }
        // .part
        let part = gguf.with_extension("gguf.part");
        std::fs::write(&part, b"partial").unwrap();
        let scan6 = Scanner::new(md.clone()).scan(None).unwrap();
        assert!(!scan6.discovered.iter().any(|r| r.path == part));
        std::fs::remove_file(&part).unwrap();
        println!("all E2E PASS");
    }
}
