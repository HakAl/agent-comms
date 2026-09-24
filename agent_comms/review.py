"""Dispatch-review gate-runner.

Review state is stored in repo-local JSON files.  The CLI also reads the
canonical agent-comms runtime database for dispatch evidence and actor/root
binding; those reads never make runtime-state changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from agent_comms import paths as runtime_paths
from agent_comms.paths import REPO_ROOT
from agent_comms.reviewing.contracts import (
    EVIDENCE_ONLY_CHECK_IDS,
    EXECUTABLE_CHECK_IDS,
    EXECUTION_BOUND_STATES,
    FINDING_SEVERITIES,
    LAND_UNRESOLVABLE_HEAD,
    PRE_APPROVAL_STATES as PRE_APPROVAL_STATES,
    Paths as Paths,
    REVIEW_STATES,
    ReviewError,
    SCHEMA_VERSION as SCHEMA_VERSION,
    STATE_SEMANTICS,
    StateSemantics,
    WORKER_DISPATCH_ID_RE as WORKER_DISPATCH_ID_RE,
    file_sha256,
    gate_epoch,
    is_evidence_only,
    raise_prior_schema_read_only as raise_prior_schema_read_only,
    validate_record,
    validate_schema1_verify_record as validate_schema1_verify_record,
    _validate_intent_key as _validate_intent_key,
)
from agent_comms.reviewing.briefs import (
    brief_sha256,
    canonical_brief_bytes,
    dod_drift,
    load_dod,
)
from agent_comms.reviewing.git_evidence import (
    canonical_diff_digest as canonical_diff_digest,
    canonicalize_patch_bytes,
    commit_in_repo,
    git_bytes as git_bytes,
    git_common_dir as git_common_dir,
    integration_checkout,
    is_ancestor,
    pinned_patch_bytes,
    require_clean_tree,
    require_git_checkout,
    resolve_head,
    run_git,
    stable_patch_id,
)
from agent_comms.reviewing.land_policy import (
    same_git_repository as same_git_repository,
)
from agent_comms.reviewing import store
from agent_comms.reviewing.store import (
    atomic_write_json as atomic_write_json,
    atomic_write_text as atomic_write_text,
    locked_update,
    maybe_revert_for_brief_change as maybe_revert_for_brief_change,
    persist as persist,
    read_record,
    require_state,
    review_paths,
    summary_text,
    utc_now,
)
from agent_comms.reviewing.ledger_evidence import (
    command_recover_binding,
    require_review_repo_worker_root,
    _atomic_claim_write as _atomic_claim_write,
    _binding_claim as _binding_claim,
    _derive_worker_evidence as _derive_worker_evidence,
    _open_ledger_for_reading as _open_ledger_for_reading,
    _resolved_binding_path as _resolved_binding_path,
    _verify_artifact_bindings as _verify_artifact_bindings,
)
from agent_comms.reviewing.execution import (
    command_brief_check,
    command_mark_blocked,
    command_mark_executed,
    command_mark_superseded,
    command_open,
    command_respawn,
    find_finding,
    validate_brief_sections,
    _BRIEF_SECTION_RULES as _BRIEF_SECTION_RULES,
    _PART_HEADING_REGEX as _PART_HEADING_REGEX,
)
from agent_comms.reviewing import intents as review_intents
from agent_comms.reviewing.rounds import command_mark_dispatched, command_redispatch
from agent_comms.reviewing.approval import (
    APPROVAL_INTEGRATION_REF,
    APPROVAL_NAMESPACE,
    CYCLE_PAYLOAD_VERSION,
    approval_key_path,
    approval_payload,
    command_approve,
    command_gate_merge,
    command_verify,
    derive_cycle_destination,
    read_tty_confirmation,
    sign_approval_payload,
    signed_cycle_destination,
    verify_approval_payload_signature,
    verify_approval_signature,
    verify_cycle_merge_authorization,
)
from agent_comms.reviewing.checks import (
    command_clean,
    command_evidence,
    command_gates,
    entry_epoch,
    redacted_env,
    registry_command,
    run_check,
)
from agent_comms.reviewing.rebind import command_rebind, command_rebind_dod

# Vocabularies, constants, and relocated helpers that external callers still read
# as ``review.<name>``; re-export them so the facade surface is stable after the
# behavior-preserving decomposition into the ``reviewing`` package.
_COMPAT_REEXPORTS = (
    StateSemantics,
    STATE_SEMANTICS,
    REVIEW_STATES,
    EXECUTION_BOUND_STATES,
    LAND_UNRESOLVABLE_HEAD,
    EVIDENCE_ONLY_CHECK_IDS,
    EXECUTABLE_CHECK_IDS,
    file_sha256,
    gate_epoch,
    is_evidence_only,
    atomic_write_json,
    atomic_write_text,
    maybe_revert_for_brief_change,
    _atomic_claim_write,
    _binding_claim,
    _derive_worker_evidence,
    _open_ledger_for_reading,
    _resolved_binding_path,
    _verify_artifact_bindings,
    command_recover_binding,
    require_review_repo_worker_root,
    command_open,
    command_brief_check,
    command_mark_dispatched,
    command_mark_executed,
    command_mark_blocked,
    command_mark_superseded,
    command_redispatch,
    command_respawn,
    find_finding,
    validate_brief_sections,
    _BRIEF_SECTION_RULES,
    _PART_HEADING_REGEX,
    canonical_brief_bytes,
    brief_sha256,
    load_dod,
    canonicalize_patch_bytes,
    commit_in_repo,
    integration_checkout,
    is_ancestor,
    pinned_patch_bytes,
    require_clean_tree,
    require_git_checkout,
    resolve_head,
    run_git,
    stable_patch_id,
    APPROVAL_NAMESPACE,
    APPROVAL_INTEGRATION_REF,
    CYCLE_PAYLOAD_VERSION,
    approval_key_path,
    approval_payload,
    derive_cycle_destination,
    read_tty_confirmation,
    sign_approval_payload,
    signed_cycle_destination,
    verify_approval_payload_signature,
    verify_approval_signature,
    verify_cycle_merge_authorization,
    entry_epoch,
    redacted_env,
    registry_command,
    run_check,
    runtime_paths,
    sqlite3,
    subprocess,
    hashlib,
)


def __getattr__(name: str) -> Any:
    # ``REVIEW_ROOT`` now lives on ``reviewing.store``; expose it as a live facade
    # attribute so external callers (and their monkeypatches of the record root)
    # read the single authoritative value without a stale re-export binding.
    if name == "REVIEW_ROOT":
        return store.REVIEW_ROOT
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def command_finding(args: argparse.Namespace) -> None:
    def update(record: dict[str, Any]) -> str:
        require_state(record, "executed", "execution_reviewed")
        finding_id = f"F{len(record['findings']) + 1}"
        record["findings"].append(
            {
                "id": finding_id,
                "severity": args.severity,
                "loc": args.loc,
                "problem": args.problem,
                "impact": args.impact,
                "fix": args.fix,
                "status": "open",
                "resolved_by_dispatch_id": None,
                "resolution_note": None,
            }
        )
        record["state"] = "execution_reviewed"
        record["history"].append({"event": "finding", "timestamp": utc_now(), "finding_id": finding_id})
        return finding_id

    finding_id = locked_update(args.dispatch_id, update)
    print(finding_id)


def command_resolve(args: argparse.Namespace) -> None:
    def update(record: dict[str, Any]) -> None:
        require_state(record, "execution_reviewed")
        finding = find_finding(record, args.finding)
        if finding["severity"] in {"blocking", "should"} and not finding.get("resolved_by_dispatch_id"):
            raise ReviewError("blocking/should findings require resolved_by_dispatch_id before resolve")
        finding["status"] = "resolved"
        finding["resolution_note"] = args.resolution_note
        record["history"].append({"event": "resolve", "timestamp": utc_now(), "finding_id": args.finding})

    locked_update(args.dispatch_id, update)


def command_escalate(args: argparse.Namespace) -> None:
    def update(record: dict[str, Any]) -> None:
        record["prior_state"] = record["state"]
        record["state"] = "escalated"
        record["escalation"] = {"reason": args.reason, "timestamp": utc_now()}
        record["history"].append({"event": "escalate", "timestamp": utc_now()})

    locked_update(args.dispatch_id, update)


def command_unblock(args: argparse.Namespace) -> None:
    def update(record: dict[str, Any]) -> None:
        require_state(record, "escalated")
        if args.action == "raise-cap":
            record["max_respawns"] = max(int(record.get("max_respawns", 3)) + 1, int(record.get("respawn_count", 0)) + 1)
            new_state = record.get("prior_state") or "execution_reviewed"
        elif args.action == "resume":
            new_state = record.get("prior_state") or "drafted_brief"
        else:
            new_state = "drafted_brief"
        record["state"] = new_state
        record["unblock"] = {"by": args.by, "action": args.action, "note": args.note, "timestamp": utc_now()}
        record["history"].append({"event": "unblock", "timestamp": utc_now(), "action": args.action})

    locked_update(args.dispatch_id, update)


def command_defer(args: argparse.Namespace) -> None:
    def update(record: dict[str, Any]) -> None:
        finding = find_finding(record, args.finding)
        if finding["severity"] != "nit":
            raise ReviewError("only nit findings may be deferred")
        finding["status"] = "deferred"
        finding["resolution_note"] = args.note
        record["history"].append({"event": "defer", "timestamp": utc_now(), "finding_id": args.finding})

    locked_update(args.dispatch_id, update)


def command_summary(args: argparse.Namespace) -> None:
    record = read_record(args.dispatch_id)
    if record["schema_version"] == 1:
        print(f"schema_version: 1\nstate: {record['state']}\ndiagnosis: prior_schema_read_only")
    print(summary_text(record), end="")


def command_status(args: argparse.Namespace) -> None:
    actual = REPO_ROOT.resolve()
    expected_value = args.expected_repo_root or os.environ.get("AGENT_COMMS_INSTALL_ROOT")
    expected_source = "arg" if args.expected_repo_root else ("env" if expected_value else None)
    expected = Path(expected_value).expanduser().resolve() if expected_value else None
    binding = {
        "module": __file__,
        "actual_repo_root": str(actual),
        "expected_repo_root": str(expected) if expected else None,
        "expected_source": expected_source,
        "review_root": str(store.REVIEW_ROOT),
        "record_path": None,
        "cwd": os.getcwd(),
        "interpreter": sys.executable,
        "cwd_shadow": False,
    }
    if expected is None:
        print(json.dumps({"binding": binding, "diagnosis": "binding_unverified", "record": None}, sort_keys=True)); raise SystemExit(1)
    if expected != actual:
        print(json.dumps({"binding": binding, "diagnosis": "wrong_store", "record": None}, sort_keys=True)); raise SystemExit(1)
    path = review_paths(args.dispatch_id).json
    binding["record_path"] = str(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(json.dumps({"binding": binding, "diagnosis": "record_not_found", "record": None}, sort_keys=True)); raise SystemExit(1)
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"binding": binding, "diagnosis": "record_malformed", "record": None}, sort_keys=True)); raise SystemExit(1)
    try:
        validate_record(record)
    except ReviewError:
        print(json.dumps({"binding": binding, "diagnosis": "record_invalid", "record": record}, sort_keys=True)); raise SystemExit(1)
    diagnosis = "prior_schema_read_only" if record["schema_version"] == 1 else "ok"
    drift = dod_drift(record)
    dod_binding: str | dict[str, Any] = (
        "unbound" if not record.get("dod_sha256") else (drift or "clean")
    )
    # Derived intent view (dispatch contract 17): state, expiry, exact ledger
    # association, and remedy for the latest recorded round, read strictly
    # read-only; status never mutates SQL or JSON state.
    attempts = record.get("intended_dispatches") or []
    entry = attempts[-1] if attempts else None
    intent_view = (
        review_intents.status_view(
            runtime_paths.db_path(),
            record["expected_producer"],
            entry["idempotency_key"],
        )
        if entry and record["schema_version"] != 1
        else None
    )
    if intent_view is not None and entry is not None:
        # Compare the durable JSON round entry with the SQL row and report the
        # association or the exact mismatch/recovery remedy, read-only. The
        # comparison rebuilds the canonical payload from the JSON companion and
        # requires it to hash to the SQL digest with a matching intent id, so a
        # divergent or partial companion shows record_json_sql_mismatch. A still
        # prepared exact pair is the durable-JSON crash pair: recovery_available.
        if intent_view["state"] == "unavailable":
            intent_view["record_association"] = "ledger_unavailable"
        elif intent_view["state"] == "absent":
            intent_view["record_association"] = "sql_row_absent"
        elif review_intents.entry_matches_row(
            entry,
            {
                "digest": intent_view.get("digest"),
                "intent_id": intent_view.get("intent_id"),
            },
        ):
            intent_view["record_association"] = (
                "recovery_available"
                if intent_view["state"] == "prepared"
                else "matched"
            )
        else:
            intent_view["record_association"] = "record_json_sql_mismatch"
    print(
        json.dumps(
            {
                "binding": binding,
                "diagnosis": diagnosis,
                "dod_binding": dod_binding,
                "intent": intent_view,
                "record": record,
            },
            indent=2,
            sort_keys=True,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="review")
    sub = parser.add_subparsers(dest="verb", required=True)

    p = sub.add_parser("open")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--brief", required=True)
    p.add_argument("--dod")
    p.add_argument("--dod-section")
    p.add_argument("--repo", required=True)
    p.add_argument("--max-respawns", type=int, default=3)
    p.add_argument("--expected-producer", required=True)
    p.add_argument("--expected-recipient", required=True)
    p.set_defaults(func=command_open)

    p = sub.add_parser("brief-check")
    p.add_argument("--dispatch-id", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--clean", action="store_true")
    group.add_argument("--finding")
    p.add_argument("--by", required=True)
    p.add_argument("--surface-verdict", choices=("complete", "incomplete"), required=True)
    p.add_argument("--surface-reason", required=True)
    p.set_defaults(func=command_brief_check)

    for name, func in [("mark-dispatched", command_mark_dispatched), ("mark-executed", command_mark_executed)]:
        p = sub.add_parser(name)
        p.add_argument("--dispatch-id", required=True)
        if name == "mark-dispatched":
            p.add_argument("--idempotency-key", required=True)
        p.set_defaults(func=func)

    for name, func in [("mark-blocked", command_mark_blocked), ("mark-superseded", command_mark_superseded)]:
        p = sub.add_parser(name)
        p.add_argument("--dispatch-id", required=True)
        p.add_argument("--note", default="")
        p.set_defaults(func=func, verb=name)

    p = sub.add_parser("redispatch")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--idempotency-key", required=True)
    p.set_defaults(func=command_redispatch)

    p = sub.add_parser("recover-binding")
    p.add_argument("--worker-dispatch-id", required=True)
    p.add_argument("--quarantine-malformed", action="store_true")
    p.add_argument("--expected-repo-root")
    p.set_defaults(func=command_recover_binding)

    # No --old-head/--new-head/--base/--force: rebind derives all evidence.
    p = sub.add_parser("rebind")
    p.add_argument("--dispatch-id", required=True)
    p.set_defaults(func=command_rebind)

    p = sub.add_parser("rebind-dod")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--dod", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=command_rebind_dod)

    p = sub.add_parser("gates")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--check", nargs="+", required=True)
    p.add_argument("--skip", action="append")
    p.add_argument("--reason")
    p.add_argument("--risk")
    p.add_argument("--actor", default="architect")
    p.add_argument("--timeout", type=int)
    p.set_defaults(func=command_gates)

    p = sub.add_parser("finding")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--severity", choices=sorted(FINDING_SEVERITIES), required=True)
    p.add_argument("--loc", required=True)
    p.add_argument("--problem", required=True)
    p.add_argument("--impact", required=True)
    p.add_argument("--fix", required=True)
    p.set_defaults(func=command_finding)

    p = sub.add_parser("respawn")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument(
        "--finding",
        action="append",
        required=True,
        help="open finding id to bind; repeat --finding to bind multiple findings as one respawn round",
    )
    p.add_argument("--respawn-dispatch-id", required=True)
    p.add_argument("--note", required=True)
    p.set_defaults(func=command_respawn)

    p = sub.add_parser("resolve")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--finding", required=True)
    p.add_argument("--resolution-note", required=True)
    p.set_defaults(func=command_resolve)

    p = sub.add_parser("defer")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--finding", required=True)
    p.add_argument("--note", required=True)
    p.set_defaults(func=command_defer)

    for name, func in [("clean", command_clean), ("summary", command_summary), ("status", command_status), ("gate-merge", command_gate_merge)]:
        p = sub.add_parser(name)
        p.add_argument("--dispatch-id", required=True)
        if name == "status":
            p.add_argument("--expected-repo-root")
        p.set_defaults(func=func)

    p = sub.add_parser("approve")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--approver", default=os.environ.get("USER", "unknown"))
    p.add_argument("--key")
    p.add_argument("--replace-legacy", action="store_true")
    p.set_defaults(func=command_approve)

    p = sub.add_parser("evidence")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--criterion-id", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--runtime-version", required=True)
    p.add_argument("--counts", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--by", default="architect")
    p.set_defaults(func=command_evidence)

    p = sub.add_parser("escalate")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=command_escalate)

    p = sub.add_parser("unblock")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--action", choices=["re-scope", "raise-cap", "resume"], required=True)
    p.add_argument("--note", required=True)
    p.set_defaults(func=command_unblock)

    p = sub.add_parser("verify")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--by", default="architect")
    p.add_argument("--note", default="")
    p.set_defaults(func=command_verify)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        args.func(args)
        return 0
    except ReviewError as exc:
        print(f"review: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
