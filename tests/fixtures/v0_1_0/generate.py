"""Build the 0.1.0 mailbox fixture with the 0.1.0 Store.

Run from a checkout of the commit recorded in README.md:

    python3 tests/fixtures/v0_1_0/generate.py NEW.sqlite

NEW.sqlite must not exist. Timestamps and ids are pinned so the rows are
reproducible. Project roots are
fictional absolute paths; nothing on the generating machine is recorded.
"""

from __future__ import annotations

import gc
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agent_comms import store as store_module  # noqa: E402
from agent_comms.store import Store  # noqa: E402

START = datetime(2025, 1, 2, 9, 0, 0, tzinfo=timezone.utc)
ROOT_A = "/srv/project-a"
ROOT_B = "/srv/project-b"


class _Clock:
    """Advance one minute per call to keep ordering stable."""

    def __init__(self) -> None:
        self.current = START

    def now(self, tz=None) -> datetime:
        self.current += timedelta(minutes=1)
        return self.current


class _Ids:
    def __init__(self) -> None:
        self.counter = 0

    def uuid4(self):
        self.counter += 1
        return mock.Mock(hex=f"{self.counter:08x}" + "0" * 24)


def build(path: Path) -> None:
    if path.exists():
        raise SystemExit(f"refusing to overwrite {path}")
    clock = _Clock()
    ids = _Ids()
    fake_datetime = mock.Mock(wraps=datetime)
    fake_datetime.now = clock.now
    with (
        mock.patch.object(store_module, "datetime", fake_datetime),
        mock.patch.object(store_module, "uuid4", ids.uuid4),
        mock.patch.object(
            store_module, "utc_now", lambda: clock.now().isoformat(timespec="seconds")
        ),
    ):
        _populate(Store(path))
    # The 0.1.0 Store leaves its connections to the garbage collector.
    gc.collect()
    # Fold the WAL back into the main file so one file carries every row.
    conn = sqlite3.connect(path)
    conn.execute("pragma wal_checkpoint(truncate)")
    conn.execute("pragma journal_mode = delete")
    conn.close()


def _populate(store: Store) -> None:
    store.register_agent("team-a-architect", "team-a", "architect", ROOT_A, ["research"])
    store.register_agent("team-a-worker", "team-a", "worker", ROOT_A, ["implementation"])
    store.register_agent("team-b-architect", "team-b", "architect", ROOT_B, ["signal-design"])
    store.register_agent("team-b-reviewer", "team-b", "reviewer", ROOT_B, [])

    # sent (unread), with a file ref and high priority
    finding = store.send_message(
        "team-a-architect",
        ["team-b-architect"],
        "Finding in the parser",
        "The parser drops trailing fields.",
        [{"path": f"{ROOT_A}/notes/finding.md", "summary": "Parser finding"}],
        priority="high",
        requires_ack=True,
    )
    # read: reply in a thread, then read by the recipient
    reply = store.send_message(
        "team-b-architect",
        ["team-a-architect"],
        "Re: Finding in the parser",
        "Confirmed on our side.",
        [{"path": f"{ROOT_B}/docs/parser.md", "summary": ""}],
        parent_message_id=finding["id"],
    )
    store.read_message("team-a-architect", reply["id"])
    # acknowledged, with an ack response; multi-recipient, one copy left unread
    handoff = store.send_message(
        "team-a-architect",
        ["team-a-worker", "team-b-reviewer"],
        "Handoff: parser fix",
        "Please take the parser fix.",
        [],
        priority="blocker",
        requires_ack=True,
    )
    store.ack_message("team-a-worker", handoff["id"], "Taking it.")
    # closed, with and without a close response
    fyi = store.send_message(
        "team-b-reviewer", ["team-b-architect"], "FYI", "Review queue is empty.", [], priority="low"
    )
    store.close_message("team-b-architect", fyi["id"], "Noted.")
    note = store.send_message(
        "team-a-worker", ["team-a-architect"], "Progress", "Halfway done.", [], priority="normal"
    )
    store.close_message("team-a-architect", note["id"])

    store.post_status("team-a-architect", "Planning", [f"{ROOT_A}/plan.md"], next_step="Split work")
    store.post_status("team-a-architect", "Reviewing", [], blocked_on="team-b reply")
    store.post_status("team-a-worker", "Implementing parser fix", [f"{ROOT_A}/parser.py"])
    store.post_status("team-b-architect", "Idle", [])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate.py OUTPUT.sqlite")
    build(Path(sys.argv[1]))
