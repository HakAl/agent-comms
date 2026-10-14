"""One interface per worker runtime (ac-4ao.10 slice 4).

Each runtime (Claude, Codex, the test-only fake) is a module here exposing a
``RUNTIME`` that satisfies :class:`Runtime`. Spawn, setup, doctor and the
adapter registry look runtimes up by name or iterate them in ``NAMES``
order; only this package says what a runtime does.

Modules load on first lookup, so ``spawn`` (which the runtime templates
import) can import this package without a cycle.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ..schema import ValidationError

NAMES = ("claude", "codex", "fake")


@dataclass(frozen=True)
class Rerender:
    """How ``rerender-spawn`` migrates a stored spawn block to the template:
    args the template added (a stored prefix may lack them), adjacent
    ``flag value`` pairs it retired, and stored commands it replaced."""

    args_introduced: tuple[str, ...] = ()
    args_retired: tuple[str, ...] = ()
    commands_introduced: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class Runtime(Protocol):
    name: str
    # Setup's registry capabilities for this runtime's worker.
    capabilities: tuple[str, ...]
    # Has a certified native pin per platform (doctor's platform check).
    pinned: bool
    # Setup registers this runtime when no other is detected.
    fallback: bool
    # Its finished dispatches still need a monitor (doctor's monitor check).
    monitored: bool
    # How rerender-spawn migrates a stored block, or None: not supported.
    rerender: Rerender | None

    @property
    def adapter_class(self) -> type: ...

    def adapter(self) -> Any: ...

    def detect(self, which: Callable[[str], str | None], pin: Any | None) -> bool:
        """This machine appears to have the runtime; ``pin`` is its pin on
        this platform, or None when it has none."""

    def render_spawn(self, actor_id: str, *, codex_home: str | None = None) -> dict:
        """The placeholder-bearing spawn block, as ``spawn.render_spawn``."""

    def provision(
        self,
        worker_id: str,
        *,
        pin: Any | None,
        pin_error: str | None,
        which: Callable[[str], str | None],
        run: Callable[..., Any],
    ) -> dict:
        """Setup's report fields for this runtime's new worker; ``pin`` and
        ``pin_error`` are what setup resolved for this platform."""

    def provision_absent(self) -> dict:
        """The same fields when setup registered no worker of this runtime."""

    def doctor_checks(
        self,
        pin: Any | None,
        snapshot: Any | None,
        *,
        run: Callable[..., Any],
        now: datetime | None,
        ledger: Any,
    ) -> list[dict]:
        """Doctor's rows when a worker of this runtime is registered."""

    def doctor_skip(self) -> dict | None:
        """Doctor's row when none is, or None for no row."""


_loaded: dict[str, Runtime] = {}


def get(name: str) -> Runtime:
    if name not in NAMES:
        raise ValidationError(f"unsupported runtime {name!r}; allowed: {', '.join(NAMES)}")
    if name not in _loaded:
        _loaded[name] = importlib.import_module(f"{__name__}.{name}").RUNTIME
    return _loaded[name]


def registered() -> list[Runtime]:
    return [get(name) for name in NAMES]
