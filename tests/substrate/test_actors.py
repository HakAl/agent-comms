import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_comms.schema import ValidationError
from agent_comms.store import Store


class ActorSchemaTest(unittest.TestCase):
    def test_register_agent_rejects_non_canonical_actor_id(self) -> None:
        bad_ids = [
            "Alpha-Worker",
            "a_b",
            "-a",
            "a-",
            "a--b",
            "a.b",
            "a/b",
            " alpha-worker ",
            "nul",
            "con",
            "com1",
            "lpt1",
            "a" * 65,
        ]
        for actor_id in bad_ids:
            with self.subTest(actor_id=actor_id):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    store = Store(root / "agent-comms.sqlite")

                    with self.assertRaises(ValidationError):
                        store.register_agent(
                            actor_id, "alpha", "worker", str(root), [], owner="alpha-architect"
                        )

    def test_register_agent_accepts_canonical_actor_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            store.register_agent(
                "alpha-architect", "alpha", "architect", str(root), []
            )

            registered = store.register_agent(
                "alpha-codex-worker",
                "alpha",
                "worker",
                str(root),
                [],
                owner="alpha-architect",
            )

            self.assertEqual(registered["agent_id"], "alpha-codex-worker")
            self.assertEqual(store.list_actors()[1]["id"], "alpha-codex-worker")

    def test_register_human_system_charset_rule_not_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")

            human = store.register_actor("01JUPPERCASE", "human", "alice")
            system = store.register_actor("SYS_UPPERCASE", "system", "cron")

            self.assertEqual(human["actor_id"], "01JUPPERCASE")
            self.assertEqual(system["actor_id"], "SYS_UPPERCASE")
            with self.assertRaisesRegex(ValidationError, "opaque"):
                store.register_actor("human-alice", "human", "alice")
            with self.assertRaisesRegex(ValidationError, "opaque"):
                store.register_actor("system-cron", "system", "cron")

    def test_register_agent_populates_actor_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")

            store.register_agent("alpha-architect", "alpha", "architect", str(root), ["signal-design"])

            actors = store.list_actors()
            self.assertEqual(len(actors), 1)
            self.assertEqual(actors[0]["id"], "alpha-architect")
            self.assertEqual(actors[0]["kind"], "agent")
            self.assertEqual(actors[0]["display_name"], "alpha-architect")
            self.assertEqual(actors[0]["team"], "alpha")
            self.assertEqual(actors[0]["role"], "architect")

    def test_register_human_and_system_actor_require_opaque_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")

            human = store.register_actor("01HUMAN", "human", "alice")
            system = store.register_actor("01SYSTEM", "system", "cron:daily_inbox_sweep", system_class="cron")

            self.assertEqual(human["kind"], "human")
            self.assertEqual(system["kind"], "system")
            with self.assertRaisesRegex(ValidationError, "opaque"):
                store.register_actor("human:alice", "human", "alice")
            with self.assertRaisesRegex(ValidationError, "opaque"):
                store.register_actor("human-alice", "human", "alice")
            with self.assertRaisesRegex(ValidationError, "opaque"):
                store.register_actor("system:cron", "system", "cron")
            with self.assertRaisesRegex(ValidationError, "opaque"):
                store.register_actor("system-cron", "system", "cron")

    def test_non_agent_actor_rejects_agent_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")

            with self.assertRaisesRegex(ValidationError, "non-agent actors"):
                store.register_actor(
                    "01HUMAN",
                    "human",
                    "alice",
                    team="alpha",
                    role="operator",
                    project_root=temp_dir,
                )

    def test_dispatch_and_session_tables_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent-comms.sqlite"
            store = Store(db_path)
            store.init()
            store.init()

            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                with conn:
                    tables = {
                        row[0]
                        for row in conn.execute(
                            "select name from sqlite_master where type = 'table'"
                        ).fetchall()
                    }
                    self.assertIn("actors", tables)
                    self.assertIn("dispatch_ledger", tables)
                    # No kept code reads architect sessions; the table is not created.
                    self.assertNotIn("architect_sessions", tables)
                    dispatch_columns = {
                        row[1]
                        for row in conn.execute("pragma table_info(dispatch_ledger)").fetchall()
                    }
                    self.assertIn("idempotency_key", dispatch_columns)
                    self.assertIn("override_reason", dispatch_columns)
                    self.assertIn("observed_values_json", dispatch_columns)
                    status_columns = {
                        row[1]
                        for row in conn.execute("pragma table_info(statuses)").fetchall()
                    }
                    self.assertIn("dispatch_id", status_columns)
                    self.assertIn("thread_ref", status_columns)

    def test_post_status_records_optional_dispatch_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            store.register_agent("alpha-architect", "alpha", "architect", str(root), [])

            store.post_status(
                "alpha-architect",
                "Heartbeat",
                [],
                dispatch_id="dispatch_123",
                thread_ref="msg_123",
            )

            status = store.list_status()[0]
            self.assertEqual(status["dispatch_id"], "dispatch_123")
            self.assertEqual(status["thread_ref"], "msg_123")


if __name__ == "__main__":
    unittest.main()
