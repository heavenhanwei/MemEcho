from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from memecho_gateway.config import get_settings
from memecho_gateway.main import app, orchestrator, safe_filename, store
from memecho_gateway.models import JobStatus


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


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer change-me"}


def create_session(client: TestClient) -> dict:
    response = client.post(
        "/v1/sessions",
        headers=headers(),
        json={
            "title": "Gateway safety",
            "context": "work",
            "occurred_at": "2026-08-02T10:00:00+08:00",
            "source_mode": "import",
        },
    )
    assert response.status_code == 200
    return response.json()


def create_upload(client: TestClient, session_id: str, data: bytes, name: str = "audio.wav") -> dict:
    response = client.post(
        f"/v1/sessions/{session_id}/uploads",
        headers=headers(),
        json={
            "track": "import",
            "file_name": name,
            "mime_type": "audio/wav",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )
    assert response.status_code == 200
    return response.json()


def test_windows_filename_is_a_safe_leaf():
    assert safe_filename(r"C:\temp\CON.txt") == "_CON.txt"
    assert safe_filename("../..") == "upload"
    assert safe_filename("name. ") == "name"
    assert "/" not in safe_filename("folder/file.wav")
    assert "\\" not in safe_filename(r"folder\file.wav")


def test_chunk_boundaries_duplicates_and_complete_are_strict(client: TestClient):
    data = b"abcde"
    session = create_session(client)
    upload = create_upload(client, session["id"], data, r"C:\unsafe\CON.wav")
    base = f"/v1/sessions/{session['id']}/uploads/{upload['upload_id']}"

    assert client.put(f"{base}/chunks/-1", headers=headers(), content=b"a").status_code == 422
    assert client.put(f"{base}/chunks/0", headers=headers(), content=b"").status_code == 422
    assert client.put(f"{base}/chunks/0", headers=headers(), content=b"abcde").status_code == 413
    assert client.put(f"{base}/chunks/2", headers=headers(), content=b"x").status_code == 422

    assert client.put(f"{base}/chunks/0", headers=headers(), content=b"abcd").status_code == 200
    assert client.put(f"{base}/chunks/0", headers=headers(), content=b"abcd").status_code == 200
    assert client.put(f"{base}/chunks/0", headers=headers(), content=b"wxyz").status_code == 409
    assert client.post(
        f"{base}/complete",
        headers=headers(),
        json={"upload_id": upload["upload_id"], "sha256": hashlib.sha256(data).hexdigest()},
    ).status_code == 409

    assert client.put(f"{base}/chunks/1", headers=headers(), content=b"e").status_code == 200
    assert client.post(
        f"{base}/complete",
        headers=headers(),
        json={"upload_id": "upl_wrong", "sha256": hashlib.sha256(data).hexdigest()},
    ).status_code == 422
    assert client.post(
        f"{base}/complete",
        headers=headers(),
        json={"upload_id": upload["upload_id"], "sha256": "0" * 64},
    ).status_code == 422

    completion = client.post(
        f"{base}/complete",
        headers=headers(),
        json={"upload_id": upload["upload_id"], "sha256": hashlib.sha256(data).hexdigest()},
    )
    repeated = client.post(
        f"{base}/complete",
        headers=headers(),
        json={"upload_id": upload["upload_id"], "sha256": hashlib.sha256(data).hexdigest()},
    )
    assert completion.status_code == repeated.status_code == 200
    assert completion.json()["sha256"] == hashlib.sha256(data).hexdigest()
    assert repeated.json()["sha256"] == completion.json()["sha256"]
    record = store.sessions[session["id"]].uploads[upload["upload_id"]]
    assert record.file_name == "_CON.wav"


def test_complete_rejects_invalid_sha_shape(client: TestClient):
    session = create_session(client)
    upload = create_upload(client, session["id"], b"a")
    response = client.post(
        f"/v1/sessions/{session['id']}/uploads/{upload['upload_id']}/complete",
        headers=headers(),
        json={"upload_id": upload["upload_id"], "sha256": "not-a-digest"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_candidates_and_resume_use_same_job_state(client: TestClient, monkeypatch):
    session_payload = create_session(client)
    session = store.sessions[session_payload["id"]]
    job = await store.create_job(session.id, "req_original")
    store.jobs[job.id] = job.model_copy(update={"status": JobStatus.awaiting_identity})
    original = {"request_id": "req_original", "focus": ["vad"]}
    session.analysis_requests[job.id] = original
    session.job_intermediates[job.id] = {
        "aligned": [
            {"speaker_id": "speaker_1", "start_ms": 100, "end_ms": 700},
            {"speaker_id": "speaker_1", "start_ms": 900, "end_ms": 1200},
            {"speaker_id": "speaker_2", "start_ms": 1300, "end_ms": 1500},
        ]
    }

    candidates = client.get(
        f"/v1/sessions/{session.id}/participants/candidates", headers=headers()
    )
    assert candidates.status_code == 200
    assert candidates.json()["candidates"] == [
        {
            "participant_id": "speaker_1",
            "display_name": "Speaker speaker_1",
            "source": "diarization",
            "speaking_time_ms": 900,
            "segment_count": 2,
        },
        {
            "participant_id": "speaker_2",
            "display_name": "Speaker speaker_2",
            "source": "diarization",
            "speaking_time_ms": 200,
            "segment_count": 1,
        },
    ]

    calls: list[tuple[str, str, dict]] = []

    async def fake_run(job_id: str, session_id: str, request: dict):
        calls.append((job_id, session_id, request))

    monkeypatch.setattr(orchestrator, "run", fake_run)
    resolution = {
        "participants": [{"id": "speaker_1", "name": "Me"}],
        "self_participant_id": "speaker_1",
        "identity_basis": "user_confirmed",
    }
    first = client.post(
        f"/v1/sessions/{session.id}/participants/resolve",
        headers=headers(),
        json=resolution,
    )
    second = client.post(
        f"/v1/sessions/{session.id}/participants/resolve",
        headers=headers(),
        json=resolution,
    )
    assert first.status_code == second.status_code == 200
    assert calls == [(job.id, session.id, original)]
    assert job.id in session.resume_scheduled_jobs


def test_analyze_preserves_first_request_for_idempotent_job(client: TestClient, monkeypatch):
    session_payload = create_session(client)

    async def fake_run(job_id: str, session_id: str, request: dict):
        return None

    monkeypatch.setattr(orchestrator, "run", fake_run)
    first_payload = {
        "request_id": session_payload["request_id"],
        "schema_version": "1.1",
        "focus": ["minutes"],
    }
    second_payload = {**first_payload, "focus": ["vad"]}
    first = client.post(
        f"/v1/sessions/{session_payload['id']}/analyze",
        headers=headers(),
        json=first_payload,
    )
    second = client.post(
        f"/v1/sessions/{session_payload['id']}/analyze",
        headers=headers(),
        json=second_payload,
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    session = store.sessions[session_payload["id"]]
    assert session.analysis_requests[first.json()["id"]]["focus"] == ["minutes"]


def test_artifact_json_excludes_rendered_fields(client: TestClient):
    session_payload = create_session(client)
    session = store.sessions[session_payload["id"]]
    session.result = {
        "schema_version": "1.1",
        "summary": {"title": "result"},
        "rendered_markdown": "# report",
        "rendered_html": "<h1>report</h1>",
    }
    response = client.get(
        f"/v1/sessions/{session.id}/artifacts", headers=headers()
    )
    assert response.status_code == 200
    exported = json.loads(response.json()["contents"]["json"])
    assert exported == {"schema_version": "1.1", "summary": {"title": "result"}}
    assert response.json()["contents"]["markdown"] == "# report"
    assert response.json()["contents"]["html"] == "<h1>report</h1>"

def test_session_rejects_unknown_fields(client: TestClient):
    response = client.post(
        "/v1/sessions",
        headers=headers(),
        json={
            "title": "strict input",
            "context": "work",
            "occurred_at": "2026-08-02T10:00:00+08:00",
            "source_mode": "import",
            "unexpected": "must be rejected",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


def test_analyze_rejects_unknown_fields(client: TestClient):
    session = create_session(client)
    response = client.post(
        f"/v1/sessions/{session['id']}/analyze",
        headers=headers(),
        json={
            "request_id": session["request_id"],
            "schema_version": "1.1",
            "unexpected": True,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"
