"""Sanitized pipeline observability state for the processing-details endpoint.

The state lives on ``SessionRecord.processing`` as plain dicts so the
orchestrator can update it incrementally. Only stable error codes, counters,
and normalized transcript segments are stored here — never credentials,
signed URLs, vendor payloads, or absolute paths.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from .models import (
    FileTransDetails,
    ModuleDetails,
    ProcessingDetailsResponse,
    ProcessingStage,
    TrackProcessingDetails,
    TranscriptSnippet,
)

REMOTE_MODULES = ("fun_asr", "emotion", "transcription")

TRANSCRIPT_SEGMENT_LIMIT = 1200
TRANSCRIPT_TEXT_LIMIT = 500


def safe_error_code(exc: BaseException) -> str:
    """Map an exception to a stable, bounded error code without its message."""
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return "upstream_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return "upstream_http_error"
    if isinstance(exc, httpx.HTTPError):
        return "upstream_connection_error"
    if isinstance(exc, ValueError):
        return "invalid_upstream_result"
    if isinstance(exc, RuntimeError):
        return "upstream_task_failed"
    return "unexpected_error"


def ensure_state(session: Any) -> dict[str, Any]:
    state = session.processing
    state.setdefault("tracks", {})
    state.setdefault("transcript", [])
    state.setdefault("aligned_segment_count", 0)
    state.setdefault("submitted_to_qwen", False)
    state.setdefault("qwen_status", ProcessingStage.queued.value)
    state.setdefault("qwen_error_code", None)
    state.setdefault("updated_at", datetime.now(UTC))
    return state


def _touch(state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(UTC)


def _default_modules() -> dict[str, dict[str, Any]]:
    return {
        name: {"status": ProcessingStage.queued.value, "error_code": None, "elapsed_ms": None}
        for name in REMOTE_MODULES
    }


def upsert_track(session: Any, upload: Any) -> dict[str, Any]:
    state = ensure_state(session)
    entry = state["tracks"].setdefault(
        upload.id,
        {
            "file_name": upload.file_name,
            "track": upload.track,
            "mime_type": upload.mime_type,
            "size_bytes": upload.size,
            "upload_status": ProcessingStage.queued.value,
            "received_chunks": len(upload.chunks),
            "expected_chunks": 0,
            "oss_status": ProcessingStage.queued.value,
            "modules": _default_modules(),
            "filetrans": {
                "status": ProcessingStage.queued.value,
                "error_code": None,
                "elapsed_ms": None,
                "sentence_count": None,
                "language": None,
                "audio_duration_ms": None,
            },
        },
    )
    entry["file_name"] = upload.file_name
    entry["track"] = upload.track
    entry["mime_type"] = upload.mime_type
    entry["size_bytes"] = upload.size
    entry["received_chunks"] = len(upload.chunks)
    _touch(state)
    return entry


def set_upload(
    session: Any,
    upload: Any,
    status: ProcessingStage,
    expected_chunks: int,
) -> None:
    entry = upsert_track(session, upload)
    entry["upload_status"] = status.value
    entry["expected_chunks"] = expected_chunks
    entry["received_chunks"] = len(upload.chunks)
    _touch(ensure_state(session))


def mark_upload_completed(session: Any, upload: Any) -> None:
    entry = upsert_track(session, upload)
    entry["upload_status"] = ProcessingStage.succeeded.value
    _touch(ensure_state(session))


def set_oss(session: Any, upload_id: str, status: ProcessingStage) -> None:
    state = ensure_state(session)
    entry = state["tracks"].get(upload_id)
    if entry is None:
        return
    entry["oss_status"] = status.value
    _touch(state)


def set_module(
    session: Any,
    upload_id: str,
    module: str,
    status: ProcessingStage,
    error_code: str | None = None,
    elapsed_ms: int | None = None,
) -> None:
    state = ensure_state(session)
    entry = state["tracks"].get(upload_id)
    if entry is None:
        return
    entry["modules"][module] = {
        "status": status.value,
        "error_code": error_code,
        "elapsed_ms": elapsed_ms,
    }
    if module == "transcription":
        entry["filetrans"]["status"] = status.value
        entry["filetrans"]["error_code"] = error_code
        entry["filetrans"]["elapsed_ms"] = elapsed_ms
    _touch(state)


def set_filetrans_stats(
    session: Any,
    upload_id: str,
    *,
    sentence_count: int,
    language: str | None,
    audio_duration_ms: int | None,
) -> None:
    state = ensure_state(session)
    entry = state["tracks"].get(upload_id)
    if entry is None:
        return
    entry["filetrans"]["sentence_count"] = sentence_count
    entry["filetrans"]["language"] = language
    entry["filetrans"]["audio_duration_ms"] = audio_duration_ms
    _touch(state)


def add_transcript(session: Any, segments: list[dict[str, Any]]) -> None:
    state = ensure_state(session)
    state["transcript"].extend(segments)
    _touch(state)


def set_alignment(session: Any, aligned_segment_count: int) -> None:
    state = ensure_state(session)
    state["aligned_segment_count"] = aligned_segment_count
    _touch(state)


def set_qwen(
    session: Any, status: ProcessingStage, error_code: str | None = None
) -> None:
    state = ensure_state(session)
    state["submitted_to_qwen"] = True
    state["qwen_status"] = status.value
    state["qwen_error_code"] = error_code
    _touch(state)


def mark_skipped_modules(session: Any, upload_id: str, missing: tuple[str, ...]) -> None:
    for module in missing:
        set_module(session, upload_id, module, ProcessingStage.skipped)


def _snippet(segment: dict[str, Any]) -> TranscriptSnippet:
    text = str(segment.get("text", ""))[:TRANSCRIPT_TEXT_LIMIT]
    return TranscriptSnippet(
        speaker_id=str(segment.get("speaker_id", "unknown")),
        start_ms=max(0, int(segment.get("start_ms", 0))),
        end_ms=max(0, int(segment.get("end_ms", 0))),
        text=text,
    )


def build_response(session: Any) -> ProcessingDetailsResponse:
    state = ensure_state(session)
    tracks: list[TrackProcessingDetails] = []
    for upload_id, entry in state["tracks"].items():
        tracks.append(
            TrackProcessingDetails(
                upload_id=upload_id,
                file_name=str(entry["file_name"]),
                track=str(entry["track"]),
                mime_type=str(entry["mime_type"]),
                size_bytes=int(entry["size_bytes"]),
                upload_status=ProcessingStage(entry["upload_status"]),
                received_chunks=int(entry["received_chunks"]),
                expected_chunks=int(entry["expected_chunks"]),
                oss_status=ProcessingStage(entry["oss_status"]),
                modules={
                    name: ModuleDetails(
                        status=ProcessingStage(details["status"]),
                        error_code=details.get("error_code"),
                        elapsed_ms=details.get("elapsed_ms"),
                    )
                    for name, details in entry["modules"].items()
                },
                filetrans=FileTransDetails(
                    status=ProcessingStage(entry["filetrans"]["status"]),
                    error_code=entry["filetrans"].get("error_code"),
                    elapsed_ms=entry["filetrans"].get("elapsed_ms"),
                    sentence_count=entry["filetrans"].get("sentence_count"),
                    language=entry["filetrans"].get("language"),
                    audio_duration_ms=entry["filetrans"].get("audio_duration_ms"),
                ),
            )
        )
    transcript = state["transcript"]
    return ProcessingDetailsResponse(
        session_id=session.id,
        updated_at=state["updated_at"],
        tracks=tracks,
        aligned_segment_count=int(state["aligned_segment_count"]),
        submitted_to_qwen=bool(state["submitted_to_qwen"]),
        qwen_status=ProcessingStage(state["qwen_status"]),
        qwen_error_code=state["qwen_error_code"],
        transcript_segments=[_snippet(segment) for segment in transcript[:TRANSCRIPT_SEGMENT_LIMIT]],
        transcript_truncated=len(transcript) > TRANSCRIPT_SEGMENT_LIMIT,
    )
