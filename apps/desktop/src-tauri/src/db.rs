use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::sync::Mutex;

const SCHEMA_VERSION: i32 = 1;

const SCHEMA_SQL: &str = "
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    mic_path TEXT,
    loopback_path TEXT,
    sample_rate INTEGER NOT NULL DEFAULT 16000,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_secs REAL,
    recovery_status TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    name TEXT,
    role TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    speaker_id TEXT REFERENCES participants(id) ON DELETE SET NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    confidence REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_results (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    analysis_type TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_candidates (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    segment_id TEXT REFERENCES transcript_segments(id) ON DELETE SET NULL,
    content TEXT NOT NULL,
    score REAL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_relations (
    id TEXT PRIMARY KEY,
    source_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    target_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_participants_session ON participants(session_id);
CREATE INDEX IF NOT EXISTS idx_segments_session ON transcript_segments(session_id);
CREATE INDEX IF NOT EXISTS idx_analysis_session ON analysis_results(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_session ON memory_candidates(session_id);
CREATE INDEX IF NOT EXISTS idx_source_rel_source ON source_relations(source_session_id);
CREATE INDEX IF NOT EXISTS idx_source_rel_target ON source_relations(target_session_id);
";

// ── Data types ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalSession {
    pub id: String,
    pub title: Option<String>,
    pub status: String,
    pub mic_path: Option<String>,
    pub loopback_path: Option<String>,
    pub sample_rate: i64,
    pub started_at: String,
    pub ended_at: Option<String>,
    pub duration_secs: Option<f64>,
    pub recovery_status: Option<String>,
    pub error_code: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Participant {
    pub id: String,
    pub session_id: String,
    pub name: Option<String>,
    pub role: Option<String>,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TranscriptSegment {
    pub id: String,
    pub session_id: String,
    pub speaker_id: Option<String>,
    pub start_ms: i64,
    pub end_ms: i64,
    pub text: String,
    pub confidence: Option<f64>,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnalysisResult {
    pub id: String,
    pub session_id: String,
    pub analysis_type: String,
    pub content_json: String,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryCandidate {
    pub id: String,
    pub session_id: String,
    pub segment_id: Option<String>,
    pub content: String,
    pub score: Option<f64>,
    pub confirmed: bool,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceRelation {
    pub id: String,
    pub source_session_id: String,
    pub target_session_id: String,
    pub relation_type: String,
    pub metadata_json: Option<String>,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnalysisResultsBundle {
    pub results: Vec<AnalysisResult>,
    pub memory_candidates: Vec<MemoryCandidate>,
}

#[derive(Debug, thiserror::Error)]
pub enum DbError {
    #[error("database error: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("path error: {0}")]
    Path(String),
    #[error("session not found: {0}")]
    SessionNotFound(String),
    #[error("invalid session id")]
    InvalidSessionId,
    #[error("path traversal detected")]
    PathTraversal,
    #[error("foreign key violation: {0}")]
    ForeignKeyViolation(String),
}

// ── Repository ──────────────────────────────────────────────────────────────

pub struct Repository {
    conn: Mutex<Connection>,
}

impl Repository {
    pub fn open(db_path: &Path) -> Result<Self, DbError> {
        let conn = Connection::open(db_path)?;
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")?;
        let repo = Repository {
            conn: Mutex::new(conn),
        };
        repo.migrate()?;
        Ok(repo)
    }

    fn migrate(&self) -> Result<(), DbError> {
        let conn = self.conn.lock().unwrap();
        let current: i32 = conn
            .query_row(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version",
                [],
                |row| row.get(0),
            )
            .unwrap_or(0);

        if current == 0 {
            conn.execute_batch(SCHEMA_SQL)?;
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?1)",
                params![SCHEMA_VERSION],
            )?;
        } else if current < SCHEMA_VERSION {
            // Future migrations go here
            conn.execute(
                "UPDATE schema_version SET version = ?1",
                params![SCHEMA_VERSION],
            )?;
        }
        Ok(())
    }

    // ── Sessions CRUD ───────────────────────────────────────────────────────

    pub fn create_session(
        &self,
        id: &str,
        title: Option<&str>,
        mic_path: Option<&str>,
        loopback_path: Option<&str>,
        sample_rate: i64,
        recovery_status: Option<&str>,
    ) -> Result<LocalSession, DbError> {
        let now = chrono::Utc::now().to_rfc3339();
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO sessions (id, title, status, mic_path, loopback_path, sample_rate, started_at, recovery_status, created_at, updated_at) VALUES (?1, ?2, 'active', ?3, ?4, ?5, ?6, ?7, ?8, ?8)",
            params![id, title, mic_path, loopback_path, sample_rate, now, recovery_status, now],
        )?;
        drop(conn);
        self.get_session(id)
    }

    pub fn update_session(
        &self,
        id: &str,
        title: Option<&str>,
        status: Option<&str>,
        ended_at: Option<&str>,
        duration_secs: Option<f64>,
        recovery_status: Option<&str>,
        error_code: Option<&str>,
    ) -> Result<LocalSession, DbError> {
        let now = chrono::Utc::now().to_rfc3339();
        let conn = self.conn.lock().unwrap();
        let changed = conn.execute(
            "UPDATE sessions SET title = COALESCE(?2, title), status = COALESCE(?3, status), ended_at = COALESCE(?4, ended_at), duration_secs = COALESCE(?5, duration_secs), recovery_status = COALESCE(?6, recovery_status), error_code = COALESCE(?7, error_code), updated_at = ?8 WHERE id = ?1",
            params![id, title, status, ended_at, duration_secs, recovery_status, error_code, now],
        )?;
        if changed == 0 {
            return Err(DbError::SessionNotFound(id.to_string()));
        }
        drop(conn);
        self.get_session(id)
    }

    pub fn get_session(&self, id: &str) -> Result<LocalSession, DbError> {
        let conn = self.conn.lock().unwrap();
        conn.query_row(
            "SELECT id, title, status, mic_path, loopback_path, sample_rate, started_at, ended_at, duration_secs, recovery_status, error_code, created_at, updated_at FROM sessions WHERE id = ?1",
            params![id],
            |row| {
                Ok(LocalSession {
                    id: row.get(0)?,
                    title: row.get(1)?,
                    status: row.get(2)?,
                    mic_path: row.get(3)?,
                    loopback_path: row.get(4)?,
                    sample_rate: row.get(5)?,
                    started_at: row.get(6)?,
                    ended_at: row.get(7)?,
                    duration_secs: row.get(8)?,
                    recovery_status: row.get(9)?,
                    error_code: row.get(10)?,
                    created_at: row.get(11)?,
                    updated_at: row.get(12)?,
                })
            },
        )
        .map_err(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => {
                DbError::SessionNotFound(id.to_string())
            }
            other => DbError::Sqlite(other),
        })
    }

    pub fn list_sessions(&self, status_filter: Option<&str>) -> Result<Vec<LocalSession>, DbError> {
        let conn = self.conn.lock().unwrap();
        let result = if let Some(status) = status_filter {
            let mut s = conn.prepare(
                "SELECT id, title, status, mic_path, loopback_path, sample_rate, started_at, ended_at, duration_secs, recovery_status, error_code, created_at, updated_at FROM sessions WHERE status = ?1 ORDER BY created_at DESC"
            )?;
            let rows = s.query_map(params![status], |row| {
                Ok(LocalSession {
                    id: row.get(0)?,
                    title: row.get(1)?,
                    status: row.get(2)?,
                    mic_path: row.get(3)?,
                    loopback_path: row.get(4)?,
                    sample_rate: row.get(5)?,
                    started_at: row.get(6)?,
                    ended_at: row.get(7)?,
                    duration_secs: row.get(8)?,
                    recovery_status: row.get(9)?,
                    error_code: row.get(10)?,
                    created_at: row.get(11)?,
                    updated_at: row.get(12)?,
                })
            })?;
            rows.collect::<Result<Vec<_>, _>>()?
        } else {
            let mut s = conn.prepare(
                "SELECT id, title, status, mic_path, loopback_path, sample_rate, started_at, ended_at, duration_secs, recovery_status, error_code, created_at, updated_at FROM sessions ORDER BY created_at DESC"
            )?;
            let rows = s.query_map([], |row| {
                Ok(LocalSession {
                    id: row.get(0)?,
                    title: row.get(1)?,
                    status: row.get(2)?,
                    mic_path: row.get(3)?,
                    loopback_path: row.get(4)?,
                    sample_rate: row.get(5)?,
                    started_at: row.get(6)?,
                    ended_at: row.get(7)?,
                    duration_secs: row.get(8)?,
                    recovery_status: row.get(9)?,
                    error_code: row.get(10)?,
                    created_at: row.get(11)?,
                    updated_at: row.get(12)?,
                })
            })?;
            rows.collect::<Result<Vec<_>, _>>()?
        };
        Ok(result)
    }

    /// Delete a session and all cascade-derived records. Returns the session
    /// so the caller can clean up files.
    pub fn delete_session(&self, id: &str) -> Result<LocalSession, DbError> {
        let session = self.get_session(id)?;
        let conn = self.conn.lock().unwrap();
        // With foreign_keys=ON, cascade deletes happen automatically
        conn.execute("DELETE FROM sessions WHERE id = ?1", params![id])?;
        Ok(session)
    }

    // ── Participants ────────────────────────────────────────────────────────

    pub fn add_participant(
        &self,
        session_id: &str,
        name: Option<&str>,
        role: Option<&str>,
    ) -> Result<Participant, DbError> {
        let id = uuid::Uuid::new_v4().to_string();
        let now = chrono::Utc::now().to_rfc3339();
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO participants (id, session_id, name, role, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![id, session_id, name, role, now],
        )?;
        drop(conn);
        self.get_participant(&id)
    }

    pub fn get_participant(&self, id: &str) -> Result<Participant, DbError> {
        let conn = self.conn.lock().unwrap();
        conn.query_row(
            "SELECT id, session_id, name, role, created_at FROM participants WHERE id = ?1",
            params![id],
            |row| {
                Ok(Participant {
                    id: row.get(0)?,
                    session_id: row.get(1)?,
                    name: row.get(2)?,
                    role: row.get(3)?,
                    created_at: row.get(4)?,
                })
            },
        )
        .map_err(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => DbError::SessionNotFound(id.to_string()),
            other => DbError::Sqlite(other),
        })
    }

    pub fn list_participants(&self, session_id: &str) -> Result<Vec<Participant>, DbError> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, session_id, name, role, created_at FROM participants WHERE session_id = ?1 ORDER BY created_at",
        )?;
        let rows = stmt.query_map(params![session_id], |row| {
            Ok(Participant {
                id: row.get(0)?,
                session_id: row.get(1)?,
                name: row.get(2)?,
                role: row.get(3)?,
                created_at: row.get(4)?,
            })
        })?;
        rows.collect::<Result<Vec<_>, _>>().map_err(DbError::Sqlite)
    }

    // ── Transcript segments ─────────────────────────────────────────────────

    pub fn save_transcript_segments(
        &self,
        segments: &[TranscriptSegment],
    ) -> Result<usize, DbError> {
        let conn = self.conn.lock().unwrap();
        let mut count = 0;
        for seg in segments {
            conn.execute(
                "INSERT OR REPLACE INTO transcript_segments (id, session_id, speaker_id, start_ms, end_ms, text, confidence, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                params![seg.id, seg.session_id, seg.speaker_id, seg.start_ms, seg.end_ms, seg.text, seg.confidence, seg.created_at],
            )?;
            count += 1;
        }
        Ok(count)
    }

    pub fn get_transcript_segments(
        &self,
        session_id: &str,
    ) -> Result<Vec<TranscriptSegment>, DbError> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, session_id, speaker_id, start_ms, end_ms, text, confidence, created_at FROM transcript_segments WHERE session_id = ?1 ORDER BY start_ms",
        )?;
        let rows = stmt.query_map(params![session_id], |row| {
            Ok(TranscriptSegment {
                id: row.get(0)?,
                session_id: row.get(1)?,
                speaker_id: row.get(2)?,
                start_ms: row.get(3)?,
                end_ms: row.get(4)?,
                text: row.get(5)?,
                confidence: row.get(6)?,
                created_at: row.get(7)?,
            })
        })?;
        rows.collect::<Result<Vec<_>, _>>().map_err(DbError::Sqlite)
    }

    // ── Analysis results ────────────────────────────────────────────────────

    pub fn save_analysis_results(
        &self,
        session_id: &str,
        results: &[AnalysisResult],
        candidates: &[MemoryCandidate],
    ) -> Result<AnalysisResultsBundle, DbError> {
        let conn = self.conn.lock().unwrap();
        let mut saved_results = Vec::new();
        for r in results {
            conn.execute(
                "INSERT OR REPLACE INTO analysis_results (id, session_id, analysis_type, content_json, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
                params![r.id, session_id, r.analysis_type, r.content_json, r.created_at],
            )?;
            saved_results.push(AnalysisResult {
                id: r.id.clone(),
                session_id: session_id.to_string(),
                analysis_type: r.analysis_type.clone(),
                content_json: r.content_json.clone(),
                created_at: r.created_at.clone(),
            });
        }
        let mut saved_candidates = Vec::new();
        for c in candidates {
            conn.execute(
                "INSERT OR REPLACE INTO memory_candidates (id, session_id, segment_id, content, score, confirmed, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                params![c.id, session_id, c.segment_id, c.content, c.score, c.confirmed as i32, c.created_at],
            )?;
            saved_candidates.push(MemoryCandidate {
                id: c.id.clone(),
                session_id: session_id.to_string(),
                segment_id: c.segment_id.clone(),
                content: c.content.clone(),
                score: c.score,
                confirmed: c.confirmed,
                created_at: c.created_at.clone(),
            });
        }
        Ok(AnalysisResultsBundle {
            results: saved_results,
            memory_candidates: saved_candidates,
        })
    }

    pub fn get_analysis_results(&self, session_id: &str) -> Result<AnalysisResultsBundle, DbError> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, session_id, analysis_type, content_json, created_at FROM analysis_results WHERE session_id = ?1 ORDER BY created_at",
        )?;
        let results: Vec<AnalysisResult> = stmt
            .query_map(params![session_id], |row| {
                Ok(AnalysisResult {
                    id: row.get(0)?,
                    session_id: row.get(1)?,
                    analysis_type: row.get(2)?,
                    content_json: row.get(3)?,
                    created_at: row.get(4)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;

        let mut stmt2 = conn.prepare(
            "SELECT id, session_id, segment_id, content, score, confirmed, created_at FROM memory_candidates WHERE session_id = ?1 ORDER BY created_at",
        )?;
        let candidates: Vec<MemoryCandidate> = stmt2
            .query_map(params![session_id], |row| {
                Ok(MemoryCandidate {
                    id: row.get(0)?,
                    session_id: row.get(1)?,
                    segment_id: row.get(2)?,
                    content: row.get(3)?,
                    score: row.get(4)?,
                    confirmed: row.get::<_, i32>(5)? != 0,
                    created_at: row.get(6)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;

        Ok(AnalysisResultsBundle {
            results,
            memory_candidates: candidates,
        })
    }

    // ── Source relations ────────────────────────────────────────────────────

    pub fn add_source_relation(
        &self,
        source_session_id: &str,
        target_session_id: &str,
        relation_type: &str,
        metadata_json: Option<&str>,
    ) -> Result<SourceRelation, DbError> {
        let id = uuid::Uuid::new_v4().to_string();
        let now = chrono::Utc::now().to_rfc3339();
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO source_relations (id, source_session_id, target_session_id, relation_type, metadata_json, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![id, source_session_id, target_session_id, relation_type, metadata_json, now],
        )?;
        drop(conn);
        self.get_source_relation(&id)
    }

    pub fn get_source_relation(&self, id: &str) -> Result<SourceRelation, DbError> {
        let conn = self.conn.lock().unwrap();
        conn.query_row(
            "SELECT id, source_session_id, target_session_id, relation_type, metadata_json, created_at FROM source_relations WHERE id = ?1",
            params![id],
            |row| {
                Ok(SourceRelation {
                    id: row.get(0)?,
                    source_session_id: row.get(1)?,
                    target_session_id: row.get(2)?,
                    relation_type: row.get(3)?,
                    metadata_json: row.get(4)?,
                    created_at: row.get(5)?,
                })
            },
        )
        .map_err(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => DbError::SessionNotFound(id.to_string()),
            other => DbError::Sqlite(other),
        })
    }

    pub fn list_source_relations(&self, session_id: &str) -> Result<Vec<SourceRelation>, DbError> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, source_session_id, target_session_id, relation_type, metadata_json, created_at FROM source_relations WHERE source_session_id = ?1 OR target_session_id = ?1 ORDER BY created_at",
        )?;
        let rows = stmt.query_map(params![session_id], |row| {
            Ok(SourceRelation {
                id: row.get(0)?,
                source_session_id: row.get(1)?,
                target_session_id: row.get(2)?,
                relation_type: row.get(3)?,
                metadata_json: row.get(4)?,
                created_at: row.get(5)?,
            })
        })?;
        rows.collect::<Result<Vec<_>, _>>().map_err(DbError::Sqlite)
    }

    // ── Cascade delete helper ───────────────────────────────────────────────

    /// Delete a session with strict path validation. Returns the session
    /// metadata so the caller can remove local files.
    pub fn cascade_delete_session(
        &self,
        session_id: &str,
        sessions_dir: &Path,
    ) -> Result<LocalSession, DbError> {
        crate::paths::validate_session_path(session_id, sessions_dir)
            .map_err(|e| DbError::Path(e.to_string()))?;
        self.delete_session(session_id)
    }
}

// ── Tests ───────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn temp_repo() -> (Repository, PathBuf) {
        let dir = std::env::temp_dir().join(format!("memecho_db_test_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let db_path = dir.join("test.db");
        let repo = Repository::open(&db_path).unwrap();
        (repo, dir)
    }

    fn cleanup(dir: &PathBuf) {
        std::fs::remove_dir_all(dir).ok();
    }

    #[test]
    fn test_schema_creates_tables() {
        let (repo, dir) = temp_repo();
        let conn = repo.conn.lock().unwrap();
        let tables: Vec<String> = conn
            .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            .unwrap()
            .query_map([], |row| row.get(0))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert!(tables.contains(&"sessions".to_string()));
        assert!(tables.contains(&"participants".to_string()));
        assert!(tables.contains(&"transcript_segments".to_string()));
        assert!(tables.contains(&"analysis_results".to_string()));
        assert!(tables.contains(&"memory_candidates".to_string()));
        assert!(tables.contains(&"source_relations".to_string()));
        assert!(tables.contains(&"schema_version".to_string()));
        drop(conn);
        cleanup(&dir);
    }

    #[test]
    fn test_schema_version() {
        let (repo, dir) = temp_repo();
        let conn = repo.conn.lock().unwrap();
        let version: i32 = conn
            .query_row("SELECT version FROM schema_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        drop(conn);
        cleanup(&dir);
    }

    #[test]
    fn test_create_and_get_session() {
        let (repo, dir) = temp_repo();
        let s = repo
            .create_session(
                "s1",
                Some("Test Session"),
                Some("mic.wav"),
                Some("lb.wav"),
                16000,
                None,
            )
            .unwrap();
        assert_eq!(s.id, "s1");
        assert_eq!(s.title.as_deref(), Some("Test Session"));
        assert_eq!(s.status, "active");
        assert_eq!(s.sample_rate, 16000);
        assert!(s.mic_path.is_some());
        assert!(s.loopback_path.is_some());
        assert!(s.started_at.len() > 0);
        assert!(s.created_at.len() > 0);
        let got = repo.get_session("s1").unwrap();
        assert_eq!(got.id, "s1");
        cleanup(&dir);
    }

    #[test]
    fn test_update_session() {
        let (repo, dir) = temp_repo();
        repo.create_session("u1", None, None, None, 16000, None)
            .unwrap();
        let updated = repo
            .update_session(
                "u1",
                Some("Updated"),
                Some("completed"),
                None,
                Some(120.5),
                Some("finalized"),
                None,
            )
            .unwrap();
        assert_eq!(updated.title.as_deref(), Some("Updated"));
        assert_eq!(updated.status, "completed");
        assert_eq!(updated.duration_secs, Some(120.5));
        assert_eq!(updated.recovery_status.as_deref(), Some("finalized"));
        cleanup(&dir);
    }

    #[test]
    fn test_update_nonexistent_session() {
        let (repo, dir) = temp_repo();
        let result = repo.update_session("nope", None, None, None, None, None, None);
        assert!(matches!(result, Err(DbError::SessionNotFound(_))));
        cleanup(&dir);
    }

    #[test]
    fn test_list_sessions() {
        let (repo, dir) = temp_repo();
        repo.create_session("l1", None, None, None, 16000, None)
            .unwrap();
        repo.create_session("l2", None, None, None, 16000, None)
            .unwrap();
        repo.update_session("l1", None, Some("completed"), None, None, None, None)
            .unwrap();
        let all = repo.list_sessions(None).unwrap();
        assert_eq!(all.len(), 2);
        let active = repo.list_sessions(Some("active")).unwrap();
        assert_eq!(active.len(), 1);
        assert_eq!(active[0].id, "l2");
        cleanup(&dir);
    }

    #[test]
    fn test_delete_session() {
        let (repo, dir) = temp_repo();
        repo.create_session("d1", None, None, None, 16000, None)
            .unwrap();
        let deleted = repo.delete_session("d1").unwrap();
        assert_eq!(deleted.id, "d1");
        assert!(matches!(
            repo.get_session("d1"),
            Err(DbError::SessionNotFound(_))
        ));
        cleanup(&dir);
    }

    #[test]
    fn test_participants_cascade_delete() {
        let (repo, dir) = temp_repo();
        repo.create_session("cd1", None, None, None, 16000, None)
            .unwrap();
        repo.add_participant("cd1", Some("Alice"), Some("speaker"))
            .unwrap();
        repo.add_participant("cd1", Some("Bob"), Some("listener"))
            .unwrap();
        let parts = repo.list_participants("cd1").unwrap();
        assert_eq!(parts.len(), 2);
        repo.delete_session("cd1").unwrap();
        // Participants should be gone (cascade)
        let parts_after = repo.list_participants("cd1").unwrap();
        assert_eq!(parts_after.len(), 0);
        cleanup(&dir);
    }

    #[test]
    fn test_transcript_segments_cascade_delete() {
        let (repo, dir) = temp_repo();
        repo.create_session("cd2", None, None, None, 16000, None)
            .unwrap();
        let now = chrono::Utc::now().to_rfc3339();
        let segments = vec![
            TranscriptSegment {
                id: "seg1".into(),
                session_id: "cd2".into(),
                speaker_id: None,
                start_ms: 0,
                end_ms: 5000,
                text: "Hello world".into(),
                confidence: Some(0.95),
                created_at: now.clone(),
            },
            TranscriptSegment {
                id: "seg2".into(),
                session_id: "cd2".into(),
                speaker_id: None,
                start_ms: 5000,
                end_ms: 10000,
                text: "Goodbye world".into(),
                confidence: Some(0.90),
                created_at: now.clone(),
            },
        ];
        repo.save_transcript_segments(&segments).unwrap();
        let segs = repo.get_transcript_segments("cd2").unwrap();
        assert_eq!(segs.len(), 2);
        repo.delete_session("cd2").unwrap();
        let segs_after = repo.get_transcript_segments("cd2").unwrap();
        assert_eq!(segs_after.len(), 0);
        cleanup(&dir);
    }

    #[test]
    fn test_analysis_cascade_delete() {
        let (repo, dir) = temp_repo();
        repo.create_session("cd3", None, None, None, 16000, None)
            .unwrap();
        let now = chrono::Utc::now().to_rfc3339();
        let results = vec![AnalysisResult {
            id: "ar1".into(),
            session_id: "cd3".into(),
            analysis_type: "summary".into(),
            content_json: r#"{"text":"hello"}"#.into(),
            created_at: now.clone(),
        }];
        let candidates = vec![MemoryCandidate {
            id: "mc1".into(),
            session_id: "cd3".into(),
            segment_id: None,
            content: "Remember this".into(),
            score: Some(0.8),
            confirmed: true,
            created_at: now.clone(),
        }];
        repo.save_analysis_results("cd3", &results, &candidates)
            .unwrap();
        let bundle = repo.get_analysis_results("cd3").unwrap();
        assert_eq!(bundle.results.len(), 1);
        assert_eq!(bundle.memory_candidates.len(), 1);
        repo.delete_session("cd3").unwrap();
        let bundle_after = repo.get_analysis_results("cd3").unwrap();
        assert_eq!(bundle_after.results.len(), 0);
        assert_eq!(bundle_after.memory_candidates.len(), 0);
        cleanup(&dir);
    }

    #[test]
    fn test_source_relations_cascade_delete() {
        let (repo, dir) = temp_repo();
        repo.create_session("sr1", None, None, None, 16000, None)
            .unwrap();
        repo.create_session("sr2", None, None, None, 16000, None)
            .unwrap();
        repo.add_source_relation("sr1", "sr2", "derived_from", None)
            .unwrap();
        let rels = repo.list_source_relations("sr1").unwrap();
        assert_eq!(rels.len(), 1);
        // Delete sr1 — relation should cascade
        repo.delete_session("sr1").unwrap();
        let rels_after = repo.list_source_relations("sr2").unwrap();
        assert_eq!(rels_after.len(), 0);
        cleanup(&dir);
    }

    #[test]
    fn test_source_relation_target_invalidation() {
        let (repo, dir) = temp_repo();
        repo.create_session("si1", None, None, None, 16000, None)
            .unwrap();
        repo.create_session("si2", None, None, None, 16000, None)
            .unwrap();
        repo.add_source_relation("si1", "si2", "related", None)
            .unwrap();
        // Delete target si2 — relation should cascade
        repo.delete_session("si2").unwrap();
        let rels = repo.list_source_relations("si1").unwrap();
        assert_eq!(rels.len(), 0);
        cleanup(&dir);
    }

    #[test]
    fn test_cascade_delete_with_path_validation() {
        let (repo, dir) = temp_repo();
        let sessions_dir = dir.join("sessions");
        std::fs::create_dir_all(&sessions_dir).unwrap();
        // Create a real session directory
        std::fs::create_dir_all(sessions_dir.join("valid-session")).unwrap();
        repo.create_session("valid-session", None, None, None, 16000, None)
            .unwrap();
        let deleted = repo
            .cascade_delete_session("valid-session", &sessions_dir)
            .unwrap();
        assert_eq!(deleted.id, "valid-session");
        // Path traversal should fail
        let result = repo.cascade_delete_session("../evil", &sessions_dir);
        assert!(matches!(result, Err(DbError::Path(_))));
        cleanup(&dir);
    }

    #[test]
    fn test_restart_persistence() {
        let dir = std::env::temp_dir().join(format!("memecho_restart_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let db_path = dir.join("persist.db");
        {
            let repo = Repository::open(&db_path).unwrap();
            repo.create_session("rp1", Some("Persistent"), None, None, 16000, None)
                .unwrap();
            let now = chrono::Utc::now().to_rfc3339();
            repo.save_analysis_results(
                "rp1",
                &[AnalysisResult {
                    id: "ar_persist".into(),
                    session_id: "rp1".into(),
                    analysis_type: "summary".into(),
                    content_json: r#"{"text":"persisted"}"#.into(),
                    created_at: now,
                }],
                &[],
            )
            .unwrap();
        }
        // Reopen — data should survive
        {
            let repo = Repository::open(&db_path).unwrap();
            let s = repo.get_session("rp1").unwrap();
            assert_eq!(s.title.as_deref(), Some("Persistent"));
            let bundle = repo.get_analysis_results("rp1").unwrap();
            assert_eq!(bundle.results.len(), 1);
            assert_eq!(bundle.results[0].analysis_type, "summary");
        }
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_path_traversal_rejected_in_cascade() {
        let (repo, dir) = temp_repo();
        let sessions_dir = dir.join("sessions");
        std::fs::create_dir_all(&sessions_dir).unwrap();
        // Various traversal attempts
        for evil_id in &["../etc/passwd", "foo/bar", "..\\windows", "con"] {
            let result = repo.cascade_delete_session(evil_id, &sessions_dir);
            assert!(result.is_err(), "should reject: {}", evil_id);
        }
        cleanup(&dir);
    }

    #[test]
    fn test_memory_candidates_with_segment_ref() {
        let (repo, dir) = temp_repo();
        repo.create_session("mc1", None, None, None, 16000, None)
            .unwrap();
        let now = chrono::Utc::now().to_rfc3339();
        repo.save_transcript_segments(&[TranscriptSegment {
            id: "seg_mc".into(),
            session_id: "mc1".into(),
            speaker_id: None,
            start_ms: 0,
            end_ms: 5000,
            text: "Important info".into(),
            confidence: Some(0.9),
            created_at: now.clone(),
        }])
        .unwrap();
        repo.save_analysis_results(
            "mc1",
            &[],
            &[MemoryCandidate {
                id: "mc_ref".into(),
                session_id: "mc1".into(),
                segment_id: Some("seg_mc".into()),
                content: "Key insight".into(),
                score: Some(0.95),
                confirmed: true,
                created_at: now,
            }],
        )
        .unwrap();
        let bundle = repo.get_analysis_results("mc1").unwrap();
        assert_eq!(
            bundle.memory_candidates[0].segment_id.as_deref(),
            Some("seg_mc")
        );
        // Cascade delete session — segment ref is SET NULL on the candidate,
        // but since the candidate is also cascade-deleted, this is moot.
        repo.delete_session("mc1").unwrap();
        assert_eq!(
            repo.get_analysis_results("mc1")
                .unwrap()
                .memory_candidates
                .len(),
            0
        );
        cleanup(&dir);
    }
}
