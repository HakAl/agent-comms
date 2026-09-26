from __future__ import annotations

import hashlib
import json
import logging
import shlex
import sqlite3
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .adapters import DispatchContext, DispatchStart, RuntimeAdapter
from .actors import ActorRegistry
from .clock import utc_now
from .code_identity import StaleModuleError, require_fresh_module
from . import codex_auth_refresh
from .reviewing import intents as review_intents
from . import param_leak
from . import payload
from .db import Database, is_declined_worker
from .mailbox import Mailbox
from . import paths
from .policies import WORKER_DISPATCH_POLICY_VERSION
from .schema import ValidationError

LOG = logging.getLogger(__name__)
WORKER_DISPATCH_POLICY = "worker_dispatch_readwrite_bounded"
WORKER_DISPATCH_TTL_SECONDS = 60 * 60
DEFAULT_DISPATCH_CAP = 4
PAGE_CLAIM_ABANDON_SECONDS = 300
DEFAULT_QUEUED_AGE_PAGE_SECONDS = 3600
LINEAGE_FAIL_CLOSED_PAGE_CLAIM_KEY = "auth_lineage_fail_closed_page_claimed_at"
LINEAGE_FAIL_CLOSED_PAGE_MESSAGE_KEY = "auth_lineage_fail_closed_page_message_id"
LINEAGE_FAIL_CLOSED_PAGED_KEY = "auth_lineage_fail_closed_paged_at"
# Terminal statuses per Spec A dispatch ledger status table. Stage 2 adds the
# sanctioned-cancellation terminal ``cancelled``. This set is duplicated in
# ``supervisor.DISPATCH_TERMINAL_STATUSES`` and their agreement is pinned by a
# stage-2 test; keep the two in lockstep. Recognizing ``cancelled`` here is
# owner/lifecycle recognition ONLY: it does not by itself authorize janitor
# deletion, which now requires the supervisor's positive same-run evidence gate.
DISPATCH_TERMINAL_STATUSES = frozenset({"closed", "dlq", "spawn_failed_message_landed", "cancelled"})

# Same-run exit predicate for a terminal CAS. The CAS must prove the row it is
# about to DLQ still carries exit evidence for the *specific* authenticated run
# token, not merely that some exit object exists. Binding the token into the
# WHERE closes the ABA window where a retry/new run replaced an older run's exit
# evidence between the snapshot and the commit. Expects the run token bound
# twice: once for worker_exit, once for reaper_exit.
_SAME_RUN_EXIT_CAS_PREDICATE = (
    "and (json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.worker_exit.run_token') = ?"
    " or json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.reaper_exit.run_token') = ?)"
)

# Exact phrase the T7 hard-TTL backstop writes into the observed evidence and the
# producer page when an authenticated halt cannot confirm the native child died.
# The ledger is still released to dlq, but never under a false claim of death.
TERMINATION_NOT_CONFIRMED_PHRASE = "ledger released; termination not confirmed"

# Hold window past ``expected_close_by`` before an UNCONFIRMED hard-TTL
# termination releases the lineage. A reachable HALT may start at the deadline
# and settle naturally after the wrapper's own TERM -> grace -> KILL grace; an
# immediate unconfirmed / unreachable result must not release early. Aligned
# with the supervisor's kill grace (``adapters._base.KILL_AFTER_SECONDS``); kept
# as a local constant so a test can hold expected_close_by relative to it.
HARD_TTL_KILL_GRACE_SECONDS = 30.0

# --------------------------------------------------------------------------- #
# Stage-2 sanctioned cancellation (internal engine constants / vocabulary)
# --------------------------------------------------------------------------- #
# Namespaced bounded cancellation object key inside ``observed_values_json``.
CANCELLATION_KEY = "cancellation"
# Namespaced durable audit object for a committed operator settlement (T7). It is
# written ONLY by the exceptional ``dlq``/``cancelled`` settlement transaction and
# never marks the cancellation confirmed. A row carrying a settlement whose
# ``plan_fingerprint`` matches a replayed plan is the idempotent winner.
SETTLEMENT_KEY = "settlement"
# The exact terminal residue an operator settlement stamps. ``failure_reason`` is
# a distinct, greppable marker separating an operator-settled release from an
# ordinary hard-TTL release; the observed ``termination_result`` remains the
# truthful ``termination_not_confirmed`` (death is never claimed).
SETTLEMENT_FAILURE_REASON = "operator_settled_termination_unconfirmed"
SETTLEMENT_TERMINATION_RESULT = "termination_not_confirmed"
# Upper bound on a cancellation reason (bytes are ASCII-ish; chars are enough).
CANCELLATION_REASON_MAX = 2000
# Recognised authorities. ``producer`` is the dispatch's own producer actor;
# ``admin`` is a credentialed human operator recorded SEPARATELY from producer
# authority. Both are internal; no MCP/CLI surface is registered in this slice.
CANCELLATION_AUTHORITIES = frozenset({"producer", "admin"})
# Durable escalation deadline for a pending cancellation: after this many seconds
# without a confirmed termination the monitor escalates ONCE (producer blocker +
# operator infra notice) but never releases cap/lineage or claims death.
CANCELLATION_ESCALATION_SECONDS = 60.0
# Positive confirmed termination results for a sanctioned cancellation. These are
# the ONLY results that permit a terminal ``cancelled`` commit; every other
# outcome leaves the request pending (nonterminal) with cap/lineage held.
CANCELLATION_CONFIRMED_RESULTS = frozenset(
    {"not_started", "supervised_halt_confirmed", "same_run_exit_confirmed"}
)
# Deterministic per-monitor-pass batch size for active same-token re-probes of
# terminal ``dlq`` rows carrying ``termination_not_confirmed`` residue. Each pass
# selects at most this many rows via SQL ``LIMIT``, ordered by durable
# last-attempt state so repeated passes -- and a fresh Store/monitor process after
# a restart -- progress FAIRLY through more than one batch without starving any
# row. There is NO lifetime attempt cap: a residue row stays eligible forever
# until positive same-run evidence appears (upgrading ONLY the termination
# observation + janitor eligibility; the ledger status stays ``dlq``) or its state
# otherwise changes. Cumulative attempt telemetry keeps accumulating across passes.
DLQ_RESIDUE_REPROBE_BATCH = 5

# Bounded partial-work evidence (T8). The SQL-backed triage evidence a dead /
# cancelled / operator-settled worker leaves persists at MOST this many recipient
# reply message IDs, deterministically ordered by ``created_at`` then ``id``, and
# stores the EXACT total reply count separately so a page render reports the
# omitted remainder without inlining ids or bodies. Reply BODIES are never
# persisted. The copied latest status summary is bounded to the same
# 200-character display ceiling the DLQ page render applies; the authoritative
# ``latest_status_id`` is preserved unbounded. Evidence is triage material only
# and never infers success.
EVIDENCE_REPLY_ID_CAP = 10
EVIDENCE_SUMMARY_DISPLAY_CAP = 200


def _past_kill_grace_boundary(expected_close_by: str | None, now: str) -> bool:
    """True once ``now`` is at/after ``expected_close_by + kill_grace``.

    Missing or unparseable deadline info returns True (do not hold indefinitely).
    """
    if not expected_close_by:
        return True
    try:
        boundary = datetime.fromisoformat(expected_close_by) + timedelta(
            seconds=HARD_TTL_KILL_GRACE_SECONDS
        )
        current = datetime.fromisoformat(now)
    except ValueError:
        return True
    return current >= boundary


def _cancellation_escalation_due(requested_at: str | None, now: str) -> bool:
    """True once ``now`` is at/after ``requested_at + CANCELLATION_ESCALATION_SECONDS``.

    A missing/unparseable request timestamp returns False: a request with no
    durable clock is never escalated (it is simply retried).
    """
    if not requested_at:
        return False
    try:
        requested = datetime.fromisoformat(requested_at)
        current = datetime.fromisoformat(now)
    except (ValueError, TypeError):
        return False
    return (current - requested).total_seconds() >= CANCELLATION_ESCALATION_SECONDS


def is_dispatch_terminal(status: str) -> bool:
    return status in DISPATCH_TERMINAL_STATUSES


# Canonical joined projection of the two INDEPENDENT state machines:
# ``dispatch_ledger.status`` (execution outcome) and
# ``message_recipients.status`` (delivery obligation). Per the stage-2 brief the
# projection is TOTAL over every MECHANICALLY REACHABLE raw pair and never infers
# one status from the other.
#
# Reachability is derived from the actual transactions, not a hand-picked subset:
# ``mailbox.read_message`` / ``ack_message`` / ``close_message`` advance the
# transport copy UNCONDITIONALLY, but only flip the ledger with a
# ``where status = 'in_flight'`` predicate. So whenever the ledger is NOT
# ``in_flight`` a racing/late recipient read/ack/close leaves the ledger row
# untouched while the transport advances. Every ordinary pair below was
# empirically constructed via real Store operations (e.g. a worker that replies
# then acks/closes while the ledger is still ``queued`` commits
# ``queued/acknowledged`` and ``queued/closed`` -- pairs a prior revision
# wrongly called impossible). For the ordinary pairs the normalized outcome
# follows the EXECUTION (ledger) machine and is never derived from the transport
# status.
#
# The sanctioned-cancellation cluster is named distinctly and never folded into
# the ordinary outcomes: confirmed cancellation is ``cancelled/cancelled`` ->
# ``confirmed_cancel``; the exceptional operator settlement is ``dlq/cancelled``
# -> ``operator_settled_termination_unconfirmed`` (never a plain ``dlq`` or a
# confirmed cancel); and the reachable race where the recipient close wins its
# transport CAS first is ``cancelled/closed`` ->
# ``confirmed_cancel_transport_closed_first`` (never an ordinary ``closed``).
#
# A stale ``in_flight`` ledger whose recipient obligation already terminalized
# (``acknowledged``/``closed``) is NOT committed on the happy path -- the
# ack/close transaction flips the ledger in lockstep -- but the pre-TTL liveness
# and hard-TTL reconcilers OWN settling such a stale row to closed. That pair is
# projected to the distinct ``RECIPIENT_TERMINAL_LEDGER_OPEN`` outcome below,
# never a plain ``closed`` (the ledger has not closed; claiming it would be the
# cross-inference this projection forbids) and never ``unknown`` (the reconciler
# must act on it), so monitoring consumes this one classifier instead of
# re-mapping raw transport states by hand.
#
# Any truly UNREACHABLE or unrecognized pair (a ``closed`` ledger with a
# ``sent``/``read`` transport, since ``closed`` is only reached via ack/close; a
# non-``dlq`` ledger with a ``cancelled`` transport, since cancelled is committed
# jointly; an unknown status) returns a loud ``unknown`` rather than guessing
# equality.
#
# The recipient obligation is terminal (acked/closed) while the execution ledger
# is still open; the liveness / hard-TTL reconciler settles the stale row.
RECIPIENT_TERMINAL_LEDGER_OPEN = "recipient_terminal_ledger_open"

_DISPATCH_TRANSPORT_OUTCOMES: dict[tuple[str, str], str] = {
    # queued: the dispatch has not started; a recipient may still read/ack/close,
    # none of which flips the queued ledger.
    ("queued", "sent"): "queued",
    ("queued", "read"): "queued",
    ("queued", "acknowledged"): "queued",
    ("queued", "closed"): "queued",
    # in_flight: a read leaves it in_flight. An ack/close atomically flips the
    # ledger to closed in lockstep, so (in_flight, acknowledged/closed) is never
    # committed on the happy path; when a stale in_flight row is nonetheless
    # observed with a terminalized recipient copy, the liveness / hard-TTL
    # reconciler settles it and consumes this distinct outcome.
    ("in_flight", "sent"): "in_flight",
    ("in_flight", "read"): "in_flight",
    ("in_flight", "acknowledged"): RECIPIENT_TERMINAL_LEDGER_OPEN,
    ("in_flight", "closed"): RECIPIENT_TERMINAL_LEDGER_OPEN,
    # closed: reached only via ack/close, which set the transport in the same
    # transaction, so only acknowledged/closed transports pair with it.
    ("closed", "acknowledged"): "closed",
    ("closed", "closed"): "closed",
    # dlq: early-exit/TTL DLQ is independent of transport; a late read/ack/close
    # then advances only the transport.
    ("dlq", "sent"): "dlq",
    ("dlq", "read"): "dlq",
    ("dlq", "acknowledged"): "dlq",
    ("dlq", "closed"): "dlq",
    # spawn_failed_message_landed: the message landed but no worker started; a
    # recipient read/ack/close advances only the transport.
    ("spawn_failed_message_landed", "sent"): "spawn_failed",
    ("spawn_failed_message_landed", "read"): "spawn_failed",
    ("spawn_failed_message_landed", "acknowledged"): "spawn_failed",
    ("spawn_failed_message_landed", "closed"): "spawn_failed",
    # Sanctioned cancellation cluster (named distinctly; never folded above).
    ("cancelled", "cancelled"): "confirmed_cancel",
    ("dlq", "cancelled"): "operator_settled_termination_unconfirmed",
    ("cancelled", "closed"): "confirmed_cancel_transport_closed_first",
}


def project_dispatch_transport(dispatch_status: str, transport_status: str) -> dict:
    """Project a (dispatch_status, transport_status) pair to a normalized outcome.

    Returns a dict exposing BOTH raw statuses plus a normalized ``outcome``. The
    mapping never infers one status from the other: an unreachable or
    unrecognized pair (e.g. a ``closed`` ledger with a ``sent`` transport, since
    ``closed`` is reached only via ack/close) yields ``outcome="unknown"`` rather
    than assuming the two machines agree. A late/racing recipient ack/close on a
    non-``in_flight`` ledger IS reachable (e.g. ``queued/acknowledged``,
    ``dlq/closed``) and is mapped to the execution status, not treated as
    impossible. A stale ``in_flight`` ledger whose recipient copy already
    terminalized projects to the distinct ``RECIPIENT_TERMINAL_LEDGER_OPEN``
    outcome that the liveness / hard-TTL reconciler consumes to settle it to
    closed -- never a plain ``closed`` and never ``unknown``.
    """
    outcome = _DISPATCH_TRANSPORT_OUTCOMES.get((dispatch_status, transport_status), "unknown")
    return {
        "dispatch_status": dispatch_status,
        "transport_status": transport_status,
        "outcome": outcome,
    }


class ConcurrencyError(Exception):
    pass


class CancellationAuthorizationError(ValidationError):
    """The requesting actor is not authorized to cancel this dispatch."""


class CancellationConflictError(ValidationError):
    """A different actor/authority/reason cannot overwrite a pending request."""


class CancellationStateError(ValidationError):
    """The dispatch is in a terminal non-cancelled state and is never relabelled."""


class DispatchLedger:
    def __init__(self, db: Database, actors: ActorRegistry, mailbox: Mailbox) -> None:
        self._db = db
        self._actors = actors
        self._mailbox = mailbox

    def list_dispatches(self, *, status: str | None = None, limit: int = 50) -> list[dict]:
        self._db.init()
        if limit < 1:
            raise ValidationError("limit must be at least 1")
        with self._db.connection() as conn:
            params: list[object] = []
            where = ""
            if status is not None:
                where = "where d.status = ?"
                params.append(status)
            params.append(limit)
            # Reporting integration of the canonical joined projection: each row
            # exposes the recipient transport status and the normalized outcome
            # via ``project_dispatch_transport`` so the dispatch-status surface
            # never infers one state machine from the other (a ``dlq/cancelled``
            # row reports ``operator_settled_termination_unconfirmed``, not a
            # plain ``dlq``). The additive ``transport_status``/``outcome`` keys
            # do not disturb the pre-existing ledger fields.
            rows = conn.execute(
                f"""
                select
                  d.dispatch_id,
                  d.recipient_actor_id,
                  d.producer_actor_id,
                  d.status,
                  d.result,
                  d.override_reason,
                  d.failure_reason,
                  d.expected_close_by,
                  d.created_at,
                  d.observed_values_json,
                  mr.status as transport_status
                from dispatch_ledger d
                left join message_recipients mr
                  on mr.message_id = d.message_id and mr.to_agent = d.recipient_actor_id
                {where}
                order by d.created_at desc
                limit ?
                """,
                params,
            ).fetchall()
        dispatches = []
        for row in rows:
            malformed = False
            try:
                observed = json.loads(row["observed_values_json"] or "{}")
            except json.JSONDecodeError:
                observed = {}
                malformed = True
            if not isinstance(observed, dict):
                observed = {}
                malformed = True
            dispatch = {
                "dispatch_id": row["dispatch_id"],
                "recipient_actor_id": row["recipient_actor_id"],
                "producer_actor_id": row["producer_actor_id"],
                "status": row["status"],
                "result": row["result"],
                "override_reason": row["override_reason"],
                "failure_reason": row["failure_reason"],
                "expected_close_by": row["expected_close_by"],
                "created_at": row["created_at"],
                "observed_values": observed,
                "transport_status": row["transport_status"],
                "outcome": project_dispatch_transport(
                    row["status"], row["transport_status"]
                )["outcome"],
            }
            if malformed:
                dispatch["observed_values_malformed"] = True
            dispatches.append(dispatch)
        return dispatches

    def project_dispatch(self, dispatch_id: str) -> dict | None:
        """Canonical joined projection for ONE dispatch, or ``None`` if absent.

        Reads the ledger execution status and its recipient transport status and
        returns ``project_dispatch_transport(...)`` (both raw statuses plus the
        normalized ``outcome``) with the ``dispatch_id`` attached. This is the
        single joined-projection read the reporting, cleanup, and monitoring
        surfaces consult; none of them may pair the two INDEPENDENT state
        machines by hand or infer one status from the other.
        """
        self._db.init()
        with self._db.connection() as conn:
            row = conn.execute(
                """
                select d.status as dispatch_status, mr.status as transport_status
                from dispatch_ledger d
                left join message_recipients mr
                  on mr.message_id = d.message_id and mr.to_agent = d.recipient_actor_id
                where d.dispatch_id = ?
                """,
                (dispatch_id,),
            ).fetchone()
        if row is None:
            return None
        projected = project_dispatch_transport(row["dispatch_status"], row["transport_status"])
        return {"dispatch_id": dispatch_id, **projected}

    def worker_usage_candidates(self, *, batch: int = 25) -> tuple[list[dict], int]:
        self._db.init()
        if batch < 1:
            raise ValidationError("batch must be at least 1")
        terminal_ts = "coalesce(d.closed_at, d.dlq_at, d.cancelled_at, CASE WHEN json_valid(d.observed_values_json) THEN json_extract(d.observed_values_json, '$.spawn_failed_at') END, d.created_at)"
        with self._db.connection() as conn:
            rows = conn.execute(
                f"""
                select d.dispatch_id, d.observed_values_json, a.runtime,
                       {terminal_ts} as terminal_ts
                from dispatch_ledger d
                join actors a on a.id = d.recipient_actor_id
                where d.status in ('closed', 'dlq', 'spawn_failed_message_landed', 'cancelled')
                  and json_valid(d.observed_values_json) = 1
                  and CASE WHEN json_valid(d.observed_values_json) THEN json_extract(d.observed_values_json, '$.worker_usage') END is null
                order by {terminal_ts}, d.dispatch_id
                limit ?
                """,
                (batch,),
            ).fetchall()
            malformed = conn.execute("""
                select count(*) from dispatch_ledger
                where status in ('closed', 'dlq', 'spawn_failed_message_landed', 'cancelled')
                  and json_valid(observed_values_json) = 0
            """).fetchone()[0]
        return [dict(row) for row in rows], malformed

    def write_worker_usage(self, dispatch_id: str, worker_usage: dict) -> bool:
        self._db.init()
        with self._db.connection() as conn:
            cursor = conn.execute(
                """
                update dispatch_ledger
                set observed_values_json = json_set(
                  coalesce(nullif(observed_values_json,''),'{}'),
                  '$.worker_usage', json(?)
                )
                where dispatch_id = ?
                  and json_extract(coalesce(nullif(observed_values_json,''),'{}'), '$.worker_usage') is null
                """,
                (json.dumps(worker_usage, sort_keys=True), dispatch_id),
            )
        return cursor.rowcount == 1

    def dispatch_agent(
        self,
        producer_actor_id: str,
        target_actor_id: str,
        idempotency_key: str,
        subject: str,
        body: str | None = None,
        refs: list[dict] | None = None,
        *,
        body_file: str | None = None,
        payload_origin: str | None = None,
        source_root: str | None = None,
        requested_policy: str = WORKER_DISPATCH_POLICY,
        override_reason: str | None = None,
        adapter_for_runtime: Callable[[str], RuntimeAdapter] | None = None,
        ttl_seconds: int = WORKER_DISPATCH_TTL_SECONDS,
    ) -> dict:
        require_fresh_module()
        refs = refs or []
        for field, value in (
            ("producer_actor_id", producer_actor_id), ("target_actor_id", target_actor_id),
            ("idempotency_key", idempotency_key), ("subject", subject), ("body", body),
            ("body_file", body_file), ("payload_origin", payload_origin),
            ("source_root", source_root),
            ("requested_policy", requested_policy), ("override_reason", override_reason),
        ):
            if value is not None:
                param_leak.assert_no_parameter_leak(field, value)
        self._db.init()
        idempotency_key = idempotency_key.strip()
        if not idempotency_key:
            raise ValidationError("idempotency_key must not be empty")
        if requested_policy != WORKER_DISPATCH_POLICY:
            raise ValidationError(f"unknown or unavailable policy: {requested_policy}")
        if ttl_seconds < 1:
            raise ValidationError("ttl_seconds must be at least 1")

        # Exactly one logical-body source; every refusal here happens before
        # any write (no message, recipient, thread, ledger row, or semaphore).
        file_backed = body_file is not None
        if file_backed == (body is not None):
            raise ValidationError(
                "dispatch_body_input_conflict: supply exactly one of inline body or body_file"
            )
        if file_backed:
            if payload_origin is None:
                raise ValidationError(
                    "dispatch_payload_origin_invalid: payload_origin is required with body_file"
                )
            payload.validate_payload_origin(payload_origin)
        else:
            if payload_origin is not None:
                raise ValidationError(
                    "dispatch_payload_origin_invalid: payload_origin is forbidden with an inline body"
                )
            if source_root is not None:
                raise ValidationError(
                    "dispatch_payload_path_invalid: source_root is only accepted with body_file"
                )
            inline_bytes = len(body.encode("utf-8"))
            if inline_bytes > payload.INLINE_BODY_MAX_BYTES:
                raise ValidationError(
                    f"dispatch_body_too_large: inline dispatch body is {inline_bytes} UTF-8 bytes; "
                    f"the limit is {payload.INLINE_BODY_MAX_BYTES}. Save the body to a "
                    "repository-relative file and dispatch it with body_file plus payload_origin"
                )

        # Review dispatch-intent expiry runs from dispatch_agent in its own
        # short committed transaction, so an expired prepared/active row is
        # durably abandoned even when the dispatch below refuses.
        self._reconcile_review_intents(utc_now())

        # Read-only idempotency pre-probe: an ordinary replay returns the
        # existing row (or refuses a different target) without taking the
        # write lock and, for a file-backed dispatch, without touching the
        # source file at all.
        existing = self._probe_idempotent_replay(producer_actor_id, target_actor_id, idempotency_key)
        if existing is not None:
            return existing

        capture: payload.CapturedPayload | None = None
        staged: payload.StagedPayload | None = None
        payload_store_root = payload.store_root_for_db(self._db.db_path)
        if file_backed:
            resolved_root = self._resolve_payload_source_root(producer_actor_id, source_root)
            try:
                capture = payload.capture_source(resolved_root, body_file)
            except payload.PayloadError:
                # One repeated read-only probe: a concurrently committed
                # matching first call wins and is returned unchanged;
                # otherwise the truthful capture failure surfaces.
                winner = self._probe_idempotent_replay(
                    producer_actor_id, target_actor_id, idempotency_key
                )
                if winner is not None:
                    return winner
                raise
            # Staging is deferred into the write transaction below, AFTER the
            # authoritative active-intent match, so a non-active or mismatched
            # review intent refuses with zero staging (contract 17). The source
            # capture above is a read and stays on the pre-probe path.

        published: payload.PublishedBlob | None = None
        try:
            with self._db.connection() as conn:
                conn.execute("begin immediate")
                try:
                    existing = self._dispatch_by_idempotency_key(conn, producer_actor_id, idempotency_key)
                    if existing is not None:
                        if existing["recipient_actor_id"] != target_actor_id:
                            raise ValidationError(
                                f"idempotency_key {idempotency_key!r} for producer {producer_actor_id} "
                                f"already dispatched to {existing['recipient_actor_id']}, not {target_actor_id}; "
                                "reuse of a key for a different target is not allowed"
                            )
                        conn.commit()
                        return existing

                    producer = self._actors._actor_row_by_id(conn, producer_actor_id)
                    target = self._actors._actor_row_by_id(conn, target_actor_id)
                    self._authorize_dispatch(producer, target, override_reason)
                    # Review dispatch-intent consumption (dispatch contract 17):
                    # exact-match any intent for this producer/key BEFORE any
                    # message, ledger, payload, or spawn effect. No intent row
                    # preserves ordinary dispatch; a non-active or mismatched
                    # intent refuses the transaction. Expiry reconciliation ran
                    # in its own committed transaction before this one.
                    review_intent = review_intents.match_for_binding(
                        conn,
                        producer_actor_id,
                        idempotency_key,
                        recipient_actor_id=target_actor_id,
                        real_project_root=str(
                            Path(str(target["project_root"] or ""))
                            .expanduser()
                            .resolve()
                        ),
                        policy_name=requested_policy,
                        policy_version=WORKER_DISPATCH_POLICY_VERSION,
                    )
                    if file_backed:
                        # Stage the captured payload only after the intent match
                        # authorizes the dispatch: a refused intent above never
                        # reaches this write effect.
                        staged = payload.stage_payload(payload_store_root, capture.data)
                    auth_lineage_key = self._codex_auth_lineage_key_for_actor_row(
                        target
                    )
                    if str(target["runtime"]) == "codex":
                        self._validate_codex_ttl_satisfiable(ttl_seconds)
                    in_flight_count = self._producer_in_flight_count(conn, producer_actor_id)
                    dispatch_cap = self._producer_dispatch_cap(conn, producer_actor_id)
                    refresh_in_progress = False
                    token_fresh = True
                    if str(target["runtime"]) == "codex":
                        refresh_in_progress = self._refresh_claim_active(conn, auth_lineage_key)
                        token_fresh = self._codex_token_fresh(target, ttl_seconds)
                    inline_promote = (
                        in_flight_count < dispatch_cap
                        and not refresh_in_progress and token_fresh
                    )
                    if file_backed:
                        # Create-only durable publication while the write lock is
                        # held: the blob and its directories are fsynced before
                        # the SQL reference commits, and the returned custody
                        # (retained shard FD plus created-inode identity) stays
                        # valid through this transaction's commit or rollback.
                        published = payload.publish_final(
                            payload_store_root, staged, capture
                        )
                        staged = None
                    message = self._mailbox._insert_message(
                        conn,
                        producer_actor_id,
                        [target_actor_id],
                        subject,
                        payload.ARTIFACT_BODY_MARKER if file_backed else body,
                        refs,
                        "normal",
                        True,
                        None,
                    )
                    dispatch_id = f"dispatch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
                    now = utc_now()
                    conn.execute(
                        """
                        insert into dispatch_ledger(
                          dispatch_id, parent_dispatch_id, idempotency_key, message_id,
                          thread_ref, spawn_handle, recipient_actor_id, producer_actor_id,
                          originating_actor_id, policy_name, policy_version, policy_issued_by,
                          expected_close_by, status, created_at, observed_values_json, override_reason,
                          auth_lineage_key
                        )
                        values(?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, 'queued', ?, '{}', ?, ?)
                        """,
                        (
                            dispatch_id,
                            idempotency_key,
                            message["id"],
                            message["id"],
                            target_actor_id,
                            producer_actor_id,
                            producer_actor_id,
                            requested_policy,
                            WORKER_DISPATCH_POLICY_VERSION,
                            producer_actor_id,
                            now,
                            override_reason,
                            auth_lineage_key,
                        ),
                    )
                    if file_backed:
                        conn.execute(
                            """
                            insert into dispatch_payload_refs(
                              dispatch_id, storage_kind, payload_origin, payload_sha256,
                              byte_count, char_count, captured_at
                            )
                            values(?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                dispatch_id,
                                payload.STORAGE_KIND,
                                payload_origin,
                                capture.sha256,
                                capture.byte_count,
                                capture.char_count,
                                now,
                            ),
                        )
                    if review_intent is not None:
                        # Same transaction as the queued row: CAS active -> bound
                        # and attach the exact ledger dispatch_id.
                        review_intents.bind(
                            conn, producer_actor_id, idempotency_key, dispatch_id, now
                        )
                    conn.commit()
                except Exception as exc:
                    # A call that created the final blob removes exactly that
                    # inode relative to the retained shard FD (identity-guarded,
                    # so a replacement at the canonical digest name is never
                    # deleted) and fsyncs the same retained shard to make the
                    # removal durable BEFORE rollback releases the BEGIN
                    # IMMEDIATE lock; a blob that already existed and was
                    # reverified carries no custody and is never unlinked. A
                    # failed identity guard or unlink, or a failed cleanup
                    # fsync, is reported as measurable orphan/cleanup
                    # uncertainty and never retried after the lock is released.
                    try:
                        payload.cleanup_failed_publication(published, exc)
                    except payload.PayloadError:
                        conn.rollback()
                        raise
                    conn.rollback()
                    raise
        finally:
            if published is not None:
                published.close()
            payload.discard_staging(staged)

        self._mailbox._write_semaphores(message["recipient_roots"], [target_actor_id], message["id"], message["created_at"])
        if adapter_for_runtime is None or not inline_promote:
            row = self._dispatch_by_idempotency_key_fresh(producer_actor_id, idempotency_key)
            if adapter_for_runtime is not None and refresh_in_progress:
                row["lineage_gate_status"] = "refresh_in_progress"
                row["lineage_key"] = auth_lineage_key
            elif adapter_for_runtime is not None and not token_fresh:
                row["lineage_gate_status"] = "token_stale"
                row["lineage_key"] = auth_lineage_key
            return row
        return self._start_dispatch_by_id(
            adapter_for_runtime,
            dispatch_id,
            ttl_seconds=ttl_seconds,
        )

    def start_queued_dispatches(
        self,
        adapter_for_runtime: Callable[[str], RuntimeAdapter],
        *,
        limit: int = 1,
        ttl_seconds: int = WORKER_DISPATCH_TTL_SECONDS,
    ) -> list[dict]:
        try:
            require_fresh_module()
        except StaleModuleError as exc:
            return [{"status": "stale_module_refused", "detail": str(exc)}]
        self._db.init()
        if limit < 1:
            raise ValidationError("limit must be at least 1")
        if ttl_seconds < 1:
            raise ValidationError("ttl_seconds must be at least 1")

        started = []
        raced_dispatch_ids: set[str] = set()
        blocked_lineage_keys: set[str] = set()
        token_gate_actions: list[dict] = []
        for _ in range(limit):
            with self._db.connection() as conn:
                context = self._next_queued_dispatch_context(
                    conn,
                    ttl_seconds,
                    raced_dispatch_ids,
                    blocked_lineage_keys,
                    token_gate_actions,
                )
                if context is None:
                    break
            try:
                row = self._start_dispatch_context(adapter_for_runtime, context)
            except ConcurrencyError as exc:
                dispatch_id = str(context.dispatch["dispatch_id"])
                raced_dispatch_ids.add(dispatch_id)
                started.append(
                    {
                        "dispatch_id": dispatch_id,
                        "status": "start_race_concurrent",
                        "detail": str(exc),
                    }
                )
                continue
            started.append(row)
        started.extend(token_gate_actions)
        return started

    def _start_dispatch_by_id(
        self,
        adapter_for_runtime: Callable[[str], RuntimeAdapter],
        dispatch_id: str,
        *,
        ttl_seconds: int,
    ) -> dict:
        with self._db.connection() as conn:
            context = self._dispatch_context_by_id(conn, dispatch_id, ttl_seconds, recheck_codex_gates=True)
            if context is None:
                return self._dispatch_by_id(conn, dispatch_id)

        return self._start_dispatch_context(adapter_for_runtime, context)

    def _start_dispatch_context(
        self,
        adapter_for_runtime: Callable[[str], RuntimeAdapter],
        context: DispatchContext,
    ) -> dict:
        try:
            self._payload_preflight(str(context.dispatch["dispatch_id"]))
            runtime_adapter = adapter_for_runtime(str(context.recipient["runtime"]))
            result = runtime_adapter.dispatch(context)
            self._validate_dispatch_start(result)
        except Exception as exc:
            now = utc_now()
            with self._db.connection() as conn:
                conn.execute("begin immediate")
                try:
                    current = conn.execute(
                        "select * from dispatch_ledger where dispatch_id = ?",
                        (context.dispatch["dispatch_id"],),
                    ).fetchone()
                    # A cancellation that landed while this row was claimed/bootstrapping,
                    # combined with a spawn failure that started NO runtime, settles the
                    # requested cancellation as ``not_started`` -- never
                    # ``spawn_failed_message_landed``.
                    if current is not None and current["status"] == "queued":
                        existing_observed = json.loads(current["observed_values_json"] or "{}")
                        cancellation = existing_observed.get(CANCELLATION_KEY)
                        if isinstance(cancellation, dict) and cancellation.get("state") == "requested":
                            cancellation = dict(cancellation)
                            cancellation["state"] = "confirmed"
                            cancellation["termination_result"] = "not_started"
                            cancellation["confirmed_at"] = now
                            cancellation["latest_detail"] = f"spawn failed before start: {exc}"
                            cancellation["partial_evidence"] = self._capture_cancellation_evidence(
                                conn, context.dispatch["dispatch_id"], current, existing_observed
                            )
                            existing_observed[CANCELLATION_KEY] = cancellation
                            existing_observed["spawn_failed_at"] = now
                            self._commit_confirmed_cancel(
                                conn,
                                context.dispatch["dispatch_id"],
                                current,
                                existing_observed,
                                now,
                                from_status="queued",
                            )
                            row = self._dispatch_by_id(conn, context.dispatch["dispatch_id"])
                            conn.commit()
                            return row
                    cursor = conn.execute(
                        """
                        update dispatch_ledger
                        set status = 'spawn_failed_message_landed',
                            failure_reason = ?,
                            observed_values_json = ?
                        where dispatch_id = ? and status = 'queued'
                        """,
                        (
                            str(exc),
                            json.dumps({"spawn_failed_at": now}, sort_keys=True),
                            context.dispatch["dispatch_id"],
                        ),
                    )
                    if cursor.rowcount == 0:
                        observed_status = self._dispatch_status_or_missing(conn, context.dispatch["dispatch_id"])
                        conn.rollback()
                        raise ConcurrencyError(
                            f"start dispatch_id={context.dispatch['dispatch_id']} discovered concurrent "
                            f"state change during spawn failure; observed status={observed_status}"
                        )
                    row = self._dispatch_by_id(conn, context.dispatch["dispatch_id"])
                    conn.commit()
                    return row
                except Exception:
                    conn.rollback()
                    raise

        now = utc_now()
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                current = conn.execute(
                    "select status, observed_values_json from dispatch_ledger where dispatch_id = ?",
                    (context.dispatch["dispatch_id"],),
                ).fetchone()
                if current is None or current["status"] != "queued":
                    # A concurrent writer already moved the row out of queued
                    # (close/ack/dlq/another start). Release the write lock, then
                    # halt the orphan we just spawned OUTSIDE the transaction, and
                    # refuse. Never publish a false in_flight over terminal state.
                    observed_status = current["status"] if current is not None else "<missing>"
                    conn.rollback()
                    halt_note = self._halt_orphan(runtime_adapter, result)
                    raise ConcurrencyError(
                        f"start dispatch_id={context.dispatch['dispatch_id']} discovered "
                        "concurrent state change; orphan spawn_handle="
                        f"{result.spawn_handle} {halt_note}; observed status={observed_status}"
                    )

                # Successful spawn commit MERGES the supervisor identity onto any
                # observed evidence a fast worker already wrote (a queued-time
                # close mismatch, a same-run exit report) instead of overwriting
                # it. The write lock is held from this re-read through the CAS, so
                # no other writer can change the row underneath us.
                existing_observed = json.loads(current["observed_values_json"] or "{}")
                merged = {**existing_observed, **dict(result.observed_values)}
                recipient_status = self._recipient_copy_status(
                    conn,
                    context.dispatch["message_id"],
                    context.dispatch["recipient_actor_id"],
                )
                # Same-run predicate: only a fast exit whose evidence carries THIS
                # spawn's run token settles the ledger. Compare the retained
                # evidence against result.observed_values.run_token (the freshly
                # spawned run), never merely the merged observed, so a retry that
                # still holds an older run's queued exit evidence cannot DLQ this
                # new run.
                current_run_token = result.observed_values.get("run_token")
                exit_evidence = self._authenticated_same_run_exit(existing_observed, current_run_token)

                cancellation = existing_observed.get(CANCELLATION_KEY)
                pending_cancellation = (
                    isinstance(cancellation, dict) and cancellation.get("state") == "requested"
                )

                if pending_cancellation:
                    # Persisted pending cancellation PRECEDES bootstrap/early-exit
                    # classification: a cancellation that landed during bootstrap
                    # while the spawn succeeded is owned by the cancellation engine.
                    # NEVER publish ordinary in_flight work, and never fold the row
                    # into a paused-close or an early-DLQ classification here.
                    # Release the write lock and settle OUTSIDE the transaction:
                    # exact CURRENT same-run SQL exit evidence confirms without a
                    # HALT, otherwise the exact orphan is HALTed; an unconfirmed
                    # result keeps the row pending (merged control identity + spawn
                    # handle) for the monitor to retry.
                    conn.rollback()
                    return self._settle_spawn_time_cancellation(
                        context, result, runtime_adapter, existing_observed=existing_observed
                    )

                if recipient_status in ("acknowledged", "closed"):
                    # Paused-spawn fast close/ack: the recipient already terminated
                    # the trigger before this commit landed, so the queued->closed
                    # mailbox update missed the still-queued ledger row. Settle the
                    # ledger directly to closed, preserve the mismatch/exit
                    # evidence, drop the lineage claim, and never publish a false
                    # in_flight that a later payload could overwrite.
                    merged["spawn_commit_recipient_terminal"] = recipient_status
                    merged["spawn_commit_settled_at"] = now
                    conn.execute(
                        """
                        update dispatch_ledger
                        set status = 'closed',
                            spawn_handle = ?,
                            spawned_at = ?,
                            closed_at = coalesce(closed_at, ?),
                            observed_values_json = ?
                        where dispatch_id = ? and status = 'queued'
                        """,
                        (
                            result.spawn_handle,
                            now,
                            now,
                            json.dumps(merged, sort_keys=True),
                            context.dispatch["dispatch_id"],
                        ),
                    )
                    row = self._dispatch_by_id(conn, context.dispatch["dispatch_id"])
                    conn.commit()
                    return row

                if exit_evidence is not None:
                    # Same-run worker exit already recorded but the trigger was
                    # never closed: a dead-before-close worker. DLQ it directly
                    # with the exact reason rather than publishing a false
                    # in_flight or masquerading it as a spawn failure. The CAS
                    # re-proves the specific run token against the row so a stale
                    # older-run exit object can never DLQ this fresh spawn.
                    merged["spawn_commit_settled_at"] = now
                    merged["early_dlq_evidence"] = self._capture_early_dlq_evidence(
                        conn, context.dispatch["dispatch_id"], context.dispatch, existing_observed
                    )
                    cursor = conn.execute(
                        """
                        update dispatch_ledger
                        set status = 'dlq',
                            spawn_handle = ?,
                            spawned_at = ?,
                            dlq_at = ?,
                            failure_reason = 'worker_exited_before_close',
                            observed_values_json = ?
                        where dispatch_id = ? and status = 'queued'
                        """
                        + _SAME_RUN_EXIT_CAS_PREDICATE,
                        (
                            result.spawn_handle,
                            now,
                            now,
                            json.dumps(merged, sort_keys=True),
                            context.dispatch["dispatch_id"],
                            current_run_token,
                            current_run_token,
                        ),
                    )
                    if cursor.rowcount == 0:
                        # The row's stored exit token no longer matches the token
                        # we authenticated in-process. Never DLQ a different run:
                        # halt the orphan we just spawned and refuse loudly.
                        conn.rollback()
                        halt_note = self._halt_orphan(runtime_adapter, result)
                        raise ConcurrencyError(
                            f"start dispatch_id={context.dispatch['dispatch_id']} same-run exit CAS "
                            "did not match the authenticated run token; orphan spawn_handle="
                            f"{result.spawn_handle} {halt_note}"
                        )
                    row = self._dispatch_by_id(conn, context.dispatch["dispatch_id"])
                    conn.commit()
                    return row

                in_flight_count = self._producer_in_flight_count(conn, context.dispatch["producer_actor_id"])
                dispatch_cap = self._producer_dispatch_cap(conn, context.dispatch["producer_actor_id"])
                if in_flight_count >= dispatch_cap:
                    # Producer filled its cap between the pre-spawn check and this
                    # commit. Release the write lock BEFORE halting so no socket
                    # HALT / child wait runs under BEGIN IMMEDIATE, then re-open a
                    # short transaction to record the race and release lineage.
                    conn.rollback()
                    halt_outcome = self._halt_orphan(runtime_adapter, result)
                    conn.execute("begin immediate")
                    race_observed = dict(merged)
                    race_observed["start_race_halted_at"] = now
                    race_observed["start_race_halt_outcome"] = halt_outcome
                    cursor = conn.execute(
                        """
                        update dispatch_ledger
                        set observed_values_json = ?
                        where dispatch_id = ? and status = 'queued'
                        """,
                        (
                            json.dumps(race_observed, sort_keys=True),
                            context.dispatch["dispatch_id"],
                        ),
                    )
                    if cursor.rowcount == 0:
                        observed_status = self._dispatch_status_or_missing(conn, context.dispatch["dispatch_id"])
                        conn.rollback()
                        raise ConcurrencyError(
                            f"start dispatch_id={context.dispatch['dispatch_id']} cannot proceed: "
                            f"producer {context.dispatch['producer_actor_id']} at cap "
                            f"{in_flight_count}/{dispatch_cap}; orphan spawn_handle="
                            f"{result.spawn_handle} {halt_outcome}; observed status={observed_status}"
                        )
                    row = self._dispatch_by_id(conn, context.dispatch["dispatch_id"])
                    conn.commit()
                    return row
                cursor = conn.execute(
                    """
                    update dispatch_ledger
                    set status = 'in_flight',
                        spawn_handle = ?,
                        expected_close_by = ?,
                        spawned_at = ?,
                        observed_values_json = ?
                    where dispatch_id = ? and status = 'queued'
                    """,
                    (
                        result.spawn_handle,
                        context.expected_close_by,
                        now,
                        json.dumps(merged, sort_keys=True),
                        context.dispatch["dispatch_id"],
                    ),
                )
                if cursor.rowcount == 0:
                    conn.rollback()
                    halt_note = self._halt_orphan(runtime_adapter, result)
                    raise ConcurrencyError(
                        f"start dispatch_id={context.dispatch['dispatch_id']} discovered "
                        "concurrent state change; orphan spawn_handle="
                        f"{result.spawn_handle} {halt_note}"
                    )
                row = self._dispatch_by_id(conn, context.dispatch["dispatch_id"])
                conn.commit()
                return self._annotate_key_drift_after_spawn(row)
            except Exception:
                conn.rollback()
                raise

    def _dispatch_status_or_missing(self, conn: sqlite3.Connection, dispatch_id: str) -> str:
        row = conn.execute(
            "select status from dispatch_ledger where dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        if row is None:
            return "<missing>"
        return str(row["status"])

    @staticmethod
    def _authenticated_same_run_exit(observed: dict, run_token: object) -> dict | None:
        """Same-run exit evidence, authenticated by exact run-token equality.

        Returns the ``worker_exit`` / ``reaper_exit`` object only when it is a
        dict carrying a non-empty string ``run_token`` that exactly equals the
        supplied current ``run_token`` (itself required to be a non-empty
        string). A stale, absent, or malformed token on either side returns
        ``None`` so a new run is never early-DLQ'd on an older run's evidence.
        This explicit same-run predicate is the T5/T7 DoD: object existence is
        never sufficient on its own.
        """
        if not isinstance(run_token, str) or not run_token:
            return None
        for key in ("worker_exit", "reaper_exit"):
            evidence = observed.get(key)
            if not isinstance(evidence, dict):
                continue
            evidence_token = evidence.get("run_token")
            if isinstance(evidence_token, str) and evidence_token and evidence_token == run_token:
                return evidence
        return None

    @staticmethod
    def _complete_same_run_reap_proof(observed: dict, run_token: object) -> dict | None:
        """Revision 7 F2: the ONLY evidence that confirms a cancellation
        ``same_run_exit_confirmed`` is the exact version-1 COMPLETE ``$.reaper_exit``
        proof for the exact current run token.

        A bare ``$.worker_exit`` (child-exit evidence a still-alive wrapper
        persists), the old four-field ``reaper_exit``, and any incomplete, false,
        malformed, stale, or wrong-token object never qualify. Every path that
        assigns cancellation ``same_run_exit_confirmed`` -- ordinary producer
        cancellation, queued/spawn-race settlement, monitor retry, and residue
        re-probe -- consumes THIS predicate, never the broader child-exit
        ``_authenticated_same_run_exit`` predicate (which still drives the early
        ``worker_exited_before_close`` reconciliation, where ``worker_exit`` is the
        truthful signal)."""
        from . import supervisor

        return supervisor.complete_reaper_proof(observed.get("reaper_exit"), run_token)

    def _recipient_copy_status(
        self, conn: sqlite3.Connection, message_id: str, recipient_actor_id: str
    ) -> str | None:
        row = conn.execute(
            "select status from message_recipients where message_id = ? and to_agent = ?",
            (message_id, recipient_actor_id),
        ).fetchone()
        if row is None:
            return None
        return str(row["status"])

    def _halt_orphan(self, runtime_adapter: RuntimeAdapter, result: DispatchStart) -> str:
        """Halt an orphaned spawn OUTSIDE any write transaction.

        The supervisor control identity is passed in ``observed_values`` so a
        supervised orphan is stopped with an authenticated socket HALT rather
        than a PID-parsed signal. Returns a human-readable outcome note for the
        ``ConcurrencyError`` / observed-values race record. Never raises: a halt
        failure is recorded, not propagated, so the caller can still refuse the
        start cleanly.
        """
        try:
            runtime_adapter.halt(result.spawn_handle, dict(result.observed_values))
            return "halted"
        except Exception as halt_exc:
            return f"halt failed: {halt_exc}"

    def retry_spawn(
        self,
        dispatch_id: str,
        adapter_for_runtime: Callable[[str], RuntimeAdapter],
        *,
        ttl_seconds: int = WORKER_DISPATCH_TTL_SECONDS,
    ) -> dict:
        """Retry runtime spawn for an existing failed row.

        When producer concurrency caps land, this transition will need to
        consume an in-flight slot exactly like a fresh dispatch start.
        """
        require_fresh_module()
        param_leak.assert_no_parameter_leak("dispatch_id", dispatch_id)
        self._db.init()
        if ttl_seconds < 1:
            raise ValidationError("ttl_seconds must be at least 1")

        with self._db.connection() as conn:
            dispatch_row = conn.execute(
                "select * from dispatch_ledger where dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
            if dispatch_row is None:
                raise ValidationError(f"unknown dispatch_id: {dispatch_id}")
            if dispatch_row["status"] != "spawn_failed_message_landed":
                raise ValidationError(
                    "retry_spawn requires status spawn_failed_message_landed, "
                    f"got {dispatch_row['status']}"
                )
            in_flight_count = self._producer_in_flight_count(conn, dispatch_row["producer_actor_id"])
            dispatch_cap = self._producer_dispatch_cap(conn, dispatch_row["producer_actor_id"])
            if in_flight_count >= dispatch_cap:
                raise ConcurrencyError(
                    f"retry_spawn for dispatch_id={dispatch_id} cannot proceed: "
                    f"producer {dispatch_row['producer_actor_id']} at cap {in_flight_count}/{dispatch_cap}"
                )
            if is_declined_worker(conn, dispatch_row["recipient_actor_id"]):
                return self._dispatch_by_id(conn, dispatch_id)
            context = self._dispatch_context_from_row(conn, dispatch_row, ttl_seconds)
        try:
            # Preflight before the runtime factory: a corrupt/missing payload
            # constructs zero adapters, and a raising factory settles the row
            # truthfully below instead of escaping before persistence.
            self._payload_preflight(dispatch_id)
            runtime_adapter = adapter_for_runtime(str(context.recipient["runtime"]))
            result = runtime_adapter.dispatch(context)
            self._validate_dispatch_start(result)
        except Exception as exc:
            outcome = "spawn_failed_message_landed"
            failure_reason = str(exc)
            result = None
        else:
            outcome = "in_flight"
            failure_reason = None

        now = utc_now()
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                current = conn.execute(
                    """
                    select status, observed_values_json
                    from dispatch_ledger
                    where dispatch_id = ?
                    """,
                    (dispatch_id,),
                ).fetchone()
                if current is None:
                    raise RuntimeError(f"dispatch row disappeared: {dispatch_id}")

                if outcome != "in_flight":
                    # Failed retry dispatch (no new run, no orphan): annotate the
                    # failure, bump the retry metadata, preserve prior diagnostics,
                    # release the lineage, and stay spawn_failed_message_landed. No
                    # recipient/exit settling: there is no authenticated run token.
                    observed = json.loads(current["observed_values_json"] or "{}")
                    observed["retry_count"] = int(observed.get("retry_count", 0)) + 1
                    observed["last_retry_at"] = now
                    observed["last_retry_outcome"] = outcome
                    conn.execute(
                        """
                        update dispatch_ledger
                        set failure_reason = ?,
                            observed_values_json = ?
                        where dispatch_id = ? and status = 'spawn_failed_message_landed'
                        """,
                        (failure_reason, json.dumps(observed, sort_keys=True), dispatch_id),
                    )
                    row_dict = self._dispatch_by_id(conn, dispatch_id)
                    conn.commit()
                    return row_dict

                # Successful retry dispatch. Exact parity with the spawn-commit
                # lifecycle: a fresh run was spawned, so re-read the row and the
                # recipient copy under this held write lock and settle a
                # paused-retry race deterministically instead of blindly
                # promoting to in_flight.
                if current["status"] != "spawn_failed_message_landed":
                    # A concurrent writer already moved the row out of
                    # spawn_failed_message_landed. Release the write lock, halt the
                    # orphan we just spawned OUTSIDE the transaction with the
                    # authenticated control identity, and refuse. Never publish a
                    # false in_flight over terminal/other state.
                    observed_status = current["status"]
                    conn.rollback()
                    halt_note = self._halt_orphan(runtime_adapter, result)
                    raise ConcurrencyError(
                        f"retry_spawn for dispatch_id={dispatch_id} discovered "
                        "concurrent state change; orphan spawn_handle="
                        f"{result.spawn_handle} {halt_note}; observed status={observed_status}"
                    )

                # MERGE the supervisor identity onto any observed evidence a fast
                # worker already wrote (a queued-time close mismatch, a same-run
                # exit report) instead of overwriting it, then layer the retry
                # metadata on top. The write lock is held from this re-read through
                # the CAS, so no other writer can change the row underneath us.
                existing_observed = json.loads(current["observed_values_json"] or "{}")
                merged = {**existing_observed, **dict(result.observed_values)}
                merged["retry_count"] = int(existing_observed.get("retry_count", 0)) + 1
                merged["last_retry_at"] = now
                merged["last_retry_outcome"] = "in_flight"
                recipient_status = self._recipient_copy_status(
                    conn,
                    context.dispatch["message_id"],
                    context.dispatch["recipient_actor_id"],
                )
                # Same-run predicate: only a fast exit whose evidence carries THIS
                # retry's run token settles the ledger. Compare against
                # result.observed_values.run_token (the freshly spawned run), never
                # the merged observed, so an older run's stale exit evidence still
                # on the row cannot settle this new retry.
                current_run_token = result.observed_values.get("run_token")
                exit_evidence = self._authenticated_same_run_exit(existing_observed, current_run_token)

                if recipient_status in ("acknowledged", "closed"):
                    # Paused-retry fast close/ack: the recipient already terminated
                    # the trigger before this retry commit landed. Settle the
                    # ledger directly to closed, preserve the mismatch/exit
                    # evidence, clear the stale spawn-failure reason, drop the
                    # lineage claim, and never publish a false in_flight.
                    merged["spawn_commit_recipient_terminal"] = recipient_status
                    merged["spawn_commit_settled_at"] = now
                    conn.execute(
                        """
                        update dispatch_ledger
                        set status = 'closed',
                            spawn_handle = ?,
                            spawned_at = ?,
                            closed_at = coalesce(closed_at, ?),
                            failure_reason = NULL,
                            observed_values_json = ?
                        where dispatch_id = ? and status = 'spawn_failed_message_landed'
                        """,
                        (
                            result.spawn_handle,
                            now,
                            now,
                            json.dumps(merged, sort_keys=True),
                            dispatch_id,
                        ),
                    )
                    row_dict = self._dispatch_by_id(conn, dispatch_id)
                    conn.commit()
                    return row_dict

                if exit_evidence is not None:
                    # Same-run worker exit already recorded but the trigger was
                    # never closed: a dead-before-close worker. DLQ directly with
                    # the exact reason rather than a false in_flight. The CAS
                    # re-proves the specific run token against the row so a stale
                    # older-run exit object can never DLQ this fresh retry.
                    merged["spawn_commit_settled_at"] = now
                    merged["early_dlq_evidence"] = self._capture_early_dlq_evidence(
                        conn, dispatch_id, context.dispatch, existing_observed
                    )
                    cursor = conn.execute(
                        """
                        update dispatch_ledger
                        set status = 'dlq',
                            spawn_handle = ?,
                            spawned_at = ?,
                            dlq_at = ?,
                            failure_reason = 'worker_exited_before_close',
                            observed_values_json = ?
                        where dispatch_id = ? and status = 'spawn_failed_message_landed'
                        """
                        + _SAME_RUN_EXIT_CAS_PREDICATE,
                        (
                            result.spawn_handle,
                            now,
                            now,
                            json.dumps(merged, sort_keys=True),
                            dispatch_id,
                            current_run_token,
                            current_run_token,
                        ),
                    )
                    if cursor.rowcount == 0:
                        # The row's stored exit token no longer matches the token
                        # we authenticated in-process. Never DLQ a different run:
                        # halt the orphan we just spawned and refuse loudly.
                        conn.rollback()
                        halt_note = self._halt_orphan(runtime_adapter, result)
                        raise ConcurrencyError(
                            f"retry_spawn for dispatch_id={dispatch_id} same-run exit CAS "
                            "did not match the authenticated run token; orphan spawn_handle="
                            f"{result.spawn_handle} {halt_note}"
                        )
                    row_dict = self._dispatch_by_id(conn, dispatch_id)
                    conn.commit()
                    return row_dict

                in_flight_count = self._producer_in_flight_count(conn, context.dispatch["producer_actor_id"])
                dispatch_cap = self._producer_dispatch_cap(conn, context.dispatch["producer_actor_id"])
                if in_flight_count >= dispatch_cap:
                    # Producer filled its cap between the pre-spawn check and this
                    # commit. Release the write lock BEFORE halting so no socket
                    # HALT / child wait runs under BEGIN IMMEDIATE, then re-open a
                    # short transaction to record the race and release lineage.
                    conn.rollback()
                    halt_outcome = self._halt_orphan(runtime_adapter, result)
                    conn.execute("begin immediate")
                    race_observed = dict(merged)
                    race_observed["retry_race_halted_at"] = now
                    race_observed["retry_race_halt_outcome"] = halt_outcome
                    cursor = conn.execute(
                        """
                        update dispatch_ledger
                        set observed_values_json = ?
                        where dispatch_id = ? and status = 'spawn_failed_message_landed'
                        """,
                        (
                            json.dumps(race_observed, sort_keys=True),
                            dispatch_id,
                        ),
                    )
                    if cursor.rowcount == 0:
                        observed_status = self._dispatch_status_or_missing(conn, dispatch_id)
                        conn.rollback()
                        raise ConcurrencyError(
                            f"retry_spawn for dispatch_id={dispatch_id} cannot proceed: "
                            f"producer {context.dispatch['producer_actor_id']} at cap "
                            f"{in_flight_count}/{dispatch_cap}; orphan spawn_handle="
                            f"{result.spawn_handle} {halt_outcome}; observed status={observed_status}"
                        )
                    conn.commit()
                    raise ConcurrencyError(
                        f"retry_spawn for dispatch_id={dispatch_id} cannot proceed: "
                        f"producer {context.dispatch['producer_actor_id']} at cap "
                        f"{in_flight_count}/{dispatch_cap}; orphan spawn_handle="
                        f"{result.spawn_handle} {halt_outcome}"
                    )

                cursor = conn.execute(
                    """
                    update dispatch_ledger
                    set status = 'in_flight',
                        spawn_handle = ?,
                        expected_close_by = ?,
                        spawned_at = ?,
                        failure_reason = NULL,
                        observed_values_json = ?
                    where dispatch_id = ? and status = 'spawn_failed_message_landed'
                    """,
                    (
                        result.spawn_handle,
                        context.expected_close_by,
                        now,
                        json.dumps(merged, sort_keys=True),
                        dispatch_id,
                    ),
                )
                if cursor.rowcount == 0:
                    conn.rollback()
                    halt_note = self._halt_orphan(runtime_adapter, result)
                    raise ConcurrencyError(
                        f"retry_spawn for dispatch_id={dispatch_id} discovered "
                        "concurrent state change; orphan spawn_handle="
                        f"{result.spawn_handle} {halt_note}"
                    )
                row_dict = self._dispatch_by_id(conn, dispatch_id)
                conn.commit()
                return self._annotate_key_drift_after_spawn(row_dict)
            except Exception:
                conn.rollback()
                raise

    # --------------------------------------------------------------------- #
    # Stage-2 sanctioned cancellation engine (internal primitives, T3-T6)
    # --------------------------------------------------------------------- #

    def request_cancellation(
        self,
        dispatch_id: str,
        *,
        requesting_actor_id: str,
        reason: str,
        authority: str,
        adapter_for_runtime: Callable[[str], RuntimeAdapter] | None = None,
    ) -> dict:
        """Internal cancellation request primitive: request -> exact terminal CAS.

        Under ``BEGIN IMMEDIATE`` this authorizes the caller (producer-only or
        credentialed-human-admin), bounds the reason, enforces idempotent
        same-request behaviour and conflicting-request refusal, and writes the
        durable namespaced cancellation object. It NEVER performs socket I/O,
        HALT/wait, paging, or queued promotion inside the write transaction.

        Every newly-created ``queued`` row has ``auth_lineage_claimed_at IS NULL``
        and therefore commits the exact ``not_started`` terminal cancellation
        (ledger + transport) atomically. A claimed queued row can only be legacy
        data and is conservatively treated as PENDING, as is an in-flight row.
        Cancel-vs-spawn safety comes from the spawn-commit CAS plus
        ``_halt_orphan``. When a live-termination ``adapter_for_runtime`` is
        supplied, PENDING cancellation drives authenticated termination and its
        terminal CAS OUTSIDE the transaction.
        """
        for field, value in (
            ("dispatch_id", dispatch_id), ("requesting_actor_id", requesting_actor_id),
            ("reason", reason), ("authority", authority),
        ):
            param_leak.assert_no_parameter_leak(field, value)
        self._db.init()
        if authority not in CANCELLATION_AUTHORITIES:
            raise ValidationError(f"unknown cancellation authority: {authority!r}")
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("cancellation reason must not be empty")
        if len(reason) > CANCELLATION_REASON_MAX:
            raise ValidationError(
                f"cancellation reason exceeds {CANCELLATION_REASON_MAX} characters"
            )

        now = utc_now()
        drive_needed = False
        # T8 admin producer notice is NO LONGER minted inside this request
        # transaction. This transaction persists ONLY the request / current
        # cancellation state and closes BEFORE any HALT (as T3/T4 require). The
        # single durable admin notice is claimed AFTER the optional
        # out-of-transaction drive returns, from the FRESH canonical row/result
        # (see ``_finish_cancellation`` / ``_claim_admin_cancellation_notice``), so
        # it truthfully describes a synchronously-confirmed cancellation as
        # confirmed/cancelled and an unconfirmed one as requested/pending, and a
        # notice-insert failure NEVER rolls back the already-durable request.
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                row = conn.execute(
                    "select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
                ).fetchone()
                if row is None:
                    raise ValidationError(f"unknown dispatch_id: {dispatch_id}")
                self._authorize_cancellation(conn, row, requesting_actor_id, authority)
                status = row["status"]
                observed = json.loads(row["observed_values_json"] or "{}")
                existing = observed.get(CANCELLATION_KEY)
                existing = existing if isinstance(existing, dict) else None

                # Terminal rows are never relabelled. A cancelled row persists
                # nothing more here but still falls through to the post-transaction
                # admin notice claim, so an identical replay can REPAIR a notice
                # whose earlier insert failed; a closed/dlq/spawn_failed row refuses.
                if status in DISPATCH_TERMINAL_STATUSES:
                    conn.commit()
                    if status != "cancelled":
                        raise CancellationStateError(
                            f"dispatch {dispatch_id} is terminal ({status}); a cancellation "
                            "never relabels a closed/dlq/spawn_failed row as cancelled"
                        )
                    # The fall-through notice repair mints/binds from the SUPPLIED
                    # admin identity and already-normalized reason, so a replay whose
                    # actor, authority, or reason does not match the persisted
                    # cancellation object must be refused here -- BEFORE any repair --
                    # rather than silently repairing under a mismatched identity. The
                    # read-only transaction already committed, so this refusal mutates
                    # nothing: no notice, no terminal rewrite, exact ledger bytes intact.
                    if existing is not None and (
                        existing.get("requested_by") != requesting_actor_id
                        or existing.get("authority") != authority
                        or existing.get("reason") != reason
                    ):
                        raise CancellationConflictError(
                            f"dispatch {dispatch_id} was already cancelled by "
                            f"{existing.get('requested_by')} ({existing.get('authority')}); a "
                            "different actor/authority/reason cannot repair its admin notice"
                        )
                else:
                    # Idempotency / conflict against an existing pending request.
                    if existing is not None and existing.get("state") == "requested":
                        if (
                            existing.get("requested_by") != requesting_actor_id
                            or existing.get("authority") != authority
                            or existing.get("reason") != reason
                        ):
                            conn.commit()
                            raise CancellationConflictError(
                                f"dispatch {dispatch_id} already has a pending cancellation from "
                                f"{existing.get('requested_by')} ({existing.get('authority')}); a "
                                "different actor/authority/reason cannot silently overwrite it"
                            )
                        # An exact same-actor/authority/reason replay carries the
                        # already-persisted request (and any notice already bound
                        # inside its cancellation object) forward; it never mints a
                        # second notice -- the post-transaction claim is idempotent.
                        cancellation = dict(existing)  # same request repeated: re-drive
                    else:
                        cancellation = {
                            "requested_at": now,
                            "requested_by": requesting_actor_id,
                            "authority": authority,
                            "reason": reason,
                            "previous_status": status,
                            "state": "requested",
                            "termination_result": None,
                            "attempts": 0,
                            "latest_attempt_at": None,
                            "latest_detail": None,
                            "confirmed_at": None,
                            "escalated_at": None,
                            "partial_evidence": None,
                        }

                    if status == "queued" and row["auth_lineage_claimed_at"] is None:
                        # Never selected for spawn: commit the exact ``not_started``
                        # terminal cancellation (ledger + transport) atomically and
                        # clear lineage. No notice inside this transaction; the admin
                        # notice is claimed afterward from the committed cancelled row.
                        cancellation["state"] = "confirmed"
                        cancellation["termination_result"] = "not_started"
                        cancellation["confirmed_at"] = now
                        cancellation["partial_evidence"] = self._capture_cancellation_evidence(
                            conn, dispatch_id, row, observed
                        )
                        observed[CANCELLATION_KEY] = cancellation
                        self._commit_confirmed_cancel(
                            conn, dispatch_id, row, observed, now, from_status="queued"
                        )
                        conn.commit()
                    else:
                        # Legacy claimed queued or in_flight: record/refresh the
                        # PENDING request only. No terminal write, socket I/O, notice,
                        # or promotion here; the drive (if any) runs outside this txn.
                        observed[CANCELLATION_KEY] = cancellation
                        conn.execute(
                            "update dispatch_ledger set observed_values_json = ? "
                            "where dispatch_id = ? and status = ?",
                            (json.dumps(observed, sort_keys=True), dispatch_id, status),
                        )
                        conn.commit()
                        drive_needed = True
            except Exception:
                conn.rollback()
                raise

        return self._finish_cancellation(
            dispatch_id,
            requesting_actor_id,
            reason,
            authority,
            now,
            drive_needed=drive_needed,
            adapter_for_runtime=adapter_for_runtime,
        )

    def _finish_cancellation(
        self,
        dispatch_id: str,
        requesting_actor_id: str,
        reason: str,
        authority: str,
        now: str,
        *,
        drive_needed: bool,
        adapter_for_runtime: Callable[[str], RuntimeAdapter] | None,
    ) -> dict:
        """Phases 2/3 of a request, strictly OUTSIDE the request transaction.

        Phase 2 drives the authenticated termination + terminal CAS (only when a
        pending request was recorded AND a live-termination adapter was supplied),
        so a reachable in-flight cancel can confirm synchronously; absent an adapter
        the request simply stays pending for the monitor. Phase 3 -- admin authority
        only -- claims the single durable producer notice from the FRESH canonical
        result. Ordering the notice claim AFTER the drive is what lets the notice
        describe a synchronously-confirmed cancellation as confirmed/cancelled and
        an unconfirmed one as requested/pending; no transaction is ever held across
        the HALT. Returns the canonical cancellation result.
        """
        if drive_needed and adapter_for_runtime is not None:
            self._drive_cancellation(dispatch_id, adapter_for_runtime, now=now)
        if authority == "admin":
            self._claim_admin_cancellation_notice(
                dispatch_id, requesting_actor_id, reason, now
            )
        return self._cancellation_result(dispatch_id)

    def _claim_admin_cancellation_notice(
        self, dispatch_id: str, admin_actor_id: str, reason: str, now: str
    ) -> None:
        """Insert + bind the SINGLE durable admin-cancellation producer notice.

        Runs AFTER the optional out-of-transaction drive returns, in its own short
        ``BEGIN IMMEDIATE`` transaction, from the FRESH canonical row/result -- so
        the wording describes that result (confirmed/cancelled vs requested/pending)
        rather than a pre-drive guess. It is concurrency-safe and idempotent: the
        in-lock re-read is the sole arbiter, so exactly one of any number of
        concurrent or replayed identical admin requests inserts the message /
        recipient / thread and binds it; every other caller re-reads the
        already-bound notice and writes NOTHING (no second message, recipient,
        thread, or wake semaphore) and converges on the same notice id. The producer
        wake semaphore is emitted ONLY by the caller that actually committed the
        notice, and only after that commit. A notice-insert failure rolls back ONLY
        this short transaction, leaving the already-durable cancellation request
        intact; the loud failure propagates and an identical replay repairs the
        missing notice exactly once. Because this transaction opens strictly AFTER
        the drive, it never holds or rolls back across a HALT. Producer-authority
        cancellations never reach here (only admin authority calls this), and even
        for an admin request against a row a producer already cancelled the mint is
        skipped (its cancellation ``authority`` is not ``admin``), so producer
        cancellation stays entirely notice-free. An existing notice is never
        rewritten on replay or after later monitor state changes.
        """
        # Cheap read-side short-circuit: a replay (or the loser of a concurrent
        # claim) whose notice is already bound does no write and opens no write
        # transaction, so it can never contend or rewrite a settled row/semaphore.
        with self._db.connection() as conn:
            pre = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        if pre is None:
            return
        pre_cancellation = json.loads(pre["observed_values_json"] or "{}").get(CANCELLATION_KEY)
        if not isinstance(pre_cancellation, dict):
            return
        if (
            pre_cancellation.get("authority") != "admin"
            or pre_cancellation.get("admin_notice_message_id") is not None
        ):
            return

        notice: dict | None = None
        producer_actor_id: str | None = None
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                row = conn.execute(
                    "select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
                ).fetchone()
                if row is None:
                    conn.commit()
                    return
                observed = json.loads(row["observed_values_json"] or "{}")
                cancellation = observed.get(CANCELLATION_KEY)
                if not isinstance(cancellation, dict) or cancellation.get("authority") != "admin":
                    conn.commit()
                    return
                # In-lock arbiter: under BEGIN IMMEDIATE serialization a concurrent
                # caller that already bound the notice makes this an exact no-op.
                if cancellation.get("admin_notice_message_id") is not None:
                    conn.commit()
                    return
                cancellation = dict(cancellation)
                # The notice describes the FRESH canonical result: the committed
                # ledger status and the current cancellation state.
                notice = self._insert_admin_cancellation_notice(
                    conn,
                    row,
                    cancellation,
                    admin_actor_id,
                    reason,
                    resulting_ledger_status=row["status"],
                    now=now,
                )
                producer_actor_id = row["producer_actor_id"]
                cancellation["admin_notice_message_id"] = notice["id"]
                cancellation["admin_notice_at"] = notice["created_at"]
                observed[CANCELLATION_KEY] = cancellation
                cursor = conn.execute(
                    "update dispatch_ledger set observed_values_json = ? "
                    "where dispatch_id = ? and json_extract("
                    "coalesce(nullif(observed_values_json, ''), '{}'), "
                    "'$.cancellation.admin_notice_message_id') is null",
                    (json.dumps(observed, sort_keys=True), dispatch_id),
                )
                if cursor.rowcount != 1:
                    # Unreachable under BEGIN IMMEDIATE serialization (the in-lock
                    # read above already proved no notice was bound); fail LOUD
                    # rather than publish a semaphore for a notice we did not bind.
                    raise ConcurrencyError(
                        f"admin cancellation notice bind for {dispatch_id} lost its exact "
                        "unbound-notice CAS"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        if notice is not None and producer_actor_id is not None:
            # Wake signal ONLY, emitted after the durable commit by the sole caller
            # that actually bound the notice; the authoritative notice is the
            # committed message + recipient copy.
            self._write_admin_cancellation_notice_semaphore(notice, producer_actor_id)

    def _settle_spawn_time_cancellation(
        self,
        context: DispatchContext,
        result: DispatchStart,
        runtime_adapter: RuntimeAdapter,
        *,
        existing_observed: dict | None = None,
    ) -> dict:
        """Settle a cancellation that raced a successful spawn, OUTSIDE any txn.

        The spawn produced a real orphan, but a cancellation was already pending,
        so ordinary in_flight is NEVER published, and this pending-cancellation
        settlement PRECEDES any bootstrap/early-exit classification of the row.
        Current persisted SQL evidence is authoritative: the spawn-result identity
        may FILL absent fields but never overwrites a conflicting current SQL
        value, so a stale spawn-result exit can never mask the current same-run
        exit. Exact CURRENT same-run SQL exit evidence -- an exit object already on
        the row whose token equals THIS spawn's run token -- confirms without a
        HALT (``same_run_exit_confirmed``); stale spawn-result evidence carrying a
        different run token cannot.

        Otherwise the exact orphan is HALTed with its authenticated identity, but
        the row's EXACT run identity is first persisted through a short observed
        CAS that COMMITS BEFORE the HALT (``_claim_spawn_run_identity_before_halt``)
        so the terminal commit later binds a token the row provably carried BEFORE
        the halt. If that pre-HALT claim loses -- the row is no longer queued, the
        spawn result carried no token, or a newer run already stamped a different
        token -- the stale spawn result is never HALTed or terminalized and the
        request is left pending. After the HALT the terminal cancel still requires
        EXACT EQUALITY with the claimed token, so a token cleared (A->NULL) or
        replaced (A->B) DURING the halt misses the terminal CAS and preserves the
        changed row with the cancellation unconfirmed; a token cleared during the
        halt is NEVER restored from the stale spawn result. A confirmed same-run
        exit persists the exact identity and commits ``cancelled`` in one
        transaction (no halt, so no drift window). An unconfirmed result (or a
        drifted terminal CAS) persists the control identity + spawn handle without
        clobbering a newer run and keeps the row pending (still ``queued``, lineage
        held) so the monitor retries the authenticated HALT.
        """
        dispatch_id = context.dispatch["dispatch_id"]
        run_token = result.observed_values.get("run_token")
        identity = dict(result.observed_values)
        # Current SQL evidence (captured under the spawn-commit lock) is
        # authoritative: layer the spawn-result identity UNDER it so a same-run
        # exit already on the row confirms while a stale spawn-result exit that
        # conflicts with current SQL never overwrites it. A stale exit carrying a
        # different run token never matches the exact-equality authentication.
        authoritative = {**identity, **(existing_observed or {})}

        did_halt = False
        pre_halt_claimed = False
        if self._complete_same_run_reap_proof(authoritative, run_token) is not None:
            # Same-run exit already recorded -> confirmed without a HALT. No token
            # can drift under us (no halt), so the exact identity is claimed and the
            # terminal commit lands together in the transaction below.
            confirmed, termination_result, detail = True, "same_run_exit_confirmed", None
        else:
            # Persist the EXACT spawn run identity BEFORE the authenticated HALT, in
            # its own short committed transaction. If the claim loses, never HALT or
            # terminalize with the stale spawn result: hold the request pending.
            claim = self._claim_spawn_run_identity_before_halt(dispatch_id, run_token)
            if claim == "not_queued":
                return self._dispatch_by_id_fresh(dispatch_id)
            if claim == "missed":
                confirmed, termination_result, detail = (
                    False,
                    None,
                    "spawn settlement: exact run identity not claimable before halt "
                    "(missing token or a newer run took the queued row); halt skipped",
                )
            else:
                pre_halt_claimed = True
                try:
                    runtime_adapter.halt(result.spawn_handle, identity)
                    confirmed, termination_result, detail = True, "supervised_halt_confirmed", None
                except Exception as exc:
                    confirmed, termination_result, detail = False, None, str(exc)
                did_halt = True

        now = utc_now()
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                row = conn.execute(
                    "select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
                ).fetchone()
                if row is None or row["status"] != "queued":
                    conn.commit()
                    return self._dispatch_by_id_fresh(dispatch_id)
                observed_now = json.loads(row["observed_values_json"] or "{}")
                # Current SQL wins on every conflicting key; the spawn-result
                # identity only fills absent fields (control identity for the
                # monitor to address the exact orphan on a retry). Record the spawn
                # handle for the same reason.
                observed = {**identity, **observed_now}
                observed["spawn_handle_at_cancel"] = result.spawn_handle
                if did_halt and "run_token" not in observed_now:
                    # The exact identity we CLAIMED before halting was cleared during
                    # the halt (A->NULL). Current SQL is authoritative: never restore
                    # the missing token from the stale spawn result.
                    observed.pop("run_token", None)
                # A same-run exit that landed (in current SQL) during the HALT
                # upgrades an unconfirmed halt to a confirmed same-run exit.
                if not confirmed and self._complete_same_run_reap_proof(observed, run_token) is not None:
                    confirmed, termination_result, detail = True, "same_run_exit_confirmed", detail
                cancellation = observed.get(CANCELLATION_KEY)
                cancellation = dict(cancellation) if isinstance(cancellation, dict) else {}
                cancellation["attempts"] = int(cancellation.get("attempts", 0)) + 1
                cancellation["latest_attempt_at"] = now
                cancellation["latest_detail"] = detail

                committed_confirmed = False
                if confirmed:
                    # The HALT path already persisted the exact run identity BEFORE
                    # halting -- never re-claim it here (a re-claim would RESTORE a
                    # token the halt cleared). The same-run-exit path persists it now,
                    # inside this transaction, since no halt ran to drift it.
                    claimed = (
                        True
                        if pre_halt_claimed
                        else self._claim_spawn_run_identity(conn, dispatch_id, run_token)
                    )
                    if claimed:
                        cancellation["state"] = "confirmed"
                        cancellation["termination_result"] = termination_result
                        cancellation["confirmed_at"] = now
                        cancellation["partial_evidence"] = self._capture_cancellation_evidence(
                            conn, dispatch_id, row, observed
                        )
                        observed[CANCELLATION_KEY] = cancellation
                        # Terminal commit binds the EXACT claimed run token by
                        # equality only, and re-proves the cancellation is still
                        # ``requested``: a token replaced (A->B)/cleared (A->NULL) OR
                        # a cancellation withdrawn/confirmed elsewhere during the halt
                        # misses the CAS, preserving the changed row.
                        rowcount = self._commit_confirmed_cancel(
                            conn,
                            dispatch_id,
                            row,
                            observed,
                            now,
                            from_status="queued",
                            run_token_predicate=True,
                            run_token=run_token,
                            require_requested_cancellation=True,
                        )
                        committed_confirmed = rowcount == 1

                if not committed_confirmed:
                    # Unconfirmed halt; an exact-identity claim that missed because a
                    # newer run took over the queued row; a token that drifted
                    # (A->B / A->NULL) during the halt; or a cancellation that drifted
                    # off ``requested`` before the pre-HALT claim or during the halt.
                    # Never terminalize a run we did not authenticate: keep the still
                    # pending request pending and preserve the row's current run
                    # identity (current SQL wins above, so a newer run's token is not
                    # clobbered and a cleared token is not restored). This diagnostic
                    # write itself carries the exact requested-state predicate, so on
                    # cancellation-state drift it is a NO-OP that preserves the winning
                    # row byte-for-byte and never RESTORES a withdrawn/confirmed
                    # request back to ``requested``.
                    cancellation["state"] = "requested"
                    cancellation["termination_result"] = None
                    observed[CANCELLATION_KEY] = cancellation
                    conn.execute(
                        """
                        update dispatch_ledger
                        set spawn_handle = ?, observed_values_json = ?
                        where dispatch_id = ? and status = 'queued'
                          and json_extract(coalesce(nullif(observed_values_json, ''), '{}'),
                                           '$.cancellation.state') = 'requested'
                        """,
                        (result.spawn_handle, json.dumps(observed, sort_keys=True), dispatch_id),
                    )
                row_dict = self._dispatch_by_id(conn, dispatch_id)
                conn.commit()
                return row_dict
            except Exception:
                conn.rollback()
                raise

    def _claim_spawn_run_identity_before_halt(
        self, dispatch_id: str, run_token: object
    ) -> str:
        """Claim the exact spawn run identity in a SHORT committed txn BEFORE HALT.

        Returns ``"not_queued"`` (the row is gone or no longer queued),
        ``"missed"`` (nothing claimable -- a missing/blank token or a newer run
        already carries a different token), or ``"claimed"``. Committing this
        transaction BEFORE the authenticated HALT means the later terminal commit
        binds a token the row provably carried before the halt, so a token cleared
        or replaced DURING the halt is caught by the terminal equality CAS and is
        never restored from the stale spawn result.
        """
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                row = conn.execute(
                    "select status from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
                ).fetchone()
                if row is None or row["status"] != "queued":
                    conn.commit()
                    return "not_queued"
                claimed = self._claim_spawn_run_identity(conn, dispatch_id, run_token)
                conn.commit()
                return "claimed" if claimed else "missed"
            except Exception:
                conn.rollback()
                raise

    def _claim_spawn_run_identity(
        self, conn: sqlite3.Connection, dispatch_id: str, run_token: object
    ) -> bool:
        """Persist a pre-spawn queued row's EXACT run identity via an observed CAS.

        A spawn-time confirmed cancellation must bind its terminal commit to the
        exact run token by equality only, but a queued row never persisted a run
        token. This exact observed-state CAS stamps ``run_token`` onto the row,
        requiring it still be ``queued``, still carry a ``requested`` cancellation,
        and carry NULL or this exact token; a row that already carries a DIFFERENT
        token is a newer run and the claim misses (returns ``False``) so the caller
        preserves it and never terminalizes a run it did not authenticate. A
        missing/blank token is never claimed. The requested-state predicate makes a
        cancellation that was withdrawn/confirmed elsewhere (drifted off
        ``requested``) between the spawn-commit re-read and this claim lose exactly
        like a token drift: the pre-HALT claim misses, so no HALT and no terminal
        commit ever bind to a request the row no longer carries.
        """
        if not isinstance(run_token, str) or not run_token:
            return False
        cursor = conn.execute(
            """
            update dispatch_ledger
            set observed_values_json = json_set(
              coalesce(nullif(observed_values_json, ''), '{}'), '$.run_token', ?)
            where dispatch_id = ? and status = 'queued'
              and json_extract(coalesce(nullif(observed_values_json, ''), '{}'),
                               '$.cancellation.state') = 'requested'
              and (
                json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.run_token') is null
                or json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.run_token') = ?
              )
            """,
            (run_token, dispatch_id, run_token),
        )
        return cursor.rowcount == 1

    def _authorize_cancellation(
        self, conn: sqlite3.Connection, row: sqlite3.Row, requesting_actor_id: str, authority: str
    ) -> None:
        if authority == "producer":
            if requesting_actor_id != row["producer_actor_id"]:
                raise CancellationAuthorizationError(
                    "producer cancellation requires the request actor to equal the dispatch "
                    f"producer {row['producer_actor_id']}, not {requesting_actor_id}"
                )
            self._actors._actor_row_by_id(conn, requesting_actor_id)
            return
        if authority == "admin":
            actor = self._actors._actor_row_by_id(conn, requesting_actor_id)
            if actor["kind"] != "human":
                raise CancellationAuthorizationError(
                    f"admin cancellation requires a human actor, not kind={actor['kind']}"
                )
            return
        raise ValidationError(f"unknown cancellation authority: {authority!r}")

    def _insert_admin_cancellation_notice(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        cancellation: dict,
        admin_actor_id: str,
        reason: str,
        *,
        resulting_ledger_status: str,
        now: str,
    ) -> dict:
        """Insert the single durable admin-cancellation producer notice in ``conn``.

        Emitted ONLY for ``authority='admin'`` after the registered-human
        authorization, in the short post-drive claim transaction
        (``_claim_admin_cancellation_notice``) so its wording reflects the FRESH
        canonical result and a notice-insert failure rolls back only the claim, not
        the already-durable cancellation request. The sender is the explicit human
        admin; the recipient is the dispatch producer; the message is threaded to
        the ORIGINAL dispatch message. The subject is deterministic
        (``Admin cancellation: <dispatch_id>``).
        The body carries the dispatch id, admin actor id, bounded reason, resulting
        cancellation state / current ledger status, and a TRUTHFUL termination
        line: a still-``requested`` request says termination is not confirmed and
        the ledger/lineage remain held; a ``confirmed`` cancellation states the
        confirmed termination result WITHOUT ever calling the outcome a DLQ. It
        never claims a native child died and never reuses the operator-settlement
        phrase. Returns the inserted message record (id/created_at/roots).
        """
        dispatch_id = row["dispatch_id"]
        state = cancellation.get("state")
        termination_result = cancellation.get("termination_result")
        if state == "confirmed":
            headline = "A credentialed human admin has cancelled this dispatch."
            termination_line = (
                f"termination_confirmed=yes; termination_result={termination_result}; "
                "the dispatch ledger is cancelled and its auth lineage released."
            )
            priority = "high"
        else:
            # A still-pending request is NEVER described as cancelled: the headline
            # states only that cancellation was REQUESTED and remains unconfirmed.
            headline = (
                "A credentialed human admin has REQUESTED cancellation of this "
                "dispatch; termination is not yet confirmed."
            )
            termination_line = (
                "termination_confirmed=no; termination is not confirmed; the ledger "
                "and its auth lineage remain HELD until an authenticated termination "
                "confirms."
            )
            priority = "blocker"
        body = (
            f"{headline}\n"
            f"dispatch_id={dispatch_id}\n"
            f"admin_actor_id={admin_actor_id}\n"
            f"reason={reason}\n"
            f"cancellation_state={state}\n"
            f"ledger_status={resulting_ledger_status}\n"
            f"{termination_line}"
        )
        return self._mailbox._insert_message(
            conn,
            admin_actor_id,
            [row["producer_actor_id"]],
            f"Admin cancellation: {dispatch_id}",
            body,
            [],
            priority,
            True,
            row["message_id"],
        )

    def _write_admin_cancellation_notice_semaphore(
        self, notice: dict, producer_actor_id: str
    ) -> None:
        """Emit the producer's new-message wake semaphore AFTER the durable commit."""
        self._mailbox._write_semaphores(
            notice["recipient_roots"],
            [producer_actor_id],
            notice["id"],
            notice["created_at"],
        )

    def _commit_confirmed_cancel(
        self,
        conn: sqlite3.Connection,
        dispatch_id: str,
        row: sqlite3.Row,
        observed: dict,
        now: str,
        *,
        from_status: str,
        run_token_predicate: bool = False,
        run_token: object = None,
        require_requested_cancellation: bool = False,
    ) -> int:
        """Commit ledger + transport ``cancelled`` under an already-open txn.

        The exact CAS binds the current status; when the confirmation rests on an
        authenticated HALT or same-run exit evidence it additionally re-proves the
        run token by EXACT EQUALITY. Exact-token means equality only: a missing
        (NULL) run token is never treated as equal to the halted/authenticated
        token, and a row carrying a DIFFERENT non-null run token (a newer run) is
        preserved (the CAS misses). Every HALT-derived terminal predicate here
        therefore commits only against the exact same run it authenticated. A
        pre-spawn ``queued`` row that never persisted a run token must first
        persist its exact run identity through the caller's own exact
        observed-state CAS before this equality predicate can match; this terminal
        commit never accepts the missing token as equal.

        ``require_requested_cancellation`` additionally re-proves the persisted
        cancellation is still ``state='requested'``. Every confirmed-cancellation
        terminal path (spawn-time settlement, the in-flight/normal drive, and the
        hard-TTL backstop) passes it so a cancellation withdrawn or confirmed by
        another writer DURING the authenticated HALT makes this commit MISS exactly
        like a token drift: the changed row is preserved and never terminalized on a
        request it no longer carries. The synchronous ``not_started`` confirmations
        (spawn-failure and the never-started queued path) do NOT pass it -- they
        confirm within the request/spawn-commit write lock where the persisted state
        may still be absent (first request) and cannot drift.

        The transport copy becomes ``cancelled`` in the SAME transaction, but ONLY
        when it is not already terminal: a recipient close that won its transport
        CAS first stays ``closed`` (the projection reports
        ``confirmed_cancel_transport_closed_first``). Returns the ledger rowcount.
        """
        params: list[object] = [now, json.dumps(observed, sort_keys=True), dispatch_id, from_status]
        predicate = ""
        if run_token_predicate:
            predicate = (
                " and json_extract(coalesce(nullif(observed_values_json, ''), '{}'), "
                "'$.run_token') = ?"
            )
            params.append(run_token)
        if require_requested_cancellation:
            predicate += (
                " and json_extract(coalesce(nullif(observed_values_json, ''), '{}'), "
                "'$.cancellation.state') = 'requested'"
            )
        cursor = conn.execute(
            f"""
            update dispatch_ledger
            set status = 'cancelled',
                cancelled_at = ?,
                observed_values_json = ?
            where dispatch_id = ? and status = ?{predicate}
            """,
            params,
        )
        if cursor.rowcount == 1:
            conn.execute(
                """
                update message_recipients
                set status = 'cancelled', cancelled_at = ?
                where message_id = ? and to_agent = ?
                  and status not in ('closed', 'cancelled')
                """,
                (now, row["message_id"], row["recipient_actor_id"]),
            )
        return cursor.rowcount

    def _drive_cancellation(
        self,
        dispatch_id: str,
        adapter_for_runtime: Callable[[str], RuntimeAdapter],
        *,
        now: str | None = None,
    ) -> dict:
        """Drive a pending cancellation: snapshot -> authenticated termination -> CAS.

        All adapter/socket I/O happens here, strictly OUTSIDE any write
        transaction. A claimed ``queued`` row whose control identity has not yet
        been published (bootstrap) is never inferred not-started and never HALTed;
        the request stays pending. An in_flight (or identity-bearing) row attempts
        the authenticated same-token HALT (or accepts exact same-run SQL exit
        evidence) and applies the exact terminal CAS.
        """
        with self._db.connection() as conn:
            row = conn.execute(
                """
                select d.dispatch_id, d.status, d.spawn_handle, d.auth_lineage_claimed_at,
                       d.observed_values_json, a.runtime
                from dispatch_ledger d
                join actors a on a.id = d.recipient_actor_id
                where d.dispatch_id = ?
                """,
                (dispatch_id,),
            ).fetchone()
        if row is None:
            return {"dispatch_id": dispatch_id, "status": "<missing>"}
        status = row["status"]
        if status in DISPATCH_TERMINAL_STATUSES or status not in ("queued", "in_flight"):
            return self._cancellation_result(dispatch_id)
        observed = json.loads(row["observed_values_json"] or "{}")
        control_socket = observed.get("control_socket")
        run_token = observed.get("run_token")
        has_identity = (
            isinstance(control_socket, str) and isinstance(run_token, str) and bool(run_token)
        )
        if status == "queued" and not has_identity:
            self._record_cancellation_attempt(
                dispatch_id, detail="bootstrap: control identity not published yet"
            )
            return self._cancellation_result(dispatch_id)
        outcome = self._attempt_cancellation_termination(
            adapter_for_runtime, str(row["runtime"]), row["spawn_handle"], observed, run_token
        )
        return self._apply_cancellation_cas(dispatch_id, outcome, now or utc_now())

    def _attempt_cancellation_termination(
        self,
        adapter_for_runtime: Callable[[str], RuntimeAdapter],
        runtime: str,
        spawn_handle: str | None,
        observed: dict,
        run_token: object,
    ) -> dict:
        """Attempt authenticated termination for a cancellation, OUTSIDE any txn.

        Exact same-run SQL exit evidence already on the row confirms termination
        WITHOUT a HALT (``same_run_exit_confirmed``). Otherwise the authenticated
        same-token adapter HALT is attempted: success is ``supervised_halt_confirmed``;
        a raised HALT, a missing spawn handle, or an unreachable supervisor is
        ``termination_not_confirmed`` (nonterminal). There is NO PID fallback.
        """
        # ``run_token`` is the EXACT token this attempt authenticates against and
        # (for a HALT) the token carried into the socket HALT. It is returned so
        # the terminal CAS can bind ``supervised_halt_confirmed`` to the halted
        # token: if the row's run token drifts before the commit, the CAS misses
        # and the newer run is preserved rather than terminalized on a stale HALT.
        if self._complete_same_run_reap_proof(observed, run_token) is not None:
            return {
                "confirmed": True,
                "termination_result": "same_run_exit_confirmed",
                "detail": None,
                "run_token": run_token,
            }
        if not spawn_handle:
            return {
                "confirmed": False,
                "termination_result": "termination_not_confirmed",
                "detail": "no spawn_handle to halt",
                "run_token": run_token,
            }
        try:
            adapter = adapter_for_runtime(runtime)
            adapter.halt(spawn_handle, observed)
        except Exception as exc:
            return {
                "confirmed": False,
                "termination_result": "termination_not_confirmed",
                "detail": str(exc),
                "run_token": run_token,
            }
        return {
            "confirmed": True,
            "termination_result": "supervised_halt_confirmed",
            "detail": None,
            "run_token": run_token,
        }

    def _apply_cancellation_cas(self, dispatch_id: str, outcome: dict, now: str) -> dict:
        """Apply the exact terminal cancellation CAS under a short BEGIN IMMEDIATE.

        First-committer wins: a row another writer already terminalized is never
        dragged back. A confirmed outcome commits ``cancelled`` (re-proving status,
        and the run token when the confirmation rests on same-run exit evidence),
        captures bounded partial-work evidence, and releases cap/lineage. An
        unconfirmed outcome records the attempt and holds the row nonterminal with
        cap/lineage retained; no queued promotion happens on the pending arm.
        """
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                row = conn.execute(
                    "select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
                ).fetchone()
                if row is None:
                    conn.commit()
                    return {"dispatch_id": dispatch_id, "status": "<missing>"}
                status = row["status"]
                if status in DISPATCH_TERMINAL_STATUSES or status not in ("queued", "in_flight"):
                    conn.commit()
                    return self._cancellation_result(dispatch_id)
                observed = json.loads(row["observed_values_json"] or "{}")
                cancellation = observed.get(CANCELLATION_KEY)
                cancellation = dict(cancellation) if isinstance(cancellation, dict) else {}
                run_token = observed.get("run_token")
                confirmed = bool(outcome.get("confirmed"))
                termination_result = outcome.get("termination_result")
                # An exit that landed during the HALT attempt upgrades an
                # unconfirmed halt to a confirmed same-run exit.
                if not confirmed and self._complete_same_run_reap_proof(observed, run_token) is not None:
                    confirmed = True
                    termination_result = "same_run_exit_confirmed"

                cancellation["attempts"] = int(cancellation.get("attempts", 0)) + 1
                cancellation["latest_attempt_at"] = now
                cancellation["latest_detail"] = outcome.get("detail")

                if not confirmed:
                    cancellation["state"] = "requested"
                    cancellation["termination_result"] = None
                    observed[CANCELLATION_KEY] = cancellation
                    conn.execute(
                        "update dispatch_ledger set observed_values_json = ? "
                        "where dispatch_id = ? and status = ?",
                        (json.dumps(observed, sort_keys=True), dispatch_id, status),
                    )
                    conn.commit()
                    return self._cancellation_result(dispatch_id)

                cancellation["state"] = "confirmed"
                cancellation["termination_result"] = termination_result
                cancellation["confirmed_at"] = now
                cancellation["partial_evidence"] = self._capture_cancellation_evidence(
                    conn, dispatch_id, row, observed
                )
                observed[CANCELLATION_KEY] = cancellation
                # EVERY confirmed terminal cancel is bound to the run token, not
                # just same-run exit: ``supervised_halt_confirmed`` binds the
                # EXACT token the HALT ran against (``outcome['run_token']``);
                # ``same_run_exit_confirmed`` binds the token its exit evidence
                # authenticated against (the row's current ``run_token``). A token
                # that drifted between the HALT and this commit misses the CAS,
                # preserving the newer run and leaving the cancellation pending. The
                # commit ALSO re-proves the cancellation is still ``requested`` so a
                # request withdrawn/confirmed by another writer during the HALT misses
                # the CAS exactly like a token drift (never a false ``cancelled``).
                bind_token = (
                    run_token
                    if termination_result == "same_run_exit_confirmed"
                    else outcome.get("run_token")
                )
                rowcount = self._commit_confirmed_cancel(
                    conn,
                    dispatch_id,
                    row,
                    observed,
                    now,
                    from_status=status,
                    run_token_predicate=True,
                    run_token=bind_token,
                    require_requested_cancellation=True,
                )
                if rowcount == 0:
                    conn.rollback()
                    # The run token drifted between the authenticated HALT and this
                    # terminal CAS, the cancellation drifted off ``requested``, or
                    # another writer won the terminal commit: preserve the newer/
                    # changed row and leave the cancellation unconfirmed. Record the
                    # drift on the still-pending request so the loud residue is
                    # visible; never terminalize the newer run. ``_record_run_token_
                    # drift`` itself carries an exact requested-state predicate, so a
                    # non-``requested`` (withdrawn/confirmed) row is preserved
                    # untouched rather than having its request state restored.
                    self._record_run_token_drift(dispatch_id, bind_token, now)
                    return self._cancellation_result(dispatch_id)
                conn.commit()
                return self._cancellation_result(dispatch_id)
            except Exception:
                conn.rollback()
                raise

    def _record_cancellation_attempt(self, dispatch_id: str, *, detail: str) -> None:
        """Bump a pending cancellation's bounded attempt diagnostic. No terminal write."""
        now = utc_now()
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                row = conn.execute(
                    "select status, observed_values_json from dispatch_ledger where dispatch_id = ?",
                    (dispatch_id,),
                ).fetchone()
                if row is None or row["status"] in DISPATCH_TERMINAL_STATUSES:
                    conn.commit()
                    return
                observed = json.loads(row["observed_values_json"] or "{}")
                cancellation = observed.get(CANCELLATION_KEY)
                if not isinstance(cancellation, dict):
                    conn.commit()
                    return
                cancellation = dict(cancellation)
                cancellation["attempts"] = int(cancellation.get("attempts", 0)) + 1
                cancellation["latest_attempt_at"] = now
                cancellation["latest_detail"] = detail
                observed[CANCELLATION_KEY] = cancellation
                conn.execute(
                    "update dispatch_ledger set observed_values_json = ? "
                    "where dispatch_id = ? and status = ?",
                    (json.dumps(observed, sort_keys=True), dispatch_id, row["status"]),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _record_run_token_drift(self, dispatch_id: str, halted_run_token: object, now: str) -> None:
        """Loudly record a HALT-vs-terminal-CAS run-token drift on a pending request.

        The terminal cancel CAS missed because the row's run token no longer
        matches the token the authenticated HALT ran against (a newer run took
        over) -- the newer row is preserved and the cancellation stays
        nonterminal. This bumps the pending request's bounded diagnostic so the
        residue is visible; it NEVER writes a terminal status.
        """
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                row = conn.execute(
                    "select status, observed_values_json from dispatch_ledger where dispatch_id = ?",
                    (dispatch_id,),
                ).fetchone()
                if row is None or row["status"] in DISPATCH_TERMINAL_STATUSES:
                    conn.commit()
                    return
                observed = json.loads(row["observed_values_json"] or "{}")
                cancellation = observed.get(CANCELLATION_KEY)
                if not isinstance(cancellation, dict) or cancellation.get("state") != "requested":
                    conn.commit()
                    return
                cancellation = dict(cancellation)
                cancellation["latest_attempt_at"] = now
                cancellation["latest_detail"] = (
                    f"terminal cancel CAS did not match halted run token {halted_run_token!r}; "
                    "newer run preserved, cancellation left unconfirmed"
                )
                observed[CANCELLATION_KEY] = cancellation
                conn.execute(
                    "update dispatch_ledger set observed_values_json = ? "
                    "where dispatch_id = ? and status = ?",
                    (json.dumps(observed, sort_keys=True), dispatch_id, row["status"]),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _capture_cancellation_evidence(
        self, conn: sqlite3.Connection, dispatch_id: str, row: sqlite3.Row, observed: dict
    ) -> dict:
        """Bounded SQL-backed partial-work evidence for a cancellation.

        Reuses the SAME bounded capture the early-DLQ path uses (reply ids, latest
        dispatch-bound status, worker log, same-run exit) but relabels the
        classification so a cancellation never masquerades as a worker exit. The
        evidence is triage material only; it is never treated as success.
        """
        evidence = self._capture_early_dlq_evidence(conn, dispatch_id, row, observed)
        evidence["classification"] = "cancellation_partial_work"
        return evidence

    def _cancellation_result(self, dispatch_id: str) -> dict:
        """Build a stable cancellation result dict from the fresh row."""
        row = self._dispatch_by_id_fresh(dispatch_id)
        cancellation = row["observed_values"].get(CANCELLATION_KEY)
        cancellation = cancellation if isinstance(cancellation, dict) else {}
        state = cancellation.get("state")
        return {
            "dispatch_id": dispatch_id,
            "status": row["status"],
            "previous_status": cancellation.get("previous_status"),
            "cancellation_state": state,
            "termination_result": cancellation.get("termination_result"),
            "lineage_released": state == "confirmed",
            "reason": cancellation.get("reason"),
            "authority": cancellation.get("authority"),
            # Present only for admin-authority cancellations; a same-request replay
            # returns the SAME bound notice id (no second notice was minted).
            "admin_notice_message_id": cancellation.get("admin_notice_message_id"),
        }

    # --------------------------------------------------------------------- #
    # T7 pinned admin ``settle-dispatch``: dry-run preview + sealed execution
    # --------------------------------------------------------------------- #
    # An operator emergency-settles a dispatch whose sanctioned cancellation is
    # stuck ``requested`` with termination UNCONFIRMED. This is NOT a cancel: it
    # releases the blocked ledger to the exceptional ``dlq``/``cancelled`` terminal
    # under ``operator_settled_termination_unconfirmed`` WITHOUT halting, signalling,
    # deleting residue, claiming the native child died, or marking the cancellation
    # confirmed. The dry run mints exactly one short-lived HMAC-sealed plan binding
    # the exact current snapshot; execution independently revalidates the credential,
    # re-verifies the plan, then re-reads and exactly compares every snapshot field
    # under one ``BEGIN IMMEDIATE`` before the terminal write. First committer wins;
    # an exact replay of a committed plan is the idempotent winner (even after
    # expiry); every drift/tamper/forgery/mismatch/expiry-before-first-success and a
    # missing release acknowledgement refuse without mutation.

    def _run_token_fingerprint(self, run_token: object) -> str | None:
        """A stable fingerprint of a run token, or ``None`` when absent.

        The snapshot binds the run token's fingerprint, never the raw token, so a
        new run (a drifted token) is detected as snapshot drift without exposing
        the token value in the shell-safe plan or the returned preview.
        """
        if not isinstance(run_token, str) or not run_token:
            return None
        return hashlib.sha256(run_token.encode("utf-8")).hexdigest()

    def _settlement_snapshot(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        """The exact settlement-relevant snapshot bound into a plan and re-compared.

        Binds the COMPLETE settlement-relevant identity: the producer and
        recipient actor IDs, the ledger status, the recipient-copy (transport)
        status, the spawn handle, the run-token FINGERPRINT (never the raw
        token), and the ENTIRE cancellation-request object (not merely its
        state). Every field is re-derived and compared exactly at execution: a
        changed producer/recipient binding, a changed ledger status, a recipient
        transport advance, a new spawn handle, a drifted run token, or any
        mutation of the cancellation object (an attempt, escalation, or a
        confirmed/withdrawn transition) shows up as drift and refuses the
        settlement without mutation.
        """
        observed = json.loads(row["observed_values_json"] or "{}")
        cancellation = observed.get(CANCELLATION_KEY)
        cancellation = cancellation if isinstance(cancellation, dict) else None
        transport = self._recipient_copy_status(
            conn, row["message_id"], row["recipient_actor_id"]
        )
        return {
            "producer_actor_id": row["producer_actor_id"],
            "recipient_actor_id": row["recipient_actor_id"],
            "dispatch_status": row["status"],
            "transport_status": transport,
            "spawn_handle": row["spawn_handle"],
            "run_token_fingerprint": self._run_token_fingerprint(observed.get("run_token")),
            "cancellation_request": cancellation,
        }

    def _require_settlement_eligible(self, dispatch_id: str, row: sqlite3.Row, snapshot: dict) -> None:
        """Refuse an ineligible settlement WITHOUT mutation.

        Eligible iff the row is still ``queued``/``in_flight`` AND carries a
        cancellation object whose state is ``requested`` (a pending, unconfirmed
        termination). Every other state (terminal row, no cancellation, or an
        already-confirmed cancellation) refuses loudly and never relabels.
        """
        status = row["status"]
        if status not in ("queued", "in_flight"):
            raise CancellationStateError(
                f"dispatch {dispatch_id} is {status}; an operator settlement applies "
                "only to a queued/in_flight row with a pending unconfirmed "
                "cancellation and never relabels another terminal row"
            )
        cancellation = snapshot["cancellation_request"]
        if not isinstance(cancellation, dict) or cancellation.get("state") != "requested":
            raise CancellationStateError(
                f"dispatch {dispatch_id} has no pending unconfirmed cancellation "
                "(cancellation object must exist with state == requested); an "
                "operator settlement refuses without mutation"
            )

    def _settle_execute_command(self, actor_id: str, dispatch_id: str, plan: str) -> str:
        """The exact literal executable settlement command a dry run prints.

        It carries the explicit actor, dispatch, the sealed plan behind the pinned
        ``--execute-plan`` flag, and the literal
        ``--release-with-termination-unconfirmed`` acknowledgement -- and NO
        caller-copied snapshot/expected-value arguments (execution re-derives and
        re-compares the snapshot from the plan). Every opaque value is shell-safe
        quoted with ``shlex.quote`` so a metacharacter-bearing (but valid) human
        actor ID, dispatch ID, or plan cannot break out of the copy/paste command.
        The credential is read from the operator's token file at call time and is
        never inlined.
        """
        return (
            'AGENT_COMMS_ADMIN_TOKEN="$(cat ~/.agent-comms/admin-token)" '
            '"$AGENT_COMMS_INSTALL_ROOT/bin/agent-comms" admin '
            "settle-dispatch "
            f"--from-actor-id {shlex.quote(actor_id)} "
            f"--dispatch-id {shlex.quote(dispatch_id)} "
            f"--execute-plan {shlex.quote(plan)} --release-with-termination-unconfirmed"
        )

    def settle_dispatch_preview(
        self,
        dispatch_id: str,
        *,
        actor_id: str,
        reason: str,
        secret: str,
        issued_at: str | None = None,
        nonce: str | None = None,
    ) -> dict:
        """DRY RUN: return the current settlement snapshot and one sealed plan.

        Performs NO application/ledger mutation. It reads the LIVE committed
        ledger through a strictly READ-ONLY connection to the EXISTING database
        (``mode=ro`` + private cache + ``query_only`` + an explicit read
        transaction) and NEVER runs schema initialization, so no parent directory,
        database file, schema, row, user_version, audit, or directory entry is
        created or changed. Because a ``mode=ro`` connection observes committed
        WAL frames (rather than the stale main image ``immutable`` would read),
        SQLite may read/create the ``-wal`` / ``-shm`` sidecars purely to
        coordinate the read; that SQLite-managed coordination is NOT an
        application mutation. Against an absent ledger the preview refuses cleanly
        as an unknown dispatch and leaves the filesystem untouched.
        It validates the explicit actor is a registered ``human``, requires
        eligibility (queued/in_flight with a pending unconfirmed cancellation),
        mints exactly one five-minute HMAC-sealed ``v1.<payload>.<hmac>`` plan
        (keyed by the verified admin credential) binding the exact snapshot, and
        returns the literal execution command. The secret is used ONLY as the
        HMAC key: it is never returned, printed, or persisted.
        """
        from .cli import settlement_plan  # deferred: avoids a cli<->store import cycle

        # No init(): a dry-run must not create or migrate anything. An absent
        # ledger is a clean refusal that leaves the filesystem untouched.
        if not self._db.db_path.exists():
            raise ValidationError(f"unknown dispatch_id: {dispatch_id}")
        reason = settlement_plan.normalize_reason(reason)
        now = utc_now()
        issued_at = issued_at or now
        nonce = nonce or uuid4().hex
        # The read-only connection observes the LIVE committed WAL state (see
        # Database.read_only_connection). If that live WAL cannot be opened/read
        # safely, refuse HERE -- before a plan is issued -- rather than mint a plan
        # from a stale main image. Business-rule refusals below raise
        # ValidationError/Cancellation* (never sqlite3.Error) and pass through.
        try:
            with self._db.read_only_connection_ctx() as conn:
                actor = self._actors._actor_row_by_id(conn, actor_id)
                if actor["kind"] != "human":
                    raise CancellationAuthorizationError(
                        f"admin settlement requires a human actor, not kind={actor['kind']}"
                    )
                row = conn.execute(
                    "select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
                ).fetchone()
                if row is None:
                    raise ValidationError(f"unknown dispatch_id: {dispatch_id}")
                snapshot = self._settlement_snapshot(conn, row)
                self._require_settlement_eligible(dispatch_id, row, snapshot)
        except sqlite3.Error as exc:
            raise ValidationError(
                "refusing to issue a settlement plan: the live agent-comms ledger "
                f"WAL could not be read safely ({exc})"
            ) from exc

        plan = settlement_plan.build_plan(
            secret=secret,
            actor_id=actor_id,
            dispatch_id=dispatch_id,
            reason=reason,
            snapshot=snapshot,
            issued_at=issued_at,
            nonce=nonce,
        )
        fingerprint = settlement_plan.plan_fingerprint(plan)
        expires_at = (
            datetime.fromisoformat(issued_at)
            + timedelta(seconds=settlement_plan.PLAN_TTL_SECONDS)
        ).isoformat(timespec="seconds")
        return {
            "mode": "dry_run",
            "dispatch_id": dispatch_id,
            "actor_id": actor_id,
            "reason": reason,
            "snapshot": snapshot,
            "plan": plan,
            "plan_fingerprint": fingerprint,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "execution_command": self._settle_execute_command(actor_id, dispatch_id, plan),
        }

    def settle_dispatch_execute(
        self,
        dispatch_id: str,
        *,
        actor_id: str,
        plan: str,
        secret: str,
        release_ack: bool,
        now: str | None = None,
    ) -> dict:
        """EXECUTION: re-verify the sealed plan and settle under one BEGIN IMMEDIATE.

        Refuses WITHOUT mutation on a missing release acknowledgement, tamper,
        forgery, a signed-but-noncanonical payload, a different/absent plan,
        actor/dispatch mismatch, a non-human/unknown actor, an
        expiry-before-first-success, or any snapshot drift. An EXACT replay of an
        already-committed plan returns the stored winner without mutation, even
        after the plan has expired -- but ONLY after the same strict encoding,
        signature, actor/dispatch binding, and registered-human checks that gate a
        first settlement, so a mismatched actor, unknown/non-human actor, bad
        signature, or bad credential can never obtain the replay winner. The
        verified secret is used ONLY as the HMAC key; it is never returned,
        persisted, or surfaced in an error.
        """
        from .cli import settlement_plan  # deferred: avoids a cli<->store import cycle

        # Validate the ENTIRE plan (release acknowledgement, structure, strict
        # encoding, signature, canonical JSON, and actor/dispatch binding) BEFORE
        # any Database initialization or connection, so a missing acknowledgement
        # or a malformed/tampered/forged/noncanonical/mis-bound plan refuses with
        # ZERO filesystem effect -- no parent directory, database file, schema, or
        # sidecar is ever created for an invalid request.
        if not release_ack:
            raise ValidationError(
                "settlement execution requires the literal "
                "--release-with-termination-unconfirmed acknowledgement"
            )
        now = now or utc_now()
        # The fingerprint is over the EXACT plan string; a malformed plan refuses
        # here (as a PlanFormatError) before any DB effect.
        fingerprint = settlement_plan.plan_fingerprint(plan)

        # Strict integrity + binding are enforced BEFORE any stored replay winner
        # can be returned. Expiry is deferred (``now=None``) so an exact committed
        # replay survives past the five-minute window, while a first use enforces
        # expiry below. Signature/canonical-encoding/actor/dispatch failures raise
        # here, so a tampered, forged, noncanonical, or mis-bound plan never
        # obtains the winner.
        claim = settlement_plan.verify_plan(
            secret=secret,
            plan=plan,
            now=None,
            expected_actor_id=actor_id,
            expected_dispatch_id=dispatch_id,
        )
        # The embedded reason must still be bounded/nonempty (defence in depth; a
        # plan minted by ``build_plan`` always is).
        settlement_plan.normalize_reason(claim["reason"])

        # The plan is valid: settlement now REQUIRES an existing ledger. Execution
        # is end-to-end mode=rw, NOT a probe: this actor-authorization open is the
        # FIRST database touch and every later settlement-execution connection (the
        # replay lookup and the terminal transaction below) opens the existing
        # ledger through the SAME non-creating ``mode=rw`` connector. No execution
        # path falls back to the ordinary create-capable ``connect()``/``init()``
        # (which would materialize the parent directory, database file, and
        # schema): an absent database or parent -- including a ledger removed
        # BETWEEN these stages -- refuses loudly and leaves the filesystem
        # untouched. Because the FIRST touch is this real authorization read (not a
        # throwaway existence probe), a validly signed plan against an absent
        # ledger refuses HERE without creation, preserving validation-before-
        # connection ordering.
        #
        # The explicit actor must STILL be registered kind == human, independent of
        # the plan binding -- also enforced before a replay winner is returned.
        with self._db.existing_connection() as conn:
            actor = self._actors._actor_row_by_id(conn, actor_id)
            if actor["kind"] != "human":
                raise CancellationAuthorizationError(
                    f"admin settlement requires a human actor, not kind={actor['kind']}"
                )

        # Idempotent replay: only AFTER strict encoding/signature/binding/human
        # validation may an exact already-committed plan return the stored winner,
        # with no mutation and regardless of expiry.
        replayed = self._settlement_winner_if_replayed(dispatch_id, fingerprint)
        if replayed is not None:
            return replayed

        # First use: enforce expiry now (a replay never reaches here). Expiry
        # before a first successful settlement refuses without mutation.
        if settlement_plan.plan_is_expired(claim, now):
            raise settlement_plan.PlanExpiredError(
                "plan expired before a first successful settlement"
            )
        return self._commit_settlement(dispatch_id, actor_id, plan, fingerprint, claim, now)

    def _settlement_winner_if_replayed(self, dispatch_id: str, fingerprint: str) -> dict | None:
        """Return the stored settlement winner iff this exact plan already committed.

        Matching is by ``plan_fingerprint`` (a SHA-256 over the exact plan string
        recorded only after a fully verified plan committed): an attacker cannot
        forge a different plan with the same fingerprint, and presenting the exact
        committed plan is precisely the idempotent replay. Returns ``None`` when no
        matching settlement exists so the caller proceeds to full verification.
        """
        # Settlement-execution stage: open the EXISTING ledger mode=rw (never the
        # create-capable connector), so a ledger deleted after actor authorization
        # refuses loudly here rather than being recreated for the replay lookup.
        with self._db.existing_connection() as conn:
            row = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        if row is None:
            return None
        settlement = json.loads(row["observed_values_json"] or "{}").get(SETTLEMENT_KEY)
        if isinstance(settlement, dict) and settlement.get("plan_fingerprint") == fingerprint:
            return self._settlement_result(dispatch_id)
        return None

    def _capture_settlement_evidence(
        self, conn: sqlite3.Connection, dispatch_id: str, row: sqlite3.Row, observed: dict
    ) -> dict:
        """Bounded SQL-backed partial-work evidence captured BEFORE the terminal write.

        Reuses the same bounded capture the early-DLQ / cancellation paths use but
        relabels the classification so an operator settlement never masquerades as a
        clean worker exit or a confirmed cancellation. Triage material only.
        """
        evidence = self._capture_early_dlq_evidence(conn, dispatch_id, row, observed)
        evidence["classification"] = "operator_settlement_partial_work"
        return evidence

    def _commit_settlement(
        self,
        dispatch_id: str,
        actor_id: str,
        plan: str,
        fingerprint: str,
        claim: dict,
        now: str,
    ) -> dict:
        """Atomic settlement under one short BEGIN IMMEDIATE. First committer wins.

        Re-reads and exactly compares EVERY embedded snapshot field, requires the
        row still queued/in_flight with a pending unconfirmed cancellation, captures
        bounded evidence, inserts the single durable producer blocker notice (its
        message + producer recipient copy) IN THE SAME TRANSACTION, then commits
        ledger ``dlq`` + transport ``cancelled`` with the exact
        ``operator_settled_termination_unconfirmed`` residue and a complete durable
        audit (including the notice's message ID), and releases cap/auth-lineage.
        The SAME transaction also binds the once-only producer-page observed state
        (``producer_page_message_id`` / ``producer_page_claimed_at`` /
        ``producer_paged_at``) to that exact atomic notice and its timestamp, so a
        later reconcile pass reads producer paging as already satisfied and never
        issues a second generic DLQ producer blocker for the settled row.

        Because the notice is inserted inside the settlement transaction, a durable
        settlement terminal can never exist without its durable producer notice:
        any notice-insertion failure rolls the entire settlement back (no orphan
        claim), and the winning commit produces EXACTLY ONE notice. A concurrent
        execution of the SAME plan that lost the race (or a row already carrying
        this settlement) returns the stored winner/notice identity WITHOUT a second
        mutation or notice.
        """
        snapshot = claim["snapshot"]
        reason = claim["reason"]
        nonce = claim["nonce"]
        notice: dict | None = None
        producer_actor_id: str | None = None
        # Terminal settlement-execution stage: the ``begin immediate`` transaction
        # opens the EXISTING ledger mode=rw (never the create-capable connector), so
        # a ledger deleted after the replay lookup refuses loudly here rather than
        # being recreated for the terminal write.
        with self._db.existing_connection() as conn:
            conn.execute("begin immediate")
            try:
                row = conn.execute(
                    "select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
                ).fetchone()
                if row is None:
                    conn.commit()
                    raise ValidationError(f"unknown dispatch_id: {dispatch_id}")
                observed = json.loads(row["observed_values_json"] or "{}")
                existing = observed.get(SETTLEMENT_KEY)
                if isinstance(existing, dict) and existing.get("plan_fingerprint") == fingerprint:
                    # A concurrent execution of THIS exact plan already committed
                    # under the lock: idempotent winner, no second mutation/notice.
                    conn.commit()
                    return self._settlement_result(dispatch_id)

                status = row["status"]
                current = self._settlement_snapshot(conn, row)
                # Re-prove eligibility under the write lock: a late safe-cancel or
                # hard-TTL writer that terminalized first is preserved (loud no-op),
                # and a different/replayed plan against an already-settled dlq refuses.
                self._require_settlement_eligible(dispatch_id, row, current)
                if current != snapshot:
                    conn.commit()
                    raise CancellationConflictError(
                        f"dispatch {dispatch_id} state drifted since the plan was issued; "
                        "the operator settlement refuses without mutation "
                        f"(plan snapshot={snapshot}, current={current})"
                    )

                evidence = self._capture_settlement_evidence(conn, dispatch_id, row, observed)
                # Insert the single durable producer notice INSIDE this transaction
                # BEFORE the terminal ledger write, so its message ID is bound into
                # the durable audit and a notice failure aborts the whole settlement.
                notice = self._insert_settlement_notice(conn, row, actor_id, reason, now)
                producer_actor_id = row["producer_actor_id"]
                audit = {
                    "actor_id": actor_id,
                    "reason": reason,
                    "snapshot": snapshot,
                    "plan_fingerprint": fingerprint,
                    "nonce": nonce,
                    "settled_at": now,
                    "failure_reason": SETTLEMENT_FAILURE_REASON,
                    "termination_result": SETTLEMENT_TERMINATION_RESULT,
                    "partial_evidence": evidence,
                    "producer_notice_message_id": notice["id"],
                }
                observed[SETTLEMENT_KEY] = audit
                observed["termination_result"] = SETTLEMENT_TERMINATION_RESULT
                observed["timeout_at"] = observed.get("timeout_at") or now
                # The atomic settlement notice IS this row's producer page: bind the
                # once-only producer-page observed state to that SAME committed notice
                # (its exact message ID and its timestamp) INSIDE this transaction, so
                # the durable terminal can never exist without a satisfied producer
                # page. A later monitor pass then reads producer paging as already
                # durably satisfied and its DLQ producer-paging collection skips the
                # operator-settled row, so it never issues a SECOND generic blocker
                # (nor a duplicate message/recipient/thread or semaphore rewrite).
                observed["producer_page_message_id"] = notice["id"]
                observed["producer_page_claimed_at"] = notice["created_at"]
                observed["producer_paged_at"] = notice["created_at"]

                cursor = conn.execute(
                    """
                    update dispatch_ledger
                    set status = 'dlq',
                        dlq_at = ?,
                        failure_reason = ?,
                        observed_values_json = ?
                    where dispatch_id = ? and status = ?
                    """,
                    (now, SETTLEMENT_FAILURE_REASON, json.dumps(observed, sort_keys=True), dispatch_id, status),
                )
                if cursor.rowcount != 1:
                    # Unreachable while holding the write lock (status was read under
                    # it), but never terminalize on a miss: roll back the whole
                    # transaction (including the notice) and preserve the winning row.
                    conn.rollback()
                    return self._settlement_result(dispatch_id)
                # Transport becomes ``cancelled`` in the SAME transaction, but never
                # over an already-terminal recipient copy (a close that won its CAS
                # first stays closed; the projection reports it distinctly).
                conn.execute(
                    """
                    update message_recipients
                    set status = 'cancelled', cancelled_at = ?
                    where message_id = ? and to_agent = ?
                      and status not in ('closed', 'cancelled')
                    """,
                    (now, row["message_id"], row["recipient_actor_id"]),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        if notice is not None and producer_actor_id is not None:
            # Best-effort wake signal ONLY, emitted after the durable commit. The
            # authoritative notice is the committed message + recipient copy above;
            # a semaphore failure never undoes a durable settlement/notice.
            self._write_settlement_notice_semaphore(notice, producer_actor_id)
        return self._settlement_result(dispatch_id)

    def _insert_settlement_notice(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        actor_id: str,
        reason: str,
        now: str,
    ) -> dict:
        """Insert the producer blocker notice message + recipient copy in ``conn``.

        Runs inside the settlement's ``BEGIN IMMEDIATE`` so the notice and the
        settlement commit or roll back together. Carries the literal facts
        ``ledger released; termination not confirmed`` and never claims the native
        child died. Returns the inserted message record (id/created_at/roots) so
        the caller binds the message ID into the audit and can emit a post-commit
        wake semaphore.
        """
        body = (
            "An operator emergency-settlement has released this dispatch's ledger.\n"
            f"dispatch_id={row['dispatch_id']}\n"
            f"recipient_actor_id={row['recipient_actor_id']}\n"
            f"reason={reason}\n"
            f"{TERMINATION_NOT_CONFIRMED_PHRASE}\n"
            "note=cap/lineage released; no native-child death is claimed; residue may remain."
        )
        return self._mailbox._insert_message(
            conn,
            actor_id,
            [row["producer_actor_id"]],
            f"[operator settled] {TERMINATION_NOT_CONFIRMED_PHRASE}: {row['dispatch_id']}",
            body,
            [],
            "blocker",
            True,
            row["message_id"],
        )

    def _write_settlement_notice_semaphore(self, notice: dict, producer_actor_id: str) -> None:
        """Emit the producer's new-message wake semaphore after the durable commit."""
        self._mailbox._write_semaphores(
            notice["recipient_roots"],
            [producer_actor_id],
            notice["id"],
            notice["created_at"],
        )

    def _settlement_result(self, dispatch_id: str) -> dict:
        """Build a stable settlement result dict from the fresh row.

        Reached only from settlement execution (replay winner and terminal
        commit), so it too reads through the non-creating ``mode=rw`` existing-
        ledger connection rather than the create-capable connector.
        """
        with self._db.existing_connection() as conn:
            row = self._dispatch_by_id(conn, dispatch_id)
            transport = self._recipient_copy_status(
                conn, row["message_id"], row["recipient_actor_id"]
            )
        settlement = row["observed_values"].get(SETTLEMENT_KEY)
        settlement = settlement if isinstance(settlement, dict) else {}
        projection = project_dispatch_transport(row["status"], transport or "")
        return {
            "mode": "execute",
            "dispatch_id": dispatch_id,
            "status": row["status"],
            "transport_status": transport,
            "outcome": projection["outcome"],
            "settled": bool(settlement),
            "settled_by": settlement.get("actor_id"),
            "reason": settlement.get("reason"),
            "plan_fingerprint": settlement.get("plan_fingerprint"),
            "termination_result": settlement.get("termination_result"),
            "producer_notice_message_id": settlement.get("producer_notice_message_id"),
            "failure_reason": row["failure_reason"],
            # Dispatch-lifetime lineage claims are retired; settlement has no
            # lineage lease to release.
            "lineage_released": bool(settlement),
        }

    def _reconcile_pending_cancellations(
        self,
        adapter_for_runtime: Callable[[str], RuntimeAdapter],
        human_actor_id: str | None,
        now: str,
    ) -> list[dict]:
        """Drive every pending cancellation before ordinary liveness/TTL.

        Snapshots the live rows carrying a pending cancellation request (no write
        lock), drives one bounded authenticated termination attempt per pass
        strictly OUTSIDE any transaction, and applies the exact terminal CAS. A
        confirmed attempt finishes ``cancelled`` (cap/lineage released so the
        ordinary queued drain later in the pass may promote a successor). A still
        unconfirmed request past its durable 60s deadline escalates ONCE (producer
        blocker + operator infra notice) without releasing cap/lineage. The
        existing hard-TTL backstop, which runs after this, still resolves an
        unconfirmed request through TTL+grace to the truthful DLQ residue.
        """
        actions: list[dict] = []
        with self._db.connection() as conn:
            snapshot = conn.execute(
                """
                select dispatch_id
                from dispatch_ledger
                where status in ('queued', 'in_flight')
                  and json_extract(coalesce(nullif(observed_values_json, ''), '{}'),
                                   '$.cancellation.state') = 'requested'
                order by created_at, dispatch_id
                """
            ).fetchall()

        for row in snapshot:
            dispatch_id = str(row["dispatch_id"])
            result = self._drive_cancellation(dispatch_id, adapter_for_runtime, now=now)
            actions.append(
                {
                    "dispatch_id": dispatch_id,
                    "status": (
                        "cancellation_confirmed"
                        if result.get("status") == "cancelled"
                        else "cancellation_pending"
                    ),
                    "termination_result": result.get("termination_result"),
                }
            )
            if result.get("status") == "cancelled":
                continue
            dispatch = self._dispatch_by_id_fresh(dispatch_id)
            if dispatch["status"] in ("queued", "in_flight"):
                actions.extend(self._escalate_pending_cancellation(dispatch, human_actor_id, now))
        return actions

    def _admin_settle_preview_command(self, dispatch_id: str) -> str:
        """The exact documented admin settlement PREVIEW command for a dispatch.

        The escalation notices carry this literal so the operator can reach the
        dry-run-first emergency settlement without copying hashes or timestamps.
        The settlement surface itself is out of scope for this slice; only the
        documented command text is rendered here.
        """
        return (
            'AGENT_COMMS_ADMIN_TOKEN="$(cat ~/.agent-comms/admin-token)" '
            '"$AGENT_COMMS_INSTALL_ROOT/bin/agent-comms" admin '
            "settle-dispatch --from-actor-id <ADMIN_ACTOR_ID> "
            f"--dispatch-id {dispatch_id} --reason \"<reason>\" --dry-run"
        )

    def _stamp_cancellation_escalated(self, dispatch_id: str, escalated_at: str) -> None:
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                row = conn.execute(
                    "select status, observed_values_json from dispatch_ledger where dispatch_id = ?",
                    (dispatch_id,),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return
                observed = json.loads(row["observed_values_json"] or "{}")
                cancellation = observed.get(CANCELLATION_KEY)
                if not isinstance(cancellation, dict):
                    conn.commit()
                    return
                cancellation = dict(cancellation)
                cancellation["escalated_at"] = escalated_at
                observed[CANCELLATION_KEY] = cancellation
                conn.execute(
                    "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                    (json.dumps(observed, sort_keys=True), dispatch_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _escalate_pending_cancellation(
        self, dispatch: dict, human_actor_id: str | None, now: str
    ) -> list[dict]:
        """Once-only 60s escalation for a still-pending cancellation.

        Sends one blocker page to the producer and one infra notice to the
        operator, each guarded by its own observed-values claim so both are sent
        at most once. Records ``escalated_at`` on the cancellation object. It NEVER
        releases cap/lineage or claims the native child died; single-lineage still
        prevents a queued successor from starting until confirmed cancel, hard-TTL
        resolution, or explicit admin settlement.
        """
        cancellation = dispatch["observed_values"].get(CANCELLATION_KEY) or {}
        requested_at = cancellation.get("requested_at")
        if not _cancellation_escalation_due(requested_at, now):
            return []
        dispatch_id = dispatch["dispatch_id"]
        # Operator notices for a cancellation escalation MUST use the explicitly
        # supplied, registered human operator. BEFORE any page claim or send the
        # supplied id is validated through the actor registry as both registered
        # AND kind == human: a missing, unknown, or agent-kind id fails/reports
        # loudly and is skipped -- we never select another human by sort order
        # (``_first_human_actor_id``) here, which could page the wrong operator
        # about a withdrawn dispatch, and validating first means no partial page
        # claim is ever left behind. With multiple registered humans only this
        # supplied id sends the producer blocker and receives the operator infra
        # notice. The unrelated legacy notice paths keep their own first-human
        # fallback; this correction is scoped to escalation only.
        try:
            sender = self._actors.require_human(human_actor_id)
        except ValidationError as exc:
            return [
                {
                    "dispatch_id": dispatch_id,
                    "status": "cancellation_escalation_skipped_no_human_actor",
                    "detail": str(exc),
                }
            ]
        preview_command = self._admin_settle_preview_command(dispatch_id)
        actions: list[dict] = []

        if self._claim_observed_page(
            dispatch_id,
            claim_key="cancellation_escalation_producer_page_claimed_at",
            message_key="cancellation_escalation_producer_page_message_id",
        ):
            body = (
                "A sanctioned cancellation has not confirmed termination within the 60s deadline.\n"
                f"dispatch_id={dispatch_id}\n"
                f"recipient_actor_id={dispatch['recipient_actor_id']}\n"
                "state=cancellation requested; termination NOT confirmed; cap/lineage still HELD.\n"
                "You may queue corrected work, but single-lineage prevents it from starting until "
                "confirmed cancel, hard-TTL resolution, or explicit admin settlement.\n"
                f"admin_settlement_preview={preview_command}"
            )
            page = self._mailbox.send_message(
                from_agent=sender,
                to_agents=[dispatch["producer_actor_id"]],
                subject=f"[cancellation escalated] termination not confirmed: {dispatch_id}",
                body=body,
                refs=[],
                priority="blocker",
                requires_ack=True,
                parent_message_id=dispatch["message_id"],
            )
            self._record_observed_page(
                dispatch_id,
                paged_key="cancellation_escalation_producer_paged_at",
                message_key="cancellation_escalation_producer_page_message_id",
                paged_at=page["created_at"],
                message_id=page["id"],
            )
            self._stamp_cancellation_escalated(dispatch_id, page["created_at"])
            actions.append(
                {
                    "dispatch_id": dispatch_id,
                    "status": "cancellation_escalated_producer",
                    "message_id": page["id"],
                }
            )

        if self._claim_observed_page(
            dispatch_id,
            claim_key="cancellation_escalation_operator_page_claimed_at",
            message_key="cancellation_escalation_operator_page_message_id",
        ):
            body = (
                "Infrastructure notice: a sanctioned cancellation is stuck unconfirmed past its "
                "60s deadline and may require the dry-run-first admin settlement.\n"
                f"dispatch_id={dispatch_id}\n"
                f"producer_actor_id={dispatch['producer_actor_id']}\n"
                f"recipient_actor_id={dispatch['recipient_actor_id']}\n"
                "note=cap/lineage remain HELD; no native-child death is claimed.\n"
                f"admin_settlement_preview={preview_command}"
            )
            page = self._mailbox.send_message(
                from_agent=dispatch["producer_actor_id"],
                to_agents=[sender],
                subject=f"[cancellation infra] unconfirmed cancellation past deadline: {dispatch_id}",
                body=body,
                refs=[],
                priority="blocker",
                requires_ack=True,
                parent_message_id=dispatch["message_id"],
            )
            self._record_observed_page(
                dispatch_id,
                paged_key="cancellation_escalation_operator_paged_at",
                message_key="cancellation_escalation_operator_page_message_id",
                paged_at=page["created_at"],
                message_id=page["id"],
            )
            actions.append(
                {
                    "dispatch_id": dispatch_id,
                    "status": "cancellation_escalated_operator",
                    "message_id": page["id"],
                }
            )
        return actions

    def reconcile_dispatches(
        self,
        adapter_for_runtime: Callable[[str], RuntimeAdapter],
        *,
        human_actor_id: str | None = None,
    ) -> list[dict]:
        self._db.init()
        actions: list[dict] = []
        pages: list[dict] = []
        mismatch_pages: list[dict] = []
        now = utc_now()
        # Pending sanctioned cancellations are processed BEFORE ordinary liveness /
        # TTL on every pass: a bounded authenticated HALT retry (or exact same-run
        # exit evidence) either finishes the terminal ``cancelled`` commit or holds
        # the row nonterminal, and a request past its durable 60s deadline escalates
        # once (never releasing cap/lineage). All socket I/O is OUTSIDE any txn.
        actions.extend(self._reconcile_pending_cancellations(adapter_for_runtime, human_actor_id, now))
        # Review dispatch-intent expiry (dispatch contract 17). The monitor
        # reaches this through the existing reconcile_dispatches call surface.
        actions.extend(self._reconcile_review_intents(now))
        # Pre-TTL supervised liveness reconciliation. Every pass snapshots the
        # in_flight rows without a write lock, performs adapter STATUS/socket I/O
        # strictly OUTSIDE any transaction, then applies the exact terminal CAS
        # under a short BEGIN IMMEDIATE. Early-DLQ'd rows are picked up once by
        # the DLQ producer-paging collection below.
        actions.extend(self._reconcile_supervised_liveness(adapter_for_runtime, human_actor_id, now))
        # Hard-TTL backstop. Runs BEFORE the shared write transaction so the
        # supervised halt (socket HALT -> SIGTERM/grace/SIGKILL -> wait, owned by
        # the wrapper) and any child wait never span BEGIN IMMEDIATE. Each
        # terminated/settled row is picked up once by the DLQ producer-paging
        # collection below.
        actions.extend(self._terminate_expired_dispatches(adapter_for_runtime, now))
        # Active, bounded same-token reconciliation of terminal ``dlq`` residue
        # that carries ``termination_not_confirmed``. A later positive same-run
        # probe (an authenticated HALT that now confirms, or a same-run exit that
        # since appeared) upgrades ONLY the termination observation + janitor
        # eligibility; the ledger status stays ``dlq``. All socket I/O is OUTSIDE
        # any txn; the re-probe count is bounded per row.
        actions.extend(self._reconcile_dlq_termination_residue(adapter_for_runtime, now))
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                seen_reply_actor_mismatches: set[str] = set()
                for row in conn.execute(
                    """
                    select
                      d.*,
                      m.id as mismatch_message_id,
                      m.from_agent as mismatch_from_agent
                    from dispatch_ledger d
                    join message_threads mt on mt.parent_message_id = d.message_id
                    join messages m on m.id = mt.message_id
                    join actors a on a.id = m.from_agent
                    where d.status = 'in_flight'
                      and a.kind = 'agent'
                      and m.from_agent not in (d.recipient_actor_id, d.producer_actor_id)
                    order by d.created_at, d.dispatch_id, m.created_at, m.id
                    """
                ).fetchall():
                    dispatch = self._dispatch_row(row)
                    if dispatch["dispatch_id"] in seen_reply_actor_mismatches:
                        continue
                    observed = dict(dispatch["observed_values"])
                    if "reply_actor_mismatch" in observed:
                        continue
                    seen_reply_actor_mismatches.add(dispatch["dispatch_id"])
                    mismatch = {
                        "message_id": row["mismatch_message_id"],
                        "from_agent": row["mismatch_from_agent"],
                        "detected_at": now,
                    }
                    observed["reply_actor_mismatch"] = mismatch
                    conn.execute(
                        """
                        update dispatch_ledger
                        set observed_values_json = json_set(
                          coalesce(nullif(observed_values_json, ''), '{}'),
                          '$.reply_actor_mismatch',
                          json(?)
                        )
                        where dispatch_id = ? and status = 'in_flight'
                        """,
                        (json.dumps(mismatch, sort_keys=True), dispatch["dispatch_id"]),
                    )
                    dispatch["observed_values"] = observed
                    mismatch_pages.append(dispatch)
                    actions.append({"dispatch_id": dispatch["dispatch_id"], "status": "reply_actor_mismatch"})

                for row in conn.execute(
                    """
                    select *
                    from dispatch_ledger
                    where status in ('dlq', 'spawn_failed_message_landed')
                      and json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.producer_paged_at') is null
                      and json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.producer_page_message_id') is null
                      and json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.producer_page_claimed_at') is null
                      and json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.human_paged_at') is null
                      and json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.human_page_message_id') is null
                      and json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.human_page_claimed_at') is null
                    order by coalesce(dlq_at, spawned_at, created_at), dispatch_id
                    """
                ).fetchall():
                    pages.append(self._dispatch_row(row))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        queued_start_actions = self.start_queued_dispatches(adapter_for_runtime, limit=16)
        actions.extend(queued_start_actions)
        actions.extend(self._page_old_unheld_queued_codex_dispatches(human_actor_id))
        stale_action = next(
            (action for action in queued_start_actions if action.get("status") == "stale_module_refused"),
            None,
        )
        if stale_action is not None:
            actions.extend(self._page_stale_queued_dispatches(str(stale_action.get("detail", "")), human_actor_id))
        for dispatch in self._unique_dispatches(pages):
            page = self._page_producer_for_dispatch(dispatch, human_actor_id)
            if page is not None:
                actions.append(page)
        for dispatch in self._unique_dispatches(mismatch_pages):
            page = self._page_producer_for_reply_actor_mismatch(dispatch, human_actor_id)
            if page is not None:
                actions.append(page)
        return actions

    def _reconcile_review_intents(self, now: str) -> list[dict]:
        """Abandon expired review dispatch intents under one short transaction.

        This JSON-blind monitor pass reads and writes no review JSON, so it
        supplies no durable probe and abandons only expired active rows
        (fifteen minutes after activation). A prepared row is never abandoned
        here -- proving it unpaired needs the durable JSON companion, which only
        the review-verb reconcile can read -- so a durable JSON+prepared crash
        pair can never be falsely classified as unpaired. Bound rows are never
        touched (their ledger row is authoritative).
        """
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                abandoned = review_intents.reconcile(conn, now)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return [
            {
                "review_intent_id": row["intent_id"],
                "idempotency_key": row["idempotency_key"],
                "status": "review_intent_abandoned",
                "expired_state": row["expired_state"],
            }
            for row in abandoned
        ]

    def _terminate_expired_dispatches(
        self,
        adapter_for_runtime: Callable[[str], RuntimeAdapter],
        now: str,
    ) -> list[dict]:
        """T7 hard-TTL backstop for in_flight rows past their deadline.

        Snapshot (no write lock) -> terminate the runtime child strictly OUTSIDE
        any transaction with an authenticated socket HALT (there is no PID-parsed
        / legacy signalling path) -> short CAS re-read. A concurrent recipient
        close wins ``closed``. A confirmed halt releases at the deadline; an
        unconfirmed / unreachable one is HELD until ``expected_close_by +
        kill_grace`` and only then RELEASED to ``dlq`` with the exact phrase
        ``ledger released; termination not confirmed`` -- never a false claim
        that the native child died.
        """
        actions: list[dict] = []
        with self._db.connection() as conn:
            snapshot = conn.execute(
                """
                select d.dispatch_id, d.spawn_handle, d.observed_values_json, a.runtime
                from dispatch_ledger d
                join actors a on a.id = d.recipient_actor_id
                where d.status = 'in_flight'
                  and d.expected_close_by is not null
                  and d.expected_close_by <= ?
                order by d.expected_close_by, d.dispatch_id
                """,
                (now,),
            ).fetchall()

        for row in snapshot:
            observed = json.loads(row["observed_values_json"] or "{}")
            outcome = self._halt_expired_child(
                adapter_for_runtime, str(row["runtime"]), row["spawn_handle"], observed
            )
            action = self._apply_ttl_termination_cas(str(row["dispatch_id"]), now, outcome)
            if action is not None:
                actions.append(action)
        return actions

    def _halt_expired_child(
        self,
        adapter_for_runtime: Callable[[str], RuntimeAdapter],
        runtime: str,
        spawn_handle: str | None,
        observed: dict,
    ) -> dict:
        """Terminate an expired dispatch's runtime child OUTSIDE any transaction.

        Always the authenticated socket HALT: ``observed_values`` carries the
        supervisor control identity so the wrapper stops its own bounded process
        group under the same run token. A row with no control identity, no
        spawn_handle, or a halt that cannot confirm termination is reported
        UNCONFIRMED (``confirmed=False``): the CAS still releases it -- but only
        at the kill-grace boundary and never under a false claim of native-child
        death. There is no PID-parsed / legacy signalling path. Never raises.
        """
        # The run token the HALT authenticates against is returned so a hard-TTL
        # HALT that confirms a row carrying a pending cancellation can commit the
        # truthful terminal ``cancelled`` bound to the EXACT halted token.
        run_token = observed.get("run_token")
        if not spawn_handle:
            return {
                "confirmed": False,
                "termination_result": "termination_not_confirmed",
                "failure_reason": f"timeout; {TERMINATION_NOT_CONFIRMED_PHRASE}",
                "detail": "no spawn_handle to halt",
                "run_token": run_token,
            }
        adapter = adapter_for_runtime(runtime)
        try:
            adapter.halt(spawn_handle, observed)
            return {
                "confirmed": True,
                "termination_result": "supervised_halt_confirmed",
                "failure_reason": "timeout",
                "detail": None,
                "run_token": run_token,
            }
        except Exception as exc:
            return {
                "confirmed": False,
                "termination_result": "termination_not_confirmed",
                "failure_reason": f"timeout; {TERMINATION_NOT_CONFIRMED_PHRASE}",
                "detail": str(exc),
                "run_token": run_token,
            }

    def _apply_ttl_termination_cas(self, dispatch_id: str, now: str, outcome: dict) -> dict | None:
        """Settle one expired dispatch under a short BEGIN IMMEDIATE re-read.

        First-committer wins: a row another writer already settled, or one whose
        deadline moved back, is never dragged back through the TTL. A recipient
        close that raced the halt wins ``closed``. A CONFIRMED halt releases to
        ``dlq`` at the deadline. An UNCONFIRMED / unreachable termination is HELD
        until ``expected_close_by + kill_grace`` so a reachable-but-slow HALT can
        still settle first; at/after that boundary it releases to ``dlq`` with
        the exact ``termination not confirmed`` phrase (death not claimed).
        """
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                row = conn.execute(
                    """
                    select status, message_id, recipient_actor_id, expected_close_by,
                           policy_version, observed_values_json
                    from dispatch_ledger
                    where dispatch_id = ?
                    """,
                    (dispatch_id,),
                ).fetchone()
                if row is None or row["status"] != "in_flight":
                    conn.commit()
                    return None
                if row["expected_close_by"] is None or row["expected_close_by"] > now:
                    conn.commit()
                    return None

                observed = json.loads(row["observed_values_json"] or "{}")
                # A recipient close that raced the hard-TTL halt settles first.
                # The decision CONSUMES the same single canonical projection that
                # reporting and pre-TTL liveness use rather than a second
                # hand-coded transport set: the ACTUAL observed pair (the row's
                # own ``in_flight`` execution status, never a synthesized state,
                # joined with the recipient transport copy) is classified, and the
                # stale-in_flight-with-terminal-recipient pair projects to
                # ``RECIPIENT_TERMINAL_LEDGER_OPEN`` (acknowledged / closed do; the
                # running sent / read and the withdrawn ``cancelled`` do not).
                # A v2 row settling through this pair skipped ``close_dispatch``:
                # that is the closeout-missing protocol failure and releases to
                # ``dlq``, never a result-less v2 ``closed``. Legacy rows
                # reconcile to ``closed``.
                recipient_status = self._recipient_copy_status(
                    conn, row["message_id"], row["recipient_actor_id"]
                )
                recipient_projection = project_dispatch_transport(row["status"], recipient_status)
                if recipient_projection["outcome"] == RECIPIENT_TERMINAL_LEDGER_OPEN:
                    if row["policy_version"] == "v2":
                        observed["closeout_missing_protocol_failure_at"] = now
                        conn.execute(
                            """
                            update dispatch_ledger
                            set status = 'dlq',
                                dlq_at = ?,
                                failure_reason = 'closeout_missing_protocol_failure',
                                observed_values_json = ?
                            where dispatch_id = ? and status = 'in_flight'
                            """,
                            (now, json.dumps(observed, sort_keys=True), dispatch_id),
                        )
                        conn.commit()
                        return {
                            "dispatch_id": dispatch_id,
                            "status": "dlq",
                            "outcome": recipient_projection["outcome"],
                        }
                    observed["closed_reconciled_at"] = now
                    observed["legacy_recipient_terminal_close"] = True
                    conn.execute(
                        """
                        update dispatch_ledger
                        set status = 'closed',
                            closed_at = coalesce(closed_at, ?),
                            observed_values_json = ?
                        where dispatch_id = ? and status = 'in_flight'
                        """,
                        (now, json.dumps(observed, sort_keys=True), dispatch_id),
                    )
                    conn.commit()
                    return {
                        "dispatch_id": dispatch_id,
                        "status": "closed",
                        "outcome": recipient_projection["outcome"],
                    }

                # A CONFIRMED hard-TTL HALT on a row carrying a pending
                # cancellation is the truthful ``cancelled`` terminal, not a plain
                # ``dlq``: an operator asked to withdraw the dispatch and this pass
                # confirmed the native child is gone (e.g. the initial cancellation
                # HALT failed transiently, but the backstop HALT confirmed). Commit
                # the confirmed cancel bound to the EXACT halted run token (a
                # drifted token misses and preserves the newer run). A row WITHOUT
                # a pending cancellation keeps ordinary TTL behaviour below.
                cancellation = observed.get(CANCELLATION_KEY)
                pending_cancellation = (
                    isinstance(cancellation, dict) and cancellation.get("state") == "requested"
                )
                if outcome.get("confirmed") and pending_cancellation:
                    cancellation = dict(cancellation)
                    cancellation["attempts"] = int(cancellation.get("attempts", 0)) + 1
                    cancellation["latest_attempt_at"] = now
                    cancellation["latest_detail"] = outcome.get("detail")
                    cancellation["state"] = "confirmed"
                    cancellation["termination_result"] = outcome["termination_result"]
                    cancellation["confirmed_at"] = now
                    cancellation["confirmed_via"] = "hard_ttl_backstop"
                    cancellation["partial_evidence"] = self._capture_cancellation_evidence(
                        conn, dispatch_id, row, observed
                    )
                    observed[CANCELLATION_KEY] = cancellation
                    observed["timeout_at"] = now
                    # The confirmed-cancel commit binds the EXACT halted run token AND
                    # re-proves the cancellation is still ``requested`` (the pending
                    # gate above already read it under this lock; the predicate keeps
                    # the shared invariant that every confirmed-cancellation terminal
                    # CAS loses on a cancellation-state drift exactly like a token
                    # drift). A miss falls through to ordinary TTL handling below.
                    rowcount = self._commit_confirmed_cancel(
                        conn,
                        dispatch_id,
                        row,
                        observed,
                        now,
                        from_status="in_flight",
                        run_token_predicate=True,
                        run_token=outcome.get("run_token"),
                        require_requested_cancellation=True,
                    )
                    if rowcount == 0:
                        # Run token drifted or another writer won: never terminalize
                        # the newer run. Fall through to ordinary TTL handling.
                        conn.rollback()
                        return None
                    conn.commit()
                    return {
                        "dispatch_id": dispatch_id,
                        "status": "cancelled",
                        "termination_result": outcome["termination_result"],
                        "via": "hard_ttl_confirmed_cancel",
                    }

                # Unconfirmed / unreachable termination holds the lineage until
                # the kill-grace boundary. Before it, refresh a bounded
                # diagnostic and hold in_flight; the lineage is not released and
                # single-flight is preserved.
                if not outcome.get("confirmed") and not _past_kill_grace_boundary(
                    row["expected_close_by"], now
                ):
                    hold = observed.get("hard_ttl_unconfirmed")
                    if not isinstance(hold, dict):
                        hold = {"first_held_at": now, "first_detail": outcome.get("detail")}
                    hold["latest_held_at"] = now
                    hold["latest_detail"] = outcome.get("detail")
                    observed["hard_ttl_unconfirmed"] = hold
                    conn.execute(
                        """
                        update dispatch_ledger
                        set observed_values_json = ?
                        where dispatch_id = ? and status = 'in_flight'
                        """,
                        (json.dumps(observed, sort_keys=True), dispatch_id),
                    )
                    conn.commit()
                    return {"dispatch_id": dispatch_id, "status": "termination_hold_pre_grace"}

                observed["timeout_at"] = now
                observed["termination_result"] = outcome["termination_result"]
                if outcome.get("detail"):
                    observed["termination_detail"] = outcome["detail"]
                conn.execute(
                    """
                    update dispatch_ledger
                    set status = 'dlq',
                        dlq_at = ?,
                        failure_reason = ?,
                        observed_values_json = ?
                    where dispatch_id = ? and status = 'in_flight'
                    """,
                    (now, outcome["failure_reason"], json.dumps(observed, sort_keys=True), dispatch_id),
                )
                conn.commit()
                return {"dispatch_id": dispatch_id, "status": "dlq"}
            except Exception:
                conn.rollback()
                raise

    def _reconcile_dlq_termination_residue(
        self,
        adapter_for_runtime: Callable[[str], RuntimeAdapter],
        now: str,
    ) -> list[dict]:
        """Deterministic, fairly-batched same-token re-probe of terminal ``dlq``
        ``termination_not_confirmed`` residue.

        A hard-TTL ``dlq`` released without confirming the native child died
        (``termination_not_confirmed``) is preserved loudly by the janitor. This
        pass actively re-probes such rows, strictly OUTSIDE any transaction, with
        the SAME authenticated run token: an exact same-run exit that since
        appeared, or an authenticated HALT that now confirms, is positive same-run
        evidence. When found, the CAS upgrades ONLY the observed
        ``termination_result`` (and therefore janitor eligibility) and is bound to
        the exact PROBED run token; the ledger status REMAINS ``dlq`` (a dlq is
        never resurrected to a live or cancelled state).

        Selection is a deterministic maximum-``DLQ_RESIDUE_REPROBE_BATCH``-rows
        batch (SQL ``LIMIT``) ordered by DURABLE last-attempt state: never-probed
        rows first, then least-recently-probed, with a stable ``dlq_at`` /
        ``dispatch_id`` tiebreak. There is NO lifetime attempt exclusion: repeated
        passes (and a fresh Store/monitor process after a restart, which re-reads
        the same durable order) progress fairly through more than one batch and no
        row starves. A row stays eligible forever until positive evidence appears
        or its state changes.
        """
        actions: list[dict] = []
        with self._db.connection() as conn:
            snapshot = conn.execute(
                """
                select d.dispatch_id, d.spawn_handle, d.observed_values_json, a.runtime
                from dispatch_ledger d
                join actors a on a.id = d.recipient_actor_id
                where d.status = 'dlq'
                  and json_extract(coalesce(nullif(d.observed_values_json, ''), '{}'),
                                   '$.termination_result') = 'termination_not_confirmed'
                  and json_extract(coalesce(nullif(d.observed_values_json, ''), '{}'),
                                   '$.control_socket') is not null
                  and json_extract(coalesce(nullif(d.observed_values_json, ''), '{}'),
                                   '$.run_token') is not null
                order by
                  (json_extract(coalesce(nullif(d.observed_values_json, ''), '{}'),
                                '$.dlq_residue_reprobe.latest_at') is not null),
                  json_extract(coalesce(nullif(d.observed_values_json, ''), '{}'),
                               '$.dlq_residue_reprobe.latest_at'),
                  d.dlq_at,
                  d.dispatch_id
                limit ?
                """,
                (DLQ_RESIDUE_REPROBE_BATCH,),
            ).fetchall()

        for row in snapshot:
            observed = json.loads(row["observed_values_json"] or "{}")
            # The PROBED token is the exact token this pass authenticated against;
            # it is carried into the CAS so a confirmed/unconfirmed effect only
            # lands while the row still carries this exact token (drift preserves).
            probed_run_token = observed.get("run_token")
            # Reuse the cancellation termination probe: exact same-run exit
            # evidence confirms without a HALT, otherwise the authenticated
            # same-token HALT is attempted. No PID fallback; never raises here.
            outcome = self._attempt_cancellation_termination(
                adapter_for_runtime, str(row["runtime"]), row["spawn_handle"], observed, probed_run_token
            )
            action = self._apply_dlq_residue_reprobe_cas(
                str(row["dispatch_id"]), outcome, now, probed_run_token
            )
            if action is not None:
                actions.append(action)
        return actions

    def _apply_dlq_residue_reprobe_cas(
        self, dispatch_id: str, outcome: dict, now: str, probed_run_token: object
    ) -> dict | None:
        """Apply one dlq-residue re-probe under a short BEGIN IMMEDIATE.

        Every effect is bound to the exact ``probed_run_token`` this pass
        authenticated against -- never a token re-read during the CAS. If the row
        no longer carries that exact token (drift: a mismatched or missing token),
        the row is PRESERVED unchanged: no attempt telemetry and no evidence from
        the stale probe are applied. Otherwise the ledger status NEVER changes: a
        confirmed probe upgrades only the observed ``termination_result`` (bound to
        the probed token, while the row is still ``dlq`` carrying
        ``termination_not_confirmed``) so the janitor's positive-evidence gate can
        reclaim the run directory; an unconfirmed probe bumps the cumulative
        re-probe counter (also bound to the probed token) and holds the residue.
        """
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                row = conn.execute(
                    "select status, observed_values_json from dispatch_ledger where dispatch_id = ?",
                    (dispatch_id,),
                ).fetchone()
                if row is None or row["status"] != "dlq":
                    conn.commit()
                    return None
                observed = json.loads(row["observed_values_json"] or "{}")
                if observed.get("termination_result") != "termination_not_confirmed":
                    # Already upgraded by a prior probe / another writer.
                    conn.commit()
                    return None
                current_run_token = observed.get("run_token")
                # Token drift: the row no longer carries the token this pass
                # probed. Preserve the row WITHOUT applying attempt telemetry or
                # evidence obtained under the stale probe.
                if (
                    not isinstance(probed_run_token, str)
                    or not probed_run_token
                    or current_run_token != probed_run_token
                ):
                    conn.commit()
                    return {
                        "dispatch_id": dispatch_id,
                        "status": "dlq_residue_reprobe_token_drift_preserved",
                    }
                reprobe = observed.get("dlq_residue_reprobe")
                reprobe = dict(reprobe) if isinstance(reprobe, dict) else {"first_at": now, "attempts": 0}
                reprobe["attempts"] = int(reprobe.get("attempts", 0)) + 1
                reprobe["latest_at"] = now
                reprobe["latest_detail"] = outcome.get("detail")

                confirmed = bool(outcome.get("confirmed"))
                termination_result = outcome.get("termination_result")
                # A same-run exit that appeared since the residue was written also
                # upgrades, authenticated against the PROBED token (object existence
                # alone is never enough: the token must match this pass's probe).
                if not confirmed and self._complete_same_run_reap_proof(observed, probed_run_token) is not None:
                    confirmed = True
                    termination_result = "same_run_exit_confirmed"

                if not confirmed:
                    observed["dlq_residue_reprobe"] = reprobe
                    # The telemetry bump requires the row still carry the probed
                    # token (drift preserves the residue untouched).
                    conn.execute(
                        """
                        update dispatch_ledger set observed_values_json = ?
                        where dispatch_id = ? and status = 'dlq'
                          and json_extract(coalesce(nullif(observed_values_json, ''), '{}'),
                                           '$.run_token') = ?
                        """,
                        (json.dumps(observed, sort_keys=True), dispatch_id, probed_run_token),
                    )
                    conn.commit()
                    return {
                        "dispatch_id": dispatch_id,
                        "status": "dlq_residue_reprobe_unconfirmed",
                        "attempts": reprobe["attempts"],
                    }

                reprobe["confirmed_at"] = now
                reprobe["confirmed_result"] = termination_result
                observed["dlq_residue_reprobe"] = reprobe
                observed["termination_result"] = termination_result
                observed["termination_reconciled_at"] = now
                # Upgrade the observation ONLY: status stays ``dlq``. Bind the
                # PROBED token and re-prove the residue marker on the pre-update row
                # so a taken-over row is never upgraded and a double pass is
                # idempotent.
                cursor = conn.execute(
                    """
                    update dispatch_ledger
                    set observed_values_json = ?
                    where dispatch_id = ? and status = 'dlq'
                      and json_extract(coalesce(nullif(observed_values_json, ''), '{}'),
                                       '$.termination_result') = 'termination_not_confirmed'
                      and json_extract(coalesce(nullif(observed_values_json, ''), '{}'),
                                       '$.run_token') = ?
                    """,
                    (json.dumps(observed, sort_keys=True), dispatch_id, probed_run_token),
                )
                if cursor.rowcount == 0:
                    conn.rollback()
                    return None
                conn.commit()
                return {
                    "dispatch_id": dispatch_id,
                    "status": "dlq_residue_termination_upgraded",
                    "termination_result": termination_result,
                }
            except Exception:
                conn.rollback()
                raise

    def _reconcile_supervised_liveness(
        self,
        adapter_for_runtime: Callable[[str], RuntimeAdapter],
        human_actor_id: str | None,
        now: str,
    ) -> list[dict]:
        """Per-pass, pre-TTL reconciliation of supervised in_flight rows.

        Snapshot (no write lock) -> adapter STATUS (outside any transaction) ->
        exact CAS under a short BEGIN IMMEDIATE. Only rows carrying a supervisor
        control identity are reconciled here; rows without one (legacy / adapter
        doubles that predate the STATUS contract) are left to the hard-TTL
        backstop. Rows already inside the hard-TTL window are also left to that
        backstop; this path only owns the pre-deadline window and never releases
        an unreachable lineage early.
        """
        actions: list[dict] = []
        with self._db.connection() as conn:
            snapshot = conn.execute(
                """
                select d.dispatch_id, d.spawn_handle, d.recipient_actor_id,
                       d.expected_close_by, d.observed_values_json, a.runtime
                from dispatch_ledger d
                join actors a on a.id = d.recipient_actor_id
                where d.status = 'in_flight'
                  and d.expected_close_by is not null
                  and d.expected_close_by > ?
                order by d.created_at, d.dispatch_id
                """,
                (now,),
            ).fetchall()

        for row in snapshot:
            observed = json.loads(row["observed_values_json"] or "{}")
            control_socket = observed.get("control_socket")
            run_token = observed.get("run_token")
            if not isinstance(control_socket, str) or not isinstance(run_token, str):
                continue
            # STATUS is read-only and runs strictly OUTSIDE any transaction.
            try:
                adapter = adapter_for_runtime(str(row["runtime"]))
                status_method = getattr(adapter, "status", None)
                if status_method is None:
                    continue
                status = status_method(row["spawn_handle"], observed)
                state = getattr(status, "state", None)
                detail = getattr(status, "detail", None)
            except Exception as exc:
                state = "supervisor_unreachable"
                detail = f"status probe raised: {exc}"
            action = self._apply_liveness_cas(str(row["dispatch_id"]), state, detail, now, human_actor_id)
            if action is not None:
                actions.append(action)
        return actions

    def _apply_liveness_cas(
        self,
        dispatch_id: str,
        state: str | None,
        detail: str | None,
        now: str,
        human_actor_id: str | None,
    ) -> dict | None:
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                row = conn.execute(
                    """
                    select status, message_id, recipient_actor_id, expected_close_by,
                           policy_version, observed_values_json
                    from dispatch_ledger
                    where dispatch_id = ?
                    """,
                    (dispatch_id,),
                ).fetchone()
                # A terminal state never transitions back to in_flight, so a row
                # another writer already settled is left untouched (first-committer
                # wins; the loser refuses).
                if row is None or row["status"] != "in_flight":
                    conn.commit()
                    return None
                if row["expected_close_by"] is not None and row["expected_close_by"] <= now:
                    conn.commit()
                    return None

                observed = json.loads(row["observed_values_json"] or "{}")
                recipient_status = self._recipient_copy_status(conn, row["message_id"], row["recipient_actor_id"])
                run_token = observed.get("run_token")
                exit_evidence = self._authenticated_same_run_exit(observed, run_token)

                # (a) recipient transport terminalized -> settle the stale
                # in_flight row. The decision CONSUMES the single
                # canonical projection instead of a hand-coded status set: the
                # ACTUAL observed pair (the row's own ``in_flight`` execution
                # status, never a synthesized state, joined with the recipient
                # transport copy) is classified, and the stale-in_flight-with-
                # terminal-recipient pair projects to ``RECIPIENT_TERMINAL_LEDGER_OPEN``
                # (acknowledged / closed do; the running sent / read and the
                # withdrawn ``cancelled`` do not). Reporting and monitoring share
                # one authority and can never diverge. A v2 row settling through
                # this pair skipped ``close_dispatch``: that is the
                # closeout-missing protocol failure and releases to ``dlq``,
                # never a result-less v2 ``closed``. Legacy rows reconcile to
                # ``closed``.
                recipient_projection = project_dispatch_transport(row["status"], recipient_status)
                if recipient_projection["outcome"] == RECIPIENT_TERMINAL_LEDGER_OPEN:
                    if row["policy_version"] == "v2":
                        observed["closeout_missing_protocol_failure_at"] = now
                        conn.execute(
                            """
                            update dispatch_ledger
                            set status = 'dlq',
                                dlq_at = ?,
                                failure_reason = 'closeout_missing_protocol_failure',
                                observed_values_json = ?
                            where dispatch_id = ? and status = 'in_flight'
                            """,
                            (now, json.dumps(observed, sort_keys=True), dispatch_id),
                        )
                        conn.commit()
                        return {
                            "dispatch_id": dispatch_id,
                            "status": "dlq",
                            "reconcile": "recipient_terminal",
                            "outcome": recipient_projection["outcome"],
                        }
                    observed["liveness_reconciled_closed_at"] = now
                    observed["legacy_recipient_terminal_close"] = True
                    conn.execute(
                        """
                        update dispatch_ledger
                        set status = 'closed',
                            closed_at = coalesce(closed_at, ?),
                            observed_values_json = ?
                        where dispatch_id = ? and status = 'in_flight'
                        """,
                        (now, json.dumps(observed, sort_keys=True), dispatch_id),
                    )
                    conn.commit()
                    return {
                        "dispatch_id": dispatch_id,
                        "status": "closed",
                        "reconcile": "recipient_terminal",
                        "outcome": recipient_projection["outcome"],
                    }

                # (b) same-run exit evidence -> early DLQ with bounded evidence.
                # Authenticated by exact run-token match above, and the CAS
                # re-proves that same token against the row it updates so a
                # concurrent retry / new run can never be DLQ'd on an older run's
                # exit object (ABA-safe: object existence alone is never enough).
                #
                # A row carrying a PENDING sanctioned cancellation whose exit
                # evidence includes the COMPLETE exact-current-token v1 reaper
                # proof is the measured stage2-003 race: the cancellation HALT
                # could not confirm (the worker was already being background-
                # reaped), the proof landed afterwards, and this liveness pass is
                # the first writer to see it. The truthful terminal is the
                # confirmed ``cancelled`` -- an operator asked to withdraw the
                # dispatch and the required proof now exists -- never the
                # ordinary ``worker_exited_before_close`` DLQ. Only the complete
                # proof qualifies to CONFIRM; while the request is pending and
                # the same-run exit is visible but the complete proof has not
                # landed yet (the second measured interleaving: both family
                # packets show the proof arriving moments after this pass), the
                # row HOLDS nonterminal so a later pass can consume the
                # completed proof -- an early DLQ here would erase the
                # sanctioned withdrawal it races. Pending-cancellation
                # escalation and the hard-TTL+grace backstop remain the
                # truthful resolution if the proof never completes. Without a
                # pending request the same bare/incomplete evidence keeps the
                # ordinary DLQ below, and a wrong-token/malformed proof never
                # confirms.
                if exit_evidence is not None:
                    cancellation = observed.get(CANCELLATION_KEY)
                    if isinstance(cancellation, dict) and cancellation.get("state") == "requested":
                        if self._complete_same_run_reap_proof(observed, run_token) is None:
                            conn.commit()
                            return None
                        cancellation = dict(cancellation)
                        cancellation["state"] = "confirmed"
                        cancellation["termination_result"] = "same_run_exit_confirmed"
                        cancellation["confirmed_at"] = now
                        cancellation["confirmed_via"] = "liveness_reconciliation"
                        cancellation["partial_evidence"] = self._capture_cancellation_evidence(
                            conn, dispatch_id, row, observed
                        )
                        observed[CANCELLATION_KEY] = cancellation
                        # The shared confirmed-cancel commit re-proves the exact
                        # run token AND the still-``requested`` state, so a token
                        # or request drift loses exactly like every other
                        # confirmed-cancellation terminal CAS.
                        rowcount = self._commit_confirmed_cancel(
                            conn,
                            dispatch_id,
                            row,
                            observed,
                            now,
                            from_status="in_flight",
                            run_token_predicate=True,
                            run_token=run_token,
                            require_requested_cancellation=True,
                        )
                        if rowcount == 0:
                            # Never DLQ on this pass after a lost commit: hold the
                            # row and let the next pass re-read the winner.
                            conn.rollback()
                            return None
                        conn.commit()
                        return {
                            "dispatch_id": dispatch_id,
                            "status": "cancelled",
                            "reconcile": "cancellation_confirmed_same_run_exit",
                        }
                    observed["early_dlq_evidence"] = self._capture_early_dlq_evidence(
                        conn, dispatch_id, row, observed
                    )
                    observed["termination_result"] = "worker_exited_before_close"
                    observed["early_dlq_at"] = now
                    cursor = conn.execute(
                        """
                        update dispatch_ledger
                        set status = 'dlq',
                            dlq_at = ?,
                            failure_reason = 'worker_exited_before_close',
                            observed_values_json = ?
                        where dispatch_id = ? and status = 'in_flight'
                        """
                        + _SAME_RUN_EXIT_CAS_PREDICATE,
                        (now, json.dumps(observed, sort_keys=True), dispatch_id, run_token, run_token),
                    )
                    if cursor.rowcount == 0:
                        # The row's stored exit token changed under the lock: never
                        # DLQ a different run on this evidence. Hold, no terminal.
                        conn.commit()
                        return None
                    conn.commit()
                    return {
                        "dispatch_id": dispatch_id,
                        "status": "dlq",
                        "reconcile": "worker_exited_before_close",
                    }

                # (c) authenticated running -> no terminal write.
                if state == "running":
                    conn.commit()
                    return None

                # (d) supervisor unreachable -> no terminal write; bounded
                # first/latest diagnostic; hold pre-TTL. STATUS 'exited' without a
                # same-run SQL exit predicate is treated the same way: liveness is
                # never inferred dead without the evidence the DLQ requires.
                diagnostic = observed.get("supervisor_unreachable")
                if not isinstance(diagnostic, dict):
                    diagnostic = {"first_detected_at": now, "first_detail": detail}
                diagnostic["latest_detected_at"] = now
                diagnostic["latest_detail"] = detail
                observed["supervisor_unreachable"] = diagnostic
                conn.execute(
                    """
                    update dispatch_ledger
                    set observed_values_json = ?
                    where dispatch_id = ? and status = 'in_flight'
                    """,
                    (json.dumps(observed, sort_keys=True), dispatch_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        # The once-only unreachable producer warning is a separate short claim,
        # never sent while a write transaction is held.
        return self._warn_producer_unreachable(dispatch_id, human_actor_id)

    def _capture_early_dlq_evidence(
        self, conn: sqlite3.Connection, dispatch_id: str, row: sqlite3.Row, observed: dict
    ) -> dict:
        """Bounded, SQL-backed triage evidence for an early DLQ. Reads only.

        Persists at most ``EVIDENCE_REPLY_ID_CAP`` recipient reply message IDs
        (deterministically ordered by ``created_at`` then ``id``) plus the EXACT
        total reply count as ``reply_total_count``, so a page render reports the
        omitted remainder without inlining every id. Reply BODIES are never read
        or persisted. The copied latest status summary is bounded to the same
        200-character display ceiling the page render applies; the authoritative
        ``latest_status_id`` is preserved unbounded.
        """
        reply_ids = [
            reply["id"]
            for reply in conn.execute(
                """
                select m.id
                from messages m
                join message_threads mt on mt.message_id = m.id
                where mt.parent_message_id = ? and m.from_agent = ?
                order by m.created_at, m.id
                limit ?
                """,
                (row["message_id"], row["recipient_actor_id"], EVIDENCE_REPLY_ID_CAP),
            ).fetchall()
        ]
        # The exact total is counted separately from the bounded id list so the
        # page render can announce the omitted remainder truthfully.
        reply_total_count = conn.execute(
            """
            select count(*) as c
            from messages m
            join message_threads mt on mt.message_id = m.id
            where mt.parent_message_id = ? and m.from_agent = ?
            """,
            (row["message_id"], row["recipient_actor_id"]),
        ).fetchone()["c"]
        status_row = conn.execute(
            """
            select id, summary
            from statuses
            where dispatch_id = ?
            order by created_at desc, id desc
            limit 1
            """,
            (dispatch_id,),
        ).fetchone()
        summary = status_row["summary"] if status_row is not None else None
        if isinstance(summary, str):
            summary = summary[:EVIDENCE_SUMMARY_DISPLAY_CAP]
        return {
            "classification": "worker_exited_before_close",
            "reply_message_ids": reply_ids,
            "reply_total_count": reply_total_count,
            "latest_status_id": status_row["id"] if status_row is not None else None,
            "latest_status_summary": summary,
            "worker_exit": observed.get("worker_exit") or observed.get("reaper_exit"),
            "worker_log": observed.get("worker_log"),
        }

    def _warn_producer_unreachable(self, dispatch_id: str, human_actor_id: str | None) -> dict | None:
        """Distinct once-only pre-TTL producer warning for an unreachable supervisor.

        It names the dispatch, its current deadline, and the stage-2/manual
        escalation limitation. It does not masquerade as a DLQ and does not free
        a lineage: the ledger stays in_flight and held.
        """
        dispatch = self._dispatch_by_id_fresh(dispatch_id)
        if dispatch["status"] != "in_flight":
            return None
        sender = human_actor_id or self._actors._first_human_actor_id()
        if sender is None:
            return {"dispatch_id": dispatch_id, "status": "supervisor_unreachable_warn_skipped_no_human_actor"}
        if not self._claim_observed_page(
            dispatch_id,
            claim_key="supervisor_unreachable_page_claimed_at",
            message_key="supervisor_unreachable_page_message_id",
            status="in_flight",
        ):
            return None
        subject = f"[dispatch warning] supervisor unreachable pre-TTL: {dispatch_id}"
        body = (
            "A supervised dispatch's per-dispatch supervisor is unreachable and its liveness "
            "cannot be confirmed. The ledger is HELD, not released, until the hard TTL.\n"
            f"dispatch_id={dispatch_id}\n"
            f"recipient_actor_id={dispatch['recipient_actor_id']}\n"
            f"expected_close_by={dispatch['expected_close_by']}\n"
            "escalation=stage-2/manual recovery owns dead-supervisor orphan handling; "
            "there is no automatic pre-TTL release."
        )
        page = self._mailbox.send_message(
            from_agent=sender,
            to_agents=[dispatch["producer_actor_id"]],
            subject=subject,
            body=body,
            refs=[],
            priority="blocker",
            requires_ack=True,
            parent_message_id=dispatch["message_id"],
        )
        self._record_observed_page(
            dispatch_id,
            paged_key="supervisor_unreachable_paged_at",
            message_key="supervisor_unreachable_page_message_id",
            paged_at=page["created_at"],
            message_id=page["id"],
            status="in_flight",
        )
        return {"dispatch_id": dispatch_id, "status": "supervisor_unreachable_warned", "message_id": page["id"]}

    def _authorize_dispatch(self, producer: sqlite3.Row, target: sqlite3.Row, override_reason: str | None) -> None:
        if target["kind"] != "agent":
            raise ValidationError("dispatch target must be an agent actor")
        if producer["kind"] == "human":
            if not override_reason or not override_reason.strip():
                raise ValidationError("operator_override requires override_reason")
            return
        if producer["kind"] == "agent" and producer["role"] == "architect":
            if target["role"] != "worker":
                raise ValidationError("architect dispatch target must be a worker")
            if target["owner_actor_id"] != producer["id"]:
                raise ValidationError("architect dispatch target must be a worker it owns")
            return
        raise ValidationError("producer is not allowed to dispatch")

    def _probe_idempotent_replay(
        self, producer_actor_id: str, target_actor_id: str, idempotency_key: str
    ) -> dict | None:
        """Read-only idempotency pre-probe outside any write transaction.

        A visible row for the same producer+key and the same target is
        returned unchanged; the same key aimed at a different target refuses
        exactly like the in-transaction check. No source file is touched.
        """
        with self._db.connection() as conn:
            existing = self._dispatch_by_idempotency_key(conn, producer_actor_id, idempotency_key)
        if existing is None:
            return None
        if existing["recipient_actor_id"] != target_actor_id:
            raise ValidationError(
                f"idempotency_key {idempotency_key!r} for producer {producer_actor_id} "
                f"already dispatched to {existing['recipient_actor_id']}, not {target_actor_id}; "
                "reuse of a key for a different target is not allowed"
            )
        return existing

    def _resolve_payload_source_root(self, producer_actor_id: str, source_root: str | None) -> Path:
        """Resolve the payload source root from the producer registration.

        An agent producer always resolves to its registered ``project_root``
        (an explicit root must resolve to that exact registered root). A
        non-agent producer under the admin CLI's local-OS trust boundary must
        supply an explicit absolute root; the working directory is never
        inferred in either mode.
        """
        with self._db.connection() as conn:
            producer = self._actors._actor_row_by_id(conn, producer_actor_id)
        if producer["kind"] == "agent":
            registered = producer["project_root"]
            if not registered:
                raise ValidationError(
                    "dispatch_payload_path_invalid: file-backed dispatch requires the producer's "
                    f"registered project_root and {producer_actor_id} has none registered"
                )
            registered_path = Path(registered)
            if source_root is not None:
                explicit = Path(source_root)
                if not explicit.is_absolute():
                    raise ValidationError(
                        "dispatch_payload_path_invalid: the explicit source root must be an absolute path"
                    )
                if os.path.realpath(explicit) != os.path.realpath(registered_path):
                    raise ValidationError(
                        "dispatch_payload_path_invalid: the explicit source root must resolve to the "
                        f"producer's registered project_root {registered_path}"
                    )
            return registered_path
        if source_root is None:
            raise ValidationError(
                "dispatch_payload_path_invalid: a file-backed dispatch from a non-agent producer "
                "requires an explicit absolute source root; the working directory is never inferred"
            )
        explicit = Path(source_root)
        if not explicit.is_absolute():
            raise ValidationError(
                "dispatch_payload_path_invalid: the explicit source root must be an absolute path"
            )
        return explicit

    def _payload_preflight(self, dispatch_id: str) -> None:
        """Shared exact payload preflight for every adapter start path.

        Immediate start, queued drain, and retry all run this before invoking
        runtime-specific code. An inline dispatch (no payload row) is a no-op.
        For an artifact-backed dispatch the referenced blob must verify
        exactly (regular non-symlink file, byte count, SHA-256, strict UTF-8
        character count); any mismatch raises the stable typed failure that
        the caller lands as ``spawn_failed_message_landed`` with the runtime
        invoked zero times.
        """
        with self._db.connection() as conn:
            ref = conn.execute(
                "select * from dispatch_payload_refs where dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        if ref is None:
            return
        payload.load_verified_text(
            payload.store_root_for_db(self._db.db_path),
            storage_kind=str(ref["storage_kind"]),
            payload_sha256=str(ref["payload_sha256"]),
            byte_count=ref["byte_count"],
            char_count=ref["char_count"],
        )

    def _dispatch_by_idempotency_key_fresh(self, producer_actor_id: str, idempotency_key: str) -> dict:
        with self._db.connection() as conn:
            row = self._dispatch_by_idempotency_key(conn, producer_actor_id, idempotency_key)
        if row is None:
            raise RuntimeError("dispatch row disappeared after commit")
        return row

    def _dispatch_by_idempotency_key(
        self,
        conn: sqlite3.Connection,
        producer_actor_id: str,
        idempotency_key: str,
    ) -> dict | None:
        row = conn.execute(
            "select * from dispatch_ledger where producer_actor_id = ? and idempotency_key = ?",
            (producer_actor_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return self._dispatch_row(row)

    def _dispatch_by_id(self, conn: sqlite3.Connection, dispatch_id: str) -> dict:
        row = conn.execute(
            "select * from dispatch_ledger where dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"dispatch row disappeared: {dispatch_id}")
        return self._dispatch_row(row)

    def _producer_in_flight_count(self, conn: sqlite3.Connection, producer_actor_id: str) -> int:
        row = conn.execute(
            """
            select count(*) as count
            from dispatch_ledger
            where producer_actor_id = ? and status = 'in_flight'
            """,
            (producer_actor_id,),
        ).fetchone()
        return int(row["count"])

    def _producer_dispatch_cap(self, conn: sqlite3.Connection, producer_actor_id: str) -> int:
        row = conn.execute(
            "select dispatch_cap from actors where id = ?",
            (producer_actor_id,),
        ).fetchone()
        if row is None:
            return DEFAULT_DISPATCH_CAP
        return int(row["dispatch_cap"])

    def _dispatch_by_id_fresh(self, dispatch_id: str) -> dict:
        with self._db.connection() as conn:
            return self._dispatch_by_id(conn, dispatch_id)

    def _codex_auth_lineage_key_for_actor_row(self, actor_row: sqlite3.Row) -> str | None:
        if actor_row["runtime"] != "codex":
            return None
        try:
            spawn = json.loads(actor_row["spawn_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValidationError(f"codex recipient {actor_row['id']} has malformed spawn_json") from exc
        env = spawn.get("env") if isinstance(spawn, dict) else None
        if not isinstance(env, dict) or "CODEX_HOME" not in env:
            raise ValidationError(f"codex recipient {actor_row['id']} requires spawn.env.CODEX_HOME")
        return paths.codex_auth_lineage_key(str(actor_row["id"]), str(env["CODEX_HOME"]))

    @staticmethod
    def _codex_home_for_actor_row(actor_row: sqlite3.Row) -> str:
        try:
            spawn = json.loads(actor_row["spawn_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValidationError(f"codex recipient {actor_row['id']} has malformed spawn_json") from exc
        env = spawn.get("env") if isinstance(spawn, dict) else None
        if not isinstance(env, dict) or "CODEX_HOME" not in env:
            raise ValidationError(f"codex recipient {actor_row['id']} requires spawn.env.CODEX_HOME")
        return str(env["CODEX_HOME"])

    @staticmethod
    def _validate_codex_ttl_satisfiable(ttl_seconds: int) -> None:
        margin = codex_auth_refresh.spawn_freshness_margin_seconds()
        lifetime = codex_auth_refresh.MINIMUM_ROTATED_TOKEN_LIFETIME_SECONDS
        maximum = lifetime - margin - 1
        if ttl_seconds + margin >= lifetime:
            raise ValidationError(
                "codex ttl_seconds is unsatisfiable for the minimum rotated-token "
                f"lifetime; maximum admissible ttl_seconds is {maximum}"
            )

    def _codex_token_fresh(self, actor_row: sqlite3.Row, ttl_seconds: int) -> bool:
        if str(actor_row["runtime"]) != "codex":
            return True
        remaining = codex_auth_refresh.access_token_remaining_seconds(
            os.path.join(self._codex_home_for_actor_row(actor_row), "auth.json")
        )
        return remaining is not None and remaining > (
            ttl_seconds + codex_auth_refresh.spawn_freshness_margin_seconds()
        )

    @staticmethod
    def _refresh_claim_active(conn: sqlite3.Connection, lineage_key: str | None) -> bool:
        if lineage_key is None:
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(
            seconds=codex_auth_refresh.CLAIM_TTL_SECONDS
        )).isoformat(timespec="seconds")
        return conn.execute(
            "select 1 from codex_refresh_claims where lineage_key=? and holder is not null "
            "and claimed_at is not null and claimed_at >= ?",
            (lineage_key, cutoff),
        ).fetchone() is not None

    def _lineage_holding(
        self,
        conn: sqlite3.Connection,
        actor_id: str,
        runtime: str | None,
        lineage_key: str | None,
        *,
        exclude_dispatch_id: str | None,
    ) -> bool:
        if runtime != "codex" or lineage_key is None:
            return False
        params: list[object] = [lineage_key]
        exclude_clause = ""
        if exclude_dispatch_id is not None:
            exclude_clause = "and dispatch_id != ?"
            params.append(exclude_dispatch_id)
        row = conn.execute(
            f"""
            select 1
            from dispatch_ledger
            where auth_lineage_key = ?
              and status = 'in_flight'
              {exclude_clause}
            limit 1
            """,
            params,
        ).fetchone()
        if row is not None:
            return True
        for holder in self._resolved_null_key_inflight_holders(conn, lineage_key, exclude_dispatch_id):
            if holder.get("lineage_key") == lineage_key or holder.get("fail_closed"):
                return True
        return False

    def codex_lineage_holding(
        self, lineage_key: str, *, exclude_dispatch_id: str | None = None
    ) -> bool:
        """Read whether the Codex lineage is held under existing ledger semantics."""
        with self._db.connection() as conn:
            return self._lineage_holding(
                conn, "", "codex", lineage_key, exclude_dispatch_id=exclude_dispatch_id
            )

    def _resolved_null_key_inflight_holders(
        self,
        conn: sqlite3.Connection,
        lineage_key: str | None,
        exclude_dispatch_id: str | None,
    ) -> list[dict]:
        if lineage_key is None:
            return []
        rows = conn.execute(
            """
            select
              d.dispatch_id,
              a.id as actor_id,
              a.runtime as actor_runtime,
              a.spawn_json as actor_spawn_json
            from dispatch_ledger d
            join actors a on a.id = d.recipient_actor_id
            where d.status = 'in_flight'
              and d.auth_lineage_key is null
              and a.runtime = 'codex'
            """
        ).fetchall()
        holders = []
        for row in rows:
            if exclude_dispatch_id is not None and row["dispatch_id"] == exclude_dispatch_id:
                continue
            try:
                actor_row = {
                    "id": row["actor_id"],
                    "runtime": row["actor_runtime"],
                    "spawn_json": row["actor_spawn_json"],
                }
                resolved = self._codex_auth_lineage_key_for_actor_mapping(actor_row)
            except Exception as exc:
                holders.append({"dispatch_id": row["dispatch_id"], "fail_closed": True, "detail": str(exc)})
                continue
            if resolved == lineage_key:
                holders.append({"dispatch_id": row["dispatch_id"], "lineage_key": resolved})
        return holders

    def _codex_auth_lineage_key_for_actor_mapping(self, actor_row: dict | sqlite3.Row) -> str | None:
        if actor_row["runtime"] != "codex":
            return None
        try:
            spawn = json.loads(actor_row["spawn_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValidationError(f"codex recipient {actor_row['id']} has malformed spawn_json") from exc
        env = spawn.get("env") if isinstance(spawn, dict) else None
        if not isinstance(env, dict) or "CODEX_HOME" not in env:
            raise ValidationError(f"codex recipient {actor_row['id']} requires spawn.env.CODEX_HOME")
        return paths.codex_auth_lineage_key(str(actor_row["id"]), str(env["CODEX_HOME"]))

    def _record_lineage_fail_closed(self, conn: sqlite3.Connection, dispatch_id: str, reason: str) -> None:
        conn.execute(
            """
            update dispatch_ledger
            set observed_values_json = json_set(
              coalesce(nullif(observed_values_json, ''), '{}'),
              '$.auth_lineage_fail_closed_at',
              ?,
              '$.auth_lineage_fail_closed_reason',
              ?
            )
            where dispatch_id = ?
            """,
            (utc_now(), reason, dispatch_id),
        )

    def _mark_and_page_lineage_fail_closed(
        self,
        dispatch_id: str,
        reason: str,
        detail: str,
        *,
        status: str,
    ) -> dict | None:
        with self._db.connection() as conn:
            self._record_lineage_fail_closed(conn, dispatch_id, reason)
            dispatch = self._dispatch_by_id(conn, dispatch_id)
        return self._page_producer_for_lineage_fail_closed(dispatch, reason, detail, status=status)

    def _page_producer_for_lineage_fail_closed(
        self,
        dispatch: dict,
        reason: str,
        detail: str,
        *,
        status: str,
    ) -> dict | None:
        sender = self._actors._first_human_actor_id()
        if sender is None:
            return {"dispatch_id": dispatch["dispatch_id"], "status": "page_skipped_no_human_actor"}
        if not self._claim_observed_page(
            dispatch["dispatch_id"],
            claim_key=LINEAGE_FAIL_CLOSED_PAGE_CLAIM_KEY,
            message_key=LINEAGE_FAIL_CLOSED_PAGE_MESSAGE_KEY,
            status=status,
        ):
            return None
        subject = f"[dispatch warning] codex auth lineage fail-closed: {dispatch['dispatch_id']}"
        body = (
            "A codex dispatch auth lineage could not be resolved, so the lineage gate failed closed.\n"
            f"dispatch_id={dispatch['dispatch_id']}\n"
            f"message_id={dispatch['message_id']}\n"
            f"recipient_actor_id={dispatch['recipient_actor_id']}\n"
            f"reason={reason}\n"
            f"detail={detail}\n"
            "producer_action=repair the target actor spawn.env.CODEX_HOME"
        )
        page = self._mailbox.send_message(
            from_agent=sender,
            to_agents=[dispatch["producer_actor_id"]],
            subject=subject,
            body=body,
            refs=[],
            priority="blocker",
            requires_ack=True,
            parent_message_id=dispatch["message_id"],
        )
        self._record_observed_page(
            dispatch["dispatch_id"],
            paged_key=LINEAGE_FAIL_CLOSED_PAGED_KEY,
            message_key=LINEAGE_FAIL_CLOSED_PAGE_MESSAGE_KEY,
            paged_at=page["created_at"],
            message_id=page["id"],
            status=status,
        )
        return {
            "dispatch_id": dispatch["dispatch_id"],
            "status": "auth_lineage_fail_closed_paged",
            "message_id": page["id"],
        }

    def _annotate_key_drift_after_spawn(self, row: dict) -> dict:
        with self._db.connection() as conn:
            # A declined recipient cannot spawn, so there is no key drift to annotate.
            actor = self._actors._actor_row_by_id(
                conn, row["recipient_actor_id"], allow_declined=True
            )
            if is_declined_worker(conn, row["recipient_actor_id"]):
                return row
            try:
                current_key = self._codex_auth_lineage_key_for_actor_row(actor)
            except Exception as exc:
                current_key = f"<resolution failed: {exc}>"
            claimed_key = row.get("auth_lineage_key")
            if actor["runtime"] != "codex" or not claimed_key or current_key == claimed_key:
                return row
            marker = {"claimed": claimed_key, "current": current_key, "detected_at": utc_now()}
            LOG.warning("codex auth lineage key drift after spawn: dispatch_id=%s marker=%s", row["dispatch_id"], marker)
            conn.execute(
                """
                update dispatch_ledger
                set observed_values_json = json_set(
                  coalesce(nullif(observed_values_json, ''), '{}'),
                  '$.auth_lineage_key_drift',
                  json(?)
                )
                where dispatch_id = ?
                """,
                (json.dumps(marker, sort_keys=True), row["dispatch_id"]),
            )
            return self._dispatch_by_id(conn, row["dispatch_id"])

    def _unique_dispatches(self, dispatches: list[dict]) -> list[dict]:
        seen = set()
        unique = []
        for dispatch in dispatches:
            dispatch_id = dispatch["dispatch_id"]
            if dispatch_id in seen:
                continue
            seen.add(dispatch_id)
            unique.append(dispatch)
        return unique

    def _early_dlq_evidence_lines(self, dispatch: dict) -> str:
        """Bounded triage evidence for a DLQ page body. Never unbounded content.

        Renders classification and same-run exit / reply / status / log evidence
        when present (early-DLQ dead-before-close rows), capping list length and
        summary size so a page is operationally useful without inlining payloads.
        Returns an empty string when there is nothing exit-related to show.
        """
        observed = dispatch.get("observed_values") or {}
        evidence = observed.get("early_dlq_evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        exit_obj = evidence.get("worker_exit") or observed.get("worker_exit") or observed.get("reaper_exit")
        if not evidence and not isinstance(exit_obj, dict):
            return ""
        classification = (
            evidence.get("classification") or dispatch.get("failure_reason") or "worker_exited_before_close"
        )
        lines = [f"classification={classification}"]
        if isinstance(exit_obj, dict):
            lines.append(f"worker_exit_returncode={exit_obj.get('returncode')}")
            source = exit_obj.get("source")
            if source is not None:
                lines.append(f"worker_exit_source={source}")
        worker_log = evidence.get("worker_log") or observed.get("worker_log")
        if worker_log:
            lines.append(f"worker_log={str(worker_log)[:500]}")
        if "worker_events" in observed:
            lines.append(f"worker_events={str(observed['worker_events'])[:500]}")
        reply_ids = evidence.get("reply_message_ids")
        if isinstance(reply_ids, list):
            # ``reply_total_count`` is the EXACT total captured at evidence time.
            # Legacy evidence written before that field truthfully falls back to
            # the stored-id count (older captures stored every id, so the stored
            # count IS the total there). The omitted remainder is always computed
            # from the exact total, never assumed zero.
            total = evidence.get("reply_total_count")
            if not isinstance(total, int):
                total = len(reply_ids)
            capped = reply_ids[:EVIDENCE_REPLY_ID_CAP]
            omitted = max(total - len(capped), 0)
            suffix = "" if omitted == 0 else f" (+{omitted} more)"
            lines.append(f"reply_count={total}")
            lines.append(f"reply_message_ids={capped}{suffix}")
        summary = evidence.get("latest_status_summary")
        if summary:
            lines.append(f"latest_status_summary={str(summary)[:EVIDENCE_SUMMARY_DISPLAY_CAP]}")
        return "\n".join(lines) + "\n"

    def _page_producer_for_dispatch(self, dispatch: dict, human_actor_id: str | None) -> dict | None:
        sender = human_actor_id or self._actors._first_human_actor_id()
        if sender is None:
            return {"dispatch_id": dispatch["dispatch_id"], "status": "page_skipped_no_human_actor"}
        if not self._claim_observed_page(
            dispatch["dispatch_id"],
            claim_key="producer_page_claimed_at",
            message_key="producer_page_message_id",
            legacy_keys=("human_paged_at", "human_page_message_id", "human_page_claimed_at"),
        ):
            return None

        subject = f"[DLQ] dispatch_agent {dispatch['failure_reason'] or dispatch['status']}: {dispatch['recipient_actor_id']}"
        body = (
            f"Dispatch {dispatch['dispatch_id']} requires producer triage.\n"
            f"dispatch_id={dispatch['dispatch_id']}\n"
            f"status={dispatch['status']}\n"
            f"message_id={dispatch['message_id']}\n"
            f"spawn_handle={dispatch['spawn_handle']}\n"
            f"failure_reason={dispatch['failure_reason'] or ''}\n"
            f"{self._early_dlq_evidence_lines(dispatch)}"
            "producer_action=retry with admin retry-spawn or triage the failure"
        )
        page = self._mailbox.send_message(
            from_agent=sender,
            to_agents=[dispatch["producer_actor_id"]],
            subject=subject,
            body=body,
            refs=[],
            priority="blocker",
            requires_ack=True,
            parent_message_id=dispatch["message_id"],
        )
        self._record_observed_page(
            dispatch["dispatch_id"],
            paged_key="producer_paged_at",
            message_key="producer_page_message_id",
            paged_at=page["created_at"],
            message_id=page["id"],
        )
        return {"dispatch_id": dispatch["dispatch_id"], "status": "producer_paged", "message_id": page["id"]}

    def _page_producer_for_reply_actor_mismatch(self, dispatch: dict, human_actor_id: str | None) -> dict | None:
        observed = dict(dispatch["observed_values"])
        mismatch = observed.get("reply_actor_mismatch")
        if not isinstance(mismatch, dict):
            return None
        if any(
            key in observed
            for key in (
                "producer_mismatch_paged_at",
                "producer_mismatch_page_message_id",
                "producer_mismatch_page_claimed_at",
                "reply_actor_mismatch_paged_at",
                "reply_actor_mismatch_page_message_id",
                "reply_actor_mismatch_page_claimed_at",
            )
        ):
            return None
        sender = human_actor_id or self._actors._first_human_actor_id()
        if sender is None:
            return {"dispatch_id": dispatch["dispatch_id"], "status": "page_skipped_no_human_actor"}
        if not self._claim_observed_page(
            dispatch["dispatch_id"],
            claim_key="producer_mismatch_page_claimed_at",
            message_key="producer_mismatch_page_message_id",
            legacy_keys=(
                "reply_actor_mismatch_paged_at",
                "reply_actor_mismatch_page_message_id",
                "reply_actor_mismatch_page_claimed_at",
            ),
        ):
            return None

        subject = f"[dispatch warning] reply actor mismatch: {dispatch['dispatch_id']}"
        body = (
            "A dispatch trigger received a direct child reply from an unexpected agent.\n"
            f"dispatch_id={dispatch['dispatch_id']}\n"
            f"producer_actor_id={dispatch['producer_actor_id']}\n"
            f"recipient_actor_id={dispatch['recipient_actor_id']}\n"
            f"reply_message_id={mismatch.get('message_id')}\n"
            f"from_agent={mismatch.get('from_agent')}"
        )
        page = self._mailbox.send_message(
            from_agent=sender,
            to_agents=[dispatch["producer_actor_id"]],
            subject=subject,
            body=body,
            refs=[],
            priority="blocker",
            requires_ack=True,
            parent_message_id=dispatch["message_id"],
        )
        self._record_observed_page(
            dispatch["dispatch_id"],
            paged_key="producer_mismatch_paged_at",
            message_key="producer_mismatch_page_message_id",
            paged_at=page["created_at"],
            message_id=page["id"],
        )
        return {"dispatch_id": dispatch["dispatch_id"], "status": "producer_mismatch_paged", "message_id": page["id"]}

    def _page_stale_queued_dispatches(self, detail: str, human_actor_id: str | None) -> list[dict]:
        target = human_actor_id or self._actors._first_human_actor_id()
        if target is None:
            with self._db.connection() as conn:
                dispatches = [
                    self._dispatch_row(row)
                    for row in conn.execute("select * from dispatch_ledger where status = 'queued'").fetchall()
                ]
            return [
                {"dispatch_id": dispatch["dispatch_id"], "status": "page_skipped_no_human_actor"}
                for dispatch in dispatches
                if "stale_module_refused_paged_at" not in dispatch["observed_values"]
            ]

        actions: list[dict] = []
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                select *
                from dispatch_ledger
                where status = 'queued'
                order by created_at, dispatch_id
                """
            ).fetchall()
        for row in rows:
            dispatch = self._dispatch_row(row)
            observed = dict(dispatch["observed_values"])
            if "stale_module_refused_paged_at" in observed:
                continue
            if not self._claim_observed_page(
                dispatch["dispatch_id"],
                claim_key="stale_module_refused_page_claimed_at",
                message_key="stale_module_refused_page_message_id",
                status="queued",
            ):
                continue
            subject = f"[dispatch warning] stale module refused queued dispatch: {dispatch['dispatch_id']}"
            body = (
                "A stale long-lived substrate process refused to start a queued dispatch.\n"
                f"dispatch_id={dispatch['dispatch_id']}\n"
                f"detail={detail}\n"
                "remedy=restart the MCP server / session, then retry"
            )
            page = self._mailbox.send_message(
                from_agent=dispatch["producer_actor_id"],
                to_agents=[target],
                subject=subject,
                body=body,
                refs=[],
                priority="blocker",
                requires_ack=True,
            )
            self._record_observed_page(
                dispatch["dispatch_id"],
                paged_key="stale_module_refused_paged_at",
                message_key="stale_module_refused_page_message_id",
                paged_at=page["created_at"],
                message_id=page["id"],
                status="queued",
            )
            actions.append({"dispatch_id": dispatch["dispatch_id"], "status": "stale_module_refused_paged", "message_id": page["id"]})
        return actions

    def _page_old_unheld_queued_codex_dispatches(self, human_actor_id: str | None) -> list[dict]:
        try:
            threshold_seconds = int(os.environ.get("AGENT_COMMS_QUEUED_AGE_PAGE_SECONDS", str(DEFAULT_QUEUED_AGE_PAGE_SECONDS)))
        except ValueError:
            threshold_seconds = DEFAULT_QUEUED_AGE_PAGE_SECONDS
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=threshold_seconds)).isoformat(timespec="seconds")
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                select d.*
                from dispatch_ledger d
                join actors a on a.id = d.recipient_actor_id
                where d.status = 'queued'
                  and d.created_at < ?
                  and a.runtime = 'codex'
                  and json_extract(coalesce(nullif(d.observed_values_json, ''), '{}'), '$.queued_age_producer_paged_at') is null
                  and json_extract(coalesce(nullif(d.observed_values_json, ''), '{}'), '$.queued_age_producer_page_message_id') is null
                  and json_extract(coalesce(nullif(d.observed_values_json, ''), '{}'), '$.queued_age_producer_page_claimed_at') is null
                order by d.created_at, d.dispatch_id
                """,
                (cutoff,),
            ).fetchall()
            candidates = []
            for row in rows:
                dispatch = self._dispatch_row(row)
                try:
                    # Cleanup needs the row in order to record the existing fail-closed outcome.
                    actor = self._actors._actor_row_by_id(
                        conn, dispatch["recipient_actor_id"], allow_declined=True
                    )
                    if is_declined_worker(conn, dispatch["recipient_actor_id"]):
                        raise ValidationError(f"declined worker: {dispatch['recipient_actor_id']}")
                    lineage_key = dispatch.get("auth_lineage_key") or self._codex_auth_lineage_key_for_actor_row(actor)
                except Exception as exc:
                    candidates.append(
                        {
                            **dispatch,
                            "_lineage_resolution_failed": True,
                            "_lineage_resolution_detail": str(exc),
                        }
                    )
                    continue
                if not self._lineage_holding(
                    conn,
                    dispatch["recipient_actor_id"],
                    str(actor["runtime"]),
                    lineage_key,
                    exclude_dispatch_id=dispatch["dispatch_id"],
                ):
                    candidates.append(dispatch)
        actions: list[dict] = []
        for dispatch in candidates:
            if dispatch.get("_lineage_resolution_failed"):
                page = self._mark_and_page_lineage_fail_closed(
                    dispatch["dispatch_id"],
                    "candidate_lineage_resolution_failed",
                    str(dispatch.get("_lineage_resolution_detail") or "lineage resolution failed"),
                    status="queued",
                )
                actions.append(
                    {
                        "dispatch_id": dispatch["dispatch_id"],
                        "status": "lineage_resolution_failed",
                        "detail": dispatch.get("_lineage_resolution_detail"),
                    }
                )
                if page is not None:
                    actions.append(page)
                continue
            page = self._page_producer_for_queued_age(dispatch, human_actor_id)
            if page is not None:
                actions.append(page)
        return actions

    def _page_producer_for_queued_age(self, dispatch: dict, human_actor_id: str | None) -> dict | None:
        sender = human_actor_id or self._actors._first_human_actor_id()
        if sender is None:
            return {"dispatch_id": dispatch["dispatch_id"], "status": "page_skipped_no_human_actor"}
        if not self._claim_observed_page(
            dispatch["dispatch_id"],
            claim_key="queued_age_producer_page_claimed_at",
            message_key="queued_age_producer_page_message_id",
            legacy_keys=(
                "producer_page_claimed_at",
                "producer_page_message_id",
                "producer_paged_at",
                "human_page_claimed_at",
                "human_page_message_id",
                "human_paged_at",
                "stale_module_refused_page_claimed_at",
                "stale_module_refused_page_message_id",
                "stale_module_refused_paged_at",
            ),
            status="queued",
        ):
            return None
        subject = f"[dispatch warning] queued codex dispatch not draining: {dispatch['dispatch_id']}"
        body = (
            "A queued codex dispatch is older than the queue-age threshold and its auth lineage is not held.\n"
            f"dispatch_id={dispatch['dispatch_id']}\n"
            f"message_id={dispatch['message_id']}\n"
            f"recipient_actor_id={dispatch['recipient_actor_id']}\n"
            "producer_action=triage why the queued dispatch did not drain"
        )
        page = self._mailbox.send_message(
            from_agent=sender,
            to_agents=[dispatch["producer_actor_id"]],
            subject=subject,
            body=body,
            refs=[],
            priority="blocker",
            requires_ack=True,
            parent_message_id=dispatch["message_id"],
        )
        self._record_observed_page(
            dispatch["dispatch_id"],
            paged_key="queued_age_producer_paged_at",
            message_key="queued_age_producer_page_message_id",
            paged_at=page["created_at"],
            message_id=page["id"],
            status="queued",
        )
        return {"dispatch_id": dispatch["dispatch_id"], "status": "queued_age_producer_paged", "message_id": page["id"]}

    def _page_token_gate_unsatisfiable(self, dispatch: dict, detail: str) -> dict | None:
        """Page a dynamic token-gate configuration failure exactly once."""
        sender = self._actors._first_human_actor_id()
        if sender is None:
            return None
        if not self._claim_observed_page(
            dispatch["dispatch_id"],
            claim_key="token_gate_unsatisfiable_page_claimed_at",
            message_key="token_gate_unsatisfiable_page_message_id",
            status="queued",
        ):
            return None
        page = self._mailbox.send_message(
            from_agent=sender,
            to_agents=[dispatch["producer_actor_id"]],
            subject="[dispatch] token gate unsatisfiable",
            body=(
                f"dispatch_id={dispatch['dispatch_id']}\n"
                "reason=token_gate_unsatisfiable\n"
                f"detail={detail}"
            ),
            refs=[], priority="blocker", requires_ack=True,
            parent_message_id=dispatch["message_id"],
        )
        self._record_observed_page(
            dispatch["dispatch_id"],
            paged_key="token_gate_unsatisfiable_paged_at",
            message_key="token_gate_unsatisfiable_page_message_id",
            paged_at=page["created_at"], message_id=page["id"], status="queued",
        )
        return page

    def _claim_observed_page(
        self,
        dispatch_id: str,
        *,
        claim_key: str,
        message_key: str,
        legacy_keys: tuple[str, ...] = (),
        status: str | None = None,
    ) -> bool:
        now = utc_now()
        abandoned_before = (datetime.now(timezone.utc) - timedelta(seconds=PAGE_CLAIM_ABANDON_SECONDS)).isoformat(
            timespec="seconds"
        )
        status_clause = "and status = ?" if status is not None else ""
        legacy_clause = "".join(
            f"\n                  and json_extract(coalesce(nullif(observed_values_json, ''), '{{}}'), '$.{key}') is null"
            for key in legacy_keys
        )
        params: list[object] = [now, dispatch_id, abandoned_before]
        if status is not None:
            params.append(status)
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            cursor = conn.execute(
                f"""
                update dispatch_ledger
                set observed_values_json = json_set(
                  coalesce(nullif(observed_values_json, ''), '{{}}'),
                  '$.{claim_key}',
                  ?
                )
                where dispatch_id = ?
                  and json_extract(coalesce(nullif(observed_values_json, ''), '{{}}'), '$.{message_key}') is null
                  and (
                    json_extract(coalesce(nullif(observed_values_json, ''), '{{}}'), '$.{claim_key}') is null
                    or json_extract(coalesce(nullif(observed_values_json, ''), '{{}}'), '$.{claim_key}') <= ?
                  )
                  {legacy_clause}
                  {status_clause}
                """,
                params,
            )
            return cursor.rowcount == 1

    def _record_observed_page(
        self,
        dispatch_id: str,
        *,
        paged_key: str,
        message_key: str,
        paged_at: str,
        message_id: str,
        status: str | None = None,
    ) -> None:
        status_clause = "and status = ?" if status is not None else ""
        params: list[object] = [paged_at, message_id, dispatch_id]
        if status is not None:
            params.append(status)
        with self._db.connection() as conn:
            conn.execute(
                f"""
                update dispatch_ledger
                set observed_values_json = json_set(
                  coalesce(nullif(observed_values_json, ''), '{{}}'),
                  '$.{paged_key}',
                  ?,
                  '$.{message_key}',
                  ?
                )
                where dispatch_id = ?
                  {status_clause}
                """,
                params,
            )

    def _next_queued_dispatch_context(
        self,
        conn: sqlite3.Connection,
        ttl_seconds: int,
        excluded_dispatch_ids: set[str] | None = None,
        blocked_lineage_keys: set[str] | None = None,
        token_gate_actions: list[dict] | None = None,
    ) -> DispatchContext | None:
        excluded_dispatch_ids = excluded_dispatch_ids or set()
        blocked_lineage_keys = blocked_lineage_keys or set()
        token_gate_actions = token_gate_actions if token_gate_actions is not None else []
        dispatch_rows = conn.execute(
            """
            select *
            from dispatch_ledger
            where status = 'queued'
            order by created_at, dispatch_id
            """
        ).fetchall()
        for dispatch_row in dispatch_rows:
            if dispatch_row["dispatch_id"] in excluded_dispatch_ids:
                continue
            # A queued row carrying a pending cancellation request is owned by the
            # cancellation engine (its monitor drive or a spawn-time settlement),
            # never the ordinary start path: never re-spawn it into a false
            # in_flight.
            if self._has_pending_cancellation(dispatch_row):
                continue
            in_flight_count = self._producer_in_flight_count(conn, dispatch_row["producer_actor_id"])
            dispatch_cap = self._producer_dispatch_cap(conn, dispatch_row["producer_actor_id"])
            if in_flight_count >= dispatch_cap:
                continue
            try:
                # Cleanup needs the row in order to record the existing fail-closed outcome.
                actor = self._actors._actor_row_by_id(
                    conn, dispatch_row["recipient_actor_id"], allow_declined=True
                )
                if is_declined_worker(conn, dispatch_row["recipient_actor_id"]):
                    raise ValidationError(f"declined worker: {dispatch_row['recipient_actor_id']}")
                lineage_key = dispatch_row["auth_lineage_key"] or self._codex_auth_lineage_key_for_actor_row(actor)
            except Exception as exc:
                self._mark_and_page_lineage_fail_closed(
                    str(dispatch_row["dispatch_id"]),
                    "candidate_lineage_resolution_failed",
                    str(exc),
                    status="queued",
                )
                token_gate_actions.append(
                    {
                        "dispatch_id": dispatch_row["dispatch_id"],
                        "status": "lineage_resolution_failed",
                        "detail": str(exc),
                    }
                )
                continue
            if lineage_key is not None and lineage_key in blocked_lineage_keys:
                continue
            if str(actor["runtime"]) == "codex":
                try:
                    self._validate_codex_ttl_satisfiable(ttl_seconds)
                except ValidationError as exc:
                    self._page_token_gate_unsatisfiable(self._dispatch_row(dispatch_row), str(exc))
                    token_gate_actions.append({
                        "dispatch_id": dispatch_row["dispatch_id"],
                        "status": "token_gate_unsatisfiable",
                        "lineage_key": lineage_key,
                        "detail": str(exc),
                    })
                    continue
                if self._refresh_claim_active(conn, lineage_key):
                    if lineage_key is not None:
                        blocked_lineage_keys.add(lineage_key)
                    token_gate_actions.append({
                        "dispatch_id": dispatch_row["dispatch_id"],
                        "status": "refresh_in_progress",
                        "lineage_key": lineage_key,
                    })
                    continue
                if not self._codex_token_fresh(actor, ttl_seconds):
                    if lineage_key is not None:
                        blocked_lineage_keys.add(lineage_key)
                    token_gate_actions.append({
                        "dispatch_id": dispatch_row["dispatch_id"],
                        "status": "token_stale",
                        "lineage_key": lineage_key,
                    })
                    continue
            return self._dispatch_context_from_row(conn, dispatch_row, ttl_seconds)
        return None

    def _dispatch_context_by_id(
        self,
        conn: sqlite3.Connection,
        dispatch_id: str,
        ttl_seconds: int,
        recheck_codex_gates: bool = False,
    ) -> DispatchContext | None:
        dispatch_row = conn.execute(
            """
            select *
            from dispatch_ledger
            where dispatch_id = ? and status = 'queued'
            """,
            (dispatch_id,),
        ).fetchone()
        if dispatch_row is None:
            return None
        # A queued row with a pending cancellation is owned by the cancellation
        # engine; the ordinary start path must not spawn it.
        if self._has_pending_cancellation(dispatch_row):
            return None
        if recheck_codex_gates:
            recipient = self._actors._actor_row_by_id(conn, dispatch_row["recipient_actor_id"])
            lineage_key = dispatch_row["auth_lineage_key"]
            if str(recipient["runtime"]) == "codex" and (
                self._refresh_claim_active(conn, lineage_key)
                or not self._codex_token_fresh(recipient, ttl_seconds)
            ):
                return None
        if is_declined_worker(conn, dispatch_row["recipient_actor_id"]):
            return None
        return self._dispatch_context_from_row(conn, dispatch_row, ttl_seconds)

    @staticmethod
    def _has_pending_cancellation(dispatch_row: sqlite3.Row) -> bool:
        try:
            observed = json.loads(dispatch_row["observed_values_json"] or "{}")
        except (TypeError, ValueError):
            return False
        cancellation = observed.get(CANCELLATION_KEY) if isinstance(observed, dict) else None
        return isinstance(cancellation, dict) and cancellation.get("state") == "requested"

    def _dispatch_context_from_row(
        self,
        conn: sqlite3.Connection,
        dispatch_row: sqlite3.Row,
        ttl_seconds: int,
    ) -> DispatchContext:
        recipient_row = self._actors._actor_row_by_id(conn, dispatch_row["recipient_actor_id"])
        message_row = conn.execute(
            """
            select m.*, mr.to_agent, mr.status, mt.parent_message_id
            from messages m
            join message_recipients mr on mr.message_id = m.id
            left join message_threads mt on mt.message_id = m.id
            where m.id = ? and mr.to_agent = ?
            """,
            (dispatch_row["message_id"], dispatch_row["recipient_actor_id"]),
        ).fetchone()
        if message_row is None:
            raise RuntimeError(f"dispatch message disappeared: {dispatch_row['message_id']}")

        expected_close_by = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        return DispatchContext(
            dispatch=self._dispatch_row(dispatch_row),
            recipient=self._actors._actor_row(recipient_row),
            message=self._mailbox._message_row(message_row, full=True),
            ttl_seconds=ttl_seconds,
            expected_close_by=expected_close_by,
            db_path=str(self._db.db_path),
        )

    def _validate_dispatch_start(self, result: DispatchStart) -> None:
        if not result.spawn_handle.strip():
            raise ValidationError("adapter returned empty spawn_handle")

    def _dispatch_row(self, row: sqlite3.Row) -> dict:
        return {
            "dispatch_id": row["dispatch_id"],
            "parent_dispatch_id": row["parent_dispatch_id"],
            "idempotency_key": row["idempotency_key"],
            "message_id": row["message_id"],
            "thread_ref": row["thread_ref"],
            "spawn_handle": row["spawn_handle"],
            "recipient_actor_id": row["recipient_actor_id"],
            "producer_actor_id": row["producer_actor_id"],
            "originating_actor_id": row["originating_actor_id"],
            "policy_name": row["policy_name"],
            "policy_version": row["policy_version"],
            "policy_issued_by": row["policy_issued_by"],
            "expected_close_by": row["expected_close_by"],
            "status": row["status"],
            "result": row["result"] if "result" in row.keys() else None,
            "created_at": row["created_at"],
            "spawned_at": row["spawned_at"],
            "closed_at": row["closed_at"],
            "dlq_at": row["dlq_at"],
            "override_reason": row["override_reason"],
            "failure_reason": row["failure_reason"],
            "auth_lineage_key": row["auth_lineage_key"] if "auth_lineage_key" in row.keys() else None,
            "auth_lineage_claimed_at": row["auth_lineage_claimed_at"] if "auth_lineage_claimed_at" in row.keys() else None,
            "observed_values": json.loads(row["observed_values_json"] or "{}"),
        }
