"""SQL exit evidence written by the wrapper and read by the janitor gate
(short WAL / busy-timeout transactions only).

Observed-value merges, truthful worker exit, the versioned reaper proof, the
terminal/cleanup projections and the positive same-run termination-evidence
gate.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from .. import terminal_statuses
from ..clock import utc_now
from . import paths

logger = logging.getLogger("agent_comms.supervisor.evidence")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=paths._SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute(f"pragma busy_timeout = {paths._SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def merge_supervisor_observed(
    db_path: str,
    dispatch_id: str,
    *,
    run_token: str,
    wrapper_pid: int,
    child_pid: int,
    control_socket: str,
) -> None:
    """Merge the supervisor identity into the dispatch's observed values.

    This is additive JSON: it never overwrites unrelated observed keys, and it
    does not transition ledger status. The transaction is short and holds no
    socket / wait / signal work.
    """
    conn = _connect(db_path)
    try:
        conn.execute("begin immediate")
        conn.execute(
            """
            update dispatch_ledger
            set observed_values_json = json_set(
              coalesce(nullif(observed_values_json, ''), '{}'),
              '$.protocol_version', ?,
              '$.run_token', ?,
              '$.wrapper_pid', ?,
              '$.child_pid', ?,
              '$.control_socket', ?
            )
            where dispatch_id = ?
            """,
            (paths.PROTOCOL_VERSION, run_token, wrapper_pid, child_pid, control_socket, dispatch_id),
        )
        conn.commit()
    finally:
        conn.close()


def record_worker_exit(
    db_path: str,
    dispatch_id: str,
    run_token: str,
    *,
    returncode: int | None,
    source: str,
) -> bool:
    """Write same-run child exit evidence under ``$.worker_exit``.

    The ``$.run_token`` equality predicate guarantees a stale or cross-run
    wrapper cannot stamp a different run's row. Returns whether a row matched.
    """
    exit_obj = {
        "returncode": returncode,
        "exited_at": utc_now(),
        "source": source,
        "run_token": run_token,
    }
    conn = _connect(db_path)
    try:
        conn.execute("begin immediate")
        cursor = conn.execute(
            """
            update dispatch_ledger
            set observed_values_json = json_set(
              coalesce(nullif(observed_values_json, ''), '{}'),
              '$.worker_exit', json(?)
            )
            where dispatch_id = ?
              and json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.run_token') = ?
            """,
            (json.dumps(exit_obj, sort_keys=True), dispatch_id, run_token),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


# Bounded vocabulary naming WHICH parent path performed/recorded the exact
# registered-wrapper reap: a claimed authenticated HALT's finalize, or the
# background registry reap. Anything else is refused loudly.
REAP_SOURCE_HALT_FINALIZE = "halt_finalize"
REAP_SOURCE_BACKGROUND_REAP = "background_reap"
REAP_EVIDENCE_SOURCES = frozenset({REAP_SOURCE_HALT_FINALIZE, REAP_SOURCE_BACKGROUND_REAP})


# Revision 7 F2: the ONLY SQL object that qualifies a cancellation
# ``same_run_exit_confirmed`` (and the janitor's same-run cleanup gate) is the
# exact version-1 ``$.reaper_exit`` COMPLETE proof. It binds, into one
# atomically-persisted write-once object, the exact registered-wrapper reap, the
# native process-group drain, and current-run owned-artifact cleanup. Anything
# less -- a bare ``$.worker_exit`` (child-exit evidence only), the old four-field
# ``reaper_exit``, or a false/incomplete/stale/wrong-token/malformed object --
# never qualifies. The literal integer return code is illustrative; the boolean
# facts must each be the exact JSON ``true``.
REAP_PROOF_VERSION = 1
_REAP_OWNED_ARTIFACT_KEYS = frozenset({"run_dir", "control_socket", "zdotdir_parent"})
_REAP_COMPLETE_TOP_KEYS = frozenset(
    {
        "proof_version",
        "run_token",
        "returncode",
        "source",
        "reaped_at",
        "registered_wrapper_reaped",
        "native_process_group_drained",
        "owned_artifacts_absent",
    }
)


def _owned_artifacts_all_absent(owned_artifacts_absent: object) -> bool:
    """Whether ``owned_artifacts_absent`` is EXACTLY the three owned-artifact
    keys, each the exact JSON boolean ``True``.

    Revision 7 F2: a proof's owned-artifact facts are never manufactured. The
    producer freshly verifies the exact run directory, control socket, and
    adapter-owned ZDOTDIR parent are gone and passes that here; anything short
    of all three exact ``True`` refuses the write, so no partial proof exists.
    """
    return (
        isinstance(owned_artifacts_absent, dict)
        and set(owned_artifacts_absent) == _REAP_OWNED_ARTIFACT_KEYS
        and all(owned_artifacts_absent.get(key) is True for key in _REAP_OWNED_ARTIFACT_KEYS)
    )


def complete_reaper_proof(existing: object, run_token: object) -> dict | None:
    """Return ``existing`` iff it is the exact version-1 complete ``reaper_exit``
    proof for ``run_token``; otherwise ``None``.

    Exactness is total. The top-level key set is EXACTLY
    ``_REAP_COMPLETE_TOP_KEYS`` and the nested ``owned_artifacts_absent`` key set
    is EXACTLY ``_REAP_OWNED_ARTIFACT_KEYS`` (no omitted, substitute, or extra
    key). ``proof_version`` is the integer 1 (never the bool ``True``);
    ``returncode`` is an integer (never a bool); ``source`` is in the bounded
    reap vocabulary; ``reaped_at`` is a non-empty string; ``run_token`` is a
    non-empty string that exactly equals the supplied current token; and each
    proof boolean (``registered_wrapper_reaped``,
    ``native_process_group_drained`` and every ``owned_artifacts_absent`` value)
    is the exact JSON ``True``. Missing, substitute, extra, false, malformed,
    stale, or wrong-token evidence returns ``None``.
    """
    if not isinstance(run_token, str) or not run_token:
        return None
    if not isinstance(existing, dict):
        return None
    if set(existing) != _REAP_COMPLETE_TOP_KEYS:
        return None
    proof_version = existing.get("proof_version")
    if isinstance(proof_version, bool) or proof_version != REAP_PROOF_VERSION:
        return None
    token = existing.get("run_token")
    if not isinstance(token, str) or token != run_token:
        return None
    returncode = existing.get("returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        return None
    if existing.get("source") not in REAP_EVIDENCE_SOURCES:
        return None
    reaped_at = existing.get("reaped_at")
    if not isinstance(reaped_at, str) or not reaped_at:
        return None
    if existing.get("registered_wrapper_reaped") is not True:
        return None
    if existing.get("native_process_group_drained") is not True:
        return None
    owned = existing.get("owned_artifacts_absent")
    if not isinstance(owned, dict) or set(owned) != _REAP_OWNED_ARTIFACT_KEYS:
        return None
    if any(owned.get(key) is not True for key in _REAP_OWNED_ARTIFACT_KEYS):
        return None
    return existing


def record_registered_wrapper_reap(
    db_path: str,
    dispatch_id: str,
    run_token: str,
    *,
    returncode: int | None,
    source: str,
    registered_wrapper_reaped: bool,
    native_process_group_drained: bool,
    owned_artifacts_absent: dict,
) -> bool:
    """Idempotent same-run ``$.reaper_exit`` evidence for the EXACT registered
    wrapper ``Popen`` the parent process actually waited.

    This is the single persistence primitive for the parent-side wrapper reap:
    a claimed-HALT finalize (``source="halt_finalize"``) and the background
    registry reap (``source="background_reap"``) both record the act through
    it, so the durable object always carries the exact wrapper return code,
    reap timestamp, run token, and which bounded parent path recorded it. It
    may write ALONGSIDE ``$.worker_exit`` -- child exit evidence and wrapper
    reap evidence are distinct facts about distinct processes -- but only into
    the row whose SQL ``$.run_token`` exactly equals ``run_token``, so a stale
    or cross-run owner can never stamp another run's row.

    It persists exactly one atomic version-1 complete proof object (the exact
    ``$.reaper_exit`` shape :func:`complete_reaper_proof` accepts). Write-once
    with exact idempotency: an existing valid same-run COMPLETE proof whose
    return code matches is success WITHOUT mutation (its timestamp and facts are
    never changed); a wrong-token, conflicting-return-code, or malformed existing
    object -- including any structurally incomplete dictionary missing the exact
    version-1 contract -- refuses loudly and leaves the row byte-identical.
    The whole decision is one short immediate transaction (no socket/wait/
    signal work) that preserves unrelated observed values and never transitions
    ledger or transport state.
    """
    if source not in REAP_EVIDENCE_SOURCES:
        raise ValueError(f"reap evidence source not in bounded vocabulary: {source!r}")
    # Revision 7 F2: NEVER manufacture proof facts. This primitive receives only
    # explicitly verified complete facts from the HALT-finalize / background-reap
    # producer, which has already waited the exact registered wrapper ``Popen``,
    # proven the wrapper and registered native child groups drained, and cleaned +
    # freshly verified absence of the owned run directory, control socket, and
    # ZDOTDIR parent. It REFUSES before any SQL mutation unless
    # ``registered_wrapper_reaped``, ``native_process_group_drained``, and the
    # exact three ``owned_artifacts_absent`` facts are each the exact ``True``; a
    # false/partial fact set publishes no proof at all.
    if (
        registered_wrapper_reaped is not True
        or native_process_group_drained is not True
        or not _owned_artifacts_all_absent(owned_artifacts_absent)
    ):
        return False
    exit_obj = {
        "proof_version": REAP_PROOF_VERSION,
        "run_token": run_token,
        "returncode": returncode,
        "source": source,
        "reaped_at": utc_now(),
        "registered_wrapper_reaped": True,
        "native_process_group_drained": True,
        "owned_artifacts_absent": {
            "run_dir": True,
            "control_socket": True,
            "zdotdir_parent": True,
        },
    }
    conn = _connect(db_path)
    try:
        conn.execute("begin immediate")
        row = conn.execute(
            "select observed_values_json from dispatch_ledger where dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        try:
            observed = json.loads(row["observed_values_json"] or "{}")
        except (TypeError, ValueError):
            observed = None
        if not isinstance(observed, dict) or observed.get("run_token") != run_token:
            conn.rollback()
            return False
        existing = observed.get("reaper_exit")
        if existing is not None:
            conn.rollback()
            if (
                complete_reaper_proof(existing, run_token) is not None
                and existing.get("returncode") == returncode
            ):
                # Exact idempotent replay: the complete proof is already durably
                # recorded; success WITHOUT mutation (timestamp / facts unchanged).
                return True
            logger.warning(
                "refusing conflicting/malformed existing reaper_exit for %s (source=%s)",
                dispatch_id,
                source,
            )
            return False
        cursor = conn.execute(
            """
            update dispatch_ledger
            set observed_values_json = json_set(
              coalesce(nullif(observed_values_json, ''), '{}'),
              '$.reaper_exit', json(?)
            )
            where dispatch_id = ?
              and json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.run_token') = ?
              and json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.reaper_exit') is null
            """,
            (json.dumps(exit_obj, sort_keys=True), dispatch_id, run_token),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def record_reaper_exit(
    db_path: str,
    dispatch_id: str,
    run_token: str,
    *,
    returncode: int | None,
) -> bool:
    """Seed the durable COMPLETE background-reap proof for an already
    fully-drained/cleaned run via :func:`record_registered_wrapper_reap`.

    Compatibility name for the pre-revision-3 recorder. The old fallback-only
    contract (refuse whenever ``$.worker_exit`` exists) is REPLACED: a normal
    HALT's finalize now persists the same object with ``source="halt_finalize"``
    before it may confirm, and this name records a background-registry reap
    through the same idempotent, identity-bound primitive. It represents a run
    whose wrapper is reaped, native group drained, and owned artifacts already
    gone, so it passes the exact-true complete facts the primitive requires;
    Revision 7's live registry paths (:meth:`ReaperRegistry.finalize_claimed`,
    :meth:`ReaperRegistry._reap_entry`) freshly VERIFY those facts before
    calling in, and never manufacture them.
    """
    return record_registered_wrapper_reap(
        db_path,
        dispatch_id,
        run_token,
        returncode=returncode,
        source=REAP_SOURCE_BACKGROUND_REAP,
        registered_wrapper_reaped=True,
        native_process_group_drained=True,
        owned_artifacts_absent={"run_dir": True, "control_socket": True, "zdotdir_parent": True},
    )


def dispatch_terminal_state(db_path: str, dispatch_id: str) -> tuple[str, str | None] | None:
    """Return ``(status, run_token)`` for a dispatch, or ``None`` if absent.

    Used by the janitor to decide whether a run directory's owner is safe to
    remove. A DB failure raises; the caller preserves loudly.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            select status,
                   json_extract(coalesce(nullif(observed_values_json, ''), '{}'), '$.run_token') as run_token
            from dispatch_ledger
            where dispatch_id = ?
            """,
            (dispatch_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row["status"], row["run_token"]


def dispatch_cleanup_state(db_path: str, dispatch_id: str) -> tuple[dict, str | None, dict] | None:
    """Return ``(projection, run_token, observed_values)`` for the janitor gate.

    The cleanup decision CONSUMES the single canonical ledger/transport
    projection: this reads the execution (ledger) status joined with its
    recipient transport status and classifies the pair through
    :func:`agent_comms.terminal_statuses.project_dispatch_transport` -- the same
    authority the reporting and monitoring surfaces use -- so cleanup never
    re-interprets the two INDEPENDENT state machines by hand. ``projection`` is
    that classifier dict (raw ``dispatch_status`` / ``transport_status`` plus the
    normalized ``outcome``). The parsed observed values and the SQL run token are
    returned alongside so the janitor can still apply the positive same-run
    termination-evidence gate: terminal-ness follows the raw execution status
    while the transport-aware ``outcome`` is surfaced on the cleanup record.
    ``None`` when the row is absent. A DB failure raises; the caller preserves
    loudly.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            select d.status as dispatch_status,
                   mr.status as transport_status,
                   d.observed_values_json
            from dispatch_ledger d
            left join message_recipients mr
              on mr.message_id = d.message_id and mr.to_agent = d.recipient_actor_id
            where d.dispatch_id = ?
            """,
            (dispatch_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        observed = json.loads(row["observed_values_json"] or "{}")
    except (TypeError, ValueError):
        observed = {}
    if not isinstance(observed, dict):
        observed = {}
    run_token = observed.get("run_token")
    if not isinstance(run_token, str):
        run_token = None
    projection = terminal_statuses.project_dispatch_transport(row["dispatch_status"], row["transport_status"])
    return projection, run_token, observed


def confirmed_termination_evidence(observed: dict, run_token: str) -> str | None:
    """Positive same-run termination evidence for janitor cleanup, or ``None``.

    Returns a short evidence label when SQL proves the native child is gone,
    always bound to the EXACT run directory token so a stale/cross-run or an
    identity-less artifact is never cleaned:

    - the exact version-1 COMPLETE ``$.reaper_exit`` proof (see
      :func:`complete_reaper_proof`) whose ``run_token`` exactly matches the run
      directory's token. This is the ONLY same-run exit object that authorizes
      cleanup. A bare ``$.worker_exit`` is child-exit evidence a still-alive
      wrapper persists; it never proves wrapper reap, native-group drain,
      owned-artifact cleanup, or cancellation completion, so it is not consulted
      here, and neither is an old four-field / incomplete / stale / wrong-token
      ``reaper_exit``.
    - the ``not_started`` ``termination_result`` string (a cancellation of a
      never-spawned dispatch) BUT ONLY when SQL run-token identity
      (``observed['run_token']``, recorded at spawn) is present AND equals the
      run directory token. Revision 7 F2: ``worker_exited_before_close``,
      ``supervised_halt_confirmed``, and ``same_run_exit_confirmed`` are NEVER
      authorized from the result string alone -- the exact version-1 COMPLETE
      ``$.reaper_exit`` proof above is their sole authority, because the string
      by itself does not prove wrapper reap, native-group drain, or
      owned-artifact cleanup. A missing/mismatched SQL run-token identity, or any
      of those three result strings without the complete proof, preserves the
      residue for operator recovery rather than deleting on the string alone.

    Absent positive same-run evidence returns ``None`` so the caller preserves
    the directory rather than deleting on terminal status (or a bare string)
    alone.
    """
    # Revision 7 F2: a same-run exit qualifies ONLY as the exact version-1
    # COMPLETE ``$.reaper_exit`` proof. A bare ``$.worker_exit`` is child-exit
    # evidence a still-alive wrapper persists; it never proves wrapper reap,
    # native-group drain, owned-artifact cleanup, HALT success, cancellation
    # completion, or janitor deletion authority, so it is not consulted here. The
    # old four-field / incomplete / stale / wrong-token ``reaper_exit`` refuses.
    if complete_reaper_proof(observed.get("reaper_exit"), run_token) is not None:
        return "reaper_exit complete same-run proof"
    sql_run_token = observed.get("run_token")
    if isinstance(sql_run_token, str) and sql_run_token == run_token:
        # Revision 7 F2: ONLY ``not_started`` is safe from the result string
        # alone. ``worker_exited_before_close`` / ``supervised_halt_confirmed`` /
        # ``same_run_exit_confirmed`` never authorize deletion from the string:
        # the complete exact-token ``$.reaper_exit`` proof checked above already
        # handles them, so a bare confirmed-looking string never cleans a run dir.
        if observed.get("termination_result") == "not_started":
            return "termination_result=not_started"
    return None
