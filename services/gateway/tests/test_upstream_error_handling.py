"""Tests for upstream error handling in voice processing pipeline.

Covers: HTTP errors, task polling, transcription_url download,
and no-evidence degradation scenarios.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from memecho_gateway.config import get_settings
from memecho_gateway.providers.dashscope import DashScopeClient
from memecho_gateway.providers.transcription import TranscriptionDownloader
from memecho_gateway.processing_details import safe_error_code


# ---------------------------------------------------------------------------
# Error code mapping tests
# ---------------------------------------------------------------------------


class TestSafeErrorCode:
    """Test the error code mapping function."""

    def test_timeout_error(self):
        exc = asyncio.TimeoutError("connection timed out")
        assert safe_error_code(exc) == "upstream_timeout"

    def test_timeout_error_builtin(self):
        exc = TimeoutError("request timed out")
        assert safe_error_code(exc) == "upstream_timeout"

    def test_http_status_error(self):
        request = httpx.Request("GET", "https://example.com")
        response = httpx.Response(500, request=request)
        exc = httpx.HTTPStatusError("server error", request=request, response=response)
        assert safe_error_code(exc) == "upstream_http_error"

    def test_http_connection_error(self):
        exc = httpx.ConnectError("connection refused")
        assert safe_error_code(exc) == "upstream_connection_error"

    def test_value_error(self):
        exc = ValueError("invalid response format")
        assert safe_error_code(exc) == "invalid_upstream_result"

    def test_runtime_error(self):
        exc = RuntimeError("task failed")
        assert safe_error_code(exc) == "upstream_task_failed"

    def test_unexpected_error(self):
        exc = Exception("something unexpected")
        assert safe_error_code(exc) == "unexpected_error"


# ---------------------------------------------------------------------------
# HTTP error handling tests for DashScope client
# ---------------------------------------------------------------------------


@pytest.fixture
def settings():
    s = get_settings()
    s.bailian_audio_base_url = "https://dashscope-mock.example.com"
    s.bailian_audio_api_key = "test-key"
    s.bailian_diarization_model = "fun-asr"
    s.bailian_emotion_model = "qwen3-asr-flash-filetrans"
    s.bailian_transcription_model = "qwen3-asr-flash-filetrans"
    return s


class TestDashScopeHTTPErrors:
    """Test HTTP error handling in DashScope client."""

    @respx.mock
    async def test_submit_handles_4xx_error(self, settings):
        """Test that 4xx HTTP errors are properly raised."""
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(return_value=respx.MockResponse(400, json={"error": "bad request"}))

        client = DashScopeClient(settings, mock=False)
        with pytest.raises(httpx.HTTPStatusError):
            await client.submit_fun_asr("https://example.com/audio.wav")

    @respx.mock
    async def test_submit_handles_5xx_error(self, settings):
        """Test that 5xx HTTP errors are properly raised."""
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(return_value=respx.MockResponse(500, json={"error": "internal server error"}))

        client = DashScopeClient(settings, mock=False)
        with pytest.raises(httpx.HTTPStatusError):
            await client.submit_fun_asr("https://example.com/audio.wav")

    @respx.mock
    async def test_poll_handles_4xx_error(self, settings):
        """Test that 4xx HTTP errors during polling are properly raised."""
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "task_001"}}))

        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_001"
        ).mock(return_value=respx.MockResponse(404, json={"error": "task not found"}))

        client = DashScopeClient(settings, mock=False)
        with pytest.raises(httpx.HTTPStatusError):
            await client.submit_fun_asr("https://example.com/audio.wav")

    @respx.mock
    async def test_poll_handles_5xx_error(self, settings):
        """Test that 5xx HTTP errors during polling are properly raised."""
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "task_001"}}))

        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_001"
        ).mock(return_value=respx.MockResponse(502, json={"error": "bad gateway"}))

        client = DashScopeClient(settings, mock=False)
        with pytest.raises(httpx.HTTPStatusError):
            await client.submit_fun_asr("https://example.com/audio.wav")

    @respx.mock
    async def test_poll_handles_connection_error(self, settings):
        """Test that connection errors during polling are properly raised."""
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "task_001"}}))

        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_001"
        ).mock(side_effect=httpx.ConnectError("connection refused"))

        client = DashScopeClient(settings, mock=False)
        with pytest.raises(httpx.ConnectError):
            await client.submit_fun_asr("https://example.com/audio.wav")


# ---------------------------------------------------------------------------
# Task polling edge cases
# ---------------------------------------------------------------------------


class TestDashScopePollingEdgeCases:
    """Test edge cases in task polling."""

    @respx.mock
    async def test_poll_handles_unknown_status(self, settings):
        """Test that unknown status values are handled gracefully."""
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "task_001"}}))

        # Return unknown status multiple times to trigger timeout
        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_001"
        ).mock(
            return_value=respx.MockResponse(
                200, json={"output": {"task_status": "UNKNOWN"}}
            )
        )

        client = DashScopeClient(settings, mock=False)
        # Mock the max poll attempts to speed up test
        with patch('memecho_gateway.providers.dashscope._MAX_POLL_ATTEMPTS', 2):
            with pytest.raises(TimeoutError, match="timed out"):
                await client.submit_fun_asr("https://example.com/audio.wav")

    @respx.mock
    async def test_poll_handles_missing_status(self, settings):
        """Test that missing status field is handled gracefully."""
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "task_001"}}))

        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_001"
        ).mock(
            return_value=respx.MockResponse(
                200, json={"output": {}}
            )
        )

        client = DashScopeClient(settings, mock=False)
        # Mock the max poll attempts to speed up test
        with patch('memecho_gateway.providers.dashscope._MAX_POLL_ATTEMPTS', 2):
            with pytest.raises(TimeoutError, match="timed out"):
                await client.submit_fun_asr("https://example.com/audio.wav")

    @respx.mock
    async def test_poll_handles_empty_output(self, settings):
        """Test that empty output field is handled gracefully."""
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "task_001"}}))

        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_001"
        ).mock(
            return_value=respx.MockResponse(
                200, json={}
            )
        )

        client = DashScopeClient(settings, mock=False)
        # Mock the max poll attempts to speed up test
        with patch('memecho_gateway.providers.dashscope._MAX_POLL_ATTEMPTS', 2):
            with pytest.raises(TimeoutError, match="timed out"):
                await client.submit_fun_asr("https://example.com/audio.wav")


# ---------------------------------------------------------------------------
# Transcription URL download tests
# ---------------------------------------------------------------------------


class TestTranscriptionDownloader:
    """Test transcription URL download scenarios."""

    @respx.mock
    async def test_download_handles_http_error(self, settings):
        """Test that HTTP errors during transcription download are properly raised."""
        # Mock the submit_transcription call
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(
            return_value=respx.MockResponse(
                200,
                json={
                    "output": {
                        "task_id": "task_001",
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": "https://example.com/transcript.json",
                            }
                        ],
                    }
                },
            )
        )

        # Mock the task polling
        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_001"
        ).mock(
            return_value=respx.MockResponse(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": "https://example.com/transcript.json",
                            }
                        ],
                    }
                },
            )
        )

        # Mock the transcription URL download with error
        respx.get("https://example.com/transcript.json").mock(
            return_value=respx.MockResponse(404, json={"error": "not found"})
        )

        downloader = TranscriptionDownloader(settings, mock=False)
        with pytest.raises(httpx.HTTPStatusError):
            await downloader.download("https://example.com/audio.wav")

    @respx.mock
    async def test_download_handles_connection_error(self, settings):
        """Test that connection errors during transcription download are properly raised."""
        # Mock the submit_transcription call
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(
            return_value=respx.MockResponse(
                200,
                json={
                    "output": {
                        "task_id": "task_001",
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": "https://example.com/transcript.json",
                            }
                        ],
                    }
                },
            )
        )

        # Mock the task polling
        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_001"
        ).mock(
            return_value=respx.MockResponse(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": "https://example.com/transcript.json",
                            }
                        ],
                    }
                },
            )
        )

        # Mock the transcription URL download with connection error
        respx.get("https://example.com/transcript.json").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        downloader = TranscriptionDownloader(settings, mock=False)
        with pytest.raises(httpx.ConnectError):
            await downloader.download("https://example.com/audio.wav")

    @respx.mock
    async def test_download_handles_missing_url(self, settings):
        """Test that missing transcription URL is properly handled."""
        # Mock the submit_transcription call
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(
            return_value=respx.MockResponse(
                200,
                json={
                    "output": {
                        "task_id": "task_001",
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                # No transcription_url field
                            }
                        ],
                    }
                },
            )
        )

        # Mock the task polling
        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_001"
        ).mock(
            return_value=respx.MockResponse(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                # No transcription_url field
                            }
                        ],
                    }
                },
            )
        )

        downloader = TranscriptionDownloader(settings, mock=False)
        with pytest.raises(RuntimeError, match="no result URL"):
            await downloader.download("https://example.com/audio.wav")

    @respx.mock
    async def test_download_handles_failed_subtask(self, settings):
        """Test that failed subtask is properly handled."""
        # Mock the submit_transcription call
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(
            return_value=respx.MockResponse(
                200,
                json={
                    "output": {
                        "task_id": "task_001",
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "FAILED",
                                "code": "AUDIO_TOO_SHORT",
                            }
                        ],
                    }
                },
            )
        )

        # Mock the task polling
        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_001"
        ).mock(
            return_value=respx.MockResponse(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "FAILED",
                                "code": "AUDIO_TOO_SHORT",
                            }
                        ],
                    }
                },
            )
        )

        downloader = TranscriptionDownloader(settings, mock=False)
        with pytest.raises(RuntimeError, match="AUDIO_TOO_SHORT"):
            await downloader.download("https://example.com/audio.wav")


# ---------------------------------------------------------------------------
# No-evidence degradation tests
# ---------------------------------------------------------------------------


class TestNoEvidenceDegradation:
    """Test scenarios where no evidence is available."""

    @respx.mock
    async def test_all_upstream_failures(self, settings):
        """Test when all upstream services fail."""
        # Mock submit_transcription to fail
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(return_value=respx.MockResponse(500, json={"error": "internal server error"}))

        client = DashScopeClient(settings, mock=False)

        # All three upstream calls should fail
        with pytest.raises(httpx.HTTPStatusError):
            await client.submit_fun_asr("https://example.com/audio.wav")

        with pytest.raises(httpx.HTTPStatusError):
            await client.submit_emotion("https://example.com/audio.wav")

        with pytest.raises(httpx.HTTPStatusError):
            await client.submit_transcription("https://example.com/audio.wav")

    @respx.mock
    async def test_partial_upstream_failure(self, settings):
        """Test when some upstream services fail and others succeed."""
        # Mock submit to succeed for fun_asr but fail for emotion
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(
            side_effect=[
                # First call (fun_asr) - success
                respx.MockResponse(
                    200,
                    json={"output": {"task_id": "task_fun_asr"}},
                ),
                # Second call (emotion) - fail
                respx.MockResponse(500, json={"error": "internal server error"}),
            ]
        )

        # Mock polling for fun_asr
        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_fun_asr"
        ).mock(
            return_value=respx.MockResponse(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {"speaker_id": "s1", "start_ms": 0, "end_ms": 1000}
                        ],
                    }
                },
            )
        )

        client = DashScopeClient(settings, mock=False)

        # fun_asr should succeed
        result = await client.submit_fun_asr("https://example.com/audio.wav")
        assert result["output"]["task_status"] == "SUCCEEDED"

        # emotion should fail
        with pytest.raises(httpx.HTTPStatusError):
            await client.submit_emotion("https://example.com/audio.wav")

    @respx.mock
    async def test_upstream_timeout(self, settings):
        """Test timeout handling in upstream services."""
        # Mock submit to succeed
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(
            return_value=respx.MockResponse(
                200,
                json={"output": {"task_id": "task_timeout"}},
            )
        )

        # Mock polling to always return RUNNING (will timeout)
        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_timeout"
        ).mock(
            return_value=respx.MockResponse(
                200,
                json={"output": {"task_status": "RUNNING"}},
            )
        )

        client = DashScopeClient(settings, mock=False)
        # Mock the max poll attempts to speed up test
        with patch('memecho_gateway.providers.dashscope._MAX_POLL_ATTEMPTS', 2):
            # Should timeout after max poll attempts
            with pytest.raises(TimeoutError, match="timed out"):
                await client.submit_fun_asr("https://example.com/audio.wav")


# ---------------------------------------------------------------------------
# Configuration validation tests
# ---------------------------------------------------------------------------


class TestConfigurationValidation:
    """Test configuration validation."""

    def test_missing_base_url_raises(self):
        """Test that missing base URL raises error."""
        s = get_settings()
        s.bailian_audio_base_url = ""
        s.bailian_audio_api_key = "test-key"

        client = DashScopeClient(s, mock=False)
        with pytest.raises(RuntimeError, match="not configured"):
            # This will be caught during submit
            asyncio.run(client.submit_fun_asr("https://example.com/audio.wav"))

    def test_missing_api_key_raises(self):
        """Test that missing API key raises error."""
        s = get_settings()
        s.bailian_audio_base_url = "https://dashscope-mock.example.com"
        s.bailian_audio_api_key = ""

        client = DashScopeClient(s, mock=False)
        with pytest.raises(RuntimeError, match="not configured"):
            # This will be caught during submit
            asyncio.run(client.submit_fun_asr("https://example.com/audio.wav"))

    def test_valid_configuration_works(self):
        """Test that valid configuration works in mock mode."""
        s = get_settings()
        s.bailian_audio_base_url = "https://dashscope-mock.example.com"
        s.bailian_audio_api_key = "test-key"

        client = DashScopeClient(s, mock=True)
        # Should not raise in mock mode
        result = asyncio.run(client.submit_fun_asr("https://example.com/audio.wav"))
        assert result["output"]["task_status"] == "SUCCEEDED"