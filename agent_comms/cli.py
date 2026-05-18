from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .schema import ValidationError
from .store import Store

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "agent-comms.sqlite"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "agents.json"


def parse_json_list(value: str) -> list:
    if not value:
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise argparse.ArgumentTypeError("value must be a JSON list")
    return parsed


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def print_message_table(messages: list[dict]) -> None:
    if not messages:
        print("No unread messages.")
        return
    columns = ("to", "priority", "ack", "created", "id", "subject")
    rows = [
        (
            message["to"],
            message["priority"],
            "yes" if message["requires_ack"] else "no",
            message["created_at"],
            message["id"],
            message["subject"],
        )
        for message in messages
    ]
    widths = [
        max(len(columns[index]), *(len(str(row[index])) for row in rows))
        for index in range(len(columns))
    ]
    print("  ".join(columns[index].ljust(widths[index]) for index in range(len(columns))))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(row[index]).ljust(widths[index]) for index in range(len(columns))))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-comms")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")

    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--config", default=str(DEFAULT_CONFIG), help="Agent registry JSON path")

    register = sub.add_parser("register")
    register.add_argument("agent_id")
    register.add_argument("--team", required=True)
    register.add_argument("--role", default="architect")
    register.add_argument("--project-root", required=True)
    register.add_argument("--capability", action="append", default=[])

    sub.add_parser("agents")

    send = sub.add_parser("send")
    send.add_argument("--from-agent", required=True)
    send.add_argument("--to", action="append", required=True)
    send.add_argument("--subject", required=True)
    send.add_argument("--body", required=True)
    send.add_argument("--ref", action="append", default=[], help="Path ref; repeatable")
    send.add_argument("--ref-summary", action="append", default=[], help="Summary for matching --ref")
    send.add_argument("--priority", default="normal")
    send.add_argument("--requires-ack", action="store_true")
    send.add_argument("--parent-message-id")

    inbox = sub.add_parser("inbox")
    inbox.add_argument("agent_id")
    inbox.add_argument("--all", action="store_true", help="Include read and acknowledged messages")
    inbox.add_argument("--include-closed", action="store_true")
    inbox.add_argument("--limit", type=int, default=20)

    unread = sub.add_parser("unread")
    unread.add_argument("--limit", type=int, default=100)
    unread.add_argument("--json", action="store_true", help="Print raw JSON instead of a compact table")

    read = sub.add_parser("read")
    read.add_argument("agent_id")
    read.add_argument("message_id")

    ack = sub.add_parser("ack")
    ack.add_argument("agent_id")
    ack.add_argument("message_id")
    ack.add_argument("--response", default="")

    close = sub.add_parser("close")
    close.add_argument("agent_id")
    close.add_argument("message_id")
    close.add_argument("--response", default="")

    wait = sub.add_parser("wait")
    wait.add_argument("agent_id")
    wait.add_argument("--after-message-id")
    wait.add_argument("--timeout", type=float, default=30.0)
    wait.add_argument("--poll-interval", type=float, default=1.0)

    status = sub.add_parser("post-status")
    status.add_argument("agent_id")
    status.add_argument("--summary", required=True)
    status.add_argument("--file", action="append", default=[])
    status.add_argument("--blocked-on", default="")
    status.add_argument("--next-step", default="")

    sub.add_parser("status")
    return parser


def refs_from_args(paths: list[str], summaries: list[str]) -> list[dict[str, str]]:
    refs = []
    for index, path in enumerate(paths):
        summary = summaries[index] if index < len(summaries) else ""
        refs.append({"path": path, "summary": summary})
    return refs


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(Path(args.db))

    try:
        if args.command == "init":
            store.init()
            print_json({"ok": True, "db": str(Path(args.db).resolve())})
        elif args.command == "bootstrap":
            config = json.loads(Path(args.config).read_text())
            registered = []
            for agent_id, entry in config.get("agents", {}).items():
                registered.append(
                    store.register_agent(
                        agent_id,
                        entry["team"],
                        entry.get("role", "architect"),
                        entry["project_root"],
                        entry.get("capabilities", []),
                    )
                )
            print_json({"registered": registered})
        elif args.command == "register":
            print_json(store.register_agent(args.agent_id, args.team, args.role, args.project_root, args.capability))
        elif args.command == "agents":
            print_json(store.list_agents())
        elif args.command == "send":
            print_json(
                store.send_message(
                    from_agent=args.from_agent,
                    to_agents=args.to,
                    subject=args.subject,
                    body=args.body,
                    refs=refs_from_args(args.ref, args.ref_summary),
                    priority=args.priority,
                    requires_ack=args.requires_ack,
                    parent_message_id=args.parent_message_id,
                )
            )
        elif args.command == "inbox":
            print_json(
                store.list_inbox(
                    args.agent_id,
                    unread_only=not args.all,
                    include_closed=args.include_closed,
                    limit=args.limit,
                )
            )
        elif args.command == "unread":
            messages = store.list_unread(limit=args.limit)
            if args.json:
                print_json(messages)
            else:
                print_message_table(messages)
        elif args.command == "read":
            print_json(store.read_message(args.agent_id, args.message_id))
        elif args.command == "ack":
            print_json(store.ack_message(args.agent_id, args.message_id, args.response))
        elif args.command == "close":
            print_json(store.close_message(args.agent_id, args.message_id, args.response))
        elif args.command == "wait":
            print_json(
                store.wait_for_reply(
                    args.agent_id,
                    after_message_id=args.after_message_id,
                    timeout_seconds=args.timeout,
                    poll_interval_seconds=args.poll_interval,
                )
            )
        elif args.command == "post-status":
            print_json(store.post_status(args.agent_id, args.summary, args.file, args.blocked_on, args.next_step))
        elif args.command == "status":
            print_json(store.list_status())
        else:
            raise AssertionError(args.command)
    except ValidationError as exc:
        print_json({"ok": False, "error": str(exc)})
        return 2

    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
