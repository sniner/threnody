import os
import sqlite3
from pathlib import Path

import mutagen.id3
import mutagen.mp4
import pytest
from mutagen._vorbis import VCommentDict

from threnody.db import get_meta, open_db
from threnody.library import find_albums, parse_folder
from threnody.scan import Refusal, scan, verdict
from threnody.tags import AudioFile, Stream, TagError, Tags, tags_of

ALBUM = Path(
    "Johann Sebastian Bach/Johann Sebastian Bach – Brandenburgische Konzerte (1982) [ripped] [owned]"
)
DISC = Path(
    "Bush/Bush – The Science of Things/Bush - The Science of Things – Disc 2 (1999) [found]"
)
VARIOUS = Path("Various/Forum West (1967) [ripped]")


def write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def fake_read(path: Path) -> AudioFile:
    """Tags derived from the file name: "<n> <title>.<suffix>" with the album
    folder's name as album; a file holding "bad" cannot be read."""
    if path.read_bytes() == b"bad":
        raise TagError("broken")
    number, _, title = path.stem.partition(" ")
    folder = parse_folder(path.parent.name)
    fmt = "flac" if path.suffix == ".flac" else "mp3"
    return AudioFile(
        Stream(fmt, 200.0, 900_000, 44100, 2, 16 if fmt == "flac" else None),
        Tags(
            title=title,
            album=folder.title,
            date=str(folder.year) if folder.year else None,
            number=int(number),
            genres=["Classical"] if "Bach" in str(path) else ["Rock", "Live"],
            credits=[("artist", path.parts[-3]), ("composer", "J. S. Bach")]
            if "Bach" in str(path)
            else [("artist", path.parts[-3])],
        ),
    )


@pytest.fixture
def library(tmp_path: Path) -> Path:
    src = tmp_path / "music"
    write(src / ALBUM / "01 Allegro.flac", b"a" * 100)
    write(src / ALBUM / "02 Andante.flac", b"b" * 100)
    write(src / ALBUM / "cover.jpg")
    write(src / ALBUM / ".DS_Store")
    write(src / DISC / "01 Warm Machine.mp3", b"c" * 100)
    write(src / VARIOUS / "01 Intro.flac", b"d" * 100)
    write(src / "@eaDir" / "Junk" / "01 x.flac")
    write(src / "Incoming" / "New Band - Demo" / "01 y.flac")
    return src


def run(src: Path | None, db: Path, **kwargs: object):
    return scan(src, db, read=fake_read, jobs=2, **kwargs)  # type: ignore[arg-type]


def query(db: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return [tuple(r) for r in con.execute(sql)]
    finally:
        con.close()


def test_find_albums(library: Path) -> None:
    found = find_albums(library, frozenset({Path("Incoming")}))
    assert list(found.albums) == [DISC, ALBUM, VARIOUS]
    assert found.albums[ALBUM] == ["01 Allegro.flac", "02 Andante.flac"]
    assert list(find_albums(library).albums) == [
        DISC,
        Path("Incoming/New Band - Demo"),
        ALBUM,
        VARIOUS,
    ]


def test_parse_folder() -> None:
    info = parse_folder(
        "AC-DC – Back in Black (2003-02-18) [ripped] [accurate] [owned] "
        "{musicbrainz-83ff6988-2f79-40b9-82d5-437f2a5da5f3}"
    )
    assert info.title == "Back in Black"
    assert info.year == 2003
    assert info.markers == ("ripped", "accurate", "owned")
    assert info.musicbrainz_id == "83ff6988-2f79-40b9-82d5-437f2a5da5f3"
    info = parse_folder("Bloodhound Gang (1999) - Hooray for Boobies [found]")
    assert (info.title, info.year) == ("Bloodhound Gang - Hooray for Boobies", 1999)
    info = parse_folder("1973 - Clannad")
    assert (info.title, info.year) == ("Clannad", 1973)
    info = parse_folder("Buffy the Vampire Slayer")
    assert (info.title, info.year, info.markers) == ("Buffy the Vampire Slayer", None, ())
    info = parse_folder("The Rolling Stones – Aftermath (1996) [hires][found][UK]")
    assert info.markers == ("hires", "found", "UK")
    assert info.title == "Aftermath"


def test_vorbis_tags() -> None:
    comments = VCommentDict()
    comments["title"] = ["Allegro"]
    comments["album"] = ["Brandenburgische Konzerte"]
    comments["date"] = ["1982"]
    comments["tracknumber"] = ["03"]
    comments["genre"] = ["Classical;Baroque", "Baroque"]
    comments["artist"] = ["Johann Sebastian Bach; Trevor Pinnock"]
    comments["composer"] = ["Johann Sebastian Bach"]
    comments["performer"] = ["The English Concert", "Trevor Pinnock"]
    tags = tags_of(comments)
    assert (tags.title, tags.album, tags.date, tags.number) == (
        "Allegro",
        "Brandenburgische Konzerte",
        "1982",
        3,
    )
    assert tags.genres == ["Classical", "Baroque"]
    assert tags.credits == [
        ("artist", "Johann Sebastian Bach"),
        ("artist", "Trevor Pinnock"),
        ("composer", "Johann Sebastian Bach"),
        ("performer", "The English Concert"),
        ("performer", "Trevor Pinnock"),
    ]


def test_mp4_tags() -> None:
    atoms = mutagen.mp4.MP4Tags()
    atoms["\xa9nam"] = ["Kryptonite"]
    atoms["\xa9alb"] = ["The Better Life"]
    atoms["\xa9day"] = ["2000"]
    atoms["\xa9gen"] = ["Alternative Rock"]
    atoms["\xa9ART"] = ["3 Doors Down"]
    atoms["trkn"] = [(1, 11)]
    atoms["disk"] = [(2, 2)]
    atoms["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"abc-123"]
    atoms["----:com.apple.iTunes:CONDUCTOR"] = [b"Nobody"]
    tags = tags_of(atoms)
    assert (tags.title, tags.number, tags.disc) == ("Kryptonite", 1, 2)
    assert tags.musicbrainz_album_id == "abc-123"
    assert tags.genres == ["Alternative Rock"]
    assert tags.credits == [("artist", "3 Doors Down"), ("conductor", "Nobody")]


def test_id3_tags() -> None:
    frames = mutagen.id3.ID3()
    frames.add(mutagen.id3.TIT2(encoding=3, text=["Glass Of Water"]))
    frames.add(mutagen.id3.TALB(encoding=3, text=["LeftRightLeftRightLeft"]))
    frames.add(mutagen.id3.TDRC(encoding=3, text=["2009-05-15"]))
    frames.add(mutagen.id3.TRCK(encoding=3, text=["1/9"]))
    frames.add(mutagen.id3.TPOS(encoding=3, text=["1/1"]))
    frames.add(mutagen.id3.TCON(encoding=3, text=["(17)", "Britpop;Live Performance"]))
    frames.add(mutagen.id3.TPE1(encoding=3, text=["Coldplay"]))
    frames.add(mutagen.id3.TPE2(encoding=3, text=["Coldplay"]))
    frames.add(mutagen.id3.TXXX(encoding=3, desc="PERFORMER", text=["Coldplay"]))
    tags = tags_of(frames)
    assert (tags.date, tags.number, tags.disc) == ("2009-05-15", 1, 1)
    assert tags.genres == ["Rock", "Britpop", "Live Performance"]
    assert tags.credits == [
        ("artist", "Coldplay"),
        ("album_artist", "Coldplay"),
        ("performer", "Coldplay"),
    ]


def test_scan_records_albums_and_tracks(library: Path, tmp_path: Path) -> None:
    db = tmp_path / "music.db"
    result = run(library, db, exclude=frozenset({Path("Incoming")}))
    assert [a.status for a in result.albums] == ["new", "new", "new"]
    assert verdict(result).startswith("3 albums with 4 tracks recorded; 3 new")
    rows = query(
        db,
        "SELECT folder_artist, title, year, genres, composers, tracks, formats, "
        "lossless, markers FROM album_overview ORDER BY folder",
    )
    assert rows[0] == (
        "Bush",
        "Disc 2",
        1999,
        "Live; Rock",
        None,
        1,
        "mp3",
        0,
        "found",
    )
    assert rows[1] == (
        "Johann Sebastian Bach",
        "Brandenburgische Konzerte",
        1982,
        "Classical",
        "J. S. Bach",
        2,
        "flac",
        1,
        "owned ripped",
    )
    assert rows[2][:2] == ("Various", "Forum West")
    assert query(db, "SELECT genre, albums FROM genre_overview") == [
        ("Live", 2),
        ("Rock", 2),
        ("Classical", 1),
    ]
    assert query(db, "SELECT number, title FROM track WHERE album_tag LIKE 'Brand%'") == [
        (1, "Allegro"),
        (2, "Andante"),
    ]
    con = sqlite3.connect(db)
    assert get_meta(con, "source") == str(library)
    assert get_meta(con, "exclude") == '["Incoming"]'
    con.close()


def test_rescan_reads_only_changes(library: Path, tmp_path: Path) -> None:
    db = tmp_path / "music.db"
    run(library, db, exclude=frozenset({Path("Incoming")}))
    reads: list[str] = []

    def counting(path: Path) -> AudioFile:
        reads.append(path.name)
        return fake_read(path)

    result = scan(None, db, read=counting)
    assert reads == []
    assert all(a.status == "unchanged" for a in result.albums)

    changed = library / ALBUM / "02 Andante.flac"
    changed.write_bytes(b"B" * 100)
    os.utime(changed, ns=(1, 1))
    write(library / ALBUM / "03 Presto.flac", b"p" * 100)
    (library / DISC / "01 Warm Machine.mp3").unlink()
    write(library / DISC / "01 Warm Machine.flac", b"w" * 100)
    result = scan(None, db, read=counting)
    assert sorted(reads) == ["01 Warm Machine.flac", "02 Andante.flac", "03 Presto.flac"]
    by_folder = {a.folder: a for a in result.albums}
    assert by_folder[ALBUM.as_posix()].status == "changed"
    assert by_folder[ALBUM.as_posix()].counts() == "1 new, 1 changed"
    assert by_folder[DISC.as_posix()].counts() == "1 new, 1 gone"
    assert by_folder[VARIOUS.as_posix()].status == "unchanged"
    assert query(db, "SELECT file FROM track ORDER BY file") == [
        ("01 Allegro.flac",),
        ("01 Intro.flac",),
        ("01 Warm Machine.flac",),
        ("02 Andante.flac",),
        ("03 Presto.flac",),
    ]


def test_gone_albums_are_removed_within_limits(library: Path, tmp_path: Path) -> None:
    db = tmp_path / "music.db"
    run(library, db, exclude=frozenset({Path("Incoming")}))
    for p in (library / VARIOUS).iterdir():
        p.unlink()
    (library / VARIOUS).rmdir()
    result = scan(None, db, read=fake_read)
    assert [a.folder for a in result.having("gone")] == [VARIOUS.as_posix()]
    assert query(db, "SELECT COUNT(*) FROM album") == [(2,)]
    assert query(db, "SELECT name FROM artist WHERE name = 'Various'") == []

    empty = tmp_path / "empty"
    (empty / "Incoming").mkdir(parents=True)
    with pytest.raises(Refusal, match="2 of 2 recorded albums are gone"):
        scan(empty, db, read=fake_read)
    assert query(db, "SELECT COUNT(*) FROM album") == [(2,)]
    scan(empty, db, read=fake_read, allow_missing=True)
    assert query(db, "SELECT COUNT(*) FROM album, artist, genre") == [(0,)]


def test_dry_run_writes_nothing(library: Path, tmp_path: Path) -> None:
    db = tmp_path / "music.db"
    result = run(library, db, dry_run=True)
    assert len(result.having("new")) == 4
    assert not db.exists()
    run(library, db)
    write(library / ALBUM / "03 Presto.flac", b"p" * 100)
    result = run(library, db, dry_run=True)
    assert len(result.having("changed")) == 1
    assert query(db, "SELECT COUNT(*) FROM track") == [(5,)]


def test_unreadable_file_is_recorded_with_error(library: Path, tmp_path: Path) -> None:
    db = tmp_path / "music.db"
    write(library / VARIOUS / "02 Broken.flac", b"bad")
    result = run(library, db)
    assert [a.folder for a in result.errors] == [VARIOUS.as_posix()]
    assert verdict(result).endswith("1 with errors, see the warnings above")
    assert query(db, "SELECT format, error FROM track WHERE file = '02 Broken.flac'") == [
        ("flac", "broken")
    ]


def test_refusals(library: Path, tmp_path: Path) -> None:
    db = tmp_path / "music.db"
    with pytest.raises(Refusal, match="no source yet"):
        run(None, db)
    with pytest.raises(Refusal, match="no such directory"):
        run(tmp_path / "nowhere", db)
    with pytest.raises(Refusal, match="--exclude Nope: no such folder"):
        run(library, db, exclude=frozenset({Path("Nope")}))
    with pytest.raises(Refusal, match="its directory does not exist"):
        run(library, tmp_path / "no" / "music.db")
    (tmp_path / "text.db").write_text("not a database")
    with pytest.raises(Refusal, match="not a threnody database"):
        run(library, tmp_path / "text.db")


def test_open_db_rejects_newer_format(tmp_path: Path) -> None:
    db = tmp_path / "music.db"
    con = open_db(db)
    con.execute("UPDATE meta SET value = '99' WHERE key = 'format'")
    con.commit()
    con.close()
    with pytest.raises(Exception, match="newer threnody"):
        open_db(db)


def test_unreadable_folder_keeps_its_albums(library: Path, tmp_path: Path) -> None:
    db = tmp_path / "music.db"
    run(library, db, exclude=frozenset({Path("Incoming")}))
    locked = library / "Various"
    locked.chmod(0)
    try:
        found = find_albums(library)
        assert VARIOUS not in found.albums
        assert list(found.unreadable) == [Path("Various")]
        result = scan(None, db, read=fake_read)
    finally:
        locked.chmod(0o755)
    assert result.having("gone") == []
    assert [a.folder for a in result.having("error")] == ["Various"]
    assert query(db, "SELECT COUNT(*) FROM album") == [(3,)]
