use crate::db::{self, LocalSession, Repository};
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use thiserror::Error;

pub const MAX_MEDIA_IMPORT_BYTES: u64 = 4 * 1024 * 1024 * 1024;
pub const MAX_TEXT_IMPORT_BYTES: usize = 5 * 1024 * 1024;

const COPY_BUFFER_BYTES: usize = 1024 * 1024;
const MAX_TITLE_CHARS: usize = 120;
const MAX_SOURCE_NAME_CHARS: usize = 255;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ImportSourceKind {
    Media,
    Text,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImportedSource {
    pub kind: ImportSourceKind,
    pub path: String,
    pub original_name: String,
    pub mime_type: String,
    pub size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImportedSession {
    pub session: LocalSession,
    pub source: ImportedSource,
}

#[derive(Debug, Error)]
pub enum ImportError {
    #[error("source path must not be empty")]
    EmptyPath,
    #[error("source path must be absolute")]
    RelativePath,
    #[error("source file was not found")]
    SourceNotFound,
    #[error("source path must point to a regular file")]
    SourceNotFile,
    #[error("symbolic-link imports are not allowed")]
    SymlinkNotAllowed,
    #[error("source path must be outside memEcho's private sessions directory")]
    SourceInsideSessions,
    #[error("unsupported media extension; allowed: mp3, wav, m4a, mp4")]
    UnsupportedExtension,
    #[error("media file must not be empty")]
    EmptyMedia,
    #[error("media file exceeds the {max_bytes} byte import limit")]
    MediaTooLarge { max_bytes: u64 },
    #[error("text import must contain non-whitespace UTF-8 text")]
    EmptyText,
    #[error("text import exceeds the {max_bytes} byte import limit")]
    TextTooLarge { max_bytes: usize },
    #[error("title must contain 1 to {max_chars} non-control characters")]
    InvalidTitle { max_chars: usize },
    #[error("source name must be a plain file name with at most {max_chars} characters")]
    InvalidSourceName { max_chars: usize },
    #[error("source file changed while it was being copied")]
    SourceChanged,
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("database error: {0}")]
    Database(#[from] db::DbError),
    #[error("path validation error: {0}")]
    Path(String),
}

#[derive(Debug)]
struct ValidatedMedia {
    canonical_path: PathBuf,
    extension: String,
    mime_type: &'static str,
    original_name: String,
    size: u64,
}

pub fn import_media_file_impl(
    source_path: &str,
    title: Option<&str>,
    sessions_dir: &Path,
    db: &Repository,
) -> Result<ImportedSession, ImportError> {
    fs::create_dir_all(sessions_dir)?;
    let media = validate_media_source(source_path, sessions_dir)?;
    let title = normalize_title(title, media.canonical_path.file_stem(), "Imported media")?;
    let (session_id, session_dir) = create_private_session_dir(sessions_dir)?;
    let destination = session_dir.join(format!("import.{}", media.extension));

    if let Err(error) = copy_file_atomically(
        &media.canonical_path,
        &destination,
        media.size,
        MAX_MEDIA_IMPORT_BYTES,
    ) {
        cleanup_private_session_dir(&session_dir);
        return Err(error);
    }

    let destination_string = destination.to_string_lossy().into_owned();
    let session = match db.create_import_session(
        &session_id,
        Some(&title),
        &destination_string,
        &media.original_name,
        media.mime_type,
        media.size,
    ) {
        Ok(session) => session,
        Err(error) => {
            cleanup_private_session_dir(&session_dir);
            return Err(error.into());
        }
    };

    Ok(ImportedSession {
        session,
        source: ImportedSource {
            kind: ImportSourceKind::Media,
            path: destination_string,
            original_name: media.original_name,
            mime_type: media.mime_type.to_string(),
            size: media.size,
        },
    })
}

pub fn import_text_content_impl(
    text: &str,
    title: Option<&str>,
    source_name: Option<&str>,
    sessions_dir: &Path,
    db: &Repository,
) -> Result<ImportedSession, ImportError> {
    validate_text_size(text)?;
    fs::create_dir_all(sessions_dir)?;

    let original_name = normalize_source_name(source_name.unwrap_or("pasted-text.txt"))?;
    let fallback_stem = Path::new(&original_name).file_stem();
    let title = normalize_title(title, fallback_stem, "Imported text")?;
    let (session_id, session_dir) = create_private_session_dir(sessions_dir)?;
    let destination = session_dir.join("source.txt");

    if let Err(error) = write_text_atomically(&destination, text.as_bytes()) {
        cleanup_private_session_dir(&session_dir);
        return Err(error);
    }

    let destination_string = destination.to_string_lossy().into_owned();
    let size = text.len() as u64;
    let session = match db.create_import_session(
        &session_id,
        Some(&title),
        &destination_string,
        &original_name,
        "text/plain; charset=utf-8",
        size,
    ) {
        Ok(session) => session,
        Err(error) => {
            cleanup_private_session_dir(&session_dir);
            return Err(error.into());
        }
    };

    Ok(ImportedSession {
        session,
        source: ImportedSource {
            kind: ImportSourceKind::Text,
            path: destination_string,
            original_name,
            mime_type: "text/plain; charset=utf-8".to_string(),
            size,
        },
    })
}

pub fn media_mime_type(path: &Path) -> Result<&'static str, ImportError> {
    match path
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("mp3") => Ok("audio/mpeg"),
        Some("wav") => Ok("audio/wav"),
        Some("m4a") => Ok("audio/mp4"),
        Some("mp4") => Ok("video/mp4"),
        _ => Err(ImportError::UnsupportedExtension),
    }
}

pub fn find_import_media(session_dir: &Path) -> Result<Option<PathBuf>, ImportError> {
    let mut matches = Vec::new();
    for entry in fs::read_dir(session_dir)? {
        let entry = entry?;
        let path = entry.path();
        if !entry.file_type()?.is_file() {
            continue;
        }
        let is_import_name = path
            .file_stem()
            .and_then(|value| value.to_str())
            .is_some_and(|value| value.eq_ignore_ascii_case("import"));
        if is_import_name && media_mime_type(&path).is_ok() {
            matches.push(path);
        }
    }
    matches.sort();
    match matches.len() {
        0 => Ok(None),
        1 => Ok(matches.pop()),
        _ => Err(ImportError::Path(
            "multiple import media files exist in one session".to_string(),
        )),
    }
}

fn validate_media_source(
    source_path: &str,
    sessions_dir: &Path,
) -> Result<ValidatedMedia, ImportError> {
    if source_path.trim().is_empty() {
        return Err(ImportError::EmptyPath);
    }
    let source = Path::new(source_path);
    if !source.is_absolute() {
        return Err(ImportError::RelativePath);
    }

    let symlink_metadata = match fs::symlink_metadata(source) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Err(ImportError::SourceNotFound)
        }
        Err(error) => return Err(error.into()),
    };
    if symlink_metadata.file_type().is_symlink() {
        return Err(ImportError::SymlinkNotAllowed);
    }
    if !symlink_metadata.is_file() {
        return Err(ImportError::SourceNotFile);
    }

    let canonical_source = source.canonicalize()?;
    let canonical_sessions = sessions_dir.canonicalize()?;
    if canonical_source.starts_with(&canonical_sessions) {
        return Err(ImportError::SourceInsideSessions);
    }

    let mime_type = media_mime_type(&canonical_source)?;
    validate_media_size(symlink_metadata.len())?;
    let extension = canonical_source
        .extension()
        .and_then(|value| value.to_str())
        .ok_or(ImportError::UnsupportedExtension)?
        .to_ascii_lowercase();
    let original_name = normalize_source_name(
        &canonical_source
            .file_name()
            .map(|value| value.to_string_lossy().into_owned())
            .unwrap_or_else(|| format!("import.{extension}")),
    )?;

    Ok(ValidatedMedia {
        canonical_path: canonical_source,
        extension,
        mime_type,
        original_name,
        size: symlink_metadata.len(),
    })
}

fn validate_media_size(size: u64) -> Result<(), ImportError> {
    if size == 0 {
        return Err(ImportError::EmptyMedia);
    }
    if size > MAX_MEDIA_IMPORT_BYTES {
        return Err(ImportError::MediaTooLarge {
            max_bytes: MAX_MEDIA_IMPORT_BYTES,
        });
    }
    Ok(())
}

fn validate_text_size(text: &str) -> Result<(), ImportError> {
    if text.trim().is_empty() {
        return Err(ImportError::EmptyText);
    }
    if text.len() > MAX_TEXT_IMPORT_BYTES {
        return Err(ImportError::TextTooLarge {
            max_bytes: MAX_TEXT_IMPORT_BYTES,
        });
    }
    Ok(())
}

fn normalize_title(
    requested: Option<&str>,
    fallback_stem: Option<&std::ffi::OsStr>,
    final_fallback: &str,
) -> Result<String, ImportError> {
    let candidate = match requested {
        Some(value) => value.trim().to_string(),
        None => fallback_stem
            .map(|value| value.to_string_lossy().trim().to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| final_fallback.to_string()),
    };
    let count = candidate.chars().count();
    if count == 0
        || count > MAX_TITLE_CHARS
        || candidate.chars().any(|character| character.is_control())
    {
        return Err(ImportError::InvalidTitle {
            max_chars: MAX_TITLE_CHARS,
        });
    }
    Ok(candidate)
}

fn normalize_source_name(value: &str) -> Result<String, ImportError> {
    let trimmed = value.trim();
    let path = Path::new(trimmed);
    let is_plain_name = path
        .file_name()
        .is_some_and(|name| name == path.as_os_str());
    if trimmed.is_empty()
        || trimmed.chars().count() > MAX_SOURCE_NAME_CHARS
        || trimmed.chars().any(|character| character.is_control())
        || !is_plain_name
    {
        return Err(ImportError::InvalidSourceName {
            max_chars: MAX_SOURCE_NAME_CHARS,
        });
    }
    Ok(trimmed.to_string())
}

fn create_private_session_dir(sessions_dir: &Path) -> Result<(String, PathBuf), ImportError> {
    let canonical_sessions = sessions_dir.canonicalize()?;
    for _ in 0..3 {
        let session_id = uuid::Uuid::new_v4().to_string();
        let session_dir = canonical_sessions.join(&session_id);
        match fs::create_dir(&session_dir) {
            Ok(()) => {
                return Ok((session_id, session_dir));
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.into()),
        }
    }
    Err(ImportError::Path(
        "failed to allocate a unique local session directory".to_string(),
    ))
}

fn copy_file_atomically(
    source: &Path,
    destination: &Path,
    expected_size: u64,
    max_size: u64,
) -> Result<(), ImportError> {
    let temporary = destination.with_file_name(format!(
        ".{}.part",
        destination
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("import")
    ));
    let result = (|| {
        let mut input = File::open(source)?;
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        let mut buffer = vec![0_u8; COPY_BUFFER_BYTES];
        let mut copied = 0_u64;
        loop {
            let read = input.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            copied = copied.saturating_add(read as u64);
            if copied > max_size {
                return Err(ImportError::MediaTooLarge {
                    max_bytes: max_size,
                });
            }
            output.write_all(&buffer[..read])?;
        }
        if copied != expected_size {
            return Err(ImportError::SourceChanged);
        }
        output.sync_all()?;
        drop(output);
        fs::rename(&temporary, destination)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn write_text_atomically(destination: &Path, bytes: &[u8]) -> Result<(), ImportError> {
    let temporary = destination.with_file_name(".source.txt.part");
    let result = (|| {
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        output.write_all(bytes)?;
        output.sync_all()?;
        drop(output);
        fs::rename(&temporary, destination)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn cleanup_private_session_dir(session_dir: &Path) {
    let _ = fs::remove_dir_all(session_dir);
}
#[cfg(test)]
mod tests {
    use super::*;

    fn test_layout() -> (PathBuf, PathBuf, PathBuf, Repository) {
        let root = std::env::temp_dir().join(format!("memecho_import_{}", uuid::Uuid::new_v4()));
        let source_dir = root.join("sources");
        let sessions_dir = root.join("sessions");
        fs::create_dir_all(&source_dir).unwrap();
        fs::create_dir_all(&sessions_dir).unwrap();
        let db = Repository::open(&sessions_dir.join("memecho.db")).unwrap();
        (root, source_dir, sessions_dir, db)
    }

    #[test]
    fn media_types_and_size_limits_are_strict() {
        assert_eq!(
            media_mime_type(Path::new("audio.MP3")).unwrap(),
            "audio/mpeg"
        );
        assert_eq!(
            media_mime_type(Path::new("audio.wav")).unwrap(),
            "audio/wav"
        );
        assert_eq!(
            media_mime_type(Path::new("audio.m4a")).unwrap(),
            "audio/mp4"
        );
        assert_eq!(
            media_mime_type(Path::new("video.mp4")).unwrap(),
            "video/mp4"
        );
        assert!(matches!(
            media_mime_type(Path::new("notes.txt")),
            Err(ImportError::UnsupportedExtension)
        ));
        assert!(matches!(
            validate_media_size(0),
            Err(ImportError::EmptyMedia)
        ));
        assert!(validate_media_size(MAX_MEDIA_IMPORT_BYTES).is_ok());
        assert!(matches!(
            validate_media_size(MAX_MEDIA_IMPORT_BYTES + 1),
            Err(ImportError::MediaTooLarge { .. })
        ));
    }

    #[test]
    fn imports_media_to_private_session_and_registers_database() {
        let (root, source_dir, sessions_dir, db) = test_layout();
        let source = source_dir.join("Meeting.MP3");
        fs::write(&source, b"authorized-audio").unwrap();

        let imported = import_media_file_impl(
            source.to_str().unwrap(),
            Some("Weekly review"),
            &sessions_dir,
            &db,
        )
        .unwrap();

        assert_eq!(imported.source.kind, ImportSourceKind::Media);
        assert_eq!(imported.source.original_name, "Meeting.MP3");
        assert_eq!(imported.source.mime_type, "audio/mpeg");
        assert_eq!(
            fs::read(&imported.source.path).unwrap(),
            b"authorized-audio"
        );
        assert!(Path::new(&imported.source.path).starts_with(sessions_dir.canonicalize().unwrap()));
        assert!(!imported.source.path.contains(source_dir.to_str().unwrap()));
        assert_eq!(imported.session.source_mode, "import");
        assert_eq!(
            imported.session.source_path.as_deref(),
            Some(imported.source.path.as_str())
        );
        assert_eq!(imported.session.sample_rate, 0);
        assert_eq!(
            db.get_session(&imported.session.id)
                .unwrap()
                .source_size_bytes,
            Some(16)
        );

        drop(db);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn rejects_relative_unsupported_and_private_source_paths() {
        let (root, source_dir, sessions_dir, db) = test_layout();
        assert!(matches!(
            import_media_file_impl("relative.mp3", None, &sessions_dir, &db),
            Err(ImportError::RelativePath)
        ));

        let unsupported = source_dir.join("meeting.exe");
        fs::write(&unsupported, b"not-media").unwrap();
        assert!(matches!(
            import_media_file_impl(unsupported.to_str().unwrap(), None, &sessions_dir, &db),
            Err(ImportError::UnsupportedExtension)
        ));

        let private_source = sessions_dir.join("private.mp3");
        fs::write(&private_source, b"private").unwrap();
        assert!(matches!(
            import_media_file_impl(private_source.to_str().unwrap(), None, &sessions_dir, &db),
            Err(ImportError::SourceInsideSessions)
        ));

        drop(db);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn imports_text_as_exact_utf8_and_registers_database() {
        let (root, _source_dir, sessions_dir, db) = test_layout();
        let text = "我先陈述事实。\n再说明观点与态度。";
        let imported = import_text_content_impl(
            text,
            Some("冲突复盘"),
            Some("meeting-notes.txt"),
            &sessions_dir,
            &db,
        )
        .unwrap();

        assert_eq!(imported.source.kind, ImportSourceKind::Text);
        assert_eq!(imported.source.original_name, "meeting-notes.txt");
        assert_eq!(imported.source.mime_type, "text/plain; charset=utf-8");
        assert_eq!(fs::read_to_string(&imported.source.path).unwrap(), text);
        assert_eq!(imported.session.source_mode, "import");
        assert_eq!(
            imported.session.source_mime_type.as_deref(),
            Some("text/plain; charset=utf-8")
        );

        drop(db);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn rejects_empty_large_text_and_path_like_source_names() {
        let (root, _source_dir, sessions_dir, db) = test_layout();
        assert!(matches!(
            import_text_content_impl(" \n ", None, None, &sessions_dir, &db),
            Err(ImportError::EmptyText)
        ));
        assert!(matches!(
            validate_text_size(&"x".repeat(MAX_TEXT_IMPORT_BYTES + 1)),
            Err(ImportError::TextTooLarge { .. })
        ));
        assert!(matches!(
            import_text_content_impl("valid", None, Some("../notes.txt"), &sessions_dir, &db),
            Err(ImportError::InvalidSourceName { .. })
        ));

        drop(db);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn finds_one_import_media_and_rejects_ambiguous_session() {
        let root = std::env::temp_dir().join(format!("memecho_find_{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        assert!(find_import_media(&root).unwrap().is_none());
        fs::write(root.join("import.mp4"), b"video").unwrap();
        assert_eq!(
            find_import_media(&root)
                .unwrap()
                .unwrap()
                .file_name()
                .unwrap(),
            "import.mp4"
        );
        fs::write(root.join("import.wav"), b"audio").unwrap();
        assert!(find_import_media(&root).is_err());
        fs::remove_dir_all(root).ok();
    }
}
