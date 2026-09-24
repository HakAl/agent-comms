from __future__ import annotations

from .._helpers import print_json, print_message_table

NAME = "unread"


def register(subparsers) -> None:
    unread = subparsers.add_parser(NAME)
    unread.add_argument("--limit", type=int, default=100)
    unread.add_argument("--json", action="store_true", help="Print raw JSON instead of a compact table")


def handle(store, args):
    messages = store.list_unread(limit=args.limit)
    if args.json:
        print_json(messages)
    else:
        print_message_table(messages)
    return None
