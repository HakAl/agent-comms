import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import base64
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_comms import paths, supervisor
from agent_comms.adapters import DispatchContext
from agent_comms.adapters.codex import CodexAdapter
from agent_comms.store import Store, WORKER_DISPATCH_POLICY

# Hermetic supervised-seam identity. The real AF_UNIX bind lives in the
# certification environment (EndToEndSupervisorTest); these runtime-free cells
# mock ``supervisor.spawn_supervised`` so they exercise the adapter wiring
# without a bind, exactly like tests/substrate/test_adapter_supervised_dispatch.
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


def provision_fresh_codex_home(codex_home: Path) -> Path:
    codex_home.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int((now + timedelta(days=365)).timestamp())}).encode()
    ).decode().rstrip("=")
    (codex_home / "auth.json").write_text(
        json.dumps({"access_token": f"x.{payload}.x", "last_refresh": now.isoformat()})
    )
    return codex_home


class CodexAdapterTest(unittest.TestCase):
    def test_spawn_env_codex_home_is_data_driven_per_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = CodexAdapter()
            registry = mock.MagicMock()

            spawn_envs = []
            for index, actor_id in enumerate(("alpha-codex-worker-a", "alpha-codex-worker-b")):
                codex_home = provision_fresh_codex_home(root / f"codex-home-{actor_id[-1]}")
                context = DispatchContext(
                    dispatch={
                        "dispatch_id": f"dispatch_20260101_00000{index + 1}_0000000{index + 1}",
                        "policy_name": WORKER_DISPATCH_POLICY,
                    },
                    recipient={
                        "id": actor_id,
                        "runtime": "codex",
                        "project_root": str(root),
                        "spawn": {
                            "command": sys.executable,
                            "args": [
                                "-c",
                                "import time; time.sleep(30)",
                                f"WakePolicy={WORKER_DISPATCH_POLICY}",
                            ],
                            "env": {"CODEX_HOME": str(codex_home)},
                        },
                    },
                    message={"id": f"msg-{actor_id}"},
                    ttl_seconds=30,
                    expected_close_by="2026-05-23T00:00:30+00:00",
                    db_path=str(root / "agent-comms.sqlite"),
                )

                # The per-recipient CODEX_HOME must reach the env the supervised
                # seam launches the child with (the seam production uses); no
                # AF_UNIX bind is needed to prove the adapter wiring.
                with mock.patch.object(paths, "dispatch_log_path", return_value=root / "worker.log"), \
                     mock.patch.object(supervisor, "janitor_sweep", return_value=[]), \
                     mock.patch.object(supervisor, "reaper_registry", return_value=registry), \
                     mock.patch.object(
                         supervisor, "spawn_supervised", return_value=_ready_spawn(4300 + index)
                     ) as spawn:
                    adapter.dispatch(context)
                spawn_envs.append((str(codex_home), spawn.call_args.kwargs["env"]))

            self.assertEqual(spawn_envs[0][1]["CODEX_HOME"], spawn_envs[0][0])
            self.assertEqual(spawn_envs[1][1]["CODEX_HOME"], spawn_envs[1][0])
            self.assertNotEqual(spawn_envs[0][1]["CODEX_HOME"], spawn_envs[1][1]["CODEX_HOME"])

    def test_spawn_env_rejects_stripped_credential_key_before_child_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            out_file = root / "child.env"
            codex_home = provision_fresh_codex_home(root / "codex-home")
            adapter = CodexAdapter()
            context = DispatchContext(
                dispatch={
                    "dispatch_id": "dispatch_20260101_000010_00000010",
                    "policy_name": WORKER_DISPATCH_POLICY,
                },
                recipient={
                    "id": "alpha-codex-worker",
                    "runtime": "codex",
                    "project_root": str(root),
                    "spawn": {
                        "command": sys.executable,
                        "args": [
                            "-c",
                            "import os, pathlib; pathlib.Path(os.environ['OUT_FILE']).write_text(os.environ.get('ANTHROPIC_API_KEY', 'absent'))",
                            f"WakePolicy={WORKER_DISPATCH_POLICY}",
                        ],
                        "env": {
                            "ANTHROPIC_API_KEY": "secret",
                            "CODEX_HOME": str(codex_home),
                            "OUT_FILE": str(out_file),
                        },
                    },
                },
                message={"id": "msg-test"},
                ttl_seconds=30,
                expected_close_by="2026-05-23T00:00:30+00:00",
                db_path=str(root / "agent-comms.sqlite"),
            )

            with self.assertRaisesRegex(RuntimeError, "spawn.env key ANTHROPIC_API_KEY"):
                adapter.dispatch(context)

            self.assertEqual(adapter._processes, {})
            self.assertFalse(out_file.exists())

    def test_spawn_env_rejects_protected_agent_comms_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = provision_fresh_codex_home(root / "codex-home")
            adapter = CodexAdapter()
            context = DispatchContext(
                dispatch={
                    "dispatch_id": "dispatch_20260101_000020_00000020",
                    "policy_name": WORKER_DISPATCH_POLICY,
                },
                recipient={
                    "id": "alpha-codex-worker",
                    "runtime": "codex",
                    "project_root": str(root),
                    "spawn": {
                        "command": sys.executable,
                        "args": ["-c", "import time; time.sleep(30)", f"WakePolicy={WORKER_DISPATCH_POLICY}"],
                        "env": {"AGENT_COMMS_ACTOR_ID": "attacker", "CODEX_HOME": str(codex_home)},
                    },
                },
                message={"id": "msg-test"},
                ttl_seconds=30,
                expected_close_by="2026-05-23T00:00:30+00:00",
                db_path=str(root / "agent-comms.sqlite"),
            )

            with self.assertRaisesRegex(RuntimeError, "spawn.env key AGENT_COMMS_ACTOR_ID"):
                adapter.dispatch(context)

            self.assertEqual(adapter._processes, {})

    def test_forbidden_spawn_env_becomes_spawn_failed_message_landed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = provision_fresh_codex_home(root / "codex-home")
            store = Store(root / "agent-comms.sqlite")
            store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")
            store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
            store.register_agent_actor(
                "alpha-codex-worker",
                "alpha",
                "worker",
                str(root / "alpha-codex-worker"),
                [],
                owner="alpha-architect",
                runtime="codex",
                spawn={
                    "command": sys.executable,
                    "args": ["-c", "import time; time.sleep(30)", f"WakePolicy={WORKER_DISPATCH_POLICY}"],
                    "env": {"AGENT_COMMS_ADMIN_TOKEN": "secret", "CODEX_HOME": str(codex_home)},
                },
            )
            adapter = CodexAdapter()

            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-codex-worker",
                "forbidden-spawn-env",
                "Work",
                "Body.",
                [],
                adapter_for_runtime=lambda _runtime: adapter,
            )

            self.assertEqual(dispatch["status"], "spawn_failed_message_landed")
            self.assertIn("spawn.env key AGENT_COMMS_ADMIN_TOKEN", dispatch["failure_reason"])
            self.assertEqual(adapter._processes, {})

    def test_dispatch_returns_stable_handle_and_halt_stops_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = provision_fresh_codex_home(root / "codex-home")
            adapter = CodexAdapter()
            context = DispatchContext(
                dispatch={
                    "dispatch_id": "dispatch_20260101_000030_00000030",
                    "policy_name": WORKER_DISPATCH_POLICY,
                },
                recipient={
                    "id": "alpha-codex-worker",
                    "runtime": "codex",
                    "project_root": str(root),
                    "spawn": {
                        "command": sys.executable,
                        "args": [
                            "-c",
                            "import time; time.sleep(30)",
                            f"WakePolicy={WORKER_DISPATCH_POLICY}",
                        ],
                        "env": {"CODEX_HOME": str(codex_home)},
                    },
                },
                message={"id": "msg-test"},
                ttl_seconds=30,
                expected_close_by="2026-05-23T00:00:30+00:00",
                db_path=str(root / "agent-comms.sqlite"),
            )

            registry = mock.MagicMock()
            with mock.patch.object(paths, "dispatch_log_path", return_value=root / "worker.log"), \
                 mock.patch.object(supervisor, "janitor_sweep", return_value=[]), \
                 mock.patch.object(supervisor, "reaper_registry", return_value=registry), \
                 mock.patch.object(supervisor, "spawn_supervised", return_value=_ready_spawn(4321)):
                result = adapter.dispatch(context)

            # The stable handle carries the wrapper pid; READY records the control
            # identity so halt authenticates over the socket, never a PID signal.
            self.assertEqual(result.spawn_handle, "codex:alpha-codex-worker:4321")
            self.assertTrue(result.spawn_handle.startswith("codex:alpha-codex-worker:"))
            self.assertEqual(result.observed_values["adapter"], "codex")
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

    def test_non_codex_runtime_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = CodexAdapter()
            context = DispatchContext(
                dispatch={
                    "dispatch_id": "dispatch-test",
                    "policy_name": WORKER_DISPATCH_POLICY,
                },
                recipient={
                    "id": "alpha-worker",
                    "runtime": "claude",
                    "project_root": str(root),
                    "spawn": {
                        "command": "{claude_binary}",
                        "args": [
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

            with self.assertRaisesRegex(RuntimeError, "not supported by CodexAdapter"):
                adapter.dispatch(context)

    def test_spawn_requires_policy_bootstrap_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = provision_fresh_codex_home(root / "codex-home")
            adapter = CodexAdapter()
            context = DispatchContext(
                dispatch={
                    "dispatch_id": "dispatch_20260101_000040_00000040",
                    "policy_name": WORKER_DISPATCH_POLICY,
                },
                recipient={
                    "id": "alpha-codex-worker",
                    "runtime": "codex",
                    "project_root": str(root),
                    "spawn": {
                        "command": sys.executable,
                        "args": ["-c", "import time; time.sleep(30)"],
                        "env": {"CODEX_HOME": str(codex_home)},
                    },
                },
                message={"id": "msg-test"},
                ttl_seconds=30,
                expected_close_by="2026-05-23T00:00:30+00:00",
                db_path=str(root / "agent-comms.sqlite"),
            )

            with self.assertRaisesRegex(RuntimeError, "bootstrap marker"):
                adapter.dispatch(context)


if __name__ == "__main__":
    unittest.main()
