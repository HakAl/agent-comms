"""The 0.1.0 mailbox fixture stays loadable for the 0.1.0 import work."""

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import sqlite3
import unittest
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "v0_1_0" / "agent-comms.sqlite"


class V010FixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(f"file:{FIXTURE}?mode=ro", uri=True)
        self.addCleanup(self.conn.close)

    def count(self, sql: str) -> int:
        return self.conn.execute(sql).fetchone()[0]

    def test_tables_match_0_1_0_schema(self) -> None:
        tables = {
            row[0]
            for row in self.conn.execute("select name from sqlite_master where type = 'table'")
        }
        self.assertEqual(
            tables, {"agents", "messages", "message_recipients", "message_threads", "statuses"}
        )

    def test_row_counts(self) -> None:
        self.assertEqual(self.count("select count(*) from agents"), 4)
        self.assertEqual(self.count("select count(distinct team) from agents"), 2)
        self.assertEqual(self.count("select count(*) from messages"), 5)
        self.assertEqual(self.count("select count(*) from message_recipients"), 6)
        self.assertEqual(self.count("select count(*) from statuses"), 4)

    def test_every_recipient_status_is_present(self) -> None:
        statuses = {
            row[0] for row in self.conn.execute("select distinct status from message_recipients")
        }
        self.assertEqual(statuses, {"sent", "read", "acknowledged", "closed"})

    def test_every_priority_and_ack_flag_is_present(self) -> None:
        priorities = {row[0] for row in self.conn.execute("select distinct priority from messages")}
        self.assertEqual(priorities, {"low", "normal", "high", "blocker"})
        self.assertEqual(
            {row[0] for row in self.conn.execute("select distinct requires_ack from messages")},
            {0, 1},
        )

    def test_threads_and_refs(self) -> None:
        self.assertEqual(
            self.count("select count(*) from message_threads where parent_message_id is not null"), 1
        )
        self.assertEqual(self.count("select count(*) from messages where refs_json != '[]'"), 2)


if __name__ == "__main__":
    unittest.main()
