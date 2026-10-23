"""Unit and integration verification tests for Suavecito's Soundboost.AI Toolkit."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from soundboost_toolkit.client import SoundBoostClient, SoundBoostPayGateError
from soundboost_toolkit.constants import PRODUCTION_KAT_HASH, PRODUCTION_KAT_ID
from soundboost_toolkit.crypto import (
    compute_mv_spending_limit,
    decrypt_cookie_value,
    decrypt_param_id,
    decrypt_ui_token,
    encrypt_cookie_value,
    encrypt_param_id,
    generate_ui_token,
    verify_param_cipher_kat,
)
from soundboost_toolkit.dsp import (
    analyze_audio_file,
    build_lead_sheet_rows,
    classify_pitch_shift_zone,
    get_chord_voicing_diagram,
    inspect_mp3_gapless_headers,
    master_album_folder,
    quantize_loop_to_bar_beat_sixteenth,
    render_18band_master,
    render_lead_sheet_html,
    render_local_pitch_and_time_stretch,
    snap_timestamp_to_synchronized_beat,
    translate_prompt_to_18band_eq,
    transpose_chord_symbol,
)
from soundboost_toolkit.mcp_server import mcp
from soundboost_toolkit.oauth import (
    build_mcp_oauth_provider,
    generate_google_pkce_challenge,
    get_vault_status,
)
from soundboost_toolkit.tagging import (
    normalize_and_validate_isrc,
    read_audio_tags,
    write_audio_tags,
)


class SoundBoostToolkitTests(unittest.TestCase):
    def test_param_cipher_kat(self) -> None:
        self.assertTrue(verify_param_cipher_kat())
        self.assertEqual(encrypt_param_id(PRODUCTION_KAT_ID), PRODUCTION_KAT_HASH)
        self.assertEqual(decrypt_param_id(PRODUCTION_KAT_HASH), PRODUCTION_KAT_ID)
        for tid in (42, 1337, 999999):
            enc = encrypt_param_id(tid)
            self.assertEqual(decrypt_param_id(enc), tid)

    def test_ui_token_and_cookie_ciphers(self) -> None:
        tok = generate_ui_token(timestamp_iso="2026-10-23T10:00:00.000Z")
        dec = decrypt_ui_token(tok)
        self.assertTrue(dec.endswith("_2026-10-23T10:00:00.000Z"))

        cookie_enc = encrypt_cookie_value({"session_id": "sample_session_123"})
        cookie_dec = decrypt_cookie_value(cookie_enc)
        self.assertEqual(cookie_dec, {"session_id": "sample_session_123"})

    def test_mv_spending_limit_formula(self) -> None:
        self.assertEqual(compute_mv_spending_limit(0), 0)
        self.assertEqual(compute_mv_spending_limit(20), 23)
        self.assertEqual(compute_mv_spending_limit(100), 115)

    def test_chord_transposition_and_grid_quantizer(self) -> None:
        self.assertEqual(transpose_chord_symbol("Cmaj7/G", 2), "Dmaj7/A")
        self.assertEqual(transpose_chord_symbol("Cm7", -2, prefer_flats=True), "Bbm7")
        self.assertEqual(classify_pitch_shift_zone(3)["zone"], "Natural zone")
        self.assertEqual(classify_pitch_shift_zone(-4)["zone"], "Creative zone")

        snapped = snap_timestamp_to_synchronized_beat(2.018, [0.5, 1.0, 1.5, 2.0, 2.5])
        self.assertAlmostEqual(snapped, 2.0, places=4)

        loop_q = quantize_loop_to_bar_beat_sixteenth(0.0, 2.0, bpm=120.0)
        self.assertEqual(loop_q["start_bar_beat_16th"], "1.1.1")
        self.assertEqual(loop_q["end_bar_beat_16th"], "2.1.1")

    def test_lead_sheet_builder_with_3s_chord_gaps(self) -> None:
        lyrics = [
            {"start": 0.5, "end": 2.0, "text": "First vocal line"},
            {"start": 6.5, "end": 8.5, "text": "Second line after solo"},
        ]
        chords = [
            {"start": 0.5, "end": 2.0, "chord": "Am"},
            {"start": 3.0, "end": 4.5, "chord": "Fmaj7"},
            {"start": 4.5, "end": 6.0, "chord": "G7"},
            {"start": 6.5, "end": 8.5, "chord": "Cmaj7"},
        ]
        rows = build_lead_sheet_rows(lyrics, chords, semitone_shift=2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["type"], "lyrics")
        self.assertEqual(rows[0]["chords"], ["Bm"])
        self.assertEqual(rows[1]["type"], "chords")
        self.assertEqual(rows[1]["chords"], ["Gmaj7", "A7"])
        html_out = render_lead_sheet_html("Test Song", rows, key_signature="Bm", bpm=120)
        self.assertIn("[Instrumental] Gmaj7   A7", html_out)

    def test_dsp_audit_mastering_and_tagging(self) -> None:
        sr = 48000
        duration = 4.0
        t = np.linspace(0.0, duration, int(sr * duration), endpoint=False)
        left = 0.25 * np.sin(2.0 * math.pi * 440.0 * t)
        right = 0.25 * np.sin(2.0 * math.pi * 440.0 * t + 0.1)
        # Inject an impulse peak at 2.35s to verify 100ms True-Peak Binary Search
        peak_idx = int(2.35 * sr)
        left[peak_idx] = 0.85
        stereo = np.column_stack([left, right])

        with tempfile.TemporaryDirectory() as tmp:
            in_wav = Path(tmp) / "input.wav"
            out_wav = Path(tmp) / "mastered.wav"
            sf.write(str(in_wav), stereo, sr, subtype="PCM_24")

            audit = analyze_audio_file(in_wav)
            self.assertAlmostEqual(audit.true_peak_time_sec, 2.35, delta=0.11)
            self.assertTrue(-25.0 <= audit.integrated_lufs <= -10.0)

            master_res = render_18band_master(
                in_wav,
                out_wav,
                preset="pop",
                stereo_width_pct=110.0,
                target_lufs=-14.0,
                true_peak_ceiling_dbtp=-1.0,
                bit_depth=24,
            )
            post_tp = master_res["post_master_audit"]["true_peak_dbtp"]
            self.assertLessEqual(post_tp, -0.85)

            isrc = normalize_and_validate_isrc("us-abc-26-12345")
            self.assertEqual(isrc, "USABC2612345")
            tags = write_audio_tags(
                out_wav,
                title="Test Master",
                artist="Suavecito",
                album="Toolkit Suite",
                year="2026",
                bpm="120",
                initial_key="Am",
                isrc=isrc,
            )
            self.assertEqual(tags["title"], "Test Master")
            self.assertEqual(tags["isrc"], "USABC2612345")
            read_back = read_audio_tags(out_wav)
            self.assertEqual(read_back["artist"], "Suavecito")

    def test_mcp_annotations_tasks_resources_and_prompts(self) -> None:
        import asyncio

        from fastmcp import Client

        async def _verify_mcp() -> None:
            async with Client(mcp) as client:
                tools = await client.list_tools()
                self.assertEqual(len(tools), 22)
                tool_map = {t.name: t for t in tools}
                self.assertNotIn("render_local_18band_master", tool_map)

                # Verify ToolAnnotations on all tools
                for t in tools:
                    self.assertIsNotNone(t.annotations, f"Missing annotations on {t.name}")
                    self.assertTrue(bool(t.annotations.title), f"Missing title on {t.name}")

                self.assertTrue(tool_map["audit_audio_loudness_and_release_readiness"].annotations.read_only_hint)
                self.assertFalse(tool_map["audit_audio_loudness_and_release_readiness"].annotations.open_world_hint)
                self.assertTrue(tool_map["soundboost_upload_and_master"].annotations.open_world_hint)
                self.assertTrue(tool_map["soundboost_oauth_and_vault"].annotations.open_world_hint)
                self.assertTrue(tool_map["soundboost_bulk_and_album_mastering"].annotations.open_world_hint)
                self.assertTrue(tool_map["soundboost_youtube_and_soundcloud_ingest"].annotations.open_world_hint)
                self.assertTrue(tool_map["soundboost_music_video_and_shoots_director"].annotations.open_world_hint)
                self.assertTrue(tool_map["studio_practice_voicings_and_cloud_shifter"].annotations.open_world_hint)

                # Verify MCP Resources & Prompts
                resources = await client.list_resources()
                uris = {str(r.uri) for r in resources}
                self.assertIn("soundboost://specs/capabilities", uris)
                self.assertIn("soundboost://specs/eq-presets", uris)
                self.assertIn("soundboost://specs/reaper-jsfx", uris)

                prompts = await client.list_prompts()
                prompt_names = {p.name for p in prompts}
                self.assertIn("distrokid_release_mastering_workflow", prompt_names)

                # Verify calling tools through the MCP protocol
                kat_res = await client.call_tool("soundboost_cipher_tool", {"action": "kat"})
                self.assertFalse(kat_res.is_error)

                oauth_res = await client.call_tool("soundboost_oauth_and_vault", {"action": "pkce_url"})
                self.assertFalse(oauth_res.is_error)

                voicings_res = await client.call_tool(
                    "studio_practice_voicings_and_cloud_shifter",
                    {"action": "chord_voicings", "chords": ["Cm", "G7"], "instrument": "guitar"},
                )
                self.assertFalse(voicings_res.is_error)

        asyncio.run(_verify_mcp())

    def test_pay_gate_enforcement_on_paid_and_studio_endpoints(self) -> None:
        # Unauthenticated client (no token) must raise SoundBoostPayGateError on paid/studio endpoints
        unauth_client = SoundBoostClient(access_token="", encrypted_da_cookie="")
        unauth_client.access_token = None

        self.assertTrue(unauth_client._is_public_free_url("https://converta.soundboost.ai/api/v1/formats"))
        self.assertTrue(unauth_client._is_public_free_url("https://converta.soundboost.ai/api/v1/convert"))
        self.assertFalse(unauth_client._is_public_free_url("https://converta.soundboost.ai/api/v1/stem-export"))
        self.assertFalse(unauth_client._is_public_free_url("https://api.soundboost.ai/api/tracks/42/master/"))
        self.assertFalse(unauth_client._is_public_free_url("https://api.soundboost.ai/api/bulk-mastering/start/"))
        self.assertFalse(unauth_client._is_public_free_url("https://mastering.soundboost.ai/api/ai/prompt"))

        with self.assertRaises(SoundBoostPayGateError):
            unauth_client.start_mastering(42, mode="auto")
        with self.assertRaises(SoundBoostPayGateError):
            unauth_client.start_bulk_mastering([1, 2], mode="auto")
        with self.assertRaises(SoundBoostPayGateError):
            unauth_client.translate_ai_mastering_prompt("warm 808s")
        with self.assertRaises(SoundBoostPayGateError):
            unauth_client.start_studio_stem_split(42, "hifi_6stem")
        with self.assertRaises(SoundBoostPayGateError):
            unauth_client.request_converta_stem_export({"semitones": 2})

    def test_new_gap_clusters_voicings_shifter_album_and_mp3_inspector(self) -> None:
        # 1. Chord Voicing Diagrams (Module 534: Guitar, Ukulele, Piano)
        g_diag = get_chord_voicing_diagram("Cmaj7", instrument="guitar")
        self.assertEqual(g_diag["frets"], [-1, 3, 2, 0, 0, 0])
        u_diag = get_chord_voicing_diagram("C", instrument="ukulele")
        self.assertEqual(u_diag["frets"], [0, 0, 0, 3])
        p_diag = get_chord_voicing_diagram("Am7", instrument="piano")
        self.assertEqual(p_diag["pitch_classes"], ["A", "C", "E", "G"])

        # 2. Natural-language Prompt -> 18-Band EQ translator
        eq_trans = translate_prompt_to_18band_eq("warm 808 sub bass, clean muddy low-mids, airy wide top end")
        self.assertEqual(len(eq_trans["eq_18band_db"]), 18)
        self.assertGreater(eq_trans["eq_by_frequency"]["30Hz"], 0.0)
        self.assertLess(eq_trans["eq_by_frequency"]["200Hz"], 0.0)
        self.assertGreater(eq_trans["eq_by_frequency"]["15000Hz"], 0.0)
        self.assertGreater(eq_trans["recommended_stereo_width_pct"], 100.0)

        # 3. Local Pitch-Shift & Time-Stretch + Album Folder Mastering + Gapless MP3 Inspector
        sr = 48000
        t = np.linspace(0.0, 1.5, int(sr * 1.5), endpoint=False)
        tone = 0.2 * np.sin(2.0 * math.pi * 440.0 * t)
        stereo = np.column_stack([tone, tone])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            album_in = tmp_path / "album_in"
            album_out = tmp_path / "album_out"
            album_in.mkdir()

            track1 = album_in / "01_Intro.wav"
            track2 = album_in / "02_Verse.wav"
            sf.write(str(track1), stereo, sr, subtype="PCM_24")
            sf.write(str(track2), stereo * 0.8, sr, subtype="PCM_24")

            # Batch Album Folder Mastering
            album_res = master_album_folder(
                input_dir=album_in,
                output_dir=album_out,
                preset="pop",
                target_lufs=-14.0,
                album_title="Test EP",
                artist_name="Suavecito",
            )
            self.assertEqual(album_res["total_tracks"], 2)
            self.assertTrue(album_res["all_distrokid_ready"])

            # Local Pitch Shift (+2 semitones) & Time Stretch (1.25x faster)
            shifted_wav = tmp_path / "shifted.wav"
            shift_res = render_local_pitch_and_time_stretch(
                input_path=track1,
                output_path=shifted_wav,
                semitones=2.0,
                speed_factor=1.25,
                bit_depth=24,
            )
            self.assertAlmostEqual(shift_res["output_duration_sec"], 1.2, delta=0.15)
            self.assertEqual(shift_res["pitch_zone"]["zone"], "Natural zone")

            # Synthetic MPEG-1 Layer III frame + Info + LAME3.100 gapless header
            mp3_file = tmp_path / "gapless.mp3"
            # 0xFFFB9064 -> MPEG-1 Layer III, 128 kbps, 44100 Hz, Joint Stereo
            frame_header = bytes([0xFF, 0xFB, 0x90, 0x64])
            xing_pad = b"\x00" * 32
            info_tag = b"Info" + (0x01).to_bytes(4, "big") + (42).to_bytes(4, "big")
            # LAME3.100 + 12 bytes + 3-byte delay/padding (576 = 0x240, 1152 = 0x480 -> 0x24 0x04 0x80)
            lame_tag = b"LAME3.100" + (b"\x00" * 12) + bytes([0x24, 0x04, 0x80])
            mp3_bytes = frame_header + xing_pad + info_tag + lame_tag + (b"\x00" * 300)
            mp3_file.write_bytes(mp3_bytes)

            mp3_info = inspect_mp3_gapless_headers(mp3_file)
            self.assertEqual(mp3_info["mpeg_version"], "MPEG-1")
            self.assertEqual(mp3_info["sample_rate"], 44100)
            self.assertEqual(mp3_info["bitrate_kbps"], 128)
            self.assertTrue(mp3_info["has_gapless_xing_info_header"])
            self.assertEqual(mp3_info["lame_version"], "LAME3.100")
            self.assertEqual(mp3_info["encoder_delay_samples"], 576)
            self.assertEqual(mp3_info["encoder_padding_samples"], 1152)
            self.assertEqual(mp3_info["total_priming_samples"], 1105)


    def test_oauth_pkce_and_mcp_oauth21_providers(self) -> None:
        pkce = generate_google_pkce_challenge()
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", pkce.authorize_url)
        self.assertIn("code_challenge_method=S256", pkce.authorize_url)
        self.assertTrue(len(pkce.code_verifier) >= 43)

        status = get_vault_status()
        self.assertIn("keyring_backend", status)

        self.assertIsNone(build_mcp_oauth_provider("none"))
        google_prov = build_mcp_oauth_provider(
            "google",
            base_url="http://127.0.0.1:8000",
            client_id="test-client-id.apps.googleusercontent.com",
        )
        self.assertIsNotNone(google_prov)
        jwt_prov = build_mcp_oauth_provider(
            "jwt",
            base_url="http://127.0.0.1:8000",
            client_id="test-client-id",
        )
        self.assertIsNotNone(jwt_prov)
        proxy_prov = build_mcp_oauth_provider(
            "oauth_proxy",
            base_url="http://127.0.0.1:8000",
            client_id="test-client-id",
        )
        self.assertIsNotNone(proxy_prov)
        remote_prov = build_mcp_oauth_provider(
            "remote",
            base_url="http://127.0.0.1:8000",
            client_id="test-client-id",
        )
        self.assertIsNotNone(remote_prov)


if __name__ == "__main__":
    unittest.main()
