"""Unified HTTP API Client for the SoundBoost.ai (Diktatorial Suite) Ecosystem.

Covers all 4 microservices:
1. `https://api.soundboost.ai` — Mastering, Mix Doctor, Audio Enhancer, Studio Stems,
   5-Piece Drum Kit Splitter, Voice Cleaner, Chord Recognition, Word-Synced Lyrics,
   Smart Links (`soundboo.st`), Text-to-Audio, and SSO/Billing.
2. `https://mastering.soundboost.ai` — Suite Token Server, Static Param Decryptor,
   AI Music Video (`h3-max`), and Billing/User verification.
3. `https://converta.soundboost.ai` — Hi-Res Audio Format Converter (`wav/aiff/flac/ogg/aac/m4a/mp3`,
   `16/24/32-bit`, `44.1/48 kHz`, dithering) and Studio Stem/Loop Segment Exporter.
4. `https://shoots.soundboost.ai` — Musician AI Clones, Press Photos, Spotify 9:16 Canvas & Cover Art.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

from soundboost_toolkit.constants import (
    ACCOUNT_TIER_ENTITLEMENTS,
    BACKEND_URL,
    CONVERTA_FORMATS,
    CONVERTA_URL,
    MASTERING_EQ_FREQUENCIES_HZ,
    MASTERING_URL,
    STEM_PROFILES,
    STUDIO_URL,
)
from soundboost_toolkit.crypto import (
    build_ui_security_headers,
    decrypt_cookie_value,
    decrypt_param_id,
    encrypt_param_id,
)
from soundboost_toolkit.oauth import load_token_from_vault, save_credentials_to_vault

DEFAULT_TIMEOUT_SEC = 60.0

# Public/free endpoints that do not require an authenticated SoundBoost account/subscription
_PUBLIC_FREE_PATH_PREFIXES: tuple[str, ...] = (
    "/api/login/",
    "/api/google-login/",
    "/api/apple/login/",
    "/api/user/register",
    "/api/user/verify-email/",
    "/api/products/",
    "/api/health/",
    "/api/decrypt-param-static",
    "/api/v1/formats",
    "/api/v1/convert",
    "/api/v1/status/",
    "/api/audio/public/",
    "/api/public/",
)


class SoundBoostPayGateError(PermissionError):
    """Raised when a paid Mastering, Studio, or Shoots endpoint is called without an authenticated SoundBoost session."""


class SoundBoostClient:
    """Production client for SoundBoost.ai with automatic `ui-token` signing & R2 failover."""

    def __init__(
        self,
        access_token: str | None = None,
        encrypted_da_cookie: str | None = None,
        guest_token: str | None = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self.timeout_sec = timeout_sec
        self.backend_url = os.environ.get("SOUNDBOOST_BACKEND_URL", BACKEND_URL).rstrip("/")
        self.mastering_url = os.environ.get("SOUNDBOOST_MASTERING_URL", MASTERING_URL).rstrip("/")
        self.studio_url = os.environ.get("SOUNDBOOST_STUDIO_URL", STUDIO_URL).rstrip("/")
        self.converta_url = os.environ.get("SOUNDBOOST_CONVERTA_URL", CONVERTA_URL).rstrip("/")

        self.guest_token = guest_token or os.environ.get("SOUNDBOOST_GUEST_TOKEN")
        self.access_token = self._resolve_access_token(access_token, encrypted_da_cookie)
        self.account_tier: str | None = os.environ.get("SOUNDBOOST_ACCOUNT_TIER")
        self.cached_entitlements: dict[str, Any] | None = None
        self.storage_proxy_host: str | None = None
        self.storage_direct_host: str | None = None

    @staticmethod
    def _resolve_access_token(
        access_token: str | None,
        encrypted_da_cookie: str | None,
    ) -> str | None:
        if access_token and access_token.strip():
            return access_token.strip()
        if encrypted_da_cookie and encrypted_da_cookie.strip():
            decrypted = decrypt_cookie_value(encrypted_da_cookie.strip())
            if isinstance(decrypted, str) and decrypted.strip():
                return decrypted.strip()
        return load_token_from_vault()

    def _build_headers(
        self,
        extra_headers: dict[str, str] | None = None,
        include_json_content_type: bool = True,
        studio_app: str | None = None,
    ) -> dict[str, str]:
        headers = build_ui_security_headers()
        headers["Accept"] = "application/json"
        if include_json_content_type:
            headers["Content-Type"] = "application/json"
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.guest_token:
            headers["x-studio-guest-token"] = self.guest_token
        if studio_app:
            headers["X-Studio-Client-App"] = studio_app
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _remember_storage_hosts(self, response: httpx.Response) -> None:
        """Capture `x-storage-proxy-host` and `x-storage-direct-host` (Studio Module 463)."""
        proxy = response.headers.get("x-storage-proxy-host", "").strip().lower()
        direct = response.headers.get("x-storage-direct-host", "").strip().lower()
        if proxy and "/" not in proxy:
            self.storage_proxy_host = proxy
        if direct and "/" not in direct:
            self.storage_direct_host = direct

    @staticmethod
    def _ensure_trailing_slash(path: str) -> str:
        if not path or path.endswith("/") or "?" in path:
            return path
        return f"{path}/"

    @staticmethod
    def _is_public_free_url(url: str) -> bool:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = parsed.path or "/"
        # Note: /api/v1/stem-export on converta is a Studio paid/authenticated stem-loop export,
        # whereas /api/v1/formats, /api/v1/convert, /api/v1/status/ are free Converta endpoints.
        return any(path.startswith(prefix) for prefix in _PUBLIC_FREE_PATH_PREFIXES)

    def require_authenticated(self, endpoint_url: str = "") -> str:
        """Enforce SoundBoost's authentication & subscription pay gate for paid Mastering, Studio, and Shoots endpoints."""
        if self.access_token and self.access_token.strip():
            return self.access_token.strip()
        raise SoundBoostPayGateError(
            f"SoundBoost authentication required for paid/studio endpoint '{endpoint_url or 'api.soundboost.ai'}'. "
            "Sign in first via `soundboost auth browser-login`, `soundboost auth google-exchange --token <token>`, "
            "or `soundboost_oauth_and_vault(action='browser_handoff')` so requests authenticate against your SoundBoost account and subscription."
        )

    @staticmethod
    def resolve_account_entitlements(
        billing_or_user_payload: dict[str, Any] | None = None,
        *,
        is_authenticated: bool = False,
        override_tier: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a user's exact SoundBoost account tier and unlocked feature flags from `/api/billing/info` or `/api/account/profile/`.

        Mirrors SoundBoost's exact client-side entitlement functions (`N(e)`, `S(e)`, `P(e)` in `_app` and Studio `Module 584`):
        - `free`: Free tools (`audit`, `tags`, `converta`), Studio standard 2/4-stem + chords/lyrics, Smart Links, 30s mastering preview.
        - `pay_as_you_go`: Credit-backed full-length mastering downloads, Studio stem exports, 480P MV.
        - `unlimited`: Unlimited full-length mastering & revisions, Studio pitch/time stem export, Shoots AI Clones.
        - `unlimited_plus`: Unlocks **Bulk Mastering** (`/api/bulk-mastering/*`), **Studio Hi-Fi 4-Stem (`hifi_4stem`), 6-Stem (`hifi_6stem`), and 5-Piece Drum Kit Splitter** (`requires_plus=True`), Auto-QC, and Deep Research.
        - `starter` / `creator` / `director` / `studio`: Shoots & AI Music Video (`h3-max`) tiers (`director` & `studio` unlock Auto-QC + Bulk Mastering; `studio` unlocks Deep Research + 3 character refs).
        """
        data = billing_or_user_payload or {}
        raw_plan = str(
            override_tier
            or data.get("plan")
            or data.get("subscription_plan")
            or data.get("tier")
            or data.get("user_type")
            or ""
        ).strip().lower()
        sub_status = str(data.get("subscription_status") or data.get("status") or "").strip().lower()
        is_frozen = bool(data.get("frozen", False))
        credits = int(data.get("credits") or data.get("credit_balance") or data.get("video_credits") or 0)
        is_unlimited_flag = bool(data.get("unlimited") or data.get("is_unlimited"))
        is_plus_flag = bool(
            data.get("unlimited_plus")
            or data.get("is_plus")
            or data.get("has_plus_access")
            or "plus" in raw_plan
        )

        # Normalize plan identifier (e.g. "unlimited_plus_monthly" -> "unlimited_plus", "director_annual" -> "director")
        normalized_tier = "free"
        if raw_plan in ACCOUNT_TIER_ENTITLEMENTS and raw_plan != "free":
            normalized_tier = raw_plan
        elif "unlimited_plus" in raw_plan or is_plus_flag:
            normalized_tier = "unlimited_plus"
        elif "unlimited" in raw_plan or is_unlimited_flag:
            normalized_tier = "unlimited"
        elif "studio" in raw_plan or "tier_4" in raw_plan or "tier_3" in raw_plan:
            normalized_tier = "studio"
        elif "director" in raw_plan or "tier_2" in raw_plan:
            normalized_tier = "director"
        elif "creator" in raw_plan:
            normalized_tier = "creator"
        elif "starter" in raw_plan or "tier_1" in raw_plan:
            normalized_tier = "starter"
        elif is_authenticated and credits > 0:
            normalized_tier = "pay_as_you_go"

        # If subscription is explicitly frozen or cancelled/expired without credits, downgrade to free
        if is_frozen or (sub_status in ("canceled", "cancelled", "unpaid", "incomplete_expired") and credits <= 0):
            normalized_tier = "free"

        base_spec = dict(ACCOUNT_TIER_ENTITLEMENTS.get(normalized_tier, ACCOUNT_TIER_ENTITLEMENTS["free"]))

        # Apply explicit `plan_tools` overrides if returned by `/api/billing/info` (Function P(e))
        plan_tools = data.get("plan_tools")
        if isinstance(plan_tools, dict):
            if "qc" in plan_tools:
                base_spec["mv_auto_qc"] = bool(plan_tools["qc"])
            if "research" in plan_tools:
                base_spec["mv_deep_research"] = bool(plan_tools["research"])

        # Build list of unlocked Studio stem profiles for this exact tier
        plus_unlocked = bool(base_spec.get("studio_hifi_plus_stems_unlocked"))
        unlocked_stem_profiles = [
            k for k, v in STEM_PROFILES.items() if (not v.requires_plus) or plus_unlocked
        ]

        return {
            **base_spec,
            "resolved_tier": normalized_tier,
            "raw_plan": raw_plan or normalized_tier,
            "subscription_status": sub_status or ("active" if normalized_tier != "free" else "free"),
            "frozen": is_frozen,
            "credits": credits,
            "unlocked_studio_stem_profiles": unlocked_stem_profiles,
            "drum_kit_5piece_unlocked": plus_unlocked,
            "studio_5piece_drum_kit_unlocked": plus_unlocked,
            "mv_auto_qc_unlocked": bool(base_spec.get("mv_auto_qc")),
            "mv_deep_research_unlocked": bool(base_spec.get("mv_deep_research")),
            "entitlements": {
                **base_spec,
                "studio_5piece_drum_kit_unlocked": plus_unlocked,
                "mv_auto_qc_unlocked": bool(base_spec.get("mv_auto_qc")),
                "mv_deep_research_unlocked": bool(base_spec.get("mv_deep_research")),
            },
        }

    def get_my_entitlements(self, force_refresh: bool = False) -> dict[str, Any]:
        """Fetch and resolve the authenticated user's live SoundBoost account tier and unlocked feature matrix."""
        if not force_refresh and self.cached_entitlements is not None:
            return self.cached_entitlements

        if not (self.access_token and self.access_token.strip()):
            resolved = self.resolve_account_entitlements(
                is_authenticated=False,
                override_tier=self.account_tier,
            )
            self.cached_entitlements = resolved
            return resolved

        billing_data: dict[str, Any] = {}
        if not self.account_tier:
            try:
                billing_data = self.get_billing_info()
            except httpx.HTTPError:
                billing_data = {}

        resolved = self.resolve_account_entitlements(
            billing_data,
            is_authenticated=True,
            override_tier=self.account_tier,
        )
        self.cached_entitlements = resolved
        return resolved

    def require_feature_entitlement(
        self,
        feature_key: str,
        feature_label: str = "",
        *,
        endpoint: str = "",
    ) -> dict[str, Any]:
        """Verify that the user's current account level unlocks `feature_key` before calling a tier-gated endpoint."""
        label = feature_label or endpoint or feature_key
        self.require_authenticated(label)
        ent = self.get_my_entitlements()
        # If no local tier override and billing endpoint wasn't reachable yet, let the backend validate the Bearer JWT directly
        if ent.get("raw_plan") == "free" and not self.account_tier and ent.get("subscription_status") == "free":
            return ent
        if not ent.get(feature_key, False):
            current_tier = ent.get("tier", "free")
            raise SoundBoostPayGateError(
                f"Account tier '{current_tier}' does not unlock '{label}' (`{feature_key}=False`). "
                "Upgrade your SoundBoost plan (`unlimited_plus`, `creator`, `director`, or `studio`) to unlock this feature."
            )
        return ent

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        files: Any | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        requires_auth: bool | None = None,
    ) -> Any:
        must_auth = (not self._is_public_free_url(url)) if requires_auth is None else requires_auth
        if must_auth:
            self.require_authenticated(url)
        include_json_ct = files is None and data is None
        headers = self._build_headers(
            extra_headers=extra_headers,
            include_json_content_type=include_json_ct,
        )
        with httpx.Client(timeout=timeout_sec or self.timeout_sec, follow_redirects=True) as client:
            resp = client.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                files=files,
                data=data,
                headers=headers,
            )
            self._remember_storage_hosts(resp)
            resp.raise_for_status()
            if not resp.content:
                return {}
            content_type = resp.headers.get("content-type", "")
            if "application/json" in content_type:
                return resp.json()
            try:
                return resp.json()
            except ValueError:
                return {"raw_text": resp.text}

    # -----------------------------------------------------------------------
    # 1. Authentication, Account, Products & SSO
    # -----------------------------------------------------------------------
    def login(self, email: str, password: str, save_credentials: bool = True) -> dict[str, Any]:
        """Authenticate against `POST /api/login/` and optionally cache token in `~/.soundboost/credentials.json`."""
        url = f"{self.backend_url}/api/login/"
        result: dict[str, Any] = self._request("POST", url, json_body={"email": email, "password": password})
        token = (
            result.get("access")
            or result.get("access_token")
            or result.get("token")
            or (result.get("data") or {}).get("access_token")
        )
        if isinstance(token, str) and token.strip():
            self.access_token = token.strip()
            if save_credentials:
                save_credentials_to_vault(
                    self.access_token,
                    email=email,
                    user_id=result.get("id") or (result.get("user") or {}).get("id"),
                    auth_method="password_login",
                )
        return result

    def google_oauth_login(
        self,
        google_token: str,
        save_credentials: bool = True,
    ) -> dict[str, Any]:
        """Exchange a Google OAuth 2.0 `id_token` / `access_token` / `credential` for a SoundBoost Bearer JWT (`POST /api/google-login/`)."""
        url = f"{self.backend_url}/api/google-login/"
        cleaned = google_token.strip()
        payload = {
            "token": cleaned,
            "access_token": cleaned,
            "id_token": cleaned,
            "credential": cleaned,
        }
        result: dict[str, Any] = self._request("POST", url, json_body=payload)
        token = (
            result.get("access")
            or result.get("access_token")
            or result.get("token")
            or (result.get("data") or {}).get("access_token")
        )
        if isinstance(token, str) and token.strip():
            self.access_token = token.strip()
            if save_credentials:
                save_credentials_to_vault(
                    self.access_token,
                    email=result.get("email") or (result.get("user") or {}).get("email"),
                    user_id=result.get("id") or (result.get("user") or {}).get("id"),
                    auth_method="google_oauth",
                )
        return result

    def get_musician_profile(self, user_id: str | int) -> dict[str, Any]:
        """Fetch musician profile and subscription entitlements (`GET /api/musicians/{id}/`)."""
        url = f"{self.backend_url}/api/musicians/{user_id}/"
        return dict(self._request("GET", url))

    def get_products(self) -> list[dict[str, Any]]:
        """List SoundBoost subscription plans and credit packs (`GET /api/products/`)."""
        url = f"{self.backend_url}/api/products/"
        data = self._request("GET", url)
        return list(data.get("products", [])) if isinstance(data, dict) else []

    def get_verified_users_count(self) -> dict[str, Any]:
        """Fetch platform verified user count (`GET /api/health/verified-users/`)."""
        url = f"{self.backend_url}/api/health/verified-users/"
        return dict(self._request("GET", url))

    def get_shoots_sso_token(self) -> dict[str, Any]:
        """Exchange Bearer token for a `shoots.soundboost.ai` SSO session (`GET /api/sso/?service=shoots`)."""
        url = f"{self.backend_url}/api/sso/"
        return dict(self._request("GET", url, params={"service": "shoots"}))

    # -----------------------------------------------------------------------
    # 2. Audio Upload, AI Mastering (8 Modes), Mix Doctor & Audio Enhancer
    # -----------------------------------------------------------------------
    def upload_audio(self, file_path: str | Path) -> dict[str, Any]:
        """Upload an audio file to `POST https://api.soundboost.ai/api/upload-audio/`."""
        resolved = Path(file_path).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Audio file not found: {resolved}")
        url = f"{self.backend_url}/api/upload-audio/"
        with resolved.open("rb") as fh:
            files = {"file": (resolved.name, fh, "application/octet-stream")}
            return dict(self._request("POST", url, files=files, timeout_sec=300.0))

    def start_mastering(
        self,
        track_id: int | str,
        *,
        mode: str = "auto",
        prompt: str | None = None,
        preset: str | None = None,
        reference_track_id: int | str | None = None,
        eq_18band_db: list[float] | None = None,
        stereo_width_pct: float | None = None,
        target_lufs: float | None = None,
        revision_notes: str | None = None,
    ) -> dict[str, Any]:
        """Trigger AI Mastering across any of the 8 Mastering modes (`prompt`, `preset`, `auto`, `reference`, `chat`, `manual`, `mix_doctor`, `enhance`)."""
        payload: dict[str, Any] = {"mode": mode}
        if prompt is not None:
            payload["prompt"] = prompt
        if preset is not None:
            payload["preset"] = preset
        if reference_track_id is not None:
            payload["reference_track_id"] = reference_track_id
        if eq_18band_db is not None:
            if len(eq_18band_db) != 18:
                raise ValueError("eq_18band_db must contain exactly 18 band values (20Hz..15kHz)")
            payload["eq_bands"] = dict(
                zip([str(f) for f in MASTERING_EQ_FREQUENCIES_HZ], eq_18band_db, strict=True)
            )
        if stereo_width_pct is not None:
            payload["stereo_width"] = stereo_width_pct
        if target_lufs is not None:
            payload["target_lufs"] = target_lufs
        if revision_notes is not None:
            payload["revision_notes"] = revision_notes

        url = f"{self.backend_url}/api/tracks/{track_id}/master/"
        return dict(self._request("POST", url, json_body=payload, timeout_sec=180.0))

    def get_track_status(self, track_id: int | str) -> dict[str, Any]:
        """Fetch track mastering status, loudness metrics, and download URLs (`GET /api/tracks/{id}/`)."""
        url = f"{self.backend_url}/api/tracks/{track_id}/"
        return dict(self._request("GET", url))

    def run_mix_doctor(self, track_id: int | str, genre: str = "pop") -> dict[str, Any]:
        """Run SoundBoost Mix Doctor diagnostic analysis on an uploaded track."""
        url = f"{self.backend_url}/api/tracks/{track_id}/mix-doctor/"
        return dict(self._request("POST", url, json_body={"genre": genre}))

    def run_audio_enhancer(self, track_id: int | str, intensity: float = 0.75) -> dict[str, Any]:
        """Run SoundBoost Audio Enhancer bandwidth/clarity restoration on a track."""
        url = f"{self.backend_url}/api/tracks/{track_id}/enhance/"
        return dict(self._request("POST", url, json_body={"intensity": intensity}))

    # -----------------------------------------------------------------------
    # 3. Studio (`studio.soundboost.ai`) — Stems, 5-Piece Drum Kit, Voice Clean, Chords, Lyrics
    # -----------------------------------------------------------------------
    @staticmethod
    def _studio_chunk_headers(chunk_profile: str = "aware") -> dict[str, str]:
        return {
            "X-Stem-Chunk-Playback-Metadata": "1",
            "X-Stem-Chunk-Profile": chunk_profile,
        }

    def claim_studio_original(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Claim an uploaded Studio original track (`POST /api/studio/originals/claim/`)."""
        url = f"{self.backend_url}/api/studio/originals/claim/"
        return dict(self._request("POST", url, json_body=payload))

    def get_studio_original_status(
        self,
        original_id: int | str,
        stem_variant: str | None = None,
        stem_format: str | None = None,
    ) -> dict[str, Any]:
        """Fetch Studio original status (`GET /api/studio/originals/{id}/status/`, Module 618)."""
        params: dict[str, Any] = {}
        if stem_variant:
            params["stem_variant"] = stem_variant
        if stem_format:
            params["stem_format"] = stem_format
        url = f"{self.backend_url}/api/studio/originals/{original_id}/status/"
        return dict(
            self._request("GET", url, params=params, extra_headers=self._studio_chunk_headers())
        )

    def start_studio_stem_split(
        self,
        original_id: int | str,
        profile_key: str = "standard_4stem",
        chunk_profile: str = "v2",
    ) -> dict[str, Any]:
        """Start Studio stem separation (`standard_2stem`, `standard_4stem`, `hifi_4stem`, `hifi_6stem`, Module 618)."""
        if profile_key not in STEM_PROFILES:
            raise ValueError(f"Unknown profile '{profile_key}'. Valid: {sorted(STEM_PROFILES)}")
        spec = STEM_PROFILES[profile_key]
        if spec.requires_plus:
            self.require_feature_entitlement(
                "studio_hifi_plus_stems_unlocked",
                f"Studio Hi-Fi Stem Separation ({profile_key})",
            )
        body = {
            "chunk_profile": chunk_profile,
            "stem_variant": spec.stem_variant,
            "stem_format": spec.stem_format,
            "separation_model": spec.separation_model,
        }
        url = f"{self.backend_url}/api/studio/originals/{original_id}/start/"
        return dict(self._request("POST", url, json_body=body))

    def get_studio_stems(
        self,
        original_id: int | str,
        stem_variant: str | None = None,
        stem_format: str | None = None,
        chunk_profile: str = "v2",
    ) -> dict[str, Any]:
        """Fetch separated Studio stem URLs (`GET /api/studio/originals/{id}/stems/`, Module 618)."""
        params: dict[str, Any] = {}
        if stem_variant:
            params["stem_variant"] = stem_variant
        if stem_format:
            params["stem_format"] = stem_format
        if chunk_profile == "aware":
            params["chunk_urls"] = "omit"
        url = f"{self.backend_url}/api/studio/originals/{original_id}/stems/"
        return dict(
            self._request(
                "GET",
                url,
                params=params,
                extra_headers=self._studio_chunk_headers(chunk_profile),
            )
        )

    def start_studio_voice_clean(
        self,
        original_id: int | str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start Studio Voice Cleaner (`Voice` + `Artifacts` separation, `POST /api/studio/originals/{id}/voice-clean/start/`)."""
        url = f"{self.backend_url}/api/studio/originals/{original_id}/voice-clean/start/"
        return dict(self._request("POST", url, json_body=options or {}))

    def start_studio_drum_split(
        self,
        original_id: int | str,
        chunk_profile: str = "v2",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start 5-Piece Drum Kit sub-stem separation (`Kick`, `Snare`, `Toms`, `Hi-Hat`, `Cymbals`, Module 618)."""
        self.require_feature_entitlement(
            "studio_hifi_plus_stems_unlocked",
            "Studio 5-Piece Drum Kit Sub-Stem Splitter",
        )
        body = {"chunk_profile": chunk_profile, **(options or {})}
        url = f"{self.backend_url}/api/studio/originals/{original_id}/drums/start/"
        return dict(self._request("POST", url, json_body=body))

    def get_studio_drum_stems(
        self,
        original_id: int | str,
        chunk_profile: str = "v2",
    ) -> dict[str, Any]:
        """Fetch the 5-Piece Drum Kit sub-stem URLs (`GET /api/studio/originals/{id}/drums/`, Module 618)."""
        url = f"{self.backend_url}/api/studio/originals/{original_id}/drums/"
        return dict(
            self._request("GET", url, extra_headers=self._studio_chunk_headers(chunk_profile))
        )

    def start_studio_chord_analysis(
        self,
        original_id: int | str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start AI Chord Recognition & Beat Alignment (`POST /api/studio/originals/{id}/chords/start/`, Module 618)."""
        url = f"{self.backend_url}/api/studio/originals/{original_id}/chords/start/"
        return dict(self._request("POST", url, json_body=options or {}))

    def get_studio_chord_analysis(self, original_id: int | str) -> dict[str, Any]:
        """Fetch beat-aligned chords and synchronized beat grid (`GET /api/studio/originals/{id}/chords/`)."""
        url = f"{self.backend_url}/api/studio/originals/{original_id}/chords/"
        return dict(self._request("GET", url))

    def start_studio_lyrics(
        self,
        original_id: int | str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start word-level AI Lyric / Voice-Clean Transcript recognition (`POST /api/studio/originals/{id}/lyrics/start/`)."""
        url = f"{self.backend_url}/api/studio/originals/{original_id}/lyrics/start/"
        return dict(self._request("POST", url, json_body=options or {}))

    def get_studio_lyrics(self, original_id: int | str) -> dict[str, Any]:
        """Fetch word-synced lyric/transcript segments (`GET /api/studio/originals/{id}/lyrics/`)."""
        url = f"{self.backend_url}/api/studio/originals/{original_id}/lyrics/"
        return dict(self._request("GET", url))

    def save_studio_lyrics_edits(
        self,
        original_id: int | str,
        segments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Save edited lyric segments (`POST /api/studio/originals/{id}/lyrics/edit/`, Module 618)."""
        url = f"{self.backend_url}/api/studio/originals/{original_id}/lyrics/edit/"
        return dict(self._request("POST", url, json_body={"segments": segments}))

    def export_studio_stems(
        self,
        original_id: int | str,
        export_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Export stems or custom mix (`POST /api/studio/originals/{id}/export/`, Module 618)."""
        self.require_feature_entitlement(
            "studio_pitch_time_export_unlocked",
            endpoint="/api/studio/originals/{id}/export/",
        )
        url = f"{self.backend_url}/api/studio/originals/{original_id}/export/"
        return dict(self._request("POST", url, json_body=export_payload))

    def handoff_studio_to_mastering(
        self,
        original_id: int | str,
        source_type: str = "mix",
        stem_name: str | None = None,
        loop_start_sec: float | None = None,
        loop_end_sec: float | None = None,
    ) -> dict[str, Any]:
        """Send active `loop`, `stems`, or full `mix` from Studio directly into Mastering (`POST /api/tracks/import-stem-splitter/`, Module 621)."""
        payload: dict[str, Any] = {
            "original_id": original_id,
            "source": source_type,
        }
        if stem_name:
            payload["stem"] = stem_name
        if loop_start_sec is not None and loop_end_sec is not None:
            payload["loop_start"] = loop_start_sec
            payload["loop_end"] = loop_end_sec
        url = f"{self.backend_url}/api/tracks/import-stem-splitter/"
        return dict(self._request("POST", url, json_body=payload))

    def build_studio_url(self, track_id: int | str, product: str | None = None) -> str:
        """Build a signed `https://studio.soundboost.ai/?track=<hash>` deep link that bypasses Cloudflare's `/` redirect."""
        enc_track = encrypt_param_id(track_id)
        base = f"{self.studio_url}/?track={enc_track}"
        if product:
            base += f"&product={product}"
        return base

    def decrypt_studio_track_param(self, track_hash: str) -> int | str:
        """Decrypt `?track=` locally via AES-128-CBC or fallback to `POST /api/decrypt-param-static` (Module 615)."""
        try:
            return decrypt_param_id(track_hash)
        except (ValueError, KeyError, TypeError):
            url = f"{self.mastering_url}/api/decrypt-param-static"
            res = self._request("POST", url, json_body={"hash": track_hash})
            return res.get("decryptedParam", track_hash)

    # -----------------------------------------------------------------------
    # 4. Converta Hi-Res Audio Converter (`https://converta.soundboost.ai`)
    # -----------------------------------------------------------------------
    def get_converta_formats(self) -> dict[str, Any]:
        """Fetch live supported formats, bit depths, sample rates & dithering flags from `GET /api/v1/formats`."""
        url = f"{self.converta_url}/api/v1/formats"
        return dict(self._request("GET", url))

    def start_converta_conversion(
        self,
        upload_id_or_url: str,
        output_format: str = "wav",
        bit_depth: int = 24,
        sample_rate: int = 48000,
        dithering: bool = True,
    ) -> dict[str, Any]:
        """Start hi-res format conversion via `POST https://converta.soundboost.ai/api/v1/convert`."""
        fmt = output_format.strip().lower()
        if fmt not in CONVERTA_FORMATS:
            raise ValueError(f"Unsupported format '{output_format}'. Valid: {sorted(CONVERTA_FORMATS)}")
        spec = CONVERTA_FORMATS[fmt]
        if bit_depth not in spec.bit_depths:
            raise ValueError(f"Format '{fmt}' supports bit depths {spec.bit_depths}, got {bit_depth}")
        if sample_rate not in spec.sample_rates:
            raise ValueError(f"Format '{fmt}' supports sample rates {spec.sample_rates}, got {sample_rate}")

        params: dict[str, Any] = {
            "format": fmt,
            "bit_depth": bit_depth,
            "sample_rate": sample_rate,
            "dithering": str(bool(dithering and spec.supports_dithering)).lower(),
        }
        url = f"{self.converta_url}/api/v1/convert"
        return dict(
            self._request(
                "POST",
                url,
                params=params,
                json_body={"source": upload_id_or_url},
            )
        )

    def request_converta_stem_export(self, export_spec: dict[str, Any]) -> dict[str, Any]:
        """Request pitched/time-stretched stem or loop export via `POST https://converta.soundboost.ai/api/v1/stem-export` (Module 620)."""
        self.require_feature_entitlement(
            "studio_pitch_time_export_unlocked",
            endpoint="/api/v1/stem-export",
        )
        url = f"{self.converta_url}/api/v1/stem-export"
        return dict(self._request("POST", url, json_body=export_spec))

    def get_converta_status(self, job_id: str) -> dict[str, Any]:
        """Poll Converta job status (`GET https://converta.soundboost.ai/api/v1/status/{id}`)."""
        url = f"{self.converta_url}/api/v1/status/{job_id}"
        return dict(self._request("GET", url, params={"_": int(time.time() * 1000)}))

    # -----------------------------------------------------------------------
    # 5. Smart Links / Link-in-Bio (`https://soundboo.st/<slug>`)
    # -----------------------------------------------------------------------
    def list_smart_links(self) -> Any:
        """List all artist Smart Links (`GET /api/smart-links/`)."""
        url = f"{self.backend_url}/api/smart-links/"
        return self._request("GET", url)

    def resolve_smart_link_source_metadata(self, source_url: str) -> dict[str, Any]:
        """Auto-scrape DSP links and cover artwork from a Spotify/Apple/streaming URL (`POST /api/smart-links/resolve-source-metadata/`)."""
        url = f"{self.backend_url}/api/smart-links/resolve-source-metadata/"
        return dict(self._request("POST", url, json_body={"source_url": source_url}))

    def create_smart_link(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a new Smart Link page (`POST /api/smart-links/`)."""
        url = f"{self.backend_url}/api/smart-links/"
        return dict(self._request("POST", url, json_body=payload))

    def update_smart_link(self, link_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
        """Update an existing Smart Link (`PATCH /api/smart-links/{id}/`)."""
        url = f"{self.backend_url}/api/smart-links/{link_id}/"
        return dict(self._request("PATCH", url, json_body=payload))

    def publish_smart_link(self, link_id: str | int) -> dict[str, Any]:
        """Publish a Smart Link to `https://soundboo.st/<publicId>` (`POST /api/smart-links/{id}/publish/`)."""
        url = f"{self.backend_url}/api/smart-links/{link_id}/publish/"
        return dict(self._request("POST", url, json_body={}))

    def archive_smart_link(self, link_id: str | int) -> dict[str, Any]:
        """Archive a Smart Link (`POST /api/smart-links/{id}/archive/`)."""
        url = f"{self.backend_url}/api/smart-links/{link_id}/archive/"
        return dict(self._request("POST", url, json_body={}))

    def delete_smart_link(self, link_id: str | int) -> dict[str, Any]:
        """Delete a Smart Link (`POST /api/smart-links/{id}/delete/`)."""
        url = f"{self.backend_url}/api/smart-links/{link_id}/delete/"
        return dict(self._request("POST", url, json_body={}))

    # -----------------------------------------------------------------------
    # 6. Text-to-Audio Generator (`soundboost.ai/text-to-audio`)
    # -----------------------------------------------------------------------
    def generate_text_to_audio(self, prompt: str, duration_sec: int = 10) -> dict[str, Any]:
        """Start AI Text-to-Audio / SFX generation (`POST /api/audio/generate/`)."""
        url = f"{self.backend_url}/api/audio/generate/"
        return dict(
            self._request("POST", url, json_body={"prompt": prompt, "duration": int(duration_sec)})
        )

    def get_text_to_audio_status(self, generation_id: str | int) -> dict[str, Any]:
        """Poll Text-to-Audio generation status (`GET /api/audio/generations/{id}/status/`)."""
        url = f"{self.backend_url}/api/audio/generations/{generation_id}/status/"
        return dict(self._request("GET", url))

    def list_public_text_to_audio(self, page: int = 1) -> dict[str, Any]:
        """List community Text-to-Audio generations (`GET /api/audio/public/list/?page={page}`)."""
        url = f"{self.backend_url}/api/audio/public/list/"
        return dict(self._request("GET", url, params={"page": page}))

    # -----------------------------------------------------------------------
    # 7. Bulk / Album Mastering Engine (`/bulk-mastering`)
    # -----------------------------------------------------------------------
    def start_bulk_mastering(
        self,
        track_ids: list[int | str],
        *,
        mode: str = "auto",
        preset: str | None = None,
        prompt: str | None = None,
        target_lufs: float | None = None,
        reference_track_id: int | str | None = None,
    ) -> dict[str, Any]:
        """Start batch album/EP mastering (`POST /api/bulk-mastering/start/`, requires Unlimited Plus / Director / Studio tier)."""
        self.require_feature_entitlement(
            "bulk_mastering_unlocked",
            "Bulk / Album Mastering (/api/bulk-mastering/start/)",
        )
        payload: dict[str, Any] = {
            "track_ids": [str(tid) for tid in track_ids],
            "mode": mode,
        }
        if preset is not None:
            payload["preset"] = preset
        if prompt is not None:
            payload["prompt"] = prompt
        if target_lufs is not None:
            payload["target_lufs"] = target_lufs
        if reference_track_id is not None:
            payload["reference_track_id"] = str(reference_track_id)
        url = f"{self.backend_url}/api/bulk-mastering/start/"
        return dict(self._request("POST", url, json_body=payload, timeout_sec=180.0))

    def list_bulk_mastering_batches(self) -> Any:
        """List all bulk mastering batches (`GET /api/bulk-mastering/batches/`)."""
        url = f"{self.backend_url}/api/bulk-mastering/batches/"
        return self._request("GET", url)

    def get_bulk_mastering_status(self, batch_id: int | str) -> dict[str, Any]:
        """Poll bulk mastering batch status (`GET /api/bulk-mastering/batches/{id}/status/`)."""
        url = f"{self.backend_url}/api/bulk-mastering/batches/{batch_id}/status/"
        return dict(self._request("GET", url))

    def get_bulk_mastering_export_zip(self, batch_id: int | str) -> dict[str, Any]:
        """Fetch or download the completed bulk mastering ZIP archive (`GET /api/bulk-mastering/batches/{id}/export-zip/`)."""
        url = f"{self.backend_url}/api/bulk-mastering/batches/{batch_id}/export-zip/"
        return dict(self._request("GET", url))

    # -----------------------------------------------------------------------
    # 8. Direct YouTube & SoundCloud Ingestion (Mastering & Studio)
    # -----------------------------------------------------------------------
    def import_youtube_for_mastering(self, youtube_url: str) -> dict[str, Any]:
        """Import a YouTube URL directly into Mastering (`POST /api/tracks/import-youtube/`)."""
        url = f"{self.backend_url}/api/tracks/import-youtube/"
        return dict(self._request("POST", url, json_body={"url": youtube_url.strip()}))

    def get_youtube_direct_url(self, youtube_url: str) -> dict[str, Any]:
        """Resolve a direct audio stream URL from YouTube (`GET /api/tracks/youtube-direct-url/`)."""
        url = f"{self.backend_url}/api/tracks/youtube-direct-url/"
        return dict(self._request("GET", url, params={"url": youtube_url.strip()}))

    def import_soundcloud_for_mastering(self, soundcloud_url: str) -> dict[str, Any]:
        """Import a SoundCloud URL directly into Mastering (`POST /api/tracks/import-soundcloud/`)."""
        url = f"{self.backend_url}/api/tracks/import-soundcloud/"
        return dict(self._request("POST", url, json_body={"url": soundcloud_url.strip()}))

    def get_soundcloud_import_status(self, job_id: str | int) -> dict[str, Any]:
        """Poll SoundCloud import job status (`GET /api/tracks/import-soundcloud/status/`)."""
        url = f"{self.backend_url}/api/tracks/import-soundcloud/status/"
        return dict(self._request("GET", url, params={"id": job_id, "job_id": job_id}))

    def get_soundcloud_direct_url(self, soundcloud_url: str) -> dict[str, Any]:
        """Resolve a direct audio stream URL from SoundCloud (`GET /api/tracks/soundcloud-direct-url/`)."""
        url = f"{self.backend_url}/api/tracks/soundcloud-direct-url/"
        return dict(self._request("GET", url, params={"url": soundcloud_url.strip()}))

    def import_youtube_to_studio(self, youtube_url: str) -> dict[str, Any]:
        """Import a YouTube URL directly into Studio Stem Splitter (`POST /api/studio/public/import-youtube`)."""
        url = f"{self.backend_url}/api/studio/public/import-youtube"
        return dict(self._request("POST", url, json_body={"url": youtube_url.strip()}))

    def import_soundcloud_to_studio(self, soundcloud_url: str) -> dict[str, Any]:
        """Import a SoundCloud URL directly into Studio Stem Splitter (`POST /api/studio/public/import-soundcloud`)."""
        url = f"{self.backend_url}/api/studio/public/import-soundcloud"
        return dict(self._request("POST", url, json_body={"url": soundcloud_url.strip()}))

    def upload_studio_original(self, file_path: str | Path) -> dict[str, Any]:
        """Upload a local audio file directly to Studio Originals (`POST /api/studio/originals/upload/`)."""
        resolved = Path(file_path).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Audio file not found: {resolved}")
        url = f"{self.backend_url}/api/studio/originals/upload/"
        with resolved.open("rb") as fh:
            files = {"file": (resolved.name, fh, "application/octet-stream")}
            return dict(self._request("POST", url, files=files, timeout_sec=300.0))

    def list_ready_studio_originals(self) -> Any:
        """List all ready Studio original tracks (`GET /api/studio/originals/ready-all`)."""
        url = f"{self.backend_url}/api/studio/originals/ready-all"
        return self._request("GET", url)

    # -----------------------------------------------------------------------
    # 9. Voice-Controlled AI Mastering (`Deepgram`) & `Toki` Prompt Engine
    # -----------------------------------------------------------------------
    def translate_ai_mastering_prompt(
        self,
        prompt: str,
        track_id: int | str | None = None,
    ) -> dict[str, Any]:
        """Translate a natural-language mastering prompt via SoundBoost AI (`POST /api/ai/prompt`)."""
        body: dict[str, Any] = {"prompt": prompt}
        if track_id is not None:
            body["track_id"] = track_id
        url = f"{self.mastering_url}/api/ai/prompt"
        return dict(self._request("POST", url, json_body=body))

    def check_toki_assistant(self) -> dict[str, Any]:
        """Check status/availability of the Toki AI Mastering assistant (`GET /api/toki/check/`)."""
        url = f"{self.backend_url}/api/toki/check/"
        return dict(self._request("GET", url))

    def get_mastering_voice_deepgram_token(self) -> dict[str, Any]:
        """Acquire a temporary Deepgram STT token for Voice-Controlled AI Mastering (`POST /api/mastering-voice/deepgram-token`)."""
        url = f"{self.mastering_url}/api/mastering-voice/deepgram-token"
        return dict(self._request("POST", url, json_body={}))

    def send_mastering_voice_speech(
        self,
        transcript: str,
        track_id: int | str | None = None,
    ) -> dict[str, Any]:
        """Submit spoken mastering revision instructions (`POST /api/mastering-voice/speech`)."""
        payload: dict[str, Any] = {"text": transcript, "transcript": transcript}
        if track_id is not None:
            payload["track_id"] = track_id
        url = f"{self.mastering_url}/api/mastering-voice/speech"
        return dict(self._request("POST", url, json_body=payload))

    def get_music_video_voice_deepgram_token(self) -> dict[str, Any]:
        """Acquire a Deepgram STT token for Voice-Controlled Music Video Director (`POST /api/music-video-voice/deepgram-token`)."""
        url = f"{self.mastering_url}/api/music-video-voice/deepgram-token"
        return dict(self._request("POST", url, json_body={}))

    # -----------------------------------------------------------------------
    # 10. Full AI Music Video (`h3-max`) & `Shoots` Director API
    # -----------------------------------------------------------------------
    def list_music_videos(self) -> Any:
        """List AI Music Video (`h3-max`) projects (`GET /api/music-videos/`)."""
        url = f"{self.backend_url}/api/music-videos/"
        return self._request("GET", url)

    def create_music_video(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a new AI Music Video (`h3-max`) project (`POST /api/music-videos/`)."""
        url = f"{self.backend_url}/api/music-videos/"
        return dict(self._request("POST", url, json_body=payload))

    def get_music_video(self, video_id: int | str) -> dict[str, Any]:
        """Fetch an AI Music Video project by ID (`GET /api/music-videos/{id}/`)."""
        url = f"{self.backend_url}/api/music-videos/{video_id}/"
        return dict(self._request("GET", url))

    def list_shoots(self) -> Any:
        """List Shoots / Music Video storyboard sessions (`GET /api/shoots/`)."""
        url = f"{self.backend_url}/api/shoots/"
        return self._request("GET", url)

    def create_shoot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a new Shoot / storyboard session (`POST /api/shoots/`)."""
        url = f"{self.backend_url}/api/shoots/"
        return dict(self._request("POST", url, json_body=payload))

    def get_shoot(self, shoot_id: int | str) -> dict[str, Any]:
        """Fetch a Shoot / storyboard session by ID (`GET /api/shoots/{id}`)."""
        url = f"{self.mastering_url}/api/shoots/{shoot_id}"
        return dict(self._request("GET", url))

    def send_shoot_director_message(
        self,
        shoot_id: int | str,
        message: str,
        scene_id: int | str | None = None,
    ) -> dict[str, Any]:
        """Send natural-language Director Chat instructions to modify storyboard shots, camera motion, or visual style (`POST /api/shoots/{id}/director-message`)."""
        body: dict[str, Any] = {"message": message}
        if scene_id is not None:
            body["scene_id"] = scene_id
        url = f"{self.mastering_url}/api/shoots/{shoot_id}/director-message"
        return dict(self._request("POST", url, json_body=body))

    def regenerate_all_shoot_scenes(
        self,
        shoot_id: int | str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Trigger batch scene/shot regeneration & Auto-QC retakes (`POST /api/shoots/{id}/scenes/regenerate-all`)."""
        opts = options or {}
        if opts.get("auto_qc") or opts.get("qc"):
            self.require_feature_entitlement(
                "mv_auto_qc_unlocked",
                endpoint=f"/api/shoots/{shoot_id}/scenes/regenerate-all",
            )
        if opts.get("deep_research") or opts.get("research"):
            self.require_feature_entitlement(
                "mv_deep_research_unlocked",
                endpoint=f"/api/shoots/{shoot_id}/scenes/regenerate-all",
            )
        url = f"{self.mastering_url}/api/shoots/{shoot_id}/scenes/regenerate-all"
        return dict(self._request("POST", url, json_body=opts))

    def list_video_clips(self) -> Any:
        """List short-form vertical video clips / Spotify 9:16 Canvas exports (`GET /api/video-clips/`)."""
        url = f"{self.backend_url}/api/video-clips/"
        return self._request("GET", url)

    def create_video_clip(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a short-form vertical video clip or Spotify 9:16 Canvas (`POST /api/video-clips/`)."""
        url = f"{self.backend_url}/api/video-clips/"
        return dict(self._request("POST", url, json_body=payload))

    def list_music_video_comments(self, video_id: int | str | None = None) -> Any:
        """List community `/watch-film` music video comments (`GET /api/music-videos/comments/`)."""
        url = f"{self.backend_url}/api/music-videos/comments/"
        params = {"video_id": video_id} if video_id is not None else None
        return self._request("GET", url, params=params)

    def post_music_video_comment(self, video_id: int | str, text: str) -> dict[str, Any]:
        """Post a comment on a community music video (`POST /api/music-videos/comments/`)."""
        url = f"{self.backend_url}/api/music-videos/comments/"
        return dict(self._request("POST", url, json_body={"video_id": video_id, "text": text}))

    def list_following_music_videos(self, page: int = 1) -> dict[str, Any]:
        """Fetch `/watch-film` following feed (`GET /api/music-videos/following/?page=`)."""
        url = f"{self.backend_url}/api/music-videos/following/"
        return dict(self._request("GET", url, params={"page": page}))

    def build_shoots_google_sso_url(self, gac_token: str | None = None) -> str:
        """Construct the direct cross-domain SSO handoff URL for `shoots.soundboost.ai` (`AI Clones`, `Press Photos`, `Spotify Canvas`, `Cover Art`)."""
        token = (gac_token or self.access_token or "").strip()
        return f"{self.mastering_url}/sso?provider=google&appName=shoots&gacTkn={token}"

    # -----------------------------------------------------------------------
    # 11. Core Mastering Telemetry, Stem-Assisted Prep & Track Lifecycle
    # -----------------------------------------------------------------------
    def get_master_stats(self, track_id: int | str) -> dict[str, Any]:
        """Fetch detailed pre/post mastering loudness, peak, dynamic range & EQ telemetry (`GET /api/get-master-stats`)."""
        url = f"{self.backend_url}/api/get-master-stats"
        return dict(self._request("GET", url, params={"track_id": track_id, "id": track_id}))

    def start_mastering_stem_prep(self, track_id: int | str) -> dict[str, Any]:
        """Run stem-assisted mastering preparation (`POST /api/mastering/stem-prep`)."""
        url = f"{self.backend_url}/api/mastering/stem-prep"
        return dict(self._request("POST", url, json_body={"track_id": track_id}))

    def process_audio_legacy(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Trigger core DSP audio processing (`POST /api/process-audio/`)."""
        url = f"{self.backend_url}/api/process-audio/"
        return dict(self._request("POST", url, json_body=payload))

    def receive_mastering_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send mastering DSP parameter updates (`POST /api/receive-data/`)."""
        url = f"{self.backend_url}/api/receive-data/"
        return dict(self._request("POST", url, json_body=payload))

    def get_song_master(self, song_id: int | str) -> dict[str, Any]:
        """Fetch song master metadata (`GET /api/songs/{id}/master`)."""
        url = f"{self.backend_url}/api/songs/{song_id}/master"
        return dict(self._request("GET", url))

    def get_song_download(self, song_id: int | str, fmt: str = "wav") -> dict[str, Any]:
        """Fetch signed download link for a mastered song (`GET /api/songs/{id}/download`)."""
        self.require_feature_entitlement(
            "mastering_full_length_download",
            endpoint=f"/api/songs/{song_id}/download",
        )
        url = f"{self.backend_url}/api/songs/{song_id}/download"
        return dict(self._request("GET", url, params={"format": fmt}))

    def delete_track(self, track_id: int | str) -> dict[str, Any]:
        """Delete an uploaded source track (`POST /api/delete-track/`)."""
        url = f"{self.backend_url}/api/delete-track/"
        return dict(self._request("POST", url, json_body={"track_id": track_id, "id": track_id}))

    def delete_mastered_track(self, track_id: int | str) -> dict[str, Any]:
        """Delete a mastered track version (`POST /api/delete-mastered/`)."""
        url = f"{self.backend_url}/api/delete-mastered/"
        return dict(self._request("POST", url, json_body={"track_id": track_id, "id": track_id}))

    def toggle_favorite_track(self, track_id: int | str) -> dict[str, Any]:
        """Toggle favorite status on a track (`POST /api/toggle-favorite/`)."""
        url = f"{self.backend_url}/api/toggle-favorite/"
        return dict(self._request("POST", url, json_body={"track_id": track_id, "id": track_id}))

    def update_track_cover_art(self, track_id: int | str, image_path_or_url: str) -> dict[str, Any]:
        """Upload or assign cover art to a track (`POST /api/tracks/{id}/cover-art/`)."""
        url = f"{self.backend_url}/api/tracks/{track_id}/cover-art/"
        candidate = Path(image_path_or_url).expanduser()
        if candidate.exists() and candidate.is_file():
            with candidate.open("rb") as fh:
                files = {"file": (candidate.name, fh, "application/octet-stream")}
                return dict(self._request("POST", url, files=files))
        return dict(self._request("POST", url, json_body={"cover_url": image_path_or_url}))

    def create_track_public_link(self, track_id: int | str, enabled: bool = True) -> dict[str, Any]:
        """Create or toggle a shareable public link for a track (`POST /api/tracks/{id}/public-link/`)."""
        url = f"{self.backend_url}/api/tracks/{track_id}/public-link/"
        return dict(self._request("POST", url, json_body={"enabled": enabled}))

    def get_public_tracks_by_ids(self, track_ids: list[int | str]) -> Any:
        """Fetch public tracks by IDs (`GET /api/public/tracks/by-id/`)."""
        joined = ",".join(str(tid) for tid in track_ids)
        url = f"{self.backend_url}/api/public/tracks/by-id/"
        return self._request("GET", url, params={"ids": joined})

    def generate_waveform(self, track_id: int | str) -> dict[str, Any]:
        """Generate waveform peak telemetry for a track (`POST /api/waveform/generate/`)."""
        url = f"{self.backend_url}/api/waveform/generate/"
        return dict(self._request("POST", url, json_body={"track_id": track_id}))

    def get_cover_image_presigned_url(self, filename: str = "cover.jpg") -> dict[str, Any]:
        """Get a presigned upload URL for cover art (`GET /api/cover-image-presigned-url`)."""
        url = f"{self.backend_url}/api/cover-image-presigned-url"
        return dict(self._request("GET", url, params={"filename": filename}))

    # -----------------------------------------------------------------------
    # 12. Artist Profile, Social Notifications & Billing/Subscriptions
    # -----------------------------------------------------------------------
    def get_account_profile(self) -> dict[str, Any]:
        """Fetch authenticated artist account profile (`GET /api/account/profile/`)."""
        url = f"{self.backend_url}/api/account/profile/"
        return dict(self._request("GET", url))

    def update_account_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update authenticated artist account profile (`PATCH /api/account/profile/`)."""
        url = f"{self.backend_url}/api/account/profile/"
        return dict(self._request("PATCH", url, json_body=payload))

    def check_username_availability(self, username: str) -> dict[str, Any]:
        """Check whether an artist handle is available (`GET /api/account/profile/username-availability/`)."""
        url = f"{self.backend_url}/api/account/profile/username-availability/"
        return dict(self._request("GET", url, params={"username": username.strip()}))

    def get_public_artist_profile(self, username: str) -> dict[str, Any]:
        """Fetch a public artist profile (`GET /api/public/profiles/{username}/`)."""
        url = f"{self.backend_url}/api/public/profiles/{username.strip()}/"
        return dict(self._request("GET", url))

    def list_notifications(self, page: int = 1) -> dict[str, Any]:
        """List social notifications (`GET /api/social/notifications/?page=`)."""
        url = f"{self.backend_url}/api/social/notifications/"
        return dict(self._request("GET", url, params={"page": page}))

    def get_unread_notifications_count(self) -> dict[str, Any]:
        """Get unread notification count (`GET /api/social/notifications/unread-count/`)."""
        url = f"{self.backend_url}/api/social/notifications/unread-count/"
        return dict(self._request("GET", url))

    def mark_notifications_read(self, notification_ids: list[int | str] | None = None) -> dict[str, Any]:
        """Mark notifications as read (`POST /api/social/notifications/read/`)."""
        url = f"{self.backend_url}/api/social/notifications/read/"
        body = {"ids": notification_ids} if notification_ids is not None else {}
        return dict(self._request("POST", url, json_body=body))

    def get_billing_info(self) -> dict[str, Any]:
        """Fetch subscription, credit balance, and billing details (`GET /api/billing/info`)."""
        url = f"{self.backend_url}/api/billing/info"
        return dict(self._request("GET", url))

    def create_checkout_session(self, product_id: str | int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create a Stripe checkout session (`POST /api/create-checkout-session`)."""
        url = f"{self.backend_url}/api/create-checkout-session"
        body: dict[str, Any] = {"product_id": product_id, **(extra or {})}
        return dict(self._request("POST", url, json_body=body))

    def create_customer_portal_session(self) -> dict[str, Any]:
        """Create a Stripe billing portal session (`POST /api/create-customer-portal-session/`)."""
        url = f"{self.backend_url}/api/create-customer-portal-session/"
        return dict(self._request("POST", url, json_body={}))

    def pause_subscription(self) -> dict[str, Any]:
        """Pause active subscription (`POST /api/pause-subscription/`)."""
        url = f"{self.backend_url}/api/pause-subscription/"
        return dict(self._request("POST", url, json_body={}))

    def resume_subscription(self) -> dict[str, Any]:
        """Resume paused subscription (`POST /api/resume-subscription/`)."""
        url = f"{self.backend_url}/api/resume-subscription/"
        return dict(self._request("POST", url, json_body={}))

    def get_referral_info(self) -> dict[str, Any]:
        """Fetch artist referral code and reward telemetry (`GET /api/get-referral-info`)."""
        url = f"{self.backend_url}/api/get-referral-info"
        return dict(self._request("GET", url))


def resolve_account_entitlements(
    billing_or_user_payload: dict[str, Any] | None = None,
    *,
    is_authenticated: bool = True,
    override_tier: str | None = None,
) -> dict[str, Any]:
    """Module-level helper for `SoundBoostClient.resolve_account_entitlements`."""
    return SoundBoostClient.resolve_account_entitlements(
        billing_or_user_payload,
        is_authenticated=is_authenticated,
        override_tier=override_tier,
    )


