from __future__ import annotations

from memecho_gateway.rendering import render_html, render_markdown


MOCK_RESULT = {
    "schema_version": "1.1",
    "request_id": "req_render_test",
    "analysis_mode": "connected_full",
    "scope": {
        "quality": 0.82,
        "signals_used": ["transcript", "acoustic"],
        "target_participant_ids": ["speaker_self", "speaker_2"],
    },
    "minutes": {
        "summary": "Test summary.",
        "focus": ["scope", "timeline"],
        "consensus": ["agree on plan"],
        "disagreements": ["scope creep"],
        "explicit_actions": [
            {"text": "Do thing", "owner": "self", "due_at": None, "origin": "discussed", "status": "confirmed", "evidence_refs": ["ev_01"]}
        ],
        "recommendations": [
            {"text": "Try better", "owner": None, "due_at": None, "origin": "suggested", "status": "proposed", "evidence_refs": ["ev_02"]}
        ],
    },
    "insights": [
        {"id": "in_01", "claim": "Test claim.", "claim_level": "observed", "confidence": 0.8, "evidence_refs": ["ev_01"], "alternatives": ["alt1"]}
    ],
    "evidence": [
        {"id": "ev_01", "source_type": "transcript", "speaker_id": "speaker_self", "start_ms": 0, "end_ms": 5000, "segment_id": "seg_01", "excerpt": "Hello", "quality_flags": []},
        {"id": "ev_02", "source_type": "acoustic", "speaker_id": "speaker_2", "start_ms": 5000, "end_ms": 10000, "segment_id": "seg_02", "excerpt": "World", "quality_flags": []},
    ],
    "uncertainties": ["This is a test."],
    "provenance": {
        "skill_version": "1.0.2",
        "service_version": "0.1.0",
        "model_manifest": [{"provider": "mock", "model": "test"}],
    },
    "memory": {"written": False, "consent_basis": None},
}


def test_render_markdown_contains_sections():
    md = render_markdown(MOCK_RESULT)
    assert "# memEcho Analysis Report" in md
    assert "## Scope" in md
    assert "## Minutes" in md
    assert "## Insights" in md
    assert "## Evidence" in md
    assert "## Provenance" in md
    assert "## Uncertainties" in md


def test_render_markdown_contains_data():
    md = render_markdown(MOCK_RESULT)
    assert "req_render_test" in md
    assert "Test summary." in md
    assert "ev_01" in md
    assert "observed" in md


def test_render_html_is_valid_html():
    html_output = render_html(MOCK_RESULT)
    assert "<!DOCTYPE html>" in html_output
    assert "memEcho Analysis Report" in html_output
    assert "<pre>" in html_output


def test_render_html_escapes_content():
    result_with_html = {**MOCK_RESULT, "minutes": {"summary": "<script>alert('x')</script>"}}
    html_output = render_html(result_with_html)
    assert "<script>" not in html_output
    assert "&lt;script&gt;" in html_output
