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

/// Write contents to a temp file in the same directory as `path`.
/// Returns the temp path on success so the caller can manage it.
fn write_to_tmp(path: &Path, contents: &[u8]) -> Result<PathBuf, ReportError> {
    let ext = path
        .extension()
        .map(|e| e.to_string_lossy().into_owned())
        .unwrap_or_default();
    let tmp_path = path.with_extension(format!("{}.tmp", ext));
    std::fs::write(&tmp_path, contents)?;
    Ok(tmp_path)
}

/// Windows-safe file replacement: back up the existing file (if any),
/// then rename the new file into place.  Returns the backup path if
/// one was created so the caller can restore on failure.
fn replace_with_backup(new_path: &Path, final_path: &Path) -> Result<Option<PathBuf>, ReportError> {
    let bak_path = final_path.with_extension(format!(
        "{}.bak",
        final_path
            .extension()
            .map(|e| e.to_string_lossy().into_owned())
            .unwrap_or_default()
    ));
    let had_existing = final_path.exists();
    if had_existing {
        std::fs::rename(final_path, &bak_path)?;
    }
    if let Err(e) = std::fs::rename(new_path, final_path) {
        // Restore backup before returning error
        if had_existing {
            let _ = std::fs::rename(&bak_path, final_path);
        }
        return Err(e.into());
    }
    Ok(if had_existing { Some(bak_path) } else { None })
}

/// Restore a file from its .bak backup, deleting the (possibly partial) new version.
fn restore_from_backup(final_path: &Path, bak_path: &Path) {
    // Remove the partial new file; ignore errors (may not exist)
    let _ = std::fs::remove_file(final_path);
    let _ = std::fs::rename(bak_path, final_path);
}

/// Clean up .tmp and .bak artifacts that may have been left behind.
fn cleanup_artifacts(base_path: &Path) {
    let ext = base_path
        .extension()
        .map(|e| e.to_string_lossy().into_owned())
        .unwrap_or_default();
    let _ = std::fs::remove_file(base_path.with_extension(format!("{}.tmp", ext)));
    let _ = std::fs::remove_file(base_path.with_extension(format!("{}.bak", ext)));
}

/// Save report files for a session.
///
/// Uses same-directory temp files and a backup/rollback strategy so that:
/// - Saving a second time replaces the prior artifacts without rename failures.
/// - If a later step fails, the previous valid report is restored from backups.
/// - No temp/backup files are left after success.
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

    // Write all three to temp files first (atomic: no final paths touched yet)
    let json_tmp = write_to_tmp(&json_path, analysis_json.as_bytes()).map_err(|e| {
        cleanup_artifacts(&json_path);
        e
    })?;
    let md_tmp = write_to_tmp(&markdown_path, markdown.as_bytes()).map_err(|e| {
        cleanup_artifacts(&json_path);
        cleanup_artifacts(&markdown_path);
        e
    })?;
    let html_tmp = write_to_tmp(&html_path, html.as_bytes()).map_err(|e| {
        cleanup_artifacts(&json_path);
        cleanup_artifacts(&markdown_path);
        cleanup_artifacts(&html_path);
        e
    })?;

    // Replace each final path, collecting backups for rollback on failure
    let mut backups: Vec<(PathBuf, PathBuf)> = Vec::new(); // (final, bak)

    match replace_with_backup(&json_tmp, &json_path) {
        Ok(bak) => {
            if let Some(b) = bak {
                backups.push((json_path.clone(), b));
            }
        }
        Err(e) => {
            cleanup_artifacts(&json_path);
            cleanup_artifacts(&markdown_path);
            cleanup_artifacts(&html_path);
            return Err(e);
        }
    }

    match replace_with_backup(&md_tmp, &markdown_path) {
        Ok(bak) => {
            if let Some(b) = bak {
                backups.push((markdown_path.clone(), b));
            }
        }
        Err(e) => {
            // Rollback: restore json_path from its backup
            for (final_p, bak_p) in backups.iter().rev() {
                restore_from_backup(final_p, bak_p);
            }
            cleanup_artifacts(&json_path);
            cleanup_artifacts(&markdown_path);
            cleanup_artifacts(&html_path);
            return Err(e);
        }
    }

    match replace_with_backup(&html_tmp, &html_path) {
        Ok(bak) => {
            if let Some(b) = bak {
                backups.push((html_path.clone(), b));
            }
        }
        Err(e) => {
            // Rollback: restore all previous backups
            for (final_p, bak_p) in backups.iter().rev() {
                restore_from_backup(final_p, bak_p);
            }
            cleanup_artifacts(&json_path);
            cleanup_artifacts(&markdown_path);
            cleanup_artifacts(&html_path);
            return Err(e);
        }
    }

    // All three written successfully — clean up backups
    for (final_p, _) in &backups {
        cleanup_artifacts(final_p);
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
    fn test_write_to_tmp_creates_file() {
        let dir = std::env::temp_dir().join("memecho_report_test_atomic");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("test.txt");
        let tmp = write_to_tmp(&path, b"hello").unwrap();
        assert!(tmp.exists());
        assert_eq!(std::fs::read_to_string(&tmp).unwrap(), "hello");
        // Final path not yet created
        assert!(!path.exists());
        // Replace into place
        let bak = replace_with_backup(&tmp, &path).unwrap();
        assert!(path.exists());
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "hello");
        assert!(!dir.join("test.txt.tmp").exists());
        assert!(bak.is_none());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_replace_overwrites_existing() {
        let dir = std::env::temp_dir().join("memecho_report_test_replace");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("data.txt");
        std::fs::write(&path, b"old").unwrap();
        let tmp = write_to_tmp(&path, b"new").unwrap();
        let bak = replace_with_backup(&tmp, &path).unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "new");
        assert!(bak.is_some());
        // Clean up backup
        cleanup_artifacts(&path);
        assert!(!dir.join("data.txt.bak").exists());
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
