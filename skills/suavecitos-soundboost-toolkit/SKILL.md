---
name: suavecitos-soundboost-toolkit
description: Unified CLI (soundboost), FastMCP Server (soundboost-mcp), OAuth 2.1 & Bearer-Token API Client, Local ITU-R BS.1770-4 / EBU R128 DSP Engine, 18-Band Mastering EQ, ID3v2.3/ISRC Tagging Engine, and Deep REAPER 7.82 (gmem/OSC/HTTP/RPP) Suite for SoundBoost.ai (Diktatorial Suite).
---

# Suavecito's Soundboost.AI Toolkit

Use this skill whenever the user wants to:
1. Audit audio files for ITU-R BS.1770-4 Integrated/Short-Term/Momentary LUFS, Loudness Range (`LRA` via $P_{95}-P_{10}$), 4x oversampled Inter-Sample True Peak (`dBTP`) with 100ms binary-search peak timestamp localization, stereo phase correlation, or Streaming Loudness Penalties across Spotify, Apple Music, YouTube, Tidal, Amazon Music, Deezer, SoundCloud, Pandora, and Club/DJ systems.
2. Render local or cloud AI Masters using SoundBoost's 18-Band Graphic EQ (`20 Hz` to `15 kHz` or any of the 12 genre presets), Mid/Side stereo width (`0–200%`), target LUFS normalization, lookahead True-Peak brickwall limiting (`-1.0 dBTP` for DistroKid), and 16/24-bit TPDF dithering.
3. Separate tracks in `studio.soundboost.ai` (`standard_2stem`, `standard_4stem`, `hifi_4stem`, `hifi_6stem`, `voice_clean` `Voice` + `Artifacts`), split drums into the **5-Piece Drum Kit** (`Kick, Snare, Toms, Hi-Hat, Cymbals`), extract beat-aligned chords and word-synced lyrics, generate printable HTML/PDF Lead Sheets, or import stems into REAPER.
4. Convert audio formats via `converta.soundboost.ai` (`wav`, `aiff`, `flac`, `ogg`, `aac`, `m4a`, `mp3`), manage `https://soundboo.st/<slug>` Smart Links, generate Text-to-Audio SFX, or write ID3v2.3 / FLAC / WAV metadata with validated 12-character ISO 3901 ISRC codes (`TSRC`) and `3000x3000` cover art.
5. Control **REAPER 7.82 (x64)** via `options:gmem=SuavecitoSoundBoost` JSFX shared memory, Loopback HTTP Web Control Surface (`127.0.0.1:8080`), UDP Open Sound Control (`127.0.0.1:9000`), `Suavecito_Live_Bridge_Daemon.lua` (`__startup.lua`), 12 Genre `.RfxChain` presets, `Suavecito_SoundBoost_9x16_Canvas_Visualizer.txt` (`reaper_video.dll`), and `.RPP` multitrack session generation.

## Available MCP Tools (`suavecitos-soundboost-toolkit` — 22 Tools across 8 Progressive Packs)

- `soundboost_discover_tools`
- `audit_audio_loudness_and_release_readiness`
- `soundboost_oauth_and_vault`
- `soundboost_account_profile_and_billing`
- `read_release_metadata_tags`
- `write_release_metadata_tags`
- `soundboost_converta_audio`
- `list_soundboost_platform_capabilities`
- `install_reaper_jsfx_and_scripts`
- `soundboost_studio_chords_and_lyrics`
- `generate_printable_lead_sheet`
- `studio_practice_voicings_and_cloud_shifter`
- `transpose_and_quantize_musical_grid`
- `soundboost_studio_stems_and_drums`
- `soundboost_youtube_and_soundcloud_ingest`
- `soundboost_smart_links`
- `soundboost_upload_and_master`
- `soundboost_voice_and_prompt_assistant`
- `soundboost_track_lifecycle_and_telemetry`
- `soundboost_bulk_and_album_mastering`
- `soundboost_music_video_and_shoots_director`
- `soundboost_text_to_audio_and_mv_budget`
