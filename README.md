# Suavecito's SoundBoost.ai Toolkit

**Master, split, tag, and ship your tracks from the terminal, your AI agent, or straight out of REAPER.**

A CLI, an MCP server, and a REAPER suite for [SoundBoost.ai](https://soundboost.ai), plus a local DSP engine that does the release-prep grunt work offline for free.

> [!IMPORTANT]
> **This is not the official SoundBoost MCP.** It's an independent, community-built toolkit, made with the SoundBoost team's permission. SoundBoost doesn't maintain or support it, so file bugs and questions [here](https://github.com/The-40-Thieves/Suavecitos-Soundboost.AI-Toolkit/issues), not with them. When SoundBoost ships its official MCP, use that for anything it covers.

---

## Why this exists

Finishing a song is the fun part. Getting it *release-ready* is a checklist: is it too loud for Spotify, is the true peak clipping, are the stems clean, is the ISRC in the file, does the cover art meet spec. Every one of those is a tab, an upload, and a wait.

This toolkit collapses that checklist into commands. Ask your agent *"is this mix ready for DistroKid?"* and it actually measures it. Tell it *"master it warm, hip-hop preset, -14 LUFS"* and it does it. Hit a key in REAPER and the stems land on tracks.

**The caller is the model.** No embedded LLM, no telemetry, no hardcoded credentials. Your agent (Claude, Cursor, whatever speaks MCP) does the thinking; this does the work.

---

## What it does

| | Runs | Cost |
|---|---|---|
| 🎚️ **Loudness & release audit** · Integrated / short-term / momentary LUFS, LRA, 4x-oversampled true peak with the exact timestamp of the worst peak, and a penalty table for 9 streaming platforms | Local | Free |
| 🏷️ **Release tagging** · ID3v2.3 / FLAC / WAV tags, validated ISRC codes, lyrics, 3000×3000 cover art | Local | Free |
| 🎼 **Chord & grid tools** · Transpose progressions, snap to beats, quantize loops, print lead sheets, draw guitar / uke / piano voicings | Local | Free |
| 🔁 **Format conversion** · WAV, AIFF, FLAC, OGG, AAC, M4A, MP3 at 44.1 / 48 kHz, with dither | Cloud (Converta) | Free |
| ✨ **AI mastering** · 8 modes: prompt, preset, auto, reference, chat, manual, Mix Doctor, enhance. 12 genre presets, 18-band EQ, stereo width, target LUFS | Cloud | Paid tier |
| 🥁 **Stem separation** · 2 / 4 / 6 stems, voice cleaning, and drum splitting into kick, snare, toms, hi-hat, cymbals | Cloud | Tier-gated |
| 📀 **Album mastering** · Batch-master a whole EP to one loudness target, export as ZIP | Cloud | Unlimited Plus and up |
| 🔗 **Smart links** · Build `soundboo.st/<slug>` landing pages with auto-scraped streaming links | Cloud | Tier-gated |
| 🎬 **Music video & Shoots** · AI music video projects, beat-locked storyboards from your REAPER session, 9:16 Spotify Canvas clips, credit budgeting | Cloud + REAPER | Paid tier |
| 🎛️ **REAPER suite** · 18-band mastering EQ JSFX, one-click stem importer, live bridge daemon, 12 genre FX chains, session and video generators | Local | Free |

Cloud features use your own SoundBoost account and plan. Nothing here unlocks anything you aren't paying for.

---

## Quick start

Requires **Python 3.12+**.

```bash
git clone https://github.com/The-40-Thieves/Suavecitos-Soundboost.AI-Toolkit.git
cd Suavecitos-Soundboost.AI-Toolkit
pip install -e ".[video]"   # drop [video] if you don't need the .mp4 renderer
```

Then audit something. No account needed:

```bash
soundboost audit my_mix.wav
```

You get integrated LUFS, true peak (and *where* it peaks), loudness range, stereo correlation, a spectral profile, how much each platform will turn you down, and a straight answer on whether it's ready to distribute.

---

## The CLI

### Free, offline

```bash
# Is it ready to release?
soundboost audit mix.wav

# Tag the master with everything a distributor wants
soundboost tags master_24bit.wav --title "Midnight Drive" --artist "Suavecito" \
  --isrc "US-ABC-26-00001" --bpm 124 --key "Cm" --cover cover.jpg

# Transpose down a whole step, flats only, and quantize a loop at 128 BPM
soundboost grid --chords "Cm7,Fm7,Abmaj7,G7b9/B" --semitones -2 --flats \
  --bpm 128 --start 3.72 --end 11.28

# Chord shapes for practice
soundboost voicings "Cm,Abmaj7,Eb,Bb" --instrument guitar

# Check an MP3's gapless headers before you loop it
soundboost mp3-inspect stem_loop.mp3
```

### With your SoundBoost account

```bash
# Sign in once; the token goes to your OS keyring
soundboost auth browser-login
soundboost auth status

# Upload and master
soundboost master upload --file mix.wav
soundboost master master --track-id 42 --mode preset --preset hip_hop --width 112 --lufs -14.0

# Describe the sound you want in plain English
soundboost prompt-eq "warm 808 sub, clean up the low-mids, crisp vocal, airy top end" --track-id 42

# Master a whole EP to one target
soundboost bulk-master start --tracks "42,43,44" --preset pop --lufs -14.0

# Pull a track you own from YouTube or SoundCloud into the stem splitter
soundboost ingest studio-youtube --url "https://www.youtube.com/watch?v=<your-video>"

# Pitch-shift and time-stretch stems in the cloud
soundboost shifter 42 --semitones 2 --speed 1.15 --format wav --bits 24
```

Run `soundboost --help` for the full list: `studio`, `converta`, `shoots`, `mv-budget`, `account`, `specs`, and more.

**Genre presets:** `hip_hop` · `trap` · `pop` · `rock_indie` · `rnb_soul` · `electronic_edm` · `acoustic_folk` · `jazz_blues` · `classical_orchestral` · `metal_punk` · `latin_reggaeton` · `podcast_spoken_word`

---

## The MCP server

Point any MCP client at it and your agent gets 26 tools for working with audio and SoundBoost.

### Setup

```json
{
  "mcpServers": {
    "soundboost-toolkit": {
      "command": "soundboost-mcp",
      "env": {
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

If `soundboost-mcp` isn't on your PATH, use `"command": "python"` with `"args": ["/path/to/Suavecitos-Soundboost.AI-Toolkit/server.py"]`.

For remote use, serve it over HTTP with OAuth in front. Everyone who connects acts as the SoundBoost account signed in on that machine, so the server refuses to start until you say who's allowed in and which folders it may touch:

```bash
export SOUNDBOOST_ALLOWED_DIRS="$HOME/Music/releases"
export SOUNDBOOST_MCP_ALLOWED_EMAILS="you@example.com"
export SOUNDBOOST_MCP_OAUTH_CLIENT_ID="<your Google OAuth client ID>"
export SOUNDBOOST_MCP_OAUTH_CLIENT_SECRET="<its secret>"
export SOUNDBOOST_MCP_JWT_SIGNING_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
soundboost serve-mcp --transport http --host 0.0.0.0 --port 8000 --oauth google
```

Without `--oauth`, HTTP only binds to `127.0.0.1`.

### It doesn't flood your context

26 tool definitions is a lot of tokens to load into every conversation. So by default the server exposes only **5 entry tools**. Your agent unlocks the rest on demand, by searching for what it needs or by syncing to your SoundBoost plan so it sees exactly the tools you can actually use.

| Pack | What's inside | Plan |
|---|---|---|
| `core` *(always on)* | Tool discovery, loudness audit, sign-in, account & plan info | Free |
| `free_tools` | Tag reader/writer, format converter, platform specs, REAPER installer | Free |
| `studio_harmony` | Chord & lyric recognition, lead sheets, voicings, pitch/time shifter, loop grid | Free |
| `studio_stems` | Stem separation, voice cleaning, drum splitting, YouTube/SoundCloud ingest | Free |
| `distribution` | Smart links | Free |
| `mastering` | Cloud mastering, natural-language & voice mastering, before/after stats | Pay-as-you-go and up |
| `music_video_shoots` | AI music video, Shoots director, text-to-audio, credit budgeting | Pay-as-you-go and up |
| `bulk_mastering` | Album / EP batch mastering | Unlimited Plus, Director, Studio |
| `account_admin` | Profile edits, checkout, pausing or resuming your subscription | Free |

Want everything visible up front? `soundboost serve-mcp --discovery all`. Want a specific set? `--packs mastering,studio_stems`.

Long-running jobs (mastering, stem splits, conversions) run as background tasks and stream progress back, so your agent isn't stuck staring at a spinner.

Anything that deletes, signs you in or out, edits your REAPER config, or touches billing is its own tool marked *destructive*, so your MCP client asks before it runs. The read-only account tool can't change anything.

### Also included

- **Resources:** the full platform capability matrix, the 18-band EQ curves for every genre preset, and the REAPER JSFX source.
- **Prompt:** `distrokid_release_mastering_workflow`, a step-by-step runbook that takes a track from raw mix to tagged, distribution-ready master.

---

## REAPER

```bash
soundboost reaper-install
```

That drops in:

- **18-Band Mastering EQ Monitor** (JSFX): SoundBoost's EQ curve, live in your session. The MCP server can push EQ changes into it while the track plays.
- **Stem Import** (Lua): pulls separated stems onto their own tracks in one go.
- **Live Bridge Daemon** (Lua): lets the toolkit talk to a running REAPER.
- **12 genre FX chains**, matching the mastering presets.
- **9:16 Canvas Visualizer** video preset for vertical clips.

By default it only copies files. Live control is opt-in, because it touches your REAPER config:

```bash
soundboost reaper-install --configure-bridge   # set up Python ReaScript in REAPER.ini
soundboost reaper-install --enable-daemon      # auto-start the Live Bridge Daemon when REAPER launches
soundboost reaper install --configure-bridge --enable-osc   # also turn on OSC (port 9000)
```

Close REAPER first: it rewrites `REAPER.ini` on exit, so the toolkit won't edit the live config while it's running. Your existing control surfaces are kept, and `REAPER.ini` and `__startup.lua` are backed up before any change.

OSC (`--enable-osc`) and the web remote (`--enable-web`, port 8080) are off unless you ask for them, because REAPER exposes both to your whole network and the web remote has no password by default. The Live Bridge Daemon doesn't need either.

From there the toolkit can generate full `.RPP` sessions from your stems, build beat-locked storyboards from your arrangement, and render audio-reactive `.mp4` videos.

---

## Auth & config

Sign in with `soundboost auth browser-login` and your token is stored in your OS keyring (Windows Credential Manager, macOS Keychain, or Secret Service on Linux). Or set it yourself:

| Variable | What it's for |
|---|---|
| `SOUNDBOOST_ACCESS_TOKEN` | Bearer token for cloud calls. Falls back to the keyring. |
| `SOUNDBOOST_GUEST_TOKEN` | Optional guest token for anonymous Studio sessions. |
| `SOUNDBOOST_ALLOWED_DIRS` | Folders the MCP tools may read and write, separated by `;` on Windows or `:` elsewhere. Defaults to the current folder plus Music, Documents, Desktop, Downloads, and Videos. |
| `SOUNDBOOST_GOOGLE_CLIENT_ID` / `SOUNDBOOST_GOOGLE_CLIENT_SECRET` | A Google "Desktop app" OAuth client for the **Continue with Google** button in `browser-login`. Without it you paste a SoundBoost token instead. |
| `SOUNDBOOST_REAPER_ACTION_ALLOWLIST` | Extra REAPER action IDs the agent may trigger. By default only transport and save actions are allowed. |
| `SOUNDBOOST_MCP_OAUTH_CLIENT_ID`, `SOUNDBOOST_MCP_OAUTH_CLIENT_SECRET`, `SOUNDBOOST_MCP_JWT_SIGNING_KEY`, `SOUNDBOOST_MCP_ALLOWED_EMAILS` | Required when serving over HTTP with `--oauth` (see above). Only identities whose token carries `email_verified: true` and a listed e-mail get in. |

---

## Security

- **Your token stays in the OS keyring.** A plaintext `~/.soundboost/credentials.json` (readable only by you) is written only if no keyring works, and it's deleted the next time the keyring save succeeds. Status checks never show the token, and sign-in responses are scrubbed before your agent sees them.
- **Browser sign-in** listens only on `127.0.0.1`, handles one sign-in, then shuts down. Every callback must carry a one-time `state` value, and requests from other hostnames are rejected.
- **File access** from the MCP server is limited to `SOUNDBOOST_ALLOWED_DIRS`. Paths that climb out with `..`, follow a symlink elsewhere, point into hidden folders, or have the wrong file type are refused.
- **Reporting a vulnerability:** see [SECURITY.md](SECURITY.md).

---

## Under the hood

For the curious. None of this is required reading.

- **Loudness** follows ITU-R BS.1770-4 / EBU R128: K-weighted gating, LRA from the 10th to 95th percentile, 4x-oversampled true peak, and a binary search that pins the worst peak to within 100 ms.
- **Local EQ** is an 18-band cascade of constant-Q biquads (20 Hz to 15 kHz, ±6 dB), followed by mid/side width (0 to 200%), a lookahead true-peak limiter, and TPDF dither to 16 or 24 bit.
- **ISRCs** are validated against ISO 3901 before they're written.
- **MCP tools** carry full annotations (read-only, destructive, idempotent, open-world) and structured output schemas, so agents know what's safe to call without asking.
- Built on FastMCP, httpx, Typer, NumPy/SciPy, soundfile, and mutagen.

---

## Development

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[video,dev]"   # .venv/bin/python on macOS/Linux
ruff check .
mypy
pytest
pip-audit --skip-editable
```

CI runs the same checks on Ubuntu and Windows for Python 3.12 and 3.13, and Dependabot opens weekly update PRs.

---

## Project layout

```
soundboost_toolkit/   CLI, MCP server, API client, OAuth, DSP, tagging, REAPER integration
reaper/               JSFX effect, Lua scripts, video preset
skills/               Agent skill file for this toolkit
tests/                Test suite (including security regression tests)
server.py             Direct entry point for MCP clients
```

---

## License

Apache-2.0.

SoundBoost, SoundBoost.ai, and related names belong to their respective owners. This project is independent and is not the official SoundBoost MCP.
