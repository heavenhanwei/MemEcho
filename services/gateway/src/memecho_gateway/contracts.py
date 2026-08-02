from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .models import AnalysisResult


def validate_result(
    data: dict[str, Any], *, text_segments: list[dict[str, Any]] | None = None
) -> list[str]:
    errors: list[str] = []
    try:
        AnalysisResult.model_validate(data)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False)
        ]

    if data.get("schema_version") != "1.1":
        errors.append("schema_version must be 1.1")
    if data.get("analysis_mode") not in {
        "connected_full",
        "local_enhanced",
        "text_only",
        "insufficient",
    }:
        errors.append("analysis_mode is invalid")
    evidence = data.get("evidence")
    if not isinstance(evidence, list):
        errors.append("evidence must be an array")
        evidence = []
    refs = {item.get("id") for item in evidence if isinstance(item, dict)}

    def check_refs(label: str, item: dict[str, Any]) -> None:
        evidence_refs = item.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            errors.append(f"{label} must reference evidence")
            return
        if any(ref not in refs for ref in evidence_refs):
            errors.append(f"{label} references missing evidence")

    text_only = data.get("analysis_mode") == "text_only"
    scope = data.get("scope", {})

    for insight in data.get("insights", []):
        if insight.get("claim_level") not in {"observed", "computed", "interpreted"}:
            errors.append("insight claim_level is invalid")
        confidence = insight.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append("insight confidence must be in [0,1]")
        check_refs("insight", insight)
    if text_only:
        signals_used = set(scope.get("signals_used") or [])
        signals_missing = set(scope.get("signals_missing") or [])
        if "acoustic" in signals_used:
            errors.append("text_only must not use acoustic signals")
        if "acoustic" not in signals_missing:
            errors.append("text_only signals_missing must include acoustic")
        for item in evidence:
            if isinstance(item, dict) and item.get("source_type") == "acoustic":
                errors.append("text_only must not contain acoustic evidence")

    if text_segments is not None:
        expected = {segment["evidence_id"]: segment for segment in text_segments}
        for item in evidence:
            if not isinstance(item, dict):
                continue
            segment = expected.get(item.get("id"))
            if segment is None:
                errors.append("text_only evidence id is not from the submitted text")
                continue
            if item.get("segment_id") != segment.get("segment_id"):
                errors.append("text_only evidence segment id does not match submitted text")
            if item.get("source_type") != "transcript":
                errors.append("text_only evidence source_type must be transcript")
            excerpt = item.get("excerpt")
            if not isinstance(excerpt, str) or excerpt not in segment.get("text", ""):
                errors.append("text_only evidence excerpt is not present in submitted text")

    for point in data.get("vad_series", []):
        for key in ("v", "a", "d", "confidence", "linguistic_weight", "acoustic_weight"):
            value = point.get(key)
            if not isinstance(value, (int, float)):
                errors.append(f"vad point {key} must be numeric")
        if text_only and point.get("linguistic_weight") != 1:
            errors.append("text_only linguistic_weight must be 1")
        if text_only and point.get("acoustic_weight") != 0:
            errors.append("text_only acoustic_weight must be 0")
        check_refs("vad point", point)
    for item in data.get("minutes", {}).get("explicit_actions", []):
        if item.get("origin") != "discussed" or item.get("status") != "confirmed":
            errors.append("explicit action must be discussed/confirmed")
        check_refs("explicit action", item)
    for item in data.get("minutes", {}).get("recommendations", []):
        if item.get("origin") != "suggested" or item.get("status") != "proposed":
            errors.append("recommendation must be suggested/proposed")
        check_refs("recommendation", item)
    for item in data.get("self_echo", {}).get("effects", []):
        check_refs("self_echo effect", item)
    if not isinstance(data.get("memory", {}).get("written"), bool):
        errors.append("memory.written must be boolean")
    return errors
