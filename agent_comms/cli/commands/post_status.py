from __future__ import annotations

NAME = "post-status"


def register(subparsers) -> None:
    status = subparsers.add_parser(NAME)
    status.add_argument("agent_id")
    status.add_argument("--summary", required=True)
    status.add_argument("--file", action="append", default=[])
    status.add_argument("--blocked-on", default="")
    status.add_argument("--next-step", default="")
    status.add_argument("--dispatch-id")
    status.add_argument("--thread-ref")


def handle(store, args):
    return store.post_status(
        args.agent_id,
        args.summary,
        args.file,
        args.blocked_on,
        args.next_step,
        args.dispatch_id,
        args.thread_ref,
    )
