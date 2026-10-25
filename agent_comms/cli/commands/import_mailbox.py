from __future__ import annotations

from pathlib import Path

from ...db import IMPORT_COMMAND
from ...mailbox_import import OWNER_FLAG_SYNTAX, parse_owner_flags, plan_import, run_import

NAME = IMPORT_COMMAND
NEEDS_STORE = False


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        NAME,
        help="Import a 0.1.0 mailbox (five tables, no actors) into this invocation's ledger, which must not exist yet",
    )
    parser.add_argument("source", help="Path to the 0.1.0 agent-comms.sqlite; it is read, never written")
    parser.add_argument(
        "--owner",
        action="append",
        default=None,
        metavar="WORKER=ARCHITECT",
        help=f"Owner for a worker whose team has no single architect ({OWNER_FLAG_SYNTAX}); repeatable",
    )


def preflight(args) -> None:
    # Everything is validated here, before the CLI would construct a Store:
    # a refusal leaves no target, parent directory, or sidecar behind.
    args.import_plan = plan_import(Path(args.source), Path(args.db), parse_owner_flags(args.owner))


def handle(_store, args):
    return run_import(args.import_plan)
