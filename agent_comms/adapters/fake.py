from __future__ import annotations

from ._base import ProcessSpawnAdapter


class FakeAdapter(ProcessSpawnAdapter):
    """Foundation-contract adapter backed by a checked-in worker helper."""

    runtime_label = "fake"
    supported_runtimes = ("fake",)
