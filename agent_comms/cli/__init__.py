from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ._helpers import (
    ADMIN_CREDENTIAL_ERROR,
    DegradedState,
    bootstrap_store,
    expand_path_value,
    load_actor_config,
    print_json,
)
from .settlement_plan import SettlementPlanError
from .commands import COMMAND_MODULES, COMMANDS
from .. import paths
from ..schema import ValidationError
from ..store import Store

DEFAULT_DB = paths.DEFAULT_DB


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-comms")
    parser.add_argument("--db", default=None, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)
    for module in COMMAND_MODULES:
        module.register(sub)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.db_arg_explicit = args.db is not None
    args.db_explicit = args.db_arg_explicit or bool(os.environ.get("AGENT_COMMS_DB"))
    if args.db is None:
        args.db = str(paths.db_path())
    try:
        module = COMMANDS.get(args.command)
        if module is None:
            raise AssertionError(args.command)
        # Preflight runs BEFORE Store/Database construction so a credential or
        # CLI-mode refusal (e.g. the settlement surface) never has any filesystem
        # effect -- no parent directory, database file, schema, or sidecar is
        # created on a refusal. A preflight may stash verified state on ``args``.
        preflight = getattr(module, "preflight", None)
        if preflight is not None:
            preflight(args)
        store = (
            Store(Path(args.db), is_default_db_open=not args.db_explicit)
            if getattr(module, "NEEDS_STORE", True)
            else None
        )
        result = module.handle(store, args)
        if result is not None:
            print_json(result)
    except (ValidationError, SettlementPlanError) as exc:
        # A settlement-plan integrity failure (tamper / forgery / expiry /
        # actor-dispatch mismatch) is a clean operator refusal at this surface,
        # exactly like a ValidationError: it never surfaces the credential/secret.
        print_json({"ok": False, "error": str(exc)})
        return 2
    except DegradedState as exc:
        if not exc.already_printed and exc.payload is not None:
            print_json(exc.payload)
        return 3
    return 0


def main() -> None:
    sys.exit(run())
