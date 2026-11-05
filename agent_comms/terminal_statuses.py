"""Dispatch statuses that are terminal for owner and lifecycle recognition.

One definition shared by the ledger and the supervisor so the two can never
disagree. Terminal here means the row will not change status again; it does
not by itself authorize janitor deletion, which requires the supervisor's
positive same-run evidence gate.
"""

from __future__ import annotations

DISPATCH_TERMINAL_STATUSES = frozenset({"closed", "dlq", "spawn_failed_message_landed", "cancelled"})
