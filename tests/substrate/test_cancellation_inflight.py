"""Stage-2 T4: authenticated in-flight cancellation.

An in_flight (or identity-bearing) cancellation uses the authenticated same-token
supervisor HALT through the existing adapter/control identity. Terminal
``cancelled`` is accepted ONLY after a matching HALT acknowledgement or exact
same-run SQL exit evidence, distinguishing ``supervised_halt_confirmed`` from
``same_run_exit_confirmed``. A missing/wrong token, unreachable supervisor, or a
raised HALT leaves the row nonterminal, holds cap/lineage, uses no PID fallback,
and never promotes queued work. Bounded partial-work evidence is captured before
the terminal commit; the terminal CAS re-proves status and run token.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import tempfile
import unittest
from pathlib import Path

from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.store import Store

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"


class ConfirmingAdapter:
    def __init__(self) -> None:
        self.control_socket = "/nonexistent/agent-comms/run/s/control.sock"
        self.run_token = "run-token-inflight-0001"
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

    def status(self, spawn_handle, observed_values=None):
        from collections import namedtuple

        return namedtuple("S", ["state", "detail"])("running", "d")

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))


class UnconfirmedAdapter(ConfirmingAdapter):
    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        raise RuntimeError("authenticated halt did not confirm termination")


class ExplodingHaltAdapter(ConfirmingAdapter):
    def halt(self, spawn_handle, observed_values=None) -> None:
        raise AssertionError("HALT must not be called when same-run exit already confirms")


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


def _transport(store: Store, message_id: str) -> str:
    with store._db.connection() as conn:
        return conn.execute(
            "select status from message_recipients where message_id = ? and to_agent = 'wrk'",
            (message_id,),
        ).fetchone()["status"]


def _observed(store: Store, dispatch_id: str) -> dict:
    return json.loads(_ledger(store, dispatch_id)["observed_values_json"] or "{}")


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


class InflightBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = Store(self.tmp / "agent-comms.sqlite")
        _seed(self.store, self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _in_flight(self, adapter, key: str = "live") -> dict:
        self.store.dispatch_agent("arch", "wrk", key, f"S {key}", f"B {key}", [])
        started = self.store.start_queued_dispatches(lambda _r: adapter, ttl_seconds=3600)
        row = started[0]
        assert row["status"] == "in_flight", row
        # The stub runtime does not engage the codex-only auth-lineage claim, so
        # stamp the lineage-claimed marker the single-lineage hold semantics assume
        # for an in_flight row: a confirmed cancellation RELEASES it (auth lineage
        # -> NULL), while a pending/unconfirmed cancellation must HOLD it.
        with self.store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set auth_lineage_claimed_at = ? where dispatch_id = ?",
                ("2026-07-26T00:00:00+00:00", row["dispatch_id"]),
            )
        return row


class HaltConfirmedTest(InflightBase):
    def test_authenticated_halt_confirmed_becomes_cancelled(self) -> None:
        adapter = ConfirmingAdapter()
        started = self._in_flight(adapter)
        result = self.store.request_cancellation(
            started["dispatch_id"],
            requesting_actor_id="arch",
            reason="withdraw live",
            authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["previous_status"], "in_flight")
        self.assertEqual(result["cancellation_state"], "confirmed")
        self.assertEqual(result["termination_result"], "supervised_halt_confirmed")
        self.assertTrue(result["lineage_released"])

        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["cancelled_at"])
        self.assertEqual(row["auth_lineage_claimed_at"], "2026-07-26T00:00:00+00:00")
        self.assertEqual(_transport(self.store, started["message_id"]), "cancelled")
        # The HALT carried the authenticated observed identity (no PID parsing).
        self.assertTrue(adapter.halt_calls)
        self.assertEqual(adapter.halt_calls[-1][1].get("run_token"), adapter.run_token)

    def test_partial_work_evidence_captured_before_terminal(self) -> None:
        adapter = ConfirmingAdapter()
        started = self._in_flight(adapter)
        reply = self.store.send_message(
            "wrk", ["arch"], "Re", "BLOCKED partial", [], parent_message_id=started["message_id"]
        )
        self.store.post_status("wrk", "blocked halfway", [], dispatch_id=started["dispatch_id"])

        self.store.request_cancellation(
            started["dispatch_id"],
            requesting_actor_id="arch",
            reason="withdraw",
            authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        evidence = _observed(self.store, started["dispatch_id"])["cancellation"]["partial_evidence"]
        self.assertEqual(evidence["reply_message_ids"], [reply["id"]])
        self.assertEqual(evidence["latest_status_summary"], "blocked halfway")
        # Never mislabelled as a worker exit.
        self.assertEqual(evidence["classification"], "cancellation_partial_work")


class BoundedCancellationEvidenceTest(InflightBase):
    """T8: the cancellation path shares the bounded evidence shape (distinct label)."""

    def test_more_than_ten_replies_cap_with_exact_total_and_no_bodies(self) -> None:
        adapter = ConfirmingAdapter()
        started = self._in_flight(adapter)
        for i in range(12):
            self.store.send_message(
                "wrk",
                ["arch"],
                f"Re {i}",
                f"secret-partial-{i}",
                [],
                parent_message_id=started["message_id"],
            )
        self.store.request_cancellation(
            started["dispatch_id"],
            requesting_actor_id="arch",
            reason="withdraw",
            authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        evidence = _observed(self.store, started["dispatch_id"])["cancellation"]["partial_evidence"]
        # Distinct classification: a cancellation never masquerades as a worker exit.
        self.assertEqual(evidence["classification"], "cancellation_partial_work")
        # Shared bounded shape: at most ten stored ids plus the EXACT total.
        self.assertEqual(len(evidence["reply_message_ids"]), 10)
        self.assertEqual(evidence["reply_total_count"], 12)
        # No reply BODY is ever persisted.
        blob = json.dumps(evidence)
        for i in range(12):
            self.assertNotIn(f"secret-partial-{i}", blob)


class SameRunExitTest(InflightBase):
    def test_same_run_exit_confirms_without_halt(self) -> None:
        adapter = ExplodingHaltAdapter()
        started = self._in_flight(adapter)
        # Revision 7 F2: same_run_exit_confirmed rests on the exact version-1
        # COMPLETE reaper proof, never a bare worker_exit. With the complete proof
        # present the cancellation confirms same-run WITHOUT a HALT (the exploding
        # halt adapter proves no HALT is attempted).
        _merge_observed(
            self.store,
            started["dispatch_id"],
            {
                "reaper_exit": {
                    "proof_version": 1,
                    "run_token": adapter.run_token,
                    "returncode": 0,
                    "source": "halt_finalize",
                    "reaped_at": "2026-07-26T00:00:00+00:00",
                    "registered_wrapper_reaped": True,
                    "native_process_group_drained": True,
                    "owned_artifacts_absent": {
                        "run_dir": True,
                        "control_socket": True,
                        "zdotdir_parent": True,
                    },
                }
            },
        )
        result = self.store.request_cancellation(
            started["dispatch_id"],
            requesting_actor_id="arch",
            reason="already exited",
            authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["termination_result"], "same_run_exit_confirmed")

    def test_stale_run_exit_does_not_confirm_and_holds(self) -> None:
        # A worker_exit object carrying an OLDER run token is not same-run
        # evidence; with an unconfirmed halt the row stays pending (object
        # existence alone never terminalizes a cancellation).
        adapter = UnconfirmedAdapter()
        started = self._in_flight(adapter)
        _merge_observed(
            self.store,
            started["dispatch_id"],
            {"worker_exit": {"returncode": 0, "run_token": "run-token-STALE-0000"}},
        )
        result = self.store.request_cancellation(
            started["dispatch_id"],
            requesting_actor_id="arch",
            reason="withdraw",
            authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        self.assertEqual(result["status"], "in_flight")
        self.assertEqual(result["cancellation_state"], "requested")


class UnconfirmedHoldsTest(InflightBase):
    def test_unconfirmed_halt_stays_pending_holds_lineage_no_pid(self) -> None:
        adapter = UnconfirmedAdapter()
        started = self._in_flight(adapter)
        result = self.store.request_cancellation(
            started["dispatch_id"],
            requesting_actor_id="arch",
            reason="withdraw",
            authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        self.assertEqual(result["status"], "in_flight")
        self.assertEqual(result["cancellation_state"], "requested")
        self.assertIsNone(result["termination_result"])
        self.assertFalse(result["lineage_released"])
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "in_flight")  # nonterminal, held
        self.assertEqual(_transport(self.store, started["message_id"]), "sent")  # not withdrawn
        cancellation = _observed(self.store, started["dispatch_id"])["cancellation"]
        self.assertGreaterEqual(cancellation["attempts"], 1)
        self.assertTrue(adapter.halt_calls)  # authenticated socket attempt, not PID


class FifoPromotionTest(InflightBase):
    def _cap_one(self) -> None:
        with self.store._db.connection() as conn:
            conn.execute("update actors set dispatch_cap = 1 where id = 'arch'")

    def test_promotion_only_after_confirmed_cancel(self) -> None:
        self._cap_one()
        confirming = ConfirmingAdapter()
        first = self._in_flight(confirming, key="a")  # holds the single cap
        self.store.dispatch_agent("arch", "wrk", "b", "S b", "B b", [])
        second = self.store._dispatch_by_idempotency_key_fresh("arch", "b")
        self.assertEqual(_ledger(self.store, second["dispatch_id"])["status"], "queued")

        # Confirmed cancel of the first releases the cap; the successor then drains.
        self.store.request_cancellation(
            first["dispatch_id"],
            requesting_actor_id="arch",
            reason="withdraw",
            authority="producer",
            adapter_for_runtime=lambda _r: confirming,
        )
        self.assertEqual(_ledger(self.store, first["dispatch_id"])["status"], "cancelled")
        self.store.start_queued_dispatches(lambda _r: confirming, limit=10)
        self.assertEqual(_ledger(self.store, second["dispatch_id"])["status"], "in_flight")

    def test_pending_cancel_does_not_promote_successor(self) -> None:
        self._cap_one()
        unconfirmed = UnconfirmedAdapter()
        first = self._in_flight(unconfirmed, key="a")  # holds the single cap
        self.store.dispatch_agent("arch", "wrk", "b", "S b", "B b", [])
        second = self.store._dispatch_by_idempotency_key_fresh("arch", "b")

        self.store.request_cancellation(
            first["dispatch_id"],
            requesting_actor_id="arch",
            reason="withdraw",
            authority="producer",
            adapter_for_runtime=lambda _r: unconfirmed,
        )
        # First held in_flight (cap still consumed); successor cannot start.
        self.assertEqual(_ledger(self.store, first["dispatch_id"])["status"], "in_flight")
        self.store.start_queued_dispatches(lambda _r: unconfirmed, limit=10)
        self.assertEqual(_ledger(self.store, second["dispatch_id"])["status"], "queued")


class RevisionSevenSameRunProofInflightTest(InflightBase):
    """Revision 7 F2 (red-first): only the COMPLETE exact version-1 ``$.reaper_exit``
    proof may confirm ``same_run_exit_confirmed``. A same-run ``worker_exit`` alone
    (child-exit evidence the still-alive wrapper persists) and an incomplete
    wrapper/group/cleanup proof must REFUSE terminal cancellation and hold
    cap/lineage. Red against the current predicate, which confirms from any
    same-run ``worker_exit``/``reaper_exit`` object carrying a matching token.
    """

    def test_same_run_worker_exit_alone_refuses_cancelled(self) -> None:
        adapter = UnconfirmedAdapter()
        started = self._in_flight(adapter)
        # Same-run child-exit evidence ONLY: no registered-wrapper reap proof.
        _merge_observed(
            self.store,
            started["dispatch_id"],
            {"worker_exit": {"returncode": 0, "source": "halt", "run_token": adapter.run_token}},
        )
        result = self.store.request_cancellation(
            started["dispatch_id"],
            requesting_actor_id="arch",
            reason="worker_exit is child-exit evidence only",
            authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        # worker_exit alone is not a qualifying proof: the request must fall
        # through to the authenticated HALT (here unconfirmed) and stay pending
        # with cap/lineage held -- never a terminal ``cancelled``.
        self.assertEqual(result["status"], "in_flight")
        self.assertEqual(result["cancellation_state"], "requested")
        self.assertIsNone(result["termination_result"])
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "in_flight")
        self.assertIsNotNone(row["auth_lineage_claimed_at"])  # lineage held
        self.assertEqual(_transport(self.store, started["message_id"]), "sent")  # not withdrawn

    def test_incomplete_wrapper_group_or_cleanup_proof_retains_cap_and_lineage(self) -> None:
        adapter = UnconfirmedAdapter()
        started = self._in_flight(adapter)
        # A reaper_exit that names the wrapper reap but whose native process-group
        # drain / owned-artifact cleanup facts are NOT yet complete: the physical
        # barriers are unmet, so the proof must not qualify.
        incomplete = {
            "proof_version": 1,
            "run_token": adapter.run_token,
            "returncode": 0,
            "source": "halt_finalize",
            "reaped_at": "2026-07-26T00:00:00+00:00",
            "registered_wrapper_reaped": True,
            "native_process_group_drained": False,  # group not drained yet
            "owned_artifacts_absent": {"run_dir": False, "control_socket": True, "zdotdir_parent": True},
        }
        _merge_observed(self.store, started["dispatch_id"], {"reaper_exit": incomplete})
        result = self.store.request_cancellation(
            started["dispatch_id"],
            requesting_actor_id="arch",
            reason="wrapper reap present but group/cleanup incomplete",
            authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        # An incomplete proof holds the request nonterminal with cap and lineage.
        self.assertEqual(result["status"], "in_flight")
        self.assertEqual(result["cancellation_state"], "requested")
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "in_flight")
        self.assertIsNotNone(row["auth_lineage_claimed_at"])  # lineage held
        self.assertEqual(_transport(self.store, started["message_id"]), "sent")


if __name__ == "__main__":
    unittest.main()
