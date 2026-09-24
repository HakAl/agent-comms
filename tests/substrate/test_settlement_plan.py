"""Focused substrate tests for the emergency-settlement execution plan.

Scope: this is the T7 *plan-integrity* leaf of dead-worker terminalization
stage 2. It pins ONLY the short-lived, shell-safe, HMAC-sealed
``v1.<payload>.<hmac>`` plan artifact that the (not-yet-built) admin
``settle-dispatch`` preview emits and execution re-verifies:

  - exact shell-safe ``v1`` wire form (unpadded base64url, no metacharacters);
  - canonical-JSON payload binding admin actor, dispatch, normalized reason,
    exact expected snapshot, issued/expiry timestamps, and a nonce;
  - expiry exactly five minutes after issue;
  - HMAC-SHA256 over the versioned payload keyed by the admin credential;
  - the plan is an integrity-sealed concurrency artifact, NOT bearer
    authorization: verification separately requires the exact credential and
    grants no authority on its own;
  - tamper / forgery / expiry / actor / dispatch refusal;
  - a stable plan fingerprint for exact-replay matching.

It deliberately does NOT cover ledger execution, the BEGIN IMMEDIATE snapshot
recheck, transport/ledger CAS, CLI wiring, or notifications -- those are the
coupled clusters that remain unbuilt in this turn.
"""
from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import re
import unittest

from agent_comms.cli import settlement_plan as sp
from agent_comms.schema import ValidationError

SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
OTHER_SECRET = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
ACTOR = "01M36YTJV9XBW95S6ZWV47C4RG"
DISPATCH = "dispatch_20260715_120000_deadbeef"
ISSUED = "2026-07-15T12:00:00+00:00"
NONCE = "8f14e45fceea167a5a36dedd4bea2543"
SNAPSHOT = {
    "producer_actor_id": "alpha-architect",
    "recipient_actor_id": "alpha-worker",
    "dispatch_status": "in_flight",
    "transport_status": "sent",
    "spawn_handle": "sup:alpha-worker:dispatch_20260715_120000_deadbeef",
    "run_token_fingerprint": "abc123",
    "cancellation_request": {
        "state": "requested",
        "authority": "admin",
        "requested_by": "01M36YTJV9XBW95S6ZWV47C4RG",
        "reason": "release blocked lineage",
    },
}


def build(**overrides):
    kwargs = dict(
        secret=SECRET,
        actor_id=ACTOR,
        dispatch_id=DISPATCH,
        reason="release blocked lineage; termination remains unconfirmed",
        snapshot=SNAPSHOT,
        issued_at=ISSUED,
        nonce=NONCE,
    )
    kwargs.update(overrides)
    return sp.build_plan(**kwargs)


class SettlementPlanShapeTest(unittest.TestCase):
    def test_wire_form_is_shell_safe_v1_unpadded_base64url(self) -> None:
        plan = build()
        # Exactly three dot-separated segments, version-prefixed.
        self.assertRegex(plan, r"^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
        # base64url, unpadded: no '=' padding and none of '+' '/' present.
        self.assertNotIn("=", plan)
        self.assertNotIn("+", plan)
        self.assertNotIn("/", plan)
        # No shell metacharacters anywhere in the single copy/paste token.
        self.assertFalse(re.search(r"[\s'\"$`\\;|&<>()*?!#]", plan))

    def test_roundtrip_returns_exact_bound_fields(self) -> None:
        plan = build()
        claim = sp.verify_plan(secret=SECRET, plan=plan, now=ISSUED)
        self.assertEqual(claim["version"], "v1")
        self.assertEqual(claim["actor_id"], ACTOR)
        self.assertEqual(claim["dispatch_id"], DISPATCH)
        self.assertEqual(claim["reason"], "release blocked lineage; termination remains unconfirmed")
        self.assertEqual(claim["snapshot"], SNAPSHOT)
        self.assertEqual(claim["issued_at"], ISSUED)
        self.assertEqual(claim["nonce"], NONCE)

    def test_expiry_is_exactly_five_minutes_after_issue(self) -> None:
        claim = sp.verify_plan(secret=SECRET, plan=build(), now=ISSUED)
        self.assertEqual(claim["expiry_at"], "2026-07-15T12:05:00+00:00")
        self.assertEqual(sp.PLAN_TTL_SECONDS, 300)


class SettlementPlanExpiryTest(unittest.TestCase):
    def test_verify_ok_up_to_and_including_the_expiry_instant(self) -> None:
        plan = build()
        # One second before expiry and exactly at expiry both verify.
        sp.verify_plan(secret=SECRET, plan=plan, now="2026-07-15T12:04:59+00:00")
        sp.verify_plan(secret=SECRET, plan=plan, now="2026-07-15T12:05:00+00:00")

    def test_verify_refuses_after_expiry(self) -> None:
        plan = build()
        with self.assertRaises(sp.PlanExpiredError):
            sp.verify_plan(secret=SECRET, plan=plan, now="2026-07-15T12:05:01+00:00")


class SettlementPlanIntegrityTest(unittest.TestCase):
    def test_wrong_credential_grants_no_authority(self) -> None:
        # The plan is integrity-sealed, not bearer authorization: the exact
        # admin credential is separately required to verify it.
        plan = build()
        with self.assertRaises(sp.PlanSignatureError):
            sp.verify_plan(secret=OTHER_SECRET, plan=plan, now=ISSUED)

    def test_tampered_payload_refuses(self) -> None:
        version, payload, sig = build().split(".")
        flipped = payload[:-1] + ("A" if payload[-1] != "A" else "B")
        with self.assertRaises(sp.PlanSignatureError):
            sp.verify_plan(secret=SECRET, plan=f"{version}.{flipped}.{sig}", now=ISSUED)

    def test_tampered_signature_refuses(self) -> None:
        version, payload, sig = build().split(".")
        flipped = sig[:-1] + ("A" if sig[-1] != "A" else "B")
        with self.assertRaises(sp.PlanSignatureError):
            sp.verify_plan(secret=SECRET, plan=f"{version}.{payload}.{flipped}", now=ISSUED)

    def test_forged_signature_under_attacker_key_refuses(self) -> None:
        forged = sp.build_plan(
            secret=OTHER_SECRET,
            actor_id=ACTOR,
            dispatch_id=DISPATCH,
            reason="malicious",
            snapshot=SNAPSHOT,
            issued_at=ISSUED,
            nonce=NONCE,
        )
        with self.assertRaises(sp.PlanSignatureError):
            sp.verify_plan(secret=SECRET, plan=forged, now=ISSUED)

    def test_version_swap_refuses(self) -> None:
        _version, payload, sig = build().split(".")
        with self.assertRaises(sp.PlanFormatError):
            sp.verify_plan(secret=SECRET, plan=f"v2.{payload}.{sig}", now=ISSUED)

    def test_malformed_segment_count_refuses(self) -> None:
        for bad in ("", "v1", "v1.onlytwo", "v1.a.b.c", "not-a-plan"):
            with self.subTest(bad=bad):
                with self.assertRaises(sp.PlanFormatError):
                    sp.verify_plan(secret=SECRET, plan=bad, now=ISSUED)

    def test_snapshot_drift_in_payload_breaks_signature(self) -> None:
        # Re-signing is impossible without the credential; editing the bound
        # snapshot without the key is caught as a signature failure.
        plan = build()
        version, payload, sig = plan.split(".")
        import base64
        import json

        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        doc = json.loads(raw)
        doc["snapshot"]["dispatch_status"] = "queued"
        mutated = base64.urlsafe_b64encode(
            json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()
        ).rstrip(b"=").decode()
        with self.assertRaises(sp.PlanSignatureError):
            sp.verify_plan(secret=SECRET, plan=f"{version}.{mutated}.{sig}", now=ISSUED)


class SettlementPlanStrictBase64UrlTest(unittest.TestCase):
    """A plan segment must be STRICT canonical unpadded base64url.

    The architect reproduced a decoding defect: because the old guard only
    rejected the three characters ``+ / =`` and then decoded with the tolerant
    (``validate=False``) decoder, appending non-alphabet punctuation to a
    segment was silently discarded and the plan still verified. Strict decoding
    must refuse any non-alphabet character, any padding, an invalid length, and
    every decodable-but-noncanonical representation, for BOTH the payload and the
    signature segment.
    """

    def _canonical(self):
        version, payload, sig = build().split(".")
        return version, payload, sig

    def test_signature_with_appended_punctuation_refuses(self) -> None:
        # Reproduced defect: four exclamation marks appended to the signature.
        version, payload, sig = self._canonical()
        with self.assertRaises(sp.SettlementPlanError):
            sp.verify_plan(secret=SECRET, plan=f"{version}.{payload}.{sig}!!!!", now=ISSUED)

    def test_payload_with_appended_punctuation_refuses(self) -> None:
        version, payload, sig = self._canonical()
        with self.assertRaises(sp.PlanFormatError):
            sp.verify_plan(secret=SECRET, plan=f"{version}.{payload}!!!!.{sig}", now=ISSUED)

    def test_single_appended_punctuation_char_refuses(self) -> None:
        # Even one stray non-alphabet byte on either segment must refuse.
        version, payload, sig = self._canonical()
        with self.assertRaises(sp.SettlementPlanError):
            sp.verify_plan(secret=SECRET, plan=f"{version}.{payload}.{sig}!", now=ISSUED)
        with self.assertRaises(sp.PlanFormatError):
            sp.verify_plan(secret=SECRET, plan=f"{version}.{payload}!.{sig}", now=ISSUED)

    def test_padding_equals_refuses(self) -> None:
        version, payload, sig = self._canonical()
        with self.assertRaises(sp.SettlementPlanError):
            sp.verify_plan(secret=SECRET, plan=f"{version}.{payload}.{sig}=", now=ISSUED)
        with self.assertRaises(sp.PlanFormatError):
            sp.verify_plan(secret=SECRET, plan=f"{version}.{payload}=.{sig}", now=ISSUED)

    def test_standard_base64_plus_slash_refuses(self) -> None:
        # A '+' or '/' (standard, non-url base64) must not be accepted.
        version, payload, sig = self._canonical()
        for bad in (payload[:-1] + "+", payload[:-1] + "/"):
            with self.subTest(bad=bad[-1]):
                with self.assertRaises(sp.PlanFormatError):
                    sp.verify_plan(secret=SECRET, plan=f"{version}.{bad}.{sig}", now=ISSUED)

    def test_whitespace_in_segment_refuses(self) -> None:
        version, payload, sig = self._canonical()
        with self.assertRaises(sp.PlanFormatError):
            sp.verify_plan(secret=SECRET, plan=f"{version}. {payload}.{sig}", now=ISSUED)

    @staticmethod
    def _noncanonical_variant(segment: str) -> str | None:
        """A base64url string that decodes to the same bytes but is not canonical.

        For a segment whose final group has unused low bits, several final
        characters decode to the identical byte string; only one is canonical
        (unused bits zero). Return a non-canonical sibling, or None if none.
        """
        import base64

        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for char in alphabet:
            candidate = segment[:-1] + char
            if candidate == segment:
                continue
            try:
                decoded = base64.urlsafe_b64decode(candidate + "=" * (-len(candidate) % 4))
            except Exception:
                continue
            if decoded == raw:
                return candidate
        return None

    def test_noncanonical_signature_refuses(self) -> None:
        # A signature that decodes to the SAME bytes but flips otherwise-unused
        # trailing bits is a decodable-but-noncanonical representation. It must
        # refuse rather than verify as if it were the canonical signature.
        version, payload, sig = self._canonical()
        variant = self._noncanonical_variant(sig)
        self.assertIsNotNone(variant, "expected a noncanonical sibling to exist for the signature")
        self.assertNotEqual(variant, sig)
        with self.assertRaises(sp.SettlementPlanError):
            sp.verify_plan(secret=SECRET, plan=f"{version}.{payload}.{variant}", now=ISSUED)

    def test_noncanonical_payload_refuses(self) -> None:
        version, payload, sig = self._canonical()
        variant = self._noncanonical_variant(payload)
        self.assertIsNotNone(variant, "expected a noncanonical sibling to exist for the payload")
        with self.assertRaises(sp.PlanFormatError):
            sp.verify_plan(secret=SECRET, plan=f"{version}.{variant}.{sig}", now=ISSUED)


class SettlementPlanCanonicalJsonTest(unittest.TestCase):
    """A correctly-signed but noncanonical JSON payload must refuse.

    Defence in depth beyond base64url canonicality: after decoding, the payload
    bytes must equal the canonical JSON re-encoding. A reordered/whitespace
    payload (even when correctly HMAC-signed with the real credential) is refused
    so exactly one canonical wire form exists per logical plan.
    """

    def _signed(self, payload_bytes: bytes) -> str:
        payload_seg = sp._b64url_nopad(payload_bytes)
        sig_seg = sp._sign(SECRET.encode("utf-8"), payload_seg)
        return f"{sp.PLAN_VERSION}.{payload_seg}.{sig_seg}"

    def test_signed_whitespace_padded_payload_refuses(self) -> None:
        import json

        canonical = sp.verify_plan(secret=SECRET, plan=build(), now=ISSUED)
        spaced = json.dumps(canonical, separators=(", ", ": "), sort_keys=True).encode("utf-8")
        self.assertNotEqual(spaced, json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode())
        plan = self._signed(spaced)
        with self.assertRaises(sp.PlanFormatError):
            sp.verify_plan(secret=SECRET, plan=plan, now=ISSUED)

    def test_signed_reordered_keys_payload_refuses(self) -> None:
        import json

        canonical = sp.verify_plan(secret=SECRET, plan=build(), now=ISSUED)
        # Reverse key order (top level) -> valid JSON, same content, noncanonical.
        reordered = {key: canonical[key] for key in reversed(list(canonical))}
        reordered_bytes = json.dumps(reordered, separators=(",", ":"), sort_keys=False).encode("utf-8")
        self.assertNotEqual(
            reordered_bytes, json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()
        )
        plan = self._signed(reordered_bytes)
        with self.assertRaises(sp.PlanFormatError):
            sp.verify_plan(secret=SECRET, plan=plan, now=ISSUED)


class SettlementPlanDeferredExpiryTest(unittest.TestCase):
    """``now=None`` defers expiry (for exact replay) but keeps every other check."""

    def test_now_none_skips_expiry_but_enforces_signature_and_binding(self) -> None:
        plan = build()
        claim = sp.verify_plan(
            secret=SECRET, plan=plan, now=None,
            expected_actor_id=ACTOR, expected_dispatch_id=DISPATCH,
        )
        self.assertEqual(claim["dispatch_id"], DISPATCH)
        # Even with now=None a bad signature / actor mismatch still refuses.
        version, payload, sig = plan.split(".")
        flipped = sig[:-1] + ("A" if sig[-1] != "A" else "B")
        with self.assertRaises(sp.PlanSignatureError):
            sp.verify_plan(secret=SECRET, plan=f"{version}.{payload}.{flipped}", now=None)
        with self.assertRaises(sp.PlanMismatchError):
            sp.verify_plan(secret=SECRET, plan=plan, now=None, expected_actor_id="other")

    def test_plan_is_expired_helper_boundary(self) -> None:
        claim = sp.verify_plan(secret=SECRET, plan=build(), now=None)
        self.assertFalse(sp.plan_is_expired(claim, "2026-07-15T12:05:00+00:00"))
        self.assertTrue(sp.plan_is_expired(claim, "2026-07-15T12:05:01+00:00"))


class SettlementPlanMismatchTest(unittest.TestCase):
    def test_actor_mismatch_refuses(self) -> None:
        plan = build()
        with self.assertRaises(sp.PlanMismatchError):
            sp.verify_plan(secret=SECRET, plan=plan, now=ISSUED, expected_actor_id="01J00000000000000000000002")

    def test_dispatch_mismatch_refuses(self) -> None:
        plan = build()
        with self.assertRaises(sp.PlanMismatchError):
            sp.verify_plan(secret=SECRET, plan=plan, now=ISSUED, expected_dispatch_id="dispatch_other")

    def test_matching_actor_and_dispatch_accepts(self) -> None:
        plan = build()
        claim = sp.verify_plan(
            secret=SECRET,
            plan=plan,
            now=ISSUED,
            expected_actor_id=ACTOR,
            expected_dispatch_id=DISPATCH,
        )
        self.assertEqual(claim["dispatch_id"], DISPATCH)


class SettlementReasonTest(unittest.TestCase):
    def test_reason_is_normalized_and_bound_in_the_plan(self) -> None:
        claim = sp.verify_plan(secret=SECRET, plan=build(reason="  spaced reason  "), now=ISSUED)
        self.assertEqual(claim["reason"], "spaced reason")

    def test_empty_reason_refused(self) -> None:
        for bad in ("", "   ", "\n\t"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValidationError):
                    build(reason=bad)

    def test_oversized_reason_refused(self) -> None:
        with self.assertRaises(ValidationError):
            build(reason="x" * (sp.MAX_SETTLEMENT_REASON_CHARS + 1))


class SettlementFingerprintTest(unittest.TestCase):
    def test_fingerprint_stable_for_same_plan_and_differs_across_plans(self) -> None:
        plan = build()
        self.assertEqual(sp.plan_fingerprint(plan), sp.plan_fingerprint(plan))
        other = build(nonce="00000000000000000000000000000000")
        self.assertNotEqual(sp.plan_fingerprint(plan), sp.plan_fingerprint(other))

    def test_fingerprint_is_hex_sha256(self) -> None:
        self.assertRegex(sp.plan_fingerprint(build()), r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
