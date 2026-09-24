"""Run one Codex auth refresh pass (refresh after expiry only).

All refresh behavior lives in ``agent_comms.codex_refresh_driver``: this
command adds no home list, discovery, validation, loop, subprocess, or
migration logic. The monitor starts this command when dispatch reports a
stale Codex token; an operator may also run it by hand.
"""

from __future__ import annotations

NAME = "refresh-codex-auth"


def register(subparsers) -> None:
    subparsers.add_parser(NAME)


def handle(store, args):
    from ... import codex_refresh_driver
    from .._helpers import print_json

    result = codex_refresh_driver.refresh(store)
    # Print one bounded, credential-free machine-readable object, then exit
    # nonzero in this command's own handle when any refresh runner failed. The
    # generic CLI runner is left unchanged (no exit_code channel); returning
    # None avoids a double print.
    print_json(result)
    if not result.get("ok"):
        raise SystemExit(1)
    return None
