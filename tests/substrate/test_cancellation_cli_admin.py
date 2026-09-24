"""Stage-2 T1: the credentialed admin ``cancel-dispatch`` CLI command.

Exercises the REAL CLI parser + handler (``agent_comms.cli.run``) against
temporary databases, proving:

* ``require_admin_credential()`` runs BEFORE any Store mutation (missing and bad
  credentials refuse and leave the ledger untouched);
* the accepted cancellation engine is invoked with ``authority='admin'`` and the
  runtime adapter, requiring the supplied ``--from-actor-id`` to be a registered
  human;
* the full T1 refusal/idempotency contract holds at this surface (wrong-kind
  admin, unknown dispatch, empty/oversized reason, idempotent repeat, existing
  cancelled returned without relabelling, conflict refusal, and closed/dlq/
  spawn_failed terminal rows never relabelled).

The command surface is ``agent-comms admin cancel-dispatch --from-actor-id ...
--dispatch-id ... --reason ...``.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import cli
from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.cli import _helpers
from agent_comms.dispatch_ledger import CANCELLATION_REASON_MAX
from agent_comms.store import Store

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"
OTHER_HUMAN_ID = "01J00000000000000000000002"
VALID_TOKEN = "operator-secret"


class ConfirmingAdapter:
    """A stub runtime adapter whose authenticated HALT always confirms.

    Used to drive an in-flight admin cancellation to a synchronously CONFIRMED
    termination through the public ``request_cancellation`` seam, so the T8 admin
    notice is claimed from a confirmed/cancelled canonical result.
    """

    def __init__(self) -> None:
        self.control_socket = "/nonexistent/agent-comms/run/s/control.sock"
        self.run_token = "run-token-cli-admin-0001"
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


class AdminCancelCliBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        self.store.register_actor(HUMAN_ID, "human", "alice")
        self.store.register_actor(OTHER_HUMAN_ID, "human", "pat")
        self.store.register_agent_actor("arch", "alpha", "architect", str(self.tmp / "arch"), [])
        self.store.register_agent_actor(
            "wrk", "alpha", "worker", str(self.tmp / "wrk"), [], runtime="stub", spawn={"command": "stub"},
            owner="arch",
        )

        # Credential file: mode 600 with a known token, wired through the helper
        # global the handler reads at call time.
        self.token_path = self.tmp / "admin-token"
        self.token_path.write_text(VALID_TOKEN)
        os.chmod(self.token_path, 0o600)
        patcher = mock.patch.object(_helpers, "ADMIN_TOKEN_PATH", self.token_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self._saved_token = os.environ.get("AGENT_COMMS_ADMIN_TOKEN")
        self.addCleanup(self._restore_token)
        os.environ["AGENT_COMMS_ADMIN_TOKEN"] = VALID_TOKEN

    def _restore_token(self) -> None:
        if self._saved_token is None:
            os.environ.pop("AGENT_COMMS_ADMIN_TOKEN", None)
        else:
            os.environ["AGENT_COMMS_ADMIN_TOKEN"] = self._saved_token

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- helpers -------------------------------------------------------- #
    def _queued(self, key: str = "q") -> dict:
        return self.store.dispatch_agent("arch", "wrk", key, f"S {key}", f"B {key}", [])

    def _in_flight(self, adapter, key: str = "live") -> dict:
        """A real in_flight row (queued dispatch started through the adapter)."""
        self.store.dispatch_agent("arch", "wrk", key, f"S {key}", f"B {key}", [])
        started = self.store.start_queued_dispatches(lambda _r: adapter, ttl_seconds=3600)
        row = started[0]
        assert row["status"] == "in_flight", row
        return row

    def _cancel(self, dispatch_id: str, reason: str, *, from_actor_id: str = HUMAN_ID) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cli.run(
                [
                    "--db",
                    str(self.db_path),
                    "admin",
                    "cancel-dispatch",
                    "--from-actor-id",
                    from_actor_id,
                    "--dispatch-id",
                    dispatch_id,
                    "--reason",
                    reason,
                ]
            )
        text = buffer.getvalue().strip()
        payload = json.loads(text) if text else {}
        return rc, payload

    def _ledger(self, dispatch_id: str):
        with self.store._db.connection() as conn:
            return conn.execute(
                "select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
            ).fetchone()

    def _observed(self, dispatch_id: str) -> dict:
        return json.loads(self._ledger(dispatch_id)["observed_values_json"] or "{}")

    def _claim(self, dispatch_id: str) -> None:
        with self.store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set auth_lineage_claimed_at = ? where dispatch_id = ?",
                ("2026-07-18T04:00:00+00:00", dispatch_id),
            )

    def _set_status(self, dispatch_id: str, status: str) -> None:
        # A v2 row forced to ``closed`` carries the checked result the ledger
        # CHECK requires; the refusal under test never reads the result.
        with self.store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status = ?, result = case when ? = 'closed' "
                "then coalesce(result, 'satisfied') else result end where dispatch_id = ?",
                (status, status, dispatch_id),
            )

    def _admin_notices(self) -> list[dict]:
        """The durable admin-cancellation producer notices (message + copy)."""
        with self.store._db.connection() as conn:
            rows = conn.execute(
                """
                select m.id, m.from_agent, m.subject, m.body, m.priority, m.requires_ack,
                       mt.parent_message_id, r.to_agent
                from messages m
                join message_recipients r on r.message_id = m.id
                left join message_threads mt on mt.message_id = m.id
                where m.subject like 'Admin cancellation:%'
                order by m.created_at, m.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _admin_notice_inventory(self) -> tuple[int, int, int]:
        """Exact (message, recipient, thread) row counts for admin notices."""
        with self.store._db.connection() as conn:
            messages = conn.execute(
                "select count(*) c from messages where subject like 'Admin cancellation:%'"
            ).fetchone()["c"]
            recipients = conn.execute(
                "select count(*) c from message_recipients r "
                "join messages m on m.id = r.message_id "
                "where m.subject like 'Admin cancellation:%'"
            ).fetchone()["c"]
            threads = conn.execute(
                "select count(*) c from message_threads mt "
                "join messages m on m.id = mt.message_id "
                "where m.subject like 'Admin cancellation:%'"
            ).fetchone()["c"]
        return messages, recipients, threads

    def _producer_semaphores(self) -> list[Path]:
        return sorted(p for p in (self.tmp / "arch").rglob("new_messages") if p.is_file())


class AdminCancelHappyPathTest(AdminCancelCliBase):
    def test_registered_human_admin_cancels_queued_dispatch(self) -> None:
        d = self._queued("happy")
        rc, payload = self._cancel(d["dispatch_id"], "operator withdrew")
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "cancelled")
        self.assertEqual(payload["cancellation_state"], "confirmed")
        self.assertEqual(payload["authority"], "admin")
        obs = self._observed(d["dispatch_id"])["cancellation"]
        self.assertEqual(obs["authority"], "admin")
        self.assertEqual(obs["requested_by"], HUMAN_ID)
        self.assertEqual(obs["reason"], "operator withdrew")


class AdminCancelCredentialTest(AdminCancelCliBase):
    def test_missing_credential_refuses_before_mutation(self) -> None:
        os.environ.pop("AGENT_COMMS_ADMIN_TOKEN", None)
        d = self._queued("nocred")
        rc, payload = self._cancel(d["dispatch_id"], "withdraw")
        self.assertEqual(rc, 2)
        self.assertFalse(payload["ok"])
        self.assertIn("operator credential", payload["error"])
        # No cancellation object was written; the ledger is untouched.
        self.assertEqual(self._ledger(d["dispatch_id"])["status"], "queued")
        self.assertNotIn("cancellation", self._observed(d["dispatch_id"]))

    def test_bad_credential_refuses_before_mutation(self) -> None:
        os.environ["AGENT_COMMS_ADMIN_TOKEN"] = "wrong-secret"
        d = self._queued("badcred")
        rc, payload = self._cancel(d["dispatch_id"], "withdraw")
        self.assertEqual(rc, 2)
        self.assertFalse(payload["ok"])
        self.assertIn("operator credential", payload["error"])
        self.assertEqual(self._ledger(d["dispatch_id"])["status"], "queued")
        self.assertNotIn("cancellation", self._observed(d["dispatch_id"]))

    def test_missing_credential_refuses_even_for_unknown_dispatch(self) -> None:
        # The credential gate precedes any engine lookup: an unknown dispatch id
        # still surfaces the credential refusal, not "unknown dispatch".
        os.environ.pop("AGENT_COMMS_ADMIN_TOKEN", None)
        rc, payload = self._cancel("dispatch_missing", "withdraw")
        self.assertEqual(rc, 2)
        self.assertIn("operator credential", payload["error"])
        self.assertNotIn("unknown dispatch", payload["error"])


class AdminCancelRefusalTest(AdminCancelCliBase):
    def test_non_human_admin_actor_refused(self) -> None:
        d = self._queued("nonhuman")
        rc, payload = self._cancel(d["dispatch_id"], "withdraw", from_actor_id="arch")
        self.assertEqual(rc, 2)
        self.assertIn("human", payload["error"])
        self.assertEqual(self._ledger(d["dispatch_id"])["status"], "queued")
        self.assertNotIn("cancellation", self._observed(d["dispatch_id"]))

    def test_unknown_dispatch_refused(self) -> None:
        rc, payload = self._cancel("dispatch_missing", "withdraw")
        self.assertEqual(rc, 2)
        self.assertIn("unknown dispatch_id", payload["error"])

    def test_empty_reason_refused(self) -> None:
        d = self._queued("emptyreason")
        rc, payload = self._cancel(d["dispatch_id"], "   ")
        self.assertEqual(rc, 2)
        self.assertIn("reason must not be empty", payload["error"])
        self.assertEqual(self._ledger(d["dispatch_id"])["status"], "queued")

    def test_oversized_reason_refused(self) -> None:
        d = self._queued("bigreason")
        rc, payload = self._cancel(d["dispatch_id"], "x" * (CANCELLATION_REASON_MAX + 1))
        self.assertEqual(rc, 2)
        self.assertIn("reason exceeds", payload["error"])
        self.assertEqual(self._ledger(d["dispatch_id"])["status"], "queued")

    def test_conflicting_actor_refused_and_original_preserved(self) -> None:
        # A claimed queued row (no control identity yet) records a PENDING admin
        # request; a second admin from a DIFFERENT actor with the same reason is a
        # conflict that never overwrites the original.
        d = self._queued("conflict")
        self._claim(d["dispatch_id"])
        rc_first, first = self._cancel(d["dispatch_id"], "shared", from_actor_id=HUMAN_ID)
        self.assertEqual(rc_first, 0)
        self.assertEqual(first["cancellation_state"], "requested")

        rc_conflict, payload = self._cancel(d["dispatch_id"], "shared", from_actor_id=OTHER_HUMAN_ID)
        self.assertEqual(rc_conflict, 2)
        self.assertIn("already has a pending cancellation", payload["error"])
        self.assertEqual(self._observed(d["dispatch_id"])["cancellation"]["requested_by"], HUMAN_ID)


class AdminCancelIdempotencyAndTerminalTest(AdminCancelCliBase):
    def test_repeat_is_idempotent_and_not_relabelled(self) -> None:
        d = self._queued("idem")
        rc1, first = self._cancel(d["dispatch_id"], "withdraw")
        self.assertEqual(rc1, 0)
        self.assertEqual(first["status"], "cancelled")
        cancelled_at = self._ledger(d["dispatch_id"])["cancelled_at"]

        rc2, second = self._cancel(d["dispatch_id"], "withdraw")
        self.assertEqual(rc2, 0)
        self.assertEqual(second["status"], "cancelled")
        self.assertEqual(second["cancellation_state"], "confirmed")
        self.assertEqual(second["termination_result"], "not_started")
        # The existing confirmed result is returned; no relabel / churn.
        self.assertEqual(self._ledger(d["dispatch_id"])["cancelled_at"], cancelled_at)

    def test_closed_dlq_spawnfailed_never_relabelled(self) -> None:
        for status in ("closed", "dlq", "spawn_failed_message_landed"):
            with self.subTest(status=status):
                d = self._queued(f"term-{status}")
                self._set_status(d["dispatch_id"], status)
                rc, payload = self._cancel(d["dispatch_id"], "late")
                self.assertEqual(rc, 2)
                self.assertIn("never relabels", payload["error"])
                self.assertEqual(self._ledger(d["dispatch_id"])["status"], status)


class AdminCancelNoticeTest(AdminCancelCliBase):
    """T8: the single durable admin-cancellation producer notice."""

    def test_immediate_confirmed_cancellation_emits_single_producer_notice(self) -> None:
        d = self._queued("noticeconfirm")
        rc, payload = self._cancel(d["dispatch_id"], "operator withdrew")
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "cancelled")

        notices = self._admin_notices()
        self.assertEqual(len(notices), 1)
        n = notices[0]
        # Sender is the explicit human admin; recipient is the dispatch producer;
        # threaded to the ORIGINAL dispatch message; deterministic subject.
        self.assertEqual(n["from_agent"], HUMAN_ID)
        self.assertEqual(n["to_agent"], "arch")
        self.assertEqual(n["parent_message_id"], d["message_id"])
        self.assertEqual(n["subject"], f"Admin cancellation: {d['dispatch_id']}")
        # Confirmed wording: termination confirmed (not_started), and the outcome
        # is NEVER called a DLQ.
        self.assertIn("cancellation_state=confirmed", n["body"])
        self.assertIn("ledger_status=cancelled", n["body"])
        self.assertIn("termination_confirmed=yes", n["body"])
        self.assertIn("not_started", n["body"])
        self.assertNotIn("dlq", n["body"].lower())
        self.assertIn("reason=operator withdrew", n["body"])
        # The notice never reuses the reserved operator-settlement phrase.
        self.assertNotIn("ledger released; termination not confirmed", n["body"])

        # The notice id/timestamp are bound inside the cancellation object and the
        # result echoes the same id.
        cancellation = self._observed(d["dispatch_id"])["cancellation"]
        self.assertEqual(cancellation["admin_notice_message_id"], n["id"])
        self.assertIn("admin_notice_at", cancellation)
        self.assertEqual(payload["admin_notice_message_id"], n["id"])
        # A producer wake semaphore was written after the commit.
        self.assertTrue(self._producer_semaphores())

    def test_pending_cancellation_notice_has_unconfirmed_wording(self) -> None:
        # A claimed queued (bootstrap) row records a PENDING request; the notice
        # says termination is not confirmed and the ledger/lineage remain held.
        d = self._queued("noticepending")
        self._claim(d["dispatch_id"])
        rc, payload = self._cancel(d["dispatch_id"], "operator withdrew")
        self.assertEqual(rc, 0)
        self.assertEqual(payload["cancellation_state"], "requested")

        notices = self._admin_notices()
        self.assertEqual(len(notices), 1)
        n = notices[0]
        self.assertEqual(n["from_agent"], HUMAN_ID)
        self.assertEqual(n["to_agent"], "arch")
        self.assertEqual(n["parent_message_id"], d["message_id"])
        self.assertEqual(n["subject"], f"Admin cancellation: {d['dispatch_id']}")
        self.assertIn("cancellation_state=requested", n["body"])
        self.assertIn("ledger_status=queued", n["body"])
        self.assertIn("termination_confirmed=no", n["body"])
        self.assertIn("not confirmed", n["body"])
        self.assertIn("HELD", n["body"])
        # A pending request is NEVER described as cancelled: the notice must not
        # claim the dispatch "has been cancelled" or "has cancelled this dispatch".
        self.assertNotIn("has been cancelled", n["body"])
        self.assertNotIn("has cancelled this dispatch", n["body"])
        # Never called a DLQ and never the reserved settlement phrase.
        self.assertNotIn("dlq", n["body"].lower())
        self.assertNotIn("ledger released; termination not confirmed", n["body"])
        cancellation = self._observed(d["dispatch_id"])["cancellation"]
        self.assertEqual(cancellation["admin_notice_message_id"], n["id"])

    def test_inflight_confirmed_public_drive_notice_says_confirmed(self) -> None:
        # A claimed/in-flight admin cancellation whose SYNCHRONOUS drive confirms
        # termination (through the public ``request_cancellation`` seam) claims its
        # sole notice from the CONFIRMED canonical result: the notice says
        # confirmed/cancelled, never pending/unconfirmed.
        adapter = ConfirmingAdapter()
        started = self._in_flight(adapter, key="inflightconfirm")
        result = self.store.request_cancellation(
            started["dispatch_id"],
            requesting_actor_id=HUMAN_ID,
            reason="operator withdrew",
            authority="admin",
            adapter_for_runtime=lambda _r: adapter,
        )
        # The drive confirmed the authenticated HALT, so the canonical row is a
        # confirmed cancel.
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["previous_status"], "in_flight")
        self.assertEqual(result["cancellation_state"], "confirmed")
        self.assertEqual(result["termination_result"], "supervised_halt_confirmed")
        self.assertTrue(adapter.halt_calls)  # the authenticated socket HALT ran

        notices = self._admin_notices()
        self.assertEqual(len(notices), 1)
        n = notices[0]
        self.assertEqual(n["from_agent"], HUMAN_ID)
        self.assertEqual(n["to_agent"], "arch")
        self.assertEqual(n["parent_message_id"], started["message_id"])
        # Confirmed wording drawn from the post-drive canonical result.
        self.assertIn("cancellation_state=confirmed", n["body"])
        self.assertIn("ledger_status=cancelled", n["body"])
        self.assertIn("termination_confirmed=yes", n["body"])
        self.assertIn("supervised_halt_confirmed", n["body"])
        # It is NEVER the pending/unconfirmed wording, and never says "has been
        # cancelled" is only requested / not confirmed.
        self.assertNotIn("termination_confirmed=no", n["body"])
        self.assertNotIn("cancellation_state=requested", n["body"])
        self.assertNotIn("has REQUESTED cancellation", n["body"])
        self.assertNotIn("not yet confirmed", n["body"])
        # Never a DLQ, never the reserved settlement phrase.
        self.assertNotIn("dlq", n["body"].lower())
        self.assertNotIn("ledger released; termination not confirmed", n["body"])
        self.assertEqual(result["admin_notice_message_id"], n["id"])
        self.assertEqual(
            self._observed(started["dispatch_id"])["cancellation"]["admin_notice_message_id"],
            n["id"],
        )
        self.assertTrue(self._producer_semaphores())

    def test_concurrent_identical_admin_requests_converge_on_single_notice(self) -> None:
        # Two genuinely concurrent identical admin requests race at the REAL
        # post-drive notice-claim boundary (``_claim_admin_cancellation_notice``),
        # NOT merely at public entry, and converge on ONE notice with exactly one
        # message, recipient, thread, and post-commit semaphore publication.
        #
        # To prove the single notice derives its wording from the canonical
        # transactional reread rather than either caller's stale interim view, the
        # two contenders reach that boundary with DIFFERING drive outcomes for the
        # SAME identical admin request against one in_flight row:
        #
        #   * the pending-side caller (A) supplies NO live-termination adapter, so it
        #     records the request and reaches the claim boundary with an unconfirmed
        #     (``requested``) caller-side outcome;
        #   * the confirming caller (B) supplies the authenticated ConfirmingAdapter
        #     and performs the REAL out-of-transaction HALT drive, so it reaches the
        #     boundary with the canonical row already ``cancelled``/``confirmed``.
        #
        # Test-only instrumentation overrides ONLY the claim seam to coordinate the
        # interleaving; each contender still invokes the real production claim
        # (unchanged predicate + BEGIN IMMEDIATE transaction) via the captured
        # superclass method. The pending-side caller is forced to run the real claim
        # FIRST while its own interim outcome is still ``requested``; if the notice
        # derived from that stale interim it would be mislabelled pending, but the
        # real canonical reread (which B's drive advanced to ``confirmed``) yields the
        # CONFIRMED notice instead.
        adapter = ConfirmingAdapter()
        started = self._in_flight(adapter, key="concurrentclaim")
        dispatch_id = started["dispatch_id"]
        started_observed = self._observed(dispatch_id)
        self.assertNotIn("cancellation", started_observed)

        a_name = "admin-cancel-A-pending"
        b_name = "admin-cancel-B-confirming"

        # Observation only: count REAL post-commit notice-semaphore publications and
        # which contender emitted each (call-through, never weakening the publish).
        publications: list[tuple[str, str]] = []
        pub_lock = threading.Lock()
        real_publish = self.store._dispatch._write_admin_cancellation_notice_semaphore

        def counting_publish(notice, producer):
            with pub_lock:
                publications.append((threading.current_thread().name, notice["id"]))
            return real_publish(notice, producer)

        # Coordination proving both contenders reached the claim boundary before any
        # real claim ran, and forcing the pending-side caller to be the sole claimant.
        coord_lock = threading.Lock()
        arrivals: list[str] = []
        interim_states: dict[str, object] = {}
        release_snapshots: dict[str, list[str]] = {}
        a_reached_boundary = threading.Event()
        boundary_barrier = threading.Barrier(2, timeout=20)
        winner_claim_done = threading.Event()
        loser_saw_winner_done: dict[str, bool] = {}
        real_claim = self.store._dispatch._claim_admin_cancellation_notice

        def coordinating_claim(dispatch_id_arg, admin_actor_id, reason, now):
            # Test-only override of the claim seam: it records arrival, blocks on a
            # barrier, then calls the REAL superclass claim. It never emulates or
            # weakens the production predicate/transaction.
            name = threading.current_thread().name
            interim = self.store._dispatch._cancellation_result(dispatch_id_arg)
            with coord_lock:
                arrivals.append(name)
                interim_states[name] = interim["cancellation_state"]
            if name == a_name:
                a_reached_boundary.set()
            # BOTH public calls must reach this boundary before EITHER real claim may
            # run; a timeout here breaks the barrier and fails the test.
            boundary_barrier.wait()
            with coord_lock:
                release_snapshots[name] = list(arrivals)
            if name == b_name:
                # Deterministically make the pending-side caller (A) the sole real
                # claimant; the confirming caller loses and must write nothing.
                loser_saw_winner_done[name] = winner_claim_done.wait(timeout=20)
            result = real_claim(dispatch_id_arg, admin_actor_id, reason, now)
            if name == a_name:
                winner_claim_done.set()
            return result

        results: dict[str, dict] = {}
        errors: dict[str, BaseException] = {}

        def run_pending() -> None:
            try:
                results[a_name] = self.store.request_cancellation(
                    dispatch_id,
                    requesting_actor_id=HUMAN_ID,
                    reason="operator withdrew",
                    authority="admin",
                )
            except BaseException as exc:  # noqa: BLE001 - captured for assertion
                errors[a_name] = exc

        def run_confirming() -> None:
            try:
                results[b_name] = self.store.request_cancellation(
                    dispatch_id,
                    requesting_actor_id=HUMAN_ID,
                    reason="operator withdrew",
                    authority="admin",
                    adapter_for_runtime=lambda _r: adapter,
                )
            except BaseException as exc:  # noqa: BLE001 - captured for assertion
                errors[b_name] = exc

        with mock.patch.object(
            self.store._dispatch,
            "_write_admin_cancellation_notice_semaphore",
            side_effect=counting_publish,
        ), mock.patch.object(
            self.store._dispatch,
            "_claim_admin_cancellation_notice",
            side_effect=coordinating_claim,
        ):
            thread_a = threading.Thread(target=run_pending, name=a_name)
            thread_b = threading.Thread(target=run_confirming, name=b_name)
            thread_a.start()
            # Force the pending caller to record its request and reach the claim
            # boundary BEFORE the confirming caller drives, so the two contenders
            # genuinely arrive with differing drive outcomes for the same request.
            self.assertTrue(
                a_reached_boundary.wait(timeout=20),
                "pending caller never reached the claim boundary",
            )
            thread_b.start()
            thread_a.join(30)
            thread_b.join(30)

        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())
        self.assertEqual(errors, {}, f"a concurrent request raised: {errors}")

        # Both contenders reached the claim boundary before either real claim ran:
        # the barrier (timeout -> BrokenBarrierError -> captured error) forbids a
        # claim from completing before the other arrived, and each release snapshot
        # mechanically proves both names were present at release.
        self.assertEqual(sorted(arrivals), sorted([a_name, b_name]))
        self.assertEqual(sorted(release_snapshots.get(a_name, [])), sorted([a_name, b_name]))
        self.assertEqual(sorted(release_snapshots.get(b_name, [])), sorted([a_name, b_name]))
        self.assertTrue(
            loser_saw_winner_done.get(b_name), "loser ran its claim before the winner committed"
        )

        # Differing drive outcomes at the boundary: the pending caller observed only
        # ``requested``; the confirming caller drove the real authenticated HALT to
        # ``confirmed``.
        self.assertEqual(interim_states.get(a_name), "requested")
        self.assertEqual(interim_states.get(b_name), "confirmed")
        self.assertTrue(adapter.halt_calls, "the confirming caller never ran the authenticated HALT")

        # Exactly one REAL claimant created + bound + published the single notice.
        notices = self._admin_notices()
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        notice_id = notice["id"]
        self.assertEqual(self._admin_notice_inventory(), (1, 1, 1))
        # Exactly one post-commit semaphore publication, emitted by the pending-side
        # caller that actually bound the notice (the loser published nothing).
        self.assertEqual(publications, [(a_name, notice_id)])

        # Both public callers converge on the SAME bound notice id and the SAME final
        # confirmed/cancelled result.
        for name in (a_name, b_name):
            self.assertEqual(results[name]["admin_notice_message_id"], notice_id)
            self.assertEqual(results[name]["status"], "cancelled")
            self.assertEqual(results[name]["cancellation_state"], "confirmed")
            self.assertEqual(results[name]["termination_result"], "supervised_halt_confirmed")

        # The single notice derives its wording from the canonical transactional
        # reread (CONFIRMED), NOT the winning caller's stale ``requested`` interim.
        self.assertEqual(notice["from_agent"], HUMAN_ID)
        self.assertEqual(notice["to_agent"], "arch")
        self.assertEqual(notice["parent_message_id"], started["message_id"])
        self.assertEqual(notice["subject"], f"Admin cancellation: {dispatch_id}")
        self.assertIn("cancellation_state=confirmed", notice["body"])
        self.assertIn("ledger_status=cancelled", notice["body"])
        self.assertIn("termination_confirmed=yes", notice["body"])
        self.assertIn("supervised_halt_confirmed", notice["body"])
        self.assertNotIn("cancellation_state=requested", notice["body"])
        self.assertNotIn("termination_confirmed=no", notice["body"])
        self.assertNotIn("has REQUESTED cancellation", notice["body"])
        self.assertNotIn("not yet confirmed", notice["body"])
        self.assertNotIn("dlq", notice["body"].lower())
        self.assertNotIn("ledger released; termination not confirmed", notice["body"])
        self.assertEqual(
            self._observed(dispatch_id)["cancellation"]["admin_notice_message_id"], notice_id
        )

        # No producer-page claim/message/paged observed fields leaked and no
        # unrelated observed-value mutation: the ONLY change to the observed map is
        # the addition of the single new "cancellation" entry. Prove it by EXACT
        # dictionary equality -- removing only that one entry must reproduce
        # started_observed key-for-key AND value-for-value. This is strictly
        # stronger than a key-set difference, which is blind to the deletion of a
        # pre-existing key and to an in-place edit of a pre-existing value: the
        # proof below fails for deletion of any pre-existing key, modification of
        # any pre-existing value, OR addition of any unrelated key.
        observed_after = self._observed(dispatch_id)
        self.assertIn("cancellation", observed_after)
        observed_after_without_cancellation = {
            k: v for k, v in observed_after.items() if k != "cancellation"
        }
        self.assertEqual(observed_after_without_cancellation, started_observed)
        self.assertFalse(
            [k for k in observed_after if "page" in k or "paged" in k],
            f"unexpected producer-page/paged observed fields: {observed_after}",
        )
        # Exactly one ledger row, terminal cancelled with its auth lineage released.
        with self.store._db.connection() as conn:
            ledger_rows = conn.execute("select count(*) c from dispatch_ledger").fetchone()["c"]
        self.assertEqual(ledger_rows, 1)
        final = self._ledger(dispatch_id)
        self.assertEqual(final["status"], "cancelled")
        self.assertIsNone(final["auth_lineage_claimed_at"])
        self.assertTrue(self._producer_semaphores())

    def test_admin_notice_is_idempotent_on_replay(self) -> None:
        d = self._queued("noticereplay")
        rc1, _first = self._cancel(d["dispatch_id"], "operator withdrew")
        self.assertEqual(rc1, 0)
        first_notices = self._admin_notices()
        self.assertEqual(len(first_notices), 1)
        notice_id = first_notices[0]["id"]
        sem_before = {p: p.read_bytes() for p in self._producer_semaphores()}

        rc2, second = self._cancel(d["dispatch_id"], "operator withdrew")
        self.assertEqual(rc2, 0)
        # No second message / recipient / thread; the same bound notice is returned.
        self.assertEqual([n["id"] for n in self._admin_notices()], [notice_id])
        self.assertEqual(second["admin_notice_message_id"], notice_id)
        # The producer semaphore was not rewritten by the duplicate request.
        sem_after = {p: p.read_bytes() for p in self._producer_semaphores()}
        self.assertEqual(sem_after, sem_before)

    def test_notice_insertion_failure_preserves_request_then_replay_repairs(self) -> None:
        # The notice is claimed AFTER the request commits, so a notice-insert
        # failure preserves the already-durable cancellation request and fails
        # loudly (it never rolls the request back). An identical replay repairs the
        # missing notice EXACTLY ONCE; a further identical replay is mutation-free.
        d = self._queued("noticefail")
        with mock.patch.object(
            self.store._dispatch,
            "_insert_admin_cancellation_notice",
            side_effect=RuntimeError("notice insert boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.request_cancellation(
                    d["dispatch_id"],
                    requesting_actor_id=HUMAN_ID,
                    reason="withdraw",
                    authority="admin",
                )
        # The cancellation REQUEST is durable: the row is cancelled, the admin
        # cancellation object persisted, but no notice was bound and none leaked.
        self.assertEqual(self._ledger(d["dispatch_id"])["status"], "cancelled")
        cancellation = self._observed(d["dispatch_id"])["cancellation"]
        self.assertEqual(cancellation["state"], "confirmed")
        self.assertEqual(cancellation["authority"], "admin")
        self.assertNotIn("admin_notice_message_id", cancellation)
        self.assertEqual(self._admin_notices(), [])

        # An identical replay repairs the missing notice exactly once, describing
        # the CONFIRMED canonical result (never pending).
        rc, payload = self._cancel(d["dispatch_id"], "withdraw")
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "cancelled")
        notices = self._admin_notices()
        self.assertEqual(len(notices), 1)
        repaired_id = notices[0]["id"]
        self.assertEqual(payload["admin_notice_message_id"], repaired_id)
        self.assertIn("cancellation_state=confirmed", notices[0]["body"])
        self.assertIn("ledger_status=cancelled", notices[0]["body"])
        self.assertEqual(self._admin_notice_inventory(), (1, 1, 1))

        # A further identical replay rewrites nothing: same notice id, no second
        # message/recipient/thread, and no producer semaphore rewrite.
        sem_before = {p: p.read_bytes() for p in self._producer_semaphores()}
        rc3, third = self._cancel(d["dispatch_id"], "withdraw")
        self.assertEqual(rc3, 0)
        self.assertEqual([n["id"] for n in self._admin_notices()], [repaired_id])
        self.assertEqual(third["admin_notice_message_id"], repaired_id)
        self.assertEqual(self._admin_notice_inventory(), (1, 1, 1))
        sem_after = {p: p.read_bytes() for p in self._producer_semaphores()}
        self.assertEqual(sem_after, sem_before)

    def test_notice_repair_refuses_mismatched_admin_or_reason_and_leaves_bytes(self) -> None:
        # SAME failed-notice-repair boundary as the test above: a cancelled row
        # whose notice insert failed, so its cancellation object carries NO
        # admin_notice_message_id. The fall-through repair mints/binds the notice
        # from the SUPPLIED admin identity and normalized reason, so a replay from a
        # DIFFERENT admin, or with a DIFFERENT normalized reason, must be REFUSED
        # mutation-free: it creates no notice and leaves the exact ledger row bytes
        # unchanged. Only an EXACT replay (same admin + reason normalizing to the
        # persisted value) repairs the missing notice, exactly once.
        d = self._queued("noticerepairconflict")
        dispatch_id = d["dispatch_id"]
        with mock.patch.object(
            self.store._dispatch,
            "_insert_admin_cancellation_notice",
            side_effect=RuntimeError("notice insert boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.request_cancellation(
                    dispatch_id,
                    requesting_actor_id=HUMAN_ID,
                    reason="withdraw",
                    authority="admin",
                )
        # Boundary reached: cancelled terminal, cancellation object durable and
        # confirmed, but no notice was bound and none leaked.
        self.assertEqual(self._ledger(dispatch_id)["status"], "cancelled")
        cancellation = self._observed(dispatch_id)["cancellation"]
        self.assertEqual(cancellation["state"], "confirmed")
        self.assertEqual(cancellation["requested_by"], HUMAN_ID)
        self.assertEqual(cancellation["reason"], "withdraw")
        self.assertNotIn("admin_notice_message_id", cancellation)
        self.assertEqual(self._admin_notices(), [])
        # Capture the EXACT persisted ledger row image at the unbound-notice boundary.
        ledger_before = dict(self._ledger(dispatch_id))

        # (a) A DIFFERENT admin replaying the same reason is refused mutation-free.
        rc_actor, payload_actor = self._cancel(
            dispatch_id, "withdraw", from_actor_id=OTHER_HUMAN_ID
        )
        self.assertEqual(rc_actor, 2)
        self.assertFalse(payload_actor["ok"])
        self.assertIn("different actor/authority/reason", payload_actor["error"])
        self.assertEqual(self._admin_notices(), [])
        self.assertEqual(self._admin_notice_inventory(), (0, 0, 0))
        self.assertEqual(dict(self._ledger(dispatch_id)), ledger_before)
        self.assertNotIn(
            "admin_notice_message_id", self._observed(dispatch_id)["cancellation"]
        )

        # (b) The SAME admin replaying a DIFFERENT normalized reason is refused too.
        rc_reason, payload_reason = self._cancel(
            dispatch_id, "a different reason", from_actor_id=HUMAN_ID
        )
        self.assertEqual(rc_reason, 2)
        self.assertFalse(payload_reason["ok"])
        self.assertIn("different actor/authority/reason", payload_reason["error"])
        self.assertEqual(self._admin_notices(), [])
        self.assertEqual(self._admin_notice_inventory(), (0, 0, 0))
        self.assertEqual(dict(self._ledger(dispatch_id)), ledger_before)

        # (c) An EXACT replay (same admin; surrounding whitespace normalizes to the
        # persisted reason) repairs the missing notice exactly once.
        rc_ok, payload_ok = self._cancel(
            dispatch_id, "  withdraw  ", from_actor_id=HUMAN_ID
        )
        self.assertEqual(rc_ok, 0)
        self.assertEqual(payload_ok["status"], "cancelled")
        notices = self._admin_notices()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["from_agent"], HUMAN_ID)
        self.assertEqual(payload_ok["admin_notice_message_id"], notices[0]["id"])
        self.assertIn("cancellation_state=confirmed", notices[0]["body"])
        self.assertEqual(self._admin_notice_inventory(), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
