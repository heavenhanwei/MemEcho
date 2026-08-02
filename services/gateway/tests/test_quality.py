from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from memecho_gateway.quality import (
    compute_quality_metrics,
    conservative_evidence_weights,
    evidence_weights_for_quality,
)


def _write_wav(path: Path, data: np.ndarray, sr: int = 16000) -> None:
    sf.write(str(path), data, sr)


def test_quality_metrics_clean_sine(tmp_path: Path):
    sr = 16000
    t = np.linspace(0, 1, sr, endpoint=False)
    data = 0.5 * np.sin(2 * np.pi * 440 * t)
    wav = tmp_path / "clean.wav"
    _write_wav(wav, data, sr)
    metrics = compute_quality_metrics(wav)
    assert metrics["sample_rate"] == sr
    assert metrics["duration_ms"] == 1000
    assert metrics["quality_score"] > 0.5
    assert "read_error" not in metrics["quality_flags"]
    assert "clipping_detected" not in metrics["quality_flags"]


def test_quality_metrics_clipping(tmp_path: Path):
    sr = 16000
    data = np.ones(sr) * 0.999
    wav = tmp_path / "clip.wav"
    _write_wav(wav, data, sr)
    metrics = compute_quality_metrics(wav)
    assert "clipping_detected" in metrics["quality_flags"]


def test_quality_metrics_excessive_silence(tmp_path: Path):
    sr = 16000
    data = np.zeros(sr)
    wav = tmp_path / "silence.wav"
    _write_wav(wav, data, sr)
    metrics = compute_quality_metrics(wav)
    assert "excessive_silence" in metrics["quality_flags"]
    assert metrics["quality_score"] < 0.5


def test_quality_metrics_low_snr(tmp_path: Path):
    sr = 16000
    t = np.linspace(0, 1, sr, endpoint=False)
    signal = 0.001 * np.sin(2 * np.pi * 440 * t)
    noise = 0.05 * np.random.randn(sr)
    data = signal + noise
    wav = tmp_path / "lowsnr.wav"
    _write_wav(wav, data, sr)
    metrics = compute_quality_metrics(wav)
    assert "low_snr" in metrics["quality_flags"]


def test_quality_metrics_very_quiet(tmp_path: Path):
    sr = 16000
    data = np.ones(sr) * 1e-6
    wav = tmp_path / "quiet.wav"
    _write_wav(wav, data, sr)
    metrics = compute_quality_metrics(wav)
    assert "very_quiet" in metrics["quality_flags"]


def test_quality_metrics_empty_file(tmp_path: Path):
    sr = 16000
    data = np.array([], dtype=np.float64)
    wav = tmp_path / "empty.wav"
    _write_wav(wav, data, sr)
    metrics = compute_quality_metrics(wav)
    assert "empty_file" in metrics["quality_flags"]
    assert metrics["quality_score"] == 0.0


def test_quality_metrics_degradation_weights_present(tmp_path: Path):
    sr = 16000
    data = np.sin(2 * np.pi * 440 * np.linspace(0, 1, sr, endpoint=False)) * 0.5
    wav = tmp_path / "weights.wav"
    _write_wav(wav, data, sr)
    metrics = compute_quality_metrics(wav)
    weights = metrics["degradation_weights"]
    assert "snr_db" in weights
    assert "clipping_ratio" in weights
    assert "silence_ratio" in weights
    assert "rms_db" in weights
    assert "peak_db" in weights
    assert abs(sum(weights.values()) - 1.0) < 0.01


def test_quality_metrics_score_range(tmp_path: Path):
    sr = 16000
    data = 0.5 * np.sin(2 * np.pi * 440 * np.linspace(0, 1, sr, endpoint=False))
    wav = tmp_path / "range.wav"
    _write_wav(wav, data, sr)
    metrics = compute_quality_metrics(wav)
    assert 0.0 <= metrics["quality_score"] <= 1.0


def test_extended_dsp_metrics_include_pitch_loudness_and_rms(tmp_path: Path):
    sr = 16000
    duration_s = 2
    time = np.linspace(0, duration_s, sr * duration_s, endpoint=False)
    data = 0.5 * np.sin(2 * np.pi * 440 * time)
    wav = tmp_path / "pitch.wav"
    _write_wav(wav, data, sr)

    metrics = compute_quality_metrics(wav)

    assert 0.35 < metrics["rms"] < 0.36
    assert -10 < metrics["loudness_lufs_approx"] < -8
    assert metrics["loudness_basis"] == "ungated_rms_approximation_not_bs1770"
    assert 435 < metrics["f0_median_hz"] < 445
    assert metrics["f0_variation_hz"] < 2
    assert metrics["f0_voiced_frame_ratio"] > 0.95
    assert metrics["evidence_weights"]["quality_tier"] == "sufficient"


def test_speech_pause_and_overlap_metrics_use_timed_segments(tmp_path: Path):
    sr = 16000
    duration_s = 4
    data = 0.3 * np.sin(
        2 * np.pi * 220 * np.linspace(0, duration_s, sr * duration_s, endpoint=False)
    )
    wav = tmp_path / "timing.wav"
    _write_wav(wav, data, sr)
    transcript = [
        {"speaker_id": "a", "start_ms": 0, "end_ms": 1000, "text": "we start"},
        {"speaker_id": "b", "start_ms": 800, "end_ms": 1800, "text": "OK"},
        {"speaker_id": "a", "start_ms": 2500, "end_ms": 3500, "text": "continue test"},
    ]

    metrics = compute_quality_metrics(
        wav, transcript_segments=transcript, speaker_segments=transcript
    )

    assert metrics["speech_rate_units_per_min"] == 107.14
    assert metrics["pause_ratio"] == 0.3
    assert metrics["pause_basis"] == "transcript_intervals"
    assert metrics["overlap_candidate_count"] == 1
    assert metrics["overlap_ratio"] == 0.05


def test_evidence_weight_tiers_are_deterministic():
    assert evidence_weights_for_quality(0.8) == {
        "quality_tier": "sufficient",
        "linguistic_weight": 0.65,
        "acoustic_weight": 0.35,
    }
    assert evidence_weights_for_quality(0.5) == {
        "quality_tier": "limited",
        "linguistic_weight": 0.8,
        "acoustic_weight": 0.2,
    }
    assert evidence_weights_for_quality(0.9, ["read_error"]) == {
        "quality_tier": "unavailable",
        "linguistic_weight": 1.0,
        "acoustic_weight": 0.0,
    }


def test_conservative_weights_use_the_weakest_track():
    selected = conservative_evidence_weights(
        [
            {"quality_score": 0.9, "quality_flags": []},
            {"quality_score": 0.5, "quality_flags": []},
        ]
    )
    assert selected["quality_tier"] == "limited"
    assert selected["linguistic_weight"] == 0.8
    assert selected["acoustic_weight"] == 0.2
    assert selected["aggregation"] == "most_conservative_track"
