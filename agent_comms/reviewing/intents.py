"""Durable review dispatch-intent substrate (dispatch contract 17).

Sole SQL owner for the additive ``review_dispatch_intents`` ledger table:
the canonical versioned intent payload and its SHA-256 identity, the
prepared/active/bound/abandoned state machine keyed unconditionally by
``(producer_actor_id, idempotency_key)``, active-expiry reconciliation, the
dispatch-time active-intent match/bind consumed inside ``dispatch_agent``'s
existing write transaction, and the read-only status view.

This module is server-imported (``mcp_server -> dispatch_ledger -> intents``)
and therefore part of the governed dispatch-contract surface. It deliberately
imports no ``reviewing`` sibling, so loading the dispatch server never pulls
the review CLI stack; connections come from ``agent_comms.db.Database`` or are
passed in by the owning transaction, never opened here directly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_comms.clock import utc_now
from agent_comms.db import Database
from agent_comms.schema import ValidationError


PAYLOAD_VERSION = 1
ROUND_KIND_IMPLEMENTATION = "implementation"
ACTIVE_TTL_SECONDS = 15 * 60
INTENT_STATES = ("prepared", "active", "bound", "abandoned")

# Every field named in dispatch contract 17, in canonical order. The digest is
# computed over deterministic versioned JSON of exactly this field set, so a
# semantically identical preparation is byte-identical and any field drift
# (policy included) changes the identity and conflicts. The complete round
# identity is bound here: the named source branch, the source and integration
# HEAD/tree pair, and the round base commit/tree (equal to the source HEAD for
# the initial implementation round, whose clean baseline the mark-dispatched
# derivation rebinds rather than refusing when integration and source advanced
# together after ``open``).
PAYLOAD_FIELDS = (
    "payload_version",
    "producer_actor_id",
    "idempotency_key",
    "recipient_actor_id",
    "real_project_root",
    "policy_name",
    "policy_version",
    "round_kind",
    "record_id",
    "brief_sha256",
    "dod_sha256",
    "source_branch",
    "source_head",
    "source_tree",
    "integration_head",
    "integration_tree",
    "base_commit",
    "base_tree",
)

# The canonical payload fields persisted as JSON companions on the review
# record's intended-dispatch entry: every payload field except
# ``payload_version`` and ``idempotency_key`` (the entry's own base field).
# With the entry's key they reconstruct the exact canonical payload, so the
# durable JSON round entry and the SQL intent row are provably the same request
# (dispatch contract 17: the JSON companion agrees with the SQL payload).
COMPANION_PAYLOAD_FIELDS = tuple(
    name
    for name in PAYLOAD_FIELDS
    if name not in ("payload_version", "idempotency_key")
)


class IntentError(ValidationError):
    """A refused review dispatch-intent operation; the message names a remedy."""


def canonical_payload(**fields: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"payload_version": PAYLOAD_VERSION, **fields}
    missing = [name for name in PAYLOAD_FIELDS if name not in payload]
    unknown = sorted(set(payload) - set(PAYLOAD_FIELDS))
    if missing or unknown:
        raise IntentError(
            f"review_intent_payload_invalid: missing={missing}; unknown={unknown}"
        )
    for name in PAYLOAD_FIELDS:
        value = payload[name]
        if name == "payload_version":
            if value != PAYLOAD_VERSION:
                raise IntentError(
                    f"review_intent_payload_invalid: payload_version={value!r}"
                )
        elif not isinstance(value, str) or not value:
            raise IntentError(f"review_intent_payload_invalid: {name}={value!r}")
    return payload


def canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    if payload.get("payload_version") != PAYLOAD_VERSION:
        raise IntentError(
            f"review_intent_payload_invalid: payload_version={payload.get('payload_version')!r}"
        )
    canonical_payload(**{k: v for k, v in payload.items() if k != "payload_version"})
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_bytes(payload)).hexdigest()


def intent_id_for(digest: str) -> str:
    """Immutable digest-derived intent id: identical preparation, identical id."""
    return f"rvi_{digest}"


def companion_entry_fields(
    payload: dict[str, Any], intent_id: str, digest: str
) -> dict[str, Any]:
    """The round-bound JSON companion for a review intended-dispatch entry.

    Carries the complete canonical identity (minus the entry's own key) plus the
    derived intent id and digest, so a reader can rebuild and re-hash the exact
    payload and prove agreement with the SQL row without any second source.
    """
    fields = {name: payload[name] for name in COMPANION_PAYLOAD_FIELDS}
    fields["intent_id"] = intent_id
    fields["intent_digest"] = digest
    return fields


def payload_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the canonical payload from a round-bound intended-dispatch entry."""
    fields = {name: entry.get(name) for name in COMPANION_PAYLOAD_FIELDS}
    fields["idempotency_key"] = entry.get("idempotency_key")
    return canonical_payload(**fields)


def entry_matches_row(entry: dict[str, Any] | None, row: dict[str, Any]) -> bool:
    """Read-only: does the durable JSON companion agree with the SQL intent row?

    True only when the entry rebuilds the exact canonical payload the row's
    digest was computed over and carries the matching intent id and digest. Any
    field drift, a missing companion, or an id/digest mismatch is False, so a
    durable JSON+prepared crash pair is recognized without a separate SQL marker.
    """
    if not entry:
        return False
    try:
        digest = payload_digest(payload_from_entry(entry))
    except IntentError:
        return False
    return (
        digest == row["digest"]
        and entry.get("intent_digest") == row["digest"]
        and entry.get("intent_id") == row["intent_id"]
        and row["intent_id"] == intent_id_for(digest)
    )


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def expiry_deadline(row: dict[str, Any]) -> str | None:
    """ISO deadline after which reconciliation abandons this row, or None.

    Only active rows expire, and the active clock starts at activation.
    Prepared rows are non-expiring and non-dispatchable: they advance only
    through an exact ``mark-dispatched`` retry under the review-record lock.
    Bound rows have no independent TTL (the attached ledger row's state is
    authoritative) and abandoned rows have already expired.
    """
    if row["state"] != "active":
        return None
    deadline = _parse_ts(row["activated_at"]) + timedelta(seconds=ACTIVE_TTL_SECONDS)
    return deadline.isoformat(timespec="seconds")


def fetch(
    conn: sqlite3.Connection, producer_actor_id: str, idempotency_key: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "select * from review_dispatch_intents "
        "where producer_actor_id=? and idempotency_key=?",
        (producer_actor_id, idempotency_key),
    ).fetchone()
    return dict(row) if row is not None else None


def prepare(
    conn: sqlite3.Connection, payload: dict[str, Any], now: str | None = None
) -> dict[str, Any]:
    """Prepare or exact-match the single (producer, key) intent row.

    A fresh key inserts one ``prepared`` row. An existing row with the same
    canonical digest is an exact replay: prepared/active rows return unchanged
    and an abandoned row reactivates in place (same row, attempt evidence
    incremented, never a fan-out). A different digest conflicts permanently,
    and a bound row refuses because its ledger row is authoritative.
    """
    now = now or utc_now()
    digest = payload_digest(payload)
    producer = payload["producer_actor_id"]
    key = payload["idempotency_key"]
    existing = fetch(conn, producer, key)
    if existing is None:
        conn.execute(
            """
            insert into review_dispatch_intents(
              producer_actor_id, idempotency_key, intent_id, state,
              preledger_state, dispatch_id, recipient_actor_id,
              real_project_root, policy_name, policy_version, round_kind,
              digest, payload_json, attempt_count, created_at, updated_at,
              prepared_at
            )
            values(?, ?, ?, 'prepared', 'prepared', NULL, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                producer,
                key,
                intent_id_for(digest),
                payload["recipient_actor_id"],
                payload["real_project_root"],
                payload["policy_name"],
                payload["policy_version"],
                payload["round_kind"],
                digest,
                canonical_payload_bytes(payload).decode("utf-8"),
                now,
                now,
                now,
            ),
        )
        return fetch(conn, producer, key)  # type: ignore[return-value]
    if existing["digest"] != digest:
        raise IntentError(
            "review_intent_conflict: a different payload is already prepared for "
            f"producer={producer} key={key!r} (state={existing['state']}); this "
            "conflict is permanent; remedy: dispatch with a fresh idempotency key"
        )
    if existing["state"] in {"prepared", "active"}:
        return existing
    if existing["state"] == "bound":
        raise IntentError(
            "review_intent_already_bound: intent for "
            f"producer={producer} key={key!r} is bound to ledger row "
            f"{existing['dispatch_id']}; the ledger state is authoritative; "
            "remedy: replay dispatch_agent with the same key, or redispatch "
            "with a fresh key"
        )
    conn.execute(
        """
        update review_dispatch_intents
        set state='prepared', preledger_state='prepared', dispatch_id=NULL,
            attempt_count=attempt_count+1, prepared_at=?, activated_at=NULL,
            abandoned_at=NULL, abandon_reason=NULL, updated_at=?
        where producer_actor_id=? and idempotency_key=? and state='abandoned'
        """,
        (now, now, producer, key),
    )
    return fetch(conn, producer, key)  # type: ignore[return-value]


def activate(
    conn: sqlite3.Connection,
    producer_actor_id: str,
    idempotency_key: str,
    digest: str,
    now: str | None = None,
) -> dict[str, Any]:
    """CAS the exact prepared row to active, after its JSON pair is durable."""
    now = now or utc_now()
    cursor = conn.execute(
        """
        update review_dispatch_intents
        set state='active', preledger_state='active', activated_at=?, updated_at=?
        where producer_actor_id=? and idempotency_key=? and digest=?
          and state='prepared'
        """,
        (now, now, producer_actor_id, idempotency_key, digest),
    )
    row = fetch(conn, producer_actor_id, idempotency_key)
    if cursor.rowcount == 1:
        return row  # type: ignore[return-value]
    if row is not None and row["state"] == "active" and row["digest"] == digest:
        return row  # idempotent recovery re-run: already activated
    raise IntentError(
        "review_intent_activate_failed: no exact prepared row for "
        f"producer={producer_actor_id} key={idempotency_key!r} "
        f"(state={row['state'] if row else 'absent'}); remedy: rerun review "
        "mark-dispatched to re-prepare from the durable record"
    )


def match_for_binding(
    conn: sqlite3.Connection,
    producer_actor_id: str,
    idempotency_key: str,
    *,
    recipient_actor_id: str,
    real_project_root: str,
    policy_name: str,
    policy_version: str,
) -> dict[str, Any] | None:
    """Match the exact active intent for a dispatch, before any write effect.

    No row preserves ordinary dispatch (returns None). A non-active row or any
    recipient/root/policy/digest mismatch refuses so the enclosing transaction
    produces zero message, ledger, payload, or spawn effects.
    """
    row = fetch(conn, producer_actor_id, idempotency_key)
    if row is None:
        return None
    if row["state"] != "active":
        raise IntentError(
            f"review_intent_not_active: state={row['state']} for "
            f"producer={producer_actor_id} key={idempotency_key!r}; remedy: "
            "rerun review mark-dispatched to activate (or use a fresh key for "
            "a non-review dispatch)"
        )
    payload = json.loads(row["payload_json"])
    mismatches = [
        name
        for name, expected in (
            ("recipient_actor_id", recipient_actor_id),
            ("real_project_root", real_project_root),
            ("policy_name", policy_name),
            ("policy_version", policy_version),
        )
        if row[name] != expected or payload.get(name) != expected
    ]
    if payload_digest(payload) != row["digest"] or row["intent_id"] != intent_id_for(
        row["digest"]
    ):
        mismatches.append("digest")
    if mismatches:
        raise IntentError(
            f"review_intent_mismatch: fields={mismatches} for "
            f"producer={producer_actor_id} key={idempotency_key!r}; remedy: "
            "rerun review mark-dispatched so the active intent matches this "
            "dispatch exactly"
        )
    return row


def bind(
    conn: sqlite3.Connection,
    producer_actor_id: str,
    idempotency_key: str,
    dispatch_id: str,
    now: str | None = None,
) -> None:
    """CAS active -> bound in the same transaction that inserts the queued row."""
    now = now or utc_now()
    cursor = conn.execute(
        """
        update review_dispatch_intents
        set state='bound', preledger_state=NULL, dispatch_id=?, bound_at=?,
            updated_at=?
        where producer_actor_id=? and idempotency_key=? and state='active'
        """,
        (dispatch_id, now, now, producer_actor_id, idempotency_key),
    )
    if cursor.rowcount != 1:
        raise IntentError(
            "review_intent_bind_failed: active row vanished for "
            f"producer={producer_actor_id} key={idempotency_key!r}"
        )


def reconcile(conn: sqlite3.Connection, now: str | None = None) -> list[dict[str, Any]]:
    """Abandon expired active rows (15 min); every other state is untouched.

    Prepared rows never expire and are never probed here: a prepared row is
    non-dispatchable pre-ledger state whose only forward path is an exact
    ``mark-dispatched`` retry under the review-record lock, so no background
    pass (JSON-blind by construction) may classify or abandon it. Abandonment
    records ``preledger_state='abandoned'`` plus the reason so status can
    report it. Bound rows are never touched; their ledger row is authoritative.
    """
    now = now or utc_now()
    now_dt = _parse_ts(now)
    abandoned: list[dict[str, Any]] = []
    for row in conn.execute(
        "select * from review_dispatch_intents where state='active'"
    ).fetchall():
        row = dict(row)
        deadline = expiry_deadline(row)
        if deadline is None or now_dt < _parse_ts(deadline):
            continue
        conn.execute(
            """
            update review_dispatch_intents
            set state='abandoned', preledger_state='abandoned', abandon_reason=?,
                abandoned_at=?, updated_at=?
            where producer_actor_id=? and idempotency_key=? and state='active'
            """,
            (
                "active_ttl_expired",
                now,
                now,
                row["producer_actor_id"],
                row["idempotency_key"],
            ),
        )
        abandoned.append(
            {
                "intent_id": row["intent_id"],
                "producer_actor_id": row["producer_actor_id"],
                "idempotency_key": row["idempotency_key"],
                "expired_state": row["state"],
                "abandon_reason": "active_ttl_expired",
                "abandoned_at": now,
            }
        )
    return abandoned


def _ledger_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open the EXISTING canonical ledger read-write for one intent operation.

    Never creates an absent ledger and never runs full schema initialization
    (``Database.init`` also re-syncs agent actors, which a review-side intent
    operation must not do); only the additive intent table is guaranteed.
    """
    db = Database(Path(db_path))
    conn = db.open_existing_read_write()
    try:
        db.ensure_review_intent_schema(conn)
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _in_ledger_txn(db_path: Path | str, fn: Any, *args: Any) -> Any:
    conn = _ledger_connection(db_path)
    try:
        with conn:
            conn.execute("begin immediate")
            return fn(conn, *args)
    finally:
        conn.close()


def reconcile_in_ledger(db_path: Path | str) -> list[dict[str, Any]]:
    return _in_ledger_txn(db_path, reconcile)


def prepare_in_ledger(db_path: Path | str, payload: dict[str, Any]) -> dict[str, Any]:
    return _in_ledger_txn(db_path, prepare, payload)


def activate_in_ledger(
    db_path: Path | str, producer_actor_id: str, idempotency_key: str, digest: str
) -> dict[str, Any]:
    return _in_ledger_txn(db_path, activate, producer_actor_id, idempotency_key, digest)


def status_view(
    db_path: Path | str, producer_actor_id: str, idempotency_key: str
) -> dict[str, Any]:
    """Read-only derived intent view: state, expiry, association, and remedy.

    Opens the ledger strictly read-only and mutates no SQL or JSON state; an
    unreadable ledger or a pre-contract-17 ledger without the table reports
    ``unavailable`` instead of failing the status read.
    """
    try:
        conn = Database(Path(db_path)).read_only_connection()
    except sqlite3.Error as exc:
        return {"state": "unavailable", "reason": str(exc)}
    try:
        row = fetch(conn, producer_actor_id, idempotency_key)
    except sqlite3.Error as exc:
        return {"state": "unavailable", "reason": str(exc)}
    finally:
        conn.close()
    if row is None:
        return {
            "state": "absent",
            "idempotency_key": idempotency_key,
            "remedy": "run review mark-dispatched to prepare and activate the intent",
        }
    deadline = expiry_deadline(row)
    expired = deadline is not None and _parse_ts(utc_now()) >= _parse_ts(deadline)
    remedies = {
        "prepared": "rerun review mark-dispatched to activate",
        "active": "dispatch_agent with this producer/key consumes the intent",
        "bound": "the bound ledger row is authoritative; use dispatch-status",
        "abandoned": "rerun review mark-dispatched to reactivate the same row",
    }
    return {
        "state": row["state"],
        "intent_id": row["intent_id"],
        "digest": row["digest"],
        "round_kind": row["round_kind"],
        "attempt_count": row["attempt_count"],
        "dispatch_id": row["dispatch_id"],
        "abandon_reason": row["abandon_reason"],
        "expires_at": deadline,
        "expired": expired,
        "remedy": remedies[row["state"]],
    }
