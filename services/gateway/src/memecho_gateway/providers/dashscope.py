from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 2.0
_MAX_POLL_ATTEMPTS = 150

# Backoff schedule for poll intervals (seconds), cycled after exhaustion.
_POLL_BACKOFF = (2.0, 2.0, 3.0, 3.0, 5.0)
_MAX_POLL_DURATION_S = 300.0

# Type alias for the phase callback: ``on_phase(phase, **kwargs)``
PhaseCallback = Callable[..., None]

_TRANSCRIPTION_SUFFIX = "/api/v1/services/audio/asr/transcription"

# Audio MIME types DashScope FileTrans is known to accept.
_SUPPORTED_AUDIO_MIMES = frozenset({
    "audio/wav",
    "audio/x-wav",
    "audio/mp3",
    "audio/mpeg",
    "audio/ogg",
    "audio/flac",
    "audio/aac",
    "audio/mp4",
    "audio/m4a",
    "audio/webm",
    "audio/x-m4a",
})


def _sanitize_url_for_log(url: str) -> str:
    """Return a URL safe for logging — strips query params (signatures)."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def validate_audio_url(url: str, *, content_type: str | None = None) -> None:
    """Pre-flight check before submitting to DashScope.

    Raises ``ValueError`` with a stable error-code-friendly message when the
    URL or content type is obviously invalid.  Does NOT fetch the file.
    """
    if not url or not url.strip():
        raise ValueError("audio_url is empty")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"audio_url scheme must be http/https, got {parsed.scheme!r}")
    if not parsed.netloc:
        raise ValueError("audio_url has no host")
    if content_type is not None:
        ct = content_type.split(";")[0].strip().lower()
        if ct not in _SUPPORTED_AUDIO_MIMES:
            log.warning(
                "audio_url content_type may be unsupported by DashScope: %s (url=%s)",
                ct,
                _sanitize_url_for_log(url),
            )


class DashScopeClient:
    def __init__(self, settings: Settings, mock: bool = False):
        self.settings = settings
        self.mock = mock

    @staticmethod
    def sanitize_task_id(task_id: str) -> str:
        """Return a desensitized task reference safe for client display."""
        if len(task_id) <= 8:
            return f"ft_***{task_id}"
        return f"ft_***{task_id[-6:]}"

    async def submit_fun_asr(self, audio_url: str, **kwargs: Any) -> dict[str, Any]:
        if self.mock:
            return self._mock_fun_asr_result(audio_url)
        return await self._submit_and_poll(
            model=self.settings.bailian_diarization_model,
            audio_url=audio_url,
            task="speaker_diarization",
            **kwargs,
        )

    async def submit_emotion(self, audio_url: str, **kwargs: Any) -> dict[str, Any]:
        if self.mock:
            return self._mock_emotion_result(audio_url)
        return await self._submit_and_poll(
            model=self.settings.bailian_emotion_model,
            audio_url=audio_url,
            task="emotion_labels",
            **kwargs,
        )

    async def submit_transcription(self, audio_url: str, **kwargs: Any) -> dict[str, Any]:
        if self.mock:
            return {"output": {"task_status": "SUCCEEDED", "result": {}}}
        task_id = await self.submit_transcription_task(audio_url, **kwargs)
        return await self.poll_task_result(task_id, **kwargs)

    async def submit_transcription_task(self, audio_url: str, **kwargs: Any) -> str:
        """Submit a transcription task and return the raw task_id."""
        if self.mock:
            return "mock_task_id"
        api_key: str = kwargs.get("api_key") or self.settings.bailian_audio_api_key
        base_url: str = kwargs.get("base_url") or self.settings.bailian_audio_base_url
        if not base_url or not api_key:
            raise RuntimeError("DashScope audio endpoint is not configured")
        validate_audio_url(audio_url)
        log.info(
            "DashScope FileTrans submit model=%s url=%s",
            self.settings.bailian_transcription_model,
            _sanitize_url_for_log(audio_url),
        )
        headers = self._build_headers(api_key=api_key)
        submit_url = self._build_transcription_url(base_url=base_url)
        payload: dict[str, Any] = {
            "model": self.settings.bailian_transcription_model,
            # Qwen3-ASR-Flash-Filetrans uses a singular file_url.  The
            # file_urls array belongs to Fun-ASR/Paraformer and is rejected by
            # Qwen FileTrans after task creation with MalformedURL.
            "input": {"file_url": audio_url},
            "parameters": {
                "channel_id": [0],
                "enable_itn": False,
                "enable_words": True,
            },
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(submit_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("DashScope submit returned non-dict response")
            task_id = data.get("output", {}).get("task_id")
            if not task_id:
                raise RuntimeError("No task_id in DashScope response")
        return task_id

    async def poll_task_result(
        self,
        task_id: str,
        *,
        on_phase: PhaseCallback | None = None,
        start_time: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Poll a DashScope task until terminal state with phase callbacks."""
        if self.mock:
            return {"output": {"task_status": "SUCCEEDED", "results": []}}
        api_key: str = kwargs.get("api_key") or self.settings.bailian_audio_api_key
        base_url: str = kwargs.get("base_url") or self.settings.bailian_audio_base_url
        headers = {"Authorization": f"Bearer {api_key}"}
        url = self._build_tasks_url(task_id, base_url=base_url)
        t0 = start_time or time.monotonic()
        task_ref = self.sanitize_task_id(task_id)
        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in range(_MAX_POLL_ATTEMPTS):
                interval = _POLL_BACKOFF[attempt % len(_POLL_BACKOFF)]
                await asyncio.sleep(interval)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                if elapsed_ms / 1000.0 > _MAX_POLL_DURATION_S:
                    if on_phase:
                        on_phase("timed_out", elapsed_ms=elapsed_ms, task_reference=task_ref)
                    raise TimeoutError(f"DashScope task timed out after {elapsed_ms}ms")
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise ValueError("DashScope poll returned non-dict response")
                status = data.get("output", {}).get("task_status", "")
                next_ms = int(_POLL_BACKOFF[(attempt + 1) % len(_POLL_BACKOFF)] * 1000)
                if on_phase:
                    on_phase(
                        "polling",
                        poll_attempts=attempt + 1,
                        next_poll_after_ms=next_ms,
                        elapsed_ms=elapsed_ms,
                        task_reference=task_ref,
                    )
                if status == "SUCCEEDED":
                    return data
                if status == "FAILED":
                    if on_phase:
                        on_phase("failed", elapsed_ms=elapsed_ms, task_reference=task_ref)
                    raise RuntimeError(f"DashScope task {task_id} failed: {data}")
                log.debug("DashScope poll attempt=%d status=%s", attempt + 1, status)
        raise TimeoutError(f"DashScope task timed out after {_MAX_POLL_ATTEMPTS} polls")

    def _build_headers(self, *, api_key: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key or self.settings.bailian_audio_api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }

    def _build_transcription_url(self, *, base_url: str | None = None) -> str:
        base = (base_url or self.settings.bailian_audio_base_url).rstrip("/")
        if base.endswith("/transcription"):
            return base
        return f"{base}{_TRANSCRIPTION_SUFFIX}"

    def _build_tasks_url(self, task_id: str, *, base_url: str | None = None) -> str:
        base = (base_url or self.settings.bailian_audio_base_url).rstrip("/")
        if base.endswith("/transcription"):
            parsed = urlparse(base)
            return f"{parsed.scheme}://{parsed.netloc}/api/v1/tasks/{task_id}"
        return f"{base}/api/v1/tasks/{task_id}"

    async def _submit_and_poll(
        self, model: str, audio_url: str, task: str, **kwargs: Any
    ) -> dict[str, Any]:
        api_key: str = kwargs.get("api_key") or self.settings.bailian_audio_api_key
        base_url: str = kwargs.get("base_url") or self.settings.bailian_audio_base_url
        if not base_url or not api_key:
            raise RuntimeError("DashScope audio endpoint is not configured")

        validate_audio_url(audio_url)
        log.info(
            "DashScope submit task=%s model=%s url=%s",
            task,
            model,
            _sanitize_url_for_log(audio_url),
        )

        headers = self._build_headers(api_key=api_key)

        submit_url = self._build_transcription_url(base_url=base_url)
        qwen_filetrans = model.casefold().startswith("qwen3-asr-flash-filetrans")
        submit_payload: dict[str, Any] = {
            "model": model,
            "input": (
                {"file_url": audio_url}
                if qwen_filetrans
                else {"file_urls": [audio_url]}
            ),
            "parameters": {
                **(
                    {
                        "channel_id": [0],
                        "enable_itn": False,
                        "enable_words": True,
                    }
                    if qwen_filetrans
                    else {"language_hints": ["zh", "en"]}
                ),
                **(
                    {"diarization_enabled": True}
                    if task == "speaker_diarization" and not qwen_filetrans
                    else {}
                ),
            },
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(submit_url, json=submit_payload, headers=headers)
            resp.raise_for_status()
            task_data = resp.json()
            if not isinstance(task_data, dict):
                raise ValueError("DashScope submit returned non-dict response")
            task_id = task_data.get("output", {}).get("task_id")
            if not task_id:
                raise RuntimeError(f"No task_id in DashScope response: {task_data}")

        return await self._poll_result(task_id, headers, base_url=base_url)

    async def _poll_result(
        self, task_id: str, headers: dict[str, str], *, base_url: str | None = None
    ) -> dict[str, Any]:
        url = self._build_tasks_url(task_id, base_url=base_url)
        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in range(_MAX_POLL_ATTEMPTS):
                await asyncio.sleep(_POLL_INTERVAL_S)
                resp = await client.get(url, headers={"Authorization": headers["Authorization"]})
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise ValueError("DashScope poll returned non-dict response")
                status = data.get("output", {}).get("task_status", "")
                if status == "SUCCEEDED":
                    return data
                if status == "FAILED":
                    raise RuntimeError(f"DashScope task {task_id} failed: {data}")
                log.debug("DashScope poll attempt=%d status=%s", attempt + 1, status)
        raise TimeoutError(f"DashScope task {task_id} timed out after {_MAX_POLL_ATTEMPTS} polls")

    def _mock_fun_asr_result(self, audio_url: str) -> dict[str, Any]:
        return {
            "output": {
                "task_status": "SUCCEEDED",
                "results": [
                    {"speaker_id": "speaker_self", "start_ms": 0, "end_ms": 8000},
                    {"speaker_id": "speaker_2", "start_ms": 8000, "end_ms": 17000},
                    {"speaker_id": "speaker_self", "start_ms": 17000, "end_ms": 26000},
                ],
            },
            "usage": {"duration_ms": 26000},
        }

    def _mock_emotion_result(self, audio_url: str) -> dict[str, Any]:
        return {
            "output": {
                "task_status": "SUCCEEDED",
                "results": [
                    {"start_ms": 0, "end_ms": 8000, "emotion": "neutral", "confidence": 0.85},
                    {"start_ms": 8000, "end_ms": 17000, "emotion": "frustration", "confidence": 0.72},
                    {"start_ms": 17000, "end_ms": 26000, "emotion": "determination", "confidence": 0.78},
                ],
            },
            "usage": {"duration_ms": 26000},
        }
