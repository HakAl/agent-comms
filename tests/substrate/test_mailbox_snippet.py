import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.mailbox import SNIPPET_CHARS
from agent_comms.store import Store


def register_defaults(store: Store, root: Path) -> None:
    store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")
    store.register_agent_actor("team-b-architect", "team-b", "architect", str(root / "team-b-architect"), [])
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
    store.register_agent_actor("alpha-worker", "alpha", "worker", str(root / "alpha-worker"), [], owner="alpha-architect")


class CapturingAdapter:
    def __init__(self) -> None:
        self.contexts: list[DispatchContext] = []

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        self.contexts.append(context)
        return DispatchStart(
            spawn_handle=f"capture:{context.dispatch['dispatch_id']}",
            observed_values={"adapter": "capture"},
        )

    def halt(self, spawn_handle: str, observed_values=None) -> None:
        return None


class MailboxSnippetTest(unittest.TestCase):
    def test_list_inbox_returns_snippet_shape_with_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)
            sent = store.send_message(
                "team-b-architect",
                ["alpha-architect"],
                "Envelope",
                "This body is returned as a snippet in list calls.",
                [],
                priority="high",
                requires_ack=True,
            )

            message = store.list_inbox("alpha-architect")[0]

            self.assertEqual(message["id"], sent["id"])
            self.assertEqual(message["from"], "team-b-architect")
            self.assertEqual(message["to"], "alpha-architect")
            self.assertEqual(message["subject"], "Envelope")
            self.assertEqual(message["refs"], [])
            self.assertEqual(message["priority"], "high")
            self.assertTrue(message["requires_ack"])
            self.assertEqual(message["status"], "sent")
            self.assertIsNone(message["parent_message_id"])
            self.assertIn("created_at", message)
            self.assertEqual(message["body_snippet"], "This body is returned as a snippet in list calls.")
            self.assertEqual(message["body_chars"], len("This body is returned as a snippet in list calls."))
            self.assertNotIn("body", message)

    def test_long_no_whitespace_body_clips_to_snippet_length(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)
            body = "x" * (SNIPPET_CHARS + 75)
            store.send_message("team-b-architect", ["alpha-architect"], "Long", body, [])

            message = store.list_inbox("alpha-architect")[0]

            self.assertEqual(len(message["body_snippet"]), SNIPPET_CHARS)
            self.assertEqual(message["body_chars"], len(body))
            self.assertNotIn("\n", message["body_snippet"])

    def test_short_body_snippet_is_folded_full_body_and_raw_length(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)
            body = "short body"
            store.send_message("team-b-architect", ["alpha-architect"], "Short", body, [])

            message = store.list_inbox("alpha-architect")[0]

            self.assertEqual(message["body_snippet"], body)
            self.assertEqual(message["body_chars"], len(body))

    def test_snippet_folds_unicode_whitespace_and_strips_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)
            body = "  alpha\n\t beta   gamma \n"
            store.send_message("team-b-architect", ["alpha-architect"], "Fold", body, [])

            message = store.list_inbox("alpha-architect")[0]

            self.assertEqual(message["body_snippet"], "alpha beta gamma")
            self.assertEqual(message["body_chars"], len(body.strip()))

    def test_read_message_returns_full_body_and_body_chars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)
            body = "line one\n\tline two   line three"
            sent = store.send_message("team-b-architect", ["alpha-architect"], "Read", body, [])

            message = store.read_message("alpha-architect", sent["id"])

            self.assertEqual(message["body"], body)
            self.assertEqual(message["body_chars"], len(body))
            self.assertNotIn("body_snippet", message)

    def test_list_and_wait_do_not_mark_messages_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)
            sent = store.send_message("team-b-architect", ["alpha-architect"], "Unread", "Stay unread.", [])

            store.list_inbox("alpha-architect")
            store.list_unread()
            store.wait_for_reply("alpha-architect", timeout_seconds=0.01, poll_interval_seconds=0.01)
            with store.connection() as conn:
                status = conn.execute(
                    "select status from message_recipients where message_id = ? and to_agent = ?",
                    (sent["id"], "alpha-architect"),
                ).fetchone()["status"]
            self.assertEqual(status, "sent")

            store.read_message("alpha-architect", sent["id"])
            with store.connection() as conn:
                status = conn.execute(
                    "select status from message_recipients where message_id = ? and to_agent = ?",
                    (sent["id"], "alpha-architect"),
                ).fetchone()["status"]
            self.assertEqual(status, "read")

    def test_wait_for_reply_defaults_to_snippet_and_full_inlines_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)
            body = "Full\nbody\tfor wait."
            store.send_message("team-b-architect", ["alpha-architect"], "Wait", body, [])

            snippet = store.wait_for_reply("alpha-architect", timeout_seconds=0.01, poll_interval_seconds=0.01)
            full = store.wait_for_reply(
                "alpha-architect",
                timeout_seconds=0.01,
                poll_interval_seconds=0.01,
                full=True,
            )

            snippet_message = snippet["messages"][0]
            full_message = full["messages"][0]
            self.assertEqual(snippet_message["body_snippet"], "Full body for wait.")
            self.assertEqual(snippet_message["body_chars"], len(body))
            self.assertNotIn("body", snippet_message)
            self.assertEqual(full_message["body"], body)
            self.assertEqual(full_message["body_chars"], len(body))
            self.assertNotIn("body_snippet", full_message)

    def test_dispatch_context_preserves_full_message_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)
            body = "Spawn task body\nwith whitespace and enough detail for the adapter."
            with patch("agent_comms.dispatch_ledger.require_fresh_module", return_value=None):
                store.dispatch_agent("alpha-architect", "alpha-worker", "snippet-dispatch", "Dispatch", body, [])
                adapter = CapturingAdapter()
                store.start_queued_dispatches(lambda _runtime: adapter)

            message = adapter.contexts[0].message
            self.assertEqual(message["body"], body)
            self.assertEqual(message["body_chars"], len(body))
            self.assertNotIn("body_snippet", message)


if __name__ == "__main__":
    unittest.main()
