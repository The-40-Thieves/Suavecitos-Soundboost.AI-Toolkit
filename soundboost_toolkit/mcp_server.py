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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
from fastmcp import Context, FastMCP
from fastmcp.server.transforms import GetToolNext
from fastmcp.server.transforms.catalog import CatalogTransform
from fastmcp.server.transforms.search.base import (
    _extract_searchable_text,
    serialize_tools_for_output_json,
    serialize_tools_for_output_markdown,
)
from fastmcp.server.transforms.search.bm25 import _BM25Index
from fastmcp.tools.base import Tool
from fastmcp.utilities.tasks import TaskConfig
from fastmcp.utilities.versions import VersionSpec
from fastmcp_tasks import TasksExtension
from mcp.types import ToolAnnotations

from soundboost_toolkit.client import SoundBoostClient, resolve_account_entitlements
from soundboost_toolkit.constants import (
    ACCOUNT_TIER_ENTITLEMENTS,
    CONVERTA_FORMATS,
    DRUM_KIT_SUBSTEMS,
    MASTERING_EQ_FREQUENCIES_HZ,
    MASTERING_GENRE_PRESETS,
    MASTERING_MODES,
    MCP_TOOL_PACKS,
    MV_CHARACTER_REF_CAPS,
    PRESET_EQ_CURVES_18BAND,
    STEM_PROFILES,
    STREAMING_TARGETS,
    TIER_UNLOCKED_MCP_PACKS,
    compute_mv_spending_limit,
)
from soundboost_toolkit.dsp import (
    analyze_audio_file,
    build_lead_sheet_rows,
    classify_pitch_shift_zone,
    get_chord_voicing_diagram,
    inspect_mp3_gapless_headers,
    quantize_loop_to_bar_beat_sixteenth,
    render_lead_sheet_html,
    snap_timestamp_to_synchronized_beat,
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

mcp = FastMCP(
    "suavecitos-soundboost-toolkit",
    auth=build_mcp_oauth_provider(),
    instructions=(
        "Suavecito's SoundBoost.ai Toolkit: an unofficial, community-built MCP server for "
        "SoundBoost.ai, made with their permission (not the official SoundBoost MCP). "
        "Use it to check whether a mix is release-ready (LUFS, true peak, streaming loudness "
        "penalties), master tracks in the SoundBoost cloud, split stems and drums, read chords "
        "and lyrics, convert formats, write release tags with ISRCs, build smart links, and "
        "control REAPER. Only 4 tools are visible at first: call soundboost_discover_tools to "
        "search for or unlock the rest."
    ),
)

_tasks_backend_url = os.environ.get("SOUNDBOOST_TASKS_URL")
mcp.add_extension(
    TasksExtension(url=_tasks_backend_url) if _tasks_backend_url else TasksExtension()
)


class SoundBoostProgressiveDiscoveryTransform(CatalogTransform):
    """Progressive Tool Discovery & Context-Window Budget Manager for SoundBoost MCP.

    Prevents agent context-window bloat by exposing only the 4 Pinned Entry Tools (`core` pack)
    in `list_tools()` by default (~82% token reduction), while keeping all 22 tools:
    1. Searchable on demand via BM25 Okapi relevance ranking (`action='search'`) with compact Markdown schemas.
    2. Dynamically activatable/deactivatable by domain pack (`action='activate_pack'` / `'deactivate_pack'`)
       with `notifications/tools/list_changed`.
    3. Automatically filterable to the user's exact SoundBoost account level (`action='sync_account_tier'`).
    4. Directly callable by name at any time (`get_tool()` delegates to `call_next`).
    """

    def __init__(self, initial_mode: str | None = None) -> None:
        super().__init__()
        env_mode = (initial_mode or os.environ.get("SOUNDBOOST_MCP_DISCOVERY_MODE", "progressive")).strip().lower()
        self.mode: str = env_mode if env_mode in {"progressive", "all"} else "progressive"
        self.active_packs: set[str] = (
            set(MCP_TOOL_PACKS.keys()) if self.mode == "all" else {"core"}
        )
        self.active_tool_names: set[str] = set()
        self._bm25_index = _BM25Index()
        self._indexed_names: list[str] = []

    def get_visible_tool_names(self) -> set[str]:
        if self.mode == "all":
            names: set[str] = set()
            for spec in MCP_TOOL_PACKS.values():
                names.update(str(t) for t in spec.get("tools", ()))  # type: ignore[arg-type]
            return names | self.active_tool_names

        visible: set[str] = set(self.active_tool_names)
        for pack_id in self.active_packs | {"core"}:
            spec = MCP_TOOL_PACKS.get(pack_id, {})
            visible.update(str(t) for t in spec.get("tools", ()))  # type: ignore[arg-type]
        return visible

    async def transform_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        if self.mode == "all":
            return list(tools)
        visible_names = self.get_visible_tool_names()
        return [t for t in tools if t.name in visible_names]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        # Always allow direct tool invocation by name even if the tool schema is currently collapsed in list_tools()
        return await call_next(name, version=version)

    async def search_catalog(
        self,
        ctx: Context | None,
        query: str,
        *,
        max_results: int = 5,
        output_format: str = "markdown",
        unlock_matches: bool = True,
    ) -> dict[str, Any]:
        catalog = list(await self.get_tool_catalog(ctx)) if ctx is not None else []
        if not catalog:
            # Fallback if called outside an active MCP context
            catalog = list(await mcp._local_provider.list_tools())

        documents = [_extract_searchable_text(t) for t in catalog]
        self._bm25_index = _BM25Index()
        self._bm25_index.build(documents)
        self._indexed_names = [t.name for t in catalog]

        indices = self._bm25_index.query(query, max(1, max_results))
        tools_by_name = {t.name: t for t in catalog}
        matched_tools = [tools_by_name[self._indexed_names[i]] for i in indices]
        matched_names = [t.name for t in matched_tools]

        if unlock_matches and matched_names:
            self.active_tool_names.update(matched_names)
            await _notify_tool_list_changed(ctx)

        if output_format.strip().lower() == "json":
            rendered: Any = serialize_tools_for_output_json(matched_tools)
        else:
            rendered = serialize_tools_for_output_markdown(matched_tools)

        return {
            "query": query,
            "matched_count": len(matched_tools),
            "matched_tools": matched_names,
            "unlocked_in_list_tools": unlock_matches,
            "schemas": rendered,
        }


PROGRESSIVE_DISCOVERY = SoundBoostProgressiveDiscoveryTransform()
mcp.add_transform(PROGRESSIVE_DISCOVERY)


async def _notify_tool_list_changed(ctx: Context | None) -> None:
    """Safely emit `notifications/tools/list_changed` when progressive tool visibility changes."""
    if ctx is None:
        return
    try:
        await ctx.send_notification(mcp_types.ToolListChangedNotification())
    except RuntimeError:
        return



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
# 0. Progressive Tool Discovery & Context-Window Budget Manager
# ---------------------------------------------------------------------------
@mcp.tool(
    tags={"discovery", "core", "catalog", "search"},
    annotations=ToolAnnotations(
        title="SoundBoost Progressive Tool Discovery, BM25 Search & Pack Activator",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def soundboost_discover_tools(
    action: str = "catalog",
    query: str | None = None,
    pack: str | None = None,
    tool_names: list[str] | None = None,
    account_tier: str | None = None,
    output_format: str = "markdown",
    max_results: int = 5,
    unlock_matches: bool = True,
    call_arguments: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> Any:
    """Progressive tool discovery & context-window budget manager for SoundBoost MCP.
    Keeps each agent's context window compact (~82% smaller initial `list_tools()`) by loading
    domain tool packs on demand or syncing visible tools to the user's exact SoundBoost account level:
    - `catalog`: List all 8 tool packs (`core`, `free_tools`, `studio_harmony`, `studio_stems`, `distribution`, `mastering`, `bulk_mastering`, `music_video_shoots`), their active state, tool names, and required tier.
    - `search`: Search all 22 tools using BM25 Okapi natural-language ranking (`query`), returning compact Markdown (`~70%` fewer tokens) or JSON schemas and optionally auto-unlocking matched tools in `list_tools()`.
    - `activate_pack`: Enable one or more tool packs (`pack='mastering'`, `'studio_stems,studio_harmony'`, or `'all'`) or individual `tool_names`, and notify the MCP client via `notifications/tools/list_changed`.
    - `deactivate_pack`: Unload one or more tool packs (`pack='mastering'`) or `tool_names` when a task phase finishes to shrink the context window.
    - `sync_account_tier`: Resolve the user's SoundBoost account tier (`free`, `pay_as_you_go`, `starter`, `unlimited`, `creator`, `director`, `unlimited_plus`, `studio`) and enable ONLY the tool packs unlocked for that tier.
    - `reset`: Collapse `list_tools()` back down to the 4 Pinned Entry Tools (`core` pack).
    - `call`: Directly invoke any tool (`query` or first `tool_names` entry) with `call_arguments`.
    """
    act = action.strip().lower()

    if act == "catalog":
        visible = PROGRESSIVE_DISCOVERY.get_visible_tool_names()
        packs_summary = {}
        for pack_id, spec in MCP_TOOL_PACKS.items():
            pack_tools = list(spec.get("tools", ()))  # type: ignore[arg-type]
            packs_summary[pack_id] = {
                "title": spec.get("title"),
                "min_tier": spec.get("min_tier"),
                "always_pinned": spec.get("always_pinned", False),
                "active": pack_id in PROGRESSIVE_DISCOVERY.active_packs or PROGRESSIVE_DISCOVERY.mode == "all",
                "summary": spec.get("summary"),
                "tools": pack_tools,
            }
        return {
            "discovery_mode": PROGRESSIVE_DISCOVERY.mode,
            "active_packs": sorted(PROGRESSIVE_DISCOVERY.active_packs),
            "visible_tool_count": len(visible),
            "total_catalog_tools": sum(len(v.get("tools", ())) for v in MCP_TOOL_PACKS.values()),  # type: ignore[arg-type]
            "visible_tools": sorted(visible),
            "packs": packs_summary,
        }

    if act == "search":
        if not query or not query.strip():
            raise ValueError("query is required for action='search'")
        return await PROGRESSIVE_DISCOVERY.search_catalog(
            ctx,
            query.strip(),
            max_results=max_results,
            output_format=output_format,
            unlock_matches=unlock_matches,
        )

    if act in {"activate_pack", "activate", "enable"}:
        if pack:
            requested = [p.strip().lower() for p in pack.split(",") if p.strip()]
            if "all" in requested:
                PROGRESSIVE_DISCOVERY.mode = "all"
                PROGRESSIVE_DISCOVERY.active_packs = set(MCP_TOOL_PACKS.keys())
            else:
                invalid = [p for p in requested if p not in MCP_TOOL_PACKS]
                if invalid:
                    raise ValueError(f"Unknown pack(s) {invalid}. Valid packs: {sorted(MCP_TOOL_PACKS)}")
                PROGRESSIVE_DISCOVERY.mode = "progressive"
                PROGRESSIVE_DISCOVERY.active_packs.update(requested)
        if tool_names:
            PROGRESSIVE_DISCOVERY.active_tool_names.update(t.strip() for t in tool_names if t.strip())
        await _notify_tool_list_changed(ctx)
        visible = PROGRESSIVE_DISCOVERY.get_visible_tool_names()
        return {
            "discovery_mode": PROGRESSIVE_DISCOVERY.mode,
            "active_packs": sorted(PROGRESSIVE_DISCOVERY.active_packs),
            "visible_tool_count": len(visible),
            "visible_tools": sorted(visible),
        }

    if act in {"deactivate_pack", "deactivate", "disable"}:
        PROGRESSIVE_DISCOVERY.mode = "progressive"
        if pack:
            requested = [p.strip().lower() for p in pack.split(",") if p.strip() and p.strip().lower() != "core"]
            for p in requested:
                PROGRESSIVE_DISCOVERY.active_packs.discard(p)
        if tool_names:
            for t in tool_names:
                PROGRESSIVE_DISCOVERY.active_tool_names.discard(t.strip())
        await _notify_tool_list_changed(ctx)
        visible = PROGRESSIVE_DISCOVERY.get_visible_tool_names()
        return {
            "discovery_mode": PROGRESSIVE_DISCOVERY.mode,
            "active_packs": sorted(PROGRESSIVE_DISCOVERY.active_packs),
            "visible_tool_count": len(visible),
            "visible_tools": sorted(visible),
        }

    if act == "reset":
        PROGRESSIVE_DISCOVERY.mode = "progressive"
        PROGRESSIVE_DISCOVERY.active_packs = {"core"}
        PROGRESSIVE_DISCOVERY.active_tool_names.clear()
        await _notify_tool_list_changed(ctx)
        visible = PROGRESSIVE_DISCOVERY.get_visible_tool_names()
        return {
            "discovery_mode": PROGRESSIVE_DISCOVERY.mode,
            "active_packs": sorted(PROGRESSIVE_DISCOVERY.active_packs),
            "visible_tool_count": len(visible),
            "visible_tools": sorted(visible),
        }

    if act == "sync_account_tier":
        if account_tier:
            ent = resolve_account_entitlements(override_tier=account_tier, is_authenticated=account_tier != "free")
        else:
            client = SoundBoostClient()
            ent = client.get_my_entitlements()
        resolved_tier = str(ent.get("resolved_tier") or ent.get("tier") or "free")
        unlocked_packs = set(TIER_UNLOCKED_MCP_PACKS.get(resolved_tier, TIER_UNLOCKED_MCP_PACKS["free"]))
        PROGRESSIVE_DISCOVERY.mode = "progressive"
        PROGRESSIVE_DISCOVERY.active_packs = unlocked_packs
        PROGRESSIVE_DISCOVERY.active_tool_names.clear()
        await _notify_tool_list_changed(ctx)
        visible = PROGRESSIVE_DISCOVERY.get_visible_tool_names()
        return {
            "resolved_tier": resolved_tier,
            "active_packs": sorted(PROGRESSIVE_DISCOVERY.active_packs),
            "visible_tool_count": len(visible),
            "visible_tools": sorted(visible),
            "entitlements": ent.get("entitlements", ent),
        }

    if act == "call":
        target_tool = (query or (tool_names[0] if tool_names else "")).strip()
        if not target_tool or target_tool == "soundboost_discover_tools":
            raise ValueError("Specify a valid target tool name in `query` or `tool_names[0]` for action='call'")
        if ctx is not None:
            return await ctx.fastmcp.call_tool(target_tool, call_arguments or {})
        return await mcp.call_tool(target_tool, call_arguments or {})

    raise ValueError(f"Unsupported action '{action}'")


# ---------------------------------------------------------------------------
# 1. Local Offline DSP, Mastering & Tagging Tools
# ---------------------------------------------------------------------------
@mcp.tool(
    tags={"dsp", "loudness", "audit"},
    annotations=ToolAnnotations(
        title="Audit Audio Loudness (BS.1770-4) & DistroKid Readiness (Free Tool)",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def audit_audio_loudness_and_release_readiness(file_path: str) -> dict[str, Any]:
    """Run SoundBoost's Free LUFS Meter & Loudness Penalty Analyzer locally:
    ITU-R BS.1770-4 Loudness (Integrated/Short-Term/Momentary LUFS), SoundBoost LRA (P95-P10),
    4x oversampled True Peak (dBTP) with 100ms binary-search peak timestamp, stereo correlation,
    6-band spectral profile, 9-platform Streaming Loudness Penalty table, and DistroKid readiness check.
    """
    return analyze_audio_file(file_path).to_dict()


@mcp.tool(
    tags={"metadata", "id3", "isrc"},
    annotations=ToolAnnotations(
        title="Read Audio Release Metadata & ISRC Tags (Free Tool)",
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
    - `save_token`: Save a SoundBoost Bearer JWT into OS Keyring
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
    tags={"reaper", "daw", "jsfx", "osc", "rpp"},
    annotations=ToolAnnotations(
        title="REAPER 7.82 Deep Integration Suite (JSFX gmem, Live OSC/HTTP/IPC Bridge & .RPP Session Generator)",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def install_reaper_jsfx_and_scripts(
    reaper_resource_dir: str | None = None,
    action: str = "install",
    preset: str = "pop",
    prompt: str | None = None,
    eq_18band_db: list[float] | None = None,
    stereo_width_pct: float = 100.0,
    makeup_drive_db: float = 0.0,
    ceiling_dbtp: float = -1.0,
    osc_address: str | None = None,
    osc_value: float | str | None = None,
    output_rpp_path: str | None = None,
    stems_folder: str | None = None,
    stem_files: list[str] | None = None,
    audit_audio_path: str | None = None,
    bpm: float = 120.0,
    isrc: str | None = None,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    chords: list[dict[str, Any]] | None = None,
    lyrics: list[dict[str, Any]] | None = None,
    reaper_action_id: str | None = None,
    video_scenes: list[str | dict[str, Any]] | None = None,
    video_aspect: str = "9:16",
    output_mp4_path: str | None = None,
    video_mode: str = "spotify_canvas_9x16",
    bars_per_scene: int = 4,
    visual_style_prompt: str = "Cinematic neon nocturnal cyberpunk performance, anamorphic 35mm lens",
    plan_tier: str = "director",
    configure_ini_bridge: bool = False,
    enable_startup_daemon: bool = False,
) -> dict[str, Any]:
    """Deep REAPER 7.82 Integration & Video Pipeline Suite for Suavecito's SoundBoost.AI Toolkit:
    - `install` (default): Install `SoundBoost_18Band_Mastering_EQ_Monitor.jsfx` (`options:gmem=SuavecitoSoundBoost`),
      `Suavecito_SoundBoost_Stem_Import.lua`, `Suavecito_Live_Bridge_Daemon.lua`,
      `Suavecito_SoundBoost_9x16_Canvas_Visualizer.txt`, and 12 Genre `.RfxChain` files. The live bridge is
      opt-in: set `configure_ini_bridge=True` to rewrite `REAPER.ini` for the HTTP Control Surface (port 8080),
      UDP OSC (port 9000), and Python 3.13 ReaScript, and `enable_startup_daemon=True` to add the `__startup.lua`
      auto-start hook. Both default to False so a bare install only copies assets.
    - `inspect`: Forensically inspect running `reaper.exe` PID, `REAPER.ini`, installed VST3/CLAP/JSFX plugins, and live `gmem` telemetry.
    - `live_status`: Query live REAPER transport, beat position, track list, markers, and `gmem` telemetry via HTTP + IPC bridge.
    - `push_eq`: Push a live 18-band Mastering EQ curve (explicit `eq_18band_db`, `preset`, or natural-language `prompt`),
      `stereo_width_pct`, `makeup_drive_db`, and `ceiling_dbtp` directly to REAPER's Master EQ via `gmem=SuavecitoSoundBoost`.
    - `send_osc`: Send a binary UDP Open Sound Control packet (`osc_address`, `osc_value`) to REAPER (`127.0.0.1:9000`).
    - `import_stems`: Command live REAPER to import `stems_folder` into hierarchical folder buses (`DRUM KIT BUS`, `VOCAL BUS`, `INSTRUMENT BUS`) with 5-Piece Drum MIDI triggers.
    - `inject_isrc`: Inject `<RENDER_METADATA>` (`ID3:TSRC|<ISRC>;VORBIS:ISRC|<ISRC>`) and DDP markers into the active REAPER project.
    - `generate_rpp`: Generate a complete REAPER 7.82 `.RPP` multitrack session (`output_rpp_path`) with folder buses, track icons, 5-Piece Drum MIDI triggers, 100ms True-Peak clipping markers, Studio Chords/Lyrics regions, DDP/ISRC metadata, and a `<VIDEO_EFFECT "Video processor">` track with `video_scenes`.
    - `reaper_to_shoots_storyboard`: Analyze `audit_audio_path`, `bpm`, `chords`, and `lyrics` into bar-locked `Shoots` (`h3-max`) storyboard scenes with energy-driven camera motion, credit budget cap, and REAPER scene regions.
    - `render_video`: Render an audio-reactive `.mp4` (`output_mp4_path`, `video_mode` = `spotify_canvas_9x16` | `social_vertical_9x16` | `music_video_16x9`) with 18-Band Mastering EQ bars, Chords/Lyrics HUD, and optional companion `.RPP` (`output_rpp_path`).
    - `insert_video_scene`: Insert a video clip or image scene (`audit_audio_path` or first item in `video_scenes`) directly onto the live REAPER Video Track and open the REAPER Video Window.
    - `run_action`: Trigger any native REAPER Action ID or named command (`reaper_action_id`).
    """
    act = action.strip().lower().replace("-", "_")
    if act == "install":
        return install_reaper_assets(
            reaper_resource_dir,
            configure_ini_bridge=configure_ini_bridge,
            enable_startup_daemon=enable_startup_daemon,
        )
    if act == "inspect":
        return inspect_reaper_environment(reaper_resource_dir)
    if act == "configure_bridge":
        return configure_reaper_ini_bridge(reaper_resource_dir)

    bridge = ReaperLiveBridge(reaper_resource_dir=reaper_resource_dir)
    if act == "live_status":
        http_state = bridge.query_http_control()
        ipc_state = bridge.dispatch_ipc_command("telemetry", wait_for_ack_sec=0.35)
        return {"http_control_surface": http_state, "ipc_daemon": ipc_state}
    if act == "push_eq":
        return bridge.push_18band_eq_to_reaper(
            eq_18band_db=eq_18band_db,
            stereo_width_pct=stereo_width_pct,
            makeup_drive_db=makeup_drive_db,
            ceiling_dbtp=ceiling_dbtp,
            preset=preset,
            prompt=prompt,
        )
    if act == "send_osc":
        if not osc_address:
            raise ValueError("osc_address is required for action='send_osc'")
        args = [osc_value] if osc_value is not None else []
        return bridge.send_osc(osc_address, *args)
    if act == "import_stems":
        if not stems_folder:
            raise ValueError("stems_folder is required for action='import_stems'")
        return bridge.dispatch_ipc_command(
            "import_stems",
            {"stems_folder": str(Path(stems_folder).expanduser().resolve()), "add_drum_triggers": 1},
        )
    if act == "inject_isrc":
        tags: list[str] = []
        if isrc:
            tags.extend([f"ID3:TSRC|{isrc}", f"VORBIS:ISRC|{isrc}", f"IXML:ISRC|{isrc}"])
        if title:
            tags.append(f"ID3:TIT2|{title}")
        if artist:
            tags.append(f"ID3:TPE1|{artist}")
        if album:
            tags.append(f"ID3:TALB|{album}")
        meta_str = ";".join(tags)
        return bridge.dispatch_ipc_command("set_render_metadata", {"render_metadata": meta_str})
    if act == "run_action":
        if not reaper_action_id:
            raise ValueError("reaper_action_id is required for action='run_action'")
        return bridge.dispatch_ipc_command("run_action", {"action_id": str(reaper_action_id)})
    if act == "generate_rpp":
        if not output_rpp_path:
            raise ValueError("output_rpp_path is required for action='generate_rpp'")
        return generate_soundboost_rpp_project(
            output_rpp_path=output_rpp_path,
            stem_files=stem_files,
            stems_folder=stems_folder,
            bpm=bpm,
            preset=preset,
            prompt=prompt,
            eq_18band_db=eq_18band_db,
            stereo_width_pct=stereo_width_pct,
            makeup_drive_db=makeup_drive_db,
            ceiling_dbtp=ceiling_dbtp,
            isrc=isrc,
            title=title,
            artist=artist,
            album=album,
            chords=chords,
            lyrics=lyrics,
            audit_audio_path=audit_audio_path,
            video_scenes=video_scenes,
            video_aspect=video_aspect,
        )
    if act in ("reaper_to_shoots_storyboard", "storyboard"):
        if not audit_audio_path:
            raise ValueError("audit_audio_path is required for action='reaper_to_shoots_storyboard'")
        return build_reaper_to_shoots_storyboard(
            audio_path=audit_audio_path,
            bpm=bpm,
            bars_per_scene=bars_per_scene,
            visual_style_prompt=visual_style_prompt,
            plan_tier=plan_tier,
            chords=chords,
            lyrics=lyrics,
            aspect_ratio=video_aspect,
        )
    if act in ("render_video", "render_reaper_video_pipeline"):
        if not audit_audio_path or not output_mp4_path:
            raise ValueError("audit_audio_path and output_mp4_path are required for action='render_video'")
        return render_reaper_video_pipeline(
            audio_path=audit_audio_path,
            output_mp4_path=output_mp4_path,
            mode=video_mode,
            preset=preset,
            prompt=prompt,
            eq_18band_db=eq_18band_db,
            title=title,
            artist=artist,
            isrc=isrc,
            chords=chords,
            lyrics=lyrics,
            output_rpp_path=output_rpp_path,
            bpm=bpm,
        )
    if act == "insert_video_scene":
        scene_target = audit_audio_path or (video_scenes[0] if video_scenes else None)
        if not scene_target or isinstance(scene_target, dict):
            raise ValueError("Provide a file path in audit_audio_path or video_scenes[0] for action='insert_video_scene'")
        return bridge.insert_video_scene_to_reaper(scene_target, pos_sec=0.0, open_video_window=True)
    raise ValueError(
        f"Unsupported REAPER action '{action}'. Valid: install, inspect, configure_bridge, live_status, push_eq, send_osc, import_stems, inject_isrc, generate_rpp, reaper_to_shoots_storyboard, render_video, insert_video_scene, run_action"
    )


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
        "account_tier_entitlements": ACCOUNT_TIER_ENTITLEMENTS,
    }


# ---------------------------------------------------------------------------
# 2B. New Feature Clusters (Gaps 1-7): Bulk Mastering, YouTube/SoundCloud Ingestion,
#     Voice/Toki AI Prompt, Full Music Video & Shoots Director, Studio Practice Voicings,
#     Local Pitch/Time-Stretch, Gapless MP3 Inspector, Track Lifecycle & Account/Billing
# ---------------------------------------------------------------------------
@mcp.tool(
    tags={"mastering", "bulk", "album", "cloud"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="SoundBoost Cloud Bulk & Album Mastering Engine (Pay-Gated)",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def soundboost_bulk_and_album_mastering(
    action: str = "cloud_list",
    track_ids: list[str | int] | None = None,
    batch_id: str | int | None = None,
    mode: str = "auto",
    preset: str = "pop",
    prompt: str | None = None,
    target_lufs: float = -14.0,
    reference_track_id: str | int | None = None,
    wait_for_completion: bool = False,
    poll_interval_sec: float = 3.0,
    max_wait_sec: float = 300.0,
    ctx: Context | None = None,
) -> Any:
    """Batch-master an entire Album/EP via SoundBoost's authenticated Cloud Bulk Mastering API (`/api/bulk-mastering/*`):
    - `cloud_start`: Start a cloud bulk mastering batch (`POST /api/bulk-mastering/start/`, requires Pro/Plus authentication)
    - `cloud_list`: List all cloud bulk mastering batches (`GET /api/bulk-mastering/batches/`)
    - `cloud_status`: Poll cloud bulk mastering batch progress (`GET /api/bulk-mastering/batches/{id}/status/`)
    - `cloud_export_zip`: Fetch completed multi-track ZIP archive (`GET /api/bulk-mastering/batches/{id}/export-zip/`)
    """
    act = action.strip().lower()
    client = SoundBoostClient()
    if act in ("cloud_start", "start"):
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
            reference_track_id=reference_track_id,
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
    if act in ("cloud_list", "list"):
        return await asyncio.to_thread(client.list_bulk_mastering_batches)
    if act in ("cloud_status", "status"):
        if batch_id is None:
            raise ValueError("batch_id is required for action='cloud_status'")
        return await asyncio.to_thread(client.get_bulk_mastering_status, batch_id)
    if act in ("cloud_export_zip", "export_zip"):
        if batch_id is None:
            raise ValueError("batch_id is required for action='cloud_export_zip'")
        return await asyncio.to_thread(client.get_bulk_mastering_export_zip, batch_id)
    raise ValueError(f"Unsupported action '{action}'. Valid: cloud_start, cloud_list, cloud_status, cloud_export_zip")


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
    tags={"ai", "prompt", "voice", "toki", "cloud"},
    annotations=ToolAnnotations(
        title="Voice-Controlled AI Mastering & Cloud Toki Prompt Engine (Pay-Gated)",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
def soundboost_voice_and_prompt_assistant(
    action: str = "cloud_ai_prompt",
    prompt_or_speech: str = "warm low end, clean mud, crisp vocal presence, airy 15kHz top end",
    track_id: str | int | None = None,
) -> dict[str, Any]:
    """Drive SoundBoost's authenticated Cloud `Toki` Prompt Engine & `Deepgram` Voice-Controlled Mastering APIs:
    - `cloud_ai_prompt`: Translate natural-language mastering prompt via SoundBoost Cloud AI (`POST /api/ai/prompt`)
    - `toki_check`: Check status of the Toki AI Mastering assistant (`GET /api/toki/check/`)
    - `mastering_voice_token`: Acquire Deepgram token for Voice-Controlled Mastering (`POST /api/mastering-voice/deepgram-token`)
    - `mastering_voice_speech`: Submit spoken mastering revision transcript (`POST /api/mastering-voice/speech`)
    - `mv_voice_token`: Acquire Deepgram token for Voice-Controlled Music Video Director (`POST /api/music-video-voice/deepgram-token`)
    """
    act = action.strip().lower()
    client = SoundBoostClient()
    if act in ("cloud_ai_prompt", "prompt"):
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
    audio_path: str | None = None,
    output_mp4_path: str | None = None,
    output_rpp_path: str | None = None,
    bpm: float = 120.0,
    bars_per_scene: int = 4,
    aspect_ratio: str = "9:16",
    preset: str = "pop",
    plan_tier: str = "director",
) -> Any:
    """Orchestrate AI Music Videos (`h3-max`), `Shoots` storyboard scenes, Director Chat, Spotify 9:16 Canvas clips, `shoots.soundboost.ai` SSO, and the **Bidirectional REAPER <-> Video Pipeline**:
    - `shoots_sso_url`: Build cross-domain SSO URL (`https://mastering.soundboost.ai/sso?provider=google&appName=shoots&gacTkn=...`) and exchange SSO token
    - `reaper_to_shoots_storyboard`: Analyze local `audio_path` + `bpm` into bar-locked `Shoots` (`h3-max`) storyboard scenes, camera motion cues, credit spending cap, and REAPER timeline regions
    - `render_reaper_video_pipeline`: Render an audio-reactive `.mp4` (`output_mp4_path`, `9:16` Spotify Canvas / Shorts or `16:9` MV) with 18-Band Mastering EQ HUD, mastered 24-bit WAV companion, and optional `.RPP` project (`output_rpp_path`)
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
    if act in ("reaper_to_shoots_storyboard", "storyboard"):
        if not audio_path:
            raise ValueError("audio_path is required for action='reaper_to_shoots_storyboard'")
        return await asyncio.to_thread(
            build_reaper_to_shoots_storyboard,
            audio_path,
            bpm=bpm,
            bars_per_scene=bars_per_scene,
            visual_style_prompt=director_message
            or "Cinematic neon nocturnal cyberpunk performance, anamorphic 35mm lens",
            plan_tier=plan_tier,
            aspect_ratio=aspect_ratio,
        )
    if act in ("render_reaper_video_pipeline", "render_video"):
        if not audio_path or not output_mp4_path:
            raise ValueError("audio_path and output_mp4_path are required for action='render_reaper_video_pipeline'")
        mode_str = "music_video_16x9" if "16:9" in aspect_ratio or "16x9" in aspect_ratio else "spotify_canvas_9x16"
        return await asyncio.to_thread(
            render_reaper_video_pipeline,
            audio_path=audio_path,
            output_mp4_path=output_mp4_path,
            mode=mode_str,
            preset=preset,
            prompt=director_message,
            output_rpp_path=output_rpp_path,
            bpm=bpm,
        )
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
    tags={"cloud", "studio", "practice", "chords", "pitch-shift", "mp3", "gapless"},
    task=TaskConfig(mode="optional"),
    annotations=ToolAnnotations(
        title="Studio Cloud Pitch/Time-Stretch Stem Export (Pay-Gated), Chord Voicings & Gapless MP3 Inspector",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
async def studio_practice_voicings_and_cloud_shifter(
    action: str = "chord_voicings",
    original_id: str | int | None = None,
    chords: list[str] | None = None,
    instrument: str = "guitar",
    input_path: str | None = None,
    semitones: float = 0.0,
    speed_factor: float = 1.0,
    loop_start_sec: float | None = None,
    loop_end_sec: float | None = None,
    export_format: str = "wav",
    bit_depth: int = 24,
    sample_rate: int = 48000,
    stem_volumes: dict[str, float] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Studio Practice Voicings, Gapless MP3 Inspector, and Pay-Gated Cloud Pitch/Time-Stretch Stem Exporter:
    - `cloud_stem_export`: Export pitched (`semitones`) / time-stretched (`speed_factor`) stems or loop segments via SoundBoost's authenticated Cloud Studio endpoints (`POST /api/studio/originals/{id}/export/` & `POST https://converta.soundboost.ai/api/v1/stem-export`)
    - `chord_voicings`: Generate interactive Chord Fingering Voicings & ASCII fretboard/keyboard diagrams for `guitar` (6-string), `ukulele` (4-string), or `piano` (MIDI + keys, Module 534)
    - `inspect_gapless_mp3`: Parse binary MP3 frames (`1152` samples/frame), `Xing`/`Info`/`LAME3.100` gapless headers, encoder delay (`576`), decoder delay (`529`), and total priming samples (`1105`, Modules 590-591)
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
    if act in ("cloud_stem_export", "pitch_time_stretch"):
        client = SoundBoostClient()
        export_spec: dict[str, Any] = {
            "semitones": semitones,
            "pitch_shift_info": classify_pitch_shift_zone(round(semitones)),
            "speed_factor": speed_factor,
            "format": export_format,
            "bit_depth": bit_depth,
            "sample_rate": sample_rate,
        }
        if loop_start_sec is not None and loop_end_sec is not None:
            export_spec["loop_start"] = loop_start_sec
            export_spec["loop_end"] = loop_end_sec
        if stem_volumes:
            export_spec["stem_volumes"] = stem_volumes
        await _notify_progress(
            ctx,
            20.0,
            100.0,
            f"Submitting pay-gated Studio cloud pitch/time-stretch export ({semitones:+.1f} st, {speed_factor:.2f}x)...",
        )
        if original_id is not None:
            export_spec["original_id"] = original_id
            return await asyncio.to_thread(client.export_studio_stems, original_id, export_spec)
        return await asyncio.to_thread(client.request_converta_stem_export, export_spec)
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
    - `entitlements`: Resolve the exact SoundBoost account level (`free`, `pay_as_you_go`, `unlimited`, `unlimited_plus`, `starter`, `creator`, `director`, `studio`) and unlocked feature matrix for the authenticated user
    - `all_tiers`: Return the full SoundBoost account-level entitlement matrix (`ACCOUNT_TIER_ENTITLEMENTS`)
    - `products`: List available subscription plans & credit packs (`GET /api/products/`)
    - `checkout_session`: Create Stripe checkout session (`POST /api/create-checkout-session`)
    - `portal_session`: Create Stripe customer billing portal session (`POST /api/create-customer-portal-session/`)
    - `pause_subscription`: Pause active subscription (`POST /api/pause-subscription/`)
    - `resume_subscription`: Resume paused subscription (`POST /api/resume-subscription/`)
    - `referral_info`: Fetch artist referral link and reward stats (`GET /api/get-referral-info`)
    """
    client = SoundBoostClient()
    act = action.strip().lower()
    if act == "entitlements":
        return client.get_my_entitlements()
    if act in {"all_tiers", "tiers"}:
        return {"account_tier_entitlements": ACCOUNT_TIER_ENTITLEMENTS}
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
    "soundboost://specs/tool-packs",
    name="SoundBoost Progressive MCP Tool Discovery Packs",
    description="Directory of all 8 modular SoundBoost MCP tool packs and account-tier pack mappings for progressive context-window loading.",
    mime_type="application/json",
)
def resource_tool_packs() -> str:
    return json.dumps(
        {
            "tool_packs": MCP_TOOL_PACKS,
            "tier_unlocked_packs": TIER_UNLOCKED_MCP_PACKS,
        },
        indent=2,
    )


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
        "4. Re-audit the mastered file with `audit_audio_loudness_and_release_readiness` to confirm `release_ready_heuristic == True`.\n"
        f"5. Embed release metadata and ISRC (`{isrc or 'CC-XXX-YY-NNNNN'}`) using `write_release_metadata_tags`."
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
