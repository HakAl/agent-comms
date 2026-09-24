import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_comms.store import Store


class SemaphoreTest(unittest.TestCase):
    def test_send_message_writes_recipient_semaphore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            store.register_agent("sender-architect", "sender", "architect", str(root / "sender"), [])
            store.register_agent("alpha-architect", "alpha", "architect", str(root / "alpha"), [])

            sent = store.send_message(
                "sender-architect",
                ["alpha-architect"],
                "Finding",
                "Please review.",
                [],
            )

            semaphore = root / "alpha" / ".agent-comms" / "alpha-architect" / "new_messages"
            self.assertTrue(semaphore.exists())
            payload = json.loads(semaphore.read_text())
            self.assertEqual(payload["agent_id"], "alpha-architect")
            self.assertEqual(payload["messages"][0]["message_id"], sent["id"])
            self.assertIn("delivered_at", payload["messages"][0])

    def test_send_message_routes_semaphore_through_recipient_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            store.register_agent("sender-architect", "sender", "architect", str(root / "sender"), [])
            store.register_agent("alpha-architect", "alpha", "architect", str(root / "alpha"), [])
            store.register_agent(
                "alpha-worker", "alpha", "worker", str(root / "alpha"), [],
                owner="alpha-architect",
            )

            store.send_message(
                "sender-architect",
                ["alpha-worker"],
                "Wake",
                "Check inbox.",
                [],
            )

            semaphore = root / "alpha" / ".agent-comms" / "alpha-worker" / "new_messages"
            self.assertTrue(semaphore.exists())

    def test_send_message_writes_one_semaphore_per_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            store.register_agent("sender-architect", "sender", "architect", str(root / "sender"), [])
            store.register_agent("alpha-architect", "alpha", "architect", str(root / "alpha"), [])
            store.register_agent("echo-architect", "echo", "architect", str(root / "echo"), [])

            sent = store.send_message(
                "sender-architect",
                ["alpha-architect", "echo-architect"],
                "Broadcast",
                "Two recipients.",
                [],
            )

            for agent_id in ["alpha-architect", "echo-architect"]:
                semaphore = root / agent_id.split("-")[0] / ".agent-comms" / agent_id / "new_messages"
                payload = json.loads(semaphore.read_text())
                self.assertEqual(payload["messages"][0]["message_id"], sent["id"])

    def test_concurrent_sends_leave_valid_semaphore_and_inbox_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            store.register_agent("sender-architect", "sender", "architect", str(root / "sender"), [])
            store.register_agent("alpha-architect", "alpha", "architect", str(root / "alpha"), [])
            errors = []

            def send(index: int) -> None:
                try:
                    store.send_message(
                        "sender-architect",
                        ["alpha-architect"],
                        f"Finding {index}",
                        "Please review.",
                        [],
                    )
                except Exception as exc:  # pragma: no cover - re-raised below.
                    errors.append(exc)

            threads = [threading.Thread(target=send, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            if errors:
                raise errors[0]
            inbox = store.list_inbox("alpha-architect")
            self.assertEqual(len(inbox), 2)
            semaphore = root / "alpha" / ".agent-comms" / "alpha-architect" / "new_messages"
            payload = json.loads(semaphore.read_text())
            self.assertEqual(payload["agent_id"], "alpha-architect")
            self.assertEqual(len(payload["messages"]), 1)
            self.assertFalse(list(semaphore.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
