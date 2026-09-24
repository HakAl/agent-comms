"""T5 + spawn-commit-side T7 for dead-worker terminalization stage 1.

These pin the exact spawn-commit CAS: a successful spawn commit MERGES the
supervisor identity onto observed evidence a fast worker already wrote, and it
re-reads the still-queued row and the recipient copy under BEGIN IMMEDIATE to
settle a paused-spawn race deterministically:

  - recipient already acknowledged/closed -> ledger directly `closed`;
  - a same-run worker exit report without a close -> ledger directly `dlq`
    with `worker_exited_before_close`;
  - otherwise the exact `queued -> in_flight` CAS.

First-committer coverage: recipient-terminal wins over exit evidence, and a
spawn commit that discovers the ledger already left `queued` halts its orphan
with an authenticated (observed-values-bearing) HALT and never resurrects a
false in_flight.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.dispatch_ledger import ConcurrencyError
from agent_comms.store import Store


def seed_dispatch_actors(store: Store, root: Path) -> None:
    store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
    store.register_agent_actor("alpha-worker", "alpha", "worker", str(root / "alpha-worker"), [], owner="alpha-architect")


class SupervisedStubAdapter:
    """Adapter double that returns a supervisor control identity in observed.

    ``halt`` records the observed_values it was handed so tests can prove the
    cap/concurrent-race halt path uses the authenticated socket identity rather
    than a PID-parsed signal.
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


class PausedSpawnCasTest(unittest.TestCase):
    def _queued_dispatch(self, store: Store, key: str) -> dict:
        # No adapter_for_runtime -> the row is left queued, modelling a spawn
        # commit that has not landed in_flight yet.
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

    def test_paused_spawn_fast_close_settles_ledger_closed_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = self._queued_dispatch(store, "paused-close")

            # A fast worker replies then closes while the ledger is still queued.
            # close_message cannot update the queued ledger, so it records the
            # mismatch and leaves the row queued.
            self._reply(store, dispatch)
            closed = store.close_message("alpha-worker", dispatch["message_id"], "Done.")
            self.assertEqual(closed["status"], "closed")
            queued_row = _ledger_row(store, dispatch["dispatch_id"])
            self.assertEqual(queued_row["status"], "queued")
            self.assertEqual(
                json.loads(queued_row["observed_values_json"])["close_ledger_status_mismatch"],
                "queued",
            )
            # A live lineage claim is present until the spawn commit clears it.
            _set_observed_and_lineage(
                store,
                dispatch["dispatch_id"],
                json.loads(queued_row["observed_values_json"]),
                "2026-07-14T00:00:00+00:00",
            )

            adapter = SupervisedStubAdapter()
            started = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=30)

            self.assertEqual(len(started), 1)
            settled = started[0]
            self.assertEqual(settled["status"], "closed")
            self.assertTrue(settled["spawn_handle"])
            observed = settled["observed_values"]
            # The supervisor identity is MERGED onto the preserved close evidence.
            self.assertEqual(observed["close_ledger_status_mismatch"], "queued")
            self.assertEqual(observed["control_socket"], adapter.control_socket)
            self.assertEqual(observed["run_token"], adapter.run_token)
            self.assertEqual(observed["spawn_commit_recipient_terminal"], "closed")
            row = _ledger_row(store, dispatch["dispatch_id"])
            self.assertTrue(row["closed_at"])
            self.assertEqual(row["auth_lineage_claimed_at"], "2026-07-14T00:00:00+00:00")
            # Never published a false in_flight, and no orphan halt was needed.
            self.assertEqual(_status_counts(store, "alpha-architect").get("in_flight", 0), 0)
            self.assertEqual(adapter.halt_calls, [])

            # No later payload overwrites the settled terminal row: a second
            # start pass finds nothing queued and leaves the closed evidence.
            self.assertEqual(store.start_queued_dispatches(lambda _runtime: adapter), [])
            self.assertEqual(_ledger_row(store, dispatch["dispatch_id"])["status"], "closed")

    def test_paused_spawn_fast_ack_settles_ledger_closed_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = self._queued_dispatch(store, "paused-ack")
            self._reply(store, dispatch)
            acked = store.ack_message("alpha-worker", dispatch["message_id"], "Got it.")
            self.assertEqual(acked["status"], "acknowledged")

            adapter = SupervisedStubAdapter()
            started = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=30)

            settled = started[0]
            self.assertEqual(settled["status"], "closed")
            self.assertEqual(
                settled["observed_values"]["spawn_commit_recipient_terminal"], "acknowledged"
            )
            self.assertEqual(settled["observed_values"]["run_token"], adapter.run_token)
            self.assertEqual(_status_counts(store, "alpha-architect").get("in_flight", 0), 0)

    def test_paused_spawn_fast_exit_without_close_becomes_early_dlq(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = self._queued_dispatch(store, "paused-exit")

            # The supervisor recorded a same-run child exit while the row was
            # still queued and the trigger was never closed.
            _set_observed_and_lineage(
                store,
                dispatch["dispatch_id"],
                {
                    "worker_exit": {
                        "returncode": 0,
                        "source": "wrapper",
                        "run_token": "run-token-abcdef0123456789",
                    }
                },
                "2026-07-14T00:00:00+00:00",
            )

            adapter = SupervisedStubAdapter()
            started = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=30)

            settled = started[0]
            self.assertEqual(settled["status"], "dlq")
            self.assertEqual(settled["failure_reason"], "worker_exited_before_close")
            observed = settled["observed_values"]
            self.assertIn("worker_exit", observed)  # preserved
            self.assertEqual(observed["control_socket"], adapter.control_socket)  # merged
            row = _ledger_row(store, dispatch["dispatch_id"])
            self.assertTrue(row["dlq_at"])
            self.assertEqual(row["auth_lineage_claimed_at"], "2026-07-14T00:00:00+00:00")
            # Early DLQ, not spawn failure and not a false in_flight.
            counts = _status_counts(store, "alpha-architect")
            self.assertEqual(counts.get("in_flight", 0), 0)
            self.assertEqual(counts.get("spawn_failed_message_landed", 0), 0)
            self.assertEqual(adapter.halt_calls, [])

    def test_spawn_commit_inflight_merges_preexisting_observed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = self._queued_dispatch(store, "merge-inflight")
            # An unrelated observed key written before the commit must survive.
            _set_observed_and_lineage(
                store, dispatch["dispatch_id"], {"preexisting_key": "survives"}, None
            )

            adapter = SupervisedStubAdapter()
            started = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=30)

            settled = started[0]
            self.assertEqual(settled["status"], "in_flight")
            observed = settled["observed_values"]
            self.assertEqual(observed["preexisting_key"], "survives")  # not overwritten
            self.assertEqual(observed["run_token"], adapter.run_token)  # merged in

    def test_recipient_terminal_takes_priority_over_exit_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = self._queued_dispatch(store, "close-and-exit")
            self._reply(store, dispatch)
            store.close_message("alpha-worker", dispatch["message_id"], "Done.")
            # Fold in a same-run exit report alongside the recorded close mismatch.
            observed = json.loads(_ledger_row(store, dispatch["dispatch_id"])["observed_values_json"])
            observed["worker_exit"] = {"returncode": 1, "source": "wrapper"}
            _set_observed_and_lineage(store, dispatch["dispatch_id"], observed, None)

            adapter = SupervisedStubAdapter()
            settled = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=30)[0]

            # Recipient-terminal (a) wins over exit-evidence (b): closed, not dlq.
            self.assertEqual(settled["status"], "closed")
            self.assertIsNone(settled["failure_reason"])

    def test_stale_old_run_exit_does_not_dlq_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = self._queued_dispatch(store, "stale-exit")
            # A prior run's exit evidence is still on the queued row while the
            # fresh spawn carries a DIFFERENT run token. The same-run predicate
            # must refuse to DLQ the new run on the older run's evidence.
            _set_observed_and_lineage(
                store,
                dispatch["dispatch_id"],
                {"worker_exit": {"returncode": 0, "source": "wrapper", "run_token": "run-token-OLD-000000000000"}},
                "2026-07-14T00:00:00+00:00",
            )

            adapter = SupervisedStubAdapter()  # run_token "run-token-abcdef0123456789"
            settled = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=30)[0]

            self.assertEqual(settled["status"], "in_flight")
            self.assertEqual(settled["observed_values"]["run_token"], adapter.run_token)
            # Stale evidence is preserved but was never authenticated as same-run.
            self.assertEqual(
                settled["observed_values"]["worker_exit"]["run_token"], "run-token-OLD-000000000000"
            )
            self.assertEqual(adapter.halt_calls, [])

    def test_missing_current_run_token_does_not_early_dlq(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = self._queued_dispatch(store, "no-current-token")
            _set_observed_and_lineage(
                store,
                dispatch["dispatch_id"],
                {"worker_exit": {"returncode": 0, "run_token": "run-token-abcdef0123456789"}},
                None,
            )

            class NoRunTokenAdapter(SupervisedStubAdapter):
                def dispatch(self, context: DispatchContext) -> DispatchStart:
                    # A spawn that reports no run token cannot authenticate any
                    # exit evidence, so the fast-exit branch must not fire.
                    return DispatchStart(
                        spawn_handle=f"claude:{context.recipient['id']}:4242",
                        observed_values={"adapter": "claude", "worker_log": "/tmp/worker.log"},
                    )

            adapter = NoRunTokenAdapter()
            settled = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=30)[0]

            self.assertEqual(settled["status"], "in_flight")

    def test_malformed_exit_token_does_not_early_dlq(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = self._queued_dispatch(store, "malformed-token")
            # The exit object exists but its run_token is malformed (non-string).
            _set_observed_and_lineage(
                store,
                dispatch["dispatch_id"],
                {"worker_exit": {"returncode": 0, "run_token": 12345}},
                None,
            )

            adapter = SupervisedStubAdapter()
            settled = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=30)[0]

            self.assertEqual(settled["status"], "in_flight")

    def test_spawn_commit_over_already_terminal_ledger_halts_orphan_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = self._queued_dispatch(store, "commit-vs-close")

            class RaceThenSupervisedAdapter(SupervisedStubAdapter):
                def dispatch(self, context: DispatchContext) -> DispatchStart:
                    # A concurrent terminal writer wins the row while the adapter
                    # is spawning (before this spawn commit re-reads it).
                    with store._db.connection() as conn:
                        conn.execute(
                            "update dispatch_ledger set status = 'closed', closed_at = ? "
                            "where dispatch_id = ?",
                            ("2026-07-14T00:00:00+00:00", context.dispatch["dispatch_id"]),
                        )
                    return super().dispatch(context)

            adapter = RaceThenSupervisedAdapter()
            started = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=30)

            self.assertEqual(started[0]["status"], "start_race_concurrent")
            self.assertIn("observed status=closed", started[0]["detail"])
            # The orphan we spawned is halted with the authenticated control
            # identity, never a PID-parsed signal, and never a false in_flight.
            self.assertEqual(len(adapter.halt_calls), 1)
            halt_handle, halt_observed = adapter.halt_calls[0]
            self.assertEqual(halt_handle, "claude:alpha-worker:4242")
            self.assertEqual(halt_observed["control_socket"], adapter.control_socket)
            self.assertEqual(halt_observed["run_token"], adapter.run_token)
            self.assertEqual(_ledger_row(store, dispatch["dispatch_id"])["status"], "closed")
            self.assertEqual(_status_counts(store, "alpha-architect").get("in_flight", 0), 0)


if __name__ == "__main__":
    unittest.main()
