"""Bounded lifecycle management for gateway-local upload copies.

Completed sessions lose their local media immediately; failed jobs keep their
media for a bounded retention window so retries remain possible. Unknown
session directories (for example after a gateway restart, since the store is
in-memory) are swept once they exceed the retention window.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import JobStatus

log = logging.getLogger(__name__)

ACTIVE_STATUSES = {
    JobStatus.queued,
    JobStatus.uploading,
    JobStatus.transcribing,
    JobStatus.awaiting_identity,
    JobStatus.aligning,
    JobStatus.analyzing,
    JobStatus.rendering,
}


def session_media_can_be_removed(
    store: Any, session_id: str, retention_seconds: float, now: datetime | None = None
) -> bool:
    session = store.sessions.get(session_id)
    if session is None:
        return False
    now = now or datetime.now(UTC)
    for job in store.jobs.values():
        if job.session_id != session_id:
            continue
        if job.status in ACTIVE_STATUSES:
            return False
        if job.status == JobStatus.failed and now - job.updated_at < timedelta(
            seconds=retention_seconds
        ):
            return False
    return True


def remove_session_media(
    store: Any, session_id: str, retention_seconds: float, now: datetime | None = None
) -> int:
    """Delete local upload copies for a session when its jobs allow it."""
    if not session_media_can_be_removed(store, session_id, retention_seconds, now):
        return 0
    session = store.sessions[session_id]
    removed = 0
    for upload in session.uploads.values():
        directory = Path(upload.directory)
        try:
            shutil.rmtree(directory)
            removed += 1
        except OSError:
            log.warning("Local media cleanup failed session=%s", session_id, exc_info=True)
    return removed


def sweep_expired_media(
    store: Any, retention_seconds: float, now: datetime | None = None
) -> int:
    """Bounded sweep of session directories with no live store state.

    Only directories whose mtime is older than the retention window are
    removed, so an in-progress session from a live gateway process is never
    touched by a concurrent sweeper.
    """
    now = now or datetime.now(UTC)
    data_dir = Path(store.data_dir)
    if not data_dir.is_dir():
        return 0
    removed = 0
    for session_dir in sorted(data_dir.iterdir()):
        if not session_dir.is_dir() or session_dir.name in store.sessions:
            continue
        try:
            mtime = datetime.fromtimestamp(session_dir.stat().st_mtime, UTC)
        except OSError:
            continue
        if now - mtime < timedelta(seconds=retention_seconds):
            continue
        try:
            shutil.rmtree(session_dir)
            removed += 1
        except OSError:
            log.warning("Media sweep skipped directory=%s", session_dir.name, exc_info=True)
    return removed
