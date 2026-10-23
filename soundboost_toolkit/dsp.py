"""Local Studio DSP, ITU-R BS.1770-4 Loudness, 18-Band Mastering EQ & Musical Theory Engine.

Implements:
- ITU-R BS.1770-4 / EBU R128 K-weighted Integrated, Short-Term, and Momentary LUFS
- SoundBoost `computeLoudnessRange` (10th to 95th percentile of gated short-term LUFS)
- 4x Oversampled Inter-Sample True Peak (dBTP) + 100ms-precision Binary-Search Peak Locator
- Streaming Loudness Penalty & DistroKid Release Readiness Audit
- 18-Band Mastering Graphic EQ (20 Hz - 15 kHz) + Mid/Side Width + True-Peak Limiter + TPDF Dither
- Studio Chord Transposition, 30ms Beat-Snapping, Bar.Beat.1/16 Loop Quantizer & Lead Sheet Renderer
"""

from __future__ import annotations

import bisect
import html
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import scipy.signal as sg
import soundfile as sf

from soundboost_toolkit.constants import (
    BEAT_SNAP_TOLERANCE_SEC,
    CHORD_GAP_MIN_SECONDS,
    CHORD_QUALITY_INTERVALS,
    CHORD_ROOTS_FLAT,
    CHORD_ROOTS_SHARP,
    GUITAR_CHORD_SHAPES,
    LAME_DEFAULT_ENCODER_DELAY_SAMPLES,
    LAME_TOTAL_PRIMING_SAMPLES,
    MASTERING_EQ_FREQUENCIES_HZ,
    MASTERING_EQ_MAX_DB,
    MASTERING_EQ_MIN_DB,
    MP3_DEFAULT_DECODER_DELAY_SAMPLES,
    MP3_SAMPLES_PER_FRAME_MPEG1,
    MP3_SAMPLES_PER_FRAME_MPEG2,
    PITCH_NATURAL_ZONE_MAX_SEMITONES,
    PRESET_EQ_CURVES_18BAND,
    ROOT_TO_INDEX,
    STREAMING_TARGETS,
    UKULELE_CHORD_SHAPES,
)


def _validate_audio_path(file_path: str | Path) -> Path:
    resolved = Path(file_path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(f"Audio file not found: {resolved}")
    return resolved


# ---------------------------------------------------------------------------
# ITU-R BS.1770-4 K-Weighting Filter Design (Any Sample Rate)
# ---------------------------------------------------------------------------
def _design_k_weighting_sos(sample_rate: int) -> np.ndarray:
    """Design the 2-stage ITU-R BS.1770-4 K-weighting filter cascade for `sample_rate`."""
    # Stage 1: High-shelf pre-filter (acoustic head model: +4 dB above ~1.5 kHz)
    f0_shelf = 1681.974450955533
    g_shelf_db = 3.999843853973347
    q_shelf = 0.7071752369554196

    k_s = math.tan(math.pi * f0_shelf / sample_rate)
    vh = math.pow(10.0, g_shelf_db / 20.0)
    vb = math.pow(vh, 0.4996667741545416)
    a0_s = 1.0 + k_s / q_shelf + k_s * k_s
    b0_s = (vh + vb * k_s / q_shelf + k_s * k_s) / a0_s
    b1_s = 2.0 * (k_s * k_s - vh) / a0_s
    b2_s = (vh - vb * k_s / q_shelf + k_s * k_s) / a0_s
    a1_s = 2.0 * (k_s * k_s - 1.0) / a0_s
    a2_s = (1.0 - k_s / q_shelf + k_s * k_s) / a0_s

    # Stage 2: RLB High-pass filter (~38 Hz)
    f0_hp = 38.13547087602444
    q_hp = 0.5003270373238773
    k_h = math.tan(math.pi * f0_hp / sample_rate)
    a0_h = 1.0 + k_h / q_hp + k_h * k_h
    b0_h = 1.0 / a0_h
    b1_h = -2.0 / a0_h
    b2_h = 1.0 / a0_h
    a1_h = 2.0 * (k_h * k_h - 1.0) / a0_h
    a2_h = (1.0 - k_h / q_hp + k_h * k_h) / a0_h

    return np.array(
        [
            [b0_s, b1_s, b2_s, 1.0, a1_s, a2_s],
            [b0_h, b1_h, b2_h, 1.0, a1_h, a2_h],
        ],
        dtype=np.float64,
    )


def _bs1770_channel_weights(n_channels: int) -> np.ndarray:
    """ITU-R BS.1770-4 channel summing weights.

    L, R, and C weight 1.0; the two surround channels (Ls, Rs) weight 1.41
    (~+1.5 dB). LFE is excluded (weight 0.0). For mono and stereo this is all
    1.0, so those measurements are unaffected.

    Weights follow the conventional interleaved channel order L, R, C, LFE,
    Ls, Rs, so LFE (index 3) is dropped and the surrounds sit at indices 4-5.
    """
    base = [1.0, 1.0, 1.0, 0.0, 1.41, 1.41]
    if n_channels <= 2:
        return np.ones(n_channels, dtype=np.float64)
    weights = [base[i] if i < len(base) else 1.0 for i in range(n_channels)]
    return np.asarray(weights, dtype=np.float64)


def _compute_windowed_lufs_series(
    k_weighted_sq: np.ndarray,
    sample_rate: int,
    window_sec: float,
    step_sec: float,
) -> np.ndarray:
    """Compute sliding-window mean-square loudness series in LUFS."""
    win_samples = max(1, round(window_sec * sample_rate))
    step_samples = max(1, round(step_sec * sample_rate))
    total_samples = k_weighted_sq.shape[0]
    channel_weights = _bs1770_channel_weights(k_weighted_sq.shape[1])
    if total_samples < win_samples:
        mean_energy = float(np.sum(np.mean(k_weighted_sq, axis=0) * channel_weights))
        if mean_energy <= 1e-12:
            return np.array([-70.0], dtype=np.float64)
        return np.array([-0.691 + 10.0 * math.log10(mean_energy)], dtype=np.float64)

    cumsum = np.vstack([np.zeros((1, k_weighted_sq.shape[1])), np.cumsum(k_weighted_sq, axis=0)])
    starts = np.arange(0, total_samples - win_samples + 1, step_samples)
    ends = starts + win_samples
    window_means = (cumsum[ends] - cumsum[starts]) / float(win_samples)
    summed_energy = np.sum(window_means * channel_weights, axis=1)
    with np.errstate(divide="ignore"):
        lufs = np.where(summed_energy > 1e-12, -0.691 + 10.0 * np.log10(np.maximum(summed_energy, 1e-12)), -100.0)
    return lufs


def compute_loudness_range_soundboost(short_term_lufs: np.ndarray) -> float:
    """Exact replica of SoundBoost's `computeLoudnessRange` (lufs-meter-819e4dcbf9c0865b.js).

    Filters short-term LUFS >= -70.0, sorts ascending, and returns P95 - P10.
    """
    valid = np.sort(short_term_lufs[np.isfinite(short_term_lufs) & (short_term_lufs >= -70.0)])
    if valid.size < 2:
        return 0.0
    p10_idx = math.floor(0.10 * valid.size)
    p95_idx = min(valid.size - 1, math.floor(0.95 * valid.size))
    return max(0.0, float(valid[p95_idx] - valid[p10_idx]))


def _measure_true_peak_linear(samples: np.ndarray) -> float:
    """Measure 4x polyphase oversampled inter-sample True Peak (linear amplitude)."""
    if samples.size == 0:
        return 0.0
    sample_peak = float(np.max(np.abs(samples)))
    if samples.shape[0] < 32:
        return sample_peak
    oversampled = sg.resample_poly(samples.astype(np.float32), up=4, down=1, axis=0)
    return max(sample_peak, float(np.max(np.abs(oversampled))))


def locate_true_peak_timestamp_soundboost(
    samples: np.ndarray,
    sample_rate: int,
) -> tuple[float, float]:
    """Exact replica of SoundBoost's 100ms-precision Binary-Search True-Peak Locator.

    Bisects `[left, right]` while `(right - left) > 0.1 * sample_rate` with a `50ms`
    filter-ringing overlap (`overlap = ceil(0.05 * sample_rate)`), returning
    `(true_peak_dbtp, true_peak_time_sec)`.
    """
    total_frames = samples.shape[0]
    if total_frames == 0:
        return -100.0, 0.0

    overall_tp_lin = _measure_true_peak_linear(samples)
    overall_dbtp = 20.0 * math.log10(max(overall_tp_lin, 1e-12))

    left = 0
    right = total_frames
    overlap = math.ceil(0.05 * sample_rate)
    min_span = round(0.10 * sample_rate)

    while (right - left) > min_span:
        mid = (left + right) // 2
        left_end = min(right, mid + overlap)
        right_start = max(left, mid - overlap)

        tp_left = _measure_true_peak_linear(samples[left:left_end])
        tp_right = _measure_true_peak_linear(samples[right_start:right])

        if tp_left >= tp_right:
            right = mid
        else:
            left = mid

    peak_time_sec = (left + right) / (2.0 * float(sample_rate))
    return float(overall_dbtp), float(peak_time_sec)


@dataclass(frozen=True, slots=True)
class LoudnessAuditResult:
    file_path: str
    sample_rate: int
    channels: int
    bit_depth_subtype: str
    duration_sec: float
    integrated_lufs: float
    short_term_max_lufs: float
    momentary_max_lufs: float
    loudness_range_lu: float
    sample_peak_dbfs: float
    true_peak_dbtp: float
    true_peak_time_sec: float
    psr_db: float
    dc_offset: float
    stereo_correlation: float
    mid_side_ratio_db: float
    estimated_bandwidth_hz: float
    tonal_profile_db: dict[str, float]
    streaming_penalties: dict[str, dict[str, Any]]
    release_ready_heuristic: bool
    mix_doctor_recommendations: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyze_audio_file(file_path: str | Path) -> LoudnessAuditResult:
    """Run full ITU-R BS.1770-4 Loudness, 100ms True-Peak, Mix Doctor & DistroKid Audit."""
    path = _validate_audio_path(file_path)
    info = sf.info(str(path))
    data, sr = sf.read(str(path), always_2d=True, dtype="float64")
    n_samples, n_channels = data.shape
    duration_sec = n_samples / float(sr) if sr > 0 else 0.0

    # K-weighted loudness (ITU-R BS.1770-4)
    sos = _design_k_weighting_sos(sr)
    k_filtered = sg.sosfilt(sos, data, axis=0)
    k_sq = np.square(k_filtered)

    # 400ms blocks (75% overlap -> 100ms step) for Integrated & Momentary LUFS
    momentary_lufs = _compute_windowed_lufs_series(k_sq, sr, window_sec=0.400, step_sec=0.100)
    # 3.0s blocks (100ms step) for Short-Term LUFS & LRA
    short_term_lufs = _compute_windowed_lufs_series(k_sq, sr, window_sec=3.000, step_sec=0.100)

    # Absolute gate (-70 LUFS) and relative gate (-10 LU)
    block_energies = np.power(10.0, (momentary_lufs + 0.691) / 10.0)
    abs_mask = momentary_lufs > -70.0
    if np.any(abs_mask):
        abs_mean = float(np.mean(block_energies[abs_mask]))
        rel_gate_lufs = -0.691 + 10.0 * math.log10(max(abs_mean, 1e-12)) - 10.0
        rel_mask = momentary_lufs > rel_gate_lufs
        if np.any(rel_mask):
            gated_mean = float(np.mean(block_energies[rel_mask]))
            integrated_lufs = -0.691 + 10.0 * math.log10(max(gated_mean, 1e-12))
        else:
            integrated_lufs = -0.691 + 10.0 * math.log10(max(abs_mean, 1e-12))
    else:
        integrated_lufs = -70.0

    momentary_max = float(np.max(momentary_lufs)) if momentary_lufs.size > 0 else -70.0
    short_term_max = float(np.max(short_term_lufs)) if short_term_lufs.size > 0 else -70.0
    lra_lu = compute_loudness_range_soundboost(short_term_lufs)

    # Sample peak & 4x oversampled True Peak with 100ms binary-search timestamp
    sample_peak_lin = float(np.max(np.abs(data))) if data.size > 0 else 0.0
    sample_peak_dbfs = 20.0 * math.log10(max(sample_peak_lin, 1e-12))
    true_peak_dbtp, true_peak_time_sec = locate_true_peak_timestamp_soundboost(data, sr)

    # PSR = Peak-to-Short-term-loudness Ratio (true-peak dBTP minus short-term LUFS).
    # This intentionally mixes the dBTP and LUFS scales; it is not crest factor.
    psr_db = float(true_peak_dbtp - short_term_max)
    dc_offset = float(np.max(np.abs(np.mean(data, axis=0))))

    # Stereo correlation & Mid/Side ratio
    if n_channels >= 2:
        left_ch = data[:, 0]
        right_ch = data[:, 1]
        denom = math.sqrt(float(np.sum(left_ch**2) * np.sum(right_ch**2)))
        stereo_corr = float(np.sum(left_ch * right_ch) / denom) if denom > 1e-12 else 1.0
        mid = 0.5 * (left_ch + right_ch)
        side = 0.5 * (left_ch - right_ch)
        mid_rms = math.sqrt(float(np.mean(mid**2)) + 1e-12)
        side_rms = math.sqrt(float(np.mean(side**2)) + 1e-12)
        mid_side_ratio_db = 20.0 * math.log10(side_rms / mid_rms)
    else:
        stereo_corr = 1.0
        mid_side_ratio_db = -100.0

    # Spectral profile & bandwidth estimation via Welch PSD
    mono = np.mean(data, axis=1)
    nperseg = min(8192, max(256, n_samples))
    freqs, psd = sg.welch(mono, fs=sr, nperseg=nperseg)
    psd_db = 10.0 * np.log10(np.maximum(psd, 1e-15))

    bands = {
        "sub_20_60hz": (20.0, 60.0),
        "bass_60_250hz": (60.0, 250.0),
        "low_mid_250_500hz": (250.0, 500.0),
        "mid_500_2000hz": (500.0, 2000.0),
        "upper_mid_2k_6khz": (2000.0, 6000.0),
        "air_6k_20khz": (6000.0, min(20000.0, sr / 2.0)),
    }
    tonal_profile_db: dict[str, float] = {}
    for band_name, (f_lo, f_hi) in bands.items():
        mask = (freqs >= f_lo) & (freqs < f_hi)
        if np.any(mask):
            tonal_profile_db[band_name] = round(float(10.0 * math.log10(np.mean(np.maximum(psd[mask], 1e-15)))), 2)
        else:
            tonal_profile_db[band_name] = -100.0

    # Bandwidth roll-off (-60 dB below peak PSD)
    peak_psd_db = float(np.max(psd_db))
    active_bins = freqs[psd_db >= (peak_psd_db - 60.0)]
    estimated_bw_hz = float(np.max(active_bins)) if active_bins.size > 0 else 0.0

    # Streaming Loudness Penalties
    penalties: dict[str, dict[str, Any]] = {}
    for key, spec in STREAMING_TARGETS.items():
        raw_delta = spec.target_lufs - integrated_lufs
        # Spotify/Tidal/YouTube only turn down loud tracks by default, or cap positive boost by True Peak
        if raw_delta < 0:
            applied_gain_db = round(raw_delta, 2)
        else:
            headroom_to_ceiling = spec.true_peak_ceiling_dbtp - true_peak_dbtp
            applied_gain_db = round(max(0.0, min(raw_delta, headroom_to_ceiling)), 2)
        penalties[key] = {
            "platform": spec.platform,
            "target_lufs": spec.target_lufs,
            "ceiling_dbtp": spec.true_peak_ceiling_dbtp,
            "gain_change_db": applied_gain_db,
            "true_peak_exceeds_ceiling": bool(true_peak_dbtp > spec.true_peak_ceiling_dbtp),
        }

    # Mix Doctor & DistroKid Recommendations
    recs: list[str] = []
    if true_peak_dbtp > -1.0:
        recs.append(
            f"True Peak is {true_peak_dbtp:.2f} dBTP (at {true_peak_time_sec:.2f}s), exceeding the -1.0 dBTP streaming ceiling. Set brickwall limiter ceiling to -1.0 dBTP."
        )
    if integrated_lufs > -7.5:
        recs.append(
            f"Integrated loudness is very hot ({integrated_lufs:.2f} LUFS); streaming services will attenuate by { -14.0 - integrated_lufs:.1f} dB."
        )
    elif integrated_lufs < -16.5:
        recs.append(
            f"Integrated loudness is quiet ({integrated_lufs:.2f} LUFS); consider targeting -14.0 to -9.0 LUFS for commercial release."
        )
    if stereo_corr < 0.2:
        recs.append(
            f"Low stereo phase correlation ({stereo_corr:.2f}); check for mono-cancellation issues on club/phone speakers."
        )
    if dc_offset > 0.001:
        recs.append(f"Non-zero DC offset detected ({dc_offset:.5f}); apply a 20 Hz high-pass filter.")
    if estimated_bw_hz < 16500.0 and sr >= 44100:
        recs.append(
            f"High-frequency roll-off detected around {estimated_bw_hz:.0f} Hz; consider Audio Enhancer or gentle 10-15 kHz air shelf."
        )
    if not recs:
        recs.append("Track passes all loudness, True-Peak (-1.0 dBTP), phase correlation, and spectral balance checks.")

    # Release-readiness is the toolkit's OWN heuristic, not a DistroKid requirement.
    # DistroKid's actual intake spec is only lossless WAV/FLAC, 16-bit+, 44.1 kHz+,
    # with no loudness or true-peak gate. These thresholds are our mastering guidance.
    release_ready_heuristic = bool(
        sr in (44100, 48000, 88200, 96000)
        and true_peak_dbtp <= -0.8
        and -18.0 <= integrated_lufs <= -6.0
        and stereo_corr >= 0.0
    )

    return LoudnessAuditResult(
        file_path=str(path),
        sample_rate=sr,
        channels=n_channels,
        bit_depth_subtype=info.subtype,
        duration_sec=round(duration_sec, 3),
        integrated_lufs=round(integrated_lufs, 2),
        short_term_max_lufs=round(short_term_max, 2),
        momentary_max_lufs=round(momentary_max, 2),
        loudness_range_lu=round(lra_lu, 2),
        sample_peak_dbfs=round(sample_peak_dbfs, 2),
        true_peak_dbtp=round(true_peak_dbtp, 2),
        true_peak_time_sec=round(true_peak_time_sec, 3),
        psr_db=round(psr_db, 2),
        dc_offset=round(dc_offset, 6),
        stereo_correlation=round(stereo_corr, 3),
        mid_side_ratio_db=round(mid_side_ratio_db, 2),
        estimated_bandwidth_hz=round(estimated_bw_hz, 1),
        tonal_profile_db=tonal_profile_db,
        streaming_penalties=penalties,
        release_ready_heuristic=release_ready_heuristic,
        mix_doctor_recommendations=recs,
    )


# ---------------------------------------------------------------------------
# 18-Band Mastering Graphic EQ + Mid/Side Width + True-Peak Limiter + Dither
# ---------------------------------------------------------------------------
def _design_peaking_biquad(freq_hz: float, gain_db: float, q: float, sample_rate: int) -> np.ndarray:
    """RBJ Audio-EQ-Cookbook peaking biquad filter section."""
    if abs(gain_db) < 1e-4 or freq_hz >= 0.49 * sample_rate:
        return np.array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0], dtype=np.float64)
    a = math.pow(10.0, gain_db / 40.0)
    w0 = 2.0 * math.pi * freq_hz / float(sample_rate)
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)

    b0 = 1.0 + alpha * a
    b1 = -2.0 * cos_w0
    b2 = 1.0 - alpha * a
    a0 = 1.0 + alpha / a
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha / a
    return np.array([b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0], dtype=np.float64)


def render_18band_master(
    input_path: str | Path,
    output_path: str | Path,
    eq_gains_db: list[float] | tuple[float, ...] | None = None,
    preset: str | None = None,
    stereo_width_pct: float = 100.0,
    target_lufs: float | None = -14.0,
    true_peak_ceiling_dbtp: float = -1.0,
    bit_depth: int = 24,
    apply_tpdf_dither: bool = True,
) -> dict[str, Any]:
    """Apply SoundBoost's 18-Band Mastering EQ, Mid/Side Stereo Width, LUFS Gain, True-Peak Limiter & TPDF Dither."""
    in_file = _validate_audio_path(input_path)
    out_file = Path(output_path).expanduser().resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    if eq_gains_db is not None:
        if len(eq_gains_db) != 18:
            raise ValueError(f"Expected exactly 18 EQ band gains, got {len(eq_gains_db)}")
        gains = tuple(
            max(MASTERING_EQ_MIN_DB, min(MASTERING_EQ_MAX_DB, float(g))) for g in eq_gains_db
        )
    elif preset is not None:
        key = preset.strip().lower()
        if key not in PRESET_EQ_CURVES_18BAND:
            raise ValueError(f"Unknown preset '{preset}'. Available: {sorted(PRESET_EQ_CURVES_18BAND)}")
        gains = PRESET_EQ_CURVES_18BAND[key]
    else:
        gains = PRESET_EQ_CURVES_18BAND["flat"]

    samples, sr = sf.read(str(in_file), always_2d=True, dtype="float64")

    # 1. Remove sub-sonic DC offset (18 Hz 2nd-order Butterworth high-pass)
    hp_sos = sg.butter(2, 18.0 / (0.5 * sr), btype="highpass", output="sos")
    processed = sg.sosfilt(hp_sos, samples, axis=0)

    # 2. Apply 18-Band Constant-Q Mastering EQ (Q = 2.2 for ~2/3-octave spacing)
    sos_sections = [
        _design_peaking_biquad(float(freq), float(gain), q=2.2, sample_rate=sr)
        for freq, gain in zip(MASTERING_EQ_FREQUENCIES_HZ, gains, strict=True)
        if abs(gain) >= 1e-3
    ]
    if sos_sections:
        eq_sos = np.vstack(sos_sections)
        processed = sg.sosfilt(eq_sos, processed, axis=0)

    # 3. Apply Mid/Side Stereo Width (0% = Mono, 100% = Unity, 200% = Wide)
    if processed.shape[1] >= 2 and abs(stereo_width_pct - 100.0) > 1e-2:
        width_factor = max(0.0, min(2.0, stereo_width_pct / 100.0))
        mid = 0.5 * (processed[:, 0] + processed[:, 1])
        side = 0.5 * (processed[:, 0] - processed[:, 1]) * width_factor
        processed[:, 0] = mid + side
        processed[:, 1] = mid - side

    # 4. Target LUFS Loudness Normalization
    if target_lufs is not None:
        k_sos = _design_k_weighting_sos(sr)
        k_sq = np.square(sg.sosfilt(k_sos, processed, axis=0))
        mom = _compute_windowed_lufs_series(k_sq, sr, 0.400, 0.100)
        energies = np.power(10.0, (mom + 0.691) / 10.0)
        mask = mom > -70.0
        if np.any(mask):
            abs_m = float(np.mean(energies[mask]))
            rel_gate = -0.691 + 10.0 * math.log10(max(abs_m, 1e-12)) - 10.0
            rel_m = mom > rel_gate
            curr_lufs = -0.691 + 10.0 * math.log10(
                max(float(np.mean(energies[rel_m])) if np.any(rel_m) else abs_m, 1e-12)
            )
            gain_delta_db = max(-18.0, min(18.0, float(target_lufs) - curr_lufs))
            processed *= math.pow(10.0, gain_delta_db / 20.0)

    # 5. Soft-Knee Lookahead True-Peak Limiter
    ceiling_lin = math.pow(10.0, float(true_peak_ceiling_dbtp) / 20.0)
    abs_env = np.max(np.abs(processed), axis=1)
    # 5ms lookahead max filter + 50ms release smoothing
    lookahead = max(1, int(0.005 * sr))
    release_samples = max(1, int(0.050 * sr))
    from scipy.ndimage import maximum_filter1d

    max_env = maximum_filter1d(abs_env, size=lookahead * 2 + 1)
    gain_reduction = np.where(max_env > ceiling_lin, ceiling_lin / np.maximum(max_env, 1e-12), 1.0)
    # Smooth gain reduction with 1-pole release filter
    alpha_rel = math.exp(-1.0 / float(release_samples))
    b_smooth = [1.0 - alpha_rel]
    a_smooth = [1.0, -alpha_rel]
    smoothed_gr = np.minimum(gain_reduction, sg.lfilter(b_smooth, a_smooth, gain_reduction))
    processed *= smoothed_gr[:, np.newaxis]

    # Verify 4x oversampled True Peak and apply final trim if inter-sample peaks exceed ceiling
    tp_lin = _measure_true_peak_linear(processed)
    if tp_lin > ceiling_lin:
        processed *= (ceiling_lin / tp_lin) * 0.998

    # 6. TPDF Dithering (for 16-bit or 24-bit integer output)
    if apply_tpdf_dither and bit_depth in (16, 24):
        lsb = 1.0 / float(1 << (bit_depth - 1))
        rng = np.random.default_rng()
        tpdf_noise = (rng.uniform(-0.5, 0.5, size=processed.shape) + rng.uniform(-0.5, 0.5, size=processed.shape)) * lsb
        processed = np.clip(processed + tpdf_noise, -1.0, 1.0 - lsb)

    subtype_map = {16: "PCM_16", 24: "PCM_24", 32: "FLOAT"}
    subtype = subtype_map.get(bit_depth, "PCM_24")
    sf.write(str(out_file), processed, sr, subtype=subtype)

    post_audit = analyze_audio_file(out_file)
    return {
        "input_path": str(in_file),
        "output_path": str(out_file),
        "applied_eq_18band_db": dict(zip([f"{f}Hz" for f in MASTERING_EQ_FREQUENCIES_HZ], gains, strict=True)),
        "stereo_width_pct": stereo_width_pct,
        "bit_depth": bit_depth,
        "tpdf_dither": apply_tpdf_dither,
        "post_master_audit": post_audit.to_dict(),
    }


# ---------------------------------------------------------------------------
# Studio Chord Transposition, 30ms Beat Alignment, Loop Quantizer & Lead Sheet
# (Exact replicas of Studio Modules 523, 529, 542, 548, 549)
# ---------------------------------------------------------------------------
_CHORD_TOKEN_RE = re.compile(r"(^|/)([A-G](?:#|b)?)")


def transpose_root_note(root: str, semitones: int, prefer_flats: bool = False) -> str:
    """Transpose a root pitch class (`C`, `F#`, `Bb`) by `semitones` (Module 542)."""
    idx = ROOT_TO_INDEX.get(root)
    if idx is None:
        return root
    target_idx = (idx + int(semitones)) % 12
    table = CHORD_ROOTS_FLAT if prefer_flats else CHORD_ROOTS_SHARP
    return table[target_idx]


def transpose_chord_symbol(chord: str, semitones: int, prefer_flats: bool = False) -> str:
    """Transpose any chord symbol including slash bass notes (`Cmaj7/G` -> `Dmaj7/A`, Module 542)."""
    if not chord or chord.upper() in ("N", "NC", "NONE", "-"):
        return chord

    def _repl(match: re.Match[str]) -> str:
        prefix = match.group(1)
        root = match.group(2)
        return f"{prefix}{transpose_root_note(root, semitones, prefer_flats=prefer_flats)}"

    return _CHORD_TOKEN_RE.sub(_repl, chord.strip())


def classify_pitch_shift_zone(semitones: int) -> dict[str, Any]:
    """Classify a pitch shift according to Studio Module 529 (`Natural zone` vs `Creative zone`)."""
    pitch_factor = math.pow(2.0, float(semitones) / 12.0)
    is_natural = abs(int(semitones)) <= PITCH_NATURAL_ZONE_MAX_SEMITONES
    return {
        "semitones": int(semitones),
        "pitch_factor": round(pitch_factor, 6),
        "zone": "Natural zone" if is_natural else "Creative zone",
        "guidance": (
            "For a natural result, stay within +/-3 semitones."
            if is_natural
            else "Outside +/-3 semitones: creative vocal/formant character shift."
        ),
    }


def snap_timestamp_to_synchronized_beat(
    timestamp_sec: float,
    synchronized_beats_sec: list[float],
    tolerance_sec: float = BEAT_SNAP_TOLERANCE_SEC,
) -> float:
    """Binary-search nearest synchronized beat within `30ms` (`0.030s`, Studio Module 542)."""
    if not synchronized_beats_sec:
        return timestamp_sec
    pos = bisect.bisect_left(synchronized_beats_sec, timestamp_sec)
    candidates: list[float] = []
    if pos < len(synchronized_beats_sec):
        candidates.append(synchronized_beats_sec[pos])
    if pos > 0:
        candidates.append(synchronized_beats_sec[pos - 1])
    best = min(candidates, key=lambda b: abs(b - timestamp_sec))
    return best if abs(best - timestamp_sec) <= tolerance_sec else timestamp_sec


def quantize_loop_to_bar_beat_sixteenth(
    start_sec: float,
    end_sec: float,
    bpm: float,
    beats_per_measure: int = 4,
    subdivisions_per_beat: int = 4,
) -> dict[str, Any]:
    """Convert and snap loop timestamps to `Bar.Beat.1/16` coordinates (Studio Module 523)."""
    if bpm <= 0:
        raise ValueError("BPM must be positive")
    step_sec = 60.0 / float(bpm) / float(subdivisions_per_beat)
    steps_per_bar = beats_per_measure * subdivisions_per_beat

    def _to_bbs(sec: float) -> tuple[float, str, dict[str, int]]:
        total_steps = max(0, round(sec / step_sec))
        snapped_sec = total_steps * step_sec
        bar = (total_steps // steps_per_bar) + 1
        rem = total_steps % steps_per_bar
        beat = (rem // subdivisions_per_beat) + 1
        sixteenth = (rem % subdivisions_per_beat) + 1
        return round(snapped_sec, 4), f"{bar}.{beat}.{sixteenth}", {
            "bar": bar,
            "beat": beat,
            "sixteenth": sixteenth,
        }

    q_start, start_label, start_coord = _to_bbs(start_sec)
    q_end, end_label, end_coord = _to_bbs(max(end_sec, start_sec + step_sec))
    return {
        "bpm": bpm,
        "sixteenth_step_sec": round(step_sec, 6),
        "quantized_start_sec": q_start,
        "quantized_end_sec": q_end,
        "duration_sec": round(q_end - q_start, 4),
        "start_bar_beat_16th": start_label,
        "end_bar_beat_16th": end_label,
        "start_coordinates": start_coord,
        "end_coordinates": end_coord,
    }


def build_lead_sheet_rows(
    lyrics_segments: list[dict[str, Any]],
    chords: list[dict[str, Any]],
    semitone_shift: int = 0,
    prefer_flats: bool = False,
) -> list[dict[str, Any]]:
    """Merge word-level lyrics and transposed chords with 3.0s instrumental gap rows (Module 548)."""
    transposed_chords = [
        {
            "start": float(c.get("start", 0.0)),
            "end": float(c.get("end", 0.0)),
            "chord": transpose_chord_symbol(
                str(c.get("chord") or c.get("label") or ""),
                semitone_shift,
                prefer_flats=prefer_flats,
            ),
        }
        for c in chords
        if str(c.get("chord") or c.get("label") or "").upper() not in ("", "N", "NC", "NONE")
    ]

    rows: list[dict[str, Any]] = []
    prev_end = 0.0

    for seg in lyrics_segments:
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", seg_start))
        text = str(seg.get("text", "")).strip()
        if not text:
            continue

        # Inject standalone instrumental chord row if gap >= CHORD_GAP_MIN_SECONDS (3.0s)
        if seg_start - prev_end >= CHORD_GAP_MIN_SECONDS:
            gap_chords = [
                c["chord"]
                for c in transposed_chords
                if prev_end <= c["start"] < seg_start
            ]
            if gap_chords:
                rows.append(
                    {
                        "type": "chords",
                        "start": round(prev_end, 2),
                        "end": round(seg_start, 2),
                        "chords": gap_chords,
                    }
                )

        line_chords = [
            c for c in transposed_chords if seg_start - 0.15 <= c["start"] <= seg_end + 0.15
        ]
        rows.append(
            {
                "type": "lyrics",
                "start": round(seg_start, 2),
                "end": round(seg_end, 2),
                "text": text,
                "chords": [c["chord"] for c in line_chords],
            }
        )
        prev_end = max(prev_end, seg_end)

    return rows


def render_lead_sheet_html(
    title: str,
    rows: list[dict[str, Any]],
    key_signature: str | None = None,
    bpm: float | None = None,
) -> str:
    """Render a print-ready HTML/PDF Lead Sheet with chords above lyrics (Studio Module 549)."""
    meta_parts: list[str] = []
    if key_signature:
        meta_parts.append(f"Key: {html.escape(key_signature)}")
    if bpm:
        meta_parts.append(f"BPM: {bpm:g}")
    meta_html = " &bull; ".join(meta_parts)

    body_lines: list[str] = []
    for row in rows:
        if row.get("type") == "chords":
            chord_str = "   ".join(html.escape(c) for c in row.get("chords", []))
            body_lines.append(
                f'<div class="row instrumental"><div class="tok-chord">[Instrumental] {chord_str}</div></div>'
            )
        else:
            chord_str = "   ".join(html.escape(c) for c in row.get("chords", []))
            lyric_str = html.escape(str(row.get("text", "")))
            chord_div = f'<div class="tok-chord">{chord_str}</div>' if chord_str else ""
            body_lines.append(
                f'<div class="row">{chord_div}<div class="tok-word">{lyric_str}</div></div>'
            )

    joined_rows = "\n".join(body_lines)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)} — Lead Sheet</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; color: #111; margin: 40px; line-height: 1.5; }}
  h1 {{ margin: 0 0 4px 0; font-size: 24px; }}
  .meta {{ color: #555; font-size: 13px; margin-bottom: 24px; }}
  .row {{ margin-bottom: 14px; page-break-inside: avoid; }}
  .tok-chord {{ font-family: ui-monospace, SFMono-Regular, monospace; font-weight: 700; color: #0f62fe; font-size: 13px; }}
  .tok-word {{ font-size: 15px; }}
  .instrumental {{ padding: 6px 10px; background: #f4f6f8; border-left: 3px solid #0f62fe; }}
</style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <div class="meta">{meta_html}</div>
  {joined_rows}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Gap 1 (Local): Cohesive Batch Album / Folder Mastering Engine
# ---------------------------------------------------------------------------
def master_album_folder(
    input_dir: str | Path,
    output_dir: str | Path,
    *,
    preset: str = "pop",
    eq_gains_db: list[float] | None = None,
    stereo_width_pct: float = 100.0,
    target_lufs: float = -14.0,
    true_peak_ceiling_dbtp: float = -1.0,
    bit_depth: int = 24,
    apply_tpdf_dither: bool = True,
    album_title: str | None = None,
    artist_name: str | None = None,
) -> dict[str, Any]:
    """Batch-master an entire album/EP directory to a cohesive LUFS target, -1.0 dBTP ceiling, and sequential track numbers."""
    from soundboost_toolkit.tagging import write_audio_tags

    in_folder = Path(input_dir).expanduser().resolve()
    if not in_folder.exists() or not in_folder.is_dir():
        raise FileNotFoundError(f"Album input directory not found: {in_folder}")

    out_folder = Path(output_dir).expanduser().resolve()
    out_folder.mkdir(parents=True, exist_ok=True)

    supported_exts = {".wav", ".flac", ".aiff", ".aif", ".ogg", ".mp3"}
    source_files = sorted(
        p for p in in_folder.iterdir() if p.is_file() and p.suffix.lower() in supported_exts
    )
    if not source_files:
        raise ValueError(f"No supported audio files found in {in_folder}")

    total_tracks = len(source_files)
    track_results: list[dict[str, Any]] = []
    lufs_values: list[float] = []
    tp_values: list[float] = []

    for idx, src_path in enumerate(source_files, start=1):
        stem_clean = re.sub(r"^\d+[\s._-]+", "", src_path.stem).strip() or src_path.stem
        out_name = f"{idx:02d}_{stem_clean}_master.wav"
        out_path = out_folder / out_name

        render_res = render_18band_master(
            input_path=src_path,
            output_path=out_path,
            eq_gains_db=eq_gains_db,
            preset=None if eq_gains_db else preset,
            stereo_width_pct=stereo_width_pct,
            target_lufs=target_lufs,
            true_peak_ceiling_dbtp=true_peak_ceiling_dbtp,
            bit_depth=bit_depth,
            apply_tpdf_dither=apply_tpdf_dither,
        )
        write_audio_tags(
            file_path=out_path,
            title=stem_clean,
            artist=artist_name,
            album=album_title or in_folder.name,
            track=f"{idx}/{total_tracks}",
            genre=preset,
        )
        post = render_res["post_master_audit"]
        lufs_values.append(float(post["integrated_lufs"]))
        tp_values.append(float(post["true_peak_dbtp"]))
        track_results.append(
            {
                "track_number": f"{idx}/{total_tracks}",
                "source_file": str(src_path),
                "mastered_file": str(out_path),
                "integrated_lufs": post["integrated_lufs"],
                "true_peak_dbtp": post["true_peak_dbtp"],
                "release_ready_heuristic": post["release_ready_heuristic"],
            }
        )

    return {
        "album_title": album_title or in_folder.name,
        "artist": artist_name,
        "input_dir": str(in_folder),
        "output_dir": str(out_folder),
        "total_tracks": total_tracks,
        "preset": preset,
        "target_lufs": target_lufs,
        "true_peak_ceiling_dbtp": true_peak_ceiling_dbtp,
        "album_mean_lufs": round(float(np.mean(lufs_values)), 2),
        "album_lufs_spread_lu": round(float(np.max(lufs_values) - np.min(lufs_values)), 2),
        "album_max_true_peak_dbtp": round(float(np.max(tp_values)), 2),
        "all_release_ready": all(bool(t["release_ready_heuristic"]) for t in track_results),
        "tracks": track_results,
    }


# ---------------------------------------------------------------------------
# Gap 3 (Local): Natural-Language Mastering Prompt -> 18-Band EQ Translator
# ---------------------------------------------------------------------------
_PROMPT_DESCRIPTOR_RULES: tuple[tuple[tuple[str, ...], dict[int, float], float, float], ...] = (
    # (keywords, {band_index: delta_db}, stereo_width_delta_pct, lufs_delta)
    (("sub", "808", "deep bass", "rumble", "low end"), {0: 1.8, 1: 2.2, 2: 1.8, 3: 1.0}, 0.0, 0.0),
    (("punch", "punchy", "thump", "kick", "impact", "slam"), {2: 1.4, 3: 1.8, 4: 1.2, 5: -0.6}, 0.0, 0.5),
    (("warm", "warmer", "warmth", "analog", "tape", "tube", "thick"), {4: 1.0, 5: 1.4, 6: 1.2, 7: 0.8, 15: -0.4, 16: -0.6}, 4.0, 0.0),
    (("mud", "muddy", "clean low-mid", "de-mud", "boxy", "boomy"), {5: -1.4, 6: -2.0, 7: -1.8, 8: -1.0}, 0.0, 0.0),
    (("vocal", "vocals", "forward", "presence", "clarity", "clear", "intimate"), {10: 0.8, 11: 1.4, 12: 1.8, 13: 1.5}, 0.0, 0.0),
    (("harsh", "de-ess", "sibilance", "smooth", "tame highs", "piercing", "cymbal"), {13: -1.6, 14: -2.2, 15: -1.8}, 0.0, 0.0),
    (("bright", "brighter", "crisp", "crispy", "sparkle", "sheen", "shimmer"), {14: 1.2, 15: 1.6, 16: 2.0, 17: 2.2}, 5.0, 0.0),
    (("air", "airy", "open", "silky", "top end", "breath"), {16: 1.8, 17: 2.5}, 8.0, 0.0),
    (("wide", "wider", "stereo", "spacious", "huge", "immersive"), {15: 0.6, 16: 1.0}, 25.0, 0.0),
    (("mono", "narrow", "tight center", "focused"), {}, -25.0, 0.0),
    (("loud", "club", "competitive", "aggressive", "festival", "maximizer"), {1: 1.2, 2: 1.4, 13: 1.0, 16: 1.2}, 8.0, 3.0),
    (("dynamic", "natural", "gentle", "audiophile", "acoustic", "open dynamics"), {}, 0.0, -2.0),
)


def translate_prompt_to_18band_eq(
    prompt: str,
    base_preset: str = "flat",
    base_target_lufs: float = -14.0,
) -> dict[str, Any]:
    """Deterministically translate a natural-language mastering prompt into an 18-band EQ curve, stereo width, and LUFS target."""
    preset_key = base_preset.strip().lower()
    if preset_key not in PRESET_EQ_CURVES_18BAND:
        preset_key = "flat"
    gains = list(PRESET_EQ_CURVES_18BAND[preset_key])
    width_pct = 100.0
    target_lufs = float(base_target_lufs)
    matched_rules: list[str] = []

    text_lower = prompt.strip().lower()
    for g_key in PRESET_EQ_CURVES_18BAND:
        if g_key != "flat" and g_key.replace("_", " ") in text_lower and preset_key == "flat":
            gains = list(PRESET_EQ_CURVES_18BAND[g_key])
            matched_rules.append(f"genre_base:{g_key}")
            break

    for keywords, band_deltas, width_delta, lufs_delta in _PROMPT_DESCRIPTOR_RULES:
        if any(kw in text_lower for kw in keywords):
            matched_rules.append(keywords[0])
            for band_idx, delta_db in band_deltas.items():
                gains[band_idx] = gains[band_idx] + delta_db
            width_pct += width_delta
            target_lufs += lufs_delta

    clamped_gains = [
        round(max(MASTERING_EQ_MIN_DB, min(MASTERING_EQ_MAX_DB, g)), 2) for g in gains
    ]
    clamped_width = round(max(0.0, min(200.0, width_pct)), 1)
    clamped_lufs = round(max(-20.0, min(-7.0, target_lufs)), 2)

    return {
        "prompt": prompt,
        "matched_descriptors": matched_rules,
        "eq_18band_db": clamped_gains,
        "eq_by_frequency": dict(
            zip([f"{f}Hz" for f in MASTERING_EQ_FREQUENCIES_HZ], clamped_gains, strict=True)
        ),
        "recommended_stereo_width_pct": clamped_width,
        "recommended_target_lufs": clamped_lufs,
        "recommended_ceiling_dbtp": -1.0,
    }


# ---------------------------------------------------------------------------
# Gap 5A: Studio Practice Mode Chord Voicing Diagrams (Studio Module 534)
# Supports Guitar (6-string), Ukulele (4-string), and Piano (MIDI + keyboard)
# ---------------------------------------------------------------------------
_CHORD_PARSE_RE = re.compile(r"^([A-G](?:#|b)?)(.*?)(?:/([A-G](?:#|b)?))?$")


def _parse_chord_symbol(chord: str) -> tuple[str, str, str | None]:
    cleaned = chord.strip()
    m = _CHORD_PARSE_RE.match(cleaned)
    if not m:
        return "C", "", None
    root, quality, bass = m.group(1), m.group(2).strip(), m.group(3)
    return root, quality, bass


def _render_fretboard_ascii(
    chord_name: str,
    string_names: tuple[str, ...],
    frets: tuple[int, ...],
    base_fret: int = 1,
) -> str:
    """Render a clean ASCII chord diagram for Guitar (6 strings) or Ukulele (4 strings)."""
    lines: list[str] = [f"  {chord_name}"]
    top_markers = "  " + " ".join(
        "X" if f < 0 else ("O" if f == 0 else " ") for f in frets
    )
    lines.append(top_markers)
    lines.append("  " + "=" * (len(frets) * 2 - 1) if base_fret == 1 else f"{base_fret:2d}" + "-" * (len(frets) * 2 - 1))
    for fret_offset in range(4):
        fret_num = base_fret + fret_offset
        row_cells = [
            "*" if f == fret_num else "|" for f in frets
        ]
        lines.append("  " + "-".join(row_cells))
    lines.append("  " + " ".join(string_names))
    return "\n".join(lines)


def get_chord_voicing_diagram(
    chord: str,
    instrument: str = "guitar",
) -> dict[str, Any]:
    """Generate interactive Chord Voicing & Fingering Diagrams for `guitar`, `ukulele`, or `piano` (Studio Module 534)."""
    inst = instrument.strip().lower()
    if inst not in ("guitar", "ukulele", "piano"):
        raise ValueError(f"Unsupported instrument '{instrument}'. Choose from: guitar, ukulele, piano")

    root, quality, bass = _parse_chord_symbol(chord)
    root_pc = ROOT_TO_INDEX.get(root, 0)
    intervals = CHORD_QUALITY_INTERVALS.get(quality, CHORD_QUALITY_INTERVALS.get(quality.lower(), (0, 4, 7)))
    pitch_classes = [CHORD_ROOTS_SHARP[(root_pc + iv) % 12] for iv in intervals]
    midi_notes = [60 + root_pc + iv for iv in intervals]
    if bass and bass in ROOT_TO_INDEX:
        bass_midi = 48 + ROOT_TO_INDEX[bass]
        midi_notes = [bass_midi, *midi_notes]

    canonical_key = f"{root}{quality}"
    sharp_root = CHORD_ROOTS_SHARP[root_pc]
    flat_root = CHORD_ROOTS_FLAT[root_pc]

    if inst == "piano":
        white_keys = ("C", "D", "E", "F", "G", "A", "B")
        active_set = set(pitch_classes)
        ascii_keys = " | ".join(
            f"[{n}]" if n in active_set else f" {n} " for n in CHORD_ROOTS_SHARP
        )
        return {
            "chord": chord,
            "instrument": "piano",
            "root": root,
            "quality": quality or "maj",
            "bass": bass,
            "pitch_classes": pitch_classes,
            "midi_notes": midi_notes,
            "white_keys_reference": list(white_keys),
            "ascii_diagram": f"Piano ({chord}): {ascii_keys}",
        }

    shape_db = GUITAR_CHORD_SHAPES if inst == "guitar" else UKULELE_CHORD_SHAPES
    string_names = ("E", "A", "D", "G", "B", "e") if inst == "guitar" else ("G", "C", "E", "A")
    open_midi = (40, 45, 50, 55, 59, 64) if inst == "guitar" else (67, 60, 64, 69)

    shape = (
        shape_db.get(canonical_key)
        or shape_db.get(f"{sharp_root}{quality}")
        or shape_db.get(f"{flat_root}{quality}")
    )

    if shape is None:
        # Procedural barre/movable voicing fallback
        is_minor = quality.startswith("m") and not quality.startswith("maj")
        if inst == "guitar":
            offset = (root_pc - 4) % 12
            base = max(1, offset)
            frets = (
                offset,
                offset + 2,
                offset + 2,
                offset + (1 if not is_minor else 0),
                offset,
                offset,
            )
            fingers = (1, 3, 4, 2 if not is_minor else 1, 1, 1)
            barres = (offset,) if offset > 0 else ()
        else:
            offset = root_pc % 12
            base = max(1, offset)
            frets = (offset, offset, offset, offset + (3 if not is_minor else 2))
            fingers = (1, 1, 1, 4)
            barres = (offset,) if offset > 0 else ()
    else:
        frets = tuple(int(x) for x in shape["frets"])  # type: ignore[index]
        fingers = tuple(int(x) for x in shape["fingers"])  # type: ignore[index]
        base = int(shape["base_fret"])  # type: ignore[arg-type]
        barres = tuple(int(x) for x in shape["barres"])  # type: ignore[index]

    sounding_midi = [
        open_m + f for open_m, f in zip(open_midi, frets, strict=True) if f >= 0
    ]
    ascii_diag = _render_fretboard_ascii(chord, string_names, frets, base_fret=base)
    return {
        "chord": chord,
        "instrument": inst,
        "root": root,
        "quality": quality or "maj",
        "bass": bass,
        "string_names": list(string_names),
        "frets": list(frets),
        "fingers": list(fingers),
        "base_fret": base,
        "barres": list(barres),
        "pitch_classes": pitch_classes,
        "sounding_midi_notes": sounding_midi,
        "ascii_diagram": ascii_diag,
    }


# ---------------------------------------------------------------------------
# Gap 5B: Local Pitch-Shift & Time-Stretch Renderer (Studio Modules 594-601)
# ---------------------------------------------------------------------------
def _phase_vocoder_time_stretch_channel(
    channel_data: np.ndarray,
    speed_factor: float,
    n_fft: int = 2048,
    hop_length: int = 512,
) -> np.ndarray:
    """STFT phase-vocoder time-stretcher preserving pitch for a single channel."""
    if abs(speed_factor - 1.0) < 1e-4:
        return channel_data.copy()

    _, _, zxx = sg.stft(
        channel_data,
        nperseg=n_fft,
        noverlap=n_fft - hop_length,
        window="hann",
    )
    n_bins, n_frames = zxx.shape
    if n_frames < 2:
        target_len = max(1, round(channel_data.shape[0] / speed_factor))
        return sg.resample(channel_data, target_len)

    time_steps = np.arange(0, n_frames - 1, speed_factor, dtype=np.float64)
    new_zxx = np.zeros((n_bins, time_steps.size), dtype=np.complex128)

    omega = 2.0 * math.pi * hop_length * np.arange(n_bins) / float(n_fft)
    phase_acc = np.angle(zxx[:, 0])

    for out_idx, t in enumerate(time_steps):
        frame_idx = math.floor(t)
        frac = t - frame_idx
        f0 = zxx[:, frame_idx]
        f1 = zxx[:, min(frame_idx + 1, n_frames - 1)]

        mag = (1.0 - frac) * np.abs(f0) + frac * np.abs(f1)
        delta_phase = np.angle(f1) - np.angle(f0) - omega
        delta_phase = (delta_phase + math.pi) % (2.0 * math.pi) - math.pi
        inst_freq = omega + delta_phase

        new_zxx[:, out_idx] = mag * np.exp(1j * phase_acc)
        phase_acc = phase_acc + inst_freq

    _, stretched = sg.istft(
        new_zxx,
        nperseg=n_fft,
        noverlap=n_fft - hop_length,
        window="hann",
    )
    return stretched.astype(np.float64)


def render_local_pitch_and_time_stretch(
    input_path: str | Path,
    output_path: str | Path,
    *,
    semitones: float = 0.0,
    speed_factor: float = 1.0,
    bit_depth: int = 24,
) -> dict[str, Any]:
    """Render local offline Pitch-Shifting (`semitones`, pitchFactor = 2^(st/12)) and Time-Stretching (`speed_factor`, Studio Modules 594-601)."""
    if not (0.25 <= speed_factor <= 4.0):
        raise ValueError("speed_factor must be between 0.25 and 4.0")
    if not (-24.0 <= semitones <= 24.0):
        raise ValueError("semitones must be between -24 and +24")

    in_file = _validate_audio_path(input_path)
    out_file = Path(output_path).expanduser().resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    samples, sr = sf.read(str(in_file), always_2d=True, dtype="float64")
    pitch_factor = math.pow(2.0, float(semitones) / 12.0)

    # Resampling by 1/pitch_factor scales duration by 1/pitch_factor, so to achieve
    # final duration = orig_duration / speed_factor, the phase vocoder stretches at
    # rate = speed_factor / pitch_factor.
    vocoder_rate = float(speed_factor) / pitch_factor
    stretched_channels: list[np.ndarray] = []
    for ch in range(samples.shape[1]):
        ch_stretched = _phase_vocoder_time_stretch_channel(samples[:, ch], vocoder_rate)
        if abs(pitch_factor - 1.0) > 1e-4:
            # Wider rational base keeps the resample ratio within ~0.01 cents of
            # the exact 2^(st/12) pitch factor for arbitrary (non-12-TET) shifts.
            up = 48000
            down = max(1, round(48000.0 * pitch_factor))
            ch_out = sg.resample_poly(ch_stretched, up=up, down=down)
        else:
            ch_out = ch_stretched
        stretched_channels.append(ch_out)

    min_len = min(c.shape[0] for c in stretched_channels)
    output_audio = np.column_stack([c[:min_len] for c in stretched_channels])
    peak = float(np.max(np.abs(output_audio))) if output_audio.size > 0 else 0.0
    if peak > 0.999:
        output_audio *= 0.995 / peak

    subtype_map = {16: "PCM_16", 24: "PCM_24", 32: "FLOAT"}
    sf.write(str(out_file), output_audio, sr, subtype=subtype_map.get(bit_depth, "PCM_24"))

    return {
        "input_path": str(in_file),
        "output_path": str(out_file),
        "semitones": semitones,
        "pitch_factor": round(pitch_factor, 6),
        "pitch_zone": classify_pitch_shift_zone(round(semitones)),
        "speed_factor": speed_factor,
        "input_duration_sec": round(samples.shape[0] / float(sr), 4),
        "output_duration_sec": round(output_audio.shape[0] / float(sr), 4),
        "sample_rate": sr,
        "bit_depth": bit_depth,
    }


# ---------------------------------------------------------------------------
# Gap 5C: LAME Gapless MP3 Frame Header & Decoder-Delay Inspector (Modules 590-591)
# ---------------------------------------------------------------------------
_MPEG1_BITRATES_KBPS = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_MPEG2_BITRATES_KBPS = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_MPEG1_SAMPLE_RATES = (44100, 48000, 32000, 0)
_MPEG2_SAMPLE_RATES = (22050, 24000, 16000, 0)
_MPEG25_SAMPLE_RATES = (11025, 12000, 8000, 0)


def inspect_mp3_gapless_headers(file_path: str | Path) -> dict[str, Any]:
    """Inspect MP3 binary frames, Xing/Info/LAME3.100 gapless tags, encoder delay (`576`), and priming samples (`1105`, Studio Modules 590-591)."""
    path = _validate_audio_path(file_path)
    raw = path.read_bytes()
    size_bytes = len(raw)

    # Skip ID3v2 tag if present at start of file
    offset = 0
    id3v2_size = 0
    if size_bytes >= 10 and raw[:3] == b"ID3":
        # Synchsafe 28-bit integer size
        id3v2_size = (
            ((raw[6] & 0x7F) << 21)
            | ((raw[7] & 0x7F) << 14)
            | ((raw[8] & 0x7F) << 7)
            | (raw[9] & 0x7F)
        ) + 10
        offset = min(size_bytes, id3v2_size)

    # Scan for first valid MPEG Layer III frame sync (11 set bits: 0xFFE0)
    first_frame_offset = -1
    mpeg_version_label = "MPEG-1"
    samples_per_frame = MP3_SAMPLES_PER_FRAME_MPEG1
    sample_rate = 44100
    bitrate_kbps = 128
    channel_mode = "Stereo"

    scan_limit = min(size_bytes - 4, offset + 65536)
    for pos in range(offset, scan_limit):
        if raw[pos] == 0xFF and (raw[pos + 1] & 0xE0) == 0xE0:
            b1 = raw[pos + 1]
            b2 = raw[pos + 2]
            b3 = raw[pos + 3]
            ver_bits = (b1 >> 3) & 0x03
            layer_bits = (b1 >> 1) & 0x03
            br_idx = (b2 >> 4) & 0x0F
            sr_idx = (b2 >> 2) & 0x03
            ch_bits = (b3 >> 6) & 0x03

            # Layer III is 0b01
            if ver_bits != 0x01 and layer_bits == 0x01 and 0 < br_idx < 15 and sr_idx < 3:
                first_frame_offset = pos
                if ver_bits == 0x03:
                    mpeg_version_label = "MPEG-1"
                    samples_per_frame = MP3_SAMPLES_PER_FRAME_MPEG1
                    sample_rate = _MPEG1_SAMPLE_RATES[sr_idx]
                    bitrate_kbps = _MPEG1_BITRATES_KBPS[br_idx]
                elif ver_bits == 0x02:
                    mpeg_version_label = "MPEG-2"
                    samples_per_frame = MP3_SAMPLES_PER_FRAME_MPEG2
                    sample_rate = _MPEG2_SAMPLE_RATES[sr_idx]
                    bitrate_kbps = _MPEG2_BITRATES_KBPS[br_idx]
                else:
                    mpeg_version_label = "MPEG-2.5"
                    samples_per_frame = MP3_SAMPLES_PER_FRAME_MPEG2
                    sample_rate = _MPEG25_SAMPLE_RATES[sr_idx]
                    bitrate_kbps = _MPEG2_BITRATES_KBPS[br_idx]
                channel_mode = ("Stereo", "Joint Stereo", "Dual Channel", "Mono")[ch_bits]
                break

    # Inspect Xing / Info / LAME gapless header inside first frame
    has_xing_or_info = False
    header_tag_type: str | None = None
    lame_version: str | None = None
    encoder_delay_samples = LAME_DEFAULT_ENCODER_DELAY_SAMPLES
    encoder_padding_samples = 0
    total_frames: int | None = None

    if first_frame_offset >= 0:
        window = raw[first_frame_offset : min(size_bytes, first_frame_offset + 512)]
        for marker in (b"Xing", b"Info"):
            idx = window.find(marker)
            if idx >= 0:
                has_xing_or_info = True
                header_tag_type = marker.decode("ascii")
                flags_pos = idx + 4
                cursor = flags_pos + 4
                if cursor <= len(window):
                    flags = int.from_bytes(window[flags_pos:cursor], "big")
                    if flags & 0x01 and cursor + 4 <= len(window):
                        total_frames = int.from_bytes(window[cursor : cursor + 4], "big")
                        cursor += 4
                    if flags & 0x02:
                        cursor += 4
                    if flags & 0x04:
                        cursor += 100
                    if flags & 0x08:
                        cursor += 4
                    # LAME 9-byte version string + 24-bit gapless delay/padding at +21 bytes from LAME tag start
                    lame_pos = window.find(b"LAME", idx)
                    if lame_pos >= 0 and lame_pos + 24 <= len(window):
                        lame_version = window[lame_pos : lame_pos + 9].decode("latin1", errors="ignore").strip()
                        b_delay = window[lame_pos + 21 : lame_pos + 24]
                        encoder_delay_samples = (b_delay[0] << 4) | (b_delay[1] >> 4)
                        encoder_padding_samples = ((b_delay[1] & 0x0F) << 8) | b_delay[2]
                break

    total_priming_samples = encoder_delay_samples + MP3_DEFAULT_DECODER_DELAY_SAMPLES
    return {
        "file_path": str(path),
        "file_size_bytes": size_bytes,
        "id3v2_header_bytes": id3v2_size,
        "first_frame_byte_offset": first_frame_offset,
        "mpeg_version": mpeg_version_label,
        "channel_mode": channel_mode,
        "sample_rate": sample_rate,
        "bitrate_kbps": bitrate_kbps,
        "samples_per_frame": samples_per_frame,
        "has_gapless_xing_info_header": has_xing_or_info,
        "gapless_header_type": header_tag_type,
        "lame_version": lame_version,
        "total_frames_reported": total_frames,
        "encoder_delay_samples": encoder_delay_samples,
        "decoder_delay_samples": MP3_DEFAULT_DECODER_DELAY_SAMPLES,
        "total_priming_samples": total_priming_samples,
        "lame_standard_priming_samples": LAME_TOTAL_PRIMING_SAMPLES,
        "encoder_padding_samples": encoder_padding_samples,
        "priming_offset_ms": round(total_priming_samples * 1000.0 / float(max(1, sample_rate)), 3),
    }

