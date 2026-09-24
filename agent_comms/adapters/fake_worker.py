from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from ..store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake_worker")
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--reply-body", default="fake-reply: PONG")
    parser.add_argument("--fail-before-close", action="store_true")
    parser.add_argument("--cell-delta", action="store_true")
    parser.add_argument("--stream-markers", action="store_true")
    parser.add_argument("bootstrap_marker", nargs="?", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.stream_markers:
        print("fake-worker-stdout", flush=True)
        print("fake-worker-stderr", file=sys.stderr, flush=True)
        return 0

    if args.fail_before_close:
        return 7

    store = Store(Path(args.db))
    message = store.read_message(args.actor_id, args.message_id)
    if args.cell_delta:
        project_root = Path.cwd()
        subprocess.run(["git", "init", "-q"], cwd=project_root, check=True)
        (project_root / "cell-delta-tracked").write_text("base\n")
        subprocess.run(["git", "add", "cell-delta-tracked"], cwd=project_root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Cell",
                "-c",
                "user.email=cell@example.invalid",
                "commit",
                "-qm",
                "cell base",
            ],
            cwd=project_root,
            check=True,
        )
        (project_root / "cell-delta-tracked").write_text("changed\n")
        (project_root / "cell-delta-untracked").write_text("new\n")
    reply = store.send_message(
        args.actor_id,
        [message["from"]],
        f"Re: {message['subject']}",
        args.reply_body,
        [],
        parent_message_id=args.message_id,
    )
    with store.connection() as conn:
        dispatch = conn.execute(
            "select policy_version from dispatch_ledger where message_id=?",
            (args.message_id,),
        ).fetchone()
    if dispatch is not None and dispatch["policy_version"] == "v2":
        store.close_dispatch(
            args.actor_id,
            message_id=args.message_id,
            result="satisfied",
            reply_message_id=reply["id"],
            summary="fake worker closed trigger",
            delta=args.cell_delta,
        )
    else:
        store.close_message(args.actor_id, args.message_id, "fake worker closed trigger")
    return 0


if __name__ == "__main__":
    sys.exit(main())
