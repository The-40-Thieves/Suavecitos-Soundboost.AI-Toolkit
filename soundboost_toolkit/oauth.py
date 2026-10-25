"""Dual-Sided OAuth 2.1 & OS Keyring Credential Vault for Suavecito's Soundboost.AI Toolkit.

Implements:
1. OS Keyring Credential Vault (`keyring` / Windows Credential Manager `WinVaultKeyring`,
   macOS Keychain, Linux SecretService). A plaintext `~/.soundboost/credentials.json` file is
   only written when no keyring backend is usable, and is created owner-read/write only.
2. Outbound Google OAuth 2.0 with RFC 7636 PKCE (`S256`) and a loopback browser callback
   (`http://127.0.0.1:<port>/auth/callback`) that exchanges the authorization code for a Google
   `id_token`, which the caller then trades at `POST https://api.soundboost.ai/api/google-login/`.
3. Inbound FastMCP 4.1+ OAuth 2.1 Provider Factory (`GoogleProvider`, `GitHubProvider`,
   `JWTVerifier`, `OAuthProxy`, `RemoteAuthProvider`) for Streamable HTTP deployments.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import http.server
import json
import os
import secrets
import threading
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import keyring
from fastmcp.server.auth import AuthProvider, OAuthProxy, RemoteAuthProvider
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from keyring.errors import KeyringError
from pydantic import AnyHttpUrl

KEYRING_SERVICE_NAME = "suavecitos-soundboost-toolkit"
KEYRING_CREDENTIAL_ENTRY = "soundboost_bearer_credential"
KEYRING_ACCOUNT_META = "soundboost_account_metadata"

# SoundBoost's public web client ID. Its authorized redirect URIs are SoundBoost's own pages, so
# the loopback flow needs a Google "Desktop app" client registered for `http://127.0.0.1`
# (set `SOUNDBOOST_GOOGLE_CLIENT_ID`, and `SOUNDBOOST_GOOGLE_CLIENT_SECRET` if Google issued one).
SOUNDBOOST_GOOGLE_OAUTH_CLIENT_ID = (
    "716860248198-t1s5lv1n1msgfoe3dt7vekro8b1fpd9g.apps.googleusercontent.com"
)
GOOGLE_OAUTH_AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_EXCHANGE_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"

LOOPBACK_HOST = "127.0.0.1"
MAX_FORM_BYTES = 16_384


# ---------------------------------------------------------------------------
# 1. OS Keyring & Local Credential Vault
# ---------------------------------------------------------------------------
def _credentials_file_path() -> Path:
    return Path.home() / ".soundboost" / "credentials.json"


def _write_private_file(path: Path, content: str) -> None:
    """Write `content` to `path` readable/writable by the current user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _keyring_backend_name() -> str:
    try:
        return type(keyring.get_keyring()).__name__
    except Exception:  # noqa: BLE001 - keyring backends raise arbitrary errors on init
        return "unavailable"


def save_credentials_to_vault(
    access_token: str,
    *,
    email: str | None = None,
    user_id: str | int | None = None,
    auth_method: str = "oauth",
) -> dict[str, Any]:
    """Persist the SoundBoost Bearer token in the OS Keyring.

    Falls back to an owner-only `~/.soundboost/credentials.json` file only when the keyring
    backend fails. A successful keyring save removes any stale plaintext file.
    """
    cleaned_token = access_token.strip()
    if not cleaned_token:
        raise ValueError("access_token cannot be empty")

    meta = {
        "email": email,
        "user_id": str(user_id) if user_id is not None else None,
        "auth_method": auth_method,
    }
    keyring_saved = False
    try:
        keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_CREDENTIAL_ENTRY, cleaned_token)
        keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_ACCOUNT_META, json.dumps(meta))
        keyring_saved = True
    except (KeyringError, RuntimeError):
        keyring_saved = False

    cred_file = _credentials_file_path()
    fallback_written = False
    if keyring_saved:
        if cred_file.exists():
            cred_file.unlink()
    else:
        _write_private_file(
            cred_file,
            json.dumps({"access_token": cleaned_token, **meta}, indent=2),
        )
        fallback_written = True

    return {
        "saved_to_os_keyring": keyring_saved,
        "keyring_backend": _keyring_backend_name(),
        "plaintext_fallback_file": str(cred_file) if fallback_written else None,
        "auth_method": auth_method,
        "email": email,
        "user_id": user_id,
    }


def load_token_from_vault() -> str | None:
    """Resolve SoundBoost Bearer token from env -> OS Keyring -> `~/.soundboost/credentials.json`."""
    env_tok = os.environ.get("SOUNDBOOST_ACCESS_TOKEN", "").strip()
    if env_tok:
        return env_tok

    try:
        kr_tok = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_CREDENTIAL_ENTRY)
        if kr_tok and kr_tok.strip():
            return kr_tok.strip()
    except (KeyringError, RuntimeError):
        pass

    cred_file = _credentials_file_path()
    if cred_file.exists():
        try:
            data = json.loads(cred_file.read_text(encoding="utf-8"))
            if isinstance(data.get("access_token"), str) and data["access_token"].strip():
                return str(data["access_token"]).strip()
        except (OSError, ValueError, KeyError, TypeError):
            return None
    return None


def get_vault_status() -> dict[str, Any]:
    """Inspect current authentication state without revealing any part of the token."""
    token = load_token_from_vault()
    meta: dict[str, Any] = {}
    try:
        raw_meta = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_ACCOUNT_META)
        if raw_meta:
            meta = json.loads(raw_meta)
    except (KeyringError, RuntimeError, ValueError):
        meta = {}

    source = None
    if os.environ.get("SOUNDBOOST_ACCESS_TOKEN", "").strip():
        source = "environment"
    elif token:
        source = "plaintext_file" if _credentials_file_path().exists() else "os_keyring"
    return {
        "authenticated": bool(token),
        "token_source": source,
        "keyring_backend": _keyring_backend_name(),
        "metadata": meta,
        "credentials_file_exists": _credentials_file_path().exists(),
    }


def clear_vault_credentials() -> dict[str, Any]:
    """Delete stored credentials from both OS Keyring and `~/.soundboost/credentials.json`."""
    kr_cleared = False
    for acct in (KEYRING_CREDENTIAL_ENTRY, KEYRING_ACCOUNT_META):
        try:
            keyring.delete_password(KEYRING_SERVICE_NAME, acct)
            kr_cleared = True
        except (KeyringError, RuntimeError):
            pass

    cred_file = _credentials_file_path()
    file_deleted = False
    if cred_file.exists():
        cred_file.unlink()
        file_deleted = True

    return {
        "keyring_cleared": kr_cleared,
        "credentials_file_deleted": file_deleted,
    }


# ---------------------------------------------------------------------------
# 2. Outbound Google OAuth 2.0 + PKCE (RFC 7636) + Loopback Flow
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PKCEChallenge:
    state: str
    code_verifier: str
    code_challenge: str
    authorize_url: str
    redirect_uri: str
    client_id: str


def _loopback_google_client_id() -> str | None:
    cid = os.environ.get("SOUNDBOOST_GOOGLE_CLIENT_ID", "").strip()
    return cid or None


def generate_google_pkce_challenge(
    redirect_uri: str = f"http://{LOOPBACK_HOST}:8765/auth/callback",  # NOSONAR: RFC 8252 loopback redirect, never leaves 127.0.0.1
    client_id: str | None = None,
    scopes: list[str] | None = None,
) -> PKCEChallenge:
    """Create an RFC 7636 S256 PKCE challenge and Google OAuth 2.0 authorization URL."""
    cid = client_id or _loopback_google_client_id() or SOUNDBOOST_GOOGLE_OAUTH_CLIENT_ID
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(24)

    scope_str = " ".join(scopes or ["openid", "email", "profile"])
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope_str,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    url = f"{GOOGLE_OAUTH_AUTHORIZE_ENDPOINT}?{urllib.parse.urlencode(params)}"
    return PKCEChallenge(
        state=state,
        code_verifier=verifier,
        code_challenge=challenge,
        authorize_url=url,
        redirect_uri=redirect_uri,
        client_id=cid,
    )


def exchange_google_authorization_code(pkce: PKCEChallenge, code: str) -> dict[str, Any]:
    """Exchange a Google authorization code (with its PKCE verifier) for Google tokens."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pkce.redirect_uri,
        "client_id": pkce.client_id,
        "code_verifier": pkce.code_verifier,
    }
    secret = os.environ.get("SOUNDBOOST_GOOGLE_CLIENT_SECRET", "").strip()
    if secret:
        form["client_secret"] = secret
    resp = httpx.post(GOOGLE_OAUTH_EXCHANGE_ENDPOINT, data=form, timeout=20.0)
    resp.raise_for_status()
    return dict(resp.json())


def _landing_html(pkce: PKCEChallenge, google_enabled: bool) -> str:
    state = html.escape(pkce.state, quote=True)
    google_block = (
        f'<p><a class="btn" href="{html.escape(pkce.authorize_url, quote=True)}">'
        "Continue with Google</a></p><hr>"
        if google_enabled
        else "<p><small>Google sign-in is disabled until <code>SOUNDBOOST_GOOGLE_CLIENT_ID</code> "
        "is set to a Desktop-app OAuth client.</small></p><hr>"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Soundboost Sign-in</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0b0d13; color: #f1f5f9; max-width: 640px; margin: 48px auto; padding: 0 16px; }}
  .card {{ background: #141824; border: 1px solid #262d40; border-radius: 12px; padding: 24px; }}
  h1 {{ font-size: 20px; margin-top: 0; color: #38bdf8; }}
  hr {{ border-color: #262d40; margin: 20px 0; }}
  input {{ width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #fff; margin: 8px 0 16px; }}
  button, a.btn {{ display: inline-block; background: #0284c7; color: #fff; text-decoration: none; border: none; padding: 10px 18px; border-radius: 8px; font-weight: 600; cursor: pointer; }}
</style>
</head>
<body>
  <div class="card">
    <h1>Suavecito's Soundboost.AI Toolkit sign-in</h1>
    {google_block}
    <form method="POST" action="/auth/callback" autocomplete="off">
      <input type="hidden" name="state" value="{state}">
      <label for="tok">SoundBoost Bearer token</label>
      <input id="tok" type="password" name="session_token" placeholder="eyJ..." required>
      <button type="submit">Save to OS Keyring</button>
    </form>
  </div>
</body>
</html>"""


def run_loopback_oauth_handoff(
    port: int = 8765,
    timeout_sec: float = 120.0,
    open_browser: bool = True,
    *,
    exchange_google_id_token: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run a one-shot `127.0.0.1` callback server that captures a SoundBoost sign-in.

    - Pasted Bearer tokens arrive by POST (never in the URL) and are stored in the OS Keyring.
    - A Google authorization code is exchanged (PKCE) for an `id_token`, which is handed to
      `exchange_google_id_token` (normally `SoundBoostClient.google_oauth_login`).
    Every callback must carry the per-session `state`, and requests whose `Host` header is not
    the loopback address are rejected (DNS-rebinding protection). No token, code, or verifier
    is included in the returned result.
    """
    google_enabled = _loopback_google_client_id() is not None
    redirect_uri = f"http://{LOOPBACK_HOST}:{port}/auth/callback"  # NOSONAR: RFC 8252 loopback redirect, never leaves 127.0.0.1
    pkce = generate_google_pkce_challenge(redirect_uri=redirect_uri)
    landing_html = _landing_html(pkce, google_enabled).encode("utf-8")
    expected_hosts = {f"{LOOPBACK_HOST}:{port}", f"localhost:{port}"}
    result: dict[str, Any] = {"status": "timeout"}
    done_event = threading.Event()
    lock = threading.Lock()

    def _state_ok(candidate: str) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate, pkce.state)

    class _CallbackHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, log_format: str, *args: Any) -> None:
            return

        def _reply(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").strip().lower() in expected_hosts

        def _finish(self, outcome: dict[str, Any]) -> None:
            with lock:
                if done_event.is_set():
                    self._reply(409, b"This sign-in session is already complete.")
                    return
                result.clear()
                result.update(outcome)
                done_event.set()
            ok = outcome.get("status") in {"saved", "google_login_complete"}
            msg = (
                b"<h2>Signed in. You may close this tab.</h2>"
                if ok
                else b"<h2>Sign-in failed. Check the terminal for details.</h2>"
            )
            self._reply(200 if ok else 400, msg, "text/html")

        def do_GET(self) -> None:
            if not self._host_ok():
                self._reply(403, b"Forbidden host.")
                return
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._reply(200, landing_html, "text/html")
                return
            if parsed.path != "/auth/callback":
                self._reply(404, b"Not found.")
                return

            qs = urllib.parse.parse_qs(parsed.query)
            if not _state_ok((qs.get("state") or [""])[0]):
                self._reply(400, b"Invalid or missing OAuth state parameter.")
                return
            if qs.get("error"):
                self._finish({"status": "google_error", "error": qs["error"][0][:200]})
                return
            code = (qs.get("code") or [""])[0].strip()
            if not code or not google_enabled:
                self._reply(400, b"Missing authorization code.")
                return
            try:
                google_tokens = exchange_google_authorization_code(pkce, code)
                id_token = str(google_tokens.get("id_token") or "")
                if not id_token:
                    raise ValueError("Google did not return an id_token")
                if exchange_google_id_token is None:
                    self._finish({"status": "google_id_token_received_no_exchanger"})
                    return
                login = exchange_google_id_token(id_token)
                self._finish(
                    {
                        "status": "google_login_complete",
                        "email": login.get("email") or (login.get("user") or {}).get("email"),
                    }
                )
            except (httpx.HTTPError, ValueError) as exc:
                self._finish({"status": "google_exchange_failed", "error": type(exc).__name__})

        def do_POST(self) -> None:
            if not self._host_ok():
                self._reply(403, b"Forbidden host.")
                return
            if urllib.parse.urlparse(self.path).path != "/auth/callback":
                self._reply(404, b"Not found.")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_FORM_BYTES:
                self._reply(413, b"Invalid request size.")
                return
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            if not _state_ok((form.get("state") or [""])[0]):
                self._reply(400, b"Invalid or missing OAuth state parameter.")
                return
            raw_sess = (form.get("session_token") or [""])[0].strip()
            if not raw_sess:
                self._reply(400, b"Missing token.")
                return
            vault_res = save_credentials_to_vault(raw_sess, auth_method="loopback_handoff")
            self._finish({"status": "saved", "vault": vault_res})

    server = http.server.HTTPServer((LOOPBACK_HOST, port), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_url = f"http://{LOOPBACK_HOST}:{port}/"  # NOSONAR: RFC 8252 loopback redirect, never leaves 127.0.0.1
    if open_browser:
        webbrowser.open(local_url)

    try:
        done_event.wait(timeout=timeout_sec)
    finally:
        server.shutdown()
        server.server_close()

    return {"local_handoff_url": local_url, "google_sign_in_enabled": google_enabled, **result}


# ---------------------------------------------------------------------------
# 3. Inbound FastMCP 4.1+ OAuth 2.1 Server Provider Factory (Streamable HTTP)
# ---------------------------------------------------------------------------
def _require(value: str | None, env_name: str, mode: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"OAuth provider '{mode}' requires {env_name} to be set.")
    return value.strip()


def build_mcp_oauth_provider(
    provider_type: str | None = None,
    *,
    base_url: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    jwks_uri: str | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    upstream_authorize_url: str | None = None,
    upstream_token_url: str | None = None,
    jwt_signing_key: str | None = None,
) -> AuthProvider | None:
    """Construct a FastMCP 4.1+ OAuth 2.1 `AuthProvider` for Streamable HTTP deployments.

    Supported `provider_type` values (or `SOUNDBOOST_MCP_OAUTH_PROVIDER` env var):
    - `none` (default for local `stdio`): Returns `None`
    - `google`: `GoogleProvider` (OAuth 2.1 + OpenID Connect + CIMD)
    - `github`: `GitHubProvider` (OAuth 2.1 + CIMD)
    - `jwt`: `JWTVerifier` (JWKS / OIDC Bearer token verification with SSRF protection)
    - `oauth_proxy`: `OAuthProxy` + `JWTVerifier` wrapping any upstream OAuth 2.0 server
    - `remote`: `RemoteAuthProvider` + `JWTVerifier` with RFC 9728 Protected Resource Metadata

    The inbound OAuth client is *your* MCP server's client (`SOUNDBOOST_MCP_OAUTH_CLIENT_ID`),
    never SoundBoost's web client. Modes that mint their own session tokens require a stable
    `SOUNDBOOST_MCP_JWT_SIGNING_KEY` so issued tokens survive restarts and cannot be forged.
    """
    mode = (
        provider_type
        or os.environ.get("SOUNDBOOST_MCP_OAUTH_PROVIDER", "none")
    ).strip().lower()
    if mode in ("", "none", "stdio", "off", "false"):
        return None

    resolved_base = (
        base_url
        or os.environ.get("SOUNDBOOST_MCP_BASE_URL", "http://127.0.0.1:8000")
    ).strip()
    cid = client_id or os.environ.get("SOUNDBOOST_MCP_OAUTH_CLIENT_ID")
    csecret = client_secret or os.environ.get("SOUNDBOOST_MCP_OAUTH_CLIENT_SECRET")

    def _signing_key() -> str:
        key = _require(
            jwt_signing_key or os.environ.get("SOUNDBOOST_MCP_JWT_SIGNING_KEY"),
            "SOUNDBOOST_MCP_JWT_SIGNING_KEY",
            mode,
        )
        if len(key) < 32:
            raise ValueError("SOUNDBOOST_MCP_JWT_SIGNING_KEY must be at least 32 characters.")
        return key

    if mode == "google":
        return GoogleProvider(
            client_id=_require(cid, "SOUNDBOOST_MCP_OAUTH_CLIENT_ID", mode),
            client_secret=csecret,
            base_url=resolved_base,
            required_scopes=["openid", "email"],
            jwt_signing_key=_signing_key(),
            enable_cimd=True,
        )

    if mode == "github":
        return GitHubProvider(
            client_id=_require(cid, "SOUNDBOOST_MCP_OAUTH_CLIENT_ID", mode),
            client_secret=_require(csecret, "SOUNDBOOST_MCP_OAUTH_CLIENT_SECRET", mode),
            base_url=resolved_base,
            jwt_signing_key=_signing_key(),
            enable_cimd=True,
        )

    resolved_jwks = jwks_uri or os.environ.get("SOUNDBOOST_MCP_JWKS_URI", GOOGLE_JWKS_URI)
    resolved_issuer = issuer or os.environ.get(
        "SOUNDBOOST_MCP_ISSUER", "https://accounts.google.com"
    )
    resolved_aud = _require(
        audience or os.environ.get("SOUNDBOOST_MCP_AUDIENCE") or cid,
        "SOUNDBOOST_MCP_AUDIENCE (or SOUNDBOOST_MCP_OAUTH_CLIENT_ID)",
        mode,
    )

    verifier = JWTVerifier(
        jwks_uri=resolved_jwks,
        issuer=resolved_issuer,
        audience=resolved_aud,
        ssrf_safe=True,
    )

    if mode == "jwt":
        return verifier

    if mode == "oauth_proxy":
        auth_ep = upstream_authorize_url or os.environ.get(
            "SOUNDBOOST_MCP_UPSTREAM_AUTH_URL", GOOGLE_OAUTH_AUTHORIZE_ENDPOINT
        )
        tok_ep = upstream_token_url or os.environ.get(
            "SOUNDBOOST_MCP_UPSTREAM_TOKEN_URL", GOOGLE_OAUTH_EXCHANGE_ENDPOINT
        )
        return OAuthProxy(
            upstream_authorization_endpoint=auth_ep,
            upstream_token_endpoint=tok_ep,
            upstream_client_id=_require(cid, "SOUNDBOOST_MCP_OAUTH_CLIENT_ID", mode),
            upstream_client_secret=csecret,
            token_verifier=verifier,
            base_url=resolved_base,
            jwt_signing_key=_signing_key(),
            enable_cimd=True,
        )

    if mode == "remote":
        return RemoteAuthProvider(
            token_verifier=verifier,
            authorization_servers=[AnyHttpUrl(resolved_issuer)],
            base_url=resolved_base,
            resource_name="Suavecito's Soundboost.AI Toolkit MCP",
        )

    raise ValueError(
        f"Unsupported OAuth provider mode '{mode}'. Valid: none, google, github, jwt, oauth_proxy, remote"
    )
