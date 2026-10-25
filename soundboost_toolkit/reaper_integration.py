"""Deep REAPER 7.82 (x64) Integration Engine for Suavecito's SoundBoost.AI Toolkit.

Provides:
1. Forensic Environment & Plugin Arsenal Inspector (`REAPER.ini`, `reaper-vstplugins64.ini`,
   `reaper-clap-win64.ini`, `reaper_python.py` 739 ReaScript API bindings, live `reaper.exe` PID).
2. Asset & Bridge Installer:
   - `Effects/Suavecito/SoundBoost_18Band_Mastering_EQ_Monitor.jsfx` (`options:gmem=SuavecitoSoundBoost`)
   - `Scripts/Suavecito/Suavecito_SoundBoost_Stem_Import.lua` (Folder Buses + 5-Piece Drum MIDI Triggers)
   - `Scripts/Suavecito/Suavecito_Live_Bridge_Daemon.lua` + `Scripts/__startup.lua` auto-hook
   - `VideoPresets/Suavecito_SoundBoost_9x16_Canvas_Visualizer.txt` (`//@gmem=SuavecitoSoundBoost`)
   - 12 `.RfxChain` Genre Mastering FX Chains (`FXChains/Suavecito/SoundBoost_<Genre>.RfxChain`)
   - Safe `REAPER.ini` configuration (`csurf_0` HTTP Web Control Surface, `csurf_1` UDP OSC, Python 3.13 ReaScript)
3. Cross-Desktop Live REAPER Controller (`ReaperLiveBridge`):
   - Loopback HTTP Web Control Surface (`http://127.0.0.1:8080/_/`)
   - Binary UDP Open Sound Control (`OSC` RFC encoder to `127.0.0.1:9000`)
   - File Mailbox + `gmem=SuavecitoSoundBoost` IPC (`%APPDATA%/REAPER/Suavecito/ipc_inbox.json` <-> `ipc_outbox.json`)
4. Complete `.RPP` Multitrack Session Generator (`generate_soundboost_rpp_project`):
   - Generates REAPER 7.82 `.RPP` projects with hierarchical folder buses (`DRUM KIT BUS`, `VOCAL BUS`,
     `INSTRUMENT BUS`), native track icons, 5-Piece Drum `JS: midi/drumtrigger` FX, 100ms True-Peak
     clipping hotspot markers, Studio Chord/Lyric regions, `<RENDER_METADATA>` ISRC tags, Red Book
     DDP `@`/`!`/`#` markers, and Master 18-Band EQ + Loudness Meter chain.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import struct
import sys
import time
import uuid
from collections.abc import Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from soundboost_toolkit.constants import (
    MASTERING_EQ_FREQUENCIES_HZ,
    MASTERING_GENRE_PRESETS,
    MV_CHARACTER_REF_CAPS,
    STREAMING_TARGETS,
    compute_mv_spending_limit,
)
from soundboost_toolkit.dsp import (
    analyze_audio_file,
    render_18band_master,
    translate_prompt_to_18band_eq,
)

# ---------------------------------------------------------------------------
# Constants & Stem/Track Classification Specs
# ---------------------------------------------------------------------------
REAPER_DEFAULT_EXE = Path(r"C:\Program Files\REAPER (x64)\reaper.exe")

GENRE_PRESET_EQ_CURVES: dict[str, dict[str, Any]] = {
    "pop": {
        "eq_18band_db": [0.5, 1.0, 1.2, 0.8, 0.0, -0.5, -1.2, -0.8, 0.0, 0.4, 0.8, 1.2, 1.6, 1.8, 1.5, 2.0, 2.2, 1.8],
        "stereo_width_pct": 112.0,
        "makeup_drive_db": 1.5,
        "ceiling_dbtp": -1.0,
    },
    "hip_hop": {
        "eq_18band_db": [2.2, 2.8, 2.4, 1.5, 0.4, -0.8, -1.6, -1.2, -0.4, 0.0, 0.6, 1.2, 1.5, 1.2, 1.0, 1.6, 1.8, 1.4],
        "stereo_width_pct": 108.0,
        "makeup_drive_db": 2.0,
        "ceiling_dbtp": -1.0,
    },
    "trap": {
        "eq_18band_db": [2.8, 3.2, 2.5, 1.2, 0.0, -1.2, -1.8, -1.4, -0.5, 0.2, 0.8, 1.4, 1.8, 1.6, 1.8, 2.4, 2.6, 2.0],
        "stereo_width_pct": 115.0,
        "makeup_drive_db": 2.2,
        "ceiling_dbtp": -0.8,
    },
    "edm": {
        "eq_18band_db": [2.0, 2.6, 2.2, 1.4, 0.2, -1.0, -1.8, -1.2, 0.0, 0.5, 1.0, 1.5, 2.0, 2.2, 2.0, 2.5, 2.4, 1.8],
        "stereo_width_pct": 122.0,
        "makeup_drive_db": 2.4,
        "ceiling_dbtp": -0.8,
    },
    "rnb": {
        "eq_18band_db": [1.6, 2.2, 2.0, 1.4, 0.6, -0.4, -1.0, -0.6, 0.0, 0.4, 0.8, 1.2, 1.4, 1.2, 1.0, 1.6, 1.8, 1.5],
        "stereo_width_pct": 110.0,
        "makeup_drive_db": 1.2,
        "ceiling_dbtp": -1.0,
    },
    "rock": {
        "eq_18band_db": [0.4, 1.2, 1.8, 1.6, 0.8, 0.0, -0.8, -0.4, 0.4, 1.0, 1.6, 2.0, 1.8, 1.4, 1.0, 1.2, 1.0, 0.6],
        "stereo_width_pct": 108.0,
        "makeup_drive_db": 1.6,
        "ceiling_dbtp": -1.0,
    },
    "metal": {
        "eq_18band_db": [0.8, 1.8, 2.2, 1.4, 0.0, -1.4, -2.2, -1.6, 0.0, 0.8, 1.8, 2.4, 2.6, 2.0, 1.4, 1.6, 1.2, 0.8],
        "stereo_width_pct": 112.0,
        "makeup_drive_db": 2.0,
        "ceiling_dbtp": -0.9,
    },
    "indie": {
        "eq_18band_db": [0.2, 0.8, 1.2, 1.0, 0.6, 0.2, -0.4, 0.0, 0.4, 0.6, 0.8, 1.0, 1.2, 1.0, 0.8, 1.2, 1.4, 1.0],
        "stereo_width_pct": 110.0,
        "makeup_drive_db": 1.0,
        "ceiling_dbtp": -1.0,
    },
    "acoustic": {
        "eq_18band_db": [-0.5, 0.2, 0.8, 1.0, 0.6, 0.0, -0.6, -0.4, 0.2, 0.4, 0.6, 1.0, 1.2, 1.4, 1.2, 1.6, 1.8, 1.4],
        "stereo_width_pct": 104.0,
        "makeup_drive_db": 0.8,
        "ceiling_dbtp": -1.2,
    },
    "jazz": {
        "eq_18band_db": [0.2, 0.6, 1.0, 0.8, 0.4, 0.0, -0.4, -0.2, 0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.0, 1.2, 1.2, 0.8],
        "stereo_width_pct": 104.0,
        "makeup_drive_db": 0.5,
        "ceiling_dbtp": -1.5,
    },
    "classical": {
        "eq_18band_db": [0.4, 0.6, 0.6, 0.4, 0.2, 0.0, -0.2, 0.0, 0.0, 0.2, 0.4, 0.6, 0.8, 0.8, 1.0, 1.2, 1.2, 1.0],
        "stereo_width_pct": 102.0,
        "makeup_drive_db": 0.2,
        "ceiling_dbtp": -1.5,
    },
    "podcast_voice": {
        "eq_18band_db": [-3.5, -2.5, -1.0, 0.6, 1.2, 0.4, -1.4, -1.0, 0.2, 0.8, 1.4, 2.0, 2.2, 1.8, 0.8, 1.2, 1.0, 0.4],
        "stereo_width_pct": 90.0,
        "makeup_drive_db": 1.8,
        "ceiling_dbtp": -1.0,
    },
}

STEM_TRACK_METADATA: list[dict[str, Any]] = [
    {"match": "kick", "label": "Kick", "bus": "drums", "icon": "kick.png", "midi_note": 36, "rgb": (14, 165, 233)},
    {"match": "snare", "label": "Snare", "bus": "drums", "icon": "snare_top.png", "midi_note": 38, "rgb": (59, 130, 246)},
    {"match": "tom", "label": "Toms", "bus": "drums", "icon": "tom.png", "midi_note": 45, "rgb": (99, 102, 241)},
    {"match": "hi-hat", "label": "Hi-Hat", "bus": "drums", "icon": "hihat.png", "midi_note": 42, "rgb": (139, 92, 246)},
    {"match": "hihat", "label": "Hi-Hat", "bus": "drums", "icon": "hihat.png", "midi_note": 42, "rgb": (139, 92, 246)},
    {"match": "cymbal", "label": "Cymbals", "bus": "drums", "icon": "overheads.png", "midi_note": 49, "rgb": (168, 85, 247)},
    {"match": "drum", "label": "Drums", "bus": "drums", "icon": "drum.png", "midi_note": None, "rgb": (56, 189, 248)},
    {"match": "vocal", "label": "Vocals", "bus": "vocals", "icon": "mic_condenser_1.png", "midi_note": None, "rgb": (255, 92, 138)},
    {"match": "voice", "label": "Voice", "bus": "vocals", "icon": "mic_dynamic_1.png", "midi_note": None, "rgb": (244, 114, 182)},
    {"match": "artifact", "label": "Artifacts", "bus": "vocals", "icon": "fx.png", "midi_note": None, "rgb": (190, 24, 93)},
    {"match": "bass", "label": "Bass", "bus": "inst", "icon": "synthbass.png", "midi_note": None, "rgb": (255, 170, 45)},
    {"match": "guitar", "label": "Guitar", "bus": "inst", "icon": "guitar_full.png", "midi_note": None, "rgb": (52, 211, 153)},
    {"match": "piano", "label": "Piano", "bus": "inst", "icon": "piano.png", "midi_note": None, "rgb": (163, 230, 53)},
    {"match": "other", "label": "Other", "bus": "inst", "icon": "synth.png", "midi_note": None, "rgb": (148, 163, 184)},
]


def _rgb_to_reaper_native(r: int, g: int, b: int) -> int:
    """Convert `(R, G, B)` to REAPER native track/marker color integer (`0x1BBGGRR`)."""
    return 0x01000000 | ((b & 0xFF) << 16) | ((g & 0xFF) << 8) | (r & 0xFF)


def get_reaper_appdata_dir() -> Path:
    """Locate the user's REAPER resource directory (`%APPDATA%/REAPER` on Windows)."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = Path(appdata) / "REAPER"
        if candidate.exists():
            return candidate
    fallback = Path.home() / "AppData" / "Roaming" / "REAPER"
    return fallback


def _find_running_reaper_pids_win32() -> list[int]:
    """Enumerate running `reaper.exe` PIDs via Win32 Toolhelp32 snapshot (zero subprocess overhead)."""
    if sys.platform != "win32":
        return []

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    pids: list[int] = []
    kernel32 = ctypes.windll.kernel32
    snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snap == -1:
        return pids
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.lower() == "reaper.exe":
                    pids.append(int(pe.th32ProcessID))
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return pids


# ---------------------------------------------------------------------------
# 1. Forensic REAPER 7.82 Environment & Plugin Arsenal Inspector
# ---------------------------------------------------------------------------
def inspect_reaper_environment(reaper_resource_dir: str | Path | None = None) -> dict[str, Any]:
    """Forensically inspect the local REAPER 7.82 installation, running process,
    installed VST3/CLAP/JSFX plugins, control surfaces, and Python 3.13 ReaScript bindings.
    """
    res_dir = (
        Path(reaper_resource_dir).expanduser().resolve()
        if reaper_resource_dir
        else get_reaper_appdata_dir()
    )
    exe_path = REAPER_DEFAULT_EXE
    reaper_python_py = exe_path.parent / "Plugins" / "reaper_python.py"

    # Check running reaper.exe process via Win32 Toolhelp32 API without invoking GUI flags
    running_pids = _find_running_reaper_pids_win32()

    # Parse REAPER.ini for Control Surfaces, Audio Driver & Python ReaScript
    ini_path = res_dir / "REAPER.ini"
    ini_summary: dict[str, Any] = {
        "exists": ini_path.exists(),
        "reascript_enabled": False,
        "pythonlibdll64": None,
        "csurf_cnt": 0,
        "http_csurf_configured": False,
        "osc_csurf_configured": False,
        "last_project": None,
    }
    if ini_path.exists():
        raw_ini = ini_path.read_text(encoding="utf-8", errors="replace")
        for line in raw_ini.splitlines():
            s = line.strip()
            if s.startswith("reascript="):
                ini_summary["reascript_enabled"] = s.split("=", 1)[1].strip() == "1"
            elif s.startswith("pythonlibdll64="):
                ini_summary["pythonlibdll64"] = s.split("=", 1)[1].strip()
            elif s.startswith("csurf_cnt="):
                val = s.split("=", 1)[1].strip()
                ini_summary["csurf_cnt"] = int(val) if val.isdigit() else 0
            elif s.startswith("csurf_") and "HTTP" in s:
                ini_summary["http_csurf_configured"] = True
                ini_summary["http_csurf_line"] = s
            elif s.startswith("csurf_") and "OSC" in s:
                ini_summary["osc_csurf_configured"] = True
                ini_summary["osc_csurf_line"] = s
            elif s.startswith("lastproject="):
                ini_summary["last_project"] = s.split("=", 1)[1].strip()

    # Parse VST3 & CLAP plugins for SoundBoost workflow integration
    vst_ini = res_dir / "reaper-vstplugins64.ini"
    clap_ini = res_dir / "reaper-clap-win64.ini"
    discovered_plugins: dict[str, list[str]] = {
        "vocal_and_ai": [],
        "guitar_and_bass": [],
        "mastering_and_eq": [],
        "cockos_builtin": [],
        "clap_plugins": [],
    }
    if vst_ini.exists():
        for line in vst_ini.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line or line.startswith("["):
                continue
            plug_file, rhs = line.split("=", 1)
            parts = rhs.split(",")
            display_name = parts[2].strip() if len(parts) >= 3 else plug_file
            low = (plug_file + " " + display_name).lower()
            if any(k in low for k in ("ace_bridge", "graillon", "neutone", "de_ess", "harmonise", "reatune", "reapitch", "reavoice")):
                discovered_plugins["vocal_and_ai"].append(display_name)
            elif any(k in low for k in ("th_u", "saturator", "bass", "guitar")):
                discovered_plugins["guitar_and_bass"].append(display_name)
            elif any(k in low for k in ("lpeq", "lpmb", "adaptivelimiter", "master", "shapeit", "placeit", "borealis", "realimit", "reaxcomp", "reafir", "reaeq", "reacomp")):
                discovered_plugins["mastering_and_eq"].append(display_name)
            if "(cockos)" in low:
                discovered_plugins["cockos_builtin"].append(display_name)

    if clap_ini.exists():
        for line in clap_ini.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.startswith("["):
                discovered_plugins["clap_plugins"].append(line.split("=", 1)[1].strip())

    # Check Suavecito installed assets & live telemetry
    jsfx_path = res_dir / "Effects" / "Suavecito" / "SoundBoost_18Band_Mastering_EQ_Monitor.jsfx"
    daemon_path = res_dir / "Scripts" / "Suavecito" / "Suavecito_Live_Bridge_Daemon.lua"
    startup_path = res_dir / "Scripts" / "__startup.lua"
    telemetry_path = res_dir / "Suavecito" / "telemetry.json"

    live_telemetry = None
    if telemetry_path.exists():
        try:
            live_telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            live_telemetry = None
            _ = exc

    return {
        "reaper_executable": str(exe_path),
        "reaper_executable_exists": exe_path.exists(),
        "reaper_running": len(running_pids) > 0,
        "reaper_pids": running_pids,
        "reaper_resource_dir": str(res_dir),
        "reaper_python_sdk_path": str(reaper_python_py),
        "reaper_python_sdk_exists": reaper_python_py.exists(),
        "ini_bridge_config": ini_summary,
        "suavecito_assets": {
            "jsfx_installed": jsfx_path.exists(),
            "jsfx_gmem_enabled": jsfx_path.exists() and "gmem=SuavecitoSoundBoost" in jsfx_path.read_text(encoding="utf-8", errors="replace"),
            "live_bridge_daemon_installed": daemon_path.exists(),
            "startup_hook_installed": startup_path.exists() and "Suavecito_Live_Bridge_Daemon.lua" in startup_path.read_text(encoding="utf-8", errors="replace"),
            "telemetry_file_exists": telemetry_path.exists(),
        },
        "plugin_arsenal": discovered_plugins,
        "live_telemetry": live_telemetry,
    }


# ---------------------------------------------------------------------------
# 2. `.RfxChain` Genre Preset Generator & Deep Asset/INI Installer
# ---------------------------------------------------------------------------
def build_rfxchain_content(
    preset_name: str,
    eq_18band_db: list[float] | None = None,
    stereo_width_pct: float | None = None,
    makeup_drive_db: float | None = None,
    ceiling_dbtp: float | None = None,
) -> str:
    """Generate a native REAPER `.RfxChain` file containing:
    1. `JS: Suavecito/SoundBoost_18Band_Mastering_EQ_Monitor.jsfx` with all 18 band gains,
       stereo width, makeup drive, and brickwall ceiling pre-populated.
    2. `JS: analysis/loudness_meter` configured for True Peak + Integrated LUFS monitoring.
    """
    curve = GENRE_PRESET_EQ_CURVES.get(preset_name.lower(), GENRE_PRESET_EQ_CURVES["pop"])
    gains = list(eq_18band_db) if eq_18band_db and len(eq_18band_db) == 18 else list(curve["eq_18band_db"])
    width = stereo_width_pct if stereo_width_pct is not None else float(curve["stereo_width_pct"])
    drive = makeup_drive_db if makeup_drive_db is not None else float(curve["makeup_drive_db"])
    ceil = ceiling_dbtp if ceiling_dbtp is not None else float(curve["ceiling_dbtp"])

    slider_vals = " ".join(f"{g:.2f}" for g in gains) + f" {width:.1f} {drive:.2f} {ceil:.2f}"
    return (
        "<FXCHAIN\n"
        "  WNDRECT 80 80 920 620\n"
        "  SHOW 0\n"
        "  LASTSEL 0\n"
        "  DOCKED 0\n"
        "  BYPASS 0 0 0\n"
        '  <JS "Suavecito/SoundBoost_18Band_Mastering_EQ_Monitor.jsfx" ""\n'
        f"    {slider_vals}\n"
        "  >\n"
        "  WAK 0 0\n"
        "  BYPASS 0 0 0\n"
        '  <JS "analysis/loudness_meter" ""\n'
        "    0 1 1 1 1 1 1 0 0 0 -14 -1 1 0 15 0 0 0 0 0 0 0 0 0 0 0 0 0 0 -100 -100 -100 -100 -100 0 0\n"
        "  >\n"
        "  WAK 0 0\n"
        ">\n"
    )


SUAVECITO_CSURF_STATE_FILE = "REAPER.ini.suavecito.json"


def configure_reaper_ini_bridge(
    reaper_resource_dir: str | Path | None = None,
    http_port: int = 8080,
    osc_listen_port: int = 9000,
    osc_send_port: int = 9001,
    *,
    enable_python_reascript: bool = True,
    enable_osc_surface: bool = False,
    enable_http_web_surface: bool = False,
    allow_while_running: bool = False,
) -> dict[str, Any]:
    """Configure `REAPER.ini` for the Suavecito bridge without disturbing existing settings.

    - Python ReaScript (`reascript=1`, `pythonlibdll64`, `pythonlibpath64`) when requested.
    - Optional OSC (`enable_osc_surface`) and Web (`enable_http_web_surface`) control surfaces.
      Both are OFF by default: REAPER's OSC and web surfaces listen on every network interface
      and the web surface has no password unless one is set in Preferences, so anyone on the
      LAN could drive transport and run actions. The file-mailbox IPC daemon needs neither.
    - Existing control surfaces are preserved; only surfaces this function added before
      (tracked in `REAPER.ini.suavecito.json`) are replaced.
    - A timestamped backup of `REAPER.ini` is written before every change.
    - Refuses to edit while `reaper.exe` is running (REAPER rewrites `REAPER.ini` on exit,
      discarding external edits) unless `allow_while_running=True`.
    """
    res_dir = (
        Path(reaper_resource_dir).expanduser().resolve()
        if reaper_resource_dir
        else get_reaper_appdata_dir()
    )
    ini_path = res_dir / "REAPER.ini"
    if not ini_path.exists():
        return {"status": "skipped", "reason": f"REAPER.ini not found at {ini_path}"}
    edits_live_install = res_dir == get_reaper_appdata_dir().resolve()
    if (
        not allow_while_running
        and edits_live_install
        and sys.platform == "win32"
        and _find_running_reaper_pids_win32()
    ):
        return {
            "status": "skipped",
            "reason": "REAPER is running; close it first so it does not overwrite REAPER.ini on exit.",
        }

    state_path = res_dir / SUAVECITO_CSURF_STATE_FILE
    try:
        prior_state = json.loads(state_path.read_text(encoding="utf-8"))
        owned_lines = {str(x) for x in prior_state.get("owned_csurf_lines", [])}
    except (OSError, ValueError):
        owned_lines = set()

    backup_path = res_dir / f"REAPER.ini.suavecito-{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(ini_path, backup_path)

    py_dir = str(Path(sys.executable).resolve().parent)
    py_dll = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    wanted_surfaces: list[str] = []
    if enable_http_web_surface:
        wanted_surfaces.append(f"HTTP 0 {int(http_port)} 'index.html' '' 0 ''")
    if enable_osc_surface:
        wanted_surfaces.append(
            f'OSC "SoundBoost" 7 {int(osc_listen_port)} "127.0.0.1" {int(osc_send_port)} 1024 10 "Default"'
        )

    lines = ini_path.read_text(encoding="utf-8", errors="replace").splitlines()
    sec_start = next((i for i, ln in enumerate(lines) if ln.strip().lower() == "[reaper]"), None)
    if sec_start is None:
        lines.append("[reaper]")
        sec_start = len(lines) - 1
    sec_end = next(
        (i for i in range(sec_start + 1, len(lines)) if lines[i].strip().startswith("[")),
        len(lines),
    )
    section = lines[sec_start + 1 : sec_end]

    # Collect existing surfaces in order, dropping the ones we previously added.
    kept_surfaces: list[str] = []
    other_lines: list[str] = []
    csurf_values: dict[int, str] = {}
    for ln in section:
        key, sep, val = ln.partition("=")
        k = key.strip().lower()
        if sep and k.startswith("csurf_") and k[6:].isdigit():
            csurf_values[int(k[6:])] = val
        elif sep and k == "csurf_cnt":
            continue
        elif enable_python_reascript and sep and k in {"reascript", "pythonlibdll64", "pythonlibpath64"}:
            continue
        else:
            other_lines.append(ln)
    for idx in sorted(csurf_values):
        val = csurf_values[idx]
        legacy_http = val.strip() == f"HTTP 0 {int(http_port)} 'index.html' '' 0 ''"
        if val not in owned_lines and 'OSC "SoundBoost"' not in val and not legacy_http:
            kept_surfaces.append(val)

    surfaces = kept_surfaces + wanted_surfaces
    new_section = list(other_lines)
    if enable_python_reascript:
        new_section += ["reascript=1", f"pythonlibpath64={py_dir}", f"pythonlibdll64={py_dll}"]
    new_section.append(f"csurf_cnt={len(surfaces)}")
    new_section += [f"csurf_{i}={val}" for i, val in enumerate(surfaces)]

    lines[sec_start + 1 : sec_end] = new_section
    ini_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state_path.write_text(
        json.dumps({"owned_csurf_lines": wanted_surfaces}, indent=2), encoding="utf-8"
    )

    return {
        "status": "configured",
        "ini_path": str(ini_path),
        "backup_path": str(backup_path),
        "preserved_control_surfaces": len(kept_surfaces),
        "osc_surface_enabled": enable_osc_surface,
        "http_web_surface_enabled": enable_http_web_surface,
        "http_control_url": f"http://127.0.0.1:{http_port}/_/" if enable_http_web_surface else None,
        "osc_endpoint": f"127.0.0.1:{osc_listen_port}" if enable_osc_surface else None,
        "warning": (
            "REAPER's OSC/Web surfaces accept connections from the local network. Set a web "
            "username:password in Preferences > Control/OSC/web and firewall these ports."
            if wanted_surfaces
            else None
        ),
        "python_reascript_dll": py_dll if enable_python_reascript else None,
        "python_reascript_dir": py_dir if enable_python_reascript else None,
    }


def install_reaper_assets(
    reaper_resource_dir: str | Path | None = None,
    configure_ini_bridge: bool = False,
    enable_startup_daemon: bool = False,
    *,
    enable_osc_surface: bool = False,
    enable_http_web_surface: bool = False,
) -> dict[str, Any]:
    """Install the complete Suavecito SoundBoost REAPER 7.82 integration suite:
    - `Effects/Suavecito/SoundBoost_18Band_Mastering_EQ_Monitor.jsfx` (`options:gmem=SuavecitoSoundBoost`)
    - `Scripts/Suavecito/Suavecito_SoundBoost_Stem_Import.lua`
    - `Scripts/Suavecito/Suavecito_Live_Bridge_Daemon.lua`
    - `Scripts/__startup.lua` hook so REAPER automatically runs the Live Bridge Daemon
    - `VideoPresets/Suavecito_SoundBoost_9x16_Canvas_Visualizer.txt`
    - 12 `.RfxChain` Genre Mastering FX Chains (`FXChains/Suavecito/SoundBoost_<genre>.RfxChain`)
    - `REAPER.ini` Python ReaScript configuration (OSC / Web surfaces only when explicitly enabled)
    """
    repo_root = Path(__file__).resolve().parent.parent
    src_jsfx = repo_root / "reaper" / "Effects" / "Suavecito" / "SoundBoost_18Band_Mastering_EQ_Monitor.jsfx"
    src_stem_lua = repo_root / "reaper" / "Scripts" / "Suavecito_SoundBoost_Stem_Import.lua"
    src_daemon_lua = repo_root / "reaper" / "Scripts" / "Suavecito_Live_Bridge_Daemon.lua"
    src_video_preset = repo_root / "reaper" / "VideoPresets" / "Suavecito_SoundBoost_9x16_Canvas_Visualizer.txt"

    target_root = (
        Path(reaper_resource_dir).expanduser().resolve()
        if reaper_resource_dir
        else get_reaper_appdata_dir()
    )
    dst_jsfx_dir = target_root / "Effects" / "Suavecito"
    dst_lua_dir = target_root / "Scripts" / "Suavecito"
    dst_fxchains_dir = target_root / "FXChains" / "Suavecito"
    dst_video_dir = target_root / "VideoPresets"
    dst_ipc_dir = target_root / "Suavecito"

    for d in (dst_jsfx_dir, dst_lua_dir, dst_fxchains_dir, dst_video_dir, dst_ipc_dir):
        d.mkdir(parents=True, exist_ok=True)

    dst_jsfx = dst_jsfx_dir / src_jsfx.name
    dst_lua = dst_lua_dir / src_stem_lua.name
    dst_daemon = dst_lua_dir / src_daemon_lua.name
    dst_video = dst_video_dir / src_video_preset.name

    shutil.copy2(src_jsfx, dst_jsfx)
    shutil.copy2(src_stem_lua, dst_lua)
    shutil.copy2(src_daemon_lua, dst_daemon)
    if src_video_preset.exists():
        shutil.copy2(src_video_preset, dst_video)

    # Generate all 12 SoundBoost Genre .RfxChain files
    installed_fxchains: list[str] = []
    for preset in MASTERING_GENRE_PRESETS:
        chain_file = dst_fxchains_dir / f"SoundBoost_{preset}.RfxChain"
        chain_file.write_text(build_rfxchain_content(preset), encoding="utf-8")
        installed_fxchains.append(str(chain_file))

    # Configure Scripts/__startup.lua to auto-start Suavecito_Live_Bridge_Daemon.lua
    startup_lua = target_root / "Scripts" / "__startup.lua"
    hook_snippet = (
        "-- [Suavecito SoundBoost Live Bridge Auto-Start]\n"
        'local suavecito_daemon = reaper.GetResourcePath() .. "/Scripts/Suavecito/Suavecito_Live_Bridge_Daemon.lua"\n'
        'local f = io.open(suavecito_daemon, "r")\n'
        "if f then f:close(); dofile(suavecito_daemon) end\n"
    )
    if enable_startup_daemon:
        if startup_lua.exists():
            existing = startup_lua.read_text(encoding="utf-8", errors="replace")
            if "Suavecito_Live_Bridge_Daemon.lua" not in existing:
                shutil.copy2(startup_lua, startup_lua.with_name("__startup.lua.suavecito.bak"))
                startup_lua.write_text(existing.rstrip() + "\n\n" + hook_snippet, encoding="utf-8")
        else:
            startup_lua.write_text(hook_snippet, encoding="utf-8")

    ini_result = (
        configure_reaper_ini_bridge(
            target_root,
            enable_osc_surface=enable_osc_surface,
            enable_http_web_surface=enable_http_web_surface,
        )
        if configure_ini_bridge
        else {"status": "skipped"}
    )

    return {
        "reaper_resource_dir": str(target_root),
        "installed_jsfx": str(dst_jsfx),
        "installed_reascript": str(dst_lua),
        "installed_daemon_reascript": str(dst_daemon),
        "installed_startup_hook": str(startup_lua),
        "installed_video_preset": str(dst_video),
        "installed_fxchains_count": len(installed_fxchains),
        "installed_fxchains": installed_fxchains,
        "ini_bridge": ini_result,
        "status": "installed",
    }


# ---------------------------------------------------------------------------
# 3. Binary Open Sound Control (OSC) Packet Encoder & Live REAPER Bridge
# ---------------------------------------------------------------------------
def _osc_pad_string(val: str) -> bytes:
    """Encode an ASCII/UTF-8 string with null terminator padded to a 4-byte boundary per OSC 1.0."""
    raw = val.encode("utf-8") + b"\x00"
    rem = len(raw) % 4
    if rem != 0:
        raw += b"\x00" * (4 - rem)
    return raw


def build_osc_packet(address: str, *args: float | str) -> bytes:
    """Build a binary Open Sound Control (OSC 1.0) UDP packet for `reaper_csurf.dll`."""
    if not address.startswith("/"):
        address = "/" + address
    type_tags = [","]
    arg_payloads: list[bytes] = []
    for arg in args:
        if isinstance(arg, bool):
            type_tags.append("i")
            arg_payloads.append(struct.pack(">i", 1 if arg else 0))
        elif isinstance(arg, int):
            type_tags.append("i")
            arg_payloads.append(struct.pack(">i", arg))
        elif isinstance(arg, float):
            type_tags.append("f")
            arg_payloads.append(struct.pack(">f", arg))
        else:
            type_tags.append("s")
            arg_payloads.append(_osc_pad_string(str(arg)))
    return _osc_pad_string(address) + _osc_pad_string("".join(type_tags)) + b"".join(arg_payloads)


def _parse_http_control_line(parsed: dict[str, Any], tag: str, cols: list[str]) -> dict[str, Any]:
    """Parse one line of REAPER's HTTP Web Control Surface response.

    Raises ValueError/IndexError on a malformed or unexpected column layout; the
    caller counts those and keeps going so one odd line never fails the whole query.
    """
    if tag == "TRANSPORT" and len(cols) >= 6:
        parsed["transport"] = {
            "playstate": int(cols[1]),
            "position_sec": float(cols[2]),
            "is_repeat_on": cols[3] == "1",
            "position_string": cols[4],
            "position_beats": cols[5],
        }
    elif tag == "BEATPOS" and len(cols) >= 6:
        parsed["beatpos"] = {
            "BPM": float(cols[3]) if cols[3] else 120.0,
            "time_sig_num": int(float(cols[4])) if cols[4] else 4,
            "time_sig_denom": int(float(cols[5])) if cols[5] else 4,
        }
    elif tag == "NTRACK" and len(cols) >= 2:
        parsed["track_count"] = int(cols[1])
    elif tag == "TRACK" and len(cols) >= 6:
        parsed["tracks"].append(
            {
                "index": int(cols[1]),
                "name": cols[2],
                "flags": int(cols[3]),
                "volume": float(cols[4]),
                "pan": float(cols[5]),
            }
        )
    elif tag == "MARKER" and len(cols) >= 4:
        parsed["markers"].append({"name": cols[1], "id": cols[2], "pos_sec": float(cols[3])})
    elif tag == "REGION" and len(cols) >= 5:
        parsed["regions"].append(
            {"name": cols[1], "id": cols[2], "start_sec": float(cols[3]), "end_sec": float(cols[4])}
        )
    elif tag == "EXTSTATE" and len(cols) >= 4:
        parsed.setdefault("extstate", {})[f"{cols[1]}.{cols[2]}"] = cols[3]
    return parsed


class ReaperLiveBridge:
    """Triple-transport cross-desktop bridge to a running REAPER 7.82 instance:
    1. Loopback HTTP Web Control Surface (`http://127.0.0.1:8080/_/`)
    2. Loopback UDP Open Sound Control (`127.0.0.1:9000`)
    3. File Mailbox + `gmem=SuavecitoSoundBoost` IPC (`%APPDATA%/REAPER/Suavecito/ipc_inbox.json`)
    """

    def __init__(
        self,
        reaper_resource_dir: str | Path | None = None,
        http_host: str = "127.0.0.1",
        http_port: int = 8080,
        osc_host: str = "127.0.0.1",
        osc_port: int = 9000,
    ) -> None:
        self.resource_dir = (
            Path(reaper_resource_dir).expanduser().resolve()
            if reaper_resource_dir
            else get_reaper_appdata_dir()
        )
        self.ipc_dir = self.resource_dir / "Suavecito"
        self.ipc_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_path = self.ipc_dir / "ipc_inbox.json"
        self.outbox_path = self.ipc_dir / "ipc_outbox.json"
        self.telemetry_path = self.ipc_dir / "telemetry.json"
        # REAPER's web remote only speaks plain HTTP, so the bridge only ever talks to this machine.
        if http_host.strip().lower() not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("ReaperLiveBridge only connects to REAPER on this machine (loopback).")
        self.http_base = f"http://{http_host}:{http_port}/_"
        self.osc_host = osc_host
        self.osc_port = osc_port

    def send_osc(self, address: str, *args: float | str) -> dict[str, Any]:
        """Send a binary OSC UDP message directly to REAPER's OSC Control Surface (`127.0.0.1:9000`)."""
        packet = build_osc_packet(address, *args)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sent = sock.sendto(packet, (self.osc_host, self.osc_port))
        return {
            "transport": "osc_udp",
            "endpoint": f"{self.osc_host}:{self.osc_port}",
            "address": address,
            "args": list(args),
            "bytes_sent": sent,
        }

    def query_http_control(self, commands: str = "TRANSPORT;BEATPOS;NTRACK;TRACK;MARKER;REGION") -> dict[str, Any]:
        """Query REAPER's built-in HTTP Web Control Surface (`reaper_csurf_www/0.1` on `/_/`)."""
        url = f"{self.http_base}/{commands.lstrip('/')}"
        try:
            with httpx.Client(timeout=1.5) as client:
                resp = client.get(url)
                resp.raise_for_status()
                text = resp.text
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return {
                "reachable": False,
                "url": url,
                "error": str(exc),
                "note": "Restart REAPER once after running configure_reaper_ini_bridge() to bind HTTP port 8080.",
            }

        parsed: dict[str, Any] = {"reachable": True, "url": url, "tracks": [], "markers": [], "regions": []}
        malformed = 0
        for line in text.splitlines():
            cols = line.split("\t")
            tag = cols[0] if cols else ""
            try:
                parsed = _parse_http_control_line(parsed, tag, cols)
            except (ValueError, IndexError):
                malformed += 1
                continue
        if malformed:
            parsed["malformed_lines"] = malformed
        return parsed

    def dispatch_ipc_command(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        wait_for_ack_sec: float = 0.6,
    ) -> dict[str, Any]:
        """Write a command into `%APPDATA%/REAPER/Suavecito/ipc_inbox.json` for `Suavecito_Live_Bridge_Daemon.lua`
        and optionally wait up to `wait_for_ack_sec` for the synchronous ack in `ipc_outbox.json`.
        """
        req_id = f"sb_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        envelope: dict[str, Any] = {"request_id": req_id, "command": command}
        if payload:
            envelope.update(payload)

        tmp_path = self.inbox_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(envelope, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(self.inbox_path)

        # Also attempt HTTP ExtState notification if HTTP control surface is active
        try:
            enc_val = quote(json.dumps(envelope, separators=(",", ":")), safe="")
            with httpx.Client(timeout=0.35) as client:
                client.get(f"{self.http_base}/SET/EXTSTATE/Suavecito/InboxCommand/{enc_val}")
        except (httpx.HTTPError, OSError, ValueError) as exc:
            _ = exc

        deadline = time.monotonic() + max(0.05, wait_for_ack_sec)
        while time.monotonic() < deadline:
            if self.outbox_path.exists():
                try:
                    out = json.loads(self.outbox_path.read_text(encoding="utf-8"))
                    if out.get("request_id") == req_id:
                        return {"acknowledged": True, "request_id": req_id, "response": out}
                except (OSError, ValueError) as exc:
                    _ = exc
            time.sleep(0.05)

        return {
            "acknowledged": False,
            "request_id": req_id,
            "inbox_path": str(self.inbox_path),
            "command": command,
            "queued_payload": envelope,
            "note": "Command queued in ipc_inbox.json; Suavecito_Live_Bridge_Daemon.lua will execute it on next tick.",
        }

    def push_18band_eq_to_reaper(
        self,
        eq_18band_db: list[float] | None = None,
        stereo_width_pct: float = 100.0,
        makeup_drive_db: float = 0.0,
        ceiling_dbtp: float = -1.0,
        preset: str | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        """Push a live 18-band Mastering EQ curve (`20 Hz -> 18.5 kHz`), stereo width, makeup drive,
        and brickwall ceiling directly into REAPER via `gmem=SuavecitoSoundBoost` + OSC.
        """
        if eq_18band_db is None:
            if prompt:
                translated = translate_prompt_to_18band_eq(prompt)
                eq_18band_db = list(translated["eq_18band_db"])
                stereo_width_pct = float(
                    translated.get("recommended_stereo_width_pct", translated.get("stereo_width_pct", stereo_width_pct))
                )
            elif preset and preset.lower() in GENRE_PRESET_EQ_CURVES:
                curve = GENRE_PRESET_EQ_CURVES[preset.lower()]
                eq_18band_db = list(curve["eq_18band_db"])
                stereo_width_pct = float(curve["stereo_width_pct"])
                makeup_drive_db = float(curve["makeup_drive_db"])
                ceiling_dbtp = float(curve["ceiling_dbtp"])
            else:
                eq_18band_db = [0.0] * 18

        if len(eq_18band_db) != 18:
            raise ValueError(f"Expected 18 EQ bands, got {len(eq_18band_db)}")

        ipc_res = self.dispatch_ipc_command(
            "set_eq_18band",
            {
                "eq_18band_db": [round(float(x), 2) for x in eq_18band_db],
                "stereo_width_pct": round(float(stereo_width_pct), 1),
                "makeup_drive_db": round(float(makeup_drive_db), 2),
                "ceiling_dbtp": round(float(ceiling_dbtp), 2),
            },
        )
        return {
            "frequencies_hz": list(MASTERING_EQ_FREQUENCIES_HZ),
            "eq_18band_db": eq_18band_db,
            "stereo_width_pct": stereo_width_pct,
            "makeup_drive_db": makeup_drive_db,
            "ceiling_dbtp": ceiling_dbtp,
            "ipc_dispatch": ipc_res,
        }

    def insert_video_scene_to_reaper(
        self,
        video_path: str | Path,
        pos_sec: float = 0.0,
        open_video_window: bool = True,
    ) -> dict[str, Any]:
        """Insert a video clip or image scene (`.mp4`, `.mov`, `.webm`, `.gif`, `.png`, `.jpg`)
        onto the live REAPER `SHOOTS / 9:16 SPOTIFY CANVAS VIDEO` track via `Suavecito_Live_Bridge_Daemon.lua`.
        """
        resolved = Path(video_path).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Video/scene file not found: {resolved}")
        return self.dispatch_ipc_command(
            "insert_video_item",
            {
                "video_path": str(resolved),
                "pos_sec": round(float(pos_sec), 3),
                "open_video_window": 1 if open_video_window else 0,
            },
        )


def build_reaper_video_processor_fx_chunk(
    title: str | None = None,
    artist: str | None = None,
    isrc: str | None = None,
) -> str:
    """Build a native REAPER `<VIDEO_EFFECT "Video processor" "">` RPP chunk (`reaper_video.dll`)
    attached to `//@gmem=SuavecitoSoundBoost` for real-time 18-band EQ + Peak/GR/Phase HUD rendering.
    """
    hud_label = " — ".join(p for p in (artist, title, isrc) if p) or "Suavecito SoundBoost.AI Master"
    hud_label = hud_label.replace('"', "'")
    eel_lines = [
        "// Suavecito's SoundBoost.AI 9:16 Spotify Canvas & Shoots (h3-max) Audio-Reactive Video Processor",
        "//@gmem=SuavecitoSoundBoost",
        '//@param1:react_gain "Audio Reactivity" 1.0 0.1 3.0 1.0 0.05',
        '//@param2:show_hud "Show 18-Band EQ HUD (1=Yes,0=No)" 1 0 1 1 1',
        "input = 0;",
        "project_wh_valid === 0 ? input_info(input, project_w, project_h);",
        "gfx_blit(input, 1);",
        "show_hud >= 0.5 ? (",
        "  bar_area_w = project_w * 0.84;",
        "  bar_area_x = project_w * 0.08;",
        "  bar_base_y = project_h * 0.88;",
        "  bar_max_h  = project_h * 0.18;",
        "  slot_w = bar_area_w / 18;",
        "  gfx_set(0.05, 0.07, 0.12, 0.72);",
        "  gfx_fillrect(bar_area_x - 12, bar_base_y - bar_max_h - 44, bar_area_w + 24, bar_max_h + 64);",
        '  gfx_setfont(1, "Segoe UI", max(14, project_h * 0.018));',
        "  gfx_set(0.92, 0.96, 1.0, 0.95);",
        f'  gfx_str_draw("{hud_label}", bar_area_x, bar_base_y - bar_max_h - 34);',
        "  i = 0;",
        "  loop(18,",
        "    gain_db = gmem[10 + i] * react_gain;",
        "    norm = min(max((gain_db + 12.0) / 24.0, 0.06), 1.0);",
        "    h = norm * bar_max_h;",
        "    bx = bar_area_x + i * slot_w + 2;",
        "    by = bar_base_y - h;",
        "    t = i / 17.0;",
        "    gfx_set(0.15 + 0.80 * t, 0.78 - 0.42 * t, 0.96 - 0.18 * t, 0.92);",
        "    gfx_fillrect(bx, by, max(slot_w - 4, 2), h);",
        "    i += 1;",
        "  );",
        ");",
    ]
    code_body = "\n".join(f"          |{line}" for line in eel_lines)
    return (
        "    <FXCHAIN\n"
        "      BYPASS 0 0 0\n"
        '      <VIDEO_EFFECT "Video processor" ""\n'
        "        <CODE\n"
        f"{code_body}\n"
        "        >\n"
        "        CODEPARM 1.0000000000 1.0000000000 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
        "      >\n"
        "      WAK 0 0\n"
        "    >\n"
    )


# ---------------------------------------------------------------------------
# 4. Complete `.RPP` REAPER 7.82 Multitrack Session Generator
# ---------------------------------------------------------------------------
def _classify_stem_path(file_path: Path) -> dict[str, Any]:
    lower = file_path.name.lower()
    for spec in STEM_TRACK_METADATA:
        if spec["match"] in lower:
            return spec
    return {
        "match": "other",
        "label": file_path.stem,
        "bus": "inst",
        "icon": "WAVE.png",
        "midi_note": None,
        "rgb": (148, 163, 184),
    }


def generate_soundboost_rpp_project(
    output_rpp_path: str | Path,
    stem_files: Sequence[str | Path] | None = None,
    stems_folder: str | Path | None = None,
    bpm: float = 120.0,
    time_sig_num: int = 4,
    time_sig_denom: int = 4,
    preset: str = "pop",
    prompt: str | None = None,
    eq_18band_db: list[float] | None = None,
    stereo_width_pct: float | None = None,
    makeup_drive_db: float | None = None,
    ceiling_dbtp: float | None = None,
    isrc: str | None = None,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    key_signature: str | None = None,
    chords: list[dict[str, Any]] | None = None,
    lyrics: list[dict[str, Any]] | None = None,
    audit_audio_path: str | Path | None = None,
    target_platform: str = "spotify",
    add_drum_midi_triggers: bool = True,
    include_video_canvas_track: bool = True,
    video_scenes: Sequence[str | Path | dict[str, Any]] | None = None,
    video_aspect: str = "9:16",
) -> dict[str, Any]:
    """Generate a complete, ready-to-open REAPER 7.82 `.RPP` multitrack session containing:
    - Hierarchical folder buses (`DRUM KIT BUS`, `VOCAL BUS`, `INSTRUMENT BUS`) with native track icons
    - Optional `JS: midi/drumtrigger` FX on 5-Piece Drum Kit sub-stems (`Kick` -> 36, `Snare` -> 38, etc.)
    - Master Track FX Chain with `SoundBoost_18Band_Mastering_EQ_Monitor.jsfx` (`gmem=SuavecitoSoundBoost`)
      + `JS: analysis/loudness_meter`
    - `<RENDER_METADATA>` populated with `ID3:TSRC|<ISRC>`, `VORBIS:ISRC|<ISRC>`, `ID3:TIT2`, `ID3:TPE1`, `ID3:TALB`
    - DDP 2.0 / Red Book CD markers (`@Album`, `!` INDEX0, `#Title|ISRC=...` INDEX1)
    - 100ms True-Peak clipping hotspot markers from `analyze_audio_file()`
    - Studio AI Chord Regions and Word-Synced Lyric Markers
    - Optional `Shoots` / 9:16 Spotify Canvas Video Track (`Suavecito_SoundBoost_9x16_Canvas_Visualizer`)
    """
    out_path = Path(output_rpp_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Resolve stem files
    resolved_stems: list[Path] = []
    if stem_files:
        for sf in stem_files:
            p = Path(sf).expanduser().resolve()
            if p.exists():
                resolved_stems.append(p)
    if stems_folder:
        folder_p = Path(stems_folder).expanduser().resolve()
        if folder_p.is_dir():
            for ext in ("*.wav", "*.flac", "*.mp3", "*.aiff", "*.ogg"):
                for fp in sorted(folder_p.glob(ext)):
                    if fp not in resolved_stems:
                        resolved_stems.append(fp)

    # Resolve 18-band EQ curve
    curve = GENRE_PRESET_EQ_CURVES.get(preset.lower(), GENRE_PRESET_EQ_CURVES["pop"])
    if prompt:
        tr = translate_prompt_to_18band_eq(prompt)
        gains = list(tr["eq_18band_db"])
        width = float(tr.get("recommended_stereo_width_pct", tr.get("stereo_width_pct", curve["stereo_width_pct"])))
    elif eq_18band_db and len(eq_18band_db) == 18:
        gains = [float(x) for x in eq_18band_db]
        width = float(stereo_width_pct if stereo_width_pct is not None else curve["stereo_width_pct"])
    else:
        gains = list(curve["eq_18band_db"])
        width = float(stereo_width_pct if stereo_width_pct is not None else curve["stereo_width_pct"])

    drive = float(makeup_drive_db if makeup_drive_db is not None else curve["makeup_drive_db"])
    ceil = float(ceiling_dbtp if ceiling_dbtp is not None else curve["ceiling_dbtp"])

    # Optional loudness & True-Peak clipping hotspot audit
    audit_report: dict[str, Any] | None = None
    clipping_markers: list[dict[str, Any]] = []
    audit_target = Path(audit_audio_path).expanduser().resolve() if audit_audio_path else (resolved_stems[0] if resolved_stems else None)
    if audit_target and audit_target.exists() and audit_target.suffix.lower() in {".wav", ".flac", ".aiff", ".mp3"}:
        try:
            raw_audit = analyze_audio_file(audit_target)
            audit_report = raw_audit.to_dict()
            target_ceil = STREAMING_TARGETS.get(target_platform, STREAMING_TARGETS["spotify"]).true_peak_ceiling_dbtp
            tp_db = float(audit_report.get("true_peak_dbtp", -90.0))
            tp_sec = float(audit_report.get("true_peak_time_sec", 0.0))
            if tp_db > target_ceil:
                clipping_markers.append(
                    {
                        "pos_sec": round(tp_sec, 3),
                        "label": f"!CLIP {tp_db:+.2f} dBTP (>{target_ceil:.1f})",
                        "color": _rgb_to_reaper_native(239, 68, 68),
                    }
                )
        except (OSError, ValueError, RuntimeError) as exc:
            audit_report = None
            _ = exc

    # Build Markers & Regions lines
    marker_lines: list[str] = []
    marker_id = 1

    # DDP 2.0 / Red Book CD & ISRC Markers (reaper_ddp.dll standard)
    if album or artist:
        ddp_album = f"@{album or title or 'SoundBoost Album'}|PERFORMER={artist or 'Suavecito'}"
        marker_lines.append(f'  MARKER {marker_id} 0.0 "{ddp_album}" 0 0 1 R')
        marker_id += 1
    if isrc or title:
        ddp_index0 = "!00"
        marker_lines.append(f'  MARKER {marker_id} 0.0 "{ddp_index0}" 0 0 1 R')
        marker_id += 1
        ddp_track = f"#{title or out_path.stem}"
        if isrc:
            ddp_track += f"|ISRC={isrc}"
        if artist:
            ddp_track += f"|PERFORMER={artist}"
        marker_lines.append(
            f'  MARKER {marker_id} 0.0 "{ddp_track}" 0 {_rgb_to_reaper_native(16, 185, 129)} 1 R'
        )
        marker_id += 1

    # Loudness audit summary marker at 0.0s
    if audit_report:
        lufs_i = audit_report.get("integrated_lufs", -14.0)
        tp = audit_report.get("true_peak_dbtp", -1.0)
        marker_lines.append(
            f'  MARKER {marker_id} 0.0 "SOUNDBOOST AUDIT: {lufs_i:.1f} LUFS-I | {tp:.2f} dBTP ({target_platform.upper()})" 0 {_rgb_to_reaper_native(56, 189, 248)} 1 R'
        )
        marker_id += 1

    # 100ms True-Peak clipping hotspot markers
    for cm in clipping_markers:
        marker_lines.append(
            f'  MARKER {marker_id} {cm["pos_sec"]:.3f} "{cm["label"]}" 0 {cm["color"]} 1 R'
        )
        marker_id += 1

    # Studio AI Chord Regions
    region_id = 1
    if chords:
        for ch in chords:
            c_name = str(ch.get("chord") or ch.get("symbol") or "N")
            c_start = float(ch.get("start_sec", ch.get("start", 0.0)))
            c_end = float(ch.get("end_sec", ch.get("end", c_start + 2.0)))
            col = _rgb_to_reaper_native(168, 85, 247)
            marker_lines.append(f'  MARKER {region_id} {c_start:.3f} "CHORD: {c_name}" 1 {col} 1 R')
            marker_lines.append(f"  MARKER {region_id} {c_end:.3f} \"\" 1")
            region_id += 1

    # Word-synced Lyric Markers
    if lyrics:
        for lyr in lyrics:
            l_text = str(lyr.get("text", "")).replace('"', "'").strip()
            l_start = float(lyr.get("start_sec", lyr.get("start", 0.0)))
            if l_text:
                col = _rgb_to_reaper_native(244, 114, 182)
                marker_lines.append(f'  MARKER {marker_id} {l_start:.3f} "LYRIC: {l_text}" 0 {col} 1 R')
                marker_id += 1

    # Build <RENDER_METADATA> block for ISRC / ID3v2.3 / Vorbis / BWF embedding
    meta_tags: list[str] = []
    if isrc:
        meta_tags.append(f"    TAG ID3:TSRC {isrc}")
        meta_tags.append(f"    TAG VORBIS:ISRC {isrc}")
        meta_tags.append(f"    TAG IXML:ISRC {isrc}")
    if title:
        meta_tags.append(f'    TAG ID3:TIT2 "{title}"')
    if artist:
        meta_tags.append(f'    TAG ID3:TPE1 "{artist}"')
    if album:
        meta_tags.append(f'    TAG ID3:TALB "{album}"')
    if bpm:
        meta_tags.append(f"    TAG ID3:TBPM {round(bpm)}")
    if key_signature:
        meta_tags.append(f'    TAG ID3:TKEY "{key_signature}"')

    render_meta_block = ""
    if meta_tags:
        render_meta_block = "  <RENDER_METADATA\n" + "\n".join(meta_tags) + "\n  >\n"

    # Build Master FX Chain block (18-Band Mastering EQ + Loudness Meter + Video Sample Peeker)
    slider_vals = " ".join(f"{g:.2f}" for g in gains) + f" {width:.1f} {drive:.2f} {ceil:.2f}"
    master_fx_block = (
        "  <MASTERFXLIST\n"
        "    BYPASS 0 0 0\n"
        '    <JS "Suavecito/SoundBoost_18Band_Mastering_EQ_Monitor.jsfx" ""\n'
        f"      {slider_vals}\n"
        "    >\n"
        "    WAK 0 0\n"
        "    BYPASS 0 0 0\n"
        '    <JS "analysis/loudness_meter" ""\n'
        "      0 1 1 1 1 1 1 0 0 0 -14 -1 1 0 15 0 0 0 0 0 0 0 0 0 0 0 0 0 0 -100 -100 -100 -100 -100 0 0\n"
        "    >\n"
        "    WAK 0 0\n"
        "    BYPASS 0 0 0\n"
        '    <JS "misc/video_sample_peeker" ""\n'
        "      1.0\n"
        "    >\n"
        "    WAK 0 0\n"
        "  >\n"
    )

    # Group stems into hierarchical folder buses (DRUM KIT BUS, VOCAL BUS, INSTRUMENT BUS)
    grouped: dict[str, list[tuple[Path, dict[str, Any]]]] = {"drums": [], "vocals": [], "inst": []}
    for sf in resolved_stems:
        spec = _classify_stem_path(sf)
        grouped[spec["bus"]].append((sf, spec))

    bus_headers = {
        "drums": ("DRUM KIT BUS", "folder_drums.png", (14, 116, 144)),
        "vocals": ("VOCAL BUS", "folder_vocals.png", (190, 24, 93)),
        "inst": ("INSTRUMENT BUS", "folder_synth.png", (21, 128, 61)),
    }

    track_blocks: list[str] = []
    created_tracks_summary: list[dict[str, Any]] = []

    for bus_key in ("drums", "vocals", "inst"):
        items = grouped[bus_key]
        if not items:
            continue
        b_name, b_icon, b_rgb = bus_headers[bus_key]
        b_col = _rgb_to_reaper_native(*b_rgb)
        track_blocks.append(
            "  <TRACK\n"
            f'    NAME "{b_name}"\n'
            f"    PEAKCOL {b_col}\n"
            f'    TRACKIMGFN "{b_icon}"\n'
            "    ISBUS 1 1\n"
            "    VOLPAN 1 0 -1 -1 1\n"
            "  >"
        )
        created_tracks_summary.append({"name": b_name, "type": "folder_bus", "icon": b_icon})

        for i, (sf_path, spec) in enumerate(items):
            is_last = i == len(items) - 1
            isbus_str = "2 -1" if is_last else "0 0"
            t_col = _rgb_to_reaper_native(*spec["rgb"])
            t_name = f"{spec['label']} ({sf_path.name})"
            ext_type = "MP3" if sf_path.suffix.lower() == ".mp3" else ("FLAC" if sf_path.suffix.lower() == ".flac" else "WAVE")

            fx_block = ""
            if add_drum_midi_triggers and spec["midi_note"] is not None:
                fx_block = (
                    "    <FXCHAIN\n"
                    "      BYPASS 0 0 0\n"
                    '      <JS "midi/drumtrigger" ""\n'
                    f"        -18 -24 60 10 {spec['midi_note']} 1 50\n"
                    "      >\n"
                    "      WAK 0 0\n"
                    "    >\n"
                )

            track_blocks.append(
                "  <TRACK\n"
                f'    NAME "{t_name}"\n'
                f"    PEAKCOL {t_col}\n"
                f'    TRACKIMGFN "{spec["icon"]}"\n'
                f"    ISBUS {isbus_str}\n"
                "    VOLPAN 1 0 -1 -1 1\n"
                f"{fx_block}"
                "    <ITEM\n"
                "      POSITION 0\n"
                f'      NAME "{sf_path.name}"\n'
                "      VOLPAN 1 0 1 -1\n"
                "      SOFFS 0\n"
                "      PLAYRATE 1 1 0 -1 0 0.0025\n"
                f"      <SOURCE {ext_type}\n"
                f'        FILE "{sf_path}"\n'
                "      >\n"
                "    >\n"
                "  >"
            )
            created_tracks_summary.append(
                {
                    "name": t_name,
                    "bus": b_name,
                    "icon": spec["icon"],
                    "midi_trigger_note": spec["midi_note"] if add_drum_midi_triggers else None,
                    "file": str(sf_path),
                }
            )

    video_items_added = 0
    if include_video_canvas_track or video_scenes:
        v_col = _rgb_to_reaper_native(236, 72, 153)
        v_fx_chunk = build_reaper_video_processor_fx_chunk(title=title, artist=artist, isrc=isrc)
        v_items_chunks: list[str] = []
        bar_sec = (60.0 / max(20.0, bpm)) * float(time_sig_num)
        cursor_sec = 0.0
        for idx_s, vs in enumerate(video_scenes or []):
            if isinstance(vs, dict):
                v_file = Path(str(vs.get("file") or vs.get("path") or "")).expanduser().resolve()
                v_start = vs.get("start_sec")
                v_pos = float(v_start) if v_start is not None else cursor_sec
                v_len = float(vs.get("length_sec") or vs.get("duration_sec") or bar_sec * 2.0)
                v_name = str(vs.get("name") or f"Scene_{idx_s + 1:02d}_{v_file.name}")
            else:
                v_file = Path(vs).expanduser().resolve()
                v_pos = cursor_sec
                v_len = bar_sec * 2.0
                v_name = f"Scene_{idx_s + 1:02d}_{v_file.name}"
            cursor_sec = v_pos + v_len
            v_items_chunks.append(
                "    <ITEM\n"
                f"      POSITION {v_pos:.3f}\n"
                f"      LENGTH {v_len:.3f}\n"
                f'      NAME "{v_name}"\n'
                "      VOLPAN 1 0 1 -1\n"
                "      SOFFS 0\n"
                "      PLAYRATE 1 1 0 -1 0 0.0025\n"
                "      <SOURCE VIDEO\n"
                f'        FILE "{v_file}"\n'
                "      >\n"
                "    >\n"
            )
            video_items_added += 1

        track_blocks.append(
            "  <TRACK\n"
            '    NAME "SHOOTS / 9:16 SPOTIFY CANVAS VIDEO (h3-max gmem)"\n'
            f"    PEAKCOL {v_col}\n"
            '    TRACKIMGFN "video.png"\n'
            "    ISBUS 0 0\n"
            "    VOLPAN 1 0 -1 -1 1\n"
            f"{v_fx_chunk}"
            + "".join(v_items_chunks)
            + "  >"
        )
        created_tracks_summary.append(
            {
                "name": "SHOOTS / 9:16 SPOTIFY CANVAS VIDEO (h3-max gmem)",
                "type": "video_canvas",
                "icon": "video.png",
                "video_processor_gmem": "SuavecitoSoundBoost",
                "video_scenes_count": video_items_added,
            }
        )

    aspect_dims = {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "1:1": (1080, 1080),
    }
    vw, vh = aspect_dims.get(video_aspect.strip(), (1080, 1920))

    rpp_content = (
        '<REAPER_PROJECT 0.1 "7.82/win64" 1729680000\n'
        "  RIPPLE 0\n"
        "  GROUPOVERRIDE 0 0 0\n"
        "  AUTOXFADE 129\n"
        "  ENVATTACH 3\n"
        "  MIXERUIFLAGS 11 48\n"
        "  PEAKGAIN 1\n"
        "  FEEDBACK 0\n"
        "  PANLAW 1\n"
        "  PROJOFFS 0 0 0\n"
        "  MAXPROJLEN 0 600\n"
        "  GRID 3199 8 1 8 1 0 0 0\n"
        "  TIMEMODE 1 5 -1 30 0 0 -1\n"
        f"  VIDEO_CONFIG {vw} {vh} 256\n"
        "  PANMODE 3\n"
        "  CURSOR 0\n"
        "  ZOOM 100 0 0\n"
        "  VZOOMEX 6 0\n"
        "  USE_REC_CFG 0\n"
        "  RECMODE 1\n"
        "  SMPTESYNC 0 30 100 40 1000 300 0 0 1 0 0\n"
        "  LOOP 0\n"
        "  LOOPGRAN 0 4\n"
        "  RECORD_PATH \"\" \"\"\n"
        "  <RECORD_CFG\n"
        "    ZXZhdxgAAQ==\n"
        "  >\n"
        "  <APPLYFX_CFG\n"
        "  >\n"
        "  RENDER_FILE \"\"\n"
        "  RENDER_PATTERN \"\"\n"
        "  RENDER_FMT 0 2 48000\n"
        "  RENDER_1X 0\n"
        "  RENDER_RANGE 1 0 0 18 1000\n"
        "  RENDER_RESAMPLE 3 0 1\n"
        "  RENDER_ADDTOPROJ 0\n"
        "  RENDER_STEMS 0\n"
        "  RENDER_DITHER 3\n"
        f"{render_meta_block}"
        f"  TEMPO {bpm:.2f} {time_sig_num} {time_sig_denom}\n"
        "  PLAYRATE 1 0 0.25 4\n"
        "  SELECTION 0 0\n"
        "  SELECTION2 0 0\n"
        "  MASTERAUTOMODE 0\n"
        "  MASTERTRACKHEIGHT 0 0\n"
        "  MASTERPEAKCOL 16576\n"
        "  MASTERMUTESOLO 0\n"
        "  MASTERTRACKVIEW 0 0.6667 0.5 0.5 0 0 0 0 0 0 0 0 0 0\n"
        "  MASTERHWOUT 0 0 1 0 0 0 0 -1\n"
        "  MASTER_NCH 2 2\n"
        "  MASTER_VOLUME 1 0 -1 -1 1\n"
        "  MASTER_PANMODE 3\n"
        "  MASTER_FX 1\n"
        "  MASTER_SEL 0\n"
        f"{master_fx_block}"
        + ("\n".join(marker_lines) + "\n" if marker_lines else "")
        + ("\n".join(track_blocks) + "\n" if track_blocks else "")
        + ">\n"
    )

    out_path.write_text(rpp_content, encoding="utf-8")
    return {
        "rpp_path": str(out_path),
        "reaper_target_version": "7.82/win64",
        "bpm": bpm,
        "time_signature": f"{time_sig_num}/{time_sig_denom}",
        "preset": preset,
        "eq_18band_db": gains,
        "stereo_width_pct": width,
        "makeup_drive_db": drive,
        "ceiling_dbtp": ceil,
        "isrc": isrc,
        "tracks_created": len(created_tracks_summary),
        "tracks": created_tracks_summary,
        "clipping_markers_injected": len(clipping_markers),
        "chord_regions_injected": len(chords or []),
        "lyric_markers_injected": len(lyrics or []),
        "video_aspect": video_aspect,
        "video_dimensions": f"{vw}x{vh}",
        "video_scenes_injected": video_items_added,
        "audit_report": audit_report,
    }


# ---------------------------------------------------------------------------
# 5. Bidirectional REAPER <-> Video Pipeline (`Shoots` h3-max Storyboard + OpenCV/PIL MP4 Renderer)
# ---------------------------------------------------------------------------
def build_reaper_to_shoots_storyboard(
    audio_path: str | Path,
    bpm: float = 120.0,
    time_sig_num: int = 4,
    bars_per_scene: int = 4,
    visual_style_prompt: str = "Cinematic neon nocturnal cyberpunk performance, anamorphic 35mm lens",
    plan_tier: str = "director",
    chords: list[dict[str, Any]] | None = None,
    lyrics: list[dict[str, Any]] | None = None,
    aspect_ratio: str = "9:16",
) -> dict[str, Any]:
    """Analyze a REAPER audio bounce or stem (`audio_path`), quantize scene cuts to exact musical
    bar boundaries (`bars_per_scene`), classify each bar window's RMS/transient energy into camera
    direction cues, attach synchronized Chords & Lyrics, compute the `h3-max` credit budget cap,
    and return both a `Shoots` (`POST /api/shoots/`) payload and REAPER timeline regions.
    """
    import numpy as np
    import soundfile as sf

    path = Path(audio_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")

    samples, sr = sf.read(str(path), always_2d=True, dtype="float64")
    total_sec = samples.shape[0] / float(max(1, sr))
    bar_sec = (60.0 / max(20.0, float(bpm))) * float(max(1, time_sig_num))
    scene_dur = max(1.5, bar_sec * float(max(1, bars_per_scene)))

    mono = np.mean(samples, axis=1)
    global_rms = float(np.sqrt(np.mean(np.square(mono)) + 1e-12))

    scenes: list[dict[str, Any]] = []
    reaper_regions: list[dict[str, Any]] = []
    idx = 0
    t_start = 0.0
    while t_start < total_sec - 0.05:
        t_end = min(total_sec, t_start + scene_dur)
        s0 = round(t_start * sr)
        s1 = min(samples.shape[0], round(t_end * sr))
        seg = mono[s0:s1]
        seg_rms = float(np.sqrt(np.mean(np.square(seg)) + 1e-12)) if seg.size > 0 else 0.0
        seg_peak = float(np.max(np.abs(seg))) if seg.size > 0 else 0.0
        rel_energy = seg_rms / max(global_rms, 1e-6)

        if rel_energy >= 1.22 or seg_peak >= 0.92:
            energy_tier = "drop_peak"
            camera_motion = "Kinetic whip-pan on downbeat, rapid cut, anamorphic lens flare, high-energy motion"
        elif rel_energy >= 0.95:
            energy_tier = "lift_high"
            camera_motion = "Dynamic low-angle gimbal arc, high-contrast key lighting, wide performance framing"
        elif rel_energy >= 0.65:
            energy_tier = "groove_mid"
            camera_motion = "Smooth tracking steadicam, medium performance shot, rhythmic camera sway"
        else:
            energy_tier = "intimate_low"
            camera_motion = "Slow push-in dolly, shallow depth of field, atmospheric rim lighting"

        # Find active chords and lyrics in [t_start, t_end]
        active_chords = [
            str(c.get("chord") or c.get("symbol"))
            for c in (chords or [])
            if float(c.get("start_sec", c.get("start", 0.0))) < t_end
            and float(c.get("end_sec", c.get("end", t_end))) >= t_start
        ]
        active_lyrics = [
            str(ly.get("text", "")).strip()
            for ly in (lyrics or [])
            if t_start <= float(ly.get("start_sec", ly.get("start", 0.0))) < t_end
            and str(ly.get("text", "")).strip()
        ]

        idx += 1
        bar_start = (idx - 1) * bars_per_scene + 1
        bar_end = bar_start + bars_per_scene - 1
        lyric_cue = " ".join(active_lyrics) if active_lyrics else None
        chord_cue = " -> ".join(active_chords) if active_chords else None

        prompt_parts = [
            visual_style_prompt,
            f"Camera: {camera_motion}.",
            f"Bars {bar_start}-{bar_end} ({energy_tier.upper()}).",
        ]
        if chord_cue:
            prompt_parts.append(f"Harmonic progression: {chord_cue}.")
        if lyric_cue:
            prompt_parts.append(f'Lyric performance cue: "{lyric_cue}".')

        scenes.append(
            {
                "scene_number": idx,
                "bars": f"{bar_start}-{bar_end}",
                "start_sec": round(t_start, 3),
                "end_sec": round(t_end, 3),
                "duration_sec": round(t_end - t_start, 3),
                "energy_tier": energy_tier,
                "relative_energy": round(rel_energy, 3),
                "peak_dbfs": round(20.0 * float(np.log10(max(seg_peak, 1e-9))), 2),
                "camera_motion": camera_motion,
                "active_chords": active_chords,
                "active_lyrics": active_lyrics,
                "scene_prompt": " ".join(prompt_parts),
            }
        )
        reaper_regions.append(
            {
                "chord": f"SCENE {idx:02d} [{energy_tier.upper()}] {chord_cue or ''}".strip(),
                "start_sec": round(t_start, 3),
                "end_sec": round(t_end, 3),
            }
        )
        t_start = t_end

    tier_norm = plan_tier.strip().lower()
    quoted_credits = max(20, len(scenes) * 25)
    spending_cap = compute_mv_spending_limit(quoted_credits)

    return {
        "engine": "h3-max",
        "audio_path": str(path),
        "duration_sec": round(total_sec, 3),
        "bpm": bpm,
        "bars_per_scene": bars_per_scene,
        "aspect_ratio": aspect_ratio,
        "scene_count": len(scenes),
        "plan_tier": tier_norm,
        "quoted_credits": quoted_credits,
        "spending_limit_credits": spending_cap,
        "retake_headroom_credits": spending_cap - quoted_credits,
        "character_reference_cap": MV_CHARACTER_REF_CAPS.get(tier_norm, 1),
        "auto_qc_enabled": tier_norm in ("director", "studio"),
        "deep_research_enabled": tier_norm == "studio",
        "scenes": scenes,
        "reaper_scene_regions": reaper_regions,
        "shoots_api_payload": {
            "engine": "h3-max",
            "aspect_ratio": aspect_ratio,
            "bpm": bpm,
            "style_prompt": visual_style_prompt,
            "scenes": [
                {
                    "scene_index": s["scene_number"],
                    "start_sec": s["start_sec"],
                    "end_sec": s["end_sec"],
                    "prompt": s["scene_prompt"],
                }
                for s in scenes
            ],
        },
    }


def render_reaper_video_pipeline(
    audio_path: str | Path,
    output_mp4_path: str | Path,
    mode: str = "spotify_canvas_9x16",
    fps: float = 30.0,
    max_duration_sec: float | None = 8.0,
    preset: str = "pop",
    prompt: str | None = None,
    eq_18band_db: list[float] | None = None,
    title: str | None = None,
    artist: str | None = None,
    isrc: str | None = None,
    background_image_path: str | Path | None = None,
    chords: list[dict[str, Any]] | None = None,
    lyrics: list[dict[str, Any]] | None = None,
    export_mastered_wav: bool = True,
    output_rpp_path: str | Path | None = None,
    bpm: float = 120.0,
) -> dict[str, Any]:
    """Render an audio-reactive `.mp4` video (`spotify_canvas_9x16` `540x960` / `1080x1920`,
    `social_vertical_9x16`, or `music_video_16x9` `1280x720` / `1920x1080`) synchronized with
    SoundBoost's 18-Band Mastering EQ (`20 Hz -> 18.5 kHz`), Stereo Correlation meter, Chords,
    Karaoke Lyrics, and Title/Artist/ISRC HUD, plus optional `.RPP` project generation so the
    rendered video opens directly on REAPER's Video Timeline!
    """
    try:
        import cv2
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise ImportError(
            "Video rendering needs the optional extra: pip install 'suavecitos-soundboost-toolkit[video]'"
        ) from exc
    import numpy as np
    import scipy.signal as sg
    import soundfile as sf

    src_audio = Path(audio_path).expanduser().resolve()
    if not src_audio.exists():
        raise FileNotFoundError(f"Source audio not found: {src_audio}")

    out_mp4 = Path(output_mp4_path).expanduser().resolve()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    # Optionally render the 18-Band Mastered WAV companion
    master_wav_path: Path | None = None
    master_report: dict[str, Any] | None = None
    if export_mastered_wav:
        master_wav_path = out_mp4.with_name(f"{out_mp4.stem}_master_24bit.wav")
        master_report = render_18band_master(
            input_path=src_audio,
            output_path=master_wav_path,
            eq_gains_db=eq_18band_db,
            preset=None if eq_18band_db else preset,
            target_lufs=-14.0,
            true_peak_ceiling_dbtp=-1.0,
            bit_depth=24,
        )

    active_audio_file = master_wav_path if (master_wav_path and master_wav_path.exists()) else src_audio
    samples, sr = sf.read(str(active_audio_file), always_2d=True, dtype="float64")
    total_sec = samples.shape[0] / float(max(1, sr))
    if max_duration_sec is not None and max_duration_sec > 0:
        render_sec = min(total_sec, float(max_duration_sec))
    else:
        render_sec = total_sec

    mode_norm = mode.strip().lower()
    if "16x9" in mode_norm or "16:9" in mode_norm or "landscape" in mode_norm:
        frame_w, frame_h = 1280, 720
        aspect_str = "16:9"
    elif "1x1" in mode_norm or "square" in mode_norm:
        frame_w, frame_h = 720, 720
        aspect_str = "1:1"
    else:
        frame_w, frame_h = 540, 960
        aspect_str = "9:16"

    # Resolve 18-band EQ gains for visual HUD
    curve = GENRE_PRESET_EQ_CURVES.get(preset.lower(), GENRE_PRESET_EQ_CURVES["pop"])
    if prompt:
        tr = translate_prompt_to_18band_eq(prompt)
        eq_gains = list(tr["eq_18band_db"])
    elif eq_18band_db and len(eq_18band_db) == 18:
        eq_gains = [float(x) for x in eq_18band_db]
    else:
        eq_gains = list(curve["eq_18band_db"])

    # Prepare optional background image or dark studio gradient
    bg_bgr: np.ndarray = np.zeros((frame_h, frame_w, 3), dtype=np.uint8)
    for y in range(frame_h):
        ratio = y / float(max(1, frame_h - 1))
        bg_bgr[y, :, 0] = int(28 + 20 * ratio)  # B
        bg_bgr[y, :, 1] = int(14 + 10 * ratio)  # G
        bg_bgr[y, :, 2] = int(12 + 24 * ratio)  # R

    if background_image_path:
        bg_path = Path(background_image_path).expanduser().resolve()
        if bg_path.exists():
            pil_bg = Image.open(bg_path).convert("RGB").resize((frame_w, frame_h))
            rgb_arr = np.array(pil_bg, dtype=np.uint8)
            bg_bgr = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2BGR)
            bg_bgr = (bg_bgr.astype(np.float32) * 0.45).astype(np.uint8)

    total_frames = max(1, round(render_sec * fps))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]
    writer = cv2.VideoWriter(str(out_mp4), fourcc, float(fps), (frame_w, frame_h))

    mono = np.mean(samples, axis=1)
    freqs_18 = np.array(MASTERING_EQ_FREQUENCIES_HZ, dtype=np.float64)
    hud_header = " • ".join(p for p in (artist, title, isrc) if p) or f"Suavecito SoundBoost.AI ({preset.upper()})"

    for f_idx in range(total_frames):
        t_cur = f_idx / float(fps)
        center_sample = round(t_cur * sr)
        win_half = max(512, int(0.045 * sr))
        s0 = max(0, center_sample - win_half)
        s1 = min(samples.shape[0], center_sample + win_half)
        chunk = mono[s0:s1]

        # Compute 18-band FFT magnitude + EQ gain boost
        if chunk.size >= 64:
            windowed = chunk * sg.windows.hann(chunk.size)
            spec = np.abs(np.fft.rfft(windowed))
            fft_freqs = np.fft.rfftfreq(chunk.size, d=1.0 / float(sr))
            rms_lvl = float(np.sqrt(np.mean(np.square(chunk)) + 1e-12))
        else:
            spec = np.zeros(32, dtype=np.float64)
            fft_freqs = np.linspace(20.0, 20000.0, 32)
            rms_lvl = 0.01

        frame = bg_bgr.copy()

        # Reactive pulse ring in upper center
        pulse_r = int(min(frame_w, frame_h) * (0.12 + 0.18 * min(1.0, rms_lvl * 3.5)))
        cx, cy = frame_w // 2, int(frame_h * 0.34)
        cv2.circle(frame, (cx, cy), pulse_r, (235, 180, 56), 2, lineType=cv2.LINE_AA)
        cv2.circle(frame, (cx, cy), max(8, pulse_r // 2), (180, 92, 255), -1, lineType=cv2.LINE_AA)

        # Draw 18-Band Mastering EQ Spectrum Bars
        bar_area_w = int(frame_w * 0.84)
        bar_area_x = int(frame_w * 0.08)
        bar_base_y = int(frame_h * 0.84)
        bar_max_h = int(frame_h * 0.22)
        slot_w = max(4, bar_area_w // 18)

        cv2.rectangle(
            frame,
            (bar_area_x - 10, bar_base_y - bar_max_h - 46),
            (bar_area_x + bar_area_w + 10, bar_base_y + 38),
            (24, 18, 14),
            -1,
        )

        for b_i, f_hz in enumerate(freqs_18):
            idx_bin = int(np.argmin(np.abs(fft_freqs - f_hz)))
            mag_db = 20.0 * float(np.log10(max(float(spec[idx_bin]), 1e-6))) + float(eq_gains[b_i])
            norm = max(0.06, min(1.0, (mag_db + 28.0) / 48.0))
            bh = int(norm * bar_max_h)
            bx0 = bar_area_x + b_i * slot_w + 2
            bx1 = bx0 + max(2, slot_w - 4)
            by0 = bar_base_y - bh
            t_col = b_i / 17.0
            b_ch = int(245 - 45 * t_col)
            g_ch = int(198 - 105 * t_col)
            r_ch = int(40 + 205 * t_col)
            cv2.rectangle(frame, (bx0, by0), (bx1, bar_base_y), (b_ch, g_ch, r_ch), -1)

        # Find active chord & lyric at t_cur
        cur_chord = ""
        for c in chords or []:
            if float(c.get("start_sec", c.get("start", 0.0))) <= t_cur <= float(c.get("end_sec", c.get("end", 999.0))):
                cur_chord = str(c.get("chord") or c.get("symbol") or "")
                break
        cur_lyric = ""
        for lyr in lyrics or []:
            l0 = float(lyr.get("start_sec", lyr.get("start", 0.0)))
            l1 = float(lyr.get("end_sec", lyr.get("end", l0 + 2.5)))
            if l0 <= t_cur <= l1:
                cur_lyric = str(lyr.get("text", "")).strip()
                break

        # Render crisp text HUD via Pillow
        pil_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil_img)
        draw.text((bar_area_x, bar_base_y - bar_max_h - 34), hud_header, fill=(235, 245, 255))
        draw.text(
            (bar_area_x, bar_base_y + 10),
            f"18-BAND EQ ({preset.upper()})  |  t={t_cur:.2f}s  |  gmem=SuavecitoSoundBoost",
            fill=(148, 163, 184),
        )
        if cur_chord:
            draw.text((bar_area_x, int(frame_h * 0.10)), f"CHORD: {cur_chord}", fill=(56, 189, 248))
        if cur_lyric:
            draw.text((bar_area_x, int(frame_h * 0.54)), f'"{cur_lyric}"', fill=(255, 192, 225))

        writer.write(cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR))

    writer.release()

    # Optional: generate a companion REAPER .RPP project with the rendered MP4 + Mastered WAV on the timeline
    rpp_summary: dict[str, Any] | None = None
    if output_rpp_path:
        rpp_summary = generate_soundboost_rpp_project(
            output_rpp_path=output_rpp_path,
            stem_files=[active_audio_file],
            bpm=bpm,
            preset=preset,
            prompt=prompt,
            eq_18band_db=eq_gains,
            isrc=isrc,
            title=title,
            artist=artist,
            chords=chords,
            lyrics=lyrics,
            video_scenes=[
                {
                    "file": str(out_mp4),
                    "start_sec": 0.0,
                    "length_sec": round(render_sec, 3),
                    "name": f"Rendered_{mode_norm}_{out_mp4.name}",
                }
            ],
            video_aspect=aspect_str,
        )

    return {
        "output_mp4_path": str(out_mp4),
        "mp4_size_bytes": out_mp4.stat().st_size if out_mp4.exists() else 0,
        "mode": mode_norm,
        "aspect_ratio": aspect_str,
        "resolution": f"{frame_w}x{frame_h}",
        "fps": fps,
        "duration_sec": round(render_sec, 3),
        "frames_rendered": total_frames,
        "mastered_wav_path": str(master_wav_path) if master_wav_path else None,
        "master_audit": master_report.get("post_master_audit") if master_report else None,
        "reaper_rpp_project": rpp_summary,
    }

