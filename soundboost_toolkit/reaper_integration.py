"""REAPER JSFX & ReaScript installer and `.RPP` multitrack session generator."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


def get_reaper_appdata_dir() -> Path:
    """Locate the user's REAPER resource directory (`%APPDATA%/REAPER` on Windows)."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = Path(appdata) / "REAPER"
        if candidate.exists():
            return candidate
    fallback = Path.home() / "AppData" / "Roaming" / "REAPER"
    return fallback


def install_reaper_assets(reaper_resource_dir: str | Path | None = None) -> dict[str, Any]:
    """Install the Suavecito SoundBoost 18-Band JSFX plugin and ReaScript into REAPER."""
    repo_root = Path(__file__).resolve().parent.parent
    src_jsfx = repo_root / "reaper" / "Effects" / "Suavecito" / "SoundBoost_18Band_Mastering_EQ_Monitor.jsfx"
    src_lua = repo_root / "reaper" / "Scripts" / "Suavecito_SoundBoost_Stem_Import.lua"

    target_root = (
        Path(reaper_resource_dir).expanduser().resolve()
        if reaper_resource_dir
        else get_reaper_appdata_dir()
    )
    dst_jsfx_dir = target_root / "Effects" / "Suavecito"
    dst_lua_dir = target_root / "Scripts" / "Suavecito"
    dst_jsfx_dir.mkdir(parents=True, exist_ok=True)
    dst_lua_dir.mkdir(parents=True, exist_ok=True)

    dst_jsfx = dst_jsfx_dir / src_jsfx.name
    dst_lua = dst_lua_dir / src_lua.name

    shutil.copy2(src_jsfx, dst_jsfx)
    shutil.copy2(src_lua, dst_lua)

    return {
        "reaper_resource_dir": str(target_root),
        "installed_jsfx": str(dst_jsfx),
        "installed_reascript": str(dst_lua),
        "status": "installed",
    }
