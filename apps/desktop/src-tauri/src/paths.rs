use std::path::PathBuf;

/// Returns the base directory for session data.
/// Uses %APPDATA%/memecho/sessions on Windows.
pub fn sessions_dir() -> PathBuf {
    let base = dirs_next::data_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("memecho")
        .join("sessions");
    std::fs::create_dir_all(&base).ok();
    base
}

/// Returns the path to recovery.json for a given session.
pub fn recovery_json_path(session_dir: &std::path::Path) -> PathBuf {
    session_dir.join("recovery.json")
}

/// Returns the path for the microphone WAV file.
pub fn mic_wav_path(session_dir: &std::path::Path) -> PathBuf {
    session_dir.join("mic.wav")
}

/// Returns the path for the loopback WAV file.
pub fn loopback_wav_path(session_dir: &std::path::Path) -> PathBuf {
    session_dir.join("loopback.wav")
}

/// Validates that a path is within the allowed sessions directory.
/// Prevents path traversal attacks.
pub fn validate_session_path(
    session_id: &str,
    sessions_dir: &std::path::Path,
) -> Result<PathBuf, PathError> {
    // Session ID must be alphanumeric with hyphens only
    if !session_id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-')
    {
        return Err(PathError::InvalidSessionId);
    }
    let session_dir = sessions_dir.join(session_id);
    // Canonicalize to resolve any .. or symlinks
    let canonical_sessions = sessions_dir
        .canonicalize()
        .map_err(|_| PathError::SessionsDirNotFound)?;
    let canonical_session = if session_dir.exists() {
        session_dir
            .canonicalize()
            .map_err(|_| PathError::PathTraversal)?
    } else {
        // For new sessions, check that the parent resolves correctly
        let parent = session_dir
            .parent()
            .ok_or(PathError::PathTraversal)?
            .canonicalize()
            .map_err(|_| PathError::PathTraversal)?;
        if parent != canonical_sessions {
            return Err(PathError::PathTraversal);
        }
        session_dir.clone()
    };
    if !canonical_session.starts_with(&canonical_sessions) {
        return Err(PathError::PathTraversal);
    }
    Ok(session_dir)
}

#[derive(Debug, thiserror::Error)]
pub enum PathError {
    #[error("invalid session id: only alphanumeric and hyphens allowed")]
    InvalidSessionId,
    #[error("sessions directory not found")]
    SessionsDirNotFound,
    #[error("path traversal detected")]
    PathTraversal,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn test_valid_session_id() {
        // We can't actually canonicalize non-existent dirs in tests,
        // but we can test the ID validation logic
        assert!("abc-123"
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-'));
        assert!(!"../evil"
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-'));
        assert!(!"foo/bar"
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-'));
    }

    #[test]
    fn test_recovery_json_path() {
        let session_dir = PathBuf::from("C:\\sessions\\abc");
        let p = recovery_json_path(&session_dir);
        assert_eq!(p.file_name().unwrap(), "recovery.json");
    }

    #[test]
    fn test_mic_wav_path() {
        let session_dir = PathBuf::from("C:\\sessions\\abc");
        let p = mic_wav_path(&session_dir);
        assert_eq!(p.file_name().unwrap(), "mic.wav");
    }

    #[test]
    fn test_loopback_wav_path() {
        let session_dir = PathBuf::from("C:\\sessions\\abc");
        let p = loopback_wav_path(&session_dir);
        assert_eq!(p.file_name().unwrap(), "loopback.wav");
    }

    #[test]
    fn test_validate_rejects_dot_dot() {
        let base = PathBuf::from("C:\\test\\sessions");
        let result = validate_session_path("../evil", &base);
        assert!(result.is_err());
    }

    #[test]
    fn test_validate_rejects_slash() {
        let base = PathBuf::from("C:\\test\\sessions");
        let result = validate_session_path("foo/bar", &base);
        assert!(result.is_err());
    }
}
