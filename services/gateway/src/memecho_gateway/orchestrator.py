from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import media, media_cleanup, persistence, processing_details
from .alignment import align_intervals
from .contracts import validate_result
from .models import FileTransPhase, JobStatus, ProcessingStage
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


def _accepts_param(func: Any, name: str) -> bool:
    """True when ``func`` declares ``name`` (or swallows arbitrary kwargs)."""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()
    )


@dataclass
class ProviderOverrides:
    text_api_key: str = ""
    text_endpoint: str = ""
    text_model: str = ""
    audio_api_key: str = ""
    audio_endpoint: str = ""
    workspace_id: str = ""
    # Profile the overrides were resolved from; None means the env/header
    # compatibility path.
    profile_id: str | None = None
    # Capability gate: text-only profiles must not drive the audio modules.
    supports_audio: bool = True

    @property
    def text_kwargs(self) -> dict[str, str]:
        """Non-empty text LLM overrides suitable for **kwargs unpacking."""
        kw: dict[str, str] = {}
        if self.text_api_key:
            kw["api_key"] = self.text_api_key
        if self.text_endpoint:
            kw["base_url"] = self.text_endpoint
        if self.text_model:
            kw["model"] = self.text_model
        return kw

    @property
    def audio_kwargs(self) -> dict[str, str]:
        """Non-empty audio ASR overrides suitable for **kwargs unpacking."""
        kw: dict[str, str] = {}
        if self.audio_api_key:
            kw["api_key"] = self.audio_api_key
        if self.audio_endpoint:
            kw["base_url"] = self.audio_endpoint
        if self.workspace_id:
            kw["workspace_id"] = self.workspace_id
        return kw


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
        # Object storage is one optional transport among four; the pipeline
        # picks a transport from provider-declared media inputs instead.
        # Prefix resolution is deferred to prepare() so text-only jobs never
        # touch the OSS client.
        self.transports: list[media.MediaTransport] = media.default_transports(
            self.oss
        )

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

    FILETRANS_CAPABILITY = "file_transcription"

    def _save_upstream_submission(
        self,
        job_id: str | None,
        session: Any | None,
        upload_id: str | None,
        task_id: str,
        media_input: str,
    ) -> None:
        """Persist the upstream task reference immediately after submission.

        Persisting before any polling means a restart at any point can resume
        the same billable task instead of submitting a duplicate.
        """
        if not job_id or upload_id is None or not hasattr(self.store, "save_upstream_task"):
            return
        self.store.save_upstream_task(
            {
                "job_id": job_id,
                "capability": self.FILETRANS_CAPABILITY,
                "upload_id": upload_id,
                "session_id": session.id if session is not None else "",
                "provider": getattr(self.transcription, "provider_id", "unknown"),
                "media_input": media_input,
                "upstream_task_id": task_id,
                "status": persistence.UPSTREAM_STATUS_SUBMITTED,
                "poll_count": 0,
                "next_poll_at": None,
                "last_error_code": None,
            }
        )

    def _persist_upstream_phase(
        self,
        job_id: str | None,
        upload_id: str | None,
        phase_name: str,
        kwargs: dict[str, Any],
    ) -> None:
        """Mirror FileTrans phase transitions onto the persisted task row."""
        if not job_id or upload_id is None or not hasattr(self.store, "update_upstream_task"):
            return
        fields: dict[str, Any] = {}
        if phase_name == "queued":
            fields["status"] = persistence.UPSTREAM_STATUS_SUBMITTED
        elif phase_name == "polling":
            fields["status"] = persistence.UPSTREAM_STATUS_POLLING
            if kwargs.get("poll_attempts") is not None:
                fields["poll_count"] = int(kwargs["poll_attempts"])
            if kwargs.get("next_poll_after_ms") is not None:
                fields["next_poll_at"] = (
                    datetime.now(UTC)
                    + timedelta(milliseconds=int(kwargs["next_poll_after_ms"]))
                ).isoformat()
        elif phase_name == "downloading":
            fields["status"] = persistence.UPSTREAM_STATUS_DOWNLOADING
        elif phase_name == "timed_out":
            # Timeout keeps the upstream reference resumable: the task may
            # still be running upstream, so it must never be resubmitted.
            fields["status"] = persistence.UPSTREAM_STATUS_TIMEOUT
            fields["last_error_code"] = "upstream_timeout"
        elif phase_name == "failed":
            fields["status"] = persistence.UPSTREAM_STATUS_FAILED
            fields["last_error_code"] = kwargs.get("error_code") or "upstream_task_failed"
        elif phase_name == "succeeded":
            fields["status"] = persistence.UPSTREAM_STATUS_COMPLETED
        if fields:
            self.store.update_upstream_task(
                job_id, self.FILETRANS_CAPABILITY, upload_id, fields
            )

    async def _download_transcription_with_phase(
        self,
        prepared: media.PreparedMedia,
        session: Any | None,
        upload_id: str | None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Run FileTrans through the selected transport with phase tracking.

        A persisted upstream task reference (from a previous attempt or a
        gateway restart) is resumed — polling continues against the same
        task id and nothing billable is resubmitted.
        """
        existing = None
        if job_id is not None and upload_id is not None and hasattr(
            self.store, "get_upstream_task"
        ):
            existing = self.store.get_upstream_task(
                job_id, self.FILETRANS_CAPABILITY, upload_id
            )
        resumable = (
            existing is not None
            and bool(existing.get("upstream_task_id"))
            and existing.get("status") in persistence.UPSTREAM_RESUMABLE_STATUSES
            and hasattr(self.transcription, "resume_with_phase")
        )
        # Resumed polling continues the persisted attempt counter.
        poll_offset = int(existing.get("poll_count", 0)) if resumable else 0
        tracking = session is not None and upload_id is not None

        def on_phase(phase_name: str, **kwargs: Any) -> None:
            raw_task_id = kwargs.pop("task_id", None)
            if raw_task_id:
                self._save_upstream_submission(
                    job_id, session, upload_id, raw_task_id, prepared.input_type.value
                )
            if phase_name == "polling" and kwargs.get("poll_attempts") is not None:
                kwargs["poll_attempts"] = int(kwargs["poll_attempts"]) + poll_offset
            self._persist_upstream_phase(job_id, upload_id, phase_name, kwargs)
            if not tracking:
                return
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
                # after the download returns.

        on_submitted = None
        if job_id is not None and upload_id is not None and hasattr(
            self.store, "save_upstream_task"
        ):
            def on_submitted(task_id: str) -> None:
                self._save_upstream_submission(
                    job_id, session, upload_id, task_id, prepared.input_type.value
                )

        if resumable:
            log.info(
                "FileTrans resuming persisted upstream task job_id=%s status=%s",
                job_id,
                existing.get("status"),
            )
            resume_kwargs: dict[str, Any] = {}
            if _accepts_param(self.transcription.resume_with_phase, "on_phase"):
                resume_kwargs["on_phase"] = on_phase
            return await self.transcription.resume_with_phase(
                existing["upstream_task_id"], **resume_kwargs
            )

        if prepared.input_type != media.MediaInput.public_url:
            if not hasattr(self.transcription, "download_with_media"):
                raise media.MediaInputUnsupportedError(
                    getattr(self.transcription, "provider_id", "transcription"),
                    self.FILETRANS_CAPABILITY,
                    (prepared.input_type,),
                    tuple(transport.capability for transport in self.transports),
                )
            media_kwargs: dict[str, Any] = {"on_phase": on_phase}
            if _accepts_param(self.transcription.download_with_media, "on_submitted"):
                media_kwargs["on_submitted"] = on_submitted
            return await self.transcription.download_with_media(prepared, **media_kwargs)

        if hasattr(self.transcription, "download_with_phase"):
            phase_kwargs: dict[str, Any] = {"on_phase": on_phase}
            if _accepts_param(self.transcription.download_with_phase, "on_submitted"):
                phase_kwargs["on_submitted"] = on_submitted
            return await self.transcription.download_with_phase(
                prepared.url, **phase_kwargs
            )
        return await self.transcription.download(prepared.url)

    async def _collect_remote_observations(
        self,
        prepared: media.PreparedMedia | str,
        session: Any | None = None,
        upload_id: str | None = None,
        job_id: str | None = None,
        audio_kwargs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if isinstance(prepared, str):
            prepared = media.PreparedMedia(
                input_type=media.MediaInput.public_url,
                transport_id="public_url",
                url=prepared,
            )
        calls: dict[str, Any] = {}
        audio_url = (
            prepared.url
            if prepared.input_type == media.MediaInput.public_url
            else None
        )
        if self.dashscope and audio_url:
            calls["fun_asr"] = self.dashscope.submit_fun_asr(audio_url, **(audio_kwargs or {}))
            calls["emotion"] = self.dashscope.submit_emotion(audio_url, **(audio_kwargs or {}))
        if self.transcription:
            calls["transcription"] = self._download_transcription_with_phase(
                prepared, session, upload_id, job_id,
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
                # Do not log the exception itself: vendor messages can embed
                # signed result URLs. The stable error code is enough.
                log.warning(
                    "Audio model failed source=%s error_code=%s",
                    name,
                    processing_details.safe_error_code(exc),
                )
                collected["errors"].append(
                    {
                        "source": name,
                        "error_code": processing_details.safe_error_code(exc),
                    }
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
                        "Audio model result normalization failed source=fun_asr "
                        "error_code=%s",
                        processing_details.safe_error_code(normalize_exc),
                    )
                    collected["errors"].append(
                        {
                            "source": "fun_asr",
                            "error_code": processing_details.safe_error_code(
                                normalize_exc
                            ),
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
        self, job_id: str, session: Any, request: dict[str, Any],
        overrides: ProviderOverrides | None = None,
        provider: Any | None = None,
    ) -> None:
        effective_provider = provider or self.provider
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
        if hasattr(self.store, 'save_job_intermediate'):
            self.store.save_job_intermediate(job_id, session.job_intermediates[job_id])
        processing_details.set_alignment(session, 0)

        await self.store.update_job(
            job_id, JobStatus.analyzing, 66, "Qwen3.7 is analyzing text"
        )
        session.resume_scheduled_jobs.discard(job_id)
        processing_details.set_qwen(session, ProcessingStage.running)
        text_kwargs = overrides.text_kwargs if overrides else {}
        result = await effective_provider.analyze(
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
            **text_kwargs,
        )
        enforce_text_only_metadata(result)
        result["_evidence_weights"] = evidence_weights

        errors = validate_result(result, text_segments=text_segments)
        if errors:
            processing_details.set_qwen(
                session, ProcessingStage.failed, "invalid_upstream_result"
            )
            raise _contract_error(errors)
        processing_details.set_qwen(session, ProcessingStage.succeeded)

        await self.store.update_job(
            job_id, JobStatus.rendering, 90, "Rendering local report"
        )
        result["rendered_markdown"] = render_markdown(result)
        result["rendered_html"] = render_html(result)
        session.result = result
        if hasattr(self.store, 'save_session_result'):
            self.store.save_session_result(session.id, result)
        await self.store.update_job(job_id, JobStatus.complete, 100, "Report complete")

    async def run(
        self,
        job_id: str,
        session_id: str,
        request: dict[str, Any],
        overrides: ProviderOverrides | None = None,
        provider: Any | None = None,
    ) -> None:
        session = self.store.sessions[session_id]
        request = session.analysis_requests.get(job_id, request)
        prepared_cleanup: list[tuple[media.MediaTransport, media.PreparedMedia]] = []
        text_kwargs = overrides.text_kwargs if overrides else {}
        audio_kw = overrides.audio_kwargs if overrides else {}
        # Text-only profiles must never drive remote audio modules; the gate
        # keeps capability-driven selection out of business branches.
        audio_enabled = overrides.supports_audio if overrides else True
        effective_provider = provider or self.provider
        try:
            job = self.store.jobs[job_id]
            is_resume = job.status == JobStatus.awaiting_identity
            if is_text_only_request(request):
                await self._run_text_only(job_id, session, request, overrides, provider)
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

                remote_tracks: list[tuple[Any, Path, media.PreparedMedia]] = []
                transport: media.MediaTransport | None = None
                if audio_enabled and completed_uploads:
                    available_caps = tuple(
                        item.capability for item in self.transports
                    )
                    # Legacy clients that declare no media inputs keep the
                    # historical preference: object storage first when it is
                    # configured, direct transports otherwise.
                    preferred = (
                        media.MediaInput.public_url,
                        media.MediaInput.binary_upload,
                        media.MediaInput.local_path,
                        media.MediaInput.base64_inline,
                    )
                    fallback = tuple(
                        item for item in preferred if item in available_caps
                    )
                    if self.transcription or self.dashscope:
                        accepted = media.compatible_media_inputs(
                            [self.transcription, self.dashscope], fallback
                        )
                        if not accepted:
                            declared = tuple(
                                dict.fromkeys(
                                    item
                                    for client in (self.transcription, self.dashscope)
                                    if client is not None
                                    for item in media.accepted_media_inputs(
                                        client, fallback
                                    )
                                )
                            )
                            for upload in completed_uploads:
                                for module in processing_details.REMOTE_MODULES:
                                    processing_details.set_module(
                                        session,
                                        upload.id,
                                        module,
                                        ProcessingStage.failed,
                                        error_code="media_input_unsupported",
                                    )
                                processing_details.set_oss(
                                    session, upload.id, ProcessingStage.skipped
                                )
                            raise media.MediaInputUnsupportedError(
                                "audio-pipeline",
                                Orchestrator.FILETRANS_CAPABILITY,
                                declared,
                                available_caps,
                            )
                    else:
                        accepted = fallback
                    transport = media.select_transport(accepted, self.transports)
                    if transport is None:
                        # Declared inputs exist but no transport can satisfy
                        # them (e.g. public_url required without object
                        # storage). Surface this loudly; skipping would fake
                        # a successful analysis with no evidence.
                        for upload in completed_uploads:
                            for module in processing_details.REMOTE_MODULES:
                                processing_details.set_module(
                                    session,
                                    upload.id,
                                    module,
                                    ProcessingStage.failed,
                                    error_code="media_input_unsupported",
                                )
                            processing_details.set_oss(
                                session, upload.id, ProcessingStage.skipped
                            )
                        raise media.MediaInputUnsupportedError(
                            "audio-pipeline",
                            Orchestrator.FILETRANS_CAPABILITY,
                            tuple(accepted),
                            available_caps,
                        )

                if transport is not None:
                    for upload in completed_uploads:
                        path = Path(upload.completed_path)
                        is_url_transport = (
                            transport.capability == media.MediaInput.public_url
                        )
                        if is_url_transport:
                            processing_details.set_oss(
                                session, upload.id, ProcessingStage.running
                            )
                        try:
                            prepared = await transport.prepare(
                                media.MediaRequest(
                                    session_id=session_id,
                                    upload_id=upload.id,
                                    path=path,
                                    file_name=path.name,
                                    mime_type=upload.mime_type,
                                    size_bytes=path.stat().st_size,
                                )
                            )
                        except Exception:
                            if is_url_transport:
                                processing_details.set_oss(
                                    session, upload.id, ProcessingStage.failed
                                )
                            raise
                        if is_url_transport:
                            processing_details.set_oss(
                                session, upload.id, ProcessingStage.succeeded
                            )
                        else:
                            processing_details.set_oss(
                                session, upload.id, ProcessingStage.skipped
                            )
                        prepared_cleanup.append((transport, prepared))
                        remote_tracks.append((upload, path, prepared))

                if not remote_tracks:
                    skipped = list(processing_details.REMOTE_MODULES)
                    for upload in completed_uploads:
                        processing_details.mark_skipped_modules(
                            session, upload.id, tuple(skipped)
                        )
                else:
                    missing: list[str] = []
                    url_transport = (
                        transport is not None
                        and transport.capability == media.MediaInput.public_url
                    )
                    if not (self.dashscope and url_transport):
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
                            item[2], session=session, upload_id=item[0].id,
                            job_id=job_id,
                            audio_kwargs=audio_kw if audio_kw else None,
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
                    # Remote modules fail independently per track. Preserve that
                    # scope so a silent microphone cannot invalidate usable text
                    # recovered from the system-audio track.
                    model_errors.extend(
                        {**error, "track": upload.track}
                        for error in observation["errors"]
                    )

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
                successful_transcript_tracks = sorted(
                    {
                        upload.track
                        for upload in completed_uploads
                        if observations_by_upload_id.get(upload.id, {}).get("transcript")
                    }
                )
                failed_transcript_tracks = sorted(
                    {
                        upload.track
                        for upload in completed_uploads
                        if any(
                            error.get("source") == "transcription"
                            for error in observations_by_upload_id.get(upload.id, {}).get(
                                "errors", []
                            )
                        )
                    }
                )
                evidence_availability = {
                    "has_usable_text": bool(aligned or transcription_segments),
                    "aligned_segment_count": len(aligned),
                    "transcript_segment_count": len(transcription_segments),
                    "successful_transcript_tracks": successful_transcript_tracks,
                    "failed_transcript_tracks": failed_transcript_tracks,
                }

                session.job_intermediates[job_id] = {
                    "aligned": aligned,
                    "quality_metrics": quality_metrics,
                    "tracks": tracks,
                    "track_labels": track_labels,
                    "model_errors": model_errors,
                    "evidence_weights": evidence_weights,
                    "evidence_availability": evidence_availability,
                }
                if hasattr(self.store, 'save_job_intermediate'):
                    self.store.save_job_intermediate(job_id, session.job_intermediates[job_id])
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
                evidence_availability = intermediate.get("evidence_availability") or {
                    "has_usable_text": bool(aligned),
                    "aligned_segment_count": len(aligned),
                    "transcript_segment_count": len(aligned),
                    "successful_transcript_tracks": [],
                    "failed_transcript_tracks": [],
                }
                processing_details.set_alignment(session, len(aligned))

            # Check participant resolution before analysis
            if not session.participant_resolution and aligned:
                # Multiple speakers detected but no resolution provided
                await self.store.update_job(job_id, JobStatus.awaiting_identity, 55, "等待参与者身份确认")
                return

            await self.store.update_job(job_id, JobStatus.analyzing, 66, "Qwen3.7 正在形成回声")
            session.resume_scheduled_jobs.discard(job_id)
            processing_details.set_qwen(session, ProcessingStage.running)
            result = await effective_provider.analyze(
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
                        "evidence_availability": evidence_availability,
                    },
                },
                tracks=track_labels,
                request=request,
                **text_kwargs,
            )

            if result.get("analysis_mode") == "text_only":
                # Providers may conservatively downgrade an audio session when
                # usable acoustic evidence is absent. Apply the same
                # deterministic metadata guarantees as an explicit text input
                # before contract validation.
                enforce_text_only_metadata(result)
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
                processing_details.set_qwen(
                    session, ProcessingStage.failed, "invalid_upstream_result"
                )
                raise _contract_error(errors)
            processing_details.set_qwen(session, ProcessingStage.succeeded)

            await self.store.update_job(job_id, JobStatus.rendering, 90, "生成本地报告")
            result["rendered_markdown"] = render_markdown(result)
            result["rendered_html"] = render_html(result)
            session.result = result
            if hasattr(self.store, 'save_session_result'):
                self.store.save_session_result(session.id, result)
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
            error_code = (
                exc.error_code
                if isinstance(exc, media.MediaInputUnsupportedError)
                else type(exc).__name__
            )
            await self.store.update_job(
                job_id,
                JobStatus.failed,
                current_progress,
                "分析失败",
                retryable=True,
                error_code=error_code,
                error_detail=error_detail,
            )
        finally:
            session.resume_scheduled_jobs.discard(job_id)
            for cleanup_transport, prepared in prepared_cleanup:
                try:
                    await cleanup_transport.cleanup(prepared)
                except Exception:
                    log.warning(
                        "Media transport cleanup failed transport=%s",
                        cleanup_transport.transport_id,
                        exc_info=True,
                    )
            try:
                media_cleanup.remove_session_media(
                    self.store, session_id, self.media_retention_seconds
                )
            except Exception:
                log.warning(
                    "Local media cleanup failed session=%s", session_id, exc_info=True
                )
