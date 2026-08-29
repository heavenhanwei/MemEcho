from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import JobStatus, JobView, SessionCreate
from . import persistence

log = logging.getLogger(__name__)


@dataclass
class UploadRecord:
    id: str
    session_id: str
    track: str
    file_name: str
    mime_type: str
    size: int
    sha256: str
    directory: Path
    chunks: set[int] = field(default_factory=set)
    completed_path: Path | None = None


@dataclass
class SessionRecord:
    id: str
    request_id: str
    create: SessionCreate
    uploads: dict[str, UploadRecord] = field(default_factory=dict)
    participant_resolution: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    analysis_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    job_intermediates: dict[str, dict[str, Any]] = field(default_factory=dict)
    resume_scheduled_jobs: set[str] = field(default_factory=set)
    # Sanitized pipeline observability state served by /processing-details.
    processing: dict[str, Any] = field(default_factory=dict)


class MemoryStore:
    """Roadshow store. Sensitive results remain process-local and expire on restart."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.sessions: dict[str, SessionRecord] = {}
        self.jobs: dict[str, JobView] = {}
        self.events: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.idempotency: dict[str, str] = {}
        # Async upstream task references keyed by (job_id, capability, upload_id).
        self.upstream_tasks: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.lock = asyncio.Lock()

    async def create_session(self, payload: SessionCreate) -> SessionRecord:
        async with self.lock:
            session_id = f"ses_{uuid4().hex[:16]}"
            request_id = f"req_{uuid4().hex[:16]}"
            record = SessionRecord(session_id, request_id, payload)
            self.sessions[session_id] = record
            (self.data_dir / session_id).mkdir(parents=True, exist_ok=True)
            return record

    async def create_job(self, session_id: str, request_id: str) -> JobView:
        async with self.lock:
            if request_id in self.idempotency:
                return self.jobs[self.idempotency[request_id]]
            now = datetime.now(UTC)
            job = JobView(
                id=f"job_{uuid4().hex[:16]}",
                session_id=session_id,
                request_id=request_id,
                status=JobStatus.queued,
                progress=2,
                stage_label="等待处理",
                created_at=now,
                updated_at=now,
            )
            self.jobs[job.id] = job
            self.events[job.id] = asyncio.Queue()
            self.idempotency[request_id] = job.id
            return job

    async def update_job(
        self,
        job_id: str,
        status: JobStatus,
        progress: int,
        label: str,
        **extra: Any,
    ) -> JobView:
        job = self.jobs[job_id]
        updated = job.model_copy(
            update={
                "status": status,
                "progress": progress,
                "stage_label": label,
                "updated_at": datetime.now(UTC),
                **extra,
            }
        )
        self.jobs[job_id] = updated
        await self.events[job_id].put(updated.model_dump(mode="json"))
        return updated

    @staticmethod
    def _upstream_key(
        job_id: str, capability: str, upload_id: str
    ) -> tuple[str, str, str]:
        return (job_id, capability, upload_id)

    def save_upstream_task(self, record: dict[str, Any]) -> None:
        """Upsert an async upstream task reference."""
        key = self._upstream_key(
            record["job_id"], record["capability"], record["upload_id"]
        )
        existing = self.upstream_tasks.get(key)
        merged = {**(existing or {}), **record}
        self.upstream_tasks[key] = merged

    def update_upstream_task(
        self,
        job_id: str,
        capability: str,
        upload_id: str,
        fields: dict[str, Any],
    ) -> None:
        """Apply a partial update; missing records are left untouched."""
        record = self.upstream_tasks.get(
            self._upstream_key(job_id, capability, upload_id)
        )
        if record is None:
            return
        record.update({name: value for name, value in fields.items() if value is not None})

    def get_upstream_task(
        self, job_id: str, capability: str, upload_id: str
    ) -> dict[str, Any] | None:
        record = self.upstream_tasks.get(
            self._upstream_key(job_id, capability, upload_id)
        )
        return dict(record) if record is not None else None

    def upstream_tasks_for_job(self, job_id: str) -> list[dict[str, Any]]:
        return [
            dict(record)
            for (stored_job_id, _, _), record in self.upstream_tasks.items()
            if stored_job_id == job_id
        ]

    def resumable_upstream_tasks(self) -> list[dict[str, Any]]:
        """Live upstream references that must be polled again, never resubmitted."""
        return [
            dict(record)
            for record in self.upstream_tasks.values()
            if record.get("upstream_task_id")
            and record.get("status") in persistence.UPSTREAM_RESUMABLE_STATUSES
        ]


class PersistentStore(MemoryStore):
    """Persistent store backed by SQLite. Survives process restarts."""

    def __init__(self, data_dir: Path, db_path: Path | None = None):
        super().__init__(data_dir)
        self.db_path = db_path or (data_dir / "gateway.db")
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize database and load persisted state."""
        if self._initialized:
            return

        persistence.init_db(self.db_path)
        await self._load_state()
        self._initialized = True
        log.info("PersistentStore initialized from %s", self.db_path)

    async def _load_state(self) -> None:
        """Load all persisted state into memory."""
        # Load sessions
        sessions_data = persistence.load_all_sessions(self.db_path)
        for session_id, data in sessions_data.items():
            try:
                create = SessionCreate(
                    title=data["title"],
                    context=data["context"],
                    occurred_at=data["occurred_at"],
                    source_mode=data["source_mode"],
                    marks=data["marks"],
                )
                record = SessionRecord(
                    id=session_id,
                    request_id=data["request_id"],
                    create=create,
                    participant_resolution=data["participant_resolution"],
                    result=data["result"],
                    processing=data["processing"],
                )
            except Exception:
                # One corrupted row must not abort gateway startup.
                log.warning("Skipping unparsable persisted session %s", session_id, exc_info=True)
                continue
            self.sessions[session_id] = record

        # Load uploads and attach to sessions
        uploads_data = persistence.load_all_uploads(self.db_path)
        for upload_id, data in uploads_data.items():
            session_id = data["session_id"]
            if session_id in self.sessions:
                upload = UploadRecord(
                    id=upload_id,
                    session_id=session_id,
                    track=data["track"],
                    file_name=data["file_name"],
                    mime_type=data["mime_type"],
                    size=data["size"],
                    sha256=data["sha256"],
                    directory=Path(data["directory"]),
                    chunks=data["chunks"],
                    completed_path=Path(data["completed_path"]) if data["completed_path"] else None,
                )
                self.sessions[session_id].uploads[upload_id] = upload

        # Load jobs
        jobs_data = persistence.load_all_jobs(self.db_path)
        for job_id, data in jobs_data.items():
            try:
                job = JobView(
                    id=job_id,
                    session_id=data["session_id"],
                    request_id=data["request_id"],
                    status=JobStatus(data["status"]),
                    progress=data["progress"],
                    stage_label=data["stage_label"],
                    retryable=data["retryable"],
                    error_code=data["error_code"],
                    error_detail=data["error_detail"],
                    created_at=data["created_at"],
                    updated_at=data["updated_at"],
                )
            except Exception:
                log.warning("Skipping unparsable persisted job %s", job_id, exc_info=True)
                continue
            if job.session_id not in self.sessions:
                log.warning("Skipping persisted job %s with missing session", job_id)
                continue
            self.jobs[job_id] = job
            self.events[job_id] = asyncio.Queue()

        # Load idempotency mappings; drop any mapping that points at a job
        # we did not restore.
        self.idempotency = {
            request_id: job_id
            for request_id, job_id in persistence.load_idempotency(self.db_path).items()
            if job_id in self.jobs
        }

        # Load analysis requests and attach to sessions
        analysis_requests = persistence.load_analysis_requests(self.db_path)
        for job_id, request_data in analysis_requests.items():
            if job_id in self.jobs:
                session_id = self.jobs[job_id].session_id
                if session_id in self.sessions:
                    self.sessions[session_id].analysis_requests[job_id] = request_data

        # Load job intermediates and attach to sessions
        intermediates = persistence.load_job_intermediates(self.db_path)
        for job_id, intermediate_data in intermediates.items():
            if job_id in self.jobs:
                session_id = self.jobs[job_id].session_id
                if session_id in self.sessions:
                    self.sessions[session_id].job_intermediates[job_id] = intermediate_data

        # Load resume scheduled jobs
        resume_jobs = persistence.load_resume_scheduled_jobs(self.db_path)
        for session_id, job_ids in resume_jobs.items():
            if session_id in self.sessions:
                self.sessions[session_id].resume_scheduled_jobs = job_ids

        # Load async upstream task references; drop any whose job was lost.
        for record in persistence.load_all_upstream_tasks(self.db_path):
            if record["job_id"] in self.jobs:
                self.upstream_tasks[
                    self._upstream_key(
                        record["job_id"], record["capability"], record["upload_id"]
                    )
                ] = record

        log.info(
            "Loaded %d sessions, %d jobs from persistence",
            len(self.sessions),
            len(self.jobs),
        )

    async def create_session(self, payload: SessionCreate) -> SessionRecord:
        record = await super().create_session(payload)
        # Persist to database
        persistence.save_session(
            self.db_path,
            record.id,
            record.request_id,
            payload.model_dump(mode="json"),
        )
        return record

    async def create_job(self, session_id: str, request_id: str) -> JobView:
        already_known = request_id in self.idempotency
        job = await super().create_job(session_id, request_id)
        if already_known:
            # Idempotent hit: the job row and mapping are already persisted.
            return job
        persistence.save_job(
            self.db_path,
            job.id,
            session_id,
            request_id,
            job.status,
            job.progress,
            job.stage_label,
            job.retryable,
            job.error_code,
            job.error_detail,
            job.created_at,
            job.updated_at,
        )
        persistence.save_idempotency(self.db_path, request_id, job.id)
        return job

    async def update_job(
        self,
        job_id: str,
        status: JobStatus,
        progress: int,
        label: str,
        **extra: Any,
    ) -> JobView:
        job = await super().update_job(job_id, status, progress, label, **extra)
        # Persist the effective state of the job so DB and memory never
        # diverge when a caller omits retryable/error fields.
        persistence.update_job_status(
            self.db_path,
            job_id,
            status,
            progress,
            label,
            retryable=job.retryable,
            error_code=job.error_code,
            error_detail=job.error_detail,
        )
        # Also persist processing state if session exists
        if job.session_id in self.sessions:
            session = self.sessions[job.session_id]
            if session.processing:
                persistence.update_session_processing(
                    self.db_path, job.session_id, session.processing
                )
        return job

    def save_upload(self, upload: UploadRecord) -> None:
        """Persist an upload record."""
        persistence.save_upload(
            self.db_path,
            upload.id,
            upload.session_id,
            upload.track,
            upload.file_name,
            upload.mime_type,
            upload.size,
            upload.sha256,
            str(upload.directory),
            upload.chunks,
            str(upload.completed_path) if upload.completed_path else None,
        )

    def update_upload_chunks(self, upload: UploadRecord) -> None:
        """Persist upload chunks update."""
        persistence.update_upload_chunks(self.db_path, upload.id, upload.chunks)

    def mark_upload_completed(self, upload: UploadRecord) -> None:
        """Persist upload completion."""
        if upload.completed_path:
            persistence.update_upload_completed(
                self.db_path, upload.id, str(upload.completed_path)
            )

    def save_analysis_request(self, job_id: str, request_data: dict[str, Any]) -> None:
        """Persist analysis request data."""
        persistence.save_analysis_request(self.db_path, job_id, request_data)

    def save_job_intermediate(self, job_id: str, intermediate_data: dict[str, Any]) -> None:
        """Persist job intermediate data."""
        persistence.save_job_intermediate(self.db_path, job_id, intermediate_data)

    def save_participant_resolution(self, session_id: str, resolution: dict[str, Any]) -> None:
        """Persist participant resolution."""
        persistence.update_session_participant_resolution(
            self.db_path, session_id, resolution
        )

    def save_session_result(self, session_id: str, result: dict[str, Any]) -> None:
        """Persist session result."""
        persistence.update_session_result(self.db_path, session_id, result)

    def save_processing_state(self, session_id: str, processing: dict[str, Any]) -> None:
        """Persist processing state."""
        persistence.update_session_processing(self.db_path, session_id, processing)

    def save_resume_scheduled_job(self, session_id: str, job_id: str) -> None:
        """Persist resume scheduled job."""
        persistence.save_resume_scheduled_job(self.db_path, session_id, job_id)

    def save_upstream_task(self, record: dict[str, Any]) -> None:
        """Persist an async upstream task reference."""
        super().save_upstream_task(record)
        merged = self.upstream_tasks[
            self._upstream_key(
                record["job_id"], record["capability"], record["upload_id"]
            )
        ]
        persistence.save_upstream_task(self.db_path, merged)

    def update_upstream_task(
        self,
        job_id: str,
        capability: str,
        upload_id: str,
        fields: dict[str, Any],
    ) -> None:
        """Persist a partial upstream task update."""
        super().update_upstream_task(job_id, capability, upload_id, fields)
        persistence.update_upstream_task(
            self.db_path, job_id, capability, upload_id, fields
        )

    def get_unfinished_jobs(self) -> list[dict[str, Any]]:
        """Get jobs that were in progress when the server stopped."""
        return persistence.get_unfinished_jobs(self.db_path)

    async def delete_session(self, session_id: str) -> None:
        """Delete a session and invalidate all derived state (jobs, uploads,
        idempotency mappings, analysis requests) in memory and in the DB."""
        async with self.lock:
            session = self.sessions.pop(session_id, None)
            if session is None:
                return
            removed_job_ids: set[str] = set()
            for job_id in [
                job_id for job_id, job in self.jobs.items()
                if job.session_id == session_id
            ]:
                self.jobs.pop(job_id, None)
                self.events.pop(job_id, None)
                removed_job_ids.add(job_id)
                self.idempotency = {
                    request_id: mapped
                    for request_id, mapped in self.idempotency.items()
                    if mapped != job_id
                }
            if removed_job_ids:
                self.upstream_tasks = {
                    key: record
                    for key, record in self.upstream_tasks.items()
                    if key[0] not in removed_job_ids
                }
        persistence.delete_session(self.db_path, session_id)

