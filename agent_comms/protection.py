"""Protected-actor guard shared by the operator CLI, spawn rerender and onboarding.

A protected actor refuses operator mutation unless the operator names a
reason; the override is printed to stderr and returned as an audit payload for
the caller to record. This module is a leaf: it depends only on
``agent_comms.schema``.
"""
from __future__ import annotations

import sys

from .schema import ValidationError


def protected_override_payload(protection: dict, reason: str) -> dict:
    return {"actor_id": protection["id"], "team": protection["team"], "reason": reason}


def require_unprotected_or_override(store, actor_id: str, override_reason: str | None) -> dict | None:
    protection = store.actor_protection(actor_id)
    if protection is None or not protection["protected"]:
        return None
    if override_reason is None or not override_reason.strip():
        raise ValidationError(
            f"actor {actor_id} on team {protection['team']} is protected; "
            "rerun with --override-protected \"<reason>\""
        )
    reason = override_reason.strip()
    print(
        f"PROTECTED ACTOR OVERRIDE: {actor_id} team={protection['team']} reason={reason}",
        file=sys.stderr,
    )
    return protected_override_payload(protection, reason)


def protected_actor_notice(protection: dict) -> None:
    print(
        f"protected actor notice: dispatch target {protection['id']} team={protection['team']}",
        file=sys.stderr,
    )
