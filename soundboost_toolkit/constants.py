"""Platform constants, DSP tables, separation profiles, and entitlement defaults."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, TypedDict

# ---------------------------------------------------------------------------
# Subdomain Base URLs
# ---------------------------------------------------------------------------
BACKEND_URL: Final[str] = "https://api.soundboost.ai"
MASTERING_URL: Final[str] = "https://mastering.soundboost.ai"
STUDIO_URL: Final[str] = "https://studio.soundboost.ai"
CONVERTA_URL: Final[str] = "https://converta.soundboost.ai"
SHOOTS_URL: Final[str] = "https://shoots.soundboost.ai"
SMART_LINK_SHORT_DOMAIN: Final[str] = "https://soundboo.st"

# ---------------------------------------------------------------------------
# 18-Band Mastering Graphic EQ (mastering.soundboost.ai Manual EQ Engine)
# Range: -6.0 dB to +6.0 dB, step 0.1 dB
# ---------------------------------------------------------------------------
MASTERING_EQ_FREQUENCIES_HZ: Final[tuple[int, ...]] = (
    20,
    30,
    45,
    70,
    100,
    140,
    200,
    315,
    500,
    700,
    1000,
    1400,
    2000,
    3150,
    5000,
    7000,
    10000,
    15000,
)
MASTERING_EQ_MIN_DB: Final[float] = -6.0
MASTERING_EQ_MAX_DB: Final[float] = 6.0
MASTERING_EQ_STEP_DB: Final[float] = 0.1

# ---------------------------------------------------------------------------
# 8 Mastering Modes & 12 Genre Presets
# ---------------------------------------------------------------------------
MASTERING_MODES: Final[tuple[str, ...]] = (
    "prompt",
    "preset",
    "auto",
    "reference",
    "chat",
    "manual",
    "mix_doctor",
    "enhance",
)

MASTERING_GENRE_PRESETS: Final[dict[str, str]] = {
    "hip_hop": "Hip-Hop — Deep 808 sub weight, punchy transient snap, crisp vocal presence",
    "trap": "Trap — Extended sub-bass shelf, tight snare transients, airy hi-hat sheen",
    "pop": "Pop — Polished modern loudness, wide stereo image, forward vocal clarity",
    "rock_indie": "Rock / Indie — Warm low-mids, energetic upper-mid bite, natural drum glue",
    "rnb_soul": "R&B / Soul — Silky low-end warmth, smooth 2-4 kHz presence, lush stereo width",
    "electronic_edm": "Electronic / EDM — Maximum club loudness, tight sub-bass, wide synth imaging",
    "acoustic_folk": "Acoustic / Folk — Transparent dynamics, natural body, gentle air shelf",
    "jazz_blues": "Jazz / Blues — Wide dynamic range, warm midrange, unclipped acoustic transients",
    "classical_orchestral": "Classical / Orchestral — Full dynamic range preservation, natural hall depth",
    "metal_punk": "Metal / Punk — Controlled 250 Hz mud cut, aggressive guitar bite, fast limiting",
    "latin_reggaeton": "Latin / Reggaeton — Punchy dembow low-end, clear vocal projection, warm highs",
    "podcast_spoken_word": "Podcast / Spoken Word — De-essed vocal clarity, -16 LUFS broadcast loudness",
}

#Preset 18-Band EQ curves (dB at each of the 18 bands) for local offline rendering
PRESET_EQ_CURVES_18BAND: Final[dict[str, tuple[float, ...]]] = {
    "flat": (0.0,) * 18,
    "hip_hop": (1.2, 1.8, 2.0, 1.2, 0.3, -0.6, -1.0, -0.5, 0.0, 0.2, 0.6, 1.0, 1.4, 1.2, 0.8, 1.0, 1.5, 1.8),
    "trap": (1.5, 2.2, 2.4, 1.0, 0.0, -0.8, -1.2, -0.6, 0.0, 0.0, 0.4, 0.8, 1.2, 1.0, 1.2, 1.6, 2.0, 2.2),
    "pop": (0.6, 1.0, 1.4, 1.0, 0.4, -0.4, -0.8, -0.4, 0.0, 0.3, 0.8, 1.2, 1.5, 1.4, 1.2, 1.4, 1.8, 2.0),
    "rock_indie": (0.4, 0.8, 1.2, 1.4, 0.8, 0.2, -0.5, -0.3, 0.2, 0.6, 1.0, 1.4, 1.6, 1.2, 0.8, 0.6, 0.8, 1.0),
    "rnb_soul": (1.0, 1.5, 1.8, 1.2, 0.6, 0.0, -0.6, -0.4, 0.0, 0.4, 0.6, 0.8, 1.0, 0.8, 0.6, 1.0, 1.4, 1.6),
    "electronic_edm": (1.4, 2.0, 2.2, 1.5, 0.4, -0.8, -1.2, -0.6, -0.2, 0.2, 0.6, 1.0, 1.4, 1.6, 1.4, 1.6, 2.0, 2.2),
    "acoustic_folk": (-0.5, 0.0, 0.5, 0.8, 0.6, 0.2, -0.4, -0.2, 0.0, 0.2, 0.5, 0.8, 1.0, 0.8, 0.6, 0.8, 1.2, 1.4),
    "jazz_blues": (0.2, 0.6, 0.8, 0.8, 0.6, 0.2, 0.0, 0.0, 0.2, 0.4, 0.4, 0.6, 0.6, 0.4, 0.4, 0.6, 0.8, 0.8),
    "classical_orchestral": (0.2, 0.4, 0.4, 0.4, 0.2, 0.0, 0.0, 0.0, 0.0, 0.2, 0.2, 0.4, 0.4, 0.4, 0.4, 0.6, 0.8, 0.8),
    "metal_punk": (0.4, 1.0, 1.6, 1.4, 0.4, -0.8, -1.5, -1.0, -0.2, 0.6, 1.2, 1.8, 2.0, 1.6, 1.0, 0.8, 1.0, 1.0),
    "latin_reggaeton": (1.2, 1.8, 2.2, 1.4, 0.4, -0.6, -0.8, -0.4, 0.0, 0.4, 0.8, 1.2, 1.4, 1.2, 1.0, 1.2, 1.6, 1.8),
    "podcast_spoken_word": (-3.0, -2.5, -1.5, -0.5, 0.4, 0.6, -0.6, -0.4, 0.2, 0.6, 1.2, 1.6, 1.8, 1.4, -0.4, -0.6, 0.4, 0.4),
}

# ---------------------------------------------------------------------------
# Studio Stem Separation Profiles & 5-Piece Drum Kit Sub-Stems
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StemProfileSpec:
    profile_key: str
    stem_variant: str
    stem_format: str
    separation_model: str
    stems: tuple[str, ...]
    requires_plus: bool


STEM_PROFILES: Final[dict[str, StemProfileSpec]] = {
    "standard_2stem": StemProfileSpec(
        profile_key="standard_2stem",
        stem_variant="standard",
        stem_format="2_stem",
        separation_model="htdemucs",
        stems=("Vocals", "Instrumental"),
        requires_plus=False,
    ),
    "voice_clean": StemProfileSpec(
        profile_key="standard_2stem",
        stem_variant="standard",
        stem_format="2_stem",
        separation_model="htdemucs",
        stems=("Voice", "Artifacts"),
        requires_plus=False,
    ),
    "standard_4stem": StemProfileSpec(
        profile_key="standard_4stem",
        stem_variant="standard",
        stem_format="4_stem",
        separation_model="htdemucs_ft",
        stems=("Vocals", "Bass", "Drums", "Other", "Metronome"),
        requires_plus=False,
    ),
    "hifi_4stem": StemProfileSpec(
        profile_key="hifi_4stem",
        stem_variant="hifi",
        stem_format="4_stem",
        separation_model="htdemucs_ft",
        stems=("Vocals", "Bass", "Drums", "Other", "Metronome"),
        requires_plus=True,
    ),
    "hifi_6stem": StemProfileSpec(
        profile_key="hifi_6stem",
        stem_variant="hifi",
        stem_format="6_stem",
        separation_model="htdemucs_6s",
        stems=("Vocals", "Bass", "Drums", "Guitar", "Piano", "Other", "Metronome"),
        requires_plus=True,
    ),
}

DRUM_KIT_SUBSTEMS: Final[tuple[str, ...]] = (
    "Kick",
    "Snare",
    "Toms",
    "Hi-Hat",
    "Cymbals",
)

# ---------------------------------------------------------------------------
# Studio Chord, Lyric, Pitch & Loop Constants (Modules 523, 529, 534, 542, 548)
# ---------------------------------------------------------------------------
CHORD_ROOTS_SHARP: Final[tuple[str, ...]] = (
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"
)
CHORD_ROOTS_FLAT: Final[tuple[str, ...]] = (
    "C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"
)
ROOT_TO_INDEX: Final[dict[str, int]] = {
    "C": 0, "B#": 0,
    "C#": 1, "Db": 1,
    "D": 2,
    "D#": 3, "Eb": 3,
    "E": 4, "Fb": 4,
    "F": 5, "E#": 5,
    "F#": 6, "Gb": 6,
    "G": 7,
    "G#": 8, "Ab": 8,
    "A": 9,
    "A#": 10, "Bb": 10,
    "B": 11, "Cb": 11,
}

BEAT_SNAP_TOLERANCE_SEC: Final[float] = 0.030  # 30ms binary search window
CHORD_GAP_MIN_SECONDS: Final[float] = 3.0      # Inject instrumental chord row >= 3.0s
PITCH_NATURAL_ZONE_MAX_SEMITONES: Final[int] = 3
PRACTICE_INSTRUMENTS: Final[tuple[str, ...]] = ("guitar", "ukulele", "piano")

# ---------------------------------------------------------------------------
# Converta Supported Audio Format Matrix (GET https://converta.soundboost.ai/api/v1/formats)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConvertaFormatSpec:
    format_name: str
    bit_depths: tuple[int, ...]
    sample_rates: tuple[int, ...]
    supports_dithering: bool


CONVERTA_FORMATS: Final[dict[str, ConvertaFormatSpec]] = {
    "wav": ConvertaFormatSpec("wav", (16, 24, 32), (44100, 48000), True),
    "aiff": ConvertaFormatSpec("aiff", (16, 24, 32), (44100, 48000), True),
    "flac": ConvertaFormatSpec("flac", (16, 24), (44100, 48000), True),
    "ogg": ConvertaFormatSpec("ogg", (16, 24), (44100, 48000), False),
    "aac": ConvertaFormatSpec("aac", (16, 24), (44100, 48000), False),
    "m4a": ConvertaFormatSpec("m4a", (16, 24), (44100, 48000), False),
    "mp3": ConvertaFormatSpec("mp3", (16,), (44100, 48000), False),
}

# ---------------------------------------------------------------------------
# Streaming Platform Loudness Normalization Targets & True-Peak Ceilings
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StreamingTargetSpec:
    platform: str
    target_lufs: float
    true_peak_ceiling_dbtp: float
    loud_track_ceiling_dbtp: float


STREAMING_TARGETS: Final[dict[str, StreamingTargetSpec]] = {
    "spotify": StreamingTargetSpec("Spotify", -14.0, -1.0, -2.0),
    "apple_music": StreamingTargetSpec("Apple Music (Sound Check)", -16.0, -1.0, -1.0),
    "youtube": StreamingTargetSpec("YouTube / YouTube Music", -14.0, -1.0, -1.0),
    "tidal": StreamingTargetSpec("Tidal", -14.0, -1.0, -1.0),
    "amazon_music": StreamingTargetSpec("Amazon Music", -14.0, -2.0, -2.0),
    "deezer": StreamingTargetSpec("Deezer", -15.0, -1.0, -1.0),
    "soundcloud": StreamingTargetSpec("SoundCloud", -14.0, -1.0, -1.0),
    "pandora": StreamingTargetSpec("Pandora", -14.0, -1.0, -1.0),
    "club_dj": StreamingTargetSpec("Club / DJ Reference", -8.0, -0.3, -0.3),
}

# ---------------------------------------------------------------------------
# AI Music Video (h3-max) Tier Character Reference Caps & Account Entitlements
# ---------------------------------------------------------------------------
MV_CHARACTER_REF_CAPS: Final[dict[str, int]] = {
    "starter": 1,
    "creator": 1,
    "director": 2,
    "studio": 3,
}


def compute_mv_spending_limit(quoted_credits: int) -> int:
    """Calculate the AI Music Video (`h3-max`) retake spending cap.

    Formula: credits + 3 * floor(credits / 20) + ceil(((credits % 20) * 3) / 20)
    """
    if quoted_credits <= 0:
        return 0
    return (
        quoted_credits
        + 3 * (quoted_credits // 20)
        + math.ceil(((quoted_credits % 20) * 3) / 20)
    )


# SoundBoost Account Tiers & Exact Feature Entitlement Matrix
# Extracted from Mastering `_app`, Studio `Module 584/585`, and Shoots `tierLevel`
ACCOUNT_TIER_ENTITLEMENTS: Final[dict[str, dict[str, object]]] = {
    "free": {
        "tier": "free",
        "requires_auth": False,
        "free_tools_unlocked": True,
        "mastering_preview": True,
        "mastering_full_length_download": False,
        "mastering_unlimited_revisions": False,
        "bulk_mastering_unlocked": False,
        "studio_standard_stems_unlocked": True,  # 2-stem, voice_clean, standard_4stem
        "studio_hifi_plus_stems_unlocked": False,  # hifi_4stem, hifi_6stem, 5-piece drums
        "studio_chord_lyric_ai_unlocked": True,
        "studio_pitch_time_export_unlocked": False,
        "smart_links_unlocked": True,
        "mv_character_ref_cap": 0,
        "mv_auto_qc": False,
        "mv_deep_research": False,
        "mv_resolutions": (),
        "shoots_ai_clones_unlocked": False,
    },
    "pay_as_you_go": {
        "tier": "pay_as_you_go",
        "requires_auth": True,
        "free_tools_unlocked": True,
        "mastering_preview": True,
        "mastering_full_length_download": True,  # if credits > 0
        "mastering_unlimited_revisions": False,
        "bulk_mastering_unlocked": False,
        "studio_standard_stems_unlocked": True,
        "studio_hifi_plus_stems_unlocked": False,
        "studio_chord_lyric_ai_unlocked": True,
        "studio_pitch_time_export_unlocked": True,
        "smart_links_unlocked": True,
        "mv_character_ref_cap": 1,
        "mv_auto_qc": False,
        "mv_deep_research": False,
        "mv_resolutions": ("480P",),
        "shoots_ai_clones_unlocked": False,
    },
    "unlimited": {
        "tier": "unlimited",
        "requires_auth": True,
        "free_tools_unlocked": True,
        "mastering_preview": True,
        "mastering_full_length_download": True,
        "mastering_unlimited_revisions": True,
        "bulk_mastering_unlocked": False,
        "studio_standard_stems_unlocked": True,
        "studio_hifi_plus_stems_unlocked": False,
        "studio_chord_lyric_ai_unlocked": True,
        "studio_pitch_time_export_unlocked": True,
        "smart_links_unlocked": True,
        "mv_character_ref_cap": 1,
        "mv_auto_qc": False,
        "mv_deep_research": False,
        "mv_resolutions": ("480P", "768P"),
        "shoots_ai_clones_unlocked": True,
    },
    "unlimited_plus": {
        "tier": "unlimited_plus",
        "requires_auth": True,
        "free_tools_unlocked": True,
        "mastering_preview": True,
        "mastering_full_length_download": True,
        "mastering_unlimited_revisions": True,
        "bulk_mastering_unlocked": True,
        "studio_standard_stems_unlocked": True,
        "studio_hifi_plus_stems_unlocked": True,  # Unlocks hifi_4stem, hifi_6stem, 5-piece drums
        "studio_chord_lyric_ai_unlocked": True,
        "studio_pitch_time_export_unlocked": True,
        "smart_links_unlocked": True,
        "mv_character_ref_cap": 2,
        "mv_auto_qc": True,
        "mv_deep_research": True,
        "mv_resolutions": ("480P", "768P"),
        "shoots_ai_clones_unlocked": True,
    },
    "starter": {
        "tier": "starter",
        "requires_auth": True,
        "free_tools_unlocked": True,
        "mastering_preview": True,
        "mastering_full_length_download": True,
        "mastering_unlimited_revisions": False,
        "bulk_mastering_unlocked": False,
        "studio_standard_stems_unlocked": True,
        "studio_hifi_plus_stems_unlocked": False,
        "studio_chord_lyric_ai_unlocked": True,
        "studio_pitch_time_export_unlocked": True,
        "smart_links_unlocked": True,
        "mv_character_ref_cap": 1,
        "mv_auto_qc": False,
        "mv_deep_research": False,
        "mv_resolutions": ("480P", "768P"),
        "shoots_ai_clones_unlocked": True,
    },
    "creator": {
        "tier": "creator",
        "requires_auth": True,
        "free_tools_unlocked": True,
        "mastering_preview": True,
        "mastering_full_length_download": True,
        "mastering_unlimited_revisions": True,
        "bulk_mastering_unlocked": False,
        "studio_standard_stems_unlocked": True,
        "studio_hifi_plus_stems_unlocked": True,
        "studio_chord_lyric_ai_unlocked": True,
        "studio_pitch_time_export_unlocked": True,
        "smart_links_unlocked": True,
        "mv_character_ref_cap": 1,
        "mv_auto_qc": False,
        "mv_deep_research": False,
        "mv_resolutions": ("480P", "768P"),
        "shoots_ai_clones_unlocked": True,
    },
    "director": {
        "tier": "director",
        "requires_auth": True,
        "free_tools_unlocked": True,
        "mastering_preview": True,
        "mastering_full_length_download": True,
        "mastering_unlimited_revisions": True,
        "bulk_mastering_unlocked": True,
        "studio_standard_stems_unlocked": True,
        "studio_hifi_plus_stems_unlocked": True,
        "studio_chord_lyric_ai_unlocked": True,
        "studio_pitch_time_export_unlocked": True,
        "smart_links_unlocked": True,
        "mv_character_ref_cap": 2,
        "mv_auto_qc": True,
        "mv_deep_research": False,
        "mv_resolutions": ("480P", "768P"),
        "shoots_ai_clones_unlocked": True,
    },
    "studio": {
        "tier": "studio",
        "requires_auth": True,
        "free_tools_unlocked": True,
        "mastering_preview": True,
        "mastering_full_length_download": True,
        "mastering_unlimited_revisions": True,
        "bulk_mastering_unlocked": True,
        "studio_standard_stems_unlocked": True,
        "studio_hifi_plus_stems_unlocked": True,
        "studio_chord_lyric_ai_unlocked": True,
        "studio_pitch_time_export_unlocked": True,
        "smart_links_unlocked": True,
        "mv_character_ref_cap": 3,
        "mv_auto_qc": True,
        "mv_deep_research": True,
        "mv_resolutions": ("480P", "768P"),
        "shoots_ai_clones_unlocked": True,
    },
}

# ---------------------------------------------------------------------------
# LAME Gapless MP3 Calibration Constants (Studio Modules 590-591)
# ---------------------------------------------------------------------------
MP3_SAMPLES_PER_FRAME_MPEG1: Final[int] = 1152
MP3_SAMPLES_PER_FRAME_MPEG2: Final[int] = 576
MP3_DEFAULT_DECODER_DELAY_SAMPLES: Final[int] = 529
LAME_DEFAULT_ENCODER_DELAY_SAMPLES: Final[int] = 576
LAME_TOTAL_PRIMING_SAMPLES: Final[int] = 1105  # 576 encoder + 529 decoder delay

# ---------------------------------------------------------------------------
# Studio Practice Mode Chord Intervals & Standard Tunings (Module 534)
# ---------------------------------------------------------------------------
CHORD_QUALITY_INTERVALS: Final[dict[str, tuple[int, ...]]] = {
    "": (0, 4, 7),
    "maj": (0, 4, 7),
    "M": (0, 4, 7),
    "m": (0, 3, 7),
    "min": (0, 3, 7),
    "-": (0, 3, 7),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "M7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10),
    "min7": (0, 3, 7, 10),
    "mM7": (0, 3, 7, 11),
    "dim": (0, 3, 6),
    "dim7": (0, 3, 6, 9),
    "m7b5": (0, 3, 6, 10),
    "aug": (0, 4, 8),
    "+": (0, 4, 8),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "7sus4": (0, 5, 7, 10),
    "6": (0, 4, 7, 9),
    "m6": (0, 3, 7, 9),
    "9": (0, 4, 7, 10, 14),
    "maj9": (0, 4, 7, 11, 14),
    "m9": (0, 3, 7, 10, 14),
    "add9": (0, 4, 7, 14),
    "5": (0, 7),
}

# Standard Guitar 6-string open-position voicing templates: (E2, A2, D3, G3, B3, E4)
# -1 = muted string, 0 = open string
class ChordShape(TypedDict):
    frets: tuple[int, ...]
    fingers: tuple[int, ...]
    base_fret: int
    barres: tuple[int, ...]


class ToolPack(TypedDict):
    pack_id: str
    title: str
    min_tier: str
    always_pinned: bool
    summary: str
    tools: tuple[str, ...]


GUITAR_CHORD_SHAPES: Final[dict[str, ChordShape]] = {
    "C": {"frets": (-1, 3, 2, 0, 1, 0), "fingers": (0, 3, 2, 0, 1, 0), "base_fret": 1, "barres": ()},
    "Cm": {"frets": (-1, 3, 5, 5, 4, 3), "fingers": (0, 1, 3, 4, 2, 1), "base_fret": 3, "barres": (3,)},
    "C7": {"frets": (-1, 3, 2, 3, 1, 0), "fingers": (0, 3, 2, 4, 1, 0), "base_fret": 1, "barres": ()},
    "Cmaj7": {"frets": (-1, 3, 2, 0, 0, 0), "fingers": (0, 3, 2, 0, 0, 0), "base_fret": 1, "barres": ()},
    "Cm7": {"frets": (-1, 3, 5, 3, 4, 3), "fingers": (0, 1, 3, 1, 2, 1), "base_fret": 3, "barres": (3,)},
    "D": {"frets": (-1, -1, 0, 2, 3, 2), "fingers": (0, 0, 0, 1, 3, 2), "base_fret": 1, "barres": ()},
    "Dm": {"frets": (-1, -1, 0, 2, 3, 1), "fingers": (0, 0, 0, 2, 3, 1), "base_fret": 1, "barres": ()},
    "D7": {"frets": (-1, -1, 0, 2, 1, 2), "fingers": (0, 0, 0, 2, 1, 3), "base_fret": 1, "barres": ()},
    "Dmaj7": {"frets": (-1, -1, 0, 2, 2, 2), "fingers": (0, 0, 0, 1, 1, 1), "base_fret": 1, "barres": (2,)},
    "Dm7": {"frets": (-1, -1, 0, 2, 1, 1), "fingers": (0, 0, 0, 2, 1, 1), "base_fret": 1, "barres": (1,)},
    "E": {"frets": (0, 2, 2, 1, 0, 0), "fingers": (0, 2, 3, 1, 0, 0), "base_fret": 1, "barres": ()},
    "Em": {"frets": (0, 2, 2, 0, 0, 0), "fingers": (0, 2, 3, 0, 0, 0), "base_fret": 1, "barres": ()},
    "E7": {"frets": (0, 2, 0, 1, 0, 0), "fingers": (0, 2, 0, 1, 0, 0), "base_fret": 1, "barres": ()},
    "Emaj7": {"frets": (0, 2, 1, 1, 0, 0), "fingers": (0, 3, 1, 2, 0, 0), "base_fret": 1, "barres": ()},
    "Em7": {"frets": (0, 2, 0, 0, 0, 0), "fingers": (0, 2, 0, 0, 0, 0), "base_fret": 1, "barres": ()},
    "F": {"frets": (1, 3, 3, 2, 1, 1), "fingers": (1, 3, 4, 2, 1, 1), "base_fret": 1, "barres": (1,)},
    "Fm": {"frets": (1, 3, 3, 1, 1, 1), "fingers": (1, 3, 4, 1, 1, 1), "base_fret": 1, "barres": (1,)},
    "Fmaj7": {"frets": (-1, -1, 3, 2, 1, 0), "fingers": (0, 0, 3, 2, 1, 0), "base_fret": 1, "barres": ()},
    "G": {"frets": (3, 2, 0, 0, 0, 3), "fingers": (2, 1, 0, 0, 0, 3), "base_fret": 1, "barres": ()},
    "Gm": {"frets": (3, 5, 5, 3, 3, 3), "fingers": (1, 3, 4, 1, 1, 1), "base_fret": 3, "barres": (3,)},
    "G7": {"frets": (3, 2, 0, 0, 0, 1), "fingers": (3, 2, 0, 0, 0, 1), "base_fret": 1, "barres": ()},
    "Gmaj7": {"frets": (3, 2, 0, 0, 0, 2), "fingers": (3, 1, 0, 0, 0, 2), "base_fret": 1, "barres": ()},
    "A": {"frets": (-1, 0, 2, 2, 2, 0), "fingers": (0, 0, 1, 2, 3, 0), "base_fret": 1, "barres": ()},
    "Am": {"frets": (-1, 0, 2, 2, 1, 0), "fingers": (0, 0, 2, 3, 1, 0), "base_fret": 1, "barres": ()},
    "A7": {"frets": (-1, 0, 2, 0, 2, 0), "fingers": (0, 0, 2, 0, 3, 0), "base_fret": 1, "barres": ()},
    "Amaj7": {"frets": (-1, 0, 2, 1, 2, 0), "fingers": (0, 0, 2, 1, 3, 0), "base_fret": 1, "barres": ()},
    "Am7": {"frets": (-1, 0, 2, 0, 1, 0), "fingers": (0, 0, 2, 0, 1, 0), "base_fret": 1, "barres": ()},
    "B": {"frets": (-1, 2, 4, 4, 4, 2), "fingers": (0, 1, 2, 3, 4, 1), "base_fret": 2, "barres": (2,)},
    "Bm": {"frets": (-1, 2, 4, 4, 3, 2), "fingers": (0, 1, 3, 4, 2, 1), "base_fret": 2, "barres": (2,)},
    "B7": {"frets": (-1, 2, 1, 2, 0, 2), "fingers": (0, 2, 1, 3, 0, 4), "base_fret": 1, "barres": ()},
    "Bb": {"frets": (-1, 1, 3, 3, 3, 1), "fingers": (0, 1, 2, 3, 4, 1), "base_fret": 1, "barres": (1,)},
    "Eb": {"frets": (-1, 6, 8, 8, 8, 6), "fingers": (0, 1, 2, 3, 4, 1), "base_fret": 6, "barres": (6,)},
    "Ab": {"frets": (4, 6, 6, 5, 4, 4), "fingers": (1, 3, 4, 2, 1, 1), "base_fret": 4, "barres": (4,)},
}

# Standard Ukulele 4-string voicing templates: (G4, C4, E4, A4)
UKULELE_CHORD_SHAPES: Final[dict[str, ChordShape]] = {
    "C": {"frets": (0, 0, 0, 3), "fingers": (0, 0, 0, 3), "base_fret": 1, "barres": ()},
    "Cm": {"frets": (0, 3, 3, 3), "fingers": (0, 1, 2, 3), "base_fret": 1, "barres": ()},
    "C7": {"frets": (0, 0, 0, 1), "fingers": (0, 0, 0, 1), "base_fret": 1, "barres": ()},
    "Cmaj7": {"frets": (0, 0, 0, 2), "fingers": (0, 0, 0, 2), "base_fret": 1, "barres": ()},
    "D": {"frets": (2, 2, 2, 0), "fingers": (1, 2, 3, 0), "base_fret": 1, "barres": ()},
    "Dm": {"frets": (2, 2, 1, 0), "fingers": (2, 3, 1, 0), "base_fret": 1, "barres": ()},
    "D7": {"frets": (2, 2, 2, 3), "fingers": (1, 1, 1, 2), "base_fret": 1, "barres": (2,)},
    "E": {"frets": (4, 4, 4, 2), "fingers": (2, 3, 4, 1), "base_fret": 1, "barres": ()},
    "Em": {"frets": (0, 4, 3, 2), "fingers": (0, 3, 2, 1), "base_fret": 1, "barres": ()},
    "E7": {"frets": (1, 2, 0, 2), "fingers": (1, 2, 0, 3), "base_fret": 1, "barres": ()},
    "F": {"frets": (2, 0, 1, 0), "fingers": (2, 0, 1, 0), "base_fret": 1, "barres": ()},
    "Fm": {"frets": (1, 0, 1, 3), "fingers": (1, 0, 2, 4), "base_fret": 1, "barres": ()},
    "G": {"frets": (0, 2, 3, 2), "fingers": (0, 1, 3, 2), "base_fret": 1, "barres": ()},
    "Gm": {"frets": (0, 2, 3, 1), "fingers": (0, 2, 3, 1), "base_fret": 1, "barres": ()},
    "G7": {"frets": (0, 2, 1, 2), "fingers": (0, 2, 1, 3), "base_fret": 1, "barres": ()},
    "A": {"frets": (2, 1, 0, 0), "fingers": (2, 1, 0, 0), "base_fret": 1, "barres": ()},
    "Am": {"frets": (2, 0, 0, 0), "fingers": (2, 0, 0, 0), "base_fret": 1, "barres": ()},
    "A7": {"frets": (0, 1, 0, 0), "fingers": (0, 1, 0, 0), "base_fret": 1, "barres": ()},
    "B": {"frets": (4, 3, 2, 2), "fingers": (3, 2, 1, 1), "base_fret": 1, "barres": (2,)},
    "Bm": {"frets": (4, 2, 2, 2), "fingers": (3, 1, 1, 1), "base_fret": 1, "barres": (2,)},
    "Bb": {"frets": (3, 2, 1, 1), "fingers": (3, 2, 1, 1), "base_fret": 1, "barres": (1,)},
}

# ---------------------------------------------------------------------------
# Progressive MCP Tool Discovery Packs & Account-Tier Pack Mapping
# ---------------------------------------------------------------------------
MCP_TOOL_PACKS: Final[dict[str, ToolPack]] = {
    "core": {
        "pack_id": "core",
        "title": "Pinned Entry & Discovery Pack",
        "min_tier": "free",
        "always_pinned": True,
        "summary": "Progressive tool search/activation, free LUFS/True-Peak loudness analyzer, OAuth 2.1 login, and account entitlement inspector.",
        "tools": (
            "soundboost_discover_tools",
            "audit_audio_loudness_and_release_readiness",
            "soundboost_auth_status",
            "soundboost_oauth_and_vault",
            "soundboost_account_profile_and_billing",
        ),
    },
    "free_tools": {
        "pack_id": "free_tools",
        "title": "Free Audio Utilities & REAPER Bundle Pack",
        "min_tier": "free",
        "always_pinned": False,
        "summary": "ID3v2.3/ISRC tag reader/writer, Converta hi-res format converter, platform specs, and REAPER JSFX/ReaScript installer.",
        "tools": (
            "read_release_metadata_tags",
            "write_release_metadata_tags",
            "soundboost_converta_audio",
            "list_soundboost_platform_capabilities",
            "install_reaper_jsfx_and_scripts",
        ),
    },
    "studio_harmony": {
        "pack_id": "studio_harmony",
        "title": "Studio Chords, Lyrics, Voicings & Loop Grid Pack",
        "min_tier": "free",
        "always_pinned": False,
        "summary": "AI chord recognition & word-synced lyric lead sheets, Guitar/Ukulele/Piano voicing diagrams, cloud stem pitch/time shifter, and bar.beat.16th loop quantizer.",
        "tools": (
            "soundboost_studio_chords_and_lyrics",
            "generate_printable_lead_sheet",
            "studio_practice_voicings_and_cloud_shifter",
            "transpose_and_quantize_musical_grid",
        ),
    },
    "studio_stems": {
        "pack_id": "studio_stems",
        "title": "Studio Stem Splitter, 5-Piece Drum Kit & YouTube/SC Ingest Pack",
        "min_tier": "free",
        "always_pinned": False,
        "summary": "2/4/6-stem neural separation, Voice Cleaner, 5-Piece Drum Kit sub-stems (Plus), Studio->Mastering handoff, and direct YouTube/SoundCloud ingestion.",
        "tools": (
            "soundboost_studio_stems_and_drums",
            "soundboost_youtube_and_soundcloud_ingest",
        ),
    },
    "distribution": {
        "pack_id": "distribution",
        "title": "Smart Links (`soundboo.st`) & Release Distribution Pack",
        "min_tier": "free",
        "always_pinned": False,
        "summary": "Artist link-in-bio Smart Links (`soundboo.st/<slug>`) with auto-scraped DSP links and release metadata.",
        "tools": (
            "soundboost_smart_links",
            "soundboost_remove_smart_link",
        ),
    },
    "mastering": {
        "pack_id": "mastering",
        "title": "Cloud AI Mastering, Voice/Toki Prompt & Track Telemetry Pack",
        "min_tier": "pay_as_you_go",
        "always_pinned": False,
        "summary": "Pay-gated 8-mode Cloud AI Mastering, Mix Doctor, Audio Enhancer, Deepgram Voice + Toki AI prompt mastering, pre/post master stats, and stem-prep.",
        "tools": (
            "soundboost_upload_and_master",
            "soundboost_voice_and_prompt_assistant",
            "soundboost_track_lifecycle_and_telemetry",
            "soundboost_delete_tracks",
        ),
    },
    "bulk_mastering": {
        "pack_id": "bulk_mastering",
        "title": "Cloud Bulk & Album Mastering Pack (Unlimited Plus / Director / Studio)",
        "min_tier": "unlimited_plus",
        "always_pinned": False,
        "summary": "Batch album/EP cloud mastering (`/api/bulk-mastering/*`) with unified target LUFS and ZIP export.",
        "tools": (
            "soundboost_bulk_and_album_mastering",
        ),
    },
    "music_video_shoots": {
        "pack_id": "music_video_shoots",
        "title": "AI Music Video (`h3-max`), Shoots Director Chat & Text-to-Audio Pack",
        "min_tier": "pay_as_you_go",
        "always_pinned": False,
        "summary": "AI Music Video (`h3-max`), `Shoots` Director Chat (`/api/shoots/{id}/director-message`), scene regeneration, 9:16 Spotify Canvas, credit budget calculator, and Text-to-Audio.",
        "tools": (
            "soundboost_music_video_and_shoots_director",
            "soundboost_text_to_audio_and_mv_budget",
        ),
    },
    "account_admin": {
        "pack_id": "account_admin",
        "title": "Account Profile Edits & Subscription Billing Changes Pack",
        "min_tier": "free",
        "always_pinned": False,
        "summary": "Destructive account changes: profile edits, Stripe checkout/portal sessions, and pausing or resuming the subscription.",
        "tools": (
            "soundboost_account_and_subscription_changes",
        ),
    },
}

# Exact MCP Tool Packs unlocked by each SoundBoost account level
TIER_UNLOCKED_MCP_PACKS: Final[dict[str, tuple[str, ...]]] = {
    "free": ("core", "free_tools", "studio_harmony", "studio_stems", "distribution", "account_admin"),
    "pay_as_you_go": ("core", "free_tools", "studio_harmony", "studio_stems", "distribution", "account_admin", "mastering", "music_video_shoots"),
    "starter": ("core", "free_tools", "studio_harmony", "studio_stems", "distribution", "account_admin", "mastering", "music_video_shoots"),
    "unlimited": ("core", "free_tools", "studio_harmony", "studio_stems", "distribution", "account_admin", "mastering", "music_video_shoots"),
    "creator": ("core", "free_tools", "studio_harmony", "studio_stems", "distribution", "account_admin", "mastering", "music_video_shoots"),
    "director": ("core", "free_tools", "studio_harmony", "studio_stems", "distribution", "account_admin", "mastering", "bulk_mastering", "music_video_shoots"),
    "unlimited_plus": ("core", "free_tools", "studio_harmony", "studio_stems", "distribution", "account_admin", "mastering", "bulk_mastering", "music_video_shoots"),
    "studio": ("core", "free_tools", "studio_harmony", "studio_stems", "distribution", "account_admin", "mastering", "bulk_mastering", "music_video_shoots"),
}


