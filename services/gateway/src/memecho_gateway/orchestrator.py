from __future__ import annotations

import asyncio
import logging
from typing import Any

from .alignment import align_intervals
from .contracts import validate_result
from .models import JobStatus
from .quality import compute_quality_metrics
from .rendering import render_html, render_markdown
from .store import MemoryStore

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        store: MemoryStore,
        provider: Any,
        oss_client: Any | None = None,
        dashscope_client: Any | None = None,
        transcription_downloader: Any | None = None,
    ):
        self.store = store
        self.provider = provider
        self.oss = oss_client
        self.dashscope = dashscope_client
        self.transcription = transcription_downloader

    async def run(self, job_id: str, session_id: str, request: dict[str, Any]) -> None:
        session = self.store.sessions[session_id]
        request = session.analysis_requests.get(job_id, request)
        oss_keys: list[str] = []
        try:
            job = self.store.jobs[job_id]
            is_resume = job.status == JobStatus.awaiting_identity

            if not is_resume:
                await self.store.update_job(job_id, JobStatus.transcribing, 20, "正式转写与说话人分离")
                tracks = [
                    str(upload.completed_path)
                    for upload in session.uploads.values()
                    if upload.completed_path
                ]

                diarization: list[dict[str, Any]] = []
                emotions: list[dict[str, Any]] = []
                transcription_segments: list[dict[str, Any]] = []

                if self.oss and tracks:
                    for track_path in tracks:
                        from pathlib import Path

                        p = Path(track_path)
                        data = p.read_bytes()
                        oss_key = f"memecho-tmp/{session_id}/{p.name}"
                        await self.oss.upload(oss_key, data, "audio/wav")
                        oss_keys.append(oss_key)
                        audio_url = await self.oss.signed_url(oss_key)

                        if self.dashscope:
                            diar_result = await self.dashscope.submit_fun_asr(audio_url)
                            diarization.extend(diar_result.get("output", {}).get("results", []))

                            emo_result = await self.dashscope.submit_emotion(audio_url)
                            emotions.extend(emo_result.get("output", {}).get("results", []))

                if self.transcription and tracks:
                    for oss_key in oss_keys:
                        audio_url = await self.oss.signed_url(oss_key)
                        tx = await self.transcription.download(audio_url)
                        transcription_segments.extend(tx.get("transcript", []))

                await self.store.update_job(job_id, JobStatus.aligning, 48, "对齐语言与声学证据")
                if transcription_segments and (diarization or emotions):
                    aligned = align_intervals(transcription_segments, diarization, emotions)
                    log.info("Aligned %d segments for session %s", len(aligned), session_id)
                else:
                    aligned = []

                quality_metrics: list[dict[str, Any]] = []
                if tracks:
                    from pathlib import Path

                    for track_path in tracks:
                        p = Path(track_path)
                        if p.suffix.lower() == ".wav":
                            qm = compute_quality_metrics(p)
                            quality_metrics.append(qm)

                session.job_intermediates[job_id] = {
                    "aligned": aligned,
                    "quality_metrics": quality_metrics,
                    "tracks": tracks,
                }
            else:
                intermediate = session.job_intermediates.get(job_id)
                if intermediate is None:
                    raise RuntimeError("analysis intermediate is unavailable")
                aligned = intermediate.get("aligned", [])
                quality_metrics = intermediate.get("quality_metrics", [])
                tracks = intermediate.get("tracks", [])

            # Check participant resolution before analysis
            if not session.participant_resolution and aligned:
                # Multiple speakers detected but no resolution provided
                await self.store.update_job(job_id, JobStatus.awaiting_identity, 55, "等待参与者身份确认")
                return

            await self.store.update_job(job_id, JobStatus.analyzing, 66, "Qwen3.7 正在形成回声")
            session.resume_scheduled_jobs.discard(job_id)
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

            if quality_metrics:
                result["_quality_metrics"] = quality_metrics
            if aligned:
                result["_aligned_segments"] = aligned

            errors = validate_result(result)
            if errors:
                raise ValueError("; ".join(errors))

            await self.store.update_job(job_id, JobStatus.rendering, 90, "生成本地报告")
            result["rendered_markdown"] = render_markdown(result)
            result["rendered_html"] = render_html(result)
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
        finally:
            session.resume_scheduled_jobs.discard(job_id)
            if self.oss and oss_keys:
                for key in oss_keys:
                    try:
                        await self.oss.delete(key)
                    except Exception:
                        log.warning("OSS cleanup failed for key=%s", key, exc_info=True)
