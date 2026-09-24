"""Governed, per-lineage Codex authentication refresh."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from .codex_auth_constants import (
    REFRESH_TOKEN_EXPIRED_MESSAGE,
    REFRESH_TOKEN_INVALIDATED_MESSAGE,
)


FALLBACK_MAX_AGE_SECONDS = 7 * 86400
# Conservative static satisfiability bound for rotated tokens, not a provider
# guarantee: the actual-token gate still checks remaining life before spawn.
MINIMUM_ROTATED_TOKEN_LIFETIME_SECONDS = FALLBACK_MAX_AGE_SECONDS
DEFERRAL_PAGE_MARGIN = 86400
PAGE_THROTTLE_SECONDS = 12 * 3600
CLAIM_TTL_SECONDS = 3600
SPAWN_FRESHNESS_MARGIN_SECONDS = 900
FAILURE_OUTCOMES = frozenset(
    {"exec_failed", "verify_failed", "binary_missing", "hardlink_refused", "due_metadata_invalid"}
)


def classify_refresh_failure(output: str | None) -> str | None:
    if not isinstance(output, str):
        return None
    reasons = [
        reason
        for message, reason in (
            (REFRESH_TOKEN_EXPIRED_MESSAGE, "expired"),
            (REFRESH_TOKEN_INVALIDATED_MESSAGE, "revoked"),
        )
        if message in output
    ]
    return reasons[0] if len(reasons) == 1 else None


def dead_credentials(store) -> list[dict]:
    with store._db.connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "select lineage_key, dead_reason, dead_at from codex_refresh_claims "
                "where dead_reason is not null order by lineage_key"
            )
        ]


def _clear_dead(conn, lineage_key: str) -> None:
    conn.execute(
        "update codex_refresh_claims set dead_reason=NULL, dead_at=NULL, dead_digest=NULL "
        "where lineage_key=?",
        (lineage_key,),
    )


class PageFailed(RuntimeError):
    pass


def _now(value=None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _env_seconds(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _jwt_exp(token: object) -> int | None:
    if not isinstance(token, str):
        return None
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        value = json.loads(base64.urlsafe_b64decode(part.encode()).decode())
        return int(value["exp"])
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        return None


def codex_auth_snapshot(auth_path: Path, *, now: datetime) -> dict:
    """Read at most once and expose only parsed credential timing and a digest."""
    current = _now(now).astimezone(timezone.utc)
    snapshot = {
        "lineage_key": str(auth_path),
        "read_status": "unreadable",
        "created": None,
        "expires": None,
        "ttl_seconds": None,
        "captured_at": current.isoformat(),
        "auth_digest": None,
        "gaps": [],
    }
    try:
        snapshot["lineage_key"] = str(auth_path.resolve())
    except Exception:
        pass
    try:
        if not auth_path.exists():
            snapshot["read_status"] = "missing"
            return snapshot
        if not auth_path.is_file():
            snapshot["read_status"] = "not_regular"
            return snapshot
        raw = auth_path.read_bytes()
    except Exception:
        return snapshot
    snapshot["auth_digest"] = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        snapshot["read_status"] = "not_json"
        return snapshot
    snapshot["read_status"] = "ok"
    data = data if isinstance(data, dict) else {}
    gaps = []
    last_refresh = data.get("last_refresh")
    if not last_refresh:
        gaps.append("no_last_refresh")
    else:
        try:
            created = datetime.fromisoformat(str(last_refresh).replace("Z", "+00:00"))
            if created.tzinfo is None:
                gaps.append("naive_last_refresh")
            else:
                snapshot["created"] = created.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            gaps.append("bad_last_refresh")
    tokens = data.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    token = data.get("access_token") or tokens.get("access_token")
    if not isinstance(token, str) or not token:
        gaps.append("no_token")
    else:
        try:
            exp = _jwt_exp(token)
            if exp is None:
                raise ValueError("no expiration")
            snapshot["expires"] = datetime.fromtimestamp(exp, timezone.utc).isoformat()
            snapshot["ttl_seconds"] = int(exp - current.timestamp())
        except (ValueError, TypeError, OverflowError, OSError):
            gaps.append("no_exp")
    snapshot["gaps"] = sorted(gaps)
    return snapshot


def access_token_remaining_seconds(auth_path: str | Path, *, now=None) -> int | None:
    """Return remaining JWT access-token life, failing closed with ``None``.

    Callers use this public helper for admission decisions so refresh and spawn
    share the same synthetic-document parsing rules.
    """
    current = _now(now)
    try:
        data = json.loads(Path(auth_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    if tokens is not None and not isinstance(tokens, dict):
        return None
    token = data.get("access_token") or (tokens or {}).get("access_token")
    exp = _jwt_exp(token)
    if exp is None:
        return None
    return int(exp - current.timestamp())


def spawn_freshness_margin_seconds() -> int:
    return _env_seconds(
        "AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS",
        SPAWN_FRESHNESS_MARGIN_SECONDS,
    )


def _jwt_exp_is_nonnumeric(token: str) -> bool:
    """Return whether a decodable JWT has an explicitly non-numeric exp."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        value = json.loads(base64.urlsafe_b64decode(part.encode()).decode())
        exp = value["exp"]
        return isinstance(exp, bool) or not isinstance(exp, (int, float))
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        return False


def _metadata(auth_path: Path, now: datetime) -> tuple[bool | None, tuple[int | None, str | None]]:
    try:
        if not auth_path.is_file():
            return None, (None, None)
        data = json.loads(auth_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, (None, None)
    if not isinstance(data, dict):
        return None, (None, None)
    top_token = data.get("access_token")
    if "access_token" in data and not isinstance(top_token, str):
        return None, (None, None)
    tokens = data.get("tokens")
    if "tokens" in data and not isinstance(tokens, dict):
        return None, (None, None)
    nested_token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if isinstance(tokens, dict) and "access_token" in tokens and not isinstance(nested_token, str):
        return None, (None, None)
    token = top_token or nested_token
    if isinstance(token, str) and _jwt_exp_is_nonnumeric(token):
        return None, (None, None)
    last = data.get("last_refresh")
    if "last_refresh" in data and not isinstance(last, str):
        return None, (None, None)
    last_dt = None
    if isinstance(last, str):
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            return None, (None, None)
        # Naive timestamps are invalid: silently choosing a timezone would make
        # age fallback depend on an assumption absent from the metadata.
        if last_dt.tzinfo is None:
            return None, (None, None)
    exp = _jwt_exp(token)
    if exp is not None:
        # Expiry-only: any positive remaining lifetime is not due.
        return exp - now.timestamp() <= 0, (exp, last)
    if last_dt is None:
        return None, (None, None)
    return last_dt < now - timedelta(seconds=FALLBACK_MAX_AGE_SECONDS), (None, last)


def _urgency(metadata: tuple[int | None, str | None], now: datetime) -> dict[str, int]:
    exp, last = metadata
    if exp is not None:
        return {"remaining_seconds": int(exp - now.timestamp())}
    if last is not None:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        return {"age_seconds": int((now - last_dt).total_seconds())}
    return {}


def _page(
    store,
    lineage_ordinal: int,
    outcome: str,
    actor_count: int,
    lineage_key: str,
    now: datetime,
    *,
    remaining_seconds: int | None = None,
    age_seconds: int | None = None,
    containment_failed: bool = False,
    dead_reason: str | None = None,
) -> None:
    stamp = now.isoformat(timespec="seconds")
    cutoff = (now - timedelta(seconds=PAGE_THROTTLE_SECONDS)).isoformat(timespec="seconds")
    with store._db.connection() as conn:
        conn.execute("insert into codex_refresh_claims(lineage_key) values(?) on conflict(lineage_key) do nothing", (lineage_key,))
        claimed = conn.execute(
            "update codex_refresh_claims set last_page_at=? where lineage_key=? "
            "and (last_page_at is null or last_page_at < ?)",
            (stamp, lineage_key, cutoff),
        ).rowcount == 1
    if not claimed:
        return
    try:
        # AGENT_COMMS_OPERATOR_ACTOR, else the single registered human.
        operator = store._actors.resolve_operator_human()
        body = [f"lineage_ordinal={lineage_ordinal}", f"outcome={outcome}", f"actor_count={actor_count}"]
        if containment_failed:
            body.append("containment_failed=true")
        if remaining_seconds is not None:
            body.append(f"remaining_seconds={remaining_seconds}")
        elif age_seconds is not None:
            body.append(f"age_seconds={age_seconds}")
        if dead_reason is not None:
            body.append(f"dead_reason={dead_reason}")
        store.send_message(operator, [operator], f"[codex refresh] lineage {lineage_ordinal}: {outcome}",
                           "\n".join(body), [],
                           priority="blocker", requires_ack=True)
    except Exception as exc:
        # The atomic throttle slot remains consumed after send failure: without
        # a transactional mailbox API, releasing it would permit page storms.
        print(f"codex auth refresh page_failed: {type(exc).__name__}", file=sys.stderr)
        raise PageFailed("page_failed") from exc


def _release(store, lineage_key: str, *, reset_deferred: bool) -> None:
    with store._db.connection() as conn:
        conn.execute("update codex_refresh_claims set holder=NULL, claimed_at=NULL" +
                     (", first_deferred_at=NULL" if reset_deferred else "") + " where lineage_key=?", (lineage_key,))


def refresh_if_due(store, *, lineage_key: str, codex_home: str, actor_count: int,
                   lineage_ordinal: int, exec_runner, now=None) -> dict:
    now = _now(now)
    auth_path = Path(codex_home) / "auth.json"
    due, before = _metadata(auth_path, now)
    with store._db.connection() as conn:
        row = conn.execute(
            "select dead_digest from codex_refresh_claims where lineage_key=?",
            (lineage_key,),
        ).fetchone()
        if row and row["dead_digest"]:
            try:
                digest = hashlib.sha256(auth_path.read_bytes()).hexdigest()
            except OSError:
                digest = None
            if digest != row["dead_digest"]:
                _clear_dead(conn, lineage_key)
    urgency = _urgency(before, now)
    if due is False:
        with store._db.connection() as conn:
            conn.execute("update codex_refresh_claims set first_deferred_at=NULL where lineage_key=?", (lineage_key,))
        return {"outcome": "not_due", "ok": True}
    if due is None:
        outcome = "due_metadata_invalid"
        with store._db.connection() as conn:
            conn.execute("insert into codex_refresh_claims(lineage_key) values(?) on conflict(lineage_key) do update set first_deferred_at=NULL", (lineage_key,))
        _page(store, lineage_ordinal, outcome, actor_count, lineage_key, now, **urgency)
        return {"outcome": outcome, "ok": False}

    holder = uuid.uuid4().hex
    cutoff = (now - timedelta(seconds=CLAIM_TTL_SECONDS)).isoformat(timespec="seconds")
    stamp = now.isoformat(timespec="seconds")
    with store._db.connection() as conn:
        conn.execute("insert into codex_refresh_claims(lineage_key) values(?) on conflict(lineage_key) do nothing", (lineage_key,))
        cur = conn.execute("update codex_refresh_claims set holder=?, claimed_at=? where lineage_key=? and (holder is null or claimed_at is null or claimed_at < ?)", (holder, stamp, lineage_key, cutoff))
        acquired = cur.rowcount == 1
    if not acquired:
        return {"outcome": "claim_contention", "ok": True}
    reset_deferred = True
    release_lease = True
    try:
        if store._dispatch.codex_lineage_holding(lineage_key):
            reset_deferred = False
            with store._db.connection() as conn:
                conn.execute("update codex_refresh_claims set first_deferred_at=coalesce(first_deferred_at, ?) where lineage_key=?", (stamp, lineage_key))
            remaining = (before[0] - now.timestamp()) if before[0] is not None else None
            if remaining is None or remaining < _env_seconds("AGENT_COMMS_CODEX_DEFERRAL_PAGE_MARGIN", DEFERRAL_PAGE_MARGIN):
                _page(store, lineage_ordinal, "deferred_busy", actor_count, lineage_key, now, **urgency)
            return {"outcome": "deferred_busy", "ok": True}
        binary = shutil.which("codex")
        if not binary:
            _page(store, lineage_ordinal, "binary_missing", actor_count, lineage_key, now, **urgency)
            return {"outcome": "binary_missing", "ok": False}
        exec_result = exec_runner(codex_home, binary)
        containment_failed = bool(
            isinstance(exec_result, dict) and exec_result.get("containment_failed")
        )
        exec_ok = bool(exec_result.get("ok")) if isinstance(exec_result, dict) else bool(exec_result)
        if not exec_ok:
            release_lease = not containment_failed
            dead_reason = classify_refresh_failure(
                exec_result.get("output") if isinstance(exec_result, dict) else None
            )
            if dead_reason is not None:
                try:
                    digest = hashlib.sha256(auth_path.read_bytes()).hexdigest()
                except OSError:
                    dead_reason = None
                else:
                    with store._db.connection() as conn:
                        conn.execute(
                            "update codex_refresh_claims set dead_reason=?, dead_at=?, dead_digest=? "
                            "where lineage_key=?",
                            (dead_reason, stamp, digest, lineage_key),
                        )
            _page(
                store,
                lineage_ordinal,
                "exec_failed",
                actor_count,
                lineage_key,
                now,
                containment_failed=containment_failed,
                dead_reason=dead_reason,
                **urgency,
            )
            result = {
                "outcome": "exec_failed",
                "ok": False,
                "containment_failed": containment_failed,
            }
            if dead_reason is not None:
                result["dead_reason"] = dead_reason
            return result
        after_due, after = _metadata(auth_path, now)
        advanced = ((before[0] is not None and after[0] is not None and after[0] > before[0]) or
                    (before[1] is not None and after[1] is not None and after[1] > before[1]))
        if after_due is None or not advanced:
            _page(store, lineage_ordinal, "verify_failed", actor_count, lineage_key, now, **urgency)
            return {"outcome": "verify_failed", "ok": False}
        with store._db.connection() as conn:
            _clear_dead(conn, lineage_key)
        return {"outcome": "refreshed", "ok": True}
    finally:
        if release_lease:
            _release(store, lineage_key, reset_deferred=reset_deferred)
