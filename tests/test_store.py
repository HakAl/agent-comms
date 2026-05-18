import tempfile
import unittest
from pathlib import Path

from agent_comms.store import Store


def register_defaults(store: Store, root: Path) -> None:
    store.register_agent("team-a-architect", "team-a", "architect", str(root), ["research"])
    store.register_agent("team-b-architect", "team-b", "architect", str(root), ["signal-design"])


class StoreTest(unittest.TestCase):
    def test_send_read_ack_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)
            ref = root / "finding.md"
            ref.write_text("finding\n")

            sent = store.send_message(
                from_agent="team-a-architect",
                to_agents=["team-b-architect"],
                subject="Finding",
                body="This affects your lane.",
                refs=[{"path": str(ref), "summary": "Test finding"}],
                requires_ack=True,
            )

            inbox = store.list_inbox("team-b-architect")
            self.assertEqual([message["id"] for message in inbox], [sent["id"]])
            self.assertEqual(inbox[0]["status"], "sent")

            read = store.read_message("team-b-architect", sent["id"])
            self.assertEqual(read["status"], "read")

            ack = store.ack_message("team-b-architect", sent["id"], "Will use this.")
            self.assertEqual(ack["status"], "acknowledged")

            status = store.post_status(
                "team-b-architect",
                "Applied finding to review plan.",
                [str(ref)],
                next_step="Continue audit.",
            )
            self.assertEqual(status["agent_id"], "team-b-architect")
            self.assertEqual(store.list_status()[0]["summary"], "Applied finding to review plan.")

    def test_wait_for_reply_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            register_defaults(store, root)

            result = store.wait_for_reply("team-b-architect", timeout_seconds=0.01, poll_interval_seconds=0.01)

            self.assertEqual(result, {"timed_out": True, "messages": []})


if __name__ == "__main__":
    unittest.main()
