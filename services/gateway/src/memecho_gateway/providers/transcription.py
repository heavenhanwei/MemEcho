from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import Settings
from ..media import ALL_MEDIA_INPUTS, MediaInput, PreparedMedia
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
    provider_id = "bailian"
    capability = "file_transcription"

    def __init__(self, settings: Settings, mock: bool = False):
        self.settings = settings
        self.mock = mock
        self.dashscope = DashScopeClient(settings, mock=mock)
        # Real DashScope FileTrans only accepts a public URL; mock/demo mode
        # accepts every transport, preferring public_url so the existing demo
        # pipeline (mock object storage) is unchanged.
        if mock:
            self.media_inputs: tuple[MediaInput, ...] = (
                MediaInput.public_url,
                *(item for item in ALL_MEDIA_INPUTS if item != MediaInput.public_url),
            )
        else:
            self.media_inputs = (MediaInput.public_url,)

    async def download(self, url: str) -> dict[str, Any]:
        if self.mock:
            return self._mock_transcription(url)
        task_result = await self.dashscope.submit_transcription(url)
        transcription_url = self._transcription_url(task_result)
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(transcription_url)
            resp.raise_for_status()
            return self._normalize_result(resp.json())

    async def download_diarization(
        self, task_result: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Resolve a Fun-ASR task result into normalized speaker intervals.

        DashScope's task endpoint returns result-file pointers, not the
        sentence intervals consumed by the aligner.  Legacy mocks may already
        contain normalized intervals, so retain that shape as a compatibility
        path.
        """
        direct = self._normalize_diarization_entries(
            task_result.get("output", {}).get("results", [])
        )
        if direct:
            return direct

        urls = self._transcription_urls(task_result)
        intervals: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=60) as client:
            for result_url in urls:
                response = await client.get(result_url)
                response.raise_for_status()
                intervals.extend(self._normalize_diarization_payload(response.json()))
        if not intervals:
            raise ValueError("Fun-ASR produced no usable speaker intervals")
        return intervals

    async def download_with_phase(
        self,
        url: str,
        *,
        on_phase: PhaseCallback | None = None,
        on_submitted: Any | None = None,
    ) -> dict[str, Any]:
        """Download transcription with phase callbacks for real-time tracking.

        ``on_submitted(task_id)`` fires exactly once with the raw upstream
        task id right after the (billable) submission succeeds, so callers
        can persist the reference before any polling happens.
        """
        if self.mock:
            return self._mock_transcription(url)

        _validate_audio_url(url)
        log.info("FileTrans download_with_phase url=%s", _sanitize_url_for_log(url))

        if on_phase:
            on_phase("submitting")
        t0 = time.monotonic()
        task_id = await self.dashscope.submit_transcription_task(url)
        log.info(
            "FileTrans submitted task_ref=%s url=%s",
            DashScopeClient.sanitize_task_id(task_id),
            _sanitize_url_for_log(url),
        )
        if on_submitted is not None:
            on_submitted(task_id)

        if on_phase:
            on_phase(
                "queued",
                task_reference=DashScopeClient.sanitize_task_id(task_id),
                task_id=task_id,
            )

        return await self._finalize_task(task_id, t0, on_phase)

    async def download_with_media(
        self,
        media: PreparedMedia,
        *,
        on_phase: PhaseCallback | None = None,
        on_submitted: Any | None = None,
    ) -> dict[str, Any]:
        """Transport-aware FileTrans entry point.

        The real DashScope adapter only accepts ``public_url``; other
        transports are accepted here so alternative providers (direct binary
        upload, local models) can plug in without touching the pipeline.
        """
        if self.mock:
            return self._mock_transcription(media.audio_reference() if media.url else media.transport_id)
        return await self.download_with_phase(
            media.audio_reference(), on_phase=on_phase, on_submitted=on_submitted
        )

    async def resume_with_phase(
        self,
        task_id: str,
        *,
        on_phase: PhaseCallback | None = None,
    ) -> dict[str, Any]:
        """Continue polling an already-submitted upstream task.

        Used after a gateway restart: the task reference is recovered from
        persistence and polling resumes. This path never resubmits, so a
        restart cannot double-bill the same transcription.
        """
        if self.mock:
            return self._mock_transcription(f"resume:{task_id}")
        log.info(
            "FileTrans resuming task_ref=%s", DashScopeClient.sanitize_task_id(task_id)
        )
        t0 = time.monotonic()
        return await self._finalize_task(task_id, t0, on_phase)

    async def _finalize_task(
        self,
        task_id: str,
        start_time: float,
        on_phase: PhaseCallback | None,
    ) -> dict[str, Any]:
        """Poll a submitted task, then fetch and normalize its result."""
        task_result = await self.dashscope.poll_task_result(
            task_id, on_phase=on_phase, start_time=start_time,
        )
        if on_phase:
            on_phase("downloading", elapsed_ms=int((time.monotonic() - start_time) * 1000))

        try:
            transcription_url = self._transcription_url(task_result)
        except RuntimeError:
            if on_phase:
                on_phase("failed", error_code="upstream_task_failed")
            raise

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(transcription_url)
            resp.raise_for_status()
            raw = resp.json()

        if on_phase:
            on_phase("normalizing", elapsed_ms=int((time.monotonic() - start_time) * 1000))

        normalized = self._normalize_result(raw)
        sentence_count = len(normalized.get("transcript", []))
        if sentence_count == 0:
            if on_phase:
                on_phase("failed", error_code="invalid_upstream_result")
            raise ValueError("transcription produced no usable sentences")

        if on_phase:
            on_phase(
                "succeeded",
                elapsed_ms=int((time.monotonic() - start_time) * 1000),
                sentence_count=sentence_count,
                language=normalized.get("language"),
                audio_duration_ms=normalized.get("duration_ms"),
            )
        return normalized

    @staticmethod
    def _transcription_url(task_result: dict[str, Any]) -> str:
        """Read Qwen FileTrans result URL, with legacy Fun-ASR fallback."""
        if not isinstance(task_result, dict):
            raise RuntimeError("transcription task result is not a dict")
        output = task_result.get("output")
        if not isinstance(output, dict):
            raise RuntimeError("transcription task output is missing or invalid")
        result = output.get("result")
        if isinstance(result, dict) and result.get("transcription_url"):
            return str(result["transcription_url"])

        results = output.get("results")
        if not isinstance(results, list):
            raise RuntimeError("transcription task returned no result URL")
        for item in results:
            if not isinstance(item, dict):
                continue
            if (item.get("subtask_status") or item.get("status")) == "FAILED":
                raise RuntimeError(
                    str(item.get("code") or "transcription task failed")
                )
            if item.get("transcription_url"):
                return str(item["transcription_url"])
        raise RuntimeError("transcription task returned no result URL")

    @staticmethod
    def _transcription_urls(task_result: dict[str, Any]) -> list[str]:
        """Return every successful result URL without exposing it in errors."""
        if not isinstance(task_result, dict):
            raise RuntimeError("transcription task result is not a dict")
        output = task_result.get("output")
        if not isinstance(output, dict):
            raise RuntimeError("transcription task output is missing or invalid")
        result = output.get("result")
        if isinstance(result, dict) and result.get("transcription_url"):
            return [str(result["transcription_url"])]

        urls: list[str] = []
        for item in output.get("results", []):
            if not isinstance(item, dict):
                continue
            if (item.get("subtask_status") or item.get("status")) == "FAILED":
                raise RuntimeError(str(item.get("code") or "transcription task failed"))
            if item.get("transcription_url"):
                urls.append(str(item["transcription_url"]))
        if not urls:
            raise RuntimeError("transcription task returned no result URL")
        return urls

    @staticmethod
    def _speaker_id(value: Any) -> str:
        speaker = str(value if value is not None else "unknown")
        return speaker if speaker.startswith("speaker_") else f"speaker_{speaker}"

    @classmethod
    def _normalize_diarization_entries(
        cls, entries: Any
    ) -> list[dict[str, Any]]:
        intervals: list[dict[str, Any]] = []
        if not isinstance(entries, list):
            return intervals
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                start_ms = int(entry["start_ms"])
                end_ms = int(entry["end_ms"])
            except (KeyError, TypeError, ValueError):
                continue
            if start_ms < 0 or end_ms <= start_ms:
                continue
            intervals.append(
                {
                    "speaker_id": cls._speaker_id(entry.get("speaker_id")),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                }
            )
        return intervals

    @classmethod
    def _normalize_diarization_payload(
        cls, data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        intervals: list[dict[str, Any]] = []
        for transcript in data.get("transcripts", []):
            if not isinstance(transcript, dict):
                continue
            for sentence in transcript.get("sentences", []):
                if not isinstance(sentence, dict):
                    continue
                try:
                    start_ms = int(sentence["begin_time"])
                    end_ms = int(sentence["end_time"])
                except (KeyError, TypeError, ValueError):
                    continue
                if start_ms < 0 or end_ms <= start_ms:
                    continue
                intervals.append(
                    {
                        "speaker_id": cls._speaker_id(
                            sentence.get(
                                "speaker_id", transcript.get("channel_id", "unknown")
                            )
                        ),
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                    }
                )
        return intervals

    @staticmethod
    def _to_ms(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_text(value: Any) -> str:
        """Normalize mixed CRLF/CR newlines so downstream matching is stable."""
        return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    @classmethod
    def _normalize_result(cls, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {"transcript": [], "language": "zh", "duration_ms": None}
        segments: list[dict[str, Any]] = []
        transcripts = data.get("transcripts")
        if not isinstance(transcripts, list):
            transcripts = []
        for transcript in transcripts:
            if not isinstance(transcript, dict):
                continue
            sentences = transcript.get("sentences")
            if not isinstance(sentences, list):
                continue
            for sentence in sentences:
                if not isinstance(sentence, dict):
                    continue
                start_ms = cls._to_ms(sentence.get("begin_time"))
                end_ms = cls._to_ms(sentence.get("end_time"))
                if start_ms is None or end_ms is None:
                    continue
                text = cls._normalize_text(sentence.get("text"))
                if not text:
                    continue
                try:
                    emotion_confidence = float(
                        sentence.get("emotion_confidence", 0.0) or 0.0
                    )
                except (TypeError, ValueError):
                    emotion_confidence = 0.0
                speaker = sentence.get("speaker_id", transcript.get("channel_id", 0))
                segments.append(
                    {
                        "speaker_id": f"speaker_{speaker}",
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "text": text,
                        "confidence": 0.9,
                        "emotion": str(sentence.get("emotion", "unknown")),
                        "emotion_confidence": emotion_confidence,
                    }
                )
        properties = data.get("properties")
        duration_ms = (
            properties.get("original_duration_in_milliseconds")
            if isinstance(properties, dict)
            else None
        )
        return {
            "transcript": segments,
            "language": "zh",
            "duration_ms": duration_ms,
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
