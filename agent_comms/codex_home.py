"""Per-worker Codex home isolation: actor enumeration and spawn-time preflight.

Each Codex worker runs with its own ``CODEX_HOME`` (generated config and
profile, plus an ``auth.json`` that may be shared by a lineage of workers).
This module enumerates the registered Codex actors and their homes, and
preflights a resolved home before a native spawn.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .schema import ValidationError, identity_to_path_segment


def _scan_codex_actors(conn) -> tuple[list[dict], list[dict]]:
    """Return resolvable Codex actors and isolated registry defects."""
    rows = conn.execute(
        "select id, spawn_json from actors where runtime = 'codex' order by id"
    ).fetchall()
    actors: list[dict] = []
    defects: list[dict] = []
    for row in rows:
        code = None
        try:
            spawn = json.loads(row["spawn_json"] or "{}")
        except (TypeError, ValueError):
            spawn = None
            code = "malformed_spawn_json"
        if code is None and (
            not isinstance(spawn, dict) or not isinstance(spawn.get("env", {}), dict)
        ):
            code = "malformed_spawn_env"
        value = spawn.get("env", {}).get("CODEX_HOME") if code is None else None
        if code is None and not value:
            code = "missing_codex_home"
        if code is not None:
            defects.append(
                {
                    "actor_id": row["id"],
                    "code": code,
                    "scope": "isolated",
                    "context": {},
                }
            )
        else:
            actors.append({"id": row["id"], "spawn": spawn, "codex_home_value": value})
    return actors, defects

def _codex_actor_rows(conn) -> list[dict]:
    """Every registered Codex actor with a resolvable ``CODEX_HOME``.

    A registered Codex actor whose ``spawn_json`` is unparseable, whose ``env``
    is not an object, or which lacks a non-empty ``CODEX_HOME`` is REFUSED
    visibly (raises ``ValidationError``) rather than silently dropped, so a
    caller never claims a complete actor set while quietly omitting a
    malformed actor.
    """
    actors, defects = _scan_codex_actors(conn)
    if defects:
        defect = defects[0]
        actor_id = defect["actor_id"]
        messages = {
            "malformed_spawn_json": f"refusing: registered Codex actor {actor_id} has malformed spawn_json",
            "malformed_spawn_env": f"refusing: registered Codex actor {actor_id} has a malformed spawn env",
            "missing_codex_home": (
                f"refusing: registered Codex actor {actor_id} has no spawn.env.CODEX_HOME; "
                "resolve or deregister it first"
            ),
        }
        raise ValidationError(messages[defect["code"]])
    return actors

def preflight_home(
    codex_home: Path,
    *,
    actor_id: str | None = None,
    stale_after: "timedelta | None" = None,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    ok, why, _ = preflight_home_snapshot(
        codex_home, actor_id=actor_id, stale_after=stale_after, now=now
    )
    return ok, why

def preflight_home_snapshot(
    codex_home: Path,
    *,
    actor_id: str | None = None,
    stale_after: "timedelta | None" = None,
    now: datetime | None = None,
) -> tuple[bool, str | None, dict | None]:
    """Preflight a resolved Codex home for a native spawn.

    Requires a real home directory (not a symlink), the regular generated base
    config, the EXACT actor-specific regular generated profile config (when
    ``actor_id`` is supplied), and a resolvable/regular/parseable/fresh auth
    file. ``stale_after`` is an exact ``timedelta`` (default 13 days); the
    freshness comparison uses the full timedelta and is never truncated through
    ``.days`` (so a threshold of, e.g., 1.5 days is honored exactly). Returns
    ``(ok, reason)``; ``reason`` is None on success.
    """
    if stale_after is None:
        stale_after = timedelta(days=13)
    if codex_home.is_symlink():
        return False, "home directory is a symlink", None
    if not codex_home.is_dir():
        return False, "home directory is missing", None
    base = codex_home / "config.toml"
    if base.is_symlink() or not base.is_file():
        return (
            False,
            "generated base config.toml is missing or not a regular file",
            None,
        )
    if actor_id is not None:
        # Require the EXACT actor-specific generated profile, not any first
        # ``*.config.toml`` entry.
        profile = codex_home / f"{identity_to_path_segment(actor_id)}.config.toml"
        if profile.is_symlink() or not profile.is_file():
            return (
                False,
                "generated profile config is missing or not a regular file",
                None,
            )
    else:
        profile_ok = False
        for entry in codex_home.iterdir():
            if entry.name.endswith(".config.toml") and entry.name != "config.toml":
                if entry.is_file() and not entry.is_symlink():
                    profile_ok = True
                break
        if not profile_ok:
            return (
                False,
                "generated profile config is missing or not a regular file",
                None,
            )
    auth = codex_home / "auth.json"
    from .codex_auth_refresh import codex_auth_snapshot

    current = now or datetime.now(timezone.utc)
    snapshot = codex_auth_snapshot(auth, now=current)
    status = snapshot["read_status"]
    if status != "ok":
        reason = {
            "missing": "auth.json is missing",
            "not_regular": "auth.json does not resolve to a regular file",
        }.get(status, "auth.json is not parseable JSON")
        return False, reason, snapshot
    reasons = {
        "no_last_refresh": "auth.json last_refresh is missing",
        "naive_last_refresh": "auth.json last_refresh is not timezone-aware",
        "bad_last_refresh": "auth.json last_refresh is unparseable",
    }
    for gap, reason in reasons.items():
        if gap in snapshot["gaps"]:
            return False, reason, snapshot
    refreshed = datetime.fromisoformat(snapshot["created"])
    if (current - refreshed.astimezone(timezone.utc)) > stale_after:
        return False, "auth.json last_refresh is stale", snapshot
    return True, None, snapshot
