from __future__ import annotations

from ...adapters.registry import adapter_for
from ...store import WORKER_DISPATCH_TTL_SECONDS

NAME = "dispatch-start"


def register(subparsers) -> None:
    dispatch_start = subparsers.add_parser(NAME)
    dispatch_start.add_argument("--limit", type=int, default=1)
    dispatch_start.add_argument("--ttl-seconds", type=int, default=WORKER_DISPATCH_TTL_SECONDS)


def handle(store, args):
    return {
        "started": store.start_queued_dispatches(
            adapter_for,
            limit=args.limit,
            ttl_seconds=args.ttl_seconds,
        )
    }
