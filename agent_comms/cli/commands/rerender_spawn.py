from __future__ import annotations

from ...spawn import rerender_spawns
from .._helpers import require_admin_credential

NAME = "rerender-spawn"


def register(subparsers) -> None:
    parser = subparsers.add_parser(NAME)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("actor_id", nargs="?")
    selection.add_argument("--runtime")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--override-protected")


def preflight(args) -> None:
    if args.apply:
        require_admin_credential()


def handle(store, args):
    return rerender_spawns(
        store,
        actor_id=args.actor_id,
        runtime=args.runtime,
        apply=args.apply,
        yes=args.yes,
        override_protected=args.override_protected,
    )
