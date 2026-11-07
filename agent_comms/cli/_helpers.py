from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path

from .. import paths
from ..actor_config import bootstrap_store, expand_actor_paths, expand_path_value, load_actor_config
from ..protection import protected_actor_notice, protected_override_payload, require_unprotected_or_override
from ..schema import ValidationError

# Domain helpers the CLI commands reach through this module; they live in
# agent_comms.actor_config and agent_comms.protection so no domain module
# imports from the CLI package.
__all__ = [
    "ADMIN_CREDENTIAL_ERROR",
    "ADMIN_TOKEN_PATH",
    "DegradedState",
    "bootstrap_store",
    "expand_actor_paths",
    "expand_path_value",
    "load_actor_config",
    "load_spawn_arg",
    "parse_json_object",
    "print_dispatch_table",
    "print_json",
    "print_message_table",
    "protected_actor_notice",
    "protected_override_payload",
    "refs_from_args",
    "require_admin_credential",
    "require_unprotected_or_override",
]

# None means the runtime root's admin-token, resolved at call time; tests
# patch in a path.
ADMIN_TOKEN_PATH: Path | None = None
ADMIN_CREDENTIAL_ERROR = "admin write paths require an operator credential; not available to workers."


class DegradedState(Exception):
    """A command answered successfully while reporting named defects."""

    def __init__(self, payload=None, *, already_printed: bool = False) -> None:
        super().__init__("degraded state")
        self.payload = payload
        self.already_printed = already_printed


def parse_json_object(value: str) -> dict:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return parsed


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def require_admin_credential() -> str:
    """Verify the operator credential and return the exact verified secret.

    Historically this returned ``None`` and callers used it purely as a gate.
    The T7 emergency-settlement surface additionally needs the verified secret as
    the HMAC key for the sealed settlement plan, so the value is now returned.
    Existing gate-only callers that ignore the return are unaffected. The secret
    is returned ONLY as an in-process HMAC key: it must never be printed,
    persisted, logged, embedded in a plan payload/audit, or surfaced in an error,
    exactly as the settlement engine treats it.
    """
    provided = os.environ.get("AGENT_COMMS_ADMIN_TOKEN", "")
    token_path = ADMIN_TOKEN_PATH or paths.runtime_root() / "admin-token"
    try:
        expected = token_path.read_text().strip()
        mode = os.stat(token_path).st_mode
    except OSError as exc:
        raise ValidationError(ADMIN_CREDENTIAL_ERROR) from exc
    if mode & 0o077:
        raise ValidationError(
            f"{ADMIN_CREDENTIAL_ERROR} {token_path} must be mode 600; "
            f"run chmod 600 {token_path}."
        )
    if not provided or not hmac.compare_digest(provided, expected):
        raise ValidationError(ADMIN_CREDENTIAL_ERROR)
    return expected


def print_message_table(messages: list[dict]) -> None:
    if not messages:
        print("No unread messages.")
        return
    columns = ("to", "priority", "ack", "created", "id", "subject")
    rows = [
        (
            message["to"],
            message["priority"],
            "yes" if message["requires_ack"] else "no",
            message["created_at"],
            message["id"],
            message["subject"],
        )
        for message in messages
    ]
    widths = [
        max(len(columns[index]), *(len(str(row[index])) for row in rows))
        for index in range(len(columns))
    ]
    print("  ".join(columns[index].ljust(widths[index]) for index in range(len(columns))))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(row[index]).ljust(widths[index]) for index in range(len(columns))))


def print_dispatch_table(dispatches: list[dict]) -> None:
    if not dispatches:
        print("No dispatches.")
        return
    # Compact columns lead with the two INDEPENDENT state machines and their
    # normalized joined outcome (transport + outcome) right after status, so the
    # table never infers one machine from the other: a dlq/cancelled row reads
    # ``operator_settled_termination_unconfirmed`` and a cancelled/closed row reads
    # ``confirmed_cancel_transport_closed_first`` rather than a plain dlq/closed.
    # ``result`` is the v2 close_dispatch result (satisfied/blocked, null
    # otherwise); the remaining pre-existing columns follow unchanged.
    columns = (
        "created",
        "status",
        "transport",
        "outcome",
        "result",
        "recipient",
        "producer",
        "override_reason",
        "failure",
        "expected_close_by",
        "dispatch_id",
    )
    rows = [
        (
            dispatch["created_at"],
            dispatch["status"],
            dispatch["transport_status"] or "",
            dispatch["outcome"],
            dispatch["result"] or "",
            dispatch["recipient_actor_id"],
            dispatch["producer_actor_id"],
            dispatch["override_reason"] or "",
            dispatch["failure_reason"] or "",
            dispatch["expected_close_by"] or "",
            dispatch["dispatch_id"],
        )
        for dispatch in dispatches
    ]
    widths = [
        max(len(columns[index]), *(len(str(row[index])) for row in rows))
        for index in range(len(columns))
    ]
    print("  ".join(columns[index].ljust(widths[index]) for index in range(len(columns))))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(row[index]).ljust(widths[index]) for index in range(len(columns))))


def refs_from_args(paths: list[str], summaries: list[str]) -> list[dict[str, str]]:
    refs = []
    for index, path in enumerate(paths):
        summary = summaries[index] if index < len(summaries) else ""
        refs.append({"path": path, "summary": summary})
    return refs


def load_spawn_arg(args: argparse.Namespace) -> dict | None:
    if args.spawn_json is not None:
        spawn = args.spawn_json
    elif args.spawn:
        spawn = json.loads(Path(args.spawn).read_text())
        if not isinstance(spawn, dict):
            raise ValidationError("--spawn file must contain a JSON object")
    else:
        return None
    entry = {"spawn": spawn}
    expand_actor_paths(entry)
    return entry["spawn"]
