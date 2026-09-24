"""Dispatch lifecycle orchestration behind the ``agent_comms.review`` facade.

Owns brief-section validation and the open, brief-check, mark-executed,
mark-blocked/superseded outcome, and respawn orchestration, plus the respawn
finding lookup and the deterministic inline-DoD digest. Preserves open-time
DoD bytes/path/digest binding, commit-first delta verification, correction
epochs, artifact binding, and brief-drift-before-later-refusal transaction
ordering. The mark-dispatched and redispatch rounds live in
``agent_comms.reviewing.rounds`` (dispatch contract 17 intent binding).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from agent_comms import delta_manifest
from agent_comms import paths as runtime_paths
from agent_comms.schema import ValidationError
from agent_comms.reviewing.contracts import (
    SCHEMA_VERSION,
    WORKER_DISPATCH_ID_RE,
    ReviewError,
    gate_epoch,
)
from agent_comms.reviewing.briefs import brief_sha256, canonical_brief_bytes, load_dod
from agent_comms.reviewing.git_evidence import (
    git_branch,
    git_head,
    integration_checkout,
    require_clean_tree,
    require_git_checkout,
    resolve_head,
    run_git,
)
from agent_comms.reviewing.store import (
    locked_update,
    persist,
    require_state,
    review_paths,
    utc_now,
)
from agent_comms.reviewing.ledger_evidence import (
    _binding_claim,
    _derive_worker_evidence,
    _open_ledger_for_reading,
    _resolved_binding_path,
    _verify_artifact_bindings,
    require_review_repo_worker_root,
)


_PART_HEADING_REGEX = re.compile(r"^part \d+")
_PRODUCTION_SURFACE_WORDS = ("production surface",)
_PRODUCTION_SURFACE_ITEMS = (
    "canonical_db",
    "installed_cli",
    "system_interpreter",
    "launchd",
    "seat_config",
    "auth_config",
    "real_runtime",
)
_PRODUCTION_SURFACE_CANDIDATE = re.compile(r"^-\s*touches\s*:")
_PRODUCTION_SURFACE_DECLARATION = re.compile(
    r"^-\s*touches:\s*([^;]+?)\s*;\s*(gated-by|residual|reason):\s*(\S(?:.*\S)?)\s*$"
)

_BRIEF_SECTION_RULES: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    (
        "Scope (any of: surface, deliverable, what changes, ^part \\d+, likely fix, requirements)",
        ("surface", "deliverable", "what changes", "likely fix", "requirements"),
        True,
    ),
    (
        "Anti-scope (any of: anti-claims, out of scope, out-of-scope, constraints, invariants)",
        ("anti-claims", "out of scope", "out-of-scope", "constraints", "invariants"),
        False,
    ),
    (
        "Definition of Done (any of: definition of done, dod, verification)",
        ("definition of done", "dod", "verification"),
        False,
    ),
    ("Process (any of: process)", ("process",), False),
    (
        "Production surface (any of: production surface)",
        _PRODUCTION_SURFACE_WORDS,
        False,
    ),
)


def validate_brief_sections(brief_path: Path) -> None:
    """Refuse briefs missing or violating the five required level-2 sections."""
    text = canonical_brief_bytes(brief_path).decode("utf-8")
    lines = text.split("\n")
    headings: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        if line.startswith("## "):
            raw = line[3:].strip()
            headings.append((index, raw, raw.lower()))
    production_headings = [
        heading
        for heading in headings
        if any(word in heading[2] for word in _PRODUCTION_SURFACE_WORDS)
    ]
    production_indexes = {heading[0] for heading in production_headings}
    missing: list[str] = []
    for label, words, accept_part_regex in _BRIEF_SECTION_RULES:
        candidates = (
            production_headings
            if words == _PRODUCTION_SURFACE_WORDS
            else [
                heading for heading in headings if heading[0] not in production_indexes
            ]
        )
        if accept_part_regex and any(
            _PART_HEADING_REGEX.match(heading[2]) for heading in candidates
        ):
            continue
        if not any(word in heading[2] for heading in candidates for word in words):
            missing.append(label)
    if missing:
        bullets = "; ".join(missing)
        raise ReviewError(
            f"brief {brief_path} is missing required level-2 sections: "
            f"{bullets}. Each required section must appear as a `## ...` "
            f"heading whose text contains at least one of the accepted "
            f"words (case-insensitive substring; the Scope set also "
            f"accepts a `^part \\d+` heading). See "
            f"local/dispatch/briefs/close-message-reply-then-close.md "
            f"and local/dispatch/briefs/d3-extract.md for the canonical "
            f"feature-brief and refactor-brief shapes."
        )
    if len(production_headings) > 1:
        evidence = ", ".join(f"`## {heading[1]}`" for heading in production_headings)
        raise ReviewError(
            f"brief {brief_path} has duplicate Production surface headings: {evidence}"
        )

    heading_index, heading_text, _ = production_headings[0]
    section_end = next(
        (index for index, _raw, _lower in headings if index > heading_index), len(lines)
    )
    candidates = [
        line.strip()
        for line in lines[heading_index + 1 : section_end]
        if _PRODUCTION_SURFACE_CANDIDATE.match(line.strip())
    ]
    if not candidates:
        raise ReviewError(
            f"brief {brief_path} has an empty Production surface section at heading `## {heading_text}`: "
            "at least one declaration is required"
        )

    seen: set[str] = set()
    parsed: list[tuple[str, str]] = []
    vocabulary = ", ".join((*_PRODUCTION_SURFACE_ITEMS, "none"))
    for line in candidates:
        match = _PRODUCTION_SURFACE_DECLARATION.fullmatch(line)
        if not match:
            raise ReviewError(f"malformed Production surface declaration: `{line}`")
        item, clause, _detail = match.groups()
        if item not in {*_PRODUCTION_SURFACE_ITEMS, "none"}:
            raise ReviewError(
                f"unknown Production surface item in `{line}`; expected one of: {vocabulary}"
            )
        if (item == "none" and clause != "reason") or (
            item != "none" and clause == "reason"
        ):
            raise ReviewError(f"invalid Production surface clause in `{line}`")
        if item in seen:
            raise ReviewError(f"duplicate Production surface item in `{line}`")
        seen.add(item)
        parsed.append((item, line))
    if len(parsed) > 1 and any(item == "none" for item, _line in parsed):
        offending = next(line for item, line in parsed if item != "none")
        raise ReviewError(
            f"Production surface `none` must be the only declaration; offending line: `{offending}`"
        )


def inline_dod_sha256(criteria: list[dict[str, Any]]) -> str:
    """Deterministic versioned digest of normalized embedded DoD criteria.

    The immutable digest for an inline (or legacy embedded) DoD source, which
    has no file bytes to hash and can never refresh.
    """
    payload = json.dumps(
        ["review-dod-inline-v1", criteria], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def command_open(args: argparse.Namespace) -> None:
    paths = review_paths(args.dispatch_id)
    brief = Path(args.brief).resolve()
    if not brief.exists():
        raise ReviewError(f"brief not found: {brief}")
    validate_brief_sections(brief)
    repo = _resolved_binding_path(
        args.repo,
        operation="review_open",
        recipient=args.expected_recipient,
        label="review_repo",
        review_repo=args.repo,
    )
    integration = integration_checkout()
    if repo == integration:
        raise ReviewError(
            f"review open: --repo ({repo}) is the integration checkout that cycle-land merges INTO ({integration}). "
            "The review repo must be the isolated worktree the worker commits to (e.g. the dogfood/self-dispatch "
            "worktree), not the merge destination -- pointing them at the same checkout makes the fast-forward a "
            "silent no-op (the 2026-06-13 false-verify footgun)."
        )
    inside_work_tree = run_git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    target_branch = git_branch(repo) if inside_work_tree == "true" else "HEAD"
    if inside_work_tree != "true" or target_branch == "HEAD":
        raise ReviewError(
            "review open: --repo must be a git checkout on a named branch; cycle-land fast-forward-merges that "
            "source branch into the integration checkout"
        )
    require_review_repo_worker_root(
        {"repo": str(repo), "expected_recipient": args.expected_recipient}
    )
    dod_path = Path(args.dod).resolve() if args.dod else None
    dod_bytes = dod_path.read_bytes() if dod_path else None
    dod = load_dod(dod_path, args.dod_section, raw_bytes=dod_bytes)
    open_event: dict[str, Any] = {"event": "open", "timestamp": utc_now()}
    if dod_path is None:
        # Contract 17 draft capture: a file-backed DoD binds its resolved path
        # and byte digest below; an inline DoD has no file, so the immutable
        # source marker and the deterministic digest of the normalized embedded
        # criteria are bound in the append-only open event instead.
        open_event["dod_source"] = "inline"
        open_event["dod_sha256"] = inline_dod_sha256(dod)
    record = {
        "schema_version": SCHEMA_VERSION,
        "dispatch_id": args.dispatch_id,
        "state": "drafted_brief",
        "repo": str(repo),
        "base_commit": git_head(repo) if inside_work_tree == "true" else None,
        "reviewed_head": None,
        "approved_head": None,
        "target_branch": target_branch if inside_work_tree == "true" else None,
        "expected_producer": args.expected_producer,
        "expected_recipient": args.expected_recipient,
        "intended_dispatches": [],
        "worker_evidence": [],
        "blocked_dispatches": [],
        "superseded_dispatches": [],
        "blocked_redispatch_count": 0,
        "max_blocked_redispatches": 3,
        "brief_path": str(brief),
        "brief_sha256": None,
        "brief_revision": 0,
        "brief_checks": [],
        "dod": dod,
        "dod_path": str(dod_path) if dod_path else None,
        "dod_sha256": hashlib.sha256(dod_bytes).hexdigest()
        if dod_bytes is not None
        else None,
        "findings": [],
        "gate_runs": [],
        "gate_epoch": 0,
        "skips": [],
        "approval": None,
        "respawn_count": 0,
        "max_respawns": args.max_respawns,
        "history": [open_event],
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    with paths.lock.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        if paths.json.exists():
            raise ReviewError(f"review already exists: {args.dispatch_id}")
        persist(paths, record)


def command_brief_check(args: argparse.Namespace) -> None:
    if not args.surface_reason or not args.surface_reason.strip():
        raise ReviewError("brief-check: --surface-reason must be non-empty")
    if args.clean and args.surface_verdict == "incomplete":
        raise ReviewError(
            "brief-check: --clean cannot be combined with --surface-verdict incomplete"
        )

    def update(record: dict[str, Any]) -> None:
        require_state(record, "drafted_brief", "brief_revised")
        validate_brief_sections(Path(record["brief_path"]))
        verdict = "clean" if args.clean else "finding"
        record["brief_sha256"] = brief_sha256(Path(record["brief_path"]))
        dod_path = record.get("dod_path")
        if dod_path:
            # The reviewed pair is the canonical brief plus the CURRENT DoD: a
            # changed file-backed DoD is reloaded here and reviewed as part of
            # this check rather than silently keeping the stale criteria.
            try:
                raw = Path(dod_path).read_bytes()
            except OSError as exc:
                raise ReviewError(
                    f"brief-check: cannot read DoD {dod_path}: {exc}"
                ) from exc
            dod_sha = hashlib.sha256(raw).hexdigest()
            if dod_sha != record.get("dod_sha256"):
                record["dod"] = load_dod(Path(dod_path), None, raw_bytes=raw)
                record["dod_sha256"] = dod_sha
        else:
            # Inline and legacy embedded criteria are immutable and cannot
            # refresh; their deterministic digest is the bound pair member.
            dod_sha = inline_dod_sha256(record["dod"])
        record["state"] = "brief_reviewed"
        record["brief_checks"].append(
            {
                "by": args.by,
                "verdict": verdict,
                "finding": args.finding,
                "surface_verdict": args.surface_verdict,
                "surface_reason": args.surface_reason,
                "timestamp": utc_now(),
                "brief_sha256": record["brief_sha256"],
                "dod_sha256": dod_sha,
            }
        )
        record["history"].append(
            {"event": "brief-check", "timestamp": utc_now(), "verdict": verdict}
        )

    locked_update(args.dispatch_id, update)


def command_mark_executed(args: argparse.Namespace) -> None:
    def update(record: dict[str, Any]) -> None:
        require_state(record, "dispatched")
        # Commit-first prevention (not a replacement for Git review): bind
        # reviewed_head only to a committed, clean head on the recorded source
        # branch -- the architect commits the reviewed files BEFORE mark-executed.
        context = "mark-executed"
        repo = Path(record["repo"])
        target_branch = record.get("target_branch")
        if not target_branch or target_branch == "HEAD":
            raise ReviewError(
                f"{context}: recorded target_branch is not a named branch"
            )
        require_git_checkout(context, "review worktree", repo)
        require_clean_tree(context, "review worktree", repo)
        head = resolve_head(context, "review worktree", repo)
        branch = git_branch(repo)
        if branch != target_branch:
            raise ReviewError(
                f"{context}: review worktree {repo} is on {branch}, not the recorded source branch {target_branch}"
            )
        evidence = _derive_worker_evidence(record, "satisfied")
        delta = evidence["closeout"].get("delta")
        if not isinstance(delta, dict):
            raise ReviewError(
                f"deltaless_closeout_for_code_review: ledger_db={evidence['ledger_db']}; worker_dispatch_id={evidence['row']['dispatch_id']}"
            )
        # A correction round is incremental: prior rounds' evidence is append-only,
        # so its delta is rooted at the prior reviewed_head, never the base_commit.
        delta_base = (
            record["reviewed_head"]
            if record.get("worker_evidence")
            else record.get("base_commit")
        )
        if delta.get("base_commit") != delta_base:
            raise ReviewError("delta_base_mismatch")
        head_tree = run_git(repo, "rev-parse", f"{head}^{{tree}}")
        if head_tree != delta.get("snapshot_tree"):
            raise ReviewError(
                f"delta_mismatch: reviewed_head tree {head_tree} != snapshot tree {delta.get('snapshot_tree')}"
            )
        base_tree = run_git(repo, "rev-parse", f"{delta_base}^{{tree}}")

        def derive(*git_args: str, failure: str) -> bytes:
            try:
                return subprocess.run(
                    ["git", *git_args],
                    cwd=repo,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                ).stdout
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ReviewError(failure) from exc

        manifest = derive(
            "diff-tree",
            "-r",
            "--no-renames",
            "--raw",
            "--abbrev=40",
            "-z",
            base_tree,
            head_tree,
            failure="delta_manifest_command_failed",
        )
        name_status = derive(
            "diff-tree",
            "-r",
            "--no-renames",
            "--name-status",
            "-z",
            base_tree,
            head_tree,
            failure="delta_name_status_command_failed",
        )
        if not manifest:
            raise ReviewError("empty_delta")
        manifest_sha256 = hashlib.sha256(manifest).hexdigest()
        try:
            entries, status_counts = delta_manifest.parse_and_crosscheck(
                manifest, name_status
            )
        except ValidationError as exc:
            if str(exc) == "delta_name_status_malformed":
                raise ReviewError("delta_name_status_malformed") from exc
            if str(exc) == "delta_manifest_malformed":
                raise ReviewError("delta_manifest_malformed") from exc
            raise ReviewError("delta_mismatch: derived delta counts disagree") from exc
        if entries != delta.get("entries") or status_counts != delta.get(
            "status_counts"
        ):
            raise ReviewError("delta_mismatch: derived delta counts disagree")
        if manifest_sha256 != delta.get("manifest_sha256"):
            raise ReviewError("delta_mismatch: recomputed manifest differs")
        artifact_bindings = _verify_artifact_bindings(record, evidence, head)
        correction_round = bool(record.get("worker_evidence"))
        with _binding_claim(record, evidence):
            if correction_round:
                old_epoch = gate_epoch(record)
                new_epoch = old_epoch + 1
                record["gate_epoch"] = new_epoch
                record["history"].append(
                    {
                        "event": "correction-gate-epoch",
                        "timestamp": utc_now(),
                        "old_gate_epoch": old_epoch,
                        "new_gate_epoch": new_epoch,
                    }
                )
            record["reviewed_head"] = head
            record["trigger_closed"] = True
            record.setdefault("worker_evidence", []).append(
                {
                    "worker_dispatch_id": evidence["row"]["dispatch_id"],
                    "intent_attempt": evidence["intent"]["attempt"],
                    "idempotency_key": evidence["intent"]["idempotency_key"],
                    "ledger_db": evidence["ledger_db"],
                    "producer": evidence["row"]["producer_actor_id"],
                    "recipient": evidence["row"]["recipient_actor_id"],
                    "status": evidence["row"]["status"],
                    "result": evidence["row"]["result"],
                    "closeout": evidence["closeout"],
                    "delta_verification": {
                        "snapshot_tree": delta["snapshot_tree"],
                        "reviewed_head_tree": head_tree,
                        "manifest_sha256": manifest_sha256,
                        "entries": entries,
                        "status_counts": status_counts,
                    },
                    "artifact_bindings": artifact_bindings,
                    "verified_at": utc_now(),
                }
            )
            record["state"] = "executed"
            record["history"].append(
                {
                    "event": "mark-executed",
                    "timestamp": utc_now(),
                    "worker_dispatch_id": evidence["row"]["dispatch_id"],
                }
            )

    locked_update(args.dispatch_id, update)


def _command_mark_outcome(
    args: argparse.Namespace, *, result: str, state: str, field: str
) -> None:
    def update(record: dict[str, Any]) -> None:
        require_state(record, "dispatched")
        evidence = _derive_worker_evidence(record, result)
        with _binding_claim(record, evidence):
            record.setdefault(field, []).append(
                {
                    "worker_dispatch_id": evidence["row"]["dispatch_id"],
                    "intent_attempt": evidence["intent"]["attempt"],
                    "idempotency_key": evidence["intent"]["idempotency_key"],
                    "ledger_db": evidence["ledger_db"],
                    "closeout": evidence["closeout"],
                    "note": args.note,
                    "verified_at": utc_now(),
                }
            )
            record["state"] = state
            record["history"].append(
                {
                    "event": args.verb,
                    "timestamp": utc_now(),
                    "worker_dispatch_id": evidence["row"]["dispatch_id"],
                }
            )

    locked_update(args.dispatch_id, update)


def command_mark_blocked(args: argparse.Namespace) -> None:
    _command_mark_outcome(
        args, result="blocked", state="dispatch_blocked", field="blocked_dispatches"
    )


def command_mark_superseded(args: argparse.Namespace) -> None:
    _command_mark_outcome(
        args,
        result="satisfied",
        state="dispatch_superseded",
        field="superseded_dispatches",
    )


def find_finding(record: dict[str, Any], finding_id: str) -> dict[str, Any]:
    for finding in record["findings"]:
        if finding["id"] == finding_id:
            return finding
    raise ReviewError(f"finding not found: {finding_id}")


def command_respawn(args: argparse.Namespace) -> None:
    def update(record: dict[str, Any]) -> None:
        require_state(record, "execution_reviewed")
        if record.get("respawn_count", 0) >= record.get("max_respawns", 3):
            record["prior_state"] = "execution_reviewed"
            record["state"] = "escalated"
            record["history"].append({"event": "respawn-cap", "timestamp": utc_now()})
            return
        findings = []
        seen: set[str] = set()
        for finding_id in args.finding:
            if finding_id in seen:
                raise ReviewError(
                    f"finding {finding_id} repeated within one respawn invocation"
                )
            seen.add(finding_id)
            finding = find_finding(record, finding_id)
            if finding["status"] != "open":
                raise ReviewError(
                    f"finding {finding_id} is not open (status {finding['status']}); only open findings may be respawned"
                )
            findings.append(finding)
        # The linked correction dispatch is canonical ledger data, never caller-
        # asserted: derive producer, recipient, and idempotency key from the ledger
        # row and fail closed before any mutation, then record it as the active
        # correction attempt so mark-executed binds THIS dispatch, not prior evidence.
        worker_id = args.respawn_dispatch_id
        if not WORKER_DISPATCH_ID_RE.fullmatch(worker_id):
            raise ReviewError(f"invalid_worker_dispatch_id: {worker_id!r}")
        db_path = runtime_paths.db_path().resolve()
        conn = _open_ledger_for_reading(
            db_path,
            lambda exc: ReviewError(
                f"ledger_open_failed: {exc}; ledger_db={db_path}; worker_dispatch_id={worker_id}; "
                f"WAL open requires write access to the ledger directory for sidecar creation"
            ),
        )
        try:
            row = conn.execute(
                "select producer_actor_id, recipient_actor_id, idempotency_key "
                "from dispatch_ledger where dispatch_id=?",
                (worker_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ReviewError(
                f"respawn_dispatch_not_found: ledger_db={db_path}; worker_dispatch_id={worker_id}"
            )
        if (
            row["producer_actor_id"] != record["expected_producer"]
            or row["recipient_actor_id"] != record["expected_recipient"]
        ):
            raise ReviewError(
                f"respawn_dispatch_actor_mismatch: ledger actors differ; ledger_db={db_path}; worker_dispatch_id={worker_id}"
            )
        key = row["idempotency_key"]
        if not isinstance(key, str) or not key.strip() or len(key.strip()) > 200:
            raise ReviewError(
                f"respawn_dispatch_malformed: ledger idempotency_key invalid; ledger_db={db_path}; worker_dispatch_id={worker_id}"
            )
        key = key.strip()
        if key in {
            item["idempotency_key"] for item in record.get("intended_dispatches", [])
        }:
            raise ReviewError(
                f"respawn_intent_reused: idempotency key {key!r} already recorded; worker_dispatch_id={worker_id}"
            )
        for finding in findings:
            finding["resolved_by_dispatch_id"] = worker_id
            finding["respawn_note"] = args.note
        record["respawn_count"] = int(record.get("respawn_count", 0)) + 1
        intents = record.setdefault("intended_dispatches", [])
        intents.append(
            {
                "attempt": len(intents) + 1,
                "idempotency_key": key,
                "note": args.note,
                "recorded_at": utc_now(),
                "respawn_dispatch_id": worker_id,
            }
        )
        record["state"] = "dispatched"
        record["history"].append(
            {
                "event": "respawn",
                "timestamp": utc_now(),
                "finding_ids": list(args.finding),
                "worker_dispatch_id": worker_id,
            }
        )

    locked_update(
        args.dispatch_id, update, precondition=require_review_repo_worker_root
    )
