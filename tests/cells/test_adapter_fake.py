import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Callable, Optional
from unittest import mock

from agent_comms.adapters import DispatchContext
from agent_comms.adapters.fake import FakeAdapter
from agent_comms.store import Store, WORKER_DISPATCH_POLICY
from agent_comms import supervisor

ROOT = Path(__file__).resolve().parents[2]
HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"


def wait_until(predicate: Callable[[], object], timeout_seconds: float = 5.0) -> object:
    deadline = time.monotonic() + timeout_seconds
    last_value: object = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for condition; last value: {last_value!r}")


def fake_worker_args(db_path: Path, *extra: str) -> list[str]:
    return [
        "-c",
        (
            "import sys; "
            f"sys.path.insert(0, {str(ROOT)!r}); "
            "from agent_comms.adapters.fake_worker import main; "
            "raise SystemExit(main())"
        ),
        "--actor-id",
        "{actor_id}",
        "--message-id",
        "{message_id}",
        "--db",
        str(db_path),
        *extra,
        f"WakePolicy={WORKER_DISPATCH_POLICY}",
    ]


def seed_fake_actors(store: Store, root: Path, db_path: Path, *extra_worker_args: str) -> None:
    store.register_actor(HUMAN_ID, "human", "alice")
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
    store.register_agent_actor(
        "alpha-fake-worker",
        "alpha",
        "worker",
        str(root / "alpha-fake-worker"),
        [],
        owner="alpha-architect",
        runtime="fake",
        spawn={
            "command": sys.executable,
            "args": fake_worker_args(db_path, *extra_worker_args),
        },
    )


class FakeAdapterTest(unittest.TestCase):
    @contextlib.contextmanager
    def isolated_real_spawn(self, root: Path):
        control_root = root / "s"
        owned_registry = supervisor.ReaperRegistry()
        with supervisor._REGISTRY_LOCK:
            prior_registry = supervisor._REGISTRY
            supervisor._REGISTRY = owned_registry
        try:
            with mock.patch.dict(
                os.environ,
                {supervisor.CONTROL_ROOT_ENV: str(control_root)},
            ):
                yield owned_registry, control_root
        finally:
            try:
                def reap_drained() -> bool:
                    owned_registry.reap_ready()
                    return owned_registry.pending() == 0

                wait_until(reap_drained)
                owned_registry.stop()
                self.assertEqual(owned_registry.pending(), 0)
                self.assertFalse(
                    owned_registry._thread is not None and owned_registry._thread.is_alive()
                )
                self.assertFalse(control_root.exists() and any(control_root.iterdir()))
            finally:
                owned_registry.stop()
                with supervisor._REGISTRY_LOCK:
                    self.assertIs(supervisor._REGISTRY, owned_registry)
                    supervisor._REGISTRY = prior_registry
            self.assertIs(supervisor._REGISTRY, prior_registry)

    def assert_test_control_root(self, started: dict, control_root: Path) -> None:
        observed = started["observed_values"]
        control_socket = Path(observed["control_socket"])
        self.assertEqual(control_socket.parent.parent, control_root)
        self.assertNotEqual(control_root, supervisor._DEFAULT_CONTROL_ROOT)
        self.assertNotIn(supervisor._DEFAULT_CONTROL_ROOT, control_socket.parents)

    def test_fake_adapter_worker_replies_closes_trigger_and_halt_is_noop(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp_dir:
            root = Path(temp_dir)
            with self.isolated_real_spawn(root) as (_registry, control_root):
                db_path = root / "agent-comms.sqlite"
                store = Store(db_path)
                seed_fake_actors(store, root, db_path)
                dispatch = store.dispatch_agent(
                    "alpha-architect", "alpha-fake-worker", "fake-positive",
                    "Ping", "Please reply.", [],
                )
                adapter = FakeAdapter()

                started = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=1)
                self.assertEqual(started[0]["status"], "in_flight", started[0])
                self.assertTrue(started[0]["spawn_handle"].startswith("fake:alpha-fake-worker:"))
                self.assert_test_control_root(started[0], control_root)

                def worker_closed_and_replied() -> Optional[tuple[dict, dict]]:
                    worker_messages = store.list_inbox(
                        "alpha-fake-worker", unread_only=False, include_closed=True,
                    )
                    replies = [
                        message
                        for message in store.list_inbox("alpha-architect", unread_only=False)
                        if message["parent_message_id"] == dispatch["message_id"]
                    ]
                    if worker_messages and worker_messages[0]["status"] == "closed" and replies:
                        return worker_messages[0], replies[0]
                    return None

                trigger, reply = wait_until(worker_closed_and_replied)
                # Authenticated teardown via the recorded control identity (current
                # API). The already-closed worker's wrapper is usually gone, so a
                # "halt is a noop" here tolerates an unconfirmed/unreachable result.
                try:
                    adapter.halt(
                        started[0]["spawn_handle"], started[0].get("observed_values")
                    )
                except Exception:
                    pass

                def ledger_closed() -> Optional[dict]:
                    store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)
                    row = store._dispatch_by_idempotency_key_fresh("alpha-architect", "fake-positive")
                    return row if row["status"] == "closed" else None

                ledger = wait_until(ledger_closed)

                self.assertEqual(trigger["id"], dispatch["message_id"])
                self.assertEqual(trigger["status"], "closed")
                self.assertEqual(reply["parent_message_id"], dispatch["message_id"])
                self.assertEqual(reply["from"], "alpha-fake-worker")
                self.assertEqual(reply["to"], "alpha-architect")
                self.assertEqual(ledger["status"], "closed")

    def test_fake_worker_dies_before_close_goes_to_dlq_and_pages_human(self) -> None:
        with mock.patch.dict(
            os.environ, {"AGENT_COMMS_SPAWN_GRACE_SECONDS": "0"}
        ), tempfile.TemporaryDirectory(dir="/private/tmp") as temp_dir:
            root = Path(temp_dir)
            with self.isolated_real_spawn(root) as (_registry, control_root):
                db_path = root / "agent-comms.sqlite"
                store = Store(db_path)
                seed_fake_actors(store, root, db_path, "--fail-before-close")
                store.dispatch_agent(
                    "alpha-architect", "alpha-fake-worker", "fake-worker-crash",
                    "Ping", "Please crash.", [],
                )
                adapter = FakeAdapter()
                started = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=10)
                self.assertEqual(started[0]["status"], "in_flight", started[0])
                self.assert_test_control_root(started[0], control_root)

                def child_exit() -> Optional[dict]:
                    row = store._dispatch_by_idempotency_key_fresh(
                        "alpha-architect", "fake-worker-crash"
                    )
                    evidence = row["observed_values"].get("worker_exit")
                    if (
                        isinstance(evidence, dict)
                        and evidence.get("source") == "child"
                        and evidence.get("run_token") == row["observed_values"].get("run_token")
                    ):
                        return evidence
                    return None

                evidence = wait_until(child_exit)
                self.assertEqual(evidence["source"], "child")
                observed_actions = store.reconcile_dispatches(
                    lambda _runtime: adapter, human_actor_id=HUMAN_ID
                )
                ledger = store._dispatch_by_idempotency_key_fresh(
                    "alpha-architect", "fake-worker-crash"
                )

                self.assertEqual(ledger["status"], "dlq")
                self.assertEqual(ledger["failure_reason"], "worker_exited_before_close")
                self.assertIn("dlq", [action["status"] for action in observed_actions])
                self.assertIn("producer_paged", [action["status"] for action in observed_actions])
                self.assertEqual(len(store.list_inbox("alpha-architect")), 1)

    def test_fake_runtime_exit_124_within_grace_times_out_to_dlq_and_pages_producer(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp_dir:
            root = Path(temp_dir)
            with self.isolated_real_spawn(root) as (_registry, control_root):
                db_path = root / "agent-comms.sqlite"
                store = Store(db_path)
                store.register_actor(HUMAN_ID, "human", "alice")
                store.register_agent_actor(
                    "alpha-architect", "alpha", "architect",
                    str(root / "alpha-architect"), [],
                )
                store.register_agent_actor(
                    "alpha-fake-worker",
                    "alpha",
                    "worker",
                    str(root / "alpha-fake-worker"),
                    [],
                    owner="alpha-architect",
                    runtime="fake",
                    spawn={
                        "command": sys.executable,
                        "args": [
                            "-c",
                            "raise SystemExit(124)",
                            f"WakePolicy={WORKER_DISPATCH_POLICY}",
                        ],
                    },
                )
                store.dispatch_agent(
                    "alpha-architect",
                    "alpha-fake-worker",
                    "fake-exit-124",
                    "Ping",
                    "Please time out.",
                    [],
                )
                adapter = FakeAdapter()
                started = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=1)
                self.assertEqual(started[0]["status"], "in_flight", started[0])
                self.assert_test_control_root(started[0], control_root)

                time.sleep(1.1)
                actions = store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)
                ledger = store._dispatch_by_idempotency_key_fresh("alpha-architect", "fake-exit-124")

                self.assertEqual(ledger["status"], "dlq")
                self.assertEqual(ledger["failure_reason"], "timeout")
                self.assertIn("dlq", [action["status"] for action in actions])
                self.assertIn("producer_paged", [action["status"] for action in actions])
                self.assertEqual(len(store.list_inbox("alpha-architect")), 1)

    def test_spawn_args_missing_bootstrap_marker_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = FakeAdapter()
            context = DispatchContext(
                dispatch={
                    "dispatch_id": "dispatch-test",
                    "policy_name": WORKER_DISPATCH_POLICY,
                },
                recipient={
                    "id": "alpha-fake-worker",
                    "runtime": "fake",
                    "project_root": str(root),
                    "spawn": {
                        "command": sys.executable,
                        "args": fake_worker_args(root / "agent-comms.sqlite")[:-1],
                    },
                },
                message={"id": "msg-test"},
                ttl_seconds=30,
                expected_close_by="2026-05-23T00:00:30+00:00",
                db_path=str(root / "agent-comms.sqlite"),
            )

            with self.assertRaisesRegex(RuntimeError, "bootstrap marker"):
                adapter.dispatch(context)

    def test_fake_runtime_actor_without_spawn_command_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = FakeAdapter()
            context = DispatchContext(
                dispatch={
                    "dispatch_id": "dispatch-test",
                    "policy_name": WORKER_DISPATCH_POLICY,
                },
                recipient={
                    "id": "alpha-fake-worker",
                    "runtime": "fake",
                    "project_root": str(root),
                    "spawn": {"args": [f"WakePolicy={WORKER_DISPATCH_POLICY}"]},
                },
                message={"id": "msg-test"},
                ttl_seconds=30,
                expected_close_by="2026-05-23T00:00:30+00:00",
                db_path=str(root / "agent-comms.sqlite"),
            )

            with self.assertRaisesRegex(RuntimeError, "fake recipient requires spawn.command"):
                adapter.dispatch(context)


if __name__ == "__main__":
    unittest.main()
