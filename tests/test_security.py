"""Regression tests for the toolkit's security boundaries."""

from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from soundboost_toolkit import oauth
from soundboost_toolkit.client import SoundBoostClient, SoundBoostPayGateError
from soundboost_toolkit.mcp_server import (
    generate_printable_lead_sheet,
    install_reaper_jsfx_and_scripts,
    redact_login_result,
    serve,
)
from soundboost_toolkit.security import (
    PathAccessError,
    email_allowlist_check,
    resolve_user_path,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class PathSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._env = mock.patch.dict(os.environ, {"SOUNDBOOST_ALLOWED_DIRS": str(self.root)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_allows_paths_inside_root(self) -> None:
        f = self.root / "song.wav"
        f.write_bytes(b"x")
        self.assertEqual(resolve_user_path(f, must_exist=True, allowed_suffixes={".wav"}), f)

    def test_rejects_traversal_and_outside_paths(self) -> None:
        with self.assertRaises(PathAccessError):
            resolve_user_path(self.root / ".." / "escape.wav")
        with self.assertRaises(PathAccessError):
            resolve_user_path(Path.home() / ".ssh" / "id_rsa")

    def test_rejects_hidden_components_and_bad_suffix(self) -> None:
        with self.assertRaises(PathAccessError):
            resolve_user_path(self.root / ".soundboost" / "credentials.json")
        with self.assertRaises(PathAccessError):
            resolve_user_path(self.root / "notes.txt", allowed_suffixes={".wav"})

    def test_rejects_symlink_escape(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        link = self.root / "link"
        try:
            link.symlink_to(outside.name, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks not permitted on this system")
        with self.assertRaises(PathAccessError):
            resolve_user_path(link / "x.wav")

    def test_lead_sheet_refuses_outside_write_and_overwrite(self) -> None:
        with self.assertRaises(PathAccessError):
            generate_printable_lead_sheet(
                title="t", lyrics_segments=[], chords=[], output_html_path=str(Path.home() / "x.html")
            )
        out = self.root / "sheet.html"
        generate_printable_lead_sheet(title="t", lyrics_segments=[], chords=[], output_html_path=str(out))
        with self.assertRaises(FileExistsError):
            generate_printable_lead_sheet(title="t", lyrics_segments=[], chords=[], output_html_path=str(out))

    def test_reaper_run_action_is_allow_listed(self) -> None:
        (self.root / "REAPER.ini").write_text("[reaper]\n", encoding="utf-8")
        with self.assertRaises(PermissionError):
            install_reaper_jsfx_and_scripts(
                reaper_resource_dir=str(self.root), action="run_action", reaper_action_id="40004"
            )
        with self.assertRaises(PathAccessError):
            install_reaper_jsfx_and_scripts(
                reaper_resource_dir=str(self.root / "nope"), action="inspect"
            )


class VaultTests(unittest.TestCase):
    def test_keyring_success_never_writes_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as home, mock.patch.object(
            oauth, "_credentials_file_path", return_value=Path(home) / ".soundboost" / "credentials.json"
        ), mock.patch.object(oauth.keyring, "set_password") as setpw:
            stale = Path(home) / ".soundboost" / "credentials.json"
            stale.parent.mkdir(parents=True)
            stale.write_text("{}", encoding="utf-8")
            res = oauth.save_credentials_to_vault("tok-123")
            self.assertTrue(res["saved_to_os_keyring"])
            self.assertIsNone(res["plaintext_fallback_file"])
            self.assertFalse(stale.exists())
            self.assertEqual(setpw.call_count, 2)

    def test_keyring_failure_writes_owner_only_file(self) -> None:
        with tempfile.TemporaryDirectory() as home, mock.patch.object(
            oauth, "_credentials_file_path", return_value=Path(home) / ".soundboost" / "credentials.json"
        ), mock.patch.object(oauth.keyring, "set_password", side_effect=oauth.KeyringError("no backend")):
            res = oauth.save_credentials_to_vault("tok-456")
            cred = Path(res["plaintext_fallback_file"])
            self.assertEqual(json.loads(cred.read_text(encoding="utf-8"))["access_token"], "tok-456")
            if os.name == "posix":
                self.assertEqual(cred.stat().st_mode & 0o777, 0o600)

    def test_status_never_reveals_token(self) -> None:
        with mock.patch.dict(os.environ, {"SOUNDBOOST_ACCESS_TOKEN": "secret-token-value-123456"}):
            status = oauth.get_vault_status()
        self.assertNotIn("secret", json.dumps(status))
        self.assertEqual(status["token_source"], "environment")

    def test_login_results_are_redacted(self) -> None:
        red = redact_login_result({"access": "a", "user": {"email": "e", "refresh_token": "r"}})
        self.assertNotIn('"a"', json.dumps(red))
        self.assertNotIn('"r"', json.dumps(red))
        self.assertEqual(red["user"]["email"], "e")


class LoopbackHandoffTests(unittest.TestCase):
    def _start(self, port: int) -> tuple[threading.Thread, dict]:
        out: dict = {}

        def run() -> None:
            out.update(oauth.run_loopback_oauth_handoff(port=port, timeout_sec=5.0, open_browser=False))

        th = threading.Thread(target=run, daemon=True)
        th.start()
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.05)
        return th, out

    def _request(self, port: int, method: str, path: str, body: str = "", host: str | None = None) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        headers = {"Host": host or f"127.0.0.1:{port}"}
        if body:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body or None, headers=headers)
        status = conn.getresponse().status
        conn.close()
        return status

    def test_rejects_missing_state_and_foreign_host_then_accepts_valid_post(self) -> None:
        port = _free_port()
        with mock.patch.object(oauth, "save_credentials_to_vault", return_value={"saved_to_os_keyring": True}) as save:
            th, out = self._start(port)
            # Missing state must not be accepted (previously bypassed the CSRF check)
            self.assertEqual(self._request(port, "GET", "/auth/callback?session_token=evil"), 400)
            self.assertEqual(self._request(port, "POST", "/auth/callback", "session_token=evil"), 400)
            # DNS-rebinding style request with a foreign Host header
            self.assertEqual(self._request(port, "GET", "/", host="attacker.example"), 403)
            save.assert_not_called()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
            page = conn.getresponse().read().decode("utf-8")
            conn.close()
            state = page.split('name="state" value="', 1)[1].split('"', 1)[0]
            body = urllib.parse.urlencode({"state": state, "session_token": "good-token"})
            self.assertEqual(self._request(port, "POST", "/auth/callback", body), 200)
            th.join(timeout=5)
            save.assert_called_once()
            self.assertEqual(out["status"], "saved")
            self.assertNotIn("good-token", json.dumps(out))
            self.assertNotIn("code_verifier", out)


class InboundAccessTests(unittest.TestCase):
    def test_email_allowlist(self) -> None:
        with mock.patch.dict(os.environ, {"SOUNDBOOST_MCP_ALLOWED_EMAILS": "Me@Example.com"}):
            ok = SimpleNamespace(token=SimpleNamespace(claims={"email": "me@example.com", "email_verified": True}))
            other = SimpleNamespace(token=SimpleNamespace(claims={"email": "x@example.com"}))
            unverified = SimpleNamespace(token=SimpleNamespace(claims={"email": "me@example.com", "email_verified": False}))
            self.assertTrue(email_allowlist_check(ok))
            self.assertFalse(email_allowlist_check(other))
            self.assertFalse(email_allowlist_check(unverified))
            self.assertFalse(email_allowlist_check(SimpleNamespace(token=None)))

    def test_http_serve_refuses_unsafe_configurations(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SOUNDBOOST_")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                serve(transport="http", host="127.0.0.1")  # no allowed dirs
            with mock.patch.dict(os.environ, {"SOUNDBOOST_ALLOWED_DIRS": tempfile.gettempdir()}):
                with self.assertRaises(RuntimeError):
                    serve(transport="http", host="0.0.0.0")  # noqa: S104 - asserting it is refused


class EntitlementGateTests(unittest.TestCase):
    def test_verified_free_account_is_blocked_from_plus_features(self) -> None:
        client = SoundBoostClient(access_token="t")
        with mock.patch.object(client, "get_billing_info", return_value={"plan": "free"}):
            with self.assertRaises(SoundBoostPayGateError):
                client.require_feature_entitlement("studio_hifi_plus_stems_unlocked")

    def test_unknown_tier_defers_to_backend(self) -> None:
        import httpx

        client = SoundBoostClient(access_token="t")
        with mock.patch.object(client, "get_billing_info", side_effect=httpx.ConnectError("down")):
            ent = client.require_feature_entitlement("studio_hifi_plus_stems_unlocked")
        self.assertEqual(ent["tier_source"], "unknown")


if __name__ == "__main__":
    unittest.main()
