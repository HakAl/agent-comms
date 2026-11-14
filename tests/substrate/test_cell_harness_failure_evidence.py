"""A failed cell assertion carries the worker's own evidence.

A cell's worker log and event stream live in the cell's temporary directory
and are gone when the test ends, so the assertion message is the only place
they can survive a certification run.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import tempfile
import unittest
from pathlib import Path

import tests.dispatch_cell_harness as harness


def _dispatch(root: Path, *, log: str | None, events: str | None, **fields: object) -> dict:
    observed: dict = {"closeout": {"result": "blocked", "blocked_reason": "no shell"}}
    if log is not None:
        (root / "worker.log").write_text(log)
        observed["worker_log"] = str(root / "worker.log")
    if events is not None:
        (root / "worker.events.jsonl").write_text(events)
        observed["worker_events"] = str(root / "worker.events.jsonl")
    return {
        "dispatch_id": "dispatch_x",
        "message_id": "msg_x",
        "status": "closed",
        "result": "blocked",
        "failure_reason": None,
        "observed_values": observed,
        **fields,
    }


class WorkerEvidenceTest(unittest.TestCase):
    def test_tail_of_log_and_events_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dispatch = _dispatch(
                root,
                log="early line\n" + "x" * 50_000 + "\nlast log line\n",
                events=json.dumps({"type": "turn.completed", "marker": "last-event"}) + "\n",
            )
            text = harness.worker_evidence(dispatch)
        self.assertIn("last log line", text)
        self.assertIn("last-event", text)
        self.assertNotIn("early line", text)
        self.assertIn("'result': 'blocked'", text)
        self.assertIn("no shell", text)
        self.assertLess(len(text), 3 * harness.WORKER_EVIDENCE_TAIL_BYTES)

    def test_missing_files_are_named_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dispatch = _dispatch(root, log=None, events=None)
            dispatch["observed_values"]["worker_log"] = str(root / "absent.log")
            text = harness.worker_evidence(dispatch)
        self.assertIn("absent.log", text)
        self.assertIn("unreadable", text)
        self.assertIn("worker_events: not recorded", text)

    def test_a_row_without_observed_values_still_reports(self) -> None:
        text = harness.worker_evidence({"dispatch_id": "d", "status": "dlq", "observed_values": None})
        self.assertIn("'status': 'dlq'", text)
        self.assertIn("worker_log: not recorded", text)

    def test_token_shaped_text_is_redacted(self) -> None:
        jwt = "ey" + "J" + "a" * 30 + "." + "b" * 30 + "." + "c" * 30
        key = "sk" + "-ant-" + "d" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dispatch = _dispatch(root, log=f"token {jwt} and {key} end\n", events=None)
            text = harness.worker_evidence(dispatch, secrets=("plain-secret",))
            dispatch = _dispatch(root, log="value plain-secret here\n", events=None)
            exact = harness.worker_evidence(dispatch, secrets=("plain-secret", ""))
        self.assertNotIn(jwt, text)
        self.assertNotIn(key, text)
        self.assertIn("token [redacted] and [redacted] end", text)
        self.assertNotIn("plain-secret", exact)
        self.assertIn("value [redacted] here", exact)


class AssertionEvidenceTest(unittest.TestCase):
    def _harness(self, root: Path) -> harness.DispatchCellHarness:
        return harness.make_fake_harness(root)

    def test_reply_assertions_append_worker_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cell = self._harness(root)
            self.addCleanup(cell.close)
            dispatch = _dispatch(root, log="the worker said why\n", events='{"type":"e","m":"evt"}\n')
            for check in (
                lambda: cell.assert_closed_with_parented_reply(dispatch),
                lambda: cell.assert_closed_with_file_backed_reply(dispatch, b""),
            ):
                with self.assertRaises(AssertionError) as raised:
                    check()
                message = str(raised.exception)
                self.assertIn("expected one trigger for msg_x", message)
                self.assertIn("the worker said why", message)
                self.assertIn("evt", message)

    def test_unclosed_dispatch_also_carries_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cell = self._harness(root)
            self.addCleanup(cell.close)
            dispatch = _dispatch(root, log="died early\n", events=None, status="dlq")
            with self.assertRaises(AssertionError) as raised:
                cell.assert_closed_with_parented_reply(dispatch)
        message = str(raised.exception)
        self.assertIn("dispatch did not close", message)
        self.assertIn("died early", message)


if __name__ == "__main__":
    unittest.main()
