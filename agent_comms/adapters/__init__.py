from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class DispatchContext:
    dispatch: Mapping[str, Any]
    recipient: Mapping[str, Any]
    message: Mapping[str, Any]
    ttl_seconds: int
    expected_close_by: str
    db_path: str


@dataclass(frozen=True)
class DispatchStart:
    spawn_handle: str
    observed_values: Mapping[str, Any] = field(default_factory=dict)


class RuntimeAdapter(Protocol):
    def dispatch(self, context: DispatchContext) -> DispatchStart:
        """Start the runtime task and return a stable handle."""

    def halt(self, spawn_handle: str, observed_values: Mapping[str, Any] | None = None) -> None:
        """Stop a live runtime task deterministically.

        When ``observed_values`` carries the supervisor control identity
        (``control_socket`` + ``run_token``) the stop is an authenticated socket
        HALT; success is only reported after confirmed termination.
        """

    def status(
        self, spawn_handle: str, observed_values: Mapping[str, Any] | None = None
    ) -> Any:
        """Typed liveness (``exited`` / ``running`` / ``supervisor_unreachable``).

        Read-only: it never signals and never infers death.
        """
