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


def is_skipped(path: Path) -> bool:
    name = path.name
    return name.startswith(".") or name in SKIP_NAMES or path.is_symlink()


def is_audio(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES


@dataclass
class Discovery:
    albums: list[Path] = field(default_factory=list)
    # Folders that could not be read, relative to the root, with the reason.
    unreadable: dict[Path, str] = field(default_factory=dict)


def find_albums(root: Path, exclude: frozenset[Path] = frozenset()) -> Discovery:
    """Find the folders below root that hold audio files directly.

    Paths in the result and in exclude are relative to root. A subfolder with audio
    files is an album of its own, whatever its name, so a box set appears as one
    album per disc folder. A folder that cannot be read is listed in
    Discovery.unreadable rather than raising; the caller must not treat albums below
    it as gone.
    """
    found = Discovery()
    pending = [root]
    while pending:
        folder = pending.pop()
        try:
            entries = sorted(folder.iterdir())
            has_audio = False
            for entry in entries:
                rel = entry.relative_to(root)
                if rel in exclude or is_skipped(entry):
                    continue
                if entry.is_dir():
                    pending.append(entry)
                elif is_audio(entry):
                    has_audio = True
        except OSError as e:
            found.unreadable[folder.relative_to(root)] = e.strerror or str(e)
            continue
        if has_audio and folder != root:
            found.albums.append(folder.relative_to(root))
    found.albums.sort()
    return found


def audio_files(folder: Path) -> list[Path]:
    """Return the audio files directly in folder, as names."""
    return sorted(
        Path(p.name) for p in folder.iterdir() if not is_skipped(p) and is_audio(p)
    )


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
