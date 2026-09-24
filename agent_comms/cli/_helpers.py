from __future__ import annotations

import argparse
import hmac
import json
import os
import string
import sys
from pathlib import Path

from .. import paths
from ..schema import ValidationError
from ..spawn import render_spawn

DEFAULT_CONFIG = paths.REPO_ROOT / "config" / "actors.json"
ADMIN_TOKEN_PATH = Path.home() / ".agent-comms" / "admin-token"
_ADMIN_TOKEN_PATH_DEFAULT = ADMIN_TOKEN_PATH
ADMIN_CREDENTIAL_ERROR = "admin write paths require an operator credential; not available to workers."


class DegradedState(Exception):
    """A command answered successfully while reporting named defects."""

    def __init__(self, payload=None, *, already_printed: bool = False) -> None:
        super().__init__("degraded state")
        self.payload = payload
        self.already_printed = already_printed


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


def parse_json_list(value: str) -> list:
    if not value:
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise argparse.ArgumentTypeError("value must be a JSON list")
    return parsed


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
    token_path = ADMIN_TOKEN_PATH
    if token_path == _ADMIN_TOKEN_PATH_DEFAULT:
        token_path = Path.home() / ".agent-comms" / "admin-token"
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


def expand_path_value(value: str) -> str:
    """Expand ``~`` and ``${ENV}`` in a config path value.

    Backward-compatible: a plain absolute path (no ``~``/``$``) is returned
    unchanged, so existing configs and already-bootstrapped DB rows resolve
    identically. Lets operators express deployment roots as ``~/dev/X`` or
    ``${PROJECT_ROOT}`` instead of baking one machine's absolute path
    into config. ``${AGENT_COMMS_ROOT}`` is always available (the repo root),
    so in-repo deployment roots need no operator setup.

    A variable that is referenced but unset is a loud failure, not a silently
    broken ``project_root`` (invariant 5: no silent failure paths).
    """
    environment = {**os.environ, "AGENT_COMMS_ROOT": str(paths.REPO_ROOT)}
    expanded = os.path.expanduser(string.Template(value).safe_substitute(environment))
    if "$" in expanded:
        raise ValidationError(
            f"unresolved environment variable in config path {value!r}; "
            "set it before bootstrap (e.g. export PROJECT_ROOT=/path/to/repo)"
        )
    return expanded


def expand_actor_paths(entry: dict) -> dict:
    """Expand env/~ in an actor's path-bearing fields (in place)."""
    root = entry.get("project_root")
    if isinstance(root, str):
        entry["project_root"] = expand_path_value(root)
    spawn = entry.get("spawn")
    if isinstance(spawn, dict) and isinstance(spawn.get("env"), dict):
        spawn["env"] = {
            key: expand_path_value(val) if isinstance(val, str) else val
            for key, val in spawn["env"].items()
        }
    return entry


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


def load_actor_config(config_path: Path) -> dict:
    if config_path.exists():
        config = json.loads(config_path.read_text())
        for entry in config.get("actors", {}).values():
            expand_actor_paths(entry)
        return config

    legacy_path = config_path.with_name("agents.json")
    if not legacy_path.exists():
        return {"actors": {}}

    legacy = json.loads(legacy_path.read_text())
    actors = {}
    for agent_id, entry in legacy.get("agents", {}).items():
        actors[agent_id] = {
            "kind": "agent",
            "display_name": agent_id,
            "team": entry["team"],
            "role": entry.get("role", "architect"),
            "project_root": entry["project_root"],
            "capabilities": entry.get("capabilities", []),
        }
        if "runtime" in entry:
            actors[agent_id]["runtime"] = entry["runtime"]
        if "spawn" in entry:
            actors[agent_id]["spawn"] = entry["spawn"]
        expand_actor_paths(actors[agent_id])

    normalized = {"actors": actors}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")
    print(
        f"warning: {legacy_path} is deprecated; wrote normalized {config_path}",
        file=sys.stderr,
    )
    return normalized


def bootstrap_store(
    store: Store,
    config_path: Path,
    *,
    override_protected: str | None = None,
    override_records: list[dict] | None = None,
) -> list[dict]:
    config = load_actor_config(config_path)
    canonical = Path(config_path).expanduser().resolve() == DEFAULT_CONFIG.resolve()
    registered = []
    for actor_id, entry in config.get("actors", {}).items():
        kind = entry.get("kind", "agent")
        override_payload = None
        if not canonical:
            override_payload = require_unprotected_or_override(store, actor_id, override_protected)
            if override_payload is not None and override_records is not None:
                override_records.append(override_payload)
        if kind == "agent":
            if not entry.get("project_root"):
                raise ValidationError(f"agent actor {actor_id} requires project_root")
            if "spawn" in entry:
                raise ValidationError(
                    f"agent actor {actor_id} must not define spawn; "
                    "declare runtime and let render_spawn generate it"
                )
            runtime = entry.get("runtime")
            role = entry.get("role", "architect")
            if role == "worker" and not entry.get("owner"):
                raise ValidationError(f"worker actor {actor_id} requires owner")
            if role != "worker" and "owner" in entry:
                raise ValidationError(f"owner is only valid for worker actor {actor_id}")
            spawn = render_spawn(runtime, actor_id) if runtime else None
            registered.append(
                store.register_agent_actor(
                    actor_id,
                    entry["team"],
                    role,
                    entry["project_root"],
                    entry.get("capabilities", []),
                    display_name=entry.get("display_name", actor_id),
                    runtime=runtime,
                    spawn=spawn,
                    protected=entry.get("protected", True),
                    owner=entry.get("owner"),
                )
            )
        else:
            if "protected" in entry:
                raise ValidationError(f"non-agent actor {actor_id} must not define: protected")
            forbidden = {"project_root", "runtime", "spawn", "team", "role", "capabilities", "owner"} & set(entry)
            if forbidden:
                fields = ", ".join(sorted(forbidden))
                raise ValidationError(f"non-agent actor {actor_id} must not define: {fields}")
            registered.append(
                store.register_actor(
                    actor_id,
                    kind,
                    entry["display_name"],
                    system_class=entry.get("system_class"),
                    system_instance=entry.get("system_instance"),
                )
            )
    return registered
