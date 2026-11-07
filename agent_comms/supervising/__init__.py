"""The per-dispatch process supervisor, split along its concerns.

Dependency direction, leaves first: ``paths``, ``process``, ``zdotdir`` <-
``evidence`` <- ``reaper`` <- ``protocol`` <- ``server``, ``spawn``, ``janitor``.
Callers import the owning module (``from agent_comms import supervising`` then
``supervising.janitor.janitor_sweep(...)``) so a patch on that module reaches
every caller; ``agent_comms.supervisor`` re-exports the names for the wrapper
and for readers.
"""
from __future__ import annotations

from . import paths, process, zdotdir, evidence, reaper, protocol, server, spawn, janitor

__all__ = ["paths", "process", "zdotdir", "evidence", "reaper", "protocol", "server", "spawn", "janitor"]
