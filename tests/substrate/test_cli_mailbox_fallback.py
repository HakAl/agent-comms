"""The read-side CLI an architect falls back on when MCP is unavailable.

docs/architect-prompt.md documents ``inbox``, ``read``, ``ack``, ``close`` and
``post-status`` as the shell fallback for any registered actor. This exercises
each of them through ``agent_comms.cli.run`` against a scratch ledger, so the
docs test (tests/substrate/test_docs_commands.py) has evidence for every path
and a change to their arguments breaks here before it breaks the docs.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import shutil
import unittest
from pathlib import Path

from agent_comms import cli
from agent_comms.store import Store

HUMAN = "01M36YTJV9XBW95S6ZWV47C4RG"


class CliMailboxFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        # Isolation already moved HOME to a scratch directory; keep the ledger
        # under it rather than in the system temp directory.
        self.root = Path.home() / "cli-fallback-test"
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)
        self.db = self.root / "agent-comms.sqlite"
        store = Store(self.db)
        store.register_actor(HUMAN, "human", "alice")
        store.register_agent("team-a-architect", "team-a", "architect", str(self.root / "project-a"), [])
        store.register_agent("team-b-architect", "team-b", "architect", str(self.root / "project-b"), [])
        sent = store.send_message(
            from_agent="team-b-architect",
            to_agents=["team-a-architect"],
            subject="shared contract changed",
            body="See docs/communication_contract.md.",
            refs=[],
            requires_ack=True,
        )
        self.message_id = sent["id"]

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def run_cli(self, *argv: str):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.run(["--db", str(self.db), *argv])
        text = stdout.getvalue().strip()
        return code, (json.loads(text) if text else None)

    def test_inbox_read_ack_close_and_post_status(self) -> None:
        code, inbox = self.run_cli("inbox", "team-a-architect")
        self.assertEqual(code, 0)
        self.assertEqual([m["id"] for m in inbox], [self.message_id])

        code, message = self.run_cli("read", "team-a-architect", self.message_id)
        self.assertEqual(code, 0)
        self.assertEqual(message["subject"], "shared contract changed")
        self.assertEqual(message["from"], "team-b-architect")

        # A response on ack is refused until a threaded reply carried it to the
        # sender; the lightweight receipt is an ack with no response.
        code, refused = self.run_cli("ack", "team-a-architect", self.message_id, "--response", "updated our lane")
        self.assertEqual(code, 2)
        self.assertIn("threaded reply", refused["error"])
        code, acked = self.run_cli("ack", "team-a-architect", self.message_id)
        self.assertEqual(code, 0)
        self.assertEqual(acked["status"], "acknowledged")
        code, inbox = self.run_cli("inbox", "team-a-architect")
        self.assertEqual((code, inbox), (0, []), "an acknowledged message leaves the unread inbox")

        code, closed = self.run_cli("close", "team-a-architect", self.message_id)
        self.assertEqual(code, 0)
        self.assertEqual(closed["status"], "closed")
        code, inbox = self.run_cli("inbox", "team-a-architect", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(inbox, [], "a closed copy is hidden without --include-closed")
        code, inbox = self.run_cli("inbox", "team-a-architect", "--all", "--include-closed")
        self.assertEqual(code, 0)
        self.assertEqual([m["id"] for m in inbox], [self.message_id])

        code, posted = self.run_cli(
            "post-status", "team-a-architect",
            "--summary", "contract updated", "--next-step", "rerun the suite",
        )
        self.assertEqual(code, 0)
        self.assertEqual(posted["agent_id"], "team-a-architect")
        code, statuses = self.run_cli("status")
        self.assertEqual(code, 0)
        by_actor = {row["agent_id"]: row for row in statuses}
        self.assertEqual(by_actor["team-a-architect"]["next_step"], "rerun the suite")

    def test_read_side_refuses_an_unknown_actor(self) -> None:
        code, result = self.run_cli("inbox", "nobody")
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
