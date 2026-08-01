from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import respx

from memecho_gateway.config import get_settings
from memecho_gateway.providers.dashscope import DashScopeClient


@pytest.fixture
def settings():
    s = get_settings()
    s.bailian_audio_base_url = "https://dashscope-mock.example.com"
    s.bailian_audio_api_key = "test-key"
    s.bailian_diarization_model = "fun-asr"
    s.bailian_emotion_model = "qwen3-asr-flash-filetrans"
    return s


async def test_mock_diarization_returns_deterministic_result(settings):
    client = DashScopeClient(settings, mock=True)
    result = await client.submit_fun_asr("https://example.com/audio.wav")
    assert result["output"]["task_status"] == "SUCCEEDED"
    segments = result["output"]["results"]
    assert len(segments) == 3
    assert segments[0]["speaker_id"] == "speaker_self"
    assert segments[1]["speaker_id"] == "speaker_2"


async def test_mock_emotion_returns_deterministic_result(settings):
    client = DashScopeClient(settings, mock=True)
    result = await client.submit_emotion("https://example.com/audio.wav")
    assert result["output"]["task_status"] == "SUCCEEDED"
    emotions = result["output"]["results"]
    assert len(emotions) == 3
    assert emotions[0]["emotion"] == "neutral"
    assert emotions[1]["emotion"] == "frustration"


@respx.mock
async def test_polling_completes_on_succeeded(settings):
    respx.post("https://dashscope-mock.example.com/v1/services/fun-asr/tasks").mock(
        return_value=respx.MockResponse(200, json={"output": {"task_id": "task_001"}})
    )
    respx.get("https://dashscope-mock.example.com/v1/services/fun-asr/tasks/task_001").mock(
        return_value=respx.MockResponse(
            200,
            json={
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [{"speaker_id": "s1", "start_ms": 0, "end_ms": 1000}],
                }
            },
        )
    )
    client = DashScopeClient(settings, mock=False)
    result = await client.submit_fun_asr("https://example.com/audio.wav")
    assert result["output"]["task_status"] == "SUCCEEDED"


@respx.mock
async def test_polling_retries_then_succeeds(settings):
    respx.post("https://dashscope-mock.example.com/v1/services/fun-asr/tasks").mock(
        return_value=respx.MockResponse(200, json={"output": {"task_id": "task_002"}})
    )
    poll_url = "https://dashscope-mock.example.com/v1/services/fun-asr/tasks/task_002"
    respx.get(poll_url).mock(
        side_effect=[
            respx.MockResponse(200, json={"output": {"task_status": "RUNNING"}}),
            respx.MockResponse(200, json={"output": {"task_status": "RUNNING"}}),
            respx.MockResponse(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [{"speaker_id": "s1", "start_ms": 0, "end_ms": 500}],
                    }
                },
            ),
        ]
    )
    client = DashScopeClient(settings, mock=False)
    result = await client.submit_fun_asr("https://example.com/audio.wav")
    assert result["output"]["task_status"] == "SUCCEEDED"


@respx.mock
async def test_polling_raises_on_failed_task(settings):
    respx.post("https://dashscope-mock.example.com/v1/services/fun-asr/tasks").mock(
        return_value=respx.MockResponse(200, json={"output": {"task_id": "task_003"}})
    )
    respx.get("https://dashscope-mock.example.com/v1/services/fun-asr/tasks/task_003").mock(
        return_value=respx.MockResponse(
            200, json={"output": {"task_status": "FAILED", "message": "bad audio"}}
        )
    )
    client = DashScopeClient(settings, mock=False)
    with pytest.raises(RuntimeError, match="failed"):
        await client.submit_fun_asr("https://example.com/audio.wav")


async def test_mock_fun_asr_deterministic_across_calls(settings):
    client = DashScopeClient(settings, mock=True)
    r1 = await client.submit_fun_asr("https://example.com/a.wav")
    r2 = await client.submit_fun_asr("https://example.com/b.wav")
    assert r1 == r2
