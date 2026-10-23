"""Dual-Sided OAuth 2.1 & OS Keyring Credential Vault for Suavecito's Soundboost.AI Toolkit.

Implements:
1. OS Keyring Credential Vault (`keyring` / Windows Credential Manager `WinVaultKeyring`,
   macOS Keychain, Linux SecretService) with fallback to `~/.soundboost/credentials.json`.
2. Outbound SoundBoost Google OAuth 2.0 (`POST https://api.soundboost.ai/api/google-login/`),
   RFC 7636 PKCE (`S256`) authorization URL generator, and local loopback browser callback
   catcher (`http://127.0.0.1:<port>/auth/callback`).
3. Inbound FastMCP 4.1+ OAuth 2.1 Provider Factory (`GoogleProvider`, `GitHubProvider`,
   `JWTVerifier`, `OAuthProxy`, `RemoteAuthProvider`) with Client ID Metadata Documents
   (`enable_cimd=True`) for Streamable HTTP deployments.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

SOUNDBOOST_GOOGLE_OAUTH_CLIENT_ID = (
    "716860248198-t1s5lv1n1msgfoe3dt7vekro8b1fpd9g.apps.googleusercontent.com"
)
GOOGLE_OAUTH_AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_EXCHANGE_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"


# ---------------------------------------------------------------------------
# 1. OS Keyring & Local Credential Vault
# ---------------------------------------------------------------------------
def _credentials_file_path() -> Path:
    return Path.home() / ".soundboost" / "credentials.json"


def save_credentials_to_vault(
    access_token: str,
    *,
    email: str | None = None,
    user_id: str | int | None = None,
    auth_method: str = "oauth",
) -> dict[str, Any]:
    """Persist SoundBoost Bearer token in OS Keyring (`WinVaultKeyring`) and fallback JSON."""
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
    except KeyringError:
        keyring_saved = False

    cred_file = _credentials_file_path()
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(
        json.dumps(
            {
                "access_token": cleaned_token,
                "email": email,
                "user_id": user_id,
                "auth_method": auth_method,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "saved_to_os_keyring": keyring_saved,
        "keyring_backend": type(keyring.get_keyring()).__name__,
        "fallback_file": str(cred_file),
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
    except KeyringError:
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
    """Inspect current authentication state across env, OS Keyring, and fallback file."""
    token = load_token_from_vault()
    meta: dict[str, Any] = {}
    try:
        raw_meta = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_ACCOUNT_META)
        if raw_meta:
            meta = json.loads(raw_meta)
    except (KeyringError, ValueError):
        meta = {}

    cred_file = _credentials_file_path()
    return {
        "authenticated": bool(token),
        "token_preview": f"{token[:10]}...{token[-6:]}" if token and len(token) > 20 else bool(token),
        "keyring_backend": type(keyring.get_keyring()).__name__,
        "metadata": meta,
        "credentials_file_exists": cred_file.exists(),
    }


def clear_vault_credentials() -> dict[str, Any]:
    """Delete stored credentials from both OS Keyring and `~/.soundboost/credentials.json`."""
    kr_cleared = False
    for acct in (KEYRING_CREDENTIAL_ENTRY, KEYRING_ACCOUNT_META):
        try:
            keyring.delete_password(KEYRING_SERVICE_NAME, acct)
            kr_cleared = True
        except KeyringError:
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
# 2. Outbound SoundBoost Google OAuth 2.0 + PKCE (RFC 7636) + Loopback Flow
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PKCEChallenge:
    state: str
    code_verifier: str
    code_challenge: str
    authorize_url: str
    redirect_uri: str


def generate_google_pkce_challenge(
    redirect_uri: str = "http://127.0.0.1:8765/auth/callback",
    client_id: str | None = None,
    scopes: list[str] | None = None,
) -> PKCEChallenge:
    """Create an RFC 7636 S256 PKCE challenge and Google OAuth 2.0 authorization URL."""
    cid = client_id or os.environ.get(
        "SOUNDBOOST_GOOGLE_CLIENT_ID", SOUNDBOOST_GOOGLE_OAUTH_CLIENT_ID
    )
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(24)

    scope_str = " ".join(
        scopes
        or [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ]
    )
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope_str,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "select_account",
    }
    url = f"{GOOGLE_OAUTH_AUTHORIZE_ENDPOINT}?{urllib.parse.urlencode(params)}"
    return PKCEChallenge(
        state=state,
        code_verifier=verifier,
        code_challenge=challenge,
        authorize_url=url,
        redirect_uri=redirect_uri,
    )


def run_loopback_oauth_handoff(
    port: int = 8765,
    timeout_sec: float = 120.0,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Start a local loopback HTTP callback server (`127.0.0.1:<port>/auth/callback`) that accepts
    a Google OAuth callback or a SoundBoost Bearer JWT from the browser
    and stores it directly in OS Keyring (`WinVaultKeyring`).
    """
    redirect_uri = f"http://127.0.0.1:{port}/auth/callback"
    pkce = generate_google_pkce_challenge(redirect_uri=redirect_uri)
    captured: dict[str, Any] = {}
    done_event = threading.Event()

    landing_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Suavecito's Soundboost.AI Toolkit — OAuth & Session Handoff</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; background: #0b0d13; color: #f1f5f9; max-width: 640px; margin: 48px auto; padding: 24px; }}
  .card {{ background: #141824; border: 1px solid #262d40; border-radius: 12px; padding: 24px; }}
  h1 {{ font-size: 20px; margin-top: 0; color: #38bdf8; }}
  input {{ width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #fff; margin: 8px 0 16px; }}
  button, a.btn {{ display: inline-block; background: #0284c7; color: #fff; text-decoration: none; border: none; padding: 10px 18px; border-radius: 8px; font-weight: 600; cursor: pointer; }}
</style>
</head>
<body>
  <div class="card">
    <h1>Suavecito's Soundboost.AI Toolkit — OAuth Handoff</h1>
    <p>Sign in via Google OAuth or paste your SoundBoost Bearer token below to store it in your OS Keyring:</p>
    <p><a class="btn" href="{pkce.authorize_url}">Continue with Google OAuth</a></p>
    <hr style="border-color:#262d40;margin:20px 0;">
    <form method="GET" action="/auth/callback">
      <input type="hidden" name="state" value="{pkce.state}">
      <label>SoundBoost Bearer Token:</label>
      <input type="text" name="session_token" placeholder="eyJ..." required>
      <button type="submit">Save to OS Keyring</button>
    </form>
  </div>
</body>
</html>"""

    class _CallbackHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(landing_html.encode("utf-8"))
                return

            if parsed.path != "/auth/callback":
                self.send_response(404)
                self.end_headers()
                return

            qs = urllib.parse.parse_qs(parsed.query)
            state = (qs.get("state") or [""])[0]
            if state and state != pkce.state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid OAuth state parameter.")
                return

            raw_sess = (qs.get("session_token") or qs.get("access_token") or [""])[0].strip()
            code = (qs.get("code") or [""])[0].strip()
            captured["code"] = code or None
            captured["code_verifier"] = pkce.code_verifier
            captured["redirect_uri"] = redirect_uri

            if raw_sess:
                vault_res = save_credentials_to_vault(raw_sess, auth_method="loopback_handoff")
                captured["vault"] = vault_res
                captured["status"] = "saved"
            elif code:
                captured["status"] = "authorization_code_received"

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='background:#0b0d13;color:#38bdf8;font-family:sans-serif;padding:40px;'>"
                b"<h2>Authentication captured by Suavecito's Soundboost.AI Toolkit.</h2>"
                b"<p>You may close this browser tab.</p></body></html>"
            )
            done_event.set()

    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_url = f"http://127.0.0.1:{port}/"
    if open_browser:
        webbrowser.open(local_url)

    try:
        done_event.wait(timeout=timeout_sec)
    finally:
        server.shutdown()
        server.server_close()

    return {
        "local_handoff_url": local_url,
        "google_authorize_url": pkce.authorize_url,
        **captured,
    }


# ---------------------------------------------------------------------------
# 3. Inbound FastMCP 4.1+ OAuth 2.1 Server Provider Factory (Streamable HTTP)
# ---------------------------------------------------------------------------
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
) -> AuthProvider | None:
    """Construct a FastMCP 4.1+ OAuth 2.1 `AuthProvider` for Streamable HTTP deployments.

    Supported `provider_type` values (or `SOUNDBOOST_MCP_OAUTH_PROVIDER` env var):
    - `none` (default for local `stdio`): Returns `None`
    - `google`: `GoogleProvider` (OAuth 2.1 + OpenID Connect + CIMD)
    - `github`: `GitHubProvider` (OAuth 2.1 + CIMD)
    - `jwt`: `JWTVerifier` (JWKS / OIDC Bearer token verification with SSRF protection)
    - `oauth_proxy`: `OAuthProxy` + `JWTVerifier` wrapping any upstream OAuth 2.0 server
    - `remote`: `RemoteAuthProvider` + `JWTVerifier` with RFC 9728 Protected Resource Metadata
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
    cid = (
        client_id
        or os.environ.get("SOUNDBOOST_MCP_OAUTH_CLIENT_ID")
        or os.environ.get("SOUNDBOOST_GOOGLE_CLIENT_ID")
        or SOUNDBOOST_GOOGLE_OAUTH_CLIENT_ID
    )
    csecret = client_secret or os.environ.get("SOUNDBOOST_MCP_OAUTH_CLIENT_SECRET")
    signing_key: str = (
        os.environ.get("SOUNDBOOST_MCP_JWT_SIGNING_KEY") or secrets.token_urlsafe(32)
    )

    if mode == "google":
        return GoogleProvider(
            client_id=cid,
            client_secret=csecret,
            base_url=resolved_base,
            required_scopes=[
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
            jwt_signing_key=signing_key,
            enable_cimd=True,
        )

    if mode == "github":
        if not csecret:
            raise ValueError(
                "GitHubProvider requires client_secret (set SOUNDBOOST_MCP_OAUTH_CLIENT_SECRET)"
            )
        return GitHubProvider(
            client_id=cid,
            client_secret=csecret,
            base_url=resolved_base,
            enable_cimd=True,
        )

    resolved_jwks = jwks_uri or os.environ.get("SOUNDBOOST_MCP_JWKS_URI", GOOGLE_JWKS_URI)
    resolved_issuer = issuer or os.environ.get(
        "SOUNDBOOST_MCP_ISSUER", "https://accounts.google.com"
    )
    resolved_aud = audience or os.environ.get("SOUNDBOOST_MCP_AUDIENCE", cid)

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
            upstream_client_id=cid,
            upstream_client_secret=csecret,
            token_verifier=verifier,
            base_url=resolved_base,
            jwt_signing_key=signing_key,
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
