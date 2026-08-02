from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from memecho_gateway.main import app
from memecho_gateway.models import AnalysisRequest, AnalysisResult
from memecho_gateway.providers.mock import MockProvider


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
GENERATOR = WORKSPACE_ROOT / "services" / "gateway" / "scripts" / "generate_types.py"


def test_openapi_exposes_canonical_analysis_contracts():
    schema = app.openapi()
    analyze_schema = schema["paths"]["/v1/sessions/{session_id}/analyze"]["post"][
        "requestBody"
    ]["content"]["application/json"]["schema"]
    result_schema = schema["paths"]["/v1/sessions/{session_id}/result"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]

    assert analyze_schema == {"$ref": "#/components/schemas/AnalysisRequest"}
    assert result_schema == {"$ref": "#/components/schemas/AnalysisResult"}
    assert "AnalysisResult" in schema["components"]["schemas"]
    assert "VadPoint" in schema["components"]["schemas"]


def test_gateway_minimal_and_portable_requests_are_both_supported():
    minimal = AnalysisRequest.model_validate({"request_id": "req_minimal"})
    assert minimal.schema_version == "1.1"
    assert minimal.source is None
    assert minimal.memory.mode == "off"

    legacy = AnalysisRequest.model_validate(
        {"request_id": "req_legacy", "memory_mode": "ask"}
    )
    assert legacy.memory.mode == "ask"
    assert "memory_mode" not in legacy.model_dump()

    portable = AnalysisRequest.model_validate(
        {
            "schema_version": "1.1",
            "request_id": "req_portable",
            "source": {
                "type": "transcript",
                "text": "[00:00] 我：先确认目标。",
                "path": None,
                "mime_type": "text/plain",
            },
            "session": {
                "title": "目标确认",
                "occurred_at": None,
                "context": "meeting",
            },
            "participants": [{"id": "speaker_1", "name": "我", "is_self": True}],
            "self_identity_basis": "auto_single_speaker",
            "target_participant_ids": ["speaker_1"],
            "focus": ["minutes", "content_analysis", "vad", "self_echo"],
            "coaching": {"enabled": False, "max_scenes": 1},
            "marks": [],
            "memory": {"mode": "off", "scope": []},
        }
    )
    assert portable.source is not None
    assert portable.source.type == "transcript"

    with pytest.raises(ValidationError, match="exactly one"):
        AnalysisRequest.model_validate(
            {
                "request_id": "req_bad_source",
                "source": {"type": "text", "text": "content", "path": "input.txt"},
            }
        )


async def test_mock_result_is_validated_by_full_result_model():
    raw = await MockProvider().analyze(
        {"title": "contract"}, ["test.wav"], {"request_id": "req_contract"}
    )
    result = AnalysisResult.model_validate(raw)

    assert result.schema_version == "1.1"
    assert result.minutes.explicit_actions[0].status == "confirmed"
    assert result.vad_series[0].evidence_refs == ["ev_01"]

    raw["vad_series"][0]["v"] = 1.5
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(raw)


def test_generated_types_are_in_sync_with_openapi():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(
        WORKSPACE_ROOT / "services" / "gateway" / "src"
    )
    completed = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=WORKSPACE_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
