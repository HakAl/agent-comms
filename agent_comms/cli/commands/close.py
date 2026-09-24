from __future__ import annotations

NAME = "close"


def register(subparsers) -> None:
    close = subparsers.add_parser(NAME)
    close.add_argument("agent_id")
    close.add_argument("message_id")
    close.add_argument("--response", default="")


def handle(store, args):
    return store.close_message(args.agent_id, args.message_id, args.response)
