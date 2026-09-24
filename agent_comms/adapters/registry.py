from __future__ import annotations

from . import RuntimeAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .fake import FakeAdapter

ADAPTERS_BY_RUNTIME: dict[str, type[RuntimeAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "fake": FakeAdapter,
}


def adapter_for(runtime: str) -> RuntimeAdapter:
    cls = ADAPTERS_BY_RUNTIME.get(runtime)
    if cls is None:
        raise ValueError(f"no adapter registered for runtime: {runtime}")
    return cls()
