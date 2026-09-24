from __future__ import annotations

NAME = "agents"


def register(subparsers) -> None:
    subparsers.add_parser(NAME)


def handle(store, args):
    return store.list_agents()
