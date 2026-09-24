"""T2-T4 wiring: ProcessSpawnAdapter.dispatch routes through the supervisor.

These prove the adapter-side integration hermetically, without an AF_UNIX
``bind`` (denied in the bounded worker sandbox). The supervisor spawn boundary,
the spawn-time janitor, and the one module-global parent reaper are mocked so we
can assert:

- the native child command carries NO timeout wrapper / ``--ttl-seconds`` argv
  and NO run token (the TTL and token travel over the bootstrap socketpair);
- the conservative janitor sweeps the protected control root before the spawn;
- exactly one global reaper registration happens on the READY path;
- READY records the supervisor control identity (run token + control socket +
  child/wrapper pid + protocol version) so status()/halt() authenticate over the
  socket instead of parsing a PID;
- pre-READY failure cleans the adapter-owned zdotdir and raises SpawnFailed
  (no false live row, no reaper registration), while the READY path keeps the
  zdotdir for the supervisor/reaper to own.

The real bind/READY end-to-end path is proven in
``tests/substrate/test_supervisor.py::EndToEndSupervisorTest`` in the
certification environment.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import paths, supervisor
from agent_comms.adapters import DispatchContext
from agent_comms.adapters._base import SpawnFailed
from agent_comms.adapters.fake import FakeAdapter
from agent_comms.store import Store, WORKER_DISPATCH_POLICY

ROOT = Path(__file__).resolve().parents[2]
HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"
RUN_TOKEN = "a" * 32
CONTROL_SOCKET = "/protected/run/s/" + RUN_TOKEN + "/s"
RUN_DIR = "/protected/run/s/" + RUN_TOKEN


def _fake_worker_args(db_path: Path) -> list[str]:
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
        f"WakePolicy={WORKER_DISPATCH_POLICY}",
    ]


class SupervisedDispatchWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        self.store.register_actor(HUMAN_ID, "human", "alice")
        self.store.register_agent_actor(
            "alpha-architect", "alpha", "architect", str(self.tmp / "alpha-architect"), []
        )
        self.store.register_agent_actor(
            "alpha-fake-worker",
            "alpha",
            "worker",
            str(self.tmp / "alpha-fake-worker"),
            [],
            runtime="fake",
            spawn={"command": sys.executable, "args": _fake_worker_args(self.db_path)},
            owner="alpha-architect",
        )
        dispatch = self.store.dispatch_agent(
            "alpha-architect", "alpha-fake-worker", "wiring", "Ping", "body", []
        )
        self.dispatch_id = dispatch["dispatch_id"]
        self.message_id = dispatch["message_id"]

        # Keep the worker log out of the real REPO_ROOT/logs tree.
        self._log = self.tmp / "worker.log"
        self._log_patch = mock.patch.object(paths, "dispatch_log_path", return_value=self._log)
        self._log_patch.start()
        self.addCleanup(self._log_patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _context(self) -> DispatchContext:
        return DispatchContext(
            dispatch={"dispatch_id": self.dispatch_id, "policy_name": WORKER_DISPATCH_POLICY,
                      "producer_actor_id": "alpha-architect"},
            recipient={
                "id": "alpha-fake-worker",
                "runtime": "fake",
                "project_root": str(self.tmp / "alpha-fake-worker"),
                "spawn": {"command": sys.executable, "args": _fake_worker_args(self.db_path)},
            },
            message={"id": self.message_id},
            ttl_seconds=90,
            expected_close_by="2026-07-14T00:01:30+00:00",
            db_path=str(self.db_path),
        )

    def _ready_spawn(self, popen_pid: int = 4321) -> supervisor.SupervisedSpawn:
        popen = types.SimpleNamespace(pid=popen_pid)
        return supervisor.SupervisedSpawn(
            popen=popen,
            run_token=RUN_TOKEN,
            control_socket=CONTROL_SOCKET,
            child_pid=9999,
            wrapper_pid=popen_pid,
            run_dir=RUN_DIR,
        )

    def test_ready_path_wires_janitor_spawn_reaper_and_identity(self) -> None:
        adapter = FakeAdapter()
        registry = mock.MagicMock()
        with mock.patch.object(supervisor, "janitor_sweep", return_value=[]) as janitor, \
             mock.patch.object(supervisor, "spawn_supervised", return_value=self._ready_spawn()) as spawn, \
             mock.patch.object(supervisor, "reaper_registry", return_value=registry):
            result = adapter.dispatch(self._context())

        # Janitor swept the protected root (default) with this db before spawning.
        janitor.assert_called_once_with(str(self.db_path))

        # spawn_supervised drove the child; the child command is the raw runtime
        # command + args, with no timeout wrapper argv and no run token.
        self.assertEqual(spawn.call_count, 1)
        child_command = spawn.call_args.args[0]
        joined = " ".join(str(part) for part in child_command)
        self.assertEqual(child_command[0], sys.executable)
        self.assertNotIn("timeout_wrapper", joined)
        self.assertNotIn("--ttl-seconds", joined)
        self.assertNotIn(RUN_TOKEN, joined)
        kwargs = spawn.call_args.kwargs
        self.assertEqual(kwargs["dispatch_id"], self.dispatch_id)
        self.assertEqual(kwargs["db_path"], str(self.db_path))
        self.assertEqual(kwargs["ttl_seconds"], 90.0)
        self.assertEqual(kwargs["kill_after_seconds"], 30.0)
        self.assertEqual(kwargs["cwd"], str(self.tmp / "alpha-fake-worker"))
        self.assertTrue(str(kwargs["zdotdir"]).endswith("empty-zdotdir"))

        # Exactly one global reaper registration, carrying same-run identity.
        registry.register.assert_called_once()
        reg_kwargs = registry.register.call_args.kwargs
        self.assertEqual(reg_kwargs["dispatch_id"], self.dispatch_id)
        self.assertEqual(reg_kwargs["run_token"], RUN_TOKEN)
        self.assertEqual(reg_kwargs["run_dir"], RUN_DIR)
        self.assertEqual(reg_kwargs["db_path"], str(self.db_path))

        # READY records the control identity so status()/halt() authenticate.
        self.assertEqual(result.spawn_handle, "fake:alpha-fake-worker:4321")
        observed = dict(result.observed_values)
        self.assertEqual(observed["run_token"], RUN_TOKEN)
        self.assertEqual(observed["control_socket"], CONTROL_SOCKET)
        self.assertEqual(observed["child_pid"], 9999)
        self.assertEqual(observed["wrapper_pid"], 4321)
        self.assertEqual(observed["protocol_version"], supervisor.PROTOCOL_VERSION)
        self.assertEqual(observed["adapter"], "fake")
        self.assertEqual(observed["worker_log"], str(self._log))

    def test_ready_path_keeps_zdotdir_for_supervisor(self) -> None:
        adapter = FakeAdapter()
        registry = mock.MagicMock()
        with mock.patch.object(supervisor, "janitor_sweep", return_value=[]), \
             mock.patch.object(supervisor, "spawn_supervised", return_value=self._ready_spawn()), \
             mock.patch.object(supervisor, "reaper_registry", return_value=registry), \
             mock.patch.object(FakeAdapter, "_cleanup_zdotdir") as cleanup:
            result = adapter.dispatch(self._context())
        # On READY the adapter does NOT tear the zdotdir down: the supervisor and
        # reaper own that after READY. It is retained under the handle instead.
        cleanup.assert_not_called()
        self.assertIn(result.spawn_handle, adapter._zdotdirs)

    def test_pre_ready_failure_cleans_zdotdir_and_raises_spawnfailed(self) -> None:
        adapter = FakeAdapter()
        registry = mock.MagicMock()
        with mock.patch.object(supervisor, "janitor_sweep", return_value=[]), \
             mock.patch.object(
                 supervisor, "spawn_supervised",
                 side_effect=supervisor.SupervisorError("AF_UNIX bind denied"),
             ), \
             mock.patch.object(supervisor, "reaper_registry", return_value=registry), \
             mock.patch.object(FakeAdapter, "_cleanup_zdotdir") as cleanup:
            with self.assertRaises(SpawnFailed) as ctx:
                adapter.dispatch(self._context())
        self.assertIn("did not reach READY", str(ctx.exception))
        # Adapter owns the zdotdir it created pre-READY; it must clean exactly it.
        cleanup.assert_called_once()
        # No false live row: nothing registered with the reaper on a spawn failure.
        registry.register.assert_not_called()
        self.assertEqual(adapter._zdotdirs, {})

    def test_janitor_failure_never_blocks_spawn(self) -> None:
        adapter = FakeAdapter()
        registry = mock.MagicMock()
        with mock.patch.object(supervisor, "janitor_sweep", side_effect=OSError("sweep boom")), \
             mock.patch.object(supervisor, "spawn_supervised", return_value=self._ready_spawn()), \
             mock.patch.object(supervisor, "reaper_registry", return_value=registry):
            result = adapter.dispatch(self._context())
        self.assertEqual(result.spawn_handle, "fake:alpha-fake-worker:4321")
        registry.register.assert_called_once()


if __name__ == "__main__":
    unittest.main()
