# Suavecito's Soundboost.AI Toolkit

Unified **CLI (`soundboost`)**, **FastMCP Server (`soundboost-mcp`)**, **OAuth 2.1 & Bearer-Token API Client**, **Local ITU-R BS.1770-4 / EBU R128 DSP Engine**, **ID3v2.3 & ISO 3901 ISRC Tagging Engine**, and **REAPER JSFX / ReaScript Suite** for the **SoundBoost.ai (`Diktatorial Suite`)** ecosystem.

No embedded LLM, no hardcoded personal credentials, no telemetry: **the caller is the model**.

---

## Architecture & Ecosystem Coverage

This toolkit implements both **full cloud API orchestration** across all 4 SoundBoost microservices (`api.soundboost.ai`, `mastering.soundboost.ai`, `studio.soundboost.ai`, `converta.soundboost.ai`, plus `shoots.soundboost.ai` SSO) and **100% offline local studio DSP equivalents**:

| Subsystem | Cloud API / Microservice | Local Offline DSP / Protocol Equivalent |
| :--- | :--- | :--- |
| **8-Mode AI Mastering & 18-Band EQ** | `POST /api/tracks/{id}/master/` (`prompt`, `preset`, `auto`, `reference`, `chat`, `manual`, `mix_doctor`, `enhance`) | RBJ constant-$Q$ 18-band biquad cascade (`20, 30, 45, 70, 100, 140, 200, 315, 500, 700, 1000, 1400, 2000, 3150, 5000, 7000, 10000, 15000 Hz`, $\pm 6.0\text{ dB}$), Mid/Side stereo width (`0–200%`), lookahead True-Peak brickwall limiter, and 16/24-bit TPDF dither |
| **Multitrack Stem Studio & Voice Cleaner** | `POST /api/studio/originals/{id}/start/`, `/voice-clean/start/` (`standard_2stem`, `standard_4stem`, `hifi_4stem`, `hifi_6stem`) | R2 storage proxy failover (`x-storage-proxy-host` / `x-storage-direct-host`), and direct `loop`/`stems`/`mix` Mastering handoff |
| **5-Piece Drum Kit Sub-Stem Splitter** | `POST /api/studio/originals/{id}/drums/start/` & `GET .../drums/` | Splits `Drums` stem into **5 discrete sub-channels**: `Kick`, `Snare`, `Toms`, `Hi-Hat`, and `Cymbals` |
| **AI Chords, Beat Grid & Lead Sheet** | `POST /api/studio/originals/{id}/chords/start/`, `/lyrics/start/`, `/lyrics/edit/` | Slash-aware chord transposer, `30ms` binary-search beat snapper, `Bar.Beat.1/16` loop quantizer, `3.0s` instrumental chord-gap injector, and printable HTML/PDF Lead Sheet generator |
| **ITU-R BS.1770-4 Loudness & 100ms True-Peak** | WebAssembly `ebur128` LUFS Meter & Loudness Penalty | Bilinear K-weighted Integrated/Short-Term/Momentary LUFS, $P_{95}-P_{10}$ LRA, 4x oversampled True Peak (`dBTP`) with **100ms-precision binary-search peak timestamp locator**, and 9-platform Streaming Loudness Penalty table |
| **Converta Hi-Res Converter** | `GET /api/v1/formats`, `POST /api/v1/convert`, `POST /api/v1/stem-export` | Supports `wav` (`16/24/32-bit`), `aiff` (`16/24/32-bit`), `flac` (`16/24-bit`), `ogg`, `aac`, `m4a`, `mp3` at `44.1 / 48 kHz` with dithering |
| **ID3v2.3 & ISRC (`TSRC`) Release Tagger** | Online ID3v2.3 Tag Editor | Reads/writes `TIT2`, `TPE1`, `TALB`, `TYER`, `TRCK`, `TCON`, `TBPM`, `TKEY`, `TPUB`, `COMM`, `USLT`, `APIC` (`3000x3000` cover art), and ISO 3901 validated 12-char `TSRC` (ISRC) on MP3, FLAC, and WAV |
| **Smart Links & AI Music Video (`h3-max`)** | `/api/smart-links/` (`soundboo.st`), `/api/audio/generate/`, `/api/shoots/` | Auto-resolves DSP metadata, publishes `https://soundboo.st/<slug>` landing pages, computes `h3-max` retake credit spending caps, and generates beat-locked `Shoots` storyboards from REAPER audio |
| **Account-Tier Feature Entitlements** | `GET /api/user-info/`, `GET /api/billing/info` | Dynamically resolves exact plan entitlements (`free`, `pay_as_you_go`, `starter`, `unlimited`, `creator`, `director`, `unlimited_plus`, `studio`) and enforces per-tier gates |
| **Progressive MCP Tool Discovery** | `soundboost_discover_tools` (`CatalogTransform` + BM25) | Exposes **4 pinned entry tools** by default (~82% context reduction) and dynamically activates any of **8 modular tool packs** via BM25 search, pack name, or `sync_account_tier` |
| **Deep REAPER 7.82 & Video Pipeline** | — | `SoundBoost_18Band_Mastering_EQ_Monitor.jsfx` (`options:gmem=SuavecitoSoundBoost`), `Suavecito_Live_Bridge_Daemon.lua` (`__startup.lua` + File Mailbox IPC), Loopback HTTP (`8080`) + UDP OSC (`9000`), 12 `.RfxChain` Genre Presets, `.RPP` Multitrack & Video Session Generator (`<VIDEO_EFFECT>`), and Audio-Reactive `.MP4` Video Renderer |

---

## MCP Tools (`soundboost-mcp` — MCP `2026-07-28` Specification — 22 Tools across 8 Progressive Packs)

By default, `list_tools()` exposes **4 pinned entry tools** (`soundboost_discover_tools`, `audit_audio_loudness_and_release_readiness`, `soundboost_oauth_and_vault`, `soundboost_account_profile_and_billing`) so agent context windows stay compact, while **`get_tool()`** allows calling any of the 22 tools directly or unlocking packs (`core`, `free_tools`, `studio_harmony`, `studio_stems`, `distribution`, `mastering`, `bulk_mastering`, `music_video_shoots`) at runtime with `ToolListChangedNotification`.

Every tool includes full **`ToolAnnotations`** (`title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`), semantic `tags`, and structured JSON `outputSchema`. All long-running DSP and cloud tools also register **`TasksExtension` (`io.modelcontextprotocol/tasks`)** with `task=TaskConfig(mode="optional")`, streaming live **`Context.report_progress`** notifications in both background-task and foreground execution modes.

| Tool | Annotations (`readOnly` / `openWorld`) | Background Task (`task=True`) | Description |
| :--- | :---: | :---: | :--- |
| **`soundboost_discover_tools`** | `readOnly=True`, `openWorld=False` | Sync | **Pinned Discovery Gateway:** List the 8 tool packs, run BM25 natural-language search across all 22 tools, activate/deactivate packs, or `sync_account_tier` to auto-unlock packs matching the user's live SoundBoost subscription. |
| **`audit_audio_loudness_and_release_readiness`** | `readOnly=True`, `openWorld=False` | Sync | **Free Tool:** Full ITU-R BS.1770-4 Integrated/Short-Term/Momentary LUFS, $P_{95}-P_{10}$ LRA, 4x oversampled True Peak (`dBTP`) + 100ms binary-search timestamp, stereo correlation, 6-band spectral profile, 9-platform streaming penalty matrix, and DistroKid readiness verdict. |
| **`read_release_metadata_tags`** | `readOnly=True`, `openWorld=False` | Sync | **Free Tool:** Read ID3v2.3 / FLAC / WAV metadata tags including 12-character ISRC (`TSRC`), BPM, Key, Publisher, Lyrics, and Cover Art status. |
| **`write_release_metadata_tags`** | `readOnly=False`, `openWorld=False` | Sync | **Free Tool:** Write release metadata tags with ISO 3901 12-character ISRC validation (`CC-XXX-YY-NNNNN`), unsynchronized lyrics (`USLT`), and front cover art (`APIC`). |
| **`transpose_and_quantize_musical_grid`** | `readOnly=True`, `openWorld=False` | Sync | Transpose chord progressions, classify Pitch Shift zone (`Natural zone` $\pm 3\text{ st}$ vs `Creative zone`), snap timestamps to synchronized beats (`30ms` tolerance), and quantize loops to `Bar.Beat.1/16`. |
| **`generate_printable_lead_sheet`** | `readOnly=False`, `openWorld=False` | Sync | Merge word-level lyric segments and transposed chords (inserting `[Instrumental]` chord rows when vocal gaps $\ge 3.0\text{s}$) into a print-ready HTML Lead Sheet. |
| **`soundboost_upload_and_master`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | **Pay-Gated Cloud Endpoint:** Upload audio to `api.soundboost.ai` and run any of the 8 cloud Mastering modes (`prompt`, `preset`, `auto`, `reference`, `chat`, `manual`, `mix_doctor`, `enhance`), with optional `wait_for_completion` polling. |
| **`soundboost_studio_stems_and_drums`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | **Pay-Gated Cloud Endpoint:** Start/poll Studio 2/4/6-stem separation (`htdemucs`, `htdemucs_6s`), Voice Cleaner (`Voice` + `Artifacts`), **5-Piece Drum Kit sub-stem separation** (`Kick, Snare, Toms, Hi-Hat, Cymbals`), or hand off `loop`/`stems`/`mix` to Mastering. |
| **`soundboost_studio_chords_and_lyrics`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | **Pay-Gated Cloud Endpoint:** Start, poll, or edit Studio AI Chord Recognition and Word-Synced Lyrics/Transcripts. |
| **`soundboost_converta_audio`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | **Free Tool Endpoint:** Query live `converta.soundboost.ai` format specs or run hi-res format conversions (`wav`, `aiff`, `flac`, `ogg`, `aac`, `m4a`, `mp3`). |
| **`soundboost_smart_links`** | `readOnly=False`, `openWorld=True` | Sync | **Pay-Gated Cloud Endpoint:** List, auto-scrape DSP metadata, create, update, and publish `https://soundboo.st/<slug>` Smart Links. |
| **`soundboost_text_to_audio_and_mv_budget`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | **Pay-Gated Cloud Endpoint:** Generate AI Text-to-Audio / SFX and compute AI Music Video (`h3-max`) credit spending caps (`ec(quote)` formula). |
| **`soundboost_oauth_and_vault`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | Manage Google OAuth 2.0 PKCE (`S256`), `POST /api/google-login/` token exchange, OS Keyring (`WinVaultKeyring` / macOS Keychain / SecretService), and local loopback browser session handoff. |
| **`soundboost_bulk_and_album_mastering`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | **Pay-Gated Cloud Endpoint:** Batch-master an entire Album/EP via SoundBoost Cloud (`/api/bulk-mastering/*` + `.zip` export). |
| **`soundboost_youtube_and_soundcloud_ingest`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | **Pay-Gated Cloud Endpoint:** Ingest tracks directly from YouTube or SoundCloud URLs into Mastering (`/api/tracks/import-youtube/`, `/import-soundcloud/`) or Studio Stem Splitter (`/api/studio/public/import-youtube`, `/import-soundcloud`). |
| **`soundboost_voice_and_prompt_assistant`** | `readOnly=False`, `openWorld=True` | Sync | **Pay-Gated Cloud Endpoint:** Translate natural-language or spoken mastering instructions via Cloud `Toki` (`POST /api/ai/prompt`) & `Deepgram` Voice Mastering (`/api/mastering-voice/*`). |
| **`soundboost_music_video_and_shoots_director`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | **Pay-Gated Cloud + REAPER Video Pipeline:** Orchestrate AI Music Video (`h3-max`) projects (`/api/music-videos/`), `Shoots` storyboard scenes (`/api/shoots/`), Director Chat, Auto-QC scene regeneration, Spotify 9:16 Canvas clips, `reaper_to_shoots_storyboard` beat-locked cue sheets, and `render_reaper_video_pipeline` audio-reactive `.mp4` rendering. |
| **`studio_practice_voicings_and_cloud_shifter`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | **Pay-Gated Cloud Stem Export + Client Helpers:** Export pitch-shifted/time-stretched stems via Cloud Studio (`POST /api/studio/originals/{id}/export/` & `POST /api/v1/stem-export`), generate interactive Chord Voicing & Fingering Diagrams (`guitar`, `ukulele`, `piano`, Module 534), and inspect binary LAME/Xing/Info Gapless MP3 headers (Modules 590–591). |
| **`soundboost_track_lifecycle_and_telemetry`** | `readOnly=False`, `openWorld=True` | Sync | **Pay-Gated Cloud Endpoint:** Fetch pre/post mastering telemetry (`/api/get-master-stats`), run stem-assisted mastering prep (`/api/mastering/stem-prep`), generate waveforms, manage cover art, toggle favorites, create shareable public links, and delete tracks. |
| **`soundboost_account_profile_and_billing`** | `readOnly=False`, `openWorld=True` | Sync | **Pay-Gated Cloud Endpoint:** Inspect live account-tier entitlements (`entitlements`, `all_tier_specs`), artist profiles (`/api/account/profile/`), username availability, social notifications, referrals, and Stripe subscription billing. |
| **`install_reaper_jsfx_and_scripts`** | `readOnly=False`, `openWorld=False` | Sync | **Deep REAPER 7.82 Suite:** Install `gmem` 18-Band Mastering EQ JSFX, Live Bridge Daemon (`__startup.lua`), 12 `.RfxChain` Genre Presets, & Video Processor preset; inspect plugins; push live 18-band EQ via `gmem`/OSC; generate `.RPP` multitrack + video sessions; and render audio-reactive `.mp4` videos. |
| **`list_soundboost_platform_capabilities`** | `readOnly=True`, `openWorld=False` | Sync | Return the complete specification matrix of EQ bands, presets, stem profiles, drum sub-stems, Converta formats, streaming loudness targets, and account tier entitlements. |

### MCP Resources & Prompts

- **`soundboost://specs/capabilities`** (`application/json`): Complete platform capability matrix.
- **`soundboost://specs/eq-presets`** (`application/json`): Exact 18-band dB gain arrays (`20 Hz` to `15 kHz`) for all 12 genre presets.
- **`soundboost://specs/reaper-jsfx`** (`text/plain`): Source code of the REAPER `SoundBoost_18Band_Mastering_EQ_Monitor.jsfx` plugin.
- **Prompt `distrokid_release_mastering_workflow`**: Step-by-step release engineering runbook for auditing, stem/drum-kit cleanup, 18-band mastering, and ISO 3901 ISRC tagging.

---

## CLI Usage (`soundboost`)

```bash
# 1. Free Tool: Run full ITU-R BS.1770-4 Loudness, 100ms True-Peak & DistroKid Release Audit
soundboost audit path/to/mix.wav

# 2. Pay-Gated Cloud AI Mastering: Upload & Master via SoundBoost Cloud (/api/tracks/{id}/master/)
soundboost master upload --file input.wav
soundboost master master --track-id 42 --mode preset --preset hip_hop --width 112 --lufs -14.0

# 3. Free Tool: Write ID3v2.3 / WAV / FLAC metadata with validated 12-char ISRC & 3000x3000 cover art
soundboost tags mastered_24bit.wav --title "Midnight Drive" --artist "Suavecito" --isrc "US-ABC-26-00001" --bpm 124 --key "Cm" --cover cover.jpg

# 4. Transpose chords, check Pitch Shift zone, and quantize loop to Bar.Beat.1/16
soundboost grid --chords "Cm7,Fm7,Abmaj7,G7b9/B" --semitones -2 --flats --bpm 128 --start 3.72 --end 11.28

# 5. Install REAPER 18-Band Mastering EQ JSFX & Multitrack Stem Importer ReaScript
soundboost reaper-install

# 6. Authenticate via Google OAuth 2.0 / Browser Loopback & Store in OS Keyring
soundboost auth status
soundboost auth browser-login
soundboost auth google-exchange --token "<google-id-or-access-token>"

# 7. Launch OAuth 2.1 Protected Streamable HTTP MCP Server
soundboost serve-mcp --transport http --port 8000 --oauth google

# 8. Pay-Gated Cloud Bulk Mastering (/api/bulk-mastering/*)
soundboost bulk-master start --tracks "42,43,44" --preset pop --lufs -14.0

# 9. Pay-Gated Cloud Toki Prompt Engine (POST /api/ai/prompt)
soundboost prompt-eq "warm 808 sub bass, clean muddy low-mids, crisp vocal presence, airy 15kHz top end" --track-id 42

# 10. Render Guitar / Ukulele / Piano Chord Voicing & Fingering Diagrams (Module 534)
soundboost voicings "Cm,Abmaj7,Eb,Bb" --instrument guitar

# 11. Pay-Gated Studio Cloud Pitch-Shift & Time-Stretch Stem Export (POST /api/studio/originals/{id}/export/)
soundboost shifter 42 --semitones 2 --speed 1.15 --format wav --bits 24

# 12. Inspect LAME/Xing/Info Gapless MP3 Headers & 1105-Sample Priming Delay
soundboost mp3-inspect stem_loop.mp3

# 13. Pay-Gated Cloud Ingestion: YouTube or SoundCloud Directly into Mastering or Studio Stem Splitter
soundboost ingest studio-youtube --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

---

## Configuration (`mcp_config.json`)

```json
{
  "mcpServers": {
    "suavecitos-soundboost-toolkit": {
      "command": "python",
      "args": ["E:\\Projects\\suavecitos-soundboost-toolkit\\server.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "SOUNDBOOST_ACCESS_TOKEN": "optional-bearer-token"
      }
    }
  }
}
```

### Environment Variables

| Variable | Description | Default |
| :--- | :--- | :--- |
| `SOUNDBOOST_ACCESS_TOKEN` | Bearer token for authenticated cloud calls | Loaded from OS Keyring or `~/.soundboost/credentials.json` if omitted |
| `SOUNDBOOST_GUEST_TOKEN` | Optional `x-studio-guest-token` for anonymous Studio sessions | Optional |

---

## License

Apache-2.0
