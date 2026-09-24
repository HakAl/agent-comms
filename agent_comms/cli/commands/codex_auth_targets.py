"""Report Codex actors grouped by distinct auth lineage.

Credential-free operator visibility: it exposes only actor ids, opaque group
ordinals, and per-group counts -- never resolved auth paths or bytes. Actors
that share the real ``auth.json`` filesystem identity the refresh driver would
refresh land in the same opaque group, but the identity itself is never printed.
"""

from __future__ import annotations

NAME = "codex-auth-targets"


def register(subparsers) -> None:
    subparsers.add_parser(NAME)


def handle(store, args):
    from ... import codex_refresh_driver
    from .._helpers import DegradedState

    report = codex_refresh_driver.auth_targets_report(store)
    if report.get("actor_defects"):
        raise DegradedState(report)
    return report
