from __future__ import annotations

NAME = "ack"


def register(subparsers) -> None:
    ack = subparsers.add_parser(NAME)
    ack.add_argument("agent_id")
    ack.add_argument("message_id")
    ack.add_argument("--response", default="")


def handle(store, args):
    return store.ack_message(args.agent_id, args.message_id, args.response)
