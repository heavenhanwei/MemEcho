use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

/// Recovery metadata persisted every 5 seconds by the supervisor thread.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecoveryMeta {
    pub session_id: String,
    pub mic_path: PathBuf,
    pub loopback_path: PathBuf,
    pub sample_rate: u32,
    pub started_at: chrono::DateTime<chrono::Utc>,
    /// Byte offset into mic.wav data chunk that was safely flushed to disk.
    pub mic_offset: u64,
    /// Byte offset into loopback.wav data chunk that was safely flushed to disk.
    pub loopback_offset: u64,
    pub status: RecoveryStatus,
    /// Optional error code when status is Failed. Absent in old JSON.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub error_code: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryStatus {
    Recording,
    Paused,
    Finalized,
    Failed,
}

impl RecoveryMeta {
    /// Atomically write recovery.json (write-to-temp then rename).
    pub fn save(&self, session_dir: &Path) -> Result<(), RecoveryError> {
        let path = crate::paths::recovery_json_path(session_dir);
        let tmp_path = path.with_extension("json.tmp");
        let json = serde_json::to_string_pretty(self)
            .map_err(|e| RecoveryError::Serialize(e.to_string()))?;
        std::fs::write(&tmp_path, &json).map_err(RecoveryError::Io)?;
        std::fs::rename(&tmp_path, &path).map_err(RecoveryError::Io)?;
        Ok(())
    }

    /// Load recovery.json from a session directory.
    pub fn load(session_dir: &Path) -> Result<Self, RecoveryError> {
        let path = crate::paths::recovery_json_path(session_dir);
        let data = std::fs::read_to_string(&path).map_err(RecoveryError::Io)?;
        serde_json::from_str(&data).map_err(|e| RecoveryError::Serialize(e.to_string()))
    }
}

/// Scan the sessions directory for recoverable sessions.
pub fn list_recoverable(sessions_dir: &Path) -> Vec<RecoveryMeta> {
    let mut results = Vec::new();
    let Ok(entries) = std::fs::read_dir(sessions_dir) else {
        return results;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        if let Ok(meta) = RecoveryMeta::load(&path) {
            if meta.status != RecoveryStatus::Finalized {
                results.push(meta);
            }
        }
    }
    results
}

#[derive(Debug, thiserror::Error)]
pub enum RecoveryError {
    #[error("io error: {0}")]
    Io(std::io::Error),
    #[error("serialization error: {0}")]
    Serialize(String),
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;

    fn make_meta(sid: &str) -> RecoveryMeta {
        RecoveryMeta {
            session_id: sid.into(),
            mic_path: PathBuf::from("C:\\s\\t\\mic.wav"),
            loopback_path: PathBuf::from("C:\\s\\t\\lb.wav"),
            sample_rate: 16000,
            started_at: Utc::now(),
            mic_offset: 0,
            loopback_offset: 0,
            status: RecoveryStatus::Recording,
            error_code: None,
        }
    }

    #[test]
    fn test_recovery_meta_roundtrip() {
        let dir = std::env::temp_dir().join("memecho_t_rec_rt");
        std::fs::create_dir_all(&dir).unwrap();
        let mut m = make_meta("rt");
        m.mic_offset = 12345;
        m.loopback_offset = 67890;
        m.save(&dir).unwrap();
        let l = RecoveryMeta::load(&dir).unwrap();
        assert_eq!(l.session_id, "rt");
        assert_eq!(l.mic_offset, 12345);
        assert_eq!(l.loopback_offset, 67890);
        assert_eq!(l.status, RecoveryStatus::Recording);
        assert_eq!(l.error_code, None);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_recovery_json_is_atomic() {
        let dir = std::env::temp_dir().join("memecho_t_rec_at");
        std::fs::create_dir_all(&dir).unwrap();
        make_meta("at").save(&dir).unwrap();
        assert!(!dir.join("recovery.json.tmp").exists());
        assert!(dir.join("recovery.json").exists());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_list_recoverable_skips_finalized() {
        let dir = std::env::temp_dir().join("memecho_t_rec_sf");
        std::fs::create_dir_all(&dir).unwrap();
        let s1 = dir.join("r");
        std::fs::create_dir_all(&s1).unwrap();
        let mut m1 = make_meta("r");
        m1.status = RecoveryStatus::Recording;
        m1.save(&s1).unwrap();
        let s2 = dir.join("f");
        std::fs::create_dir_all(&s2).unwrap();
        let mut m2 = make_meta("f");
        m2.status = RecoveryStatus::Finalized;
        m2.save(&s2).unwrap();
        let r = list_recoverable(&dir);
        assert_eq!(r.len(), 1);
        assert_eq!(r[0].session_id, "r");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_list_recoverable_includes_paused() {
        let dir = std::env::temp_dir().join("memecho_t_rec_ip");
        std::fs::create_dir_all(&dir).unwrap();
        let s1 = dir.join("p");
        std::fs::create_dir_all(&s1).unwrap();
        let mut m1 = make_meta("p");
        m1.status = RecoveryStatus::Paused;
        m1.save(&s1).unwrap();
        let r = list_recoverable(&dir);
        assert_eq!(r.len(), 1);
        assert_eq!(r[0].status, RecoveryStatus::Paused);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_recovery_serialization_format() {
        let m = make_meta("fmt");
        let j = serde_json::to_string(&m).unwrap();
        assert!(j.contains("\"session_id\""));
        assert!(j.contains("\"mic_offset\""));
        assert!(j.contains("\"loopback_offset\""));
        assert!(j.contains("\"status\""));
        assert!(j.contains("\"recording\""));
        // error_code should be absent when None
        assert!(!j.contains("error_code"));
    }

    #[test]
    fn test_recovery_failed_with_error_code() {
        let dir = std::env::temp_dir().join("memecho_t_rec_fail");
        std::fs::create_dir_all(&dir).unwrap();
        let mut m = make_meta("fail");
        m.status = RecoveryStatus::Failed;
        m.error_code = Some("WASAPI_INIT".into());
        m.save(&dir).unwrap();
        let l = RecoveryMeta::load(&dir).unwrap();
        assert_eq!(l.status, RecoveryStatus::Failed);
        assert_eq!(l.error_code.as_deref(), Some("WASAPI_INIT"));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_old_json_without_error_code() {
        // Simulate loading old JSON that lacks error_code field
        let dir = std::env::temp_dir().join("memecho_t_rec_old");
        std::fs::create_dir_all(&dir).unwrap();
        let old_json = r#"{
            "session_id": "old",
            "mic_path": "C:\\m.wav",
            "loopback_path": "C:\\l.wav",
            "sample_rate": 16000,
            "started_at": "2025-01-01T00:00:00Z",
            "mic_offset": 100,
            "loopback_offset": 200,
            "status": "recording"
        }"#;
        std::fs::write(dir.join("recovery.json"), old_json).unwrap();
        let l = RecoveryMeta::load(&dir).unwrap();
        assert_eq!(l.error_code, None);
        assert_eq!(l.status, RecoveryStatus::Recording);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_recovery_dual_offsets() {
        let dir = std::env::temp_dir().join("memecho_t_rec_do");
        std::fs::create_dir_all(&dir).unwrap();
        let mut m = make_meta("do");
        m.mic_offset = 1000;
        m.loopback_offset = 2000;
        m.save(&dir).unwrap();
        let l = RecoveryMeta::load(&dir).unwrap();
        assert_eq!(l.mic_offset, 1000);
        assert_eq!(l.loopback_offset, 2000);
        std::fs::remove_dir_all(&dir).ok();
    }
}
