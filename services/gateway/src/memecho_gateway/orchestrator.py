from __future__ import annotations

import asyncio
from typing import Any

from .contracts import validate_result
from .models import JobStatus
from .store import MemoryStore


class Orchestrator:
    def __init__(self, store: MemoryStore, provider: Any):
        self.store = store
        self.provider = provider

    async def run(self, job_id: str, session_id: str, request: dict[str, Any]) -> None:
        session = self.store.sessions[session_id]
        try:
            await self.store.update_job(job_id, JobStatus.transcribing, 20, "正式转写与说话人分离")
            await asyncio.sleep(0.05)
            tracks = [
                str(upload.completed_path)
                for upload in session.uploads.values()
                if upload.completed_path
            ]
            await self.store.update_job(job_id, JobStatus.aligning, 48, "对齐语言与声学证据")
            await asyncio.sleep(0.05)
            await self.store.update_job(job_id, JobStatus.analyzing, 66, "Qwen3.7 正在形成回声")
            result = await self.provider.analyze(
                session={
                    "id": session.id,
                    "title": session.create.title,
                    "context": session.create.context,
                    "occurred_at": session.create.occurred_at.isoformat(),
                    "participant_resolution": session.participant_resolution,
                },
                tracks=tracks,
                request=request,
            )
            errors = validate_result(result)
            if errors:
                raise ValueError("; ".join(errors))
            await self.store.update_job(job_id, JobStatus.rendering, 90, "生成本地报告")
            session.result = result
            await self.store.update_job(job_id, JobStatus.complete, 100, "报告已完成")
        except Exception as exc:
            await self.store.update_job(
                job_id,
                JobStatus.failed,
                100,
                "分析失败",
                retryable=True,
                error_code=type(exc).__name__,
            )

