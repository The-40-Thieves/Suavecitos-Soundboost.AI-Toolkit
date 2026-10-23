"""FastMCP Server for Suavecito's Soundboost.AI Toolkit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from soundboost_toolkit.client import SoundBoostClient
from soundboost_toolkit.constants import (
    CONVERTA_FORMATS,
    DRUM_KIT_SUBSTEMS,
    MASTERING_EQ_FREQUENCIES_HZ,
    MASTERING_GENRE_PRESETS,
    MASTERING_MODES,
    MV_CHARACTER_REF_CAPS,
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
    quantize_loop_to_bar_beat_sixteenth,
    render_18band_master,
    render_lead_sheet_html,
    snap_timestamp_to_synchronized_beat,
    transpose_chord_symbol,
)
from soundboost_toolkit.reaper_integration import install_reaper_assets
from soundboost_toolkit.tagging import read_audio_tags, write_audio_tags

mcp = FastMCP(
    "suavecitos-soundboost-toolkit",
    instructions=(
        "Suavecito's Soundboost.AI Toolkit — Complete MCP server for SoundBoost.ai "
        "(Diktatorial Suite) cloud APIs, local ITU-R BS.1770-4 / EBU R128 loudness & "
        "100ms True-Peak diagnostics, 18-Band Mastering EQ rendering, ID3v2.3/ISRC tagging, "
        "and REAPER JSFX/ReaScript integration."
    ),
)


@mcp.tool()
def audit_audio_loudness_and_release_readiness(file_path: str) -> dict[str, Any]:
    """Run ITU-R BS.1770-4 Loudness (Integrated/Short-Term/Momentary LUFS), SoundBoost LRA (P95-P10),
    4x oversampled True Peak (dBTP) with 100ms binary-search peak timestamp, stereo correlation,
    6-band spectral profile, 9-platform Streaming Loudness Penalty table, and DistroKid readiness check.
    """
    return analyze_audio_file(file_path).to_dict()


@mcp.tool()
def render_local_18band_master(
    input_path: str,
    output_path: str,
    preset: str | None = None,
    eq_gains_db: list[float] | None = None,
    stereo_width_pct: float = 100.0,
    target_lufs: float | None = -14.0,
    true_peak_ceiling_dbtp: float = -1.0,
    bit_depth: int = 24,
    apply_tpdf_dither: bool = True,
) -> dict[str, Any]:
    """Render a local master using SoundBoost's 18-Band Graphic EQ (20 Hz to 15 kHz or a genre preset),
    Mid/Side Stereo Width (0-200%), LUFS normalization, lookahead True-Peak brickwall limiter,
    and 16/24-bit TPDF dithering.
    """
    return render_18band_master(
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


@mcp.tool()
def read_release_metadata_tags(file_path: str) -> dict[str, Any]:
    """Read ID3v2.3 / FLAC VorbisComment / WAV metadata tags including 12-char ISRC (TSRC), BPM, Key, Publisher, Lyrics, and Cover Art."""
    return read_audio_tags(file_path)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def soundboost_upload_and_master(
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
) -> dict[str, Any]:
    """Interact with SoundBoost Cloud AI Mastering, Mix Doctor & Audio Enhancer:
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
        return client.upload_audio(file_path)
    if track_id is None:
        raise ValueError(f"track_id is required for action='{action}'")
    if act == "master":
        return client.start_mastering(
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
    if act == "status":
        return client.get_track_status(track_id)
    if act == "mix_doctor":
        return client.run_mix_doctor(track_id, genre=preset or "pop")
    if act == "enhance":
        return client.run_audio_enhancer(track_id, intensity=enhance_intensity)
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool()
def soundboost_studio_stems_and_drums(
    action: str,
    original_id: str | int,
    profile_key: str = "standard_4stem",
    stem_variant: str | None = None,
    stem_format: str | None = None,
    handoff_source: str = "mix",
    stem_name: str | None = None,
    loop_start_sec: float | None = None,
    loop_end_sec: float | None = None,
) -> dict[str, Any]:
    """Control `studio.soundboost.ai` Stem Separation, Voice Cleaner & 5-Piece Drum Kit Splitter:
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
        return client.get_studio_original_status(original_id, stem_variant, stem_format)
    if act == "start_stems":
        return client.start_studio_stem_split(original_id, profile_key=profile_key)
    if act == "get_stems":
        return client.get_studio_stems(original_id, stem_variant=stem_variant, stem_format=stem_format)
    if act == "start_voice_clean":
        return client.start_studio_voice_clean(original_id)
    if act == "start_drums":
        return client.start_studio_drum_split(original_id)
    if act == "get_drums":
        return client.get_studio_drum_stems(original_id)
    if act == "handoff_to_mastering":
        return client.handoff_studio_to_mastering(
            original_id,
            source_type=handoff_source,
            stem_name=stem_name,
            loop_start_sec=loop_start_sec,
            loop_end_sec=loop_end_sec,
        )
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool()
def soundboost_studio_chords_and_lyrics(
    action: str,
    original_id: str | int,
    edited_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Control `studio.soundboost.ai` AI Chord Detection & Word-Synced Lyrics/Transcripts:
    - `start_chords`: Start beat-aligned AI Chord Recognition
    - `get_chords`: Fetch detected chords and synchronized beat timestamps
    - `start_lyrics`: Start word-level AI Lyric / Voice-Clean Transcript recognition
    - `get_lyrics`: Fetch word-timed lyric segments
    - `edit_lyrics`: Save edited lyric segments (`[{id, text}]`)
    """
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "start_chords":
        return client.start_studio_chord_analysis(original_id)
    if act == "get_chords":
        return client.get_studio_chord_analysis(original_id)
    if act == "start_lyrics":
        return client.start_studio_lyrics(original_id)
    if act == "get_lyrics":
        return client.get_studio_lyrics(original_id)
    if act == "edit_lyrics":
        if edited_segments is None:
            raise ValueError("edited_segments is required for action='edit_lyrics'")
        return client.save_studio_lyrics_edits(original_id, edited_segments)
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool()
def soundboost_converta_audio(
    action: str = "formats",
    source_id_or_url: str | None = None,
    job_id: str | None = None,
    output_format: str = "wav",
    bit_depth: int = 24,
    sample_rate: int = 48000,
    dithering: bool = True,
) -> dict[str, Any]:
    """Interact with `converta.soundboost.ai` Hi-Res Audio Converter:
    - `formats`: Query live supported output formats, bit depths (`16/24/32`), sample rates (`44.1/48kHz`), and dithering support
    - `convert`: Start conversion job (`POST /api/v1/convert`)
    - `status`: Poll conversion job status (`GET /api/v1/status/{job_id}`)
    """
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "formats":
        return client.get_converta_formats()
    if act == "convert":
        if not source_id_or_url:
            raise ValueError("source_id_or_url is required for action='convert'")
        return client.start_converta_conversion(
            upload_id_or_url=source_id_or_url,
            output_format=output_format,
            bit_depth=bit_depth,
            sample_rate=sample_rate,
            dithering=dithering,
        )
    if act == "status":
        if not job_id:
            raise ValueError("job_id is required for action='status'")
        return client.get_converta_status(job_id)
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool()
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


@mcp.tool()
def soundboost_text_to_audio_and_mv_budget(
    action: str = "mv_budget",
    prompt: str | None = None,
    duration_sec: int = 10,
    generation_id: str | int | None = None,
    page: int = 1,
    quoted_credits: int = 100,
    plan_tier: str = "director",
) -> dict[str, Any]:
    """Generate AI Text-to-Audio / SFX or compute AI Music Video (`h3-max`) credit spending caps:
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
        return client.generate_text_to_audio(prompt=prompt, duration_sec=duration_sec)
    if act == "sfx_status":
        if generation_id is None:
            raise ValueError("generation_id is required for action='sfx_status'")
        return client.get_text_to_audio_status(generation_id)
    if act == "list_public_sfx":
        return client.list_public_text_to_audio(page=page)
    raise ValueError(f"Unsupported action '{action}'")


@mcp.tool()
def install_reaper_jsfx_and_scripts(reaper_resource_dir: str | None = None) -> dict[str, Any]:
    """Install Suavecito's SoundBoost 18-Band Mastering EQ JSFX plugin and Multitrack Stem Importer ReaScript into REAPER."""
    return install_reaper_assets(reaper_resource_dir)


@mcp.tool()
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
