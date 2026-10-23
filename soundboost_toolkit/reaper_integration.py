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
from ctypes import wintypes
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from soundboost_toolkit.constants import (
    MASTERING_EQ_FREQUENCIES_HZ,
    MASTERING_GENRE_PRESETS,
    STREAMING_TARGETS,
)
from soundboost_toolkit.dsp import (
    analyze_audio_file,
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


def configure_reaper_ini_bridge(
    reaper_resource_dir: str | Path | None = None,
    http_port: int = 8080,
    osc_listen_port: int = 9000,
    osc_send_port: int = 9001,
) -> dict[str, Any]:
    """Safely configure `REAPER.ini` to enable:
    - Embedded Python 3.13 ReaScript (`reascript=1`, `pythonlibdll64=python313.dll`, `pythonlibpath64`)
    - Loopback HTTP Web Control Surface (`csurf_0=HTTP 0 8080 'index.html' '' 0 ''`)
    - Loopback UDP OSC Control Surface (`csurf_1=OSC "SoundBoost" 7 9000 "127.0.0.1" 9001 1024 10 "Default"`)
    Creates a `.suavecito.bak` backup of `REAPER.ini` before writing.
    """
    res_dir = (
        Path(reaper_resource_dir).expanduser().resolve()
        if reaper_resource_dir
        else get_reaper_appdata_dir()
    )
    ini_path = res_dir / "REAPER.ini"
    if not ini_path.exists():
        return {"status": "skipped", "reason": f"REAPER.ini not found at {ini_path}"}

    backup_path = res_dir / "REAPER.ini.suavecito.bak"
    if not backup_path.exists():
        shutil.copy2(ini_path, backup_path)

    py_dir = str(Path(sys.executable).resolve().parent)
    py_dll = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    http_line = f"csurf_0=HTTP 0 {http_port} 'index.html' '' 0 ''"
    osc_line = f'csurf_1=OSC "SoundBoost" 7 {osc_listen_port} "127.0.0.1" {osc_send_port} 1024 10 "Default"'

    lines = ini_path.read_text(encoding="utf-8", errors="replace").splitlines()
    new_lines: list[str] = []
    in_reaper_sec = False
    injected_in_reaper = False

    skip_prefixes = ("reascript=", "pythonlibdll64=", "pythonlibpath64=", "csurf_cnt=", "csurf_0=", "csurf_1=")
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_reaper_sec and not injected_in_reaper:
                new_lines.extend(
                    [
                        "reascript=1",
                        f"pythonlibpath64={py_dir}",
                        f"pythonlibdll64={py_dll}",
                        "csurf_cnt=2",
                        http_line,
                        osc_line,
                    ]
                )
                injected_in_reaper = True
            in_reaper_sec = stripped.lower() == "[reaper]"
            new_lines.append(line)
            continue

        if in_reaper_sec and any(stripped.startswith(p) for p in skip_prefixes):
            continue
        new_lines.append(line)

    if in_reaper_sec and not injected_in_reaper:
        new_lines.extend(
            [
                "reascript=1",
                f"pythonlibpath64={py_dir}",
                f"pythonlibdll64={py_dll}",
                "csurf_cnt=2",
                http_line,
                osc_line,
            ]
        )

    ini_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return {
        "status": "configured",
        "ini_path": str(ini_path),
        "backup_path": str(backup_path),
        "http_control_url": f"http://127.0.0.1:{http_port}/_/",
        "osc_endpoint": f"127.0.0.1:{osc_listen_port}",
        "python_reascript_dll": py_dll,
        "python_reascript_dir": py_dir,
    }


def install_reaper_assets(
    reaper_resource_dir: str | Path | None = None,
    configure_ini_bridge: bool = True,
    enable_startup_daemon: bool = True,
) -> dict[str, Any]:
    """Install the complete Suavecito SoundBoost REAPER 7.82 integration suite:
    - `Effects/Suavecito/SoundBoost_18Band_Mastering_EQ_Monitor.jsfx` (`options:gmem=SuavecitoSoundBoost`)
    - `Scripts/Suavecito/Suavecito_SoundBoost_Stem_Import.lua`
    - `Scripts/Suavecito/Suavecito_Live_Bridge_Daemon.lua`
    - `Scripts/__startup.lua` hook so REAPER automatically runs the Live Bridge Daemon
    - `VideoPresets/Suavecito_SoundBoost_9x16_Canvas_Visualizer.txt`
    - 12 `.RfxChain` Genre Mastering FX Chains (`FXChains/Suavecito/SoundBoost_<genre>.RfxChain`)
    - `REAPER.ini` HTTP (port 8080) + OSC (port 9000) + Python 3.13 ReaScript configuration
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
                startup_lua.write_text(existing.rstrip() + "\n\n" + hook_snippet, encoding="utf-8")
        else:
            startup_lua.write_text(hook_snippet, encoding="utf-8")

    ini_result = (
        configure_reaper_ini_bridge(target_root)
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
        for line in text.splitlines():
            cols = line.split("\t")
            tag = cols[0] if cols else ""
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
    stem_files: list[str | Path] | None = None,
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
            audit_report = raw_audit.to_dict() if hasattr(raw_audit, "to_dict") else dict(raw_audit)
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

    # Build Master FX Chain block
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

    if include_video_canvas_track:
        v_col = _rgb_to_reaper_native(236, 72, 153)
        track_blocks.append(
            "  <TRACK\n"
            '    NAME "SHOOTS / 9:16 SPOTIFY CANVAS VIDEO (h3-max gmem)"\n'
            f"    PEAKCOL {v_col}\n"
            '    TRACKIMGFN "video.png"\n'
            "    ISBUS 0 0\n"
            "    VOLPAN 1 0 -1 -1 1\n"
            "  >"
        )
        created_tracks_summary.append(
            {"name": "SHOOTS / 9:16 SPOTIFY CANVAS VIDEO (h3-max gmem)", "type": "video_canvas", "icon": "video.png"}
        )

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
        "  VIDEO_CONFIG 0 0 256\n"
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
        "audit_report": audit_report,
    }
