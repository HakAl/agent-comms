from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import paths, supervisor
from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.adapters._base import ProcessSpawnAdapter, SpawnFailed
from agent_comms.policies import CompiledPolicy, compile_policy
from agent_comms.store import Store, WORKER_DISPATCH_POLICY

_RUN_TOKEN = "a" * 32


def _fake_spawn(pid: int, popen: object | None = None) -> supervisor.SupervisedSpawn:
    return supervisor.SupervisedSpawn(
        popen=popen if popen is not None else types.SimpleNamespace(pid=pid),
        run_token=_RUN_TOKEN,
        control_socket=f"/nonexistent/run/s/{_RUN_TOKEN}/s",
        child_pid=getattr(popen, "pid", 9000) if popen is not None else 9000,
        wrapper_pid=pid,
        run_dir=f"/nonexistent/run/s/{_RUN_TOKEN}",
    )


@contextlib.contextmanager
def _supervised_ready(pid: int = 4321, capture: dict | None = None):
    """Supervised READY with no AF_UNIX bind and no child launch.

    The adapter proceeds exactly as it does on a real READY: it returns a
    ``DispatchStart`` carrying the supervisor control identity. ``capture``
    records the ``stdout`` handle and child command the adapter handed the
    supervisor so fd-lifetime and argv assertions stay provable hermetically.
    """

    def _spawn(child_command, *, stdout=None, **_kwargs):  # type: ignore[no-untyped-def]
        if capture is not None:
            capture["stdout"] = stdout
            capture["child_command"] = [str(part) for part in child_command]
        return _fake_spawn(pid)

    with mock.patch.object(supervisor, "spawn_supervised", side_effect=_spawn) as spawn, mock.patch.object(
        supervisor, "reaper_registry", return_value=mock.MagicMock()
    ), mock.patch.object(supervisor, "janitor_sweep", return_value=[]):
        yield spawn


@contextlib.contextmanager
def _supervised_launch(*, seed_ledger: bool = False, record_exit: int | None = None):
    """Simulate the wrapper without an AF_UNIX bind: launch the child for real
    with the adapter-built env/cwd/stdout (so worker-log capture and ZDOTDIR
    isolation are genuinely exercised), wait for it, and -- when the dispatch
    has a real ledger -- record the same-run worker exit the supervisor writes.
    """

    def _spawn(
        child_command,
        *,
        dispatch_id=None,
        db_path=None,
        env=None,
        cwd=None,
        stdout=None,
        stderr=None,
        **_kwargs,
    ):  # type: ignore[no-untyped-def]
        proc = subprocess.Popen(
            [str(part) for part in child_command],
            env=env,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        proc.wait()
        if seed_ledger and db_path and dispatch_id:
            returncode = record_exit if record_exit is not None else proc.returncode
            supervisor.merge_supervisor_observed(
                str(db_path),
                str(dispatch_id),
                run_token=_RUN_TOKEN,
                wrapper_pid=proc.pid,
                child_pid=proc.pid,
                control_socket=f"/nonexistent/run/s/{_RUN_TOKEN}/s",
            )
            supervisor.record_worker_exit(
                str(db_path), str(dispatch_id), _RUN_TOKEN, returncode=returncode, source="child"
            )
        return _fake_spawn(proc.pid, popen=proc)

    with mock.patch.object(supervisor, "spawn_supervised", side_effect=_spawn) as spawn, mock.patch.object(
        supervisor, "reaper_registry", return_value=mock.MagicMock()
    ), mock.patch.object(supervisor, "janitor_sweep", return_value=[]):
        yield spawn


class TestAdapter(ProcessSpawnAdapter):
    runtime_label = "fake"
    supported_runtimes = ("fake",)


def _context(root: Path, *, dispatch_id: str = "dispatch_20260609_011536_cdd9b013", spawn: dict | None = None) -> DispatchContext:
    project_root = root / "worker"
    project_root.mkdir(parents=True, exist_ok=True)
    if spawn is None:
        spawn = _spawn("import time; time.sleep(1)")
    return DispatchContext(
        dispatch={"dispatch_id": dispatch_id, "policy_name": WORKER_DISPATCH_POLICY},
        recipient={
            "id": "alpha-fake-worker",
            "runtime": "fake",
            "project_root": str(project_root),
            "spawn": spawn,
        },
        message={"id": "msg_20260609_011536_cdd9b013"},
        ttl_seconds=30,
        expected_close_by="2026-06-09T01:16:06+00:00",
        db_path=str(root / "agent-comms.sqlite"),
    )


def _spawn(script: str) -> dict:
    return {
        "command": sys.executable,
        "args": [
            "-c",
            script,
            "WakePolicy=worker_dispatch_readwrite_bounded",
        ],
    }


def _live_process() -> mock.Mock:
    process = mock.Mock()
    process.pid = 4321
    process.wait.side_effect = subprocess.TimeoutExpired(["worker"], 2.0)
    return process


def _best_effort_halt(adapter, result) -> None:  # type: ignore[no-untyped-def]
    # Authenticated teardown via the supervisor control identity; tolerate an
    # already-gone wrapper. There is no PID-parsed fallback to lean on.
    try:
        adapter.halt(result.spawn_handle, dict(result.observed_values))
    except Exception:
        pass


def _store(root: Path, spawn: dict) -> Store:
    store = Store(root / "agent-comms.sqlite")
    store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "architect"), [])
    store.register_agent_actor(
        "alpha-fake-worker",
        "alpha",
        "worker",
        str(root / "worker"),
        [],
        runtime="fake",
        spawn=spawn,
        owner="alpha-architect",
    )
    return store


def _dispatch_logs_in(temp_dir: str):
    """Point the dispatch log directory at ``<temp_dir>/logs/dispatch``."""
    return mock.patch.dict(os.environ, {"AGENT_COMMS_DISPATCH_LOG_DIR": str(Path(temp_dir) / "logs" / "dispatch")})


class SpawnHardeningTest(unittest.TestCase):
    def test_f1_log_capture_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, _dispatch_logs_in(temp_dir):
            with _supervised_launch():
                result = TestAdapter().dispatch(_context(Path(temp_dir), spawn=_spawn("print('worker-out')")))
            self.assertIsInstance(result, DispatchStart)
            worker_log = Path(result.observed_values["worker_log"])
            self.assertEqual(worker_log.read_text(), "worker-out\n")

    def test_f1_path_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, _dispatch_logs_in(temp_dir):
            with self.assertRaises(ValueError):
                paths.dispatch_log_path("../dispatch_20260609_011536_cdd9b013")

    def test_f1_path_accepts_real_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, _dispatch_logs_in(temp_dir):
            path = paths.dispatch_log_path("dispatch_20260609_011536_cdd9b013")

        self.assertEqual(path.name, "dispatch_20260609_011536_cdd9b013.log")
        self.assertEqual(path.parent, (Path(temp_dir) / "logs" / "dispatch").resolve())

    def test_f1_parent_fd_closed_on_return(self) -> None:
        captured: dict = {}

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            with _supervised_ready(capture=captured):
                TestAdapter().dispatch(_context(Path(temp_dir)))

        # The adapter opens the worker log, hands it to the supervisor as
        # stdout, and closes its own copy once the supervised spawn returns.
        self.assertTrue(captured["stdout"].closed)

    def test_f1_parent_fd_closed_on_raise(self) -> None:
        captured = {}

        def popen(*args, **kwargs):  # type: ignore[no-untyped-def]
            captured["stdout"] = kwargs["stdout"]
            raise RuntimeError("boom")

        # The spawn-time janitor is mocked exactly like the adjacent
        # ``_supervised_ready``/``_supervised_launch`` helpers: the fd-close-on-
        # raise assertion is about the adapter's log handle, not about scanning a
        # real control root. Without this, ``dispatch`` would drive the REAL
        # janitor over the default ``~/.agent-comms/run/s`` with an uninitialized
        # temp DB (see ``test_f1_raise_path_never_scans_default_control_root``).
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            with mock.patch("subprocess.Popen", side_effect=popen), mock.patch.object(
                supervisor, "janitor_sweep", return_value=[]
            ):
                with self.assertRaises(RuntimeError):
                    TestAdapter().dispatch(_context(Path(temp_dir)))

        self.assertTrue(captured["stdout"].closed)

    def test_f1_raise_path_never_scans_default_control_root(self) -> None:
        # T12 guard: the fd-close-on-raise path builds a DispatchContext whose
        # temp DB is uninitialized. Dispatch must not let that DB drive the
        # spawn-time janitor over the FIXED default control root
        # (~/.agent-comms/run/s): pairing an uninitialized test DB with the live
        # default root is the isolation defect this proves absent. We fail loudly
        # if os.scandir is ever invoked on the default control root during the
        # raise path. Isolating the janitor (as the F1 helpers do) is what keeps
        # this green; the assertion is what proves the isolation is real.
        default_root = supervisor._DEFAULT_CONTROL_ROOT
        scanned: list[str] = []
        real_scandir = os.scandir

        def guard_scandir(path=".", *args, **kwargs):  # type: ignore[no-untyped-def]
            try:
                if Path(path) == default_root:
                    scanned.append(str(path))
            except TypeError:
                pass
            return real_scandir(path, *args, **kwargs)

        def popen(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            with mock.patch("subprocess.Popen", side_effect=popen), mock.patch.object(
                supervisor, "janitor_sweep", return_value=[]
            ), mock.patch("os.scandir", side_effect=guard_scandir):
                with self.assertRaises(RuntimeError):
                    TestAdapter().dispatch(_context(Path(temp_dir)))

        self.assertEqual(
            scanned,
            [],
            "spawn-time janitor scanned the fixed default control root with an uninitialized test DB",
        )

    def test_f1_respawn_appends(self) -> None:
        dispatch_id = "dispatch_20260609_011536_cdd9b013"
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            adapter = TestAdapter()
            with _supervised_launch():
                adapter.dispatch(_context(Path(temp_dir), dispatch_id=dispatch_id, spawn=_spawn("print('first')")))
                adapter.dispatch(_context(Path(temp_dir), dispatch_id=dispatch_id, spawn=_spawn("print('second')")))
            worker_log = paths.dispatch_log_path(dispatch_id)
            self.assertEqual(worker_log.read_text(), "first\nsecond\n")

    def test_f2_alive_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            with mock.patch.dict(os.environ, {"AGENT_COMMS_SPAWN_GRACE_SECONDS": "0.05"}):
                with _supervised_ready():
                    adapter = TestAdapter()
                    result = adapter.dispatch(_context(Path(temp_dir), spawn=_spawn("import time; time.sleep(1)")))
                    _best_effort_halt(adapter, result)

        self.assertEqual(result.spawn_handle, "fake:alpha-fake-worker:" + result.spawn_handle.rsplit(":", 1)[1])

    def test_f2_halt_rejects_none_spawn_handle_without_side_effects(self) -> None:
        adapter = TestAdapter()
        adapter._processes["fake:alpha-fake-worker:4321"] = _live_process()
        zdotdir = Path(tempfile.gettempdir()) / "agent-comms-zdotdir-sentinel" / "empty-zdotdir"
        adapter._zdotdirs["fake:alpha-fake-worker:4321"] = zdotdir
        processes = dict(adapter._processes)
        zdotdirs = dict(adapter._zdotdirs)

        with mock.patch("os.killpg") as killpg:
            with self.assertRaises(RuntimeError) as raised:
                adapter.halt(None)  # type: ignore[arg-type]

        self.assertIn("invalid fake spawn_handle: None", str(raised.exception))
        self.assertIsInstance(raised.exception, RuntimeError)
        killpg.assert_not_called()
        self.assertEqual(adapter._processes, processes)
        self.assertEqual(adapter._zdotdirs, zdotdirs)

    def test_f2_halt_rejects_non_string_spawn_handle_without_side_effects(self) -> None:
        adapter = TestAdapter()
        adapter._processes["fake:alpha-fake-worker:4321"] = _live_process()
        zdotdir = Path(tempfile.gettempdir()) / "agent-comms-zdotdir-sentinel" / "empty-zdotdir"
        adapter._zdotdirs["fake:alpha-fake-worker:4321"] = zdotdir
        processes = dict(adapter._processes)
        zdotdirs = dict(adapter._zdotdirs)

        with mock.patch("os.killpg") as killpg:
            with self.assertRaises(RuntimeError) as raised:
                adapter.halt(123)  # type: ignore[arg-type]

        self.assertIn("invalid fake spawn_handle: 123", str(raised.exception))
        killpg.assert_not_called()
        self.assertEqual(adapter._processes, processes)
        self.assertEqual(adapter._zdotdirs, zdotdirs)

    def test_f2_fast_clean_exit_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            with _supervised_ready():
                result = TestAdapter().dispatch(_context(Path(temp_dir), spawn=_spawn("raise SystemExit(0)")))

        # A clean fast exit is NOT a spawn failure at the adapter: READY was sent
        # after launch, so the adapter returns a DispatchStart and the ledger
        # settles the exit-before-close outcome (proven in the store tests below).
        self.assertIn("worker_log", result.observed_values)

    def test_f2_exit_124_within_grace_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            with _supervised_ready():
                result = TestAdapter().dispatch(_context(Path(temp_dir), spawn=_spawn("raise SystemExit(124)")))

        self.assertIsInstance(result, DispatchStart)
        self.assertIn("worker_log", result.observed_values)
        self.assertTrue(result.spawn_handle.startswith("fake:alpha-fake-worker:"))

    def test_f2_pre_ready_supervisor_failure_lands_spawn_failed(self) -> None:
        # The real spawn-failure path: a typed pre-READY SupervisorError (for
        # example a bootstrap BrokenPipe/EOF normalised by spawn_supervised) is
        # surfaced as SpawnFailed and the ledger records
        # spawn_failed_message_landed with the log path -- never a false
        # in_flight, never a raw OSError.
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            with self.assertRaises(SpawnFailed) as raised:
                with mock.patch.object(
                    supervisor,
                    "spawn_supervised",
                    side_effect=supervisor.SupervisorError("bootstrap read failed before READY: broken pipe"),
                ), mock.patch.object(
                    supervisor, "reaper_registry", return_value=mock.MagicMock()
                ), mock.patch.object(supervisor, "janitor_sweep", return_value=[]):
                    TestAdapter().dispatch(_context(Path(temp_dir), spawn=_spawn("raise SystemExit(17)")))

        message = str(raised.exception)
        self.assertIn("did not reach READY", message)
        self.assertIn("broken pipe", message)
        self.assertIn("dispatch_20260609_011536_cdd9b013.log", message)

    def test_f2_early_nonzero_worker_exit_becomes_early_dlq(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            root = Path(temp_dir)
            store = _store(root, _spawn("raise SystemExit(17)"))
            with _supervised_launch(seed_ledger=True):
                dispatch = store.dispatch_agent(
                    "alpha-architect",
                    "alpha-fake-worker",
                    "nonzero-early",
                    "Worker dispatch",
                    "Do work.",
                    [],
                    adapter_for_runtime=lambda _runtime: TestAdapter(),
                )

        # A worker that exits nonzero before closing its trigger is terminalized
        # to DLQ (worker_exited_before_close), never a false in_flight and never
        # a spawn failure. The child's own returncode rides same-run exit
        # evidence recorded by the supervisor.
        self.assertEqual(dispatch["status"], "dlq")
        self.assertEqual(dispatch["failure_reason"], "worker_exited_before_close")
        self.assertEqual(dispatch["observed_values"]["worker_exit"]["returncode"], 17)
        self.assertEqual(dispatch["observed_values"]["worker_exit"]["source"], "child")
        self.assertIn("worker_log", dispatch["observed_values"])

    def test_f2_failure_reason_excludes_raw_tail(self) -> None:
        token = "sk-proj-secret-token-shaped-value"  # hygiene:allow token-shaped test value
        script = f"import sys; print({token!r}); print({token!r}, file=sys.stderr); raise SystemExit(9)"
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            root = Path(temp_dir)
            store = _store(root, _spawn(script))
            with _supervised_launch(seed_ledger=True):
                dispatch = store.dispatch_agent(
                    "alpha-architect",
                    "alpha-fake-worker",
                    "raw-tail-secret",
                    "Worker dispatch",
                    "Do work.",
                    [],
                    adapter_for_runtime=lambda _runtime: TestAdapter(),
                )
            actions = store.reconcile_dispatches(lambda _runtime: TestAdapter(), human_actor_id="01M36YTJV9XBW95S6ZWV47C4RG")
            page_id = next(action["message_id"] for action in actions if action["status"] == "producer_paged")
            page = store.read_message("alpha-architect", page_id)
            worker_log = Path(dispatch["observed_values"]["worker_log"])
            # Early DLQ carries no raw worker output: the reason is the fixed
            # classification and the producer page names only the log PATH, while
            # the secret-shaped token stays confined to the worker log file.
            self.assertEqual(dispatch["status"], "dlq")
            self.assertEqual(dispatch["failure_reason"], "worker_exited_before_close")
            self.assertNotIn(token, dispatch["failure_reason"])
            self.assertNotIn(token, page["subject"])
            self.assertNotIn(token, page["body"])
            self.assertIn(token, worker_log.read_text())

    def test_f2_end_to_end_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            root = Path(temp_dir)
            store = _store(root, _spawn("raise SystemExit(23)"))
            with _supervised_launch(seed_ledger=True):
                dispatch = store.dispatch_agent(
                    "alpha-architect",
                    "alpha-fake-worker",
                    "nonzero-ledger",
                    "Worker dispatch",
                    "Do work.",
                    [],
                    adapter_for_runtime=lambda _runtime: TestAdapter(),
                )

        self.assertEqual(dispatch["status"], "dlq")
        self.assertEqual(dispatch["failure_reason"], "worker_exited_before_close")
        self.assertEqual(dispatch["observed_values"]["worker_exit"]["returncode"], 23)
        self.assertEqual(
            dispatch["observed_values"]["early_dlq_evidence"]["classification"],
            "worker_exited_before_close",
        )

    def test_f2_child_exit_124_is_worker_exit_not_wrapper_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            root = Path(temp_dir)
            store = _store(root, _spawn("raise SystemExit(124)"))
            with _supervised_launch(seed_ledger=True):
                dispatch = store.dispatch_agent(
                    "alpha-architect",
                    "alpha-fake-worker",
                    "exit-124-ledger",
                    "Worker dispatch",
                    "Do work.",
                    [],
                    adapter_for_runtime=lambda _runtime: TestAdapter(),
                )

        # A child that exits 124 carries its OWN returncode with source=child; it
        # rides the same worker-exit-before-close DLQ path as any other nonzero
        # exit and is never confused with the wrapper's TTL timeout (source
        # timeout) nor misread as a spawn failure.
        self.assertEqual(dispatch["status"], "dlq")
        self.assertEqual(dispatch["failure_reason"], "worker_exited_before_close")
        worker_exit = dispatch["observed_values"]["worker_exit"]
        self.assertEqual(worker_exit["returncode"], 124)
        self.assertEqual(worker_exit["source"], "child")

    def test_f2_grace_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(paths, "REPO_ROOT", Path(temp_dir)):
            with mock.patch.dict(os.environ, {"AGENT_COMMS_SPAWN_GRACE_SECONDS": "0.05"}):
                with _supervised_ready():
                    adapter = TestAdapter()
                    result = adapter.dispatch(_context(Path(temp_dir), spawn=_spawn("import time; time.sleep(1)")))
                    _best_effort_halt(adapter, result)
            with mock.patch.dict(os.environ, {"AGENT_COMMS_SPAWN_GRACE_SECONDS": "not-a-float"}):
                with _supervised_ready():
                    clean = TestAdapter().dispatch(_context(Path(temp_dir), spawn=_spawn("raise SystemExit(0)")))

        self.assertIn("worker_log", result.observed_values)
        self.assertIn("worker_log", clean.observed_values)

    def test_f3_allowlist(self) -> None:
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        context = _context(Path(tempfile.gettempdir()))
        adapter = TestAdapter()
        with mock.patch.dict(
            os.environ,
            {
                "PATH": "/bin",
                "HOME": "/tmp/home",
                "UV_RUN_RECURSION_DEPTH": "3",
                "VIRTUAL_ENV": "/tmp/venv",
                "OPENAI_API_KEY": "secret",
            },
            clear=True,
        ):
            env = adapter._build_env(context, policy, {"env": {"CODEX_HOME": "/tmp/codex"}})

        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["HOME"], "/tmp/home")
        self.assertNotIn("UV_RUN_RECURSION_DEPTH", env)
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["CODEX_HOME"], "/tmp/codex")
        self.assertEqual(env["AGENT_COMMS_ACTOR_ID"], "alpha-fake-worker")
        self.assertEqual(env["WAKE_POLICY"], WORKER_DISPATCH_POLICY)
        zdotdir = Path(env["ZDOTDIR"])
        self.assertTrue(zdotdir.is_dir())
        self.assertNotEqual(
            os.path.commonpath([str(zdotdir), str(context.recipient["project_root"])]),
            str(context.recipient["project_root"]),
        )
        adapter._cleanup_zdotdir(zdotdir)

    def test_f3_zdotdir_ignores_worker_runtime_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context = _context(root)
            project_root = Path(str(context.recipient["project_root"]))
            outside = root / "outside-target"
            attacked_zdotdir = outside / "empty-zdotdir"
            attacked_zdotdir.mkdir(parents=True)
            sentinel = attacked_zdotdir / "sentinel.txt"
            sentinel.write_text("keep")
            (project_root / ".agent-comms-runtime").symlink_to(outside)

            adapter = TestAdapter()
            env = adapter._build_env(context, compile_policy(WORKER_DISPATCH_POLICY), {})
            zdotdir = Path(env["ZDOTDIR"])

            self.assertTrue(sentinel.exists())
            self.assertEqual(sentinel.read_text(), "keep")
            self.assertTrue(zdotdir.is_dir())
            self.assertNotEqual(os.path.commonpath([str(zdotdir), str(project_root)]), str(project_root))
            self.assertNotEqual(os.path.commonpath([str(zdotdir), str(outside)]), str(outside))
            adapter._cleanup_zdotdir(zdotdir)

    def test_f3_denylist_runs_after_allowlist(self) -> None:
        policy = CompiledPolicy(
            name=WORKER_DISPATCH_POLICY,
            version="v1",
            mcp_allowed_tools=frozenset(),
            mcp_denied_tools=frozenset(),
            hook_denied_tools=frozenset(),
            stripped_env_prefixes=(),
            stripped_env_names=frozenset({"PATH", "HOME"}),
            env={},
            bootstrap_marker="WakePolicy=worker_dispatch_readwrite_bounded",
        )
        context = _context(Path(tempfile.gettempdir()))
        with mock.patch.dict(os.environ, {"PATH": "/bin", "HOME": "/tmp/home", "TERM": "xterm"}, clear=True):
            env = TestAdapter()._build_env(context, policy, {})

        self.assertNotIn("PATH", env)
        self.assertNotIn("HOME", env)
        self.assertEqual(env["TERM"], "xterm")

    def test_f3_zdotdir_blocks_parent_home_zshenv_secret_rederivation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            operator_home = root / "operator-home"
            operator_home.mkdir()
            secret_path = operator_home / "admin-token"
            secret_path.write_text("operator-secret")
            (operator_home / ".zshenv").write_text(
                f"export RC_DERIVED_SECRET=\"$(cat {shlex.quote(str(secret_path))})\"\n"
            )
            out_file = root / "worker" / "rc-secret.txt"
            worker_script = (
                "import os, pathlib; "
                f"pathlib.Path({str(out_file)!r}).write_text(os.environ.get('RC_DERIVED_SECRET', ''))"
            )
            zsh_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(worker_script)}"
            spawn = {
                "command": "/bin/zsh",
                "args": ["-lc", zsh_command, f"WakePolicy={WORKER_DISPATCH_POLICY}"],
            }

            with mock.patch.dict(os.environ, {"HOME": str(operator_home), "PATH": os.environ["PATH"]}, clear=True):
                adapter = TestAdapter()
                with _supervised_launch():
                    result = adapter.dispatch(_context(root, spawn=spawn))

            adapter._processes[result.spawn_handle].wait(timeout=5)
            self.assertEqual(out_file.read_text(), "")


if __name__ == "__main__":
    unittest.main()
