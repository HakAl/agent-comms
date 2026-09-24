"""Evidence attachment, gate execution, and clean policy behind the facade.

Owns evidence-payload attachment, gate-epoch and required/latest-run predicates,
the check registry with redacted-environment execution, gate recording, and the
clean policy. Preserves evidence/gate epochs, skips, timeouts, redaction, and the
required/non-passing clean predicates exactly.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_comms.reviewing.contracts import (
    EVIDENCE_ONLY_CHECK_IDS,
    EXECUTABLE_CHECK_IDS,
    ReviewError,
    file_sha256,
    gate_epoch,
    is_evidence_only,
)
from agent_comms.reviewing.briefs import refuse_dod_drift
from agent_comms.reviewing.git_evidence import git_branch, git_head
from agent_comms.reviewing.store import locked_update, require_state, utc_now

DEFAULT_CHECK_TIMEOUT_S = 30
CHECK_DEFAULT_TIMEOUTS_S = {"unittest": 600, "extra": 900}


def command_evidence(args: argparse.Namespace) -> None:
    # Validate caller-controlled inputs before record lookup or lock creation.
    log_path = Path(args.log)
    if not log_path.is_file() or not os.access(log_path, os.R_OK):
        raise ReviewError(f"--log must name an existing readable file: {log_path}")
    runtime_version = args.runtime_version.strip()
    if not runtime_version:
        raise ReviewError("--runtime-version must be non-empty")
    counts = args.counts.strip()
    if not counts:
        raise ReviewError("--counts must be non-empty")
    if re.fullmatch(r"[0-9a-f]{40}", args.head) is None:
        raise ReviewError("--head must be exactly 40 lowercase hexadecimal characters")
    resolved_log = log_path.resolve()

    def update(record: dict[str, Any]) -> None:
        require_state(record, "executed", "execution_reviewed", "review_clean")
        criterion = next(
            (item for item in record["dod"] if item["id"] == args.criterion_id), None
        )
        evidence_ids = [item["id"] for item in record["dod"] if is_evidence_only(item)]
        if criterion is None:
            raise ReviewError(
                f"criterion not found: {args.criterion_id}; evidence-only criterion "
                f"ids: {', '.join(evidence_ids) or '(none)'}"
            )
        if not is_evidence_only(criterion):
            raise ReviewError(
                f"criterion {args.criterion_id} is not evidence-only; executable "
                f"check_ids: {', '.join(sorted(EXECUTABLE_CHECK_IDS))}; evidence-only "
                f"check_ids: {', '.join(sorted(EVIDENCE_ONLY_CHECK_IDS))}"
            )
        reviewed_head = record.get("reviewed_head")
        if args.head != reviewed_head:
            raise ReviewError(
                f"--head {args.head} does not equal reviewed head {reviewed_head}"
            )
        payload = {
            "log_path": str(resolved_log),
            "log_sha256": file_sha256(resolved_log, f"criterion {args.criterion_id}"),
            "runtime_version": runtime_version,
            "counts": counts,
            "head": args.head,
            "attached_at": utc_now(),
            "by": args.by,
        }
        criterion["evidence_payload"] = dict(payload)
        record["history"].append(
            {
                "event": "evidence",
                "criterion_id": args.criterion_id,
                "payload": dict(payload),
            }
        )

    locked_update(args.dispatch_id, update)


def entry_epoch(entry: dict[str, Any]) -> int:
    return int(entry.get("epoch", 0))


def required_checks_satisfied(record: dict[str, Any]) -> bool:
    epoch = gate_epoch(record)
    green = {
        check_id
        for check_id, run in latest_gate_runs(record).items()
        if run.get("verdict") == "pass"
    }
    skipped = {
        skip["check_id"]
        for skip in record.get("skips", [])
        if entry_epoch(skip) == epoch
    }
    for criterion in record["dod"]:
        if (
            criterion.get("required", True)
            and criterion["check_id"] not in green
            and criterion["check_id"] not in skipped
        ):
            return False
    return True


def latest_gate_runs(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Latest run per check within the ACTIVE epoch only. Older-epoch runs
    stay stored append-only but cannot satisfy or block the current epoch."""
    epoch = gate_epoch(record)
    latest = {}
    for run in record["gate_runs"]:
        if entry_epoch(run) == epoch:
            latest[run["check_id"]] = run
    return latest


def redacted_env() -> dict[str, str]:
    keep = {"PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH"}
    return {key: value for key, value in os.environ.items() if key in keep}


def registry_command(name: str, record: dict[str, Any]) -> list[str]:
    if name == "green":
        return [sys.executable, "-c", "raise SystemExit(0)"]
    if name == "fail":
        return [sys.executable, "-c", "raise SystemExit(1)"]
    if name == "unittest":
        return [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    if name == "extra":
        extras = [item for item in record["dod"] if item.get("check_id") == "extra"]
        if len(extras) != 1 or not isinstance(extras[0].get("argv"), list):
            raise ReviewError(
                "extra check must be declared exactly once in DoD with argv"
            )
        return [str(part) for part in extras[0]["argv"]]
    raise ReviewError(f"unknown check: {name}")


def default_timeout_s(name: str) -> int:
    return CHECK_DEFAULT_TIMEOUTS_S.get(name, DEFAULT_CHECK_TIMEOUT_S)


def run_check(
    name: str, record: dict[str, Any], timeout_s: int | None = None
) -> dict[str, Any]:
    repo = Path(record["repo"])
    argv = registry_command(name, record)
    effective_timeout_s = (
        timeout_s if timeout_s is not None else default_timeout_s(name)
    )
    started = utc_now()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(repo),
            env=redacted_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=effective_timeout_s,
            check=False,
        )
        exit_code = proc.returncode
        stdout = proc.stdout[:2000]
        stderr = proc.stderr[:2000]
        verdict = "pass" if exit_code == 0 else "fail"
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        stdout = (exc.stdout or "")[:2000] if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "")[:2000] if isinstance(exc.stderr, str) else ""
        verdict = "timeout"
    return {
        "check_id": name,
        "argv_or_registry_name": name,
        "cwd": str(repo),
        "git_head": git_head(repo) if (repo / ".git").exists() else None,
        "branch": git_branch(repo) if (repo / ".git").exists() else None,
        "env_policy": "redacted",
        "started_at": started,
        "ended_at": utc_now(),
        "timeout_s": effective_timeout_s,
        "exit_code": exit_code,
        "stdout_excerpt": stdout,
        "stderr_excerpt": stderr,
        "verdict": verdict,
    }


def command_gates(args: argparse.Namespace) -> None:
    appended_runs: list[dict[str, Any]] = []

    def update(record: dict[str, Any]) -> None:
        require_state(record, "executed", "execution_reviewed")
        refuse_dod_drift(record)
        epoch = gate_epoch(record)
        names = list(args.check)
        # Validate the complete expansion before the first subprocess or write.
        for name in names:
            registry_command(name, record)
        appended_runs.clear()
        for name in names:
            run = run_check(name, record, args.timeout)
            run["epoch"] = epoch
            record["gate_runs"].append(run)
            appended_runs.append(run)
        for skipped in args.skip or []:
            record.setdefault("skips", []).append(
                {
                    "check_id": skipped,
                    "reason": args.reason,
                    "risk": args.risk,
                    "actor": args.actor,
                    "timestamp": utc_now(),
                    "epoch": epoch,
                }
            )
        record["state"] = "execution_reviewed"
        record["history"].append(
            {"event": "gates", "timestamp": utc_now(), "checks": names}
        )

    locked_update(args.dispatch_id, update)
    failures = [run for run in appended_runs if run["verdict"] != "pass"]
    for run in failures:
        msg = f"{run['check_id']} {run['verdict']} exit_code={run['exit_code']}"
        print(msg, file=sys.stderr)
    if failures:
        raise SystemExit(1)


def command_clean(args: argparse.Namespace) -> None:
    def update(record: dict[str, Any]) -> None:
        require_state(record, "execution_reviewed")
        for finding in record["findings"]:
            if (
                finding["severity"] in {"blocking", "should"}
                and finding["status"] == "open"
            ):
                raise ReviewError("open blocking/should finding prevents clean")
            if finding["severity"] == "nit" and finding["status"] == "open":
                raise ReviewError("open nit must be resolved or deferred before clean")
        if not required_checks_satisfied(record):
            raise ReviewError("required gates are not green or skipped")
        if any(
            run.get("verdict") != "pass" for run in latest_gate_runs(record).values()
        ):
            raise ReviewError("non-passing gate run prevents clean")
        record["state"] = "review_clean"
        record["history"].append({"event": "clean", "timestamp": utc_now()})

    locked_update(args.dispatch_id, update)
