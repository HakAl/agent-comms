from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_comms import supervisor
from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.adapters.codex import AuthStale, CODEX_AUTH_STALE_AFTER, CodexAdapter
from agent_comms.adapters.fake import FakeAdapter
from agent_comms.spawn import render_spawn
from agent_comms.store import Store, WORKER_DISPATCH_POLICY


NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


def _last_refresh(age: timedelta) -> str:
    return (NOW - age).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write_auth(codex_home: Path, age: timedelta) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(json.dumps({"last_refresh": _last_refresh(age)}))


def _context(runtime: str, actor_id: str, root: Path, spawn: dict) -> DispatchContext:
    project_root = root / actor_id
    project_root.mkdir(parents=True, exist_ok=True)
    return DispatchContext(
        dispatch={"dispatch_id": "dispatch_20260607_120000_abcdef12", "policy_name": WORKER_DISPATCH_POLICY},
        recipient={
            "id": actor_id,
            "runtime": runtime,
            "project_root": str(project_root),
            "spawn": spawn,
        },
        message={"id": f"msg-{actor_id}"},
        ttl_seconds=30,
        expected_close_by="2026-06-07T12:00:30+00:00",
        db_path=str(root / "agent-comms.sqlite"),
    )


def _codex_context(root: Path, codex_home: Path, *, literal: bool = False) -> DispatchContext:
    actor_id = "gamma-codex-worker" if literal else "alpha-codex-worker"
    spawn = render_spawn("codex", actor_id)
    spawn["env"]["CODEX_HOME"] = str(codex_home) if literal else str(codex_home)
    return _context("codex", actor_id, root, spawn)


def _fake_supervised(pid: int = 4321) -> supervisor.SupervisedSpawn:
    run_token = "a" * 32
    return supervisor.SupervisedSpawn(
        popen=types.SimpleNamespace(pid=pid),
        run_token=run_token,
        control_socket=f"/nonexistent/run/s/{run_token}/s",
        child_pid=9000,
        wrapper_pid=pid,
        run_dir=f"/nonexistent/run/s/{run_token}",
    )


@contextlib.contextmanager
def _supervised_boundary(pid: int = 4321):
    """Patch the supervised spawn boundary the adapter now drives.

    The obsolete direct ``subprocess.Popen`` patch stopped the timeout wrapper
    from ever spawning; the adapter now hands the child to
    ``supervisor.spawn_supervised`` (which owns the wrapper + AF_UNIX bind). A
    preflight that PROCEEDS must therefore reach exactly one supervised spawn.
    Mocked hermetically here (no wrapper, no bind) so auth-preflight coverage
    stays runtime-free.
    """
    with mock.patch.object(
        supervisor, "spawn_supervised", return_value=_fake_supervised(pid)
    ) as spawn, mock.patch.object(
        supervisor, "reaper_registry", return_value=mock.MagicMock()
    ), mock.patch.object(supervisor, "janitor_sweep", return_value=[]):
        yield spawn


class CodexAuthPreflightTest(unittest.TestCase):
    def test_t1_fresh_auth_dispatch_spawns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            _write_auth(codex_home, timedelta(hours=1))
            adapter = CodexAdapter()

            with mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW), _supervised_boundary() as spawn:
                result = adapter.dispatch(_codex_context(root, codex_home))

        self.assertIsInstance(result, DispatchStart)
        self.assertEqual(result.spawn_handle, "codex:alpha-codex-worker:4321")
        spawn.assert_called_once()

    def test_t2_stale_auth_raises_authstale_with_recovery_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            _write_auth(codex_home, timedelta(days=20))

            with mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW):
                with self.assertRaises(AuthStale) as raised:
                    CodexAdapter().dispatch(_codex_context(root, codex_home))

        message = str(raised.exception)
        self.assertIn(str(codex_home), message)
        self.assertIn(f"CODEX_HOME={codex_home} codex login", message)

    def test_t3_missing_auth_json_raises_authstale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            codex_home.mkdir()

            with self.assertRaises(AuthStale) as raised:
                CodexAdapter().dispatch(_codex_context(root, codex_home))

        self.assertIn(f"CODEX_HOME={codex_home} codex login", str(raised.exception))

    def test_t4_missing_codex_home_dir_raises_authstale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "missing-codex-home"

            with self.assertRaises(AuthStale) as raised:
                CodexAdapter().dispatch(_codex_context(root, codex_home))

        self.assertIn("CODEX_HOME directory is missing", str(raised.exception))
        self.assertIn(f"CODEX_HOME={codex_home} codex login", str(raised.exception))

    def test_t5_malformed_auth_json_variants_raise_authstale(self) -> None:
        cases = [
            ("invalid-json", "{not json"),
            ("missing-last-refresh", json.dumps({"refresh_token": "redacted"})),
            ("unparseable-last-refresh", json.dumps({"last_refresh": "not-a-date"})),
        ]
        for name, content in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                codex_home = root / "codex-home"
                codex_home.mkdir()
                (codex_home / "auth.json").write_text(content)

                with self.assertRaises(AuthStale) as raised:
                    CodexAdapter().dispatch(_codex_context(root, codex_home))

                self.assertIn(f"CODEX_HOME={codex_home} codex login", str(raised.exception))

    def test_t6_stale_auth_queues_with_lineage_gate_and_does_not_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            _write_auth(codex_home, timedelta(days=20))
            store = Store(root / "agent-comms.sqlite")
            store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")
            store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "architect"), [])
            spawn = render_spawn("codex", "alpha-codex-worker")
            spawn["env"]["CODEX_HOME"] = str(codex_home)
            store.register_agent_actor(
                "alpha-codex-worker",
                "alpha",
                "worker",
                str(root / "worker"),
                [],
                runtime="codex",
                spawn=spawn,
                owner="alpha-architect",
            )

            with (
                mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW),
                mock.patch("agent_comms.dispatch_ledger.require_fresh_module", return_value=None),
                mock.patch("subprocess.Popen") as popen,
            ):
                dispatch = store.dispatch_agent(
                    "alpha-architect",
                    "alpha-codex-worker",
                    "stale-auth",
                    "Work",
                    "Body.",
                    [],
                    adapter_for_runtime=lambda _runtime: CodexAdapter(),
                )

        self.assertEqual(dispatch["status"], "queued")
        self.assertEqual(dispatch["lineage_gate_status"], "token_stale")
        # The queued row's recovery path is the post-expiry refresh, not an
        # operator login message.
        self.assertNotEqual(dispatch["status"], "spawn_failed_message_landed")
        popen.assert_not_called()

    def test_t7_literal_codex_home_is_checked_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            literal_home = root / "literal-codex-home"
            _write_auth(literal_home, timedelta(days=20))

            with mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW):
                with self.assertRaises(AuthStale) as raised:
                    CodexAdapter().dispatch(_codex_context(root, literal_home, literal=True))

        self.assertIn(f"CODEX_HOME={literal_home} codex login", str(raised.exception))

    def test_t8_fake_adapter_unaffected_by_missing_auth_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spawn = render_spawn("fake", "alpha-fake-worker")

            with _supervised_boundary() as spawn_call:
                result = FakeAdapter().dispatch(_context("fake", "alpha-fake-worker", root, spawn))

        self.assertEqual(result.spawn_handle, "fake:alpha-fake-worker:4321")
        spawn_call.assert_called_once()

    def test_t8b_fake_worker_runs_on_the_dispatching_interpreter(self) -> None:
        # Regression for ac-qy8: the rendered command was the bare name
        # "python3", so the worker only imported agent_comms when the first
        # python3 on PATH happened to be the project environment. The spawn
        # block stays portable ({python}) and the adapter resolves it to the
        # interpreter that is running the dispatch, which can import the
        # package by definition.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spawn = render_spawn("fake", "alpha-fake-worker")
            self.assertEqual(spawn["command"], "{python}")

            with _supervised_boundary() as spawn_call:
                FakeAdapter().dispatch(_context("fake", "alpha-fake-worker", root, spawn))

        child_command = spawn_call.call_args.args[0]
        self.assertEqual(child_command[0], sys.executable)
        self.assertEqual(child_command[1:3], ["-m", "agent_comms.adapters.fake_worker"])

    def test_t9_symlink_auth_target_controls_staleness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shared_auth = root / "shared" / "auth.json"
            shared_auth.parent.mkdir()
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "auth.json").symlink_to(shared_auth)

            shared_auth.write_text(json.dumps({"last_refresh": _last_refresh(timedelta(days=20))}))
            with mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW):
                with self.assertRaises(AuthStale):
                    CodexAdapter().dispatch(_codex_context(root, codex_home))

            shared_auth.write_text(json.dumps({"last_refresh": _last_refresh(timedelta(hours=1))}))
            with mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW), _supervised_boundary() as spawn:
                result = CodexAdapter().dispatch(_codex_context(root, codex_home))

        self.assertEqual(result.spawn_handle, "codex:alpha-codex-worker:4321")
        spawn.assert_called_once()

    def test_threshold_boundary_exact_allowed_just_over_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            _write_auth(codex_home, CODEX_AUTH_STALE_AFTER)

            with mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW), _supervised_boundary():
                result = CodexAdapter().dispatch(_codex_context(root, codex_home))

            _write_auth(codex_home, CODEX_AUTH_STALE_AFTER + timedelta(microseconds=1))
            with mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW):
                with self.assertRaises(AuthStale):
                    CodexAdapter().dispatch(_codex_context(root, codex_home))

        self.assertEqual(result.spawn_handle, "codex:alpha-codex-worker:4321")

    def test_env_override_changes_stale_threshold_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            _write_auth(codex_home, timedelta(days=2))

            with (
                mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_AUTH_STALE_DAYS": "1"}),
                mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW),
            ):
                with self.assertRaises(AuthStale):
                    CodexAdapter().dispatch(_codex_context(root, codex_home))

    def test_bad_env_override_falls_back_to_default_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            _write_auth(codex_home, timedelta(days=2))

            with (
                mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_AUTH_STALE_DAYS": "not-an-int"}),
                mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW),
                _supervised_boundary(),
            ):
                result = CodexAdapter().dispatch(_codex_context(root, codex_home))

        self.assertEqual(result.spawn_handle, "codex:alpha-codex-worker:4321")


if __name__ == "__main__":
    unittest.main()
