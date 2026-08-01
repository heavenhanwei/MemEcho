from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

_DEGRADATION_WEIGHTS: dict[str, float] = {
    "snr_db": 0.30,
    "clipping_ratio": 0.25,
    "silence_ratio": 0.20,
    "rms_db": 0.15,
    "peak_db": 0.10,
}


def compute_quality_metrics(wav_path: Path) -> dict[str, Any]:
    try:
        import soundfile as sf

        data, sr = sf.read(wav_path, dtype="float64")
    except Exception:
        log.warning("Failed to read WAV for quality metrics: %s", wav_path)
        return _empty_metrics("read_error")

    if data.ndim > 1:
        data = data.mean(axis=1)

    total_samples = len(data)
    if total_samples == 0:
        return _empty_metrics("empty_file")

    peak = float(np.max(np.abs(data)))
    rms = float(np.sqrt(np.mean(data**2)))
    peak_db = 20 * math.log10(max(peak, 1e-10))
    rms_db = 20 * math.log10(max(rms, 1e-10))

    clipping_threshold = 0.99
    clipping_ratio = float(np.mean(np.abs(data) >= clipping_threshold))

    silence_threshold = 0.01
    silence_ratio = float(np.mean(np.abs(data) < silence_threshold))

    snr_db = rms_db - 20 * math.log10(max(silence_threshold, 1e-10)) if silence_ratio < 1.0 else 0.0
    snr_db = min(snr_db, 120.0)

    raw_scores = {
        "snr_db": _normalize_snr(snr_db),
        "clipping_ratio": 1.0 - clipping_ratio,
        "silence_ratio": 1.0 - silence_ratio,
        "rms_db": _normalize_rms(rms_db),
        "peak_db": _normalize_peak(peak_db),
    }

    weighted_score = sum(
        raw_scores[k] * _DEGRADATION_WEIGHTS[k] for k in _DEGRADATION_WEIGHTS
    )

    quality_flags: list[str] = []
    if clipping_ratio > 0.01:
        quality_flags.append("clipping_detected")
    if silence_ratio > 0.5:
        quality_flags.append("excessive_silence")
    if snr_db < 20:
        quality_flags.append("low_snr")
    if rms_db < -40:
        quality_flags.append("very_quiet")

    return {
        "sample_rate": sr,
        "duration_ms": int(total_samples / sr * 1000),
        "peak_db": round(peak_db, 2),
        "rms_db": round(rms_db, 2),
        "snr_db": round(snr_db, 2),
        "clipping_ratio": round(clipping_ratio, 4),
        "silence_ratio": round(silence_ratio, 4),
        "quality_score": round(weighted_score, 4),
        "quality_flags": quality_flags,
        "degradation_weights": dict(_DEGRADATION_WEIGHTS),
    }


def _empty_metrics(reason: str) -> dict[str, Any]:
    return {
        "sample_rate": 0,
        "duration_ms": 0,
        "peak_db": 0.0,
        "rms_db": 0.0,
        "snr_db": 0.0,
        "clipping_ratio": 0.0,
        "silence_ratio": 0.0,
        "quality_score": 0.0,
        "quality_flags": [reason],
        "degradation_weights": dict(_DEGRADATION_WEIGHTS),
    }


def _normalize_snr(snr_db: float) -> float:
    return max(0.0, min(1.0, snr_db / 60.0))


def _normalize_rms(rms_db: float) -> float:
    return max(0.0, min(1.0, (rms_db + 60) / 40.0))


def _normalize_peak(peak_db: float) -> float:
    return max(0.0, min(1.0, (peak_db + 60) / 60.0))
