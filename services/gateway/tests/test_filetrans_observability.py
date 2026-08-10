"""P0 observability: FileTrans text must provably reach the Qwen provider input.

These tests use capturing/stub providers only — no real Bailian calls.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from memecho_gateway import processing_details
from memecho_gateway.config import get_settings
from memecho_gateway.main import app, store
from memecho_gateway.models import SessionCreate
from memecho_gateway.orchestrator import Orchestrator
from memecho_gateway.providers.mock import MockProvider
from memecho_gateway.store import MemoryStore, UploadRecord

CANONICAL_TEXTS = [
    "我们先确认一下今天要解决的问题。",
    "我们好像一直在绕开真正的问题。",
    "我们先把这一版必须完成的部分定下来。",
]


def canonical_segments() -> list[dict]:
    return [
        {
            "speaker_id": "speaker_self",
            "start_ms": index * 8000,
            "end_ms": index * 8000 + 8000,
            "text": text,
            "confidence": 0.9,
        }
        for index, text in enumerate(CANONICAL_TEXTS)
    ]


class StubOSS:
    settings = SimpleNamespace(oss_prefix="memecho-test")

    def __init__(self):
        self.keys: list[str] = []
        self.deleted: list[str] = []

    async def upload_file(self, key, path, content_type):
        self.keys.append(key)
        return f"oss://bucket/{key}"

    async def signed_url(self, key, expires: int = 3600):
        return f"https://signed.example.invalid/{key}"

    async def delete(self, key):
        self.deleted.append(key)


class StubDashScope:
    async def submit_fun_asr(self, _url):
        return {
            "output": {
                "results": [
                    {"speaker_id": "speaker_self", "start_ms": 0, "end_ms": 12000},
                    {"speaker_id": "speaker_2", "start_ms": 12000, "end_ms": 24000},
                ]
            }
        }

    async def submit_emotion(self, _url):
        return {
            "output": {
                "results": [
                    {"start_ms": 0, "end_ms": 12000, "emotion": "neutral", "confidence": 0.8}
                ]
            }
        }


class FailingTranscription:
    async def download(self, _url):
        raise RuntimeError("vendor payload exploded with secret details")


class SucceedingTranscription:
    async def download(self, _url):
        return {
            "transcript": canonical_segments(),
            "language": "zh",
            "duration_ms": 24000,
        }


class CapturingProvider(MockProvider):
    def __init__(self):
        self.session_input = None
        self.track_input = None

    async def analyze(self, session, tracks, request):
        self.session_input = session
        self.track_input = tracks
        return await super().analyze(session, tracks, request)


async def _session_with_wav(tmp_path: Path) -> tuple[MemoryStore, object, Path]:
    store = MemoryStore(tmp_path)
    session = await store.create_session(
        SessionCreate(
            title="observability",
            context="work",
            occurred_at=datetime.now(UTC),
            source_mode="mixed",
        )
    )
    # Identity is pre-resolved so the job does not pause for confirmation.
    session.participant_resolution = {
        "participants": [
            {"id": "speaker_self", "name": "我", "is_self": True},
            {"id": "speaker_2", "name": "参与者 B", "is_self": False},
        ],
        "self_participant_id": "speaker_self",
        "identity_basis": "user_confirmed",
    }
    wav_path = tmp_path / session.id / "upl_obs" / "audio.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    signal = 0.5 * np.sin(2 * np.pi * 220 * np.linspace(0, 2, 32000, endpoint=False))
    sf.write(wav_path, signal.astype(np.float32), 16000)
    upload = UploadRecord(
        "upl_obs",
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
    return store, session, wav_path


async def test_filetrans_text_reaches_qwen_provider_input(tmp_path):
    store, session, wav_path = await _session_with_wav(tmp_path)
    provider = CapturingProvider()
    orchestrator = Orchestrator(
        store,
        provider,
        StubOSS(),
        dashscope_client=StubDashScope(),
        transcription_downloader=SucceedingTranscription(),
    )
    job = await store.create_job(session.id, "req_obs")

    await orchestrator.run(job.id, session.id, {"request_id": "req_obs"})

    assert store.jobs[job.id].status.value == "complete"
    aligned = provider.session_input["observations"]["aligned_segments"]
    assert [segment["text"] for segment in aligned] == CANONICAL_TEXTS
    assert session.result["_aligned_segments"] == aligned

    details = processing_details.build_response(session)
    assert details.aligned_segment_count == len(CANONICAL_TEXTS)
    assert details.submitted_to_qwen is True
    assert details.qwen_status.value == "succeeded"
    track = details.tracks[0]
    assert track.filetrans.status.value == "succeeded"
    assert track.filetrans.sentence_count == len(CANONICAL_TEXTS)
    assert track.filetrans.language == "zh"
    assert track.filetrans.audio_duration_ms == 24000
    assert track.modules["fun_asr"].status.value == "succeeded"
    assert track.modules["emotion"].status.value == "succeeded"
    assert track.modules["transcription"].status.value == "succeeded"
    assert [snippet.text for snippet in details.transcript_segments] == CANONICAL_TEXTS


async def test_filetrans_failure_reports_module_and_safe_code(tmp_path):
    store, session, wav_path = await _session_with_wav(tmp_path)
    provider = CapturingProvider()
    orchestrator = Orchestrator(
        store,
        provider,
        StubOSS(),
        dashscope_client=StubDashScope(),
        transcription_downloader=FailingTranscription(),
    )
    job = await store.create_job(session.id, "req_fail")

    await orchestrator.run(job.id, session.id, {"request_id": "req_fail"})

    assert store.jobs[job.id].status.value == "complete"
    # Alignment must not fabricate segments when FileTrans failed.
    assert provider.session_input["observations"]["aligned_segments"] == []
    errors = provider.session_input["observations"]["model_errors"]
    assert {"source": "transcription", "error_code": "RuntimeError"} in errors

    details = processing_details.build_response(session)
    track = details.tracks[0]
    assert track.filetrans.status.value == "failed"
    assert track.filetrans.error_code == "upstream_task_failed"
    assert track.filetrans.sentence_count is None
    assert details.aligned_segment_count == 0
    assert details.transcript_segments == []
    # The raw vendor exception message must not leak into the contract.
    assert "vendor payload exploded" not in details.model_dump_json()


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "memecho_data_dir", tmp_path)
    monkeypatch.setattr(settings, "chunk_size_bytes", 4)
    store.data_dir = tmp_path
    store.sessions.clear()
    store.jobs.clear()
    store.events.clear()
    store.idempotency.clear()
    with TestClient(app) as test_client:
        yield test_client
    store.sessions.clear()
    store.jobs.clear()
    store.events.clear()
    store.idempotency.clear()


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer change-me"}


def test_processing_details_endpoint_is_authenticated_and_sanitized(
    client: TestClient, tmp_path
):
    created = client.post(
        "/v1/sessions",
        headers=_headers(),
        json={
            "title": "可观察",
            "context": "工作",
            "occurred_at": "2026-08-08T10:00:00+08:00",
            "source_mode": "mixed",
        },
    ).json()
    session_id = created["id"]

    assert client.get(f"/v1/sessions/{session_id}/processing-details").status_code == 401
    assert (
        client.get(
            "/v1/sessions/ses_missing/processing-details", headers=_headers()
        ).status_code
        == 404
    )

    data = b"webm-bytes"
    upload = client.post(
        f"/v1/sessions/{session_id}/uploads",
        headers=_headers(),
        json={
            "track": "mixed",
            "file_name": "browser-mixed.webm",
            "mime_type": "audio/webm",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    ).json()
    for index in range(0, len(data), 4):
        chunk = data[index : index + 4]
        response = client.put(
            f"/v1/sessions/{session_id}/uploads/{upload['upload_id']}/chunks/{index // 4}",
            headers=_headers(),
            content=chunk,
        )
        assert response.status_code == 200
    assert (
        client.post(
            f"/v1/sessions/{session_id}/uploads/{upload['upload_id']}/complete",
            headers=_headers(),
            json={"upload_id": upload["upload_id"], "sha256": hashlib.sha256(data).hexdigest()},
        ).status_code
        == 200
    )

    store.sessions[session_id].participant_resolution = {
        "participants": [
            {"id": "speaker_self", "name": "我", "is_self": True},
            {"id": "speaker_2", "name": "对方", "is_self": False},
        ],
        "self_participant_id": "speaker_self",
        "identity_basis": "user_confirmed",
    }
    job = client.post(
        f"/v1/sessions/{session_id}/analyze",
        headers=_headers(),
        json={"request_id": created["request_id"], "schema_version": "1.1"},
    ).json()
    assert client.get(f"/v1/jobs/{job['id']}", headers=_headers()).json()["status"] == "complete"

    response = client.get(
        f"/v1/sessions/{session_id}/processing-details", headers=_headers()
    )
    assert response.status_code == 200
    details = response.json()

    track = details["tracks"][0]
    assert track["file_name"] == "browser-mixed.webm"
    assert track["track"] == "mixed"
    assert track["mime_type"] == "audio/webm"
    assert track["size_bytes"] == len(data)
    assert track["upload_status"] == "succeeded"
    assert track["received_chunks"] == track["expected_chunks"] == 3
    assert track["oss_status"] == "succeeded"
    assert track["filetrans"]["status"] == "succeeded"
    assert track["filetrans"]["sentence_count"] == 3
    assert details["aligned_segment_count"] == 3
    assert details["submitted_to_qwen"] is True
    assert details["qwen_status"] == "succeeded"
    assert len(details["transcript_segments"]) == 3

    serialized = response.text
    assert str(tmp_path) not in serialized.replace("\\\\", "\\")
    assert "mock-oss.example.com" not in serialized
    assert "expires=" not in serialized
    assert "Bearer" not in serialized


def test_transcript_contract_stays_bounded_for_local_persistence():
    session = SimpleNamespace(id="ses_big", processing={})
    oversized_text = "长" * 900
    for index in range(processing_details.TRANSCRIPT_SEGMENT_LIMIT + 50):
        processing_details.add_transcript(
            session,
            [
                {
                    "speaker_id": "speaker_self",
                    "start_ms": index,
                    "end_ms": index + 1,
                    "text": oversized_text,
                }
            ],
        )

    details = processing_details.build_response(session)

    assert len(details.transcript_segments) == processing_details.TRANSCRIPT_SEGMENT_LIMIT
    assert details.transcript_truncated is True
    assert all(
        len(snippet.text) <= processing_details.TRANSCRIPT_TEXT_LIMIT
        for snippet in details.transcript_segments
    )
