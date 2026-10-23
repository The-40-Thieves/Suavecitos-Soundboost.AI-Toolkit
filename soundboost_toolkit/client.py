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

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from soundboost_toolkit.constants import (
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

DEFAULT_TIMEOUT_SEC = 60.0


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
        self.storage_proxy_host: str | None = None
        self.storage_direct_host: str | None = None

    @staticmethod
    def _resolve_access_token(
        access_token: str | None,
        encrypted_da_cookie: str | None,
    ) -> str | None:
        if access_token and access_token.strip():
            return access_token.strip()
        env_tok = os.environ.get("SOUNDBOOST_ACCESS_TOKEN", "").strip()
        if env_tok:
            return env_tok

        cookie_raw = encrypted_da_cookie or os.environ.get("SOUNDBOOST_COOKIE_DA", "").strip()
        if cookie_raw:
            decrypted = decrypt_cookie_value(cookie_raw)
            if isinstance(decrypted, str) and decrypted.strip():
                return decrypted.strip()

        cred_file = Path.home() / ".soundboost" / "credentials.json"
        if cred_file.exists():
            try:
                data = json.loads(cred_file.read_text(encoding="utf-8"))
                if isinstance(data.get("access_token"), str) and data["access_token"].strip():
                    return str(data["access_token"]).strip()
                if isinstance(data.get("_d_a"), str) and data["_d_a"].strip():
                    dec = decrypt_cookie_value(data["_d_a"])
                    if isinstance(dec, str):
                        return dec.strip()
            except (OSError, ValueError, KeyError):
                return None
        return None

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
    ) -> Any:
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
                cred_dir = Path.home() / ".soundboost"
                cred_dir.mkdir(parents=True, exist_ok=True)
                cred_file = cred_dir / "credentials.json"
                cred_file.write_text(
                    json.dumps({"email": email, "access_token": self.access_token}, indent=2),
                    encoding="utf-8",
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
