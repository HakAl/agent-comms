"""Emergency-settlement execution-plan integrity primitive (stage 2, T7 leaf).

This is the sealed artifact behind ``agent-comms admin settle-dispatch``: the
dry-run preview emits ONE short-lived, shell-safe execution plan and execution
re-verifies it before touching the ledger. The plan is an *integrity-sealed
concurrency artifact, not bearer authorization*: verifying it proves it was
issued intact and has not expired, but execution separately requires and
revalidates the exact admin credential and rechecks the embedded snapshot under
``BEGIN IMMEDIATE`` (both of which live in the ledger-execution cluster, not
here). The plan's integrity strength therefore inherits the existing admin
credential's strength; this primitive introduces no new token-entropy guarantee.

Wire form (a single copy/paste token, no shell metacharacters)::

    v1.<unpadded-payload-base64url>.<unpadded-hmac-base64url>

The payload is canonical JSON binding the admin actor, dispatch, normalized
reason, exact expected snapshot, issued/expiry timestamps, and a nonce. The
signature is HMAC-SHA256 over the versioned signing input ``v1.<payload>`` keyed
by the admin credential, so the version, payload, and every bound value are
covered. Expiry is exactly five minutes after issue.

This module is pure and stdlib-only. It performs no I/O, reads no clock, and
makes no DB decision; callers pass ``issued_at`` / ``nonce`` / ``now`` and the
admin credential explicitly. Classified off the dispatch spawn path via the
existing ``agent_comms/cli/**`` contract-surface exclusion.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta
from typing import Any

from ..schema import ValidationError

PLAN_VERSION = "v1"

# The exact unpadded base64url alphabet. A plan segment must be composed solely
# of these characters: any other byte (standard-base64 ``+``/``/``, ``=``
# padding, whitespace, or arbitrary punctuation such as ``!``) is refused.
_B64URL_ALPHABET_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PLAN_TTL_SECONDS = 300
MAX_SETTLEMENT_REASON_CHARS = 2000

# Payload keys in their canonical (sorted) presence. Kept explicit so a decoded
# plan with missing or extra keys is rejected as malformed rather than silently
# accepted.
_PAYLOAD_KEYS = frozenset(
    {"version", "actor_id", "dispatch_id", "reason", "snapshot", "issued_at", "expiry_at", "nonce"}
)


class SettlementPlanError(ValueError):
    """Base class for a plan that cannot be trusted for execution."""


class PlanFormatError(SettlementPlanError):
    """The plan is not a well-formed ``v1`` triple / decodable payload."""


class PlanSignatureError(SettlementPlanError):
    """The HMAC does not verify under the supplied admin credential."""


class PlanExpiredError(SettlementPlanError):
    """``now`` is past the plan's five-minute expiry."""


class PlanMismatchError(SettlementPlanError):
    """A bound actor/dispatch does not match the caller's expected value."""


def normalize_reason(reason: str) -> str:
    """Return the whitespace-trimmed reason, refusing empty or oversized input."""
    if not isinstance(reason, str):
        raise ValidationError("settlement reason must be a string")
    trimmed = reason.strip()
    if not trimmed:
        raise ValidationError("settlement reason must not be empty")
    if len(trimmed) > MAX_SETTLEMENT_REASON_CHARS:
        raise ValidationError(
            f"settlement reason must be at most {MAX_SETTLEMENT_REASON_CHARS} characters"
        )
    return trimmed


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, bytes):
        if not secret:
            raise ValidationError("admin credential must not be empty")
        return secret
    if not isinstance(secret, str) or not secret.strip():
        raise ValidationError("admin credential must not be empty")
    return secret.encode("utf-8")


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    """Decode ONE strict, canonical, unpadded base64url segment to bytes.

    Strictness is load-bearing for the settlement plan's integrity seal: the
    tolerant standard-library decoder silently discards characters outside the
    alphabet, so a lax guard let an attacker append punctuation (e.g. ``!!!!``)
    to the signature and still verify. This refuses, as a ``PlanFormatError``:

    - any character outside the unpadded base64url alphabet (``+``, ``/``, ``=``
      padding, whitespace, or arbitrary punctuation);
    - a length that cannot be valid unpadded base64 (``len % 4 == 1``);
    - every decodable-but-noncanonical representation whose otherwise-unused
      trailing bits are nonzero, verified by requiring the canonical re-encoding
      to reproduce the exact input.
    """
    if not isinstance(segment, str) or not segment:
        raise PlanFormatError("plan segment is empty")
    if not _B64URL_ALPHABET_RE.match(segment):
        raise PlanFormatError("plan segment is not unpadded base64url")
    if len(segment) % 4 == 1:
        raise PlanFormatError("plan segment has an invalid base64url length")
    padding = "=" * (-len(segment) % 4)
    try:
        # binascii.Error subclasses ValueError, so this covers a bad decode too.
        decoded = base64.urlsafe_b64decode(segment + padding)
    except ValueError as exc:
        raise PlanFormatError("plan segment is not decodable base64url") from exc
    if _b64url_nopad(decoded) != segment:
        raise PlanFormatError("plan segment is not canonical base64url")
    return decoded


def _parse_ts(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PlanFormatError(f"plan {field} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PlanFormatError(f"plan {field} must be timezone-aware")
    return parsed


def _canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sign(secret: bytes, payload_segment: str) -> str:
    signing_input = f"{PLAN_VERSION}.{payload_segment}".encode("ascii")
    digest = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return _b64url_nopad(digest)


def build_plan(
    *,
    secret: str | bytes,
    actor_id: str,
    dispatch_id: str,
    reason: str,
    snapshot: dict[str, Any],
    issued_at: str,
    nonce: str,
) -> str:
    """Build a sealed ``v1`` execution plan.

    ``issued_at`` is an ISO-8601, timezone-aware instant; expiry is exactly
    ``PLAN_TTL_SECONDS`` later. ``snapshot`` is bound opaquely (the caller owns
    its shape). ``nonce`` and ``issued_at`` are supplied by the caller so this
    function stays pure and deterministic.
    """
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValidationError("settlement plan actor_id must not be empty")
    if not isinstance(dispatch_id, str) or not dispatch_id.strip():
        raise ValidationError("settlement plan dispatch_id must not be empty")
    if not isinstance(nonce, str) or not nonce.strip():
        raise ValidationError("settlement plan nonce must not be empty")
    if not isinstance(snapshot, dict):
        raise ValidationError("settlement plan snapshot must be an object")

    secret_bytes = _secret_bytes(secret)
    normalized_reason = normalize_reason(reason)
    issued = _parse_ts(issued_at, "issued_at")
    expiry_at = (issued + timedelta(seconds=PLAN_TTL_SECONDS)).isoformat(timespec="seconds")

    payload = {
        "version": PLAN_VERSION,
        "actor_id": actor_id,
        "dispatch_id": dispatch_id,
        "reason": normalized_reason,
        "snapshot": snapshot,
        "issued_at": issued_at,
        "expiry_at": expiry_at,
        "nonce": nonce,
    }
    payload_segment = _b64url_nopad(_canonical_payload_bytes(payload))
    signature_segment = _sign(secret_bytes, payload_segment)
    return f"{PLAN_VERSION}.{payload_segment}.{signature_segment}"


def verify_plan(
    *,
    secret: str | bytes,
    plan: str,
    now: str | None,
    expected_actor_id: str | None = None,
    expected_dispatch_id: str | None = None,
) -> dict[str, Any]:
    """Verify integrity (+ optional expiry) and return the bound claim.

    Raises the appropriate ``SettlementPlanError`` subclass on a malformed
    triple, a signature that does not verify under ``secret`` (tamper/forgery or
    a wrong/absent credential), a signed-but-noncanonical JSON payload, an
    expired plan, or an actor/dispatch that does not match a supplied
    expectation. Verification proves integrity only; it is not authorization and
    performs no DB recheck.

    When ``now`` is ``None`` the expiry check is deferred to the caller (used by
    execution: an EXACT already-committed replay must still pass every integrity
    and binding check but is permitted after expiry, while a first use enforces
    expiry via :func:`plan_is_expired`). Encoding, signature, canonical-JSON, and
    actor/dispatch binding are ALWAYS enforced regardless of ``now``.
    """
    secret_bytes = _secret_bytes(secret)
    if not isinstance(plan, str):
        raise PlanFormatError("plan must be a string")
    parts = plan.split(".")
    if len(parts) != 3:
        raise PlanFormatError("plan must have exactly three dot-separated segments")
    version, payload_segment, signature_segment = parts
    if version != PLAN_VERSION:
        raise PlanFormatError(f"unsupported plan version: {version!r}")

    # Strict-decode BOTH segments' format up front: punctuation, ``=`` padding,
    # an invalid length, or a decodable-but-noncanonical representation in EITHER
    # the payload or the signature is a format error, independent of the
    # signature value. Validating the payload segment here (rather than after the
    # signature compare) means a malformed payload refuses as a format error
    # instead of being reported as a signature mismatch.
    payload_bytes = _b64url_decode(payload_segment)
    provided_signature = _b64url_decode(signature_segment)

    expected_signature = _sign(secret_bytes, payload_segment)
    if not hmac.compare_digest(_b64url_decode(expected_signature), provided_signature):
        raise PlanSignatureError("plan signature does not verify under the admin credential")

    try:
        payload = json.loads(payload_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PlanFormatError("plan payload is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        raise PlanFormatError("plan payload does not have the expected fields")
    if payload.get("version") != PLAN_VERSION:
        raise PlanFormatError("plan payload version mismatch")
    # Strict canonical-JSON: the decoded bytes must equal the canonical
    # re-encoding. A correctly HMAC-signed payload whose JSON was reordered,
    # whitespace-padded, or otherwise noncanonically encoded is refused, so a
    # single canonical wire form exists per logical plan (defence in depth for
    # fingerprint/replay identity, independent of the base64url canonicality).
    if payload_bytes != _canonical_payload_bytes(payload):
        raise PlanFormatError("plan payload is not canonical JSON")

    issued = _parse_ts(payload["issued_at"], "issued_at")
    expiry = _parse_ts(payload["expiry_at"], "expiry_at")
    if expiry != issued + timedelta(seconds=PLAN_TTL_SECONDS):
        raise PlanFormatError("plan expiry is not exactly the fixed TTL after issue")

    if now is not None:
        now_ts = _parse_ts(now, "now")
        if now_ts > expiry:
            raise PlanExpiredError("plan has expired")

    if expected_actor_id is not None and payload["actor_id"] != expected_actor_id:
        raise PlanMismatchError("plan actor does not match the expected admin actor")
    if expected_dispatch_id is not None and payload["dispatch_id"] != expected_dispatch_id:
        raise PlanMismatchError("plan dispatch does not match the expected dispatch")

    return payload


def plan_is_expired(claim: dict[str, Any], now: str) -> bool:
    """Return whether ``now`` is strictly past the verified claim's expiry.

    Execution uses this for the deferred first-use expiry decision after
    :func:`verify_plan` proved integrity/binding with ``now=None``: a first
    settlement refuses when expired, while an EXACT already-committed replay is
    permitted regardless (the caller never reaches this on the replay path).
    """
    expiry = _parse_ts(claim["expiry_at"], "expiry_at")
    now_ts = _parse_ts(now, "now")
    return now_ts > expiry


def plan_fingerprint(plan: str) -> str:
    """Return a stable SHA-256 hex fingerprint of the exact plan string.

    Execution stores this alongside the settlement audit so an exact replay of
    the same committed plan returns the stored winner, while a different plan
    (different nonce, tamper, or re-issue) fingerprints differently and cannot
    match.
    """
    if not isinstance(plan, str):
        raise PlanFormatError("plan must be a string")
    return hashlib.sha256(plan.encode("utf-8")).hexdigest()
