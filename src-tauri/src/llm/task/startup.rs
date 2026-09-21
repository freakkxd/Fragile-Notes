use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::{Mutex, Notify};

#[derive(Debug, Clone, PartialEq)]
pub enum StartupState { Idle, Starting, Ready, Failed(String) }

pub struct StartupEntry {
    pub state: StartupState,
    pub notify: Arc<Notify>,
}

pub struct StartupManager {
    map: Mutex<HashMap<String, Arc<StartupEntry>>>,
}

impl StartupManager {
    pub fn new() -> Self { Self { map: Mutex::new(HashMap::new()) } }

    // Single-flight: if already starting, wait for existing; otherwise insert Starting and return true if caller should start
    pub async fn should_start(&self, key: &str) -> (bool, Arc<Notify>) {
        let mut map = self.map.lock().await;
        if let Some(entry) = map.get(key) {
            match entry.state {
                StartupState::Starting => {
                    let notify = Arc::clone(&entry.notify);
                    return (false, notify);
                },
                StartupState::Ready => {
                    return (false, Arc::new(Notify::new()));
                },
                _ => {}
            }
        }
        let notify = Arc::new(Notify::new());
        map.insert(key.to_string(), Arc::new(StartupEntry{ state: StartupState::Starting, notify: Arc::clone(&notify) }));
        (true, notify)
    }

    pub async fn mark_ready(&self, key: &str) {
        let mut map = self.map.lock().await;
        if let Some(entry) = map.get_mut(key) {
            // We need to replace the entry; since Arc, we insert new
            let notify = Arc::clone(&entry.notify);
            *entry = Arc::new(StartupEntry{ state: StartupState::Ready, notify: notify.clone() });
            notify.notify_waiters();
        }
    }

    pub async fn mark_failed(&self, key: &str, err: String) {
        let mut map = self.map.lock().await;
        if let Some(entry) = map.get_mut(key) {
            let notify = Arc::clone(&entry.notify);
            *entry = Arc::new(StartupEntry{ state: StartupState::Failed(err), notify: notify.clone() });
            notify.notify_waiters();
        }
    }

    pub async fn get_state(&self, key: &str) -> StartupState {
        let map = self.map.lock().await;
        map.get(key).map(|e| e.state.clone()).unwrap_or(StartupState::Idle)
    }

    pub async fn clear(&self, key: &str) {
        let mut map = self.map.lock().await;
        map.remove(key);
    }

    pub async fn wait(&self, key: &str, notify: Arc<Notify>) {
        // If already Ready, don't wait — immediate return. Check state first.
        {
            let map = self.map.lock().await;
            if let Some(entry) = map.get(key) {
                if entry.state == StartupState::Ready {
                    return;
                }
            }
        }
        // Wait for the startup entry's notify
        let entry_notify = {
            let map = self.map.lock().await;
            map.get(key).map(|e| Arc::clone(&e.notify)).unwrap_or(notify)
        };
        entry_notify.notified().await;
    }
}
