from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import db as db_module
import agent_comms.cli as cli
from agent_comms.handoff import HANDOFF_WARNING, session_start_text_from_env
from agent_comms.schema import ValidationError
from agent_comms.store import Store


def register_handoff_actors(store: Store, root: Path) -> None:
    store.register_agent("team-b-architect", "team-b", "architect", str(root / "shared"), [])
    store.register_agent("alpha-architect", "alpha", "architect", str(root / "shared"), [])
    store.register_agent(
        "team-b-worker", "team-b", "worker", str(root / "shared"), [],
        owner="team-b-architect",
    )
    store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")


class HandoffTest(unittest.TestCase):
    def test_post_read_history_and_empty_body_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_handoff_actors(store, root)

            with self.assertRaises(ValidationError):
                store.post_handoff(
                    "team-b-architect",
                    "   ",
                    [],
                    created_by_actor_id="team-b-architect",
                )

            first = store.post_handoff(
                "team-b-architect",
                "first handoff",
                [],
                created_by_actor_id="team-b-architect",
            )
            second = store.post_handoff(
                "team-b-architect",
                "second handoff",
                [],
                created_by_actor_id="team-b-architect",
            )

            self.assertEqual(store.read_handoff("team-b-architect")["id"], second["id"])
            history = store.list_handoffs("team-b-architect")
            self.assertEqual([row["id"] for row in history], [second["id"], first["id"]])
            self.assertEqual(second["supersedes_handoff_id"], first["id"])

    def test_latest_selection_uses_created_at_then_id_desc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_handoff_actors(store, root)
            first = store.post_handoff(
                "team-b-architect",
                "a",
                [],
                created_by_actor_id="team-b-architect",
            )
            second = store.post_handoff(
                "team-b-architect",
                "b",
                [],
                created_by_actor_id="team-b-architect",
            )
            same_time = "2026-06-28T12:00:00+00:00"
            lower_id, higher_id = sorted([first["id"], second["id"]])
            with store.connection() as conn:
                conn.execute("update handoffs set created_at = ?", (same_time,))
                conn.execute("update handoffs set body = 'lower' where id = ?", (lower_id,))
                conn.execute("update handoffs set body = 'higher' where id = ?", (higher_id,))

            latest = store.read_handoff("team-b-architect")

            self.assertEqual(latest["id"], higher_id)
            self.assertEqual(latest["body"], "higher")

    def test_admin_handoff_post_requires_credential_and_records_operator_author(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            store = Store(db_path)
            register_handoff_actors(store, root)
            body_file = root / "handoff.md"
            body_file.write_text("operator-authored handoff\n")
            token_path = root / "home" / ".agent-comms" / "admin-token"
            token_path.parent.mkdir(parents=True)
            token_path.write_text("secret")
            os.chmod(token_path, 0o600)

            code, payload = self._run_cli(
                [
                    "--db",
                    str(db_path),
                    "admin",
                    "handoff-post",
                    "--target-actor-id",
                    "alpha-architect",
                    "--created-by-actor-id",
                    "01M36YTJV9XBW95S6ZWV47C4RG",
                    "--body-file",
                    str(body_file),
                ],
                {"HOME": str(root / "home")},
            )
            self.assertEqual(code, 2)
            self.assertIn("admin write paths require", payload["error"])

            code, payload = self._run_cli(
                [
                    "--db",
                    str(db_path),
                    "admin",
                    "handoff-post",
                    "--target-actor-id",
                    "alpha-architect",
                    "--created-by-actor-id",
                    "01M36YTJV9XBW95S6ZWV47C4RG",
                    "--body-file",
                    str(body_file),
                ],
                {"HOME": str(root / "home"), "AGENT_COMMS_ADMIN_TOKEN": "secret"},
            )

            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["actor_id"], "alpha-architect")
            self.assertEqual(payload["created_by_actor_id"], "01M36YTJV9XBW95S6ZWV47C4RG")
            self.assertNotEqual(payload["actor_id"], payload["created_by_actor_id"])

    def test_handoff_read_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            store = Store(db_path)
            register_handoff_actors(store, root)
            posted = store.post_handoff(
                "alpha-architect",
                "cli-visible handoff",
                [],
                created_by_actor_id="alpha-architect",
            )

            code, payload = self._run_cli(
                ["--db", str(db_path), "handoff", "read", "--actor-id", "alpha-architect"],
                {},
            )

            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["id"], posted["id"])
            self.assertEqual(payload["body"], "cli-visible handoff")

    def test_handoffs_are_additive_and_guard_still_fails_closed(self) -> None:
        # The handoff table itself is additive: it rides whatever ledger floor
        # is declared (the advance to floor 2 came from dispatch payload
        # transport, not from handoffs), and the newer-ledger guard still
        # fails closed for a default open.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            Store(db_path, is_default_db_open=True).init()
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                self.assertEqual(
                    conn.execute("pragma user_version").fetchone()[0],
                    db_module.LEDGER_SCHEMA_VERSION,
                )
                self.assertIsNotNone(
                    conn.execute(
                        "select name from sqlite_master where type = 'table' and name = 'handoffs'"
                    ).fetchone()
                )

            # Reopening at the declared floor stays idempotent.
            Store(db_path, is_default_db_open=True).init()

            higher_version_db_path = root / "higher-version-agent-comms.sqlite"
            synthetic_newer_floor = db_module.LEDGER_SCHEMA_VERSION + 1
            with contextlib.closing(sqlite3.connect(higher_version_db_path)) as conn:
                with conn:
                    conn.execute(f"pragma user_version = {synthetic_newer_floor}")
                self.assertEqual(
                    conn.execute("pragma user_version").fetchone()[0],
                    synthetic_newer_floor,
                )

            with self.assertRaises(ValidationError):
                Store(higher_version_db_path, is_default_db_open=True).init()

    def test_session_start_uses_actor_env_not_shared_cwd_and_missing_env_is_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            store = Store(db_path)
            register_handoff_actors(store, root)
            store.post_handoff(
                "team-b-architect",
                "local handoff",
                [],
                created_by_actor_id="team-b-architect",
            )
            alpha = store.post_handoff(
                "alpha-architect",
                "alpha handoff",
                [],
                created_by_actor_id="alpha-architect",
            )

            self.assertEqual(session_start_text_from_env(store, {}), "")
            local_text = session_start_text_from_env(store, {"AGENT_COMMS_ACTOR_ID": "team-b-architect"})
            alpha_text = session_start_text_from_env(store, {"AGENT_COMMS_ACTOR_ID": "alpha-architect"})

            self.assertIn(HANDOFF_WARNING, alpha_text)
            self.assertIn(alpha["id"], alpha_text)
            self.assertIn("alpha handoff", alpha_text)
            self.assertNotIn("local handoff", alpha_text)
            self.assertIn("local handoff", local_text)
            self.assertNotEqual(local_text, alpha_text)

    def _run_cli(self, argv: list[str], env: dict[str, str]) -> tuple[int, dict]:
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(stdout):
            code = cli.run(argv)
        return code, json.loads(stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
