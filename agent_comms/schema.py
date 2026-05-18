from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRIORITIES = {"low", "normal", "high", "blocker"}
# "closed" is a recipient-side terminal state. It is intentionally separate from
# "requires_ack"; prompts enforce when an ack is socially required.
STATUSES = {"sent", "read", "acknowledged", "closed"}


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


def validate_priority(priority: str) -> str:
    priority = require_non_empty(priority, "priority")
    if priority not in PRIORITIES:
        allowed = ", ".join(sorted(PRIORITIES))
        raise ValidationError(f"priority must be one of: {allowed}")
    return priority


def validate_refs(refs: list[dict[str, Any]], project_roots: list[Path]) -> list[dict[str, str]]:
    validated: list[dict[str, str]] = []
    roots = [root.expanduser().resolve() for root in project_roots]

    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            raise ValidationError(f"refs[{index}] must be an object")
        path_value = require_non_empty(str(ref.get("path", "")), f"refs[{index}].path")
        summary = str(ref.get("summary", "")).strip()
        path = Path(path_value).expanduser().resolve()

        if roots and not any(path == root or root in path.parents for root in roots):
            allowed = ", ".join(str(root) for root in roots)
            raise ValidationError(f"ref path is outside configured project roots: {path}; allowed roots: {allowed}")

        validated.append({"path": str(path), "summary": summary})

    return validated
