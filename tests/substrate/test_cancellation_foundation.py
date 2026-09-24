"""Stage-2 T2 foundation: additive cancellation schema, terminal vocabulary,
the canonical dispatch+transport projection, and mixed-reader compatibility.

This module proves only the T2 *foundation* the reviewed stage-2 brief couples
together (Required outcome 2 and the projection/mixed-reader slice of T2). It
does NOT exercise the producer/admin cancellation surface, the cancellation
state machine, or the emergency settlement plan -- those are separate rounds.
The point here is that *readers* stay correct once additive ``cancelled_at``
columns and ``cancelled`` values exist, and that the join projection never
infers one state machine from the other. Historically the cancellation
additions were additive at the then-deployed ledger floor 1; the ledger floor
has since advanced to 2 for dispatch payload transport (not a cancellation
change), so the floor assertions here track the declared
``LEDGER_SCHEMA_VERSION`` rather than a stale literal.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import db as db_module
from agent_comms import paths, schema, supervisor
from agent_comms.db import Database, LEDGER_SCHEMA_VERSION
from agent_comms.schema import ValidationError
from agent_comms.dispatch_ledger import (
    DISPATCH_TERMINAL_STATUSES,
    RECIPIENT_TERMINAL_LEDGER_OPEN,
    is_dispatch_terminal,
    project_dispatch_transport,
)
from agent_comms.store import Store


def _columns(db_path: Path, table: str) -> set[str]:
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        return {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}


def _user_version(db_path: Path) -> int:
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        return int(conn.execute("pragma user_version").fetchone()[0])


def _seed_dispatch(store: Store, root: Path, key: str) -> tuple[str, str]:
    """Create a queued dispatch and return (dispatch_id, message_id)."""
    store.register_agent_actor("arch", "alpha", "architect", str(root / "arch"), [])
    store.register_agent_actor("wrk", "alpha", "worker", str(root / "wrk"), [], owner="arch")
    dispatch = store.dispatch_agent("arch", "wrk", key, "subject", "body", [])
    return dispatch["dispatch_id"], dispatch["message_id"]


class SchemaColumnsTest(unittest.TestCase):
    def test_fresh_db_has_cancelled_at_columns_and_declared_floor_user_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agent-comms.sqlite"
            Database(db_path).init()

            self.assertIn("cancelled_at", _columns(db_path, "dispatch_ledger"))
            self.assertIn("cancelled_at", _columns(db_path, "message_recipients"))
            # The cancellation columns were additive at the floor deployed when
            # they landed (floor 1). Dispatch payload transport has since moved
            # the declared floor to 2, so a fresh db lands on the declared
            # contract, not on any historical literal.
            self.assertEqual(LEDGER_SCHEMA_VERSION, 3)
            self.assertEqual(_user_version(db_path), LEDGER_SCHEMA_VERSION)

    def test_cancelled_at_columns_are_writable_and_default_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agent-comms.sqlite"
            store = Store(db_path)
            dispatch_id, message_id = _seed_dispatch(store, Path(tmp), "writable")

            with store.connection() as conn:
                row = conn.execute(
                    "select cancelled_at from dispatch_ledger where dispatch_id = ?",
                    (dispatch_id,),
                ).fetchone()
                self.assertIsNone(row["cancelled_at"])
                conn.execute(
                    "update dispatch_ledger set cancelled_at = ? where dispatch_id = ?",
                    ("2026-07-15T12:00:00+00:00", dispatch_id),
                )
                conn.execute(
                    "update message_recipients set cancelled_at = ? where message_id = ?",
                    ("2026-07-15T12:00:00+00:00", message_id),
                )
                ledger_ts = conn.execute(
                    "select cancelled_at from dispatch_ledger where dispatch_id = ?",
                    (dispatch_id,),
                ).fetchone()["cancelled_at"]
                transport_ts = conn.execute(
                    "select cancelled_at from message_recipients where message_id = ?",
                    (message_id,),
                ).fetchone()["cancelled_at"]
            self.assertEqual(ledger_ts, "2026-07-15T12:00:00+00:00")
            self.assertEqual(transport_ts, "2026-07-15T12:00:00+00:00")

    def test_reinit_is_idempotent_and_keeps_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agent-comms.sqlite"
            Database(db_path).init()
            Database(db_path).init()
            self.assertIn("cancelled_at", _columns(db_path, "dispatch_ledger"))
            self.assertIn("cancelled_at", _columns(db_path, "message_recipients"))
            self.assertEqual(_user_version(db_path), LEDGER_SCHEMA_VERSION)


class TerminalVocabularyTest(unittest.TestCase):
    def test_cancelled_is_in_transport_status_vocabulary(self) -> None:
        # Stage-2 foundation adds the recipient transport status ``cancelled`` to
        # the mailbox vocabulary alongside the prior four. This stage-2 record
        # DOES perform the single contract-pin advance (contract 10 -> 11) via
        # T10; the executable contract pin now lives in
        # tests/substrate/test_dead_worker_contract.py.
        self.assertIn("cancelled", schema.STATUSES)
        self.assertEqual(schema.STATUSES, {"sent", "read", "acknowledged", "closed", "cancelled"})

    def test_cancelled_is_a_dispatch_terminal_status(self) -> None:
        self.assertIn("cancelled", DISPATCH_TERMINAL_STATUSES)
        self.assertTrue(is_dispatch_terminal("cancelled"))

    def test_the_two_duplicated_terminal_sets_agree(self) -> None:
        # The brief requires cancelled to land in BOTH the dispatch_ledger and
        # supervisor copies and requires their agreement to be pinned.
        self.assertEqual(
            set(DISPATCH_TERMINAL_STATUSES),
            set(supervisor.DISPATCH_TERMINAL_STATUSES),
        )
        self.assertIn("cancelled", supervisor.DISPATCH_TERMINAL_STATUSES)

    def test_prior_terminal_statuses_still_terminal(self) -> None:
        for status in ("closed", "dlq", "spawn_failed_message_landed"):
            self.assertTrue(is_dispatch_terminal(status))
        self.assertFalse(is_dispatch_terminal("queued"))
        self.assertFalse(is_dispatch_terminal("in_flight"))


class ListInboxCancelledTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        self.dispatch_id, self.message_id = _seed_dispatch(self.store, self.tmp, "inbox")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _cancel_transport(self) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "update message_recipients set status = 'cancelled', cancelled_at = ? "
                "where message_id = ? and to_agent = 'wrk'",
                ("2026-07-15T12:00:00+00:00", self.message_id),
            )
            conn.execute(
                "update dispatch_ledger set status = 'cancelled', cancelled_at = ? where dispatch_id = ?",
                ("2026-07-15T12:00:00+00:00", self.dispatch_id),
            )

    def test_default_listing_excludes_cancelled_copy(self) -> None:
        self._cancel_transport()
        # Default listing (include_closed=False) treats cancelled as terminal,
        # exactly like closed: the withdrawn obligation does not resurface.
        default = self.store.list_inbox("wrk", unread_only=False, include_closed=False)
        self.assertEqual([m["id"] for m in default], [])
        # include_closed=True still shows it for audit.
        audited = self.store.list_inbox("wrk", unread_only=False, include_closed=True)
        self.assertIn(self.message_id, [m["id"] for m in audited])

    def test_read_ack_close_cannot_resurrect_cancelled_copy(self) -> None:
        self._cancel_transport()
        # None of the terminal-copy operations may rewrite a cancelled copy back
        # to a live/closed transport status.
        self.store.read_message("wrk", self.message_id)
        self.assertEqual(self._transport_status(), "cancelled")
        with contextlib.suppress(Exception):
            self.store.ack_message("wrk", self.message_id, "late ack")
        self.assertEqual(self._transport_status(), "cancelled")
        with contextlib.suppress(Exception):
            self.store.close_message("wrk", self.message_id, "late close")
        self.assertEqual(self._transport_status(), "cancelled")

    def _transport_status(self) -> str:
        with self.store.connection() as conn:
            return conn.execute(
                "select status from message_recipients where message_id = ? and to_agent = 'wrk'",
                (self.message_id,),
            ).fetchone()["status"]


class WorkerUsageCancelledTest(unittest.TestCase):
    def test_cancelled_rows_are_worker_usage_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "agent-comms.sqlite")
            dispatch_id, _ = _seed_dispatch(store, Path(tmp), "usage")
            with store.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set status = 'cancelled', cancelled_at = ? where dispatch_id = ?",
                    ("2026-07-15T12:00:00+00:00", dispatch_id),
                )
            candidates, malformed = store.worker_usage_candidates(batch=25)
            self.assertEqual(malformed, 0)
            self.assertIn(dispatch_id, [c["dispatch_id"] for c in candidates])


class ProjectionTest(unittest.TestCase):
    def test_named_cancellation_pairs(self) -> None:
        confirmed = project_dispatch_transport("cancelled", "cancelled")
        self.assertEqual(confirmed["dispatch_status"], "cancelled")
        self.assertEqual(confirmed["transport_status"], "cancelled")
        self.assertEqual(confirmed["outcome"], "confirmed_cancel")

        settled = project_dispatch_transport("dlq", "cancelled")
        self.assertEqual(settled["outcome"], "operator_settled_termination_unconfirmed")

        close_first = project_dispatch_transport("cancelled", "closed")
        self.assertEqual(close_first["outcome"], "confirmed_cancel_transport_closed_first")

    def test_ordinary_lifecycle_pairs_are_total_and_distinct_from_cancel(self) -> None:
        # Every mechanically reachable ordinary pair, including the late/racing
        # ack/close on a non-in_flight ledger (queued/*, dlq/*, spawn_failed/*)
        # that a prior revision wrongly called impossible.
        cases = {
            ("queued", "sent"): "queued",
            ("queued", "read"): "queued",
            ("queued", "acknowledged"): "queued",
            ("queued", "closed"): "queued",
            ("in_flight", "sent"): "in_flight",
            ("in_flight", "read"): "in_flight",
            ("closed", "closed"): "closed",
            ("closed", "acknowledged"): "closed",
            ("dlq", "sent"): "dlq",
            ("dlq", "read"): "dlq",
            ("dlq", "acknowledged"): "dlq",
            ("dlq", "closed"): "dlq",
            ("spawn_failed_message_landed", "sent"): "spawn_failed",
            ("spawn_failed_message_landed", "read"): "spawn_failed",
            ("spawn_failed_message_landed", "acknowledged"): "spawn_failed",
            ("spawn_failed_message_landed", "closed"): "spawn_failed",
        }
        for (dispatch_status, transport_status), outcome in cases.items():
            result = project_dispatch_transport(dispatch_status, transport_status)
            self.assertEqual(result["outcome"], outcome, (dispatch_status, transport_status))
            # Ordinary outcomes must never be confused with a cancellation.
            self.assertNotIn("cancel", result["outcome"])

    def test_reachable_transport_advances_are_not_unknown(self) -> None:
        # These pairs are constructed by real Store operations in
        # ProjectionReachabilityTest; here we pin that the projection recognizes
        # them (non-unknown) and follows the execution status.
        for dispatch_status in ("queued", "dlq", "spawn_failed_message_landed"):
            for transport_status in ("acknowledged", "closed"):
                outcome = project_dispatch_transport(dispatch_status, transport_status)["outcome"]
                self.assertNotEqual(
                    outcome, "unknown", (dispatch_status, transport_status)
                )
                self.assertNotIn("cancel", outcome)

    def test_stale_in_flight_with_terminal_recipient_projects_reconciler_outcome(self) -> None:
        # A close/ack flips an in_flight ledger to closed in lockstep, so the
        # pair is never committed on the happy path. But the liveness / hard-TTL
        # reconciler OWNS settling a stale in_flight row whose recipient copy
        # already terminalized, so the projection recognizes it with a DISTINCT
        # outcome the reconciler consumes -- never a plain ``closed`` (the ledger
        # has not closed) and never ``unknown`` (the reconciler must act).
        self.assertEqual(
            project_dispatch_transport("in_flight", "closed")["outcome"],
            RECIPIENT_TERMINAL_LEDGER_OPEN,
        )
        self.assertEqual(
            project_dispatch_transport("in_flight", "acknowledged")["outcome"],
            RECIPIENT_TERMINAL_LEDGER_OPEN,
        )
        # It is neither an ordinary close nor an unknown, and carries no cancel.
        self.assertNotEqual(RECIPIENT_TERMINAL_LEDGER_OPEN, "closed")
        self.assertNotEqual(RECIPIENT_TERMINAL_LEDGER_OPEN, "unknown")
        self.assertNotIn("cancel", RECIPIENT_TERMINAL_LEDGER_OPEN)

    def test_impossible_or_unrecognized_pair_is_loud_unknown(self) -> None:
        # closed is reached ONLY via ack/close, so a sent/read transport with a
        # closed ledger cannot occur.
        self.assertEqual(project_dispatch_transport("closed", "sent")["outcome"], "unknown")
        self.assertEqual(project_dispatch_transport("closed", "read")["outcome"], "unknown")
        # A transport becomes cancelled only jointly with a cancelled/dlq ledger,
        # so a cancelled transport under any other ledger status is unreachable.
        self.assertEqual(project_dispatch_transport("queued", "cancelled")["outcome"], "unknown")
        self.assertEqual(project_dispatch_transport("closed", "cancelled")["outcome"], "unknown")
        self.assertEqual(project_dispatch_transport("cancelled", "sent")["outcome"], "unknown")
        # Unknown status values.
        self.assertEqual(project_dispatch_transport("bogus", "sent")["outcome"], "unknown")

    def test_projection_never_infers_one_status_from_the_other(self) -> None:
        # dlq/cancelled must NOT collapse to a plain dlq or a confirmed cancel.
        settled = project_dispatch_transport("dlq", "cancelled")
        self.assertNotEqual(settled["outcome"], "dlq")
        self.assertNotEqual(settled["outcome"], "confirmed_cancel")
        # cancelled/closed must NOT collapse to an ordinary close.
        self.assertNotEqual(project_dispatch_transport("cancelled", "closed")["outcome"], "closed")


class ProjectionReachabilityTest(unittest.TestCase):
    """Prove the newly-recognized ordinary pairs are MECHANICALLY REACHABLE by
    constructing them with real Store operations, not by assertion.

    The mechanism: ``ack_message``/``close_message`` advance the transport copy
    unconditionally but only flip the ledger ``where status = 'in_flight'``. So a
    recipient that replies then acks/closes while the ledger is still ``queued``
    (or already ``dlq``/``spawn_failed_message_landed``) commits a pair whose
    ledger did not move. A prior revision wrongly classified ``queued/acknowledged``
    and ``queued/closed`` as impossible.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = Store(self.tmp / "agent-comms.sqlite")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fresh(self, key: str) -> tuple[str, str]:
        # These reachability proofs exercise the LEGACY transport mechanics:
        # ``ack_message``/``close_message`` advancing the recipient copy while
        # the ledger stays put. A v2 trigger refuses both calls by design
        # (reply-then-``close_dispatch`` is the only v2 closeout), so the pairs
        # are only mechanically reachable on a v1 row.
        dispatch_id, message_id = _seed_dispatch(self.store, self.tmp, key)
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set policy_version = 'v1' where dispatch_id = ?",
                (dispatch_id,),
            )
        return dispatch_id, message_id

    def _reply(self, message_id: str) -> None:
        self.store.send_message("wrk", ["arch"], "re", "reply body", [], parent_message_id=message_id)

    def _pair(self, dispatch_id: str, message_id: str) -> tuple[str, str]:
        with self.store.connection() as conn:
            ledger = conn.execute(
                "select status from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
            ).fetchone()["status"]
            transport = conn.execute(
                "select status from message_recipients where message_id = ? and to_agent = 'wrk'",
                (message_id,),
            ).fetchone()["status"]
        return ledger, transport

    def _set_ledger(self, dispatch_id: str, status: str) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status = ? where dispatch_id = ?", (status, dispatch_id)
            )

    def test_queued_ack_and_close_are_reachable(self) -> None:
        dispatch_id, message_id = self._fresh("queued-ack")
        self._reply(message_id)
        self.store.ack_message("wrk", message_id, "done")
        self.assertEqual(self._pair(dispatch_id, message_id), ("queued", "acknowledged"))
        self.assertEqual(
            project_dispatch_transport("queued", "acknowledged")["outcome"], "queued"
        )

        dispatch_id, message_id = self._fresh("queued-close")
        self._reply(message_id)
        self.store.close_message("wrk", message_id, "")
        self.assertEqual(self._pair(dispatch_id, message_id), ("queued", "closed"))
        self.assertEqual(project_dispatch_transport("queued", "closed")["outcome"], "queued")

    def test_dlq_late_ack_and_close_are_reachable(self) -> None:
        for suffix, action in (("ack", "ack"), ("close", "close")):
            dispatch_id, message_id = self._fresh(f"dlq-{suffix}")
            self._set_ledger(dispatch_id, "dlq")
            self._reply(message_id)
            if action == "ack":
                self.store.ack_message("wrk", message_id, "late")
                transport = "acknowledged"
            else:
                self.store.close_message("wrk", message_id, "")
                transport = "closed"
            self.assertEqual(self._pair(dispatch_id, message_id), ("dlq", transport))
            self.assertEqual(project_dispatch_transport("dlq", transport)["outcome"], "dlq")

    def test_spawn_failed_late_close_is_reachable(self) -> None:
        dispatch_id, message_id = self._fresh("spawn-failed-close")
        self._set_ledger(dispatch_id, "spawn_failed_message_landed")
        self._reply(message_id)
        self.store.close_message("wrk", message_id, "")
        self.assertEqual(
            self._pair(dispatch_id, message_id), ("spawn_failed_message_landed", "closed")
        )
        self.assertEqual(
            project_dispatch_transport("spawn_failed_message_landed", "closed")["outcome"],
            "spawn_failed",
        )

    def test_in_flight_ack_atomically_flips_ledger_so_pair_is_not_in_flight_ack(self) -> None:
        # Confirms (in_flight, acknowledged) is NOT committed on the happy path:
        # the ack transaction flips the ledger to closed in lockstep. The
        # projection still recognizes that pair as the reconciler's domain (see
        # test_stale_in_flight_with_terminal_recipient_projects_reconciler_outcome),
        # but no ordinary Store transaction commits it.
        dispatch_id, message_id = self._fresh("in-flight-ack")
        self._set_ledger(dispatch_id, "in_flight")
        self._reply(message_id)
        self.store.ack_message("wrk", message_id, "ok")
        self.assertEqual(self._pair(dispatch_id, message_id), ("closed", "acknowledged"))


class ProjectionIntegrationTest(unittest.TestCase):
    """The single canonical joined projection is INTEGRATED into the REPORTING
    read.

    This class proves only the reporting surface: ``list_dispatches`` and the
    canonical ``project_dispatch`` joined read preserve the two distinct
    execution/transport states and expose the normalized emergency-settlement
    outcome, never inferring one state machine from the other. That the SAME
    shared classifier is also CONSUMED by the cleanup and monitoring production
    paths is proven separately -- cleanup in
    ``test_cancellation_janitor_gate.JanitorConsumesJoinedProjectionTest`` (via
    ``supervisor.janitor_sweep``) and monitoring in ``test_monitor_terminal_cas``
    (via ``reconcile_dispatches`` reconcile-to-closed actions).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = Store(self.tmp / "agent-comms.sqlite")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(self, key: str) -> tuple[str, str]:
        return _seed_dispatch(self.store, self.tmp, key)

    def _set_states(self, dispatch_id: str, message_id: str, ledger: str, transport: str) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status = ? where dispatch_id = ?", (ledger, dispatch_id)
            )
            conn.execute(
                "update message_recipients set status = ? where message_id = ? and to_agent = 'wrk'",
                (transport, message_id),
            )

    def _report_row(self, dispatch_id: str) -> dict:
        rows = {row["dispatch_id"]: row for row in self.store.list_dispatches(limit=50)}
        return rows[dispatch_id]

    def test_reporting_exposes_emergency_settlement_outcome_distinctly(self) -> None:
        # dlq ledger + cancelled transport = the exceptional operator settlement.
        dispatch_id, message_id = self._seed("settled")
        self._set_states(dispatch_id, message_id, "dlq", "cancelled")

        row = self._report_row(dispatch_id)
        # Both raw states are preserved and distinct; the reporting outcome is the
        # normalized settlement outcome, NOT a plain dlq or a confirmed cancel.
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(row["transport_status"], "cancelled")
        self.assertEqual(row["outcome"], "operator_settled_termination_unconfirmed")

        # The single canonical joined read agrees.
        projected = self.store.project_dispatch(dispatch_id)
        self.assertEqual(projected["dispatch_status"], "dlq")
        self.assertEqual(projected["transport_status"], "cancelled")
        self.assertEqual(projected["outcome"], "operator_settled_termination_unconfirmed")

    def test_reporting_and_projection_distinguish_confirmed_cancel(self) -> None:
        dispatch_id, message_id = self._seed("confirmed")
        self._set_states(dispatch_id, message_id, "cancelled", "cancelled")
        self.assertEqual(self._report_row(dispatch_id)["outcome"], "confirmed_cancel")
        self.assertEqual(self.store.project_dispatch(dispatch_id)["outcome"], "confirmed_cancel")

    def test_reporting_ordinary_rows_are_not_cancellations(self) -> None:
        queued_id, _ = self._seed("ord-queued")
        rows = {row["dispatch_id"]: row for row in self.store.list_dispatches(limit=50)}
        self.assertEqual(rows[queued_id]["outcome"], "queued")
        self.assertNotIn("cancel", rows[queued_id]["outcome"])

    def test_transport_close_first_race_is_not_folded_into_close(self) -> None:
        # The shared classifier keeps the confirmed-cancel-with-transport-closed
        # race distinct from an ordinary close, so the cleanup and monitoring
        # surfaces that consume it can never fold the two together.
        dispatch_id, message_id = self._seed("close-first")
        self._set_states(dispatch_id, message_id, "cancelled", "closed")
        projected = self.store.project_dispatch(dispatch_id)
        self.assertEqual(projected["outcome"], "confirmed_cancel_transport_closed_first")
        self.assertNotEqual(projected["outcome"], "closed")

    def test_project_dispatch_absent_row_is_none(self) -> None:
        self.assertIsNone(self.store.project_dispatch("dispatch_does_not_exist"))


class MixedReaderCompatibilityTest(unittest.TestCase):
    """A v1.0.9-shaped reader stays safe when additive columns and cancelled
    values exist: it never sees a cancelled copy as unread and no query it knows
    can recreate queued/in-flight state from a cancelled row."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        self.live_id, self.live_msg = _seed_dispatch(self.store, self.tmp, "live")
        self.cancelled_id, self.cancelled_msg = _seed_dispatch(self.store, self.tmp, "cancelled")
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status = 'cancelled', cancelled_at = ? where dispatch_id = ?",
                ("2026-07-15T12:00:00+00:00", self.cancelled_id),
            )
            conn.execute(
                "update message_recipients set status = 'cancelled', cancelled_at = ? where message_id = ?",
                ("2026-07-15T12:00:00+00:00", self.cancelled_msg),
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _old_reader(self) -> sqlite3.Connection:
        # Simulates a pinned older release: it opens the same file and issues
        # only the column-named queries it already knew, unaware of cancelled_at
        # or the cancelled value.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_prior_default_ledger_reader_refuses_floor_2(self) -> None:
        # The production boundary that keeps a floor-1 binary out of a
        # payload-capable ledger is the DEFAULT-ledger guard: a prior reader
        # declaring ledger floor 1 must refuse this floor-2 ledger loudly
        # before any write. An explicit --db/AGENT_COMMS_DB override
        # intentionally bypasses that guard, so an override open proves
        # nothing about the boundary; this proof goes through the canonical
        # default path under a redirected HOME.
        self.assertEqual(_user_version(self.db_path), LEDGER_SCHEMA_VERSION)
        home = self.tmp / "prior-home"
        default_ledger = home / ".agent-comms" / "agent-comms.sqlite"
        default_ledger.parent.mkdir(parents=True)
        shutil.copyfile(self.db_path, default_ledger)
        before = default_ledger.read_bytes()
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(paths.canonical_db_path(), default_ledger)
            with mock.patch.object(db_module, "LEDGER_SCHEMA_VERSION", 1):
                with self.assertRaises(ValidationError) as refused:
                    Store(paths.canonical_db_path(), is_default_db_open=True).init()
        self.assertIn("newer ledger schema", str(refused.exception))
        # The refusal happened before any write: the ledger is byte-identical.
        self.assertEqual(default_ledger.read_bytes(), before)

    def test_old_unread_listing_does_not_surface_cancelled_copy(self) -> None:
        with contextlib.closing(self._old_reader()) as conn:
            unread = conn.execute(
                "select message_id from message_recipients where to_agent = 'wrk' and status = 'sent'"
            ).fetchall()
        surfaced = {row["message_id"] for row in unread}
        self.assertIn(self.live_msg, surfaced)
        self.assertNotIn(self.cancelled_msg, surfaced)

    def test_old_dispatch_liveness_query_excludes_cancelled_row(self) -> None:
        with contextlib.closing(self._old_reader()) as conn:
            live = conn.execute(
                "select dispatch_id from dispatch_ledger where status in ('queued', 'in_flight')"
            ).fetchall()
        live_ids = {row["dispatch_id"] for row in live}
        self.assertIn(self.live_id, live_ids)
        self.assertNotIn(self.cancelled_id, live_ids)

    def test_old_named_column_queries_ignore_additive_columns(self) -> None:
        # A named-column select the old reader knew keeps working unchanged; the
        # additive cancelled_at is simply not part of its result set.
        with contextlib.closing(self._old_reader()) as conn:
            row = conn.execute(
                "select dispatch_id, status, created_at from dispatch_ledger where dispatch_id = ?",
                (self.cancelled_id,),
            ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(set(row.keys()), {"dispatch_id", "status", "created_at"})

    def test_old_reader_producer_cap_query_excludes_cancelled(self) -> None:
        # Producer dispatch-cap accounting counts only in_flight rows. Promote the
        # live row to in_flight and confirm the OLD reader's identical cap query
        # counts exactly one: a cancelled row released the producer cap and is
        # never miscounted as consuming it.
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status = 'in_flight' where dispatch_id = ?",
                (self.live_id,),
            )
        with contextlib.closing(self._old_reader()) as conn:
            count = conn.execute(
                "select count(*) as c from dispatch_ledger "
                "where producer_actor_id = 'arch' and status = 'in_flight'"
            ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_old_reader_lineage_holding_query_excludes_cancelled(self) -> None:
        # Give the cancelled row an auth lineage key + a far-future claim. The OLD
        # reader's lineage-holding predicate (in_flight, or queued/spawn_failed
        # with a live claim) must NOT treat the cancelled row as holding the
        # lineage: a cancelled dispatch has released it.
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set auth_lineage_key = 'LK', auth_lineage_claimed_at = ? "
                "where dispatch_id = ?",
                ("2999-01-01T00:00:00+00:00", self.cancelled_id),
            )
        with contextlib.closing(self._old_reader()) as conn:
            holding = conn.execute(
                """
                select 1
                from dispatch_ledger
                where auth_lineage_key = 'LK'
                  and (
                    status = 'in_flight'
                    or (
                      status in ('queued', 'spawn_failed_message_landed')
                      and auth_lineage_claimed_at is not null
                    )
                  )
                limit 1
                """
            ).fetchone()
        self.assertIsNone(holding)

    def test_old_reader_terminal_status_set_degrades_conservatively(self) -> None:
        # NAMED degraded older-reader behavior (rather than silently assumed
        # compatible): a v1.0.9 reader's terminal-status set predates ``cancelled``,
        # so a status-partitioned old reporter/cleanup buckets a cancelled row as
        # an UNRECOGNIZED / non-terminal status. That degradation is bounded and
        # SAFE: ``cancelled`` is never one of the live states, so the old reader
        # never counts it as live, resurrects it, or double-counts it -- it merely
        # under-labels it as not-yet-terminal and, with a status-only cleanup gate,
        # preserves rather than deletes it.
        old_terminal = {"closed", "dlq", "spawn_failed_message_landed"}
        old_live = {"queued", "in_flight"}
        self.assertNotIn("cancelled", old_terminal)
        self.assertNotIn("cancelled", old_live)
        with contextlib.closing(self._old_reader()) as conn:
            status = conn.execute(
                "select status from dispatch_ledger where dispatch_id = ?",
                (self.cancelled_id,),
            ).fetchone()["status"]
        self.assertEqual(status, "cancelled")
        self.assertNotIn(status, old_terminal)  # degraded: unrecognized as terminal
        self.assertNotIn(status, old_live)  # safe: never mistaken for a live dispatch


if __name__ == "__main__":
    unittest.main()
