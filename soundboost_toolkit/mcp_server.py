"""FastMCP 4.1+ Server for Suavecito's Soundboost.AI Toolkit (MCP 2026-07-28 Spec).

Includes:
- Full `ToolAnnotations` (`title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`)
- Protocol-native Background Tasks (`TasksExtension` / `io.modelcontextprotocol/tasks` with `TaskConfig(mode="optional")`)
- Real-time `Context.report_progress` notifications & optional automatic polling (`wait_for_completion`)
- Structured MCP Resources (`soundboost://specs/...`) and Prompts (`distrokid_release_mastering_workflow`)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.utilities.tasks import TaskConfig
from fastmcp_tasks import TasksExtension
from mcp.types import ToolAnnotations

from soundboost_toolkit.client import SoundBoostClient
from soundboost_toolkit.constants import (
    CONVERTA_FORMATS,
    DRUM_KIT_SUBSTEMS,
    MASTERING_EQ_FREQUENCIES_HZ,
    MASTERING_GENRE_PRESETS,
    MASTERING_MODES,
    MV_CHARACTER_REF_CAPS,
    PRESET_EQ_CURVES_18BAND,
    STEM_PROFILES,
    STREAMING_TARGETS,
)
from soundboost_toolkit.crypto import (
    build_ui_security_headers,
    compute_mv_spending_limit,
    decrypt_cookie_value,
    decrypt_param_id,
    decrypt_ui_token,
    encrypt_cookie_value,
    encrypt_param_id,
    verify_param_cipher_kat,
)
from soundboost_toolkit.dsp import (
    analyze_audio_file,
    build_lead_sheet_rows,
    classify_pitch_shift_zone,
    get_chord_voicing_diagram,
    inspect_mp3_gapless_headers,
    master_album_folder,
    quantize_loop_to_bar_beat_sixteenth,
    render_18band_master,
    render_lead_sheet_html,
    render_local_pitch_and_time_stretch,
    snap_timestamp_to_synchronized_beat,
    translate_prompt_to_18band_eq,
    transpose_chord_symbol,
)
from soundboost_toolkit.oauth import (
    build_mcp_oauth_provider,
    clear_vault_credentials,
    generate_google_pkce_challenge,
    get_vault_status,
    run_loopback_oauth_handoff,
    save_credentials_to_vault,
)
from soundboost_toolkit.reaper_integration import install_reaper_assets
from soundboost_toolkit.tagging import read_audio_tags, write_audio_tags

mcp = FastMCP(
    "suavecitos-soundboost-toolkit",
    auth=build_mcp_oauth_provider(),
    instructions=(
        "Suavecito's Soundboost.AI Toolkit — Complete MCP 2026-07-28 server for SoundBoost.ai "
        "(Diktatorial Suite) cloud APIs, OAuth 2.1 + OS Keyring Vault, local ITU-R BS.1770-4 / "
        "EBU R128 loudness & 100ms True-Peak diagnostics, 18-Band Mastering EQ rendering, "
        "ID3v2.3/ISRC tagging, and REAPER JSFX/ReaScript integration."
    ),
)

_tasks_backend_url = os.environ.get("SOUNDBOOST_TASKS_URL")
mcp.add_extension(
    TasksExtension(url=_tasks_backend_url) if _tasks_backend_url else TasksExtension()
)


async def _notify_progress(
    ctx: Context | None,
    progress: float,
    total: float,
    message: str,
) -> None:
    """Safely emit MCP progress notifications and info logs when a Context is active."""
    if ctx is None:
        return
    try:
        await ctx.report_progress(progress=progress, total=total, message=message)
        await ctx.info(message)
    except RuntimeError:
        # Context may not be bound when invoked directly as a plain Python function in unit tests
        return


async def _poll_until_ready(
    ctx: Context | None,
    fetch_fn: Any,
    *,
    poll_interval_sec: float = 3.0,
    max_wait_sec: float = 300.0,
    task_label: str = "Cloud job",
) -> dict[str, Any]:
    """Poll a synchronous status function in a worker thread while reporting MCP progress."""
    start_ts = time.monotonic()
    last_res: dict[str, Any] = {}
    while True:
        elapsed = time.monotonic() - start_ts
        last_res = await asyncio.to_thread(fetch_fn)
        status_str = str(
            last_res.get("status")
            or last_res.get("state")
            or (last_res.get("data") or {}).get("status")
            or ""
        ).lower()
        is_ready = bool(
            last_res.get("is_ready")
            or last_res.get("ready")
            or status_str in ("completed", "complete", "done", "succeeded", "success", "ready")
        )
        is_failed = status_str in ("failed", "error", "cancelled", "canceled")

        await _notify_progress(
            ctx,
            min(elapsed, max_wait_sec),
            max_wait_sec,
            f"{task_label} status: {status_str or 'processing'} ({elapsed:.1f}s elapsed)",
        )
        if is_ready or is_failed or elapsed >= max_wait_sec:
            await _notify_progress(ctx, max_wait_sec, max_wait_sec, f"{task_label} finished ({status_str or 'done'})")
            return last_res
        await asyncio.sleep(max(0.5, poll_interval_sec))


# ---------------------------------------------------------------------------
# 1. Local Offline DSP, Mastering & Tagging Tools
# ---------------------------------------------------------------------------
@mcp.tool(
    tags={"dsp", "loudness", "audit"},
    annotations=ToolAnnotations(
        title="Audit Audio Loudness (BS.1770-4) & DistroKid Readiness",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def audit_audio_loudness_and_release_readiness(file_path: str) -> dict[str, Any]:
    """Run ITU-R BS.1770-4 Loudness (Integrated/Short-Term/Momentary LUFS), SoundBoost LRA (P95-P10),
    4x oversampled True Peak (dBTP) with 100ms binary-search peak timestamp, stereo correlation,
    6-band spectral profile, 9-platform Streaming Loudness Penalty table, and DistroKid readiness check.
    """
    return analyze_audio_file(file_path).to_dict()


@mcp.tool(
    tags={"dsp", "mastering", "eq"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="Render Local 18-Band Studio Master",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def render_local_18band_master(
    input_path: str,
    output_path: str,
    preset: str | None = None,
    eq_gains_db: list[float] | None = None,
    stereo_width_pct: float = 100.0,
    target_lufs: float | None = -14.0,
    true_peak_ceiling_dbtp: float = -1.0,
    bit_depth: int = 24,
    apply_tpdf_dither: bool = True,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Render a local master using SoundBoost's 18-Band Graphic EQ (20 Hz to 15 kHz or a genre preset),
    Mid/Side Stereo Width (0-200%), LUFS normalization, lookahead True-Peak brickwall limiter,
    and 16/24-bit TPDF dithering. Supports background task execution.
    """
    await _notify_progress(ctx, 10.0, 100.0, f"Starting 18-band mastering render for {Path(input_path).name}")
    result = await asyncio.to_thread(
        render_18band_master,
        input_path=input_path,
        output_path=output_path,
        eq_gains_db=eq_gains_db,
        preset=preset,
        stereo_width_pct=stereo_width_pct,
        target_lufs=target_lufs,
        true_peak_ceiling_dbtp=true_peak_ceiling_dbtp,
        bit_depth=bit_depth,
        apply_tpdf_dither=apply_tpdf_dither,
    )
    await _notify_progress(ctx, 100.0, 100.0, f"Completed master render -> {output_path}")
    return result


@mcp.tool(
    tags={"metadata", "id3", "isrc"},
    annotations=ToolAnnotations(
        title="Read Audio Release Metadata & ISRC Tags",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def read_release_metadata_tags(file_path: str) -> dict[str, Any]:
    """Read ID3v2.3 / FLAC VorbisComment / WAV metadata tags including 12-char ISRC (TSRC), BPM, Key, Publisher, Lyrics, and Cover Art."""
    return read_audio_tags(file_path)


@mcp.tool(
    tags={"metadata", "id3", "isrc"},
    annotations=ToolAnnotations(
        title="Write Audio Release Metadata, ISRC & Cover Art",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def write_release_metadata_tags(
    file_path: str,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    year: str | None = None,
    track: str | None = None,
    genre: str | None = None,
    bpm: str | None = None,
    initial_key: str | None = None,
    publisher: str | None = None,
    isrc: str | None = None,
    comment: str | None = None,
    lyrics: str | None = None,
    cover_art_path: str | None = None,
) -> dict[str, Any]:
    """Write release metadata tags, ISO 3901 12-character ISRC (`TSRC`), unsynchronized lyrics (`USLT`), and front cover art (`APIC`) to MP3/FLAC/WAV."""
    return write_audio_tags(
        file_path=file_path,
        title=title,
        artist=artist,
        album=album,
        year=year,
        track=track,
        genre=genre,
        bpm=bpm,
        initial_key=initial_key,
        publisher=publisher,
        isrc=isrc,
        comment=comment,
        lyrics=lyrics,
        cover_art_path=cover_art_path,
    )


@mcp.tool(
    tags={"crypto", "auth", "protocol"},
    annotations=ToolAnnotations(
        title="SoundBoost AES-CBC Cipher & Token Generator",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def soundboost_cipher_tool(
    action: str,
    value: str = "1",
    product: str | None = None,
) -> dict[str, Any]:
    """Execute SoundBoost cryptographic operations:
    - `kat`: Run Known-Answer Self-Test (`1` -> `'Hck7LVPBFsyNC95Qzf6-0Q'`)
    - `encrypt_track`: Encrypt integer track ID into `?track=` Base64URL hash and Studio deep link
    - `decrypt_track`: Decrypt `?track=` hash back into track ID
    - `generate_ui_headers`: Generate `ui-api-key` and AES-CBC time-windowed `ui-token` headers
    - `decrypt_ui_token`: Decrypt a `ui-token` header value
    - `encrypt_cookie`: Encrypt a value using CryptoJS OpenSSL `_d_a` cookie format
    - `decrypt_cookie`: Decrypt a SoundBoost `_d_a` / `_d_e` / `_d_i` / `_d_u` cookie
    """
    act = action.strip().lower()
    if act == "kat":
        return {
            "kat_passed": verify_param_cipher_kat(),
            "encrypted_1": encrypt_param_id(1),
            "decrypted_hash": decrypt_param_id("Hck7LVPBFsyNC95Qzf6-0Q"),
        }
    if act == "encrypt_track":
        tid: int | str = int(value) if value.strip().isdigit() else value
        enc = encrypt_param_id(tid)
        client = SoundBoostClient()
        return {
            "track_id": tid,
            "encrypted_hash": enc,
            "studio_url": client.build_studio_url(tid, product=product),
        }
    if act == "decrypt_track":
        return {"encrypted_hash": value, "track_id": decrypt_param_id(value)}
    if act == "generate_ui_headers":
        return build_ui_security_headers()
    if act == "decrypt_ui_token":
        return {"decrypted": decrypt_ui_token(value)}
    if act == "encrypt_cookie":
        return {"encrypted_cookie": encrypt_cookie_value(value)}
    if act == "decrypt_cookie":
        return {"decrypted_cookie": decrypt_cookie_value(value)}
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"music-theory", "chords", "loops"},
    annotations=ToolAnnotations(
        title="Transpose Chords & Quantize Musical Bar.Beat.1/16 Grid",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def transpose_and_quantize_musical_grid(
    chords: list[str] | None = None,
    semitones: int = 0,
    prefer_flats: bool = False,
    bpm: float = 120.0,
    loop_start_sec: float = 0.0,
    loop_end_sec: float = 8.0,
    beat_timestamp_sec: float | None = None,
    synchronized_beats_sec: list[float] | None = None,
) -> dict[str, Any]:
    """Transpose chord symbols (including slash bass notes), classify Pitch Shift zone
    (`Natural zone` +/-3 st vs `Creative zone`), snap timestamps to synchronized beats (30ms window),
    and quantize loop points to `Bar.Beat.1/16`.
    """
    transposed = [
        {
            "original": c,
            "transposed": transpose_chord_symbol(c, semitones, prefer_flats=prefer_flats),
        }
        for c in (chords or [])
    ]
    res: dict[str, Any] = {
        "pitch_shift_info": classify_pitch_shift_zone(semitones),
        "transposed_chords": transposed,
        "quantized_loop": quantize_loop_to_bar_beat_sixteenth(loop_start_sec, loop_end_sec, bpm),
    }
    if beat_timestamp_sec is not None and synchronized_beats_sec is not None:
        res["snapped_beat_sec"] = snap_timestamp_to_synchronized_beat(
            beat_timestamp_sec, synchronized_beats_sec
        )
    return res


@mcp.tool(
    tags={"music-theory", "lyrics", "lead-sheet"},
    annotations=ToolAnnotations(
        title="Generate Printable Chord & Lyric Lead Sheet (HTML/PDF)",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def generate_printable_lead_sheet(
    title: str,
    lyrics_segments: list[dict[str, Any]],
    chords: list[dict[str, Any]],
    semitone_shift: int = 0,
    prefer_flats: bool = False,
    key_signature: str | None = None,
    bpm: float | None = None,
    output_html_path: str | None = None,
) -> dict[str, Any]:
    """Merge word-level lyric segments and transposed chords (automatically inserting
    `[Instrumental]` chord rows when vocal gaps >= 3.0s) and generate a print-ready HTML Lead Sheet.
    """
    rows = build_lead_sheet_rows(
        lyrics_segments=lyrics_segments,
        chords=chords,
        semitone_shift=semitone_shift,
        prefer_flats=prefer_flats,
    )
    html_doc = render_lead_sheet_html(
        title=title,
        rows=rows,
        key_signature=key_signature,
        bpm=bpm,
    )
    saved_path: str | None = None
    if output_html_path:
        out_p = Path(output_html_path).expanduser().resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(html_doc, encoding="utf-8")
        saved_path = str(out_p)
    return {
        "title": title,
        "row_count": len(rows),
        "rows": rows,
        "output_html_path": saved_path,
        "html_preview": html_doc[:2000],
    }


# ---------------------------------------------------------------------------
# 2. Long-Running Cloud Tools (Background Tasks Enabled + Progress Streaming)
# ---------------------------------------------------------------------------
@mcp.tool(
    tags={"cloud", "mastering", "mix-doctor"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="SoundBoost Cloud AI Mastering, Mix Doctor & Audio Enhancer",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def soundboost_upload_and_master(
    action: str,
    file_path: str | None = None,
    track_id: str | int | None = None,
    mode: str = "auto",
    prompt: str | None = None,
    preset: str | None = None,
    reference_track_id: str | int | None = None,
    eq_18band_db: list[float] | None = None,
    stereo_width_pct: float | None = None,
    target_lufs: float | None = None,
    revision_notes: str | None = None,
    enhance_intensity: float = 0.75,
    wait_for_completion: bool = False,
    poll_interval_sec: float = 3.0,
    max_wait_sec: float = 300.0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Interact with SoundBoost Cloud AI Mastering, Mix Doctor & Audio Enhancer (supports Background Tasks):
    - `upload`: Upload local audio file to `POST /api/upload-audio/`
    - `master`: Run any of the 8 Mastering modes (`prompt`, `preset`, `auto`, `reference`, `chat`, `manual`, `mix_doctor`, `enhance`)
    - `status`: Poll track status and download URLs (`GET /api/tracks/{id}/`)
    - `mix_doctor`: Run Mix Doctor analysis (`POST /api/tracks/{id}/mix-doctor/`)
    - `enhance`: Run Audio Enhancer (`POST /api/tracks/{id}/enhance/`)
    """
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "upload":
        if not file_path:
            raise ValueError("file_path is required for action='upload'")
        await _notify_progress(ctx, 10.0, 100.0, f"Uploading {Path(file_path).name} to SoundBoost...")
        res = await asyncio.to_thread(client.upload_audio, file_path)
        await _notify_progress(ctx, 100.0, 100.0, "Upload complete.")
        return res

    if track_id is None:
        raise ValueError(f"track_id is required for action='{action}'")

    if act == "master":
        await _notify_progress(ctx, 15.0, 100.0, f"Starting cloud mastering ({mode}) for track {track_id}...")
        res = await asyncio.to_thread(
            client.start_mastering,
            track_id,
            mode=mode,
            prompt=prompt,
            preset=preset,
            reference_track_id=reference_track_id,
            eq_18band_db=eq_18band_db,
            stereo_width_pct=stereo_width_pct,
            target_lufs=target_lufs,
            revision_notes=revision_notes,
        )
        if wait_for_completion:
            return await _poll_until_ready(
                ctx,
                lambda: client.get_track_status(track_id),
                poll_interval_sec=poll_interval_sec,
                max_wait_sec=max_wait_sec,
                task_label=f"Cloud Mastering ({mode})",
            )
        await _notify_progress(ctx, 100.0, 100.0, "Cloud mastering job submitted.")
        return res

    if act == "status":
        return await asyncio.to_thread(client.get_track_status, track_id)
    if act == "mix_doctor":
        await _notify_progress(ctx, 20.0, 100.0, f"Running Mix Doctor on track {track_id}...")
        res = await asyncio.to_thread(client.run_mix_doctor, track_id, preset or "pop")
        await _notify_progress(ctx, 100.0, 100.0, "Mix Doctor analysis complete.")
        return res
    if act == "enhance":
        await _notify_progress(ctx, 20.0, 100.0, f"Running Audio Enhancer on track {track_id}...")
        res = await asyncio.to_thread(client.run_audio_enhancer, track_id, enhance_intensity)
        await _notify_progress(ctx, 100.0, 100.0, "Audio Enhancer complete.")
        return res
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"cloud", "studio", "stems", "drums"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="Studio Stem Splitter, Voice Cleaner & 5-Piece Drum Kit Separator",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def soundboost_studio_stems_and_drums(
    action: str,
    original_id: str | int,
    profile_key: str = "standard_4stem",
    stem_variant: str | None = None,
    stem_format: str | None = None,
    handoff_source: str = "mix",
    stem_name: str | None = None,
    loop_start_sec: float | None = None,
    loop_end_sec: float | None = None,
    wait_for_completion: bool = False,
    poll_interval_sec: float = 3.0,
    max_wait_sec: float = 300.0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Control `studio.soundboost.ai` Stem Separation, Voice Cleaner & 5-Piece Drum Kit Splitter (supports Background Tasks):
    - `status`: Fetch Studio original status
    - `start_stems`: Start stem separation (`standard_2stem`, `standard_4stem`, `hifi_4stem`, `hifi_6stem`)
    - `get_stems`: Fetch separated stem URLs (`Vocals, Bass, Drums, Guitar, Piano, Other`)
    - `start_voice_clean`: Start Voice Cleaner (`Voice` + `Artifacts` noise stem)
    - `start_drums`: Start 5-Piece Drum Kit sub-stem separation (`Kick, Snare, Toms, Hi-Hat, Cymbals`)
    - `get_drums`: Fetch the 5-Piece Drum Kit sub-stem URLs
    - `handoff_to_mastering`: Send `loop`, `stems`, or `mix` directly to Mastering
    """
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "status":
        return await asyncio.to_thread(
            client.get_studio_original_status, original_id, stem_variant, stem_format
        )
    if act == "start_stems":
        await _notify_progress(ctx, 10.0, 100.0, f"Starting Studio stem split ({profile_key}) for {original_id}...")
        res = await asyncio.to_thread(client.start_studio_stem_split, original_id, profile_key)
        if wait_for_completion:
            return await _poll_until_ready(
                ctx,
                lambda: client.get_studio_stems(original_id, stem_variant=stem_variant, stem_format=stem_format),
                poll_interval_sec=poll_interval_sec,
                max_wait_sec=max_wait_sec,
                task_label=f"Stem Split ({profile_key})",
            )
        return res
    if act == "get_stems":
        return await asyncio.to_thread(
            client.get_studio_stems, original_id, stem_variant, stem_format
        )
    if act == "start_voice_clean":
        await _notify_progress(ctx, 10.0, 100.0, f"Starting Studio Voice Cleaner for {original_id}...")
        res = await asyncio.to_thread(client.start_studio_voice_clean, original_id)
        if wait_for_completion:
            return await _poll_until_ready(
                ctx,
                lambda: client.get_studio_stems(original_id),
                poll_interval_sec=poll_interval_sec,
                max_wait_sec=max_wait_sec,
                task_label="Voice Cleaner",
            )
        return res
    if act == "start_drums":
        await _notify_progress(ctx, 10.0, 100.0, f"Starting 5-Piece Drum Kit split for {original_id}...")
        res = await asyncio.to_thread(client.start_studio_drum_split, original_id)
        if wait_for_completion:
            return await _poll_until_ready(
                ctx,
                lambda: client.get_studio_drum_stems(original_id),
                poll_interval_sec=poll_interval_sec,
                max_wait_sec=max_wait_sec,
                task_label="5-Piece Drum Kit Split",
            )
        return res
    if act == "get_drums":
        return await asyncio.to_thread(client.get_studio_drum_stems, original_id)
    if act == "handoff_to_mastering":
        await _notify_progress(ctx, 25.0, 100.0, f"Handing off Studio {handoff_source} to Mastering...")
        return await asyncio.to_thread(
            client.handoff_studio_to_mastering,
            original_id,
            handoff_source,
            stem_name,
            loop_start_sec,
            loop_end_sec,
        )
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"cloud", "studio", "chords", "lyrics"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="Studio AI Chord Detection & Word-Synced Lyrics",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def soundboost_studio_chords_and_lyrics(
    action: str,
    original_id: str | int,
    edited_segments: list[dict[str, Any]] | None = None,
    wait_for_completion: bool = False,
    poll_interval_sec: float = 3.0,
    max_wait_sec: float = 180.0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Control `studio.soundboost.ai` AI Chord Detection & Word-Synced Lyrics/Transcripts (supports Background Tasks):
    - `start_chords`: Start beat-aligned AI Chord Recognition
    - `get_chords`: Fetch detected chords and synchronized beat timestamps
    - `start_lyrics`: Start word-level AI Lyric / Voice-Clean Transcript recognition
    - `get_lyrics`: Fetch word-timed lyric segments
    - `edit_lyrics`: Save edited lyric segments (`[{id, text}]`)
    """
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "start_chords":
        await _notify_progress(ctx, 10.0, 100.0, f"Starting AI Chord Recognition for {original_id}...")
        res = await asyncio.to_thread(client.start_studio_chord_analysis, original_id)
        if wait_for_completion:
            return await _poll_until_ready(
                ctx,
                lambda: client.get_studio_chord_analysis(original_id),
                poll_interval_sec=poll_interval_sec,
                max_wait_sec=max_wait_sec,
                task_label="AI Chord Analysis",
            )
        return res
    if act == "get_chords":
        return await asyncio.to_thread(client.get_studio_chord_analysis, original_id)
    if act == "start_lyrics":
        await _notify_progress(ctx, 10.0, 100.0, f"Starting word-level Lyric Transcription for {original_id}...")
        res = await asyncio.to_thread(client.start_studio_lyrics, original_id)
        if wait_for_completion:
            return await _poll_until_ready(
                ctx,
                lambda: client.get_studio_lyrics(original_id),
                poll_interval_sec=poll_interval_sec,
                max_wait_sec=max_wait_sec,
                task_label="AI Lyric Transcription",
            )
        return res
    if act == "get_lyrics":
        return await asyncio.to_thread(client.get_studio_lyrics, original_id)
    if act == "edit_lyrics":
        if edited_segments is None:
            raise ValueError("edited_segments is required for action='edit_lyrics'")
        return await asyncio.to_thread(
            client.save_studio_lyrics_edits, original_id, edited_segments
        )
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"cloud", "converta", "transcode"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="Converta Hi-Res Audio Format & Dither Transcoder",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
async def soundboost_converta_audio(
    action: str = "formats",
    source_id_or_url: str | None = None,
    job_id: str | None = None,
    output_format: str = "wav",
    bit_depth: int = 24,
    sample_rate: int = 48000,
    dithering: bool = True,
    wait_for_completion: bool = False,
    poll_interval_sec: float = 2.0,
    max_wait_sec: float = 180.0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Interact with `converta.soundboost.ai` Hi-Res Audio Converter (supports Background Tasks):
    - `formats`: Query live supported output formats, bit depths (`16/24/32`), sample rates (`44.1/48kHz`), and dithering support
    - `convert`: Start conversion job (`POST /api/v1/convert`)
    - `status`: Poll conversion job status (`GET /api/v1/status/{job_id}`)
    """
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "formats":
        return await asyncio.to_thread(client.get_converta_formats)
    if act == "convert":
        if not source_id_or_url:
            raise ValueError("source_id_or_url is required for action='convert'")
        await _notify_progress(
            ctx,
            15.0,
            100.0,
            f"Starting Converta transcode -> {output_format} ({bit_depth}-bit / {sample_rate} Hz)",
        )
        res = await asyncio.to_thread(
            client.start_converta_conversion,
            source_id_or_url,
            output_format,
            bit_depth,
            sample_rate,
            dithering,
        )
        cid = res.get("id") or res.get("job_id")
        if wait_for_completion and cid:
            return await _poll_until_ready(
                ctx,
                lambda: client.get_converta_status(str(cid)),
                poll_interval_sec=poll_interval_sec,
                max_wait_sec=max_wait_sec,
                task_label=f"Converta ({output_format})",
            )
        return res
    if act == "status":
        if not job_id:
            raise ValueError("job_id is required for action='status'")
        return await asyncio.to_thread(client.get_converta_status, job_id)
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"cloud", "smart-links", "marketing"},
    annotations=ToolAnnotations(
        title="Manage SoundBoost Smart Links (soundboo.st)",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
def soundboost_smart_links(
    action: str = "list",
    link_id: str | int | None = None,
    source_url: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    """Manage SoundBoost Smart Links (`https://soundboo.st/<slug>`):
    - `list`: List all Smart Links
    - `resolve`: Auto-scrape streaming platform links and cover art from a `source_url`
    - `create`: Create a new Smart Link
    - `update`: Update an existing Smart Link (`link_id` + `payload`)
    - `publish`: Publish a Smart Link to `https://soundboo.st/<publicId>`
    - `archive`: Archive a Smart Link
    - `delete`: Delete a Smart Link
    """
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "list":
        return client.list_smart_links()
    if act == "resolve":
        if not source_url:
            raise ValueError("source_url is required for action='resolve'")
        return client.resolve_smart_link_source_metadata(source_url)
    if act == "create":
        return client.create_smart_link(payload or {})
    if link_id is None:
        raise ValueError(f"link_id is required for action='{action}'")
    if act == "update":
        return client.update_smart_link(link_id, payload or {})
    if act == "publish":
        return client.publish_smart_link(link_id)
    if act == "archive":
        return client.archive_smart_link(link_id)
    if act == "delete":
        return client.delete_smart_link(link_id)
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"cloud", "sfx", "music-video"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="AI Text-to-Audio Generator & Music Video (h3-max) Budget Calculator",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def soundboost_text_to_audio_and_mv_budget(
    action: str = "mv_budget",
    prompt: str | None = None,
    duration_sec: int = 10,
    generation_id: str | int | None = None,
    page: int = 1,
    quoted_credits: int = 100,
    plan_tier: str = "director",
    wait_for_completion: bool = False,
    poll_interval_sec: float = 1.5,
    max_wait_sec: float = 180.0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Generate AI Text-to-Audio / SFX or compute AI Music Video (`h3-max`) credit spending caps (supports Background Tasks):
    - `mv_budget`: Compute exact `h3-max` retake spending cap (`ec(quote)` formula) and character reference limit (`starter/creator: 1, director: 2, studio: 3`)
    - `generate_sfx`: Start AI Text-to-Audio generation (`POST /api/audio/generate/`)
    - `sfx_status`: Poll Text-to-Audio generation status
    - `list_public_sfx`: Browse community Text-to-Audio generations
    """
    act = action.strip().lower()
    if act == "mv_budget":
        tier = plan_tier.strip().lower()
        max_credits = compute_mv_spending_limit(quoted_credits)
        return {
            "engine": "h3-max",
            "plan_tier": tier,
            "quoted_credits": quoted_credits,
            "spending_limit_credits": max_credits,
            "retake_buffer_credits": max_credits - quoted_credits,
            "character_reference_limit": MV_CHARACTER_REF_CAPS.get(tier, 1),
            "auto_qc_enabled": tier in ("director", "studio"),
            "deep_research_enabled": tier == "studio",
        }
    client = SoundBoostClient()
    if act == "generate_sfx":
        if not prompt:
            raise ValueError("prompt is required for action='generate_sfx'")
        await _notify_progress(ctx, 10.0, 100.0, f"Generating {duration_sec}s Text-to-Audio SFX...")
        res = await asyncio.to_thread(client.generate_text_to_audio, prompt, duration_sec)
        gid = res.get("id") or res.get("generation_id")
        if wait_for_completion and gid is not None:
            return await _poll_until_ready(
                ctx,
                lambda: client.get_text_to_audio_status(gid),
                poll_interval_sec=poll_interval_sec,
                max_wait_sec=max_wait_sec,
                task_label="Text-to-Audio Generation",
            )
        return res
    if act == "sfx_status":
        if generation_id is None:
            raise ValueError("generation_id is required for action='sfx_status'")
        return await asyncio.to_thread(client.get_text_to_audio_status, generation_id)
    if act == "list_public_sfx":
        return await asyncio.to_thread(client.list_public_text_to_audio, page)
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"oauth", "auth", "keyring", "vault"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="SoundBoost Google OAuth 2.0, OS Keyring Vault & Session Manager",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
async def soundboost_oauth_and_vault(
    action: str = "status",
    token_or_cookie: str | None = None,
    email: str | None = None,
    port: int = 8765,
    timeout_sec: float = 120.0,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Manage SoundBoost OAuth 2.0, OS Keyring (`WinVaultKeyring`), and Inbound MCP OAuth 2.1:
    - `status`: Check OS Keyring (`WinVaultKeyring`) and credential vault authentication status
    - `pkce_url`: Generate RFC 7636 S256 PKCE Google OAuth 2.0 authorization URL (`716860248198-...`)
    - `google_exchange`: Exchange a Google OAuth `id_token` / `access_token` at `POST /api/google-login/` and store the SoundBoost JWT in OS Keyring
    - `save_token`: Save a SoundBoost Bearer JWT or encrypted `_d_a` cookie into OS Keyring
    - `browser_handoff`: Launch local loopback OAuth callback server (`127.0.0.1:<port>/auth/callback`) to capture browser sign-in
    - `logout`: Clear stored credentials from OS Keyring and `~/.soundboost/credentials.json`
    """
    act = action.strip().lower()
    if act == "status":
        return get_vault_status()
    if act == "pkce_url":
        pkce = generate_google_pkce_challenge(
            redirect_uri=f"http://127.0.0.1:{port}/auth/callback"
        )
        return {
            "authorize_url": pkce.authorize_url,
            "redirect_uri": pkce.redirect_uri,
            "state": pkce.state,
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
        }
    if act == "google_exchange":
        if not token_or_cookie:
            raise ValueError("token_or_cookie is required for action='google_exchange'")
        client = SoundBoostClient()
        return await asyncio.to_thread(client.google_oauth_login, token_or_cookie, True)
    if act == "save_token":
        if not token_or_cookie:
            raise ValueError("token_or_cookie is required for action='save_token'")
        raw = token_or_cookie.strip()
        if raw.startswith("U2FsdGVkX1") or "%2F" in raw or "%2B" in raw:
            dec = decrypt_cookie_value(raw)
            if isinstance(dec, str) and dec.strip():
                raw = dec.strip()
        return save_credentials_to_vault(raw, email=email, auth_method="manual_vault_save")
    if act == "browser_handoff":
        return await asyncio.to_thread(
            run_loopback_oauth_handoff,
            port,
            timeout_sec,
            open_browser,
        )
    if act == "logout":
        return clear_vault_credentials()
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"reaper", "daw", "jsfx"},
    annotations=ToolAnnotations(
        title="Install REAPER 18-Band Mastering EQ JSFX & Stem Importer ReaScript",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def install_reaper_jsfx_and_scripts(reaper_resource_dir: str | None = None) -> dict[str, Any]:
    """Install Suavecito's SoundBoost 18-Band Mastering EQ JSFX plugin and Multitrack Stem Importer ReaScript into REAPER."""
    return install_reaper_assets(reaper_resource_dir)


@mcp.tool(
    tags={"specs", "capabilities"},
    annotations=ToolAnnotations(
        title="List SoundBoost Platform Capabilities & DSP Constants",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def list_soundboost_platform_capabilities() -> dict[str, Any]:
    """Return the complete specification matrix of SoundBoost.ai's 18-band EQ frequencies,
    8 Mastering modes, 12 Genre presets, 4 Stem profiles, 5-Piece Drum Kit sub-stems,
    Converta format matrix, and 9 Streaming Loudness targets.
    """
    return {
        "mastering_eq_18band_hz": list(MASTERING_EQ_FREQUENCIES_HZ),
        "mastering_modes": list(MASTERING_MODES),
        "mastering_genre_presets": MASTERING_GENRE_PRESETS,
        "studio_stem_profiles": {
            k: {
                "stem_variant": v.stem_variant,
                "stem_format": v.stem_format,
                "separation_model": v.separation_model,
                "stems": list(v.stems),
                "requires_plus": v.requires_plus,
            }
            for k, v in STEM_PROFILES.items()
        },
        "drum_kit_5piece_substems": list(DRUM_KIT_SUBSTEMS),
        "converta_formats": {
            k: {
                "bit_depths": list(v.bit_depths),
                "sample_rates": list(v.sample_rates),
                "supports_dithering": v.supports_dithering,
            }
            for k, v in CONVERTA_FORMATS.items()
        },
        "streaming_loudness_targets": {
            k: {
                "platform": v.platform,
                "target_lufs": v.target_lufs,
                "true_peak_ceiling_dbtp": v.true_peak_ceiling_dbtp,
            }
            for k, v in STREAMING_TARGETS.items()
        },
    }


# ---------------------------------------------------------------------------
# 2B. New Feature Clusters (Gaps 1-7): Bulk Mastering, YouTube/SoundCloud Ingestion,
#     Voice/Toki AI Prompt, Full Music Video & Shoots Director, Studio Practice Voicings,
#     Local Pitch/Time-Stretch, Gapless MP3 Inspector, Track Lifecycle & Account/Billing
# ---------------------------------------------------------------------------
@mcp.tool(
    tags={"mastering", "bulk", "album", "dsp"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="SoundBoost Bulk & Album Mastering Engine (Cloud & Local Folder)",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def soundboost_bulk_and_album_mastering(
    action: str = "master_local_folder",
    input_dir: str | None = None,
    output_dir: str | None = None,
    track_ids: list[str | int] | None = None,
    batch_id: str | int | None = None,
    mode: str = "auto",
    preset: str = "pop",
    prompt: str | None = None,
    target_lufs: float = -14.0,
    true_peak_ceiling_dbtp: float = -1.0,
    stereo_width_pct: float = 100.0,
    bit_depth: int = 24,
    album_title: str | None = None,
    artist_name: str | None = None,
    wait_for_completion: bool = False,
    poll_interval_sec: float = 3.0,
    max_wait_sec: float = 300.0,
    ctx: Context | None = None,
) -> Any:
    """Batch-master an entire Album/EP either locally (`master_local_folder`) or via SoundBoost Cloud (`/api/bulk-mastering/*`):
    - `master_local_folder`: Batch-master a directory of audio files locally with unified LUFS, -1.0 dBTP ceiling, 18-band EQ, and sequential track tags
    - `cloud_start`: Start a cloud bulk mastering batch (`POST /api/bulk-mastering/start/`)
    - `cloud_list`: List all cloud bulk mastering batches (`GET /api/bulk-mastering/batches/`)
    - `cloud_status`: Poll cloud bulk mastering batch progress (`GET /api/bulk-mastering/batches/{id}/status/`)
    - `cloud_export_zip`: Fetch completed multi-track ZIP archive (`GET /api/bulk-mastering/batches/{id}/export-zip/`)
    """
    act = action.strip().lower()
    if act == "master_local_folder":
        if not input_dir or not output_dir:
            raise ValueError("input_dir and output_dir are required for action='master_local_folder'")
        await _notify_progress(ctx, 10.0, 100.0, f"Batch-mastering local album folder: {input_dir}")
        res = await asyncio.to_thread(
            master_album_folder,
            input_dir=input_dir,
            output_dir=output_dir,
            preset=preset,
            stereo_width_pct=stereo_width_pct,
            target_lufs=target_lufs,
            true_peak_ceiling_dbtp=true_peak_ceiling_dbtp,
            bit_depth=bit_depth,
            album_title=album_title,
            artist_name=artist_name,
        )
        await _notify_progress(ctx, 100.0, 100.0, f"Completed album mastering ({res['total_tracks']} tracks).")
        return res

    client = SoundBoostClient()
    if act == "cloud_start":
        if not track_ids:
            raise ValueError("track_ids is required for action='cloud_start'")
        await _notify_progress(ctx, 15.0, 100.0, f"Starting cloud bulk mastering for {len(track_ids)} tracks...")
        res = await asyncio.to_thread(
            client.start_bulk_mastering,
            track_ids,
            mode=mode,
            preset=preset,
            prompt=prompt,
            target_lufs=target_lufs,
        )
        bid = res.get("id") or res.get("batch_id")
        if wait_for_completion and bid is not None:
            return await _poll_until_ready(
                ctx,
                lambda: client.get_bulk_mastering_status(bid),
                poll_interval_sec=poll_interval_sec,
                max_wait_sec=max_wait_sec,
                task_label="Cloud Bulk Mastering",
            )
        return res
    if act == "cloud_list":
        return await asyncio.to_thread(client.list_bulk_mastering_batches)
    if act == "cloud_status":
        if batch_id is None:
            raise ValueError("batch_id is required for action='cloud_status'")
        return await asyncio.to_thread(client.get_bulk_mastering_status, batch_id)
    if act == "cloud_export_zip":
        if batch_id is None:
            raise ValueError("batch_id is required for action='cloud_export_zip'")
        return await asyncio.to_thread(client.get_bulk_mastering_export_zip, batch_id)
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"cloud", "youtube", "soundcloud", "ingest"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="Direct YouTube & SoundCloud Track Ingestion (Mastering & Studio)",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def soundboost_youtube_and_soundcloud_ingest(
    action: str,
    url_or_path: str | None = None,
    job_id: str | int | None = None,
    ctx: Context | None = None,
) -> Any:
    """Ingest audio directly from YouTube, SoundCloud, or local files into SoundBoost Mastering or Studio Stem Splitter:
    - `mastering_import_youtube`: Import YouTube URL into Mastering (`POST /api/tracks/import-youtube/`)
    - `mastering_youtube_direct_url`: Resolve direct YouTube stream URL (`GET /api/tracks/youtube-direct-url/`)
    - `mastering_import_soundcloud`: Import SoundCloud URL into Mastering (`POST /api/tracks/import-soundcloud/`)
    - `mastering_soundcloud_status`: Poll SoundCloud import status (`GET /api/tracks/import-soundcloud/status/`)
    - `mastering_soundcloud_direct_url`: Resolve direct SoundCloud stream URL (`GET /api/tracks/soundcloud-direct-url/`)
    - `studio_import_youtube`: Import YouTube URL into Studio Stem Splitter (`POST /api/studio/public/import-youtube`)
    - `studio_import_soundcloud`: Import SoundCloud URL into Studio Stem Splitter (`POST /api/studio/public/import-soundcloud`)
    - `studio_upload_original`: Upload local audio file to Studio Originals (`POST /api/studio/originals/upload/`)
    - `studio_list_ready`: List all ready Studio original tracks (`GET /api/studio/originals/ready-all`)
    """
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "studio_list_ready":
        return await asyncio.to_thread(client.list_ready_studio_originals)
    if act == "mastering_soundcloud_status":
        if job_id is None:
            raise ValueError("job_id is required for action='mastering_soundcloud_status'")
        return await asyncio.to_thread(client.get_soundcloud_import_status, job_id)
    if not url_or_path:
        raise ValueError(f"url_or_path is required for action='{action}'")

    await _notify_progress(ctx, 20.0, 100.0, f"Executing {act} for {url_or_path}...")
    if act == "mastering_import_youtube":
        return await asyncio.to_thread(client.import_youtube_for_mastering, url_or_path)
    if act == "mastering_youtube_direct_url":
        return await asyncio.to_thread(client.get_youtube_direct_url, url_or_path)
    if act == "mastering_import_soundcloud":
        return await asyncio.to_thread(client.import_soundcloud_for_mastering, url_or_path)
    if act == "mastering_soundcloud_direct_url":
        return await asyncio.to_thread(client.get_soundcloud_direct_url, url_or_path)
    if act == "studio_import_youtube":
        return await asyncio.to_thread(client.import_youtube_to_studio, url_or_path)
    if act == "studio_import_soundcloud":
        return await asyncio.to_thread(client.import_soundcloud_to_studio, url_or_path)
    if act == "studio_upload_original":
        return await asyncio.to_thread(client.upload_studio_original, url_or_path)
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"ai", "prompt", "voice", "toki", "eq"},
    annotations=ToolAnnotations(
        title="Voice-Controlled AI Mastering & Natural-Language Toki Prompt Translator",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def soundboost_voice_and_prompt_assistant(
    action: str = "local_prompt_to_eq",
    prompt_or_speech: str = "warm low end, clean mud, crisp vocal presence, airy 15kHz top end",
    base_preset: str = "flat",
    base_target_lufs: float = -14.0,
    track_id: str | int | None = None,
) -> dict[str, Any]:
    """Translate natural-language or spoken mastering instructions into 18-Band EQ parameters (`Toki` & `Deepgram` Voice APIs):
    - `local_prompt_to_eq`: Offline deterministic translator mapping natural-language vibe descriptors to an 18-band EQ curve, stereo width %, and LUFS target
    - `cloud_ai_prompt`: Translate prompt via SoundBoost Cloud AI (`POST /api/ai/prompt`)
    - `toki_check`: Check status of the Toki AI Mastering assistant (`GET /api/toki/check/`)
    - `mastering_voice_token`: Acquire Deepgram token for Voice-Controlled Mastering (`POST /api/mastering-voice/deepgram-token`)
    - `mastering_voice_speech`: Submit spoken mastering revision transcript (`POST /api/mastering-voice/speech`)
    - `mv_voice_token`: Acquire Deepgram token for Voice-Controlled Music Video Director (`POST /api/music-video-voice/deepgram-token`)
    """
    act = action.strip().lower()
    if act == "local_prompt_to_eq":
        return translate_prompt_to_18band_eq(
            prompt=prompt_or_speech,
            base_preset=base_preset,
            base_target_lufs=base_target_lufs,
        )
    client = SoundBoostClient()
    if act == "cloud_ai_prompt":
        return client.translate_ai_mastering_prompt(prompt_or_speech, track_id=track_id)
    if act == "toki_check":
        return client.check_toki_assistant()
    if act == "mastering_voice_token":
        return client.get_mastering_voice_deepgram_token()
    if act == "mastering_voice_speech":
        return client.send_mastering_voice_speech(prompt_or_speech, track_id=track_id)
    if act == "mv_voice_token":
        return client.get_music_video_voice_deepgram_token()
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"cloud", "music-video", "shoots", "director"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="Full AI Music Video (h3-max) & Shoots Director Orchestrator",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def soundboost_music_video_and_shoots_director(
    action: str = "shoots_sso_url",
    video_or_shoot_id: str | int | None = None,
    scene_id: str | int | None = None,
    director_message: str | None = None,
    payload: dict[str, Any] | None = None,
    page: int = 1,
) -> Any:
    """Orchestrate AI Music Videos (`h3-max`), `Shoots` storyboard scenes, Director Chat, Spotify 9:16 Canvas clips, and `shoots.soundboost.ai` SSO:
    - `shoots_sso_url`: Build cross-domain SSO URL (`https://mastering.soundboost.ai/sso?provider=google&appName=shoots&gacTkn=...`) and exchange SSO token
    - `list_music_videos`: List AI Music Video (`h3-max`) projects (`GET /api/music-videos/`)
    - `create_music_video`: Create AI Music Video project (`POST /api/music-videos/`)
    - `get_music_video`: Fetch AI Music Video project details (`GET /api/music-videos/{id}/`)
    - `list_shoots`: List Shoots storyboard sessions (`GET /api/shoots/`)
    - `create_shoot`: Create Shoot session (`POST /api/shoots/`)
    - `get_shoot`: Fetch Shoot storyboard session (`GET /api/shoots/{id}`)
    - `director_message`: Send natural-language Director Chat instructions (`POST /api/shoots/{id}/director-message`)
    - `regenerate_all_scenes`: Trigger batch scene regeneration & Auto-QC retakes (`POST /api/shoots/{id}/scenes/regenerate-all`)
    - `list_video_clips`: List vertical video clips / Spotify 9:16 Canvas exports (`GET /api/video-clips/`)
    - `create_video_clip`: Create vertical video clip / Spotify Canvas (`POST /api/video-clips/`)
    - `list_comments`: List `/watch-film` community comments (`GET /api/music-videos/comments/`)
    - `post_comment`: Post comment on a music video (`POST /api/music-videos/comments/`)
    - `following_feed`: Fetch `/watch-film` following feed (`GET /api/music-videos/following/?page=`)
    """
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "shoots_sso_url":
        return {
            "shoots_google_sso_url": client.build_shoots_google_sso_url(),
            "shoots_service_url": "https://shoots.soundboost.ai",
            "features": [
                "Musician AI Clones (Consistent Artist Likeness)",
                "Advanced Editorial & Press Photo Generator",
                "Spotify 9:16 Canvas Loop Generator",
                "3000x3000 DistroKid-Compliant Album Cover Generator",
            ],
        }
    if act == "list_music_videos":
        return await asyncio.to_thread(client.list_music_videos)
    if act == "create_music_video":
        return await asyncio.to_thread(client.create_music_video, payload or {})
    if act == "get_music_video":
        if video_or_shoot_id is None:
            raise ValueError("video_or_shoot_id is required for action='get_music_video'")
        return await asyncio.to_thread(client.get_music_video, video_or_shoot_id)
    if act == "list_shoots":
        return await asyncio.to_thread(client.list_shoots)
    if act == "create_shoot":
        return await asyncio.to_thread(client.create_shoot, payload or {})
    if act == "get_shoot":
        if video_or_shoot_id is None:
            raise ValueError("video_or_shoot_id is required for action='get_shoot'")
        return await asyncio.to_thread(client.get_shoot, video_or_shoot_id)
    if act == "director_message":
        if video_or_shoot_id is None or not director_message:
            raise ValueError("video_or_shoot_id and director_message are required for action='director_message'")
        return await asyncio.to_thread(
            client.send_shoot_director_message,
            video_or_shoot_id,
            director_message,
            scene_id=scene_id,
        )
    if act == "regenerate_all_scenes":
        if video_or_shoot_id is None:
            raise ValueError("video_or_shoot_id is required for action='regenerate_all_scenes'")
        return await asyncio.to_thread(
            client.regenerate_all_shoot_scenes, video_or_shoot_id, payload
        )
    if act == "list_video_clips":
        return await asyncio.to_thread(client.list_video_clips)
    if act == "create_video_clip":
        return await asyncio.to_thread(client.create_video_clip, payload or {})
    if act == "list_comments":
        return await asyncio.to_thread(client.list_music_video_comments, video_or_shoot_id)
    if act == "post_comment":
        if video_or_shoot_id is None or not director_message:
            raise ValueError("video_or_shoot_id and director_message (comment text) are required")
        return await asyncio.to_thread(
            client.post_music_video_comment, video_or_shoot_id, director_message
        )
    if act == "following_feed":
        return await asyncio.to_thread(client.list_following_music_videos, page)
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"dsp", "practice", "chords", "pitch-shift", "mp3", "gapless"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="Studio Practice Chord Voicings (Guitar/Ukulele/Piano), Local Pitch/Time-Stretch & Gapless MP3 Inspector",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def studio_practice_voicings_and_local_shifter(
    action: str = "chord_voicings",
    chords: list[str] | None = None,
    instrument: str = "guitar",
    input_path: str | None = None,
    output_path: str | None = None,
    semitones: float = 0.0,
    speed_factor: float = 1.0,
    bit_depth: int = 24,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Local Studio Practice & Calibration tools (exact replicas of Studio Modules 534, 590-591, 594-601):
    - `chord_voicings`: Generate interactive Chord Fingering Voicings & ASCII fretboard/keyboard diagrams for `guitar` (6-string), `ukulele` (4-string), or `piano` (MIDI + keys)
    - `pitch_time_stretch`: Render local offline Pitch-Shifting (`semitones` in [-24, +24], pitchFactor = 2^(st/12)) and Phase-Vocoder Time-Stretching (`speed_factor` in [0.25, 4.0])
    - `inspect_gapless_mp3`: Parse binary MP3 frames (`1152` samples/frame), `Xing`/`Info`/`LAME3.100` gapless headers, encoder delay (`576`), decoder delay (`529`), and total priming samples (`1105`)
    """
    act = action.strip().lower()
    if act == "chord_voicings":
        chord_list = chords or ["Cm", "Abmaj7", "Eb", "Bb"]
        diagrams = [get_chord_voicing_diagram(c, instrument=instrument) for c in chord_list]
        return {
            "instrument": instrument.lower(),
            "chord_count": len(diagrams),
            "voicings": diagrams,
        }
    if act == "pitch_time_stretch":
        if not input_path or not output_path:
            raise ValueError("input_path and output_path are required for action='pitch_time_stretch'")
        await _notify_progress(
            ctx,
            15.0,
            100.0,
            f"Rendering local pitch shift ({semitones:+.1f} st) & time stretch ({speed_factor:.2f}x)...",
        )
        res = await asyncio.to_thread(
            render_local_pitch_and_time_stretch,
            input_path=input_path,
            output_path=output_path,
            semitones=semitones,
            speed_factor=speed_factor,
            bit_depth=bit_depth,
        )
        await _notify_progress(ctx, 100.0, 100.0, f"Saved pitch/time-stretched audio -> {output_path}")
        return res
    if act == "inspect_gapless_mp3":
        if not input_path:
            raise ValueError("input_path is required for action='inspect_gapless_mp3'")
        return await asyncio.to_thread(inspect_mp3_gapless_headers, input_path)
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"cloud", "tracks", "telemetry", "waveform"},
    annotations=ToolAnnotations(
        title="SoundBoost Track Lifecycle, Mastering Telemetry & Waveform Manager",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
def soundboost_track_lifecycle_and_telemetry(
    action: str = "master_stats",
    track_id: str | int | None = None,
    track_ids: list[str | int] | None = None,
    image_path_or_url: str | None = None,
    download_format: str = "wav",
    public_enabled: bool = True,
) -> Any:
    """Manage SoundBoost tracks, pre/post mastering telemetry, stem-assisted prep, waveforms, and public links:
    - `master_stats`: Fetch pre/post mastering loudness, peak, dynamic range & EQ telemetry (`GET /api/get-master-stats`)
    - `stem_prep`: Run stem-assisted mastering preparation (`POST /api/mastering/stem-prep`)
    - `song_master`: Fetch song master metadata (`GET /api/songs/{id}/master`)
    - `song_download`: Get signed download link (`GET /api/songs/{id}/download`)
    - `generate_waveform`: Generate waveform peak telemetry (`POST /api/waveform/generate/`)
    - `toggle_favorite`: Toggle favorite star on a track (`POST /api/toggle-favorite/`)
    - `update_cover_art`: Upload or assign cover art to a track (`POST /api/tracks/{id}/cover-art/`)
    - `public_link`: Create or toggle a shareable public link (`POST /api/tracks/{id}/public-link/`)
    - `public_tracks_by_ids`: Fetch public tracks by ID list (`GET /api/public/tracks/by-id/`)
    - `delete_track`: Delete an uploaded source track (`POST /api/delete-track/`)
    - `delete_mastered`: Delete a mastered track version (`POST /api/delete-mastered/`)
    """
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "public_tracks_by_ids":
        if not track_ids:
            raise ValueError("track_ids is required for action='public_tracks_by_ids'")
        return client.get_public_tracks_by_ids(track_ids)
    if track_id is None:
        raise ValueError(f"track_id is required for action='{action}'")
    if act == "master_stats":
        return client.get_master_stats(track_id)
    if act == "stem_prep":
        return client.start_mastering_stem_prep(track_id)
    if act == "song_master":
        return client.get_song_master(track_id)
    if act == "song_download":
        return client.get_song_download(track_id, fmt=download_format)
    if act == "generate_waveform":
        return client.generate_waveform(track_id)
    if act == "toggle_favorite":
        return client.toggle_favorite_track(track_id)
    if act == "update_cover_art":
        if not image_path_or_url:
            raise ValueError("image_path_or_url is required for action='update_cover_art'")
        return client.update_track_cover_art(track_id, image_path_or_url)
    if act == "public_link":
        return client.create_track_public_link(track_id, enabled=public_enabled)
    if act == "delete_track":
        return client.delete_track(track_id)
    if act == "delete_mastered":
        return client.delete_mastered_track(track_id)
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool(
    tags={"cloud", "account", "profile", "notifications", "billing"},
    annotations=ToolAnnotations(
        title="SoundBoost Artist Profile, Social Notifications & Subscription Billing Manager",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
def soundboost_account_profile_and_billing(
    action: str = "billing_info",
    username: str | None = None,
    product_id: str | int | None = None,
    notification_ids: list[str | int] | None = None,
    payload: dict[str, Any] | None = None,
    page: int = 1,
) -> Any:
    """Manage SoundBoost Artist Profile, Social Notifications, Referrals & Subscription Billing:
    - `get_profile`: Fetch authenticated artist profile (`GET /api/account/profile/`)
    - `update_profile`: Update authenticated artist profile (`PATCH /api/account/profile/`)
    - `check_username`: Check handle availability (`GET /api/account/profile/username-availability/`)
    - `public_profile`: Fetch public profile by username (`GET /api/public/profiles/{username}/`)
    - `notifications`: List social notifications (`GET /api/social/notifications/?page=`)
    - `unread_count`: Get unread notification count (`GET /api/social/notifications/unread-count/`)
    - `mark_read`: Mark notifications read (`POST /api/social/notifications/read/`)
    - `billing_info`: Fetch subscription, credit balance & billing details (`GET /api/billing/info`)
    - `products`: List available subscription plans & credit packs (`GET /api/products/`)
    - `checkout_session`: Create Stripe checkout session (`POST /api/create-checkout-session`)
    - `portal_session`: Create Stripe customer billing portal session (`POST /api/create-customer-portal-session/`)
    - `pause_subscription`: Pause active subscription (`POST /api/pause-subscription/`)
    - `resume_subscription`: Resume paused subscription (`POST /api/resume-subscription/`)
    - `referral_info`: Fetch artist referral link and reward stats (`GET /api/get-referral-info`)
    """
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "get_profile":
        return client.get_account_profile()
    if act == "update_profile":
        return client.update_account_profile(payload or {})
    if act == "check_username":
        if not username:
            raise ValueError("username is required for action='check_username'")
        return client.check_username_availability(username)
    if act == "public_profile":
        if not username:
            raise ValueError("username is required for action='public_profile'")
        return client.get_public_artist_profile(username)
    if act == "notifications":
        return client.list_notifications(page=page)
    if act == "unread_count":
        return client.get_unread_notifications_count()
    if act == "mark_read":
        return client.mark_notifications_read(notification_ids)
    if act == "billing_info":
        return client.get_billing_info()
    if act == "products":
        return {"products": client.get_products()}
    if act == "checkout_session":
        if product_id is None:
            raise ValueError("product_id is required for action='checkout_session'")
        return client.create_checkout_session(product_id, extra=payload)
    if act == "portal_session":
        return client.create_customer_portal_session()
    if act == "pause_subscription":
        return client.pause_subscription()
    if act == "resume_subscription":
        return client.resume_subscription()
    if act == "referral_info":
        return client.get_referral_info()
    raise ValueError(f"Unsupported action '{action}'")



# ---------------------------------------------------------------------------
# 3. MCP Resources & Prompts
# ---------------------------------------------------------------------------
@mcp.resource(
    "soundboost://specs/capabilities",
    name="SoundBoost Platform Capabilities Matrix",
    description="Complete specification of 18-Band EQ frequencies, stem profiles, drum sub-stems, Converta formats, and streaming loudness targets.",
    mime_type="application/json",
)
def resource_platform_capabilities() -> str:
    return json.dumps(list_soundboost_platform_capabilities(), indent=2)


@mcp.resource(
    "soundboost://specs/eq-presets",
    name="18-Band Mastering Genre EQ Curves",
    description="Exact 18-band dB gain arrays (20 Hz to 15 kHz) for all 12 genre presets.",
    mime_type="application/json",
)
def resource_eq_presets() -> str:
    return json.dumps(
        {
            "frequencies_hz": list(MASTERING_EQ_FREQUENCIES_HZ),
            "presets": {k: list(v) for k, v in PRESET_EQ_CURVES_18BAND.items()},
        },
        indent=2,
    )


@mcp.resource(
    "soundboost://specs/reaper-jsfx",
    name="Suavecito REAPER 18-Band Mastering EQ JSFX Source",
    description="Source code of the REAPER JSFX 18-Band Mastering EQ, Mid/Side Stereo Width & Brickwall Ceiling Limiter plugin.",
    mime_type="text/plain",
)
def resource_reaper_jsfx() -> str:
    jsfx_path = (
        Path(__file__).resolve().parent.parent
        / "reaper"
        / "Effects"
        / "Suavecito"
        / "SoundBoost_18Band_Mastering_EQ_Monitor.jsfx"
    )
    return jsfx_path.read_text(encoding="utf-8")


@mcp.prompt(
    name="distrokid_release_mastering_workflow",
    description="Step-by-step release engineering workflow for auditing, cleaning stems, mastering, and tagging tracks for DistroKid.",
)
def prompt_distrokid_release_workflow(track_path: str, genre: str = "pop", isrc: str = "") -> str:
    return (
        f"Execute a complete DistroKid release engineering pass on `{track_path}` (genre: `{genre}`):\n"
        "1. Call `audit_audio_loudness_and_release_readiness` on the source file to inspect Integrated LUFS, "
        "LRA (P95-P10), 4x oversampled True Peak (dBTP) + 100ms peak timestamp, stereo correlation, and high-frequency roll-off.\n"
        "2. If cymbal harshness or vocal/instrument imbalance is detected, use `soundboost_studio_stems_and_drums` "
        "(`hifi_6stem` + `start_drums` for Kick/Snare/Toms/Hi-Hat/Cymbals) or import into REAPER via `Suavecito_SoundBoost_Stem_Import.lua`.\n"
        f"3. Render the 24-bit / 48 kHz master using `render_local_18band_master` (preset=`{genre}`, target_lufs=-14.0, true_peak_ceiling_dbtp=-1.0, bit_depth=24, apply_tpdf_dither=True).\n"
        "4. Re-audit the mastered file with `audit_audio_loudness_and_release_readiness` to confirm `distrokid_ready == True`.\n"
        f"5. Embed release metadata and ISRC (`{isrc or 'CC-XXX-YY-NNNNN'}`) using `write_release_metadata_tags`."
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
