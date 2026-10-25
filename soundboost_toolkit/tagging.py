"""ID3v2.3, FLAC VorbisComment, WAV ID3, and ISRC (`TSRC`) Metadata Tagging Engine.

Supports all tags exposed in SoundBoost's ID3 Tag Editor + DistroKid release specs:
- Title (`TIT2`), Artist (`TPE1`), Album (`TALB`), Year (`TYER`/`TDRC`), Track (`TRCK`)
- Genre (`TCON`), Comment (`COMM`), Unsynchronized Lyrics (`USLT`)
- BPM (`TBPM`), Musical Key (`TKEY`), Record Label / Publisher (`TPUB`)
- 12-Character ISRC (`TSRC`, validated against ISO 3901 `CC-XXX-YY-NNNNN`)
- Cover Artwork (`APIC`, front cover Type 3)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    TALB,
    TBPM,
    TCON,
    TIT2,
    TKEY,
    TPE1,
    TPUB,
    TRCK,
    TSRC,
    TYER,
    USLT,
    ID3NoHeaderError,
)
from mutagen.mp3 import MP3
from mutagen.wave import WAVE

_ISRC_RE = re.compile(r"^[A-Z]{2}-?[A-Z0-9]{3}-?[0-9]{2}-?[0-9]{5}$", re.IGNORECASE)


def normalize_and_validate_isrc(isrc: str) -> str:
    """Validate an ISO 3901 ISRC code and return the canonical 12-character uppercase string."""
    cleaned = isrc.strip().upper()
    if not _ISRC_RE.match(cleaned):
        raise ValueError(
            f"Invalid ISRC '{isrc}'. Expected 12-character ISO 3901 format (CC-XXX-YY-NNNNN or CCXXXYYNNNNN)."
        )
    return cleaned.replace("-", "")


def read_audio_tags(file_path: str | Path) -> dict[str, Any]:
    """Read release metadata tags (including ISRC, BPM, Key, Publisher, Lyrics) from MP3/FLAC/WAV."""
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")

    ext = path.suffix.lower()
    if ext == ".flac":
        fl = FLAC(str(path))
        return {
            "file_path": str(path),
            "format": "FLAC",
            "title": (fl.get("title") or [None])[0],
            "artist": (fl.get("artist") or [None])[0],
            "album": (fl.get("album") or [None])[0],
            "year": (fl.get("date") or [None])[0],
            "track": (fl.get("tracknumber") or [None])[0],
            "genre": (fl.get("genre") or [None])[0],
            "bpm": (fl.get("bpm") or [None])[0],
            "key": (fl.get("initialkey") or [None])[0],
            "publisher": (fl.get("organization") or [None])[0],
            "isrc": (fl.get("isrc") or [None])[0],
            "lyrics": (fl.get("lyrics") or [None])[0],
            "has_cover_art": len(fl.pictures) > 0,
        }

    tags: ID3 | None = None
    if ext == ".mp3":
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            tags = ID3()
    elif ext in (".wav", ".wave"):
        wav = WAVE(str(path))
        tags = wav.tags if isinstance(wav.tags, ID3) else ID3()
    else:
        raise ValueError(f"Unsupported tag container format '{ext}'. Supported: .mp3, .flac, .wav")

    def _txt(frame_id: str) -> str | None:
        frame = tags.get(frame_id)
        if frame is not None and getattr(frame, "text", None):
            return str(frame.text[0])
        return None

    uslt_frames = tags.getall("USLT")
    comm_frames = tags.getall("COMM")
    apic_frames = tags.getall("APIC")

    return {
        "file_path": str(path),
        "format": ext.lstrip(".").upper(),
        "title": _txt("TIT2"),
        "artist": _txt("TPE1"),
        "album": _txt("TALB"),
        "year": _txt("TYER") or _txt("TDRC"),
        "track": _txt("TRCK"),
        "genre": _txt("TCON"),
        "bpm": _txt("TBPM"),
        "key": _txt("TKEY"),
        "publisher": _txt("TPUB"),
        "isrc": _txt("TSRC"),
        "comment": str(comm_frames[0].text[0]) if comm_frames and comm_frames[0].text else None,
        "lyrics": str(uslt_frames[0].text) if uslt_frames else None,
        "has_cover_art": len(apic_frames) > 0,
    }


def write_audio_tags(
    file_path: str | Path,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    year: str | None = None,
    track: str | None = None,
    genre: str | None = None,
    bpm: str | float | None = None,
    initial_key: str | None = None,
    publisher: str | None = None,
    isrc: str | None = None,
    comment: str | None = None,
    lyrics: str | None = None,
    cover_art_path: str | Path | None = None,
) -> dict[str, Any]:
    """Write ID3v2.3 / VorbisComment release metadata, ISRC (`TSRC`), and cover art."""
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")

    canonical_isrc = normalize_and_validate_isrc(isrc) if isrc else None
    ext = path.suffix.lower()

    if ext == ".flac":
        fl = FLAC(str(path))
        if title is not None:
            fl["title"] = title
        if artist is not None:
            fl["artist"] = artist
        if album is not None:
            fl["album"] = album
        if year is not None:
            fl["date"] = str(year)
        if track is not None:
            fl["tracknumber"] = str(track)
        if genre is not None:
            fl["genre"] = genre
        if bpm is not None:
            fl["bpm"] = str(bpm)
        if initial_key is not None:
            fl["initialkey"] = initial_key
        if publisher is not None:
            fl["organization"] = publisher
        if canonical_isrc is not None:
            fl["isrc"] = canonical_isrc
        if comment is not None:
            fl["comment"] = comment
        if lyrics is not None:
            fl["lyrics"] = lyrics
        if cover_art_path is not None:
            art_file = Path(cover_art_path).expanduser().resolve()
            mime = "image/png" if art_file.suffix.lower() == ".png" else "image/jpeg"
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.desc = "Front Cover"
            pic.data = art_file.read_bytes()
            fl.clear_pictures()
            fl.add_picture(pic)
        fl.save()
        return read_audio_tags(path)

    audio_obj: Any
    if ext == ".mp3":
        audio_obj = MP3(str(path))
        if audio_obj.tags is None:
            audio_obj.add_tags()
        tags = audio_obj.tags
    elif ext in (".wav", ".wave"):
        audio_obj = WAVE(str(path))
        if audio_obj.tags is None:
            audio_obj.add_tags()
        tags = audio_obj.tags
    else:
        raise ValueError(f"Unsupported tag container format '{ext}'. Supported: .mp3, .flac, .wav")

    if not isinstance(tags, ID3):
        raise TypeError("Expected ID3 tag container")
    if title is not None:
        tags.setall("TIT2", [TIT2(encoding=3, text=title)])
    if artist is not None:
        tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
    if album is not None:
        tags.setall("TALB", [TALB(encoding=3, text=album)])
    if year is not None:
        tags.setall("TYER", [TYER(encoding=3, text=str(year))])
    if track is not None:
        tags.setall("TRCK", [TRCK(encoding=3, text=str(track))])
    if genre is not None:
        tags.setall("TCON", [TCON(encoding=3, text=genre)])
    if bpm is not None:
        tags.setall("TBPM", [TBPM(encoding=3, text=str(bpm))])
    if initial_key is not None:
        tags.setall("TKEY", [TKEY(encoding=3, text=initial_key)])
    if publisher is not None:
        tags.setall("TPUB", [TPUB(encoding=3, text=publisher)])
    if canonical_isrc is not None:
        tags.setall("TSRC", [TSRC(encoding=3, text=canonical_isrc)])
    if comment is not None:
        tags.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=comment)])
    if lyrics is not None:
        tags.setall("USLT", [USLT(encoding=3, lang="eng", desc="", text=lyrics)])
    if cover_art_path is not None:
        art_file = Path(cover_art_path).expanduser().resolve()
        mime = "image/png" if art_file.suffix.lower() == ".png" else "image/jpeg"
        tags.setall(
            "APIC",
            [
                APIC(
                    encoding=3,
                    mime=mime,
                    type=3,
                    desc="Front Cover",
                    data=art_file.read_bytes(),
                )
            ],
        )

    audio_obj.save(v2_version=3)
    return read_audio_tags(path)
