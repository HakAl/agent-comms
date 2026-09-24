from __future__ import annotations

from ...handoff import session_start_text_from_env

NAME = "handoff"


def register(subparsers) -> None:
    handoff = subparsers.add_parser(NAME)
    handoff_sub = handoff.add_subparsers(dest="handoff_command", required=True)

    read = handoff_sub.add_parser("read")
    read.add_argument("--actor-id", required=True)

    session_start = handoff_sub.add_parser("session-start")
    session_start.add_argument(
        "--allow-empty",
        action="store_true",
        help="Return success when no AGENT_COMMS_ACTOR_ID or handoff exists.",
    )


def handle(store, args):
    if args.handoff_command == "read":
        return store.read_handoff(args.actor_id)
    if args.handoff_command == "session-start":
        text = session_start_text_from_env(store)
        if text:
            print(text)
        return None
    raise AssertionError(args.handoff_command)
