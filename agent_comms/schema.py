from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRIORITIES = {"low", "normal", "high", "blocker"}
KINDS = {"agent", "human", "system"}
CANONICAL_AGENT_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_CANONICAL_AGENT_ID_LENGTH = 64
RESERVED_PATH_SEGMENTS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
# "closed" is a recipient-side terminal state. It is intentionally separate from
# "requires_ack"; prompts enforce when an ack is socially required. Stage-2
# dead-worker terminalization adds "cancelled": a withdrawn delivery obligation.
# Like "closed" it is terminal for default (include_closed=False) listing and can
# never be resurrected by read/ack/close; a recipient copy only becomes cancelled
# in the same transaction that commits a confirmed dispatch cancellation or the
# explicit admin unconfirmed settlement.
STATUSES = {"sent", "read", "acknowledged", "closed", "cancelled"}


class ValidationError(ValueError):
    """Raised when caller input does not match the relay contract."""


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    team: str
    role: str
    project_root: Path
    capabilities: tuple[str, ...]


def require_non_empty(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValidationError(f"{field} must not be empty")
    return value


def _validate_canonical_agent_id(value: str, field: str) -> str:
    """Validate canonical agent ids safe for paths and git refs.

    The accepted form is lowercase ASCII alphanumeric segments separated by
    single hyphens. With reserved device names excluded, that form is
    filesystem-safe, Windows-safe, git-ref-safe, case-fold-stable, and
    NFC-stable without normalization.
    """
    if not value:
        raise ValidationError(f"{field} must not be empty")
    if len(value) > MAX_CANONICAL_AGENT_ID_LENGTH:
        raise ValidationError(
            f"{field} must be at most {MAX_CANONICAL_AGENT_ID_LENGTH} characters"
        )
    if not CANONICAL_AGENT_ID_RE.fullmatch(value):
        raise ValidationError(
            f"{field} must match canonical agent id form: "
            "lowercase ASCII alphanumeric segments separated by single hyphens"
        )
    if value in RESERVED_PATH_SEGMENTS:
        raise ValidationError(f"{field} must not be a reserved path segment")
    return value


def identity_to_path_segment(identity: str) -> str:
    """Return a canonical agent identity unchanged for path/ref derivation."""
    return _validate_canonical_agent_id(identity, "identity")


def validate_priority(priority: str) -> str:
    priority = require_non_empty(priority, "priority")
    if priority not in PRIORITIES:
        allowed = ", ".join(sorted(PRIORITIES))
        raise ValidationError(f"priority must be one of: {allowed}")
    return priority


def validate_kind(kind: str) -> str:
    kind = require_non_empty(kind, "kind")
    if kind not in KINDS:
        allowed = ", ".join(sorted(KINDS))
        raise ValidationError(f"kind must be one of: {allowed}")
    return kind


def validate_actor_id_for_kind(actor_id: str, kind: str) -> str:
    kind = validate_kind(kind)
    if kind == "agent":
        return _validate_canonical_agent_id(actor_id, "actor_id")
    actor_id = require_non_empty(actor_id, "actor_id")
    if kind in {"human", "system"}:
        lowered = actor_id.lower()
        if (
            lowered.startswith(f"{kind}:")
            or lowered.startswith(f"{kind}-")
            or lowered == kind
        ):
            raise ValidationError(
                f"{kind} actor ids must be opaque and must not encode kind"
            )
    return actor_id


def validate_refs(
    refs: list[dict[str, Any]], project_roots: list[Path]
) -> list[dict[str, str]]:
    validated: list[dict[str, str]] = []
    roots = []
    for root in project_roots:
        try:
            roots.append(root.resolve())
        except (ValueError, RuntimeError, OSError) as exc:
            raise ValidationError(
                f"configured project root cannot be resolved: {root}; inline the content "
                "in the message body, or ask the operator to repair the registered project root"
            ) from exc

    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            raise ValidationError(f"refs[{index}] must be an object")
        path_value = require_non_empty(str(ref.get("path", "")), f"refs[{index}].path")
        summary = str(ref.get("summary", "")).strip()
        given_path = Path(path_value)
        if not given_path.is_absolute():
            raise ValidationError(
                f"ref path must be absolute as given: {path_value}; use a true absolute path "
                "inside a configured project root, or inline the content in the message body"
            )
        try:
            path = given_path.resolve()
        except (ValueError, RuntimeError, OSError) as exc:
            raise ValidationError(
                f"refs[{index}].path is invalid: {path_value}; use a true absolute path "
                "inside a configured project root, or inline the content in the message body"
            ) from exc

        if roots and not any(path == root or root in path.parents for root in roots):
            allowed = ", ".join(str(root) for root in roots)
            raise ValidationError(
                f"ref path is outside configured project roots: {path}; inline the content in "
                "the message body, or use a path inside a configured project root; "
                f"allowed roots: {allowed}"
            )

        validated.append({"path": str(path), "summary": summary})

    return validated
