from __future__ import annotations

from pathlib import Path

from ... import paths
from .._helpers import bootstrap_store

NAME = "bootstrap"


def register(subparsers) -> None:
    bootstrap = subparsers.add_parser(NAME)
    bootstrap.add_argument("--config", default=None, help="Agent registry JSON path; default: actors.json under the runtime root")
    bootstrap.add_argument("--override-protected")


def handle(store, args):
    overrides = []
    result = {"registered": bootstrap_store(store, Path(args.config) if args.config else paths.actors_config_path(), override_protected=args.override_protected, override_records=overrides)}
    if overrides:
        result["override_protected"] = overrides[0] if len(overrides) == 1 else overrides
    return result
