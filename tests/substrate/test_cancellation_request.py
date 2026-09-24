"""Stage-2 T3: the internal cancellation *request* primitive and the exact
queued/bootstrap state machine.

Covers Required outcome 1 (producer/admin authorization, bounds, idempotency,
conflict refusal, terminal refusal) and Required outcome 3's queued arms
(unclaimed queued -> immediate ``not_started`` confirm + FIFO drain; claimed
queued/bootstrap before control identity -> pending, lineage held, no false
in_flight publication).

These are the INTERNAL store/domain primitives only. No MCP tool or CLI surface
is registered here; the primitive is exercised directly through the Store facade
(``request_cancellation``).
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import tempfile
import unittest
from pathlib import Path

from agent_comms.dispatch_ledger import (
    CANCELLATION_REASON_MAX,
    CancellationAuthorizationError,
    CancellationConflictError,
    CancellationStateError,
)
from agent_comms.schema import ValidationError
from agent_comms.store import Store

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"
OTHER_HUMAN_ID = "01J00000000000000000000002"


class _StubAdapter:
    """Supervised adapter double: dispatch publishes control identity; halt confirms."""

    def __init__(self) -> None:
        self.control_socket = "/nonexistent/agent-comms/run/s/control.sock"
        self.run_token = "run-token-cancel-req-0001"
        self.halt_calls: list = []

    def dispatch(self, context):
        from agent_comms.adapters import DispatchStart

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

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))


def _seed(store: Store, root: Path) -> None:
    store.register_actor(HUMAN_ID, "human", "alice")
    store.register_actor(OTHER_HUMAN_ID, "human", "pat")
    store.register_agent_actor("arch", "alpha", "architect", str(root / "arch"), [])
    store.register_agent_actor("arch2", "alpha", "architect", str(root / "arch2"), [])
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


class RequestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = Store(self.tmp / "agent-comms.sqlite")
        _seed(self.store, self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _queued(self, key: str = "q") -> dict:
        return self.store.dispatch_agent("arch", "wrk", key, f"S {key}", f"B {key}", [])


class AuthorizationTest(RequestBase):
    def test_producer_can_cancel_own_queued_dispatch(self) -> None:
        d = self._queued()
        result = self.store.request_cancellation(
            d["dispatch_id"], requesting_actor_id="arch", reason="obsolete", authority="producer"
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["cancellation_state"], "confirmed")

    def test_non_producer_agent_is_refused(self) -> None:
        d = self._queued()
        with self.assertRaises(CancellationAuthorizationError):
            self.store.request_cancellation(
                d["dispatch_id"], requesting_actor_id="arch2", reason="nope", authority="producer"
            )
        # No mutation: still queued.
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], "queued")

    def test_admin_human_can_cancel_any_dispatch(self) -> None:
        d = self._queued()
        result = self.store.request_cancellation(
            d["dispatch_id"], requesting_actor_id=HUMAN_ID, reason="operator withdrew", authority="admin"
        )
        self.assertEqual(result["status"], "cancelled")
        obs = _observed(self.store, d["dispatch_id"])
        self.assertEqual(obs["cancellation"]["authority"], "admin")
        self.assertEqual(obs["cancellation"]["requested_by"], HUMAN_ID)

    def test_admin_authority_requires_human_actor(self) -> None:
        d = self._queued()
        with self.assertRaises(CancellationAuthorizationError):
            self.store.request_cancellation(
                d["dispatch_id"], requesting_actor_id="arch", reason="x", authority="admin"
            )
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], "queued")

    def test_admin_unknown_actor_refused_before_mutation(self) -> None:
        d = self._queued()
        with self.assertRaises(ValidationError):
            self.store.request_cancellation(
                d["dispatch_id"], requesting_actor_id="ghost", reason="x", authority="admin"
            )
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], "queued")

    def test_unknown_authority_is_refused(self) -> None:
        d = self._queued()
        with self.assertRaises(ValidationError):
            self.store.request_cancellation(
                d["dispatch_id"], requesting_actor_id="arch", reason="x", authority="worker"
            )

    def test_unknown_dispatch_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.request_cancellation(
                "dispatch_missing", requesting_actor_id="arch", reason="x", authority="producer"
            )


class ReasonBoundsTest(RequestBase):
    def test_empty_reason_refused(self) -> None:
        d = self._queued()
        for bad in ("", "   "):
            with self.assertRaises(ValidationError):
                self.store.request_cancellation(
                    d["dispatch_id"], requesting_actor_id="arch", reason=bad, authority="producer"
                )
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], "queued")

    def test_oversized_reason_refused(self) -> None:
        d = self._queued()
        with self.assertRaises(ValidationError):
            self.store.request_cancellation(
                d["dispatch_id"],
                requesting_actor_id="arch",
                reason="x" * (CANCELLATION_REASON_MAX + 1),
                authority="producer",
            )
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], "queued")


class IdempotencyAndConflictTest(RequestBase):
    def _pending_inflight(self, key: str = "conflict"):
        # Build a queued row claimed for spawn but WITHOUT control identity so the
        # request stays pending (no adapter passed), giving us a durable request
        # object to test idempotency/conflict against.
        d = self._queued(key)
        with self.store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set auth_lineage_claimed_at = ? where dispatch_id = ?",
                ("2026-07-15T12:00:00+00:00", d["dispatch_id"]),
            )
        return d

    def test_same_actor_reason_is_idempotent(self) -> None:
        d = self._pending_inflight()
        first = self.store.request_cancellation(
            d["dispatch_id"], requesting_actor_id="arch", reason="dup", authority="producer"
        )
        self.assertEqual(first["cancellation_state"], "requested")
        obs1 = _observed(self.store, d["dispatch_id"])["cancellation"]
        second = self.store.request_cancellation(
            d["dispatch_id"], requesting_actor_id="arch", reason="dup", authority="producer"
        )
        self.assertEqual(second["cancellation_state"], "requested")
        obs2 = _observed(self.store, d["dispatch_id"])["cancellation"]
        # The original request timestamp / requester is preserved (not overwritten).
        self.assertEqual(obs1["requested_at"], obs2["requested_at"])
        self.assertEqual(obs1["requested_by"], obs2["requested_by"])

    def test_different_reason_conflicts(self) -> None:
        d = self._pending_inflight()
        self.store.request_cancellation(
            d["dispatch_id"], requesting_actor_id="arch", reason="first", authority="producer"
        )
        with self.assertRaises(CancellationConflictError):
            self.store.request_cancellation(
                d["dispatch_id"], requesting_actor_id="arch", reason="second", authority="producer"
            )
        # The original pending request is untouched.
        self.assertEqual(_observed(self.store, d["dispatch_id"])["cancellation"]["reason"], "first")

    def test_different_actor_cannot_overwrite_pending(self) -> None:
        d = self._pending_inflight()
        self.store.request_cancellation(
            d["dispatch_id"], requesting_actor_id="arch", reason="same", authority="producer"
        )
        # An admin with the same reason string is still a different authority/actor.
        with self.assertRaises(CancellationConflictError):
            self.store.request_cancellation(
                d["dispatch_id"], requesting_actor_id=HUMAN_ID, reason="same", authority="admin"
            )
        self.assertEqual(_observed(self.store, d["dispatch_id"])["cancellation"]["requested_by"], "arch")


class TerminalRefusalTest(RequestBase):
    def _set_status(self, dispatch_id: str, status: str) -> None:
        # A v2 row forced to ``closed`` carries the checked result the ledger
        # CHECK requires; the refusal under test never reads the result.
        with self.store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status = ?, result = case when ? = 'closed' "
                "then coalesce(result, 'satisfied') else result end where dispatch_id = ?",
                (status, status, dispatch_id),
            )

    def test_closed_dlq_spawnfailed_never_relabelled(self) -> None:
        for status in ("closed", "dlq", "spawn_failed_message_landed"):
            d = self._queued(f"term-{status}")
            self._set_status(d["dispatch_id"], status)
            with self.assertRaises(CancellationStateError):
                self.store.request_cancellation(
                    d["dispatch_id"], requesting_actor_id="arch", reason="late", authority="producer"
                )
            self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], status)

    def test_repeating_completed_cancellation_returns_existing_result(self) -> None:
        d = self._queued("done")
        first = self.store.request_cancellation(
            d["dispatch_id"], requesting_actor_id="arch", reason="withdraw", authority="producer"
        )
        self.assertEqual(first["status"], "cancelled")
        # Repeat returns the existing confirmed result, no error, no mutation churn.
        cancelled_at = _ledger(self.store, d["dispatch_id"])["cancelled_at"]
        second = self.store.request_cancellation(
            d["dispatch_id"], requesting_actor_id="arch", reason="withdraw", authority="producer"
        )
        self.assertEqual(second["status"], "cancelled")
        self.assertEqual(second["cancellation_state"], "confirmed")
        self.assertEqual(second["termination_result"], "not_started")
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["cancelled_at"], cancelled_at)


class QueuedUnclaimedTest(RequestBase):
    def test_unclaimed_queued_commits_not_started_and_cancels_transport(self) -> None:
        d = self._queued("unclaimed")
        result = self.store.request_cancellation(
            d["dispatch_id"], requesting_actor_id="arch", reason="withdraw", authority="producer"
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["previous_status"], "queued")
        self.assertEqual(result["cancellation_state"], "confirmed")
        self.assertEqual(result["termination_result"], "not_started")

        row = _ledger(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["cancelled_at"])
        self.assertIsNone(row["auth_lineage_claimed_at"])
        self.assertEqual(_transport(self.store, d["message_id"]), "cancelled")
        obs = _observed(self.store, d["dispatch_id"])["cancellation"]
        self.assertEqual(obs["state"], "confirmed")
        self.assertEqual(obs["termination_result"], "not_started")
        self.assertEqual(obs["reason"], "withdraw")

    def test_confirmed_queued_cancel_permits_fifo_drain_of_successor(self) -> None:
        # cap=1 so only one in_flight at a time. Two queued rows; cancel the first,
        # then the successor may drain.
        with self.store._db.connection() as conn:
            conn.execute("update actors set dispatch_cap = 1 where id = 'arch'")
        first = self._queued("fifo-first")
        second = self._queued("fifo-second")
        adapter = _StubAdapter()

        self.store.request_cancellation(
            first["dispatch_id"], requesting_actor_id="arch", reason="drop", authority="producer"
        )
        self.assertEqual(_ledger(self.store, first["dispatch_id"])["status"], "cancelled")

        started = self.store.start_queued_dispatches(lambda _r: adapter, limit=10)
        promoted = [a for a in started if a.get("dispatch_id") == second["dispatch_id"]]
        self.assertEqual(len(promoted), 1)
        self.assertEqual(_ledger(self.store, second["dispatch_id"])["status"], "in_flight")

    def test_no_socket_io_for_unclaimed_queued_cancel(self) -> None:
        # An unclaimed queued cancel needs no HALT: passing an adapter whose halt
        # would explode proves the terminal commit never touched the socket path.
        class ExplodingHalt(_StubAdapter):
            def halt(self, *a, **k):  # noqa: D401
                raise AssertionError("halt must not be called for an unclaimed queued cancel")

        d = self._queued("nosock")
        result = self.store.request_cancellation(
            d["dispatch_id"],
            requesting_actor_id="arch",
            reason="withdraw",
            authority="producer",
            adapter_for_runtime=lambda _r: ExplodingHalt(),
        )
        self.assertEqual(result["status"], "cancelled")


class ClaimedBootstrapTest(RequestBase):
    def _claimed_queued(self, key: str = "boot") -> dict:
        d = self._queued(key)
        with self.store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set auth_lineage_claimed_at = ? where dispatch_id = ?",
                ("2026-07-15T12:00:00+00:00", d["dispatch_id"]),
            )
        return d

    def test_claimed_queued_without_control_identity_stays_pending(self) -> None:
        # Bootstrap in progress (claimed for spawn) but control identity not yet
        # published: the request is recorded pending, never inferred not_started,
        # never publishes a false in_flight, and holds the lineage claim.
        d = self._claimed_queued()
        adapter = _StubAdapter()
        result = self.store.request_cancellation(
            d["dispatch_id"],
            requesting_actor_id="arch",
            reason="withdraw",
            authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        self.assertEqual(result["cancellation_state"], "requested")
        row = _ledger(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "queued")  # NOT cancelled, NOT in_flight
        self.assertIsNotNone(row["auth_lineage_claimed_at"])  # lineage held
        self.assertEqual(_transport(self.store, d["message_id"]), "sent")  # transport untouched
        self.assertEqual(result["lineage_released"], False)
        # No false not_started termination.
        self.assertNotEqual(result.get("termination_result"), "not_started")
        self.assertEqual(adapter.halt_calls, [])  # nothing to halt yet


if __name__ == "__main__":
    unittest.main()
