"""Regression: `review gates` exit status must reflect recorded verdicts (0o3).

Before this fix `command_gates` recorded every run but returned None, so `main`
returned 0 no matter what verdicts were recorded: a red required gate looked
green to any wrapper reading `$?`. These tests drive the real exit path in
process by calling `review.main([...gates argv...])` and asserting process
meaning (return 0 vs SystemExit(1)) plus the durable record on disk.

Self-contained by construction: it imports only the standard library and the
installed `agent_comms` package, never checkout-local test helpers, so the same
module runs unchanged against a built candidate from outside the checkout.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_comms import review
from agent_comms.reviewing import store


def _green_criterion() -> dict:
    return {
        "id": "unit",
        "claim": "a passing check",
        "check_id": "green",
        "expected": "pass",
        "scope": "gates_exit_aggregation_001",
        "evidence": "",
        "required": True,
    }


def _extra_criterion(argv: list[str]) -> dict:
    return {
        "id": "extra",
        "claim": "an argv-driven check",
        "check_id": "extra",
        "expected": "pass",
        "scope": "gates_exit_aggregation_001",
        "evidence": "",
        "required": True,
        "argv": argv,
    }


class GatesExitAggregationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gates-exit-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.review_root = self.root / "reviews"
        self.review_root.mkdir()
        # No .git under the repo, so run_check skips git_head/git_branch.
        self.repo = self.root / "repo"
        self.repo.mkdir()
        # REVIEW_ROOT is a module global read through review_paths(); redirect it
        # to the temp tree and restore it afterwards.
        self._saved_review_root = store.REVIEW_ROOT
        store.REVIEW_ROOT = self.review_root
        self.addCleanup(setattr, store, "REVIEW_ROOT", self._saved_review_root)

    def write_record(self, dispatch_id: str, dod: list[dict]) -> None:
        # A gates-eligible record is execution-bound: the schema demands a full
        # reviewed_head + worker_evidence + trigger_closed lineage, so a minimal
        # but complete lineage is supplied here to isolate the exit-status
        # behavior under test.
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record = {
            "schema_version": 2,
            "dispatch_id": dispatch_id,
            "state": "executed",
            "repo": str(self.repo),
            "base_commit": None,
            "reviewed_head": "d" * 40,
            "approved_head": None,
            "target_branch": None,
            "expected_producer": "architect",
            "expected_recipient": "worker",
            "intended_dispatches": [
                {
                    "attempt": 1,
                    "idempotency_key": "gates-exit-aggregation-001",
                    "recorded_at": now,
                }
            ],
            "worker_evidence": [
                {
                    "worker_dispatch_id": "dispatch_20260830_000000_00000000",
                    "intent_attempt": 1,
                    "idempotency_key": "gates-exit-aggregation-001",
                    "ledger_db": "/tmp/ledger.db",
                    "closeout": {
                        "protocol": 1,
                        "recorded_by": "gamma-codex-worker",
                        "reply_message_id": "msg_gates_exit",
                        "delta": None,
                    },
                    "verified_at": now,
                    "producer": "gamma-architect",
                    "recipient": "gamma-codex-worker",
                    "status": "closed",
                    "result": "satisfied",
                    "delta_verification": {
                        "snapshot_tree": "a" * 40,
                        "reviewed_head_tree": "b" * 40,
                        "manifest_sha256": "c" * 64,
                        "entries": 1,
                        "status_counts": {"M": 1},
                    },
                    "artifact_bindings": [],
                }
            ],
            "blocked_dispatches": [],
            "superseded_dispatches": [],
            "blocked_redispatch_count": 0,
            "max_blocked_redispatches": 3,
            "brief_path": "brief.md",
            "brief_sha256": None,
            "brief_revision": 0,
            "brief_checks": [],
            "dod": dod,
            "findings": [],
            "gate_runs": [],
            "skips": [],
            "approval": None,
            "respawn_count": 0,
            "max_respawns": 3,
            "history": [],
            "created_at": now,
            "updated_at": now,
            "gate_epoch": 0,
            "trigger_closed": True,
        }
        path = self.review_root / f"{dispatch_id}.json"
        path.write_text(json.dumps(record), encoding="utf-8")

    def read_record(self, dispatch_id: str) -> dict:
        path = self.review_root / f"{dispatch_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def gates(self, dispatch_id: str, *argv: str) -> int:
        return review.main(["gates", "--dispatch-id", dispatch_id, *argv])

    # 1. Mixed ordering regression (the 2026-08-08 shape).
    def test_mixed_order_fail_before_pass_raises_and_records_both(self) -> None:
        self.write_record("mixed", [_green_criterion()])
        with self.assertRaises(SystemExit) as caught:
            self.gates("mixed", "--check", "fail", "green")
        self.assertEqual(caught.exception.code, 1)
        runs = self.read_record("mixed")["gate_runs"]
        self.assertEqual([run["check_id"] for run in runs], ["fail", "green"])
        self.assertEqual([run["verdict"] for run in runs], ["fail", "pass"])
        self.assertEqual([run["epoch"] for run in runs], [0, 0])

    # 2. Single-fail regression (the 2026-08-15 shape).
    def test_single_fail_raises(self) -> None:
        self.write_record("single", [_green_criterion()])
        with self.assertRaises(SystemExit) as caught:
            self.gates("single", "--check", "fail")
        self.assertEqual(caught.exception.code, 1)

    # 3. All-pass invocation returns 0 from main.
    def test_all_pass_returns_zero(self) -> None:
        self.write_record("allpass", [_green_criterion()])
        self.assertEqual(self.gates("allpass", "--check", "green"), 0)
        runs = self.read_record("allpass")["gate_runs"]
        self.assertEqual([run["verdict"] for run in runs], ["pass"])

    # 4. Timeout verdict counts as failure.
    def test_timeout_counts_as_failure(self) -> None:
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        self.write_record("slow", [_extra_criterion(argv)])
        with self.assertRaises(SystemExit) as caught:
            self.gates("slow", "--check", "extra", "--timeout", "1")
        self.assertEqual(caught.exception.code, 1)
        runs = self.read_record("slow")["gate_runs"]
        self.assertEqual([run["verdict"] for run in runs], ["timeout"])
        self.assertIsNone(runs[0]["exit_code"])

    # 5. Skip-only invocation exits 0; the skip is in skips, not gate_runs.
    def test_skip_only_returns_zero(self) -> None:
        self.write_record("skip", [_green_criterion()])
        self.assertEqual(
            self.gates(
                "skip",
                "--check",
                "green",
                "--skip",
                "unittest",
                "--reason",
                "not applicable to this change",
                "--risk",
                "low",
            ),
            0,
        )
        record = self.read_record("skip")
        self.assertEqual([run["check_id"] for run in record["gate_runs"]], ["green"])
        self.assertNotIn("unittest", [run["check_id"] for run in record["gate_runs"]])
        self.assertEqual([skip["check_id"] for skip in record["skips"]], ["unittest"])

    # 6. Evidence-before-exit: every requested run is durably recorded even
    #    though SystemExit(1) was raised.
    def test_evidence_recorded_before_exit(self) -> None:
        self.write_record("evidence", [_green_criterion()])
        with self.assertRaises(SystemExit):
            self.gates("evidence", "--check", "fail", "green")
        record = self.read_record("evidence")
        self.assertEqual(
            [(run["check_id"], run["verdict"]) for run in record["gate_runs"]],
            [("fail", "fail"), ("green", "pass")],
        )
        self.assertEqual(record["state"], "execution_reviewed")

    # 7. Captured stderr names each non-passing check; passing checks are not.
    def test_stderr_names_only_non_passing_checks(self) -> None:
        self.write_record("stderr", [_green_criterion()])
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), self.assertRaises(SystemExit):
            self.gates("stderr", "--check", "fail", "green")
        err = buffer.getvalue()
        self.assertIn("fail fail exit_code=1", err)
        self.assertNotIn("green", err)

    # 8. Pre-flight refusals are unchanged: an unknown check id still raises
    #    ReviewError (main returns 1 via the existing path) and records nothing.
    def test_unknown_check_refuses_via_review_error_and_records_nothing(self) -> None:
        self.write_record("unknown", [_green_criterion()])
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            self.assertEqual(self.gates("unknown", "--check", "bogus"), 1)
        self.assertIn("review: error:", buffer.getvalue())
        record = self.read_record("unknown")
        self.assertEqual(record["gate_runs"], [])
        self.assertEqual(record["state"], "executed")


if __name__ == "__main__":
    unittest.main()
