from __future__ import annotations

from .. import runtimes
from . import RuntimeAdapter

ADAPTERS_BY_RUNTIME: dict[str, type[RuntimeAdapter]] = {
    runtime.name: runtime.adapter_class for runtime in runtimes.registered()
}


def adapter_for(runtime: str) -> RuntimeAdapter:
    cls = ADAPTERS_BY_RUNTIME.get(runtime)
    if cls is None:
        raise ValueError(f"no adapter registered for runtime: {runtime}")
    return cls()
