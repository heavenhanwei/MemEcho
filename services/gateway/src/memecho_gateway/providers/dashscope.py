from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 2.0
_MAX_POLL_ATTEMPTS = 150


class DashScopeClient:
    def __init__(self, settings: Settings, mock: bool = False):
        self.settings = settings
        self.mock = mock

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

    async def _submit_and_poll(
        self, model: str, audio_url: str, task: str
    ) -> dict[str, Any]:
        if not self.settings.bailian_audio_base_url or not self.settings.bailian_audio_api_key:
            raise RuntimeError("DashScope audio endpoint is not configured")

        headers = {
            "Authorization": f"Bearer {self.settings.bailian_audio_api_key}",
            "Content-Type": "application/json",
        }
        base = self.settings.bailian_audio_base_url.rstrip("/")

        submit_payload: dict[str, Any] = {
            "model": model,
            "input": {"audio_url": audio_url},
            "parameters": {"task": task},
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base}/v1/services/{model}/tasks",
                json=submit_payload,
                headers=headers,
            )
            resp.raise_for_status()
            task_data = resp.json()
            task_id = task_data.get("output", {}).get("task_id")
            if not task_id:
                raise RuntimeError(f"No task_id in DashScope response: {task_data}")

        return await self._poll_result(base, model, task_id, headers)

    async def _poll_result(
        self, base: str, model: str, task_id: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        url = f"{base}/v1/services/{model}/tasks/{task_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in range(_MAX_POLL_ATTEMPTS):
                await asyncio.sleep(_POLL_INTERVAL_S)
                resp = await client.get(url, headers=headers)
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
