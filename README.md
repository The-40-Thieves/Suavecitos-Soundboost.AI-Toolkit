# Suavecito's Soundboost.AI Toolkit

Unified **CLI (`soundboost`)**, **FastMCP Server (`soundboost-mcp`)**, **Cryptographic Protocol Client**, **Local ITU-R BS.1770-4 / EBU R128 DSP Engine**, **ID3v2.3 & ISO 3901 ISRC Tagging Engine**, and **REAPER JSFX / ReaScript Suite** for the **SoundBoost.ai (`Diktatorial Suite`)** ecosystem.

No embedded LLM, no hardcoded personal credentials, no telemetry: **the caller is the model**.

---

## Architecture & Ecosystem Coverage

This toolkit implements both **full cloud API orchestration** across all 4 SoundBoost microservices (`api.soundboost.ai`, `mastering.soundboost.ai`, `studio.soundboost.ai`, `converta.soundboost.ai`, plus `shoots.soundboost.ai` SSO) and **100% offline local studio DSP equivalents**:

| Subsystem | Cloud API / Microservice | Local Offline DSP / Protocol Equivalent |
| :--- | :--- | :--- |
| **Cryptographic Auth & URL Cipher** | `POST /api/decrypt-param-static`, `/api/token` | AES-128-CBC `?track=` cipher (`KAT: 1 -> Hck7LVPBFsyNC95Qzf6-0Q`), time-windowed `ui-token` signer, and OpenSSL `Salted__` `_d_a` cookie decryptor |
| **8-Mode AI Mastering & 18-Band EQ** | `POST /api/tracks/{id}/master/` (`prompt`, `preset`, `auto`, `reference`, `chat`, `manual`, `mix_doctor`, `enhance`) | RBJ constant-$Q$ 18-band biquad cascade (`20, 30, 45, 70, 100, 140, 200, 315, 500, 700, 1000, 1400, 2000, 3150, 5000, 7000, 10000, 15000 Hz`, $\pm 6.0\text{ dB}$), Mid/Side stereo width (`0–200%`), lookahead True-Peak brickwall limiter, and 16/24-bit TPDF dither |
| **Multitrack Stem Studio & Voice Cleaner** | `POST /api/studio/originals/{id}/start/`, `/voice-clean/start/` (`standard_2stem`, `standard_4stem`, `hifi_4stem`, `hifi_6stem`) | Signed Studio deep-link generator, R2 storage proxy failover (`x-storage-proxy-host` / `x-storage-direct-host`), and direct `loop`/`stems`/`mix` Mastering handoff |
| **5-Piece Drum Kit Sub-Stem Splitter** | `POST /api/studio/originals/{id}/drums/start/` & `GET .../drums/` | Splits `Drums` stem into **5 discrete sub-channels**: `Kick`, `Snare`, `Toms`, `Hi-Hat`, and `Cymbals` |
| **AI Chords, Beat Grid & Lead Sheet** | `POST /api/studio/originals/{id}/chords/start/`, `/lyrics/start/`, `/lyrics/edit/` | Slash-aware chord transposer, `30ms` binary-search beat snapper, `Bar.Beat.1/16` loop quantizer, `3.0s` instrumental chord-gap injector, and printable HTML/PDF Lead Sheet generator |
| **ITU-R BS.1770-4 Loudness & 100ms True-Peak** | WebAssembly `ebur128` LUFS Meter & Loudness Penalty | Bilinear K-weighted Integrated/Short-Term/Momentary LUFS, $P_{95}-P_{10}$ LRA, 4x oversampled True Peak (`dBTP`) with **100ms-precision binary-search peak timestamp locator**, and 9-platform Streaming Loudness Penalty table |
| **Converta Hi-Res Converter** | `GET /api/v1/formats`, `POST /api/v1/convert`, `POST /api/v1/stem-export` | Supports `wav` (`16/24/32-bit`), `aiff` (`16/24/32-bit`), `flac` (`16/24-bit`), `ogg`, `aac`, `m4a`, `mp3` at `44.1 / 48 kHz` with dithering |
| **ID3v2.3 & ISRC (`TSRC`) Release Tagger** | Online ID3v2.3 Tag Editor | Reads/writes `TIT2`, `TPE1`, `TALB`, `TYER`, `TRCK`, `TCON`, `TBPM`, `TKEY`, `TPUB`, `COMM`, `USLT`, `APIC` (`3000x3000` cover art), and ISO 3901 validated 12-char `TSRC` (ISRC) on MP3, FLAC, and WAV |
| **Smart Links & AI Music Video (`h3-max`)** | `/api/smart-links/` (`soundboo.st`), `/api/audio/generate/` | Auto-resolves DSP metadata, publishes `https://soundboo.st/<slug>` landing pages, and computes `h3-max` retake credit spending caps |
| **REAPER DAW Integration** | — | Native `SoundBoost_18Band_Mastering_EQ_Monitor.jsfx` (18-band EQ + M/S width + `-1.0 dBTP` brickwall limiter + `@gfx` spectrum UI) & `Suavecito_SoundBoost_Stem_Import.lua` ReaScript |

---

## MCP Tools (`soundboost-mcp` — MCP `2026-07-28` Specification)

Every tool includes full **`ToolAnnotations`** (`title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`), semantic `tags`, and structured JSON `outputSchema`. All long-running DSP and cloud tools also register **`TasksExtension` (`io.modelcontextprotocol/tasks`)** with `task=TaskConfig(mode="optional")`, streaming live **`Context.report_progress`** notifications in both background-task and foreground execution modes.

| Tool | Annotations (`readOnly` / `openWorld`) | Background Task (`task=True`) | Description |
| :--- | :---: | :---: | :--- |
| **`audit_audio_loudness_and_release_readiness`** | `readOnly=True`, `openWorld=False` | Sync | Full ITU-R BS.1770-4 Integrated/Short-Term/Momentary LUFS, $P_{95}-P_{10}$ LRA, 4x oversampled True Peak (`dBTP`) + 100ms binary-search timestamp, stereo correlation, 6-band spectral profile, 9-platform streaming penalty matrix, and DistroKid readiness verdict. |
| **`render_local_18band_master`** | `readOnly=False`, `openWorld=False` | **`TaskConfig(mode="optional")`** | Local 18-Band Mastering EQ (`20 Hz`–`15 kHz` or any of the 12 genre presets), Mid/Side stereo width (`0–200%`), target LUFS normalization, lookahead True-Peak brickwall limiter, and 16/24-bit TPDF dithering. |
| **`read_release_metadata_tags`** | `readOnly=True`, `openWorld=False` | Sync | Read ID3v2.3 / FLAC / WAV metadata tags including 12-character ISRC (`TSRC`), BPM, Key, Publisher, Lyrics, and Cover Art status. |
| **`write_release_metadata_tags`** | `readOnly=False`, `openWorld=False` | Sync | Write release metadata tags with ISO 3901 12-character ISRC validation (`CC-XXX-YY-NNNNN`), unsynchronized lyrics (`USLT`), and front cover art (`APIC`). |
| **`soundboost_cipher_tool`** | `readOnly=True`, `openWorld=False` | Sync | Run the Studio Known-Answer Self-Test (`1 -> Hck7LVPBFsyNC95Qzf6-0Q`), encrypt/decrypt `?track=` hashes, generate `ui-api-key` + `ui-token` headers, and encrypt/decrypt `_d_a` cookies. |
| **`transpose_and_quantize_musical_grid`** | `readOnly=True`, `openWorld=False` | Sync | Transpose chord progressions, classify Pitch Shift zone (`Natural zone` $\pm 3\text{ st}$ vs `Creative zone`), snap timestamps to synchronized beats (`30ms` tolerance), and quantize loops to `Bar.Beat.1/16`. |
| **`generate_printable_lead_sheet`** | `readOnly=False`, `openWorld=False` | Sync | Merge word-level lyric segments and transposed chords (inserting `[Instrumental]` chord rows when vocal gaps $\ge 3.0\text{s}$) into a print-ready HTML Lead Sheet. |
| **`soundboost_upload_and_master`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | Upload audio to `api.soundboost.ai` and run any of the 8 cloud Mastering modes (`prompt`, `preset`, `auto`, `reference`, `chat`, `manual`, `mix_doctor`, `enhance`), with optional `wait_for_completion` polling. |
| **`soundboost_studio_stems_and_drums`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | Start/poll Studio 2/4/6-stem separation (`htdemucs`, `htdemucs_6s`), Voice Cleaner (`Voice` + `Artifacts`), **5-Piece Drum Kit sub-stem separation** (`Kick, Snare, Toms, Hi-Hat, Cymbals`), or hand off `loop`/`stems`/`mix` to Mastering. |
| **`soundboost_studio_chords_and_lyrics`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | Start, poll, or edit Studio AI Chord Recognition and Word-Synced Lyrics/Transcripts. |
| **`soundboost_converta_audio`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | Query live `converta.soundboost.ai` format specs or run hi-res format conversions (`wav`, `aiff`, `flac`, `ogg`, `aac`, `m4a`, `mp3`). |
| **`soundboost_smart_links`** | `readOnly=False`, `openWorld=True` | Sync | List, auto-scrape DSP metadata, create, update, and publish `https://soundboo.st/<slug>` Smart Links. |
| **`soundboost_text_to_audio_and_mv_budget`** | `readOnly=False`, `openWorld=True` | **`TaskConfig(mode="optional")`** | Generate AI Text-to-Audio / SFX and compute AI Music Video (`h3-max`) credit spending caps (`ec(quote)` formula). |
| **`install_reaper_jsfx_and_scripts`** | `readOnly=False`, `openWorld=False` | Sync | Install the 18-Band Mastering EQ JSFX plugin and Multitrack Stem Importer ReaScript into REAPER. |
| **`list_soundboost_platform_capabilities`** | `readOnly=True`, `openWorld=False` | Sync | Return the complete specification matrix of EQ bands, presets, stem profiles, drum sub-stems, Converta formats, and streaming loudness targets. |

### MCP Resources & Prompts

- **`soundboost://specs/capabilities`** (`application/json`): Complete platform capability matrix.
- **`soundboost://specs/eq-presets`** (`application/json`): Exact 18-band dB gain arrays (`20 Hz` to `15 kHz`) for all 12 genre presets.
- **`soundboost://specs/reaper-jsfx`** (`text/plain`): Source code of the REAPER `SoundBoost_18Band_Mastering_EQ_Monitor.jsfx` plugin.
- **Prompt `distrokid_release_mastering_workflow`**: Step-by-step release engineering runbook for auditing, stem/drum-kit cleanup, 18-band mastering, and ISO 3901 ISRC tagging.

---

## CLI Usage (`soundboost`)

```bash
# 1. Run full ITU-R BS.1770-4 Loudness, 100ms True-Peak & DistroKid Release Audit
soundboost audit path/to/mix.wav

# 2. Render a 24-bit / -14 LUFS / -1.0 dBTP master with the 18-Band EQ & TPDF dither
soundboost master-local input.wav mastered_24bit.wav --preset hip_hop --width 112 --lufs -14.0 --ceiling -1.0 --bits 24

# 3. Write ID3v2.3 / WAV / FLAC metadata with validated 12-char ISRC & 3000x3000 cover art
soundboost tags mastered_24bit.wav --title "Midnight Drive" --artist "Suavecito" --isrc "US-ABC-26-00001" --bpm 124 --key "Cm" --cover cover.jpg

# 4. Verify Studio AES-128-CBC Cipher KAT & generate signed Studio deep link
soundboost cipher --encrypt-track 42 --ui-headers

# 5. Transpose chords, check Pitch Shift zone, and quantize loop to Bar.Beat.1/16
soundboost grid --chords "Cm7,Fm7,Abmaj7,G7b9/B" --semitones -2 --flats --bpm 128 --start 3.72 --end 11.28

# 6. Install REAPER 18-Band Mastering EQ JSFX & Multitrack Stem Importer ReaScript
soundboost reaper-install
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
| `SOUNDBOOST_ACCESS_TOKEN` | Bearer token for authenticated cloud calls | Loaded from `~/.soundboost/credentials.json` if omitted |
| `SOUNDBOOST_COOKIE_DA` | Encrypted `_d_a` browser cookie (automatically decrypted via OpenSSL `EVP_BytesToKey`) | Optional |
| `SOUNDBOOST_GUEST_TOKEN` | Optional `x-studio-guest-token` for anonymous Studio sessions | Optional |

---

## License

Apache-2.0
