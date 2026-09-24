import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_comms.adapters.claude import ClaudeAdapter
from agent_comms.runtime_pins import (
    CLAUDE_PINNED_SHA256_ENV,
    CLAUDE_PINNED_VERSION,
    CLAUDE_VERSIONS_DIR_ENV,
)
from agent_comms.store import Store, WORKER_DISPATCH_POLICY


def seed_dispatch_actors(store: Store, root: Path) -> None:
    store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
    store.register_agent_actor(
        "alpha-worker", "alpha", "worker", str(root / "alpha-worker"), [], owner="alpha-architect"
    )


def process_is_live(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            text=True,
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, PermissionError):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    if result.returncode != 0:
        return False
    return "Z" not in result.stdout.strip()


def wait_for_file(path: Path, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def write_claude_pin_stub(root: Path) -> Path:
    versions_dir = root / "claude-versions"
    binary = versions_dir / CLAUDE_PINNED_VERSION
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        f"  printf 'Claude Code {CLAUDE_PINNED_VERSION}\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exec \"$@\"\n"
    )
    binary.chmod(0o755)
    return versions_dir


def claude_pin_env(root: Path) -> dict[str, str]:
    versions_dir = write_claude_pin_stub(root)
    binary = versions_dir / CLAUDE_PINNED_VERSION
    return {
        CLAUDE_VERSIONS_DIR_ENV: str(versions_dir),
        CLAUDE_PINNED_SHA256_ENV: hashlib.sha256(binary.read_bytes()).hexdigest(),
    }


class TtlRuntimeDeathTest(unittest.TestCase):
    def test_ttl_kills_actual_runtime_task_and_monitor_dlqs(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_COMMS_SPAWN_GRACE_SECONDS": "0"}), tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child_pid_file = root / "child.pid"
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            store.register_agent_actor(
                "alpha-worker",
                "alpha",
                "worker",
                str(root / "alpha-worker"),
                [],
                owner="alpha-architect",
                runtime="claude",
                spawn={
                    "command": "{claude_binary}",
                    "args": [
                        sys.executable,
                        "-c",
                        (
                            "import os, pathlib, time; "
                            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid())); "
                            "time.sleep(30)"
                        ),
                        f"WakePolicy={WORKER_DISPATCH_POLICY}",
                    ],
                },
            )
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "ttl-runtime-death",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )

            adapter = ClaudeAdapter()
            with mock.patch.dict(os.environ, claude_pin_env(root)):
                started = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=1)
            self.assertEqual(started[0]["status"], "in_flight")
            wait_for_file(child_pid_file)
            child_pid = int(child_pid_file.read_text())
            self.assertTrue(process_is_live(child_pid))

            time.sleep(2.0)

            self.assertFalse(process_is_live(child_pid))
            pre_reconcile = Store(root / "agent-comms.sqlite")._dispatch_by_idempotency_key_fresh("alpha-architect", "ttl-runtime-death")
            self.assertEqual(pre_reconcile["status"], "in_flight")

            actions = store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id="01M36YTJV9XBW95S6ZWV47C4RG")
            action_statuses = [action["status"] for action in actions]
            self.assertIn("dlq", action_statuses)
            self.assertIn("producer_paged", action_statuses)

            reconciled = Store(root / "agent-comms.sqlite")._dispatch_by_idempotency_key_fresh("alpha-architect", "ttl-runtime-death")
            self.assertEqual(reconciled["dispatch_id"], dispatch["dispatch_id"])
            self.assertEqual(reconciled["status"], "dlq")
            self.assertEqual(reconciled["failure_reason"], "timeout")
            # The wrapper's TTL kill recorded same-run exit evidence, so the
            # authenticated hard-TTL halt confirms via that evidence.
            self.assertEqual(reconciled["observed_values"]["termination_result"], "supervised_halt_confirmed")

            producer_inbox = store.list_inbox("alpha-architect")
            self.assertEqual(len(producer_inbox), 1)
            self.assertEqual(producer_inbox[0]["priority"], "blocker")
            self.assertEqual(producer_inbox[0]["from"], "01M36YTJV9XBW95S6ZWV47C4RG")
            self.assertIn("[DLQ] dispatch_agent timeout", producer_inbox[0]["subject"])


if __name__ == "__main__":
    unittest.main()
