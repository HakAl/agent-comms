from __future__ import annotations

from pathlib import Path

NAME = "init"


def register(subparsers) -> None:
    subparsers.add_parser(NAME)


def handle(store, args):
    store.init()
    return {"ok": True, "db": str(Path(args.db).resolve())}
