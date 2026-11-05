"""Dispatch statuses that count as terminal for owner, lifecycle and
cancellation recognition.

One definition shared by the ledger and the supervisor so the two can never
disagree. Terminal here means the dispatch is no longer live for those
checks, not that the row can never change again: a
``spawn_failed_message_landed`` row is the explicit input of ``retry_spawn``,
which moves it back to ``in_flight``. Membership does not by itself authorize
janitor deletion, which requires the supervisor's positive same-run evidence
gate.
"""

from __future__ import annotations

DISPATCH_TERMINAL_STATUSES = frozenset({"closed", "dlq", "spawn_failed_message_landed", "cancelled"})
