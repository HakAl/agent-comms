import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_comms.schema import ValidationError
from agent_comms.store import Store


def register_defaults(store: Store, root: Path) -> None:
    store.register_agent("team-b-architect", "team-b", "architect", str(root), ["research"])
    store.register_agent("alpha-architect", "alpha", "architect", str(root), ["signal-design"])


class StoreTest(unittest.TestCase):
    def test_send_writes_semaphore_with_self_ignoring_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recipient_root = root / "alpha-architect"
            store = Store(root / "agent-comms.sqlite")
            store.register_agent("team-b-architect", "team-b", "architect", str(root), ["research"])
            store.register_agent("alpha-architect", "alpha", "architect", str(recipient_root), ["signal-design"])

            sent = store.send_message(
                "team-b-architect",
                ["alpha-architect"],
                "Finding",
                "This affects your lane.",
                [],
            )

            runtime_dir = recipient_root / ".agent-comms"
            gitignore_path = runtime_dir / ".gitignore"
            semaphore_path = runtime_dir / "alpha-architect" / "new_messages"
            self.assertEqual(gitignore_path.read_text(), "*\n")
            self.assertTrue(semaphore_path.is_file())
            self.assertIn(sent["id"], semaphore_path.read_text())

            gitignore_path.write_text("existing content\n")
            store.send_message(
                "team-b-architect",
                ["alpha-architect"],
                "Follow-up",
                "Leave the existing ignore file untouched.",
                [],
            )

            self.assertEqual(gitignore_path.read_text(), "existing content\n")
            self.assertTrue(semaphore_path.is_file())

    def test_db_path_delegates_to_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent-comms.sqlite"
            store = Store(db_path)

            self.assertEqual(store.db_path, store._db.db_path)
            self.assertIsInstance(type(store).__dict__.get("db_path"), property)
            self.assertNotIn("db_path", store.__dict__)

    def test_send_read_ack_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)
            ref = root / "finding.md"
            ref.write_text("finding\n")

            sent = store.send_message(
                from_agent="team-b-architect",
                to_agents=["alpha-architect"],
                subject="Finding",
                body="This affects your lane.",
                refs=[{"path": str(ref), "summary": "Test finding"}],
                requires_ack=True,
            )

            inbox = store.list_inbox("alpha-architect")
            self.assertEqual([message["id"] for message in inbox], [sent["id"]])
            self.assertEqual(inbox[0]["status"], "sent")
            unread = store.list_unread()
            self.assertEqual([message["id"] for message in unread], [sent["id"]])
            self.assertEqual(unread[0]["to"], "alpha-architect")

            read = store.read_message("alpha-architect", sent["id"])
            self.assertEqual(read["status"], "read")

            store.send_message(
                "alpha-architect",
                ["team-b-architect"],
                "Re: Finding",
                "Will use this.",
                [],
                parent_message_id=sent["id"],
            )

            ack = store.ack_message("alpha-architect", sent["id"], "Will use this.")
            self.assertEqual(ack["status"], "acknowledged")

            closed = store.close_message("alpha-architect", sent["id"], "Done.")
            self.assertEqual(closed["status"], "closed")
            self.assertIn("closed_at", closed)
            self.assertEqual(store.list_inbox("alpha-architect"), [])

            status = store.post_status(
                "alpha-architect",
                "Applied finding to review plan.",
                [str(ref)],
                next_step="Continue audit.",
            )
            self.assertEqual(status["agent_id"], "alpha-architect")
            self.assertEqual(store.list_status()[0]["summary"], "Applied finding to review plan.")

    def test_close_and_ack_normal_messages_without_dispatch_ledger_rows_are_noops(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)

            close_message = store.send_message(
                "team-b-architect",
                ["alpha-architect"],
                "Normal close",
                "This is not a dispatch trigger.",
                [],
            )
            ack_message = store.send_message(
                "team-b-architect",
                ["alpha-architect"],
                "Normal ack",
                "This is not a dispatch trigger.",
                [],
                requires_ack=True,
            )

            closed = store.close_message("alpha-architect", close_message["id"], "")
            acked = store.ack_message("alpha-architect", ack_message["id"], "")

            self.assertEqual(closed["status"], "closed")
            self.assertEqual(acked["status"], "acknowledged")
            with store.connection() as conn:
                dispatch_rows = conn.execute("select count(*) from dispatch_ledger").fetchone()[0]
                recipient_rows = conn.execute(
                    """
                    select message_id, status
                    from message_recipients
                    where message_id in (?, ?)
                    order by message_id
                    """,
                    (close_message["id"], ack_message["id"]),
                ).fetchall()
            self.assertEqual(dispatch_rows, 0)
            self.assertEqual(
                {row["message_id"]: row["status"] for row in recipient_rows},
                {close_message["id"]: "closed", ack_message["id"]: "acknowledged"},
            )

    def _recipient_state(self, store: Store, message_id: str, agent_id: str) -> tuple[str, str | None]:
        with store.connection() as conn:
            row = conn.execute(
                "select status, ack_response from message_recipients where message_id = ? and to_agent = ?",
                (message_id, agent_id),
            ).fetchone()
        return row["status"], row["ack_response"]

    def _peer_message(self, store: Store, *, requires_ack: bool = False) -> dict:
        return store.send_message(
            "team-b-architect", ["alpha-architect"], "Peer", "Please respond.", [],
            requires_ack=requires_ack,
        )

    def _reply(self, store: Store, message_id: str, to_agents: list[str] | None = None) -> dict:
        return store.send_message(
            "alpha-architect", to_agents or ["team-b-architect"], "Re: Peer", "Reply", [],
            parent_message_id=message_id,
        )

    def test_close_peer_message_nonempty_response_without_reply_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")
            register_defaults(store, Path(temp_dir))
            message = self._peer_message(store)
            with self.assertRaises(ValidationError) as raised:
                store.close_message("alpha-architect", message["id"], "Done.")
            error = str(raised.exception)
            self.assertIn("not visible to the sender", error)
            self.assertIn(message["id"], error)
            self.assertIn("team-b-architect", error)
            self.assertIn("parent_message_id", error)
            self.assertEqual(self._recipient_state(store, message["id"], "alpha-architect"), ("sent", None))

    def test_close_peer_message_empty_response_without_reply_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")
            register_defaults(store, Path(temp_dir))
            message = self._peer_message(store)
            store.close_message("alpha-architect", message["id"], "")
            self.assertEqual(self._recipient_state(store, message["id"], "alpha-architect")[0], "closed")

    def test_close_peer_message_nonempty_response_after_reply_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")
            register_defaults(store, Path(temp_dir))
            message = self._peer_message(store)
            self._reply(store, message["id"])
            store.close_message("alpha-architect", message["id"], "Done.")
            self.assertEqual(self._recipient_state(store, message["id"], "alpha-architect"), ("closed", "Done."))

    def test_ack_peer_message_nonempty_response_without_reply_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")
            register_defaults(store, Path(temp_dir))
            message = self._peer_message(store, requires_ack=True)
            with self.assertRaises(ValidationError) as raised:
                store.ack_message("alpha-architect", message["id"], "Got it.")
            error = str(raised.exception)
            for text in ("not visible to the sender", message["id"], "team-b-architect", "parent_message_id", "ack_message"):
                self.assertIn(text, error)
            self.assertEqual(self._recipient_state(store, message["id"], "alpha-architect"), ("sent", None))

    def test_ack_peer_message_empty_response_without_reply_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")
            register_defaults(store, Path(temp_dir))
            message = self._peer_message(store, requires_ack=True)
            store.ack_message("alpha-architect", message["id"], "")
            self.assertEqual(self._recipient_state(store, message["id"], "alpha-architect")[0], "acknowledged")

    def test_ack_peer_message_nonempty_response_after_reply_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")
            register_defaults(store, Path(temp_dir))
            message = self._peer_message(store, requires_ack=True)
            self._reply(store, message["id"])
            store.ack_message("alpha-architect", message["id"], "Got it.")
            self.assertEqual(self._recipient_state(store, message["id"], "alpha-architect"), ("acknowledged", "Got it."))

    def test_close_peer_message_reply_to_third_party_does_not_satisfy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)
            store.register_agent("echo-architect", "echo", "architect", str(root), ["echo"])
            message = self._peer_message(store)
            self._reply(store, message["id"], ["echo-architect"])
            with self.assertRaises(ValidationError):
                store.close_message("alpha-architect", message["id"], "Done.")
            self._reply(store, message["id"], ["echo-architect", "team-b-architect"])
            store.close_message("alpha-architect", message["id"], "Done.")

    def test_close_peer_message_reply_parented_elsewhere_does_not_satisfy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")
            register_defaults(store, Path(temp_dir))
            message = self._peer_message(store)
            other = self._peer_message(store)
            self._reply(store, other["id"])
            with self.assertRaises(ValidationError):
                store.close_message("alpha-architect", message["id"], "Done.")
            self._reply(store, message["id"])
            store.close_message("alpha-architect", message["id"], "Done.")

    def test_close_self_sent_message_with_note_succeeds_without_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")
            register_defaults(store, Path(temp_dir))
            message = store.send_message("team-b-architect", ["team-b-architect"], "Page", "Stale", [], priority="blocker", requires_ack=True)
            store.close_message("team-b-architect", message["id"], "triaged")
            self.assertEqual(self._recipient_state(store, message["id"], "team-b-architect"), ("closed", "triaged"))

    def test_close_peer_message_already_closed_bypasses_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")
            register_defaults(store, Path(temp_dir))
            message = self._peer_message(store)
            self._reply(store, message["id"])
            store.close_message("alpha-architect", message["id"], "note")
            store.close_message("alpha-architect", message["id"], "Done again.")
            self.assertEqual(self._recipient_state(store, message["id"], "alpha-architect"), ("closed", "Done again."))

    def test_ack_peer_message_already_closed_flips_to_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")
            register_defaults(store, Path(temp_dir))
            message = self._peer_message(store)
            self._reply(store, message["id"])
            store.close_message("alpha-architect", message["id"], "note")
            store.ack_message("alpha-architect", message["id"], "late note")
            self.assertEqual(self._recipient_state(store, message["id"], "alpha-architect"), ("acknowledged", "late note"))

    def test_close_peer_message_already_acknowledged_flips_to_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")
            register_defaults(store, Path(temp_dir))
            message = self._peer_message(store, requires_ack=True)
            self._reply(store, message["id"])
            store.ack_message("alpha-architect", message["id"], "note")
            store.close_message("alpha-architect", message["id"], "wrap")
            self.assertEqual(self._recipient_state(store, message["id"], "alpha-architect"), ("closed", "wrap"))

    def test_close_peer_message_multi_recipient_other_recipient_unaffected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)
            store.register_agent("echo-architect", "echo", "architect", str(root), ["echo"])
            message = store.send_message("team-b-architect", ["alpha-architect", "echo-architect"], "Peer", "Please respond.", [])
            self.assertEqual(self._recipient_state(store, message["id"], "echo-architect")[0], "sent")
            self._reply(store, message["id"])
            store.close_message("alpha-architect", message["id"], "Done.")
            self.assertEqual(self._recipient_state(store, message["id"], "echo-architect")[0], "sent")
            store.close_message("echo-architect", message["id"], "")
            self.assertEqual(self._recipient_state(store, message["id"], "echo-architect")[0], "closed")

    def test_wait_for_reply_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)

            result = store.wait_for_reply("alpha-architect", timeout_seconds=0.01, poll_interval_seconds=0.01)

            self.assertEqual(result, {"timed_out": True, "messages": []})

    def test_init_migrates_legacy_message_foreign_keys_to_actors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                with conn:
                    conn.execute("pragma foreign_keys = on")
                    conn.executescript(
                        """
                        create table agents(
                          id text primary key,
                          team text not null,
                          role text not null,
                          project_root text not null,
                          capabilities_json text not null,
                          last_seen_at text not null
                        );
                        create table messages(
                          id text primary key,
                          from_agent text not null,
                          subject text not null,
                          body text not null,
                          refs_json text not null,
                          priority text not null,
                          requires_ack integer not null,
                          created_at text not null,
                          foreign key(from_agent) references agents(id)
                        );
                        create table message_recipients(
                          message_id text not null,
                          to_agent text not null,
                          status text not null,
                          read_at text,
                          acked_at text,
                          closed_at text,
                          ack_response text,
                          primary key(message_id, to_agent),
                          foreign key(message_id) references messages(id),
                          foreign key(to_agent) references agents(id)
                        );
                        create table message_threads(
                          message_id text primary key,
                          parent_message_id text,
                          foreign key(message_id) references messages(id),
                          foreign key(parent_message_id) references messages(id)
                        );
                        """
                    )
            store = Store(db_path)
            store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
            store.register_agent_actor(
                "team-b-architect",
                "team-b",
                "architect",
                str(root / "team-b-architect"),
                [],
            )
            store.register_agent_actor("team-b-worker", "team-b", "worker", str(root / "team-b-worker"), [], owner="team-b-architect")
            store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")

            sent = store.send_message(
                "01M36YTJV9XBW95S6ZWV47C4RG",
                ["alpha-architect"],
                "Human page",
                "This exercises migrated actor FKs.",
                [],
            )

            self.assertEqual(sent["from"], "01M36YTJV9XBW95S6ZWV47C4RG")
            self.assertEqual(store.list_inbox("alpha-architect")[0]["subject"], "Human page")
            dispatch = store.dispatch_agent(
                "team-b-architect",
                "team-b-worker",
                "migration-dispatch-ledger-insert",
                "Worker dispatch",
                "This exercises migrated dispatch ledger FKs.",
                [],
            )
            self.assertEqual(dispatch["recipient_actor_id"], "team-b-worker")
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                with conn:
                    message_fks = conn.execute("pragma foreign_key_list(messages)").fetchall()
                    recipient_fks = conn.execute("pragma foreign_key_list(message_recipients)").fetchall()
                    thread_fks = conn.execute("pragma foreign_key_list(message_threads)").fetchall()
                    dispatch_fks = conn.execute("pragma foreign_key_list(dispatch_ledger)").fetchall()
            self.assertIn("actors", [row[2] for row in message_fks])
            self.assertIn("actors", [row[2] for row in recipient_fks])
            self.assertEqual(
                ["messages", "messages"],
                sorted(row[2] for row in thread_fks if row[3] in {"message_id", "parent_message_id"}),
            )
            self.assertEqual(["messages"], [row[2] for row in dispatch_fks if row[3] == "message_id"])


if __name__ == "__main__":
    unittest.main()
