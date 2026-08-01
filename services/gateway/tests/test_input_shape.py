from __future__ import annotations

from memecho_gateway.contracts import validate_result


def test_result_with_rendered_fields_still_valid():
    result = {
        "schema_version": "1.1",
        "request_id": "req_test",
        "analysis_mode": "text_only",
        "scope": {
            "quality": 0.5,
            "signals_used": ["transcript"],
            "signals_missing": ["acoustic"],
            "quality": 0.5,
            "target_participant_ids": ["speaker_self"],
            "self_participant_id": "speaker_self",
            "self_identity_basis": "user_confirmed",
        },
        "minutes": {
            "summary": "Test.",
            "focus": [],
            "consensus": [],
            "disagreements": [],
            "explicit_actions": [],
            "recommendations": [],
        },
        "content_analysis": [],
        "participants": [{"id": "speaker_self", "name": "Me", "is_self": True}],
        "vad_series": [
            {
                "participant_id": "speaker_self",
                "segment_id": "seg_01",
                "v": 0.0,
                "a": 0.0,
                "d": 0.0,
                "scale": "-1..1",
                "confidence": 0.5,
                "linguistic_weight": 1.0,
                "acoustic_weight": 0.0,
                "evidence_refs": ["ev_01"],
            }
        ],
        "interaction_events": [],
        "self_echo": {
            "participant_id": "speaker_self",
            "identity_basis": "user_confirmed",
            "effects": [],
            "alternatives": [],
        },
        "coaching": {"enabled": False, "status": "not_requested", "scenes": []},
        "insights": [
            {
                "id": "in_01",
                "claim": "Test.",
                "claim_level": "observed",
                "confidence": 0.7,
                "evidence_refs": ["ev_01"],
                "alternatives": [],
            }
        ],
        "evidence": [
            {
                "id": "ev_01",
                "source_type": "transcript",
                "speaker_id": "speaker_self",
                "start_ms": 0,
                "end_ms": 5000,
                "segment_id": "seg_01",
                "excerpt": "text",
                "quality_flags": [],
            }
        ],
        "uncertainties": [],
        "provenance": {
            "skill_version": "1.0.2",
            "service_version": "0.1.0",
            "model_manifest": [{"provider": "test", "model": "test"}],
        },
        "memory": {"written": False, "consent_basis": None},
        "rendered_markdown": "# Report",
        "rendered_html": "<html></html>",
    }
    errors = validate_result(result)
    assert errors == []
