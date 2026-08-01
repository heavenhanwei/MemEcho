from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def align_intervals(
    transcripts: list[dict[str, Any]],
    speaker_segments: list[dict[str, Any]],
    emotion_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    aligned: list[dict[str, Any]] = []
    for ts in transcripts:
        t_start = ts["start_ms"]
        t_end = ts["end_ms"]

        matched_speaker: dict[str, Any] | None = None
        best_overlap_s = 0.0
        for sp in speaker_segments:
            overlap = _overlap_ms(t_start, t_end, sp["start_ms"], sp["end_ms"])
            if overlap > best_overlap_s:
                best_overlap_s = overlap
                matched_speaker = sp

        matched_emotion: dict[str, Any] | None = None
        best_overlap_e = 0.0
        for em in emotion_segments:
            overlap = _overlap_ms(t_start, t_end, em["start_ms"], em["end_ms"])
            if overlap > best_overlap_e:
                best_overlap_e = overlap
                matched_emotion = em

        aligned.append({
            "speaker_id": matched_speaker["speaker_id"] if matched_speaker else ts.get("speaker_id", "unknown"),
            "start_ms": t_start,
            "end_ms": t_end,
            "text": ts.get("text", ""),
            "transcript_confidence": ts.get("confidence", 0.0),
            "speaker_overlap_ms": int(best_overlap_s),
            "emotion": matched_emotion.get("emotion", "neutral") if matched_emotion else "unknown",
            "emotion_confidence": matched_emotion.get("confidence", 0.0) if matched_emotion else 0.0,
        })

    log.info("Aligned %d transcript segments", len(aligned))
    return aligned


def _overlap_ms(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))
