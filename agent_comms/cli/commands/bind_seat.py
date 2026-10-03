from __future__ import annotations

import os
from pathlib import Path

from .._helpers import DegradedState

NAME = "bind-seat"
NEEDS_STORE = False


def register(subparsers) -> None:
    bind = subparsers.add_parser(
        NAME,
        help="Bind a registered architect's seat in one MCP client (claude or codex) and record it in the registry",
    )
    bind.add_argument("architect", help="The architect actor id, as setup registered it")
    bind.add_argument("--client", required=True, help="The MCP client: claude or codex")
    bind.add_argument("--replace-seat", action="store_true", help="Hand a seat bound to another actor or ledger over to this architect")


def handle(_store, args):
    from ...setup import bind_seat

    result = bind_seat(
        db_path=Path(os.path.abspath(os.path.expanduser(args.db))),
        db_explicit=bool(args.db_explicit),
        architect=args.architect,
        client=args.client,
        replace_seat=bool(args.replace_seat),
    )
    if not result["ok"]:
        raise DegradedState(result)
    return result
