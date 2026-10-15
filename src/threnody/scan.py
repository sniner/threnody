"""A scan of the library: find albums, read changed files, update the database."""

import concurrent.futures
import datetime
import logging
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .db import (
    DbError,
    Names,
    get_exclude,
    get_meta,
    known_files,
    open_db,
    prune_unused,
    set_exclude,
    set_meta,
)
from .library import audio_files, find_albums, parse_folder
from .tags import AudioFile, Reader, TagError, read_audio

# A scan refuses to remove more albums than this share of the known albums (but
# always allows GONE_MIN), so an unmounted or half-readable share does not empty
# the inventory.
GONE_SHARE = 0.05
GONE_MIN = 3

YEAR_RE = re.compile(r"\d{4}")

log = logging.getLogger(__name__)


class Refusal(Exception):
    """The scan cannot start; the message says why and what to do."""


@dataclass
class FileScan:
    file: str
    size: int
    mtime_ns: int
    status: str  # new, changed, unchanged
    audio: AudioFile | None = None
    error: str | None = None


@dataclass
class AlbumScan:
    folder: str
    status: str = "unchanged"  # new, changed, unchanged, gone, error
    files: list[FileScan] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def counts(self) -> str:
        c = Counter(f.status for f in self.files)
        parts = [f"{c[s]} {s}" for s in ("new", "changed") if c[s]]
        if self.gone:
            parts.append(f"{len(self.gone)} gone")
        return ", ".join(parts)


@dataclass
class ScanResult:
    albums: list[AlbumScan]
    started: datetime.datetime

    def having(self, status: str) -> list[AlbumScan]:
        return [a for a in self.albums if a.status == status]

    @property
    def errors(self) -> list[AlbumScan]:
        return [a for a in self.albums if a.errors]

    @property
    def tracks(self) -> int:
        return sum(len(a.files) for a in self.albums)


@dataclass
class Context:
    source: Path
    read: Reader
    today: str


def scan_album(
    ctx: Context, rel: Path, known: dict[str, tuple[int, int]] | None
) -> AlbumScan:
    """Read the files of one album folder that are new or changed since the last
    scan; known is None for an album not recorded yet."""
    scan = AlbumScan(rel.as_posix(), "new" if known is None else "unchanged")
    folder = ctx.source / rel
    old = known or {}
    try:
        names = audio_files(folder)
        for name in names:
            st = (folder / name).stat()
            entry = FileScan(name.name, st.st_size, st.st_mtime_ns, "unchanged")
            if old.get(entry.file) != (st.st_size, st.st_mtime_ns):
                entry.status = "new" if entry.file not in old else "changed"
                log.debug("reading %s", folder / name)
                try:
                    entry.audio = ctx.read(folder / name)
                except TagError as e:
                    entry.error = str(e)
                    scan.errors.append(f"{entry.file}: cannot read the tags: {e}")
            scan.files.append(entry)
    except OSError as e:
        scan.status = "error"
        scan.errors.append(f"cannot read this folder: {e.strerror or e}")
        return scan
    present = {f.file for f in scan.files}
    scan.gone = sorted(f for f in old if f not in present)
    if scan.status == "unchanged" and (
        scan.gone or any(f.status != "unchanged" for f in scan.files)
    ):
        scan.status = "changed"
    return scan


def scan_all(
    ctx: Context,
    albums: list[Path],
    known: dict[str, dict[str, tuple[int, int]]],
    jobs: int,
) -> list[AlbumScan]:
    results: dict[Path, AlbumScan] = {}
    last_report = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(scan_album, ctx, rel, known.get(rel.as_posix())): rel
            for rel in albums
        }
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            results[futures[future]] = res
            for err in res.errors:
                log.warning("%s: %s", res.folder, err)
            if res.status == "new":
                log.info("new: %s", res.folder)
            elif res.status == "changed":
                log.info("changed: %s (%s)", res.folder, res.counts())
            if time.monotonic() - last_report > 10:
                log.info("reading tags: %d of %d albums done", len(results), len(albums))
                last_report = time.monotonic()
    return [results[rel] for rel in albums]


def year_of(date: str | None) -> int | None:
    m = YEAR_RE.search(date or "")
    return int(m.group()) if m else None


def track_row(album_id: int, f: FileScan) -> dict[str, object]:
    """The columns of a track row; a file whose tags could not be read keeps its
    name, size and suffix so the inventory still lists it."""
    audio = f.audio
    row: dict[str, object] = {
        "album_id": album_id,
        "file": f.file,
        "size": f.size,
        "mtime_ns": f.mtime_ns,
        "format": audio.stream.format if audio else Path(f.file).suffix.lstrip("."),
        "lossless": int(audio.stream.lossless) if audio else 0,
        "error": f.error,
    }
    if audio:
        s, t = audio.stream, audio.tags
        row.update(
            disc=t.disc,
            number=t.number,
            title=t.title,
            duration=s.duration,
            bitrate=s.bitrate,
            sample_rate=s.sample_rate,
            bit_depth=s.bit_depth,
            channels=s.channels,
            album_tag=t.album,
            date=t.date,
            original_date=t.original_date,
            musicbrainz_album_id=t.musicbrainz_album_id,
        )
    return row


class Writer:
    """Applies album scans to the database; runs in the main thread only, since a
    SQLite connection must not be shared between threads."""

    def __init__(self, con: sqlite3.Connection, today: str) -> None:
        self.con = con
        self.today = today
        self.artists = Names(con, "artist")
        self.genres = Names(con, "genre")

    def apply(self, scan: AlbumScan) -> None:
        if scan.status in ("unchanged", "error"):
            return
        if scan.status == "gone":
            self.con.execute("DELETE FROM album WHERE folder = ?", (scan.folder,))
            return
        album_id = self.album_id(scan.folder)
        for f in scan.files:
            if f.status != "unchanged":
                self.write_track(album_id, f)
        for name in scan.gone:
            self.con.execute(
                "DELETE FROM track WHERE album_id = ? AND file = ?", (album_id, name)
            )
        self.update_album(album_id, scan.folder)

    def album_id(self, folder: str) -> int:
        row = self.con.execute(
            "SELECT id FROM album WHERE folder = ?", (folder,)
        ).fetchone()
        if row is not None:
            return int(row[0])
        rel = Path(folder)
        cur = self.con.execute(
            "INSERT INTO album (folder, folder_artist_id, title, scanned) "
            "VALUES (?, ?, ?, ?)",
            (folder, self.artists.id_of(rel.parts[0]), rel.name, self.today),
        )
        return int(cur.lastrowid or 0)

    def write_track(self, album_id: int, f: FileScan) -> None:
        row = track_row(album_id, f)
        self.con.execute(
            "DELETE FROM track WHERE album_id = ? AND file = ?", (album_id, f.file)
        )
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        cur = self.con.execute(
            f"INSERT INTO track ({cols}) VALUES ({marks})", tuple(row.values())
        )
        track_id = cur.lastrowid
        if f.audio is None:
            return
        self.con.executemany(
            "INSERT OR IGNORE INTO track_genre (track_id, genre_id) VALUES (?, ?)",
            [(track_id, self.genres.id_of(g)) for g in f.audio.tags.genres],
        )
        self.con.executemany(
            "INSERT OR IGNORE INTO track_credit (track_id, artist_id, role) "
            "VALUES (?, ?, ?)",
            [
                (track_id, self.artists.id_of(name), role)
                for role, name in f.audio.tags.credits
            ],
        )

    def majority(self, album_id: int, column: str) -> str | None:
        """The value most tracks of the album agree on."""
        row = self.con.execute(
            f"SELECT {column} FROM track WHERE album_id = ? AND {column} IS NOT NULL "
            f"AND {column} != '' GROUP BY {column} ORDER BY COUNT(*) DESC, {column} "
            "LIMIT 1",
            (album_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    def update_album(self, album_id: int, folder: str) -> None:
        """Set the album's title, year and id from its tracks, falling back to the
        folder name."""
        info = parse_folder(Path(folder).name)
        date = self.majority(album_id, "date")
        self.con.execute(
            "UPDATE album SET title = ?, year = ?, original_year = ?, date = ?, "
            "musicbrainz_id = ?, scanned = ? WHERE id = ?",
            (
                self.majority(album_id, "album_tag") or info.title,
                year_of(date) or info.year,
                year_of(self.majority(album_id, "original_date")),
                date,
                info.musicbrainz_id or self.majority(album_id, "musicbrainz_album_id"),
                self.today,
                album_id,
            ),
        )
        self.con.execute("DELETE FROM album_marker WHERE album_id = ?", (album_id,))
        self.con.executemany(
            "INSERT OR IGNORE INTO album_marker (album_id, marker) VALUES (?, ?)",
            [(album_id, m) for m in info.markers],
        )


def check_gone(found: list[Path], known: list[str], gone: list[str]) -> None:
    if not gone:
        return
    limit = max(GONE_MIN, int(len(known) * GONE_SHARE))
    if not found or len(gone) > limit:
        raise Refusal(
            f"{len(gone)} of {len(known)} recorded albums are gone from the source "
            f"(at most {limit} are removed per scan); check that the share is "
            "complete, or pass --allow-missing if they were really removed"
        )


def resolve_source(con: sqlite3.Connection, source: Path | None) -> Path:
    if source is None:
        recorded = get_meta(con, "source")
        if recorded is None:
            raise Refusal(
                "this database has no source yet, name the library folder to scan"
            )
        source = Path(recorded)
        log.info("scanning %s, the source recorded in the database", source)
    if not source.is_dir():
        raise Refusal(f"{source}: no such directory, check that the share is mounted")
    return source


def resolve_exclude(
    con: sqlite3.Connection, source: Path, exclude: frozenset[Path]
) -> frozenset[Path]:
    """Combine --exclude with the folders saved in the database; a database
    without any gets the folders of this scan, so later scans need no options."""
    for rel in sorted(exclude):
        if not (source / rel).is_dir():
            raise Refusal(f"--exclude {rel}: no such folder in the source")
    saved = get_exclude(con)
    if saved is None:
        set_exclude(con, exclude)
    else:
        for rel in sorted(saved):
            if not (source / rel).is_dir():
                raise Refusal(
                    f"{rel}: no such folder in the source; it is excluded in the "
                    "database, remove it from meta.exclude with sqlite3"
                )
        exclude = exclude | saved
    if exclude:
        log.info("skipping %s", ", ".join(p.as_posix() for p in sorted(exclude)))
    return exclude


def scan(
    source: Path | None,
    db_path: Path,
    *,
    read: Reader = read_audio,
    jobs: int = 4,
    dry_run: bool = False,
    allow_missing: bool = False,
    exclude: frozenset[Path] = frozenset(),
) -> ScanResult:
    """Bring the inventory in db_path up to date with source; a dry run changes
    nothing, not even a database that does not exist yet."""
    started = datetime.datetime.now()
    if not db_path.exists() and not db_path.parent.is_dir():
        raise Refusal(f"{db_path}: its directory does not exist, create it first")
    try:
        con = open_db(None if dry_run and not db_path.exists() else db_path)
    except DbError as e:
        raise Refusal(str(e)) from e
    try:
        source = resolve_source(con, source)
        exclude = resolve_exclude(con, source, exclude)
        result = scan_into(con, source, read, jobs, allow_missing, exclude, started)
        if dry_run:
            con.rollback()
        else:
            set_meta(con, "source", str(source))
            set_meta(con, "scanned", started.isoformat(timespec="seconds"))
            con.commit()
    finally:
        con.close()
    return result


def scan_into(
    con: sqlite3.Connection,
    source: Path,
    read: Reader,
    jobs: int,
    allow_missing: bool,
    exclude: frozenset[Path],
    started: datetime.datetime,
) -> ScanResult:
    log.info("step 1 of 2: looking for album folders in %s", source)
    discovery = find_albums(source, exclude)
    found = discovery.albums
    for rel, reason in discovery.unreadable.items():
        log.warning("%s: cannot read this folder (%s), its albums are kept", rel, reason)
    known = known_files(con)
    unseen = [*discovery.unreadable, *exclude]
    found_set = {rel.as_posix() for rel in found}
    gone = sorted(
        folder
        for folder in known
        if folder not in found_set
        and not any(Path(folder).is_relative_to(u) for u in unseen)
    )
    if not allow_missing:
        check_gone(found, sorted(known), gone)

    log.info("step 2 of 2: reading %s", albums_count(len(found)))
    if not known:
        log.info("empty database: every file gets read, which takes a while")
    ctx = Context(source, read, started.date().isoformat())
    albums = scan_all(ctx, found, known, jobs)
    for folder in gone:
        log.info("gone: %s", folder)
    albums += [AlbumScan(folder, "gone") for folder in gone]
    albums += [
        AlbumScan(rel.as_posix(), "error", errors=[f"cannot read this folder: {r}"])
        for rel, r in discovery.unreadable.items()
    ]
    writer = Writer(con, ctx.today)
    for album in albums:
        writer.apply(album)
    prune_unused(con)
    return ScanResult(albums, started)


def albums_count(n: int) -> str:
    return f"{n:,} album" if n == 1 else f"{n:,} albums"


def verdict(result: ScanResult) -> str:
    recorded = (
        len(result.albums) - len(result.having("gone")) - len(result.having("error"))
    )
    text = (
        f"{albums_count(recorded)} with {result.tracks:,} tracks recorded; "
        f"{len(result.having('new'))} new, {len(result.having('changed'))} changed, "
        f"{len(result.having('gone'))} gone"
    )
    if result.errors:
        text += f"; {len(result.errors)} with errors, see the warnings above"
    return text
