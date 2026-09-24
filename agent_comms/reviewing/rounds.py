"""Review-record lock and dispatch-round orchestration (dispatch contract 17).

Owns mark-dispatched for the implicit initial ``implementation`` round -- the
derived clean-baseline identity, the canonical intent payload, and the
mandatory crash-ordered sequence (SQL prepare or exact match, the tri-state
durable probe, durable JSON ``dispatched`` persistence when definitely absent,
the durable re-read, then the prepared -> active CAS) under one review-record
lock -- plus redispatch, which reconciles active-intent expiry before recording
a fresh attempt. Record persistence stays in ``store``, Git observation in
``git_evidence``, ledger reads in ``ledger_evidence``, and every SQL intent
operation in ``intents``."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from agent_comms import paths as runtime_paths
from agent_comms.policies import WORKER_DISPATCH_POLICY, WORKER_DISPATCH_POLICY_VERSION
from agent_comms.schema import ValidationError
from agent_comms.reviewing import intents
from agent_comms.reviewing.briefs import brief_sha256
from agent_comms.reviewing.contracts import ReviewError, _validate_intent_key
from agent_comms.reviewing.git_evidence import (
    git_branch,
    git_bytes,
    integration_checkout,
    is_ancestor,
    require_git_checkout,
    resolve_head,
    run_git,
)
from agent_comms.reviewing.ledger_evidence import (
    _open_ledger_for_reading,
    require_review_repo_worker_root,
)
from agent_comms.reviewing.store import (
    locked_update,
    persist,
    require_state,
    utc_now,
)


def _intent_op(fn: Callable[..., Any], *op_args: Any) -> Any:
    """Run one SQL intent operation, surfacing refusals as review errors."""
    try:
        return fn(*op_args)
    except (ValidationError, sqlite3.Error) as exc:
        raise ReviewError(str(exc)) from exc


PROBE_EXACT = "exact"
PROBE_ABSENT = "absent"
PROBE_UNKNOWN = "unknown"


def _probe_durable_round(paths: Any, row: dict[str, Any]) -> str:
    """Tri-state read-only probe of the durable record for one intent row.

    Re-reads the persisted record bytes (never an in-memory copy) and
    classifies this row's durable round entry: ``exact`` when the latest
    entry for the row's idempotency key rebuilds the row's canonical
    identity on a ``dispatched`` record, definite ``absent`` when the record
    read cleanly and carries no entry for the key, or ``unknown`` when the
    bytes could not be read or parsed. A present entry that does not rebuild
    the row's identity is a hard conflict raised here, never classified as
    absent, so recovery can never overwrite a mismatched durable round.
    """
    try:
        durable = json.loads(paths.json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return PROBE_UNKNOWN
    entries = [
        entry
        for entry in durable.get("intended_dispatches") or []
        if entry.get("idempotency_key") == row["idempotency_key"]
    ]
    if not entries:
        return PROBE_ABSENT
    if durable.get("state") == "dispatched" and intents.entry_matches_row(
        entries[-1], row
    ):
        return PROBE_EXACT
    raise ReviewError(
        "mark_dispatched_durable_conflict: the durable record already binds a "
        f"different round for key={row['idempotency_key']!r}; a mismatched "
        "durable entry is never treated as absent; remedy: dispatch with a "
        "fresh idempotency key"
    )


def _require_clean_baseline(context: str, label: str, repo: Path) -> None:
    """Refuse a dirty checkout, reporting the exact staged, tracked (unstaged),
    and non-ignored untracked paths; no cleanup, and ignored paths never block.

    Reads a pinned NUL-delimited status stream that requests all non-ignored
    untracked files (``-uall`` overrides any ``status.showUntrackedFiles``
    config and lists nested untracked paths) and reports exact path bytes,
    never depending on porcelain text, path quoting, or ambient config. Renames
    are disabled so every record is a single ``XY <path>`` entry.
    """
    stream = git_bytes(
        context,
        repo,
        "-c",
        "core.quotePath=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=no",
        "--no-renames",
    )
    staged: list[str] = []
    tracked: list[str] = []
    untracked: list[str] = []
    for record in stream.split(b"\x00"):
        if not record:
            continue
        code = record[:2]
        path = record[3:].decode("utf-8", "surrogateescape")
        if code == b"??":
            untracked.append(path)
            continue
        if code[:1] not in (b" ", b"?"):
            staged.append(path)
        if code[1:2] not in (b" ", b"?"):
            tracked.append(path)
    if staged or tracked or untracked:
        raise ReviewError(
            f"{context}: {label} {repo} is not a clean baseline; "
            f"staged={sorted(staged)}; tracked={sorted(tracked)}; "
            f"untracked={sorted(untracked)}; no cleanup is performed and ignored paths are allowed"
        )


def _resolve_tree(context: str, label: str, repo: Path, commit: str) -> str:
    tree = run_git(repo, "rev-parse", "--verify", f"{commit}^{{tree}}", check=False)
    if not tree:
        raise ReviewError(
            f"{context}: {label} {repo} tree for {commit} is unresolvable"
        )
    return tree


def _derive_intent_payload(record: dict[str, Any], key: str) -> dict[str, Any]:
    """Derive (never accept) every bound fact of the initial implementation round.

    The clean source/integration common HEAD is the round base: the draft
    ``base_commit`` captured at ``open`` is REBOUND to it rather than refused
    when integration and source advanced together, and the canonical payload
    binds the source HEAD/tree, integration HEAD/tree, and round base
    commit/tree (equal for the initial round) alongside the branch, brief/DoD,
    actors, key, root, policy, and round.
    """
    context = "mark-dispatched"
    repo = Path(record["repo"]).resolve()
    require_git_checkout(context, "review worktree", repo)
    branch = git_branch(repo)
    target_branch = record.get("target_branch")
    if not target_branch or target_branch == "HEAD" or branch != target_branch:
        raise ReviewError(
            f"{context}: review worktree {repo} is on {branch}, not the recorded "
            f"named source branch {target_branch}"
        )
    _require_clean_baseline(context, "review worktree", repo)
    source_head = resolve_head(context, "review worktree", repo)
    source_tree = _resolve_tree(context, "review worktree", repo, source_head)
    integration = integration_checkout()
    require_git_checkout(context, "integration checkout", integration)
    _require_clean_baseline(context, "integration checkout", integration)
    integration_head = resolve_head(context, "integration checkout", integration)
    integration_tree = _resolve_tree(
        context, "integration checkout", integration, integration_head
    )
    if not is_ancestor(context, repo, integration_head, source_head):
        raise ReviewError(
            f"{context}: integration HEAD {integration_head} is not an ancestor of "
            f"source HEAD {source_head}; rebase the review worktree onto it"
        )
    if source_head != integration_head:
        raise ReviewError(
            f"{context}: source HEAD {source_head} on branch {branch} does not "
            f"equal integration HEAD {integration_head} in {integration}; the "
            "first implementation source HEAD must equal the integration HEAD; "
            "remedy: rebase or reset the review worktree onto the integration head, or re-open the record from it"
        )
    # Rebind the draft base to the derived clean common HEAD: the first active
    # intent must not refuse merely because integration and source advanced
    # together after open. For the initial implementation round the base commit
    # and tree equal the source (= integration) HEAD and tree.
    record["base_commit"] = source_head
    brief_digest = brief_sha256(Path(record["brief_path"]))
    checks = record.get("brief_checks") or []
    pair = checks[-1] if checks else {}
    if (
        not record.get("brief_sha256")
        or brief_digest != record["brief_sha256"]
        or pair.get("brief_sha256") != brief_digest
        or not pair.get("dod_sha256")
    ):
        raise ReviewError(
            f"{context}: the reviewed brief/DoD pair is not bound; rerun review "
            "brief-check to bind the canonical brief and current DoD digests"
        )
    return intents.canonical_payload(
        producer_actor_id=record["expected_producer"],
        idempotency_key=key,
        recipient_actor_id=record["expected_recipient"],
        real_project_root=str(repo),
        policy_name=WORKER_DISPATCH_POLICY,
        policy_version=WORKER_DISPATCH_POLICY_VERSION,
        round_kind=intents.ROUND_KIND_IMPLEMENTATION,
        record_id=record["dispatch_id"],
        brief_sha256=brief_digest,
        dod_sha256=pair["dod_sha256"],
        source_branch=branch,
        source_head=source_head,
        source_tree=source_tree,
        integration_head=integration_head,
        integration_tree=integration_tree,
        base_commit=source_head,
        base_tree=source_tree,
    )


def command_mark_dispatched(args: argparse.Namespace) -> None:
    key = _validate_intent_key(args.idempotency_key)

    def run(record: dict[str, Any], paths: Any) -> None:
        require_state(record, "brief_reviewed", "dispatched")
        if any(
            not criterion.get("check_id")
            for criterion in record["dod"]
            if criterion.get("required", True)
        ):
            raise ReviewError("required DoD criteria must declare executable check_id")
        payload = _derive_intent_payload(record, key)
        digest = intents.payload_digest(payload)
        recovery = record["state"] == "dispatched"
        if recovery:
            # The durable JSON already names a round: recover only the exact
            # recorded identity (re-prepare or reactivate, then CAS).
            last = (record.get("intended_dispatches") or [{}])[-1]
            if (
                last.get("idempotency_key") != key
                or last.get("intent_digest") != digest
            ):
                raise ReviewError(
                    "mark_dispatched_recovery_mismatch: the dispatched record "
                    f"binds key={last.get('idempotency_key')!r} digest="
                    f"{last.get('intent_digest')!r}; recover with the recorded "
                    "key from the recorded baseline, or use redispatch"
                )
        db_path = runtime_paths.db_path().resolve()
        _intent_op(intents.reconcile_in_ledger, db_path)
        row = _intent_op(intents.prepare_in_ledger, db_path, payload)
        # Tri-state durable evidence gates activation: exact resumes, definite
        # absence performs (or repeats) the atomic JSON write and re-reads, and
        # unknown or mismatched durable state fails closed without mutation.
        outcome = _probe_durable_round(paths, row)
        if outcome == PROBE_UNKNOWN:
            raise ReviewError(
                "mark_dispatched_durable_unknown: the persisted record bytes "
                "could not be read or parsed, so the durable round state is "
                "unproven; nothing was mutated; remedy: restore the record "
                "file, then rerun mark-dispatched with the same key"
            )
        if outcome == PROBE_ABSENT:
            if not recovery:
                attempts = record.setdefault("intended_dispatches", [])
                attempts.append(
                    {
                        "attempt": len(attempts) + 1,
                        "idempotency_key": key,
                        "recorded_at": utc_now(),
                        # The round-bound companion carries the complete
                        # canonical identity (contract 17), so the durable JSON
                        # entry and the SQL row are provably the same request.
                        **intents.companion_entry_fields(
                            payload, row["intent_id"], digest
                        ),
                    }
                )
                record["state"] = "dispatched"
                record["history"].append(
                    {"event": "mark-dispatched", "timestamp": utc_now()}
                )
            record["updated_at"] = utc_now()
            persist(paths, record)
            outcome = _probe_durable_round(paths, row)
        if outcome != PROBE_EXACT:
            raise ReviewError(
                "mark_dispatched_durability_verify_failed: the re-read record "
                "does not carry this round's exact intent identity; rerun "
                "mark-dispatched"
            )
        # The durable JSON round entry now IS the crash-pair evidence: a crash
        # between here and activation leaves a non-expiring prepared row whose
        # exact companion this probe re-proves on retry, so recovery activates
        # the same row in place without incrementing the attempt.
        _intent_op(
            intents.activate_in_ledger,
            db_path,
            payload["producer_actor_id"],
            key,
            digest,
        )

    locked_update(
        args.dispatch_id,
        run,
        precondition=require_review_repo_worker_root,
        pass_paths=True,
    )


def command_redispatch(args: argparse.Namespace) -> None:
    def update(record: dict[str, Any]) -> None:
        db_path = runtime_paths.db_path().resolve()
        _intent_op(intents.reconcile_in_ledger, db_path)
        if record["state"] == "dispatched":
            attempts = record.get("intended_dispatches") or []
            if not attempts:
                raise ReviewError("intended_dispatch_unrecorded")
            intent = attempts[-1]
            conn = _open_ledger_for_reading(
                db_path,
                lambda exc: ReviewError(
                    f"ledger_open_failed: {exc}; ledger_db={db_path}; "
                    f"WAL open requires write access to the ledger directory for sidecar creation"
                ),
            )
            try:
                row = conn.execute(
                    "select status, result from dispatch_ledger "
                    "where producer_actor_id=? and idempotency_key=?",
                    (record["expected_producer"], intent["idempotency_key"]),
                ).fetchone()
            except sqlite3.Error as exc:
                raise ReviewError(
                    f"ledger_open_failed: {exc}; ledger_db={db_path}"
                ) from exc
            finally:
                conn.close()
            if row is None:
                raise ReviewError(
                    f"active_dispatch_absent: retry dispatch_agent with active key "
                    f"{intent['idempotency_key']!r}; ledger_db={db_path}"
                )
            status, result = row["status"], row["result"]
            if status in {"queued", "in_flight"}:
                raise ReviewError(
                    f"active_dispatch_live: status={status}; ledger_db={db_path}"
                )
            if status == "closed" and result == "satisfied":
                raise ReviewError(
                    f"active_dispatch_satisfied: use mark-executed or mark-superseded; ledger_db={db_path}"
                )
            if status == "closed" and result == "blocked":
                raise ReviewError(
                    f"active_dispatch_blocked: use mark-blocked; ledger_db={db_path}"
                )
            if status not in {"dlq", "spawn_failed_message_landed", "rejected"}:
                raise ReviewError(
                    f"active_dispatch_live: status={status}; ledger_db={db_path}"
                )
        else:
            require_state(record, "dispatch_blocked", "dispatch_superseded")
        key = _validate_intent_key(args.idempotency_key)
        if key in {
            item["idempotency_key"] for item in record.get("intended_dispatches", [])
        }:
            raise ReviewError("intent_key_reused")
        count = int(record.get("blocked_redispatch_count", 0))
        if count >= int(record.get("max_blocked_redispatches", 3)):
            record["state"] = "escalated"
            record["history"].append(
                {"event": "redispatch-cap", "timestamp": utc_now()}
            )
            return
        record["blocked_redispatch_count"] = count + 1
        record.setdefault("intended_dispatches", []).append(
            {
                "attempt": len(record.get("intended_dispatches", [])) + 1,
                "idempotency_key": key,
                "note": args.note,
                "recorded_at": utc_now(),
            }
        )
        record["state"] = "dispatched"
        record["history"].append({"event": "redispatch", "timestamp": utc_now()})

    locked_update(
        args.dispatch_id, update, precondition=require_review_repo_worker_root
    )
