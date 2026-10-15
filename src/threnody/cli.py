"""Command line interface."""

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .scan import Refusal, scan, verdict

log = logging.getLogger("threnody")

DEFAULT_DB = Path("threnody.db")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="threnody",
        description=(
            "Keep an inventory of a music library in a SQLite database: albums, "
            "tracks, genres, credits and the format of every file."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        metavar="FILE",
        help=f"the inventory to work on (default: {DEFAULT_DB} in the current folder)",
    )
    loud = parser.add_mutually_exclusive_group()
    loud.add_argument("-v", "--verbose", action="store_true", help="log every file read")
    loud.add_argument("-q", "--quiet", action="store_true", help="log only problems")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    p = commands.add_parser(
        "scan",
        help="bring the inventory up to date with the library",
        description=(
            "Read every album folder below SOURCE into the inventory. A repeated scan "
            "reads only files that are new or changed and removes what is gone."
        ),
    )
    p.add_argument(
        "source",
        type=Path,
        nargs="?",
        metavar="SOURCE",
        help="library to scan (default: the one the inventory was built from)",
    )
    p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show what would change, write nothing",
    )
    p.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=4,
        help="albums read in parallel (default: 4)",
    )
    p.add_argument(
        "-x",
        "--exclude",
        type=Path,
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "skip this folder of SOURCE (relative to it); may be repeated; "
            "the first scan into a database saves these in it"
        ),
    )
    p.add_argument(
        "--allow-missing",
        action="store_true",
        help="accept any number of albums that are gone from SOURCE",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    level = (
        logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    )
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)
    try:
        result = scan(
            args.source,
            args.db,
            jobs=max(1, args.jobs),
            dry_run=args.dry_run,
            allow_missing=args.allow_missing,
            exclude=frozenset(args.exclude),
        )
    except Refusal as e:
        log.error("%s", e)
        return 2
    if args.dry_run:
        print(f"dry run, nothing written: {verdict(result)}")
    else:
        print(f"{verdict(result)}; inventory in {args.db}")
    return 1 if result.errors else 0
