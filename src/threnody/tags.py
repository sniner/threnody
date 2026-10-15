"""Reading tags and stream properties of an audio file with mutagen."""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
import mutagen.aiff
import mutagen.dsdiff
import mutagen.dsf
import mutagen.flac
import mutagen.id3
import mutagen.mp3
import mutagen.mp4
import mutagen.oggflac
import mutagen.oggopus
import mutagen.oggvorbis
import mutagen.wave
from mutagen._vorbis import VCommentDict

LOSSLESS = frozenset({"flac", "alac", "dsd", "wav", "aiff"})

# Roles a credit can have; the role is the tag the name came from.
ROLES = ("artist", "album_artist", "composer", "performer", "conductor")


class TagError(Exception):
    """An audio file could not be read."""


@dataclass
class Tags:
    title: str | None = None
    album: str | None = None
    date: str | None = None
    original_date: str | None = None
    number: int | None = None
    disc: int | None = None
    musicbrainz_album_id: str | None = None
    genres: list[str] = field(default_factory=list)
    # (role, name) pairs in tag order, without duplicates.
    credits: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Stream:
    format: str
    duration: float
    bitrate: int
    sample_rate: int
    channels: int
    bit_depth: int | None

    @property
    def lossless(self) -> bool:
        return self.format in LOSSLESS


@dataclass
class AudioFile:
    stream: Stream
    tags: Tags


Reader = Callable[[Path], AudioFile]


def values(raw: Iterable[object]) -> list[str]:
    """Split multi-valued tags; the library writes several values as one string
    separated by semicolons as often as it writes them as separate values."""
    out: list[str] = []
    for item in raw:
        text = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
        for part in text.split(";"):
            part = " ".join(part.split())
            if part and part not in out:
                out.append(part)
    return out


def first(vals: list[str]) -> str | None:
    return vals[0] if vals else None


def number(vals: list[str]) -> int | None:
    """Return the number before a slash ("3/12") or in a tuple, if there is one."""
    if not vals:
        return None
    m = re.match(r"\s*\(?(\d+)", vals[0])
    return int(m.group(1)) if m else None


def add_credits(tags: Tags, role: str, names: list[str]) -> None:
    for name in names:
        if (role, name) not in tags.credits:
            tags.credits.append((role, name))


def vorbis_tags(comments: VCommentDict) -> Tags:
    def get(key: str) -> list[str]:
        return values(comments.get(key) or [])

    tags = Tags(
        title=first(get("title")),
        album=first(get("album")),
        date=first(get("date")),
        original_date=first(get("originaldate") or get("originalyear")),
        number=number(get("tracknumber")),
        disc=number(get("discnumber")),
        musicbrainz_album_id=first(get("musicbrainz_albumid")),
        genres=get("genre"),
    )
    for role, key in (
        ("artist", "artist"),
        ("album_artist", "albumartist"),
        ("composer", "composer"),
        ("performer", "performer"),
        ("conductor", "conductor"),
    ):
        add_credits(tags, role, get(key))
    return tags


def freeform_key(key: str) -> str:
    """Normalize "----:com.apple.iTunes:MusicBrainz Album Id" to "musicbrainzalbumid"."""
    return re.sub(r"[\s_]", "", key.rsplit(":", 1)[-1]).lower()


def mp4_tags(atoms: mutagen.mp4.MP4Tags) -> Tags:
    freeform = {freeform_key(k): values(v) for k, v in atoms.items() if k[:4] == "----"}

    def get(key: str) -> list[str]:
        return values(atoms.get(key) or [])

    tags = Tags(
        title=first(get("\xa9nam")),
        album=first(get("\xa9alb")),
        date=first(get("\xa9day")),
        original_date=first(freeform.get("originaldate", [])),
        number=number([str(t[0]) for t in atoms.get("trkn") or []]),
        disc=number([str(t[0]) for t in atoms.get("disk") or []]),
        musicbrainz_album_id=first(freeform.get("musicbrainzalbumid", [])),
        genres=get("\xa9gen"),
    )
    add_credits(tags, "artist", get("\xa9ART"))
    add_credits(tags, "album_artist", get("aART"))
    add_credits(tags, "composer", get("\xa9wrt"))
    add_credits(tags, "performer", freeform.get("performer", []))
    add_credits(tags, "conductor", freeform.get("conductor", []))
    return tags


def id3_tags(frames: mutagen.id3.ID3) -> Tags:
    def get(key: str) -> list[str]:
        frame = frames.get(key)
        return values(getattr(frame, "text", [])) if frame is not None else []

    genre = frames.get("TCON")
    tags = Tags(
        title=first(get("TIT2")),
        album=first(get("TALB")),
        date=first(get("TDRC") or get("TYER")),
        original_date=first(get("TDOR") or get("TXXX:ORIGINALDATE")),
        number=number(get("TRCK")),
        disc=number(get("TPOS")),
        musicbrainz_album_id=first(get("TXXX:MusicBrainz Album Id")),
        genres=values(genre.genres) if genre is not None else [],
    )
    add_credits(tags, "artist", get("TPE1"))
    add_credits(tags, "album_artist", get("TPE2"))
    add_credits(tags, "composer", get("TCOM"))
    add_credits(tags, "performer", get("TXXX:PERFORMER"))
    musicians = frames.get("TMCL")
    if musicians is not None:
        add_credits(tags, "performer", values(name for _, name in musicians.people))
    add_credits(tags, "conductor", get("TPE3"))
    return tags


def tags_of(tags: object) -> Tags:
    if tags is None:
        return Tags()
    if isinstance(tags, VCommentDict):
        return vorbis_tags(tags)
    if isinstance(tags, mutagen.mp4.MP4Tags):
        return mp4_tags(tags)
    if isinstance(tags, mutagen.id3.ID3):
        return id3_tags(tags)
    raise TagError(f"tags of type {type(tags).__name__} are not supported")


def format_of(audio: mutagen.FileType) -> str:
    if isinstance(audio, mutagen.flac.FLAC | mutagen.oggflac.OggFLAC):
        return "flac"
    if isinstance(audio, mutagen.mp4.MP4):
        return "alac" if audio.info.codec == "alac" else "aac"
    if isinstance(audio, mutagen.mp3.MP3):
        return "mp3"
    if isinstance(audio, mutagen.dsf.DSF | mutagen.dsdiff.DSDIFF):
        return "dsd"
    if isinstance(audio, mutagen.oggvorbis.OggVorbis):
        return "vorbis"
    if isinstance(audio, mutagen.oggopus.OggOpus):
        return "opus"
    if isinstance(audio, mutagen.wave.WAVE):
        return "wav"
    if isinstance(audio, mutagen.aiff.AIFF):
        return "aiff"
    raise TagError(f"{type(audio).__name__} files are not supported")


def stream_of(audio: mutagen.FileType) -> Stream:
    fmt = format_of(audio)
    info = audio.info
    if info is None:
        raise TagError("no stream information")
    depth = getattr(info, "bits_per_sample", None)
    return Stream(
        format=fmt,
        duration=float(info.length),
        bitrate=int(getattr(info, "bitrate", 0) or 0),
        sample_rate=int(getattr(info, "sample_rate", 0) or 0),
        channels=int(getattr(info, "channels", 0) or 0),
        # Lossy formats report a nominal depth that says nothing about the file.
        bit_depth=int(depth) if depth and fmt in LOSSLESS else None,
    )


def read_audio(path: Path) -> AudioFile:
    """Return the stream properties and tags of an audio file.

    Raises TagError if mutagen cannot read the file or does not know its format;
    OSError is left to the caller.
    """
    try:
        audio = mutagen.File(path)
    except mutagen.MutagenError as e:
        raise TagError(str(e) or type(e).__name__) from e
    if audio is None:
        raise TagError("not an audio file mutagen can read")
    return AudioFile(stream_of(audio), tags_of(audio.tags))
