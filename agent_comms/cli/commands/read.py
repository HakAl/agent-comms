from __future__ import annotations

NAME = "read"


def register(subparsers) -> None:
    read = subparsers.add_parser(NAME)
    read.add_argument("agent_id")
    read.add_argument("message_id")


def handle(store, args):
    return store.read_message(args.agent_id, args.message_id)
