"""Review record persistence behind the ``agent_comms.review`` facade.

Owns the repo-local review paths, the UTC timestamp, the atomic JSON/text
writes, the record read, the summary serialization, the persistence step, the
brief-drift revert transaction, the record lock, and the state guard. It
depends only on the standard library, ``agent_comms.paths``, and the
``agent_comms.reviewing`` contracts and brief policy leaves.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from agent_comms import paths
from agent_comms.reviewing.contracts import (
    PRE_APPROVAL_STATES,
    Paths,
    ReviewError,
    raise_prior_schema_read_only,
    validate_record,
    validate_schema1_verify_record,
)
from agent_comms.reviewing.briefs import brief_sha256, dod_drift


REVIEW_ROOT = paths.review_root()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def review_paths(dispatch_id: str) -> Paths:
    if not dispatch_id or "/" in dispatch_id or dispatch_id in {".", ".."}:
        raise ReviewError("invalid dispatch_id")
    root = REVIEW_ROOT
    return Paths(
        root / f"{dispatch_id}.json",
        root / f"{dispatch_id}.lock",
        root / f"{dispatch_id}.summary.md",
    )


def atomic_write_json(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def read_record(dispatch_id: str) -> dict[str, Any]:
    paths = review_paths(dispatch_id)
    try:
        record = json.loads(paths.json.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReviewError(f"review record not found: {dispatch_id}") from exc
    validate_record(record)
    return record


def summary_text(record: dict[str, Any]) -> str:
    lines = [
        f"# Dispatch Review {record['dispatch_id']}",
        "",
        f"- State: {record['state']}",
        f"- Repo: {record['repo']}",
        f"- Brief: {record['brief_path']}",
        f"- Brief SHA256: {record.get('brief_sha256') or ''}",
        f"- Reviewed HEAD: {record.get('reviewed_head') or ''}",
        f"- Approved HEAD: {record.get('approved_head') or ''}",
        f"- Respawns: {record.get('respawn_count', 0)}/{record.get('max_respawns', 3)}",
        "",
        "## Brief Checks",
    ]
    for check in record.get("brief_checks", []):
        lines.append(
            f"- {check.get('timestamp')}: {check.get('by')} {check.get('verdict')}"
        )
    lines.extend(["", "## Findings"])
    if record.get("findings"):
        for finding in record["findings"]:
            lines.append(
                f"- {finding['id']} [{finding['severity']}/{finding['status']}]: "
                f"{finding['problem']} -> {finding.get('resolved_by_dispatch_id') or ''}"
            )
    else:
        lines.append("- None")
    lines.extend(["", "## Gate Runs"])
    if record.get("gate_runs"):
        for run in record["gate_runs"]:
            lines.append(
                f"- {run['check_id']}: {run['verdict']} exit={run.get('exit_code')}"
            )
    else:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def persist(paths: Paths, record: dict[str, Any]) -> None:
    validate_record(record)
    atomic_write_json(paths.json, record)
    atomic_write_text(paths.summary, summary_text(record))


def maybe_revert_for_brief_change(record: dict[str, Any]) -> bool:
    if record.get("state") not in PRE_APPROVAL_STATES or not record.get("brief_sha256"):
        return False
    try:
        current = brief_sha256(Path(record["brief_path"]))
    except FileNotFoundError as exc:
        raise ReviewError(f"brief not found: {record['brief_path']}") from exc
    # The reviewed pair is brief plus DoD: later drift of either moves every
    # pre-approval state to brief_revised. Only a file-backed DoD (bound
    # dod_path + dod_sha256) can drift; an inline or legacy embedded DoD has no
    # file to change and cannot refresh.
    if current != record["brief_sha256"] or dod_drift(record) is not None:
        record["state_before_brief_revised"] = record["state"]
        record["state"] = "brief_revised"
        record["brief_revision"] = int(record.get("brief_revision", 0)) + 1
        return True
    return False


def locked_update(
    dispatch_id: str,
    fn: Callable[..., Any],
    *,
    precondition: Callable[[dict[str, Any]], None] | None = None,
    terminal_schema1_verify: bool = False,
    pass_paths: bool = False,
) -> Any:
    # With ``pass_paths`` the callback receives ``(record, paths)`` and owns
    # persistence itself: the contract-17 dispatch round must order persist ->
    # durable re-read -> SQL CAS inside this one record lock. The brief/DoD
    # drift revert is still persisted here when the callback refuses.
    paths = review_paths(dispatch_id)
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    with paths.lock.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        record = read_record(dispatch_id)
        if record["schema_version"] == 1:
            if not terminal_schema1_verify:
                raise_prior_schema_read_only(record)
            validate_schema1_verify_record(record)
        if precondition is not None:
            precondition(record)
        reverted = (
            False
            if terminal_schema1_verify and record["schema_version"] == 1
            else maybe_revert_for_brief_change(record)
        )
        try:
            result = fn(record, paths) if pass_paths else fn(record)
        except Exception:
            if reverted:
                record["updated_at"] = utc_now()
                persist(paths, record)
            raise
        if not pass_paths:
            record["updated_at"] = utc_now()
            persist(paths, record)
        return result


def require_state(record: dict[str, Any], *states: str) -> None:
    if record["state"] not in states:
        raise ReviewError(
            f"state {record['state']} not allowed; expected {', '.join(states)}"
        )
