"""Tests for FileTrans post-meeting fixes:

- raw UnboundLocalError guard in download_with_phase
- Pre-submit URL validation and sanitized logging
- OSS signed URL default expiry
- DashScope validate_audio_url
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx

from memecho_gateway.config import get_settings
from memecho_gateway.providers.dashscope import (
    DashScopeClient,
    _sanitize_url_for_log as ds_sanitize,
    validate_audio_url,
)
from memecho_gateway.providers.oss import AliyunOSSClient, OSSClient
from memecho_gateway.providers.transcription import (
    TranscriptionDownloader,
    _sanitize_url_for_log,
    _validate_audio_url,
)


# ---------------------------------------------------------------------------
# _sanitize_url_for_log
# ---------------------------------------------------------------------------


class TestSanitizeUrlForLog:
    """URL must be logged without query-string (signatures/tokens)."""

    def test_strips_query_params(self):
        url = "https://bucket.oss-cn-hangzhou.aliyuncs.com/audio.wav?OSSAccessKeyId=AK&Expires=123&Signature=abc"
        assert _sanitize_url_for_log(url) == "https://bucket.oss-cn-hangzhou.aliyuncs.com/audio.wav"

    def test_strips_complex_query(self):
        url = "https://example.com/path/file.webm?a=1&b=2&c=3"
        assert _sanitize_url_for_log(url) == "https://example.com/path/file.webm"

    def test_no_query_passthrough(self):
        url = "https://example.com/audio.wav"
        assert _sanitize_url_for_log(url) == "https://example.com/audio.wav"

    def test_dashscope_version_same(self):
        """Both modules should produce the same output for the same input."""
        url = "https://oss.example.com/k?X=Y"
        assert _sanitize_url_for_log(url) == ds_sanitize(url)


# ---------------------------------------------------------------------------
# _validate_audio_url (transcription.py)
# ---------------------------------------------------------------------------


class TestValidateAudioUrlTranscription:
    """Pre-flight URL validation in transcription module."""

    def test_empty_url_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_audio_url("")

    def test_whitespace_url_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_audio_url("   ")

    def test_ftp_scheme_raises(self):
        with pytest.raises(ValueError, match="http/https"):
            _validate_audio_url("ftp://example.com/audio.wav")

    def test_no_host_raises(self):
        with pytest.raises(ValueError, match="no host"):
            _validate_audio_url("https://")

    def test_valid_https_passes(self):
        _validate_audio_url("https://example.com/audio.wav")

    def test_valid_http_passes(self):
        _validate_audio_url("http://localhost:9000/audio.wav")

    def test_signed_url_passes(self):
        _validate_audio_url(
            "https://bucket.oss.example.com/a.wav?OSSAccessKeyId=x&Expires=1&Signature=y"
        )


# ---------------------------------------------------------------------------
# validate_audio_url (dashscope.py)
# ---------------------------------------------------------------------------


class TestValidateAudioUrlDashScope:
    """Pre-flight URL validation in DashScope module."""

    def test_empty_url_raises(self):
        with pytest.raises(ValueError, match="empty"):
            validate_audio_url("")

    def test_ftp_scheme_raises(self):
        with pytest.raises(ValueError, match="http/https"):
            validate_audio_url("ftp://example.com/audio.wav")

    def test_no_host_raises(self):
        with pytest.raises(ValueError, match="no host"):
            validate_audio_url("https://")

    def test_valid_https_passes(self):
        validate_audio_url("https://example.com/audio.wav")

    def test_unsupported_content_type_warns(self, caplog):
        """Unsupported MIME type should warn but not raise."""
        import logging

        with caplog.at_level(logging.WARNING):
            validate_audio_url(
                "https://example.com/f.txt",
                content_type="text/plain",
            )
        assert "unsupported" in caplog.text

    def test_supported_content_type_no_warning(self, caplog):
        """Supported MIME type should not warn."""
        import logging

        with caplog.at_level(logging.WARNING):
            validate_audio_url(
                "https://example.com/f.webm",
                content_type="audio/webm",
            )
        assert "unsupported" not in caplog.text


# ---------------------------------------------------------------------------
# download_with_phase — raw guard
# ---------------------------------------------------------------------------


@pytest.fixture
def settings():
    s = get_settings()
    s.bailian_audio_base_url = "https://dashscope-mock.example.com"
    s.bailian_audio_api_key = "test-key"
    s.bailian_transcription_model = "qwen3-asr-flash-filetrans"
    return s


class TestDownloadWithPhaseRawGuard:
    """download_with_phase must not raise UnboundLocalError."""

    async def test_empty_results_raises_runtime_error(self, settings):
        """When DashScope returns empty results, must raise RuntimeError (not UnboundLocalError)."""
        downloader = TranscriptionDownloader(settings, mock=False)

        # Mock submit_transcription_task to return a task id
        downloader.dashscope.submit_transcription_task = AsyncMock(return_value="task_001")
        # Mock poll_task_result to return empty results
        downloader.dashscope.poll_task_result = AsyncMock(
            return_value={"output": {"task_status": "SUCCEEDED", "results": []}}
        )

        phases: list[str] = []
        def on_phase(phase, **kw):
            phases.append(phase)

        with pytest.raises(RuntimeError, match="no result URL"):
            await downloader.download_with_phase(
                "https://example.com/audio.wav", on_phase=on_phase,
            )
        assert "failed" in phases

    async def test_results_without_transcription_url_raises(self, settings):
        """When results have no transcription_url, must raise RuntimeError (not UnboundLocalError)."""
        downloader = TranscriptionDownloader(settings, mock=False)

        downloader.dashscope.submit_transcription_task = AsyncMock(return_value="task_002")
        downloader.dashscope.poll_task_result = AsyncMock(
            return_value={
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [{"subtask_status": "SUCCEEDED"}],  # no transcription_url
                }
            }
        )

        phases: list[str] = []
        def on_phase(phase, **kw):
            phases.append(phase)

        with pytest.raises(RuntimeError, match="no result URL"):
            await downloader.download_with_phase(
                "https://example.com/audio.wav", on_phase=on_phase,
            )
        assert "failed" in phases

    async def test_failed_subtask_raises_with_code(self, settings):
        """When subtask_status is FAILED, must raise RuntimeError with upstream code."""
        downloader = TranscriptionDownloader(settings, mock=False)

        downloader.dashscope.submit_transcription_task = AsyncMock(return_value="task_003")
        downloader.dashscope.poll_task_result = AsyncMock(
            return_value={
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [
                        {"subtask_status": "FAILED", "code": "AUDIO_TOO_SHORT"}
                    ],
                }
            }
        )

        phases: list[str] = []
        def on_phase(phase, **kw):
            phases.append(phase)

        with pytest.raises(RuntimeError, match="AUDIO_TOO_SHORT"):
            await downloader.download_with_phase(
                "https://example.com/audio.wav", on_phase=on_phase,
            )
        assert "failed" in phases

    async def test_invalid_url_raises_value_error(self, settings):
        """Invalid URL must be caught by _validate_audio_url before submit."""
        downloader = TranscriptionDownloader(settings, mock=False)
        downloader.dashscope.submit_transcription_task = AsyncMock()

        with pytest.raises(ValueError, match="http/https"):
            await downloader.download_with_phase("ftp://bad.example.com/audio.wav")

        downloader.dashscope.submit_transcription_task.assert_not_called()

    async def test_mock_mode_bypasses_validation(self, settings):
        """Mock mode returns placeholder without hitting validation or network."""
        downloader = TranscriptionDownloader(settings, mock=True)
        result = await downloader.download_with_phase("ftp://whatever")
        assert result["language"] == "zh"
        assert len(result["transcript"]) > 0


# ---------------------------------------------------------------------------
# OSS signed URL default expiry
# ---------------------------------------------------------------------------


class TestOSSSignedUrlExpiry:
    """OSS signed URL default expiry must be >= 7200s."""

    def test_protocol_default_is_7200(self):
        """The Protocol annotation should specify 7200s default."""
        import inspect
        sig = inspect.signature(OSSClient.signed_url)
        assert sig.parameters["expires"].default == 7200

    async def test_aliyun_client_default_is_7200(self):
        """AliyunOSSClient mock signed_url should use 7200s default."""
        s = get_settings()
        client = AliyunOSSClient(s, mock=True)
        url = await client.signed_url("test/key.wav")
        # mock URL contains expires=<time+7200>
        import re
        m = re.search(r"expires=(\d+)", url)
        assert m is not None
        expires_val = int(m.group(1))
        now = int(time.time())
        # Should be approximately now + 7200 (within 5s tolerance)
        assert abs(expires_val - (now + 7200)) < 5

    async def test_aliyun_signed_url_preserves_object_path_slashes(self, settings):
        """DashScope must not receive object path separators encoded as %2F."""
        client = AliyunOSSClient(settings, mock=False)
        bucket = MagicMock()
        bucket.sign_url.return_value = (
            "https://bucket.example.com/memecho-tmp/session/audio.webm?Signature=redacted"
        )
        key = "memecho-tmp/session/audio.webm"

        with patch.object(client, "_bucket", return_value=bucket):
            result = await client.signed_url(key)

        bucket.sign_url.assert_called_once_with("GET", key, 7200, slash_safe=True)
        assert "%2F" not in result


# ---------------------------------------------------------------------------
# DashScope submit sanitised logging (no full URL in logs)
# ---------------------------------------------------------------------------


class TestDashScopeSanitisedLogging:
    """DashScope submit must log sanitized URL, not full signed URL."""

    @respx.mock
    async def test_submit_logs_sanitized_url(self, settings, caplog):
        """Log output must contain path but NOT query params."""
        import logging

        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "t1"}}))

        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/t1"
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

        signed_url = "https://bucket.oss.example.com/sess/up1/a.wav?OSSAccessKeyId=AKID&Expires=999&Signature=SECRET"

        client = DashScopeClient(settings, mock=False)
        with caplog.at_level(logging.INFO):
            await client.submit_fun_asr(signed_url)

        # Must contain sanitized path
        assert "bucket.oss.example.com/sess/up1/a.wav" in caplog.text
        # Must NOT contain signature components
        assert "OSSAccessKeyId" not in caplog.text
        assert "SECRET" not in caplog.text
        assert "AKID" not in caplog.text
