"""Conservative spawn-time janitor over the protected control root only.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..terminal_statuses import DISPATCH_TERMINAL_STATUSES
from . import evidence, paths

logger = logging.getLogger("agent_comms.supervisor.janitor")


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
    root = root if root is not None else paths.control_root()
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
        if not paths.is_strict_run_dir_name(name):
            note(path, "preserved", "non-strict name")
            continue
        if entry.is_symlink():
            note(path, "preserved", "symlink", loud=True)
            continue
        if not entry.is_dir(follow_symlinks=False):
            note(path, "preserved", "not a directory", loud=True)
            continue
        marker = paths.read_owner_marker(path)
        if marker is None:
            note(path, "preserved", "missing/malformed marker", loud=True)
            continue
        if marker.get("marker_version") != paths.MARKER_VERSION:
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
            state = evidence.dispatch_cleanup_state(db_path, dispatch_id)
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
        termination_evidence = evidence.confirmed_termination_evidence(observed, run_token)
        if termination_evidence is None:
            note(
                path,
                "preserved",
                f"terminal {status}/{outcome} without confirmed termination evidence",
                loud=True,
            )
            continue
        paths.cleanup_run_dir(path)
        note(path, "removed", f"terminal {status}/{outcome} ({termination_evidence})")
    return outcomes
