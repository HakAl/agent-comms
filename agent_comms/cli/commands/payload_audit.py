from __future__ import annotations

from pathlib import Path

from ... import payload
from .._helpers import print_json

NAME = "payload-audit"

# Read-only: the audit never creates, repairs, or deletes anything, so no
# Store/Database construction (which could materialize a ledger) is performed.
NEEDS_STORE = False


def register(subparsers) -> None:
    subparsers.add_parser(
        NAME,
        help=(
            "Read-only audit of the payload store against dispatch and message SQL references. "
            "Reports referenced-good, referenced-missing/corrupt, unreferenced blob, "
            "staging-residue, and irregular-entry counts/bytes; exits nonzero exactly "
            "when a referenced artifact is unavailable or corrupt, an unreferenced "
            "blob or staging residue exists, or the store contains a symlinked or "
            "otherwise irregular entry. Never repairs or "
            "deletes."
        ),
    )


def handle(store, args):
    report = payload.audit_store(Path(args.db))
    if not report["ok"]:
        print_json(report)
        raise SystemExit(1)
    return report
