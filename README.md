# threnody

> [!NOTE]
> This is a private tool and does not get the same polish as my other projects.
> You are welcome to use it, but it is written for my own requirements first.

Keeps an inventory of a music library in a SQLite database: every album with
its tracks, genres, credited names and the format, bit rate and sample rate of
every file. The database is meant to be queried, by hand or by a script, for
questions the library itself cannot answer: which genres and decades are well
covered and which are not, which composers and performers are there, how much
of the library is lossless.

## Installation

```
uv tool install /path/to/threnody
```

threnody needs Python 3.11 or later and runs on Linux and macOS.

## Usage

```
threnody [--db FILE] [-v | -q] scan [-n] [-j N] [-x DIR]... [--allow-missing] [SOURCE]
```

- `--db FILE`: the inventory (default: `threnody.db` in the current folder)
- `-n`, `--dry-run`: show what would change, write nothing
- `-j N`, `--jobs N`: albums read in parallel (default 4)
- `-x DIR`, `--exclude DIR`: skip this folder of SOURCE, given relative to it;
  may be repeated (see [Settings](#settings))
- `--allow-missing`: accept any number of albums that are gone from SOURCE
- `-v` logs every file read, `-q` only problems

SOURCE is only read, never written to. Example:

```
threnody --db ~/music.db scan -x Incoming /mnt/nas/music
```

Exit code 0 means everything was recorded, 1 that some files or folders could
not be read (listed as warnings), 2 that the scan did not start.

## Settings

The first scan into a database saves SOURCE and the folders given by
`--exclude` in it, so later scans need neither:

```
threnody --db ~/music.db scan
```

`--exclude` on a later scan adds folders for that scan only. The saved folders
are in the `meta` table under `exclude`, as a JSON list; edit them with
`sqlite3`. A scan stops if a saved folder does not exist in SOURCE.

## Which folders are albums

An album is a folder that holds audio files directly. threnody searches SOURCE
at any depth, so a library laid out as `Artist/Album/` works as well as one with
deeper folders. A subfolder with audio files is an album of its own, whatever
its name: a box set with one folder per disc appears as one album per disc, with
the box folder visible in the album's path.

The top-level folder an album lives in is recorded as its *folder artist*, as
named. What the tags say is recorded separately (see below), so a library that
files classical albums under the composer and compilations under `Various`
loses nothing.

Audio files are those with the suffix `.flac`, `.m4a`, `.mp3`, `.dsf`, `.dff`,
`.ogg`, `.opus`, `.wav`, `.aif` or `.aiff`. Hidden files, NAS housekeeping
folders (`@eaDir`, `#recycle`, ...) and the folders given with `--exclude` are
skipped.

## What is recorded

| Table | Content |
|-------|---------|
| `album` | one row per album folder: path, folder artist, title, year, original year, date, MusicBrainz release id |
| `album_marker` | the `[marker]` tokens of the folder name, such as `owned`, `ripped`, `hires` |
| `track` | one row per audio file: disc and track number, title, format, lossless, duration, bit rate, sample rate, bit depth, channels, size, modification time, plus the album, date and MusicBrainz id tags as written in the file |
| `genre`, `track_genre` | the genres of each track; a tag holding `Rock;Live Performance` gives two |
| `artist`, `track_credit` | every name credited on a track, with the role the tag gave it: `artist`, `album_artist`, `composer`, `performer` or `conductor` |
| `meta` | format version, SOURCE, excluded folders, time of the last scan |

An album's title, date and MusicBrainz id are the values most of its tracks
agree on; a folder whose files have no tags gets them from the folder name. The
year is the first four digits of the date, `original_year` those of the
`originaldate` tag where there is one.

Nothing is interpreted. "Ludwig van Beethoven; Alfred Brendel" in an artist tag
gives two `artist` credits, not a composer and a performer; `Various` as a
folder artist stays `Various`. If the tags are improved later, a scan picks the
change up.

Views for the usual questions:

| View | Content |
|------|---------|
| `album_overview` | one line per album with genres, album artists, composers, track count, minutes, formats, lowest bit rate, markers |
| `genre_overview` | albums, tracks and hours per genre |
| `decade_overview` | albums per decade of the original year, or the year where there is none |
| `artist_overview` | albums, tracks, hours and genres per folder artist |
| `credit_overview` | albums per credited name and role |
| `album_genre`, `album_credit` | the genres and credits of each album, taken from its tracks |

For example, the composers with the most albums:

```
sqlite3 music.db "SELECT artist, albums FROM credit_overview WHERE role = 'composer' LIMIT 20"
```

## Repeated scans

A scan compares each file's size and modification time with the inventory and
reads only new or changed files, so a scan over an unchanged library takes
seconds. Albums and files that are gone from SOURCE are removed from the
inventory; a renamed folder counts as gone and new.

If more than 5% of the recorded albums (at least 3) are gone at once, the scan
stops before writing anything. That is usually an unmounted or incomplete share.
Pass `--allow-missing` if the albums were removed on purpose.

A file whose tags cannot be read is still recorded with its name, size and
suffix, and the scan reports it as a warning.

## Development

```
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
