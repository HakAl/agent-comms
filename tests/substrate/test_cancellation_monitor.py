"""Stage-2 T5: monitor retry/escalation, TTL outcomes, and terminal residue.

The monitor processes pending cancellations BEFORE ordinary liveness/TTL: a
bounded authenticated HALT retry either confirms ``cancelled`` or holds the row
nonterminal, and a request past its durable 60s deadline escalates once (producer
blocker + operator infra notice) without releasing cap/lineage. A confirmed retry
finishes ``cancelled``; a hard-TTL confirmed halt finishes ``cancelled``; a
request still unconfirmed through TTL+kill-grace falls to the existing truthful
DLQ ``termination_not_confirmed`` and is NEVER relabelled cancelled. A confirmed
cancellation releases cap/lineage and the ordinary queued drain promotes a
successor in the same pass. The DLQ residue is preserved until positive same-run
evidence permits janitor cleanup.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.dispatch_ledger import (
    CANCELLATION_ESCALATION_SECONDS,
    DLQ_RESIDUE_REPROBE_BATCH,
    HARD_TTL_KILL_GRACE_SECONDS,
)
from agent_comms.store import Store
from agent_comms.supervisor import confirmed_termination_evidence

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"
OTHER_HUMAN_ID = "01J00000000000000000000002"
_Status = namedtuple("_Status", ["state", "detail"])


class ConfirmingAdapter:
    def __init__(self, state: str = "running") -> None:
        self.state = state
        self.control_socket = "/nonexistent/agent-comms/run/s/control.sock"
        self.run_token = "run-token-monitor-0001"
        self.halt_calls: list = []

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

    def status(self, spawn_handle, observed_values=None) -> _Status:
        return _Status(self.state, f"detail:{self.state}")

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))


class UnconfirmedAdapter(ConfirmingAdapter):
    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        raise RuntimeError("authenticated halt did not confirm termination")


def _seed(store: Store, root: Path) -> None:
    store.register_actor(HUMAN_ID, "human", "alice")
    store.register_agent_actor("arch", "alpha", "architect", str(root / "arch"), [])
    store.register_agent_actor(
        "wrk", "alpha", "worker", str(root / "wrk"), [], runtime="stub", spawn={"command": "stub"},
        owner="arch",
    )


def _ledger(store: Store, dispatch_id: str):
    with store._db.connection() as conn:
        return conn.execute(
            "select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
        ).fetchone()


def _observed(store: Store, dispatch_id: str) -> dict:
    return json.loads(_ledger(store, dispatch_id)["observed_values_json"] or "{}")


def _backdate_request(store: Store, dispatch_id: str, seconds_ago: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")
    with store._db.connection() as conn:
        observed = json.loads(
            conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()["observed_values_json"]
        )
        observed["cancellation"]["requested_at"] = ts
        conn.execute(
            "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
            (json.dumps(observed, sort_keys=True), dispatch_id),
        )


def _expire(store: Store, dispatch_id: str, seconds_ago: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")
    with store._db.connection() as conn:
        conn.execute(
            "update dispatch_ledger set expected_close_by = ? where dispatch_id = ?", (ts, dispatch_id)
        )


class MonitorBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = Store(self.tmp / "agent-comms.sqlite")
        _seed(self.store, self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _in_flight(self, adapter, key: str = "live") -> dict:
        self.store.dispatch_agent("arch", "wrk", key, f"S {key}", f"B {key}", [])
        return self.store.start_queued_dispatches(lambda _r: adapter, ttl_seconds=3600)[0]

    def _request_pending(self, dispatch_id: str) -> None:
        # Record a pending cancellation WITHOUT driving (no adapter) so the monitor
        # owns the first termination attempt.
        self.store.request_cancellation(
            dispatch_id, requesting_actor_id="arch", reason="withdraw", authority="producer"
        )


class ProcessBeforeLivenessTest(MonitorBase):
    def test_monitor_confirms_pending_cancellation(self) -> None:
        adapter = ConfirmingAdapter()
        started = self._in_flight(adapter)
        self._request_pending(started["dispatch_id"])

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")
        obs = _observed(self.store, started["dispatch_id"])["cancellation"]
        self.assertEqual(obs["termination_result"], "supervised_halt_confirmed")

    def test_monitor_holds_unconfirmed_pending_cancellation(self) -> None:
        adapter = UnconfirmedAdapter()
        started = self._in_flight(adapter)
        self._request_pending(started["dispatch_id"])

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "in_flight")  # held, nonterminal
        self.assertTrue(adapter.halt_calls)


class EscalationTest(MonitorBase):
    def test_no_escalation_before_deadline(self) -> None:
        adapter = UnconfirmedAdapter()
        started = self._in_flight(adapter)
        self._request_pending(started["dispatch_id"])
        # Just requested: well within the 60s window.
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        obs = _observed(self.store, started["dispatch_id"])
        self.assertNotIn("cancellation_escalation_producer_page_message_id", obs)

    def test_escalation_after_deadline_pages_once_no_release(self) -> None:
        adapter = UnconfirmedAdapter()
        started = self._in_flight(adapter)
        self._request_pending(started["dispatch_id"])
        claimed_before = _ledger(self.store, started["dispatch_id"])["auth_lineage_claimed_at"]
        _backdate_request(self.store, started["dispatch_id"], CANCELLATION_ESCALATION_SECONDS + 1)

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        row = _ledger(self.store, started["dispatch_id"])
        obs = json.loads(row["observed_values_json"])
        # Held, not released: still in_flight, lineage/cap untouched.
        self.assertEqual(row["status"], "in_flight")
        self.assertEqual(row["auth_lineage_claimed_at"], claimed_before)
        self.assertIn("escalated_at", obs["cancellation"])
        # Producer blocker page: exactly one, carries the admin preview command.
        producer_pages = [
            m for m in self.store.list_inbox("arch", unread_only=False)
            if "cancellation escalated" in m["subject"]
        ]
        self.assertEqual(len(producer_pages), 1)
        body = self.store.read_message("arch", producer_pages[0]["id"])["body"]
        self.assertIn("admin settle-dispatch", body)
        self.assertIn("--dry-run", body)
        # Operator infra notice: exactly one to the human operator.
        operator_pages = [
            m for m in self.store.list_inbox(HUMAN_ID, unread_only=False)
            if "cancellation infra" in m["subject"]
        ]
        self.assertEqual(len(operator_pages), 1)


class HardTtlInterplayTest(MonitorBase):
    def test_hard_ttl_confirmed_cancel_finishes_cancelled(self) -> None:
        adapter = ConfirmingAdapter()
        started = self._in_flight(adapter)
        self._request_pending(started["dispatch_id"])
        _expire(self.store, started["dispatch_id"], seconds_ago=5)  # past TTL, within grace

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        # The pre-TTL cancellation pass confirmed the halt: cancelled, not dlq.
        self.assertEqual(_ledger(self.store, started["dispatch_id"])["status"], "cancelled")

    def test_ttl_grace_unconfirmed_falls_to_dlq_never_cancelled(self) -> None:
        adapter = UnconfirmedAdapter()
        started = self._in_flight(adapter)
        self._request_pending(started["dispatch_id"])
        _expire(self.store, started["dispatch_id"], seconds_ago=HARD_TTL_KILL_GRACE_SECONDS + 2)

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        row = _ledger(self.store, started["dispatch_id"])
        obs = json.loads(row["observed_values_json"])
        self.assertEqual(row["status"], "dlq")  # never cancelled
        self.assertEqual(obs["termination_result"], "termination_not_confirmed")
        self.assertIn("ledger released; termination not confirmed", row["failure_reason"])
        # The cancellation request is preserved as unconfirmed residue.
        self.assertEqual(obs["cancellation"]["state"], "requested")


class PromotionTest(MonitorBase):
    def test_confirmed_cancel_releases_cap_and_promotes_same_pass(self) -> None:
        with self.store._db.connection() as conn:
            conn.execute("update actors set dispatch_cap = 1 where id = 'arch'")
        adapter = ConfirmingAdapter()
        first = self._in_flight(adapter, key="a")  # holds the single cap
        self.store.dispatch_agent("arch", "wrk", "b", "S b", "B b", [])
        second = self.store._dispatch_by_idempotency_key_fresh("arch", "b")
        self._request_pending(first["dispatch_id"])

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        self.assertEqual(_ledger(self.store, first["dispatch_id"])["status"], "cancelled")
        self.assertEqual(_ledger(self.store, second["dispatch_id"])["status"], "in_flight")


class ResidueBatchTest(MonitorBase):
    def _seed_residue(self, key: str, token: str) -> str:
        d = self.store.dispatch_agent("arch", "wrk", key, f"S {key}", f"B {key}", [])
        observed = {
            "control_socket": "/nonexistent/agent-comms/run/s/control.sock",
            "run_token": token,
            "termination_result": "termination_not_confirmed",
        }
        with self.store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status = 'dlq', dlq_at = ?, spawn_handle = ?, "
                "observed_values_json = ? where dispatch_id = ?",
                (
                    "2026-07-15T00:00:00+00:00",
                    f"stub:{d['dispatch_id']}:1",
                    json.dumps(observed, sort_keys=True),
                    d["dispatch_id"],
                ),
            )
        return d["dispatch_id"]

    def _has_reprobe(self, dispatch_id: str) -> bool:
        return "dlq_residue_reprobe" in _observed(self.store, dispatch_id)

    def test_first_pass_limit_five_then_fair_progress_across_restart(self) -> None:
        # Six eligible residue rows; an unreachable probe leaves them all residue.
        # Pass 1 probes at most DLQ_RESIDUE_REPROBE_BATCH (5) rows (SQL LIMIT). A
        # FRESH Store/monitor process on the same DB re-reads the durable
        # last-attempt order and prioritizes the never-probed row, so no row starves
        # and all six are eventually probed.
        ids = [self._seed_residue(f"res-{i}", f"run-token-res-{i}") for i in range(6)]
        adapter = UnconfirmedAdapter()  # never confirms; rows stay residue

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        probed_after_1 = [i for i in ids if self._has_reprobe(i)]
        unprobed_after_1 = [i for i in ids if not self._has_reprobe(i)]
        self.assertEqual(len(probed_after_1), DLQ_RESIDUE_REPROBE_BATCH)  # exactly 5
        self.assertEqual(len(unprobed_after_1), 1)

        # Restart: a brand-new Store instance on the same sqlite file continues.
        restarted = Store(self.tmp / "agent-comms.sqlite")
        restarted.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        # The never-probed row was prioritized by the durable order (NULL last-attempt
        # first) and all six have now been probed at least once.
        self.assertTrue(self._has_reprobe(unprobed_after_1[0]))
        for dispatch_id in ids:
            self.assertTrue(self._has_reprobe(dispatch_id))
            self.assertEqual(_ledger(self.store, dispatch_id)["status"], "dlq")

    def test_confirmed_batch_upgrades_five_per_pass_ledger_stays_dlq(self) -> None:
        # Six eligible residue rows; a confirming probe upgrades ONLY the termination
        # observation. Pass 1 upgrades at most 5 (SQL LIMIT), pass 2 the sixth; every
        # row's ledger status REMAINS dlq after confirmation (a dlq is never
        # resurrected).
        ids = [self._seed_residue(f"cres-{i}", f"run-token-cres-{i}") for i in range(6)]
        adapter = ConfirmingAdapter()  # halt confirms

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        upgraded_1 = [
            i for i in ids
            if _observed(self.store, i).get("termination_result") == "supervised_halt_confirmed"
        ]
        self.assertEqual(len(upgraded_1), DLQ_RESIDUE_REPROBE_BATCH)  # 5 upgraded first pass
        for dispatch_id in ids:
            self.assertEqual(_ledger(self.store, dispatch_id)["status"], "dlq")

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        for dispatch_id in ids:
            obs = _observed(self.store, dispatch_id)
            self.assertEqual(_ledger(self.store, dispatch_id)["status"], "dlq")  # stays dlq
            self.assertEqual(obs["termination_result"], "supervised_halt_confirmed")


class OperatorValidationTest(MonitorBase):
    def _pending_past_deadline(self, adapter, key: str = "op"):
        started = self._in_flight(adapter, key)
        self._request_pending(started["dispatch_id"])
        _backdate_request(self.store, started["dispatch_id"], CANCELLATION_ESCALATION_SECONDS + 1)
        return started

    def _assert_no_partial_claim_and_skip(self, started, actions) -> None:
        obs = _observed(self.store, started["dispatch_id"])
        # No page claim was ever left behind (validation precedes any claim).
        self.assertNotIn("cancellation_escalation_producer_page_claimed_at", obs)
        self.assertNotIn("cancellation_escalation_producer_page_message_id", obs)
        self.assertNotIn("cancellation_escalation_operator_page_claimed_at", obs)
        # ``escalated_at`` exists on every request object but stays unset (None)
        # unless an escalation actually fired.
        self.assertIsNone(obs.get("cancellation", {}).get("escalated_at"))
        self.assertTrue(
            any(
                a.get("status") == "cancellation_escalation_skipped_no_human_actor"
                for a in actions
            ),
            actions,
        )

    def test_absent_operator_id_reports_loudly_no_partial_claim(self) -> None:
        adapter = UnconfirmedAdapter()
        started = self._pending_past_deadline(adapter)
        actions = self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=None)
        self._assert_no_partial_claim_and_skip(started, actions)

    def test_unknown_operator_id_reports_loudly_no_partial_claim(self) -> None:
        adapter = UnconfirmedAdapter()
        started = self._pending_past_deadline(adapter)
        actions = self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id="ghost")
        self._assert_no_partial_claim_and_skip(started, actions)

    def test_agent_kind_operator_id_rejected_no_partial_claim(self) -> None:
        adapter = UnconfirmedAdapter()
        started = self._pending_past_deadline(adapter)
        # 'arch' is a registered actor but kind == agent, not human.
        actions = self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id="arch")
        self._assert_no_partial_claim_and_skip(started, actions)

    def test_require_human_helper_contract(self) -> None:
        # Narrow ActorRegistry.require_human: a registered human passes; missing,
        # unknown, or agent-kind ids fail loudly (never selecting another human).
        from agent_comms.schema import ValidationError

        self.assertEqual(self.store._actors.require_human(HUMAN_ID), HUMAN_ID)
        for bad in (None, "", "ghost", "arch"):
            with self.assertRaises(ValidationError):
                self.store._actors.require_human(bad)

    def test_only_supplied_human_sends_and_receives_with_multiple_humans(self) -> None:
        self.store.register_actor(OTHER_HUMAN_ID, "human", "pat")
        adapter = UnconfirmedAdapter()
        started = self._pending_past_deadline(adapter)

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        # Only the explicitly supplied human receives the operator infra notice.
        supplied = [
            m for m in self.store.list_inbox(HUMAN_ID, unread_only=False)
            if "cancellation infra" in m["subject"]
        ]
        other = [
            m for m in self.store.list_inbox(OTHER_HUMAN_ID, unread_only=False)
            if "cancellation infra" in m["subject"]
        ]
        self.assertEqual(len(supplied), 1)
        self.assertEqual(len(other), 0)
        # The producer blocker is sent FROM the supplied human only.
        producer_pages = [
            m for m in self.store.list_inbox("arch", unread_only=False)
            if "cancellation escalated" in m["subject"]
        ]
        self.assertEqual(len(producer_pages), 1)
        self.assertEqual(producer_pages[0]["from"], HUMAN_ID)


class ResidueEvidenceTest(unittest.TestCase):
    def test_unconfirmed_residue_needs_positive_same_run_evidence(self) -> None:
        # A dlq/termination_not_confirmed cancellation residue is NOT cleanable on
        # status alone; positive same-run exit evidence is required (the exact gate
        # the janitor consumes). This pins the residue-preservation contract.
        run_token = "run-token-residue-0001"
        without_evidence = {"run_token": run_token, "termination_result": "termination_not_confirmed"}
        self.assertIsNone(confirmed_termination_evidence(without_evidence, run_token))
        # A later exact same-run exit object upgrades cleanability. Revision 7 F2:
        # the janitor gate consumes ONLY the exact version-1 COMPLETE reaper proof.
        with_evidence = {
            "run_token": run_token,
            "termination_result": "termination_not_confirmed",
            "reaper_exit": {
                "proof_version": 1,
                "run_token": run_token,
                "returncode": 0,
                "source": "background_reap",
                "reaped_at": "2026-07-26T00:00:00+00:00",
                "registered_wrapper_reaped": True,
                "native_process_group_drained": True,
                "owned_artifacts_absent": {
                    "run_dir": True,
                    "control_socket": True,
                    "zdotdir_parent": True,
                },
            },
        }
        self.assertIsNotNone(confirmed_termination_evidence(with_evidence, run_token))


if __name__ == "__main__":
    unittest.main()
