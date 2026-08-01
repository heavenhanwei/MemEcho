from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from memecho_gateway.config import get_settings
from memecho_gateway.main import app


@pytest.fixture
def client(tmp_path):
    settings = get_settings()
    settings.memecho_data_dir = tmp_path
    with TestClient(app) as tc:
        yield tc


def headers():
    return {"Authorization": "Bearer change-me"}


def _create_session(client: TestClient) -> dict:
    return client.post(
        "/v1/sessions",
        headers=headers(),
        json={
            "title": "Idempotency Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
        },
    ).json()


def test_idempotent_same_request_id_returns_same_job(client: TestClient):
    session = _create_session(client)
    payload = {"request_id": session["request_id"], "schema_version": "1.1"}
    r1 = client.post(f"/v1/sessions/{session['id']}/analyze", headers=headers(), json=payload)
    r2 = client.post(f"/v1/sessions/{session['id']}/analyze", headers=headers(), json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]


def test_different_request_ids_create_different_jobs(client: TestClient):
    session = _create_session(client)
    p1 = {"request_id": "req_aaa", "schema_version": "1.1"}
    p2 = {"request_id": "req_bbb", "schema_version": "1.1"}
    r1 = client.post(f"/v1/sessions/{session['id']}/analyze", headers=headers(), json=p1)
    r2 = client.post(f"/v1/sessions/{session['id']}/analyze", headers=headers(), json=p2)
    assert r1.json()["id"] != r2.json()["id"]


def test_health_endpoint(client: TestClient):
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body
