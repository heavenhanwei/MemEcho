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


@pytest.fixture
def settings_full_endpoint():
    s = get_settings()
    s.bailian_audio_base_url = "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
    s.bailian_audio_api_key = "test-key"
    s.bailian_diarization_model = "fun-asr"
    s.bailian_emotion_model = "qwen3-asr-flash-filetrans"
    return s


# ---------------------------------------------------------------------------
# Mock tests
# ---------------------------------------------------------------------------

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


async def test_mock_fun_asr_deterministic_across_calls(settings):
    client = DashScopeClient(settings, mock=True)
    r1 = await client.submit_fun_asr("https://example.com/a.wav")
    r2 = await client.submit_fun_asr("https://example.com/b.wav")
    assert r1 == r2


# ---------------------------------------------------------------------------
# URL construction tests
# ---------------------------------------------------------------------------

def test_build_transcription_url_from_base(settings):
    client = DashScopeClient(settings)
    assert client._build_transcription_url() == (
        "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
    )


def test_build_transcription_url_from_full_endpoint(settings_full_endpoint):
    client = DashScopeClient(settings_full_endpoint)
    assert client._build_transcription_url() == (
        "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
    )


def test_build_tasks_url_from_base(settings):
    client = DashScopeClient(settings)
    assert client._build_tasks_url("task_001") == (
        "https://dashscope-mock.example.com/api/v1/tasks/task_001"
    )


def test_build_tasks_url_from_full_endpoint(settings_full_endpoint):
    client = DashScopeClient(settings_full_endpoint)
    assert client._build_tasks_url("task_001") == (
        "https://dashscope-mock.example.com/api/v1/tasks/task_001"
    )


# ---------------------------------------------------------------------------
# Live (mocked HTTP) tests — submit & poll with correct API contract
# ---------------------------------------------------------------------------

@respx.mock
async def test_submit_sends_correct_headers_and_body(settings):
    route = respx.post(
        "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
    ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "task_001"}}))

    respx.get(
        "https://dashscope-mock.example.com/api/v1/tasks/task_001"
    ).mock(
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
    await client.submit_fun_asr("https://example.com/audio.wav")

    request = route.calls.last.request
    assert request.headers["X-DashScope-Async"] == "enable"
    assert request.headers["Authorization"] == "Bearer test-key"
    body = route.calls.last.request.content
    import json

    payload = json.loads(body)
    assert payload["input"]["file_urls"] == ["https://example.com/audio.wav"]
    assert payload["model"] == "fun-asr"


@respx.mock
async def test_submit_with_full_endpoint_url(settings_full_endpoint):
    route = respx.post(
        "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
    ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "task_ep"}}))

    respx.get(
        "https://dashscope-mock.example.com/api/v1/tasks/task_ep"
    ).mock(
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

    client = DashScopeClient(settings_full_endpoint, mock=False)
    result = await client.submit_emotion("https://example.com/audio.wav")
    assert result["output"]["task_status"] == "SUCCEEDED"
    assert route.called


@respx.mock
async def test_polling_uses_get_method(settings):
    respx.post(
        "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
    ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "task_post"}}))

    poll_route = respx.get(
        "https://dashscope-mock.example.com/api/v1/tasks/task_post"
    ).mock(
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
    await client.submit_fun_asr("https://example.com/audio.wav")
    assert poll_route.called
    poll_request = poll_route.calls.last.request
    assert poll_request.method == "GET"


@respx.mock
async def test_polling_completes_on_succeeded(settings):
    respx.post(
        "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
    ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "task_001"}}))

    respx.get(
        "https://dashscope-mock.example.com/api/v1/tasks/task_001"
    ).mock(
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
    respx.post(
        "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
    ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "task_002"}}))

    respx.get(
        "https://dashscope-mock.example.com/api/v1/tasks/task_002"
    ).mock(
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
    respx.post(
        "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
    ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "task_003"}}))

    respx.get(
        "https://dashscope-mock.example.com/api/v1/tasks/task_003"
    ).mock(
        return_value=respx.MockResponse(
            200, json={"output": {"task_status": "FAILED", "message": "bad audio"}}
        )
    )

    client = DashScopeClient(settings, mock=False)
    with pytest.raises(RuntimeError, match="failed"):
        await client.submit_fun_asr("https://example.com/audio.wav")


@respx.mock
async def test_no_task_id_raises(settings):
    respx.post(
        "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
    ).mock(return_value=respx.MockResponse(200, json={"output": {}}))

    client = DashScopeClient(settings, mock=False)
    with pytest.raises(RuntimeError, match="No task_id"):
        await client.submit_fun_asr("https://example.com/audio.wav")


async def test_missing_config_raises():
    s = get_settings()
    s.bailian_audio_base_url = ""
    s.bailian_audio_api_key = "test-key"
    client = DashScopeClient(s, mock=False)
    with pytest.raises(RuntimeError, match="not configured"):
        await client.submit_fun_asr("https://example.com/audio.wav")
