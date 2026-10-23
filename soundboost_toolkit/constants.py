"""Platform constants, DSP tables, separation profiles, and protocol defaults."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

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
# Public Client-Side Protocol Parameters (Studio Metro Modules 444, 446, 616)
# Overridable via environment variables:
#   SOUNDBOOST_UI_API_KEY, SOUNDBOOST_TOKEN_CIPHER, SOUNDBOOST_COOKIE_CIPHER,
#   SOUNDBOOST_PARAM_CIPHER, SOUNDBOOST_PARAM_IV
# ---------------------------------------------------------------------------
DEFAULT_UI_HEADER_VAL: Final[str] = "dk-654321-jhgpol-2789456-ghysvn-bkjsqb"
DEFAULT_TOKEN_CIPHER_VAL: Final[str] = "1837837__LEBlebi"
DEFAULT_COOKIE_CIPHER_VAL: Final[str] = "inanilmaz_gizli_bir_anahtar"
DEFAULT_PARAM_CIPHER_VAL: Final[str] = "siradan__anahtar"
DEFAULT_PARAM_IV_HEX: Final[str] = "101112131415161718191a1b1c1d1e1f"

PRODUCTION_KAT_ID: Final[int] = 1
PRODUCTION_KAT_HASH: Final[str] = "Hck7LVPBFsyNC95Qzf6-0Q"

USER_COOKIE_KEYS: Final[tuple[str, ...]] = ("_d_i", "_d_e", "_d_a", "_d_u")

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
# AI Music Video (h3-max) Tier Character Reference Caps
# ---------------------------------------------------------------------------
MV_CHARACTER_REF_CAPS: Final[dict[str, int]] = {
    "starter": 1,
    "creator": 1,
    "director": 2,
    "studio": 3,
}
