"""Per-dispatch process supervisor for the dead-worker terminalization stage.

This module promotes the old best-effort ``timeout_wrapper.py`` into an
authenticated per-dispatch supervisor. Each dispatch gets one supervisor
process (the "wrapper") that:

- receives its run token and identity over an inherited ``socket.socketpair``
  bootstrap, never over argv or the native runtime child's environment, so the
  token cannot leak to the runtime child;
- owns a bounded process group containing exactly the native runtime child;
- binds an authenticated Unix-domain control socket under a fixed, protected,
  ``0700`` control root (``~/.agent-comms/run/s``) with a strict, short name so
  the encoded ``sun_path`` stays within the macOS 103-byte limit;
- serves read-only ``STATUS`` and authenticated ``HALT`` requests that carry the
  exact same run token;
- enforces the hard TTL with ``SIGTERM`` -> grace -> ``SIGKILL`` -> ``wait``;
- writes truthful, same-run SQL exit evidence into the dispatch's observed
  values using the same WAL / bounded busy-timeout discipline as ``Database``;
- and cleans up its socket, strict run directory, and ZDOTDIR on the way out.

The run token persisted into observed values is NOT claimed to be a same-uid
secret. It is a same-run correlation/authentication nonce. The capability
boundary is the OS sandbox denial of the protected control root: a bounded
runtime worker cannot create/connect/write there, so it cannot address any run
other than (at most) its own supervisor, and even then can do no more than exit
its own dispatch.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Bumped together with the machine contract. STATUS/HALT peers refuse a protocol
# they do not recognise rather than guess.
PROTOCOL_VERSION = 1
# Versioned cleanup-ownership marker written into each run directory. The
# spawn-time janitor removes a run directory only when this marker parses and
# names a SQL-terminal dispatch/run token; a bumped version that this code does
# not understand is preserved loudly.
MARKER_VERSION = 1

# macOS caps AF_UNIX ``sun_path`` at 104 bytes including the terminating NUL, so
# the usable encoded path length is 103 bytes. We assert against this ceiling on
# every platform so a socket path that would bind on Linux (108) but not on
# macOS fails loudly and identically everywhere, before READY.
SUN_PATH_MAX_BYTES = 103

# Test-only override for the protected control root. Production always uses the
# fixed default so the janitor and negative-cell sandbox boundary have a single
# known location.
CONTROL_ROOT_ENV = "AGENT_COMMS_SUPERVISOR_ROOT"
_DEFAULT_CONTROL_ROOT = Path.home() / ".agent-comms" / "run" / "s"

# A run directory is named by its 32-hex-char run token; the control socket is a
# single short "s" inside it. Both are strict so the janitor never has to reason
# about arbitrary names.
_STRICT_RUN_DIR_RE = re.compile(r"^[0-9a-f]{32}$")
_MARKER_NAME = "owner.json"
_SOCKET_NAME = "s"

# Same bounded busy-timeout discipline as ``Database`` (10s connect timeout,
# WAL already enabled on the ledger). Wrapper transactions stay short and never
# hold the DB open across socket I/O, wait, signal escalation, or cleanup.
_SQLITE_TIMEOUT_SECONDS = 10.0
_SQLITE_BUSY_TIMEOUT_MS = 5000

DISPATCH_TERMINAL_STATUSES = frozenset({"closed", "dlq", "spawn_failed_message_landed", "cancelled"})

READY_TOKEN = "ready"
_MAX_CONTROL_LINE_BYTES = 65536


class SupervisorError(RuntimeError):
    """A supervisor invariant could not be met."""


class SunPathTooLong(SupervisorError):
    """The bound control-socket path would exceed the AF_UNIX byte limit."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Paths / identity
# --------------------------------------------------------------------------- #


def control_root() -> Path:
    override = os.environ.get(CONTROL_ROOT_ENV)
    if override:
        return Path(override)
    return _DEFAULT_CONTROL_ROOT


def new_run_token() -> str:
    """A cryptographically random 32-hex-char run token / run-dir name."""
    return secrets.token_hex(16)


def is_strict_run_dir_name(name: str) -> bool:
    return _STRICT_RUN_DIR_RE.fullmatch(name) is not None


def run_dir_for(root: Path, run_token: str) -> Path:
    if not is_strict_run_dir_name(run_token):
        raise ValueError(f"run token is not a strict run-dir name: {run_token!r}")
    return root / run_token


def control_socket_for(root: Path, run_token: str) -> Path:
    return run_dir_for(root, run_token) / _SOCKET_NAME


def encoded_path_len(path: Path | str) -> int:
    return len(os.fsencode(str(path)))


def assert_sun_path_ok(path: Path | str) -> None:
    length = encoded_path_len(path)
    if length > SUN_PATH_MAX_BYTES:
        raise SunPathTooLong(
            f"control socket path is {length} bytes, exceeds AF_UNIX limit of "
            f"{SUN_PATH_MAX_BYTES}: {str(path)!r}; "
            "recover with: shorten the supervisor control root"
        )


def create_run_dir(root: Path, run_token: str) -> Path:
    """Create the strict, ``0700`` run directory under the protected root."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Never widen an existing root, but re-assert 0700 on the directory we own.
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    run_dir = run_dir_for(root, run_token)
    run_dir.mkdir(mode=0o700)
    os.chmod(run_dir, 0o700)
    return run_dir


def write_owner_marker(run_dir: Path, *, dispatch_id: str, run_token: str, wrapper_pid: int) -> Path:
    marker = {
        "marker_version": MARKER_VERSION,
        "dispatch_id": dispatch_id,
        "run_token": run_token,
        "wrapper_pid": wrapper_pid,
        "created_at": _utc_now(),
    }
    path = run_dir / _MARKER_NAME
    path.write_text(json.dumps(marker, sort_keys=True))
    os.chmod(path, 0o600)
    return path


def read_owner_marker(run_dir: Path) -> dict | None:
    path = run_dir / _MARKER_NAME
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def cleanup_run_dir(run_dir: Path) -> None:
    """Remove a strict run directory without ever following a symlink."""
    if run_dir.is_symlink():
        return
    if not is_strict_run_dir_name(run_dir.name):
        return
    shutil.rmtree(run_dir, ignore_errors=True)


def _path_present(path: Path) -> bool:
    """Whether a filesystem path still exists (a leftover symlink counts too).

    Used by HALT cleanup confirmation: a path we cannot even stat (an lstat
    ``OSError``) is treated as PRESENT so confirmation fails closed on residue it
    cannot rule out, never confirming blind while an artifact may leak.
    """
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return True


# --------------------------------------------------------------------------- #
# SQL evidence (short WAL / busy-timeout transactions only)
# --------------------------------------------------------------------------- #


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=_SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute(f"pragma busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
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
            (PROTOCOL_VERSION, run_token, wrapper_pid, child_pid, control_socket, dispatch_id),
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
        "exited_at": _utc_now(),
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


def _complete_reaper_exit_evidence(existing: object) -> bool:
    """Structural four-field contract for an existing ``$.reaper_exit``
    object: integer return code (bools excluded), non-empty string
    ``reaped_at``, non-empty string ``run_token``, and a ``source`` in the
    bounded vocabulary. Anything less is malformed evidence and must take the
    loud refusal path, never the replay path.
    """
    if not isinstance(existing, dict):
        return False
    returncode = existing.get("returncode")
    reaped_at = existing.get("reaped_at")
    run_token = existing.get("run_token")
    return (
        isinstance(returncode, int)
        and not isinstance(returncode, bool)
        and isinstance(reaped_at, str)
        and bool(reaped_at)
        and isinstance(run_token, str)
        and bool(run_token)
        and existing.get("source") in REAP_EVIDENCE_SOURCES
    )


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
        "reaped_at": _utc_now(),
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
    :func:`agent_comms.dispatch_ledger.project_dispatch_transport` -- the same
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
    # Imported lazily inside the function: ``agent_comms.adapters`` imports this
    # supervisor module, and ``dispatch_ledger`` imports adapters, so a
    # module-level ``from .dispatch_ledger import ...`` would close an import
    # cycle. By janitor call time both modules are fully initialised.
    from .dispatch_ledger import project_dispatch_transport

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
    projection = project_dispatch_transport(row["dispatch_status"], row["transport_status"])
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


# --------------------------------------------------------------------------- #
# Control protocol (framed newline-delimited JSON)
# --------------------------------------------------------------------------- #


def _read_line(conn: socket.socket, *, timeout: float) -> bytes | None:
    conn.settimeout(timeout)
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = conn.recv(4096)
        except (socket.timeout, TimeoutError):
            return None
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b"\n" in chunk or total >= _MAX_CONTROL_LINE_BYTES:
            break
    data = b"".join(chunks)
    if not data:
        return None
    line, _, _ = data.partition(b"\n")
    return line


def _send_json(conn: socket.socket, payload: dict[str, Any]) -> None:
    conn.sendall((json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))


@dataclass(frozen=True)
class ControlResult:
    """Outcome of a STATUS/HALT client request.

    ``ok`` is only true when the peer authenticated us and answered. Any
    connect/protocol/token failure yields ``ok=False`` with a diagnostic and is
    never interpreted as process death by the caller.
    """

    ok: bool
    state: str | None = None
    returncode: int | None = None
    error: str | None = None


def _client_request(
    control_socket: str,
    run_token: str,
    op: str,
    *,
    connect_timeout: float,
    io_timeout: float,
) -> ControlResult:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:  # pragma: no cover - AF_UNIX always present on posix
        return ControlResult(ok=False, error=f"socket: {exc}")
    try:
        client.settimeout(connect_timeout)
        client.connect(control_socket)
        client.settimeout(io_timeout)
        _send_json(client, {"protocol_version": PROTOCOL_VERSION, "run_token": run_token, "op": op})
        line = _read_line(client, timeout=io_timeout)
        if line is None:
            return ControlResult(ok=False, error="no response")
        resp = json.loads(line.decode("utf-8"))
        if not isinstance(resp, dict):
            return ControlResult(ok=False, error="malformed response")
        return ControlResult(
            ok=bool(resp.get("ok")),
            state=resp.get("state"),
            returncode=resp.get("returncode"),
            error=resp.get("error"),
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return ControlResult(ok=False, error=str(exc))
    finally:
        client.close()


def probe_status(
    control_socket: str,
    run_token: str,
    *,
    connect_timeout: float = 2.0,
    io_timeout: float = 2.0,
) -> ControlResult:
    """Read-only STATUS. Never signals."""
    return _client_request(
        control_socket, run_token, "STATUS", connect_timeout=connect_timeout, io_timeout=io_timeout
    )


def _wait_for_eof(conn: socket.socket, *, timeout: float) -> bool:
    """Read the connection until the peer closes it (EOF). Barrier 2.

    Returns ``True`` on a clean EOF within ``timeout``; ``False`` if the peer
    keeps the connection open past the deadline (a halted response WITHOUT
    wrapper shutdown, which must not confirm). Any bytes after the response line
    are drained and ignored -- only the close matters.
    """
    conn.settimeout(timeout)
    while True:
        try:
            chunk = conn.recv(4096)
        except (socket.timeout, TimeoutError):
            return False
        except OSError:
            return False
        if not chunk:
            return True


def _halt_over_connection(
    conn: socket.socket,
    run_token: str,
    wrapper_popen: subprocess.Popen | None,
    *,
    io_timeout: float,
    wrapper_wait_timeout: float,
) -> ControlResult:
    """Drive the three client-side HALT barriers over an open control socket.

    Confirmation (``ok=True``) requires ALL of, in order:
      1. a valid authenticated ``halted`` response;
      2. EOF on this same connection (the wrapper closes it only at shutdown);
      3. the exact registered wrapper ``Popen`` exiting/reaping (parent-owned).
    A missing/failed response, response-without-EOF, EOF-without-valid-response,
    absent wrapper handle, or wrapper-wait timeout each stays unconfirmed.
    """
    try:
        _send_json(conn, {"protocol_version": PROTOCOL_VERSION, "run_token": run_token, "op": "HALT"})
        line = _read_line(conn, timeout=io_timeout)
        if line is None:
            return ControlResult(ok=False, error="no response")
        resp = json.loads(line.decode("utf-8"))
        if not isinstance(resp, dict):
            return ControlResult(ok=False, error="malformed response")
        if not resp.get("ok") or resp.get("state") != "halted":
            return ControlResult(
                ok=False,
                state=resp.get("state"),
                returncode=resp.get("returncode"),
                error=resp.get("error") or "halt not confirmed",
            )
        returncode = resp.get("returncode")
        if not _wait_for_eof(conn, timeout=io_timeout):
            return ControlResult(
                ok=False,
                state="halted",
                returncode=returncode,
                error="halted response without wrapper EOF; termination not confirmed",
            )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return ControlResult(ok=False, error=str(exc))
    # Barrier 3: positive parent-owned evidence the exact wrapper exited/reaped.
    if wrapper_popen is None:
        return ControlResult(
            ok=False,
            state="halted",
            returncode=returncode,
            error="no registered wrapper process to reap; termination not confirmed",
        )
    try:
        wrapper_popen.wait(timeout=wrapper_wait_timeout)
    except subprocess.TimeoutExpired:
        return ControlResult(
            ok=False,
            state="halted",
            returncode=returncode,
            error="wrapper did not exit before deadline; termination not confirmed",
        )
    return ControlResult(ok=True, state="halted", returncode=returncode)


def request_halt(
    control_socket: str,
    run_token: str,
    *,
    connect_timeout: float = 2.0,
    io_timeout: float = 30.0,
    registry: "ReaperRegistry | None" = None,
    wrapper_popen: subprocess.Popen | None = None,
    wrapper_wait_timeout: float | None = None,
) -> ControlResult:
    """Authenticated HALT with a wrapper-exit barrier.

    The wrapper terminates/waits its own child, proves the group empty, persists
    exact-token exit evidence, and cleans up BEFORE its confirmed response; this
    client only reports ``ok=True`` once it has also observed EOF on the same
    connection AND reaped the EXACT registered wrapper ``Popen``. The wrapper
    handle is found by run token in the process-global reaper registry the
    spawning adapter already populates (or supplied directly for tests), so the
    adapter call site needs no change. After the barriers pass, the finalize of
    the claimed entry durably persists the exact registered-wrapper reap
    (``$.reaper_exit``, ``source="halt_finalize"``) BEFORE ``ok=True`` is
    returned, so the terminal cancellation CAS stays downstream of every HALT
    barrier and its durable evidence. Any barrier miss -- unreachable socket,
    server-side refusal, response without EOF, wrapper-wait timeout, or reap
    evidence persistence failure -- is a truthful ``termination_not_confirmed``,
    never death inferred from a socket round-trip alone.
    """
    reg = None
    claimed_entry = None
    if wrapper_popen is None:
        reg = registry if registry is not None else reaper_registry()
        # Atomically claim sole ownership of the exact registered wrapper so the
        # background reaper does not also process it while this HALT runs. A miss
        # (no entry) leaves ``wrapper_popen`` None -> barrier 3 refuses truthfully.
        claimed_entry = reg.claim_by_run_token(run_token)
        if claimed_entry is not None:
            wrapper_popen = claimed_entry.popen
    if wrapper_wait_timeout is None:
        wrapper_wait_timeout = io_timeout

    disposed = False

    def _dispose(result: ControlResult) -> ControlResult:
        # A confirmed HALT owns the exact-wrapper reap; finalize must durably
        # persist that reap ($.reaper_exit, source="halt_finalize") BEFORE the
        # claimed entry may be dropped, and ok=True is returned only downstream
        # of that persistence. A persistence failure (or an unconfirmed HALT)
        # releases the claim so the exact registered wrapper stays retryable by
        # a later HALT or the background reaper -- never stranded, and the only
        # exact Popen association is never discarded before durable evidence
        # exists.
        nonlocal disposed
        disposed = True
        if reg is None or claimed_entry is None:
            return result
        if not result.ok:
            reg.release_claim(claimed_entry)
            return result
        if reg.finalize_claimed(claimed_entry):
            return result
        return ControlResult(
            ok=False,
            state=result.state,
            returncode=result.returncode,
            error="registered-wrapper reap evidence persistence failed; termination not confirmed",
        )

    try:
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as exc:  # pragma: no cover - AF_UNIX always present on posix
            return _dispose(ControlResult(ok=False, error=f"socket: {exc}"))
        try:
            client.settimeout(connect_timeout)
            client.connect(control_socket)
            client.settimeout(io_timeout)
            result = _halt_over_connection(
                client,
                run_token,
                wrapper_popen,
                io_timeout=io_timeout,
                wrapper_wait_timeout=wrapper_wait_timeout,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            result = ControlResult(ok=False, error=str(exc))
        finally:
            client.close()
        return _dispose(result)
    finally:
        # Never strand the claim: an exception OUTSIDE the caught set (an
        # unexpected wrapper-wait failure) still releases ownership so the
        # idempotent fallback reaper can reap the exact wrapper. The exception
        # continues to propagate; on every normal path ``_dispose`` already ran.
        if not disposed and reg is not None and claimed_entry is not None:
            reg.release_claim(claimed_entry)


# --------------------------------------------------------------------------- #
# Supervisor server (runs inside the wrapper process)
# --------------------------------------------------------------------------- #


@dataclass
class BootstrapPayload:
    protocol_version: int
    dispatch_id: str
    run_token: str
    db_path: str
    control_root: str
    ttl_seconds: float
    kill_after_seconds: float
    zdotdir: str | None
    expected_close_by: str | None

    @classmethod
    def from_json(cls, raw: bytes) -> "BootstrapPayload":
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise SupervisorError("bootstrap payload is not an object")
        if data.get("protocol_version") != PROTOCOL_VERSION:
            raise SupervisorError(
                f"bootstrap protocol_version {data.get('protocol_version')!r} != {PROTOCOL_VERSION}"
            )
        try:
            return cls(
                protocol_version=int(data["protocol_version"]),
                dispatch_id=str(data["dispatch_id"]),
                run_token=str(data["run_token"]),
                db_path=str(data["db_path"]),
                control_root=str(data["control_root"]),
                ttl_seconds=float(data["ttl_seconds"]),
                kill_after_seconds=float(data["kill_after_seconds"]),
                zdotdir=(str(data["zdotdir"]) if data.get("zdotdir") else None),
                expected_close_by=(str(data["expected_close_by"]) if data.get("expected_close_by") else None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SupervisorError(f"malformed bootstrap payload: {exc}") from exc


def terminate_process_tree(child: subprocess.Popen, grace_seconds: float) -> int | None:
    """SIGTERM -> grace -> SIGKILL, escalating on the child's WHOLE process group.

    The wrapper launches the child with ``start_new_session=True`` so the child
    is its own group leader and the process-group id equals the child PID. That
    identity survives the leader exiting: if ``os.getpgid`` loses the race and
    raises ``ProcessLookupError`` (the leader was already reaped) we fall back to
    the child PID and STILL signal the group, so a surviving descendant is never
    abandoned by merely waiting an already-gone leader. We signal exactly that
    group, never the wrapper's.

    The leader exiting is NOT proof the group drained: a descendant that ignores
    SIGTERM keeps the group alive after the leader is reaped. Escalation to
    SIGKILL is therefore gated on a GROUP-level ``killpg(pgid, 0)`` probe over the
    grace window -- never on the leader's exit alone -- so a SIGTERM-ignoring
    descendant is always force-killed and the native group is confirmed drained.
    The leader is reaped the instant it exits so its status is captured and it is
    never left a zombie. The gate uses the single-shot ``_process_group_gone``
    probe, not ``process_group_empty``, so termination's internal escalation never
    perturbs the HALT barrier's separate, mandatory group-emptiness proof.
    """
    grace = max(grace_seconds, 0.0)
    try:
        pgid = os.getpgid(child.pid)
    except ProcessLookupError:
        # Leader-exit race: the group id is the child PID by construction.
        pgid = child.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # Give the ENTIRE group the grace to drain under SIGTERM, reaping the leader
    # the moment it exits but continuing to watch the whole group.
    leader_status = _drain_group(child, pgid, grace, status=None)
    if _process_group_gone(pgid):
        return leader_status
    # Residue survived the SIGTERM grace: escalate the whole group with SIGKILL
    # (never just the reaped leader) and confirm the group drains within grace.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return _drain_group(child, pgid, grace, status=leader_status)


def process_group_empty(pgid: int, *, timeout: float, interval: float = 0.02) -> bool:
    """Bounded GROUP-LEVEL kernel predicate: has the whole native group drained?

    Polls ``os.killpg(pgid, 0)`` (the null signal) until the kernel reports the
    entire process group is gone. ``killpg`` addresses the group as long as ANY
    member survives, so a bare surviving grandchild keeps it alive and raises
    nothing -- only an empty group raises ``ProcessLookupError``. This is
    therefore proof the COMPLETE native tree is gone, not a per-PID or ``ps``
    check that a reaped leader alone would satisfy. A member we are not permitted
    to signal (``PermissionError``) counts as residue. Returns ``False`` if the
    group is still non-empty when ``timeout`` elapses.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # pragma: no cover - unsignalable member survives
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _process_group_gone(pgid: int) -> bool:
    """Single non-blocking probe: has the whole native process group drained?

    ``killpg(pgid, 0)`` (the null signal) addresses the group while ANY member
    survives -- including an unreaped zombie -- so only a fully empty group raises
    ``ProcessLookupError``. A member we cannot signal (``PermissionError``) counts
    as surviving residue. This is the escalation gate used inside
    :func:`terminate_process_tree`; it is deliberately distinct from the blocking
    :func:`process_group_empty` the HALT barrier uses so termination's internal
    escalation never perturbs that separate proof.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:  # pragma: no cover - unsignalable residue survives
        return False
    return False


def _drain_group(
    child: subprocess.Popen, pgid: int, grace: float, *, status: int | None
) -> int | None:
    """Reap the leader as it exits and wait up to ``grace`` for the whole GROUP
    to drain. Returns the leader's exit status once observed (or the passed-in
    prior ``status``). Reaping is non-blocking (``child.poll``) so a
    SIGTERM-ignoring descendant is watched for the whole window rather than the
    leader's exit ending the wait early; never blocks past the bounded grace.
    """
    deadline = time.monotonic() + grace
    while True:
        if status is None:
            status = child.poll()
        if _process_group_gone(pgid):
            return status
        if time.monotonic() >= deadline:
            return status
        time.sleep(0.02)


class _Supervisor:
    def __init__(self, payload: BootstrapPayload, child_command: Sequence[str]) -> None:
        self.payload = payload
        self.child_command = list(child_command)
        self.root = Path(payload.control_root)
        self.run_dir: Path | None = None
        self.socket_path: Path | None = None
        self.listener: socket.socket | None = None
        self.child: subprocess.Popen | None = None
        self._exit_recorded = False
        self._exit_persisted = False
        self._halted = False
        self._cleaned = False
        # The authenticated HALT connection is held open from the confirmed
        # response until wrapper shutdown so the client observes EOF (barrier 2)
        # only when the wrapper actually tears down; ``run_supervisor`` closes it
        # last, after cleanup.
        self._halt_conn: socket.socket | None = None
        self._deadline = time.monotonic() + max(payload.ttl_seconds, 0.0)

    # -- setup (all before READY) ------------------------------------------ #

    def prepare(self) -> None:
        self.run_dir = create_run_dir(self.root, self.payload.run_token)
        self.socket_path = control_socket_for(self.root, self.payload.run_token)
        # Assert the byte ceiling BEFORE bind; clean the attempted run dir on
        # failure so no partial run directory leaks and READY is never sent.
        assert_sun_path_ok(self.socket_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(8)
        except OSError:
            listener.close()
            raise
        self.listener = listener

    def launch_child(self) -> None:
        assert self.run_dir is not None
        # The child is spawned with close_fds=True and NO pass_fds, so every
        # inherited FD >= 3 (the still-open bootstrap peer and the bound
        # listener) is closed at exec. Python already opens those sockets
        # non-inheritable (CLOEXEC) as a second layer, and no control connection
        # has been accepted yet, so no bootstrap/listener/accepted control FD can
        # reach the runtime child. start_new_session gives the child its own
        # bounded process group that the wrapper owns.
        self.child = subprocess.Popen(
            self.child_command,
            env=os.environ.copy(),
            start_new_session=True,
            close_fds=True,
        )
        write_owner_marker(
            self.run_dir,
            dispatch_id=self.payload.dispatch_id,
            run_token=self.payload.run_token,
            wrapper_pid=os.getpid(),
        )

    def merge_observed(self) -> None:
        assert self.child is not None and self.socket_path is not None
        merge_supervisor_observed(
            self.payload.db_path,
            self.payload.dispatch_id,
            run_token=self.payload.run_token,
            wrapper_pid=os.getpid(),
            child_pid=self.child.pid,
            control_socket=str(self.socket_path),
        )

    def ready_ack(self) -> dict[str, Any]:
        assert self.child is not None and self.socket_path is not None
        return {
            "ready": True,
            "child_pid": self.child.pid,
            "wrapper_pid": os.getpid(),
            "control_socket": str(self.socket_path),
            "run_token": self.payload.run_token,
        }

    # -- serve loop -------------------------------------------------------- #

    def serve(self) -> int:
        assert self.child is not None and self.listener is not None
        self.listener.settimeout(0.25)
        while True:
            rc = self.child.poll()
            if rc is not None:
                self._record_exit(rc, source="child")
                return rc if rc is not None else 0
            if time.monotonic() >= self._deadline:
                rc = terminate_process_tree(self.child, self.payload.kill_after_seconds)
                self._record_exit(rc, source="timeout")
                return 124
            try:
                conn, _ = self.listener.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                continue
            keep_open = False
            try:
                keep_open = self._handle_request(conn)
            finally:
                # A confirmed HALT retains its connection (stored on
                # ``self._halt_conn``) so the client sees EOF only at wrapper
                # shutdown; every other request closes here.
                if not keep_open:
                    conn.close()
            if self._halted:
                return self.child.returncode if self.child.returncode is not None else 0

    def _handle_request(self, conn: socket.socket) -> bool:
        """Serve one control request. Returns ``True`` only for a confirmed HALT
        whose connection must stay open (barrier 2 EOF); every other outcome
        returns ``False`` so ``serve()`` closes the connection immediately."""
        assert self.child is not None
        line = _read_line(conn, timeout=2.0)
        if line is None:
            _send_json(conn, {"ok": False, "error": "empty request"})
            return False
        try:
            req = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            _send_json(conn, {"ok": False, "error": "malformed request"})
            return False
        if not isinstance(req, dict):
            _send_json(conn, {"ok": False, "error": "malformed request"})
            return False
        # Authenticate protocol + exact run token before ANY action. A wrong
        # token/protocol never signals and never mutates: it is refused.
        if req.get("protocol_version") != PROTOCOL_VERSION:
            _send_json(conn, {"ok": False, "error": "unsupported protocol"})
            return False
        presented = req.get("run_token")
        if not isinstance(presented, str) or not secrets.compare_digest(presented, self.payload.run_token):
            _send_json(conn, {"ok": False, "error": "unauthenticated"})
            return False
        op = req.get("op")
        if op == "STATUS":
            _send_json(
                conn,
                {"ok": True, "state": "running", "child_pid": self.child.pid, "wrapper_pid": os.getpid()},
            )
            return False
        if op == "HALT":
            return self._handle_halt(conn)
        _send_json(conn, {"ok": False, "error": f"unknown op: {op!r}"})
        return False

    def _handle_halt(self, conn: socket.socket) -> bool:
        """Authenticated HALT with the full pre-response barrier.

        A successful ``halted`` response is sent ONLY after, in order: the owned
        native child tree is terminated/waited; the complete process group is
        proven empty by the group-level kernel predicate; the exact-token
        ``worker_exit`` is persisted; and the run directory / ZDOTDIR are cleaned.
        Any barrier failure sends an explicit ``ok=False`` refusal (the client
        reads it as ``termination_not_confirmed``) and closes normally. On success
        the connection is retained on ``self._halt_conn`` and closed only at
        wrapper shutdown, so the client observes EOF as barrier 2.
        """
        confirmed, returncode, error = self._perform_halt()
        if not confirmed:
            _send_json(
                conn,
                {"ok": False, "state": "halt_not_confirmed", "returncode": returncode, "error": error},
            )
            return False
        self._halted = True
        self._halt_conn = conn
        _send_json(conn, {"ok": True, "state": "halted", "returncode": returncode})
        return True

    def _perform_halt(self) -> tuple[bool, int | None, str | None]:
        """Drive the ordered HALT barriers. Returns ``(confirmed, rc, error)``.

        No SQL transaction spans any external wait: termination/group-drain
        happen first, then a short exit-persist transaction, then filesystem
        cleanup -- each a distinct step, none holding a DB connection open across
        a wait.
        """
        child = self.child
        assert child is not None
        # A start_new_session child is its own process-group leader, so the group
        # id equals its PID by construction and stays valid even if the leader
        # exits/gets reaped before os.getpgid() could observe it. Derive the group
        # id from the PID so the leader-exit/surviving-descendant race can never
        # null the target out and SKIP the mandatory group-level kernel predicate.
        pgid = child.pid
        returncode = terminate_process_tree(child, self.payload.kill_after_seconds)
        # Mandatory group proof, never skipped: killpg(pgid, 0) addresses the whole
        # native group and only an empty group raises ProcessLookupError, so a
        # surviving descendant (even after the leader was reaped) keeps this False
        # and refuses the HALT BEFORE any worker_exit persistence.
        if not process_group_empty(pgid, timeout=max(self.payload.kill_after_seconds, 0.0)):
            return False, returncode, "native process group not empty; termination not confirmed"
        if not self._record_exit(returncode, source="halt"):
            return False, returncode, "worker_exit persistence failed; termination not confirmed"
        if not self._halt_cleanup():
            return False, returncode, "run-directory/ZDOTDIR cleanup failed; termination not confirmed"
        return True, returncode, None

    def _halt_cleanup(self) -> bool:
        """Remove and CONFIRM the disappearance of every owned filesystem artifact
        before a confirmed HALT response.

        Refuses (returns ``False``) on ANY residue: the run directory, its control
        socket (nested in the run directory, verified as its own residue so a
        partial run-dir removal that leaves the bound socket cannot confirm), or
        the OWNED ZDOTDIR PARENT -- the ``agent-comms-zdotdir-*`` directory
        ``_cleanup_zdotdir`` actually removes, not merely the nested child ZDOTDIR
        path. A removal denied by the OS leaves residue and must never confirm
        while a supervisor artifact leaks.
        """
        ok = True
        if self.run_dir is not None:
            cleanup_run_dir(self.run_dir)
            if _path_present(self.run_dir):
                ok = False
        if self.socket_path is not None and _path_present(self.socket_path):
            ok = False
        if self.payload.zdotdir:
            parent = _cleanup_zdotdir(Path(self.payload.zdotdir))
            # Confirm the OWNED artifact (_cleanup_zdotdir's target) is gone, not
            # the nested child path. When the parent is not an owned zdotdir
            # wrapper nothing was removed, so fall back to the child path.
            artifact = parent if parent is not None else Path(self.payload.zdotdir)
            if _path_present(artifact):
                ok = False
        return ok

    def _record_exit(self, returncode: int | None, *, source: str) -> bool:
        """Persist same-run ``worker_exit`` exactly once. Returns whether the
        authoritative row is persisted (from this call or an earlier one).

        The HALT barrier requires this to be TRUE before a confirmed response:
        exit persistence failing (no matching same-run row, or a SQL error) must
        not be masked by the fact that the process died. Non-HALT callers
        (child/timeout/signal/cleanup) ignore the result; the parent reaper is
        their idempotent fallback, so a wrapper that cannot write never crashes
        before cleanup.
        """
        if self._exit_recorded:
            return self._exit_persisted
        self._exit_recorded = True
        try:
            self._exit_persisted = record_worker_exit(
                self.payload.db_path,
                self.payload.dispatch_id,
                self.payload.run_token,
                returncode=returncode,
                source=source,
            )
        except sqlite3.Error:
            self._exit_persisted = False
        return self._exit_persisted

    # -- termination / signals --------------------------------------------- #

    def _terminate_child(self, *, source: str) -> None:
        """Terminate and wait the owned child tree if still alive; record once.

        Idempotent: a child already reaped by ``serve()`` / HALT / TTL is a
        no-op. This is the wrapper terminating its OWN ``start_new_session``
        child, never adapter handle/PID signalling, so a wrapper that dies for
        any reason never leaves a supervisor-less native child alive.
        """
        child = self.child
        if child is None:
            return
        # The leader exiting is NOT proof the native group drained: a
        # SIGTERM-ignoring descendant can outlive the reaped leader. Always drive
        # group termination so teardown never SKIPS it because ``child.poll()`` is
        # already non-None and leaves that descendant orphaned; preserve the
        # leader's own exit code when ``poll()`` already observed it.
        observed = child.poll()
        tree_status = terminate_process_tree(child, self.payload.kill_after_seconds)
        returncode = observed if observed is not None else tree_status
        self._record_exit(returncode, source=source)

    def _on_terminating_signal(self, signum: int, frame: Any) -> None:
        # Robust parent-loss / adapter-timeout guard: an external SIGTERM (for
        # example the adapter abandoning a pre-READY spawn) must never kill only
        # the wrapper and leave its start_new_session child alive. Terminate and
        # wait the owned child tree, record truthful exit evidence, then exit so
        # the finally cleanup runs.
        self._terminate_child(source="signal")
        raise SystemExit(143)

    def install_signal_handlers(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}
        for signum in (signal.SIGTERM, signal.SIGHUP):
            try:
                previous[signum] = signal.signal(signum, self._on_terminating_signal)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass
        return previous

    def restore_signal_handlers(self, previous: dict[int, Any]) -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass

    # -- teardown ---------------------------------------------------------- #

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        # Orphan guard first: never leave a live start_new_session child behind
        # when the wrapper tears down for ANY reason (pre-READY failure,
        # READY-send failure, parent loss, or normal serve() exit). Idempotent
        # with serve()/HALT/TTL, which have already reaped the child.
        self._terminate_child(source="cleanup")
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
        if self.run_dir is not None:
            cleanup_run_dir(self.run_dir)
        if self.payload.zdotdir:
            _cleanup_zdotdir(Path(self.payload.zdotdir))

    def close_halt_connection(self) -> None:
        """Close a retained confirmed-HALT connection, delivering EOF (barrier 2).

        Called last in ``run_supervisor`` teardown -- after termination, exit
        persistence, and cleanup have all completed -- so the client sees EOF
        only when the wrapper is genuinely shutting down. A no-op when no HALT was
        confirmed.
        """
        conn = self._halt_conn
        if conn is None:
            return
        self._halt_conn = None
        try:
            conn.close()
        except OSError:
            pass


def run_supervisor(bootstrap_fd: int, child_command: Sequence[str]) -> int:
    """Wrapper entry point: bootstrap, supervise the child, clean up.

    Any failure before READY is delivered -- adapter timeout, READY-send
    failure, bootstrap parent loss, or a prepare/launch/merge error -- is a
    spawn failure: the finally cleanup terminates and waits any launched child
    so no supervisor-less native child is orphaned, cleans the attempted run
    directory, and READY is never sent, so the adapter records
    ``spawn_failed_message_landed`` rather than a false live row. Observed
    identity written by ``merge_observed`` is only ever added to (exit evidence),
    never overwritten, on such a failure.
    """
    boot = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=bootstrap_fd)
    supervisor: _Supervisor | None = None
    previous_handlers: dict[int, Any] | None = None
    try:
        raw = _read_line(boot, timeout=30.0)
        if raw is None:
            raise SupervisorError("no bootstrap payload received")
        payload = BootstrapPayload.from_json(raw)
        supervisor = _Supervisor(payload, child_command)
        # Install the parent-loss / adapter-timeout signal guard before any
        # child exists so a SIGTERM at any point tears the owned child down.
        previous_handlers = supervisor.install_signal_handlers()
        supervisor.prepare()
        supervisor.launch_child()
        supervisor.merge_observed()
        # READY delivery is the last pre-serve step. If it raises (the adapter
        # peer is already gone / the pipe is broken), the finally cleanup below
        # terminates the just-launched child, so a failed READY never leaves a
        # supervisor-less child alive.
        _send_json(boot, supervisor.ready_ack())
        boot.close()
        boot = None  # type: ignore[assignment]
        return supervisor.serve()
    finally:
        if boot is not None:
            try:
                boot.close()
            except OSError:
                pass
        if supervisor is not None:
            supervisor.cleanup()
            if previous_handlers is not None:
                supervisor.restore_signal_handlers(previous_handlers)
            # Deliver EOF on a confirmed-HALT connection LAST, so the client's
            # barrier 2 fires only once the wrapper has finished tearing down and
            # is about to exit (barrier 3, the parent-owned wrapper reap).
            supervisor.close_halt_connection()


# --------------------------------------------------------------------------- #
# Adapter-side bootstrap (socketpair handshake) and supervised spawn
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SupervisedSpawn:
    popen: subprocess.Popen
    run_token: str
    control_socket: str
    child_pid: int
    wrapper_pid: int
    run_dir: str


def wrapper_script_path() -> Path:
    return Path(__file__).resolve().parent / "timeout_wrapper.py"


def spawn_supervised(
    child_command: Sequence[str],
    *,
    dispatch_id: str,
    db_path: str,
    ttl_seconds: float,
    kill_after_seconds: float,
    run_token: str | None = None,
    root: Path | None = None,
    zdotdir: str | None = None,
    expected_close_by: str | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    stdout: Any = None,
    stderr: Any = None,
    python_executable: str | None = None,
    ready_timeout: float = 30.0,
) -> SupervisedSpawn:
    """Launch the supervisor wrapper and block until READY.

    The run token travels only over the inherited ``socketpair`` bootstrap; it
    is never placed on argv or in the child's environment. The adapter keeps one
    peer and lists only the wrapper peer in ``pass_fds``. On any failure before
    READY the wrapper is terminated and a ``SupervisorError`` is raised.
    """
    import sys

    root = root if root is not None else control_root()
    run_token = run_token or new_run_token()
    python_executable = python_executable or sys.executable

    adapter_peer, wrapper_peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    wrapper_argv = [
        python_executable,
        str(wrapper_script_path()),
        "--supervise",
        "--bootstrap-fd",
        str(wrapper_peer.fileno()),
        "--",
        *[str(part) for part in child_command],
    ]
    popen: subprocess.Popen | None = None
    try:
        popen = subprocess.Popen(
            wrapper_argv,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            pass_fds=(wrapper_peer.fileno(),),
            close_fds=True,
        )
        # The wrapper owns its peer now; the adapter retains only adapter_peer.
        wrapper_peer.close()
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "dispatch_id": dispatch_id,
            "run_token": run_token,
            "db_path": str(db_path),
            "control_root": str(root),
            "ttl_seconds": float(ttl_seconds),
            "kill_after_seconds": float(kill_after_seconds),
            "zdotdir": zdotdir,
            "expected_close_by": expected_close_by,
        }
        # The adapter-side bootstrap send/read/parse can fail if the wrapper dies
        # early: a broken pipe / reset on send or read, a truncated or non-JSON
        # READY line, or an ack missing its required fields. Every such
        # OSError / EOF / decode failure is normalised to a typed pre-READY
        # SupervisorError (cause preserved via ``from``) so the adapter records
        # ``spawn_failed_message_landed`` instead of leaking a raw OSError; the
        # outer ``except BaseException`` still runs the safe wrapper teardown
        # (``_terminate_wrapper``) so no supervisor-less child is orphaned.
        try:
            _send_json(adapter_peer, payload)
        except OSError as exc:
            raise SupervisorError(
                f"supervisor bootstrap send failed before READY: {exc}"
            ) from exc
        try:
            line = _read_line(adapter_peer, timeout=ready_timeout)
        except OSError as exc:
            raise SupervisorError(
                f"supervisor bootstrap read failed before READY: {exc}"
            ) from exc
        if line is None:
            raise SupervisorError("supervisor did not send READY before deadline")
        try:
            ack = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise SupervisorError(
                f"supervisor READY handshake was not valid JSON: {exc}"
            ) from exc
        if not isinstance(ack, dict) or not ack.get("ready"):
            raise SupervisorError(f"supervisor READY handshake failed: {ack!r}")
        try:
            return SupervisedSpawn(
                popen=popen,
                run_token=run_token,
                control_socket=str(ack.get("control_socket") or control_socket_for(root, run_token)),
                child_pid=int(ack["child_pid"]),
                wrapper_pid=int(ack.get("wrapper_pid") or popen.pid),
                run_dir=str(run_dir_for(root, run_token)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SupervisorError(
                f"supervisor READY handshake missing/invalid fields ({ack!r}): {exc}"
            ) from exc
    except BaseException:
        if popen is not None:
            _terminate_wrapper(popen)
        try:
            wrapper_peer.close()
        except OSError:
            pass
        raise
    finally:
        adapter_peer.close()


def _terminate_wrapper(popen: subprocess.Popen, grace_seconds: float = 5.0) -> None:
    """Tear a wrapper down (on a pre-READY failure) without orphaning its child.

    Sends the wrapper an ownership ``SIGTERM`` (``Popen.terminate``), never
    ``killpg`` of a handle-parsed PID and never a ``SIGKILL`` of the wrapper's
    session -- that would kill only the wrapper and orphan its separate
    ``start_new_session`` child. The wrapper's own signal handler terminates and
    waits its owned child tree before exiting. If the wrapper does not exit
    within the grace it is PRESERVED rather than force-killed into an orphan; its
    own hard TTL bounds the lifecycle and stage-2/manual owns dead-supervisor
    recovery.
    """
    if popen.poll() is not None:
        return
    try:
        popen.terminate()
    except OSError:
        pass
    try:
        popen.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:  # pragma: no cover - pathological wrapper
        pass


# --------------------------------------------------------------------------- #
# ZDOTDIR cleanup (shared with the adapter's pre-READY failure path)
# --------------------------------------------------------------------------- #


def _cleanup_zdotdir(zdotdir: Path) -> Path | None:
    """Remove the owned ``agent-comms-zdotdir-*`` parent of a ZDOTDIR.

    Returns the parent path it targeted for removal (the OWNED artifact) so a
    caller can confirm disappearance of the exact directory this removes rather
    than the nested child path; returns ``None`` when the parent is not an owned
    zdotdir wrapper (nothing removed).
    """
    parent = zdotdir.parent
    if not parent.name.startswith("agent-comms-zdotdir-"):
        return None
    shutil.rmtree(parent, ignore_errors=True)
    return parent


# --------------------------------------------------------------------------- #
# One module-global parent reaper registry per spawning process
# --------------------------------------------------------------------------- #


# Bounded, non-signalling parent-side proof of the registered native child
# process group: the wrapper (server side) already drained it before its HALT
# response, so this poll returns immediately in the normal path. The parent
# never signals the group; a group that has not drained refuses the proof (the
# entry is retained for bounded retry) rather than blocking or force-killing.
_REAP_GROUP_VERIFY_TIMEOUT_SECONDS = 2.0


def _owned_zdotdir_parent(zdotdir: str | None) -> Path | None:
    """The exact owned ``agent-comms-zdotdir-*`` parent of a registered ZDOTDIR,
    or ``None`` when there is no owned parent to verify.

    Mirrors exactly the ownership boundary :func:`_cleanup_zdotdir` removes: it
    never widens to an unrelated actor-named directory, so a pre-existing
    unrelated directory can neither satisfy nor poison the absence barrier.
    """
    if not zdotdir:
        return None
    parent = Path(zdotdir).parent
    if not parent.name.startswith("agent-comms-zdotdir-"):
        return None
    return parent


@dataclass
class _ReaperEntry:
    handle: str
    popen: subprocess.Popen
    db_path: str
    dispatch_id: str
    run_token: str
    run_dir: str | None
    zdotdir: str | None
    # Revision 7 F2: the exact parent-registry association retains, alongside the
    # exact wrapper ``Popen`` above, the identities the terminal-proof barriers
    # need -- the exact control socket, the wrapper process-group id (the wrapper
    # is launched ``start_new_session`` so its group id equals its PID), and the
    # registered native child process-group id (from ``SupervisedSpawn.child_pid``,
    # itself a ``start_new_session`` leader). Identity is never rediscovered by
    # ``ps``/glob/recency/spawn_handle parsing; it is what the adapter registered
    # at READY. ``None`` means an identity was not registered and the matching
    # barrier fails closed rather than being assumed drained.
    control_socket: str | None = None
    wrapper_pgid: int | None = None
    child_pgid: int | None = None
    # Single-owner marker: an entry is CLAIMED atomically (under the registry
    # lock) by exactly one reaping path -- a HALT that owns the exact-wrapper reap,
    # or one reap_ready() caller. A claimed entry is skipped by every other path so
    # one registered wrapper is never processed twice; a failed HALT releases it.
    claimed: bool = False


class ReaperRegistry:
    """One registry + one background thread per spawning process.

    It retains every wrapper ``Popen`` (the adapter's direct children), reaps
    those that have exited, merges exact same-run return code / time into only
    the matching run-token row, and performs idempotent fallback cleanup. It
    never transitions ledger status; the producer cap bounds its size.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _ReaperEntry] = {}
        # Claimed entries displaced by a same-handle re-registration are held here
        # (not in ``_entries``, whose key is now the replacement) so fallback
        # reaping never loses a HALT-claimed wrapper. Reaped/finalized out
        # identity-guarded; only ever holds entries an owner still has to
        # finalize() or release_claim().
        self._displaced: list[_ReaperEntry] = []
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = False

    def register(
        self,
        handle: str,
        popen: subprocess.Popen,
        *,
        db_path: str,
        dispatch_id: str,
        run_token: str,
        run_dir: str | None = None,
        zdotdir: str | None = None,
        control_socket: str | None = None,
        wrapper_pgid: int | None = None,
        child_pgid: int | None = None,
    ) -> None:
        with self._lock:
            prior = self._entries.get(handle)
            if prior is not None and prior.claimed:
                # A HALT owns ``prior``'s exact reap. Reusing the handle must not
                # drop the claimed wrapper on the floor: preserve it as displaced
                # so a later failed HALT/release restores its fallback reaping
                # instead of stranding it, while the replacement takes the slot.
                self._displaced.append(prior)
            self._entries[handle] = _ReaperEntry(
                handle=handle,
                popen=popen,
                db_path=db_path,
                dispatch_id=dispatch_id,
                run_token=run_token,
                run_dir=run_dir,
                zdotdir=zdotdir,
                control_socket=control_socket,
                wrapper_pgid=wrapper_pgid,
                child_pgid=child_pgid,
            )
            self._ensure_thread_locked()
        self._wake.set()

    def discard(self, handle: str) -> _ReaperEntry | None:
        with self._lock:
            return self._entries.pop(handle, None)

    def find_by_run_token(self, run_token: str) -> _ReaperEntry | None:
        """The retained entry (wrapper ``Popen`` + identity) for a run token.

        Run tokens are unique per dispatch, so this yields the single exact
        registered wrapper the authenticated HALT client reaps for barrier 3.
        Returns ``None`` when no such wrapper is registered (e.g. it already
        exited and was reaped/discarded).
        """
        with self._lock:
            for entry in self._entries.values():
                if entry.run_token == run_token:
                    return entry
        return None

    def pending(self) -> int:
        with self._lock:
            return len(self._entries)

    def claim_by_run_token(self, run_token: str) -> _ReaperEntry | None:
        """Atomically claim sole ownership of the single entry for a run token.

        Returns the claimed entry, or ``None`` when no still-registered, unclaimed
        entry matches. A claimed entry is skipped by ``reap_ready()`` (and by a
        second claim), so the authenticated HALT that owns the exact-wrapper reap
        and the background reaper never both process one registered wrapper. The
        claimer must finalize (:meth:`finalize_claimed`) or release
        (:meth:`release_claim`) it.
        """
        with self._lock:
            for entry in self._entries.values():
                if entry.run_token == run_token:
                    if entry.claimed:
                        return None
                    entry.claimed = True
                    return entry
        return None

    def release_claim(self, entry: _ReaperEntry) -> None:
        """Release a claim so fallback background reaping can still own the entry.

        Used when a HALT does not confirm: the exact wrapper must remain reapable
        by the idempotent fallback rather than being stranded owned-but-unreaped.
        """
        with self._lock:
            entry.claimed = False

    def _reap_barriers_verified(self, entry: _ReaperEntry, *, clean: bool) -> bool:
        """Freshly prove EVERY physical reap barrier for ``entry``.

        In order: the exact wrapper ``Popen`` is reaped; its exact wrapper
        process group is absent (the wrapper is a ``start_new_session`` leader,
        so its group id is its PID -- a registered ``wrapper_pgid`` is used when
        present, else the exact ``Popen`` pid); the registered native child
        process group (a ``start_new_session`` leader whose group id is
        ``child_pid``) is empty -- a missing child identity FAILS CLOSED rather
        than being assumed drained; and the exact run directory, control socket,
        and owned ZDOTDIR parent are absent.

        The no-HALT (background) producer passes ``clean=True`` so it
        idempotently removes ONLY its exact registered owned artifacts before
        the absence check; the HALT producer passes ``clean=False`` because the
        server-side HALT already cleaned and the parent only VERIFIES. Either
        way absence is freshly verified here. Unrelated residue is never touched.
        """
        if entry.popen.poll() is None:
            return False
        wrapper_pgid = entry.wrapper_pgid if entry.wrapper_pgid is not None else entry.popen.pid
        if not _process_group_gone(wrapper_pgid):
            return False
        if entry.child_pgid is None:
            return False
        if not process_group_empty(entry.child_pgid, timeout=_REAP_GROUP_VERIFY_TIMEOUT_SECONDS):
            return False
        if clean:
            if entry.run_dir is not None:
                cleanup_run_dir(Path(entry.run_dir))
            if entry.zdotdir:
                _cleanup_zdotdir(Path(entry.zdotdir))
        return self._owned_artifacts_absent(entry)

    def _owned_artifacts_absent(self, entry: _ReaperEntry) -> bool:
        """Whether the exact registered run directory, control socket, and owned
        ZDOTDIR parent are all absent. A path that cannot be ruled out counts as
        present (fail closed). An unregistered (``None``) identity is nothing
        owned to leak and does not by itself fail the barrier."""
        if entry.run_dir is not None and _path_present(Path(entry.run_dir)):
            return False
        if entry.control_socket and _path_present(Path(entry.control_socket)):
            return False
        zparent = _owned_zdotdir_parent(entry.zdotdir)
        if zparent is not None and _path_present(zparent):
            return False
        return True

    def finalize_claimed(self, entry: _ReaperEntry) -> bool:
        """Prove every remaining HALT-finalize barrier, THEN durably persist the
        complete reap proof, THEN remove the claimed entry. Returns whether a
        durable complete proof now exists.

        The claimed HALT already drove response -> EOF -> the exact ``Popen``
        wait. Before publishing any proof or dropping the only exact-``Popen``
        association, this proves the exact wrapper process group is absent, the
        registered native child process group is empty, and the exact run
        directory, control socket, and owned ZDOTDIR parent are (freshly
        verified) absent -- the server-side HALT already cleaned them; the parent
        only verifies. Only then does it persist the complete
        ``source="halt_finalize"`` proof and drop the entry. Any barrier miss OR
        persistence failure publishes NO proof, RELEASES the claim, and RETAINS
        the exact entry so a later HALT or the background reaper can retry: no
        claimed entry is stranded and the only exact-``Popen`` association is
        never discarded before a durable complete proof exists. Persistence runs
        OUTSIDE the registry lock (a short SQL transaction). Removal stays
        identity-guarded: a same-handle replacement registered since the claim is
        never evicted; a displaced claimed entry is dropped from the holding list
        instead of the live slot.
        """
        if not self._reap_barriers_verified(entry, clean=False):
            with self._lock:
                entry.claimed = False
            return False
        try:
            persisted = record_registered_wrapper_reap(
                entry.db_path,
                entry.dispatch_id,
                entry.run_token,
                returncode=entry.popen.returncode,
                source=REAP_SOURCE_HALT_FINALIZE,
                registered_wrapper_reaped=True,
                native_process_group_drained=True,
                owned_artifacts_absent={"run_dir": True, "control_socket": True, "zdotdir_parent": True},
            )
        except sqlite3.Error as exc:
            logger.warning(
                "halt-finalize reap persistence failed for %s: %s", entry.dispatch_id, exc
            )
            persisted = False
        with self._lock:
            if not persisted:
                entry.claimed = False
                return False
            if self._entries.get(entry.handle) is entry:
                del self._entries[entry.handle]
            else:
                self._displaced = [e for e in self._displaced if e is not entry]
            return True

    def reap_ready(self) -> list[str]:
        """Synchronously reap any exited wrappers. Returns reaped handles.

        Each exited entry is CLAIMED atomically under the lock before processing,
        so concurrent ``reap_ready()`` callers (or a HALT that already claimed it)
        never process one registered wrapper twice. Removal is identity-guarded:
        a same-handle replacement registered since the snapshot is never evicted
        by the stale owner. Displaced-but-claimed wrappers (a HALT-claimed entry
        pushed aside by a same-handle re-registration) are scanned here too, so a
        released claim regains fallback reaping.
        """
        with self._lock:
            items = list(self._entries.values()) + list(self._displaced)
        reaped: list[str] = []
        for entry in items:
            returncode = entry.popen.poll()
            if returncode is None:
                continue
            with self._lock:
                live = self._entries.get(entry.handle) is entry
                displaced = any(e is entry for e in self._displaced)
                if not live and not displaced:
                    continue  # removed or replaced since the snapshot
                if entry.claimed:
                    continue  # another owner (HALT or a concurrent reap) has it
                entry.claimed = True
            try:
                persisted = self._reap_entry(entry, returncode)
            except Exception as exc:
                # Containment: an unexpected per-entry failure (e.g. an owned
                # run-dir cleanup OSError) publishes no proof and must neither
                # strand this claim nor terminate a reaping caller/loop; the
                # exact association is retained below for bounded retry.
                logger.warning(
                    "unexpected reap failure for %s (handle %s): %s",
                    entry.dispatch_id,
                    entry.handle,
                    exc,
                )
                persisted = False
            with self._lock:
                if not persisted:
                    # Durable evidence does not exist yet: keep the exact Popen
                    # association and release the claim so the same idempotent
                    # persistence is retried on the next pass, never discarded.
                    entry.claimed = False
                    continue
                if self._entries.get(entry.handle) is entry:
                    del self._entries[entry.handle]
                else:
                    self._displaced = [e for e in self._displaced if e is not entry]
            reaped.append(entry.handle)
        return reaped

    def _reap_entry(self, entry: _ReaperEntry, returncode: int | None) -> bool:
        """No-HALT background producer for one exited registered wrapper.

        Revision 7 F2 order: ``reap_ready`` has observed the exact wrapper
        exited; this proves the wrapper and registered native child groups
        drained, then IDEMPOTENTLY CLEANS only the exact registered run
        directory / control socket / ZDOTDIR parent and FRESHLY VERIFIES their
        absence (all via :meth:`_reap_barriers_verified` with ``clean=True``),
        and ONLY THEN persists the complete ``source="background_reap"`` proof
        through the same identity-bound primitive the HALT finalize uses.
        ``reap_ready`` drops the entry only after this returns ``True``. A group,
        cleanup, absence, or persistence miss publishes NO proof and returns
        ``False`` so the exact association is retained for bounded retry;
        unrelated residue is never touched.
        """
        if not self._reap_barriers_verified(entry, clean=True):
            return False
        try:
            return record_registered_wrapper_reap(
                entry.db_path,
                entry.dispatch_id,
                entry.run_token,
                returncode=returncode,
                source=REAP_SOURCE_BACKGROUND_REAP,
                registered_wrapper_reaped=True,
                native_process_group_drained=True,
                owned_artifacts_absent={"run_dir": True, "control_socket": True, "zdotdir_parent": True},
            )
        except sqlite3.Error as exc:
            logger.warning(
                "background reap persistence failed for %s: %s", entry.dispatch_id, exc
            )
            return False

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(
            target=self._loop, name="agent-comms-reaper", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop:
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            # Recheck after every wake: the wake stop() delivers must never
            # trigger one final post-stop reap.
            if self._stop:
                break
            self.reap_ready()

    def stop(self) -> None:
        """Signal the owned thread and synchronously join it to quiescence.

        Returns only after the thread has exited: in-flight entry processing has
        completed and no post-stop background reap can run (the loop rechecks
        the stop flag after the wake this delivers). Entries stay registered for
        a later synchronous ``reap_ready()`` or a restarted thread."""
        with self._lock:
            self._stop = True
            thread = self._thread
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join()


_REGISTRY: ReaperRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def reaper_registry() -> ReaperRegistry:
    """The one registry for this spawning process."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = ReaperRegistry()
        return _REGISTRY


# --------------------------------------------------------------------------- #
# Conservative spawn-time janitor over the protected root only
# --------------------------------------------------------------------------- #


def janitor_sweep(db_path: str, root: Path | None = None) -> list[dict[str, Any]]:
    """Remove only run directories whose owner is SQL-terminal AND confirmed gone.

    Bounded to the fixed root's immediate strict-name, real-directory,
    non-symlink children. Terminal status alone is NOT sufficient authority: a
    directory is removed only when its versioned owner marker parses and matches
    a dispatch/run token that SQL reports terminal (``closed``, ``dlq``,
    ``spawn_failed_message_landed``, ``cancelled``) AND SQL carries positive
    same-run termination evidence (a not_started/confirmed ``termination_result``
    or a ``worker_exit``/``reaper_exit`` object whose run token matches). Missing
    DB row, nonterminal row, a terminal owner WITHOUT confirmed termination
    evidence (e.g. a hard-TTL ``dlq`` with ``termination_not_confirmed``),
    malformed/other-version marker, symlink, token mismatch, or DB failure
    preserves the directory loudly. No filesystem scan decides lifecycle state.
    """
    root = root if root is not None else control_root()
    outcomes: list[dict[str, Any]] = []

    def note(path: Path, action: str, reason: str, *, loud: bool = False) -> None:
        # Operationally loud without blocking spawn: a removal or a suspicious
        # preserve (DB error, malformed/mismatched marker on a strict-named run
        # dir, unknown marker version) is logged at WARNING/INFO so a lingering
        # protected-root directory is visible; benign structural skips are DEBUG.
        # The filesystem never decides lifecycle state; it is only cleanup
        # ownership evidence, so a preserve is always safe.
        outcomes.append({"path": str(path), "action": action, "reason": reason})
        if action == "removed":
            logger.info("supervisor janitor removed %s: %s", path, reason)
        elif loud:
            logger.warning("supervisor janitor preserved %s: %s", path, reason)
        else:
            logger.debug("supervisor janitor preserved %s: %s", path, reason)

    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return outcomes
    except OSError as exc:
        note(root, "preserved", f"scandir failed: {exc}", loud=True)
        return outcomes

    for entry in entries:
        name = entry.name
        path = Path(entry.path)
        if not is_strict_run_dir_name(name):
            note(path, "preserved", "non-strict name")
            continue
        if entry.is_symlink():
            note(path, "preserved", "symlink", loud=True)
            continue
        if not entry.is_dir(follow_symlinks=False):
            note(path, "preserved", "not a directory", loud=True)
            continue
        marker = read_owner_marker(path)
        if marker is None:
            note(path, "preserved", "missing/malformed marker", loud=True)
            continue
        if marker.get("marker_version") != MARKER_VERSION:
            note(path, "preserved", "unknown marker version", loud=True)
            continue
        dispatch_id = marker.get("dispatch_id")
        run_token = marker.get("run_token")
        if not isinstance(dispatch_id, str) or not isinstance(run_token, str):
            note(path, "preserved", "malformed marker fields", loud=True)
            continue
        if run_token != name:
            note(path, "preserved", "marker token != dir name", loud=True)
            continue
        try:
            state = dispatch_cleanup_state(db_path, dispatch_id)
        except sqlite3.Error as exc:
            note(path, "preserved", f"db error: {exc}", loud=True)
            continue
        if state is None:
            note(path, "preserved", "no dispatch row")
            continue
        projection, sql_run_token, observed = state
        # Cleanup consumes the joined projection: terminal-ness follows the raw
        # EXECUTION (ledger) status, while the transport-aware normalized
        # ``outcome`` is recorded on every cleanup decision so cleanup and
        # reporting share one classification (a dlq owner whose recipient copy
        # was operator-settled reads ``operator_settled_termination_unconfirmed``,
        # not a bare ``dlq``).
        status = projection["dispatch_status"]
        outcome = projection["outcome"]
        if status not in DISPATCH_TERMINAL_STATUSES:
            note(path, "preserved", f"nonterminal: {status}")
            continue
        if sql_run_token is not None and sql_run_token != run_token:
            note(path, "preserved", "sql token mismatch", loud=True)
            continue
        # Positive-evidence gate: terminal status alone never authorizes
        # deletion. A terminal owner without confirmed same-run termination
        # evidence (e.g. a hard-TTL dlq with termination_not_confirmed, possibly a
        # real zombie) is preserved loudly for operator recovery.
        evidence = confirmed_termination_evidence(observed, run_token)
        if evidence is None:
            note(
                path,
                "preserved",
                f"terminal {status}/{outcome} without confirmed termination evidence",
                loud=True,
            )
            continue
        cleanup_run_dir(path)
        note(path, "removed", f"terminal {status}/{outcome} ({evidence})")
    return outcomes
