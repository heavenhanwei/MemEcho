"""Offline contract tests for the FileTrans probe, parsing, and evidence gates.

Everything here is mocked (respx / stub providers). No paid upstream call is
made; the explicit paid smoke test lives in ``scripts/filetrans_smoke.py``
and is disabled by default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
import pytest
import respx
import soundfile as sf

from memecho_gateway.config import get_settings
from memecho_gateway import processing_details
from memecho_gateway.models import SessionCreate
from memecho_gateway.orchestrator import Orchestrator
from memecho_gateway.providers.dashscope import (
    DashScopeClient,
    _PROBE_TASK_ID,
    _output_dict,
)
from memecho_gateway.providers.mock import MockProvider
from memecho_gateway.providers.transcription import TranscriptionDownloader
from memecho_gateway.store import MemoryStore, UploadRecord


@pytest.fixture
def settings():
    s = get_settings()
    s.bailian_audio_base_url = "https://dashscope-mock.example.com"
    s.bailian_audio_api_key = "test-key"
    s.bailian_transcription_model = "qwen3-asr-flash-filetrans"
    return s


# ---------------------------------------------------------------------------
# Bill-free credentials probe
# ---------------------------------------------------------------------------


class TestProbeCredentials:
    """The probe must be protocol-correct and never create a paid task."""

    @respx.mock
    async def test_task_not_found_means_credentials_ok(self, settings):
        route = respx.get(
            f"https://dashscope-mock.example.com/api/v1/tasks/{_PROBE_TASK_ID}"
        ).mock(
            return_value=respx.MockResponse(
                404, json={"code": "InvalidTaskId", "message": "task not found"}
            )
        )
        client = DashScopeClient(settings, mock=False)
        ok, error = await client.probe_credentials(
            api_key="sk-test", base_url="https://dashscope-mock.example.com"
        )
        assert ok is True
        assert error is None
        assert route.calls.last.request.headers["Authorization"] == "Bearer sk-test"

    @respx.mock
    async def test_401_maps_to_auth_failure(self, settings):
        respx.get(
            f"https://dashscope-mock.example.com/api/v1/tasks/{_PROBE_TASK_ID}"
        ).mock(
            return_value=respx.MockResponse(
                401, json={"code": "InvalidApiKey"}
            )
        )
        client = DashScopeClient(settings, mock=False)
        ok, error = await client.probe_credentials(
            api_key="sk-bad", base_url="https://dashscope-mock.example.com"
        )
        assert ok is False
        assert error is not None
        assert "认证失败" in error

    @respx.mock
    async def test_5xx_maps_to_upstream_failure(self, settings):
        respx.get(
            f"https://dashscope-mock.example.com/api/v1/tasks/{_PROBE_TASK_ID}"
        ).mock(return_value=respx.MockResponse(502, json={}))
        client = DashScopeClient(settings, mock=False)
        ok, error = await client.probe_credentials(
            api_key="sk-test", base_url="https://dashscope-mock.example.com"
        )
        assert ok is False
        assert "HTTP 502" in (error or "")

    @respx.mock
    async def test_connection_error_maps_to_connection_failure(self, settings):
        respx.get(
            f"https://dashscope-mock.example.com/api/v1/tasks/{_PROBE_TASK_ID}"
        ).mock(side_effect=httpx.ConnectError("connection refused"))
        client = DashScopeClient(settings, mock=False)
        ok, error = await client.probe_credentials(
            api_key="sk-test", base_url="https://dashscope-mock.example.com"
        )
        assert ok is False
        assert "连接失败" in (error or "")

    async def test_missing_config_short_circuits(self, settings):
        client = DashScopeClient(settings, mock=False)
        ok, error = await client.probe_credentials(api_key="", base_url="https://x")
        assert ok is False
        assert error == "缺少 API Key 或 Endpoint"

    @respx.mock
    async def test_full_endpoint_base_url_is_normalized(self, settings):
        route = respx.get(
            f"https://dashscope-mock.example.com/api/v1/tasks/{_PROBE_TASK_ID}"
        ).mock(return_value=respx.MockResponse(400, json={"code": "InvalidTaskId"}))
        client = DashScopeClient(settings, mock=False)
        ok, _ = await client.probe_credentials(
            api_key="sk-test",
            base_url=(
                "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
            ),
        )
        assert ok is True
        assert route.called


# ---------------------------------------------------------------------------
# Defensive parsing: submit/poll field differences, no bare KeyError
# ---------------------------------------------------------------------------


class TestDefensiveParsing:
    def test_output_dict_tolerates_missing_or_null_output(self):
        assert _output_dict({}) == {}
        assert _output_dict({"output": None}) == {}
        assert _output_dict({"output": "oops"}) == {}
        assert _output_dict([]) == {}
        assert _output_dict({"output": {"task_id": "t"}}) == {"task_id": "t"}

    @respx.mock
    async def test_submit_with_null_output_raises_runtime_error(self, settings):
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(return_value=respx.MockResponse(200, json={"output": None}))
        client = DashScopeClient(settings, mock=False)
        with pytest.raises(RuntimeError, match="No task_id"):
            await client.submit_transcription_task("https://example.com/a.wav")

    @respx.mock
    async def test_poll_with_null_output_treated_as_non_terminal(self, settings):
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(return_value=respx.MockResponse(200, json={"output": {"task_id": "t_n"}}))
        respx.get("https://dashscope-mock.example.com/api/v1/tasks/t_n").mock(
            return_value=respx.MockResponse(200, json={"output": None})
        )
        client = DashScopeClient(settings, mock=False)
        with patch("memecho_gateway.providers.dashscope._MAX_POLL_ATTEMPTS", 2):
            with patch("memecho_gateway.providers.dashscope._POLL_BACKOFF", (0.0,)):
                with pytest.raises(TimeoutError, match="timed out"):
                    await client.poll_task_result("t_n")

    def test_transcription_url_accepts_singular_and_plural_shapes(self):
        singular = {
            "output": {"result": {"transcription_url": "https://r.example.com/1.json"}}
        }
        plural = {
            "output": {
                "results": [
                    {"subtask_status": "SUCCEEDED", "transcription_url": "https://r.example.com/2.json"}
                ]
            }
        }
        assert (
            TranscriptionDownloader._transcription_url(singular)
            == "https://r.example.com/1.json"
        )
        assert (
            TranscriptionDownloader._transcription_url(plural)
            == "https://r.example.com/2.json"
        )

    def test_transcription_url_failed_status_variant(self):
        legacy = {
            "output": {
                "results": [{"subtask_status": "FAILED", "code": "AUDIO_TOO_SHORT"}]
            }
        }
        variant = {
            "output": {"results": [{"status": "FAILED", "code": "MalformedURL"}]}
        }
        with pytest.raises(RuntimeError, match="AUDIO_TOO_SHORT"):
            TranscriptionDownloader._transcription_url(legacy)
        with pytest.raises(RuntimeError, match="MalformedURL"):
            TranscriptionDownloader._transcription_url(variant)

    def test_normalize_result_skips_malformed_entries_without_raising(self):
        payload = {
            "transcripts": [
                "not-a-dict",
                {"sentences": "not-a-list"},
                {
                    "channel_id": 0,
                    "sentences": [
                        None,
                        "junk",
                        {"text": "缺时间戳"},
                        {"begin_time": "bad", "end_time": 10, "text": "坏时间"},
                        {
                            "begin_time": 0,
                            "end_time": 1000,
                            "text": "  有效句子  ",
                            "emotion_confidence": "not-a-number",
                        },
                    ],
                },
            ],
            "properties": "not-a-dict",
        }
        result = TranscriptionDownloader._normalize_result(payload)
        assert len(result["transcript"]) == 1
        assert result["transcript"][0]["text"] == "有效句子"
        assert result["transcript"][0]["emotion_confidence"] == 0.0
        assert result["duration_ms"] is None

    def test_normalize_result_normalizes_mixed_newlines(self):
        payload = {
            "transcripts": [
                {
                    "channel_id": 0,
                    "sentences": [
                        {"begin_time": 0, "end_time": 1000, "text": "第一行\r\n第二行"},
                        {"begin_time": 1000, "end_time": 2000, "text": "旧Mac换行\r结尾"},
                        {"begin_time": 2000, "end_time": 3000, "text": "   \r\n  "},
                    ],
                }
            ],
            "properties": {"original_duration_in_milliseconds": 3000},
        }
        result = TranscriptionDownloader._normalize_result(payload)
        texts = [segment["text"] for segment in result["transcript"]]
        assert texts == ["第一行\n第二行", "旧Mac换行\n结尾"]
        assert result["duration_ms"] == 3000


class TestFailedTaskMessageIsSanitized:
    """FAILED task errors must not leak signed URLs or vendor payloads."""

    @respx.mock
    async def test_poll_failure_message_has_no_urls(self, settings):
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(
            return_value=respx.MockResponse(
                200, json={"output": {"task_id": "task_secret_001"}}
            )
        )
        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_secret_001"
        ).mock(
            return_value=respx.MockResponse(
                200,
                json={
                    "output": {
                        "task_status": "FAILED",
                        "code": "AudioDecodeError",
                        "transcription_url": (
                            "https://bucket.oss.example.com/r.json?Signature=SECRET"
                        ),
                    }
                },
            )
        )
        client = DashScopeClient(settings, mock=False)
        with pytest.raises(RuntimeError) as excinfo:
            await client.submit_fun_asr("https://example.com/audio.wav")
        message = str(excinfo.value)
        assert "failed" in message
        assert "AudioDecodeError" in message
        assert "SECRET" not in message
        assert "http" not in message
        # Raw task id is replaced by the sanitized reference.
        assert "task_secret_001" not in message
        assert "ft_***" in message


# ---------------------------------------------------------------------------
# End-to-end (offline) download_with_phase contract
# ---------------------------------------------------------------------------


class TestDownloadWithPhaseContract:
    @respx.mock
    async def test_full_flow_exposes_stats_and_normalizes_text(self, settings):
        respx.post(
            "https://dashscope-mock.example.com/api/v1/services/audio/asr/transcription"
        ).mock(
            return_value=respx.MockResponse(
                200, json={"output": {"task_id": "task_flow_001"}}
            )
        )
        respx.get(
            "https://dashscope-mock.example.com/api/v1/tasks/task_flow_001"
        ).mock(
            side_effect=[
                respx.MockResponse(
                    200, json={"output": {"task_status": "RUNNING"}}
                ),
                respx.MockResponse(
                    200,
                    json={
                        "output": {
                            "task_status": "SUCCEEDED",
                            "result": {
                                "transcription_url": "https://r.example.com/flow.json"
                            },
                        }
                    },
                ),
            ]
        )
        respx.get("https://r.example.com/flow.json").mock(
            return_value=respx.MockResponse(
                200,
                json={
                    "transcripts": [
                        {
                            "channel_id": 0,
                            "sentences": [
                                {
                                    "begin_time": 0,
                                    "end_time": 1500,
                                    "text": "第一句\r\n带换行",
                                },
                                {"begin_time": 1500, "end_time": 3000, "text": "第二句"},
                            ],
                        }
                    ],
                    "properties": {"original_duration_in_milliseconds": 3000},
                },
            )
        )

        with patch("memecho_gateway.providers.dashscope._POLL_BACKOFF", (0.0,)):
            downloader = TranscriptionDownloader(settings, mock=False)
            phases: list[tuple[str, dict]] = []
            result = await downloader.download_with_phase(
                "https://example.com/audio.wav",
                on_phase=lambda name, **kw: phases.append((name, kw)),
            )

        assert [segment["text"] for segment in result["transcript"]] == [
            "第一句\n带换行",
            "第二句",
        ]
        by_name = {name: kw for name, kw in phases}
        assert by_name["queued"]["task_reference"] == "ft_***ow_001"
        assert by_name["polling"]["poll_attempts"] >= 1
        assert by_name["succeeded"]["sentence_count"] == 2
        assert by_name["succeeded"]["audio_duration_ms"] == 3000
        # Phases must never carry signed URLs or transcript text.
        serialized = repr(phases)
        assert "Signature" not in serialized
        assert "第一句" not in serialized


# ---------------------------------------------------------------------------
# Evidence citation validation regression (audio path)
# ---------------------------------------------------------------------------


class BrokenEvidenceProvider(MockProvider):
    """Returns an otherwise-valid result whose insight cites missing evidence."""

    async def analyze(self, session, tracks, request, **kwargs):
        result = await super().analyze(session, tracks, request, **kwargs)
        result["insights"][0]["evidence_refs"] = ["ev_does_not_exist"]
        return result


class StubOSS:
    settings = SimpleNamespace(oss_prefix="memecho-test")

    async def upload_file(self, key, path, content_type):
        return f"oss://bucket/{key}"

    async def signed_url(self, key, expires: int = 7200):
        return f"https://signed.example.invalid/{key}"

    async def delete(self, key):
        return None


class StubDashScope:
    async def submit_fun_asr(self, _url, **_kwargs):
        return {
            "output": {
                "results": [
                    {"speaker_id": "speaker_self", "start_ms": 0, "end_ms": 24000}
                ]
            }
        }

    async def submit_emotion(self, _url, **_kwargs):
        return {"output": {"results": []}}


class StubTranscription:
    async def download(self, _url):
        return self._result()

    async def download_with_phase(self, _url, *, on_phase=None):
        if on_phase:
            on_phase("succeeded", sentence_count=1)
        return self._result()

    @staticmethod
    def _result():
        return {
            "transcript": [
                {
                    "speaker_id": "speaker_self",
                    "start_ms": 0,
                    "end_ms": 8000,
                    "text": "我们先确认一下今天要解决的问题。",
                    "confidence": 0.9,
                }
            ],
            "language": "zh",
            "duration_ms": 8000,
        }


async def _audio_session(tmp_path: Path) -> tuple[MemoryStore, object]:
    store = MemoryStore(tmp_path)
    session = await store.create_session(
        SessionCreate(
            title="evidence",
            context="work",
            occurred_at=datetime.now(UTC),
            source_mode="mixed",
        )
    )
    session.participant_resolution = {
        "participants": [
            {"id": "speaker_self", "name": "我", "is_self": True},
            {"id": "speaker_2", "name": "参与者 B", "is_self": False},
        ],
        "self_participant_id": "speaker_self",
        "identity_basis": "user_confirmed",
    }
    wav_path = tmp_path / session.id / "upl_ev" / "audio.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    signal = 0.5 * np.sin(2 * np.pi * 220 * np.linspace(0, 2, 32000, endpoint=False))
    sf.write(wav_path, signal.astype(np.float32), 16000)
    upload = UploadRecord(
        "upl_ev",
        session.id,
        "mixed",
        "audio.wav",
        "audio/wav",
        wav_path.stat().st_size,
        "sha",
        wav_path.parent,
        completed_path=wav_path,
    )
    session.uploads[upload.id] = upload
    processing_details.mark_upload_completed(session, upload)
    return store, session


async def test_evidence_citation_violation_fails_job_with_stable_code(tmp_path):
    store, session = await _audio_session(tmp_path)
    orchestrator = Orchestrator(
        store,
        BrokenEvidenceProvider(),
        StubOSS(),
        dashscope_client=StubDashScope(),
        transcription_downloader=StubTranscription(),
    )
    job = await store.create_job(session.id, "req_ev")

    await orchestrator.run(job.id, session.id, {"request_id": "req_ev"})

    record = store.jobs[job.id]
    assert record.status.value == "failed"
    assert record.error_code == "AnalysisContractError"
    assert "references missing evidence" in (record.error_detail or "")

    details = processing_details.build_response(session)
    # The qwen stage produced a contract-violating result: it must be
    # reported as failed with a stable code, not as succeeded.
    assert details.qwen_status.value == "failed"
    assert details.qwen_error_code == "invalid_upstream_result"
    # Transcript still reached processing details before validation failed.
    assert details.aligned_segment_count == 1
    assert session.result is None


async def test_valid_evidence_refs_keep_qwen_succeeded(tmp_path):
    store, session = await _audio_session(tmp_path)
    orchestrator = Orchestrator(
        store,
        MockProvider(),
        StubOSS(),
        dashscope_client=StubDashScope(),
        transcription_downloader=StubTranscription(),
    )
    job = await store.create_job(session.id, "req_ok")

    await orchestrator.run(job.id, session.id, {"request_id": "req_ok"})

    assert store.jobs[job.id].status.value == "complete"
    details = processing_details.build_response(session)
    assert details.qwen_status.value == "succeeded"
    assert details.qwen_error_code is None
