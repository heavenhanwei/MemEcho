from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from memecho_gateway.config import get_settings
from memecho_gateway.main import app, store
from memecho_gateway.models import AnalysisRequest, JobStatus, SessionCreate
from memecho_gateway.providers.bailian import BailianProvider
from memecho_gateway.orchestrator import Orchestrator
from memecho_gateway.providers.mock import MockProvider
from memecho_gateway.store import MemoryStore
from memecho_gateway.text_only import build_text_segments


AUTH_HEADERS = {"Authorization": "Bearer change-me"}


@pytest.fixture
def client(tmp_path):
    settings = get_settings()
    settings.memecho_data_dir = tmp_path
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


def create_session(client: TestClient) -> dict:
    response = client.post(
        "/v1/sessions",
        headers=AUTH_HEADERS,
        json={
            "title": "Text-only review",
            "context": "work",
            "occurred_at": "2026-08-02T10:00:00+08:00",
            "source_mode": "import",
        },
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("source_type", ["text", "transcript"])
async def test_text_only_orchestrator_skips_audio_services_and_uses_stable_evidence(
    tmp_path, source_type
):
    memory_store = MemoryStore(tmp_path)
    session = await memory_store.create_session(
        SessionCreate(
            title="Text review",
            context="work",
            occurred_at=datetime.now(UTC),
            source_mode="import",
        )
    )
    source_text = (
        "First paragraph states the observable fact.\n\nSecond paragraph gives an opinion."
    )

    class ForbiddenAudioService:
        def __getattr__(self, name):
            raise AssertionError(f"text-only orchestration called audio service: {name}")

    class CapturingProvider(MockProvider):
        def __init__(self):
            self.session_input = None
            self.tracks_input = None

        async def analyze(self, session, tracks, request):
            self.session_input = session
            self.tracks_input = tracks
            return await super().analyze(session, tracks, request)

    provider = CapturingProvider()
    orchestrator = Orchestrator(
        memory_store,
        provider,
        oss_client=ForbiddenAudioService(),
        dashscope_client=ForbiddenAudioService(),
        transcription_downloader=ForbiddenAudioService(),
    )
    request = {
        "request_id": f"req_{source_type}",
        "schema_version": "1.1",
        "source": {"type": source_type, "text": source_text},
        "participants": [{"id": "self", "name": "Self", "is_self": True}],
        "self_identity_basis": "auto_single_speaker",
        "target_participant_ids": ["self"],
    }
    job = await memory_store.create_job(session.id, request["request_id"])
    session.analysis_requests[job.id] = request

    await orchestrator.run(job.id, session.id, request)

    statuses = []
    while not memory_store.events[job.id].empty():
        statuses.append((await memory_store.events[job.id].get())["status"])
    assert statuses == [
        "aligning",
        "analyzing",
        "rendering",
        "complete",
    ]

    assert memory_store.jobs[job.id].status == JobStatus.complete
    assert provider.tracks_input == []
    segments = provider.session_input["observations"]["text_segments"]
    assert segments == build_text_segments(source_text)
    assert len(segments) == 2
    assert all(segment["start_ms"] == segment["end_ms"] == 0 for segment in segments)
    assert provider.session_input["observations"]["acoustic_metrics"] == []
    assert session.job_intermediates[job.id]["tracks"] == []

    result = session.result
    assert result["analysis_mode"] == "text_only"
    assert result["scope"]["signals_used"] == ["transcript", "linguistic"]
    assert "acoustic" in result["scope"]["signals_missing"]
    assert all(item["source_type"] == "transcript" for item in result["evidence"])
    assert all(point["linguistic_weight"] == 1 for point in result["vad_series"])
    assert all(point["acoustic_weight"] == 0 for point in result["vad_series"])
    assert "_quality_metrics" not in result
    assert "_aligned_segments" not in result


@pytest.mark.parametrize(
    "source",
    [
        {"type": "text", "text": "   "},
        {"type": "transcript", "text": ""},
        {"type": "text", "path": "local.txt"},
    ],
)
def test_text_source_requires_non_empty_inline_text(source):
    with pytest.raises(ValidationError, match="require non-empty source.text"):
        AnalysisRequest.model_validate({"request_id": "req_empty", "source": source})


def test_analysis_source_locator_is_xor():
    with pytest.raises(ValidationError, match="exactly one"):
        AnalysisRequest.model_validate(
            {
                "request_id": "req_xor",
                "source": {"type": "text", "text": "content", "path": "input.txt"},
            }
        )


def test_text_source_cannot_be_combined_with_media_upload(client: TestClient):
    session = create_session(client)
    digest = hashlib.sha256(b"audio").hexdigest()
    upload = client.post(
        f"/v1/sessions/{session['id']}/uploads",
        headers=AUTH_HEADERS,
        json={
            "track": "import",
            "file_name": "audio.wav",
            "mime_type": "audio/wav",
            "size": 5,
            "sha256": digest,
        },
    )
    assert upload.status_code == 200

    response = client.post(
        f"/v1/sessions/{session['id']}/analyze",
        headers=AUTH_HEADERS,
        json={
            "request_id": session["request_id"],
            "source": {"type": "text", "text": "Only this text may be analyzed."},
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "text source cannot be combined with media uploads"


def test_text_only_analysis_is_idempotent_and_returns_result(client: TestClient):
    session = create_session(client)
    payload = {
        "request_id": session["request_id"],
        "source": {
            "type": "transcript",
            "text": "Paragraph one.\n\nParagraph two.",
        },
    }
    first = client.post(
        f"/v1/sessions/{session['id']}/analyze",
        headers=AUTH_HEADERS,
        json=payload,
    )
    second = client.post(
        f"/v1/sessions/{session['id']}/analyze",
        headers=AUTH_HEADERS,
        json=payload,
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(store.sessions[session["id"]].analysis_requests) == 1
    result = client.get(
        f"/v1/sessions/{session['id']}/result", headers=AUTH_HEADERS
    )
    assert result.status_code == 200
    assert result.json()["analysis_mode"] == "text_only"


async def test_bailian_text_provider_receives_strict_text_only_prompt():
    request = {
        "request_id": "req_bailian_text",
        "source": {"type": "text", "text": "Only submitted evidence."},
    }
    segments = build_text_segments(request["source"]["text"])
    session = {"observations": {"text_segments": segments}}
    mock_result = await MockProvider().analyze(session, [], request)

    class RecordingBailianProvider(BailianProvider):
        def __init__(self):
            self.messages = None

        async def _chat_completion(self, messages):
            self.messages = messages
            return json.dumps(mock_result)

    provider = RecordingBailianProvider()
    result = await provider.analyze(session, [], request)

    assert result["analysis_mode"] == "text_only"
    system_prompt = provider.messages[0]["content"]
    assert "TEXT-ONLY MODE" in system_prompt
    assert "acoustic_weight=0" in system_prompt
    assert "session.observations.text_segments" in system_prompt
    assert "REQUIRED OUTPUT JSON SCHEMA" in system_prompt
    assert '"schema_version"' in system_prompt


async def test_bailian_provider_repairs_structurally_invalid_json_once():
    request = {
        "request_id": "req_bailian_repair",
        "source": {"type": "text", "text": "Only submitted evidence."},
    }
    segments = build_text_segments(request["source"]["text"])
    session = {"observations": {"text_segments": segments}}
    valid_result = await MockProvider().analyze(session, [], request)

    class RepairingBailianProvider(BailianProvider):
        def __init__(self):
            self.calls = []

        async def _chat_completion(self, messages):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return json.dumps({"summary": "wrong wrapper"})
            return json.dumps(valid_result)

    provider = RepairingBailianProvider()
    result = await provider.analyze(session, [], request)

    assert result["schema_version"] == "1.1"
    assert len(provider.calls) == 2
    repair_payload = json.loads(provider.calls[1][1]["content"])
    assert repair_payload["invalid_result"] == {"summary": "wrong wrapper"}
    assert repair_payload["validation_errors"]


async def test_bailian_provider_repairs_missing_evidence_reference_once():
    request = {
        "request_id": "req_bailian_semantic_repair",
        "source": {"type": "text", "text": "Only submitted evidence."},
    }
    segments = build_text_segments(request["source"]["text"])
    session = {"observations": {"text_segments": segments}}
    valid_result = await MockProvider().analyze(session, [], request)
    invalid_result = json.loads(json.dumps(valid_result))
    invalid_result["insights"][0]["evidence_refs"] = ["ev_missing"]

    class SemanticRepairingBailianProvider(BailianProvider):
        def __init__(self):
            self.calls = []

        async def _chat_completion(self, messages):
            self.calls.append(messages)
            return json.dumps(invalid_result if len(self.calls) == 1 else valid_result)

    provider = SemanticRepairingBailianProvider()
    result = await provider.analyze(session, [], request)

    assert result["insights"][0]["evidence_refs"] == [segments[0]["evidence_id"]]
    assert len(provider.calls) == 2
    repair_payload = json.loads(provider.calls[1][1]["content"])
    assert any(
        error["message"] == "insight references missing evidence"
        for error in repair_payload["validation_errors"]
    )


async def test_bailian_provider_does_not_discard_aligned_text_on_partial_track_failure():
    request = {"request_id": "req_partial_track", "source": {"type": "audio"}}
    session = {
        "observations": {
            "aligned_segments": [
                {
                    "segment_id": "seg_system_1",
                    "speaker_id": "speaker_2",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "text": "系统轨存在可用文本。",
                }
            ],
            "model_errors": [
                {
                    "source": "transcription",
                    "error_code": "upstream_task_failed",
                    "track": "microphone",
                }
            ],
            "evidence_availability": {
                "has_usable_text": True,
                "aligned_segment_count": 1,
                "transcript_segment_count": 1,
                "successful_transcript_tracks": ["system"],
                "failed_transcript_tracks": ["microphone"],
            },
        }
    }
    valid_result = await MockProvider().analyze(session, ["system"], request)
    invalid_result = json.loads(json.dumps(valid_result))
    invalid_result["analysis_mode"] = "insufficient"

    class PartialFailureProvider(BailianProvider):
        def __init__(self):
            self.calls = []

        async def _chat_completion(self, messages):
            self.calls.append(messages)
            return json.dumps(invalid_result if len(self.calls) == 1 else valid_result)

    provider = PartialFailureProvider()
    result = await provider.analyze(session, ["system"], request)

    assert result["analysis_mode"] == "connected_full"
    assert len(provider.calls) == 2
    assert "model_errors 是限定到 track 的局部失败" in provider.calls[0][0]["content"]
    repair_payload = json.loads(provider.calls[1][1]["content"])
    assert any(
        error["message"]
        == "analysis_mode cannot be insufficient when aligned transcript evidence exists"
        for error in repair_payload["validation_errors"]
    )


async def test_text_only_rejects_provider_acoustic_hallucination(tmp_path):
    memory_store = MemoryStore(tmp_path)
    session = await memory_store.create_session(
        SessionCreate(
            title="No acoustic claims",
            context="work",
            occurred_at=datetime.now(UTC),
            source_mode="import",
        )
    )

    class AcousticHallucinatingProvider(MockProvider):
        async def analyze(self, session, tracks, request):
            result = await super().analyze(session, tracks, request)
            result["evidence"][0]["source_type"] = "acoustic"
            result["scope"]["signals_used"].append("acoustic")
            return result

    request = {
        "request_id": "req_no_acoustic",
        "source": {"type": "text", "text": "This sentence is the only evidence."},
    }
    job = await memory_store.create_job(session.id, request["request_id"])
    session.analysis_requests[job.id] = request
    orchestrator = Orchestrator(memory_store, AcousticHallucinatingProvider())

    await orchestrator.run(job.id, session.id, request)

    assert memory_store.jobs[job.id].status == JobStatus.failed
    assert memory_store.jobs[job.id].error_code == "AnalysisContractError"
    assert memory_store.jobs[job.id].progress < 100
    assert "text_only" in (memory_store.jobs[job.id].error_detail or "")
    assert session.result is None
