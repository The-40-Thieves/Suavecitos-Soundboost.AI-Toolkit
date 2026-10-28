"""CodSpeed benchmarks for the local DSP engine (loudness audit, mastering, time-stretch)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from soundboost_toolkit.dsp import (
    _compute_windowed_lufs_series,
    _design_k_weighting_sos,
    _measure_true_peak_linear,
    analyze_audio_file,
    compute_loudness_range_soundboost,
    locate_true_peak_timestamp_soundboost,
    render_18band_master,
    render_local_pitch_and_time_stretch,
)

from .conftest import SAMPLE_RATE


def test_analyze_audio_file(benchmark, stereo_wav: Path) -> None:
    audit = benchmark(analyze_audio_file, stereo_wav)
    assert audit.sample_rate == SAMPLE_RATE


def test_locate_true_peak_timestamp(benchmark, stereo_mix: np.ndarray) -> None:
    dbtp, peak_time = benchmark(locate_true_peak_timestamp_soundboost, stereo_mix, SAMPLE_RATE)
    assert dbtp > -10.0
    assert peak_time > 0.0


def test_measure_true_peak_linear(benchmark, stereo_mix: np.ndarray) -> None:
    peak = benchmark(_measure_true_peak_linear, stereo_mix)
    assert peak > 0.0


@pytest.mark.parametrize("window_sec", [0.4, 3.0], ids=["momentary", "short_term"])
def test_windowed_lufs_series(benchmark, stereo_mix: np.ndarray, window_sec: float) -> None:
    from scipy import signal as sg

    k_sq = np.square(sg.sosfilt(_design_k_weighting_sos(SAMPLE_RATE), stereo_mix, axis=0))
    series = benchmark(_compute_windowed_lufs_series, k_sq, SAMPLE_RATE, window_sec, 0.1)
    assert series.size > 0


def test_loudness_range(benchmark) -> None:
    rng = np.random.default_rng(1234)
    short_term = rng.normal(-14.0, 4.0, size=6000)
    lra = benchmark(compute_loudness_range_soundboost, short_term)
    assert lra > 0.0


@pytest.mark.parametrize("preset", ["flat", "pop", "hip_hop"])
def test_render_18band_master(benchmark, stereo_wav: Path, tmp_path: Path, preset: str) -> None:
    out_wav = tmp_path / f"master_{preset}.wav"
    result = benchmark(
        render_18band_master,
        stereo_wav,
        out_wav,
        preset=preset,
        stereo_width_pct=110.0,
        target_lufs=-14.0,
        true_peak_ceiling_dbtp=-1.0,
        bit_depth=24,
    )
    assert result["post_master_audit"]["true_peak_dbtp"] <= -0.85


def test_render_pitch_and_time_stretch(benchmark, short_wav: Path, tmp_path: Path) -> None:
    out_wav = tmp_path / "shifted.wav"
    result = benchmark(
        render_local_pitch_and_time_stretch,
        input_path=short_wav,
        output_path=out_wav,
        semitones=2.0,
        speed_factor=1.25,
        bit_depth=24,
    )
    assert result["output_duration_sec"] > 0.0
