from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_comms.monitor import enrich_worker_usage, run_monitor_loop, upsert_heartbeat
from agent_comms.store import Store

OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
HUMAN = "01M36YTJV9XBW95S6ZWV47C4RG"


class WorkerUsageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "db.sqlite"
        self.store = Store(self.db)
        self.store.register_actor(HUMAN, "human", "human")
        self.store.register_actor(
            "test-architect", "agent", "architect", team="test", role="architect",
            project_root=str(self.root / "architect"),
        )
        for runtime in ("codex", "claude", "fake"):
            self.store.register_actor(
                f"{runtime}-worker", "agent", runtime, runtime=runtime,
                team="test", role="worker", project_root=str(self.root / runtime),
                owner="test-architect",
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def log(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text)
        return path

    def row(self, name: str, runtime: str = "codex", *, status: str = "closed", log: Path | None = None,
            when: datetime = OLD, created_at: datetime | None = None,
            observed: dict | None = None) -> str:
        values = dict(observed or {})
        if log is not None:
            values["worker_log"] = str(log)
        closed_at = when.isoformat() if status == "closed" else None
        dlq_at = when.isoformat() if status == "dlq" else None
        if status == "spawn_failed_message_landed":
            values["spawn_failed_at"] = when.isoformat()
        with self.store.connection() as conn:
            conn.execute("""
                insert into dispatch_ledger(
                  dispatch_id,idempotency_key,thread_ref,recipient_actor_id,producer_actor_id,
                  originating_actor_id,policy_name,policy_version,policy_issued_by,status,created_at,
                  closed_at,dlq_at,observed_values_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (name, name, name, f"{runtime}-worker", HUMAN, HUMAN, "p", "v", HUMAN,
                  status, (created_at or when).isoformat(), closed_at, dlq_at, json.dumps(values)))
        return name

    def usage(self, dispatch_id: str) -> dict | None:
        with self.store.connection() as conn:
            raw = conn.execute("select observed_values_json from dispatch_ledger where dispatch_id=?", (dispatch_id,)).fetchone()[0]
        return json.loads(raw).get("worker_usage")

    def enrich(self, **kwargs):
        now = kwargs.pop("now", NOW)
        return enrich_worker_usage(self.db, now=now, **kwargs)

    def test_codex_complete_log_enriches_complete(self):
        row = self.row("a", log=self.log("a.log", "x\ntokens used\n1,234\n")); self.enrich()
        self.assertEqual(self.usage(row)["total_tokens"], 1234)
        self.assertEqual(self.usage(row)["total_basis"], "runtime_reported_total")

    def test_second_pass_is_noop(self):
        row = self.row("a", log=self.log("a.log", "tokens used\n2\n")); self.enrich(); first = self.usage(row)
        self.enrich(now=NOW + timedelta(hours=1)); self.assertEqual(self.usage(row)["measured_at"], first["measured_at"])

    def test_claude_result_event_recorded_verbatim(self):
        events = [{"type":"assistant","usage":{"input_tokens":999}}, {"type":"result","usage":{"input_tokens":10,"cache_creation_input_tokens":20,"cache_read_input_tokens":30,"output_tokens":40}}]
        row = self.row("a", "claude", log=self.log("a.log", "\n".join(map(json.dumps, events)))); self.enrich()
        usage = self.usage(row); self.assertEqual(usage["total_tokens"], 70); self.assertEqual(usage["cache_read_input_tokens"], 30)
        self.assertEqual(usage["total_basis"], "input+cache_creation+output")

    def test_claude_log_without_result_event_incomplete(self):
        row = self.row("a", "claude", log=self.log("a.log", '{"type":"assistant","usage":{}}'), when=NOW)
        self.enrich(); self.assertIsNone(self.usage(row)); self.enrich(now=NOW+timedelta(days=2)); self.assertEqual(self.usage(row)["reason"], "no_result_event")

    def test_young_missing_log_skipped(self):
        row = self.row("a", log=self.root/"missing", when=NOW); self.enrich(); self.assertIsNone(self.usage(row))

    def test_aged_missing_log_marks_unavailable(self):
        row = self.row("a", log=self.root/"missing"); self.enrich(); self.assertEqual(self.usage(row)["reason"], "log_missing")

    def test_codex_log_without_trailer_young_then_aged(self):
        row = self.row("a", log=self.log("a.log", "unfinished"), when=NOW); self.enrich(); self.assertIsNone(self.usage(row))
        self.enrich(now=NOW+timedelta(days=2)); self.assertEqual(self.usage(row)["reason"], "no_trailer")

    def test_terminal_row_without_worker_log_key_marks_immediately(self):
        for status in ("closed", "dlq", "spawn_failed_message_landed"):
            row = self.row(status, status=status, when=NOW); self.enrich(); self.assertEqual(self.usage(row)["reason"], "no_worker_log")

    def test_in_flight_row_untouched(self):
        row = self.row("a", status="in_flight", log=self.log("a.log", "tokens used\n2\n")); self.enrich(); self.assertIsNone(self.usage(row))

    def test_dlq_row_enriched(self):
        row = self.row("a", status="dlq", log=self.log("a.log", "tokens used\n2\n")); self.enrich(); self.assertEqual(self.usage(row)["total_tokens"], 2)

    def test_batch_limit_bounds_the_pass(self):
        log = self.log("a.log", "tokens used\n2\n")
        for i in range(3): self.row(f"a{i}", log=log)
        self.enrich(batch=2); self.assertEqual(sum(self.usage(f"a{i}") is not None for i in range(3)), 2)
        self.enrich(batch=2); self.assertEqual(sum(self.usage(f"a{i}") is not None for i in range(3)), 3)

    def test_sibling_observed_keys_preserved(self):
        row = self.row("a", log=self.log("a.log", "tokens used\n2\n"), observed={"producer_paged_at":"yes"}); self.enrich()
        with self.store.connection() as conn: values=json.loads(conn.execute("select observed_values_json from dispatch_ledger where dispatch_id=?",(row,)).fetchone()[0])
        self.assertEqual(values["producer_paged_at"], "yes")

    def test_one_bad_row_does_not_stop_the_pass(self):
        self.row("a", log=self.root); good=self.row("b", log=self.log("b.log", "tokens used\n2\n")); result=self.enrich()
        self.assertEqual(self.usage(good)["total_tokens"], 2); self.assertEqual(result["marked"], 1)

    def test_non_string_worker_log_ages_to_unreadable(self):
        row = self.row("a", observed={"worker_log": None})
        self.enrich()
        self.assertEqual(self.usage(row)["reason"], "unreadable")

    def test_monitor_once_runs_enrichment_after_reconcile(self):
        row=self.row("a", log=self.log("a.log", "tokens used\n2\n")); order=[]
        run_monitor_loop(self.db, once=True, reconcile=lambda *_:(order.append("reconcile") or []),
            heartbeat=lambda p,i:(order.append("heartbeat") or upsert_heartbeat(p, interval=i)),
            enrich=lambda p:(order.append("enrich") or enrich_worker_usage(p, now=NOW)))
        self.assertEqual(order, ["reconcile","heartbeat","enrich"]); self.assertEqual(self.usage(row)["total_tokens"],2); self.assertIsNotNone(self.store.monitor_heartbeat())

    def test_unsupported_runtime_ages_to_unavailable(self):
        row=self.row("a","fake",log=self.log("a.log","anything")); self.enrich(); self.assertEqual(self.usage(row)["reason"],"unsupported_runtime")

    def test_trailer_within_tail_bound(self):
        row=self.row("a",log=self.log("a.log","x"*(70*1024)+"\ntokens used\n9\n")); self.enrich(); self.assertEqual(self.usage(row)["total_tokens"],9)

    def test_backlog_progresses_under_fresh_row_pressure(self):
        log=self.log("a.log","tokens used\n1\n")
        for i in range(3): self.row(f"old{i}",log=log,when=OLD+timedelta(seconds=i))
        self.enrich(batch=2)
        for i in range(2): self.row(f"fresh{i}",log=log,when=NOW+timedelta(seconds=i))
        self.enrich(batch=2)
        self.assertTrue(all(self.usage(f"old{i}") for i in range(3)))
        self.assertTrue(any(self.usage(f"fresh{i}") is None for i in range(2)))

    def test_terminal_ts_per_status(self):
        missing=self.root/"missing"
        self.row("closed",status="closed",log=missing,when=NOW,created_at=OLD)
        self.row("dlq",status="dlq",log=missing,when=OLD)
        self.row("spawn",status="spawn_failed_message_landed",log=missing,when=OLD,created_at=NOW)
        legacy=self.row("legacy",status="in_flight",log=missing,when=OLD)
        with self.store.connection() as conn: conn.execute("update dispatch_ledger set status='closed', closed_at=null where dispatch_id=?",(legacy,))
        self.enrich(batch=10)
        self.assertIsNone(self.usage("closed"))
        self.assertTrue(all(self.usage(x)["reason"]=="log_missing" for x in ("dlq","spawn","legacy")))


if __name__ == "__main__":
    unittest.main()
