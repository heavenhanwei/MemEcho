from __future__ import annotations

import hashlib
import re
from typing import Any


TEXT_SOURCE_TYPES = frozenset({"text", "transcript"})
TEXT_ONLY_SIGNALS_MISSING = (
    "acoustic",
    "pitch",
    "energy",
    "speech_rate",
    "voice_quality",
)
TEXT_ONLY_UNCERTAINTY = (
    "Text-only mode cannot evaluate pitch, energy, pace, pauses, or voice quality."
)


def is_text_only_request(request: dict[str, Any]) -> bool:
    source = request.get("source")
    return isinstance(source, dict) and source.get("type") in TEXT_SOURCE_TYPES


def build_text_segments(text: str) -> list[dict[str, Any]]:
    """Build deterministic paragraph evidence without inventing a timeline."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n[ \t]*\n+", normalized)
        if paragraph.strip()
    ]
    if len(paragraphs) == 1:
        lines = [line.strip() for line in paragraphs[0].split("\n") if line.strip()]
        if len(lines) > 1:
            paragraphs = lines

    segments: list[dict[str, Any]] = []
    for index, paragraph in enumerate(paragraphs, start=1):
        digest = hashlib.sha256(
            f"{index}\0{paragraph}".encode("utf-8")
        ).hexdigest()[:10]
        segments.append(
            {
                "segment_id": f"text_seg_{index:03d}_{digest}",
                "evidence_id": f"text_ev_{index:03d}_{digest}",
                "paragraph_index": index,
                "speaker_id": None,
                "start_ms": 0,
                "end_ms": 0,
                "text": paragraph,
                "source_type": "transcript",
                "quality_flags": ["text_only", "no_audio", "no_timestamps"],
            }
        )
    return segments


def enforce_text_only_metadata(result: dict[str, Any]) -> None:
    """Force evidence weights and scope metadata; semantic evidence is validated later."""

    result["analysis_mode"] = "text_only"
    scope = result.setdefault("scope", {})
    scope["signals_used"] = ["transcript", "linguistic"]
    missing = list(scope.get("signals_missing") or [])
    for signal in TEXT_ONLY_SIGNALS_MISSING:
        if signal not in missing:
            missing.append(signal)
    scope["signals_missing"] = missing

    for point in result.get("vad_series", []):
        point["linguistic_weight"] = 1.0
        point["acoustic_weight"] = 0.0

    uncertainties = result.setdefault("uncertainties", [])
    if TEXT_ONLY_UNCERTAINTY not in uncertainties:
        uncertainties.append(TEXT_ONLY_UNCERTAINTY)
