"""Filesystem sandboxing and inbound-access policy for Suavecito's Soundboost.AI Toolkit.

MCP tools accept file paths from the calling model (and, over Streamable HTTP, from remote
clients), so every path crossing the tool boundary is resolved against an allow-list of root
directories before it is read or written.

Configuration:
- `SOUNDBOOST_ALLOWED_DIRS`: `os.pathsep`-separated root directories tools may read/write.
  Defaults to the current working directory plus the user's Music, Documents, Desktop,
  Downloads and Videos folders. Required explicitly when serving over HTTP.
- `SOUNDBOOST_MCP_ALLOWED_EMAILS`: comma-separated verified e-mail addresses permitted to call
  the HTTP server when an inbound OAuth provider is enabled.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ALLOWED_DIRS_ENV = "SOUNDBOOST_ALLOWED_DIRS"
ALLOWED_EMAILS_ENV = "SOUNDBOOST_MCP_ALLOWED_EMAILS"

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".wav", ".wave", ".aif", ".aiff", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus"}
)
IMAGE_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp"})
HTML_EXTENSIONS: frozenset[str] = frozenset({".html", ".htm"})
RPP_EXTENSIONS: frozenset[str] = frozenset({".rpp"})
VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mov", ".webm"})


class PathAccessError(PermissionError):
    """Raised when a tool argument points outside the configured allowed directories."""


def _default_roots() -> list[Path]:
    home = Path.home()
    roots = [Path.cwd()]
    roots.extend(home / name for name in ("Music", "Documents", "Desktop", "Downloads", "Videos"))
    return roots


def allowed_roots() -> list[Path]:
    """Return the resolved allow-list of root directories."""
    raw = os.environ.get(ALLOWED_DIRS_ENV, "").strip()
    candidates = (
        [Path(p) for p in raw.split(os.pathsep) if p.strip()] if raw else _default_roots()
    )
    return [c.expanduser().resolve() for c in candidates]


def allowed_dirs_explicitly_configured() -> bool:
    return bool(os.environ.get(ALLOWED_DIRS_ENV, "").strip())


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_user_path(
    raw_path: str | os.PathLike[str],
    *,
    must_exist: bool = False,
    allowed_suffixes: Iterable[str] | None = None,
    directory: bool = False,
    extra_roots: Iterable[Path] = (),
) -> Path:
    """Resolve a caller-supplied path and verify it lives under an allowed root.

    Symlinks are resolved first, so a link inside an allowed root that points outside of it
    is rejected. Hidden path components (`.ssh`, `.soundboost`, ...) are always rejected.
    """
    if raw_path is None or not str(raw_path).strip():
        raise PathAccessError("A non-empty path is required.")
    resolved = Path(raw_path).expanduser().resolve()

    roots = [*allowed_roots(), *(Path(r).expanduser().resolve() for r in extra_roots)]
    root = next((r for r in roots if _is_within(resolved, r)), None)
    if root is None:
        raise PathAccessError(
            f"Path '{resolved}' is outside the allowed directories. "
            f"Set {ALLOWED_DIRS_ENV} to grant access (current: {[str(r) for r in roots]})."
        )
    if any(part.startswith(".") for part in resolved.relative_to(root).parts):
        raise PathAccessError(f"Hidden path components are not accessible: '{resolved}'.")

    if allowed_suffixes is not None and not directory:
        suffixes = {s.lower() for s in allowed_suffixes}
        if resolved.suffix.lower() not in suffixes:
            raise PathAccessError(
                f"File type '{resolved.suffix or '(none)'}' is not permitted here; "
                f"expected one of {sorted(suffixes)}."
            )

    if must_exist:
        if directory and not resolved.is_dir():
            raise FileNotFoundError(f"Directory not found: {resolved}")
        if not directory and not resolved.is_file():
            raise FileNotFoundError(f"File not found: {resolved}")
    return resolved


def allowed_emails() -> set[str]:
    raw = os.environ.get(ALLOWED_EMAILS_ENV, "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def email_allowlist_check(ctx: Any) -> bool:
    """FastMCP `AuthCheck`: allow only verified e-mails listed in `SOUNDBOOST_MCP_ALLOWED_EMAILS`."""
    token = getattr(ctx, "token", None)
    if token is None:
        return False
    claims: dict[str, Any] = getattr(token, "claims", None) or {}
    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is False:
        return False
    return email in allowed_emails()


def is_loopback_host(host: str) -> bool:
    return host.strip().lower() in {"127.0.0.1", "localhost", "::1", "[::1]"}
