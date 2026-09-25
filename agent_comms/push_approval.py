"""Approval-only push records.

This module is intentionally a thin sibling to review.py: it reuses the
operator key, allowed-signers lookup, and SSHSIG helpers while keeping a
distinct payload version and signature namespace.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from agent_comms import paths
from agent_comms.review import (
    ReviewError,
    approval_key_path,
    atomic_write_json,
    read_tty_confirmation,
    run_git,
    sign_approval_payload,
    utc_now,
    verify_approval_payload_signature,
)

SCHEMA_VERSION = 1
PUSH_APPROVAL_NAMESPACE = "agent-comms-push-approval"
PUSH_APPROVAL_VERSION = "agent-comms-push-approval-v1"
PUSH_APPROVAL_ROOT = paths.push_approval_root()
PUSH_APPROVAL_KIND = "push-approval"
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SIGNED_FIELDS = ("approved_head", "target_ref", "repo_identity", "approver", "approved_at")


def push_approval_payload(
    approved_head: str,
    target_ref: str,
    repo_identity: str,
    approver: str,
    approved_at: str,
) -> bytes:
    fields = {
        "approved_head": approved_head,
        "target_ref": target_ref,
        "repo_identity": repo_identity,
        "approver": approver,
        "approved_at": approved_at,
    }
    for label in _SIGNED_FIELDS:
        value = fields[label]
        if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
            raise ReviewError(f"invalid push approval payload field: {label}")
    if not _FULL_SHA_RE.fullmatch(approved_head):
        raise ReviewError("approved_head must be a full 40-character lowercase hex sha")
    if not target_ref.startswith("refs/"):
        raise ReviewError("target_ref must start with refs/")
    return (
        f"{PUSH_APPROVAL_VERSION}\n"
        f"approved_head={approved_head}\n"
        f"target_ref={target_ref}\n"
        f"repo_identity={repo_identity}\n"
        f"approver={approver}\n"
        f"approved_at={approved_at}\n"
    ).encode("utf-8")


def record_id(approved_at: str, approved_head: str) -> str:
    stamp = approved_at.replace("-", "").replace(":", "").replace("T", "_").replace("Z", "")
    return f"push_{stamp}_{approved_head[:12]}"


def repo_identity_from_args(args: argparse.Namespace) -> str:
    if args.repo_identity:
        return args.repo_identity
    repo = Path(args.repo)
    return run_git(repo, "remote", "get-url", args.remote)


def build_record(args: argparse.Namespace, repo_identity: str, approved_at: str, signature: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": PUSH_APPROVAL_KIND,
        "approved_head": args.head,
        "target_ref": args.target_ref,
        "repo_identity": repo_identity,
        "approver": args.approver,
        "approved_at": approved_at,
        "signature": signature,
        "consensus_refs": list(args.consensus_ref or []),
        "note": args.note or "",
    }


def read_push_record(path: Path) -> dict[str, Any]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReviewError(f"push approval record not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ReviewError(f"push approval record is unreadable: {path}") from exc
    except OSError as exc:
        raise ReviewError(f"push approval record is unreadable: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReviewError(f"push approval record is malformed JSON: {path}") from exc
    if not isinstance(record, dict):
        raise ReviewError("push approval record must be a JSON object")
    return record


def require_push_record_kind(record: dict[str, Any]) -> None:
    if record.get("kind") != PUSH_APPROVAL_KIND:
        raise ReviewError("wrong kind: expected push-approval record")


def record_payload(record: dict[str, Any]) -> bytes:
    for field in _SIGNED_FIELDS:
        if field not in record:
            raise ReviewError(f"push approval record missing signed field: {field}")
    return push_approval_payload(
        record["approved_head"],
        record["target_ref"],
        record["repo_identity"],
        record["approver"],
        record["approved_at"],
    )


def verify_record(record: dict[str, Any], *, head: str, target_ref: str, repo_identity: str) -> None:
    require_push_record_kind(record)
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ReviewError(f"unsupported push approval schema_version {record.get('schema_version')!r}")
    signature = record.get("signature")
    if not isinstance(signature, str) or not signature.strip():
        raise ReviewError("push approval signature is missing or malformed")
    payload = record_payload(record)
    try:
        verify_approval_payload_signature(payload, signature, namespace=PUSH_APPROVAL_NAMESPACE)
    except ReviewError as exc:
        if str(exc) == "approval signature verification failed":
            raise ReviewError("push approval signature verification failed") from exc
        raise
    bindings = {
        "approved_head": head,
        "target_ref": target_ref,
        "repo_identity": repo_identity,
    }
    for field, expected in bindings.items():
        actual = record.get(field)
        if actual != expected:
            raise ReviewError(f"{field} mismatch: record has {actual!r}, expected {expected!r}")


def verify_record_path(record_path: Path, *, head: str, target_ref: str, repo_identity: str) -> None:
    verify_record(read_push_record(record_path), head=head, target_ref=target_ref, repo_identity=repo_identity)


def command_create(args: argparse.Namespace) -> None:
    repo_identity = repo_identity_from_args(args)
    print(f"approved_head={args.head}", file=sys.stderr)
    print(f"target_ref={args.target_ref}", file=sys.stderr)
    print(f"repo_identity={repo_identity}", file=sys.stderr)
    print(f"approver={args.approver}", file=sys.stderr)
    print("approved_at=<stamped at signing>", file=sys.stderr)
    confirmation = read_tty_confirmation(f"Approve push {args.head} to {args.target_ref}? Type APPROVE: ")
    if confirmation != "APPROVE":
        raise ReviewError("approval confirmation failed")
    approved_at = utc_now()
    payload = push_approval_payload(args.head, args.target_ref, repo_identity, args.approver, approved_at)
    signature = sign_approval_payload(payload, approval_key_path(args), namespace=PUSH_APPROVAL_NAMESPACE)
    record = build_record(args, repo_identity, approved_at, signature)
    path = PUSH_APPROVAL_ROOT / f"{record_id(approved_at, args.head)}.json"
    if path.exists():
        raise ReviewError(f"push approval record already exists: {path}")
    atomic_write_json(path, record)
    print(json.dumps(record, indent=2, sort_keys=True))
    print(str(path), file=sys.stderr)


def command_verify(args: argparse.Namespace) -> None:
    verify_record_path(Path(args.record), head=args.head, target_ref=args.target_ref, repo_identity=args.repo_identity)


def command_show(args: argparse.Namespace) -> None:
    record = read_push_record(Path(args.record))
    signed = {field: record.get(field) for field in _SIGNED_FIELDS}
    advisory = {"consensus_refs": record.get("consensus_refs", []), "note": record.get("note", "")}
    print("push approval record inspection only; no verification performed")
    print(json.dumps({"signed": signed, "advisory_unsigned": advisory, "kind": record.get("kind")}, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="push-approval")
    sub = parser.add_subparsers(dest="verb", required=True)

    p = sub.add_parser("create")
    p.add_argument("--head", required=True)
    p.add_argument("--target-ref", required=True)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo")
    source.add_argument("--repo-identity")
    p.add_argument("--remote", default="origin")
    p.add_argument("--approver", required=True)
    p.add_argument("--consensus-ref", action="append")
    p.add_argument("--note")
    p.add_argument("--key")
    p.set_defaults(func=command_create)

    p = sub.add_parser("verify")
    p.add_argument("--record", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--target-ref", required=True)
    p.add_argument("--repo-identity", required=True)
    p.set_defaults(func=command_verify)

    p = sub.add_parser("show")
    p.add_argument("--record", required=True)
    p.set_defaults(func=command_show)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        args.func(args)
        return 0
    except ReviewError as exc:
        print(f"push-approval: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
