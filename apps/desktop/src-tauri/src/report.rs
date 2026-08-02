use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use thiserror::Error;

const MAX_JSON_SIZE: usize = 10 * 1024 * 1024; // 10 MiB
const MAX_MARKDOWN_SIZE: usize = 5 * 1024 * 1024; // 5 MiB
const MAX_HTML_SIZE: usize = 10 * 1024 * 1024; // 10 MiB

#[derive(Debug, Error)]
pub enum ReportError {
    #[error("invalid session id")]
    InvalidSessionId,
    #[error("session not found: {0}")]
    SessionNotFound(String),
    #[error("json too large: {size} bytes (max {max})")]
    JsonTooLarge { size: usize, max: usize },
    #[error("markdown too large: {size} bytes (max {max})")]
    MarkdownTooLarge { size: usize, max: usize },
    #[error("html too large: {size} bytes (max {max})")]
    HtmlTooLarge { size: usize, max: usize },
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("json validation error: {0}")]
    JsonValidation(String),
    #[error("database error: {0}")]
    Database(String),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SavedReportFiles {
    pub json_path: PathBuf,
    pub markdown_path: PathBuf,
    pub html_path: PathBuf,
}

/// Validate that the JSON string is parseable.
fn validate_json(json_str: &str) -> Result<(), ReportError> {
    serde_json::from_str::<serde_json::Value>(json_str)
        .map_err(|e| ReportError::JsonValidation(e.to_string()))?;
    Ok(())
}

/// Atomically write a file: write to .tmp then rename.
fn atomic_write(path: &Path, contents: &[u8]) -> Result<(), ReportError> {
    let tmp_path = path.with_extension(format!(
        "{}.tmp",
        path.extension().unwrap_or_default().to_string_lossy()
    ));
    std::fs::write(&tmp_path, contents)?;
    std::fs::rename(&tmp_path, path)?;
    Ok(())
}

/// Save report files for a session.
pub fn save_report_files_impl(
    local_session_id: &str,
    analysis_json: &str,
    markdown: &str,
    html: &str,
    sessions_dir: &Path,
    db: &crate::db::Repository,
) -> Result<SavedReportFiles, ReportError> {
    // Validate session ID
    if local_session_id.is_empty()
        || !local_session_id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-')
    {
        return Err(ReportError::InvalidSessionId);
    }

    // Validate path and confirm session exists in DB
    let session_dir = crate::paths::validate_session_path(local_session_id, sessions_dir)
        .map_err(|_| ReportError::InvalidSessionId)?;

    // Ensure session exists in database
    db.get_session(local_session_id)
        .map_err(|_| ReportError::SessionNotFound(local_session_id.to_string()))?;

    // Enforce size limits
    if analysis_json.len() > MAX_JSON_SIZE {
        return Err(ReportError::JsonTooLarge {
            size: analysis_json.len(),
            max: MAX_JSON_SIZE,
        });
    }
    if markdown.len() > MAX_MARKDOWN_SIZE {
        return Err(ReportError::MarkdownTooLarge {
            size: markdown.len(),
            max: MAX_MARKDOWN_SIZE,
        });
    }
    if html.len() > MAX_HTML_SIZE {
        return Err(ReportError::HtmlTooLarge {
            size: html.len(),
            max: MAX_HTML_SIZE,
        });
    }

    // Validate JSON is parseable
    validate_json(analysis_json)?;

    // Ensure session directory exists
    std::fs::create_dir_all(&session_dir)?;

    let json_path = session_dir.join("report.json");
    let markdown_path = session_dir.join("report.md");
    let html_path = session_dir.join("report.html");

    // Atomic writes: if any fails, clean up previously written files
    let mut written: Vec<&Path> = Vec::new();

    match atomic_write(&json_path, analysis_json.as_bytes()) {
        Ok(()) => written.push(&json_path),
        Err(e) => {
            cleanup_tmp(&json_path);
            return Err(e);
        }
    }

    match atomic_write(&markdown_path, markdown.as_bytes()) {
        Ok(()) => written.push(&markdown_path),
        Err(e) => {
            cleanup_tmp(&markdown_path);
            for p in written {
                std::fs::remove_file(p).ok();
            }
            return Err(e);
        }
    }

    match atomic_write(&html_path, html.as_bytes()) {
        Ok(()) => {}
        Err(e) => {
            cleanup_tmp(&html_path);
            for p in written {
                std::fs::remove_file(p).ok();
            }
            return Err(e);
        }
    }

    // Update SQLite with the analysis JSON
    let now = chrono::Utc::now().to_rfc3339();
    let analysis_result = crate::db::AnalysisResult {
        id: uuid::Uuid::new_v4().to_string(),
        session_id: local_session_id.to_string(),
        analysis_type: "report".to_string(),
        content_json: analysis_json.to_string(),
        created_at: now,
    };
    db.save_analysis_results(local_session_id, &[analysis_result], &[])
        .map_err(|e| ReportError::Database(e.to_string()))?;

    Ok(SavedReportFiles {
        json_path,
        markdown_path,
        html_path,
    })
}

/// Clean up .tmp files that may have been left behind.
fn cleanup_tmp(path: &Path) {
    let tmp_path = path.with_extension(format!(
        "{}.tmp",
        path.extension().unwrap_or_default().to_string_lossy()
    ));
    std::fs::remove_file(tmp_path).ok();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_json_valid() {
        assert!(validate_json(r#"{"key":"value"}"#).is_ok());
        assert!(validate_json("[]").is_ok());
        assert!(validate_json("null").is_ok());
    }

    #[test]
    fn test_validate_json_invalid() {
        assert!(validate_json("{invalid}").is_err());
        assert!(validate_json("").is_err());
    }

    #[test]
    fn test_atomic_write_creates_file() {
        let dir = std::env::temp_dir().join("memecho_report_test_atomic");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("test.txt");
        atomic_write(&path, b"hello").unwrap();
        assert!(path.exists());
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "hello");
        assert!(!dir.join("test.txt.tmp").exists());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_size_limits() {
        // validate_json only checks JSON syntax, not size
        // Size limits are enforced in save_report_files_impl
        assert!(validate_json(r#"{"key":"value"}"#).is_ok());

        // Verify the constants are reasonable
        assert_eq!(MAX_JSON_SIZE, 10 * 1024 * 1024); // 10 MiB
        assert_eq!(MAX_MARKDOWN_SIZE, 5 * 1024 * 1024); // 5 MiB
        assert_eq!(MAX_HTML_SIZE, 10 * 1024 * 1024); // 10 MiB
    }
}
