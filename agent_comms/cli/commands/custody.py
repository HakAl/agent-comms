from __future__ import annotations

from pathlib import Path

NAME = "custody"
NEEDS_STORE = False


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        NAME,
        help="Copy the pinned runtime build into custody, where dispatch runs it",
    )
    parser.add_argument("runtime", choices=["codex"])
    parser.add_argument(
        "--from", dest="source", default=None,
        help="Native bundle directory (holding bin/codex); default: the one behind codex on PATH",
    )
    parser.add_argument(
        "--replace", action="store_true",
        help="Replace a custody copy that differs from the certified bundle",
    )


def handle(_store, args):
    from ...custody import fill_codex_custody

    return fill_codex_custody(
        Path(args.source).expanduser() if args.source else None,
        replace=args.replace,
    )
