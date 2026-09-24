"""Brief and DoD policy for the review tool.

Holds brief canonicalization and digesting and the Definition-of-Done
normalization, drift detection, and deterministic refusal used behind the
``agent_comms.review`` facade. It depends only on the standard library and
``agent_comms.reviewing.contracts``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_comms.reviewing.contracts import (
    EVIDENCE_ONLY_CHECK_IDS,
    EXECUTABLE_CHECK_IDS,
    ReviewError,
    is_evidence_only,
)


def canonical_brief_bytes(path: Path) -> bytes:
    raw = path.read_text(encoding="utf-8")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    canonical = "\n".join(line.rstrip() for line in normalized.split("\n"))
    return canonical.encode("utf-8")


def brief_sha256(path: Path) -> str:
    return hashlib.sha256(canonical_brief_bytes(path)).hexdigest()


def load_dod(
    path: Path | None,
    dod_section: str | None,
    *,
    raw_bytes: bytes | None = None,
) -> list[dict[str, Any]]:
    if path is None and not dod_section:
        raise ReviewError("open requires --dod or --dod-section")
    if path is not None:
        data = json.loads(raw_bytes if raw_bytes is not None else path.read_bytes())
        criteria = data.get("criteria", data) if isinstance(data, dict) else data
    else:
        criteria = []
        for index, line in enumerate((dod_section or "").splitlines(), start=1):
            text = line.strip().lstrip("-* ").strip()
            if text:
                criteria.append(
                    {"id": f"dod-{index}", "claim": text, "check_id": "green"}
                )
    if not isinstance(criteria, list):
        raise ReviewError("DoD must be a JSON list or object with criteria")
    normalized = []
    for index, item in enumerate(criteria, start=1):
        if not isinstance(item, dict):
            raise ReviewError("each DoD criterion must be an object")
        criterion = {
            "id": str(item.get("id") or f"dod-{index}"),
            "claim": str(item.get("claim") or ""),
            "check_id": str(item.get("check_id") or "green"),
            "expected": item.get("expected", "pass"),
            "scope": item.get("scope", ""),
            "evidence": item.get("evidence", ""),
            "required": bool(item.get("required", True)),
        }
        if criterion["check_id"] not in EXECUTABLE_CHECK_IDS and not is_evidence_only(
            criterion
        ):
            raise ReviewError(
                f"DoD criterion {criterion['id']} has unregistered check_id {criterion['check_id']}; "
                f"executable check_ids: {', '.join(sorted(EXECUTABLE_CHECK_IDS))}; "
                f"evidence-only check_ids: {', '.join(sorted(EVIDENCE_ONLY_CHECK_IDS))}"
            )
        if "argv" in item:
            criterion["argv"] = item["argv"]
        normalized.append(criterion)
    return normalized


def dod_drift(record: dict[str, Any]) -> dict[str, Any] | None:
    bound_sha256 = record.get("dod_sha256")
    if not bound_sha256:
        return None
    path = record.get("dod_path")
    if not path:
        return {
            "path": path,
            "bound_sha256": bound_sha256,
            "actual_sha256": None,
            "reason": "unreadable",
        }
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_sha256 = digest.hexdigest()
    except OSError:
        return {
            "path": path,
            "bound_sha256": bound_sha256,
            "actual_sha256": None,
            "reason": "unreadable",
        }
    if actual_sha256 == bound_sha256:
        return None
    return {
        "path": path,
        "bound_sha256": bound_sha256,
        "actual_sha256": actual_sha256,
        "reason": "digest_mismatch",
    }


def refuse_dod_drift(record: dict[str, Any]) -> None:
    drift = dod_drift(record)
    if drift is not None:
        raise ReviewError(
            "DoD drift: "
            f"path={drift['path']}; bound_sha256={drift['bound_sha256']}; "
            f"actual_sha256={drift['actual_sha256']}; reason={drift['reason']}; "
            "restore the file to the bound digest, or open a new governed review; "
            "rebind-dod is available only before dispatch"
        )
