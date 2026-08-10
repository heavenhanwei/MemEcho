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

    async def submit_fun_asr(self, audio_url: str) -> dict[str, Any]:
        if self.mock:
            return self._mock_fun_asr_result(audio_url)
        return await self._submit_and_poll(
            model=self.settings.bailian_diarization_model,
            audio_url=audio_url,
            task="speaker_diarization",
        )

    async def submit_emotion(self, audio_url: str) -> dict[str, Any]:
        if self.mock:
            return self._mock_emotion_result(audio_url)
        return await self._submit_and_poll(
            model=self.settings.bailian_emotion_model,
            audio_url=audio_url,
            task="emotion_labels",
        )

    async def submit_transcription(self, audio_url: str) -> dict[str, Any]:
        if self.mock:
            return {"output": {"task_status": "SUCCEEDED", "results": []}}
        return await self._submit_and_poll(
            model=self.settings.bailian_transcription_model,
            audio_url=audio_url,
            task="transcription",
        )

    async def submit_transcription_task(self, audio_url: str) -> str:
        """Submit a transcription task and return the raw task_id."""
        if self.mock:
            return "mock_task_id"
        if not self.settings.bailian_audio_base_url or not self.settings.bailian_audio_api_key:
            raise RuntimeError("DashScope audio endpoint is not configured")
        headers = self._build_headers()
        submit_url = self._build_transcription_url()
        payload: dict[str, Any] = {
            "model": self.settings.bailian_transcription_model,
            "input": {"file_urls": [audio_url]},
            "parameters": {"language_hints": ["zh", "en"]},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(submit_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            task_id = data.get("output", {}).get("task_id")
            if not task_id:
                raise RuntimeError(f"No task_id in DashScope response: {data}")
        return task_id

    async def poll_task_result(
        self,
        task_id: str,
        *,
        on_phase: PhaseCallback | None = None,
        start_time: float | None = None,
    ) -> dict[str, Any]:
        """Poll a DashScope task until terminal state with phase callbacks."""
        if self.mock:
            return {"output": {"task_status": "SUCCEEDED", "results": []}}
        headers = {"Authorization": f"Bearer {self.settings.bailian_audio_api_key}"}
        url = self._build_tasks_url(task_id)
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

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.bailian_audio_api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }

    def _build_transcription_url(self) -> str:
        base = self.settings.bailian_audio_base_url.rstrip("/")
        if base.endswith("/transcription"):
            return base
        return f"{base}{_TRANSCRIPTION_SUFFIX}"

    def _build_tasks_url(self, task_id: str) -> str:
        base = self.settings.bailian_audio_base_url.rstrip("/")
        if base.endswith("/transcription"):
            parsed = urlparse(base)
            return f"{parsed.scheme}://{parsed.netloc}/api/v1/tasks/{task_id}"
        return f"{base}/api/v1/tasks/{task_id}"

    async def _submit_and_poll(
        self, model: str, audio_url: str, task: str
    ) -> dict[str, Any]:
        if not self.settings.bailian_audio_base_url or not self.settings.bailian_audio_api_key:
            raise RuntimeError("DashScope audio endpoint is not configured")

        headers = self._build_headers()

        submit_url = self._build_transcription_url()
        submit_payload: dict[str, Any] = {
            "model": model,
            "input": {"file_urls": [audio_url]},
            "parameters": {
                "language_hints": ["zh", "en"],
                **(
                    {"diarization_enabled": True}
                    if task == "speaker_diarization"
                    else {}
                ),
            },
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(submit_url, json=submit_payload, headers=headers)
            resp.raise_for_status()
            task_data = resp.json()
            task_id = task_data.get("output", {}).get("task_id")
            if not task_id:
                raise RuntimeError(f"No task_id in DashScope response: {task_data}")

        return await self._poll_result(task_id, headers)

    async def _poll_result(
        self, task_id: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        url = self._build_tasks_url(task_id)
        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in range(_MAX_POLL_ATTEMPTS):
                await asyncio.sleep(_POLL_INTERVAL_S)
                resp = await client.get(url, headers={"Authorization": headers["Authorization"]})
                resp.raise_for_status()
                data = resp.json()
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
