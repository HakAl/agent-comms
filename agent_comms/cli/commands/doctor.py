from __future__ import annotations

import os
from pathlib import Path

from .._helpers import DegradedState

NAME = "doctor"
NEEDS_STORE = False


def register(subparsers) -> None:
    doctor = subparsers.add_parser(
        NAME,
        help="Check the install, registry, ledger, runtimes, monitor and MCP seats; exit 3 with fixes when anything fails",
    )
    doctor.add_argument(
        "--clients",
        default=None,
        help="Comma-separated MCP clients to check for every architect (claude, codex); overrides the registry's seats",
    )


def parse_clients(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def handle(_store, args):
    from ...doctor import run_doctor

    result = run_doctor(
        db_path=Path(os.path.abspath(os.path.expanduser(args.db))),
        db_explicit=bool(args.db_explicit),
        clients=parse_clients(args.clients),
    )
    if not result["ok"]:
        raise DegradedState(result)
    return result
