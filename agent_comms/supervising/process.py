"""Process-group termination and drain for the native runtime child.

SIGTERM -> grace -> SIGKILL on the child's whole process group, and the bounded
non-signalling polls that prove a group is empty. Platform behavior for process
groups lives here.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time


def terminate_process_tree(child: subprocess.Popen, grace_seconds: float) -> int | None:
    """SIGTERM -> grace -> SIGKILL, escalating on the child's WHOLE process group.

    The wrapper launches the child with ``start_new_session=True`` so the child
    is its own group leader and the process-group id equals the child PID. That
    identity survives the leader exiting: if ``os.getpgid`` loses the race and
    raises ``ProcessLookupError`` (the leader was already reaped) we fall back to
    the child PID and STILL signal the group, so a surviving descendant is never
    abandoned by merely waiting an already-gone leader. We signal exactly that
    group, never the wrapper's.

    The leader exiting is NOT proof the group drained: a descendant that ignores
    SIGTERM keeps the group alive after the leader is reaped. Escalation to
    SIGKILL is therefore gated on a GROUP-level ``killpg(pgid, 0)`` probe over the
    grace window -- never on the leader's exit alone -- so a SIGTERM-ignoring
    descendant is always force-killed and the native group is confirmed drained.
    The leader is reaped the instant it exits so its status is captured and it is
    never left a zombie. The gate uses the single-shot ``_process_group_gone``
    probe, not ``process_group_empty``, so termination's internal escalation never
    perturbs the HALT barrier's separate, mandatory group-emptiness proof.
    """
    grace = max(grace_seconds, 0.0)
    try:
        pgid = os.getpgid(child.pid)
    except ProcessLookupError:
        # Leader-exit race: the group id is the child PID by construction.
        pgid = child.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # Give the ENTIRE group the grace to drain under SIGTERM, reaping the leader
    # the moment it exits but continuing to watch the whole group.
    leader_status = _drain_group(child, pgid, grace, status=None)
    if _process_group_gone(pgid):
        return leader_status
    # Residue survived the SIGTERM grace: escalate the whole group with SIGKILL
    # (never just the reaped leader) and confirm the group drains within grace.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return _drain_group(child, pgid, grace, status=leader_status)


def process_group_empty(pgid: int, *, timeout: float, interval: float = 0.02) -> bool:
    """Bounded GROUP-LEVEL kernel predicate: has the whole native group drained?

    Polls ``os.killpg(pgid, 0)`` (the null signal) until the kernel reports the
    entire process group is gone. ``killpg`` addresses the group as long as ANY
    member survives, so a bare surviving grandchild keeps it alive and raises
    nothing -- only an empty group raises ``ProcessLookupError``. This is
    therefore proof the COMPLETE native tree is gone, not a per-PID or ``ps``
    check that a reaped leader alone would satisfy. A member we are not permitted
    to signal (``PermissionError``) counts as residue. Returns ``False`` if the
    group is still non-empty when ``timeout`` elapses.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # pragma: no cover - unsignalable member survives
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _process_group_gone(pgid: int) -> bool:
    """Single non-blocking probe: has the whole native process group drained?

    ``killpg(pgid, 0)`` (the null signal) addresses the group while ANY member
    survives -- including an unreaped zombie -- so only a fully empty group raises
    ``ProcessLookupError``. A member we cannot signal (``PermissionError``) counts
    as surviving residue. This is the escalation gate used inside
    :func:`terminate_process_tree`; it is deliberately distinct from the blocking
    :func:`process_group_empty` the HALT barrier uses so termination's internal
    escalation never perturbs that separate proof.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:  # pragma: no cover - unsignalable residue survives
        return False
    return False


def _drain_group(
    child: subprocess.Popen, pgid: int, grace: float, *, status: int | None
) -> int | None:
    """Reap the leader as it exits and wait up to ``grace`` for the whole GROUP
    to drain. Returns the leader's exit status once observed (or the passed-in
    prior ``status``). Reaping is non-blocking (``child.poll``) so a
    SIGTERM-ignoring descendant is watched for the whole window rather than the
    leader's exit ending the wait early; never blocks past the bounded grace.
    """
    deadline = time.monotonic() + grace
    while True:
        if status is None:
            status = child.poll()
        if _process_group_gone(pgid):
            return status
        if time.monotonic() >= deadline:
            return status
        time.sleep(0.02)
