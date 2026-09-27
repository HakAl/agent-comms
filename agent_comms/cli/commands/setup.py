from __future__ import annotations

import os
import sys
from pathlib import Path

from .._helpers import DegradedState

NAME = "setup"


def register(subparsers) -> None:
    setup = subparsers.add_parser(
        NAME,
        help="Write the registry, register a team, provision its workers and bind the architect seat in your MCP clients",
    )
    setup.add_argument("--project-root", default=None, help="The project the team works in (asked for on a terminal)")
    setup.add_argument("--team", default=None, help="Team name; default: the project directory name")
    setup.add_argument("--runtimes", default=None, help="Comma-separated runtimes the team uses: claude, codex, fake (asked for on a terminal)")
    setup.add_argument("--clients", default=None, help="Comma-separated MCP clients to bind the architect seat in (claude, codex); default: the native runtimes chosen; '' for none")
    setup.add_argument("--human", default=None, help="Your display name; default: the login name")
    setup.add_argument("--human-id", default=None, help="Your actor id; default: a generated ULID")
    setup.add_argument("--replace-seat", action="store_true", help="Hand a client seat bound to another actor over to this team's architect")
    setup.add_argument("--yes", action="store_true", help="Never prompt; omitted arguments take their defaults or are refused")


def handle(store, args):
    from ...setup import console_ask, parse_list, run_setup

    ask = None if args.yes or not sys.stdin.isatty() else console_ask
    result = run_setup(
        store,
        db_path=Path(os.path.abspath(os.path.expanduser(args.db))),
        db_explicit=bool(args.db_explicit),
        project_root=args.project_root,
        team=args.team,
        runtimes=parse_list(args.runtimes),
        clients=parse_list(args.clients),
        human_name=args.human,
        human_id=args.human_id,
        replace_seat=bool(args.replace_seat),
        ask=ask,
    )
    if not result["ok"]:
        raise DegradedState(result)
    return result
