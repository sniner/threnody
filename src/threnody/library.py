"""Finding album folders and reading what their names say."""

import re
from dataclasses import dataclass, field
from pathlib import Path

# Suffixes of files whose tags threnody can read (Vorbis comments, MP4 atoms, ID3).
AUDIO_SUFFIXES = frozenset(
    {".flac", ".m4a", ".mp3", ".dsf", ".dff", ".ogg", ".opus", ".wav", ".aif", ".aiff"}
)

# NAS housekeeping folders (Synology, QNAP) that are not part of any album.
SKIP_NAMES = frozenset({"@eaDir", "#recycle", "#snapshot", "@Recycle", "lost+found"})

MARKER_RE = re.compile(r"\s*\[([^\]]+)\]")
MUSICBRAINZ_RE = re.compile(r"\s*\{musicbrainz-([0-9a-f-]{36})\}")
YEAR_RE = re.compile(r"\s*\((\d{4})(?:-\d\d){0,2}\)")
LEADING_YEAR_RE = re.compile(r"^(\d{4}) - ")


def is_skipped(name: str) -> bool:
    return name.startswith(".") or name in SKIP_NAMES


def is_audio(name: str) -> bool:
    return Path(name).suffix.lower() in AUDIO_SUFFIXES


@dataclass
class Discovery:
    # Album folder relative to the root -> names of its audio files, sorted.
    albums: dict[Path, list[str]] = field(default_factory=dict)
    # Folders that could not be read, relative to the root, with the reason.
    unreadable: dict[Path, str] = field(default_factory=dict)


def find_albums(root: Path, exclude: frozenset[Path] = frozenset()) -> Discovery:
    """Find the folders below root that hold audio files directly.

    Paths in the result and in exclude are relative to root. A subfolder with audio
    files is an album of its own, whatever its name, so a box set appears as one
    album per disc folder. A folder that cannot be read is listed in
    Discovery.unreadable rather than raising; the caller must not treat albums below
    it as gone.

    Path.walk tells files from folders without a stat per entry, which matters on
    a network share; the only stat per file is the one the scan makes for size and
    modification time.
    """
    found = Discovery()

    def on_error(e: OSError) -> None:
        folder = Path(e.filename or root)
        found.unreadable[folder.relative_to(root)] = e.strerror or str(e)

    for folder, dirnames, filenames in root.walk(on_error=on_error):
        rel = folder.relative_to(root)
        dirnames[:] = sorted(
            d for d in dirnames if not is_skipped(d) and rel / d not in exclude
        )
        audio = sorted(f for f in filenames if not is_skipped(f) and is_audio(f))
        if audio and folder != root:
            found.albums[rel] = audio
    found.albums = dict(sorted(found.albums.items()))
    return found


@dataclass(frozen=True)
class FolderInfo:
    title: str
    year: int | None
    markers: tuple[str, ...]
    musicbrainz_id: str | None


def parse_folder(name: str) -> FolderInfo:
    """Read title, year and markers from an album folder name.

    The folder convention is "Artist – Title (Year) [marker]... {musicbrainz-id}",
    but older folders are named "Artist (Year) - Title" or "Year - Title", so the
    year is taken from anywhere in the name and the title is what is left once the
    artist, year, markers and id are removed. Tags take precedence over all of
    this; it is the fallback for files without them.
    """
    markers = tuple(m.group(1) for m in MARKER_RE.finditer(name))
    rest = MARKER_RE.sub("", name)
    mb = MUSICBRAINZ_RE.search(rest)
    rest = MUSICBRAINZ_RE.sub("", rest)
    year = None
    if m := YEAR_RE.search(rest):
        year = int(m.group(1))
        rest = rest[: m.start()] + rest[m.end() :]
    elif m := LEADING_YEAR_RE.match(rest):
        year = int(m.group(1))
        rest = rest[m.end() :]
    _, dash, after = rest.partition(" – ")
    title = (after if dash else rest).strip(" -")
    return FolderInfo(title or name, year, markers, mb.group(1) if mb else None)
