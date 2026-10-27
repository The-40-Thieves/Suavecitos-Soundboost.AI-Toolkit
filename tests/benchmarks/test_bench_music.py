"""CodSpeed benchmarks for chord, grid, lead-sheet, tagging and REAPER helpers (pure Python paths)."""

from __future__ import annotations

from soundboost_toolkit.dsp import (
    build_lead_sheet_rows,
    get_chord_voicing_diagram,
    quantize_loop_to_bar_beat_sixteenth,
    render_lead_sheet_html,
    snap_timestamp_to_synchronized_beat,
    translate_prompt_to_18band_eq,
    transpose_chord_symbol,
)
from soundboost_toolkit.reaper_integration import build_osc_packet, build_rfxchain_content
from soundboost_toolkit.tagging import normalize_and_validate_isrc

PROGRESSION = ["Cm7", "Fm7", "Abmaj7", "G7b9/B", "Ebmaj7/G", "Dm7b5", "Bbsus4", "C#dim7", "F#m11", "Gadd9"] * 20


def _song_fixture(n_lines: int = 80) -> tuple[list[dict], list[dict]]:
    lyrics: list[dict] = []
    chords: list[dict] = []
    t = 0.5
    for i in range(n_lines):
        start, end = t, t + 2.0
        lyrics.append({"start": start, "end": end, "text": f"Lyric line number {i}"})
        chords.append({"start": start, "end": start + 1.0, "chord": PROGRESSION[i % 10]})
        chords.append({"start": start + 1.0, "end": end, "chord": PROGRESSION[(i + 3) % 10]})
        # Every fourth line is followed by an instrumental break of > 3 s.
        if i % 4 == 3:
            chords.append({"start": end + 0.5, "end": end + 2.0, "chord": PROGRESSION[(i + 5) % 10]})
            t = end + 4.0
        else:
            t = end + 0.25
    return lyrics, chords


def test_transpose_progression(benchmark) -> None:
    def run() -> list[str]:
        return [transpose_chord_symbol(c, -2, prefer_flats=True) for c in PROGRESSION]

    out = benchmark(run)
    assert out[0] == "Bbm7"


def test_snap_and_quantize_grid(benchmark) -> None:
    beats = [i * 0.5 for i in range(2000)]
    stamps = [i * 0.4987 + 0.011 for i in range(1000)]

    def run() -> tuple[list[float], list[dict]]:
        snapped = [snap_timestamp_to_synchronized_beat(s, beats) for s in stamps]
        loops = [quantize_loop_to_bar_beat_sixteenth(s, s + 7.56, bpm=128.0) for s in stamps[:200]]
        return snapped, loops

    snapped, loops = benchmark(run)
    assert len(snapped) == 1000
    assert len(loops) == 200


def test_build_and_render_lead_sheet(benchmark) -> None:
    lyrics, chords = _song_fixture()

    def run() -> str:
        rows = build_lead_sheet_rows(lyrics, chords, semitone_shift=2)
        return render_lead_sheet_html("Benchmark Song", rows, key_signature="Dm", bpm=124)

    html_out = benchmark(run)
    assert "Benchmark Song" in html_out


def test_chord_voicing_diagrams(benchmark) -> None:
    chords = ["C", "Cmaj7", "Am", "Am7", "F", "G", "G7", "Dm", "Em", "E7"]

    def run() -> list[dict]:
        return [
            get_chord_voicing_diagram(c, instrument=inst) for inst in ("guitar", "ukulele", "piano") for c in chords
        ]

    out = benchmark(run)
    assert len(out) == 30


def test_translate_prompt_to_eq(benchmark) -> None:
    prompt = "warm 808 sub bass, clean muddy low-mids, airy wide top end, loud club master with punchy drums"
    out = benchmark(translate_prompt_to_18band_eq, prompt, "hip_hop")
    assert len(out["eq_18band_db"]) == 18


def test_isrc_validation(benchmark) -> None:
    codes = [f"us-abc-26-{i:05d}" for i in range(500)]

    def run() -> list[str]:
        return [normalize_and_validate_isrc(c) for c in codes]

    out = benchmark(run)
    assert out[0] == "USABC2600000"


def test_reaper_osc_and_rfxchain(benchmark) -> None:
    def run() -> tuple[list[bytes], str]:
        packets = [build_osc_packet("/track/1/volume", float(i) / 100.0, "label") for i in range(200)]
        chain = build_rfxchain_content("pop", stereo_width_pct=115.0, makeup_drive_db=2.0, ceiling_dbtp=-1.0)
        return packets, chain

    packets, chain = benchmark(run)
    assert len(packets) == 200
    assert "SoundBoost_18Band_Mastering_EQ_Monitor.jsfx" in chain
