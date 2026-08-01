from __future__ import annotations

from typing import Any


def validate_result(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
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
    for insight in data.get("insights", []):
        if insight.get("claim_level") not in {"observed", "computed", "interpreted"}:
            errors.append("insight claim_level is invalid")
        confidence = insight.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append("insight confidence must be in [0,1]")
        if any(ref not in refs for ref in insight.get("evidence_refs", [])):
            errors.append("insight references missing evidence")
    for point in data.get("vad_series", []):
        for key in ("v", "a", "d", "confidence", "linguistic_weight", "acoustic_weight"):
            value = point.get(key)
            if not isinstance(value, (int, float)):
                errors.append(f"vad point {key} must be numeric")
        if data.get("analysis_mode") == "text_only" and point.get("acoustic_weight") != 0:
            errors.append("text_only acoustic_weight must be 0")
    for item in data.get("minutes", {}).get("explicit_actions", []):
        if item.get("origin") != "discussed" or item.get("status") != "confirmed":
            errors.append("explicit action must be discussed/confirmed")
    for item in data.get("minutes", {}).get("recommendations", []):
        if item.get("origin") != "suggested" or item.get("status") != "proposed":
            errors.append("recommendation must be suggested/proposed")
    if not isinstance(data.get("memory", {}).get("written"), bool):
        errors.append("memory.written must be boolean")
    return errors

