"""Rich Typer CLI for Suavecito's Soundboost.AI Toolkit (`soundboost`)."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from soundboost_toolkit.client import SoundBoostClient
from soundboost_toolkit.constants import (
    MASTERING_GENRE_PRESETS,
    MV_CHARACTER_REF_CAPS,
    STEM_PROFILES,
    compute_mv_spending_limit,
)
from soundboost_toolkit.dsp import (
    analyze_audio_file,
    classify_pitch_shift_zone,
    get_chord_voicing_diagram,
    inspect_mp3_gapless_headers,
    quantize_loop_to_bar_beat_sixteenth,
    transpose_chord_symbol,
)
from soundboost_toolkit.mcp_server import list_soundboost_platform_capabilities, redact_login_result
from soundboost_toolkit.oauth import (
    clear_vault_credentials,
    generate_google_pkce_challenge,
    get_vault_status,
    run_loopback_oauth_handoff,
    save_credentials_to_vault,
)
from soundboost_toolkit.reaper_integration import (
    ReaperLiveBridge,
    build_reaper_to_shoots_storyboard,
    configure_reaper_ini_bridge,
    generate_soundboost_rpp_project,
    inspect_reaper_environment,
    install_reaper_assets,
    render_reaper_video_pipeline,
)
from soundboost_toolkit.tagging import read_audio_tags, write_audio_tags

app = typer.Typer(
    name="soundboost",
    help="Master, split, tag, and ship your tracks. Unofficial SoundBoost.ai toolkit, built with their permission.",
    no_args_is_help=True,
)
console = Console()


@app.command("audit")
def cmd_audit(
    file_path: str = typer.Argument(..., help="Path to local WAV, FLAC, AIFF, or MP3 file"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON output"),
) -> None:
    """Run ITU-R BS.1770-4 Loudness, LRA, 100ms True-Peak, Mix Doctor & DistroKid Release Audit."""
    res = analyze_audio_file(file_path)
    if as_json:
        console.print_json(data=res.to_dict())
        return

    metrics = Table(title=f"ITU-R BS.1770-4 & Mix Doctor Audit — {res.file_path}", show_header=True)
    metrics.add_column("Metric", style="bold cyan")
    metrics.add_column("Value", style="white")
    metrics.add_row("Format / Sample Rate", f"{res.bit_depth_subtype} @ {res.sample_rate} Hz ({res.channels} ch)")
    metrics.add_row("Duration", f"{res.duration_sec:.2f} s")
    metrics.add_row("Integrated Loudness", f"{res.integrated_lufs:.2f} LUFS")
    metrics.add_row("Short-Term Max (3s)", f"{res.short_term_max_lufs:.2f} LUFS")
    metrics.add_row("Momentary Max (400ms)", f"{res.momentary_max_lufs:.2f} LUFS")
    metrics.add_row("Loudness Range (LRA P95-P10)", f"{res.loudness_range_lu:.2f} LU")
    metrics.add_row("Sample Peak", f"{res.sample_peak_dbfs:.2f} dBFS")
    metrics.add_row(
        "4x True Peak (100ms Binary Search)",
        f"{res.true_peak_dbtp:.2f} dBTP @ {res.true_peak_time_sec:.2f} s",
    )
    metrics.add_row("PSR (Peak-to-Short-term Loudness)", f"{res.psr_db:.2f} dB")
    metrics.add_row("Stereo Phase Correlation", f"{res.stereo_correlation:.3f} (M/S: {res.mid_side_ratio_db:.2f} dB)")
    metrics.add_row("Estimated High-Freq Bandwidth", f"{res.estimated_bandwidth_hz:.0f} Hz")
    metrics.add_row(
        "Release Ready (toolkit heuristic)",
        "[bold green]YES[/bold green]" if res.release_ready_heuristic else "[bold yellow]NEEDS MASTERING[/bold yellow]",
    )
    console.print(metrics)

    pen_table = Table(title="Streaming Loudness Penalty Matrix", show_header=True)
    pen_table.add_column("Platform", style="cyan")
    pen_table.add_column("Target LUFS", justify="right")
    pen_table.add_column("Ceiling dBTP", justify="right")
    pen_table.add_column("Gain Change", justify="right")
    pen_table.add_column("Peak Status", justify="center")
    for p in res.streaming_penalties.values():
        delta = p["gain_change_db"]
        delta_str = f"{delta:+.2f} dB"
        clip_str = "[red]EXCEEDS CEILING[/red]" if p["true_peak_exceeds_ceiling"] else "[green]OK[/green]"
        pen_table.add_row(
            str(p["platform"]),
            f"{p['target_lufs']:.1f}",
            f"{p['ceiling_dbtp']:.1f}",
            delta_str,
            clip_str,
        )
    console.print(pen_table)

    rec_text = "\n".join(f"• {r}" for r in res.mix_doctor_recommendations)
    console.print(Panel(rec_text, title="Mix Doctor Recommendations", border_style="blue"))


@app.command("master")
def cmd_master(
    action: str = typer.Argument(
        "status",
        help="upload | master | status | mix-doctor | enhance | stats | stem-prep",
    ),
    file_path: str | None = typer.Option(None, "--file", "-f", help="Audio file path to upload"),
    track_id: str | None = typer.Option(None, "--track-id", "-id", help="Uploaded SoundBoost track ID"),
    mode: str = typer.Option("auto", "--mode", "-m", help="Mastering mode: prompt | preset | auto | reference | chat | manual | mix_doctor | enhance"),
    preset: str = typer.Option("pop", "--preset", "-p", help=f"Genre preset ({', '.join(MASTERING_GENRE_PRESETS)})"),
    prompt: str | None = typer.Option(None, "--prompt", help="Natural-language mastering prompt"),
    eq_csv: str | None = typer.Option(None, "--eq", help="18 comma-separated dB values (-6.0 to +6.0) for 20Hz..15kHz"),
    width: float | None = typer.Option(None, "--width", "-w", help="Mid/Side stereo width % (0-200)"),
    target_lufs: float | None = typer.Option(None, "--lufs", "-l", help="Target Integrated LUFS"),
) -> None:
    """Run pay-gated SoundBoost Cloud AI Mastering (`/api/tracks/{id}/master/`), Mix Doctor, or Audio Enhancer."""
    client = SoundBoostClient()
    act = action.strip().lower().replace("_", "-")
    if act == "upload":
        if not file_path:
            raise typer.BadParameter("--file is required for action='upload'")
        console.print_json(data=client.upload_audio(file_path))
        return
    if not track_id:
        raise typer.BadParameter("--track-id is required for this action")
    if act == "master":
        eq_list: list[float] | None = None
        if eq_csv:
            eq_list = [float(x.strip()) for x in eq_csv.split(",") if x.strip()]
        res = client.start_mastering(
            track_id,
            mode=mode,
            prompt=prompt,
            preset=preset,
            eq_18band_db=eq_list,
            stereo_width_pct=width,
            target_lufs=target_lufs,
        )
    elif act == "status":
        res = client.get_track_status(track_id)
    elif act == "mix-doctor":
        res = client.run_mix_doctor(track_id, genre=preset)
    elif act == "enhance":
        res = client.run_audio_enhancer(track_id)
    elif act == "stats":
        res = client.get_master_stats(track_id)
    elif act == "stem-prep":
        res = client.start_mastering_stem_prep(track_id)
    else:
        raise typer.BadParameter(f"Unknown master action '{action}'")
    console.print_json(data=res)


@app.command("tags")
def cmd_tags(
    file_path: str = typer.Argument(..., help="Path to MP3, FLAC, or WAV file"),
    title: str | None = typer.Option(None, "--title", help="Track title (TIT2)"),
    artist: str | None = typer.Option(None, "--artist", help="Primary artist (TPE1)"),
    album: str | None = typer.Option(None, "--album", help="Album title (TALB)"),
    year: str | None = typer.Option(None, "--year", help="Release year (TYER)"),
    track: str | None = typer.Option(None, "--track", help="Track number (TRCK)"),
    genre: str | None = typer.Option(None, "--genre", help="Genre (TCON)"),
    bpm: str | None = typer.Option(None, "--bpm", help="Tempo BPM (TBPM)"),
    key: str | None = typer.Option(None, "--key", help="Initial musical key (TKEY)"),
    publisher: str | None = typer.Option(None, "--publisher", help="Record Label / Publisher (TPUB)"),
    isrc: str | None = typer.Option(None, "--isrc", help="12-character ISO 3901 ISRC code (TSRC)"),
    comment: str | None = typer.Option(None, "--comment", help="Comment (COMM)"),
    lyrics: str | None = typer.Option(None, "--lyrics", help="Unsynchronized lyrics (USLT)"),
    cover: str | None = typer.Option(None, "--cover", help="Path to 3000x3000 JPG/PNG cover art (APIC)"),
) -> None:
    """Read or write ID3v2.3 / FLAC / WAV release metadata, 12-char ISRC (`TSRC`), and cover artwork."""
    any_write = any(
        v is not None
        for v in (title, artist, album, year, track, genre, bpm, key, publisher, isrc, comment, lyrics, cover)
    )
    if any_write:
        out = write_audio_tags(
            file_path=file_path,
            title=title,
            artist=artist,
            album=album,
            year=year,
            track=track,
            genre=genre,
            bpm=bpm,
            initial_key=key,
            publisher=publisher,
            isrc=isrc,
            comment=comment,
            lyrics=lyrics,
            cover_art_path=cover,
        )
    else:
        out = read_audio_tags(file_path)
    console.print_json(data=out)


@app.command("grid")
def cmd_grid(
    chords_csv: str = typer.Option("Cm,Abmaj7,Eb,Bb/D", "--chords", "-c", help="Comma-separated chord progression"),
    semitones: int = typer.Option(0, "--semitones", "-s", help="Semitone shift (-12 to +12)"),
    flats: bool = typer.Option(False, "--flats", help="Prefer flat enharmonic spelling"),
    bpm: float = typer.Option(120.0, "--bpm", help="Tempo in BPM"),
    loop_start: float = typer.Option(0.0, "--start", help="Loop start in seconds"),
    loop_end: float = typer.Option(8.0, "--end", help="Loop end in seconds"),
) -> None:
    """Transpose chords, classify Pitch Shift zone (`Natural` vs `Creative`), and quantize loops to `Bar.Beat.1/16`."""
    chords = [c.strip() for c in chords_csv.split(",") if c.strip()]
    out = {
        "pitch_shift": classify_pitch_shift_zone(semitones),
        "chords": [
            {"original": c, "transposed": transpose_chord_symbol(c, semitones, prefer_flats=flats)}
            for c in chords
        ],
        "loop_grid": quantize_loop_to_bar_beat_sixteenth(loop_start, loop_end, bpm),
    }
    console.print_json(data=out)


@app.command("studio")
def cmd_studio(
    action: str = typer.Argument(
        ...,
        help="Action: status | start-stems | get-stems | voice-clean | start-drums | get-drums | start-chords | get-chords | start-lyrics | get-lyrics | handoff",
    ),
    original_id: str = typer.Argument(..., help="Studio original track ID"),
    profile: str = typer.Option("standard_4stem", "--profile", "-p", help=f"Stem profile ({', '.join(STEM_PROFILES)})"),
    source: str = typer.Option("mix", "--source", help="Handoff source: loop | stems | mix"),
) -> None:
    """Control `studio.soundboost.ai` Stem Separation, 5-Piece Drum Kit Splitter, Voice Cleaner, Chords & Lyrics."""
    client = SoundBoostClient()
    act = action.strip().lower().replace("_", "-")
    if act == "status":
        res = client.get_studio_original_status(original_id)
    elif act == "start-stems":
        res = client.start_studio_stem_split(original_id, profile_key=profile)
    elif act == "get-stems":
        res = client.get_studio_stems(original_id)
    elif act == "voice-clean":
        res = client.start_studio_voice_clean(original_id)
    elif act == "start-drums":
        res = client.start_studio_drum_split(original_id)
    elif act == "get-drums":
        res = client.get_studio_drum_stems(original_id)
    elif act == "start-chords":
        res = client.start_studio_chord_analysis(original_id)
    elif act == "get-chords":
        res = client.get_studio_chord_analysis(original_id)
    elif act == "start-lyrics":
        res = client.start_studio_lyrics(original_id)
    elif act == "get-lyrics":
        res = client.get_studio_lyrics(original_id)
    elif act == "handoff":
        res = client.handoff_studio_to_mastering(original_id, source_type=source)
    else:
        raise typer.BadParameter(f"Unknown studio action '{action}'")
    console.print_json(data=res)


@app.command("converta")
def cmd_converta(
    action: str = typer.Argument("formats", help="formats | convert | status"),
    source: str | None = typer.Option(None, "--source", "-s", help="Upload ID or source audio URL"),
    job_id: str | None = typer.Option(None, "--job", "-j", help="Converta job ID for status check"),
    fmt: str = typer.Option("wav", "--format", "-f", help="Output format: wav | aiff | flac | ogg | aac | m4a | mp3"),
    bits: int = typer.Option(24, "--bits", "-b", help="Bit depth: 16 | 24 | 32"),
    rate: int = typer.Option(48000, "--rate", "-r", help="Sample rate: 44100 | 48000"),
    dither: bool = typer.Option(True, "--dither/--no-dither", help="Enable dithering on lossless formats"),
) -> None:
    """Query `converta.soundboost.ai` supported formats or run hi-res audio format conversions."""
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "formats":
        console.print_json(data=client.get_converta_formats())
    elif act == "convert":
        if not source:
            raise typer.BadParameter("--source is required for convert")
        console.print_json(
            data=client.start_converta_conversion(
                upload_id_or_url=source,
                output_format=fmt,
                bit_depth=bits,
                sample_rate=rate,
                dithering=dither,
            )
        )
    elif act == "status":
        if not job_id:
            raise typer.BadParameter("--job is required for status")
        console.print_json(data=client.get_converta_status(job_id))
    else:
        raise typer.BadParameter(f"Unknown converta action '{action}'")


@app.command("mv-budget")
def cmd_mv_budget(
    credits: int = typer.Argument(..., help="Quoted storyboard credits"),
    tier: str = typer.Option("director", "--tier", "-t", help="Plan tier: starter | creator | director | studio"),
) -> None:
    """Compute AI Music Video (`h3-max`) retake spending limit (`ec(quote)` formula) and tier capabilities."""
    t_norm = tier.strip().lower()
    cap = compute_mv_spending_limit(credits)
    console.print_json(
        data={
            "engine": "h3-max",
            "plan_tier": t_norm,
            "quoted_credits": credits,
            "spending_limit_credits": cap,
            "retake_headroom_credits": cap - credits,
            "character_reference_cap": MV_CHARACTER_REF_CAPS.get(t_norm, 1),
            "auto_qc_enabled": t_norm in ("director", "studio"),
            "deep_research_enabled": t_norm == "studio",
        }
    )


@app.command("reaper-install")
def cmd_reaper_install(
    reaper_dir: str | None = typer.Option(None, "--reaper-dir", help="Custom REAPER resource path"),
    configure_bridge: bool = typer.Option(
        False,
        "--configure-bridge",
        help="Also rewrite REAPER.ini to enable the HTTP (8080) + OSC (9000) bridge and Python ReaScript (opt-in).",
    ),
    enable_daemon: bool = typer.Option(
        False,
        "--enable-daemon",
        help="Also install the __startup.lua hook so REAPER auto-launches the Live Bridge Daemon on boot (opt-in).",
    ),
) -> None:
    """Install the Suavecito SoundBoost 18-Band Mastering EQ JSFX plugin, Live Bridge Daemon, 12 .RfxChain presets & Stem Importer into REAPER.

    By default this only copies the assets. The live HTTP/OSC bridge and the auto-start
    daemon are opt-in via --configure-bridge and --enable-daemon, since both modify
    REAPER.ini / add a startup hook.
    """
    console.print_json(
        data=install_reaper_assets(
            reaper_dir,
            configure_ini_bridge=configure_bridge,
            enable_startup_daemon=enable_daemon,
        )
    )


@app.command("reaper")
def cmd_reaper(
    action: str = typer.Argument(
        "inspect",
        help="inspect | install | status | push-eq | osc | import-stems | generate-rpp | storyboard | render-video | insert-video | inject-isrc | action",
    ),
    reaper_dir: str | None = typer.Option(None, "--reaper-dir", help="Custom REAPER resource path"),
    preset: str = typer.Option("pop", "--preset", "-p", help="SoundBoost Mastering genre preset"),
    prompt: str | None = typer.Option(None, "--prompt", help="Natural-language 18-band EQ or visual style prompt"),
    width: float = typer.Option(100.0, "--width", help="Mid/Side Stereo Width % (0..200)"),
    drive: float = typer.Option(0.0, "--drive", help="Limiter Makeup Drive dB"),
    ceiling: float = typer.Option(-1.0, "--ceiling", help="Brickwall Ceiling dBTP"),
    osc_address: str | None = typer.Option(None, "--osc-addr", help="OSC address (e.g. /tempo/raw)"),
    osc_val: float | None = typer.Option(None, "--osc-val", help="OSC numeric argument"),
    stems_folder: str | None = typer.Option(None, "--stems-dir", help="Folder of separated stems"),
    rpp_out: str | None = typer.Option(None, "--rpp", "-o", help="Output .RPP project file path"),
    mp4_out: str | None = typer.Option(None, "--mp4", help="Output .MP4 video file path for render-video"),
    video_mode: str = typer.Option("spotify_canvas_9x16", "--video-mode", help="spotify_canvas_9x16 | social_vertical_9x16 | music_video_16x9"),
    audit_file: str | None = typer.Option(None, "--audit-file", help="Audio or video file path for audit, storyboard, render-video, or insert-video"),
    bpm: float = typer.Option(120.0, "--bpm", help="Project tempo in BPM"),
    bars_per_scene: int = typer.Option(4, "--bars", help="Bars per scene for Shoots storyboard"),
    isrc: str | None = typer.Option(None, "--isrc", help="ISRC code for <RENDER_METADATA> and DDP INDEX1"),
    title: str | None = typer.Option(None, "--title", help="Track title"),
    artist: str | None = typer.Option(None, "--artist", help="Artist / Performer name"),
    album: str | None = typer.Option(None, "--album", help="Album title"),
    action_id: str | None = typer.Option(None, "--action-id", help="REAPER Action ID or command name"),
    configure_bridge: bool = typer.Option(
        False, "--configure-bridge", help="On install: also rewrite REAPER.ini for the HTTP/OSC bridge (opt-in)."
    ),
    enable_daemon: bool = typer.Option(
        False, "--enable-daemon", help="On install: also install the __startup.lua auto-start daemon hook (opt-in)."
    ),
    enable_osc: bool = typer.Option(False, "--enable-osc", help="Also enable REAPER's OSC surface (listens on the LAN)"),
    enable_web: bool = typer.Option(False, "--enable-web", help="Also enable REAPER's web surface (listens on the LAN, no password by default)"),
) -> None:
    """Deep REAPER 7.82 integration & video pipeline controller (JSFX `gmem`, HTTP/OSC/IPC Live Bridge, `Shoots` Storyboard, `.MP4` Renderer & `.RPP` Generator)."""
    act = action.strip().lower().replace("_", "-")
    if act == "inspect":
        console.print_json(data=inspect_reaper_environment(reaper_dir))
        return
    if act == "install":
        console.print_json(
            data=install_reaper_assets(
                reaper_dir,
                configure_ini_bridge=configure_bridge,
                enable_startup_daemon=enable_daemon,
                enable_osc_surface=enable_osc,
                enable_http_web_surface=enable_web,
            )
        )
        return
    if act == "configure-bridge":
        console.print_json(
            data=configure_reaper_ini_bridge(
                reaper_dir, enable_osc_surface=enable_osc, enable_http_web_surface=enable_web
            )
        )
        return

    bridge = ReaperLiveBridge(reaper_resource_dir=reaper_dir)
    if act == "status":
        console.print_json(
            data={
                "http_control_surface": bridge.query_http_control(),
                "ipc_daemon": bridge.dispatch_ipc_command("telemetry", wait_for_ack_sec=0.35),
            }
        )
    elif act == "push-eq":
        console.print_json(
            data=bridge.push_18band_eq_to_reaper(
                stereo_width_pct=width,
                makeup_drive_db=drive,
                ceiling_dbtp=ceiling,
                preset=preset,
                prompt=prompt,
            )
        )
    elif act == "osc":
        if not osc_address:
            raise typer.BadParameter("--osc-addr is required for osc")
        args = [osc_val] if osc_val is not None else []
        console.print_json(data=bridge.send_osc(osc_address, *args))
    elif act == "import-stems":
        if not stems_folder:
            raise typer.BadParameter("--stems-dir is required for import-stems")
        console.print_json(
            data=bridge.dispatch_ipc_command(
                "import_stems",
                {"stems_folder": stems_folder, "add_drum_triggers": 1},
            )
        )
    elif act == "inject-isrc":
        tags: list[str] = []
        if isrc:
            tags.extend([f"ID3:TSRC|{isrc}", f"VORBIS:ISRC|{isrc}", f"IXML:ISRC|{isrc}"])
        if title:
            tags.append(f"ID3:TIT2|{title}")
        if artist:
            tags.append(f"ID3:TPE1|{artist}")
        if album:
            tags.append(f"ID3:TALB|{album}")
        console.print_json(
            data=bridge.dispatch_ipc_command("set_render_metadata", {"render_metadata": ";".join(tags)})
        )
    elif act == "generate-rpp":
        if not rpp_out:
            raise typer.BadParameter("--rpp is required for generate-rpp")
        console.print_json(
            data=generate_soundboost_rpp_project(
                output_rpp_path=rpp_out,
                stems_folder=stems_folder,
                bpm=bpm,
                preset=preset,
                prompt=prompt,
                stereo_width_pct=width,
                makeup_drive_db=drive,
                ceiling_dbtp=ceiling,
                isrc=isrc,
                title=title,
                artist=artist,
                album=album,
                audit_audio_path=audit_file,
            )
        )
    elif act == "action":
        if not action_id:
            raise typer.BadParameter("--action-id is required for action")
        console.print_json(data=bridge.dispatch_ipc_command("run_action", {"action_id": action_id}))
    elif act == "storyboard":
        if not audit_file:
            raise typer.BadParameter("--audit-file (audio path) is required for storyboard")
        console.print_json(
            data=build_reaper_to_shoots_storyboard(
                audio_path=audit_file,
                bpm=bpm,
                bars_per_scene=bars_per_scene,
                visual_style_prompt=prompt or "Cinematic neon nocturnal cyberpunk performance, anamorphic 35mm lens",
            )
        )
    elif act == "render-video":
        if not audit_file or not mp4_out:
            raise typer.BadParameter("--audit-file and --mp4 are required for render-video")
        console.print_json(
            data=render_reaper_video_pipeline(
                audio_path=audit_file,
                output_mp4_path=mp4_out,
                mode=video_mode,
                preset=preset,
                prompt=prompt,
                title=title,
                artist=artist,
                isrc=isrc,
                output_rpp_path=rpp_out,
                bpm=bpm,
            )
        )
    elif act == "insert-video":
        if not audit_file:
            raise typer.BadParameter("--audit-file (video/image path) is required for insert-video")
        console.print_json(data=bridge.insert_video_scene_to_reaper(audit_file))
    else:
        raise typer.BadParameter(f"Unknown reaper action '{action}'")


@app.command("specs")
def cmd_specs() -> None:
    """Print the full SoundBoost.ai platform capability matrix (18-band EQ, stems, drums, Converta, LUFS targets)."""
    console.print_json(data=list_soundboost_platform_capabilities())


@app.command("auth")
def cmd_auth(
    action: str = typer.Argument(
        "status",
        help="status | pkce-url | browser-login | google-exchange | save-token | logout",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        "-t",
        help="Google OAuth token or SoundBoost Bearer JWT",
    ),
    email: str | None = typer.Option(None, "--email", help="Optional account email metadata"),
    port: int = typer.Option(8765, "--port", help="Local loopback callback port"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not auto-open browser"),
) -> None:
    """Manage SoundBoost Google OAuth 2.0, OS Keyring (`WinVaultKeyring`), and session credentials."""
    act = action.strip().lower().replace("_", "-")
    if act == "status":
        console.print_json(data=get_vault_status())
    elif act == "pkce-url":
        pkce = generate_google_pkce_challenge(
            redirect_uri=f"http://127.0.0.1:{port}/auth/callback"
        )
        console.print_json(
            data={
                "authorize_url": pkce.authorize_url,
                "redirect_uri": pkce.redirect_uri,
                "state": pkce.state,
                "code_challenge": pkce.code_challenge,
                "code_challenge_method": "S256",
            }
        )
    elif act == "browser-login":
        console.print_json(
            data=run_loopback_oauth_handoff(
                port=port,
                open_browser=not no_browser,
                exchange_google_id_token=lambda id_tok: SoundBoostClient().google_oauth_login(id_tok, True),
            )
        )
    elif act == "google-exchange":
        if not token:
            raise typer.BadParameter("--token is required for google-exchange")
        client = SoundBoostClient()
        console.print_json(data=redact_login_result(client.google_oauth_login(token, save_credentials=True)))
    elif act == "save-token":
        if not token:
            raise typer.BadParameter("--token is required for save-token")
        raw = token.strip()
        console.print_json(
            data=save_credentials_to_vault(raw, email=email, auth_method="cli_save_token")
        )
    elif act == "logout":
        console.print_json(data=clear_vault_credentials())
    else:
        raise typer.BadParameter(f"Unknown auth action '{action}'")


@app.command("serve-mcp")
def cmd_serve_mcp(
    transport: str = typer.Option("stdio", "--transport", help="stdio | http | sse"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host for HTTP transport"),
    port: int = typer.Option(8000, "--port", help="Bind port for HTTP transport"),
    oauth: str = typer.Option(
        "none",
        "--oauth",
        help="Inbound MCP OAuth 2.1 provider for HTTP: none | google | github | jwt | oauth_proxy | remote",
    ),
    discovery: str = typer.Option(
        "progressive",
        "--discovery",
        help="Progressive tool discovery mode: progressive (5 entry tools + BM25 search/pack unlock) | all (all 26 tools visible)",
    ),
    packs: str | None = typer.Option(
        None,
        "--packs",
        help="Optional comma-separated initial tool packs to activate in progressive mode (e.g. mastering,studio_stems)",
    ),
) -> None:
    """Launch the FastMCP server over `stdio` or OAuth 2.1-protected `Streamable HTTP` with progressive tool discovery."""
    from soundboost_toolkit.constants import MCP_TOOL_PACKS
    from soundboost_toolkit.mcp_server import PROGRESSIVE_DISCOVERY, serve

    disc_mode = discovery.strip().lower()
    if disc_mode == "all":
        PROGRESSIVE_DISCOVERY.mode = "all"
        PROGRESSIVE_DISCOVERY.active_packs = set(MCP_TOOL_PACKS.keys())
    else:
        PROGRESSIVE_DISCOVERY.mode = "progressive"
        PROGRESSIVE_DISCOVERY.active_packs = {"core"}
        if packs:
            for p in packs.split(","):
                pk = p.strip().lower()
                if pk in MCP_TOOL_PACKS:
                    PROGRESSIVE_DISCOVERY.active_packs.add(pk)

    try:
        serve(transport=transport, host=host, port=port, oauth=oauth)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("bulk-master")
def cmd_bulk_master(
    action: str = typer.Argument("list", help="start | list | status | export-zip"),
    tracks_csv: str | None = typer.Option(None, "--tracks", "-t", help="Comma-separated uploaded track IDs"),
    batch_id: str | None = typer.Option(None, "--batch-id", "-b", help="Bulk mastering batch ID"),
    mode: str = typer.Option("auto", "--mode", "-m", help="Mastering mode"),
    preset: str = typer.Option("pop", "--preset", "-p", help="Genre preset"),
    prompt: str | None = typer.Option(None, "--prompt", help="Mastering prompt"),
    target_lufs: float = typer.Option(-14.0, "--lufs", "-l", help="Unified Album Target Integrated LUFS"),
) -> None:
    """Manage pay-gated SoundBoost Cloud Bulk / Album Mastering (`/api/bulk-mastering/*`)."""
    client = SoundBoostClient()
    act = action.strip().lower().replace("_", "-")
    if act == "list":
        res = client.list_bulk_mastering_batches()
    elif act == "start":
        if not tracks_csv:
            raise typer.BadParameter("--tracks is required for start")
        tids: list[int | str] = [x.strip() for x in tracks_csv.split(",") if x.strip()]
        res = client.start_bulk_mastering(
            tids,
            mode=mode,
            preset=preset,
            prompt=prompt,
            target_lufs=target_lufs,
        )
    elif act == "status":
        if not batch_id:
            raise typer.BadParameter("--batch-id is required for status")
        res = client.get_bulk_mastering_status(batch_id)
    elif act == "export-zip":
        if not batch_id:
            raise typer.BadParameter("--batch-id is required for export-zip")
        res = client.get_bulk_mastering_export_zip(batch_id)
    else:
        raise typer.BadParameter(f"Unknown bulk-master action '{action}'")
    console.print_json(data=res)


@app.command("ingest")
def cmd_ingest(
    source_type: str = typer.Argument(
        ...,
        help="mastering-youtube | mastering-soundcloud | studio-youtube | studio-soundcloud | studio-upload | studio-ready",
    ),
    url_or_path: str | None = typer.Option(None, "--url", "-u", help="YouTube/SoundCloud URL or local file path"),
) -> None:
    """Ingest tracks directly from YouTube, SoundCloud, or local disk into Mastering or Studio Stem Splitter."""
    client = SoundBoostClient()
    act = source_type.strip().lower().replace("_", "-")
    if act == "studio-ready":
        console.print_json(data=client.list_ready_studio_originals())
        return
    if not url_or_path:
        raise typer.BadParameter("--url is required for this ingest action")
    if act == "mastering-youtube":
        res = client.import_youtube_for_mastering(url_or_path)
    elif act == "mastering-soundcloud":
        res = client.import_soundcloud_for_mastering(url_or_path)
    elif act == "studio-youtube":
        res = client.import_youtube_to_studio(url_or_path)
    elif act == "studio-soundcloud":
        res = client.import_soundcloud_to_studio(url_or_path)
    elif act == "studio-upload":
        res = client.upload_studio_original(url_or_path)
    else:
        raise typer.BadParameter(f"Unknown ingest source_type '{source_type}'")
    console.print_json(data=res)


@app.command("prompt-eq")
def cmd_prompt_eq(
    prompt: str = typer.Argument(..., help="Natural-language mastering description"),
    track_id: str | None = typer.Option(None, "--track-id", "-id", help="Optional SoundBoost track ID"),
) -> None:
    """Translate a natural-language mastering prompt via pay-gated SoundBoost Cloud `POST /api/ai/prompt` (`Toki`)."""
    client = SoundBoostClient()
    console.print_json(data=client.translate_ai_mastering_prompt(prompt, track_id=track_id))


@app.command("voicings")
def cmd_voicings(
    chords_csv: str = typer.Argument("Cm,Abmaj7,Eb,Bb", help="Comma-separated chord symbols"),
    instrument: str = typer.Option("guitar", "--instrument", "-i", help="guitar | ukulele | piano"),
) -> None:
    """Generate Studio Practice Mode Chord Fingering Voicings & ASCII Diagrams for Guitar, Ukulele, or Piano (Module 534)."""
    chords = [c.strip() for c in chords_csv.split(",") if c.strip()]
    diagrams = [get_chord_voicing_diagram(c, instrument=instrument) for c in chords]
    for d in diagrams:
        console.print(Panel(str(d["ascii_diagram"]), title=f"{d['chord']} ({d['instrument'].title()})", border_style="cyan"))
    console.print_json(data={"instrument": instrument, "voicings": diagrams})


@app.command("shifter")
def cmd_shifter(
    original_id: str = typer.Argument(..., help="Studio original track ID"),
    semitones: float = typer.Option(0.0, "--semitones", "-s", help="Semitone shift (-12 to +12)"),
    speed: float = typer.Option(1.0, "--speed", "-x", help="Playback speed / time-stretch factor (0.5 to 2.0)"),
    fmt: str = typer.Option("wav", "--format", "-f", help="Export format: wav | flac | mp3"),
    bits: int = typer.Option(24, "--bits", "-b", help="Output bit depth (16, 24, 32)"),
) -> None:
    """Export pitch-shifted and time-stretched stems via pay-gated SoundBoost Studio Cloud (`POST /api/studio/originals/{id}/export/`)."""
    client = SoundBoostClient()
    res = client.export_studio_stems(
        original_id,
        {
            "semitones": semitones,
            "pitch_shift_info": classify_pitch_shift_zone(round(semitones)),
            "speed_factor": speed,
            "format": fmt,
            "bit_depth": bits,
        },
    )
    console.print_json(data=res)


@app.command("mp3-inspect")
def cmd_mp3_inspect(
    file_path: str = typer.Argument(..., help="Path to MP3 file"),
) -> None:
    """Inspect binary MP3 frame headers, LAME/Xing/Info gapless delay (`576`), and priming samples (`1105`, Modules 590-591)."""
    console.print_json(data=inspect_mp3_gapless_headers(file_path))


@app.command("shoots")
def cmd_shoots(
    action: str = typer.Argument(
        "sso-url",
        help="sso-url | list-mv | list-shoots | get-shoot | director-msg | regen-all | list-clips",
    ),
    target_id: str | None = typer.Option(None, "--id", help="Shoot ID or Music Video ID"),
    message: str | None = typer.Option(None, "--message", "-m", help="Director Chat instruction"),
) -> None:
    """Manage AI Music Video (`h3-max`), `Shoots` Director Chat, and `shoots.soundboost.ai` SSO."""
    client = SoundBoostClient()
    act = action.strip().lower().replace("_", "-")
    if act == "sso-url":
        console.print_json(
            data={
                "shoots_google_sso_url": client.build_shoots_google_sso_url(),
                "shoots_portal": "https://shoots.soundboost.ai",
            }
        )
    elif act == "list-mv":
        console.print_json(data=client.list_music_videos())
    elif act == "list-shoots":
        console.print_json(data=client.list_shoots())
    elif act == "get-shoot":
        if not target_id:
            raise typer.BadParameter("--id is required for get-shoot")
        console.print_json(data=client.get_shoot(target_id))
    elif act == "director-msg":
        if not target_id or not message:
            raise typer.BadParameter("--id and --message are required for director-msg")
        console.print_json(data=client.send_shoot_director_message(target_id, message))
    elif act == "regen-all":
        if not target_id:
            raise typer.BadParameter("--id is required for regen-all")
        console.print_json(data=client.regenerate_all_shoot_scenes(target_id))
    elif act == "list-clips":
        console.print_json(data=client.list_video_clips())
    else:
        raise typer.BadParameter(f"Unknown shoots action '{action}'")


@app.command("account")
def cmd_account(
    action: str = typer.Argument(
        "entitlements",
        help="entitlements | tiers | tool-packs | profile | billing | products | notifications | unread | referral | check-username",
    ),
    username: str | None = typer.Option(None, "--username", "-u", help="Username to inspect or check"),
) -> None:
    """Inspect SoundBoost Account-Level Entitlements, Progressive MCP Tool Packs, Artist Profile, Notifications, Referrals & Subscription Billing."""
    client = SoundBoostClient()
    act = action.strip().lower().replace("_", "-")
    if act == "entitlements":
        res = client.get_my_entitlements()
    elif act in {"tiers", "all-tiers"}:
        from soundboost_toolkit.constants import ACCOUNT_TIER_ENTITLEMENTS

        res = {"account_tier_entitlements": ACCOUNT_TIER_ENTITLEMENTS}
    elif act in {"tool-packs", "packs"}:
        from soundboost_toolkit.constants import MCP_TOOL_PACKS, TIER_UNLOCKED_MCP_PACKS

        res = {"tool_packs": MCP_TOOL_PACKS, "tier_unlocked_packs": TIER_UNLOCKED_MCP_PACKS}
    elif act == "profile":
        res = client.get_public_artist_profile(username) if username else client.get_account_profile()
    elif act == "billing":
        res = client.get_billing_info()
    elif act == "products":
        res = {"products": client.get_products()}
    elif act == "notifications":
        res = client.list_notifications()
    elif act == "unread":
        res = client.get_unread_notifications_count()
    elif act == "referral":
        res = client.get_referral_info()
    elif act == "check-username":
        if not username:
            raise typer.BadParameter("--username is required for check-username")
        res = client.check_username_availability(username)
    else:
        raise typer.BadParameter(f"Unknown account action '{action}'")
    console.print_json(data=res)



if __name__ == "__main__":
    app()
