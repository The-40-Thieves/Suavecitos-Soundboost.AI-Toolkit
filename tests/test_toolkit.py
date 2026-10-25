"""Unit and integration verification tests for Suavecito's Soundboost.AI Toolkit."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from soundboost_toolkit.client import (
    SoundBoostClient,
    SoundBoostPayGateError,
    resolve_account_entitlements,
)
from soundboost_toolkit.constants import (
    ACCOUNT_TIER_ENTITLEMENTS,
    compute_mv_spending_limit,
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
from soundboost_toolkit.reaper_integration import (
    ReaperLiveBridge,
    build_osc_packet,
    build_reaper_to_shoots_storyboard,
    build_rfxchain_content,
    generate_soundboost_rpp_project,
    inspect_reaper_environment,
    install_reaper_assets,
    render_reaper_video_pipeline,
)
from soundboost_toolkit.tagging import (
    normalize_and_validate_isrc,
    read_audio_tags,
    write_audio_tags,
)


class SoundBoostToolkitTests(unittest.TestCase):
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
                # 1. Verify default Progressive Discovery mode starts with only the 5 Pinned Entry Tools
                await client.call_tool("soundboost_discover_tools", {"action": "reset"})
                initial_tools = await client.list_tools()
                self.assertEqual(len(initial_tools), 5)
                initial_names = {t.name for t in initial_tools}
                self.assertEqual(
                    initial_names,
                    {
                        "soundboost_discover_tools",
                        "audit_audio_loudness_and_release_readiness",
                        "soundboost_auth_status",
                        "soundboost_oauth_and_vault",
                        "soundboost_account_profile_and_billing",
                    },
                )

                # 2. Verify BM25 Search discovers and auto-unlocks matching tools
                search_res = await client.call_tool(
                    "soundboost_discover_tools",
                    {"action": "search", "query": "split 6 stems and 5-piece drum kit", "max_results": 2},
                )
                self.assertFalse(search_res.is_error)
                after_search_tools = await client.list_tools()
                self.assertIn("soundboost_studio_stems_and_drums", {t.name for t in after_search_tools})

                # 3. Verify Pack Activation ('mastering') and Account-Tier Sync ('free' vs 'unlimited' vs 'studio')
                await client.call_tool("soundboost_discover_tools", {"action": "reset"})
                await client.call_tool(
                    "soundboost_discover_tools",
                    {"action": "activate_pack", "pack": "mastering"},
                )
                mastering_pack_tools = await client.list_tools()
                self.assertEqual(len(mastering_pack_tools), 9)  # 5 core + 4 mastering

                await client.call_tool(
                    "soundboost_discover_tools",
                    {"action": "sync_account_tier", "account_tier": "free"},
                )
                free_tier_tools = {t.name for t in await client.list_tools()}
                self.assertEqual(len(free_tier_tools), 19)
                self.assertNotIn("soundboost_upload_and_master", free_tier_tools)
                self.assertNotIn("soundboost_bulk_and_album_mastering", free_tier_tools)

                await client.call_tool(
                    "soundboost_discover_tools",
                    {"action": "sync_account_tier", "account_tier": "unlimited"},
                )
                unlim_tier_tools = {t.name for t in await client.list_tools()}
                self.assertEqual(len(unlim_tier_tools), 25)
                self.assertIn("soundboost_upload_and_master", unlim_tier_tools)
                self.assertNotIn("soundboost_bulk_and_album_mastering", unlim_tier_tools)

                # 4. Unlock all 26 tools ('studio' tier / pack='all') and verify annotations on every tool
                await client.call_tool(
                    "soundboost_discover_tools",
                    {"action": "activate_pack", "pack": "all"},
                )
                tools = await client.list_tools()
                self.assertEqual(len(tools), 26)
                tool_map = {t.name: t for t in tools}
                self.assertNotIn("render_local_18band_master", tool_map)

                # Verify ToolAnnotations on all 26 tools
                for t in tools:
                    self.assertIsNotNone(t.annotations, f"Missing annotations on {t.name}")
                    self.assertTrue(bool(t.annotations.title), f"Missing title on {t.name}")

                self.assertTrue(tool_map["soundboost_discover_tools"].annotations.read_only_hint)
                self.assertTrue(tool_map["audit_audio_loudness_and_release_readiness"].annotations.read_only_hint)
                self.assertFalse(tool_map["audit_audio_loudness_and_release_readiness"].annotations.open_world_hint)
                self.assertTrue(tool_map["soundboost_upload_and_master"].annotations.open_world_hint)
                self.assertTrue(tool_map["soundboost_oauth_and_vault"].annotations.open_world_hint)
                self.assertTrue(tool_map["soundboost_bulk_and_album_mastering"].annotations.open_world_hint)
                self.assertTrue(tool_map["soundboost_youtube_and_soundcloud_ingest"].annotations.open_world_hint)
                self.assertTrue(tool_map["soundboost_music_video_and_shoots_director"].annotations.open_world_hint)
                self.assertTrue(tool_map["studio_practice_voicings_and_cloud_shifter"].annotations.open_world_hint)

                # Every tool that can delete data, change credentials, or touch billing is flagged destructive
                for name in (
                    "soundboost_oauth_and_vault",
                    "soundboost_account_and_subscription_changes",
                    "soundboost_delete_tracks",
                    "soundboost_remove_smart_link",
                    "write_release_metadata_tags",
                    "install_reaper_jsfx_and_scripts",
                ):
                    self.assertTrue(tool_map[name].annotations.destructive_hint, name)
                for name in ("soundboost_auth_status", "soundboost_account_profile_and_billing"):
                    self.assertTrue(tool_map[name].annotations.read_only_hint, name)
                # Read-only tools must not expose any mutating action
                billing_doc = tool_map["soundboost_account_profile_and_billing"].description or ""
                for forbidden in ("pause_subscription", "checkout_session", "update_profile"):
                    self.assertNotIn(f"`{forbidden}`:", billing_doc)

                # Verify MCP Resources & Prompts
                resources = await client.list_resources()
                uris = {str(r.uri) for r in resources}
                self.assertIn("soundboost://specs/tool-packs", uris)
                self.assertIn("soundboost://specs/capabilities", uris)
                self.assertIn("soundboost://specs/eq-presets", uris)
                self.assertIn("soundboost://specs/reaper-jsfx", uris)

                prompts = await client.list_prompts()
                prompt_names = {p.name for p in prompts}
                self.assertIn("distrokid_release_mastering_workflow", prompt_names)

                # Reset back to 5 core tools and verify hidden tools remain directly callable by name
                await client.call_tool("soundboost_discover_tools", {"action": "reset"})
                self.assertEqual(len(await client.list_tools()), 5)

                oauth_res = await client.call_tool("soundboost_auth_status", {"action": "pkce_url"})
                self.assertFalse(oauth_res.is_error)

                voicings_res = await client.call_tool(
                    "studio_practice_voicings_and_cloud_shifter",
                    {"action": "chord_voicings", "chords": ["Cm", "G7"], "instrument": "guitar"},
                )
                self.assertFalse(voicings_res.is_error)

        asyncio.run(_verify_mcp())

    def test_pay_gate_enforcement_on_paid_and_studio_endpoints(self) -> None:
        # Unauthenticated client (no token) must raise SoundBoostPayGateError on paid/studio endpoints
        unauth_client = SoundBoostClient(access_token="")
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

        with self.assertRaises(NotImplementedError):
            unauth_client.build_studio_url(42)
        with self.assertRaises(NotImplementedError):
            unauth_client.decrypt_studio_track_param("param")

    def test_account_tier_entitlements_and_feature_unlocking(self) -> None:
        self.assertEqual(len(ACCOUNT_TIER_ENTITLEMENTS), 8)

        # 1. Free tier: unlocks standard 2/4-stem, voice_clean, chords/lyrics, 30s preview; blocks full download, bulk mastering, hifi 6-stem
        free_ent = resolve_account_entitlements({"tier": "free", "credits": 0})
        self.assertEqual(free_ent["resolved_tier"], "free")
        self.assertTrue(free_ent["entitlements"]["studio_standard_stems_unlocked"])
        self.assertFalse(free_ent["entitlements"]["mastering_full_length_download"])
        self.assertFalse(free_ent["entitlements"]["bulk_mastering_unlocked"])
        self.assertFalse(free_ent["entitlements"]["studio_hifi_plus_stems_unlocked"])

        # 2. Pay-As-You-Go with credits > 0: unlocks full download and pitch/time stem export, blocks bulk mastering & hifi 6-stem
        payg_ent = resolve_account_entitlements({"tier": "free", "credits": 3})
        self.assertEqual(payg_ent["resolved_tier"], "pay_as_you_go")
        self.assertTrue(payg_ent["entitlements"]["mastering_full_length_download"])
        self.assertTrue(payg_ent["entitlements"]["studio_pitch_time_export_unlocked"])
        self.assertFalse(payg_ent["entitlements"]["bulk_mastering_unlocked"])

        # 3. Unlimited: unlocks unlimited downloads & Shoots AI Clones, still blocks Bulk Mastering & Studio Hi-Fi Plus Stems
        unlim_ent = resolve_account_entitlements({"plan": "unlimited_monthly", "subscription_status": "active"})
        self.assertEqual(unlim_ent["resolved_tier"], "unlimited")
        self.assertTrue(unlim_ent["entitlements"]["mastering_unlimited_revisions"])
        self.assertTrue(unlim_ent["entitlements"]["shoots_ai_clones_unlocked"])
        self.assertFalse(unlim_ent["entitlements"]["bulk_mastering_unlocked"])
        self.assertFalse(unlim_ent["entitlements"]["studio_hifi_plus_stems_unlocked"])

        # 4. Unlimited Plus: unlocks Bulk Mastering, Hi-Fi 4/6-stem, 5-Piece Drum Kit, Auto-QC & Deep Research
        plus_ent = resolve_account_entitlements({"plan": "unlimited_plus_annual", "subscription_status": "active"})
        self.assertEqual(plus_ent["resolved_tier"], "unlimited_plus")
        self.assertTrue(plus_ent["entitlements"]["bulk_mastering_unlocked"])
        self.assertTrue(plus_ent["entitlements"]["studio_hifi_plus_stems_unlocked"])
        self.assertTrue(plus_ent["entitlements"]["studio_5piece_drum_kit_unlocked"])
        self.assertTrue(plus_ent["entitlements"]["mv_auto_qc_unlocked"])
        self.assertTrue(plus_ent["entitlements"]["mv_deep_research_unlocked"])

        # 5. Director vs Studio tiers: character ref caps (2 vs 3) and Auto-QC vs Deep Research
        dir_ent = resolve_account_entitlements({"plan": "director_monthly", "subscription_status": "active"})
        self.assertEqual(dir_ent["resolved_tier"], "director")
        self.assertEqual(dir_ent["entitlements"]["mv_character_ref_cap"], 2)
        self.assertTrue(dir_ent["entitlements"]["mv_auto_qc_unlocked"])
        self.assertFalse(dir_ent["entitlements"]["mv_deep_research_unlocked"])

        studio_ent = resolve_account_entitlements({"plan": "studio_annual", "subscription_status": "active"})
        self.assertEqual(studio_ent["resolved_tier"], "studio")
        self.assertEqual(studio_ent["entitlements"]["mv_character_ref_cap"], 3)
        self.assertTrue(studio_ent["entitlements"]["mv_auto_qc_unlocked"])
        self.assertTrue(studio_ent["entitlements"]["mv_deep_research_unlocked"])

        # 6. Frozen account downgrades to free entitlements
        frozen_ent = resolve_account_entitlements({"plan": "studio_annual", "frozen": True, "credits": 50})
        self.assertEqual(frozen_ent["resolved_tier"], "free")
        self.assertFalse(frozen_ent["entitlements"]["mastering_full_length_download"])

        # 7. Verify SoundBoostClient enforces exact account tier before calling endpoints
        client = SoundBoostClient(access_token="valid-jwt-token")
        client.cached_entitlements = unlim_ent  # Authenticated on 'unlimited' (not 'unlimited_plus')
        with self.assertRaises(SoundBoostPayGateError):
            client.start_studio_stem_split(42, "hifi_6stem")
        with self.assertRaises(SoundBoostPayGateError):
            client.start_studio_drum_split(42)
        with self.assertRaises(SoundBoostPayGateError):
            client.start_bulk_mastering([1, 2])

        # Upgrading the client's resolved account level to 'unlimited_plus' unlocks those exact features
        client.cached_entitlements = plus_ent
        unlocked = client.require_feature_entitlement("studio_hifi_plus_stems_unlocked")
        self.assertEqual(unlocked["resolved_tier"], "unlimited_plus")

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
            self.assertTrue(album_res["all_release_ready"])

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
        signing_key = "k" * 48
        with self.assertRaises(ValueError):
            build_mcp_oauth_provider("google", client_id="test-client-id.apps.googleusercontent.com")
        with self.assertRaises(ValueError):
            build_mcp_oauth_provider("jwt")
        google_prov = build_mcp_oauth_provider(
            "google",
            base_url="http://127.0.0.1:8000",
            client_id="test-client-id.apps.googleusercontent.com",
            jwt_signing_key=signing_key,
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
            jwt_signing_key=signing_key,
        )
        self.assertIsNotNone(proxy_prov)
        remote_prov = build_mcp_oauth_provider(
            "remote",
            base_url="http://127.0.0.1:8000",
            client_id="test-client-id",
        )
        self.assertIsNotNone(remote_prov)

    def test_reaper_deep_integration_osc_rfxchain_and_rpp_generator(self) -> None:
        # 1. Binary OSC packet encoding
        pkt = build_osc_packet("/tempo/raw", 128.0)
        self.assertTrue(pkt.startswith(b"/tempo/raw\x00\x00,f\x00\x00"))
        self.assertEqual(len(pkt) % 4, 0)

        # 2. .RfxChain generator
        chain_text = build_rfxchain_content("pop")
        self.assertIn("SoundBoost_18Band_Mastering_EQ_Monitor.jsfx", chain_text)
        self.assertIn("analysis/loudness_meter", chain_text)

        # 3. Full sandboxed REAPER asset installer, INI bridge & .RPP generator
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_ini = tmp_path / "REAPER.ini"
            fake_ini.write_text("[reaper]\nmixeruiflags=11\n[verchk]\nlastt=1\n", encoding="utf-8")

            fake_ini.write_text(
                "[reaper]\nmixeruiflags=11\ncsurf_cnt=1\ncsurf_0=MCU 0 1 2 0 0\n[verchk]\nlastt=1\n",
                encoding="utf-8",
            )
            # INI-bridge and startup daemon are opt-in; this test exercises them explicitly.
            inst = install_reaper_assets(
                tmp_path,
                configure_ini_bridge=True,
                enable_startup_daemon=True,
                enable_osc_surface=True,
                enable_http_web_surface=True,
            )
            self.assertEqual(inst["status"], "installed")
            self.assertEqual(inst["ini_bridge"]["preserved_control_surfaces"], 1)
            ini_text = fake_ini.read_text(encoding="utf-8")
            self.assertIn("csurf_0=MCU 0 1 2 0 0", ini_text)
            self.assertIn("csurf_cnt=3", ini_text)
            # Re-running replaces only the surfaces the toolkit owns
            install_reaper_assets(
                tmp_path, configure_ini_bridge=True, enable_osc_surface=True, enable_http_web_surface=True
            )
            self.assertIn("csurf_cnt=3", fake_ini.read_text(encoding="utf-8"))
            self.assertEqual(inst["installed_fxchains_count"], 12)
            self.assertTrue((tmp_path / "Scripts" / "__startup.lua").exists())
            self.assertTrue((tmp_path / "Scripts" / "Suavecito" / "Suavecito_Live_Bridge_Daemon.lua").exists())

            env_info = inspect_reaper_environment(tmp_path)
            self.assertTrue(env_info["ini_bridge_config"]["reascript_enabled"])
            self.assertTrue(env_info["ini_bridge_config"]["http_csurf_configured"])
            self.assertTrue(env_info["ini_bridge_config"]["osc_csurf_configured"])
            self.assertTrue(env_info["suavecito_assets"]["jsfx_gmem_enabled"])
            self.assertTrue(env_info["suavecito_assets"]["startup_hook_installed"])

            # Create fake stem WAVs (Kick, Snare, Vocals, Bass) and generate .RPP session
            stems_dir = tmp_path / "stems"
            stems_dir.mkdir(parents=True, exist_ok=True)
            sr = 48000
            t = np.linspace(0.0, 0.25, sr // 4, endpoint=False)
            tone = (0.98 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)
            stereo = np.column_stack([tone, tone])
            for name in ("01_Kick.wav", "02_Snare.wav", "03_Vocals.wav", "04_Bass.wav"):
                sf.write(str(stems_dir / name), stereo, sr, subtype="PCM_24")

            rpp_out = tmp_path / "Session.RPP"
            rpp_res = generate_soundboost_rpp_project(
                output_rpp_path=rpp_out,
                stems_folder=stems_dir,
                bpm=128.0,
                preset="hip_hop",
                prompt="warm sub bass, crisp vocal presence, wide stereo",
                isrc="US-S1Z-26-00001",
                title="Midnight Hustle",
                artist="Suavecito",
                album="40 Thieves Vol. 1",
                chords=[{"chord": "Cm", "start_sec": 0.0, "end_sec": 2.0}],
                lyrics=[{"text": "Step into the light", "start_sec": 0.5}],
                audit_audio_path=stems_dir / "01_Kick.wav",
            )
            self.assertTrue(rpp_out.exists())
            self.assertGreaterEqual(rpp_res["tracks_created"], 7)
            rpp_text = rpp_out.read_text(encoding="utf-8")
            self.assertIn("DRUM KIT BUS", rpp_text)
            self.assertIn("VOCAL BUS", rpp_text)
            self.assertIn("INSTRUMENT BUS", rpp_text)
            self.assertIn('JS "midi/drumtrigger"', rpp_text)
            self.assertIn("TAG ID3:TSRC US-S1Z-26-00001", rpp_text)
            self.assertIn("CHORD: Cm", rpp_text)
            self.assertIn("LYRIC: Step into the light", rpp_text)

            # Verify LiveBridge IPC inbox dispatch
            bridge = ReaperLiveBridge(reaper_resource_dir=tmp_path)
            eq_push = bridge.push_18band_eq_to_reaper(prompt="warm low end and airy top")
            self.assertEqual(len(eq_push["eq_18band_db"]), 18)
            self.assertTrue(bridge.inbox_path.exists())

            # 4. Verify REAPER -> Shoots (h3-max) Beat-Locked Storyboard Generator
            sb_manifest = build_reaper_to_shoots_storyboard(
                audio_path=stems_dir / "01_Kick.wav",
                bpm=128.0,
                bars_per_scene=1,
                chords=[{"chord": "Cm", "start_sec": 0.0, "end_sec": 2.0}],
                lyrics=[{"text": "Step into the light", "start_sec": 0.1}],
                plan_tier="director",
            )
            self.assertEqual(sb_manifest["engine"], "h3-max")
            self.assertGreaterEqual(sb_manifest["scene_count"], 1)
            self.assertGreater(sb_manifest["spending_limit_credits"], sb_manifest["quoted_credits"])

            # 5. Verify Local Audio-Reactive MP4 Video Pipeline + Companion REAPER .RPP
            mp4_out = tmp_path / "Canvas_9x16.mp4"
            video_rpp_out = tmp_path / "Video_Session.RPP"
            vid_res = render_reaper_video_pipeline(
                audio_path=stems_dir / "01_Kick.wav",
                output_mp4_path=mp4_out,
                mode="spotify_canvas_9x16",
                fps=15.0,
                max_duration_sec=0.25,
                preset="hip_hop",
                title="Midnight Hustle",
                artist="Suavecito",
                isrc="US-S1Z-26-00001",
                chords=[{"chord": "Cm", "start_sec": 0.0, "end_sec": 2.0}],
                lyrics=[{"text": "Step into the light", "start_sec": 0.0, "end_sec": 0.25}],
                output_rpp_path=video_rpp_out,
                bpm=128.0,
            )
            self.assertTrue(mp4_out.exists())
            self.assertGreater(vid_res["mp4_size_bytes"], 1000)
            self.assertTrue(video_rpp_out.exists())
            video_rpp_text = video_rpp_out.read_text(encoding="utf-8")
            self.assertIn('<VIDEO_EFFECT "Video processor" ""', video_rpp_text)
            self.assertIn("<SOURCE VIDEO", video_rpp_text)
            self.assertIn('JS "misc/video_sample_peeker"', video_rpp_text)

            # Verify live video scene insertion via ReaperLiveBridge
            v_ins = bridge.insert_video_scene_to_reaper(mp4_out, pos_sec=0.0)
            self.assertEqual(v_ins["command"], "insert_video_item")


if __name__ == "__main__":
    unittest.main()
