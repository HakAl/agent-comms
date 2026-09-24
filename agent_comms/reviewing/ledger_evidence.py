"""Query-only ledger evidence and binding registry behind the review facade.

Sole owner of the query-only SQLite connection and the binding registry. Holds
the centralized read-only ledger open, the resolved worker-root binding and
recipient-root verification, the worker closeout derivation and artifact
verification, the atomic claim publication and binding claim, and the
recover-binding transaction. Preserves the claim/recovery lock order, fsync
order, forensic publication, idempotent audit events, stdout, and exact
refusals.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from agent_comms import paths as runtime_paths
from agent_comms.reviewing import store
from agent_comms.reviewing.contracts import (
    ReviewError,
    WORKER_DISPATCH_ID_RE,
    raise_prior_schema_read_only,
)


def _open_ledger_for_reading(
    db_path: Path,
    error_builder: Callable[[sqlite3.Error], ReviewError],
) -> sqlite3.Connection:
    """Open an existing ledger with query-only enforcement."""
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error as exc:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        raise error_builder(exc) from exc


def _resolved_binding_path(
    value: str | Path,
    *,
    operation: str,
    recipient: str,
    label: str,
    ledger_db: Path | None = None,
    review_repo: str | Path | None = None,
    configured_root: str | Path | None = None,
) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        details = [f"recipient={recipient}"]
        if ledger_db is not None:
            details.append(f"ledger_db={ledger_db}")
        if review_repo is not None:
            details.append(f"review_repo={review_repo}")
        if configured_root is not None:
            details.append(f"configured_root={configured_root}")
        raise ReviewError(
            f"review_repo_worker_root_resolution_failed: operation={operation}; "
            f"path={label}; {'; '.join(details)}; error={exc}"
        ) from exc


def require_review_repo_worker_root(record: dict[str, Any]) -> None:
    """Fail closed unless the review repo is the recipient's execution root."""
    operation = "review_repo_worker_root_binding"
    recipient = record["expected_recipient"]
    review_repo_raw = record["repo"]
    try:
        ledger_raw = runtime_paths.db_path()
        ledger_db = Path(ledger_raw).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ReviewError(
            f"review_repo_worker_root_ledger_path_failed: operation={operation}; "
            f"recipient={recipient}; review_repo={review_repo_raw}; error={exc}"
        ) from exc

    def open_error(exc: sqlite3.Error) -> ReviewError:
        return ReviewError(
            f"review_repo_worker_root_ledger_open_failed: operation={operation}; "
            f"recipient={recipient}; ledger_db={ledger_db}; review_repo={review_repo_raw}; error={exc}; "
            f"WAL open requires write access to the ledger directory for sidecar creation"
        )

    conn = _open_ledger_for_reading(ledger_db, open_error)
    try:
        try:
            actor = conn.execute(
                "select project_root from actors where id=?", (recipient,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise ReviewError(
                f"review_repo_worker_root_ledger_query_failed: operation={operation}; "
                f"recipient={recipient}; ledger_db={ledger_db}; review_repo={review_repo_raw}; error={exc}"
            ) from exc
    finally:
        try:
            conn.close()
        except sqlite3.Error as exc:
            raise ReviewError(
                f"review_repo_worker_root_ledger_close_failed: operation={operation}; "
                f"recipient={recipient}; ledger_db={ledger_db}; review_repo={review_repo_raw}; error={exc}"
            ) from exc
    if actor is None:
        raise ReviewError(
            f"review_repo_worker_root_actor_missing: operation={operation}; recipient={recipient}; "
            f"ledger_db={ledger_db}; review_repo={review_repo_raw}"
        )
    configured_root_raw = actor["project_root"]
    if not isinstance(configured_root_raw, str) or not configured_root_raw.strip():
        raise ReviewError(
            f"review_repo_worker_root_missing: operation={operation}; recipient={recipient}; "
            f"ledger_db={ledger_db}; review_repo={review_repo_raw}; configured_root={configured_root_raw!r}"
        )
    review_repo = _resolved_binding_path(
        review_repo_raw,
        operation=operation,
        recipient=recipient,
        label="review_repo",
        ledger_db=ledger_db,
        review_repo=review_repo_raw,
        configured_root=configured_root_raw,
    )
    configured_root = _resolved_binding_path(
        configured_root_raw,
        operation=operation,
        recipient=recipient,
        label="configured_root",
        ledger_db=ledger_db,
        review_repo=review_repo_raw,
        configured_root=configured_root_raw,
    )
    if review_repo != configured_root:
        raise ReviewError(
            f"review_repo_worker_root_mismatch: operation={operation}; recipient={recipient}; "
            f"ledger_db={ledger_db}; review_repo={review_repo}; configured_root={configured_root}"
        )


def _derive_worker_evidence(
    record: dict[str, Any], required_result: str
) -> dict[str, Any]:
    intents = record.get("intended_dispatches") or []
    if not intents:
        raise ReviewError(
            "intended_dispatch_unrecorded: no active dispatch intent; worker_dispatch_id=unresolved"
        )
    intent = intents[-1]
    db_path = runtime_paths.db_path().resolve()
    conn = _open_ledger_for_reading(
        db_path,
        lambda exc: ReviewError(
            f"ledger_open_failed: {exc}; ledger_db={db_path}; worker_dispatch_id=unresolved; "
            f"WAL open requires write access to the ledger directory for sidecar creation"
        ),
    )
    try:
        row = conn.execute(
            "select * from dispatch_ledger where producer_actor_id=? and idempotency_key=?",
            (record["expected_producer"], intent["idempotency_key"]),
        ).fetchone()
        if row is None:
            raise ReviewError(
                f"intended_dispatch_not_found: active key {intent['idempotency_key']!r}; ledger_db={db_path}; worker_dispatch_id=unresolved"
            )
        worker_id = row["dispatch_id"]
        if not WORKER_DISPATCH_ID_RE.fullmatch(worker_id):
            raise ReviewError(f"invalid_worker_dispatch_id: {worker_id!r}")
        linked = intent.get("respawn_dispatch_id")
        if isinstance(linked, str) and worker_id != linked:
            raise ReviewError(
                f"respawn_dispatch_mismatch: intent links {linked}; ledger_db={db_path}; worker_dispatch_id={worker_id}"
            )
        if (
            row["producer_actor_id"] != record["expected_producer"]
            or row["recipient_actor_id"] != record["expected_recipient"]
        ):
            raise ReviewError(
                f"intent_mismatch: ledger actors differ; ledger_db={db_path}; worker_dispatch_id={worker_id}"
            )
        if row["status"] != "closed" or row["result"] != required_result:
            raise ReviewError(
                f"result_not_{required_result}: status={row['status']} result={row['result']}; ledger_db={db_path}; worker_dispatch_id={worker_id}"
            )
        observed = json.loads(row["observed_values_json"] or "{}")
        closeout = observed.get("closeout")
        if not isinstance(closeout, dict) or closeout.get("protocol") != 1:
            raise ReviewError(
                f"closeout_missing: ledger_db={db_path}; worker_dispatch_id={worker_id}"
            )
        if closeout.get("recorded_by") != record["expected_recipient"]:
            raise ReviewError(
                f"intent_mismatch: closeout recorder differs; ledger_db={db_path}; worker_dispatch_id={worker_id}"
            )
        corroborated = conn.execute(
            """select 1 from messages m join message_threads mt on mt.message_id=m.id
               join message_recipients mr on mr.message_id=m.id
               where m.id=? and m.from_agent=? and mt.parent_message_id=? and mr.to_agent=?""",
            (
                closeout.get("reply_message_id"),
                record["expected_recipient"],
                row["message_id"],
                record["expected_producer"],
            ),
        ).fetchone()
        if corroborated is None:
            raise ReviewError(
                f"thread_mismatch: ledger_db={db_path}; worker_dispatch_id={worker_id}"
            )
        return {
            "row": dict(row),
            "closeout": closeout,
            "intent": intent,
            "ledger_db": str(db_path),
        }
    finally:
        conn.close()


def _verify_artifact_bindings(
    record: dict[str, Any], evidence: dict[str, Any], head: str
) -> list[dict[str, Any]]:
    repo = Path(record["repo"]).resolve()
    verified = []
    for artifact in evidence["closeout"].get("artifacts") or []:
        if not isinstance(artifact, dict):
            raise ReviewError("stale_artifact_evidence: malformed artifact binding")
        real = Path(str(artifact.get("real_path", ""))).resolve()
        expected = artifact.get("remeasured_sha256")
        try:
            current = hashlib.sha256(real.read_bytes()).hexdigest()
        except OSError as exc:
            raise ReviewError("stale_artifact_evidence: artifact missing") from exc
        binding = "filesystem"
        if real.is_relative_to(repo):
            rel = str(real.relative_to(repo))
            proc = subprocess.run(
                ["git", "show", f"{head}:{rel}"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if (
                proc.returncode != 0
                or hashlib.sha256(proc.stdout).hexdigest() != expected
            ):
                raise ReviewError("stale_artifact_evidence: committed blob differs")
            binding = "committed"
        if current != expected:
            raise ReviewError("stale_artifact_evidence: working tree differs")
        verified.append(
            {**artifact, "binding": binding, "verified_at": store.utc_now()}
        )
    return verified


def _atomic_claim_write(path: Path, payload: dict[str, Any]) -> None:
    """Publish a complete claim from outside bindings/, then fsync its directory."""
    staging = store.REVIEW_ROOT / ".binding-staging"
    staging.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="claim-", dir=str(staging))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


@contextlib.contextmanager
def _binding_claim(record: dict[str, Any], evidence: dict[str, Any]):
    worker_id = evidence["row"]["dispatch_id"]
    if not WORKER_DISPATCH_ID_RE.fullmatch(worker_id):
        raise ReviewError(f"invalid_worker_dispatch_id: {worker_id!r}")
    root = store.REVIEW_ROOT / "bindings"
    root.mkdir(parents=True, exist_ok=True)
    lock_path, claim_path = root / ".lock", root / worker_id
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReviewError("binding_registry_busy") from exc
        if claim_path.exists():
            try:
                claim = json.loads(claim_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ReviewError("binding_metadata_malformed") from exc
            token = {
                "bound": "evidence_already_bound",
                "pending": "evidence_binding_pending",
                "quarantined": "evidence_quarantined",
            }.get(claim.get("state"), "binding_metadata_malformed")
            raise ReviewError(f"{token}: winner={claim.get('record_id')}")
        claim = {
            "worker_dispatch_id": worker_id,
            "record_id": record["dispatch_id"],
            "state": "pending",
            "claimed_at": store.utc_now(),
        }
        _atomic_claim_write(claim_path, claim)
        try:
            yield claim_path
            claim["state"] = "bound"
            claim["bound_at"] = store.utc_now()
            _atomic_claim_write(claim_path, claim)
        except Exception:
            raise


def command_recover_binding(args) -> None:
    worker_id = args.worker_dispatch_id
    if not WORKER_DISPATCH_ID_RE.fullmatch(worker_id):
        raise ReviewError(f"invalid_worker_dispatch_id: {worker_id!r}")
    root = store.REVIEW_ROOT / "bindings"
    root.mkdir(parents=True, exist_ok=True)
    claim_path = root / worker_id

    def sweep_staging() -> None:
        staging = store.REVIEW_ROOT / ".binding-staging"
        if not staging.is_dir():
            return
        for candidate in staging.iterdir():
            if candidate.is_file() and (
                candidate.name.startswith("claim-")
                or candidate.name.startswith("forensic-")
            ):
                candidate.unlink()

    def audit(record: dict[str, Any], event: str) -> bool:
        history = record.setdefault("history", [])
        if any(
            item.get("event") == event and item.get("worker_dispatch_id") == worker_id
            for item in history
        ):
            return False
        history.append(
            {
                "event": event,
                "timestamp": store.utc_now(),
                "worker_dispatch_id": worker_id,
            }
        )
        return True

    with (root / ".lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not claim_path.exists():
            raise ReviewError("binding_not_found")
        try:
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if not args.quarantine_malformed:
                raise ReviewError("binding_metadata_malformed") from exc
            quarantine = root / "quarantine"
            quarantine.mkdir(exist_ok=True)
            (store.REVIEW_ROOT / ".binding-staging").mkdir(parents=True, exist_ok=True)
            forensic = quarantine / f"{worker_id}.{int(time.time_ns())}"
            raw = claim_path.read_bytes()
            fd, tmp = tempfile.mkstemp(
                prefix="forensic-", dir=str(store.REVIEW_ROOT / ".binding-staging")
            )
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, forensic)
            _atomic_claim_write(
                claim_path,
                {
                    "worker_dispatch_id": worker_id,
                    "state": "quarantined",
                    "quarantined_at": store.utc_now(),
                    "forensics": str(forensic),
                },
            )
            print(f"recover-binding {worker_id}: quarantined malformed claim")
            return
        if claim.get("state") in {"bound", "quarantined"}:
            print(f"recover-binding {worker_id}: {claim.get('state')} no-op")
            return
        if claim.get("state") != "pending" or not isinstance(
            claim.get("record_id"), str
        ):
            raise ReviewError("binding_metadata_malformed")
        record_id = claim["record_id"]
        record = store.read_record(record_id)
        if record["schema_version"] == 1:
            raise_prior_schema_read_only(record)
        sweep_staging()
    paths = store.review_paths(record_id)
    with paths.lock.open("a+") as record_lock:
        fcntl.flock(record_lock.fileno(), fcntl.LOCK_EX)
        with (root / ".lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                current = json.loads(claim_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ReviewError("binding_changed_rerun") from exc
            if current.get("record_id") != record_id:
                raise ReviewError("binding_changed_rerun")
            if current.get("state") == "bound":
                record = store.read_record(record_id)
                if audit(record, "recover-binding-bound-noop"):
                    record["updated_at"] = store.utc_now()
                    store.persist(paths, record)
                print(f"recover-binding {worker_id}: bound no-op")
                return
            if not paths.json.exists():
                raise ReviewError("record_not_found")
            record = store.read_record(record_id)
            used = any(
                item.get("worker_dispatch_id") == worker_id
                for field in (
                    "worker_evidence",
                    "blocked_dispatches",
                    "superseded_dispatches",
                )
                for item in record.get(field, [])
            )
            if used:
                current["state"] = "bound"
                current["bound_at"] = store.utc_now()
                _atomic_claim_write(claim_path, current)
                if audit(record, "recover-binding-finalized"):
                    record["updated_at"] = store.utc_now()
                    store.persist(paths, record)
                print(f"recover-binding {worker_id}: finalized")
            else:
                claim_path.unlink()
                dir_fd = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                if audit(record, "recover-binding-orphan-removed"):
                    record["updated_at"] = store.utc_now()
                    store.persist(paths, record)
                print(f"recover-binding {worker_id}: orphan removed")
