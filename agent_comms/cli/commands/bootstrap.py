from __future__ import annotations

from pathlib import Path

from .._helpers import DEFAULT_CONFIG, bootstrap_store

NAME = "bootstrap"


def register(subparsers) -> None:
    bootstrap = subparsers.add_parser(NAME)
    bootstrap.add_argument("--config", default=str(DEFAULT_CONFIG), help="Agent registry JSON path")
    bootstrap.add_argument("--override-protected")


def handle(store, args):
    overrides = []
    result = {"registered": bootstrap_store(store, Path(args.config), override_protected=args.override_protected, override_records=overrides)}
    if overrides:
        result["override_protected"] = overrides[0] if len(overrides) == 1 else overrides
    return result
