"""T2/T4: per-dispatch supervisor, control protocol, reaper, and janitor.

Design note on the OS sandbox: several worker environments deny ``AF_UNIX``
``bind()`` outright (``[Errno 1] Operation not permitted``). To keep the whole
mechanism provable there, the control-server request handling is exercised over
a connected ``socket.socketpair`` (which needs no ``bind``), the reaper is
exercised over plain exited subprocesses, and the SQL evidence / janitor / path
logic is exercised directly. The single class that must actually ``bind`` and
serve over a real control socket end-to-end skips loudly when the sandbox denies
``bind``; it runs for real in the certification environment. The skip reason is
recorded as environmental evidence, never silently swallowed.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import supervisor
from agent_comms.adapters._base import ProcessSpawnAdapter, SupervisorUnreachable
from agent_comms.store import Store

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "agent_comms" / "timeout_wrapper.py"

SLEEPER = [sys.executable, "-c", "import sys, time; time.sleep(float(sys.argv[1]))", "30"]
QUICK_EXIT = [sys.executable, "-c", "raise SystemExit(0)"]

# Revision 7 F2: the complete, explicitly-verified barrier facts a producer
# passes into ``record_registered_wrapper_reap`` for a fully-drained/cleaned
# reap. Tests that exercise the recorder's persistence / idempotency / conflict
# / malformed logic (not the physical barriers) pass these so the fact-guard
# admits the write, then assert the persistence behavior under test.
_COMPLETE_REAP_FACTS = dict(
    registered_wrapper_reaped=True,
    native_process_group_drained=True,
    owned_artifacts_absent={"run_dir": True, "control_socket": True, "zdotdir_parent": True},
)

# F1a/F1b real process-group fixtures. A ``start_new_session`` leader spawns a
# descendant that inherits the leader's process group and IGNORES SIGTERM, so
# only a GROUP SIGKILL removes it. The descendant publishes readiness (after its
# SIG_IGN handler is installed) via a file so synchronization is deterministic
# and bounded. The leader either sleeps (leader survives SIGTERM until signalled)
# or exits at once (leader already gone at teardown), selected by argv.
_STUBBORN_DESC_SRC = (
    "import os,signal,sys,time;"
    "sync=sys.argv[1];"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    "open(os.path.join(sync,'desc.ready'),'w').close();"
    "time.sleep(120)"
)
#
# The descendant is launched with ``os.posix_spawn`` rather than
# ``subprocess.Popen`` so the leader holds no live ``Popen`` object: in "exit"
# mode the leader reaches normal interpreter teardown WHILE the descendant is
# still (intentionally) alive, and a lingering ``Popen`` would emit a
# "subprocess still running" ``ResourceWarning``. The descendant's group and
# SIGTERM-ignoring behavior are unchanged (no ``setpgid``, so it inherits the
# leader's process group); its std fds go to /dev/null so it never keeps an
# inherited stderr pipe open. The ``desc.pid`` file is closed deterministically.
_GROUP_LEADER_SRC = (
    "import os,sys,time;"
    "sync,mode,desc_src=sys.argv[1],sys.argv[2],sys.argv[3];"
    "fa=[(os.POSIX_SPAWN_OPEN,1,os.devnull,os.O_WRONLY,0),"
    "(os.POSIX_SPAWN_OPEN,2,os.devnull,os.O_WRONLY,0)];"
    "pid=os.posix_spawn(sys.executable,[sys.executable,'-c',desc_src,sync],os.environ,file_actions=fa);"
    "f=open(os.path.join(sync,'desc.pid'),'w');f.write(str(pid));f.close();"
    "open(os.path.join(sync,'leader.ready'),'w').close();"
    "(time.sleep(120) if mode=='sleep' else None)"
)


def _af_unix_bind_supported() -> bool:
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "s")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.bind(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()
        try:
            os.unlink(path)
        except OSError:
            pass


_BIND_OK = _af_unix_bind_supported()
_BIND_SKIP = (
    "AF_UNIX bind() denied by the OS sandbox (Operation not permitted); the "
    "end-to-end supervisor bind/READY/STATUS/HALT path runs in the certification "
    "environment. Recorded as environmental evidence, not a passing skip."
)


def _seed_dispatch(store: Store, root: Path, key: str = "supervisor-test") -> str:
    store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
    store.register_agent_actor("alpha-worker", "alpha", "worker", str(root / "alpha-worker"), [], owner="alpha-architect")
    dispatch = store.dispatch_agent("alpha-architect", "alpha-worker", key, "subject", "body", [])
    return dispatch["dispatch_id"]


def _observed(store: Store, dispatch_id: str) -> dict:
    with store.connection() as conn:
        row = conn.execute(
            "select observed_values_json from dispatch_ledger where dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
    return json.loads(row["observed_values_json"] or "{}")


def _set_status(store: Store, dispatch_id: str, status: str) -> None:
    with store.connection() as conn:
        conn.execute("update dispatch_ledger set status = ? where dispatch_id = ?", (status, dispatch_id))


def _wait_until(predicate, timeout: float = 8.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive but not signalable
        return True


class SupervisorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        self.dispatch_id = _seed_dispatch(self.store, self.tmp)
        self.control_root = self.tmp / "s"
        self._children: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for child in self._children:
            if child.poll() is None:
                try:
                    child.kill()
                except OSError:
                    pass
                try:
                    child.wait(timeout=5)
                except Exception:
                    pass
        self._tmp.cleanup()

    def _spawn_child(self, command=SLEEPER) -> subprocess.Popen:
        child = subprocess.Popen(command, start_new_session=True)
        self._children.append(child)
        return child

    def _drained_pgid(self) -> int:
        """The pgid of an EXITED ``start_new_session`` child: its process group
        is empty, so the Revision 7 native-child-group barrier
        (``process_group_empty``) is satisfiable. Used by reaper fixtures to pin
        a registered child group that is already drained."""
        child = subprocess.Popen(QUICK_EXIT, start_new_session=True)
        self._children.append(child)
        _wait_until(lambda: child.poll() is not None)
        child.wait()
        return child.pid


# --------------------------------------------------------------------------- #
# Path / mode / identity (no bind)
# --------------------------------------------------------------------------- #


class PathAndModeTest(SupervisorTestBase):
    def test_create_run_dir_is_strict_and_0700(self) -> None:
        token = supervisor.new_run_token()
        run_dir = supervisor.create_run_dir(self.control_root, token)
        self.assertTrue(supervisor.is_strict_run_dir_name(run_dir.name))
        self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.control_root.stat().st_mode), 0o700)

    def test_run_token_is_32_hex_and_names_are_strict(self) -> None:
        token = supervisor.new_run_token()
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertTrue(supervisor.is_strict_run_dir_name(token))
        self.assertFalse(supervisor.is_strict_run_dir_name("../escape"))
        self.assertFalse(supervisor.is_strict_run_dir_name("short"))
        self.assertFalse(supervisor.is_strict_run_dir_name(token + "z"))

    def test_run_dir_for_rejects_non_strict_token(self) -> None:
        with self.assertRaises(ValueError):
            supervisor.run_dir_for(self.control_root, "../etc")

    def test_sun_path_assertion_rejects_overlength(self) -> None:
        supervisor.assert_sun_path_ok(self.control_root / ("a" * 8) / "s")
        with self.assertRaises(supervisor.SunPathTooLong):
            supervisor.assert_sun_path_ok(Path("/" + "x" * 200) / "s")

    def test_default_control_root_is_fixed_and_overridable(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(supervisor.CONTROL_ROOT_ENV, None)
            self.assertEqual(supervisor.control_root(), Path.home() / ".agent-comms" / "run" / "s")
        with mock.patch.dict(os.environ, {supervisor.CONTROL_ROOT_ENV: "/tmp/x"}):
            self.assertEqual(supervisor.control_root(), Path("/tmp/x"))


class MarkerTest(SupervisorTestBase):
    def test_marker_roundtrips(self) -> None:
        token = supervisor.new_run_token()
        run_dir = supervisor.create_run_dir(self.control_root, token)
        supervisor.write_owner_marker(run_dir, dispatch_id=self.dispatch_id, run_token=token, wrapper_pid=42)
        marker = supervisor.read_owner_marker(run_dir)
        assert marker is not None
        self.assertEqual(marker["marker_version"], supervisor.MARKER_VERSION)
        self.assertEqual(marker["dispatch_id"], self.dispatch_id)
        self.assertEqual(marker["run_token"], token)
        self.assertEqual(marker["wrapper_pid"], 42)

    def test_read_marker_tolerates_missing_and_malformed(self) -> None:
        token = supervisor.new_run_token()
        run_dir = supervisor.create_run_dir(self.control_root, token)
        self.assertIsNone(supervisor.read_owner_marker(run_dir))
        (run_dir / "owner.json").write_text("{not json")
        self.assertIsNone(supervisor.read_owner_marker(run_dir))

    def test_cleanup_run_dir_never_follows_symlink(self) -> None:
        target = self.tmp / "target"
        target.mkdir()
        (target / "keep").write_text("x")
        link = self.control_root
        link.parent.mkdir(parents=True, exist_ok=True)
        token = supervisor.new_run_token()
        link.mkdir()
        sym = link / token
        sym.symlink_to(target)
        supervisor.cleanup_run_dir(sym)
        self.assertTrue(target.exists())
        self.assertTrue((target / "keep").exists())


# --------------------------------------------------------------------------- #
# SQL evidence (no bind)
# --------------------------------------------------------------------------- #


class SqlEvidenceTest(SupervisorTestBase):
    def test_merge_observed_is_additive(self) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                (json.dumps({"worker_log": "/x/y.log"}), self.dispatch_id),
            )
        supervisor.merge_supervisor_observed(
            str(self.db_path),
            self.dispatch_id,
            run_token="a" * 32,
            wrapper_pid=11,
            child_pid=22,
            control_socket="/run/s/x/s",
        )
        observed = _observed(self.store, self.dispatch_id)
        self.assertEqual(observed["worker_log"], "/x/y.log")
        self.assertEqual(observed["protocol_version"], supervisor.PROTOCOL_VERSION)
        self.assertEqual(observed["run_token"], "a" * 32)
        self.assertEqual(observed["wrapper_pid"], 11)
        self.assertEqual(observed["child_pid"], 22)
        self.assertEqual(observed["control_socket"], "/run/s/x/s")

    def test_worker_exit_requires_matching_run_token(self) -> None:
        token = "b" * 32
        supervisor.merge_supervisor_observed(
            str(self.db_path), self.dispatch_id, run_token=token, wrapper_pid=1, child_pid=2, control_socket="x"
        )
        # Wrong token does not stamp the row.
        self.assertFalse(
            supervisor.record_worker_exit(str(self.db_path), self.dispatch_id, "wrong" * 6 + "ff", returncode=0, source="halt")
        )
        self.assertNotIn("worker_exit", _observed(self.store, self.dispatch_id))
        # Right token stamps exactly once.
        self.assertTrue(
            supervisor.record_worker_exit(str(self.db_path), self.dispatch_id, token, returncode=7, source="child")
        )
        worker_exit = _observed(self.store, self.dispatch_id)["worker_exit"]
        self.assertEqual(worker_exit["returncode"], 7)
        self.assertEqual(worker_exit["source"], "child")
        self.assertEqual(worker_exit["run_token"], token)

    def test_reaper_exit_is_additive_and_same_run(self) -> None:
        token = "c" * 32
        supervisor.merge_supervisor_observed(
            str(self.db_path), self.dispatch_id, run_token=token, wrapper_pid=1, child_pid=2, control_socket="x"
        )
        self.assertTrue(
            supervisor.record_reaper_exit(str(self.db_path), self.dispatch_id, token, returncode=143)
        )
        observed = _observed(self.store, self.dispatch_id)
        self.assertEqual(observed["reaper_exit"]["returncode"], 143)
        self.assertEqual(observed["reaper_exit"]["run_token"], token)

    def test_dispatch_terminal_state_reads_status_and_token(self) -> None:
        token = "d" * 32
        supervisor.merge_supervisor_observed(
            str(self.db_path), self.dispatch_id, run_token=token, wrapper_pid=1, child_pid=2, control_socket="x"
        )
        state = supervisor.dispatch_terminal_state(str(self.db_path), self.dispatch_id)
        assert state is not None
        status, run_token = state
        self.assertEqual(status, "queued")
        self.assertEqual(run_token, token)
        self.assertIsNone(supervisor.dispatch_terminal_state(str(self.db_path), "dispatch_20260714_000000_deadbeef"))


# --------------------------------------------------------------------------- #
# Pre-READY orphan guard (no bind): cleanup / signal handler / wrapper teardown
# --------------------------------------------------------------------------- #


class OrphanGuardTest(SupervisorTestBase):
    def _supervisor_with_child(self, kill_after: float = 2.0) -> supervisor._Supervisor:
        run_token = supervisor.new_run_token()
        child = self._spawn_child(SLEEPER)
        payload = supervisor.BootstrapPayload(
            protocol_version=supervisor.PROTOCOL_VERSION,
            dispatch_id=self.dispatch_id,
            run_token=run_token,
            db_path=str(self.db_path),
            control_root=str(self.control_root),
            ttl_seconds=30.0,
            kill_after_seconds=kill_after,
            zdotdir=None,
            expected_close_by=None,
        )
        supervisor.merge_supervisor_observed(
            str(self.db_path), self.dispatch_id, run_token=run_token,
            wrapper_pid=os.getpid(), child_pid=child.pid, control_socket="x",
        )
        sup = supervisor._Supervisor(payload, SLEEPER)
        sup.child = child
        return sup

    def test_cleanup_terminates_live_child_and_records_exit(self) -> None:
        # The READY-send-failure / parent-loss path: cleanup() (run from the
        # run_supervisor finally) must terminate a live child so no
        # supervisor-less native child is orphaned, and record truthful exit.
        sup = self._supervisor_with_child()
        self.assertIsNone(sup.child.poll())
        sup.cleanup()
        self.assertIsNotNone(_wait_until(lambda: sup.child.poll() is not None))
        exit_ev = _observed(self.store, self.dispatch_id)["worker_exit"]
        self.assertEqual(exit_ev["source"], "cleanup")
        self.assertEqual(exit_ev["run_token"], sup.payload.run_token)

    def test_cleanup_is_idempotent(self) -> None:
        sup = self._supervisor_with_child()
        sup.cleanup()
        first = _observed(self.store, self.dispatch_id)["worker_exit"]
        sup.cleanup()  # guarded: no re-terminate, no re-record
        self.assertEqual(_observed(self.store, self.dispatch_id)["worker_exit"], first)

    def test_signal_handler_terminates_child_and_exits(self) -> None:
        # The adapter-timeout / parent-loss path: an external SIGTERM to the
        # wrapper must terminate its owned child (not orphan it) and exit.
        sup = self._supervisor_with_child()
        with self.assertRaises(SystemExit):
            sup._on_terminating_signal(signal.SIGTERM, None)
        self.assertIsNotNone(_wait_until(lambda: sup.child.poll() is not None))
        self.assertEqual(_observed(self.store, self.dispatch_id)["worker_exit"]["source"], "signal")

    def test_terminate_wrapper_sigterms_and_preserves_without_killpg(self) -> None:
        # Adapter side: SIGTERM the wrapper we own (so its handler tears the
        # child down); a pathological unresponsive wrapper is PRESERVED, never
        # killpg'd into a separate orphan.
        popen = mock.Mock()
        popen.poll.return_value = None
        popen.wait.side_effect = subprocess.TimeoutExpired(["wrapper"], 0.01)
        with mock.patch("os.killpg", side_effect=AssertionError("must not killpg the wrapper session")):
            supervisor._terminate_wrapper(popen, grace_seconds=0.01)
        popen.terminate.assert_called_once()
        popen.kill.assert_not_called()

    def test_terminate_wrapper_noop_when_already_exited(self) -> None:
        popen = mock.Mock()
        popen.poll.return_value = 0
        supervisor._terminate_wrapper(popen)
        popen.terminate.assert_not_called()


# --------------------------------------------------------------------------- #
# Control server logic over socketpair (no bind)
# --------------------------------------------------------------------------- #


class ControlServerTest(SupervisorTestBase):
    def _supervisor_with_child(self, run_token: str | None = None) -> supervisor._Supervisor:
        run_token = run_token or supervisor.new_run_token()
        child = self._spawn_child(SLEEPER)
        payload = supervisor.BootstrapPayload(
            protocol_version=supervisor.PROTOCOL_VERSION,
            dispatch_id=self.dispatch_id,
            run_token=run_token,
            db_path=str(self.db_path),
            control_root=str(self.control_root),
            ttl_seconds=30.0,
            kill_after_seconds=2.0,
            zdotdir=None,
            expected_close_by=None,
        )
        supervisor.merge_supervisor_observed(
            str(self.db_path), self.dispatch_id, run_token=run_token, wrapper_pid=os.getpid(), child_pid=child.pid, control_socket="x"
        )
        sup = supervisor._Supervisor(payload, SLEEPER)
        sup.child = child
        return sup

    def _roundtrip(self, sup: supervisor._Supervisor, request: dict) -> dict:
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            supervisor._send_json(client, request)
            sup._handle_request(server)
            line = supervisor._read_line(client, timeout=2.0)
            assert line is not None
            return json.loads(line.decode())
        finally:
            client.close()
            server.close()

    def test_status_reports_running_and_never_signals(self) -> None:
        sup = self._supervisor_with_child()
        resp = self._roundtrip(sup, {"protocol_version": supervisor.PROTOCOL_VERSION, "run_token": sup.payload.run_token, "op": "STATUS"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["state"], "running")
        self.assertEqual(resp["child_pid"], sup.child.pid)
        self.assertIsNone(sup.child.poll())
        self.assertFalse(sup._halted)

    def test_wrong_token_denied_and_never_signals(self) -> None:
        sup = self._supervisor_with_child()
        for op in ("STATUS", "HALT"):
            resp = self._roundtrip(sup, {"protocol_version": supervisor.PROTOCOL_VERSION, "run_token": "f" * 32, "op": op})
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"], "unauthenticated")
        self.assertIsNone(sup.child.poll())
        self.assertFalse(sup._halted)
        self.assertNotIn("worker_exit", _observed(self.store, self.dispatch_id))

    def test_wrong_protocol_denied_and_never_signals(self) -> None:
        sup = self._supervisor_with_child()
        resp = self._roundtrip(sup, {"protocol_version": 999, "run_token": sup.payload.run_token, "op": "HALT"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "unsupported protocol")
        self.assertIsNone(sup.child.poll())

    def test_unknown_op_denied(self) -> None:
        sup = self._supervisor_with_child()
        resp = self._roundtrip(sup, {"protocol_version": supervisor.PROTOCOL_VERSION, "run_token": sup.payload.run_token, "op": "NUKE"})
        self.assertFalse(resp["ok"])
        self.assertIsNone(sup.child.poll())

    def test_authenticated_halt_terminates_child_and_records_exit(self) -> None:
        sup = self._supervisor_with_child()
        resp = self._roundtrip(sup, {"protocol_version": supervisor.PROTOCOL_VERSION, "run_token": sup.payload.run_token, "op": "HALT"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["state"], "halted")
        self.assertTrue(sup._halted)
        self.assertIsNotNone(_wait_until(lambda: sup.child.poll() is not None))
        worker_exit = _observed(self.store, self.dispatch_id)["worker_exit"]
        self.assertEqual(worker_exit["source"], "halt")
        self.assertEqual(worker_exit["run_token"], sup.payload.run_token)

    def _supervisor_with_dirs(self):  # type: ignore[no-untyped-def]
        """A supervisor whose real run directory + ZDOTDIR must be gone before a
        confirmed HALT response (barrier: cleanup precedes confirmation)."""
        sup = self._supervisor_with_child()
        run_dir = supervisor.create_run_dir(self.control_root, sup.payload.run_token)
        sup.run_dir = run_dir
        sup.socket_path = run_dir / "s"
        zparent = self.tmp / "agent-comms-zdotdir-ctl"
        zdotdir = zparent / "z"
        zdotdir.mkdir(parents=True)
        sup.payload = supervisor.BootstrapPayload(
            protocol_version=sup.payload.protocol_version,
            dispatch_id=sup.payload.dispatch_id,
            run_token=sup.payload.run_token,
            db_path=sup.payload.db_path,
            control_root=sup.payload.control_root,
            ttl_seconds=sup.payload.ttl_seconds,
            kill_after_seconds=sup.payload.kill_after_seconds,
            zdotdir=str(zdotdir),
            expected_close_by=None,
        )
        return sup, run_dir, zparent

    def test_halt_persists_exit_and_clears_dirs_before_the_response(self) -> None:
        # Ordering proof: at the instant the confirmed halted response is written
        # the child/group is gone, worker_exit is already persisted, and both the
        # run directory and ZDOTDIR are already removed.
        sup, run_dir, zparent = self._supervisor_with_dirs()
        snapshot: dict = {}
        real_send = supervisor._send_json

        def capturing_send(conn, payload):  # type: ignore[no-untyped-def]
            if payload.get("state") == "halted" and payload.get("ok"):
                snapshot["observed"] = _observed(self.store, self.dispatch_id)
                snapshot["run_dir_exists"] = run_dir.exists()
                snapshot["zparent_exists"] = zparent.exists()
                snapshot["child_dead"] = sup.child.poll() is not None
            return real_send(conn, payload)

        with mock.patch.object(supervisor, "_send_json", capturing_send):
            resp = self._roundtrip(
                sup,
                {"protocol_version": supervisor.PROTOCOL_VERSION, "run_token": sup.payload.run_token, "op": "HALT"},
            )
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["state"], "halted")
        self.assertIn("worker_exit", snapshot["observed"])
        self.assertEqual(snapshot["observed"]["worker_exit"]["source"], "halt")
        self.assertTrue(snapshot["child_dead"])
        self.assertFalse(snapshot["run_dir_exists"], "run dir must be gone before the confirmed response")
        self.assertFalse(snapshot["zparent_exists"], "ZDOTDIR must be gone before the confirmed response")

    def test_halt_refused_on_group_residue_does_not_confirm(self) -> None:
        sup = self._supervisor_with_child()
        with mock.patch.object(supervisor, "process_group_empty", return_value=False):
            resp = self._roundtrip(
                sup,
                {"protocol_version": supervisor.PROTOCOL_VERSION, "run_token": sup.payload.run_token, "op": "HALT"},
            )
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["state"], "halt_not_confirmed")
        self.assertIn("group", resp["error"])
        self.assertFalse(sup._halted)
        self.assertIsNone(sup._halt_conn)
        # Exit persistence never ran: the group-residue barrier failed first.
        self.assertNotIn("worker_exit", _observed(self.store, self.dispatch_id))

    def test_halt_refused_on_exit_persistence_failure(self) -> None:
        sup = self._supervisor_with_child()
        with mock.patch.object(supervisor, "record_worker_exit", return_value=False):
            resp = self._roundtrip(
                sup,
                {"protocol_version": supervisor.PROTOCOL_VERSION, "run_token": sup.payload.run_token, "op": "HALT"},
            )
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["state"], "halt_not_confirmed")
        self.assertIn("persistence", resp["error"])
        self.assertFalse(sup._halted)
        self.assertIsNone(sup._halt_conn)
        self.assertNotIn("worker_exit", _observed(self.store, self.dispatch_id))

    def test_no_sql_transaction_spans_an_external_wait(self) -> None:
        # Requirement 9: the exit-persist transaction must be a distinct step; no
        # DB connection is held open across termination, the group-drain, or
        # cleanup. Prove it by ordering: record_worker_exit's enter/exit bracket
        # is adjacent with no wait event interleaved.
        sup, _run_dir, _zparent = self._supervisor_with_dirs()
        events: list[str] = []
        real_terminate = supervisor.terminate_process_tree
        real_group = supervisor.process_group_empty
        real_record = supervisor.record_worker_exit
        real_cleanup = supervisor.cleanup_run_dir

        def ev_terminate(*a, **k):  # type: ignore[no-untyped-def]
            events.append("wait:terminate")
            return real_terminate(*a, **k)

        def ev_group(*a, **k):  # type: ignore[no-untyped-def]
            events.append("wait:group")
            return real_group(*a, **k)

        def ev_record(*a, **k):  # type: ignore[no-untyped-def]
            events.append("sql:enter")
            out = real_record(*a, **k)
            events.append("sql:exit")
            return out

        def ev_cleanup(*a, **k):  # type: ignore[no-untyped-def]
            events.append("wait:cleanup")
            return real_cleanup(*a, **k)

        with mock.patch.object(supervisor, "terminate_process_tree", ev_terminate), \
            mock.patch.object(supervisor, "process_group_empty", ev_group), \
            mock.patch.object(supervisor, "record_worker_exit", ev_record), \
            mock.patch.object(supervisor, "cleanup_run_dir", ev_cleanup):
            confirmed, _rc, _err = sup._perform_halt()
        self.assertTrue(confirmed)
        self.assertEqual(events, ["wait:terminate", "wait:group", "sql:enter", "sql:exit", "wait:cleanup"])
        # The SQL bracket is adjacent -> nothing (no wait) executed mid-transaction.
        self.assertEqual(events.index("sql:exit"), events.index("sql:enter") + 1)


class TerminateAndExitTest(SupervisorTestBase):
    def test_terminate_process_tree_kills_own_group(self) -> None:
        child = self._spawn_child(SLEEPER)
        returncode = supervisor.terminate_process_tree(child, grace_seconds=2.0)
        self.assertIsNotNone(returncode)
        self.assertIsNotNone(child.poll())

    def test_record_exit_is_write_once(self) -> None:
        token = supervisor.new_run_token()
        supervisor.merge_supervisor_observed(
            str(self.db_path), self.dispatch_id, run_token=token, wrapper_pid=1, child_pid=2, control_socket="x"
        )
        payload = supervisor.BootstrapPayload(
            protocol_version=1, dispatch_id=self.dispatch_id, run_token=token, db_path=str(self.db_path),
            control_root=str(self.control_root), ttl_seconds=1, kill_after_seconds=1, zdotdir=None, expected_close_by=None,
        )
        sup = supervisor._Supervisor(payload, SLEEPER)
        sup._record_exit(3, source="child")
        sup._record_exit(9, source="timeout")  # ignored: already recorded
        self.assertEqual(_observed(self.store, self.dispatch_id)["worker_exit"]["returncode"], 3)


# --------------------------------------------------------------------------- #
# Control client error paths (no bind)
# --------------------------------------------------------------------------- #


class ControlClientTest(SupervisorTestBase):
    def test_probe_status_on_missing_socket_is_unreachable_not_dead(self) -> None:
        missing = str(self.control_root / ("0" * 32) / "s")
        result = supervisor.probe_status(missing, "0" * 32, connect_timeout=1.0)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        self.assertIsNone(result.state)

    def test_request_halt_on_missing_socket_reports_error(self) -> None:
        missing = str(self.control_root / ("1" * 32) / "s")
        result = supervisor.request_halt(missing, "1" * 32, connect_timeout=1.0, io_timeout=1.0)
        self.assertFalse(result.ok)


# --------------------------------------------------------------------------- #
# Authenticated HALT wrapper-exit barrier: client side over socketpair (no bind)
# --------------------------------------------------------------------------- #


class HaltClientBarrierTest(SupervisorTestBase):
    """``_halt_over_connection`` confirms ONLY after all three barriers: a valid
    authenticated halted response, EOF on that same connection, and the exact
    registered wrapper ``Popen`` exiting/reaping. Each barrier is exercised over
    a connected ``socketpair`` (no ``bind`` required) with a scripted server."""

    RUN_TOKEN = "a" * 32

    def _exited_wrapper(self) -> subprocess.Popen:
        popen = subprocess.Popen(QUICK_EXIT)
        self._children.append(popen)
        _wait_until(lambda: popen.poll() is not None)
        return popen

    def _live_wrapper(self) -> subprocess.Popen:
        return self._spawn_child(SLEEPER)

    def _drive(self, server_behavior, wrapper_popen, *, io_timeout=0.6, wrapper_wait_timeout=0.6):
        client_sock, server_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        box: dict = {}

        def run_client() -> None:
            try:
                box["result"] = supervisor._halt_over_connection(
                    client_sock,
                    self.RUN_TOKEN,
                    wrapper_popen,
                    io_timeout=io_timeout,
                    wrapper_wait_timeout=wrapper_wait_timeout,
                )
            except BaseException as exc:  # pragma: no cover - surfaced to the test
                box["error"] = exc
            finally:
                client_sock.close()

        thread = threading.Thread(target=run_client)
        thread.start()
        try:
            request = supervisor._read_line(server_sock, timeout=2.0)
            self.assertIsNotNone(request)
            self.assertEqual(json.loads(request.decode())["op"], "HALT")
            server_behavior(server_sock)
        finally:
            thread.join(timeout=6.0)
            try:
                server_sock.close()
            except OSError:
                pass
        self.assertFalse(thread.is_alive(), "halt client thread did not finish")
        if "error" in box:
            raise box["error"]
        return box["result"]

    @staticmethod
    def _send_halted(server_sock) -> None:  # type: ignore[no-untyped-def]
        supervisor._send_json(server_sock, {"ok": True, "state": "halted", "returncode": 0})

    def test_all_three_barriers_confirm(self) -> None:
        def behavior(server_sock):  # type: ignore[no-untyped-def]
            self._send_halted(server_sock)
            server_sock.close()  # EOF (barrier 2)

        result = self._drive(behavior, self._exited_wrapper())
        self.assertTrue(result.ok)
        self.assertEqual(result.state, "halted")

    def test_response_without_eof_is_not_confirmed(self) -> None:
        def behavior(server_sock):  # type: ignore[no-untyped-def]
            self._send_halted(server_sock)  # valid response but NEVER closes -> no EOF

        result = self._drive(behavior, self._exited_wrapper())
        self.assertFalse(result.ok)
        self.assertIn("without wrapper EOF", result.error)

    def test_eof_without_valid_response_is_not_confirmed(self) -> None:
        def behavior(server_sock):  # type: ignore[no-untyped-def]
            server_sock.close()  # EOF with no response line at all

        result = self._drive(behavior, self._exited_wrapper())
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no response")

    def test_failed_response_then_eof_is_not_confirmed(self) -> None:
        def behavior(server_sock):  # type: ignore[no-untyped-def]
            supervisor._send_json(server_sock, {"ok": False, "state": "halt_not_confirmed", "error": "boom"})
            server_sock.close()

        result = self._drive(behavior, self._exited_wrapper())
        self.assertFalse(result.ok)

    def test_missing_wrapper_exit_is_not_confirmed(self) -> None:
        live = self._live_wrapper()

        def behavior(server_sock):  # type: ignore[no-untyped-def]
            self._send_halted(server_sock)
            server_sock.close()  # valid response + EOF, but the wrapper stays alive

        result = self._drive(behavior, live, wrapper_wait_timeout=0.4)
        self.assertFalse(result.ok)
        self.assertIn("wrapper did not exit", result.error)

    def test_no_registered_wrapper_is_not_confirmed(self) -> None:
        def behavior(server_sock):  # type: ignore[no-untyped-def]
            self._send_halted(server_sock)
            server_sock.close()

        result = self._drive(behavior, None)
        self.assertFalse(result.ok)
        self.assertIn("no registered wrapper", result.error)


# --------------------------------------------------------------------------- #
# Registered-wrapper reap persistence: normal HALT vs abnormal wrapper death
# (no bind)
# --------------------------------------------------------------------------- #


class ReaperFallbackOnlyTest(SupervisorTestBase):
    def _identify(self, token: str) -> None:
        supervisor.merge_supervisor_observed(
            str(self.db_path), self.dispatch_id, run_token=token, wrapper_pid=1, child_pid=2, control_socket="x"
        )

    def test_worker_exit_no_longer_blocks_registered_reap_evidence(self) -> None:
        # Revision 3 (RPE): $.worker_exit is child exit evidence the still-alive
        # wrapper persists during a normal HALT; the exact registered-wrapper
        # reap is a DISTINCT fact the parent records alongside it. The old
        # fallback-only guard (refuse whenever worker_exit exists) is replaced:
        # the identity-bound primitive writes $.reaper_exit next to a same-run
        # $.worker_exit, once, preserving both.
        token = supervisor.new_run_token()
        self._identify(token)
        self.assertTrue(
            supervisor.record_worker_exit(str(self.db_path), self.dispatch_id, token, returncode=0, source="halt")
        )
        self.assertTrue(
            supervisor.record_reaper_exit(str(self.db_path), self.dispatch_id, token, returncode=143)
        )
        observed = _observed(self.store, self.dispatch_id)
        self.assertEqual(observed["worker_exit"]["run_token"], token)
        self.assertEqual(observed["reaper_exit"]["run_token"], token)
        self.assertEqual(observed["reaper_exit"]["returncode"], 143)
        self.assertEqual(observed["reaper_exit"]["source"], supervisor.REAP_SOURCE_BACKGROUND_REAP)

    def test_abnormal_death_permits_exactly_one_fallback_reaper_exit(self) -> None:
        token = supervisor.new_run_token()
        self._identify(token)
        # No worker_exit (abnormal wrapper death) -> exactly one reaper_exit.
        self.assertTrue(
            supervisor.record_reaper_exit(str(self.db_path), self.dispatch_id, token, returncode=143)
        )
        first = _observed(self.store, self.dispatch_id)["reaper_exit"]
        # Write-once: a second reap must not stamp again.
        self.assertFalse(
            supervisor.record_reaper_exit(str(self.db_path), self.dispatch_id, token, returncode=9)
        )
        self.assertEqual(_observed(self.store, self.dispatch_id)["reaper_exit"], first)

    def test_find_by_run_token_returns_exact_registered_wrapper(self) -> None:
        registry = supervisor.ReaperRegistry()
        token = supervisor.new_run_token()
        popen = subprocess.Popen(QUICK_EXIT)
        self._children.append(popen)
        registry.register(
            "h", popen, db_path=str(self.db_path), dispatch_id=self.dispatch_id, run_token=token
        )
        entry = registry.find_by_run_token(token)
        self.assertIsNotNone(entry)
        self.assertIs(entry.popen, popen)
        self.assertIsNone(registry.find_by_run_token("f" * 32))


# --------------------------------------------------------------------------- #
# Bootstrap payload + adapter-side argv (no bind)
# --------------------------------------------------------------------------- #


class BootstrapPayloadTest(unittest.TestCase):
    def test_valid_payload_parses(self) -> None:
        raw = json.dumps(
            {
                "protocol_version": 1,
                "dispatch_id": "d",
                "run_token": "t",
                "db_path": "/db",
                "control_root": "/root",
                "ttl_seconds": 45,
                "kill_after_seconds": 30,
                "zdotdir": "/z",
                "expected_close_by": "2026-01-01T00:00:00Z",
            }
        ).encode()
        payload = supervisor.BootstrapPayload.from_json(raw)
        self.assertEqual(payload.dispatch_id, "d")
        self.assertEqual(payload.ttl_seconds, 45.0)
        self.assertEqual(payload.zdotdir, "/z")

    def test_wrong_protocol_rejected(self) -> None:
        raw = json.dumps({"protocol_version": 2, "dispatch_id": "d"}).encode()
        with self.assertRaises(supervisor.SupervisorError):
            supervisor.BootstrapPayload.from_json(raw)

    def test_malformed_payload_rejected(self) -> None:
        with self.assertRaises(supervisor.SupervisorError):
            supervisor.BootstrapPayload.from_json(json.dumps({"protocol_version": 1}).encode())


class SpawnArgvTest(SupervisorTestBase):
    def test_run_token_never_on_wrapper_argv(self) -> None:
        with mock.patch("subprocess.Popen") as popen:
            popen.side_effect = RuntimeError("stop before spawn")
            with self.assertRaises(RuntimeError):
                supervisor.spawn_supervised(
                    SLEEPER,
                    dispatch_id=self.dispatch_id,
                    db_path=str(self.db_path),
                    ttl_seconds=30,
                    kill_after_seconds=2,
                    root=self.control_root,
                    run_token="a" * 32,
                )
        argv = popen.call_args.args[0]
        self.assertNotIn("a" * 32, " ".join(str(part) for part in argv))
        self.assertIn("--supervise", argv)
        self.assertIn("--bootstrap-fd", argv)
        # pass_fds carries exactly one wrapper peer, and close_fds is on.
        self.assertEqual(len(popen.call_args.kwargs["pass_fds"]), 1)
        self.assertTrue(popen.call_args.kwargs["close_fds"])


class BootstrapHandshakeFailureTest(SupervisorTestBase):
    """Adapter-side bootstrap send/read/JSON failures normalise to a typed
    pre-READY ``SupervisorError`` after the safe wrapper teardown, preserving
    the cause. A real early wrapper death must surface the specified
    spawn-failure path, never a raw ``OSError``/``EOFError`` from the socketpair.
    The wrapper ``Popen`` is mocked so this needs no ``AF_UNIX`` bind.
    """

    def _fake_wrapper(self) -> mock.Mock:
        popen = mock.Mock()
        popen.poll.return_value = None  # still "live" so teardown SIGTERMs it
        popen.pid = 4321
        return popen

    def _spawn(self):  # type: ignore[no-untyped-def]
        return supervisor.spawn_supervised(
            SLEEPER,
            dispatch_id=self.dispatch_id,
            db_path=str(self.db_path),
            ttl_seconds=30,
            kill_after_seconds=2,
            root=self.control_root,
        )

    def test_send_oserror_becomes_supervisor_error_and_tears_down(self) -> None:
        popen = self._fake_wrapper()
        with mock.patch("subprocess.Popen", return_value=popen), mock.patch.object(
            supervisor, "_send_json", side_effect=BrokenPipeError("broken pipe")
        ):
            with self.assertRaises(supervisor.SupervisorError) as ctx:
                self._spawn()
        # Typed pre-READY failure with the underlying cause preserved.
        self.assertIsInstance(ctx.exception.__cause__, BrokenPipeError)
        self.assertIn("bootstrap send failed", str(ctx.exception))
        # Safe wrapper teardown ran (ownership SIGTERM), never an orphaned child.
        popen.terminate.assert_called_once()

    def test_read_oserror_becomes_supervisor_error(self) -> None:
        popen = self._fake_wrapper()
        with mock.patch("subprocess.Popen", return_value=popen), mock.patch.object(
            supervisor, "_send_json"
        ), mock.patch.object(
            supervisor, "_read_line", side_effect=ConnectionResetError("reset")
        ):
            with self.assertRaises(supervisor.SupervisorError) as ctx:
                self._spawn()
        self.assertIsInstance(ctx.exception.__cause__, ConnectionResetError)
        self.assertIn("bootstrap read failed", str(ctx.exception))
        popen.terminate.assert_called_once()

    def test_malformed_json_ready_becomes_supervisor_error(self) -> None:
        popen = self._fake_wrapper()
        with mock.patch("subprocess.Popen", return_value=popen), mock.patch.object(
            supervisor, "_send_json"
        ), mock.patch.object(supervisor, "_read_line", return_value=b"not-json{"):
            with self.assertRaises(supervisor.SupervisorError) as ctx:
                self._spawn()
        self.assertIn("not valid JSON", str(ctx.exception))
        popen.terminate.assert_called_once()


# --------------------------------------------------------------------------- #
# Reaper registry over plain exited subprocesses (no bind)
# --------------------------------------------------------------------------- #


class ReaperRegistryTest(SupervisorTestBase):
    def test_reaps_multiple_with_same_run_evidence_and_cleanup(self) -> None:
        registry = supervisor.ReaperRegistry()
        second = _seed_dispatch(self.store, self.tmp, key="reaper-2")
        entries = []
        for dispatch_id in (self.dispatch_id, second):
            token = supervisor.new_run_token()
            run_dir = supervisor.create_run_dir(self.control_root, token)
            socket_path = run_dir / "s"
            socket_path.touch()
            child_pgid = self._drained_pgid()
            supervisor.merge_supervisor_observed(
                str(self.db_path), dispatch_id, run_token=token,
                wrapper_pid=1, child_pid=child_pgid, control_socket=str(socket_path),
            )
            popen = subprocess.Popen(QUICK_EXIT)
            self._children.append(popen)
            registry.register(
                f"handle:{dispatch_id}",
                popen,
                db_path=str(self.db_path),
                dispatch_id=dispatch_id,
                run_token=token,
                run_dir=str(run_dir),
                control_socket=str(socket_path),
                child_pgid=child_pgid,
            )
            entries.append((dispatch_id, token, run_dir, popen))

        self.assertEqual(registry.pending(), 2)
        for _dispatch_id, _token, _run_dir, popen in entries:
            _wait_until(lambda p=popen: p.poll() is not None)
        registry.reap_ready()
        _wait_until(lambda: registry.pending() == 0)
        self.assertEqual(registry.pending(), 0)

        for dispatch_id, token, run_dir, _popen in entries:
            observed = _observed(self.store, dispatch_id)
            self.assertIn("reaper_exit", observed)
            self.assertEqual(observed["reaper_exit"]["run_token"], token)
            self.assertFalse(run_dir.exists())

    def test_registry_never_transitions_status(self) -> None:
        registry = supervisor.ReaperRegistry()
        token = supervisor.new_run_token()
        run_dir = supervisor.create_run_dir(self.control_root, token)
        socket_path = run_dir / "s"
        socket_path.touch()
        child_pgid = self._drained_pgid()
        supervisor.merge_supervisor_observed(
            str(self.db_path), self.dispatch_id, run_token=token,
            wrapper_pid=1, child_pid=child_pgid, control_socket=str(socket_path),
        )
        popen = subprocess.Popen(QUICK_EXIT)
        self._children.append(popen)
        _wait_until(lambda: popen.poll() is not None)
        registry.register(
            "h", popen, db_path=str(self.db_path), dispatch_id=self.dispatch_id, run_token=token,
            run_dir=str(run_dir), control_socket=str(socket_path), child_pgid=child_pgid,
        )
        registry.reap_ready()
        with self.store.connection() as conn:
            status = conn.execute(
                "select status from dispatch_ledger where dispatch_id = ?", (self.dispatch_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "queued")

    def test_module_global_registry_is_singleton(self) -> None:
        self.assertIs(supervisor.reaper_registry(), supervisor.reaper_registry())


# --------------------------------------------------------------------------- #
# Conservative janitor (no bind)
# --------------------------------------------------------------------------- #


class JanitorTest(SupervisorTestBase):
    def _owned_dir(self, token: str, dispatch_id: str, *, marker_version=None, marker_token=None) -> Path:
        run_dir = supervisor.create_run_dir(self.control_root, token)
        marker = {
            "marker_version": marker_version if marker_version is not None else supervisor.MARKER_VERSION,
            "dispatch_id": dispatch_id,
            "run_token": marker_token if marker_token is not None else token,
            "wrapper_pid": 1234,
            "created_at": "2026-07-14T00:00:00+00:00",
        }
        (run_dir / "owner.json").write_text(json.dumps(marker, sort_keys=True))
        return run_dir

    def _terminal_dispatch(
        self, key: str, token: str, status: str = "closed", *, termination: str | None = "same_run_exit"
    ) -> str:
        dispatch_id = _seed_dispatch(self.store, self.tmp, key=key)
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set policy_version = 'v1' where dispatch_id = ?",
                (dispatch_id,),
            )
            self.assertEqual(
                conn.execute(
                    "select policy_version from dispatch_ledger where dispatch_id = ?",
                    (dispatch_id,),
                ).fetchone()["policy_version"],
                "v1",
            )
        supervisor.merge_supervisor_observed(
            str(self.db_path), dispatch_id, run_token=token, wrapper_pid=1, child_pid=2, control_socket="x"
        )
        # Positive same-run termination evidence is now required for cleanup, and
        # Revision 7 F2 requires it be the exact version-1 COMPLETE ``reaper_exit``
        # proof (a bare same-run ``worker_exit`` is child-exit evidence only and no
        # longer authorizes cleanup). The default terminal dispatch therefore
        # carries the complete registered-wrapper reap proof a completed +
        # reaped/cleaned run leaves. ``termination`` may instead pin a specific
        # ``termination_result`` (e.g. ``termination_not_confirmed`` residue) or
        # ``None`` for a terminal owner with no confirmed evidence at all.
        if termination == "same_run_exit":
            supervisor.record_reaper_exit(str(self.db_path), dispatch_id, token, returncode=0)
        elif termination is not None:
            with self.store.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set observed_values_json = json_set("
                    "coalesce(nullif(observed_values_json, ''), '{}'), '$.termination_result', ?) "
                    "where dispatch_id = ?",
                    (termination, dispatch_id),
                )
        _set_status(self.store, dispatch_id, status)
        return dispatch_id

    def test_removes_only_terminal_owned_dirs(self) -> None:
        # (a) terminal + matching + confirmed same-run exit evidence -> removed
        terminal_token = supervisor.new_run_token()
        terminal_dispatch = self._terminal_dispatch("jan-terminal", terminal_token, "closed")
        terminal_dir = self._owned_dir(terminal_token, terminal_dispatch)

        # (b) nonterminal -> preserved
        nonterminal_token = supervisor.new_run_token()
        nonterminal_dispatch = _seed_dispatch(self.store, self.tmp, key="jan-nonterminal")
        nonterminal_dir = self._owned_dir(nonterminal_token, nonterminal_dispatch)

        # (c) no DB row -> preserved
        missing_token = supervisor.new_run_token()
        missing_dir = self._owned_dir(missing_token, "dispatch_20260714_000000_deadbeef")

        # (d) malformed marker -> preserved
        malformed_token = supervisor.new_run_token()
        malformed_dir = supervisor.create_run_dir(self.control_root, malformed_token)
        (malformed_dir / "owner.json").write_text("{not json")

        # (e) unknown marker version -> preserved
        version_token = supervisor.new_run_token()
        version_dispatch = self._terminal_dispatch("jan-version", version_token, "closed")
        version_dir = self._owned_dir(version_token, version_dispatch, marker_version=999)

        # (f) marker token != dir name -> preserved
        mismatch_token = supervisor.new_run_token()
        mismatch_dispatch = self._terminal_dispatch("jan-mismatch", mismatch_token, "dlq")
        mismatch_dir = self._owned_dir(mismatch_token, mismatch_dispatch, marker_token="0" * 32)

        # (g) symlink named like a run dir -> never followed/removed
        real_target = self.tmp / "real-target"
        real_target.mkdir()
        symlink_token = supervisor.new_run_token()
        symlink_path = self.control_root / symlink_token
        symlink_path.symlink_to(real_target)

        # (h) non-strict name -> preserved
        stray = self.control_root / "not-a-run-dir"
        stray.mkdir()

        # (i) terminal owner but termination_not_confirmed residue (e.g. a
        # hard-TTL dlq, possibly a live zombie) -> preserved despite terminal
        # status. Terminal status alone is no longer janitor authority.
        residue_token = supervisor.new_run_token()
        residue_dispatch = self._terminal_dispatch(
            "jan-residue", residue_token, "dlq", termination="termination_not_confirmed"
        )
        residue_dir = self._owned_dir(residue_token, residue_dispatch)

        outcomes = supervisor.janitor_sweep(str(self.db_path), self.control_root)
        by_path = {outcome["path"]: outcome["action"] for outcome in outcomes}

        self.assertFalse(terminal_dir.exists())
        self.assertEqual(by_path[str(terminal_dir)], "removed")
        for preserved in (
            nonterminal_dir,
            missing_dir,
            malformed_dir,
            version_dir,
            mismatch_dir,
            stray,
            residue_dir,
        ):
            self.assertTrue(preserved.exists(), f"{preserved} should be preserved")
            self.assertEqual(by_path[str(preserved)], "preserved")
        self.assertTrue(symlink_path.is_symlink())
        self.assertTrue(real_target.exists())
        self.assertEqual(by_path[str(symlink_path)], "preserved")

    def test_missing_root_is_noop(self) -> None:
        self.assertEqual(supervisor.janitor_sweep(str(self.db_path), self.tmp / "nonexistent"), [])

    def test_dispatch_id_not_used_as_path(self) -> None:
        # Marker dispatch_id is looked up in SQL only; it never becomes a path.
        token = supervisor.new_run_token()
        self._owned_dir(token, "../../etc/passwd")
        outcomes = supervisor.janitor_sweep(str(self.db_path), self.control_root)
        self.assertEqual(outcomes[0]["action"], "preserved")


# --------------------------------------------------------------------------- #
# End-to-end bind path (skips loudly when the sandbox denies AF_UNIX bind)
# --------------------------------------------------------------------------- #


@unittest.skipUnless(_BIND_OK, _BIND_SKIP)
class EndToEndSupervisorTest(SupervisorTestBase):
    def _spawn(self, child_command, *, ttl_seconds=30, kill_after_seconds=2, **kwargs):
        spawned = supervisor.spawn_supervised(
            child_command,
            dispatch_id=self.dispatch_id,
            db_path=str(self.db_path),
            ttl_seconds=ttl_seconds,
            kill_after_seconds=kill_after_seconds,
            root=self.control_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
        self.addCleanup(lambda: supervisor._terminate_wrapper(spawned.popen))
        return spawned

    def test_ready_merges_observed_and_status_and_halt(self) -> None:
        # Real bind/READY, then the full authenticated HALT wrapper-exit barrier:
        # response -> EOF -> exact registered wrapper reaped, with child/group gone,
        # exit persisted, and run dir + ZDOTDIR removed all BEFORE confirmation.
        zparent = self.tmp / "agent-comms-zdotdir-e2e"
        zdotdir = zparent / "z"
        zdotdir.mkdir(parents=True)
        spawned = self._spawn(SLEEPER, zdotdir=str(zdotdir))
        observed = _observed(self.store, self.dispatch_id)
        self.assertEqual(observed["run_token"], spawned.run_token)
        self.assertEqual(observed["child_pid"], spawned.child_pid)
        self.assertEqual(observed["control_socket"], spawned.control_socket)

        status = supervisor.probe_status(spawned.control_socket, spawned.run_token)
        self.assertTrue(status.ok)
        self.assertEqual(status.state, "running")

        # Wrong token never signals.
        self.assertFalse(supervisor.probe_status(spawned.control_socket, "f" * 32).ok)
        self.assertIsNone(spawned.popen.poll())

        # Register the wrapper exactly as the spawning adapter does -- including
        # the exact control socket and wrapper/native child group identities the
        # finalize barriers verify -- so the HALT client resolves the exact
        # wrapper Popen for barrier 3 by run token.
        registry = supervisor.ReaperRegistry()
        self.addCleanup(registry.stop)
        registry.register(
            "e2e",
            spawned.popen,
            db_path=str(self.db_path),
            dispatch_id=self.dispatch_id,
            run_token=spawned.run_token,
            run_dir=spawned.run_dir,
            zdotdir=str(zdotdir),
            control_socket=spawned.control_socket,
            wrapper_pgid=spawned.wrapper_pid,
            child_pgid=spawned.child_pid,
        )

        halt = supervisor.request_halt(
            spawned.control_socket, spawned.run_token, io_timeout=8.0, registry=registry
        )
        self.assertTrue(halt.ok)
        self.assertEqual(halt.state, "halted")
        # Confirmation already implies every barrier passed, so these hold WITHOUT
        # polling: the wrapper is reaped, the child/group is gone, exit is
        # persisted, and both artifacts are removed.
        self.assertIsNotNone(spawned.popen.poll())
        self.assertFalse(_pid_alive(spawned.child_pid))
        worker_exit = _observed(self.store, self.dispatch_id)["worker_exit"]
        self.assertEqual(worker_exit["source"], "halt")
        self.assertEqual(worker_exit["run_token"], spawned.run_token)
        self.assertFalse(Path(spawned.run_dir).exists())
        self.assertFalse(zparent.exists())
        # Revision 3 (RPE-T1 strengthening of the old requirement-7 assertion):
        # a confirmed normal HALT now durably RECORDS the exact registered-
        # wrapper reap it performed. The finalize persisted same-run
        # $.reaper_exit (source="halt_finalize") before ok=True, and the
        # background reaper afterwards has nothing left to process: replay is
        # idempotent, the evidence stays byte-identical.
        reaper_exit = _observed(self.store, self.dispatch_id)["reaper_exit"]
        self.assertEqual(reaper_exit["run_token"], spawned.run_token)
        self.assertEqual(reaper_exit["source"], supervisor.REAP_SOURCE_HALT_FINALIZE)
        self.assertEqual(reaper_exit["returncode"], spawned.popen.returncode)
        self.assertEqual(registry.reap_ready(), [])
        self.assertEqual(_observed(self.store, self.dispatch_id)["reaper_exit"], reaper_exit)

    def test_adapter_teardown_terminates_child_no_orphan(self) -> None:
        # End-to-end: a real supervised child is up (READY observed); when the
        # adapter tears the wrapper down via its real _terminate_wrapper (the
        # adapter-timeout / abandonment path), the wrapper's signal handler
        # terminates its owned child. No supervisor-less child is left alive and
        # the run directory is cleaned.
        spawned = self._spawn(SLEEPER, ttl_seconds=30, kill_after_seconds=2)
        child_pid = spawned.child_pid
        self.assertTrue(_pid_alive(child_pid))
        supervisor._terminate_wrapper(spawned.popen, grace_seconds=8.0)
        self.assertTrue(
            _wait_until(lambda: not _pid_alive(child_pid), timeout=12.0),
            "supervisor-less child left alive after wrapper teardown",
        )
        self.assertIsNotNone(_wait_until(lambda: spawned.popen.poll() is not None, timeout=12.0))
        self.assertFalse(Path(spawned.run_dir).exists())

    def test_overlength_socket_fails_before_ready(self) -> None:
        deep_root = self.tmp / ("d" * 90) / ("e" * 90) / "s"
        with self.assertRaises(supervisor.SupervisorError):
            supervisor.spawn_supervised(
                SLEEPER,
                dispatch_id=self.dispatch_id,
                db_path=str(self.db_path),
                ttl_seconds=30,
                kill_after_seconds=2,
                root=deep_root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self.assertNotIn("run_token", _observed(self.store, self.dispatch_id))

    def test_ttl_expiry_terminates_child(self) -> None:
        spawned = self._spawn(SLEEPER, ttl_seconds=1, kill_after_seconds=1)
        self.assertEqual(_wait_until(lambda: spawned.popen.poll(), timeout=12.0), 124)
        worker_exit = _wait_until(lambda: _observed(self.store, self.dispatch_id).get("worker_exit"))
        assert worker_exit is not None
        self.assertEqual(worker_exit["source"], "timeout")

    def test_child_does_not_inherit_control_fds(self) -> None:
        adapter_peer, wrapper_peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        bootstrap_fd = wrapper_peer.fileno()
        out = self.tmp / "fds.json"
        probe = (
            "import os, sys, json, stat, errno\n"
            "probe = int(sys.argv[2])\n"
            "try:\n"
            "    os.fstat(probe); probe_ebadf = False\n"
            "except OSError as exc:\n"
            "    probe_ebadf = (exc.errno == errno.EBADF)\n"
            "open_socks = []\n"
            "for fd in range(3, 256):\n"
            "    try:\n"
            "        st = os.fstat(fd)\n"
            "    except OSError:\n"
            "        continue\n"
            "    if stat.S_ISSOCK(st.st_mode):\n"
            "        open_socks.append(fd)\n"
            "open(sys.argv[1], 'w').write(json.dumps({'probe_ebadf': probe_ebadf, 'open_socks': open_socks}))\n"
            "import time; time.sleep(float(sys.argv[3]))\n"
        )
        child_command = [sys.executable, "-c", probe, str(out), str(bootstrap_fd), "30"]
        argv = [sys.executable, str(WRAPPER), "--supervise", "--bootstrap-fd", str(bootstrap_fd), "--", *child_command]
        popen = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, pass_fds=(bootstrap_fd,), close_fds=True,
        )
        self.addCleanup(lambda: supervisor._terminate_wrapper(popen))
        try:
            wrapper_peer.close()
            supervisor._send_json(
                adapter_peer,
                {
                    "protocol_version": supervisor.PROTOCOL_VERSION, "dispatch_id": self.dispatch_id,
                    "run_token": supervisor.new_run_token(), "db_path": str(self.db_path),
                    "control_root": str(self.control_root), "ttl_seconds": 30.0, "kill_after_seconds": 2.0,
                    "zdotdir": None, "expected_close_by": None,
                },
            )
            self.assertIsNotNone(supervisor._read_line(adapter_peer, timeout=8.0))
            self.assertIsNotNone(_wait_until(out.exists))
            data = json.loads(out.read_text())
            self.assertTrue(data["probe_ebadf"])
            self.assertEqual(data["open_socks"], [])
        finally:
            adapter_peer.close()


# --------------------------------------------------------------------------- #
# F1: mandatory process-group proof under the leader-exit race (no bind)
# --------------------------------------------------------------------------- #


class ProcessGroupRaceTest(SupervisorTestBase):
    """A start_new_session child's process-group id equals its PID by
    construction and stays valid even if the leader exits/gets reaped before
    ``os.getpgid`` can observe it. Termination must still signal that group, and
    HALT must never SKIP the group-level kernel predicate on the getpgid race --
    a surviving descendant must refuse the HALT before any worker_exit."""

    def _supervisor(self, run_token: str | None = None) -> supervisor._Supervisor:
        run_token = run_token or supervisor.new_run_token()
        child = self._spawn_child(SLEEPER)
        payload = supervisor.BootstrapPayload(
            protocol_version=supervisor.PROTOCOL_VERSION,
            dispatch_id=self.dispatch_id,
            run_token=run_token,
            db_path=str(self.db_path),
            control_root=str(self.control_root),
            ttl_seconds=30.0,
            kill_after_seconds=2.0,
            zdotdir=None,
            expected_close_by=None,
        )
        supervisor.merge_supervisor_observed(
            str(self.db_path), self.dispatch_id, run_token=run_token,
            wrapper_pid=os.getpid(), child_pid=child.pid, control_socket="x",
        )
        sup = supervisor._Supervisor(payload, SLEEPER)
        sup.child = child
        return sup

    def test_terminate_process_tree_signals_group_when_getpgid_races(self) -> None:
        # The leader-exit race: os.getpgid raises, but the group (with a surviving
        # descendant) is still addressable by the child PID. Termination must fall
        # back to the PID and STILL signal the group, never abandon it by merely
        # waiting the already-gone leader.
        child = self._spawn_child(SLEEPER)
        kills: list[tuple[int, int]] = []
        real_killpg = os.killpg

        def rec_killpg(pgid, sig):  # type: ignore[no-untyped-def]
            kills.append((pgid, sig))
            return real_killpg(pgid, sig)

        with mock.patch.object(supervisor.os, "getpgid", side_effect=ProcessLookupError), \
            mock.patch.object(supervisor.os, "killpg", side_effect=rec_killpg):
            returncode = supervisor.terminate_process_tree(child, grace_seconds=3.0)
        self.assertTrue(
            any(pgid == child.pid for pgid, _sig in kills),
            f"expected killpg on the child pid {child.pid} group; got {kills}",
        )
        self.assertIsNotNone(returncode)
        self.assertIsNotNone(child.poll())

    def test_halt_refuses_when_getpgid_races_and_group_residue_survives(self) -> None:
        # getpgid loses the race (the leader is already reaped) yet a descendant
        # keeps the group non-empty. The mandatory group predicate must run on the
        # by-construction pgid (child.pid) and refuse BEFORE worker_exit.
        sup = self._supervisor()
        seen: dict = {}

        def fake_group_empty(pgid, **kwargs):  # type: ignore[no-untyped-def]
            seen["pgid"] = pgid
            return False

        with mock.patch.object(supervisor, "terminate_process_tree", return_value=0), \
            mock.patch.object(supervisor.os, "getpgid", side_effect=ProcessLookupError), \
            mock.patch.object(supervisor, "process_group_empty", fake_group_empty):
            confirmed, _returncode, error = sup._perform_halt()
        self.assertFalse(confirmed)
        self.assertIn("group", error)
        # The predicate was NOT skipped: it ran on the child's own pgid.
        self.assertEqual(seen.get("pgid"), sup.child.pid)
        # Refused before any exit persistence.
        self.assertFalse(sup._exit_recorded)
        self.assertNotIn("worker_exit", _observed(self.store, self.dispatch_id))

    def _reap_group_fixture(self, leader: subprocess.Popen, desc_pid: int) -> None:
        # Best-effort teardown of the real process tree, run even on assertion
        # failure: force-kill the descendant PID and the whole leader group, then
        # reap the leader so no fixture process leaks past the test.
        for fn in (
            lambda: os.kill(desc_pid, signal.SIGKILL),
            lambda: os.killpg(leader.pid, signal.SIGKILL),
        ):
            try:
                fn()
            except OSError:
                pass
        try:
            leader.wait(timeout=5)
        except Exception:
            pass

    def _spawn_group_with_stubborn_descendant(
        self, mode: str
    ) -> tuple[subprocess.Popen, int]:
        sync = Path(tempfile.mkdtemp(dir=self.tmp))
        leader = subprocess.Popen(
            [sys.executable, "-c", _GROUP_LEADER_SRC, str(sync), mode, _STUBBORN_DESC_SRC],
            start_new_session=True,
        )
        self._children.append(leader)
        ready = _wait_until(
            lambda: (sync / "desc.ready").exists()
            and (sync / "desc.pid").exists()
            and (sync / "leader.ready").exists()
        )
        self.assertTrue(ready, "group fixture (leader + stubborn descendant) never became ready")
        desc_pid = int((sync / "desc.pid").read_text())
        self.addCleanup(self._reap_group_fixture, leader, desc_pid)
        return leader, desc_pid

    def test_terminate_process_tree_escalates_group_sigkill_when_descendant_ignores_sigterm(
        self,
    ) -> None:
        # F1a: the leader exits promptly on SIGTERM but a descendant in the SAME
        # group IGNORES SIGTERM. terminate_process_tree must escalate the WHOLE
        # group to SIGKILL within the bounded grace and prove the group drained --
        # never return as soon as the leader exited and abandon the orphan.
        leader, desc_pid = self._spawn_group_with_stubborn_descendant("sleep")
        self.assertTrue(_pid_alive(desc_pid))
        returncode = supervisor.terminate_process_tree(leader, grace_seconds=0.5)
        self.assertIsNotNone(leader.poll())
        self.assertIsNotNone(returncode)
        self.assertTrue(
            supervisor.process_group_empty(leader.pid, timeout=3.0),
            "group residue survived: SIGTERM-ignoring descendant was orphaned, not SIGKILL'd",
        )
        self.assertFalse(_pid_alive(desc_pid), "descendant survived group termination")

    def test_terminate_child_group_kills_residue_when_leader_already_exited(self) -> None:
        # F1b: the leader has ALREADY exited (child.poll() is non-None) but a
        # SIGTERM-ignoring descendant keeps the group alive. Teardown must still
        # terminate the process group instead of skipping because poll() returned
        # a code (supervisor.py:1103-1105), or the descendant stays orphaned.
        leader, desc_pid = self._spawn_group_with_stubborn_descendant("exit")
        self.assertIsNotNone(
            _wait_until(lambda: leader.poll() is not None), "leader did not exit on its own"
        )
        self.assertTrue(_pid_alive(desc_pid))
        run_token = supervisor.new_run_token()
        payload = supervisor.BootstrapPayload(
            protocol_version=supervisor.PROTOCOL_VERSION,
            dispatch_id=self.dispatch_id,
            run_token=run_token,
            db_path=str(self.db_path),
            control_root=str(self.control_root),
            ttl_seconds=30.0,
            kill_after_seconds=0.5,
            zdotdir=None,
            expected_close_by=None,
        )
        supervisor.merge_supervisor_observed(
            str(self.db_path), self.dispatch_id, run_token=run_token,
            wrapper_pid=os.getpid(), child_pid=leader.pid, control_socket="x",
        )
        sup = supervisor._Supervisor(payload, SLEEPER)
        sup.child = leader
        sup._terminate_child(source="cleanup")
        self.assertTrue(
            supervisor.process_group_empty(leader.pid, timeout=3.0),
            "teardown skipped group termination: descendant orphaned after the leader exited",
        )
        self.assertFalse(_pid_alive(desc_pid), "descendant survived teardown")

    def test_group_fixture_leader_teardown_is_resourcewarning_clean(self) -> None:
        # T14: the leader source both F1a/F1b race fixtures spawn must reach
        # normal interpreter teardown WITHOUT emitting a ResourceWarning, while
        # the SIGTERM-ignoring in-group descendant is still intentionally alive.
        # Two real leaks are asserted absent: the ``desc.pid`` writer must be
        # closed (no "unclosed file"), and the intentionally live descendant must
        # leave no "subprocess still running" warning. Runs the exact leader
        # source in "exit" mode -- the only mode that reaches normal teardown --
        # with ResourceWarning promoted to an error, captures stderr to a file (a
        # pipe would never EOF while the descendant holds it), then reaps the real
        # descendant/group. The captured stderr is the warning evidence, so this
        # is a genuine correction of the leak, not a buffered-away warning.
        sync = Path(tempfile.mkdtemp(dir=self.tmp))
        stderr_path = sync / "leader.stderr"
        with stderr_path.open("wb") as errf:
            leader = subprocess.Popen(
                [
                    sys.executable,
                    "-W",
                    "error::ResourceWarning",
                    "-c",
                    _GROUP_LEADER_SRC,
                    str(sync),
                    "exit",
                    _STUBBORN_DESC_SRC,
                ],
                stderr=errf,
                start_new_session=True,
            )
        self._children.append(leader)
        ready = _wait_until(
            lambda: (sync / "desc.ready").exists()
            and (sync / "desc.pid").exists()
            and (sync / "leader.ready").exists()
        )
        self.assertTrue(ready, "group fixture never became ready under warning-capture")
        desc_pid = int((sync / "desc.pid").read_text())
        self.addCleanup(self._reap_group_fixture, leader, desc_pid)
        self.assertIsNotNone(
            _wait_until(lambda: leader.poll() is not None), "leader did not exit under warning-capture"
        )
        stderr_text = stderr_path.read_text()
        self.assertNotIn("ResourceWarning", stderr_text)
        self.assertNotIn("still running", stderr_text)
        self.assertNotIn("unclosed", stderr_text)
        self.assertEqual(leader.returncode, 0, f"leader exited nonzero under warning-capture: {stderr_text}")
        # The warning-clean teardown did not come from reaping the descendant
        # early: the orphaned in-group residue the F1b path must SIGKILL is still
        # alive here, exactly as the race fixtures require.
        self.assertTrue(_pid_alive(desc_pid), "descendant was not kept alive across leader teardown")


# --------------------------------------------------------------------------- #
# F2: single-owner registry/reaper lifecycle (no bind)
# --------------------------------------------------------------------------- #


class ReaperSingleOwnerTest(SupervisorTestBase):
    """HALT and background reaping must atomically establish ONE owner per exact
    registered wrapper: concurrent reap_ready() must not double-process an entry,
    a stale owner must not evict a same-handle replacement, and a failed HALT must
    release ownership so fallback reaping still runs."""

    def _quiet_registry(self) -> supervisor.ReaperRegistry:
        registry = supervisor.ReaperRegistry()
        # Deterministic single-threaded control: no background reaper loop races
        # the test's own reap_ready()/claim calls.
        registry._ensure_thread_locked = lambda: None  # type: ignore[assignment]
        return registry

    def _register_exited(
        self, registry: supervisor.ReaperRegistry, handle: str, dispatch_id: str,
        *, halt_cleaned: bool = False,
    ):  # type: ignore[no-untyped-def]
        # Revision 7 F2: register the exact native child group (already drained)
        # and control socket so the parent-registry barriers are satisfiable. For
        # a HALT-finalized entry the server-side HALT already cleaned the owned
        # artifacts, so ``halt_cleaned=True`` removes the run directory the parent
        # then only VERIFIES absent; a background-reaped entry leaves them present
        # for the parent to idempotently clean.
        token = supervisor.new_run_token()
        run_dir = supervisor.create_run_dir(self.control_root, token)
        socket_path = run_dir / "s"
        socket_path.touch()
        child_pgid = self._drained_pgid()
        supervisor.merge_supervisor_observed(
            str(self.db_path), dispatch_id, run_token=token,
            wrapper_pid=1, child_pid=child_pgid, control_socket=str(socket_path),
        )
        popen = subprocess.Popen(QUICK_EXIT)
        self._children.append(popen)
        _wait_until(lambda: popen.poll() is not None)
        if halt_cleaned:
            supervisor.cleanup_run_dir(run_dir)
        registry.register(
            handle, popen, db_path=str(self.db_path), dispatch_id=dispatch_id,
            run_token=token, run_dir=str(run_dir),
            control_socket=str(socket_path), child_pgid=child_pgid,
        )
        return token, run_dir, popen

    def test_halt_claim_blocks_background_reap_of_same_wrapper(self) -> None:
        registry = self._quiet_registry()
        token, _run_dir, popen = self._register_exited(registry, "h", self.dispatch_id, halt_cleaned=True)
        # HALT atomically claims sole ownership of the exact registered wrapper.
        claimed = registry.claim_by_run_token(token)
        self.assertIsNotNone(claimed)
        self.assertIs(claimed.popen, popen)
        # A concurrent background reap must NOT process a claimed entry.
        self.assertEqual(registry.reap_ready(), [])
        self.assertNotIn("reaper_exit", _observed(self.store, self.dispatch_id))
        self.assertEqual(registry.pending(), 1)
        # The HALT still holds sole ownership; the background reap never took it.
        self.assertTrue(claimed.claimed)
        # A second claim is refused: single owner.
        self.assertIsNone(registry.claim_by_run_token(token))
        # HALT confirmed -> finalize durably persists the exact registered-
        # wrapper reap (revision 3), then removes the entry; the background
        # reaper still never processed it (single owner held throughout).
        self.assertTrue(registry.finalize_claimed(claimed))
        self.assertEqual(registry.pending(), 0)
        reaper_exit = _observed(self.store, self.dispatch_id)["reaper_exit"]
        self.assertEqual(reaper_exit["run_token"], token)
        self.assertEqual(reaper_exit["source"], supervisor.REAP_SOURCE_HALT_FINALIZE)

    def test_concurrent_reap_ready_processes_each_entry_once(self) -> None:
        registry = self._quiet_registry()
        _token, _run_dir, _popen = self._register_exited(registry, "h", self.dispatch_id)
        calls: list = []
        gate = threading.Event()
        real_reap = registry._reap_entry

        def blocking_reap(entry, returncode):  # type: ignore[no-untyped-def]
            calls.append(entry)
            gate.wait(2.0)  # hold the owner inside processing while the rival runs
            return real_reap(entry, returncode)

        registry._reap_entry = blocking_reap  # type: ignore[assignment]
        results: dict = {}

        def run(name):  # type: ignore[no-untyped-def]
            results[name] = registry.reap_ready()

        first = threading.Thread(target=run, args=("first",))
        first.start()
        self.assertTrue(_wait_until(lambda: len(calls) == 1))  # first is inside processing
        second = threading.Thread(target=run, args=("second",))
        second.start()
        second.join(3.0)
        self.assertFalse(second.is_alive(), "rival blocked on an already-claimed entry")
        self.assertEqual(results.get("second"), [])
        gate.set()
        first.join(3.0)
        self.assertFalse(first.is_alive())
        # Exactly one processing invocation; the handle is reaped exactly once.
        self.assertEqual(len(calls), 1)
        combined = (results.get("first") or []) + (results.get("second") or [])
        self.assertEqual(combined.count("h"), 1)
        self.assertEqual(registry.pending(), 0)

    def test_stale_reap_does_not_remove_same_handle_replacement(self) -> None:
        registry = self._quiet_registry()
        token_a, _run_dir_a, _popen_a = self._register_exited(registry, "h", self.dispatch_id)
        # Replacement B: a fresh LIVE wrapper reusing the SAME handle, registered
        # mid-processing of A (a slot re-use race).
        dispatch_b = _seed_dispatch(self.store, self.tmp, key="same-handle-b")
        token_b = supervisor.new_run_token()
        run_dir_b = supervisor.create_run_dir(self.control_root, token_b)
        supervisor.merge_supervisor_observed(
            str(self.db_path), dispatch_b, run_token=token_b, wrapper_pid=3, child_pid=4, control_socket="x"
        )
        popen_b = self._spawn_child(SLEEPER)
        real_reap = registry._reap_entry

        def replacing_reap(entry, returncode):  # type: ignore[no-untyped-def]
            registry.register(
                "h", popen_b, db_path=str(self.db_path), dispatch_id=dispatch_b,
                run_token=token_b, run_dir=str(run_dir_b),
            )
            return real_reap(entry, returncode)

        registry._reap_entry = replacing_reap  # type: ignore[assignment]
        reaped = registry.reap_ready()
        self.assertIn("h", reaped)  # A was processed
        # The stale owner (processing A) must NOT evict the same-handle replacement.
        survivor = registry.find_by_run_token(token_b)
        self.assertIsNotNone(survivor, "same-handle replacement was wrongly removed")
        self.assertIs(survivor.popen, popen_b)
        self.assertEqual(registry.pending(), 1)

    def test_failed_halt_releases_claim_for_fallback_reaper(self) -> None:
        registry = self._quiet_registry()
        token, _run_dir, _popen = self._register_exited(registry, "h", self.dispatch_id)
        # A HALT whose control socket is unreachable cannot confirm.
        missing_socket = str(supervisor.control_socket_for(self.control_root, token))
        result = supervisor.request_halt(
            missing_socket, token, connect_timeout=0.5, io_timeout=0.5, registry=registry
        )
        self.assertFalse(result.ok)
        # Failed HALT ownership is RELEASED, not lost: the entry stays tracked and
        # unclaimed so the fallback background reaper still reaps it.
        entry = registry.find_by_run_token(token)
        self.assertIsNotNone(entry)
        self.assertFalse(entry.claimed)
        self.assertIn("h", registry.reap_ready())
        self.assertIn("reaper_exit", _observed(self.store, self.dispatch_id))

    def test_failed_halt_after_same_handle_replacement_keeps_original_reapable(self) -> None:
        # F2a: A is HALT-claimed, then a same-handle replacement B (a slot re-use
        # race) displaces it, then the HALT on A fails and releases the claim. The
        # ORIGINAL wrapper A must retain fallback reaping (register must not drop a
        # claimed entry on the floor), while the live replacement B stays tracked
        # and is not removed by A's stale ownership.
        registry = self._quiet_registry()
        token_a, _run_dir_a, popen_a = self._register_exited(registry, "h", self.dispatch_id)
        claimed_a = registry.claim_by_run_token(token_a)
        self.assertIsNotNone(claimed_a)
        self.assertIs(claimed_a.popen, popen_a)
        # Replacement B: a fresh LIVE wrapper reusing the SAME handle for a
        # different dispatch/run, registered while A is still HALT-claimed.
        dispatch_b = _seed_dispatch(self.store, self.tmp, key="f2a-same-handle-b")
        token_b = supervisor.new_run_token()
        run_dir_b = supervisor.create_run_dir(self.control_root, token_b)
        supervisor.merge_supervisor_observed(
            str(self.db_path), dispatch_b, run_token=token_b, wrapper_pid=5, child_pid=6, control_socket="x"
        )
        popen_b = self._spawn_child(SLEEPER)
        registry.register(
            "h", popen_b, db_path=str(self.db_path), dispatch_id=dispatch_b,
            run_token=token_b, run_dir=str(run_dir_b),
        )
        # The HALT on A fails -> its claim is released for the fallback reaper.
        registry.release_claim(claimed_a)
        # Fallback reaping still reaps the ORIGINAL exited wrapper A...
        reaped = registry.reap_ready()
        self.assertIn("h", reaped, "original wrapper A lost its fallback reaping after displacement")
        self.assertIn("reaper_exit", _observed(self.store, self.dispatch_id))
        # ...without evicting the live same-handle replacement B.
        survivor = registry.find_by_run_token(token_b)
        self.assertIsNotNone(survivor, "same-handle replacement B was wrongly removed by A's stale ownership")
        self.assertIs(survivor.popen, popen_b)
        self.assertNotIn("reaper_exit", _observed(self.store, dispatch_b))

    def test_unexpected_halt_exception_releases_claim_for_fallback(self) -> None:
        # F2b: an exception OUTSIDE the caught (OSError, JSONDecodeError, ValueError)
        # set during the HALT round trip (e.g. an unexpected wrapper-wait failure)
        # must not strand the claim owned-but-unreaped; the fallback reaper must
        # still be able to reap the exact wrapper.
        registry = self._quiet_registry()
        token, _run_dir, _popen = self._register_exited(registry, "h", self.dispatch_id)
        fake_client = mock.Mock()  # connect/settimeout/close are no-op mocks
        boom = RuntimeError("unexpected wrapper-wait failure")
        with mock.patch.object(supervisor.socket, "socket", return_value=fake_client), \
            mock.patch.object(supervisor, "_halt_over_connection", side_effect=boom):
            with self.assertRaises(RuntimeError):
                supervisor.request_halt(
                    "unused-socket-path", token, connect_timeout=0.5, io_timeout=0.5, registry=registry
                )
        entry = registry.find_by_run_token(token)
        self.assertIsNotNone(entry)
        self.assertFalse(entry.claimed, "claim stranded owned-but-unreaped after an unexpected HALT exception")
        self.assertIn("h", registry.reap_ready())
        self.assertIn("reaper_exit", _observed(self.store, self.dispatch_id))


# --------------------------------------------------------------------------- #
# Revision 3 (RPE): registered-wrapper reap evidence -- a normal authenticated
# HALT durably persists the exact parent-registry reap ($.reaper_exit) BEFORE
# request_halt confirms, through one idempotent identity-bound persistence
# primitive shared with background reaping (no bind)
# --------------------------------------------------------------------------- #


class RegisteredWrapperReapEvidenceTest(SupervisorTestBase):
    """RPE-T1..T6: the parent-side reap of the exact registered wrapper is an
    act the parent already performs (response -> EOF -> exact ``Popen`` wait);
    these pin that it is durably RECORDED: normal-HALT finalize persists exact
    same-run ``$.reaper_exit`` before success, persistence failure stays
    unconfirmed with the exact entry retryable, persistence precedes registry
    removal, replay is idempotent and conflicts refuse, handle/run identity
    never crosses, and abnormal background reaping stays truthful."""

    def _quiet_registry(self) -> supervisor.ReaperRegistry:
        registry = supervisor.ReaperRegistry()
        registry._ensure_thread_locked = lambda: None  # type: ignore[assignment]
        return registry

    def _register_exited(
        self, registry: supervisor.ReaperRegistry, handle: str, dispatch_id: str,
        *, halt_cleaned: bool = False,
    ):  # type: ignore[no-untyped-def]
        # Revision 7 F2: register the exact native child group (already drained)
        # and control socket so the parent-registry barriers are satisfiable. For
        # a HALT-finalized entry the server-side HALT already cleaned the owned
        # artifacts, so ``halt_cleaned=True`` removes the run directory the parent
        # then only VERIFIES absent; a background-reaped entry leaves them present
        # for the parent to idempotently clean.
        token = supervisor.new_run_token()
        run_dir = supervisor.create_run_dir(self.control_root, token)
        socket_path = run_dir / "s"
        socket_path.touch()
        child_pgid = self._drained_pgid()
        supervisor.merge_supervisor_observed(
            str(self.db_path), dispatch_id, run_token=token,
            wrapper_pid=1, child_pid=child_pgid, control_socket=str(socket_path),
        )
        popen = subprocess.Popen(QUICK_EXIT)
        self._children.append(popen)
        _wait_until(lambda: popen.poll() is not None)
        if halt_cleaned:
            supervisor.cleanup_run_dir(run_dir)
        registry.register(
            handle, popen, db_path=str(self.db_path), dispatch_id=dispatch_id,
            run_token=token, run_dir=str(run_dir),
            control_socket=str(socket_path), child_pgid=child_pgid,
        )
        return token, run_dir, popen

    def _raw_observed_json(self, dispatch_id: str | None = None) -> str:
        with self.store.connection() as conn:
            return conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (dispatch_id or self.dispatch_id,),
            ).fetchone()["observed_values_json"]

    class _Preconnected:
        """A real connected socket whose ``connect`` is a no-op, so the FULL
        production ``request_halt`` client (response -> EOF -> exact wait ->
        finalize) runs over a ``socketpair`` without needing ``bind``."""

        def __init__(self, sock: socket.socket) -> None:
            self._sock = sock

        def connect(self, addr) -> None:  # type: ignore[no-untyped-def]
            pass

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(self._sock, name)

    def _run_halt(self, registry: supervisor.ReaperRegistry, token: str) -> supervisor.ControlResult:
        """Drive the REAL ``request_halt`` end to end: a scripted wrapper reads
        the authenticated HALT, sends the confirmed ``halted`` response, and
        closes (EOF); the exact registered wrapper ``Popen`` has already exited
        so barrier 3's wait succeeds; finalize then owns persistence."""
        client_end, server_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        box: dict = {}

        def serve() -> None:
            try:
                request = supervisor._read_line(server_end, timeout=4.0)
                box["op"] = json.loads(request.decode())["op"] if request else None
                supervisor._send_json(server_end, {"ok": True, "state": "halted", "returncode": 0})
            finally:
                server_end.close()

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            with mock.patch.object(
                supervisor.socket, "socket", return_value=self._Preconnected(client_end)
            ):
                result = supervisor.request_halt(
                    "unused-socket-path", token, connect_timeout=1.0, io_timeout=4.0, registry=registry
                )
        finally:
            thread.join(timeout=6.0)
            for sock in (client_end, server_end):
                try:
                    sock.close()
                except OSError:
                    pass
        self.assertFalse(thread.is_alive(), "scripted wrapper thread did not finish")
        self.assertEqual(box.get("op"), "HALT")
        return result

    def test_rpe_t1_normal_halt_persists_exact_registered_reap_evidence(self) -> None:
        # RPE-T1: with same-run $.worker_exit already present (the wrapper's own
        # pre-response child-exit persistence), a confirmed normal HALT must
        # ALSO persist same-run $.reaper_exit for the exact registered wrapper
        # before request_halt.ok becomes true.
        registry = self._quiet_registry()
        token, _run_dir, popen = self._register_exited(registry, "h", self.dispatch_id, halt_cleaned=True)
        self.assertTrue(
            supervisor.record_worker_exit(str(self.db_path), self.dispatch_id, token, returncode=0, source="halt")
        )
        result = self._run_halt(registry, token)
        self.assertTrue(result.ok)
        self.assertEqual(result.state, "halted")
        observed = _observed(self.store, self.dispatch_id)
        # Child evidence is retained untouched; it remains child evidence only.
        self.assertEqual(observed["worker_exit"]["run_token"], token)
        reaper_exit = observed.get("reaper_exit")
        self.assertIsNotNone(
            reaper_exit, "confirmed normal HALT left no durable registered-wrapper reap evidence"
        )
        self.assertEqual(reaper_exit["run_token"], token)
        self.assertEqual(reaper_exit["returncode"], popen.returncode)
        self.assertEqual(reaper_exit["source"], supervisor.REAP_SOURCE_HALT_FINALIZE)
        self.assertIn("reaped_at", reaper_exit)
        # The claimed entry was finalized out of the registry.
        self.assertEqual(registry.pending(), 0)
        self.assertIsNone(registry.find_by_run_token(token))

    def test_rpe_t2_success_cannot_outrun_persistence(self) -> None:
        # RPE-T2: an injected reap-evidence SQL failure AFTER the exact wait
        # keeps request_halt.ok false (a confirmed cancellation can never rest
        # on it), and the exact claimed entry remains retryable rather than
        # silently discarded.
        registry = self._quiet_registry()
        token, _run_dir, popen = self._register_exited(registry, "h", self.dispatch_id, halt_cleaned=True)
        self.assertTrue(
            supervisor.record_worker_exit(str(self.db_path), self.dispatch_id, token, returncode=0, source="halt")
        )
        with mock.patch.object(
            supervisor,
            "record_registered_wrapper_reap",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            result = self._run_halt(registry, token)
        self.assertFalse(result.ok)
        self.assertIn("termination not confirmed", result.error)
        self.assertNotIn("reaper_exit", _observed(self.store, self.dispatch_id))
        # The only exact Popen association survives, unclaimed, for bounded
        # idempotent persistence retry -- never stranded, never discarded.
        entry = registry.find_by_run_token(token)
        self.assertIsNotNone(entry, "claimed entry was silently discarded on persistence failure")
        self.assertIs(entry.popen, popen)
        self.assertFalse(entry.claimed)
        # A healthy retry drives the same barriers to a confirmed, persisted end.
        retry = self._run_halt(registry, token)
        self.assertTrue(retry.ok)
        reaper_exit = _observed(self.store, self.dispatch_id)["reaper_exit"]
        self.assertEqual(reaper_exit["run_token"], token)
        self.assertEqual(registry.pending(), 0)

    def test_rpe_t3_persistence_precedes_registry_removal(self) -> None:
        # RPE-T3: finalize ordering observed directly -- the exact entry is
        # removed only AFTER durable evidence persistence succeeds.
        registry = self._quiet_registry()
        token, _run_dir, _popen = self._register_exited(registry, "h", self.dispatch_id, halt_cleaned=True)
        claimed = registry.claim_by_run_token(token)
        self.assertIsNotNone(claimed)
        events: list = []
        real_record = supervisor.record_registered_wrapper_reap

        def spying_record(*args, **kwargs):  # type: ignore[no-untyped-def]
            events.append(("persist", registry.find_by_run_token(token) is not None))
            return real_record(*args, **kwargs)

        with mock.patch.object(supervisor, "record_registered_wrapper_reap", spying_record):
            finalized = registry.finalize_claimed(claimed)
        self.assertTrue(finalized)
        self.assertEqual(
            events,
            [("persist", True)],
            "evidence must persist exactly once, while the exact entry is still registered",
        )
        self.assertIsNone(registry.find_by_run_token(token))
        self.assertEqual(registry.pending(), 0)
        self.assertEqual(_observed(self.store, self.dispatch_id)["reaper_exit"]["run_token"], token)

    def test_rpe_t4_idempotent_exact_replay_and_conflict_refusal(self) -> None:
        # RPE-T4: an identical persisted reap replays as success with
        # byte-identical evidence; wrong-token, conflicting, unbounded-source,
        # and malformed-existing writes refuse and leave the row byte-identical.
        token = supervisor.new_run_token()
        supervisor.merge_supervisor_observed(
            str(self.db_path), self.dispatch_id, run_token=token, wrapper_pid=1, child_pid=2, control_socket="x"
        )
        self.assertTrue(
            supervisor.record_registered_wrapper_reap(
                str(self.db_path), self.dispatch_id, token,
                returncode=-15, source=supervisor.REAP_SOURCE_HALT_FINALIZE,
                **_COMPLETE_REAP_FACTS,
            )
        )
        baseline = self._raw_observed_json()
        # Identical replay (either recorder) is idempotent success: no duplicate
        # mutation, timestamp and return code unchanged.
        self.assertTrue(
            supervisor.record_registered_wrapper_reap(
                str(self.db_path), self.dispatch_id, token,
                returncode=-15, source=supervisor.REAP_SOURCE_BACKGROUND_REAP,
                **_COMPLETE_REAP_FACTS,
            )
        )
        self.assertEqual(self._raw_observed_json(), baseline)
        # Wrong token refuses; byte-identical.
        self.assertFalse(
            supervisor.record_registered_wrapper_reap(
                str(self.db_path), self.dispatch_id, "f" * 32,
                returncode=-15, source=supervisor.REAP_SOURCE_HALT_FINALIZE,
                **_COMPLETE_REAP_FACTS,
            )
        )
        self.assertEqual(self._raw_observed_json(), baseline)
        # Conflicting existing evidence (different return code) refuses; byte-identical.
        self.assertFalse(
            supervisor.record_registered_wrapper_reap(
                str(self.db_path), self.dispatch_id, token,
                returncode=9, source=supervisor.REAP_SOURCE_HALT_FINALIZE,
                **_COMPLETE_REAP_FACTS,
            )
        )
        self.assertEqual(self._raw_observed_json(), baseline)
        # The source vocabulary is bounded; anything else refuses loudly.
        with self.assertRaises(ValueError):
            supervisor.record_registered_wrapper_reap(
                str(self.db_path), self.dispatch_id, token, returncode=-15, source="adhoc",
                **_COMPLETE_REAP_FACTS,
            )
        self.assertEqual(self._raw_observed_json(), baseline)
        # A malformed existing $.reaper_exit object refuses; byte-identical.
        malformed_dispatch = _seed_dispatch(self.store, self.tmp, key="rpe-t4-malformed")
        malformed_token = supervisor.new_run_token()
        supervisor.merge_supervisor_observed(
            str(self.db_path), malformed_dispatch, run_token=malformed_token,
            wrapper_pid=3, child_pid=4, control_socket="x",
        )
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set observed_values_json = "
                "json_set(observed_values_json, '$.reaper_exit', json('\"bogus\"')) "
                "where dispatch_id = ?",
                (malformed_dispatch,),
            )
        malformed_baseline = self._raw_observed_json(malformed_dispatch)
        self.assertFalse(
            supervisor.record_registered_wrapper_reap(
                str(self.db_path), malformed_dispatch, malformed_token,
                returncode=0, source=supervisor.REAP_SOURCE_BACKGROUND_REAP,
                **_COMPLETE_REAP_FACTS,
            )
        )
        self.assertEqual(self._raw_observed_json(malformed_dispatch), malformed_baseline)

    def test_rpe_c1_incomplete_existing_evidence_refuses_and_exact_replay_survives(self) -> None:
        # T9b/RPE correction 1: a pre-existing $.reaper_exit dictionary whose
        # token and return code match but which is structurally incomplete or
        # invalid (missing/empty/non-string reaped_at, missing/unbounded
        # source, non-integer returncode) is MALFORMED evidence: it must take
        # the loud refusal path and leave the row byte-identical, never the
        # replay path. Revision 7 F2: only the exact version-1 COMPLETE proof
        # object replays as success, without mutation.
        token = supervisor.new_run_token()
        complete = {
            "proof_version": 1,
            "run_token": token,
            "returncode": -15,
            "source": supervisor.REAP_SOURCE_HALT_FINALIZE,
            "reaped_at": "2026-07-21T22:00:00+00:00",
            "registered_wrapper_reaped": True,
            "native_process_group_drained": True,
            "owned_artifacts_absent": {
                "run_dir": True,
                "control_socket": True,
                "zdotdir_parent": True,
            },
        }

        def seeded_row(key: str, existing: dict) -> str:
            dispatch_id = _seed_dispatch(self.store, self.tmp, key=key)
            supervisor.merge_supervisor_observed(
                str(self.db_path), dispatch_id, run_token=token,
                wrapper_pid=7, child_pid=8, control_socket="x",
            )
            with self.store.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set observed_values_json = "
                    "json_set(observed_values_json, '$.reaper_exit', json(?)) "
                    "where dispatch_id = ?",
                    (json.dumps(existing, sort_keys=True), dispatch_id),
                )
            return dispatch_id

        invalid_variants = (
            ("missing-reaped-at", {k: v for k, v in complete.items() if k != "reaped_at"}, -15),
            ("missing-source", {k: v for k, v in complete.items() if k != "source"}, -15),
            ("empty-reaped-at", {**complete, "reaped_at": ""}, -15),
            ("non-string-reaped-at", {**complete, "reaped_at": 1753135200}, -15),
            ("unbounded-source", {**complete, "source": "adhoc"}, -15),
            ("non-integer-returncode", {**complete, "returncode": None}, None),
        )
        for label, existing, returncode in invalid_variants:
            with self.subTest(label):
                dispatch_id = seeded_row(f"rpe-c1-{label}", existing)
                baseline = self._raw_observed_json(dispatch_id)
                with self.assertLogs("agent_comms.supervisor", level="WARNING") as captured:
                    self.assertFalse(
                        supervisor.record_registered_wrapper_reap(
                            str(self.db_path), dispatch_id, token,
                            returncode=returncode, source=supervisor.REAP_SOURCE_BACKGROUND_REAP,
                            **_COMPLETE_REAP_FACTS,
                        ),
                        f"structurally incomplete existing reaper_exit was accepted: {label}",
                    )
                self.assertTrue(
                    any("reaper_exit" in line for line in captured.output),
                    f"no loud malformed/conflict warning for {label}: {captured.output}",
                )
                self.assertEqual(self._raw_observed_json(dispatch_id), baseline)
        # The exact complete four-field replay continues to succeed without
        # mutation through either bounded recorder path.
        replay_id = seeded_row("rpe-c1-complete", complete)
        baseline = self._raw_observed_json(replay_id)
        self.assertTrue(
            supervisor.record_registered_wrapper_reap(
                str(self.db_path), replay_id, token,
                returncode=-15, source=supervisor.REAP_SOURCE_BACKGROUND_REAP,
                **_COMPLETE_REAP_FACTS,
            )
        )
        self.assertEqual(self._raw_observed_json(replay_id), baseline)

    def test_rpe_t5_handle_and_run_identity_cannot_cross(self) -> None:
        # RPE-T5: a stale claimed owner and a same-handle replacement for
        # another run can neither stamp nor remove one another's row/entry; the
        # single-owner, displacement, and failure-release controls all hold.
        registry = self._quiet_registry()
        token_a, _run_dir_a, popen_a = self._register_exited(registry, "h", self.dispatch_id, halt_cleaned=True)
        claimed_a = registry.claim_by_run_token(token_a)
        self.assertIsNotNone(claimed_a)
        self.assertIs(claimed_a.popen, popen_a)
        # Same-handle replacement B for a DIFFERENT dispatch/run displaces A.
        dispatch_b = _seed_dispatch(self.store, self.tmp, key="rpe-t5-b")
        token_b = supervisor.new_run_token()
        run_dir_b = supervisor.create_run_dir(self.control_root, token_b)
        supervisor.merge_supervisor_observed(
            str(self.db_path), dispatch_b, run_token=token_b, wrapper_pid=5, child_pid=6, control_socket="x"
        )
        popen_b = subprocess.Popen(QUICK_EXIT)
        self._children.append(popen_b)
        _wait_until(lambda: popen_b.poll() is not None)
        registry.register(
            "h", popen_b, db_path=str(self.db_path), dispatch_id=dispatch_b,
            run_token=token_b, run_dir=str(run_dir_b),
            control_socket=str(run_dir_b / "s"), child_pgid=self._drained_pgid(),
        )
        # The stale owner's finalize stamps ONLY A's row and removes ONLY the
        # displaced A -- never the live replacement in the slot.
        self.assertTrue(registry.finalize_claimed(claimed_a))
        reap_a = _observed(self.store, self.dispatch_id)["reaper_exit"]
        self.assertEqual(reap_a["run_token"], token_a)
        self.assertNotIn("reaper_exit", _observed(self.store, dispatch_b))
        survivor = registry.find_by_run_token(token_b)
        self.assertIsNotNone(survivor, "same-handle replacement was wrongly removed by the stale owner")
        self.assertIs(survivor.popen, popen_b)
        # Cross-run stamping is refused in BOTH directions by the exact SQL
        # token predicate / write-once identity guard.
        self.assertFalse(
            supervisor.record_registered_wrapper_reap(
                str(self.db_path), dispatch_b, token_a,
                returncode=0, source=supervisor.REAP_SOURCE_BACKGROUND_REAP,
                registered_wrapper_reaped=True, native_process_group_drained=True,
                owned_artifacts_absent={"run_dir": True, "control_socket": True, "zdotdir_parent": True},
            )
        )
        self.assertFalse(
            supervisor.record_registered_wrapper_reap(
                str(self.db_path), self.dispatch_id, token_b,
                returncode=0, source=supervisor.REAP_SOURCE_BACKGROUND_REAP,
                registered_wrapper_reaped=True, native_process_group_drained=True,
                owned_artifacts_absent={"run_dir": True, "control_socket": True, "zdotdir_parent": True},
            )
        )
        self.assertNotIn("reaper_exit", _observed(self.store, dispatch_b))
        # B's own background reap stamps ONLY B's row; A's evidence is untouched.
        self.assertIn("h", registry.reap_ready())
        reap_b = _observed(self.store, dispatch_b)["reaper_exit"]
        self.assertEqual(reap_b["run_token"], token_b)
        self.assertEqual(reap_b["source"], supervisor.REAP_SOURCE_BACKGROUND_REAP)
        self.assertEqual(_observed(self.store, self.dispatch_id)["reaper_exit"], reap_a)

    def test_rpe_t6_abnormal_background_reap_remains_truthful(self) -> None:
        # RPE-T6: an abnormally exited registered wrapper (no worker_exit)
        # still gets exactly ONE same-run reap object -- via the same exact
        # identity-bound primitive -- with no ledger transition; concurrent
        # reapers process it once and replay is idempotent.
        registry = self._quiet_registry()
        token, run_dir, popen = self._register_exited(registry, "h", self.dispatch_id)
        results: dict = {}

        def run(name: str) -> None:
            results[name] = registry.reap_ready()

        threads = [threading.Thread(target=run, args=(name,)) for name in ("first", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)
            self.assertFalse(thread.is_alive())
        combined = (results.get("first") or []) + (results.get("second") or [])
        self.assertEqual(combined.count("h"), 1)
        observed = _observed(self.store, self.dispatch_id)
        self.assertNotIn("worker_exit", observed)
        reaper_exit = observed["reaper_exit"]
        self.assertEqual(reaper_exit["run_token"], token)
        self.assertEqual(reaper_exit["returncode"], popen.returncode)
        self.assertEqual(reaper_exit["source"], supervisor.REAP_SOURCE_BACKGROUND_REAP)
        baseline = self._raw_observed_json()
        # Idempotent replay: nothing left to reap, evidence byte-identical.
        self.assertEqual(registry.reap_ready(), [])
        self.assertEqual(self._raw_observed_json(), baseline)
        self.assertEqual(registry.pending(), 0)
        with self.store.connection() as conn:
            status = conn.execute(
                "select status from dispatch_ledger where dispatch_id = ?", (self.dispatch_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "queued")
        self.assertFalse(run_dir.exists())


# --------------------------------------------------------------------------- #
# F3: complete cleanup confirmation before a confirmed HALT (no bind)
# --------------------------------------------------------------------------- #


class HaltCleanupConfirmationTest(SupervisorTestBase):
    """A confirmed HALT requires every OWNED filesystem artifact to be gone: the
    run directory, its control socket, and the owned ZDOTDIR PARENT (the
    ``agent-comms-zdotdir-*`` directory ``_cleanup_zdotdir`` removes, not merely
    the nested child path). Each representable residue must refuse confirmation."""

    def _sup_with_dirs(self):  # type: ignore[no-untyped-def]
        run_token = supervisor.new_run_token()
        run_dir = supervisor.create_run_dir(self.control_root, run_token)
        socket_path = run_dir / "s"
        socket_path.touch()  # a plain file standing in for the bound control socket
        zparent = self.tmp / "agent-comms-zdotdir-ctl"
        zdotdir = zparent / "z"
        zdotdir.mkdir(parents=True)
        payload = supervisor.BootstrapPayload(
            protocol_version=supervisor.PROTOCOL_VERSION, dispatch_id=self.dispatch_id,
            run_token=run_token, db_path=str(self.db_path), control_root=str(self.control_root),
            ttl_seconds=30.0, kill_after_seconds=2.0, zdotdir=str(zdotdir), expected_close_by=None,
        )
        sup = supervisor._Supervisor(payload, SLEEPER)
        sup.run_dir = run_dir
        sup.socket_path = socket_path
        return sup, run_dir, socket_path, zparent, zdotdir

    def test_halt_cleanup_refuses_on_zdotdir_parent_residue(self) -> None:
        sup, run_dir, _socket_path, zparent, _zdotdir = self._sup_with_dirs()

        def leaky_zdotdir(z):  # type: ignore[no-untyped-def]
            # Clears the NESTED child but leaves the OWNED parent (e.g. an rmtree
            # denied on a sibling); the child-only check would miss this residue.
            Path(z).rmdir()
            return Path(z).parent

        with mock.patch.object(supervisor, "_cleanup_zdotdir", leaky_zdotdir):
            ok = sup._halt_cleanup()
        self.assertFalse(ok, "residual owned ZDOTDIR parent must refuse confirmation")
        self.assertFalse(run_dir.exists())  # run dir + socket removed (isolates the control)
        self.assertTrue(zparent.exists())  # owned parent residue remains

    def test_halt_cleanup_refuses_on_control_socket_residue(self) -> None:
        sup, _run_dir, socket_path, zparent, _zdotdir = self._sup_with_dirs()
        # The run directory is reported gone while its control socket lingers as a
        # DISTINCT residue the run-directory check alone would miss.
        sup.run_dir = None
        self.assertTrue(socket_path.exists())
        ok = sup._halt_cleanup()  # real _cleanup_zdotdir removes the owned parent
        self.assertFalse(ok, "residual control socket must refuse confirmation")
        self.assertFalse(zparent.exists())  # zdotdir cleaned -> socket is the only residue

    def test_halt_cleanup_refuses_on_run_directory_residue(self) -> None:
        # Control: the run-directory gate holds for the confirmation path alongside
        # the socket and ZDOTDIR-parent gates.
        sup, run_dir, _socket_path, _zparent, _zdotdir = self._sup_with_dirs()
        with mock.patch.object(supervisor, "cleanup_run_dir", lambda rd: None):
            ok = sup._halt_cleanup()
        self.assertFalse(ok, "residual run directory must refuse confirmation")
        self.assertTrue(run_dir.exists())

    def test_halt_cleanup_confirms_when_every_artifact_is_gone(self) -> None:
        # Positive control: with the run dir, socket, and owned ZDOTDIR parent all
        # removed, cleanup confirms.
        sup, run_dir, socket_path, zparent, _zdotdir = self._sup_with_dirs()
        ok = sup._halt_cleanup()
        self.assertTrue(ok)
        self.assertFalse(run_dir.exists())
        self.assertFalse(socket_path.exists())
        self.assertFalse(zparent.exists())


class RevisionSevenReaperProofPredicateTest(unittest.TestCase):
    """Revision 7 F2 (red-first): ``supervisor.confirmed_termination_evidence``
    must require the COMPLETE exact version-1 ``$.reaper_exit`` proof to authorize
    a ``same_run_exit_confirmed`` / ``supervised_halt_confirmed`` cleanup. An old
    four-field ``reaper_exit``, a false/incomplete proof, a ``worker_exit`` alone,
    and a stale/wrong-token proof must all refuse. Red against the current
    predicate, which authorizes from any same-run object with a matching token.
    """

    TOKEN = "e" * 32

    def _complete_proof(self) -> dict:
        return {
            "proof_version": 1,
            "run_token": self.TOKEN,
            "returncode": 0,
            "source": "halt_finalize",
            "reaped_at": "2026-07-26T00:00:00+00:00",
            "registered_wrapper_reaped": True,
            "native_process_group_drained": True,
            "owned_artifacts_absent": {"run_dir": True, "control_socket": True, "zdotdir_parent": True},
        }

    def test_malformed_incomplete_stale_or_wrong_token_reap_refuses_cancelled(self) -> None:
        cte = supervisor.confirmed_termination_evidence
        # Old four-field reaper_exit (matching token) is not the version-1 proof.
        self.assertIsNone(
            cte(
                {
                    "run_token": self.TOKEN,
                    "reaper_exit": {
                        "run_token": self.TOKEN,
                        "returncode": 0,
                        "reaped_at": "t",
                        "source": "halt_finalize",
                    },
                },
                self.TOKEN,
            )
        )
        # Complete-shaped but a barrier boolean is false -> incomplete proof.
        false_boolean = self._complete_proof()
        false_boolean["native_process_group_drained"] = False
        self.assertIsNone(cte({"run_token": self.TOKEN, "reaper_exit": false_boolean}, self.TOKEN))
        # A same-run worker_exit alone (child-exit evidence) never authorizes.
        self.assertIsNone(
            cte(
                {"run_token": self.TOKEN, "worker_exit": {"run_token": self.TOKEN, "returncode": 0}},
                self.TOKEN,
            )
        )
        # A wrong-token reaper is never same-run evidence.
        wrong = self._complete_proof()
        wrong["run_token"] = "f" * 32
        self.assertIsNone(cte({"run_token": self.TOKEN, "reaper_exit": wrong}, self.TOKEN))
        # The complete exact version-1 proof (matching token) DOES authorize.
        self.assertIsNotNone(cte({"run_token": self.TOKEN, "reaper_exit": self._complete_proof()}, self.TOKEN))


class RevisionSevenBackgroundReapPersistenceTest(SupervisorTestBase):
    """Revision 7 F2 no-HALT producer (red-first): the background registry reap
    must clean and verify the native process group + owned artifacts BEFORE it
    persists, so the durable ``$.reaper_exit`` it writes is the COMPLETE exact
    version-1 proof (``source=background_reap``, group-drain and owned-artifact
    absence facts all true), never the old four-field object. Red against the
    current recorder, which persists a four-field reaper_exit with no
    group/cleanup proof.
    """

    def test_background_reap_waits_for_group_and_cleanup_before_qualifying(self) -> None:
        token = "c" * 32
        supervisor.merge_supervisor_observed(
            str(self.db_path),
            self.dispatch_id,
            run_token=token,
            wrapper_pid=1,
            child_pid=2,
            control_socket="x",
        )
        self.assertTrue(
            supervisor.record_reaper_exit(str(self.db_path), self.dispatch_id, token, returncode=0)
        )
        reaper = _observed(self.store, self.dispatch_id)["reaper_exit"]
        # The persisted object must be the complete exact version-1 proof that
        # records group drain and owned-artifact cleanup as already verified.
        self.assertEqual(reaper.get("proof_version"), 1)
        self.assertEqual(reaper.get("source"), supervisor.REAP_SOURCE_BACKGROUND_REAP)
        self.assertIs(reaper.get("native_process_group_drained"), True)
        self.assertEqual(
            reaper.get("owned_artifacts_absent"),
            {"run_dir": True, "control_socket": True, "zdotdir_parent": True},
        )


class RevisionSevenReaperRegistryOrderTest(SupervisorTestBase):
    """Revision 7 F2 (red-first): the parent reaper registry must prove every
    physical barrier BEFORE it publishes a complete ``$.reaper_exit`` proof or
    drops the exact entry.

    These methods mechanically drive the production ``ReaperRegistry``
    (``finalize_claimed`` for the HALT producer, ``reap_ready``/``_reap_entry``
    for the background no-HALT producer) against REAL wrapper/child process
    groups and REAL owned artifacts. They do not inspect source strings or
    helper presence. Red against the current registry, which persists (with
    manufactured all-true facts) and drops without waiting for the native child
    group to drain, and which cleans owned artifacts only AFTER it has already
    persisted.
    """

    def _quiet_registry(self) -> supervisor.ReaperRegistry:
        registry = supervisor.ReaperRegistry()
        registry._ensure_thread_locked = lambda: None  # type: ignore[assignment]
        return registry

    def _drained_child_pgid(self) -> int:
        """An exited ``start_new_session`` child: its process-group id equals its
        PID and the group is drained, so ``process_group_empty`` succeeds."""
        child = subprocess.Popen(QUICK_EXIT, start_new_session=True)
        self._children.append(child)
        _wait_until(lambda: child.poll() is not None)
        child.wait()
        return child.pid

    def _live_child(self) -> subprocess.Popen:
        """A live ``start_new_session`` child: its pgid==pid names a NON-empty
        native group that only drains when the group is signalled."""
        child = subprocess.Popen(SLEEPER, start_new_session=True)
        self._children.append(child)
        _wait_until(lambda: _pid_alive(child.pid))
        return child

    def _register_drainable(self, registry, handle, dispatch_id, *, child_pgid):  # type: ignore[no-untyped-def]
        """Register an EXITED wrapper for a fresh run token with every owned
        artifact (run directory, control socket, owned ZDOTDIR parent) present,
        and pin the exact wrapper/native group identities and control socket on
        the entry. Returns the token, the entry, and the owned artifact paths."""
        token = supervisor.new_run_token()
        supervisor.merge_supervisor_observed(
            str(self.db_path), dispatch_id, run_token=token,
            wrapper_pid=1, child_pid=child_pgid, control_socket="x",
        )
        wrapper = subprocess.Popen(QUICK_EXIT, start_new_session=True)
        self._children.append(wrapper)
        _wait_until(lambda: wrapper.poll() is not None)
        wrapper.wait()
        run_dir = supervisor.create_run_dir(self.control_root, token)
        socket_path = run_dir / "s"
        socket_path.touch()
        zparent = self.tmp / f"agent-comms-zdotdir-{handle}"
        zdotdir = zparent / "z"
        zdotdir.mkdir(parents=True)
        registry.register(
            handle, wrapper, db_path=str(self.db_path), dispatch_id=dispatch_id,
            run_token=token, run_dir=str(run_dir),
        )
        entry = registry.find_by_run_token(token)
        entry.control_socket = str(socket_path)
        entry.wrapper_pgid = wrapper.pid
        entry.child_pgid = child_pgid
        entry.zdotdir = str(zdotdir)
        return token, entry, run_dir, socket_path, zparent

    def test_halt_finalize_requires_exact_group_and_artifact_barriers_before_proof(self) -> None:
        registry = self._quiet_registry()
        live = self._live_child()
        token, _entry, run_dir, _socket, zparent = self._register_drainable(
            registry, "h", self.dispatch_id, child_pgid=live.pid
        )
        # Barrier 1 -- native child group still LIVE: HALT finalize must publish
        # no proof and drop no entry; it releases the claim for bounded retry.
        claimed = registry.claim_by_run_token(token)
        self.assertIsNotNone(claimed)
        self.assertFalse(
            registry.finalize_claimed(claimed),
            "HALT finalize confirmed while the registered native child group was still live",
        )
        self.assertNotIn(
            "reaper_exit", _observed(self.store, self.dispatch_id),
            "a partial/false reaper_exit was published before the native group drained",
        )
        retained = registry.find_by_run_token(token)
        self.assertIsNotNone(retained, "the exact entry was dropped before its barriers passed")
        self.assertFalse(retained.claimed, "the claim was not released for bounded retry")
        # Drain the native child group; the owned artifacts still linger.
        live.terminate()
        live.wait()
        _wait_until(lambda: not _pid_alive(live.pid))
        # Barrier 2 -- owned artifacts still present: HALT finalize freshly
        # verifies absence and must still refuse without a proof or a drop.
        claimed = registry.claim_by_run_token(token)
        self.assertIsNotNone(claimed)
        self.assertFalse(
            registry.finalize_claimed(claimed),
            "HALT finalize confirmed while the exact owned run directory still existed",
        )
        self.assertNotIn("reaper_exit", _observed(self.store, self.dispatch_id))
        self.assertIsNotNone(registry.find_by_run_token(token))
        # Clear the owned artifacts (the server-side HALT cleanup the parent now
        # only VERIFIES): every barrier is satisfiable, so finalize confirms,
        # persists exactly the complete version-1 proof, and drops the entry.
        supervisor.cleanup_run_dir(run_dir)
        supervisor._cleanup_zdotdir(Path(self.tmp / "agent-comms-zdotdir-h" / "z"))
        claimed = registry.claim_by_run_token(token)
        self.assertIsNotNone(claimed)
        self.assertTrue(registry.finalize_claimed(claimed))
        reap = _observed(self.store, self.dispatch_id)["reaper_exit"]
        self.assertEqual(reap["proof_version"], 1)
        self.assertEqual(reap["source"], supervisor.REAP_SOURCE_HALT_FINALIZE)
        self.assertIs(reap["registered_wrapper_reaped"], True)
        self.assertIs(reap["native_process_group_drained"], True)
        self.assertEqual(
            reap["owned_artifacts_absent"],
            {"run_dir": True, "control_socket": True, "zdotdir_parent": True},
        )
        self.assertIsNone(registry.find_by_run_token(token))
        self.assertFalse(run_dir.exists())
        self.assertFalse(zparent.exists())

    def test_background_reap_cleans_and_verifies_before_complete_proof_and_drop(self) -> None:
        registry = self._quiet_registry()
        token, _entry, run_dir, socket_path, zparent = self._register_drainable(
            registry, "h", self.dispatch_id, child_pgid=self._drained_child_pgid()
        )
        # Order trace: capture, at the exact moment persistence is invoked,
        # whether the owned artifacts are already absent (cleaned + verified)
        # and whether the exact entry is still registered (not yet dropped).
        events: list = []
        real_record = supervisor.record_registered_wrapper_reap

        def spying_record(*args, **kwargs):  # type: ignore[no-untyped-def]
            events.append(
                (
                    "persist",
                    run_dir.exists(),
                    socket_path.exists(),
                    zparent.exists(),
                    registry.find_by_run_token(token) is not None,
                )
            )
            return real_record(*args, **kwargs)

        with mock.patch.object(supervisor, "record_registered_wrapper_reap", spying_record):
            reaped = registry.reap_ready()
        self.assertIn("h", reaped)
        # Cleanup + fresh absence verification must PRECEDE persistence, and
        # persistence must PRECEDE the registry drop.
        self.assertEqual(
            events,
            [("persist", False, False, False, True)],
            "background reap persisted before cleaning/verifying its owned artifacts, "
            "or dropped the entry before persisting",
        )
        reap = _observed(self.store, self.dispatch_id)["reaper_exit"]
        self.assertEqual(reap["proof_version"], 1)
        self.assertEqual(reap["source"], supervisor.REAP_SOURCE_BACKGROUND_REAP)
        self.assertIs(reap["native_process_group_drained"], True)
        self.assertEqual(
            reap["owned_artifacts_absent"],
            {"run_dir": True, "control_socket": True, "zdotdir_parent": True},
        )
        self.assertIsNone(registry.find_by_run_token(token))
        self.assertFalse(run_dir.exists())
        self.assertFalse(socket_path.exists())
        self.assertFalse(zparent.exists())

    def test_barrier_or_persistence_failure_retains_registry_without_partial_proof(self) -> None:
        # Unrelated residue must never be perturbed by a refusing reap.
        unrelated = self.tmp / "agent-comms-zdotdir-unrelated"
        unrelated.mkdir()
        (unrelated / "keep").write_text("byte-identical")
        unrelated_before = sorted(p.name for p in unrelated.iterdir())

        # (a) barrier failure -- the native child group is still live: the
        # background reap publishes no proof, retains the exact association, and
        # releases the claim; it never touches unrelated residue.
        registry = self._quiet_registry()
        live = self._live_child()
        token, entry, run_dir, _socket, _zparent = self._register_drainable(
            registry, "h", self.dispatch_id, child_pgid=live.pid
        )
        self.assertEqual(
            registry.reap_ready(), [],
            "background reap dropped an entry whose native child group was still live",
        )
        self.assertNotIn("reaper_exit", _observed(self.store, self.dispatch_id))
        retained = registry.find_by_run_token(token)
        self.assertIsNotNone(retained, "the exact association was discarded on a barrier miss")
        self.assertFalse(retained.claimed, "the claim was not released for bounded retry")
        self.assertEqual(sorted(p.name for p in unrelated.iterdir()), unrelated_before)
        self.assertEqual((unrelated / "keep").read_text(), "byte-identical")
        live.terminate()
        live.wait()
        _wait_until(lambda: not _pid_alive(live.pid))

        # (b) persistence failure -- barriers pass but the SQL write raises: no
        # partial proof is published and the exact entry is retained, claim
        # released, for a bounded retry.
        registry2 = self._quiet_registry()
        d2 = _seed_dispatch(self.store, self.tmp, key="rev7-persist-fail")
        token2, _entry2, _run_dir2, _socket2, _zparent2 = self._register_drainable(
            registry2, "h2", d2, child_pgid=self._drained_child_pgid()
        )
        with mock.patch.object(
            supervisor, "record_registered_wrapper_reap",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            self.assertEqual(registry2.reap_ready(), [])
        self.assertNotIn("reaper_exit", _observed(self.store, d2))
        retained2 = registry2.find_by_run_token(token2)
        self.assertIsNotNone(retained2, "the exact association was discarded on persistence failure")
        self.assertFalse(retained2.claimed)
        # A healthy retry drives the same barriers to a persisted, dropped end.
        self.assertIn("h2", registry2.reap_ready())
        self.assertEqual(_observed(self.store, d2)["reaper_exit"]["run_token"], token2)
        self.assertIsNone(registry2.find_by_run_token(token2))

    def test_cleanup_exception_retains_unclaims_and_retries(self) -> None:
        # F6: an unexpected exception from the exact-owned cleanup (not a mere
        # False barrier) must behave like every other per-entry failure: no
        # proof is published, the exact association is retained, and the claim
        # is released so a later healthy pass can retry the SAME entry.
        registry = self._quiet_registry()
        token, _entry, run_dir, socket_path, zparent = self._register_drainable(
            registry, "h", self.dispatch_id, child_pgid=self._drained_child_pgid()
        )
        with mock.patch.object(
            supervisor, "cleanup_run_dir", side_effect=OSError(16, "resource busy")
        ):
            self.assertEqual(
                registry.reap_ready(), [],
                "reap_ready dropped an entry whose owned cleanup raised",
            )
        self.assertNotIn(
            "reaper_exit", _observed(self.store, self.dispatch_id),
            "a proof was published despite the owned-cleanup exception",
        )
        retained = registry.find_by_run_token(token)
        self.assertIsNotNone(retained, "the exact association was discarded on a cleanup exception")
        self.assertFalse(retained.claimed, "the claim was not released for retry after the exception")
        # The later healthy attempt drives the SAME exact association through
        # every barrier and drops the entry only after the durable proof exists.
        self.assertIn("h", registry.reap_ready())
        reap = _observed(self.store, self.dispatch_id)["reaper_exit"]
        self.assertEqual(reap["run_token"], token)
        self.assertEqual(reap["source"], supervisor.REAP_SOURCE_BACKGROUND_REAP)
        self.assertIsNone(registry.find_by_run_token(token))
        self.assertFalse(run_dir.exists())
        self.assertFalse(socket_path.exists())
        self.assertFalse(zparent.exists())

    def test_background_loop_survives_entry_exception_and_retries(self) -> None:
        # F6: the LIVE background loop must contain a per-entry exception --
        # logged, claim released, entry retained, thread still alive -- and a
        # later healthy pass must retry the same exact association to a
        # persisted, dropped end. Synchronization is event-driven and bounded.
        registry = supervisor.ReaperRegistry()
        self.addCleanup(registry.stop)
        failed = threading.Event()
        heal = threading.Event()
        real_cleanup = supervisor.cleanup_run_dir

        def flaky_cleanup(path):  # type: ignore[no-untyped-def]
            if not heal.is_set():
                failed.set()
                raise OSError(16, "resource busy")
            return real_cleanup(path)

        with mock.patch.object(supervisor, "cleanup_run_dir", side_effect=flaky_cleanup):
            token, _entry, run_dir, _socket, zparent = self._register_drainable(
                registry, "h", self.dispatch_id, child_pgid=self._drained_child_pgid()
            )
            self.assertTrue(failed.wait(8.0), "the background loop never attempted the entry")

            def retained_and_released():  # type: ignore[no-untyped-def]
                entry = registry.find_by_run_token(token)
                return entry is not None and not entry.claimed

            self.assertTrue(
                _wait_until(retained_and_released),
                "the entry was dropped or its claim stayed stranded after the exception",
            )
            self.assertNotIn("reaper_exit", _observed(self.store, self.dispatch_id))
            self.assertTrue(
                registry._thread is not None and registry._thread.is_alive(),
                "the per-entry exception terminated the background reaper loop",
            )
            heal.set()
            registry._wake.set()
            self.assertTrue(
                _wait_until(lambda: registry.find_by_run_token(token) is None),
                "the healthy retry never reaped the exact retained entry",
            )
        reap = _observed(self.store, self.dispatch_id)["reaper_exit"]
        self.assertEqual(reap["run_token"], token)
        self.assertEqual(reap["source"], supervisor.REAP_SOURCE_BACKGROUND_REAP)
        self.assertFalse(run_dir.exists())
        self.assertFalse(zparent.exists())

    def test_stop_joins_and_prevents_post_stop_reap(self) -> None:
        # F6: stop() must signal AND synchronously join the owned thread, and
        # the wake stop() delivers must never trigger one final post-stop reap.
        # The wake is instrumented so the park/flip/stop handshake is entirely
        # event-driven: the loop signals each time it parks in wait() and wakes
        # only on set(), so no timeout tick can race the observation.
        registry = supervisor.ReaperRegistry()
        self.addCleanup(registry.stop)
        parks = threading.Semaphore(0)

        class SignallingWake(threading.Event):
            def wait(self, timeout=None):  # type: ignore[override]
                parks.release()
                return super().wait()

        registry._wake = SignallingWake()
        child_pgid = self._drained_child_pgid()
        token = supervisor.new_run_token()
        supervisor.merge_supervisor_observed(
            str(self.db_path), self.dispatch_id, run_token=token,
            wrapper_pid=1, child_pid=child_pgid, control_socket="x",
        )
        wrapper = subprocess.Popen(SLEEPER, start_new_session=True)
        self._children.append(wrapper)
        run_dir = supervisor.create_run_dir(self.control_root, token)
        socket_path = run_dir / "s"
        socket_path.touch()
        zparent = self.tmp / "agent-comms-zdotdir-stop"
        zdotdir = zparent / "z"
        zdotdir.mkdir(parents=True)
        registry.register(
            "h", wrapper, db_path=str(self.db_path), dispatch_id=self.dispatch_id,
            run_token=token, run_dir=str(run_dir), zdotdir=str(zdotdir),
            control_socket=str(socket_path), wrapper_pgid=wrapper.pid, child_pgid=child_pgid,
        )
        thread = registry._thread
        self.assertIsNotNone(thread)
        # Handshake: the register wake drives one pass over the still-live
        # wrapper; the SECOND park means that pass is complete and the loop is
        # parked in wait() with nothing left in flight.
        self.assertTrue(parks.acquire(timeout=8.0))
        self.assertTrue(parks.acquire(timeout=8.0), "the reaper loop never parked after its pass")
        # Flip the entry to fully reapable while the loop is parked: only
        # stop()'s own wake could reap it now.
        wrapper.terminate()
        wrapper.wait()
        registry.stop()
        self.assertFalse(
            thread.is_alive(),
            "stop() returned before the owned reaper thread quiesced",
        )
        thread.join(8.0)  # bounded backstop; a no-op once stop() itself joins
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(
            registry.find_by_run_token(token),
            "stop()'s wake triggered a final post-stop reap of the entry",
        )
        self.assertNotIn(
            "reaper_exit", _observed(self.store, self.dispatch_id),
            "a post-stop reap published proof after stop()",
        )
        self.assertTrue(run_dir.exists())
        self.assertTrue(zparent.exists())


class RevisionSevenF2BypassTest(unittest.TestCase):
    """Revision 7 F2 (red-first): a matching same-run ``$.worker_exit`` is
    child-exit evidence only. It must never let a failed authenticated HALT
    report success (the adapter HALT fallback), and the janitor cleanup gate
    must never authorize a ``worker_exited_before_close`` /
    ``supervised_halt_confirmed`` / ``same_run_exit_confirmed`` deletion from a
    matching result string alone. Only the exact version-1 COMPLETE
    ``$.reaper_exit`` proof confirms the HALT fallback or those cleanup strings;
    ``not_started`` stays separately safe on the exact-token match.
    """

    TOKEN = "a" * 32

    def _complete_proof(self) -> dict:
        return {
            "proof_version": 1,
            "run_token": self.TOKEN,
            "returncode": 0,
            "source": "halt_finalize",
            "reaped_at": "2026-07-26T00:00:00+00:00",
            "registered_wrapper_reaped": True,
            "native_process_group_drained": True,
            "owned_artifacts_absent": {
                "run_dir": True,
                "control_socket": True,
                "zdotdir_parent": True,
            },
        }

    def _adapter_with_owned_zdotdir(self, handle: str):
        adapter = ProcessSpawnAdapter()
        parent = Path(tempfile.mkdtemp(prefix="agent-comms-zdotdir-f2bypass-"))
        self.addCleanup(shutil.rmtree, parent, ignore_errors=True)
        zdotdir = parent / "empty-zdotdir"
        zdotdir.mkdir()
        adapter._zdotdirs[handle] = zdotdir
        return adapter, parent

    def test_adapter_halt_fallback_rejects_worker_exit_without_complete_reaper_proof(self) -> None:
        unconfirmed = supervisor.ControlResult(ok=False, state="unreachable", error="halt not acked")

        # Negative: request_halt is unconfirmed and the only same-run exit object
        # is a matching-token worker_exit (child-exit evidence). The HALT fallback
        # must refuse (SupervisorUnreachable) and must NOT clean the owned ZDOTDIR.
        adapter, parent = self._adapter_with_owned_zdotdir("h-neg")
        observed = {
            "run_token": self.TOKEN,
            "control_socket": "sock",
            "worker_exit": {"run_token": self.TOKEN, "returncode": 0},
        }
        with mock.patch.object(supervisor, "request_halt", return_value=unconfirmed):
            with self.assertRaises(SupervisorUnreachable):
                adapter._authenticated_halt("h-neg", "sock", self.TOKEN, observed)
        self.assertTrue(parent.exists(), "owned ZDOTDIR must survive an unconfirmed HALT")
        self.assertIn("h-neg", adapter._zdotdirs)

        # Positive control: the exact version-1 COMPLETE reaper proof for the
        # current token satisfies the SQL fallback even when request_halt is
        # unconfirmed, so the HALT confirms and the owned ZDOTDIR is cleaned.
        adapter2, parent2 = self._adapter_with_owned_zdotdir("h-pos")
        observed_ok = {
            "run_token": self.TOKEN,
            "control_socket": "sock",
            "reaper_exit": self._complete_proof(),
        }
        with mock.patch.object(supervisor, "request_halt", return_value=unconfirmed):
            adapter2._authenticated_halt("h-pos", "sock", self.TOKEN, observed_ok)
        self.assertFalse(parent2.exists(), "confirmed HALT must clean the owned ZDOTDIR")
        self.assertNotIn("h-pos", adapter2._zdotdirs)

    def test_cleanup_result_strings_require_complete_reaper_proof_except_not_started(self) -> None:
        cte = supervisor.confirmed_termination_evidence
        # Exact matching SQL/run-directory token but NO complete reaper proof:
        # these result strings must NOT by themselves authorize janitor cleanup.
        for result in (
            "worker_exited_before_close",
            "supervised_halt_confirmed",
            "same_run_exit_confirmed",
        ):
            self.assertIsNone(
                cte({"run_token": self.TOKEN, "termination_result": result}, self.TOKEN),
                f"{result} must not authorize cleanup without complete same-run proof",
            )
        # not_started is separately safe on the exact-token match.
        self.assertIsNotNone(
            cte({"run_token": self.TOKEN, "termination_result": "not_started"}, self.TOKEN)
        )
        # The exact version-1 COMPLETE reaper proof authorizes cleanup.
        self.assertIsNotNone(
            cte({"run_token": self.TOKEN, "reaper_exit": self._complete_proof()}, self.TOKEN)
        )


if __name__ == "__main__":
    unittest.main()
