from __future__ import annotations

import logging
import math
import re
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


def compute_quality_metrics(
    wav_path: Path,
    transcript_segments: list[dict[str, Any]] | None = None,
    speaker_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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

    f0_values, voiced_ratio = _estimate_f0(data, sr)
    f0_median = float(np.median(f0_values)) if f0_values else None
    f0_variation = float(np.std(f0_values)) if f0_values else None
    duration_ms = int(total_samples / sr * 1000)
    speech_rate, pause_ratio, pause_basis = _speech_timing_metrics(
        transcript_segments, duration_ms, silence_ratio
    )
    overlap_count, overlap_ratio = _overlap_metrics(speaker_segments, duration_ms)
    evidence_weights = evidence_weights_for_quality(weighted_score, quality_flags)
    loudness_lufs_approx = -0.691 + 10 * math.log10(max(rms * rms, 1e-12))

    return {
        "sample_rate": sr,
        "duration_ms": duration_ms,
        "peak_db": round(peak_db, 2),
        "rms": round(rms, 6),
        "rms_db": round(rms_db, 2),
        "loudness_lufs_approx": round(loudness_lufs_approx, 2),
        "loudness_basis": "ungated_rms_approximation_not_bs1770",
        "f0_median_hz": round(f0_median, 2) if f0_median is not None else None,
        "f0_variation_hz": round(f0_variation, 2) if f0_variation is not None else None,
        "f0_voiced_frame_ratio": round(voiced_ratio, 4),
        "speech_rate_units_per_min": speech_rate,
        "speech_unit_basis": "han_characters_plus_alphanumeric_tokens",
        "pause_ratio": pause_ratio,
        "pause_basis": pause_basis,
        "overlap_candidate_count": overlap_count,
        "overlap_ratio": overlap_ratio,
        "snr_db": round(snr_db, 2),
        "clipping_ratio": round(clipping_ratio, 4),
        "silence_ratio": round(silence_ratio, 4),
        "quality_score": round(weighted_score, 4),
        "quality_flags": quality_flags,
        "degradation_weights": dict(_DEGRADATION_WEIGHTS),
        "evidence_weights": evidence_weights,
    }


def evidence_weights_for_quality(
    quality_score: float, quality_flags: list[str] | None = None
) -> dict[str, float | str]:
    flags = set(quality_flags or [])
    unavailable = bool(flags & {"read_error", "empty_file"}) or quality_score < 0.35
    if unavailable:
        return {
            "quality_tier": "unavailable",
            "linguistic_weight": 1.0,
            "acoustic_weight": 0.0,
        }
    if quality_score < 0.7:
        return {
            "quality_tier": "limited",
            "linguistic_weight": 0.8,
            "acoustic_weight": 0.2,
        }
    return {
        "quality_tier": "sufficient",
        "linguistic_weight": 0.65,
        "acoustic_weight": 0.35,
    }


def conservative_evidence_weights(
    metrics: list[dict[str, Any]],
) -> dict[str, float | str]:
    if not metrics:
        return evidence_weights_for_quality(0.0, ["read_error"])
    ranking = {"unavailable": 0, "limited": 1, "sufficient": 2}
    weights = [
        item.get("evidence_weights")
        or evidence_weights_for_quality(
            float(item.get("quality_score", 0.0)), item.get("quality_flags", [])
        )
        for item in metrics
    ]
    selected = min(weights, key=lambda item: ranking[str(item["quality_tier"])])
    return {**selected, "aggregation": "most_conservative_track"}


def _estimate_f0(data: np.ndarray, sample_rate: int) -> tuple[list[float], float]:
    frame_size = max(1, int(sample_rate * 0.04))
    hop_size = max(1, int(sample_rate * 0.02))
    if len(data) < frame_size or sample_rate < 1000:
        return [], 0.0

    total_frames = 1 + (len(data) - frame_size) // hop_size
    stride = max(1, math.ceil(total_frames / 2000))
    min_lag = max(1, sample_rate // 500)
    max_lag = min(frame_size - 1, sample_rate // 60)
    if min_lag >= max_lag:
        return [], 0.0

    f0_values: list[float] = []
    sampled_frames = 0
    window = np.hanning(frame_size)
    for frame_number, start in enumerate(
        range(0, len(data) - frame_size + 1, hop_size)
    ):
        if frame_number % stride:
            continue
        sampled_frames += 1
        frame = data[start : start + frame_size]
        frame = (frame - np.mean(frame)) * window
        energy = float(np.sqrt(np.mean(frame**2)))
        if energy < 0.005:
            continue
        fft_size = 1 << (2 * frame_size - 1).bit_length()
        spectrum = np.fft.rfft(frame, n=fft_size)
        correlation = np.fft.irfft(spectrum * np.conj(spectrum), n=fft_size)[:frame_size]
        if correlation[0] <= 0:
            continue
        search = correlation[min_lag : max_lag + 1]
        lag = min_lag + int(np.argmax(search))
        if correlation[lag] / correlation[0] < 0.3:
            continue
        f0_values.append(sample_rate / lag)
    voiced_ratio = len(f0_values) / sampled_frames if sampled_frames else 0.0
    return f0_values, voiced_ratio


def _speech_timing_metrics(
    segments: list[dict[str, Any]] | None,
    duration_ms: int,
    silence_ratio: float,
) -> tuple[float | None, float, str]:
    if segments is None:
        return None, round(silence_ratio, 4), "amplitude_threshold_approximation"
    intervals = sorted(
        (
            max(0, int(item.get("start_ms", 0))),
            min(duration_ms, int(item.get("end_ms", 0))),
        )
        for item in segments
        if int(item.get("end_ms", 0)) > int(item.get("start_ms", 0))
    )
    merged = _merge_intervals(intervals)
    active_ms = sum(end - start for start, end in merged)
    text = " ".join(str(item.get("text", "")) for item in segments)
    units = len(re.findall(r"[\u4e00-\u9fff]", text))
    units += len(re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*", text))
    speech_rate = round(units / (active_ms / 60000), 2) if active_ms else 0.0
    pause_ratio = 1.0 - active_ms / duration_ms if duration_ms else 1.0
    return speech_rate, round(max(0.0, min(1.0, pause_ratio)), 4), "transcript_intervals"


def _overlap_metrics(
    segments: list[dict[str, Any]] | None, duration_ms: int
) -> tuple[int | None, float | None]:
    if segments is None:
        return None, None
    intervals = sorted(
        (
            int(item.get("start_ms", 0)),
            int(item.get("end_ms", 0)),
            str(item.get("speaker_id", "unknown")),
        )
        for item in segments
        if int(item.get("end_ms", 0)) > int(item.get("start_ms", 0))
    )
    active: list[tuple[int, str]] = []
    overlaps: list[tuple[int, int]] = []
    for start, end, speaker_id in intervals:
        active = [(active_end, active_speaker) for active_end, active_speaker in active if active_end > start]
        for active_end, active_speaker in active:
            if active_speaker != speaker_id:
                overlaps.append((start, min(end, active_end)))
        active.append((end, speaker_id))
    overlap_ms = sum(end - start for start, end in _merge_intervals(overlaps))
    ratio = overlap_ms / duration_ms if duration_ms else 0.0
    return len(overlaps), round(max(0.0, min(1.0, ratio)), 4)


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in intervals:
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _empty_metrics(reason: str) -> dict[str, Any]:
    return {
        "sample_rate": 0,
        "duration_ms": 0,
        "peak_db": 0.0,
        "rms": 0.0,
        "rms_db": 0.0,
        "loudness_lufs_approx": None,
        "evidence_weights": evidence_weights_for_quality(0.0, [reason]),
        "loudness_basis": "unavailable",
        "f0_median_hz": None,
        "f0_variation_hz": None,
        "f0_voiced_frame_ratio": 0.0,
        "speech_rate_units_per_min": None,
        "speech_unit_basis": "han_characters_plus_alphanumeric_tokens",
        "pause_ratio": None,
        "pause_basis": "unavailable",
        "overlap_candidate_count": None,
        "overlap_ratio": None,
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
