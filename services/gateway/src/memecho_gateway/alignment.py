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
    valid_speakers = [item for item in speaker_segments if _interval(item) is not None]
    valid_emotions = [item for item in emotion_segments if _interval(item) is not None]
    skipped = (
        len(speaker_segments) - len(valid_speakers)
        + len(emotion_segments) - len(valid_emotions)
    )
    if skipped:
        log.warning("Skipped %d malformed alignment intervals", skipped)
    for ts in transcripts:
        bounds = _interval(ts)
        if bounds is None:
            log.warning("Skipped malformed transcript interval")
            continue
        t_start, t_end = bounds

        matched_speaker: dict[str, Any] | None = None
        best_overlap_s = 0.0
        for sp in valid_speakers:
            sp_start, sp_end = _interval(sp)  # type: ignore[misc]
            overlap = _overlap_ms(t_start, t_end, sp_start, sp_end)
            if overlap > best_overlap_s:
                best_overlap_s = overlap
                matched_speaker = sp

        matched_emotion: dict[str, Any] | None = None
        best_overlap_e = 0.0
        for em in valid_emotions:
            em_start, em_end = _interval(em)  # type: ignore[misc]
            overlap = _overlap_ms(t_start, t_end, em_start, em_end)
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


def _interval(item: dict[str, Any]) -> tuple[int, int] | None:
    try:
        start = int(item.get("start_ms"))
        end = int(item.get("end_ms"))
    except (TypeError, ValueError):
        return None
    if start < 0 or end <= start:
        return None
    return start, end


def _overlap_ms(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))
