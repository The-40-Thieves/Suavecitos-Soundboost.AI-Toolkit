"""Shared fixtures for the CodSpeed performance benchmarks."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

SAMPLE_RATE = 48000


def _synth_mix(duration_sec: float, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Deterministic stereo test mix: a few harmonics, slight decorrelation and one sharp transient."""
    t = np.linspace(0.0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    left = (
        0.20 * np.sin(2.0 * math.pi * 110.0 * t)
        + 0.10 * np.sin(2.0 * math.pi * 440.0 * t)
        + 0.05 * np.sin(2.0 * math.pi * 3520.0 * t)
    )
    right = (
        0.20 * np.sin(2.0 * math.pi * 110.0 * t + 0.05)
        + 0.10 * np.sin(2.0 * math.pi * 440.0 * t + 0.1)
        + 0.05 * np.sin(2.0 * math.pi * 3520.0 * t + 0.2)
    )
    left[int(0.6 * left.size)] = 0.9
    return np.column_stack([left, right])


@pytest.fixture(scope="session")
def stereo_mix() -> np.ndarray:
    return _synth_mix(2.0)


@pytest.fixture(scope="session")
def stereo_wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("bench_audio") / "mix.wav"
    sf.write(str(path), _synth_mix(2.0), SAMPLE_RATE, subtype="PCM_24")
    return path


@pytest.fixture(scope="session")
def short_wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("bench_audio_short") / "short.wav"
    sf.write(str(path), _synth_mix(0.5), SAMPLE_RATE, subtype="PCM_24")
    return path
