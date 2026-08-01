from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from memecho_gateway.quality import compute_quality_metrics


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
