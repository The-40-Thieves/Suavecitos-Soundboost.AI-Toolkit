"""Unit and integration verification tests for Suavecito's Soundboost.AI Toolkit."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

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
    quantize_loop_to_bar_beat_sixteenth,
    render_18band_master,
    render_lead_sheet_html,
    snap_timestamp_to_synchronized_beat,
    transpose_chord_symbol,
)
from soundboost_toolkit.mcp_server import mcp
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
                self.assertEqual(len(tools), 15)
                tool_map = {t.name: t for t in tools}

                # Verify ToolAnnotations on all tools
                for t in tools:
                    self.assertIsNotNone(t.annotations, f"Missing annotations on {t.name}")
                    self.assertTrue(bool(t.annotations.title), f"Missing title on {t.name}")

                self.assertTrue(tool_map["audit_audio_loudness_and_release_readiness"].annotations.read_only_hint)
                self.assertFalse(tool_map["audit_audio_loudness_and_release_readiness"].annotations.open_world_hint)
                self.assertTrue(tool_map["soundboost_upload_and_master"].annotations.open_world_hint)

                # Verify MCP Resources & Prompts
                resources = await client.list_resources()
                uris = {str(r.uri) for r in resources}
                self.assertIn("soundboost://specs/capabilities", uris)
                self.assertIn("soundboost://specs/eq-presets", uris)
                self.assertIn("soundboost://specs/reaper-jsfx", uris)

                prompts = await client.list_prompts()
                prompt_names = {p.name for p in prompts}
                self.assertIn("distrokid_release_mastering_workflow", prompt_names)

                # Verify calling a tool through the MCP protocol
                kat_res = await client.call_tool("soundboost_cipher_tool", {"action": "kat"})
                self.assertFalse(kat_res.is_error)

        asyncio.run(_verify_mcp())


if __name__ == "__main__":
    unittest.main()
