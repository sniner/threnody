"""The SQLite inventory: schema, views and the small amount of SQL the scan needs."""

import json
import sqlite3
from pathlib import Path

FORMAT = 1

SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE artist (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE genre (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

-- One row per folder that holds audio files. folder_artist is the top-level
-- folder the album lives in, as named; it is a composer for classical albums
-- and "Various" for compilations. The credits in the tags are in track_credit.
CREATE TABLE album (
    id INTEGER PRIMARY KEY,
    folder TEXT NOT NULL UNIQUE,
    folder_artist_id INTEGER NOT NULL REFERENCES artist(id),
    title TEXT NOT NULL,
    year INTEGER,
    original_year INTEGER,
    date TEXT,
    musicbrainz_id TEXT,
    scanned TEXT NOT NULL
);

-- The [marker] tokens of the folder name: owned, ripped, accurate, found, hires...
CREATE TABLE album_marker (
    album_id INTEGER NOT NULL REFERENCES album(id) ON DELETE CASCADE,
    marker TEXT NOT NULL,
    PRIMARY KEY (album_id, marker)
);

CREATE TABLE track (
    id INTEGER PRIMARY KEY,
    album_id INTEGER NOT NULL REFERENCES album(id) ON DELETE CASCADE,
    file TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    disc INTEGER,
    number INTEGER,
    title TEXT,
    format TEXT NOT NULL,
    lossless INTEGER NOT NULL,
    duration REAL,
    bitrate INTEGER,
    sample_rate INTEGER,
    bit_depth INTEGER,
    channels INTEGER,
    album_tag TEXT,
    date TEXT,
    original_date TEXT,
    musicbrainz_album_id TEXT,
    error TEXT,
    UNIQUE (album_id, file)
);

CREATE TABLE track_genre (
    track_id INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genre(id),
    PRIMARY KEY (track_id, genre_id)
);

-- role is the tag the name came from: artist, album_artist, composer,
-- performer or conductor.
CREATE TABLE track_credit (
    track_id INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artist(id),
    role TEXT NOT NULL,
    PRIMARY KEY (track_id, artist_id, role)
);

CREATE INDEX track_album ON track(album_id);
CREATE INDEX track_credit_artist ON track_credit(artist_id, role);
CREATE INDEX track_genre_genre ON track_genre(genre_id);

CREATE VIEW album_genre AS
    SELECT DISTINCT t.album_id, g.id AS genre_id, g.name AS genre
    FROM track t
    JOIN track_genre tg ON tg.track_id = t.id
    JOIN genre g ON g.id = tg.genre_id;

CREATE VIEW album_credit AS
    SELECT DISTINCT t.album_id, c.role, a.id AS artist_id, a.name AS artist
    FROM track t
    JOIN track_credit c ON c.track_id = t.id
    JOIN artist a ON a.id = c.artist_id;

CREATE VIEW album_overview AS
    SELECT al.id, fa.name AS folder_artist, al.title, al.year, al.original_year,
        (SELECT group_concat(genre, '; ') FROM
            (SELECT genre FROM album_genre WHERE album_id = al.id ORDER BY genre)
        ) AS genres,
        (SELECT group_concat(artist, '; ') FROM
            (SELECT artist FROM album_credit
             WHERE album_id = al.id AND role = 'album_artist' ORDER BY artist)
        ) AS album_artists,
        (SELECT group_concat(artist, '; ') FROM
            (SELECT artist FROM album_credit
             WHERE album_id = al.id AND role = 'composer' ORDER BY artist)
        ) AS composers,
        COUNT(t.id) AS tracks,
        CAST(ROUND(COALESCE(SUM(t.duration), 0) / 60) AS INTEGER) AS minutes,
        (SELECT group_concat(format, '/') FROM
            (SELECT DISTINCT format FROM track WHERE album_id = al.id ORDER BY format)
        ) AS formats,
        MIN(t.lossless) AS lossless,
        MIN(t.bitrate) / 1000 AS min_kbps,
        MAX(t.sample_rate) AS sample_rate,
        MAX(t.bit_depth) AS bit_depth,
        (SELECT group_concat(marker, ' ') FROM
            (SELECT marker FROM album_marker WHERE album_id = al.id ORDER BY marker)
        ) AS markers,
        al.folder
    FROM album al
    JOIN artist fa ON fa.id = al.folder_artist_id
    LEFT JOIN track t ON t.album_id = al.id
    GROUP BY al.id;

CREATE VIEW genre_overview AS
    SELECT g.name AS genre,
        COUNT(DISTINCT t.album_id) AS albums,
        COUNT(t.id) AS tracks,
        ROUND(SUM(t.duration) / 3600, 1) AS hours
    FROM genre g
    JOIN track_genre tg ON tg.genre_id = g.id
    JOIN track t ON t.id = tg.track_id
    GROUP BY g.id
    ORDER BY albums DESC, g.name;

CREATE VIEW decade_overview AS
    SELECT (COALESCE(original_year, year) / 10) * 10 AS decade,
        COUNT(*) AS albums
    FROM album
    GROUP BY decade
    ORDER BY decade;

CREATE VIEW artist_overview AS
    SELECT a.name AS folder_artist,
        COUNT(DISTINCT al.id) AS albums,
        COUNT(t.id) AS tracks,
        ROUND(SUM(t.duration) / 3600, 1) AS hours,
        (SELECT group_concat(genre, '; ') FROM
            (SELECT DISTINCT ag.genre FROM album_genre ag
             JOIN album x ON x.id = ag.album_id
             WHERE x.folder_artist_id = a.id ORDER BY ag.genre)
        ) AS genres
    FROM artist a
    JOIN album al ON al.folder_artist_id = a.id
    LEFT JOIN track t ON t.album_id = al.id
    GROUP BY a.id
    ORDER BY albums DESC, a.name;

CREATE VIEW credit_overview AS
    SELECT role, artist,
        COUNT(DISTINCT album_id) AS albums
    FROM album_credit
    GROUP BY role, artist_id
    ORDER BY role, albums DESC, artist;
"""


class DbError(Exception):
    """The database cannot be used; the message names the file and the fix."""


def open_db(path: Path | None) -> sqlite3.Connection:
    """Open the inventory, creating the schema in a new file; None opens an empty
    inventory in memory."""
    con = sqlite3.connect(":memory:" if path is None else path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        tables = con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not tables.fetchall():
            con.executescript(SCHEMA)
            set_meta(con, "format", str(FORMAT))
            con.commit()
            return con
        fmt = get_meta(con, "format")
    except sqlite3.DatabaseError as e:
        raise DbError(f"{path}: not a threnody database ({e})") from e
    if fmt is None or not fmt.isdigit():
        raise DbError(f"{path}: not a threnody database, choose another file")
    if int(fmt) > FORMAT:
        raise DbError(f"{path}: written by a newer threnody, upgrade this script")
    return con


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def get_exclude(con: sqlite3.Connection) -> frozenset[Path] | None:
    raw = get_meta(con, "exclude")
    return None if raw is None else frozenset(Path(p) for p in json.loads(raw))


def set_exclude(con: sqlite3.Connection, exclude: frozenset[Path]) -> None:
    set_meta(con, "exclude", json.dumps([p.as_posix() for p in sorted(exclude)]))


class Names:
    """Get-or-create for the artist and genre tables, cached per scan."""

    def __init__(self, con: sqlite3.Connection, table: str) -> None:
        self.con = con
        self.table = table
        self.ids: dict[str, int] = {}

    def id_of(self, name: str) -> int:
        key = name.casefold()
        if key in self.ids:
            return self.ids[key]
        row = self.con.execute(
            f"SELECT id FROM {self.table} WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            cur = self.con.execute(f"INSERT INTO {self.table} (name) VALUES (?)", (name,))
            self.ids[key] = int(cur.lastrowid or 0)
        else:
            self.ids[key] = int(row[0])
        return self.ids[key]


def known_files(con: sqlite3.Connection) -> dict[str, dict[str, tuple[int, int]]]:
    """Return {album folder: {file: (size, mtime_ns)}} for every recorded track.

    Tracks whose tags could not be read are left out, so the next scan reads them
    again; a file fixed by renaming it on the server keeps its size and time.
    """
    result: dict[str, dict[str, tuple[int, int]]] = {}
    rows = con.execute(
        "SELECT al.folder, t.file, t.size, t.mtime_ns FROM track t "
        "JOIN album al ON al.id = t.album_id WHERE t.error IS NULL"
    )
    for folder, file, size, mtime in rows:
        result.setdefault(folder, {})[file] = (size, mtime)
    for (folder,) in con.execute("SELECT folder FROM album"):
        result.setdefault(folder, {})
    return result


def prune_unused(con: sqlite3.Connection) -> None:
    """Remove artists and genres no track or album refers to any more."""
    con.execute("DELETE FROM genre WHERE id NOT IN (SELECT genre_id FROM track_genre)")
    con.execute(
        "DELETE FROM artist WHERE id NOT IN (SELECT artist_id FROM track_credit) "
        "AND id NOT IN (SELECT folder_artist_id FROM album)"
    )
