import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import os
import hashlib
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import paths, runtime_pins, supervisor
from agent_comms.adapters import DispatchContext
from agent_comms.adapters.claude import ClaudeAdapter
from agent_comms.policies import compile_policy, scoped_env
from agent_comms.store import WORKER_DISPATCH_POLICY

# Hermetic supervised-seam identity (see tests/cells/test_adapter_codex.py). The
# real AF_UNIX bind proof lives in the certification EndToEndSupervisorTest.
RUN_TOKEN = "a" * 32
CONTROL_SOCKET = "/protected/run/s/" + RUN_TOKEN + "/s"
RUN_DIR = "/protected/run/s/" + RUN_TOKEN


def _ready_spawn(wrapper_pid: int) -> supervisor.SupervisedSpawn:
    return supervisor.SupervisedSpawn(
        popen=types.SimpleNamespace(pid=wrapper_pid),
        run_token=RUN_TOKEN,
        control_socket=CONTROL_SOCKET,
        child_pid=wrapper_pid + 1,
        wrapper_pid=wrapper_pid,
        run_dir=RUN_DIR,
    )


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ClaudeAdapterTest(unittest.TestCase):
    def _write_fake_pin(self, root: Path) -> tuple[Path, str]:
        versions_dir = root / "claude-versions"
        binary = versions_dir / runtime_pins.CLAUDE_PINNED_VERSION
        binary.parent.mkdir(parents=True)
        binary.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            f"  printf 'Claude Code {runtime_pins.CLAUDE_PINNED_VERSION}\\n'\n"
            "  exit 0\n"
            "fi\n"
            "exec \"$@\"\n"
        )
        binary.chmod(0o755)
        return versions_dir, hashlib.sha256(binary.read_bytes()).hexdigest()

    def _adapter(self, expected_sha256: str) -> ClaudeAdapter:
        return ClaudeAdapter(
            version_runner=lambda _binary: subprocess.CompletedProcess(
                [str(_binary), "--version"],
                0,
                stdout=f"Claude Code {runtime_pins.CLAUDE_PINNED_VERSION}\n",
                stderr="",
            ),
            expected_sha256=expected_sha256,
        )

    def test_dispatch_returns_stable_handle_and_halt_stops_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            versions_dir, expected_sha256 = self._write_fake_pin(root)
            adapter = self._adapter(expected_sha256)
            context = DispatchContext(
                dispatch={
                    "dispatch_id": "dispatch_20260627_000000_abcdef12",
                    "policy_name": WORKER_DISPATCH_POLICY,
                },
                recipient={
                    "id": "alpha-worker",
                    "runtime": "claude",
                    "project_root": str(root),
                    "spawn": {
                        "command": "{claude_binary}",
                        "args": [
                            sys.executable,
                            "-c",
                            "import time; time.sleep(30)",
                            f"WakePolicy={WORKER_DISPATCH_POLICY}",
                        ],
                    },
                },
                message={"id": "msg-test"},
                ttl_seconds=30,
                expected_close_by="2026-05-23T00:00:30+00:00",
                db_path=str(root / "agent-comms.sqlite"),
            )

            registry = mock.MagicMock()
            with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}), \
                 mock.patch.object(paths, "dispatch_log_path", return_value=root / "worker.log"), \
                 mock.patch.object(supervisor, "janitor_sweep", return_value=[]), \
                 mock.patch.object(supervisor, "reaper_registry", return_value=registry), \
                 mock.patch.object(supervisor, "spawn_supervised", return_value=_ready_spawn(4321)):
                result = adapter.dispatch(context)

            # The stable handle carries the wrapper pid; READY records the control
            # identity so halt authenticates over the socket, never a PID signal.
            self.assertEqual(result.spawn_handle, "claude:alpha-worker:4321")
            self.assertTrue(result.spawn_handle.startswith("claude:alpha-worker:"))
            self.assertEqual(result.observed_values["adapter"], "claude")
            self.assertEqual(result.observed_values["control_socket"], CONTROL_SOCKET)
            self.assertEqual(result.observed_values["run_token"], RUN_TOKEN)

            with mock.patch.object(
                supervisor,
                "request_halt",
                return_value=supervisor.ControlResult(ok=True, state="halted", returncode=0),
            ) as halt:
                adapter.halt(result.spawn_handle, dict(result.observed_values))
            halt.assert_called_once()
            self.assertEqual(halt.call_args.args[0], CONTROL_SOCKET)
            self.assertEqual(halt.call_args.args[1], RUN_TOKEN)

    def test_unsupported_spawn_placeholder_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            versions_dir, expected_sha256 = self._write_fake_pin(root)
            adapter = self._adapter(expected_sha256)
            context = DispatchContext(
                dispatch={
                    "dispatch_id": "dispatch_20260627_000000_abcdef12",
                    "policy_name": WORKER_DISPATCH_POLICY,
                },
                recipient={
                    "id": "alpha-worker",
                    "runtime": "claude",
                    "project_root": str(root),
                    "spawn": {
                        "command": "{claude_binary}",
                        "args": ["{subject}"],
                    },
                },
                message={"id": "msg-test"},
                ttl_seconds=30,
                expected_close_by="2026-05-23T00:00:30+00:00",
                db_path=str(root / "agent-comms.sqlite"),
            )

            with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
                with self.assertRaisesRegex(RuntimeError, "unsupported spawn placeholder"):
                    adapter.dispatch(context)

    def test_spawn_requires_policy_bootstrap_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            versions_dir, expected_sha256 = self._write_fake_pin(root)
            adapter = self._adapter(expected_sha256)
            context = DispatchContext(
                dispatch={
                    "dispatch_id": "dispatch_20260627_000000_abcdef12",
                    "policy_name": WORKER_DISPATCH_POLICY,
                },
                recipient={
                    "id": "alpha-worker",
                    "runtime": "claude",
                    "project_root": str(root),
                    "spawn": {
                        "command": "{claude_binary}",
                        "args": [sys.executable, "-c", "import time; time.sleep(30)"],
                    },
                },
                message={"id": "msg-test"},
                ttl_seconds=30,
                expected_close_by="2026-05-23T00:00:30+00:00",
                db_path=str(root / "agent-comms.sqlite"),
            )

            with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
                with self.assertRaisesRegex(RuntimeError, "bootstrap marker"):
                    adapter.dispatch(context)

    def test_policy_env_strips_credentials_and_adds_wake_policy(self) -> None:
        policy = compile_policy(WORKER_DISPATCH_POLICY)

        env = scoped_env(
            {
                "PATH": "/bin",
                "GITHUB_TOKEN": "secret",
                "AGENT_COMMS_ADMIN_TOKEN": "secret",
                "AWS_ACCESS_KEY_ID": "secret",
                "KEEP_ME": "yes",
            },
            policy,
        )

        self.assertEqual(env["WAKE_POLICY"], WORKER_DISPATCH_POLICY)
        self.assertEqual(env["KEEP_ME"], "yes")
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("AGENT_COMMS_ADMIN_TOKEN", env)
        self.assertNotIn("AWS_ACCESS_KEY_ID", env)


if __name__ == "__main__":
    unittest.main()
