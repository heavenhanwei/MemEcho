from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import JobStatus, JobView, SessionCreate


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


class MemoryStore:
    """Roadshow store. Sensitive results remain process-local and expire on restart."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.sessions: dict[str, SessionRecord] = {}
        self.jobs: dict[str, JobView] = {}
        self.events: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.idempotency: dict[str, str] = {}
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

