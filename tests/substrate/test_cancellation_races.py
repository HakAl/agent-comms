"""Stage-2 T6: first-committer races through PRODUCTION entry points.

These proofs drive the internal cancellation engine ONLY through its production
seams -- ``dispatch_agent`` / ``start_queued_dispatches`` / ``request_cancellation``
/ ``reconcile_dispatches`` -- and inject the race with a CONTROLLED interleaving
(an adapter ``dispatch()`` / ``halt()`` that mutates the ledger at the exact
window under test). They replace the earlier proofs that reached into private
spawn-commit helpers. Covered interleavings (T3-T5 corrections):

- run-token replacement between the authenticated HALT and the terminal CAS: the
  newer run is preserved and the cancellation stays unconfirmed (T3);
- a first cancellation HALT failure followed by a hard-TTL HALT confirmation of
  the SAME pending cancellation: truthful ``cancelled``, never ``dlq`` (T4);
- persisted ``dlq`` / ``termination_not_confirmed`` residue followed by an ACTUAL
  monitor re-probe and same-run evidence upgrade: the observation and janitor
  eligibility upgrade while the ledger status stays ``dlq`` (T5);
- bootstrap cancellation through the public start path, including current same-run
  exit evidence confirming ahead of any early-exit classification (T5);
- concurrent/controlled duplicate monitor passes: a stable terminal winner with
  no observed-value loss and no terminal-to-live transition.

The exceptional settlement-vs-hard-TTL settlement races remain pending with the
excluded settlement implementation and are intentionally NOT asserted here.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import json
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.dispatch_ledger import (
    DLQ_RESIDUE_REPROBE_BATCH,
    HARD_TTL_KILL_GRACE_SECONDS,
    CancellationConflictError,
    CancellationStateError,
    DispatchLedger,
)
from agent_comms.store import Store
from agent_comms.supervisor import confirmed_termination_evidence

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"
_Status = namedtuple("_Status", ["state", "detail"])


def _complete_reaper(token: str, returncode: int = 0) -> dict:
    """Revision 7 F2: the exact version-1 COMPLETE ``$.reaper_exit`` proof bound to
    ``token`` -- the only evidence that confirms a cancellation
    ``same_run_exit_confirmed``. A bare ``worker_exit`` or the old four-field
    reaper no longer qualifies."""
    return {
        "proof_version": 1,
        "run_token": token,
        "returncode": returncode,
        "source": "halt_finalize",
        "reaped_at": "2026-07-26T00:00:00+00:00",
        "registered_wrapper_reaped": True,
        "native_process_group_drained": True,
        "owned_artifacts_absent": {"run_dir": True, "control_socket": True, "zdotdir_parent": True},
    }


class ConfirmingAdapter:
    def __init__(self) -> None:
        self.control_socket = "/nonexistent/agent-comms/run/s/control.sock"
        self.run_token = "run-token-race-0001"
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
        return _Status("running", "d")

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))


class UnconfirmedAdapter(ConfirmingAdapter):
    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        raise RuntimeError("halt unconfirmed")


class FlakyHaltAdapter(ConfirmingAdapter):
    """Halt raises on the FIRST call and confirms on every later call.

    Models a supervisor whose first termination attempt (the pre-TTL cancellation
    drive) fails transiently but whose next attempt (the hard-TTL backstop, same
    pass) confirms the native child is gone.
    """

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        if len(self.halt_calls) == 1:
            raise RuntimeError("first halt did not confirm termination")


class GatedHaltAdapter(ConfirmingAdapter):
    """Halt raises until ``confirm`` is flipped, then confirms.

    Models a supervisor that is unreachable when the row is hard-TTL'd (leaving
    ``termination_not_confirmed`` residue) and becomes reachable on a later pass.
    """

    def __init__(self) -> None:
        super().__init__()
        self.confirm = False

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        if not self.confirm:
            raise RuntimeError("supervisor unreachable")


class ReprobePersistingHaltAdapter(GatedHaltAdapter):
    """A ``GatedHaltAdapter`` whose CONFIRMING halt also persists the complete
    exact-token proof, mirroring the real authenticated HALT.

    Revision 7 F2: a real ``ProcessSpawnAdapter`` HALT persists the complete
    exact-token version-1 ``$.reaper_exit`` proof DURING the successful halt call,
    before the monitor records ``supervised_halt_confirmed``. While the supervisor
    is unreachable this fake raises and persists nothing (the residue stays
    proof-free and uncleanable); once ``confirm`` is flipped its successful halt
    atomically persists the exact complete proof for the row's current run token,
    so a residue re-probe upgrade rests on real same-run evidence rather than a
    bare halt return.
    """

    def __init__(self, store: Store, dispatch_id: str) -> None:
        super().__init__()
        self._store = store
        self._dispatch_id = dispatch_id

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        if not self.confirm:
            raise RuntimeError("supervisor unreachable")
        with self._store._db.connection() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (self._dispatch_id,),
            ).fetchone()
            observed = json.loads(row["observed_values_json"] or "{}")
            observed["reaper_exit"] = _complete_reaper(self.run_token)
            conn.execute(
                "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                (json.dumps(observed, sort_keys=True), self._dispatch_id),
            )
            conn.commit()


class TokenReplacingHaltAdapter(ConfirmingAdapter):
    """Halt confirms but a concurrent run replaces the row's run token first.

    The HALT authenticates the OLD token, then -- before the terminal CAS re-reads
    the row -- a newer run stamps a different ``run_token`` onto the ledger. The
    terminal CAS is bound to the halted token, so it must miss and preserve the
    newer run.
    """

    def __init__(self, store: Store, dispatch_id: str, new_token: str) -> None:
        super().__init__()
        self._store = store
        self._dispatch_id = dispatch_id
        self._new_token = new_token

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        with self._store._db.connection() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (self._dispatch_id,),
            ).fetchone()
            observed = json.loads(row["observed_values_json"] or "{}")
            observed["run_token"] = self._new_token
            conn.execute(
                "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                (json.dumps(observed, sort_keys=True), self._dispatch_id),
            )
            conn.commit()


class BootstrapCancelAdapter(ConfirmingAdapter):
    """``dispatch()`` records a pending cancellation (and optional exit) mid-bootstrap.

    Injects the cancellation at the exact window a real one would land: after the
    row is claimed for spawn but before the spawn-commit re-reads it. The pending
    request is recorded through the PUBLIC ``request_cancellation`` seam. The stub
    runtime carries no auth lineage, so its production claim is a no-op; the
    adapter stamps the claim marker a real supervised spawn would hold so the
    concurrent cancel records a PENDING request rather than an immediate
    ``not_started`` on an unclaimed queued row.
    """

    def __init__(self, store: Store, *, extra_observed: dict | None = None) -> None:
        super().__init__()
        self._store = store
        self._extra_observed = extra_observed or {}

    def _bootstrap_pending(self, dispatch_id: str) -> None:
        with self._store._db.connection() as conn:
            conn.execute("begin immediate")
            conn.execute(
                "update dispatch_ledger set auth_lineage_claimed_at = ? "
                "where dispatch_id = ? and auth_lineage_claimed_at is null",
                ("2026-07-15T12:00:00+00:00", dispatch_id),
            )
            conn.commit()
        self._store.request_cancellation(
            dispatch_id, requesting_actor_id="arch", reason="withdraw", authority="producer"
        )
        if self._extra_observed:
            with self._store._db.connection() as conn:
                conn.execute("begin immediate")
                row = conn.execute(
                    "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                    (dispatch_id,),
                ).fetchone()
                observed = json.loads(row["observed_values_json"] or "{}")
                observed.update(self._extra_observed)
                conn.execute(
                    "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                    (json.dumps(observed, sort_keys=True), dispatch_id),
                )
                conn.commit()

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        self._bootstrap_pending(context.dispatch["dispatch_id"])
        return super().dispatch(context)


class BootstrapCancelUnconfirmedAdapter(BootstrapCancelAdapter):
    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        raise RuntimeError("halt unconfirmed")


class TokenNullingHaltAdapter(ConfirmingAdapter):
    """Halt confirms but REMOVES the row's run token first (A -> NULL).

    Models the row's authenticated run token being cleared between the HALT and
    the terminal CAS. Exact-token means equality only: a missing token is never
    equal to the halted token, so the terminal CAS must miss and preserve the row.
    """

    def __init__(self, store: Store, dispatch_id: str) -> None:
        super().__init__()
        self._store = store
        self._dispatch_id = dispatch_id

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        with self._store._db.connection() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (self._dispatch_id,),
            ).fetchone()
            observed = json.loads(row["observed_values_json"] or "{}")
            observed.pop("run_token", None)
            conn.execute(
                "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                (json.dumps(observed, sort_keys=True), self._dispatch_id),
            )
            conn.commit()


class BootstrapHaltRowTokenMutator(BootstrapCancelAdapter):
    """Bootstrap cancel whose HALT stamps a different/absent token on the queued row.

    ``new_token=None`` removes the token; otherwise it stamps a NEWER run's token.
    The spawn-time settlement authenticated the OLD spawn token, so its exact
    identity claim must miss (a NEWER non-null token is a run we did not spawn) or
    -- for the pre-spawn NULL base -- remain claimable. Used to exercise the spawn
    A->B drift miss.
    """

    def __init__(self, store: Store, dispatch_id: str, new_token: str | None) -> None:
        super().__init__(store)
        self._dispatch_id = dispatch_id
        self._new_token = new_token

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        with self._store._db.connection() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (self._dispatch_id,),
            ).fetchone()
            observed = json.loads(row["observed_values_json"] or "{}")
            if self._new_token is None:
                observed.pop("run_token", None)
            else:
                observed["run_token"] = self._new_token
            conn.execute(
                "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                (json.dumps(observed, sort_keys=True), self._dispatch_id),
            )
            conn.commit()


class BootstrapNoRunTokenAdapter(BootstrapCancelAdapter):
    """Bootstrap cancel whose spawn result carries NO run token at all.

    A confirmed HALT cannot bind an exact identity to a missing token, so the
    spawn terminal HALT-derived commit must never terminalize: the row stays
    queued + pending (missing token is never treated as equal).
    """

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        self._bootstrap_pending(context.dispatch["dispatch_id"])
        return DispatchStart(
            spawn_handle=f"stub:{context.recipient['id']}:1",
            observed_values={
                "adapter": "stub",
                "control_socket": self.control_socket,
                "protocol_version": 1,
                "worker_log": "/tmp/worker.log",
            },
        )


class SpawnResultStaleExitAdapter(BootstrapCancelAdapter):
    """Current SQL carries a MATCHING same-run exit; the spawn RESULT a stale one.

    The row (current SQL evidence) already holds a same-run ``worker_exit`` whose
    token matches this spawn; the spawn result echoes a CONFLICTING stale exit.
    Current SQL evidence is authoritative, so the stale spawn-result exit must
    never mask it: the cancellation confirms ``same_run_exit_confirmed`` with NO
    HALT (an exploding HALT proves the socket path was never taken).
    """

    def __init__(self, store: Store, current_exit_token: str, stale_exit_token: str) -> None:
        super().__init__(
            store, extra_observed={"reaper_exit": _complete_reaper(current_exit_token)}
        )
        self._stale_exit_token = stale_exit_token

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        self._bootstrap_pending(context.dispatch["dispatch_id"])
        return DispatchStart(
            spawn_handle=f"stub:{context.recipient['id']}:1",
            observed_values={
                "adapter": "stub",
                "control_socket": self.control_socket,
                "run_token": self.run_token,
                "protocol_version": 1,
                "worker_log": "/tmp/worker.log",
                "worker_exit": {"returncode": 9, "run_token": self._stale_exit_token},
            },
        )

    def halt(self, spawn_handle, observed_values=None) -> None:
        raise AssertionError("HALT must not run when current SQL same-run exit confirms")


class ResidueTokenMutatingHaltAdapter(ConfirmingAdapter):
    """Residue probe whose HALT mutates the row's run token (drift), then (un)confirms.

    ``confirm=True`` returns normally (a would-be positive probe); ``confirm=False``
    raises (a would-be unconfirmed probe). Either way the row's token drifts away
    from the PROBED token before the residue CAS re-reads, so the CAS must apply
    NOTHING -- no telemetry, no evidence -- and preserve the residue.
    """

    def __init__(self, store: Store, dispatch_id: str, new_token: str, *, confirm: bool) -> None:
        super().__init__()
        self._store = store
        self._dispatch_id = dispatch_id
        self._new_token = new_token
        self._confirm = confirm

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        with self._store._db.connection() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (self._dispatch_id,),
            ).fetchone()
            observed = json.loads(row["observed_values_json"] or "{}")
            observed["run_token"] = self._new_token
            conn.execute(
                "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                (json.dumps(observed, sort_keys=True), self._dispatch_id),
            )
            conn.commit()
        if not self._confirm:
            raise RuntimeError("residue probe halt unconfirmed")


class ReentrantMonitorHaltAdapter(ConfirmingAdapter):
    """HALT that runs a full concurrent monitor pass BEFORE this pass's terminal CAS.

    The nested reconcile (a genuinely interleaved second monitor execution, using a
    plain confirming adapter so it does not recurse) drives the SAME pending
    cancellation to its terminal commit through the PRODUCTION Store entry point
    while this outer pass is mid-HALT, strictly outside any write transaction. The
    inner winner's COMPLETE relevant persisted state is captured the instant it
    commits -- BEFORE the outer loser is allowed to resume -- so the test can prove
    the outer pass wrote nothing. The outer pass's terminal CAS then re-reads a row
    another execution already terminalized: first-committer wins, no double commit,
    no observed-value loss, no terminal-to-live transition.
    """

    def __init__(self, store: Store, human_id: str, dispatch_id: str, message_id: str) -> None:
        super().__init__()
        self._store = store
        self._human_id = human_id
        self._dispatch_id = dispatch_id
        self._message_id = message_id
        self._reentered = False
        self.inner_state: dict | None = None
        self.inner_message_counts: tuple[int, int] | None = None

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        if not self._reentered:
            self._reentered = True
            # Second COMPLETE monitor execution through the production entry point,
            # DURING this outer pass's HALT and strictly before its terminal CAS.
            self._store.reconcile_dispatches(
                lambda _r: ConfirmingAdapter(), human_actor_id=self._human_id
            )
            # Capture the inner winner's complete persisted state the instant it
            # commits, BEFORE the outer loser resumes to run its (losing) CAS.
            self.inner_state = _full_state(self._store, self._dispatch_id, self._message_id)
            self.inner_message_counts = _message_counts(self._store)


def _set_cancellation_state(store: Store, dispatch_id: str, new_state: str | None) -> str:
    """Controlled interleaving: drift the persisted cancellation OFF ``requested``.

    Models a concurrent writer that withdraws/confirms the cancellation. When
    ``new_state`` is a string the cancellation object's ``state`` is set to it (all
    other cancellation fields are preserved); when ``new_state`` is ``None`` the
    cancellation object is removed entirely. The ledger STATUS is left unchanged so
    the change isolates the requested-state predicate (never the status guard).
    Returns the exact raw ``observed_values_json`` written (``sort_keys=True``, the
    same encoding production uses) so a caller can prove a losing settlement
    preserved it byte-for-byte.
    """
    with store._db.connection() as conn:
        conn.execute("begin immediate")
        row = conn.execute(
            "select observed_values_json from dispatch_ledger where dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        observed = json.loads(row["observed_values_json"] or "{}")
        if new_state is None:
            observed.pop("cancellation", None)
        else:
            cancellation = observed.get("cancellation")
            cancellation = dict(cancellation) if isinstance(cancellation, dict) else {}
            cancellation["state"] = new_state
            observed["cancellation"] = cancellation
        payload = json.dumps(observed, sort_keys=True)
        conn.execute(
            "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
            (payload, dispatch_id),
        )
        conn.commit()
    return payload


class StateDriftingHaltAdapter(ConfirmingAdapter):
    """In-flight cancel HALT that CONFIRMS but a concurrent writer changes the row's
    cancellation STATE (off ``requested``) before the terminal CAS re-reads.

    The run token is untouched, so the confirmed-cancel CAS's token predicate still
    matches; ONLY the requested-state predicate can make it miss. A
    cancellation-state change during the HALT must make the CAS lose exactly like a
    token drift: the changed row is preserved (never a false ``cancelled``) and the
    request is never restored.
    """

    def __init__(self, store: Store, dispatch_id: str, new_state: str | None = "confirmed") -> None:
        super().__init__()
        self._store = store
        self._dispatch_id = dispatch_id
        self._new_state = new_state

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        _set_cancellation_state(self._store, self._dispatch_id, self._new_state)


class BootstrapHaltStateMutator(BootstrapCancelAdapter):
    """Bootstrap cancel whose HALT CONFIRMS but changes the queued row's
    cancellation STATE (off ``requested``) before the spawn terminal CAS.

    The pre-HALT identity claim already ran and succeeded (the state was
    ``requested`` at claim time, so the exact spawn token is stamped and the HALT
    is attempted). The state then drifts DURING the HALT, so the terminal
    confirmed-cancel CAS -- and the pending diagnostic -- must miss on the
    requested-state predicate and preserve the changed row (never a false
    ``cancelled``, never restoring the request).
    """

    def __init__(self, store: Store, dispatch_id: str, new_state: str | None = "confirmed") -> None:
        super().__init__(store)
        self._dispatch_id = dispatch_id
        self._new_state = new_state

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        _set_cancellation_state(self._store, self._dispatch_id, self._new_state)


class TtlHaltStateMutator(ConfirmingAdapter):
    """Pre-TTL drive HALT is unconfirmed; the hard-TTL backstop HALT CONFIRMS but a
    concurrent writer changes the cancellation STATE (off ``requested``) first.

    The hard-TTL confirmed-cancel requires the ``requested`` state (its pending
    gate re-reads the state under the terminal lock), so the drift makes the
    confirmed-cancel path NOT fire: the row settles through ordinary TTL (``dlq``),
    never a false ``cancelled``, and the changed cancellation is preserved.
    """

    def __init__(self, store: Store, dispatch_id: str, new_state: str | None = "confirmed") -> None:
        super().__init__()
        self._store = store
        self._dispatch_id = dispatch_id
        self._new_state = new_state

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        if len(self.halt_calls) == 1:
            # Pre-TTL cancellation drive: leave the row pending so the hard-TTL
            # backstop owns the confirmed-cancel attempt.
            raise RuntimeError("pre-TTL drive halt unconfirmed")
        _set_cancellation_state(self._store, self._dispatch_id, self._new_state)


class PreHaltClaimStateDriftLedger(DispatchLedger):
    """DispatchLedger modelling a concurrent writer that drifts the cancellation OFF
    ``requested`` in the EXACT window between the spawn-commit re-read (which saw
    ``requested`` and thus entered spawn-time settlement) and the pre-HALT identity
    claim.

    No adapter seam fires in that window, so the controlled interleaving wraps the
    private pre-HALT claim (swapped in for the Store's default ledger); the row is
    still driven entirely through the public ``start_queued_dispatches`` entry
    point. Status stays ``queued`` and the token stays claimable, so ONLY the
    requested-state predicate can make the pre-HALT claim miss -- proving the claim
    loses on cancellation-state drift and the HALT never runs. The persisted
    observed json captured at the instant of the drift lets a test prove the losing
    settlement preserved the winning row byte-for-byte.
    """

    def __init__(self, *args, drift_state: str | None = "confirmed", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._drift_state = drift_state
        self._pre_halt_claim_calls = 0
        self._drifted_observed_json: str | None = None

    def _claim_spawn_run_identity_before_halt(self, dispatch_id, run_token):
        self._pre_halt_claim_calls += 1
        # A concurrent writer changes the cancellation state just before our exact
        # identity claim. Status/token are left claimable so the miss can ONLY be
        # the requested-state predicate.
        self._drifted_observed_json = _set_cancellation_state(
            self, dispatch_id, self._drift_state
        )
        return super()._claim_spawn_run_identity_before_halt(dispatch_id, run_token)


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


def _full_state(store: Store, dispatch_id: str, message_id: str) -> dict:
    """Complete relevant persisted state for a byte/equality-identity comparison.

    ``observed_values_json`` is the RAW persisted string (every write uses
    ``sort_keys=True``), so this is a byte-level snapshot of the full observed
    values -- run token, cancellation state/evidence/result, and every cancellation
    escalation/page/claim field all live inside it -- alongside the ledger's
    terminal columns and the transport copy status.
    """
    row = _ledger(store, dispatch_id)
    return {
        "status": row["status"],
        "cancelled_at": row["cancelled_at"],
        "dlq_at": row["dlq_at"],
        "closed_at": row["closed_at"],
        "spawned_at": row["spawned_at"],
        "spawn_handle": row["spawn_handle"],
        "expected_close_by": row["expected_close_by"],
        "failure_reason": row["failure_reason"],
        "auth_lineage_claimed_at": row["auth_lineage_claimed_at"],
        "observed_values_json": row["observed_values_json"],
        "transport": _transport(store, message_id),
    }


def _message_counts(store: Store) -> tuple[int, int]:
    """(messages, message_recipients) row counts -- proof no duplicate page/message."""
    with store._db.connection() as conn:
        messages = conn.execute("select count(*) as c from messages").fetchone()["c"]
        recipients = conn.execute("select count(*) as c from message_recipients").fetchone()["c"]
    return messages, recipients


def _claim(store: Store, dispatch_id: str) -> None:
    with store._db.connection() as conn:
        conn.execute(
            "update dispatch_ledger set auth_lineage_claimed_at = ? where dispatch_id = ?",
            ("2026-07-15T12:00:00+00:00", dispatch_id),
        )


def _expire(store: Store, dispatch_id: str, seconds_ago: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")
    with store._db.connection() as conn:
        conn.execute(
            "update dispatch_ledger set expected_close_by = ? where dispatch_id = ?", (ts, dispatch_id)
        )


class RaceBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = Store(self.tmp / "agent-comms.sqlite")
        _seed(self.store, self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _queued(self, key: str = "q") -> dict:
        return self.store.dispatch_agent("arch", "wrk", key, f"S {key}", f"B {key}", [])

    def _in_flight(self, adapter, key: str = "live") -> dict:
        self.store.dispatch_agent("arch", "wrk", key, f"S {key}", f"B {key}", [])
        row = self.store.start_queued_dispatches(lambda _r: adapter, ttl_seconds=3600)[0]
        assert row["status"] == "in_flight", row
        return row

    def _cancel(self, dispatch_id: str, adapter=None, reason: str = "withdraw", authority: str = "producer", actor: str = "arch"):
        kwargs = {"requesting_actor_id": actor, "reason": reason, "authority": authority}
        if adapter is not None:
            kwargs["adapter_for_runtime"] = lambda _r: adapter
        return self.store.request_cancellation(dispatch_id, **kwargs)

    def _request_pending(self, dispatch_id: str) -> None:
        # Record a pending cancellation WITHOUT driving (no adapter) so the monitor
        # owns the first termination attempt.
        self.store.request_cancellation(
            dispatch_id, requesting_actor_id="arch", reason="withdraw", authority="producer"
        )

    def _legacy(self, dispatch_id: str) -> None:
        # A bare recipient close_message is the LEGACY (v1) leg: a v2 trigger
        # refuses ack/close and terminalizes only via close_dispatch, so the
        # close-raced interleavings below pin the v1 transport state machine.
        with self.store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set policy_version='v1' where dispatch_id=?",
                (dispatch_id,),
            )


class RequestVsStartTest(RaceBase):
    def test_confirmed_queued_cancel_is_not_started(self) -> None:
        # cancel completion vs start claim: a confirmed queued cancel is terminal,
        # so the public start path never spawns it into in_flight.
        d = self._queued()
        self._cancel(d["dispatch_id"])
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], "cancelled")
        adapter = ConfirmingAdapter()
        self.store.start_queued_dispatches(lambda _r: adapter, limit=10)
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], "cancelled")
        self.assertEqual(adapter.halt_calls, [])

    def test_pending_claimed_cancel_is_not_started_by_ordinary_path(self) -> None:
        # request vs claim: a claimed queued row with a pending cancellation is
        # owned by the cancellation engine; the public start path skips it.
        d = self._queued()
        _claim(self.store, d["dispatch_id"])
        self._cancel(d["dispatch_id"])  # no adapter -> pending
        self.assertEqual(_observed(self.store, d["dispatch_id"])["cancellation"]["state"], "requested")
        adapter = ConfirmingAdapter()
        self.store.start_queued_dispatches(lambda _r: adapter, limit=10)
        # Not started: still queued (never a false in_flight).
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], "queued")


class RunTokenReplacementTest(RaceBase):
    def test_run_token_replaced_between_halt_and_cas_preserves_newer_run(self) -> None:
        # T3: the authenticated HALT confirms the OLD token, then a newer run
        # replaces the row's run_token before the terminal CAS. The CAS is bound
        # to the halted token, so it MISSES: the newer run is preserved and the
        # cancellation is left unconfirmed (nonterminal). No terminalization of a
        # run we did not halt.
        confirming = ConfirmingAdapter()
        started = self._in_flight(confirming)
        replacer = TokenReplacingHaltAdapter(
            self.store, started["dispatch_id"], new_token="run-token-NEWER-0002"
        )
        result = self._cancel(started["dispatch_id"], replacer)

        self.assertEqual(result["status"], "in_flight")
        self.assertEqual(result["cancellation_state"], "requested")
        self.assertIsNone(result["termination_result"])
        self.assertFalse(result["lineage_released"])
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "in_flight")  # newer run preserved, not cancelled
        obs = _observed(self.store, started["dispatch_id"])
        self.assertEqual(obs["run_token"], "run-token-NEWER-0002")  # newer token intact
        self.assertEqual(obs["cancellation"]["state"], "requested")
        self.assertTrue(replacer.halt_calls)  # a HALT was attempted against the old token

    def test_same_token_halt_still_confirms_cancelled(self) -> None:
        # Control: with NO token drift the exact same path commits cancelled, so
        # the run-token binding does not regress the ordinary confirmed halt.
        confirming = ConfirmingAdapter()
        started = self._in_flight(confirming)
        result = self._cancel(started["dispatch_id"], confirming)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["termination_result"], "supervised_halt_confirmed")


class HardTtlConfirmsCancellationTest(RaceBase):
    def test_first_halt_fails_then_ttl_halt_confirms_commits_cancelled(self) -> None:
        # T4: the pre-TTL cancellation drive's HALT fails, but the hard-TTL
        # backstop HALT (same pass) confirms the SAME pending cancellation. The
        # truthful terminal is cancelled, NOT dlq.
        adapter = FlakyHaltAdapter()
        started = self._in_flight(adapter)
        self._request_pending(started["dispatch_id"])
        _expire(self.store, started["dispatch_id"], seconds_ago=5)  # past TTL, within grace

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")  # never dlq
        self.assertIsNone(row["auth_lineage_claimed_at"])  # cap/lineage released
        obs = _observed(self.store, started["dispatch_id"])["cancellation"]
        self.assertEqual(obs["termination_result"], "supervised_halt_confirmed")
        self.assertEqual(obs["confirmed_via"], "hard_ttl_backstop")
        self.assertEqual(_transport(self.store, started["message_id"]), "cancelled")
        self.assertGreaterEqual(len(adapter.halt_calls), 2)  # failed drive + confirmed backstop

    def test_ttl_without_pending_cancellation_stays_dlq(self) -> None:
        # T4 guard: an ordinary hard-TTL death with NO pending cancellation keeps
        # the ordinary dlq behaviour (the confirmed-cancel branch never fires).
        adapter = ConfirmingAdapter()
        started = self._in_flight(adapter)
        _expire(self.store, started["dispatch_id"], seconds_ago=5)

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "dlq")
        self.assertNotIn("cancellation", _observed(self.store, started["dispatch_id"]))


class DlqResidueReprobeTest(RaceBase):
    def test_residue_reprobe_upgrades_evidence_status_stays_dlq(self) -> None:
        # T5: a hard-TTL dlq released without confirming death carries
        # ``termination_not_confirmed`` residue (not janitor-cleanable). A later
        # monitor pass ACTIVELY re-probes with the SAME run token; once the
        # supervisor confirms, ONLY the termination observation + janitor
        # eligibility upgrade -- the ledger status REMAINS dlq.
        #
        # Revision 7 F2: the confirming re-probe HALT persists the complete
        # exact-token version-1 ``$.reaper_exit`` proof DURING the halt call (as the
        # real adapter does), so ``confirmed_termination_evidence`` upgrades from
        # None to non-None on real same-run evidence -- never a bare halt return.
        # The first unreachable pass persists no proof and stays uncleanable.
        adapter = ReprobePersistingHaltAdapter(self.store, "")
        started = self._in_flight(adapter)
        adapter._dispatch_id = started["dispatch_id"]
        _expire(self.store, started["dispatch_id"], seconds_ago=HARD_TTL_KILL_GRACE_SECONDS + 2)

        # Pass 1: supervisor unreachable -> dlq residue, not cleanable yet.
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        row = _ledger(self.store, started["dispatch_id"])
        obs = _observed(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(obs["termination_result"], "termination_not_confirmed")
        self.assertIsNone(confirmed_termination_evidence(obs, adapter.run_token))
        self.assertGreaterEqual(obs["dlq_residue_reprobe"]["attempts"], 1)

        # Pass 2: supervisor reachable -> re-probe confirms, observation upgraded.
        adapter.confirm = True
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        row2 = _ledger(self.store, started["dispatch_id"])
        obs2 = _observed(self.store, started["dispatch_id"])
        self.assertEqual(row2["status"], "dlq")  # ledger status unchanged
        self.assertEqual(obs2["termination_result"], "supervised_halt_confirmed")
        self.assertIsNotNone(confirmed_termination_evidence(obs2, adapter.run_token))
        self.assertTrue(obs2["dlq_residue_reprobe"]["confirmed_at"])

    def test_residue_reprobe_has_no_lifetime_cap_then_upgrades_on_later_evidence(self) -> None:
        # T5 (corrected): a residue row stays eligible FOREVER until positive
        # same-run evidence appears -- there is no lifetime attempt cap. Repeated
        # unreachable passes keep accumulating cumulative attempt telemetry well
        # past the old bound; when a same-run exit finally appears the observation
        # upgrades and the ledger status stays dlq.
        adapter = GatedHaltAdapter()  # never confirms via halt
        started = self._in_flight(adapter)
        _expire(self.store, started["dispatch_id"], seconds_ago=HARD_TTL_KILL_GRACE_SECONDS + 2)

        passes = DLQ_RESIDUE_REPROBE_BATCH + 4
        for _ in range(passes):
            self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        obs = _observed(self.store, started["dispatch_id"])
        self.assertEqual(_ledger(self.store, started["dispatch_id"])["status"], "dlq")
        self.assertEqual(obs["termination_result"], "termination_not_confirmed")
        # No lifetime cap: attempts exceed the (former) bound and keep climbing.
        self.assertGreater(obs["dlq_residue_reprobe"]["attempts"], DLQ_RESIDUE_REPROBE_BATCH)

        # Later positive same-run evidence upgrades the observation; status stays dlq.
        with self.store._db.connection() as conn:
            row = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (started["dispatch_id"],),
            ).fetchone()
            merged = json.loads(row["observed_values_json"] or "{}")
            merged["reaper_exit"] = _complete_reaper(adapter.run_token)
            conn.execute(
                "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                (json.dumps(merged, sort_keys=True), started["dispatch_id"]),
            )
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        obs2 = _observed(self.store, started["dispatch_id"])
        self.assertEqual(_ledger(self.store, started["dispatch_id"])["status"], "dlq")
        self.assertEqual(obs2["termination_result"], "same_run_exit_confirmed")
        self.assertIsNotNone(confirmed_termination_evidence(obs2, adapter.run_token))


class BootstrapStartPathTest(RaceBase):
    def test_bootstrap_cancel_via_public_start_path_becomes_cancelled(self) -> None:
        # T5: dispatch_agent queues; the PUBLIC start path claims + spawns; the
        # cancellation lands DURING bootstrap (inside dispatch()). The spawn commit
        # settles cancelled through the public path -- never a false in_flight.
        d = self._queued("boot")
        adapter = BootstrapCancelAdapter(self.store)
        self.store.start_queued_dispatches(lambda _r: adapter, limit=10)

        row = _ledger(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")
        obs = _observed(self.store, d["dispatch_id"])["cancellation"]
        self.assertEqual(obs["termination_result"], "supervised_halt_confirmed")
        self.assertTrue(adapter.halt_calls)  # the exact orphan was halted
        self.assertEqual(_transport(self.store, d["message_id"]), "cancelled")

    def test_bootstrap_cancel_current_same_run_exit_confirms_before_early_exit(self) -> None:
        # T5: the pending cancellation PRECEDES early-exit classification. A
        # CURRENT same-run exit (this spawn's run token) confirms the cancellation
        # as same_run_exit_confirmed -- it is NOT folded into an early DLQ -- and
        # needs no HALT.
        d = self._queued("boot-exit")
        adapter = BootstrapCancelAdapter(
            self.store,
            extra_observed={"reaper_exit": _complete_reaper("run-token-race-0001")},
        )
        self.store.start_queued_dispatches(lambda _r: adapter, limit=10)

        row = _ledger(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")  # not dlq
        obs = _observed(self.store, d["dispatch_id"])["cancellation"]
        self.assertEqual(obs["termination_result"], "same_run_exit_confirmed")
        self.assertEqual(adapter.halt_calls, [])  # confirmed by exit evidence, no HALT

    def test_bootstrap_cancel_stale_exit_unconfirmed_halt_holds_pending(self) -> None:
        # T5: stale spawn-result exit evidence (a DIFFERENT run token) cannot
        # confirm the cancellation. With an unconfirmed halt the row is never
        # published in_flight and stays queued + pending for the monitor.
        d = self._queued("boot-stale")
        adapter = BootstrapCancelUnconfirmedAdapter(
            self.store,
            extra_observed={"worker_exit": {"returncode": 0, "run_token": "run-token-STALE-0000"}},
        )
        self.store.start_queued_dispatches(lambda _r: adapter, limit=10)

        row = _ledger(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "queued")  # never in_flight
        self.assertEqual(_observed(self.store, d["dispatch_id"])["cancellation"]["state"], "requested")
        self.assertTrue(adapter.halt_calls)  # stale exit did not confirm -> HALT attempted


class SpawnFailureBootstrapTest(RaceBase):
    def test_spawn_failure_with_pending_cancel_settles_not_started(self) -> None:
        # queued/bootstrap vs spawn failure through the public start path: no
        # runtime started -> the requested cancellation settles not_started, NOT
        # spawn_failed_message_landed.
        d = self._queued("boot-fail")

        class FailingBootstrapCancelAdapter(BootstrapCancelAdapter):
            def dispatch(self, context: DispatchContext) -> DispatchStart:
                self._bootstrap_pending(context.dispatch["dispatch_id"])
                raise RuntimeError("boom: spawn failed before any runtime started")

        adapter = FailingBootstrapCancelAdapter(self.store)
        self.store.start_queued_dispatches(lambda _r: adapter, limit=10)

        row = _ledger(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(
            _observed(self.store, d["dispatch_id"])["cancellation"]["termination_result"], "not_started"
        )
        self.assertEqual(_transport(self.store, d["message_id"]), "cancelled")


class InflightCloseAckRaceTest(RaceBase):
    def test_recipient_close_first_remains_closed(self) -> None:
        # in-flight cancel vs recipient close: the close committed first flips the
        # in_flight ledger to closed in lockstep; a later cancel refuses (never
        # relabels a closed row).
        adapter = ConfirmingAdapter()
        started = self._in_flight(adapter)
        self._legacy(started["dispatch_id"])
        self.store.send_message("wrk", ["arch"], "re", "done", [], parent_message_id=started["message_id"])
        self.store.close_message("wrk", started["message_id"], "")
        self.assertEqual(_ledger(self.store, started["dispatch_id"])["status"], "closed")
        with self.assertRaises(CancellationStateError):
            self._cancel(started["dispatch_id"], adapter)
        self.assertEqual(_ledger(self.store, started["dispatch_id"])["status"], "closed")

    def test_confirmed_cancel_then_late_close_cannot_resurrect(self) -> None:
        # terminal cancel vs late worker close: once cancelled, a late close is
        # refused and cannot rewrite the withdrawn transport copy.
        adapter = ConfirmingAdapter()
        started = self._in_flight(adapter)
        self._cancel(started["dispatch_id"], adapter)
        self.assertEqual(_ledger(self.store, started["dispatch_id"])["status"], "cancelled")
        self.assertEqual(_transport(self.store, started["message_id"]), "cancelled")
        with contextlib.suppress(Exception):
            self.store.close_message("wrk", started["message_id"], "late")
        self.assertEqual(_transport(self.store, started["message_id"]), "cancelled")
        self.assertEqual(_ledger(self.store, started["dispatch_id"])["status"], "cancelled")

    def test_transport_closed_first_projects_close_first_variant(self) -> None:
        # cancel vs transport close on a queued row: the recipient close advanced
        # only the transport (the queued ledger did not flip), then the confirmed
        # cancel commits ledger cancelled while transport stays closed -> the
        # distinct confirmed_cancel_transport_closed_first projection.
        d = self._queued()
        self._legacy(d["dispatch_id"])
        self.store.send_message("wrk", ["arch"], "re", "done", [], parent_message_id=d["message_id"])
        self.store.close_message("wrk", d["message_id"], "")
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], "queued")
        self.assertEqual(_transport(self.store, d["message_id"]), "closed")
        self._cancel(d["dispatch_id"])
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], "cancelled")
        self.assertEqual(_transport(self.store, d["message_id"]), "closed")
        self.assertEqual(
            self.store.project_dispatch(d["dispatch_id"])["outcome"],
            "confirmed_cancel_transport_closed_first",
        )


class TerminalDlqRaceTest(RaceBase):
    def test_early_dlq_first_remains_dlq_and_cancel_refuses(self) -> None:
        # cancel vs early-exit DLQ (controlled interleaving: a concurrent early DLQ
        # committed first via a real same-run worker_exit reconcile). A later cancel
        # never relabels the dlq row.
        adapter = ConfirmingAdapter()
        started = self._in_flight(adapter)
        # A same-run worker exit lands; the monitor early-DLQs the row first.
        with self.store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set observed_values_json = json_set("
                "coalesce(nullif(observed_values_json,''),'{}'), '$.worker_exit', json(?)) "
                "where dispatch_id = ?",
                (json.dumps({"returncode": 0, "run_token": adapter.run_token}), started["dispatch_id"]),
            )
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        self.assertEqual(_ledger(self.store, started["dispatch_id"])["status"], "dlq")
        with self.assertRaises(CancellationStateError):
            self._cancel(started["dispatch_id"], adapter)
        self.assertEqual(_ledger(self.store, started["dispatch_id"])["status"], "dlq")


class ProducerVsAdminTest(RaceBase):
    def test_admin_request_after_producer_pending_conflicts(self) -> None:
        # producer vs admin request: a different authority cannot silently
        # overwrite an existing pending request.
        d = self._queued()
        _claim(self.store, d["dispatch_id"])
        self._cancel(d["dispatch_id"], reason="same")  # producer pending
        with self.assertRaises(CancellationConflictError):
            self._cancel(d["dispatch_id"], reason="same", authority="admin", actor=HUMAN_ID)
        self.assertEqual(_observed(self.store, d["dispatch_id"])["cancellation"]["authority"], "producer")


class DuplicateMonitorPassTest(RaceBase):
    def test_duplicate_monitor_passes_stable_terminal_no_aba(self) -> None:
        # controlled duplicate monitor passes over a confirmed cancel: a stable
        # terminal winner with NO observed-value loss and NO terminal-to-live
        # transition.
        adapter = ConfirmingAdapter()
        started = self._in_flight(adapter)
        self._request_pending(started["dispatch_id"])

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        first = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(first["status"], "cancelled")
        cancelled_at = first["cancelled_at"]
        observed_first = _observed(self.store, started["dispatch_id"])

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        second = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(second["status"], "cancelled")  # never terminal -> live
        self.assertEqual(second["cancelled_at"], cancelled_at)  # stable winner
        self.assertEqual(_observed(self.store, started["dispatch_id"]), observed_first)  # no loss

    def test_duplicate_residue_reprobe_is_idempotent(self) -> None:
        # a second re-probe pass after an evidence upgrade neither re-writes the
        # upgraded observation nor resurrects the dlq row.
        adapter = GatedHaltAdapter()
        started = self._in_flight(adapter)
        _expire(self.store, started["dispatch_id"], seconds_ago=HARD_TTL_KILL_GRACE_SECONDS + 2)
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)  # -> residue
        adapter.confirm = True
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)  # -> upgraded
        upgraded = _observed(self.store, started["dispatch_id"])
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)  # duplicate
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(_observed(self.store, started["dispatch_id"]), upgraded)  # stable


class TtlHaltTokenReplacer(ConfirmingAdapter):
    """Pre-TTL drive HALT is unconfirmed; the hard-TTL HALT authenticates the OLD
    token but a newer run replaces it (``new_token``) or clears it
    (``new_token=None``) before the terminal CAS re-reads. The hard-TTL
    confirmed-cancel commit is bound to the OLD token by equality, so it must miss
    (A->B / A->NULL) and never terminalize a run it did not authenticate.
    """

    def __init__(self, store: Store, dispatch_id: str, new_token: str | None) -> None:
        super().__init__()
        self._store = store
        self._dispatch_id = dispatch_id
        self._new_token = new_token

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        if len(self.halt_calls) == 1:
            # Pre-TTL cancellation drive: leave the row pending so the hard-TTL
            # backstop owns the confirmed-cancel attempt.
            raise RuntimeError("pre-TTL drive halt unconfirmed")
        with self._store._db.connection() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (self._dispatch_id,),
            ).fetchone()
            observed = json.loads(row["observed_values_json"] or "{}")
            if self._new_token is None:
                observed.pop("run_token", None)
            else:
                observed["run_token"] = self._new_token
            conn.execute(
                "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                (json.dumps(observed, sort_keys=True), self._dispatch_id),
            )
            conn.commit()


def _make_residue(store: Store, dispatch_id: str, token: str) -> None:
    observed = {
        "control_socket": "/nonexistent/agent-comms/run/s/control.sock",
        "run_token": token,
        "termination_result": "termination_not_confirmed",
    }
    with store._db.connection() as conn:
        conn.execute(
            "update dispatch_ledger set status = 'dlq', dlq_at = ?, spawn_handle = ?, "
            "observed_values_json = ? where dispatch_id = ?",
            (
                "2026-07-15T00:00:00+00:00",
                f"stub:{dispatch_id}:1",
                json.dumps(observed, sort_keys=True),
                dispatch_id,
            ),
        )


class ExactTokenEqualityTest(RaceBase):
    def test_normal_inflight_cancel_a_to_null_misses_preserves_row(self) -> None:
        # normal A->NULL: the authenticated HALT confirms, then the row's run token
        # is cleared before the terminal CAS. Exact-token means equality only: a
        # missing token is never equal to the halted token, so the CAS misses and
        # the row stays in_flight + pending (never cancelled).
        confirming = ConfirmingAdapter()
        started = self._in_flight(confirming)
        nuller = TokenNullingHaltAdapter(self.store, started["dispatch_id"])
        result = self._cancel(started["dispatch_id"], nuller)

        self.assertEqual(result["status"], "in_flight")
        self.assertEqual(result["cancellation_state"], "requested")
        self.assertFalse(result["lineage_released"])
        obs = _observed(self.store, started["dispatch_id"])
        self.assertNotIn("run_token", obs)  # token gone; never treated as equal
        self.assertEqual(obs["cancellation"]["state"], "requested")
        self.assertTrue(nuller.halt_calls)

    def test_ttl_confirmed_cancel_a_to_b_misses_preserves_newer_run(self) -> None:
        # TTL A->B: the pre-TTL drive is unconfirmed; the hard-TTL HALT authenticates
        # the OLD token but a newer run replaces it before the terminal CAS. The
        # confirmed-cancel commit binds the OLD token, so it misses -> the newer run
        # is preserved and the row is NOT cancelled.
        adapter = TtlHaltTokenReplacer(self.store, "", new_token="run-token-NEWER-TTL")
        started = self._in_flight(adapter)
        adapter._dispatch_id = started["dispatch_id"]
        self._request_pending(started["dispatch_id"])
        _expire(self.store, started["dispatch_id"], seconds_ago=HARD_TTL_KILL_GRACE_SECONDS + 2)

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        row = _ledger(self.store, started["dispatch_id"])
        self.assertNotEqual(row["status"], "cancelled")  # newer run preserved
        obs = _observed(self.store, started["dispatch_id"])
        self.assertEqual(obs["run_token"], "run-token-NEWER-TTL")
        self.assertEqual(obs["cancellation"]["state"], "requested")

    def test_ttl_confirmed_cancel_a_to_null_misses_preserves_row(self) -> None:
        # TTL A->NULL: the hard-TTL HALT authenticates the OLD token but it is
        # cleared before the terminal CAS. A missing token is never equal, so the
        # confirmed-cancel commit misses and the row is NOT cancelled.
        adapter = TtlHaltTokenReplacer(self.store, "", new_token=None)
        started = self._in_flight(adapter)
        adapter._dispatch_id = started["dispatch_id"]
        self._request_pending(started["dispatch_id"])
        _expire(self.store, started["dispatch_id"], seconds_ago=HARD_TTL_KILL_GRACE_SECONDS + 2)

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        row = _ledger(self.store, started["dispatch_id"])
        self.assertNotEqual(row["status"], "cancelled")
        obs = _observed(self.store, started["dispatch_id"])
        self.assertNotIn("run_token", obs)
        self.assertEqual(obs["cancellation"]["state"], "requested")


class SpawnExactTokenTest(RaceBase):
    def test_spawn_settlement_a_to_b_identity_claim_misses_preserves_newer_run(self) -> None:
        # spawn A->B: a cancellation raced a successful spawn; the settlement HALT
        # authenticated the spawn token, but a newer run stamped a different token on
        # the queued row before the exact identity claim. The claim (NULL or exact
        # spawn token) misses a different token -> the newer run is preserved and the
        # row stays queued + pending (never cancelled).
        d = self._queued("spawn-drift")
        adapter = BootstrapHaltRowTokenMutator(
            self.store, d["dispatch_id"], new_token="run-token-NEWER-SPAWN"
        )
        self.store.start_queued_dispatches(lambda _r: adapter, limit=10)

        row = _ledger(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "queued")  # newer run preserved, not cancelled
        obs = _observed(self.store, d["dispatch_id"])
        self.assertEqual(obs["run_token"], "run-token-NEWER-SPAWN")
        self.assertEqual(obs["cancellation"]["state"], "requested")
        self.assertTrue(adapter.halt_calls)

    def test_spawn_settlement_missing_token_never_terminalizes(self) -> None:
        # spawn never-supplied-A: the spawn result carries NO run token at all, so a
        # confirmed HALT cannot bind an exact identity. The pre-HALT identity claim
        # has nothing to claim, so the terminal HALT-derived commit never runs and
        # never treats a missing token as equal: the row stays queued + pending,
        # never cancelled. (A DISTINCT case from the genuine spawn A->NULL below,
        # which DID supply A and then cleared it during the halt.)
        d = self._queued("spawn-notoken")
        adapter = BootstrapNoRunTokenAdapter(self.store)
        self.store.start_queued_dispatches(lambda _r: adapter, limit=10)

        row = _ledger(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "queued")
        obs = _observed(self.store, d["dispatch_id"])
        self.assertEqual(obs["cancellation"]["state"], "requested")
        self.assertNotIn("run_token", obs)

    def test_spawn_settlement_a_to_null_post_halt_clear_preserves_row(self) -> None:
        # GENUINE spawn A->NULL: the spawn result DID supply run token A (claimed via
        # the pre-HALT exact-state CAS), then the authenticated HALT clears the row's
        # run token before the terminal commit. Exact-token means equality only: a
        # missing token is never equal to the claimed token, so the terminal CAS
        # misses. The cleared token is NEVER restored from the stale spawn result and
        # the row is preserved queued + pending, never cancelled. This reproduces
        # A->NULL with the post-HALT token mutator (a real spawn that supplied A),
        # NOT a spawn result that never supplied A.
        d = self._queued("spawn-anull")
        adapter = BootstrapHaltRowTokenMutator(self.store, d["dispatch_id"], new_token=None)
        self.store.start_queued_dispatches(lambda _r: adapter, limit=10)

        row = _ledger(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "queued")  # cleared token preserved, not cancelled
        obs = _observed(self.store, d["dispatch_id"])
        self.assertNotIn("run_token", obs)  # A->NULL: missing token never restored
        self.assertEqual(obs["cancellation"]["state"], "requested")
        self.assertTrue(adapter.halt_calls)  # spawn supplied A, the exact orphan was HALTed

    def test_spawn_settlement_current_sql_exit_wins_over_stale_spawn_result(self) -> None:
        # Correction 5: current persisted SQL evidence is authoritative. The row
        # holds a MATCHING same-run worker_exit; the spawn result echoes a stale
        # conflicting exit. The stale spawn-result exit must never mask current SQL:
        # the cancellation confirms same_run_exit_confirmed with NO HALT.
        d = self._queued("spawn-evidence")
        adapter = SpawnResultStaleExitAdapter(
            self.store,
            current_exit_token="run-token-race-0001",
            stale_exit_token="run-token-STALE-9999",
        )
        self.store.start_queued_dispatches(lambda _r: adapter, limit=10)

        row = _ledger(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")
        obs = _observed(self.store, d["dispatch_id"])["cancellation"]
        self.assertEqual(obs["termination_result"], "same_run_exit_confirmed")
        self.assertEqual(adapter.halt_calls, [])  # current SQL exit confirmed, no HALT


class ResidueProbeTokenDriftTest(RaceBase):
    def test_unconfirmed_residue_probe_token_drift_applies_nothing(self) -> None:
        # Correction 2: the residue CAS is bound to the PROBED token. If the row's
        # token drifts before the CAS re-reads, an unconfirmed probe applies NO
        # attempt telemetry and no evidence -- the residue is preserved untouched.
        d = self._queued("res-drift-unconf")
        _make_residue(self.store, d["dispatch_id"], "run-token-residue-A")
        adapter = ResidueTokenMutatingHaltAdapter(
            self.store, d["dispatch_id"], "run-token-DRIFT-B", confirm=False
        )
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        obs = _observed(self.store, d["dispatch_id"])
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], "dlq")
        self.assertEqual(obs["termination_result"], "termination_not_confirmed")
        self.assertEqual(obs["run_token"], "run-token-DRIFT-B")  # drifted token intact
        self.assertNotIn("dlq_residue_reprobe", obs)  # no stale telemetry applied

    def test_confirmed_residue_probe_token_drift_does_not_upgrade(self) -> None:
        # Correction 2: even a CONFIRMED probe must not upgrade the observation when
        # the row's token drifted from the probed token -- the confirmation
        # authenticated a different run than the row now carries.
        d = self._queued("res-drift-conf")
        _make_residue(self.store, d["dispatch_id"], "run-token-residue-A")
        adapter = ResidueTokenMutatingHaltAdapter(
            self.store, d["dispatch_id"], "run-token-DRIFT-B", confirm=True
        )
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        obs = _observed(self.store, d["dispatch_id"])
        self.assertEqual(_ledger(self.store, d["dispatch_id"])["status"], "dlq")
        self.assertEqual(obs["termination_result"], "termination_not_confirmed")  # not upgraded
        self.assertEqual(obs["run_token"], "run-token-DRIFT-B")
        self.assertNotIn("dlq_residue_reprobe", obs)


class GenuinelyInterleavedMonitorTest(RaceBase):
    def test_interleaved_second_monitor_execution_first_committer_wins(self) -> None:
        # A GENUINELY interleaved duplicate monitor execution (not two sequential
        # calls after a terminal commit): a second full monitor pass runs DURING the
        # first pass's HALT, before the first pass's terminal CAS, and commits the
        # cancellation. The first pass then re-reads a row another execution already
        # terminalized -> first-committer wins with no double commit, no observed
        # loss, and no terminal-to-live transition. The inner winner's COMPLETE
        # persisted state is captured the instant it commits, before the outer loser
        # resumes, and the final state must be byte/equality-identical to it.
        confirming = ConfirmingAdapter()
        started = self._in_flight(confirming)
        self._request_pending(started["dispatch_id"])

        # Seed an UNRELATED observed-values sentinel before the race. A confirmed
        # cancel rewrites the observed values, so a survivor proves the terminal
        # commit preserved unrelated evidence rather than clobbering it.
        sentinel_key = "unrelated_sentinel_marker"
        sentinel_value = "keep-me-0xC0FFEE"
        with self.store._db.connection() as conn:
            conn.execute("begin immediate")
            row0 = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (started["dispatch_id"],),
            ).fetchone()
            merged = json.loads(row0["observed_values_json"] or "{}")
            merged[sentinel_key] = sentinel_value
            conn.execute(
                "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                (json.dumps(merged, sort_keys=True), started["dispatch_id"]),
            )
            conn.commit()

        baseline_counts = _message_counts(self.store)
        adapter = ReentrantMonitorHaltAdapter(
            self.store, HUMAN_ID, started["dispatch_id"], started["message_id"]
        )

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        # The inner winner committed the terminal cancel and its state was captured
        # BEFORE the outer loser resumed.
        self.assertEqual(len(adapter.halt_calls), 1)  # the outer pass reentered exactly once
        inner = adapter.inner_state
        self.assertIsNotNone(inner)
        self.assertEqual(inner["status"], "cancelled")
        inner_obs = json.loads(inner["observed_values_json"])
        self.assertEqual(inner_obs["cancellation"]["state"], "confirmed")
        self.assertEqual(
            inner_obs["cancellation"]["termination_result"], "supervised_halt_confirmed"
        )
        self.assertEqual(inner_obs[sentinel_key], sentinel_value)  # sentinel captured intact
        self.assertIsNone(inner["auth_lineage_claimed_at"])  # lineage released once
        self.assertEqual(inner["transport"], "cancelled")

        # Final state after the outer loser resumed and ran its (losing) CAS must be
        # BYTE/EQUALITY-identical to the captured inner winner: the loser wrote
        # nothing -- no double commit, no observed loss, no terminal-to-live churn.
        final = _full_state(self.store, started["dispatch_id"], started["message_id"])
        self.assertEqual(final, inner)
        self.assertEqual(final["observed_values_json"], inner["observed_values_json"])  # byte-identity

        # Only ONE terminal transition / claim / message occurred: the loser sent no
        # duplicate page or transport message (counts unchanged since the inner
        # commit, and no new message beyond the transport withdrawal at baseline).
        self.assertEqual(_message_counts(self.store), adapter.inner_message_counts)
        self.assertEqual(_message_counts(self.store), baseline_counts)

        # The unrelated sentinel survived the terminal commit.
        final_obs = _observed(self.store, started["dispatch_id"])
        self.assertEqual(final_obs[sentinel_key], sentinel_value)
        self.assertEqual(final_obs["cancellation"]["state"], "confirmed")

        # A later monitor pass remains a NO-OP: no terminal-state regression and the
        # full persisted state is still byte-identical to the inner winner.
        self.store.reconcile_dispatches(lambda _r: ConfirmingAdapter(), human_actor_id=HUMAN_ID)
        later = _full_state(self.store, started["dispatch_id"], started["message_id"])
        self.assertEqual(later, inner)
        self.assertEqual(_message_counts(self.store), baseline_counts)


class SpawnPreHaltClaimStateDriftTest(unittest.TestCase):
    """Requirement 1: the spawn-time pre-HALT identity claim must ALSO require the
    persisted cancellation is still ``requested``. A cancellation withdrawn or
    confirmed elsewhere between the spawn-commit re-read and the pre-HALT claim
    makes the claim LOSE (like a token drift): the HALT is never attempted and the
    changed row is preserved, never restoring the request.
    """

    def _seeded_store(self) -> Store:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = Store(root / "agent-comms.sqlite")
        _seed(store, root)
        return store

    def _install_drift_ledger(self, store: Store, drift_state: str | None) -> DispatchLedger:
        # Swap the Store's composed ledger for the drift-injecting subclass; the
        # public Store entry points then route spawn/cancel through it.
        ledger = PreHaltClaimStateDriftLedger(
            store._db, store._actors, store._mailbox, drift_state=drift_state
        )
        store._dispatch = ledger
        return ledger

    def test_state_change_before_pre_halt_claim_loses_and_halt_never_called(self) -> None:
        # A concurrent writer flips the cancellation to 'confirmed' (status left
        # queued, token left claimable) in the exact pre-claim window. ONLY the new
        # requested-state predicate can make the claim miss -> no HALT, row preserved.
        store = self._seeded_store()
        ledger = self._install_drift_ledger(store, drift_state="confirmed")
        d = store.dispatch_agent("arch", "wrk", "preclaim-change", "S", "B", [])
        adapter = BootstrapCancelAdapter(store)
        store.start_queued_dispatches(lambda _r: adapter, limit=1)

        self.assertEqual(ledger._pre_halt_claim_calls, 1)  # the pre-HALT claim ran once
        self.assertEqual(adapter.halt_calls, [])  # claim lost -> HALT never attempted
        row = _ledger(store, d["dispatch_id"])
        self.assertEqual(row["status"], "queued")  # never a false in_flight/cancelled
        obs = _observed(store, d["dispatch_id"])
        self.assertEqual(obs["cancellation"]["state"], "confirmed")  # drift not restored
        self.assertNotIn("run_token", obs)  # claim missed -> no token stamped
        # The losing settlement preserved the winning (drifted) row byte-for-byte.
        self.assertEqual(row["observed_values_json"], ledger._drifted_observed_json)

    def test_state_removed_before_pre_halt_claim_loses_and_halt_never_called(self) -> None:
        # Same window, but the concurrent writer REMOVES the cancellation object.
        store = self._seeded_store()
        ledger = self._install_drift_ledger(store, drift_state=None)
        d = store.dispatch_agent("arch", "wrk", "preclaim-remove", "S", "B", [])
        adapter = BootstrapCancelAdapter(store)
        store.start_queued_dispatches(lambda _r: adapter, limit=1)

        self.assertEqual(ledger._pre_halt_claim_calls, 1)
        self.assertEqual(adapter.halt_calls, [])  # claim lost -> HALT never attempted
        row = _ledger(store, d["dispatch_id"])
        self.assertEqual(row["status"], "queued")
        obs = _observed(store, d["dispatch_id"])
        self.assertNotIn("cancellation", obs)  # removed request never restored
        self.assertNotIn("run_token", obs)
        self.assertEqual(row["observed_values_json"], ledger._drifted_observed_json)

    def test_requested_state_pre_halt_claim_happy_path_confirms(self) -> None:
        # Control (plain ledger, no drift): the pre-HALT claim still succeeds when the
        # state stays 'requested', so the exact orphan is HALTed and the row confirms.
        store = self._seeded_store()
        d = store.dispatch_agent("arch", "wrk", "preclaim-happy", "S", "B", [])
        adapter = BootstrapCancelAdapter(store)
        store.start_queued_dispatches(lambda _r: adapter, limit=10)

        row = _ledger(store, d["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")
        obs = _observed(store, d["dispatch_id"])["cancellation"]
        self.assertEqual(obs["termination_result"], "supervised_halt_confirmed")
        self.assertTrue(adapter.halt_calls)  # claim succeeded -> exact orphan HALTed


class TerminalCasCancellationStateDriftTest(RaceBase):
    """Requirement 2: EVERY confirmed-cancellation terminal CAS -- spawn, normal,
    and TTL -- must also require the persisted cancellation stays ``requested``. A
    cancellation-state change during the authenticated HALT must make the terminal
    CAS lose exactly like a token drift: the changed row is preserved and never a
    false ``cancelled``. Requirement 3: the losing settlement never restores the
    request state.
    """

    def test_normal_inflight_state_change_during_halt_cas_misses_preserves_row(self) -> None:
        # normal drive: the HALT confirms but the cancellation flips to 'confirmed'
        # (token untouched) before the terminal CAS. ONLY the requested-state
        # predicate makes the confirmed-cancel commit miss -> row stays in_flight.
        confirming = ConfirmingAdapter()
        started = self._in_flight(confirming)
        drifter = StateDriftingHaltAdapter(self.store, started["dispatch_id"], new_state="confirmed")
        result = self._cancel(started["dispatch_id"], drifter)

        self.assertEqual(result["status"], "in_flight")  # not a false cancelled
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "in_flight")
        obs = _observed(self.store, started["dispatch_id"])
        self.assertEqual(obs["cancellation"]["state"], "confirmed")  # drift not restored
        self.assertEqual(obs["run_token"], confirming.run_token)  # only state drifted
        self.assertTrue(drifter.halt_calls)  # a confirming HALT ran before the losing CAS

    def test_normal_inflight_state_removed_during_halt_cas_misses_preserves_row(self) -> None:
        # Same normal path but the cancellation is REMOVED during the HALT.
        confirming = ConfirmingAdapter()
        started = self._in_flight(confirming)
        drifter = StateDriftingHaltAdapter(self.store, started["dispatch_id"], new_state=None)
        result = self._cancel(started["dispatch_id"], drifter)

        self.assertEqual(result["status"], "in_flight")
        self.assertFalse(result["lineage_released"])
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "in_flight")
        obs = _observed(self.store, started["dispatch_id"])
        self.assertNotIn("cancellation", obs)  # removed request never restored
        self.assertTrue(drifter.halt_calls)

    def test_spawn_state_change_during_halt_terminal_cas_misses_preserves_row(self) -> None:
        # spawn settlement: the pre-HALT claim succeeds (state was 'requested' ->
        # token stamped, HALT attempted), then the state flips to 'confirmed' during
        # the HALT. The terminal confirmed-cancel CAS and the pending diagnostic both
        # miss on the requested-state predicate -> the row is preserved queued.
        d = self._queued("spawn-state-drift")
        adapter = BootstrapHaltStateMutator(self.store, d["dispatch_id"], new_state="confirmed")
        self.store.start_queued_dispatches(lambda _r: adapter, limit=10)

        row = _ledger(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "queued")  # not a false cancelled
        obs = _observed(self.store, d["dispatch_id"])
        self.assertEqual(obs["cancellation"]["state"], "confirmed")  # drift not restored
        self.assertEqual(obs["run_token"], adapter.run_token)  # pre-HALT-claimed token preserved
        self.assertTrue(adapter.halt_calls)  # claim succeeded -> the exact orphan was HALTed

    def test_ttl_state_change_during_halt_confirmed_cancel_does_not_fire(self) -> None:
        # hard-TTL backstop: the pre-TTL drive HALT is unconfirmed; the backstop HALT
        # confirms but the cancellation flips to 'confirmed' first. The confirmed-
        # cancel requires 'requested', so it does NOT fire -> the row settles through
        # ordinary TTL (dlq), never a false cancelled, and the drift is preserved.
        adapter = TtlHaltStateMutator(self.store, "", new_state="confirmed")
        started = self._in_flight(adapter)
        adapter._dispatch_id = started["dispatch_id"]
        self._request_pending(started["dispatch_id"])
        _expire(self.store, started["dispatch_id"], seconds_ago=HARD_TTL_KILL_GRACE_SECONDS + 2)

        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)

        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "dlq")  # ordinary TTL, never a false cancelled
        self.assertNotEqual(row["status"], "cancelled")
        obs = _observed(self.store, started["dispatch_id"])
        self.assertEqual(obs["cancellation"]["state"], "confirmed")  # drift preserved
        self.assertGreaterEqual(len(adapter.halt_calls), 2)  # failed drive + confirmed backstop

    def test_requested_state_normal_happy_path_confirms_cancelled(self) -> None:
        # Control: with the state left 'requested' the normal drive confirms.
        confirming = ConfirmingAdapter()
        started = self._in_flight(confirming)
        result = self._cancel(started["dispatch_id"], confirming)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["termination_result"], "supervised_halt_confirmed")

    def test_requested_state_spawn_happy_path_confirms_cancelled(self) -> None:
        # Control: spawn-time settlement confirms when the state stays 'requested'.
        d = self._queued("spawn-happy")
        adapter = BootstrapCancelAdapter(self.store)
        self.store.start_queued_dispatches(lambda _r: adapter, limit=10)
        row = _ledger(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(
            _observed(self.store, d["dispatch_id"])["cancellation"]["termination_result"],
            "supervised_halt_confirmed",
        )

    def test_requested_state_ttl_happy_path_confirms_cancelled(self) -> None:
        # Control: the hard-TTL backstop confirms when the state stays 'requested'.
        adapter = FlakyHaltAdapter()
        started = self._in_flight(adapter)
        self._request_pending(started["dispatch_id"])
        _expire(self.store, started["dispatch_id"], seconds_ago=5)
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(
            _observed(self.store, started["dispatch_id"])["cancellation"]["confirmed_via"],
            "hard_ttl_backstop",
        )


class RevisionSevenCompleteProofStableWinnerTest(RaceBase):
    """Revision 7 F2 (red-first): the COMPLETE exact version-1 ``$.reaper_exit``
    proof -- and only it -- terminalizes a same-run cancellation exactly once with
    a stable terminal winner; a structurally invalid near-complete proof (here,
    ``proof_version`` is the bool ``True`` rather than the integer ``1``) can never
    win the terminal CAS. Red against the current predicate, which confirms from
    any same-run ``reaper_exit`` object with a matching token regardless of shape.
    """

    def _inject_observed(self, dispatch_id: str, extra: dict) -> None:
        with self.store._db.connection() as conn:
            row = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
            observed = json.loads(row["observed_values_json"] or "{}")
            observed.update(extra)
            conn.execute(
                "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                (json.dumps(observed, sort_keys=True), dispatch_id),
            )

    @staticmethod
    def _complete_proof(token: str) -> dict:
        return {
            "proof_version": 1,
            "run_token": token,
            "returncode": 0,
            "source": "halt_finalize",
            "reaped_at": "2026-07-26T00:00:00+00:00",
            "registered_wrapper_reaped": True,
            "native_process_group_drained": True,
            "owned_artifacts_absent": {"run_dir": True, "control_socket": True, "zdotdir_parent": True},
        }

    def test_complete_exact_same_run_proof_terminalizes_once_with_stable_winner(self) -> None:
        # Arm A: the complete exact version-1 proof terminalizes once, and an
        # idempotent replay returns the SAME stable terminal winner.
        good = ConfirmingAdapter()
        first = self._in_flight(good, key="complete")
        self._inject_observed(first["dispatch_id"], {"reaper_exit": self._complete_proof(good.run_token)})
        result = self._cancel(first["dispatch_id"], adapter=good)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["termination_result"], "same_run_exit_confirmed")
        replay = self._cancel(first["dispatch_id"], adapter=good)
        self.assertEqual(replay["status"], "cancelled")  # stable terminal winner on replay

        # Arm B (red driver): a proof whose ``proof_version`` is the bool ``True``
        # (not the integer ``1``) is structurally invalid and must NEVER win the
        # terminal CAS; the request stays pending with cap/lineage held.
        bad = UnconfirmedAdapter()
        second = self._in_flight(bad, key="badshape")
        invalid = self._complete_proof(bad.run_token)
        invalid["proof_version"] = True  # bool, not the integer 1
        self._inject_observed(second["dispatch_id"], {"reaper_exit": invalid})
        outcome = self._cancel(second["dispatch_id"], adapter=bad)
        self.assertEqual(outcome["status"], "in_flight")
        self.assertEqual(outcome["cancellation_state"], "requested")
        self.assertEqual(_ledger(self.store, second["dispatch_id"])["status"], "in_flight")


class ReapLandsDuringLivenessAdapter(ConfirmingAdapter):
    """HALT raises ENOENT (the control socket already unlinked by the concurrent
    background reap); the liveness STATUS probe of the SAME reconcile pass then
    lands the reap evidence on the row before the liveness CAS re-reads it.

    Models the measured F7 Claude interleaving
    (dead-worker-terminalization-stage2-003): the pending-cancellation drive
    fails to confirm (ENOENT, no proof on the row yet), the background reap
    persists its evidence, and ordinary pre-TTL liveness reconciliation is the
    first writer to see the completed proof.
    """

    def __init__(self, store: Store, evidence: dict) -> None:
        super().__init__()
        self._store = store
        self._evidence = evidence
        self.dispatch_id: str | None = None
        self.status_calls = 0

    def halt(self, spawn_handle, observed_values=None) -> None:
        self.halt_calls.append((spawn_handle, observed_values))
        raise FileNotFoundError(2, "No such file or directory")

    def status(self, spawn_handle, observed_values=None) -> _Status:
        self.status_calls += 1
        if self.status_calls == 1 and self.dispatch_id:
            with self._store._db.connection() as conn:
                row = conn.execute(
                    "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                    (self.dispatch_id,),
                ).fetchone()
                observed = json.loads(row["observed_values_json"] or "{}")
                observed.update(self._evidence)
                conn.execute(
                    "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                    (json.dumps(observed, sort_keys=True), self.dispatch_id),
                )
        return _Status("running", "d")


class LivenessConsumesPendingCancellationProofTest(RaceBase):
    """Measured F7 race (stage2-003, Claude packet): a pending cancellation's
    HALT fails with ENOENT because the worker is already being background-reaped;
    the complete exact-token v1 reaper proof then lands on the row; ordinary
    pre-TTL liveness reconciliation reaches the row FIRST. The sanctioned
    cancellation must win the terminal commit (``cancelled`` /
    ``same_run_exit_confirmed``), never the ``worker_exited_before_close`` DLQ.
    When the same-run exit is visible but the complete proof has NOT landed yet
    (the second measured interleaving: both family packets show the proof
    arriving moments after the liveness pass), the row HOLDS nonterminal
    in_flight with the cancellation still requested so a later pass can consume
    the completed proof; incomplete or wrong-token proof never falsely confirms,
    and without a pending cancellation the same evidence keeps the ordinary
    early DLQ.
    """

    TOKEN = "run-token-race-0001"  # ConfirmingAdapter's published run token

    def _race(self, evidence: dict, *, pending: bool = True):
        adapter = ReapLandsDuringLivenessAdapter(self.store, evidence)
        started = self._in_flight(adapter)
        adapter.dispatch_id = started["dispatch_id"]
        if pending:
            self._request_pending(started["dispatch_id"])
        actions = self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        return adapter, started, actions

    def _same_run_worker_exit(self) -> dict:
        return {
            "run_token": self.TOKEN,
            "returncode": 143,
            "source": "halt",
            "exited_at": "2026-07-28T12:30:41+00:00",
        }

    def test_pending_cancellation_with_complete_proof_wins_over_early_dlq(self) -> None:
        adapter, started, actions = self._race(
            {
                "reaper_exit": _complete_reaper(self.TOKEN, returncode=143),
                "worker_exit": self._same_run_worker_exit(),
            }
        )
        # The pending drive attempted its HALT first and could not confirm.
        self.assertEqual(len(adapter.halt_calls), 1)
        self.assertEqual(
            [a["status"] for a in actions if a.get("status", "").startswith("cancellation_")],
            ["cancellation_pending"],
        )
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")  # never worker_exited_before_close dlq
        self.assertIsNone(row["dlq_at"])
        self.assertIsNone(row["failure_reason"])
        self.assertIsNotNone(row["cancelled_at"])
        self.assertIsNone(row["auth_lineage_claimed_at"])  # cap/lineage released
        cancellation = _observed(self.store, started["dispatch_id"])["cancellation"]
        self.assertEqual(cancellation["state"], "confirmed")
        self.assertEqual(cancellation["termination_result"], "same_run_exit_confirmed")
        self.assertEqual(cancellation["confirmed_via"], "liveness_reconciliation")
        self.assertEqual(
            cancellation["partial_evidence"]["classification"], "cancellation_partial_work"
        )
        # The unconfirmed ENOENT attempt stays truthfully recorded on the object.
        self.assertEqual(cancellation["attempts"], 1)
        self.assertIn("No such file or directory", cancellation["latest_detail"])
        self.assertEqual(_transport(self.store, started["message_id"]), "cancelled")
        terminal_actions = [
            a
            for a in actions
            if a.get("dispatch_id") == started["dispatch_id"]
            and a.get("status") in ("dlq", "cancelled")
        ]
        self.assertEqual([a["status"] for a in terminal_actions], ["cancelled"])

        # A duplicate pass is idempotent: stable terminal winner, no value loss.
        stable = _full_state(self.store, started["dispatch_id"], started["message_id"])
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        self.assertEqual(
            _full_state(self.store, started["dispatch_id"], started["message_id"]), stable
        )

    def _assert_held_requested(self, started: dict) -> None:
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "in_flight")  # neither cancelled nor dlq
        self.assertIsNone(row["dlq_at"])
        self.assertIsNone(row["failure_reason"])
        self.assertIsNone(row["cancelled_at"])
        cancellation = _observed(self.store, started["dispatch_id"])["cancellation"]
        self.assertEqual(cancellation["state"], "requested")  # never falsely confirmed
        self.assertIsNone(cancellation["termination_result"])

    def test_pending_exit_only_holds_then_landed_proof_confirms_cancelled(self) -> None:
        # Pass 1 is the measured incomplete-proof interleaving from both family
        # packets: pending request, HALT ENOENT, exact same-run ``worker_exit``
        # visible, NO reap proof yet. The row must hold nonterminal in_flight
        # with the cancellation still requested -- never the early DLQ.
        adapter, started, actions = self._race({"worker_exit": self._same_run_worker_exit()})
        self.assertEqual(len(adapter.halt_calls), 1)
        self._assert_held_requested(started)
        terminal_actions = [
            a
            for a in actions
            if a.get("dispatch_id") == started["dispatch_id"]
            and a.get("status") in ("dlq", "cancelled")
        ]
        self.assertEqual(terminal_actions, [])

        # The complete same-token v1 reaper proof lands moments later, again
        # AFTER the next pass's failed HALT (during its STATUS probe): the next
        # liveness reconciliation must consume it and commit the sanctioned
        # ``cancelled`` / ``same_run_exit_confirmed`` terminal.
        adapter._evidence = {"reaper_exit": _complete_reaper(self.TOKEN, returncode=143)}
        adapter.status_calls = 0
        self.store.reconcile_dispatches(lambda _r: adapter, human_actor_id=HUMAN_ID)
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["dlq_at"])
        self.assertIsNone(row["failure_reason"])
        self.assertIsNotNone(row["cancelled_at"])
        cancellation = _observed(self.store, started["dispatch_id"])["cancellation"]
        self.assertEqual(cancellation["state"], "confirmed")
        self.assertEqual(cancellation["termination_result"], "same_run_exit_confirmed")
        self.assertEqual(cancellation["confirmed_via"], "liveness_reconciliation")
        # Both failed HALT attempts stay truthfully recorded on the object.
        self.assertEqual(cancellation["attempts"], 2)
        self.assertEqual(_transport(self.store, started["message_id"]), "cancelled")

    def test_incomplete_proof_holds_pending_row_and_never_falsely_cancels(self) -> None:
        incomplete = _complete_reaper(self.TOKEN, returncode=143)
        incomplete["native_process_group_drained"] = False
        adapter, started, _ = self._race(
            {"reaper_exit": incomplete, "worker_exit": self._same_run_worker_exit()}
        )
        self._assert_held_requested(started)

    def test_wrong_token_proof_holds_pending_row_and_never_falsely_cancels(self) -> None:
        adapter, started, _ = self._race(
            {
                "reaper_exit": _complete_reaper("run-token-OTHER-9999", returncode=143),
                "worker_exit": self._same_run_worker_exit(),
            }
        )
        self._assert_held_requested(started)

    def test_bare_worker_exit_without_pending_keeps_ordinary_dlq(self) -> None:
        adapter, started, _ = self._race(
            {"worker_exit": self._same_run_worker_exit()}, pending=False
        )
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(row["failure_reason"], "worker_exited_before_close")
        self.assertNotIn("cancellation", _observed(self.store, started["dispatch_id"]))

    def test_foreign_run_evidence_holds_the_row_nonterminal(self) -> None:
        foreign_exit = self._same_run_worker_exit()
        foreign_exit["run_token"] = "run-token-OTHER-9999"
        adapter, started, _ = self._race(
            {
                "reaper_exit": _complete_reaper("run-token-OTHER-9999", returncode=143),
                "worker_exit": foreign_exit,
            }
        )
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "in_flight")  # neither cancelled nor dlq
        cancellation = _observed(self.store, started["dispatch_id"])["cancellation"]
        self.assertEqual(cancellation["state"], "requested")

    def test_complete_proof_without_pending_cancellation_keeps_ordinary_dlq(self) -> None:
        adapter, started, _ = self._race(
            {
                "reaper_exit": _complete_reaper(self.TOKEN, returncode=143),
                "worker_exit": self._same_run_worker_exit(),
            },
            pending=False,
        )
        row = _ledger(self.store, started["dispatch_id"])
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(row["failure_reason"], "worker_exited_before_close")
        self.assertNotIn("cancellation", _observed(self.store, started["dispatch_id"]))


if __name__ == "__main__":
    unittest.main()
