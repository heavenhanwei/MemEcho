from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from . import media_cleanup, processing_details
from .alignment import align_intervals
from .contracts import validate_result
from .models import FileTransPhase, JobStatus, ProcessingStage
from .providers.oss import make_oss_key
from .quality import compute_quality_metrics, conservative_evidence_weights
from .rendering import render_html, render_markdown
from .text_only import (
    build_text_segments,
    enforce_text_only_metadata,
    is_text_only_request,
)
from .store import MemoryStore

log = logging.getLogger(__name__)


class AnalysisContractError(ValueError):
    """The provider returned JSON that violates the memEcho result contract."""


def _contract_error(errors: list[str]) -> AnalysisContractError:
    # validate_result emits field paths and invariant messages only. Keep the
    # response bounded and do not expose transcript text or raw model output.
    detail = "; ".join(errors[:12])
    if len(errors) > 12:
        detail += f"; and {len(errors) - 12} more validation errors"
    return AnalysisContractError(detail[:2000])


class Orchestrator:
    def __init__(
        self,
        store: MemoryStore,
        provider: Any,
        oss_client: Any | None = None,
        dashscope_client: Any | None = None,
        transcription_downloader: Any | None = None,
        media_retention_seconds: float = 24 * 60 * 60,
    ):
        self.store = store
        self.provider = provider
        self.oss = oss_client
        self.dashscope = dashscope_client
        self.transcription = transcription_downloader
        self.media_retention_seconds = media_retention_seconds

    async def _invoke_module(
        self,
        name: str,
        coro: Any,
        session: Any | None,
        upload_id: str | None,
    ) -> tuple[Any, BaseException | None, int]:
        if session is not None and upload_id is not None:
            processing_details.set_module(
                session, upload_id, name, ProcessingStage.running
            )
        started = time.monotonic()
        try:
            value = await coro
        except BaseException as exc:  # noqa: BLE001 - status is recorded, then re-raised
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if session is not None and upload_id is not None:
                processing_details.set_module(
                    session,
                    upload_id,
                    name,
                    ProcessingStage.failed,
                    error_code=processing_details.safe_error_code(exc),
                    elapsed_ms=elapsed_ms,
                )
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if session is not None and upload_id is not None:
            processing_details.set_module(
                session, upload_id, name, ProcessingStage.succeeded, elapsed_ms=elapsed_ms
            )
        return value, None, elapsed_ms

    async def _download_transcription_with_phase(
        self,
        audio_url: str,
        session: Any | None,
        upload_id: str | None,
    ) -> dict[str, Any]:
        """Download transcription with real-time phase tracking."""
        has_tracking = (
            session is not None
            and upload_id is not None
            and hasattr(self.transcription, "download_with_phase")
        )
        if not has_tracking:
            return await self.transcription.download(audio_url)

        def on_phase(phase_name: str, **kwargs: Any) -> None:
            try:
                phase = FileTransPhase(phase_name)
            except ValueError:
                return
            # Extract stats that belong in a separate call.
            sentence_count = kwargs.pop("sentence_count", None)
            language = kwargs.pop("language", None)
            audio_duration_ms = kwargs.pop("audio_duration_ms", None)
            processing_details.set_filetrans_phase(
                session, upload_id, phase, **kwargs  # type: ignore[arg-type]
            )
            if phase == FileTransPhase.succeeded and sentence_count is not None:
                processing_details.set_filetrans_stats(
                    session,
                    upload_id,
                    sentence_count=sentence_count,
                    language=language,
                    audio_duration_ms=audio_duration_ms,
                )
                # The transcript segments will be added by the caller
                # after download_with_phase returns.

        return await self.transcription.download_with_phase(
            audio_url, on_phase=on_phase,
        )

    async def _collect_remote_observations(
        self,
        audio_url: str,
        session: Any | None = None,
        upload_id: str | None = None,
    ) -> dict[str, Any]:
        calls: dict[str, Any] = {}
        if self.dashscope:
            calls["fun_asr"] = self.dashscope.submit_fun_asr(audio_url)
            calls["emotion"] = self.dashscope.submit_emotion(audio_url)
        if self.transcription:
            calls["transcription"] = self._download_transcription_with_phase(
                audio_url, session, upload_id,
            )
        if not calls:
            return {
                "diarization": [],
                "emotions": [],
                "transcript": [],
                "errors": [],
                "filetrans": None,
            }

        async def guarded(name: str, coro: Any) -> tuple[str, Any, BaseException | None]:
            try:
                value, _, _ = await self._invoke_module(name, coro, session, upload_id)
                return name, value, None
            except BaseException as exc:  # noqa: BLE001 - degradation is recorded per module
                return name, None, exc

        outcomes = await asyncio.gather(
            *(guarded(name, coro) for name, coro in calls.items())
        )
        collected: dict[str, Any] = {
            "diarization": [],
            "emotions": [],
            "transcript": [],
            "errors": [],
            "filetrans": None,
        }
        for name, value, exc in outcomes:
            if exc is not None:
                log.warning("Audio model failed source=%s", name, exc_info=exc)
                collected["errors"].append(
                    {"source": name, "error_code": type(exc).__name__}
                )
                if name == "transcription" and session is not None and upload_id is not None:
                    error_phase = (
                        FileTransPhase.timed_out
                        if isinstance(exc, TimeoutError | asyncio.TimeoutError)
                        else FileTransPhase.failed
                    )
                    processing_details.set_filetrans_phase(
                        session,
                        upload_id,
                        error_phase,
                        error_code=processing_details.safe_error_code(exc),
                        retryable=True,
                    )
            elif name == "fun_asr":
                try:
                    if self.transcription and hasattr(
                        self.transcription, "download_diarization"
                    ):
                        collected["diarization"] = (
                            await self.transcription.download_diarization(value)
                        )
                    else:
                        collected["diarization"] = value.get("output", {}).get(
                            "results", []
                        )
                except Exception as normalize_exc:
                    log.warning(
                        "Audio model result normalization failed source=fun_asr",
                        exc_info=normalize_exc,
                    )
                    collected["errors"].append(
                        {
                            "source": "fun_asr",
                            "error_code": type(normalize_exc).__name__,
                        }
                    )
                    if session is not None and upload_id is not None:
                        processing_details.set_module(
                            session,
                            upload_id,
                            "fun_asr",
                            ProcessingStage.failed,
                            error_code=processing_details.safe_error_code(normalize_exc),
                        )
            elif name == "emotion":
                collected["emotions"] = value.get("output", {}).get("results", [])
            else:
                collected["transcript"] = value.get("transcript", [])
                collected["filetrans"] = {
                    "sentence_count": len(value.get("transcript", [])),
                    "language": value.get("language"),
                    "audio_duration_ms": value.get("duration_ms"),
                }
                if session is not None and upload_id is not None:
                    processing_details.set_filetrans_stats(
                        session,
                        upload_id,
                        sentence_count=collected["filetrans"]["sentence_count"],
                        language=collected["filetrans"]["language"],
                        audio_duration_ms=collected["filetrans"]["audio_duration_ms"],
                    )
                    processing_details.add_transcript(
                        session, value.get("transcript", [])
                    )
        transcript_emotions = []
        for segment in collected["transcript"]:
            emotion = str(segment.get("emotion", "unknown"))
            if emotion and emotion != "unknown":
                transcript_emotions.append(
                    {
                        "start_ms": segment.get("start_ms"),
                        "end_ms": segment.get("end_ms"),
                        "emotion": emotion,
                        "confidence": segment.get("emotion_confidence", 0.0),
                    }
                )
        if transcript_emotions:
            collected["emotions"] = transcript_emotions
        return collected

    async def _run_text_only(
        self, job_id: str, session: Any, request: dict[str, Any]
    ) -> None:
        source = request.get("source") or {}
        text = source.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text-only analysis requires non-empty source.text")

        await self.store.update_job(
            job_id, JobStatus.aligning, 48, "Preparing text evidence"
        )
        text_segments = build_text_segments(text)
        if not text_segments:
            raise ValueError("text-only analysis produced no usable text segments")

        evidence_weights = {
            "quality_tier": "text_only",
            "linguistic_weight": 1.0,
            "acoustic_weight": 0.0,
            "aggregation": "text_only",
        }
        observations = {
            "text_segments": text_segments,
            "aligned_segments": [],
            "acoustic_metrics": [],
            "model_errors": [],
            "evidence_weights": evidence_weights,
        }
        session.job_intermediates[job_id] = {
            "text_segments": text_segments,
            "aligned": [],
            "quality_metrics": [],
            "tracks": [],
            "track_labels": [],
            "model_errors": [],
            "evidence_weights": evidence_weights,
        }
        processing_details.set_alignment(session, 0)

        await self.store.update_job(
            job_id, JobStatus.analyzing, 66, "Qwen3.7 is analyzing text"
        )
        session.resume_scheduled_jobs.discard(job_id)
        processing_details.set_qwen(session, ProcessingStage.running)
        result = await self.provider.analyze(
            session={
                "id": session.id,
                "title": session.create.title,
                "context": session.create.context,
                "occurred_at": session.create.occurred_at.isoformat(),
                "participant_resolution": session.participant_resolution,
                "observations": observations,
            },
            tracks=[],
            request=request,
        )
        processing_details.set_qwen(session, ProcessingStage.succeeded)
        enforce_text_only_metadata(result)
        result["_evidence_weights"] = evidence_weights

        errors = validate_result(result, text_segments=text_segments)
        if errors:
            raise _contract_error(errors)

        await self.store.update_job(
            job_id, JobStatus.rendering, 90, "Rendering local report"
        )
        result["rendered_markdown"] = render_markdown(result)
        result["rendered_html"] = render_html(result)
        session.result = result
        await self.store.update_job(job_id, JobStatus.complete, 100, "Report complete")

    async def run(self, job_id: str, session_id: str, request: dict[str, Any]) -> None:
        session = self.store.sessions[session_id]
        request = session.analysis_requests.get(job_id, request)
        oss_keys: list[str] = []
        try:
            job = self.store.jobs[job_id]
            is_resume = job.status == JobStatus.awaiting_identity
            if is_text_only_request(request):
                await self._run_text_only(job_id, session, request)
                return

            if not is_resume:
                await self.store.update_job(job_id, JobStatus.transcribing, 20, "正式转写与说话人分离")
                completed_uploads = [
                    upload
                    for upload in session.uploads.values()
                    if upload.completed_path
                ]
                tracks = [str(upload.completed_path) for upload in completed_uploads]
                diarization: list[dict[str, Any]] = []
                emotions: list[dict[str, Any]] = []
                transcription_segments: list[dict[str, Any]] = []
                model_errors: list[dict[str, str]] = []

                remote_tracks: list[tuple[Any, Path, str]] = []
                if self.oss and completed_uploads:
                    prefix = getattr(
                        getattr(self.oss, "settings", None),
                        "oss_prefix",
                        "memecho-tmp",
                    )
                    for upload in completed_uploads:
                        path = Path(upload.completed_path)
                        oss_key = make_oss_key(prefix, session_id, upload.id, path.name)
                        oss_keys.append(oss_key)
                        processing_details.set_oss(
                            session, upload.id, ProcessingStage.running
                        )
                        try:
                            await self.oss.upload_file(oss_key, path, upload.mime_type)
                            audio_url = await self.oss.signed_url(oss_key)
                        except Exception:
                            processing_details.set_oss(
                                session, upload.id, ProcessingStage.failed
                            )
                            raise
                        processing_details.set_oss(
                            session, upload.id, ProcessingStage.succeeded
                        )
                        remote_tracks.append((upload, path, audio_url))

                if not remote_tracks:
                    skipped = list(processing_details.REMOTE_MODULES)
                    for upload in completed_uploads:
                        processing_details.mark_skipped_modules(
                            session, upload.id, tuple(skipped)
                        )
                else:
                    missing: list[str] = []
                    if not self.dashscope:
                        missing.extend(["fun_asr", "emotion"])
                    if not self.transcription:
                        missing.append("transcription")
                    for upload, _, _ in remote_tracks:
                        processing_details.mark_skipped_modules(
                            session, upload.id, tuple(missing)
                        )

                observations = await asyncio.gather(
                    *(
                        self._collect_remote_observations(
                            item[2], session=session, upload_id=item[0].id
                        )
                        for item in remote_tracks
                    )
                )
                observations_by_upload_id: dict[str, dict[str, Any]] = {}
                for remote_track, observation in zip(remote_tracks, observations, strict=True):
                    upload = remote_track[0]
                    observations_by_upload_id[upload.id] = observation
                    diarization.extend(observation["diarization"])
                    emotions.extend(observation["emotions"])
                    transcription_segments.extend(observation["transcript"])
                    model_errors.extend(observation["errors"])

                await self.store.update_job(job_id, JobStatus.aligning, 48, "对齐语言与声学证据")
                if transcription_segments and (diarization or emotions):
                    aligned = align_intervals(transcription_segments, diarization, emotions)
                    log.info("Aligned %d segments for session %s", len(aligned), session_id)
                else:
                    aligned = []
                processing_details.set_alignment(session, len(aligned))

                quality_metrics: list[dict[str, Any]] = []
                for upload in completed_uploads:
                    path = Path(upload.completed_path)
                    if path.suffix.lower() == ".wav":
                        observation = observations_by_upload_id.get(upload.id)
                        metrics = compute_quality_metrics(
                            path,
                            transcript_segments=(
                                observation["transcript"] if observation is not None else None
                            ),
                            speaker_segments=(
                                observation["diarization"] if observation is not None else None
                            ),
                        )
                        metrics["track"] = upload.track
                        quality_metrics.append(metrics)
                evidence_weights = conservative_evidence_weights(quality_metrics)
                track_labels = [upload.track for upload in completed_uploads]

                session.job_intermediates[job_id] = {
                    "aligned": aligned,
                    "quality_metrics": quality_metrics,
                    "tracks": tracks,
                    "track_labels": track_labels,
                    "model_errors": model_errors,
                    "evidence_weights": evidence_weights,
                }
            else:
                intermediate = session.job_intermediates.get(job_id)
                if intermediate is None:
                    raise RuntimeError("analysis intermediate is unavailable")
                aligned = intermediate.get("aligned", [])
                quality_metrics = intermediate.get("quality_metrics", [])
                tracks = intermediate.get("tracks", [])
                track_labels = intermediate.get("track_labels", [Path(item).name for item in tracks])
                model_errors = intermediate.get("model_errors", [])
                evidence_weights = intermediate.get("evidence_weights") or conservative_evidence_weights(quality_metrics)
                processing_details.set_alignment(session, len(aligned))

            # Check participant resolution before analysis
            if not session.participant_resolution and aligned:
                # Multiple speakers detected but no resolution provided
                await self.store.update_job(job_id, JobStatus.awaiting_identity, 55, "等待参与者身份确认")
                return

            await self.store.update_job(job_id, JobStatus.analyzing, 66, "Qwen3.7 正在形成回声")
            session.resume_scheduled_jobs.discard(job_id)
            processing_details.set_qwen(session, ProcessingStage.running)
            result = await self.provider.analyze(
                session={
                    "id": session.id,
                    "title": session.create.title,
                    "context": session.create.context,
                    "occurred_at": session.create.occurred_at.isoformat(),
                    "participant_resolution": session.participant_resolution,
                    "observations": {
                        "aligned_segments": aligned,
                        "acoustic_metrics": quality_metrics,
                        "model_errors": model_errors,
                        "evidence_weights": evidence_weights,
                    },
                },
                tracks=track_labels,
                request=request,
            )
            processing_details.set_qwen(session, ProcessingStage.succeeded)

            if result.get("analysis_mode") == "text_only":
                evidence_weights = conservative_evidence_weights([])
            for point in result.get("vad_series", []):
                point["linguistic_weight"] = evidence_weights["linguistic_weight"]
                point["acoustic_weight"] = evidence_weights["acoustic_weight"]

            if quality_metrics:
                result["_quality_metrics"] = quality_metrics
            if aligned:
                result["_aligned_segments"] = aligned
            if model_errors:
                result["_model_errors"] = model_errors
            result["_evidence_weights"] = evidence_weights

            errors = validate_result(result)
            if errors:
                raise _contract_error(errors)

            await self.store.update_job(job_id, JobStatus.rendering, 90, "生成本地报告")
            result["rendered_markdown"] = render_markdown(result)
            result["rendered_html"] = render_html(result)
            session.result = result
            await self.store.update_job(job_id, JobStatus.complete, 100, "报告已完成")

        except Exception as exc:
            log.exception(
                "Analysis job failed job_id=%s session_id=%s error_type=%s",
                job_id,
                session_id,
                type(exc).__name__,
            )
            current_progress = self.store.jobs[job_id].progress
            error_detail = str(exc) if isinstance(exc, AnalysisContractError) else None
            if session.processing.get("qwen_status") == ProcessingStage.running.value:
                processing_details.set_qwen(
                    session,
                    ProcessingStage.failed,
                    processing_details.safe_error_code(exc),
                )
            await self.store.update_job(
                job_id,
                JobStatus.failed,
                current_progress,
                "分析失败",
                retryable=True,
                error_code=type(exc).__name__,
                error_detail=error_detail,
            )
        finally:
            session.resume_scheduled_jobs.discard(job_id)
            if self.oss and oss_keys:
                for key in oss_keys:
                    try:
                        await self.oss.delete(key)
                    except Exception:
                        log.warning("OSS cleanup failed for key=%s", key, exc_info=True)
            try:
                media_cleanup.remove_session_media(
                    self.store, session_id, self.media_retention_seconds
                )
            except Exception:
                log.warning(
                    "Local media cleanup failed session=%s", session_id, exc_info=True
                )
