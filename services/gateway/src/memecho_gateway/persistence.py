"""SQLite persistence layer for Gateway sessions, jobs, and processing state.

Provides durable storage that survives process restarts. Sensitive data
(API keys, transcript text, credentials) is never persisted.
"""

from __future__ import annotations

import json
import sqlite3
import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generator

from .models import JobStatus

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '工作',
    occurred_at TEXT NOT NULL,
    source_mode TEXT NOT NULL,
    marks TEXT NOT NULL DEFAULT '[]',
    participant_resolution TEXT,
    result TEXT,
    processing TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    track TEXT NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    directory TEXT NOT NULL,
    chunks TEXT NOT NULL DEFAULT '[]',
    completed_path TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    stage_label TEXT NOT NULL DEFAULT '等待处理',
    retryable INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    request_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS analysis_requests (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    request_data TEXT NOT NULL,
    PRIMARY KEY (job_id)
);

CREATE TABLE IF NOT EXISTS job_intermediates (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    intermediate_data TEXT NOT NULL,
    PRIMARY KEY (job_id)
);

CREATE TABLE IF NOT EXISTS resume_scheduled_jobs (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    PRIMARY KEY (session_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_uploads_session ON uploads(session_id);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_idempotency_job ON idempotency(job_id);
"""


@contextmanager
def _connect(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database connections with WAL mode."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    """Initialize database schema if not exists."""
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        # Check/set schema version
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] < SCHEMA_VERSION:
            # Future: add migration logic here
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def save_session(
    db_path: Path,
    session_id: str,
    request_id: str,
    create_data: dict[str, Any],
) -> None:
    """Persist a new session."""
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO sessions
               (id, request_id, title, context, occurred_at, source_mode, marks, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                request_id,
                create_data["title"],
                create_data.get("context", "工作"),
                create_data["occurred_at"].isoformat() if isinstance(create_data["occurred_at"], datetime) else create_data["occurred_at"],
                create_data["source_mode"],
                json.dumps(create_data.get("marks", []), ensure_ascii=False),
                _now_iso(),
                _now_iso(),
            ),
        )


def save_upload(
    db_path: Path,
    upload_id: str,
    session_id: str,
    track: str,
    file_name: str,
    mime_type: str,
    size: int,
    sha256: str,
    directory: str,
    chunks: set[int] | None = None,
    completed_path: str | None = None,
) -> None:
    """Persist an upload record."""
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO uploads
               (id, session_id, track, file_name, mime_type, size, sha256, directory, chunks, completed_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                upload_id,
                session_id,
                track,
                file_name,
                mime_type,
                size,
                sha256,
                directory,
                json.dumps(sorted(chunks) if chunks else []),
                completed_path,
            ),
        )


def save_job(
    db_path: Path,
    job_id: str,
    session_id: str,
    request_id: str,
    status: JobStatus,
    progress: int,
    stage_label: str,
    retryable: bool = False,
    error_code: str | None = None,
    error_detail: str | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> None:
    """Persist a job record."""
    now = _now_iso()
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO jobs
               (id, session_id, request_id, status, progress, stage_label,
                retryable, error_code, error_detail, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                session_id,
                request_id,
                status.value if isinstance(status, JobStatus) else status,
                progress,
                stage_label,
                1 if retryable else 0,
                error_code,
                error_detail,
                created_at.isoformat() if created_at else now,
                updated_at.isoformat() if updated_at else now,
            ),
        )


def save_idempotency(db_path: Path, request_id: str, job_id: str) -> None:
    """Persist idempotency mapping."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO idempotency (request_id, job_id) VALUES (?, ?)",
            (request_id, job_id),
        )


def save_analysis_request(db_path: Path, job_id: str, request_data: dict[str, Any]) -> None:
    """Persist analysis request data."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO analysis_requests (job_id, request_data) VALUES (?, ?)",
            (job_id, json.dumps(request_data, ensure_ascii=False)),
        )


def save_job_intermediate(db_path: Path, job_id: str, intermediate_data: dict[str, Any]) -> None:
    """Persist job intermediate data."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO job_intermediates (job_id, intermediate_data) VALUES (?, ?)",
            (job_id, json.dumps(intermediate_data, ensure_ascii=False, default=str)),
        )


def save_resume_scheduled_job(db_path: Path, session_id: str, job_id: str) -> None:
    """Persist resume scheduled job."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO resume_scheduled_jobs (session_id, job_id) VALUES (?, ?)",
            (session_id, job_id),
        )


def update_session_result(db_path: Path, session_id: str, result: dict[str, Any]) -> None:
    """Persist session result."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET result = ?, updated_at = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False, default=str), _now_iso(), session_id),
        )


def update_session_processing(db_path: Path, session_id: str, processing: dict[str, Any]) -> None:
    """Persist session processing state."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET processing = ?, updated_at = ? WHERE id = ?",
            (json.dumps(processing, ensure_ascii=False, default=str), _now_iso(), session_id),
        )


def update_session_participant_resolution(
    db_path: Path, session_id: str, resolution: dict[str, Any]
) -> None:
    """Persist participant resolution."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET participant_resolution = ?, updated_at = ? WHERE id = ?",
            (json.dumps(resolution, ensure_ascii=False), _now_iso(), session_id),
        )


def update_job_status(
    db_path: Path,
    job_id: str,
    status: JobStatus,
    progress: int,
    stage_label: str,
    retryable: bool = False,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    """Update job status in persistence."""
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE jobs
               SET status = ?, progress = ?, stage_label = ?,
                   retryable = ?, error_code = ?, error_detail = ?, updated_at = ?
               WHERE id = ?""",
            (
                status.value if isinstance(status, JobStatus) else status,
                progress,
                stage_label,
                1 if retryable else 0,
                error_code,
                error_detail,
                _now_iso(),
                job_id,
            ),
        )


def update_upload_chunks(db_path: Path, upload_id: str, chunks: set[int]) -> None:
    """Update upload chunks."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE uploads SET chunks = ? WHERE id = ?",
            (json.dumps(sorted(chunks)), upload_id),
        )


def update_upload_completed(db_path: Path, upload_id: str, completed_path: str) -> None:
    """Mark upload as completed."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE uploads SET completed_path = ? WHERE id = ?",
            (completed_path, upload_id),
        )


def load_all_sessions(db_path: Path) -> dict[str, dict[str, Any]]:
    """Load all sessions from database."""
    sessions: dict[str, dict[str, Any]] = {}
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM sessions").fetchall()
        for row in rows:
            session_id = row["id"]
            sessions[session_id] = {
                "id": session_id,
                "request_id": row["request_id"],
                "title": row["title"],
                "context": row["context"],
                "occurred_at": row["occurred_at"],
                "source_mode": row["source_mode"],
                "marks": json.loads(row["marks"]),
                "participant_resolution": json.loads(row["participant_resolution"]) if row["participant_resolution"] else None,
                "result": json.loads(row["result"]) if row["result"] else None,
                "processing": json.loads(row["processing"]),
            }
    return sessions


def load_all_uploads(db_path: Path) -> dict[str, dict[str, Any]]:
    """Load all uploads from database."""
    uploads: dict[str, dict[str, Any]] = {}
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM uploads").fetchall()
        for row in rows:
            upload_id = row["id"]
            uploads[upload_id] = {
                "id": upload_id,
                "session_id": row["session_id"],
                "track": row["track"],
                "file_name": row["file_name"],
                "mime_type": row["mime_type"],
                "size": row["size"],
                "sha256": row["sha256"],
                "directory": row["directory"],
                "chunks": set(json.loads(row["chunks"])),
                "completed_path": row["completed_path"],
            }
    return uploads


def load_all_jobs(db_path: Path) -> dict[str, dict[str, Any]]:
    """Load all jobs from database."""
    jobs: dict[str, dict[str, Any]] = {}
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM jobs").fetchall()
        for row in rows:
            job_id = row["id"]
            jobs[job_id] = {
                "id": job_id,
                "session_id": row["session_id"],
                "request_id": row["request_id"],
                "status": row["status"],
                "progress": row["progress"],
                "stage_label": row["stage_label"],
                "retryable": bool(row["retryable"]),
                "error_code": row["error_code"],
                "error_detail": row["error_detail"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
    return jobs


def load_idempotency(db_path: Path) -> dict[str, str]:
    """Load idempotency mappings."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT request_id, job_id FROM idempotency").fetchall()
        return {row["request_id"]: row["job_id"] for row in rows}


def load_analysis_requests(db_path: Path) -> dict[str, dict[str, Any]]:
    """Load all analysis requests."""
    requests: dict[str, dict[str, Any]] = {}
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT job_id, request_data FROM analysis_requests").fetchall()
        for row in rows:
            requests[row["job_id"]] = json.loads(row["request_data"])
    return requests


def load_job_intermediates(db_path: Path) -> dict[str, dict[str, Any]]:
    """Load all job intermediates."""
    intermediates: dict[str, dict[str, Any]] = {}
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT job_id, intermediate_data FROM job_intermediates").fetchall()
        for row in rows:
            intermediates[row["job_id"]] = json.loads(row["intermediate_data"])
    return intermediates


def load_resume_scheduled_jobs(db_path: Path) -> dict[str, set[str]]:
    """Load resume scheduled jobs by session."""
    result: dict[str, set[str]] = {}
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT session_id, job_id FROM resume_scheduled_jobs").fetchall()
        for row in rows:
            session_id = row["session_id"]
            if session_id not in result:
                result[session_id] = set()
            result[session_id].add(row["job_id"])
    return result


def delete_session(db_path: Path, session_id: str) -> None:
    """Delete a session and all related data (cascading)."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def get_unfinished_jobs(db_path: Path) -> list[dict[str, Any]]:
    """Get jobs that were in progress when the server stopped."""
    jobs: list[dict[str, Any]] = []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status NOT IN (?, ?)",
            (JobStatus.complete.value, JobStatus.failed.value),
        ).fetchall()
        for row in rows:
            jobs.append({
                "id": row["id"],
                "session_id": row["session_id"],
                "request_id": row["request_id"],
                "status": row["status"],
                "progress": row["progress"],
                "stage_label": row["stage_label"],
            })
    return jobs
