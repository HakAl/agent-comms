"""T6 + monitor-side T7 for dead-worker terminalization stage 1.

Every monitor pass reconciles supervised in_flight rows: adapter STATUS runs
outside any transaction, then an exact CAS applies one of recipient-terminal ->
closed, same-run exit -> early DLQ (bounded evidence, paged once), authenticated
running -> hold, supervisor unreachable -> bounded diagnostic + once-only warning
and hold before the hard TTL. Cap/lineage released on early DLQ allows normal
queued promotion in the same pass. First-committer: a row another writer already
settled is never dragged back to in_flight.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import agent_comms.dispatch_ledger as dispatch_ledger
from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.dispatch_ledger import (
    HARD_TTL_KILL_GRACE_SECONDS,
    RECIPIENT_TERMINAL_LEDGER_OPEN,
)
from agent_comms.store import Store

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"


def _iso_seconds_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


class HaltUnconfirmedAdapter:
    """Supervised adapter double whose authenticated halt cannot confirm."""

    def __init__(self, state: str = "supervisor_unreachable") -> None:
        self.state = state
        self.control_socket = "/nonexistent/agent-comms/run/s/control.sock"
        self.run_token = "run-token-liveness-0001"
        self.status_calls: list[str] = []
        self.halt_calls: list[tuple[str, dict | None]] = []

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        return DispatchStart(
            spawn_handle=f"stub:{context.recipient['id']}:1",
            observed_values={
                "adapter": "stub",
                "control_socket": self.control_socket,
                "run_token": self.run_token,
                "protocol_version": 1,
                "worker_log": "/tmp/worker.log",
            },
        )

    def status(self, spawn_handle: str, observed_values=None):
        self.status_calls.append(spawn_handle)
        return _Status(self.state, f"detail:{self.state}")

    def halt(self, spawn_handle: str, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        raise RuntimeError("authenticated halt did not confirm termination")

_Status = namedtuple("_Status", ["state", "detail"])


class LivenessStubAdapter:
    """Supervised adapter double with a configurable STATUS state."""

    def __init__(self, state: str = "running") -> None:
        self.state = state
        self.control_socket = "/nonexistent/agent-comms/run/s/control.sock"
        self.run_token = "run-token-liveness-0001"
        self.status_calls: list[str] = []
        self.halt_calls: list[tuple[str, dict | None]] = []

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        return DispatchStart(
            spawn_handle=f"stub:{context.recipient['id']}:1",
            observed_values={
                "adapter": "stub",
                "control_socket": self.control_socket,
                "run_token": self.run_token,
                "protocol_version": 1,
                "worker_log": "/tmp/worker.log",
            },
        )

    def status(self, spawn_handle: str, observed_values=None) -> _Status:
        self.status_calls.append(spawn_handle)
        return _Status(self.state, f"detail:{self.state}")

    def halt(self, spawn_handle: str, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))


def _seed(store: Store, root: Path) -> None:
    store.register_actor(HUMAN_ID, "human", "alice")
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
    store.register_agent_actor(
        "alpha-worker", "alpha", "worker", str(root / "alpha-worker"), [], runtime="stub", spawn={"command": "stub"},
        owner="alpha-architect",
    )


def _ledger(store: Store, dispatch_id: str):
    with store._db.connection() as conn:
        return conn.execute("select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)).fetchone()


def _merge_observed(store: Store, dispatch_id: str, extra: dict) -> None:
    with store._db.connection() as conn:
        row = conn.execute(
            "select observed_values_json from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
        ).fetchone()
        observed = json.loads(row["observed_values_json"] or "{}")
        observed.update(extra)
        conn.execute(
            "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
            (json.dumps(observed, sort_keys=True), dispatch_id),
        )


class MonitorLivenessCasTest(unittest.TestCase):
    def _stamp_v1(self, store: Store, dispatch: dict) -> None:
        with store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set policy_version = 'v1' where dispatch_id = ?",
                (dispatch["dispatch_id"],),
            )
            self.assertEqual(
                conn.execute(
                    "select policy_version from dispatch_ledger where dispatch_id = ?",
                    (dispatch["dispatch_id"],),
                ).fetchone()["policy_version"],
                "v1",
            )

    def _in_flight(self, store: Store, adapter: LivenessStubAdapter, key: str = "live") -> dict:
        store.dispatch_agent("alpha-architect", "alpha-worker", key, f"S {key}", f"B {key}", [])
        return store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=3600)[0]

    def test_authenticated_running_holds_no_terminal_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)

            actions = store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            self.assertEqual(_ledger(store, started["dispatch_id"])["status"], "in_flight")
            self.assertTrue(adapter.status_calls)  # STATUS was consulted
            self.assertNotIn(
                started["dispatch_id"],
                [a.get("dispatch_id") for a in actions if a.get("status") in ("closed", "dlq")],
            )

    def test_pre_ttl_exit_without_reply_becomes_early_dlq_paged_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            _merge_observed(
                store,
                started["dispatch_id"],
                {"worker_exit": {"returncode": 0, "source": "wrapper", "run_token": adapter.run_token}},
            )

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            row = _ledger(store, started["dispatch_id"])
            observed = json.loads(row["observed_values_json"])
            self.assertEqual(row["status"], "dlq")
            self.assertEqual(row["failure_reason"], "worker_exited_before_close")
            evidence = observed["early_dlq_evidence"]
            self.assertEqual(evidence["classification"], "worker_exited_before_close")
            self.assertEqual(evidence["reply_message_ids"], [])  # no reply
            self.assertEqual(evidence["worker_log"], "/tmp/worker.log")
            self.assertIsNone(row["auth_lineage_claimed_at"])
            # Paged exactly once to the producer.
            producer_inbox = store.list_inbox("alpha-architect", unread_only=False)
            self.assertEqual(len(producer_inbox), 1)
            self.assertIn("producer_page_message_id", observed)

            # A second pass does not re-page or resurrect in_flight.
            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)
            self.assertEqual(_ledger(store, started["dispatch_id"])["status"], "dlq")
            self.assertEqual(len(store.list_inbox("alpha-architect", unread_only=False)), 1)

    def test_pre_ttl_exit_with_blocked_reply_and_log_captures_bounded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            reply = store.send_message(
                "alpha-worker",
                ["alpha-architect"],
                "Re: dispatch",
                "BLOCKED: cannot proceed",
                [],
                parent_message_id=started["message_id"],
            )
            store.post_status("alpha-worker", "blocked on upstream", [], dispatch_id=started["dispatch_id"])
            _merge_observed(
                store,
                started["dispatch_id"],
                {"reaper_exit": {"returncode": 1, "run_token": adapter.run_token}},
            )

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            observed = json.loads(_ledger(store, started["dispatch_id"])["observed_values_json"])
            evidence = observed["early_dlq_evidence"]
            self.assertEqual(evidence["reply_message_ids"], [reply["id"]])
            self.assertEqual(evidence["latest_status_summary"], "blocked on upstream")
            self.assertEqual(evidence["worker_exit"]["returncode"], 1)

    def test_recipient_terminal_reconciles_closed_every_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            self._stamp_v1(store, started)
            # A recipient copy closed while the ledger stayed in_flight (mailbox
            # close raced the ledger write). The next pass settles it directly.
            with store._db.connection() as conn:
                conn.execute(
                    "update message_recipients set status = 'closed' where message_id = ? and to_agent = ?",
                    (started["message_id"], "alpha-worker"),
                )

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            row = _ledger(store, started["dispatch_id"])
            self.assertEqual(row["status"], "closed")
            self.assertTrue(row["closed_at"])
            self.assertEqual(store.list_inbox("alpha-architect", unread_only=False), [])  # no DLQ page

    def test_v2_recipient_terminal_without_closeout_releases_protocol_failure_dlq(self) -> None:
        # The v2 result contract coexists with the reconciler: a v2 row whose
        # recipient copy went terminal WITHOUT ``close_dispatch`` skipped the
        # checked closeout, so the liveness pass releases it to the
        # closeout-missing dlq -- never a result-less v2 ``closed`` and never
        # the legacy reconcile-to-closed.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)  # stays policy_version v2
            with store._db.connection() as conn:
                conn.execute(
                    "update message_recipients set status = 'closed' where message_id = ? and to_agent = ?",
                    (started["message_id"], "alpha-worker"),
                )

            actions = store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            row = _ledger(store, started["dispatch_id"])
            self.assertEqual(row["status"], "dlq")
            self.assertEqual(row["failure_reason"], "closeout_missing_protocol_failure")
            self.assertIsNone(row["result"])
            settled = [
                a
                for a in actions
                if a.get("dispatch_id") == started["dispatch_id"]
                and a.get("reconcile") == "recipient_terminal"
            ]
            self.assertEqual([a.get("status") for a in settled], ["dlq"])

    def test_recipient_terminal_reconcile_action_carries_projection_outcome(self) -> None:
        # (integration, real classifier) The reconcile-to-closed DECISION
        # consumes the single canonical projection rather than a hand-coded
        # transport set: the emitted action carries the classifier's normalized
        # ``outcome`` for the actual (in_flight, <terminal>) pair, and the settled
        # row then reads ``closed`` under the shared ``project_dispatch`` join.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            # Legacy row: only a v1 dispatch reconciles recipient-terminal to
            # ``closed``; a v2 row releases to the closeout-missing dlq.
            self._stamp_v1(store, started)
            with store._db.connection() as conn:
                conn.execute(
                    "update message_recipients set status = 'acknowledged' where message_id = ? and to_agent = ?",
                    (started["message_id"], "alpha-worker"),
                )

            actions = store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            closed = [
                a
                for a in actions
                if a.get("dispatch_id") == started["dispatch_id"] and a.get("status") == "closed"
            ]
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0].get("reconcile"), "recipient_terminal")
            # The action's normalized outcome IS the shared classifier's outcome
            # for the actual pair (never a plain "closed"); once settled the row
            # reads ``closed`` under the canonical joined read.
            self.assertEqual(closed[0].get("outcome"), RECIPIENT_TERMINAL_LEDGER_OPEN)
            self.assertEqual(store.project_dispatch(started["dispatch_id"])["outcome"], "closed")

    def test_monitoring_supplies_actual_pair_to_classifier_never_synthesizes_closed(self) -> None:
        # BYPASS-PROOF (Required outcome 1 + 3): monitoring must feed the shared
        # classifier the ACTUAL observed pair -- the row's own ``in_flight``
        # execution status joined with the recipient transport copy -- and must
        # NOT synthesize a ``closed`` (or any other) ledger status. A spy on the
        # classifier reference the production consumer uses records every pair;
        # the reconcile only settles because the real projection of the ACTUAL
        # pair authorized it.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            # Legacy row: the authorized settle under test is the v1
            # reconcile-to-closed (a v2 row releases to the closeout-missing dlq).
            self._stamp_v1(store, started)
            with store._db.connection() as conn:
                conn.execute(
                    "update message_recipients set status = 'closed' where message_id = ? and to_agent = ?",
                    (started["message_id"], "alpha-worker"),
                )

            calls: list[tuple] = []
            real = dispatch_ledger.project_dispatch_transport

            def spy(dispatch_status, transport_status):
                calls.append((dispatch_status, transport_status))
                return real(dispatch_status, transport_status)

            with mock.patch.object(dispatch_ledger, "project_dispatch_transport", spy):
                store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            # The exact raw pair supplied is the ACTUAL execution status, never a
            # synthesized "closed" ledger state.
            self.assertIn(("in_flight", "closed"), calls)
            self.assertFalse(
                any(dispatch_status == "closed" for dispatch_status, _ in calls),
                calls,
            )
            # The real projection of that actual pair authorized the settle.
            self.assertEqual(_ledger(store, started["dispatch_id"])["status"], "closed")

    def test_classifier_outcome_controls_reconcile_no_local_raw_mapping(self) -> None:
        # BYPASS-PROOF (Required outcome 3 + 4): the classifier's RETURNED outcome
        # -- not a local raw-transport mapping -- decides the reconcile. When the
        # (patched) classifier declines for a terminal transport, monitoring must
        # NOT settle; when it authorizes for a NON-terminal transport, monitoring
        # MUST settle. A consumer that shadowed the classifier with its own
        # ``recipient_status in {...}`` check would fail both halves.
        def _fixed_outcome(outcome):
            def classifier(dispatch_status, transport_status):
                return {
                    "dispatch_status": dispatch_status,
                    "transport_status": transport_status,
                    "outcome": outcome,
                }

            return classifier

        # (a) terminal transport, classifier declines -> held in_flight.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            # Legacy rows throughout: the settle whose authorization is under
            # test is the v1 reconcile-to-closed.
            self._stamp_v1(store, started)
            with store._db.connection() as conn:
                conn.execute(
                    "update message_recipients set status = 'closed' where message_id = ? and to_agent = ?",
                    (started["message_id"], "alpha-worker"),
                )
            with mock.patch.object(
                dispatch_ledger, "project_dispatch_transport", _fixed_outcome("in_flight")
            ):
                store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)
            self.assertEqual(_ledger(store, started["dispatch_id"])["status"], "in_flight")

        # (b) NON-terminal transport, classifier authorizes -> settled closed.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)  # transport stays "sent"
            self._stamp_v1(store, started)
            with mock.patch.object(
                dispatch_ledger,
                "project_dispatch_transport",
                _fixed_outcome(RECIPIENT_TERMINAL_LEDGER_OPEN),
            ):
                store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)
            self.assertEqual(_ledger(store, started["dispatch_id"])["status"], "closed")

    def test_cancelled_recipient_copy_is_not_reconciled_to_closed(self) -> None:
        # A withdrawn (cancelled) recipient copy is distinct from a closed one and
        # must NOT be folded into an ordinary close by monitoring. The shared
        # classifier maps (closed, cancelled) to a non-close, so a running row is
        # held in_flight, never reconciled to closed.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            with store._db.connection() as conn:
                conn.execute(
                    "update message_recipients set status = 'cancelled' where message_id = ? and to_agent = ?",
                    (started["message_id"], "alpha-worker"),
                )

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            self.assertEqual(_ledger(store, started["dispatch_id"])["status"], "in_flight")

    def test_recipient_terminal_wins_over_exit_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            self._stamp_v1(store, started)
            _merge_observed(
                store, started["dispatch_id"], {"worker_exit": {"returncode": 0, "run_token": adapter.run_token}}
            )
            with store._db.connection() as conn:
                conn.execute(
                    "update message_recipients set status = 'acknowledged' where message_id = ? and to_agent = ?",
                    (started["message_id"], "alpha-worker"),
                )

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            self.assertEqual(_ledger(store, started["dispatch_id"])["status"], "closed")

    def test_supervisor_unreachable_holds_pre_ttl_and_warns_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="supervisor_unreachable")
            started = self._in_flight(store, adapter)

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)
            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            row = _ledger(store, started["dispatch_id"])
            observed = json.loads(row["observed_values_json"])
            # Held, not released: still in_flight, lineage untouched.
            self.assertEqual(row["status"], "in_flight")
            diag = observed["supervisor_unreachable"]
            self.assertTrue(diag["first_detected_at"])
            self.assertTrue(diag["latest_detected_at"])
            # Warned exactly once across both passes.
            warnings = [
                m
                for m in store.list_inbox("alpha-architect", unread_only=False)
                if "supervisor unreachable" in m["subject"]
            ]
            self.assertEqual(len(warnings), 1)
            self.assertIn("supervisor_unreachable_page_message_id", observed)

    def _expire(self, store: Store, dispatch_id: str, seconds_ago: float) -> None:
        with store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set expected_close_by = ? where dispatch_id = ?",
                (_iso_seconds_ago(seconds_ago), dispatch_id),
            )

    def test_hard_ttl_confirmed_halt_releases_at_deadline(self) -> None:
        # A confirmed halt (the stub's halt returns without raising) releases at
        # the deadline; the kill-grace hold applies only to unconfirmed results.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            self._expire(store, started["dispatch_id"], seconds_ago=5)

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            self.assertEqual(_ledger(store, started["dispatch_id"])["status"], "dlq")

    def test_hard_ttl_unconfirmed_holds_just_before_grace_boundary(self) -> None:
        # An immediate unconfirmed / unreachable termination past the deadline
        # but before expected_close_by + kill_grace HOLDS: lineage not released.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = HaltUnconfirmedAdapter()
            started = self._in_flight(store, adapter)
            self._expire(store, started["dispatch_id"], seconds_ago=5)  # < kill_grace

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            row = _ledger(store, started["dispatch_id"])
            observed = json.loads(row["observed_values_json"])
            self.assertEqual(row["status"], "in_flight")  # HELD, not released
            self.assertIn("hard_ttl_unconfirmed", observed)
            self.assertTrue(adapter.halt_calls)  # halt was attempted each pass

    def test_hard_ttl_unconfirmed_releases_at_grace_boundary_with_phrase(self) -> None:
        # At/after expected_close_by + kill_grace the unconfirmed row is RELEASED
        # to dlq with the exact phrase; native-child death is never claimed.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = HaltUnconfirmedAdapter()
            started = self._in_flight(store, adapter)
            self._expire(store, started["dispatch_id"], seconds_ago=HARD_TTL_KILL_GRACE_SECONDS + 2)

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            row = _ledger(store, started["dispatch_id"])
            observed = json.loads(row["observed_values_json"])
            self.assertEqual(row["status"], "dlq")
            self.assertEqual(observed["termination_result"], "termination_not_confirmed")
            self.assertIn("ledger released; termination not confirmed", row["failure_reason"])

    def test_early_dlq_releases_cap_and_promotes_queued_same_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            with store._db.connection() as conn:
                conn.execute("update actors set dispatch_cap = 1 where id = ?", ("alpha-architect",))
            adapter = LivenessStubAdapter(state="running")

            store.dispatch_agent("alpha-architect", "alpha-worker", "cap-first", "S1", "B1", [])
            store.dispatch_agent("alpha-architect", "alpha-worker", "cap-second", "S2", "B2", [])
            first = store._dispatch_by_idempotency_key_fresh("alpha-architect", "cap-first")
            second = store._dispatch_by_idempotency_key_fresh("alpha-architect", "cap-second")
            with store._db.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set created_at = ? where dispatch_id = ?",
                    ("2026-01-01T00:00:01+00:00", first["dispatch_id"]),
                )
                conn.execute(
                    "update dispatch_ledger set created_at = ? where dispatch_id = ?",
                    ("2026-01-01T00:00:02+00:00", second["dispatch_id"]),
                )
            promoted = store.start_queued_dispatches(lambda _runtime: adapter, limit=10)
            self.assertEqual(promoted[0]["dispatch_id"], first["dispatch_id"])
            self.assertEqual(promoted[0]["status"], "in_flight")
            _merge_observed(
                store, first["dispatch_id"], {"worker_exit": {"returncode": 0, "run_token": adapter.run_token}}
            )

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            self.assertEqual(_ledger(store, first["dispatch_id"])["status"], "dlq")
            self.assertEqual(_ledger(store, second["dispatch_id"])["status"], "in_flight")

    def test_stale_old_run_exit_holds_not_early_dlq(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            # The current run token is adapter.run_token, but the exit object
            # carries an OLDER run's token. It must not authenticate as same-run.
            _merge_observed(
                store,
                started["dispatch_id"],
                {"worker_exit": {"returncode": 0, "run_token": "run-token-STALE-old-0000"}},
            )

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            row = _ledger(store, started["dispatch_id"])
            self.assertEqual(row["status"], "in_flight")  # held, not DLQ'd
            self.assertEqual(store.list_inbox("alpha-architect", unread_only=False), [])

    def test_missing_or_malformed_exit_token_holds_not_early_dlq(self) -> None:
        for label, exit_obj in (
            ("missing-token", {"returncode": 0, "source": "wrapper"}),
            ("malformed-token", {"returncode": 0, "run_token": 42}),
        ):
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    store = Store(root / "agent-comms.sqlite")
                    _seed(store, root)
                    adapter = LivenessStubAdapter(state="running")
                    started = self._in_flight(store, adapter, key=label)
                    _merge_observed(store, started["dispatch_id"], {"worker_exit": exit_obj})

                    store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

                    self.assertEqual(_ledger(store, started["dispatch_id"])["status"], "in_flight")

    def test_hard_ttl_supervised_termination_not_confirmed_releases_with_phrase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)

            adapter = HaltUnconfirmedAdapter(state="supervisor_unreachable")
            started = self._in_flight(store, adapter)
            # Past the kill-grace boundary so the unconfirmed row is released.
            with store._db.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set expected_close_by = ? where dispatch_id = ?",
                    (_iso_seconds_ago(HARD_TTL_KILL_GRACE_SECONDS + 5), started["dispatch_id"]),
                )

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            row = _ledger(store, started["dispatch_id"])
            observed = json.loads(row["observed_values_json"])
            # Released to DLQ, but never under a false claim of native-child death.
            self.assertEqual(row["status"], "dlq")
            self.assertEqual(observed["termination_result"], "termination_not_confirmed")
            self.assertIn("ledger released; termination not confirmed", row["failure_reason"])
            self.assertTrue(observed.get("termination_detail"))
            # The producer page carries the exact phrase.
            pages = store.list_inbox("alpha-architect", unread_only=False)
            bodies = [store.read_message("alpha-architect", page["id"])["body"] for page in pages]
            self.assertTrue(any("ledger released; termination not confirmed" in body for body in bodies))
            # The halt was the authenticated socket path, carrying observed_values.
            self.assertTrue(adapter.halt_calls)
            self.assertEqual(adapter.halt_calls[-1][1].get("run_token"), adapter.run_token)

    def test_hard_ttl_recipient_close_wins_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            self._stamp_v1(store, started)
            past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
            with store._db.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set expected_close_by = ? where dispatch_id = ?",
                    (past, started["dispatch_id"]),
                )
                # A recipient close raced the deadline while the ledger stayed
                # in_flight; the hard-TTL CAS re-read must let closed win.
                conn.execute(
                    "update message_recipients set status = 'closed' where message_id = ? and to_agent = ?",
                    (started["message_id"], "alpha-worker"),
                )

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            row = _ledger(store, started["dispatch_id"])
            self.assertEqual(row["status"], "closed")
            self.assertTrue(row["closed_at"])
            self.assertEqual(store.list_inbox("alpha-architect", unread_only=False), [])  # no DLQ page

    def test_monitor_vs_monitor_second_pass_does_not_overwrite_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            _merge_observed(
                store, started["dispatch_id"], {"worker_exit": {"returncode": 0, "run_token": adapter.run_token}}
            )

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)
            dlq_at = _ledger(store, started["dispatch_id"])["dlq_at"]
            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            row = _ledger(store, started["dispatch_id"])
            self.assertEqual(row["status"], "dlq")
            self.assertEqual(row["dlq_at"], dlq_at)  # not re-written back through in_flight


class BoundedPartialEvidenceTest(unittest.TestCase):
    """T8: the shared bounded partial-work evidence for an early DLQ."""

    def _in_flight(self, store: Store, adapter: LivenessStubAdapter, key: str = "bounded") -> dict:
        store.dispatch_agent("alpha-architect", "alpha-worker", key, f"S {key}", f"B {key}", [])
        return store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=3600)[0]

    def test_more_than_ten_replies_caps_ids_records_exact_total_and_no_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            # Thirteen recipient replies to the dispatch thread.
            for i in range(13):
                store.send_message(
                    "alpha-worker",
                    ["alpha-architect"],
                    f"Re {i}",
                    f"secret-reply-body-{i}",
                    [],
                    parent_message_id=started["message_id"],
                )
            _merge_observed(
                store,
                started["dispatch_id"],
                {"worker_exit": {"returncode": 0, "run_token": adapter.run_token}},
            )

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            observed = json.loads(_ledger(store, started["dispatch_id"])["observed_values_json"])
            evidence = observed["early_dlq_evidence"]
            self.assertEqual(evidence["classification"], "worker_exited_before_close")
            # At most ten stored ids, but the EXACT total recorded separately.
            self.assertEqual(len(evidence["reply_message_ids"]), 10)
            self.assertEqual(evidence["reply_total_count"], 13)
            # Deterministic order by (created_at, id): the stored ids are exactly the
            # first ten under that same ordering.
            with store._db.connection() as conn:
                expected = [
                    row["id"]
                    for row in conn.execute(
                        """
                        select m.id
                        from messages m
                        join message_threads mt on mt.message_id = m.id
                        where mt.parent_message_id = ? and m.from_agent = ?
                        order by m.created_at, m.id
                        limit 10
                        """,
                        (started["message_id"], "alpha-worker"),
                    ).fetchall()
                ]
            self.assertEqual(evidence["reply_message_ids"], expected)
            # No reply BODY is ever persisted in the evidence.
            blob = json.dumps(evidence)
            for i in range(13):
                self.assertNotIn(f"secret-reply-body-{i}", blob)

            # The producer DLQ page reports the stored ids and the omitted count
            # using the EXACT total, never assuming zero omitted. (The producer's
            # inbox also holds the 13 replies; the page is the single [DLQ] message.)
            dlq_pages = [
                m
                for m in store.list_inbox("alpha-architect", unread_only=False)
                if m["subject"].startswith("[DLQ]")
            ]
            self.assertEqual(len(dlq_pages), 1)
            body = store.read_message("alpha-architect", dlq_pages[0]["id"])["body"]
            self.assertIn("reply_count=13", body)
            self.assertIn("(+3 more)", body)
            for i in range(13):
                self.assertNotIn(f"secret-reply-body-{i}", body)

    def test_long_status_summary_is_bounded_to_display_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            adapter = LivenessStubAdapter(state="running")
            started = self._in_flight(store, adapter)
            store.post_status(
                "alpha-worker", "X" * 500, [], dispatch_id=started["dispatch_id"]
            )
            _merge_observed(
                store,
                started["dispatch_id"],
                {"worker_exit": {"returncode": 0, "run_token": adapter.run_token}},
            )

            store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            observed = json.loads(_ledger(store, started["dispatch_id"])["observed_values_json"])
            evidence = observed["early_dlq_evidence"]
            # The copied summary is bounded to the 200-char display ceiling; the
            # authoritative status id is preserved.
            self.assertEqual(len(evidence["latest_status_summary"]), 200)
            self.assertIsNotNone(evidence["latest_status_id"])

    def test_page_render_uses_exact_total_and_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed(store, root)
            # New evidence carries the exact total separate from the capped ids.
            new_evidence = {
                "failure_reason": "worker_exited_before_close",
                "observed_values": {
                    "early_dlq_evidence": {
                        "classification": "worker_exited_before_close",
                        "reply_message_ids": [f"m{i}" for i in range(10)],
                        "reply_total_count": 25,
                        "worker_log": "/tmp/w.log",
                    }
                },
            }
            lines_new = store._dispatch._early_dlq_evidence_lines(new_evidence)
            self.assertIn("reply_count=25", lines_new)
            self.assertIn("(+15 more)", lines_new)
            # LEGACY evidence written before the total-count field falls back to the
            # stored-id count truthfully (older captures stored every id).
            legacy_evidence = {
                "failure_reason": "worker_exited_before_close",
                "observed_values": {
                    "early_dlq_evidence": {
                        "classification": "worker_exited_before_close",
                        "reply_message_ids": [f"m{i}" for i in range(12)],
                        "worker_log": "/tmp/w.log",
                    }
                },
            }
            lines_legacy = store._dispatch._early_dlq_evidence_lines(legacy_evidence)
            self.assertIn("reply_count=12", lines_legacy)
            self.assertIn("(+2 more)", lines_legacy)


if __name__ == "__main__":
    unittest.main()
