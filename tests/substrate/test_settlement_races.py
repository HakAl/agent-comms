"""Stage-2 T7: genuine barrier-controlled races for admin ``settle-dispatch``.

Every cross-family race here runs TWO real threads through the SAME PRODUCTION
entry points a live deployment uses -- never a private simulation or a single
sequential caller:

* ``request_cancellation`` (the public admin-cancel drive) vs settlement;
* the public monitor pass ``reconcile_dispatches`` (hard-TTL backstop) vs
  settlement;
* the public recipient ``close_message`` vs settlement;
* two concurrent ``settle_dispatch_execute`` of the same plan.

Both competitors are gated at the INTERNAL production transaction/CAS seam their
public entry point reaches AFTER its full read-side -- ``_commit_settlement`` for
settlement, ``_apply_cancellation_cas`` / ``_apply_ttl_termination_cas`` for the
monitor/cancel drive, and ``close_message``'s own ``begin immediate`` for the
recipient close. A ``threading.Barrier`` releases only once BOTH outer threads
are already executing their real public call stacks poised at that contended
write, so the barrier proves concurrency at the production boundary rather than
before either public operation is invoked.

A single ``winner_committed`` event then orders the two commits so BOTH
first-committer outcomes are covered deterministically. Immediately after the
winner commits -- and BEFORE the loser's contended write is released -- the
complete logical state is snapshotted; the winner is then frozen so its own
downstream monitor housekeeping cannot race the comparison. The loser's contended
production write runs against that committed state and the complete state is
snapshotted again the instant it returns. The two snapshots must be byte-equal:
the losing operation re-read the committed winner under its own ``BEGIN
IMMEDIATE`` and added no row/message/page, rewrote no value, and could not
overwrite the winner's status, recipient transport, settlement/cancellation
audit, termination evidence, lineage/cap release, timestamps, or the single
producer notice.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_comms.cli import _helpers
from agent_comms.dispatch_ledger import (
    CancellationStateError,
    SETTLEMENT_FAILURE_REASON,
    SETTLEMENT_KEY,
    SETTLEMENT_TERMINATION_RESULT,
)
from agent_comms.schema import ValidationError
from agent_comms.store import Store

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"
ARCH_ID = "arch"
WORKER_ID = "wrk"
VALID_TOKEN = "operator-secret"
CLAIMED_AT = "2026-07-18T04:00:00+00:00"
CLOSE_BY = "2026-07-18T12:00:00+00:00"
ISSUED = "2026-07-18T11:59:30+00:00"
SETTLE_NOW = "2026-07-18T12:00:00+00:00"
TTL_FROZEN_NOW = "2026-07-18T12:01:00+00:00"
# A strictly later frozen clock for a SECOND public monitor pass, so the bounded
# same-token dlq residue re-probe telemetry advances deterministically.
MONITOR_NOW_2 = "2026-07-18T12:02:00+00:00"
TTL_FAILURE = "timeout; ledger released; termination not confirmed"
# A phrase unique to the operator-settlement producer notice, so counting it is
# immune to the monitor pass's own generic DLQ producer pages / escalations.
SETTLEMENT_NOTICE_PHRASE = "An operator emergency-settlement has released this dispatch's ledger"


class _RaisingAdapter:
    def halt(self, spawn_handle, observed):  # noqa: ANN001 - test double
        raise RuntimeError("supervisor unreachable")


class _ConfirmingAdapter:
    def halt(self, spawn_handle, observed):  # noqa: ANN001 - test double
        return None


def _raising_for(_runtime):  # noqa: ANN001 - test double
    return _RaisingAdapter()


def _confirming_for(_runtime):  # noqa: ANN001 - test double
    return _ConfirmingAdapter()


class SettlementRaceBase(unittest.TestCase):
    # A subclass may nest the ledger under this directory so the ENTIRE ledger
    # directory (parent included) can be removed to prove non-recreation.
    LEDGER_SUBDIR: str | None = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        if self.LEDGER_SUBDIR:
            self.ledger_dir = self.tmp / self.LEDGER_SUBDIR
        else:
            self.ledger_dir = self.tmp
        self.db_path = self.ledger_dir / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        self.store.register_actor(HUMAN_ID, "human", "alice")
        self.store.register_agent_actor(ARCH_ID, "alpha", "architect", str(self.tmp / "arch"), [])
        self.store.register_agent_actor(
            WORKER_ID,
            "alpha",
            "worker",
            str(self.tmp / "wrk"),
            [],
            runtime="stub",
            spawn={"command": "stub"},
            owner=ARCH_ID,
        )
        # The admin token lives OUTSIDE the ledger directory so removing the ledger
        # never disturbs the credential.
        self.token_path = self.tmp / "admin-token"
        self.token_path.write_text(VALID_TOKEN)
        os.chmod(self.token_path, 0o600)
        patcher = mock.patch.object(_helpers, "ADMIN_TOKEN_PATH", self.token_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ["AGENT_COMMS_ADMIN_TOKEN"] = VALID_TOKEN
        self.addCleanup(lambda: os.environ.pop("AGENT_COMMS_ADMIN_TOKEN", None))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- state helpers -------------------------------------------------- #
    def _pending_in_flight(self, key: str, *, run_token: str | None = None, close_by: str | None = None) -> str:
        d = self.store.dispatch_agent(ARCH_ID, WORKER_ID, key, f"S {key}", f"B {key}", [])
        did = d["dispatch_id"]
        token = run_token or f"rt-{key}"
        observed = {"run_token": token, "control_socket": f"sock-{key}"}
        with self.store._db.connection() as conn:
            conn.execute(
                """
                update dispatch_ledger
                set status = 'in_flight',
                    spawn_handle = ?,
                    expected_close_by = ?,
                    auth_lineage_claimed_at = ?,
                    observed_values_json = ?
                where dispatch_id = ?
                """,
                (f"sup:{WORKER_ID}:{did}", close_by, CLAIMED_AT, json.dumps(observed, sort_keys=True), did),
            )
        result = self.store.request_cancellation(
            did, requesting_actor_id=HUMAN_ID, reason=f"cancel {key}", authority="admin"
        )
        self.assertEqual(result["cancellation_state"], "requested")
        return did

    def _preview(self, did: str, *, issued: str = ISSUED, nonce: str = "n" * 32) -> dict:
        return self.store.settle_dispatch_preview(
            did,
            actor_id=HUMAN_ID,
            reason=f"settle {did}",
            secret=VALID_TOKEN,
            issued_at=issued,
            nonce=nonce,
        )

    def _execute(self, did: str, plan: str, *, now: str = SETTLE_NOW):
        return self.store.settle_dispatch_execute(
            did, actor_id=HUMAN_ID, plan=plan, secret=VALID_TOKEN, release_ack=True, now=now
        )

    def _ledger(self, did: str):
        with self.store._db.connection() as conn:
            return conn.execute("select * from dispatch_ledger where dispatch_id = ?", (did,)).fetchone()

    def _observed(self, did: str) -> dict:
        return json.loads(self._ledger(did)["observed_values_json"] or "{}")

    def _transport(self, did: str) -> str | None:
        row = self._ledger(did)
        with self.store._db.connection() as conn:
            r = conn.execute(
                "select status from message_recipients where message_id = ? and to_agent = ?",
                (row["message_id"], row["recipient_actor_id"]),
            ).fetchone()
        return None if r is None else r["status"]

    def _settlement_notices(self):
        """Producer notices that are unambiguously the operator-settlement blocker."""
        with self.store._db.connection() as conn:
            rows = conn.execute(
                """
                select m.id, m.body, m.subject, m.priority, m.from_agent
                from messages m
                join message_recipients r on r.message_id = m.id
                where r.to_agent = ?
                order by m.created_at, m.id
                """,
                (ARCH_ID,),
            ).fetchall()
        return [r for r in rows if SETTLEMENT_NOTICE_PHRASE in r["body"]]

    def _worker_reply(self, did: str) -> str:
        """The recipient replies to its dispatch thread (a close precondition).

        Returns the reply message id so a v2 ``close_dispatch`` can bind it.
        """
        mid = self._ledger(did)["message_id"]
        reply = self.store.send_message(
            from_agent=WORKER_ID,
            to_agents=[ARCH_ID],
            subject="re",
            body="worker reply before close",
            refs=[],
            priority="normal",
            requires_ack=False,
            parent_message_id=mid,
        )
        return reply["id"]

    # --- complete-state capture ----------------------------------------- #
    def _capture_full_state(self) -> dict:
        """A complete, deterministic snapshot of every mutable ledger/transport row.

        Captures the full dispatch_ledger rows (all columns PLUS the decoded
        observed_values audit / snapshot / evidence), the full message_recipients
        inventory (recipient state and every timestamp), the full messages
        inventory (raw bodies / subjects / metadata and ordering-identifying
        id / created_at), the thread edges, statuses, AND every on-disk producer /
        recipient wake semaphore (its raw bytes) -- so an exact-equality comparison
        proves a losing operation added no row/message/page, rewrote no value or
        semaphore, and could not overwrite any winning value.
        """
        with self.store._db.connection() as conn:
            def rows(sql: str) -> list[dict]:
                return [dict(r) for r in conn.execute(sql).fetchall()]

            ledger = rows("select * from dispatch_ledger order by dispatch_id")
            for r in ledger:
                r["observed_decoded"] = json.loads(r.get("observed_values_json") or "{}")
            recipients = rows("select * from message_recipients order by message_id, to_agent")
            messages = rows("select * from messages order by created_at, id")
            threads = rows("select * from message_threads order by message_id")
            statuses = rows("select * from statuses order by id, created_at")
        return {
            "ledger": ledger,
            "recipients": recipients,
            "messages": messages,
            "threads": threads,
            "statuses": statuses,
            "semaphores": self._capture_semaphores(),
        }

    def _capture_semaphores(self) -> dict[str, bytes]:
        """Raw bytes of every ``new_messages`` wake semaphore, keyed by path.

        A second generic producer page would (last-write-wins) rewrite the
        producer's semaphore with a different message; capturing the exact bytes
        lets an equality comparison prove no such rewrite occurred. Temp files
        (``.new_messages.<pid>.<uuid>.tmp``) are skipped; the atomic rename means
        only the settled ``new_messages`` file is ever read.
        """
        return {
            str(path.relative_to(self.tmp)): path.read_bytes()
            for path in sorted(self.tmp.rglob("new_messages"))
            if path.is_file()
        }

    def _assert_loser_added_nothing(self, caps: dict) -> None:
        self.assertIn("winner", caps, "winner state was never captured")
        self.assertIn("loser", caps, "loser state was never captured")
        self.assertEqual(
            caps["loser"],
            caps["winner"],
            "the losing operation mutated the committed winner state",
        )

    # --- the barrier-controlled race harness ---------------------------- #
    @staticmethod
    def _match_first_arg(value):
        def match(args, kwargs):
            return (args[0] if args else kwargs.get("dispatch_id")) == value

        return match

    @staticmethod
    def _match_message(mid):
        def match(args, kwargs):
            return (args[1] if len(args) > 1 else kwargs.get("message_id")) == mid

        return match

    def _race(self, did: str, *, settlement_thunk, competitor_thunk, competitor_seam, settlement_wins: bool):
        """Run settlement and one competing production entry point on two threads.

        ``competitor_seam`` is ``(target, method_name, match)`` identifying the
        competitor's INTERNAL production transaction/CAS seam to gate. Settlement is
        always gated at ``_commit_settlement``. Both gates share one barrier (proving
        both outer threads are inside their real public call stacks at the contended
        write) and one ``winner_committed`` ordering. The winner runs its contended
        write, the complete state is captured, then the winner is frozen while the
        loser runs ITS contended write against the committed winner; the complete
        state is captured again the instant the loser's write returns.

        Returns ``(settlement_outcome, competitor_outcome, caps)`` where each outcome
        is ``{"result": ...}`` or ``{"error": exc}`` and ``caps`` carries the
        ``winner`` / ``loser`` complete-state snapshots.
        """
        barrier = threading.Barrier(2, timeout=25)
        winner_committed = threading.Event()
        resume_winner = threading.Event()
        caps: dict[str, dict] = {}
        box: dict[str, dict] = {}
        guard = threading.Lock()

        def gate(real, match, is_winner):
            fired = {"done": False}

            def wrapped(*args, **kwargs):
                with guard:
                    take = not fired["done"] and match(args, kwargs)
                    if take:
                        fired["done"] = True
                if not take:
                    return real(*args, **kwargs)
                barrier.wait()
                if is_winner:
                    result = real(*args, **kwargs)
                    caps["winner"] = self._capture_full_state()
                    winner_committed.set()
                    resume_winner.wait(25)
                    return result
                if not winner_committed.wait(25):
                    raise AssertionError("winner never committed before the loser ran")
                try:
                    return real(*args, **kwargs)
                finally:
                    caps["loser"] = self._capture_full_state()
                    resume_winner.set()

            return wrapped

        settlement_real = self.store._dispatch._commit_settlement
        settlement_gate = gate(settlement_real, self._match_first_arg(did), settlement_wins)

        comp_target, comp_method, comp_match = competitor_seam
        comp_real = getattr(comp_target, comp_method)
        comp_gate = gate(comp_real, comp_match, not settlement_wins)

        def run(tag, fn):
            try:
                box[tag] = {"result": fn()}
            except BaseException as exc:  # noqa: BLE001 - captured for assertion
                box[tag] = {"error": exc}

        with mock.patch.object(
            self.store._dispatch, "_commit_settlement", side_effect=settlement_gate
        ), mock.patch.object(comp_target, comp_method, side_effect=comp_gate):
            threads = [
                threading.Thread(target=run, args=("settlement", settlement_thunk)),
                threading.Thread(target=run, args=("competitor", competitor_thunk)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(40)

        return box.get("settlement", {}), box.get("competitor", {}), caps

    def _assert_settlement_winner(self, did: str, settlement_outcome: dict) -> None:
        """The settlement committed the exceptional operator terminal, exactly once."""
        result = settlement_outcome.get("result")
        self.assertIsNotNone(result, f"settlement did not win: {settlement_outcome}")
        self.assertEqual(result["status"], "dlq")
        self.assertEqual(result["failure_reason"], SETTLEMENT_FAILURE_REASON)
        row = self._ledger(did)
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(row["failure_reason"], SETTLEMENT_FAILURE_REASON)
        self.assertEqual(row["auth_lineage_claimed_at"], CLAIMED_AT)
        observed = self._observed(did)
        self.assertEqual(observed["termination_result"], SETTLEMENT_TERMINATION_RESULT)
        # The cancellation is NEVER marked confirmed by a settlement.
        self.assertEqual(observed["cancellation"]["state"], "requested")
        # The durable settlement audit is intact and binds this exact plan/notice.
        audit = observed[SETTLEMENT_KEY]
        self.assertEqual(audit["plan_fingerprint"], result["plan_fingerprint"])
        self.assertEqual(audit["producer_notice_message_id"], result["producer_notice_message_id"])
        self.assertEqual(self._transport(did), "cancelled")
        notices = self._settlement_notices()
        self.assertEqual([n["id"] for n in notices], [result["producer_notice_message_id"]])

    # --- full post-settlement monitor-delta helpers --------------------- #
    def _message_created_at(self, message_id: str) -> str:
        with self.store._db.connection() as conn:
            return conn.execute(
                "select created_at from messages where id = ?", (message_id,)
            ).fetchone()["created_at"]

    def _assert_only_residue_delta(self, before: dict, after: dict, *, attempts: int) -> dict:
        """Prove two complete-state snapshots differ SOLELY by the settled row's
        bounded ``dlq_residue_reprobe`` telemetry and return that telemetry.

        Every non-ledger inventory (messages, recipients, threads, statuses,
        semaphores) must be byte-identical, the single ledger row's every column
        except its observed JSON must be unchanged, and stripping the residue key
        from both observed dicts must leave them exactly equal -- so the residue
        telemetry is the one and only difference the monitor pass introduced.
        """
        for key in ("messages", "recipients", "threads", "statuses", "semaphores"):
            self.assertEqual(after[key], before[key], f"{key} inventory changed unexpectedly")
        self.assertEqual(len(after["ledger"]), len(before["ledger"]))
        self.assertEqual(len(after["ledger"]), 1)
        b, a = before["ledger"][0], after["ledger"][0]
        for col in b:
            if col in ("observed_values_json", "observed_decoded"):
                continue
            self.assertEqual(a[col], b[col], f"ledger column {col} changed unexpectedly")
        before_obs = dict(b["observed_decoded"])
        after_obs = dict(a["observed_decoded"])
        reprobe = after_obs.get("dlq_residue_reprobe")
        self.assertIsInstance(reprobe, dict)
        self.assertEqual(reprobe.get("attempts"), attempts)
        before_obs.pop("dlq_residue_reprobe", None)
        after_obs.pop("dlq_residue_reprobe", None)
        self.assertEqual(after_obs, before_obs)
        return reprobe

    def _assert_producer_semaphore_only(self, message_id: str, delivered_at: str) -> None:
        """The producer's wake semaphore references EXACTLY the one settlement
        notice -- proof no second generic page rewrote it (last-write-wins)."""
        matches = [p for p in sorted((self.tmp / "arch").rglob("new_messages")) if p.is_file()]
        self.assertEqual(len(matches), 1, f"expected exactly one producer semaphore: {matches}")
        payload = json.loads(matches[0].read_text())
        self.assertEqual(
            payload["messages"],
            [{"message_id": message_id, "delivered_at": delivered_at}],
            "the producer semaphore was rewritten away from the atomic settlement notice",
        )


class RequestCancellationVsSettlementTest(SettlementRaceBase):
    """request_cancellation (public admin drive) vs settlement, both orders.

    The competitor is gated at ``_apply_cancellation_cas`` -- the terminal CAS the
    public ``request_cancellation`` drive reaches after its own read-side and its
    out-of-transaction authenticated HALT -- so both threads are inside their real
    public call stacks at the contended write.
    """

    def _cancel_thunk(self, did, key):
        def thunk():
            return self.store.request_cancellation(
                did,
                requesting_actor_id=HUMAN_ID,
                reason=f"cancel {key}",
                authority="admin",
                adapter_for_runtime=_confirming_for,
            )

        return thunk

    def test_cancellation_confirms_first_settlement_refuses(self) -> None:
        did = self._pending_in_flight("canfirst")
        plan = self._preview(did)["plan"]

        settlement_outcome, competitor_outcome, caps = self._race(
            did,
            settlement_thunk=lambda: self._execute(did, plan),
            competitor_thunk=self._cancel_thunk(did, "canfirst"),
            competitor_seam=(self.store._dispatch, "_apply_cancellation_cas", self._match_first_arg(did)),
            settlement_wins=False,
        )

        # The cancellation is the first committer: the row is the confirmed cancel.
        self.assertEqual(competitor_outcome.get("result", {}).get("status"), "cancelled")
        self.assertIsInstance(settlement_outcome.get("error"), CancellationStateError)
        row = self._ledger(did)
        self.assertEqual(row["status"], "cancelled")
        observed = self._observed(did)
        self.assertEqual(observed["cancellation"]["state"], "confirmed")
        # The losing settlement wrote NOTHING: no audit, no notice, transport cancelled
        # by the cancel (never relabelled to the operator terminal).
        self.assertNotIn(SETTLEMENT_KEY, observed)
        self.assertEqual(row["failure_reason"], None)
        self.assertEqual(self._transport(did), "cancelled")
        self.assertEqual(self._settlement_notices(), [])
        self._assert_loser_added_nothing(caps)

    def test_settlement_commits_first_cancellation_refuses(self) -> None:
        did = self._pending_in_flight("setfirst")
        plan = self._preview(did)["plan"]

        settlement_outcome, competitor_outcome, caps = self._race(
            did,
            settlement_thunk=lambda: self._execute(did, plan),
            competitor_thunk=self._cancel_thunk(did, "setfirst"),
            competitor_seam=(self.store._dispatch, "_apply_cancellation_cas", self._match_first_arg(did)),
            settlement_wins=True,
        )

        self._assert_settlement_winner(did, settlement_outcome)
        # The already-in-flight admin cancellation lost the terminal CAS: it re-read
        # the operator-settled dlq under its own lock and no-opped, never relabelling
        # the winner to ``cancelled`` and never confirming the cancellation.
        self.assertIn("result", competitor_outcome)
        self.assertEqual(self._ledger(did)["status"], "dlq")
        self.assertEqual(self._observed(did)["cancellation"]["state"], "requested")
        self._assert_loser_added_nothing(caps)


class MonitorHardTtlVsSettlementTest(SettlementRaceBase):
    """The public monitor pass (hard-TTL backstop) vs settlement, both orders.

    Each order gates the monitor pass at the production CAS that actually contends
    for the row in that order. When the hard-TTL backstop wins, the barrier is at
    its terminal write ``_apply_ttl_termination_cas``. When settlement wins, it must
    commit before the monitor pass touches the row at all, so the barrier is at the
    monitor pass's FIRST contended write to the row -- the pending-cancellation
    drive's terminal CAS ``_apply_cancellation_cas`` -- after which the row is
    already terminal and the hard-TTL selection never picks it up.
    """

    def test_hard_ttl_releases_first_settlement_refuses(self) -> None:
        did = self._pending_in_flight("ttlfirst", close_by=CLOSE_BY)
        plan = self._preview(did)["plan"]
        # Freeze the monitor's clock past kill-grace so the pass's hard-TTL backstop
        # deterministically releases the unconfirmed in_flight row to dlq.
        with mock.patch("agent_comms.dispatch_ledger.utc_now", return_value=TTL_FROZEN_NOW):
            settlement_outcome, competitor_outcome, caps = self._race(
                did,
                settlement_thunk=lambda: self._execute(did, plan),
                competitor_thunk=lambda: self.store.reconcile_dispatches(_raising_for),
                competitor_seam=(
                    self.store._dispatch,
                    "_apply_ttl_termination_cas",
                    self._match_first_arg(did),
                ),
                settlement_wins=False,
            )

        # The monitor pass is the first committer: hard-TTL dlq with the truthful
        # ``termination not confirmed`` residue (NOT the operator-settled marker).
        self.assertIn("result", competitor_outcome)
        self.assertIsInstance(settlement_outcome.get("error"), CancellationStateError)
        row = self._ledger(did)
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(row["failure_reason"], TTL_FAILURE)
        observed = self._observed(did)
        self.assertNotIn(SETTLEMENT_KEY, observed)
        self.assertEqual(observed["cancellation"]["state"], "requested")
        self.assertEqual(self._settlement_notices(), [])
        # The losing settlement re-read the hard-TTL terminal and added nothing.
        self._assert_loser_added_nothing(caps)

    def test_settlement_commits_first_hard_ttl_cannot_overwrite(self) -> None:
        did = self._pending_in_flight("setttl", close_by=CLOSE_BY)
        plan = self._preview(did)["plan"]
        with mock.patch("agent_comms.dispatch_ledger.utc_now", return_value=TTL_FROZEN_NOW):
            settlement_outcome, competitor_outcome, caps = self._race(
                did,
                settlement_thunk=lambda: self._execute(did, plan),
                competitor_thunk=lambda: self.store.reconcile_dispatches(_raising_for),
                competitor_seam=(
                    self.store._dispatch,
                    "_apply_cancellation_cas",
                    self._match_first_arg(did),
                ),
                settlement_wins=True,
            )

        # The settlement is the first committer; the monitor pass's first contended
        # write to the row (the pending-cancellation drive CAS) then re-reads the
        # operator-settled dlq and no-ops, and the hard-TTL selection never picks up
        # the now-terminal row, so the pass never re-terminalizes it.
        self._assert_settlement_winner(did, settlement_outcome)
        self.assertIn("result", competitor_outcome)
        self.assertEqual(self._ledger(did)["failure_reason"], SETTLEMENT_FAILURE_REASON)
        # The loser's contended _apply_cancellation_cas boundary added nothing.
        self._assert_loser_added_nothing(caps)

        # --- Evaluate the COMPLETE public reconcile_dispatches() pass, not merely
        # the losing _apply_cancellation_cas boundary. ``caps["winner"]`` is the
        # complete state immediately after settlement committed and before the
        # losing monitor was allowed to finish; the entire monitor call has now
        # returned, so re-capture the final complete state and prove the exact
        # delta is SOLELY the bounded same-token dlq residue re-probe telemetry.
        post_settlement = caps["winner"]
        final = self._capture_full_state()

        actions = competitor_outcome["result"]
        self.assertIsInstance(actions, list)
        # No second generic producer blocker anywhere in the pass.
        self.assertFalse(
            any(a.get("status") == "producer_paged" for a in actions),
            f"the monitor pass issued a second generic producer page: {actions}",
        )
        # The pass's only effect on the settled row is the unconfirmed same-token
        # re-probe (the raising adapter cannot confirm termination).
        self.assertTrue(
            any(
                a.get("dispatch_id") == did
                and a.get("status") == "dlq_residue_reprobe_unconfirmed"
                for a in actions
            ),
            f"expected a bounded dlq residue re-probe action for {did}: {actions}",
        )

        # No second message, recipient, or thread; still exactly one notice.
        self.assertEqual(final["messages"], post_settlement["messages"])
        self.assertEqual(final["recipients"], post_settlement["recipients"])
        self.assertEqual(final["threads"], post_settlement["threads"])
        self.assertEqual(len(self._settlement_notices()), 1)
        # The exact ledger delta is solely the added dlq_residue_reprobe telemetry
        # (this also proves no producer semaphore rewrite via the equal inventories).
        reprobe = self._assert_only_residue_delta(post_settlement, final, attempts=1)
        self.assertEqual(reprobe["first_at"], TTL_FROZEN_NOW)
        self.assertEqual(reprobe["latest_at"], TTL_FROZEN_NOW)

        # The once-only producer-page state stays bound to the ORIGINAL atomic
        # settlement notice (its message id and its timestamp), never a second page.
        notice_id = settlement_outcome["result"]["producer_notice_message_id"]
        notice_created = self._message_created_at(notice_id)
        observed = self._observed(did)
        self.assertEqual(observed["producer_page_message_id"], notice_id)
        self.assertEqual(observed["producer_page_claimed_at"], notice_created)
        self.assertEqual(observed["producer_paged_at"], notice_created)
        self._assert_producer_semaphore_only(notice_id, notice_created)

        # Exact plan replay is the idempotent winner and rewrites NOTHING.
        before_replay = self._capture_full_state()
        replay = self._execute(did, plan)
        self.assertEqual(replay["status"], "dlq")
        self.assertEqual(replay["producer_notice_message_id"], notice_id)
        self.assertEqual(self._capture_full_state(), before_replay)

        # Another public monitor pass stays duplicate-free and advances only the
        # bounded NEXT residue telemetry (attempts 1 -> 2), touching nothing else.
        with mock.patch("agent_comms.dispatch_ledger.utc_now", return_value=MONITOR_NOW_2):
            actions2 = self.store.reconcile_dispatches(_raising_for)
        self.assertFalse(any(a.get("status") == "producer_paged" for a in actions2))
        self.assertEqual(len(self._settlement_notices()), 1)
        after_second_pass = self._capture_full_state()
        reprobe2 = self._assert_only_residue_delta(final, after_second_pass, attempts=2)
        self.assertEqual(reprobe2["first_at"], TTL_FROZEN_NOW)
        self.assertEqual(reprobe2["latest_at"], MONITOR_NOW_2)
        # The producer-page binding is still the original notice after the replay
        # and the second monitor pass.
        observed2 = self._observed(did)
        self.assertEqual(observed2["producer_page_message_id"], notice_id)
        self.assertEqual(observed2["producer_page_claimed_at"], notice_created)
        self.assertEqual(observed2["producer_paged_at"], notice_created)
        self._assert_producer_semaphore_only(notice_id, notice_created)


class RecipientCloseVsSettlementTest(SettlementRaceBase):
    """The public recipient closeout vs settlement, both orders.

    A v2 recipient terminalizes only through ``close_dispatch`` (the reply-bound
    checked result), so the recipient-commits-first order races that public
    method; gating it places the barrier while the outer thread is inside the
    real public call at its contended write. The settlement-first order keeps
    the legacy ``close_message`` competitor, whose late close of a settled v2
    trigger refuses without mutation.
    """

    def test_recipient_close_commits_first_settlement_refuses(self) -> None:
        did = self._pending_in_flight("closefirst")
        plan = self._preview(did)["plan"]
        reply_id = self._worker_reply(did)  # the closeout binds this reply
        mid = self._ledger(did)["message_id"]

        settlement_outcome, competitor_outcome, caps = self._race(
            did,
            settlement_thunk=lambda: self._execute(did, plan),
            competitor_thunk=lambda: self.store.close_dispatch(
                WORKER_ID,
                message_id=mid,
                result="satisfied",
                reply_message_id=reply_id,
                summary="closing",
            ),
            competitor_seam=(self.store._mailbox, "close_dispatch", self._match_message(mid)),
            settlement_wins=False,
        )

        # The recipient closeout is the first committer: in_flight -> closed
        # with the v2 checked result recorded on the row.
        self.assertEqual(competitor_outcome.get("result", {}).get("status"), "closed")
        self.assertEqual(competitor_outcome.get("result", {}).get("result"), "satisfied")
        self.assertIsInstance(settlement_outcome.get("error"), CancellationStateError)
        row = self._ledger(did)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["result"], "satisfied")
        # The losing settlement wrote nothing: no audit, no notice, and the closed
        # transport copy is never relabelled to ``cancelled``.
        self.assertNotIn(SETTLEMENT_KEY, self._observed(did))
        self.assertEqual(self._transport(did), "closed")
        self.assertEqual(self._settlement_notices(), [])
        self._assert_loser_added_nothing(caps)

    def test_settlement_commits_first_recipient_close_refuses(self) -> None:
        did = self._pending_in_flight("setclose")
        plan = self._preview(did)["plan"]
        self._worker_reply(did)
        mid = self._ledger(did)["message_id"]

        settlement_outcome, competitor_outcome, caps = self._race(
            did,
            settlement_thunk=lambda: self._execute(did, plan),
            competitor_thunk=lambda: self.store.close_message(WORKER_ID, mid, "closing"),
            competitor_seam=(self.store._mailbox, "close_message", self._match_message(mid)),
            settlement_wins=True,
        )

        # The settlement is the first committer; the late recipient close of the
        # now-``cancelled`` copy refuses and never overwrites the operator terminal.
        self._assert_settlement_winner(did, settlement_outcome)
        self.assertIn("error", competitor_outcome)
        row = self._ledger(did)
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(self._transport(did), "cancelled")
        self._assert_loser_added_nothing(caps)


class ConcurrentSettlementVsSettlementTest(SettlementRaceBase):
    """Two concurrent executions of the SAME plan: one winner, one notice."""

    def test_two_concurrent_executions_yield_one_winner_and_one_notice(self) -> None:
        did = self._pending_in_flight("concurrent")
        plan = self._preview(did)["plan"]
        now = "2026-07-18T12:00:15+00:00"

        # A barrier forces BOTH executions to pass the pre-commit replay check (each
        # sees no committed settlement yet) before EITHER enters its BEGIN IMMEDIATE,
        # so both truly race into the terminal transaction through the public entry
        # point. A commit barrier + winner_committed event then orders the two commits
        # deterministically: the first committer writes the single terminal + notice,
        # is frozen while the complete state is captured, and the second re-reads the
        # committed fingerprint under the lock and returns the same stored winner with
        # no second mutation/notice.
        replay_barrier = threading.Barrier(2, timeout=15)
        real_replay = self.store._dispatch._settlement_winner_if_replayed

        def synced_replay(dispatch_id, fingerprint):
            result = real_replay(dispatch_id, fingerprint)
            if result is None:
                replay_barrier.wait()
            return result

        commit_barrier = threading.Barrier(2, timeout=15)
        winner_committed = threading.Event()
        resume_winner = threading.Event()
        caps: dict[str, dict] = {}
        guard = threading.Lock()
        order = {"n": 0}
        real_commit = self.store._dispatch._commit_settlement

        def gated_commit(*args, **kwargs):
            with guard:
                idx = order["n"]
                order["n"] += 1
            commit_barrier.wait()
            if idx == 0:
                result = real_commit(*args, **kwargs)
                caps["winner"] = self._capture_full_state()
                winner_committed.set()
                resume_winner.wait(15)
                return result
            if not winner_committed.wait(15):
                raise AssertionError("first committer never signalled")
            try:
                return real_commit(*args, **kwargs)
            finally:
                caps["loser"] = self._capture_full_state()
                resume_winner.set()

        results: dict[str, dict] = {}
        errors: dict[str, BaseException] = {}

        def run(tag: str) -> None:
            try:
                results[tag] = self.store.settle_dispatch_execute(
                    did, actor_id=HUMAN_ID, plan=plan, secret=VALID_TOKEN,
                    release_ack=True, now=now,
                )
            except BaseException as exc:  # noqa: BLE001 - captured for assertion
                errors[tag] = exc

        with mock.patch.object(
            self.store._dispatch, "_settlement_winner_if_replayed", side_effect=synced_replay
        ), mock.patch.object(
            self.store._dispatch, "_commit_settlement", side_effect=gated_commit
        ):
            threads = [threading.Thread(target=run, args=(tag,)) for tag in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)

        self.assertEqual(errors, {}, f"unexpected settlement errors: {errors}")
        self.assertEqual(results["a"]["status"], "dlq")
        self.assertEqual(results["b"]["status"], "dlq")
        self.assertEqual(results["a"]["plan_fingerprint"], results["b"]["plan_fingerprint"])
        # Both observers see the identical single winner + single notice identity.
        self.assertEqual(
            results["a"]["producer_notice_message_id"],
            results["b"]["producer_notice_message_id"],
        )
        row = self._ledger(did)
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(row["failure_reason"], SETTLEMENT_FAILURE_REASON)
        self.assertEqual(len(self._settlement_notices()), 1)
        # The second committer re-read the committed winner and added nothing.
        self._assert_loser_added_nothing(caps)

    def test_public_replay_is_idempotent_winner(self) -> None:
        did = self._pending_in_flight("pubreplay")
        plan = self._preview(did)["plan"]
        first = self._execute(did, plan, now="2026-07-18T12:00:15+00:00")
        after_first = self._capture_full_state()
        second = self._execute(did, plan, now="2026-07-18T12:00:20+00:00")
        after_second = self._capture_full_state()
        self.assertEqual(first["status"], "dlq")
        self.assertEqual(second["status"], "dlq")
        self.assertEqual(second["plan_fingerprint"], first["plan_fingerprint"])
        self.assertEqual(len(self._settlement_notices()), 1)
        # The idempotent replay re-read the committed winner and rewrote nothing:
        # the complete ledger/transport/message inventory is byte-identical.
        self.assertEqual(after_second, after_first)


class SettlementExecutionLedgerDeletionTest(SettlementRaceBase):
    """Removing the ledger BETWEEN settlement-execution stages refuses loudly and
    never recreates the database or its parent.

    Settlement execution opens the existing ledger mode=rw at four distinct reopen
    stages -- actor authorization, the successful-replay lookup, the terminal
    transaction, and the post-commit result lookup. A ledger removed before ANY of
    those reopens must raise (never fall back to a create-capable connection) and
    leave the ledger directory and every sidecar absent. This is the end-to-end
    analogue of the DB-seam reopen regression, exercised through the real public
    ``settle_dispatch_execute``.
    """

    LEDGER_SUBDIR = "ledger"

    def _remove_ledger_tree(self) -> None:
        shutil.rmtree(self.ledger_dir)

    def _assert_ledger_tree_absent(self) -> None:
        self.assertFalse(self.ledger_dir.exists())
        self.assertFalse(self.db_path.exists())
        self.assertFalse(self.db_path.with_name(self.db_path.name + "-wal").exists())
        self.assertFalse(self.db_path.with_name(self.db_path.name + "-shm").exists())

    def test_deletion_before_actor_authorization_refuses_without_recreation(self) -> None:
        did = self._pending_in_flight("delauth")
        plan = self._preview(did)["plan"]  # a prior mode=ro open succeeds
        self._remove_ledger_tree()
        # The actor-authorization reopen (the FIRST execution DB touch) refuses.
        with self.assertRaises(ValidationError):
            self._execute(did, plan)
        self._assert_ledger_tree_absent()

    def test_deletion_before_replay_lookup_refuses_without_recreation(self) -> None:
        did = self._pending_in_flight("delreplay")
        plan = self._preview(did)["plan"]
        original = self.store._dispatch._settlement_winner_if_replayed

        def deleting(dispatch_id, fingerprint):
            # Actor authorization has already reopened successfully; remove the
            # ledger before the replay-lookup reopen.
            self._remove_ledger_tree()
            return original(dispatch_id, fingerprint)

        with mock.patch.object(
            self.store._dispatch, "_settlement_winner_if_replayed", side_effect=deleting
        ):
            with self.assertRaises(ValidationError):
                self._execute(did, plan)
        self._assert_ledger_tree_absent()

    def test_deletion_before_terminal_transaction_refuses_without_recreation(self) -> None:
        did = self._pending_in_flight("delcommit")
        plan = self._preview(did)["plan"]
        original = self.store._dispatch._commit_settlement

        def deleting(*args, **kwargs):
            # Actor authorization and the replay lookup have already reopened
            # successfully; remove the ledger before the terminal transaction reopen.
            self._remove_ledger_tree()
            return original(*args, **kwargs)

        with mock.patch.object(
            self.store._dispatch, "_commit_settlement", side_effect=deleting
        ):
            with self.assertRaises(ValidationError):
                self._execute(did, plan)
        self._assert_ledger_tree_absent()

    def test_deletion_before_result_lookup_refuses_without_recreation(self) -> None:
        did = self._pending_in_flight("delresult")
        plan = self._preview(did)["plan"]
        original = self.store._dispatch._settlement_result
        reached = {"result_lookup": False}

        def deleting(*args, **kwargs):
            # Actor authorization, the replay lookup, and the terminal transaction
            # have already committed the durable operator settlement (its atomic
            # notice + producer semaphore included); remove the ledger immediately
            # before the FINAL post-commit result-lookup reopen so it must refuse
            # loudly without recreating the parent, database, or any sidecar.
            reached["result_lookup"] = True
            self._remove_ledger_tree()
            return original(*args, **kwargs)

        with mock.patch.object(
            self.store._dispatch, "_settlement_result", side_effect=deleting
        ):
            with self.assertRaises(ValidationError):
                self._execute(did, plan)
        # The terminal transaction committed (the post-commit result lookup was
        # reached) yet the result reopen still refused and recreated nothing.
        self.assertTrue(reached["result_lookup"])
        self._assert_ledger_tree_absent()


if __name__ == "__main__":
    unittest.main()
