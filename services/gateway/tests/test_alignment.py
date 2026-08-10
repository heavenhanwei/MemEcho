from __future__ import annotations

from memecho_gateway.alignment import _overlap_ms, align_intervals


def test_overlap_ms_basic():
    assert _overlap_ms(0, 100, 50, 150) == 50
    assert _overlap_ms(0, 100, 100, 200) == 0
    assert _overlap_ms(0, 100, 0, 100) == 100
    assert _overlap_ms(0, 100, 200, 300) == 0


def test_overlap_ms_contained():
    assert _overlap_ms(10, 90, 0, 100) == 80
    assert _overlap_ms(0, 100, 20, 80) == 60


def test_align_intervals_matches_speakers():
    transcripts = [
        {"speaker_id": "unknown", "start_ms": 0, "end_ms": 5000, "text": "hello", "confidence": 0.9},
        {"speaker_id": "unknown", "start_ms": 5000, "end_ms": 10000, "text": "world", "confidence": 0.85},
    ]
    speakers = [
        {"speaker_id": "speaker_self", "start_ms": 0, "end_ms": 5000},
        {"speaker_id": "speaker_2", "start_ms": 5000, "end_ms": 10000},
    ]
    emotions = [
        {"start_ms": 0, "end_ms": 5000, "emotion": "neutral", "confidence": 0.8},
        {"start_ms": 5000, "end_ms": 10000, "emotion": "frustration", "confidence": 0.7},
    ]
    result = align_intervals(transcripts, speakers, emotions)
    assert len(result) == 2
    assert result[0]["speaker_id"] == "speaker_self"
    assert result[0]["emotion"] == "neutral"
    assert result[1]["speaker_id"] == "speaker_2"
    assert result[1]["emotion"] == "frustration"


def test_align_intervals_partial_overlap():
    transcripts = [
        {"speaker_id": "unknown", "start_ms": 0, "end_ms": 8000, "text": "a", "confidence": 0.9},
    ]
    speakers = [
        {"speaker_id": "speaker_self", "start_ms": 0, "end_ms": 5000},
        {"speaker_id": "speaker_2", "start_ms": 4000, "end_ms": 10000},
    ]
    emotions = [
        {"start_ms": 0, "end_ms": 10000, "emotion": "determination", "confidence": 0.75},
    ]
    result = align_intervals(transcripts, speakers, emotions)
    assert len(result) == 1
    assert result[0]["speaker_id"] == "speaker_self"
    assert result[0]["speaker_overlap_ms"] == 5000
    assert result[0]["emotion"] == "determination"


def test_align_intervals_no_matching_speaker():
    transcripts = [
        {"speaker_id": "unknown", "start_ms": 0, "end_ms": 5000, "text": "x", "confidence": 0.9},
    ]
    speakers: list[dict] = []
    emotions: list[dict] = []
    result = align_intervals(transcripts, speakers, emotions)
    assert len(result) == 1
    assert result[0]["speaker_id"] == "unknown"
    assert result[0]["emotion"] == "unknown"


def test_align_intervals_empty_inputs():
    result = align_intervals([], [], [])
    assert result == []


def test_align_intervals_best_overlap_wins():
    transcripts = [
        {"speaker_id": "unknown", "start_ms": 0, "end_ms": 10000, "text": "z", "confidence": 0.9},
    ]
    speakers = [
        {"speaker_id": "speaker_self", "start_ms": 0, "end_ms": 3000},
        {"speaker_id": "speaker_2", "start_ms": 0, "end_ms": 10000},
    ]
    emotions = []
    result = align_intervals(transcripts, speakers, emotions)
    assert result[0]["speaker_id"] == "speaker_2"


def test_align_intervals_skips_malformed_supplier_metadata():
    transcripts = [
        {"speaker_id": "speaker_0", "start_ms": 0, "end_ms": 1000, "text": "ok"},
        {"speaker_id": "speaker_0", "text": "missing time"},
    ]
    speakers = [
        {"transcription_url": "https://example.invalid/result.json"},
        {"speaker_id": "speaker_1", "start_ms": 0, "end_ms": 1000},
    ]
    emotions = [{"emotion": "neutral"}]

    result = align_intervals(transcripts, speakers, emotions)

    assert len(result) == 1
    assert result[0]["speaker_id"] == "speaker_1"
    assert result[0]["emotion"] == "unknown"
