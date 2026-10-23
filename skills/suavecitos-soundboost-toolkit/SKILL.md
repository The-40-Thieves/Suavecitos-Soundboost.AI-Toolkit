---
name: suavecitos-soundboost-toolkit
description: Unified CLI (soundboost), FastMCP Server (soundboost-mcp), Cryptographic Protocol Client, Local ITU-R BS.1770-4 / EBU R128 DSP Engine, 18-Band Mastering EQ, ID3v2.3/ISRC Tagging Engine, and REAPER JSFX/ReaScript Suite for SoundBoost.ai (Diktatorial Suite).
---

# Suavecito's Soundboost.AI Toolkit

Use this skill whenever the user wants to:
1. Audit audio files for ITU-R BS.1770-4 Integrated/Short-Term/Momentary LUFS, Loudness Range (`LRA` via $P_{95}-P_{10}$), 4x oversampled Inter-Sample True Peak (`dBTP`) with 100ms binary-search peak timestamp localization, stereo phase correlation, or Streaming Loudness Penalties across Spotify, Apple Music, YouTube, Tidal, Amazon Music, Deezer, SoundCloud, Pandora, and Club/DJ systems.
2. Render local or cloud AI Masters using SoundBoost's 18-Band Graphic EQ (`20 Hz` to `15 kHz` or any of the 12 genre presets), Mid/Side stereo width (`0–200%`), target LUFS normalization, lookahead True-Peak brickwall limiting (`-1.0 dBTP` for DistroKid), and 16/24-bit TPDF dithering.
3. Separate tracks in `studio.soundboost.ai` (`standard_2stem`, `standard_4stem`, `hifi_4stem`, `hifi_6stem`, `voice_clean` `Voice` + `Artifacts`), split drums into the **5-Piece Drum Kit** (`Kick, Snare, Toms, Hi-Hat, Cymbals`), extract beat-aligned chords and word-synced lyrics, generate printable HTML/PDF Lead Sheets, or import stems into REAPER.
4. Convert audio formats via `converta.soundboost.ai` (`wav`, `aiff`, `flac`, `ogg`, `aac`, `m4a`, `mp3`), manage `https://soundboo.st/<slug>` Smart Links, generate Text-to-Audio SFX, or write ID3v2.3 / FLAC / WAV metadata with validated 12-character ISO 3901 ISRC codes (`TSRC`) and `3000x3000` cover art.

## Available MCP Tools (`suavecitos-soundboost-toolkit`)

- `audit_audio_loudness_and_release_readiness`
- `render_local_18band_master`
- `read_release_metadata_tags`
- `write_release_metadata_tags`
- `soundboost_cipher_tool`
- `transpose_and_quantize_musical_grid`
- `generate_printable_lead_sheet`
- `soundboost_upload_and_master`
- `soundboost_studio_stems_and_drums`
- `soundboost_studio_chords_and_lyrics`
- `soundboost_converta_audio`
- `soundboost_smart_links`
- `soundboost_text_to_audio_and_mv_budget`
- `install_reaper_jsfx_and_scripts`
- `list_soundboost_platform_capabilities`
