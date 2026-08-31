"""Provider Profile (BYOK) contract, persistence, and secrecy tests.

Acceptance coverage:
- one bailian and one openai_compatible profile can be created;
- verification returns capabilities plus stable error codes;
- sessions bind a profile and never fall back to another global key;
- SQLite, logs, API JSON, and job events contain no plaintext key.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import respx
import httpx
from fastapi.testclient import TestClient

from memecho_gateway import main
from memecho_gateway.config import get_settings
from memecho_gateway.main import app
from memecho_gateway.models import JobStatus
from memecho_gateway.orchestrator import ProviderOverrides

PROFILE_SECRET = "sk-profile-secret-abc123"
OTHER_HEADER_SECRET = "sk-header-secret-xyz789"

AUTH = {"Authorization": "Bearer change-me"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "memecho_data_dir", tmp_path)
    # The store captures its paths at import time; rebind it to the tmp dir
    # and force a fresh initialize() so fixed test request_ids never collide
    # with state persisted by previous runs.
    monkeypatch.setattr(main.store, "data_dir", tmp_path)
    monkeypatch.setattr(main.store, "db_path", tmp_path / "gateway.db")
    monkeypatch.setattr(main.store, "_initialized", False)
    for state in (
        main.store.sessions,
        main.store.jobs,
        main.store.events,
        main.store.idempotency,
        main.store.profiles,
    ):
        state.clear()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def cleanup_profiles():
    created: list[str] = []
    yield created
    # Drop test profiles and any sessions bound to them from the shared store.
    for profile_id in created:
        for session_id in [
            session_id
            for session_id, session in main.store.sessions.items()
            if session.create.provider_profile_id == profile_id
        ]:
            main.store.sessions.pop(session_id, None)
        main.store.profiles.pop(profile_id, None)


def make_profile(
    client: TestClient,
    cleanup: list[str],
    *,
    provider: str = "bailian",
    credential_ref: str | None = "env:MEMECHO_PROFILE_TEST_KEY",
    **fields: Any,
) -> dict[str, Any]:
    payload = {"name": f"profile-{provider}", "provider": provider, **fields}
    if credential_ref is not None:
        payload["credential_ref"] = credential_ref
    response = client.post("/v1/provider-profiles", headers=AUTH, json=payload)
    assert response.status_code == 201, response.text
    profile = response.json()
    cleanup.append(profile["id"])
    return profile


def create_bound_session(
    client: TestClient, profile_id: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "profile session",
        "context": "work",
        "occurred_at": "2026-08-01T10:00:00+08:00",
        "source_mode": "import",
    }
    if profile_id:
        payload["provider_profile_id"] = profile_id
    response = client.post("/v1/sessions", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def db_text() -> str:
    path = Path(main.store.db_path)
    if not path.exists():
        return ""
    return path.read_bytes().decode("utf-8", errors="ignore")


# ── CRUD and contract shape ──────────────────────────────────────────────────


def test_bailian_and_openai_profiles_can_be_created(
    client: TestClient, cleanup_profiles: list[str]
):
    bailian = make_profile(
        client,
        cleanup_profiles,
        provider="bailian",
        text_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        audio_base_url="https://dashscope.aliyuncs.com",
    )
    openai = make_profile(
        client,
        cleanup_profiles,
        provider="openai_compatible",
        name="company gateway",
        text_base_url="https://llm.internal.example/v1",
        text_model="gpt-4o",
    )

    assert set(bailian["capabilities"]) == {
        "realtime_asr",
        "file_transcription",
        "diarization",
        "audio_emotion",
        "text_analysis",
    }
    assert openai["capabilities"] == ["text_analysis"]

    listed = client.get("/v1/provider-profiles", headers=AUTH).json()
    ids = {item["id"] for item in listed["profiles"]}
    assert {bailian["id"], openai["id"]} <= ids

    fetched = client.get(f"/v1/provider-profiles/{openai['id']}", headers=AUTH)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "company gateway"


def test_create_profile_rejects_plaintext_key_field(
    client: TestClient, cleanup_profiles: list[str]
):
    response = client.post(
        "/v1/provider-profiles",
        headers=AUTH,
        json={
            "name": "bad",
            "provider": "bailian",
            "api_key": PROFILE_SECRET,
        },
    )
    assert response.status_code == 422


def test_update_and_delete_profile(client: TestClient, cleanup_profiles: list[str]):
    profile = make_profile(client, cleanup_profiles, text_model="qwen-max")
    patched = client.patch(
        f"/v1/provider-profiles/{profile['id']}",
        headers=AUTH,
        json={"name": "renamed", "text_model": ""},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "renamed"
    assert patched.json()["text_model"] == ""
    # credential_ref untouched by a partial update
    assert patched.json()["credential_ref"] == profile["credential_ref"]

    deleted = client.delete(f"/v1/provider-profiles/{profile['id']}", headers=AUTH)
    assert deleted.status_code == 200
    assert client.get(f"/v1/provider-profiles/{profile['id']}", headers=AUTH).status_code == 404


def test_profile_config_file_is_editable_and_reloadable(
    client: TestClient, cleanup_profiles: list[str]
):
    profile = make_profile(
        client,
        cleanup_profiles,
        text_base_url="https://text.example/v1",
        text_model="qwen-max",
    )
    status = client.get("/v1/provider-profiles/config", headers=AUTH)
    assert status.status_code == 200
    path = Path(status.json()["path"])
    assert path == Path(main.settings.memecho_data_dir) / "provider_profiles.json"

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert PROFILE_SECRET not in path.read_text(encoding="utf-8")
    payload["profiles"][0]["name"] = "edited in json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    reloaded = client.post("/v1/provider-profiles/config/reload", headers=AUTH)
    assert reloaded.status_code == 200
    assert reloaded.json()["profiles"] == 1
    fetched = client.get(f"/v1/provider-profiles/{profile['id']}", headers=AUTH)
    assert fetched.json()["name"] == "edited in json"


def test_realtime_profile_expands_workspace_placeholder(
    client: TestClient, cleanup_profiles: list[str]
):
    profile = make_profile(
        client,
        cleanup_profiles,
        realtime_ws_url=(
            "wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
        ),
        realtime_model="qwen-audio-3.0-asr-flash-streaming",
        workspace_id="llm-workspace-123",
    )
    resolved = main.profile_registry.realtime_settings_for(
        main.store.profiles[profile["id"]], PROFILE_SECRET, get_settings()
    )

    assert resolved.bailian_realtime_ws_url == (
        "wss://llm-workspace-123.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    )
    assert resolved.bailian_workspace_id == "llm-workspace-123"


def test_invalid_profile_config_file_is_rejected_without_losing_profiles(
    client: TestClient, cleanup_profiles: list[str]
):
    profile = make_profile(client, cleanup_profiles)
    path = Path(main.settings.memecho_data_dir) / "provider_profiles.json"
    path.write_text('{"version": 1, "profiles": "invalid"}', encoding="utf-8")

    response = client.post("/v1/provider-profiles/config/reload", headers=AUTH)
    assert response.status_code == 422
    assert response.json()["detail"] == "provider_profile_config_invalid"
    assert profile["id"] in main.store.profiles


def test_capabilities_endpoint_lists_manifests(client: TestClient):
    response = client.get("/v1/capabilities", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    kinds = {item["id"]: item for item in body["provider_kinds"]}
    assert "bailian" in kinds and "openai_compatible" in kinds
    assert set(kinds["bailian"]["capabilities"]) >= {
        "realtime_asr",
        "file_transcription",
        "diarization",
        "audio_emotion",
        "text_analysis",
    }
    assert kinds["bailian"]["auth_fields"] == ["api_key"]


# ── Verification probes ──────────────────────────────────────────────────────


def test_verify_profile_reports_capabilities(
    client: TestClient, cleanup_profiles: list[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MEMECHO_PROFILE_TEST_KEY", PROFILE_SECRET)
    profile = make_profile(
        client,
        cleanup_profiles,
        text_base_url="https://text.example/v1",
        text_model="qwen-max",
        audio_base_url="https://audio.example",
    )
    with respx.mock(assert_all_called=True) as router:
        router.post("https://text.example/v1/chat/completions").respond(200, json={})
        router.get(
            "https://audio.example/api/v1/tasks/00000000-0000-0000-0000-000000000000"
        ).respond(404, json={})
        response = client.post(
            f"/v1/provider-profiles/{profile['id']}/verify", headers=AUTH
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["error_code"] is None
    statuses = {item["capability"]: item["status"] for item in body["capabilities"]}
    assert statuses == {
        "text_analysis": "ok",
        "realtime_asr": "ok",
        "file_transcription": "ok",
        "diarization": "ok",
        "audio_emotion": "ok",
    }


def test_verify_profile_returns_stable_auth_error(
    client: TestClient, cleanup_profiles: list[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MEMECHO_PROFILE_TEST_KEY", PROFILE_SECRET)
    profile = make_profile(
        client,
        cleanup_profiles,
        text_base_url="https://text.example/v1",
        audio_base_url="https://audio.example",
    )
    with respx.mock(assert_all_called=True) as router:
        router.post("https://text.example/v1/chat/completions").respond(403)
        router.get(
            "https://audio.example/api/v1/tasks/00000000-0000-0000-0000-000000000000"
        ).respond(401)
        body = client.post(
            f"/v1/provider-profiles/{profile['id']}/verify", headers=AUTH
        ).json()
    assert body["ok"] is False
    assert body["error_code"] == "provider_auth_failed"
    for probe in body["capabilities"]:
        assert probe["status"] == "failed"
        assert probe["error_code"] == "provider_auth_failed"


def test_verify_profile_without_resolvable_credential(
    client: TestClient, cleanup_profiles: list[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("MEMECHO_PROFILE_TEST_KEY", raising=False)
    profile = make_profile(client, cleanup_profiles)
    body = client.post(
        f"/v1/provider-profiles/{profile['id']}/verify", headers=AUTH
    ).json()
    assert body["ok"] is False
    assert body["error_code"] == "credential_unresolved"


def test_verify_openai_profile_marks_audio_unavailable(
    client: TestClient, cleanup_profiles: list[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MEMECHO_PROFILE_TEST_KEY", PROFILE_SECRET)
    profile = make_profile(
        client,
        cleanup_profiles,
        provider="openai_compatible",
        text_base_url="https://llm.example/v1",
        text_model="gpt-4o",
    )
    with respx.mock(assert_all_called=True) as router:
        router.post("https://llm.example/v1/chat/completions").respond(200, json={})
        body = client.post(
            f"/v1/provider-profiles/{profile['id']}/verify", headers=AUTH
        ).json()
    statuses = {item["capability"]: item["status"] for item in body["capabilities"]}
    assert statuses["text_analysis"] == "ok"
    assert statuses["realtime_asr"] == "unavailable"
    assert body["ok"] is True


# ── Session binding and provider selection ───────────────────────────────────


@pytest.fixture
def captured_runs(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, Any]] = []

    async def fake_run(job_id, session_id, request, overrides=None, provider=None):
        calls.append(
            {
                "job_id": job_id,
                "session_id": session_id,
                "overrides": overrides,
                "provider": provider,
            }
        )

    monkeypatch.setattr(main.orchestrator, "run", fake_run)
    return calls


def test_bound_session_uses_profile_key_not_header_or_env(
    client: TestClient,
    cleanup_profiles: list[str],
    captured_runs: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MEMECHO_PROFILE_TEST_KEY", PROFILE_SECRET)
    settings = get_settings()
    monkeypatch.setattr(settings, "bailian_text_api_key", "sk-global-env-secret")

    profile = make_profile(client, cleanup_profiles, text_base_url="https://text.example/v1")
    session = create_bound_session(client, profile["id"])
    assert session["provider_profile_id"] == profile["id"]

    response = client.post(
        f"/v1/sessions/{session['id']}/analyze",
        headers={**AUTH, "X-LLM-Text-Api-Key": OTHER_HEADER_SECRET},
        json={"request_id": "req_profile_bound", "schema_version": "1.1"},
    )
    assert response.status_code == 200
    assert len(captured_runs) == 1
    overrides: ProviderOverrides = captured_runs[0]["overrides"]
    assert overrides.profile_id == profile["id"]
    assert overrides.text_api_key == PROFILE_SECRET
    assert overrides.text_endpoint == "https://text.example/v1"
    # The bound profile must not fall back to the header key or the env key.
    assert overrides.text_api_key not in {OTHER_HEADER_SECRET, "sk-global-env-secret"}
    assert captured_runs[0]["provider"] is not None


def test_unbound_session_keeps_header_compatibility(
    client: TestClient,
    cleanup_profiles: list[str],
    captured_runs: list[dict[str, Any]],
):
    session = create_bound_session(client)
    assert session["provider_profile_id"] is None
    client.post(
        f"/v1/sessions/{session['id']}/analyze",
        headers={**AUTH, "X-LLM-Text-Api-Key": OTHER_HEADER_SECRET},
        json={"request_id": "req_unbound", "schema_version": "1.1"},
    )
    overrides: ProviderOverrides = captured_runs[0]["overrides"]
    assert overrides.profile_id is None
    assert overrides.text_api_key == OTHER_HEADER_SECRET
    assert captured_runs[0]["provider"] is None


def test_create_session_rejects_unknown_profile(client: TestClient):
    response = client.post(
        "/v1/sessions",
        headers=AUTH,
        json={
            "title": "bad binding",
            "occurred_at": "2026-08-01T10:00:00+08:00",
            "source_mode": "import",
            "provider_profile_id": "prof_missing",
        },
    )
    assert response.status_code == 404


def test_bound_session_with_unresolved_credential_fails_with_stable_code(
    client: TestClient,
    cleanup_profiles: list[str],
    captured_runs: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("MEMECHO_PROFILE_TEST_KEY", raising=False)
    profile = make_profile(client, cleanup_profiles)
    session = create_bound_session(client, profile["id"])
    response = client.post(
        f"/v1/sessions/{session['id']}/analyze",
        headers=AUTH,
        json={"request_id": "req_no_cred", "schema_version": "1.1"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "credential_unresolved"
    assert captured_runs == []


def test_identity_resume_keeps_profile_binding(
    client: TestClient,
    cleanup_profiles: list[str],
    captured_runs: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MEMECHO_PROFILE_TEST_KEY", PROFILE_SECRET)
    profile = make_profile(client, cleanup_profiles)
    session = create_bound_session(client, profile["id"])
    created = client.post(
        f"/v1/sessions/{session['id']}/analyze",
        headers=AUTH,
        json={"request_id": "req_resume_seed", "schema_version": "1.1"},
    ).json()

    # Simulate the orchestrator pausing for identity confirmation.
    job = main.store.jobs[created["id"]]
    main.store.jobs[created["id"]] = job.model_copy(
        update={"status": JobStatus.awaiting_identity}
    )

    response = client.post(
        f"/v1/sessions/{session['id']}/participants/resolve",
        headers=AUTH,
        json={
            "participants": [],
            "self_participant_id": None,
            "identity_basis": "unknown",
        },
    )
    assert response.status_code == 200
    assert len(captured_runs) == 2
    resume = captured_runs[1]
    assert resume["overrides"].profile_id == profile["id"]
    assert resume["overrides"].text_api_key == PROFILE_SECRET


def test_delete_profile_blocked_while_sessions_bound(
    client: TestClient, cleanup_profiles: list[str]
):
    profile = make_profile(client, cleanup_profiles)
    session = create_bound_session(client, profile["id"])
    blocked = client.delete(f"/v1/provider-profiles/{profile['id']}", headers=AUTH)
    assert blocked.status_code == 409

    # Remove the binding, then deletion succeeds.
    main.store.sessions.pop(session["id"], None)
    from memecho_gateway import persistence

    persistence.delete_session(main.store.db_path, session["id"])
    assert (
        client.delete(f"/v1/provider-profiles/{profile['id']}", headers=AUTH).status_code
        == 200
    )


# ── Plaintext key secrecy ────────────────────────────────────────────────────


def test_session_binding_persists_profile_id_not_key(
    client: TestClient,
    cleanup_profiles: list[str],
    captured_runs: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MEMECHO_PROFILE_TEST_KEY", PROFILE_SECRET)
    profile = make_profile(client, cleanup_profiles)
    session = create_bound_session(client, profile["id"])
    client.post(
        f"/v1/sessions/{session['id']}/analyze",
        headers=AUTH,
        json={"request_id": "req_persist", "schema_version": "1.1"},
    )

    with sqlite3.connect(main.store.db_path) as conn:
        row = conn.execute(
            "SELECT provider_profile_id FROM sessions WHERE id = ?",
            (session["id"],),
        ).fetchone()
    assert row is not None and row[0] == profile["id"]
    assert PROFILE_SECRET not in db_text()


def test_no_plaintext_key_in_responses_events_or_logs(
    client: TestClient,
    cleanup_profiles: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    monkeypatch.setenv("MEMECHO_PROFILE_TEST_KEY", PROFILE_SECRET)
    caplog.set_level(logging.DEBUG)

    profile = make_profile(
        client,
        cleanup_profiles,
        provider="openai_compatible",
        text_base_url="https://llm.example/v1",
        text_model="gpt-4o",
    )
    with respx.mock(assert_all_called=True) as router:
        router.post("https://llm.example/v1/chat/completions").respond(200, json={})
        verify_response = client.post(
            f"/v1/provider-profiles/{profile['id']}/verify", headers=AUTH
        )
    list_response = client.get("/v1/provider-profiles", headers=AUTH)
    get_response = client.get(f"/v1/provider-profiles/{profile['id']}", headers=AUTH)
    capabilities_response = client.get("/v1/capabilities", headers=AUTH)

    # Run a full text-only job through the real orchestrator with a bound
    # mock profile (no network), then read the SSE event stream back.
    run_profile = make_profile(
        client, cleanup_profiles, provider="mock", name="local mock"
    )
    session = create_bound_session(client, run_profile["id"])
    analyze_response = client.post(
        f"/v1/sessions/{session['id']}/analyze",
        headers=AUTH,
        json={
            "request_id": "req_secrecy",
            "schema_version": "1.1",
            "source": {"type": "text", "text": "我们先确认一下目标。"},
        },
    )
    job_id = analyze_response.json()["id"]
    events_response = client.get(f"/v1/jobs/{job_id}/events", headers=AUTH)

    for response in (
        verify_response,
        list_response,
        get_response,
        capabilities_response,
        analyze_response,
    ):
        assert PROFILE_SECRET not in response.text
        assert PROFILE_SECRET not in json.dumps(response.json(), ensure_ascii=False)

    # The events endpoint streams SSE; every data frame must be valid JSON
    # carrying no plaintext key.
    assert PROFILE_SECRET not in events_response.text
    frames = [
        line.removeprefix("data:").strip()
        for line in events_response.text.splitlines()
        if line.startswith("data:")
    ]
    assert frames, "expected at least one SSE event for the completed job"
    for frame in frames:
        parsed = json.loads(frame)
        assert PROFILE_SECRET not in json.dumps(parsed, ensure_ascii=False)

    assert PROFILE_SECRET not in caplog.text
    assert PROFILE_SECRET not in db_text()
