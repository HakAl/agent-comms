"""Dispatch statuses that count as terminal for owner, lifecycle and
cancellation recognition, and the pure ledger/transport projection.

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


# Canonical joined projection of the two INDEPENDENT state machines:
# ``dispatch_ledger.status`` (execution outcome) and
# ``message_recipients.status`` (delivery obligation). Per the stage-2 brief the
# projection is TOTAL over every MECHANICALLY REACHABLE raw pair and never infers
# one status from the other.
#
# Reachability is derived from the actual transactions, not a hand-picked subset:
# ``mailbox.read_message`` / ``ack_message`` / ``close_message`` advance the
# transport copy UNCONDITIONALLY, but only flip the ledger with a
# ``where status = 'in_flight'`` predicate. So whenever the ledger is NOT
# ``in_flight`` a racing/late recipient read/ack/close leaves the ledger row
# untouched while the transport advances. Every ordinary pair below was
# empirically constructed via real Store operations (e.g. a worker that replies
# then acks/closes while the ledger is still ``queued`` commits
# ``queued/acknowledged`` and ``queued/closed`` -- pairs a prior revision
# wrongly called impossible). For the ordinary pairs the normalized outcome
# follows the EXECUTION (ledger) machine and is never derived from the transport
# status.
#
# The sanctioned-cancellation cluster is named distinctly and never folded into
# the ordinary outcomes: confirmed cancellation is ``cancelled/cancelled`` ->
# ``confirmed_cancel``; the exceptional operator settlement is ``dlq/cancelled``
# -> ``operator_settled_termination_unconfirmed`` (never a plain ``dlq`` or a
# confirmed cancel); and the reachable race where the recipient close wins its
# transport CAS first is ``cancelled/closed`` ->
# ``confirmed_cancel_transport_closed_first`` (never an ordinary ``closed``).
#
# A stale ``in_flight`` ledger whose recipient obligation already terminalized
# (``acknowledged``/``closed``) is NOT committed on the happy path -- the
# ack/close transaction flips the ledger in lockstep -- but the pre-TTL liveness
# and hard-TTL reconcilers OWN settling such a stale row to closed. That pair is
# projected to the distinct ``RECIPIENT_TERMINAL_LEDGER_OPEN`` outcome below,
# never a plain ``closed`` (the ledger has not closed; claiming it would be the
# cross-inference this projection forbids) and never ``unknown`` (the reconciler
# must act on it), so monitoring consumes this one classifier instead of
# re-mapping raw transport states by hand.
#
# Any truly UNREACHABLE or unrecognized pair (a ``closed`` ledger with a
# ``sent``/``read`` transport, since ``closed`` is only reached via ack/close; a
# non-``dlq`` ledger with a ``cancelled`` transport, since cancelled is committed
# jointly; an unknown status) returns a loud ``unknown`` rather than guessing
# equality.
#
# The recipient obligation is terminal (acked/closed) while the execution ledger
# is still open; the liveness / hard-TTL reconciler settles the stale row.
RECIPIENT_TERMINAL_LEDGER_OPEN = "recipient_terminal_ledger_open"

_DISPATCH_TRANSPORT_OUTCOMES: dict[tuple[str, str], str] = {
    # queued: the dispatch has not started; a recipient may still read/ack/close,
    # none of which flips the queued ledger.
    ("queued", "sent"): "queued",
    ("queued", "read"): "queued",
    ("queued", "acknowledged"): "queued",
    ("queued", "closed"): "queued",
    # in_flight: a read leaves it in_flight. An ack/close atomically flips the
    # ledger to closed in lockstep, so (in_flight, acknowledged/closed) is never
    # committed on the happy path; when a stale in_flight row is nonetheless
    # observed with a terminalized recipient copy, the liveness / hard-TTL
    # reconciler settles it and consumes this distinct outcome.
    ("in_flight", "sent"): "in_flight",
    ("in_flight", "read"): "in_flight",
    ("in_flight", "acknowledged"): RECIPIENT_TERMINAL_LEDGER_OPEN,
    ("in_flight", "closed"): RECIPIENT_TERMINAL_LEDGER_OPEN,
    # closed: reached only via ack/close, which set the transport in the same
    # transaction, so only acknowledged/closed transports pair with it.
    ("closed", "acknowledged"): "closed",
    ("closed", "closed"): "closed",
    # dlq: early-exit/TTL DLQ is independent of transport; a late read/ack/close
    # then advances only the transport.
    ("dlq", "sent"): "dlq",
    ("dlq", "read"): "dlq",
    ("dlq", "acknowledged"): "dlq",
    ("dlq", "closed"): "dlq",
    # spawn_failed_message_landed: the message landed but no worker started; a
    # recipient read/ack/close advances only the transport.
    ("spawn_failed_message_landed", "sent"): "spawn_failed",
    ("spawn_failed_message_landed", "read"): "spawn_failed",
    ("spawn_failed_message_landed", "acknowledged"): "spawn_failed",
    ("spawn_failed_message_landed", "closed"): "spawn_failed",
    # Sanctioned cancellation cluster (named distinctly; never folded above).
    ("cancelled", "cancelled"): "confirmed_cancel",
    ("dlq", "cancelled"): "operator_settled_termination_unconfirmed",
    ("cancelled", "closed"): "confirmed_cancel_transport_closed_first",
}


def project_dispatch_transport(dispatch_status: str, transport_status: str) -> dict:
    """Project a (dispatch_status, transport_status) pair to a normalized outcome.

    Returns a dict exposing BOTH raw statuses plus a normalized ``outcome``. The
    mapping never infers one status from the other: an unreachable or
    unrecognized pair (e.g. a ``closed`` ledger with a ``sent`` transport, since
    ``closed`` is reached only via ack/close) yields ``outcome="unknown"`` rather
    than assuming the two machines agree. A late/racing recipient ack/close on a
    non-``in_flight`` ledger IS reachable (e.g. ``queued/acknowledged``,
    ``dlq/closed``) and is mapped to the execution status, not treated as
    impossible. A stale ``in_flight`` ledger whose recipient copy already
    terminalized projects to the distinct ``RECIPIENT_TERMINAL_LEDGER_OPEN``
    outcome that the liveness / hard-TTL reconciler consumes to settle it to
    closed -- never a plain ``closed`` and never ``unknown``.
    """
    outcome = _DISPATCH_TRANSPORT_OUTCOMES.get((dispatch_status, transport_status), "unknown")
    return {
        "dispatch_status": dispatch_status,
        "transport_status": transport_status,
        "outcome": outcome,
    }
