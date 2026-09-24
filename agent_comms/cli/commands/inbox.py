from __future__ import annotations

NAME = "inbox"


def register(subparsers) -> None:
    inbox = subparsers.add_parser(NAME)
    inbox.add_argument("agent_id")
    inbox.add_argument("--all", action="store_true", help="Include read and acknowledged messages")
    inbox.add_argument("--include-closed", action="store_true")
    inbox.add_argument("--limit", type=int, default=20)


def handle(store, args):
    return store.list_inbox(
        args.agent_id,
        unread_only=not args.all,
        include_closed=args.include_closed,
        limit=args.limit,
    )
