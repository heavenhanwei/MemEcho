from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import Settings
from .dashscope import DashScopeClient, PhaseCallback

log = logging.getLogger(__name__)


def _sanitize_url_for_log(url: str) -> str:
    """Return URL with query-string (signatures/tokens) stripped for safe logging."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _validate_audio_url(url: str) -> None:
    """Pre-flight validation before submitting to DashScope.

    Raises ``ValueError`` (mapped to ``invalid_upstream_result`` error code)
    when the URL is obviously malformed.  Does NOT fetch the file.
    """
    if not url or not url.strip():
        raise ValueError("audio_url is empty")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"audio_url scheme must be http/https, got {parsed.scheme!r}")
    if not parsed.netloc:
        raise ValueError("audio_url has no host")


class TranscriptionDownloader:
    def __init__(self, settings: Settings, mock: bool = False):
        self.settings = settings
        self.mock = mock
        self.dashscope = DashScopeClient(settings, mock=mock)

    async def download(self, url: str) -> dict[str, Any]:
        if self.mock:
            return self._mock_transcription(url)
        task_result = await self.dashscope.submit_transcription(url)
        results = task_result.get("output", {}).get("results", [])
        for item in results:
            if item.get("subtask_status") == "FAILED":
                raise RuntimeError(str(item.get("code") or "transcription task failed"))
            transcription_url = item.get("transcription_url")
            if not transcription_url:
                continue
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(transcription_url)
                resp.raise_for_status()
                return self._normalize_result(resp.json())
        raise RuntimeError("transcription task returned no result URL")

    async def download_with_phase(
        self,
        url: str,
        *,
        on_phase: PhaseCallback | None = None,
    ) -> dict[str, Any]:
        """Download transcription with phase callbacks for real-time tracking."""
        if self.mock:
            return self._mock_transcription(url)

        _validate_audio_url(url)
        log.info("FileTrans download_with_phase url=%s", _sanitize_url_for_log(url))

        if on_phase:
            on_phase("submitting")
        t0 = time.monotonic()
        task_id = await self.dashscope.submit_transcription_task(url)
        log.info("FileTrans submitted task_id=%s url=%s", task_id, _sanitize_url_for_log(url))

        if on_phase:
            on_phase("queued", task_reference=DashScopeClient.sanitize_task_id(task_id))

        task_result = await self.dashscope.poll_task_result(
            task_id, on_phase=on_phase, start_time=t0,
        )
        results = task_result.get("output", {}).get("results", [])

        if on_phase:
            on_phase("downloading", elapsed_ms=int((time.monotonic() - t0) * 1000))

        raw: dict[str, Any] | None = None
        for item in results:
            if item.get("subtask_status") == "FAILED":
                if on_phase:
                    on_phase("failed", error_code="upstream_task_failed")
                raise RuntimeError(str(item.get("code") or "transcription task failed"))
            transcription_url = item.get("transcription_url")
            if not transcription_url:
                continue
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(transcription_url)
                resp.raise_for_status()
                raw = resp.json()

        if raw is None:
            if on_phase:
                on_phase("failed", error_code="upstream_task_failed")
            raise RuntimeError("transcription task returned no result URL")

        if on_phase:
            on_phase("normalizing", elapsed_ms=int((time.monotonic() - t0) * 1000))

        normalized = self._normalize_result(raw)
        sentence_count = len(normalized.get("transcript", []))
        if sentence_count == 0:
            if on_phase:
                on_phase("failed", error_code="invalid_upstream_result")
            raise ValueError("transcription produced no usable sentences")

        if on_phase:
            on_phase(
                "succeeded",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                sentence_count=sentence_count,
                language=normalized.get("language"),
                audio_duration_ms=normalized.get("duration_ms"),
            )
        return normalized

    @staticmethod
    def _normalize_result(data: dict[str, Any]) -> dict[str, Any]:
        segments: list[dict[str, Any]] = []
        for transcript in data.get("transcripts", []):
            for sentence in transcript.get("sentences", []):
                speaker = sentence.get("speaker_id", transcript.get("channel_id", 0))
                segments.append(
                    {
                        "speaker_id": f"speaker_{speaker}",
                        "start_ms": int(sentence.get("begin_time", 0)),
                        "end_ms": int(sentence.get("end_time", 0)),
                        "text": str(sentence.get("text", "")),
                        "confidence": 0.9,
                    }
                )
        return {
            "transcript": [segment for segment in segments if segment["text"].strip()],
            "language": "zh",
            "duration_ms": data.get("properties", {}).get("original_duration_in_milliseconds"),
        }

    def _mock_transcription(self, url: str) -> dict[str, Any]:
        return {
            "transcript": [
                {
                    "speaker_id": "speaker_self",
                    "start_ms": 0,
                    "end_ms": 8000,
                    "text": "placeholder",
                    "confidence": 0.92,
                },
                {
                    "speaker_id": "speaker_2",
                    "start_ms": 8000,
                    "end_ms": 17000,
                    "text": "placeholder",
                    "confidence": 0.89,
                },
                {
                    "speaker_id": "speaker_self",
                    "start_ms": 17000,
                    "end_ms": 26000,
                    "text": "placeholder",
                    "confidence": 0.94,
                },
            ],
            "language": "zh",
            "duration_ms": 26000,
        }
