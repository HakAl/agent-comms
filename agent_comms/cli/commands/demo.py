from __future__ import annotations

import argparse
import os
from pathlib import Path

from .._helpers import DegradedState

NAME = "demo"


def _timeout(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number of seconds: {value!r}") from None
    if seconds < 1:
        raise argparse.ArgumentTypeError("must be at least 1 second")
    return seconds


def register(subparsers) -> None:
    from ...demo import DEFAULT_TIMEOUT_SECONDS

    demo = subparsers.add_parser(
        NAME,
        help="Dispatch one task to a team's fake worker and show the dispatch, the reply and the final status (no login needed)",
    )
    demo.add_argument("--team", default=None, help="Team whose fake worker to use; default: the only fake worker registered")
    demo.add_argument(
        "--timeout", type=_timeout, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Seconds to wait for a terminal state (default {DEFAULT_TIMEOUT_SECONDS})",
    )


def handle(store, args):
    from ...demo import DemoFailed, run_demo

    try:
        return run_demo(
            store,
            db_path=Path(os.path.abspath(os.path.expanduser(args.db))),
            db_explicit=bool(args.db_explicit),
            team=args.team,
            timeout_seconds=args.timeout,
        )
    except DemoFailed as exc:
        raise DegradedState(exc.payload) from exc
