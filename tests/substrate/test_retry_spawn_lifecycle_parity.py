"""retry_spawn lifecycle parity with the spawn-commit lifecycle (dead-worker
terminalization stage 1).

These pin that a successful retry dispatch is brought into EXACT parity with the
spawn-commit CAS: the retry MERGES the supervisor identity onto observed
evidence a fast worker already wrote (while preserving retry metadata and prior
diagnostics), then re-reads the row and the recipient copy under a short
BEGIN IMMEDIATE to settle a paused-retry race deterministically:

  - recipient already acknowledged/closed -> ledger directly `closed`;
  - an authenticated same-run worker exit without a close -> ledger directly
    `dlq` with `worker_exited_before_close`;
  - otherwise the exact `spawn_failed_message_landed -> in_flight` CAS.

Stale/missing/malformed old exit evidence must never settle the new retry, and
all cap/concurrent/orphan HALT calls happen AFTER rollback / outside the write
transaction and pass ``result.observed_values`` for supervised authentication;
the cap-race re-enters only a short annotate transaction with an exact status
guard.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.dispatch_ledger import ConcurrencyError, WORKER_DISPATCH_POLICY
from agent_comms.store import Store


def seed_dispatch_actors(store: Store, root: Path) -> None:
    store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
    store.register_agent_actor("alpha-worker", "alpha", "worker", str(root / "alpha-worker"), [], owner="alpha-architect")


class FailingAdapter:
    """Adapter whose dispatch raises so a queued row lands spawn_failed."""

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        raise RuntimeError("adapter spawn failed")

    def halt(self, spawn_handle: str, observed_values=None) -> None:
        return None


class SupervisedStubAdapter:
    """Adapter double that returns a supervisor control identity in observed.

    ``halt`` records the ``(spawn_handle, observed_values)`` it was handed so
    tests can prove the cap/concurrent-race halt path uses the authenticated
    socket identity rather than a PID-parsed signal.
    """

    def __init__(self) -> None:
        self.control_socket = "/nonexistent/agent-comms/run/s/control.sock"
        self.run_token = "run-token-abcdef0123456789"
        self.halt_calls: list[tuple[str, dict | None]] = []

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        return DispatchStart(
            spawn_handle=f"claude:{context.recipient['id']}:4242",
            observed_values={
                "adapter": "claude",
                "control_socket": self.control_socket,
                "run_token": self.run_token,
                "protocol_version": 1,
                "worker_log": "/tmp/worker.log",
            },
        )

    def halt(self, spawn_handle: str, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))


def _ledger_row(store: Store, dispatch_id: str) -> sqlite3.Row:
    with store._db.connection() as conn:
        return conn.execute(
            "select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
        ).fetchone()


def _status_counts(store: Store, producer_actor_id: str) -> dict[str, int]:
    with store._db.connection() as conn:
        rows = conn.execute(
            """
            select status, count(*) as count
            from dispatch_ledger
            where producer_actor_id = ?
            group by status
            """,
            (producer_actor_id,),
        ).fetchall()
    return {row["status"]: row["count"] for row in rows}


def _set_observed_and_lineage(
    store: Store, dispatch_id: str, observed: dict, lineage_claimed_at: str | None
) -> None:
    with store._db.connection() as conn:
        conn.execute(
            """
            update dispatch_ledger
            set observed_values_json = ?, auth_lineage_claimed_at = ?
            where dispatch_id = ?
            """,
            (json.dumps(observed, sort_keys=True), lineage_claimed_at, dispatch_id),
        )


def _set_cap(store: Store, actor_id: str, cap: int) -> None:
    with store._db.connection() as conn:
        conn.execute("update actors set dispatch_cap = ? where id = ?", (cap, actor_id))


def _insert_inflight_sibling(store: Store, suffix: str) -> None:
    """Insert an in_flight sibling row for alpha-architect to fill its cap."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with store._db.connection() as conn:
        conn.execute(
            """
            insert into dispatch_ledger(
              dispatch_id, parent_dispatch_id, idempotency_key, message_id,
              thread_ref, spawn_handle, recipient_actor_id, producer_actor_id,
              originating_actor_id, policy_name, policy_version, policy_issued_by,
              expected_close_by, status, created_at, spawned_at, observed_values_json
            )
            values(?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, 'v1', ?, ?, 'in_flight', ?, ?, '{}')
            """,
            (
                f"dispatch_retry_sibling_{suffix}",
                f"retry-sibling-{suffix}",
                f"retry-sibling-thread-{suffix}",
                f"retry-sibling-handle-{suffix}",
                "alpha-worker",
                "alpha-architect",
                "alpha-architect",
                WORKER_DISPATCH_POLICY,
                "alpha-architect",
                "2099-01-01T00:00:00+00:00",
                now,
                now,
            ),
        )


class RetrySpawnLifecycleParityTest(unittest.TestCase):
    def _store(self, root: Path) -> Store:
        store = Store(root / "agent-comms.sqlite")
        seed_dispatch_actors(store, root)
        return store

    def _spawn_failed(self, store: Store, key: str) -> dict:
        # dispatch_agent with no adapter leaves the row queued; a failing start
        # then lands it in spawn_failed_message_landed with the message retained.
        dispatch = store.dispatch_agent(
            "alpha-architect", "alpha-worker", key, f"S {key}", f"B {key}", []
        )
        with store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set policy_version = 'v1' where dispatch_id = ?",
                (dispatch["dispatch_id"],),
            )
            policy_version = conn.execute(
                "select policy_version from dispatch_ledger where dispatch_id = ?",
                (dispatch["dispatch_id"],),
            ).fetchone()["policy_version"]
        self.assertEqual(policy_version, "v1")
        started = store.start_queued_dispatches(lambda _runtime: FailingAdapter())
        self.assertEqual(started[0]["status"], "spawn_failed_message_landed")
        return dispatch

    def _reply(self, store: Store, dispatch: dict) -> None:
        store.send_message(
            "alpha-worker",
            ["alpha-architect"],
            "Re: dispatch",
            "done",
            [],
            parent_message_id=dispatch["message_id"],
        )

    def test_retry_fast_close_settles_ledger_closed_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self._store(root)
            dispatch = self._spawn_failed(store, "retry-close")

            # A fast worker replies then closes the trigger while the ledger is
            # still spawn_failed_message_landed. The recipient copy is closed even
            # though the ledger row was never in_flight.
            self._reply(store, dispatch)
            store.close_message("alpha-worker", dispatch["message_id"], "Done.")
            # A live lineage claim is present until the retry commit clears it.
            observed = json.loads(_ledger_row(store, dispatch["dispatch_id"])["observed_values_json"])
            _set_observed_and_lineage(
                store, dispatch["dispatch_id"], observed, "2026-07-14T00:00:00+00:00"
            )

            adapter = SupervisedStubAdapter()
            settled = store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: adapter, ttl_seconds=30)

            self.assertEqual(settled["status"], "closed")
            self.assertTrue(settled["spawn_handle"])
            self.assertIsNone(settled["failure_reason"])
            observed = settled["observed_values"]
            # The supervisor identity is MERGED onto the preserved close evidence.
            self.assertEqual(observed["spawn_commit_recipient_terminal"], "closed")
            self.assertEqual(observed["control_socket"], adapter.control_socket)
            self.assertEqual(observed["run_token"], adapter.run_token)
            self.assertIn("spawn_failed_at", observed)  # prior diagnostic preserved
            self.assertEqual(observed["retry_count"], 1)
            self.assertEqual(observed["last_retry_outcome"], "in_flight")
            self.assertTrue(observed["last_retry_at"])
            row = _ledger_row(store, dispatch["dispatch_id"])
            self.assertTrue(row["closed_at"])
            self.assertEqual(row["auth_lineage_claimed_at"], "2026-07-14T00:00:00+00:00")
            # Never published a false in_flight, and no orphan halt was needed.
            self.assertEqual(_status_counts(store, "alpha-architect").get("in_flight", 0), 0)
            self.assertEqual(adapter.halt_calls, [])

    def test_retry_fast_ack_settles_ledger_closed_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self._store(root)
            dispatch = self._spawn_failed(store, "retry-ack")
            self._reply(store, dispatch)
            acked = store.ack_message("alpha-worker", dispatch["message_id"], "Got it.")
            self.assertEqual(acked["status"], "acknowledged")

            adapter = SupervisedStubAdapter()
            settled = store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: adapter, ttl_seconds=30)

            self.assertEqual(settled["status"], "closed")
            self.assertEqual(
                settled["observed_values"]["spawn_commit_recipient_terminal"], "acknowledged"
            )
            self.assertEqual(settled["observed_values"]["run_token"], adapter.run_token)
            self.assertEqual(_status_counts(store, "alpha-architect").get("in_flight", 0), 0)
            self.assertEqual(adapter.halt_calls, [])

    def test_retry_fast_exit_without_close_becomes_early_dlq(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self._store(root)
            dispatch = self._spawn_failed(store, "retry-exit")

            # The supervisor recorded a same-run child exit while the row was
            # spawn_failed_message_landed and the trigger was never closed.
            _set_observed_and_lineage(
                store,
                dispatch["dispatch_id"],
                {
                    "spawn_failed_at": "2026-07-14T00:00:00+00:00",
                    "worker_exit": {
                        "returncode": 0,
                        "source": "wrapper",
                        "run_token": "run-token-abcdef0123456789",
                    },
                },
                "2026-07-14T00:00:00+00:00",
            )

            adapter = SupervisedStubAdapter()
            settled = store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: adapter, ttl_seconds=30)

            self.assertEqual(settled["status"], "dlq")
            self.assertEqual(settled["failure_reason"], "worker_exited_before_close")
            observed = settled["observed_values"]
            self.assertIn("worker_exit", observed)  # preserved
            self.assertEqual(observed["control_socket"], adapter.control_socket)  # merged
            self.assertEqual(
                observed["early_dlq_evidence"]["classification"], "worker_exited_before_close"
            )
            self.assertEqual(observed["retry_count"], 1)
            row = _ledger_row(store, dispatch["dispatch_id"])
            self.assertTrue(row["dlq_at"])
            self.assertEqual(row["auth_lineage_claimed_at"], "2026-07-14T00:00:00+00:00")
            # Early DLQ, not a spawn failure and not a false in_flight.
            counts = _status_counts(store, "alpha-architect")
            self.assertEqual(counts.get("in_flight", 0), 0)
            self.assertEqual(counts.get("spawn_failed_message_landed", 0), 0)
            self.assertEqual(adapter.halt_calls, [])

    def test_retry_inflight_merges_preexisting_observed_and_retry_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self._store(root)
            dispatch = self._spawn_failed(store, "retry-merge")
            # An unrelated observed key written before the retry must survive, as
            # must the spawn_failed_at diagnostic from the prior failure.
            observed = json.loads(_ledger_row(store, dispatch["dispatch_id"])["observed_values_json"])
            observed["preexisting_key"] = "survives"
            _set_observed_and_lineage(store, dispatch["dispatch_id"], observed, None)

            adapter = SupervisedStubAdapter()
            settled = store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: adapter, ttl_seconds=30)

            self.assertEqual(settled["status"], "in_flight")
            self.assertIsNone(settled["failure_reason"])
            self.assertTrue(settled["expected_close_by"])
            observed = settled["observed_values"]
            self.assertEqual(observed["preexisting_key"], "survives")  # not overwritten
            self.assertIn("spawn_failed_at", observed)  # prior diagnostic preserved
            self.assertEqual(observed["run_token"], adapter.run_token)  # merged in
            self.assertEqual(observed["retry_count"], 1)
            self.assertEqual(observed["last_retry_outcome"], "in_flight")
            self.assertTrue(observed["last_retry_at"])

    def test_retry_stale_old_run_exit_does_not_dlq_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self._store(root)
            dispatch = self._spawn_failed(store, "retry-stale")
            # A prior run's exit evidence is still on the row while the fresh
            # retry carries a DIFFERENT run token. The same-run predicate must
            # refuse to DLQ the new run on the older run's evidence.
            _set_observed_and_lineage(
                store,
                dispatch["dispatch_id"],
                {"worker_exit": {"returncode": 0, "source": "wrapper", "run_token": "run-token-OLD-000000000000"}},
                "2026-07-14T00:00:00+00:00",
            )

            adapter = SupervisedStubAdapter()  # run_token "run-token-abcdef0123456789"
            settled = store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: adapter, ttl_seconds=30)

            self.assertEqual(settled["status"], "in_flight")
            self.assertEqual(settled["observed_values"]["run_token"], adapter.run_token)
            # Stale evidence is preserved but was never authenticated as same-run.
            self.assertEqual(
                settled["observed_values"]["worker_exit"]["run_token"], "run-token-OLD-000000000000"
            )
            self.assertEqual(adapter.halt_calls, [])

    def test_retry_malformed_old_exit_token_does_not_dlq_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self._store(root)
            dispatch = self._spawn_failed(store, "retry-malformed")
            # The exit object exists but its run_token is malformed (non-string).
            _set_observed_and_lineage(
                store,
                dispatch["dispatch_id"],
                {"worker_exit": {"returncode": 0, "run_token": 12345}},
                None,
            )

            adapter = SupervisedStubAdapter()
            settled = store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: adapter, ttl_seconds=30)

            self.assertEqual(settled["status"], "in_flight")
            self.assertEqual(adapter.halt_calls, [])

    def test_retry_recipient_terminal_takes_priority_over_exit_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self._store(root)
            dispatch = self._spawn_failed(store, "retry-close-and-exit")
            self._reply(store, dispatch)
            store.close_message("alpha-worker", dispatch["message_id"], "Done.")
            # Fold in a same-run exit report alongside the recorded close.
            observed = json.loads(_ledger_row(store, dispatch["dispatch_id"])["observed_values_json"])
            observed["worker_exit"] = {
                "returncode": 1,
                "source": "wrapper",
                "run_token": "run-token-abcdef0123456789",
            }
            _set_observed_and_lineage(store, dispatch["dispatch_id"], observed, None)

            adapter = SupervisedStubAdapter()
            settled = store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: adapter, ttl_seconds=30)

            # Recipient-terminal wins over exit-evidence: closed, not dlq.
            self.assertEqual(settled["status"], "closed")
            self.assertIsNone(settled["failure_reason"])
            self.assertEqual(adapter.halt_calls, [])

    def test_retry_cap_race_halts_orphan_authenticated_and_annotates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self._store(root)
            _set_cap(store, "alpha-architect", 1)
            dispatch = self._spawn_failed(store, "retry-cap-race")

            class CapRaceAdapter(SupervisedStubAdapter):
                def dispatch(self, context: DispatchContext) -> DispatchStart:
                    # Fill the producer cap AFTER retry's pre-spawn check, so the
                    # cap-race is discovered only at the commit re-check.
                    _insert_inflight_sibling(store, "cap-race")
                    return super().dispatch(context)

            adapter = CapRaceAdapter()
            with self.assertRaises(ConcurrencyError) as raised:
                store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: adapter, ttl_seconds=30)

            message = str(raised.exception)
            self.assertIn("at cap 1/1", message)
            self.assertIn(dispatch["dispatch_id"], message)
            self.assertIn("alpha-architect", message)
            # The orphan is halted with the authenticated control identity, never
            # a PID-parsed signal, and all HALT I/O is outside the transaction.
            self.assertEqual(len(adapter.halt_calls), 1)
            halt_handle, halt_observed = adapter.halt_calls[0]
            self.assertEqual(halt_handle, "claude:alpha-worker:4242")
            self.assertEqual(halt_observed["control_socket"], adapter.control_socket)
            self.assertEqual(halt_observed["run_token"], adapter.run_token)
            # The short annotate transaction records the race and releases lineage
            # under the exact spawn_failed_message_landed status guard.
            row = _ledger_row(store, dispatch["dispatch_id"])
            self.assertEqual(row["status"], "spawn_failed_message_landed")
            observed = json.loads(row["observed_values_json"])
            self.assertTrue(observed["retry_race_halted_at"])
            self.assertEqual(observed["retry_race_halt_outcome"], "halted")
            self.assertIsNone(row["auth_lineage_claimed_at"])

    def test_retry_concurrent_terminal_halts_orphan_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self._store(root)
            dispatch = self._spawn_failed(store, "retry-vs-close")

            class RaceThenSupervisedAdapter(SupervisedStubAdapter):
                def dispatch(self, context: DispatchContext) -> DispatchStart:
                    # A concurrent terminal writer wins the row while the adapter
                    # is spawning (before this retry commit re-reads it).
                    with store._db.connection() as conn:
                        conn.execute(
                            "update dispatch_ledger set status = 'closed', closed_at = ? "
                            "where dispatch_id = ?",
                            ("2026-07-14T00:00:00+00:00", context.dispatch["dispatch_id"]),
                        )
                    return super().dispatch(context)

            adapter = RaceThenSupervisedAdapter()
            with self.assertRaises(ConcurrencyError) as raised:
                store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: adapter, ttl_seconds=30)

            message = str(raised.exception)
            self.assertIn("concurrent", message)
            self.assertIn("observed status=closed", message)
            # The orphan we spawned is halted with the authenticated control
            # identity, and no false in_flight is ever published.
            self.assertEqual(len(adapter.halt_calls), 1)
            _, halt_observed = adapter.halt_calls[0]
            self.assertEqual(halt_observed["control_socket"], adapter.control_socket)
            self.assertEqual(halt_observed["run_token"], adapter.run_token)
            self.assertEqual(_ledger_row(store, dispatch["dispatch_id"])["status"], "closed")
            self.assertEqual(_status_counts(store, "alpha-architect").get("in_flight", 0), 0)

    def test_retry_cap_race_halt_runs_outside_write_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self._store(root)
            _set_cap(store, "alpha-architect", 1)
            dispatch = self._spawn_failed(store, "retry-txn-boundary")

            class ProbingCapRaceAdapter(SupervisedStubAdapter):
                def __init__(self) -> None:
                    super().__init__()
                    self.halt_acquired_write_lock: bool | None = None

                def dispatch(self, context: DispatchContext) -> DispatchStart:
                    _insert_inflight_sibling(store, "txn-boundary")
                    return super().dispatch(context)

                def halt(self, spawn_handle: str, observed_values=None) -> None:
                    super().halt(spawn_handle, observed_values)
                    # If retry still held its BEGIN IMMEDIATE write lock at halt
                    # time, a fresh zero-timeout connection could not acquire the
                    # reserved lock and would raise. Success proves the halt runs
                    # strictly OUTSIDE the write transaction.
                    probe = sqlite3.connect(store._db.db_path, timeout=0)
                    try:
                        probe.execute("begin immediate")
                        probe.execute("select 1")
                        probe.commit()
                        self.halt_acquired_write_lock = True
                    except sqlite3.OperationalError:
                        self.halt_acquired_write_lock = False
                    finally:
                        probe.close()

            adapter = ProbingCapRaceAdapter()
            with self.assertRaises(ConcurrencyError):
                store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: adapter, ttl_seconds=30)

            # Halt held no write lock -> it ran outside the transaction.
            self.assertIs(adapter.halt_acquired_write_lock, True)
            # The short annotate transaction still committed the race record.
            row = _ledger_row(store, dispatch["dispatch_id"])
            self.assertEqual(row["status"], "spawn_failed_message_landed")
            self.assertTrue(json.loads(row["observed_values_json"])["retry_race_halted_at"])


if __name__ == "__main__":
    unittest.main()
