"""Approval, gate-merge, and verify orchestration behind the review facade.

Owns the approval payload/key/signer/TTY/signature helpers, the evidence-payload
validation performed at approval boundaries, and the approve, gate-merge, and
verify command orchestration. Preserves the approval payload/key/signer/TTY and
signature format, the signature-before-preflight security ordering, and the
gate-merge/verify ancestry behavior.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from agent_comms.paths import REPO_ROOT
from agent_comms.reviewing.contracts import (
    CYCLE_PAYLOAD_VERSION,
    LOCAL_WORKTREE_PREFIX,
    ReviewError,
    file_sha256,
    is_evidence_only,
)
from agent_comms.reviewing.briefs import refuse_dod_drift
from agent_comms.reviewing.git_evidence import (
    commit_in_repo,
    git_head,
    git_proc,
    integration_checkout,
    is_ancestor,
    require_clean_tree,
    require_git_checkout,
    resolve_head,
    run_git,
)
from agent_comms.reviewing.land_policy import landing_preflight
from agent_comms.reviewing.store import locked_update, require_state, utc_now

APPROVAL_NAMESPACE = "agent-comms-approval"
APPROVAL_PRINCIPAL = "agent-comms-approver"
APPROVAL_INTEGRATION_REF = "main"

_LEGACY_REPLACE_REMEDY = "review approve --replace-legacy --dispatch-id"


def _reject_destination_field(context: str, label: str, value: str) -> None:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ReviewError(f"{context}: invalid destination field {label}")


def _git_line(integration: Path, *args: str) -> tuple[int, str, str]:
    # Strip exactly one trailing newline so an interior newline in a path/ref
    # survives for the CR/LF guard to reject, never a broad strip that hides it.
    proc = git_proc(integration, *args)
    out = proc.stdout[:-1] if proc.stdout.endswith("\n") else proc.stdout
    return proc.returncode, out, proc.stderr.strip()


def derive_cycle_destination(context: str = "cycle destination") -> tuple[str, str]:
    # A legal Unix checkout path may contain a newline; it must refuse here, before
    # payload serialization, so it can never become an extra signed-payload line.
    integration = integration_checkout()
    require_git_checkout(context, "integration checkout", integration)
    rc, toplevel, err = _git_line(integration, "rev-parse", "--show-toplevel")
    if rc != 0 or not toplevel:
        raise ReviewError(f"{context}: {integration} has no worktree toplevel: {err}")
    try:
        repo_identity = f"{LOCAL_WORKTREE_PREFIX}{Path(toplevel).resolve()}"
    except OSError as exc:
        raise ReviewError(f"{context}: cannot resolve integration toplevel: {exc}")
    rc, target_ref, err = _git_line(integration, "symbolic-ref", "--quiet", "HEAD")
    if rc != 0 or not target_ref:
        raise ReviewError(
            f"{context}: {integration} is in detached HEAD; a named branch is required"
        )
    _reject_destination_field(context, "repo_identity", repo_identity)
    _reject_destination_field(context, "target_ref", target_ref)
    if not target_ref.startswith("refs/heads/") or not target_ref[len("refs/heads/") :]:
        raise ReviewError(
            f"{context}: integration HEAD {target_ref} is not refs/heads/<name>"
        )
    return repo_identity, target_ref


def approval_payload(
    dispatch_id: str,
    approved_head: str,
    brief_hash: str,
    repo_identity: str,
    target_ref: str,
) -> bytes:
    for label, value in (
        ("dispatch_id", dispatch_id),
        ("approved_head", approved_head),
        ("brief_sha256", brief_hash),
        ("repo_identity", repo_identity),
        ("target_ref", target_ref),
    ):
        if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
            raise ReviewError(f"invalid approval payload field: {label}")
    return (
        f"{CYCLE_PAYLOAD_VERSION}\n"
        f"dispatch_id={dispatch_id}\n"
        f"approved_head={approved_head}\n"
        f"brief_sha256={brief_hash}\n"
        f"repo_identity={repo_identity}\n"
        f"target_ref={target_ref}\n"
    ).encode("utf-8")


def approval_key_path(args: argparse.Namespace) -> Path:
    raw = (
        args.key
        or os.environ.get("AGENT_COMMS_APPROVAL_KEY")
        or "~/.agent-comms/approval-key"
    )
    return Path(raw).expanduser()


def sign_approval_payload(
    payload: bytes, key_path: Path, *, namespace: str = APPROVAL_NAMESPACE
) -> str:
    if not key_path.exists() or not key_path.is_file():
        raise ReviewError(
            f"approval signing key unavailable at {key_path}; "
            "see runbook step 'Approval signing key'"
        )
    with tempfile.TemporaryDirectory(prefix="agent-comms-approval-sign-") as temp_dir:
        payload_path = Path(temp_dir) / "approval-payload"
        sig_path = payload_path.with_suffix(payload_path.suffix + ".sig")
        payload_path.write_bytes(payload)
        proc = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "sign",
                "-f",
                str(key_path),
                "-n",
                namespace,
                str(payload_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0 or not sig_path.exists():
            raise ReviewError(
                "approval signing failed; see runbook step 'Approval signing key'"
            )
        signature = sig_path.read_text(encoding="utf-8")
    if not signature.strip():
        raise ReviewError(
            "approval signing produced an empty signature; "
            "see runbook step 'Approval signing key'"
        )
    return signature


def approval_integration_ref() -> str:
    return (
        os.environ.get("AGENT_COMMS_APPROVAL_INTEGRATION_REF")
        or APPROVAL_INTEGRATION_REF
    )


def committed_approval_signers() -> str:
    ref = approval_integration_ref()
    proc = subprocess.run(
        ["git", "show", f"{ref}:config/approval-signers"],
        cwd=str(REPO_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise ReviewError(
            f"approval signers unavailable at {ref}:config/approval-signers"
        )
    if not proc.stdout.strip():
        raise ReviewError(
            f"approval signers at {ref}:config/approval-signers are empty"
        )
    return proc.stdout


def approval_signature(record: dict[str, Any]) -> str:
    approval = record.get("approval")
    if not isinstance(approval, dict):
        raise ReviewError("approval signature is missing or malformed")
    signature = approval.get("signature")
    if not isinstance(signature, str) or not signature.strip():
        raise ReviewError("approval signature is missing or malformed")
    return signature


def signed_cycle_destination(record: dict[str, Any]) -> tuple[str, str]:
    # These members are signed data: verification reconstructs the payload from them.
    approval = record.get("approval")
    if not isinstance(approval, dict):
        raise ReviewError("approval signature is missing or malformed")
    payload_version = approval.get("payload_version")
    repo_identity = approval.get("repo_identity")
    target_ref = approval.get("target_ref")
    if payload_version is None and repo_identity is None and target_ref is None:
        raise ReviewError(
            "cycle approval predates destination binding (contract 18) and cannot "
            "authorize a merge, land, or verification; re-sign with "
            f"`{_LEGACY_REPLACE_REMEDY} {record.get('dispatch_id')}`"
        )
    if (
        payload_version != CYCLE_PAYLOAD_VERSION
        or not isinstance(repo_identity, str)
        or not repo_identity
        or not isinstance(target_ref, str)
        or not target_ref
    ):
        raise ReviewError("cycle approval destination binding is malformed")
    return repo_identity, target_ref


def verify_approval_signature(record: dict[str, Any]) -> None:
    repo_identity, target_ref = signed_cycle_destination(record)
    payload = approval_payload(
        record["dispatch_id"],
        record["approved_head"],
        record["brief_sha256"],
        repo_identity,
        target_ref,
    )
    signature = approval_signature(record)
    verify_approval_payload_signature(payload, signature)


def verify_cycle_merge_authorization(record: dict[str, Any], context: str) -> None:
    verify_approval_signature(record)
    repo_identity, target_ref = signed_cycle_destination(record)
    current_identity, current_ref = derive_cycle_destination(context)
    if current_identity != repo_identity or current_ref != target_ref:
        raise ReviewError(
            f"{context}: cycle approval destination mismatch; approved "
            f"{repo_identity} on {target_ref}, current {current_identity} on {current_ref}"
        )


def verify_approval_payload_signature(
    payload: bytes, signature: str, *, namespace: str = APPROVAL_NAMESPACE
) -> None:
    signers = committed_approval_signers()
    with tempfile.TemporaryDirectory(prefix="agent-comms-approval-verify-") as temp_dir:
        temp_path = Path(temp_dir)
        signers_path = temp_path / "approval-signers"
        signature_path = temp_path / "approval.sig"
        signers_path.write_text(signers, encoding="utf-8")
        signature_path.write_text(signature, encoding="utf-8")
        proc = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(signers_path),
                "-I",
                APPROVAL_PRINCIPAL,
                "-n",
                namespace,
                "-s",
                str(signature_path),
            ],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    if proc.returncode != 0:
        raise ReviewError("approval signature verification failed")


def validate_evidence_payloads(record: dict[str, Any]) -> None:
    reviewed_head = record.get("reviewed_head")
    for criterion in record["dod"]:
        if not is_evidence_only(criterion):
            continue
        criterion_id = criterion["id"]
        payload = criterion.get("evidence_payload")
        if not isinstance(payload, dict):
            raise ReviewError(
                f"evidence criterion {criterion_id}: missing evidence payload"
            )
        for field in ("log_path", "log_sha256", "runtime_version", "counts", "head"):
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                raise ReviewError(
                    f"evidence criterion {criterion_id}: empty field {field}"
                )
        if payload["head"] != reviewed_head:
            raise ReviewError(
                f"evidence criterion {criterion_id}: payload head {payload['head']} "
                f"does not equal reviewed head {reviewed_head}"
            )
        log_path = Path(payload["log_path"])
        if not log_path.is_file():
            raise ReviewError(
                f"evidence criterion {criterion_id}: evidence log missing {log_path}"
            )
        measured = file_sha256(log_path, f"evidence criterion {criterion_id}")
        if measured != payload["log_sha256"]:
            raise ReviewError(
                f"evidence criterion {criterion_id}: sha256 mismatch; "
                f"expected {payload['log_sha256']}, measured {measured}"
            )


def read_tty_confirmation(prompt: str, tty_path: str = "/dev/tty") -> str:
    try:
        with (
            open(tty_path, "w", encoding="utf-8") as out,
            open(tty_path, "r", encoding="utf-8") as inp,
        ):
            out.write(prompt)
            out.flush()
            return inp.readline().strip()
    except OSError as exc:
        raise ReviewError("approval requires an interactive terminal") from exc


def _sign_cycle_approval(
    args: argparse.Namespace,
    approved_head: str,
    brief_sha256: str,
    dispatch_id: str,
    repo_identity: str,
    target_ref: str,
) -> dict[str, Any]:
    payload = approval_payload(
        dispatch_id, approved_head, brief_sha256, repo_identity, target_ref
    )
    signature = sign_approval_payload(payload, approval_key_path(args))
    return {
        "approver": args.approver,
        "mechanism": "dev-tty-presence+ssh-sig",
        "timestamp": utc_now(),
        "approved_head": approved_head,
        "payload_version": CYCLE_PAYLOAD_VERSION,
        "repo_identity": repo_identity,
        "target_ref": target_ref,
        "signature": signature,
    }


def command_approve(args: argparse.Namespace) -> None:
    if getattr(args, "replace_legacy", False):
        command_approve_replace_legacy(args)
        return

    def update(record: dict[str, Any]) -> None:
        require_state(record, "review_clean")
        # Preflight, evidence, and destination are measured before the human is asked
        # and remeasured after: a head/destination that drifts writes no approval.
        approved_head = record["reviewed_head"]
        approval = _confirm_destination(
            args, record, "approve", approved_head, _source_remeasure(record, "approve")
        )
        record["approved_head"] = approved_head
        record["approval"] = approval
        record["state"] = "human_approved"
        record["history"].append(
            {"event": "approve", "timestamp": utc_now(), "approver": args.approver}
        )

    locked_update(args.dispatch_id, update)


def _source_remeasure(record: dict[str, Any], context: str) -> Any:
    def remeasure() -> tuple[str, str]:
        refuse_dod_drift(record)
        validate_evidence_payloads(record)
        landing_preflight(record)
        approved = git_head(Path(record["repo"]))
        if approved != record.get("reviewed_head"):
            raise ReviewError(
                f"{context}: review worktree HEAD {approved} moved away from "
                f"reviewed_head {record.get('reviewed_head')}"
            )
        return derive_cycle_destination(context)

    return remeasure


def command_gate_merge(args: argparse.Namespace) -> None:
    def update(record: dict[str, Any]) -> None:
        require_state(record, "human_approved")
        repo = Path(record["repo"])
        if run_git(repo, "status", "--porcelain"):
            raise ReviewError("working tree is dirty")
        head = git_head(repo)
        if not (head == record.get("approved_head") == record.get("reviewed_head")):
            raise ReviewError("HEAD, approved_head, and reviewed_head do not match")
        # Predicate order is load-bearing: the signature/destination and security checks
        # above run BEFORE the landing preflight so a forged, retargeted, or
        # destination-drifted record refuses for that reason, not ancestry errors.
        verify_cycle_merge_authorization(record, "gate-merge")
        validate_evidence_payloads(record)
        landing_preflight(record)
        # The gate grants signed merge eligibility; the fast-forward into the
        # integration checkout is the actual integration, observed by verify.
        record["state"] = "merge_eligible"
        record["history"].append(
            {
                "event": "gate-merge",
                "timestamp": utc_now(),
                "head": head,
                "result": "merge_eligible",
            }
        )

    locked_update(args.dispatch_id, update)


def _require_integrated(context: str, integration: Path, approved_head: str) -> str:
    integration_head = resolve_head(context, "integration checkout", integration)
    if not commit_in_repo(integration, approved_head):
        raise ReviewError(
            f"{context}: approved_head {approved_head} is not an object in "
            f"integration checkout {integration}"
        )
    if not is_ancestor(context, integration, approved_head, integration_head):
        raise ReviewError(
            f"{context}: approved_head {approved_head} is not an ancestor of "
            f"integration HEAD {integration_head} in {integration}"
        )
    return integration_head


def command_verify(args: argparse.Namespace) -> None:
    def update(record: dict[str, Any]) -> None:
        # Legacy records written before merge_eligible existed carry "merged".
        require_state(record, "merge_eligible", "merged")
        context = "verify"
        approved_head = record.get("approved_head")
        reviewed_head = record.get("reviewed_head")
        if not approved_head:
            raise ReviewError(f"{context}: approved_head is not recorded")
        if approved_head != reviewed_head:
            raise ReviewError(
                f"{context}: approved_head {approved_head} does not equal "
                f"reviewed_head {reviewed_head}"
            )
        # Contract 18: every v2 approval is reconstructed, signature-verified, and
        # destination-compared before ancestry or state mutation.
        verify_cycle_merge_authorization(record, context)
        integration = integration_checkout()
        require_git_checkout(context, "integration checkout", integration)
        integration_head = _require_integrated(context, integration, approved_head)
        record["history"].append(
            {
                "event": "merge-observed",
                "timestamp": utc_now(),
                "approved_head": approved_head,
                "integration_head": integration_head,
            }
        )
        record["verification"] = {
            "by": args.by,
            "timestamp": utc_now(),
            "note": args.note,
            "approved_head": approved_head,
            "integration_head": integration_head,
        }
        record["state"] = "verified"
        record["history"].append({"event": "verify", "timestamp": utc_now()})

    locked_update(args.dispatch_id, update, terminal_schema1_verify=True)


# Legacy v1 approval replacement (contract 18). Operator-only; never reuses the
# archived v1 signature (a fresh /dev/tty confirmation + SSH signature over the
# current destination) and never changes the record schema version.
_LEGACY_REPLACE_STATES = ("human_approved", "merge_eligible", "merged")


def _archive_legacy_approval(record: dict[str, Any], prior_state: str) -> None:
    record.setdefault("superseded_approvals", []).append(
        {
            "approval": dict(record.get("approval") or {}),
            "prior_state": prior_state,
            "archived_at": utc_now(),
        }
    )


def _require_legacy_cycle_approval(record: dict[str, Any], context: str) -> None:
    approval = record.get("approval")
    if not isinstance(approval, dict):
        raise ReviewError(f"{context}: record carries no approval to replace")
    if (
        approval.get("payload_version") is not None
        or "repo_identity" in approval
        or "target_ref" in approval
    ):
        raise ReviewError(
            f"{context}: approval already carries the v2 destination binding"
        )
    if (
        not isinstance(approval.get("signature"), str)
        or not approval["signature"].strip()
    ):
        raise ReviewError(
            f"{context}: legacy approval signature is missing or malformed"
        )


def _confirm_destination(
    args: argparse.Namespace,
    record: dict[str, Any],
    context: str,
    head: str,
    remeasure: Any,
) -> dict[str, Any]:
    repo_identity, target_ref = remeasure()
    prompt = (
        f"Approve dispatch {record['dispatch_id']} at {head}\n"
        f"  repo_identity: {repo_identity}\n  target_ref:    {target_ref}\n"
        "Type APPROVE: "
    )
    if read_tty_confirmation(prompt) != "APPROVE":
        raise ReviewError(f"{context}: approval confirmation failed")
    current_identity, current_ref = remeasure()
    if current_identity != repo_identity or current_ref != target_ref:
        raise ReviewError(
            f"{context}: integration destination changed while awaiting confirmation"
        )
    return _sign_cycle_approval(
        args,
        head,
        record["brief_sha256"],
        record["dispatch_id"],
        current_identity,
        current_ref,
    )


def _replace_legacy_premerge(
    args: argparse.Namespace, record: dict[str, Any], context: str, prior_state: str
) -> None:
    approved_head = record["reviewed_head"]
    approval = _confirm_destination(
        args, record, context, approved_head, _source_remeasure(record, context)
    )
    _archive_legacy_approval(record, prior_state)
    record["approved_head"] = approved_head
    record["approval"] = approval
    record["state"] = "human_approved"
    record["history"].append(
        {
            "event": "replace-legacy",
            "timestamp": utc_now(),
            "prior_state": prior_state,
            "approver": args.approver,
        }
    )


def _replace_legacy_merged(
    args: argparse.Namespace, record: dict[str, Any], context: str
) -> None:
    approved_head = record.get("approved_head")
    if not approved_head or approved_head != record.get("reviewed_head"):
        raise ReviewError(
            f"{context}: approved_head {approved_head} does not equal reviewed_head "
            f"{record.get('reviewed_head')}"
        )
    if not isinstance(record.get("brief_sha256"), str) or not record["brief_sha256"]:
        raise ReviewError(f"{context}: brief_sha256 is not recorded")

    def remeasure() -> tuple[str, str]:
        integration = integration_checkout()
        require_git_checkout(context, "integration checkout", integration)
        require_clean_tree(context, "integration checkout", integration)
        dest = derive_cycle_destination(context)
        _require_integrated(context, integration, approved_head)
        return dest

    approval = _confirm_destination(args, record, context, approved_head, remeasure)
    _archive_legacy_approval(record, "merged")
    record["approval"] = approval
    record["history"].append(
        {
            "event": "replace-legacy",
            "timestamp": utc_now(),
            "prior_state": "merged",
            "approver": args.approver,
        }
    )


def command_approve_replace_legacy(args: argparse.Namespace) -> None:
    context = "replace-legacy"

    def update(record: dict[str, Any]) -> None:
        state = record.get("state")
        if state not in _LEGACY_REPLACE_STATES:
            raise ReviewError(
                f"{context}: state {state} is not a replaceable legacy state "
                f"({', '.join(_LEGACY_REPLACE_STATES)})"
            )
        _require_legacy_cycle_approval(record, context)
        if state == "merged":
            _replace_legacy_merged(args, record, context)
        else:
            _replace_legacy_premerge(args, record, context, state)

    locked_update(args.dispatch_id, update, terminal_schema1_verify=True)
