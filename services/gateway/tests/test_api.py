from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memecho_gateway.config import get_settings
from memecho_gateway.main import app


@pytest.fixture
def client(tmp_path: Path):
    settings = get_settings()
    settings.memecho_data_dir = tmp_path
    with TestClient(app) as test_client:
        yield test_client


def headers():
    return {"Authorization": "Bearer change-me"}


def test_health(client: TestClient):
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_session_analysis_is_idempotent(client: TestClient):
    created = client.post(
        "/v1/sessions",
        headers=headers(),
        json={
            "title": "路演测试",
            "context": "工作",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
        },
    ).json()
    payload = {"request_id": created["request_id"], "schema_version": "1.1"}
    first = client.post(
        f"/v1/sessions/{created['id']}/analyze", headers=headers(), json=payload
    )
    second = client.post(
        f"/v1/sessions/{created['id']}/analyze", headers=headers(), json=payload
    )
    assert first.status_code == 200
    assert first.json()["id"] == second.json()["id"]

