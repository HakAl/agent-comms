from __future__ import annotations

NAME = "wait"


def register(subparsers) -> None:
    wait = subparsers.add_parser(NAME)
    wait.add_argument("agent_id")
    wait.add_argument("--after-message-id")
    wait.add_argument("--timeout", type=float, default=30.0)
    wait.add_argument("--poll-interval", type=float, default=1.0)
    wait.add_argument("--full", action="store_true")


def handle(store, args):
    return store.wait_for_reply(
        args.agent_id,
        after_message_id=args.after_message_id,
        timeout_seconds=args.timeout,
        poll_interval_seconds=args.poll_interval,
        full=args.full,
    )
