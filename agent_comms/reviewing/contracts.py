"""State and schema contracts for the review tool.

Holds the shared state, finding, and check vocabularies, the record and path
types, and the schema-1/schema-2 record, land-attempt, closeout, delta,
artifact, and generic-object validation used behind the ``agent_comms.review``
facade. This module is a leaf: it depends only on the standard library,
``agent_comms.delta_manifest``, and ``agent_comms.schema``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from agent_comms import delta_manifest


SCHEMA_VERSION = 2
LAND_UNRESOLVABLE_HEAD = "unresolvable-head"

# Contract 18: the optional, presence-gated v2 destination trio bound into a cycle
# approval object. All three or none; a present trio is shape-checked here (owned by
# reviewing/approval.py for signing/verification). These constants are the single
# source for the v2 payload version and repo-identity prefix.
CYCLE_PAYLOAD_VERSION = "agent-comms-approval-v2"
LOCAL_WORKTREE_PREFIX = "local-worktree-v1:"
CYCLE_APPROVAL_TRIO = ("payload_version", "repo_identity", "target_ref")


def _prefixed_dest_field(value: Any, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and len(value) > len(prefix)
        and "\n" not in value
        and "\r" not in value
    )


def _validate_cycle_approval_trio(approval: dict[str, Any], label: str) -> None:
    present = [name for name in CYCLE_APPROVAL_TRIO if name in approval]
    if not present:
        return
    if len(present) != len(CYCLE_APPROVAL_TRIO):
        raise ReviewError(f"{label} v2 destination binding must be all-or-none")
    if approval["payload_version"] != CYCLE_PAYLOAD_VERSION:
        raise ReviewError(f"invalid {label} payload_version")
    if not _prefixed_dest_field(approval["repo_identity"], LOCAL_WORKTREE_PREFIX):
        raise ReviewError(f"invalid {label} repo_identity")
    if not _prefixed_dest_field(approval["target_ref"], "refs/heads/"):
        raise ReviewError(f"invalid {label} target_ref")


class StateSemantics(NamedTuple):
    brief_revisable: bool
    execution_evidence: bool


STATE_SEMANTICS = {
    "drafted_brief": StateSemantics(False, False),
    "brief_revised": StateSemantics(False, False),
    "brief_reviewed": StateSemantics(True, False),
    "dispatched": StateSemantics(True, False),
    "dispatch_blocked": StateSemantics(True, False),
    "dispatch_superseded": StateSemantics(True, False),
    "escalated": StateSemantics(False, False),
    "executed": StateSemantics(True, True),
    "execution_reviewed": StateSemantics(True, True),
    "review_clean": StateSemantics(True, True),
    "human_approved": StateSemantics(False, True),
    "merge_eligible": StateSemantics(False, True),
    "merged": StateSemantics(False, True),
    "verified": StateSemantics(False, True),
}
REVIEW_STATES = frozenset(STATE_SEMANTICS)
PRE_APPROVAL_STATES = frozenset(
    state for state, semantics in STATE_SEMANTICS.items() if semantics.brief_revisable
)
WORKER_DISPATCH_ID_RE = re.compile(r"^dispatch_[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")
# Contract-17 round companions: the COMPLETE canonical identity
# (``intents.PAYLOAD_FIELDS`` sans payload_version/key) plus the derived
# intent id/digest.
_INTENT_COMPANION_STRS = (
    "intent_id",
    "producer_actor_id",
    "recipient_actor_id",
    "real_project_root",
    "policy_name",
    "policy_version",
    "record_id",
    "source_branch",
)
_INTENT_COMPANION_HEX = {
    "source_head": 40,
    "source_tree": 40,
    "integration_head": 40,
    "integration_tree": 40,
    "base_commit": 40,
    "base_tree": 40,
    "brief_sha256": 64,
    "dod_sha256": 64,
    "intent_digest": 64,
}
INTENT_COMPANION_FIELDS = (
    "round_kind",
    *_INTENT_COMPANION_STRS,
    *_INTENT_COMPANION_HEX,
)
FINDING_SEVERITIES = {"blocking", "should", "nit"}
FINDING_STATUSES = {"open", "resolved", "deferred"}
EXECUTABLE_CHECK_IDS = frozenset({"green", "fail", "unittest", "extra"})
EVIDENCE_ONLY_CHECK_IDS = frozenset({"runtime-cert"})
EXECUTION_BOUND_STATES = frozenset(
    state
    for state, semantics in STATE_SEMANTICS.items()
    if semantics.execution_evidence
)


class ReviewError(Exception):
    pass


@dataclass
class Paths:
    json: Path
    lock: Path
    summary: Path


def is_evidence_only(criterion: dict[str, Any]) -> bool:
    return criterion.get("check_id") in EVIDENCE_ONLY_CHECK_IDS


def file_sha256(path: Path, context: str) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise ReviewError(f"{context}: cannot read evidence log {path}: {exc}") from exc


def _validate_intent_key(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 200:
        raise ReviewError(
            "idempotency_key must be non-empty and at most 200 characters"
        )
    return value


def validate_schema1_record(record: dict[str, Any]) -> None:
    """Validate the final historical schema-1 read contract, without upgrade."""
    required = {
        "schema_version",
        "dispatch_id",
        "state",
        "repo",
        "brief_path",
        "dod",
        "findings",
        "gate_runs",
    }
    missing = required - set(record)
    if missing:
        raise ReviewError(f"schema missing fields: {', '.join(sorted(missing))}")
    if not isinstance(record["dod"], list):
        raise ReviewError("dod must be a list")
    if not isinstance(record["findings"], list):
        raise ReviewError("findings must be a list")
    if not isinstance(record["gate_runs"], list):
        raise ReviewError("gate_runs must be a list")
    for finding in record["findings"]:
        if not isinstance(finding, dict):
            raise ReviewError("finding must be an object")
        if finding.get("severity") not in FINDING_SEVERITIES:
            raise ReviewError("invalid finding severity")
        if finding.get("status") not in FINDING_STATUSES:
            raise ReviewError("invalid finding status")
    # Schema-1 stays a loose historical reader: it does not newly require an approval
    # object or reject unrelated members. Only a present v2-trio member triggers the
    # same all-or-none and v2 shape checks (contract 18).
    if isinstance(record.get("approval"), dict):
        _validate_cycle_approval_trio(record["approval"], "approval")


def validate_schema2_record(record: dict[str, Any]) -> None:
    if not isinstance(record, dict):
        raise ReviewError("record must be an object")
    required = {
        "schema_version",
        "dispatch_id",
        "state",
        "repo",
        "base_commit",
        "reviewed_head",
        "approved_head",
        "target_branch",
        "expected_producer",
        "expected_recipient",
        "intended_dispatches",
        "worker_evidence",
        "blocked_dispatches",
        "superseded_dispatches",
        "blocked_redispatch_count",
        "max_blocked_redispatches",
        "brief_path",
        "brief_sha256",
        "brief_revision",
        "brief_checks",
        "dod",
        "findings",
        "gate_runs",
        "skips",
        "approval",
        "respawn_count",
        "max_respawns",
        "history",
        "created_at",
        "updated_at",
    }
    optional = {
        "gate_epoch",
        "trigger_closed",
        "prior_state",
        "state_before_brief_revised",
        "escalation",
        "unblock",
        "verification",
        "superseded_approvals",
        "dod_path",
        "dod_sha256",
        # Additive, optional post-merge land evidence (schema v2, no SCHEMA_VERSION
        # bump). Absence is a legacy empty list; a record written before landing
        # existed validates unchanged.
        "land_attempts",
    }
    missing = required - set(record)
    if missing:
        raise ReviewError(f"schema missing fields: {', '.join(sorted(missing))}")
    unknown = set(record) - required - optional
    if unknown:
        raise ReviewError(f"schema unknown fields: {', '.join(sorted(unknown))}")
    for name in (
        "dispatch_id",
        "repo",
        "brief_path",
        "expected_producer",
        "expected_recipient",
        "created_at",
        "updated_at",
    ):
        if not isinstance(record.get(name), str) or not record[name].strip():
            raise ReviewError(f"schema missing or invalid {name}")
    if record.get("state") not in REVIEW_STATES:
        raise ReviewError("invalid state")
    for name in (
        "blocked_redispatch_count",
        "max_blocked_redispatches",
        "brief_revision",
        "respawn_count",
        "max_respawns",
        "gate_epoch",
    ):
        if name not in record and name == "gate_epoch":
            continue
        value = record.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReviewError(f"{name} must be a non-negative integer")
    if record["blocked_redispatch_count"] > record["max_blocked_redispatches"]:
        raise ReviewError("blocked_redispatch_count exceeds maximum")
    if record["respawn_count"] > record["max_respawns"]:
        raise ReviewError("respawn_count exceeds maximum")
    for name in ("base_commit", "reviewed_head", "approved_head"):
        value = record.get(name)
        if value is not None and (
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None
        ):
            raise ReviewError(f"invalid {name}")
    if record.get("target_branch") is not None and (
        not isinstance(record["target_branch"], str)
        or not record["target_branch"].strip()
    ):
        raise ReviewError("invalid target_branch")
    if record["brief_sha256"] is not None and (
        not isinstance(record["brief_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", record["brief_sha256"]) is None
    ):
        raise ReviewError("invalid brief_sha256")
    if record["approval"] is not None:
        _validate_object(
            record["approval"],
            "approval",
            required=("approver", "mechanism", "signature"),
            optional=("timestamp", "approved_head", *CYCLE_APPROVAL_TRIO),
        )
        _require_strings(
            record["approval"], "approval", ("approver", "mechanism", "signature")
        )
        for name in ("timestamp", "approved_head"):
            if name in record["approval"] and (
                not isinstance(record["approval"][name], str)
                or not record["approval"][name]
            ):
                raise ReviewError(f"invalid approval {name}")
        _validate_cycle_approval_trio(record["approval"], "approval")
    if not isinstance(record["dod"], list):
        raise ReviewError("dod must be a list")
    if not isinstance(record["findings"], list):
        raise ReviewError("findings must be a list")
    if not isinstance(record["gate_runs"], list):
        raise ReviewError("gate_runs must be a list")
    for finding in record["findings"]:
        _validate_object(
            finding,
            "finding",
            required=(
                "id",
                "severity",
                "loc",
                "problem",
                "impact",
                "fix",
                "status",
                "resolved_by_dispatch_id",
                "resolution_note",
            ),
            optional=("respawn_note",),
        )
        _require_strings(finding, "finding", ("id", "loc", "problem", "impact", "fix"))
        if finding.get("severity") not in FINDING_SEVERITIES:
            raise ReviewError("invalid finding severity")
        if finding.get("status") not in FINDING_STATUSES:
            raise ReviewError("invalid finding status")
        for name in ("resolved_by_dispatch_id", "resolution_note", "respawn_note"):
            if (
                name in finding
                and finding[name] is not None
                and not isinstance(finding[name], str)
            ):
                raise ReviewError(f"invalid finding {name}")
    list_fields = (
        "brief_checks",
        "intended_dispatches",
        "worker_evidence",
        "blocked_dispatches",
        "superseded_dispatches",
        "skips",
        "history",
    )
    for name in list_fields:
        if not isinstance(record.get(name), list):
            raise ReviewError(f"{name} must be a list")
    for index, intent in enumerate(record["intended_dispatches"], 1):
        # ``respawn_dispatch_id`` is additive and optional: a correction intent
        # recorded by respawn binds the exact linked worker dispatch, while
        # historical records without it stay valid without a schema bump.
        _validate_object(
            intent,
            "intended_dispatch",
            required=("attempt", "idempotency_key", "recorded_at"),
            optional=("note", "respawn_dispatch_id", *INTENT_COMPANION_FIELDS),
        )
        if intent.get("attempt") != index:
            raise ReviewError("invalid intended_dispatches attempt sequence")
        _validate_intent_key(intent.get("idempotency_key", ""))
        if not isinstance(intent.get("recorded_at"), str) or not intent["recorded_at"]:
            raise ReviewError("invalid intended_dispatches recorded_at")
        if "note" in intent and not isinstance(intent["note"], str):
            raise ReviewError("invalid intended_dispatches note")
        # Contract-17 round companions: optional for legacy, else all-or-nothing
        # and in-shape with intent_id the digest-derived id (agrees with SQL row).
        if any(name in intent for name in INTENT_COMPANION_FIELDS):
            if (
                any(name not in intent for name in INTENT_COMPANION_FIELDS)
                or intent.get("round_kind") != "implementation"
                or intent.get("intent_id") != f"rvi_{intent.get('intent_digest')}"
            ):
                raise ReviewError("invalid intended_dispatches intent binding")
            _require_strings(intent, "intended_dispatch", _INTENT_COMPANION_STRS)
            for _name, _width in _INTENT_COMPANION_HEX.items():
                if (
                    re.fullmatch(rf"[0-9a-f]{{{_width}}}", str(intent.get(_name)))
                    is None
                ):
                    raise ReviewError("invalid intended_dispatches intent binding")
        if "respawn_dispatch_id" in intent and (
            not isinstance(intent["respawn_dispatch_id"], str)
            or not WORKER_DISPATCH_ID_RE.fullmatch(intent["respawn_dispatch_id"])
        ):
            raise ReviewError("invalid intended_dispatches respawn_dispatch_id")
    for name in ("worker_evidence", "blocked_dispatches", "superseded_dispatches"):
        for item in record[name]:
            common = (
                "worker_dispatch_id",
                "intent_attempt",
                "idempotency_key",
                "ledger_db",
                "closeout",
                "verified_at",
            )
            extra = (
                (
                    "producer",
                    "recipient",
                    "status",
                    "result",
                    "delta_verification",
                    "artifact_bindings",
                )
                if name == "worker_evidence"
                else ("note",)
            )
            _validate_object(item, name, required=common, optional=extra)
            if not WORKER_DISPATCH_ID_RE.fullmatch(
                str(item.get("worker_dispatch_id", ""))
            ):
                raise ReviewError(f"invalid {name} entry")
            if (
                isinstance(item["intent_attempt"], bool)
                or not isinstance(item["intent_attempt"], int)
                or item["intent_attempt"] < 1
            ):
                raise ReviewError(f"invalid {name} intent_attempt")
            if item["intent_attempt"] > len(record["intended_dispatches"]):
                raise ReviewError(f"invalid {name} intent_attempt")
            _validate_intent_key(item["idempotency_key"])
            if not all(
                isinstance(item[key], str) and item[key]
                for key in ("ledger_db", "verified_at")
            ):
                raise ReviewError(f"invalid {name} entry")
            _validate_closeout(item["closeout"])
            if name == "worker_evidence":
                _require_strings(item, "worker_evidence", ("producer", "recipient"))
                if item["status"] != "closed" or item["result"] != "satisfied":
                    raise ReviewError("invalid worker_evidence result")
                _validate_delta_verification(item["delta_verification"])
                _validate_artifacts(item["artifact_bindings"])
            elif not isinstance(item["note"], str):
                raise ReviewError(f"invalid {name} note")
    for criterion in record["dod"]:
        _validate_object(
            criterion,
            "dod",
            required=(
                "id",
                "claim",
                "check_id",
                "expected",
                "scope",
                "evidence",
                "required",
            ),
            optional=("argv", "evidence_payload", "detached_evidence"),
        )
        _require_strings(criterion, "dod", ("id", "claim", "check_id"))
        if criterion["check_id"] not in EXECUTABLE_CHECK_IDS | EVIDENCE_ONLY_CHECK_IDS:
            raise ReviewError("invalid dod check_id")
        if not isinstance(criterion["required"], bool):
            raise ReviewError("invalid dod required")
        if "argv" in criterion and (
            not isinstance(criterion["argv"], list)
            or any(not isinstance(value, str) for value in criterion["argv"])
        ):
            raise ReviewError("invalid dod argv")
        if "evidence_payload" in criterion:
            payload = criterion["evidence_payload"]
            _validate_object(
                payload,
                "evidence_payload",
                required=(
                    "log_path",
                    "log_sha256",
                    "runtime_version",
                    "counts",
                    "head",
                    "attached_at",
                    "by",
                ),
            )
            _require_strings(
                payload,
                "evidence_payload",
                (
                    "log_path",
                    "log_sha256",
                    "runtime_version",
                    "counts",
                    "head",
                    "attached_at",
                    "by",
                ),
            )
            if (
                re.fullmatch(r"[0-9a-f]{64}", payload["log_sha256"]) is None
                or re.fullmatch(r"[0-9a-f]{40}", payload["head"]) is None
            ):
                raise ReviewError("invalid evidence_payload digest")
    for check in record["brief_checks"]:
        # ``dod_sha256`` is the contract-17 reviewed-pair binding; checks
        # recorded by earlier contracts stay valid without it.
        _validate_object(
            check,
            "brief_check",
            required=(
                "by",
                "verdict",
                "finding",
                "surface_verdict",
                "surface_reason",
                "timestamp",
                "brief_sha256",
            ),
            optional=("dod_sha256",),
        )
        if "dod_sha256" in check and (
            not isinstance(check["dod_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", check["dod_sha256"]) is None
        ):
            raise ReviewError("invalid brief_check dod_sha256")
        _require_strings(
            check, "brief_check", ("by", "surface_reason", "timestamp", "brief_sha256")
        )
        if check["verdict"] not in {"clean", "finding"} or check[
            "surface_verdict"
        ] not in {"complete", "incomplete"}:
            raise ReviewError("invalid brief_check vocabulary")
        if check["finding"] is not None and not isinstance(check["finding"], str):
            raise ReviewError("invalid brief_check finding")
    for run in record["gate_runs"]:
        _validate_object(
            run,
            "gate_run",
            required=(
                "check_id",
                "argv_or_registry_name",
                "cwd",
                "git_head",
                "branch",
                "env_policy",
                "started_at",
                "ended_at",
                "timeout_s",
                "exit_code",
                "stdout_excerpt",
                "stderr_excerpt",
                "verdict",
            ),
            optional=("epoch",),
        )
        _require_strings(
            run,
            "gate_run",
            (
                "check_id",
                "argv_or_registry_name",
                "cwd",
                "env_policy",
                "started_at",
                "ended_at",
            ),
        )
        for key in ("stdout_excerpt", "stderr_excerpt"):
            if not isinstance(run[key], str):
                raise ReviewError(f"invalid gate_run {key}")
        for key in ("git_head", "branch"):
            if run[key] is not None and not isinstance(run[key], str):
                raise ReviewError(f"invalid gate_run {key}")
        if run["verdict"] not in {"pass", "fail", "timeout"}:
            raise ReviewError("invalid gate_run verdict")
        for key in ("timeout_s", "exit_code", "epoch"):
            if (
                key in run
                and run[key] is not None
                and (isinstance(run[key], bool) or not isinstance(run[key], int))
            ):
                raise ReviewError(f"invalid gate_run {key}")
    for skip in record["skips"]:
        _validate_object(
            skip,
            "skip",
            required=("check_id", "reason", "risk", "actor", "timestamp"),
            optional=("epoch",),
        )
        _require_strings(
            skip, "skip", ("check_id", "reason", "risk", "actor", "timestamp")
        )
        if "epoch" in skip and (
            isinstance(skip["epoch"], bool)
            or not isinstance(skip["epoch"], int)
            or skip["epoch"] < 0
        ):
            raise ReviewError("invalid skip epoch")
    for entry in record["history"]:
        _validate_object(entry, "history", required=("event",), allow_unknown=True)
        _require_strings(entry, "history", ("event",))
        if "timestamp" in entry and (
            not isinstance(entry["timestamp"], str) or not entry["timestamp"]
        ):
            raise ReviewError("invalid history timestamp")
    if (
        record["state"] in {"dispatch_blocked", "dispatch_superseded"}
        and not record["intended_dispatches"]
    ):
        raise ReviewError("state requires intended_dispatches")
    if record["state"] == "dispatch_blocked" and not record["blocked_dispatches"]:
        raise ReviewError("dispatch_blocked state requires blocked_dispatches")
    if record["state"] == "dispatch_superseded" and not record["superseded_dispatches"]:
        raise ReviewError("dispatch_superseded state requires superseded_dispatches")
    prior_state = record.get("state_before_brief_revised")
    if "state_before_brief_revised" in record and (
        not isinstance(prior_state, str) or prior_state not in PRE_APPROVAL_STATES
    ):
        raise ReviewError("invalid state_before_brief_revised")
    execution_lineage = (
        record["state"] in EXECUTION_BOUND_STATES
        or prior_state in EXECUTION_BOUND_STATES
        or any(entry.get("event") == "mark-executed" for entry in record["history"])
    )
    evidence_bound = bool(record["worker_evidence"])
    trigger_closed = record.get("trigger_closed") is True
    reviewed_head_bound = record["reviewed_head"] is not None
    if execution_lineage and not (
        evidence_bound and trigger_closed and reviewed_head_bound
    ):
        raise ReviewError(
            "execution lineage requires reviewed_head, worker_evidence, and trigger_closed"
        )
    if any((evidence_bound, trigger_closed, reviewed_head_bound)) and not all(
        (evidence_bound, trigger_closed, reviewed_head_bound)
    ):
        raise ReviewError("partial execution evidence binding")
    _validate_land_attempts(record)


def _validate_land_attempts(record: dict[str, Any]) -> None:
    """Validate the additive, optional, append-only ``land_attempts`` audit.

    Absence is a legacy empty list. Every attempt -- red included -- binds the
    integration heads it observed, the clean-tree observations, the per-leg
    argv/interpreter/exit code, the environment policy (names removed and the
    isolated supervisor root, never secret values), the interpreter/dependency
    versions, and the transcript path plus its SHA-256, so a stored green attempt
    can only be reused while every bound fact still validates.
    """
    attempts = record.get("land_attempts")
    if attempts is None:
        return
    if not isinstance(attempts, list):
        raise ReviewError("land_attempts must be a list")
    seen_ids: set[str] = set()
    for attempt in attempts:
        _validate_object(
            attempt,
            "land_attempt",
            required=(
                "attempt_id",
                "dispatch_id",
                "started_at",
                "ended_at",
                "integration_head_before",
                "merge_state",
                "clean_before",
                "clean_after",
                "interpreter",
                "dependency",
                "env_policy",
                "legs",
                "transcript_path",
                "transcript_sha256",
                "verdict",
            ),
            optional=(
                "note",
                "source_branch",
                "reviewed_head",
                "integration_head_after",
            ),
        )
        _require_strings(
            attempt,
            "land_attempt",
            (
                "attempt_id",
                "dispatch_id",
                "started_at",
                "ended_at",
                "interpreter",
                "dependency",
                "transcript_path",
            ),
        )
        if attempt["attempt_id"] in seen_ids:
            raise ReviewError("duplicate land_attempt attempt_id")
        seen_ids.add(attempt["attempt_id"])
        # Every attempt -- red included -- is bound to its enclosing review
        # record's cycle. Evidence copied from a different cycle is rejected here,
        # so a cross-cycle replay cannot even be read back as a valid record.
        if attempt["dispatch_id"] != record.get("dispatch_id"):
            raise ReviewError(
                "land_attempt dispatch_id does not match record dispatch_id"
            )
        # ``integration_head_before`` is ALWAYS a real observed 40-hex commit: it
        # is the head the attempt started from, so the explicit unresolvable-head
        # marker is never a truthful value there and is rejected outright. That
        # marker is honest ONLY for the post-attempt head, so it is accepted for
        # ``integration_head_after`` alone (never a fabricated hash), where it
        # stays non-reusable because it can never equal a resolved head.
        before = str(attempt.get("integration_head_before", ""))
        if re.fullmatch(r"[0-9a-f]{40}", before) is None:
            raise ReviewError("invalid land_attempt integration_head_before")
        merge_state = attempt.get("merge_state")
        if merge_state not in {"pending", "merged"}:
            raise ReviewError("invalid land_attempt merge_state")
        if merge_state == "pending" and "integration_head_after" in attempt:
            raise ReviewError(
                "pending land_attempt must not have integration_head_after"
            )
        if merge_state == "merged":
            after = str(attempt.get("integration_head_after", ""))
            if (
                re.fullmatch(r"[0-9a-f]{40}", after) is None
                and after != LAND_UNRESOLVABLE_HEAD
            ):
                raise ReviewError("invalid land_attempt integration_head_after")
        if (
            re.fullmatch(r"[0-9a-f]{64}", str(attempt.get("transcript_sha256", "")))
            is None
        ):
            raise ReviewError("invalid land_attempt transcript_sha256")
        for name in ("clean_before", "clean_after"):
            if not isinstance(attempt.get(name), bool):
                raise ReviewError(f"invalid land_attempt {name}")
        if attempt["verdict"] not in {"green", "red"}:
            raise ReviewError("invalid land_attempt verdict")
        for name in ("note", "source_branch", "reviewed_head"):
            if name in attempt and not isinstance(attempt[name], str):
                raise ReviewError(f"invalid land_attempt {name}")
        env_policy = attempt["env_policy"]
        _validate_object(
            env_policy,
            "land_attempt env_policy",
            required=("removed_names", "supervisor_root"),
        )
        if not isinstance(env_policy["removed_names"], list) or any(
            not isinstance(item, str) for item in env_policy["removed_names"]
        ):
            raise ReviewError("invalid land_attempt env_policy removed_names")
        if (
            not isinstance(env_policy["supervisor_root"], str)
            or not env_policy["supervisor_root"]
        ):
            raise ReviewError("invalid land_attempt env_policy supervisor_root")
        legs = attempt["legs"]
        if not isinstance(legs, list) or not legs:
            raise ReviewError("land_attempt requires at least one leg")
        for leg in legs:
            _validate_object(
                leg,
                "land_attempt leg",
                required=("name", "argv", "interpreter", "exit_code"),
                optional=("counts", "dependency"),
            )
            _require_strings(leg, "land_attempt leg", ("name", "interpreter"))
            if (
                not isinstance(leg["argv"], list)
                or not leg["argv"]
                or any(not isinstance(item, str) for item in leg["argv"])
            ):
                raise ReviewError("invalid land_attempt leg argv")
            if isinstance(leg["exit_code"], bool) or not isinstance(
                leg["exit_code"], int
            ):
                raise ReviewError("invalid land_attempt leg exit_code")
            if (
                "counts" in leg
                and leg["counts"] is not None
                and not isinstance(leg["counts"], str)
            ):
                raise ReviewError("invalid land_attempt leg counts")
            if "dependency" in leg and not isinstance(leg["dependency"], str):
                raise ReviewError("invalid land_attempt leg dependency")


def validate_record(record: dict[str, Any]) -> None:
    if not isinstance(record, dict):
        raise ReviewError("record must be an object")
    version = record.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in {1, SCHEMA_VERSION}
    ):
        raise ReviewError(f"unsupported schema_version {version!r}")
    if version == 1:
        validate_schema1_record(record)
    else:
        validate_schema2_record(record)


def _validate_object(
    value: Any,
    label: str,
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
    allow_unknown: bool = False,
) -> None:
    if not isinstance(value, dict):
        raise ReviewError(f"{label} must be an object")
    missing = set(required) - set(value)
    if missing:
        raise ReviewError(f"{label} missing fields: {', '.join(sorted(missing))}")
    unknown = set(value) - set(required) - set(optional)
    if unknown and not allow_unknown:
        raise ReviewError(f"{label} unknown fields: {', '.join(sorted(unknown))}")


def _require_strings(value: dict[str, Any], label: str, names: tuple[str, ...]) -> None:
    for name in names:
        if not isinstance(value.get(name), str) or not value[name]:
            raise ReviewError(f"invalid {label} {name}")


def _validate_closeout(closeout: Any) -> None:
    _validate_object(
        closeout,
        "closeout",
        required=("protocol", "recorded_by", "reply_message_id", "delta"),
        optional=(
            "artifacts",
            "result",
            "summary",
            "blocked_reason",
            "caller_payload_sha256",
            "recorded_at",
        ),
    )
    if closeout["protocol"] != 1:
        raise ReviewError("invalid closeout protocol")
    _require_strings(closeout, "closeout", ("recorded_by", "reply_message_id"))
    if closeout["delta"] is not None and not isinstance(closeout["delta"], dict):
        raise ReviewError("invalid closeout delta")
    if "artifacts" in closeout:
        _validate_artifacts(closeout["artifacts"])
    if "blocked_reason" in closeout:
        blocked_reason = closeout["blocked_reason"]
        if not isinstance(blocked_reason, str) or bool(blocked_reason) != (
            closeout.get("result") == "blocked"
        ):
            raise ReviewError("invalid closeout blocked_reason")
    if "caller_payload_sha256" in closeout and (
        not isinstance(closeout["caller_payload_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", closeout["caller_payload_sha256"]) is None
    ):
        raise ReviewError("invalid closeout caller_payload_sha256")
    if "recorded_at" in closeout and (
        not isinstance(closeout["recorded_at"], str) or not closeout["recorded_at"]
    ):
        raise ReviewError("invalid closeout recorded_at")


def _validate_delta_verification(delta: Any) -> None:
    _validate_object(
        delta,
        "delta_verification",
        required=(
            "snapshot_tree",
            "reviewed_head_tree",
            "manifest_sha256",
            "entries",
            "status_counts",
        ),
    )
    for name, size in (
        ("snapshot_tree", 40),
        ("reviewed_head_tree", 40),
        ("manifest_sha256", 64),
    ):
        if (
            not isinstance(delta[name], str)
            or re.fullmatch(rf"[0-9a-f]{{{size}}}", delta[name]) is None
        ):
            raise ReviewError(f"invalid delta_verification {name}")
    if (
        isinstance(delta["entries"], bool)
        or not isinstance(delta["entries"], int)
        or delta["entries"] < 1
    ):
        raise ReviewError("invalid delta_verification entries")
    status_counts = delta["status_counts"]
    if (
        not isinstance(status_counts, dict)
        or not status_counts
        or any(
            key not in delta_manifest.RAW_GIT_STATUS_CODES
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            for key, value in status_counts.items()
        )
        or sum(status_counts.values()) != delta["entries"]
    ):
        raise ReviewError("invalid delta_verification status_counts")


def _validate_artifacts(artifacts: Any) -> None:
    if not isinstance(artifacts, list):
        raise ReviewError("artifacts must be a list")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ReviewError("artifact must be an object")
        for key in ("real_path", "remeasured_sha256"):
            if (
                key not in artifact
                or not isinstance(artifact[key], str)
                or not artifact[key]
            ):
                raise ReviewError(f"invalid artifact {key}")
        if re.fullmatch(r"[0-9a-f]{64}", artifact["remeasured_sha256"]) is None:
            raise ReviewError("invalid artifact remeasured_sha256")
        if "binding" in artifact and artifact["binding"] not in {
            "committed",
            "filesystem",
        }:
            raise ReviewError("invalid artifact binding")


def raise_prior_schema_read_only(record: dict[str, Any]) -> None:
    raise ReviewError(
        "prior_schema_read_only: "
        f"dispatch_id={record.get('dispatch_id')}; schema_version={record.get('schema_version')}; "
        f"state={record.get('state')}; permitted_operation=verify_from_merge_eligible_or_merged"
    )


def validate_schema1_verify_record(record: dict[str, Any]) -> None:
    # human_approved is admitted only as legacy-replacement input (contract 18);
    # command_verify still enforces its own merge_eligible/merged state gate.
    if record.get("state") not in {"human_approved", "merge_eligible", "merged"}:
        raise_prior_schema_read_only(record)
    for name in ("approved_head", "reviewed_head"):
        value = record.get(name)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ReviewError(f"verify: invalid {name}")
    brief_sha = record.get("brief_sha256")
    if (
        not isinstance(brief_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", brief_sha) is None
    ):
        raise ReviewError("verify: invalid brief_sha256")
    if not isinstance(record.get("history"), list):
        raise ReviewError("verify: history must be a list")
    approval = record.get("approval")
    if not isinstance(approval, dict):
        raise ReviewError("verify: approval must be an object")
    for name in ("approver", "mechanism", "signature"):
        if not isinstance(approval.get(name), str) or not approval[name]:
            raise ReviewError(f"verify: invalid approval {name}")


def gate_epoch(record: dict[str, Any]) -> int:
    """Active gate epoch. Legacy records and entries without the field are
    epoch 0; rebind increments it so every required check must rerun."""
    return int(record.get("gate_epoch", 0))
