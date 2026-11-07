"""Owned ZDOTDIR cleanup, shared by the server teardown, the adapter's
pre-READY failure path and the parent reaper's absence barrier.
"""
from __future__ import annotations

import shutil
from pathlib import Path


def _cleanup_zdotdir(zdotdir: Path) -> Path | None:
    """Remove the owned ``agent-comms-zdotdir-*`` parent of a ZDOTDIR.

    Returns the parent path it targeted for removal (the OWNED artifact) so a
    caller can confirm disappearance of the exact directory this removes rather
    than the nested child path; returns ``None`` when the parent is not an owned
    zdotdir wrapper (nothing removed).
    """
    parent = zdotdir.parent
    if not parent.name.startswith("agent-comms-zdotdir-"):
        return None
    shutil.rmtree(parent, ignore_errors=True)
    return parent


def _owned_zdotdir_parent(zdotdir: str | None) -> Path | None:
    """The exact owned ``agent-comms-zdotdir-*`` parent of a registered ZDOTDIR,
    or ``None`` when there is no owned parent to verify.

    Mirrors exactly the ownership boundary :func:`_cleanup_zdotdir` removes: it
    never widens to an unrelated actor-named directory, so a pre-existing
    unrelated directory can neither satisfy nor poison the absence barrier.
    """
    if not zdotdir:
        return None
    parent = Path(zdotdir).parent
    if not parent.name.startswith("agent-comms-zdotdir-"):
        return None
    return parent
