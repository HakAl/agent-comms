"""The test-only fake runtime: a checked-in worker helper, no native binary."""

from __future__ import annotations

from ..policies import compile_policy
from ..store import WORKER_DISPATCH_POLICY


def render_spawn() -> dict:
    # {python} resolves at dispatch time to the interpreter running the
    # dispatch, which can import agent_comms by definition; a bare "python3"
    # only worked when the project environment happened to be first on PATH.
    policy = compile_policy(WORKER_DISPATCH_POLICY)
    return {
        "command": "{python}",
        "args": [
            "-m",
            "agent_comms.adapters.fake_worker",
            "--actor-id",
            "{actor_id}",
            "--message-id",
            "{message_id}",
            "--db",
            "{db_path}",
            policy.bootstrap_marker,
        ],
    }


class FakeRuntime:
    name = "fake"
    capabilities = ("demo",)
    pinned = False
    # Setup registers a fake worker when no native runtime is found.
    fallback = True
    # A finished fake dispatch (the demo's) needs no monitor.
    monitored = False
    rerender = None

    @property
    def adapter_class(self) -> type:
        from ..adapters.fake import FakeAdapter

        return FakeAdapter

    def adapter(self):  # type: ignore[no-untyped-def]
        return self.adapter_class()

    def detect(self, which, pin) -> bool:  # type: ignore[no-untyped-def]
        return False

    def render_spawn(self, actor_id: str, *, codex_home: str | None = None) -> dict:
        return render_spawn()

    def provision(self, worker_id, *, pin, pin_error, which, run) -> dict:  # type: ignore[no-untyped-def]
        return {}

    def provision_absent(self) -> dict:
        return {}

    def doctor_checks(self, pin, snapshot, *, run, now, ledger) -> list[dict]:  # type: ignore[no-untyped-def]
        return []

    def doctor_skip(self) -> dict | None:
        return None


RUNTIME = FakeRuntime()
