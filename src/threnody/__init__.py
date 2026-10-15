"""Inventory of a music library in a SQLite database.

threnody reads the tags and stream properties of every audio file below SOURCE and
keeps them in one SQLite file: albums (one per folder holding audio files), tracks,
genres and the names credited as artist, album artist, composer, performer or
conductor, each with the role the tag gave it. Nothing is interpreted: the folder
an album lives in and the names in its tags are recorded side by side.

A repeated scan reads only files that are new or changed and removes what is gone.
"""

from importlib.metadata import version

__version__ = version("threnody")
