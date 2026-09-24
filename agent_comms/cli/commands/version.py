from __future__ import annotations

from ...release import release_info

NAME = "version"
NEEDS_STORE = False


def register(subparsers) -> None:
    subparsers.add_parser(NAME)


def handle(_store, args):
    return release_info()
