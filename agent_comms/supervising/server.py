"""Supervisor server: runs inside the wrapper process.

Bootstrap payload over the inherited socketpair, the control socket, the hard
TTL, exit evidence and teardown.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import evidence, paths, process, protocol, zdotdir


@dataclass
class BootstrapPayload:
    protocol_version: int
    dispatch_id: str
    run_token: str
    db_path: str
    control_root: str
    ttl_seconds: float
    kill_after_seconds: float
    zdotdir: str | None
    expected_close_by: str | None

    @classmethod
    def from_json(cls, raw: bytes) -> "BootstrapPayload":
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise paths.SupervisorError("bootstrap payload is not an object")
        if data.get("protocol_version") != paths.PROTOCOL_VERSION:
            raise paths.SupervisorError(
                f"bootstrap protocol_version {data.get('protocol_version')!r} != {paths.PROTOCOL_VERSION}"
            )
        try:
            return cls(
                protocol_version=int(data["protocol_version"]),
                dispatch_id=str(data["dispatch_id"]),
                run_token=str(data["run_token"]),
                db_path=str(data["db_path"]),
                control_root=str(data["control_root"]),
                ttl_seconds=float(data["ttl_seconds"]),
                kill_after_seconds=float(data["kill_after_seconds"]),
                zdotdir=(str(data["zdotdir"]) if data.get("zdotdir") else None),
                expected_close_by=(str(data["expected_close_by"]) if data.get("expected_close_by") else None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise paths.SupervisorError(f"malformed bootstrap payload: {exc}") from exc


class _Supervisor:
    def __init__(self, payload: BootstrapPayload, child_command: Sequence[str]) -> None:
        self.payload = payload
        self.child_command = list(child_command)
        self.root = Path(payload.control_root)
        self.run_dir: Path | None = None
        self.socket_path: Path | None = None
        self.listener: socket.socket | None = None
        self.child: subprocess.Popen | None = None
        self._exit_recorded = False
        self._exit_persisted = False
        self._halted = False
        self._cleaned = False
        # The authenticated HALT connection is held open from the confirmed
        # response until wrapper shutdown so the client observes EOF (barrier 2)
        # only when the wrapper actually tears down; ``run_supervisor`` closes it
        # last, after cleanup.
        self._halt_conn: socket.socket | None = None
        self._deadline = time.monotonic() + max(payload.ttl_seconds, 0.0)

    # -- setup (all before READY) ------------------------------------------ #

    def prepare(self) -> None:
        self.run_dir = paths.create_run_dir(self.root, self.payload.run_token)
        self.socket_path = paths.control_socket_for(self.root, self.payload.run_token)
        # Assert the byte ceiling BEFORE bind; clean the attempted run dir on
        # failure so no partial run directory leaks and READY is never sent.
        paths.assert_sun_path_ok(self.socket_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(8)
        except OSError:
            listener.close()
            raise
        self.listener = listener

    def launch_child(self) -> None:
        assert self.run_dir is not None
        # The child is spawned with close_fds=True and NO pass_fds, so every
        # inherited FD >= 3 (the still-open bootstrap peer and the bound
        # listener) is closed at exec. Python already opens those sockets
        # non-inheritable (CLOEXEC) as a second layer, and no control connection
        # has been accepted yet, so no bootstrap/listener/accepted control FD can
        # reach the runtime child. start_new_session gives the child its own
        # bounded process group that the wrapper owns.
        self.child = subprocess.Popen(
            self.child_command,
            env=os.environ.copy(),
            start_new_session=True,
            close_fds=True,
        )
        paths.write_owner_marker(
            self.run_dir,
            dispatch_id=self.payload.dispatch_id,
            run_token=self.payload.run_token,
            wrapper_pid=os.getpid(),
        )

    def merge_observed(self) -> None:
        assert self.child is not None and self.socket_path is not None
        evidence.merge_supervisor_observed(
            self.payload.db_path,
            self.payload.dispatch_id,
            run_token=self.payload.run_token,
            wrapper_pid=os.getpid(),
            child_pid=self.child.pid,
            control_socket=str(self.socket_path),
        )

    def ready_ack(self) -> dict[str, Any]:
        assert self.child is not None and self.socket_path is not None
        return {
            "ready": True,
            "child_pid": self.child.pid,
            "wrapper_pid": os.getpid(),
            "control_socket": str(self.socket_path),
            "run_token": self.payload.run_token,
        }

    # -- serve loop -------------------------------------------------------- #

    def serve(self) -> int:
        assert self.child is not None and self.listener is not None
        self.listener.settimeout(0.25)
        while True:
            rc = self.child.poll()
            if rc is not None:
                self._record_exit(rc, source="child")
                return rc if rc is not None else 0
            if time.monotonic() >= self._deadline:
                rc = process.terminate_process_tree(self.child, self.payload.kill_after_seconds)
                self._record_exit(rc, source="timeout")
                return 124
            try:
                conn, _ = self.listener.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                continue
            keep_open = False
            try:
                keep_open = self._handle_request(conn)
            finally:
                # A confirmed HALT retains its connection (stored on
                # ``self._halt_conn``) so the client sees EOF only at wrapper
                # shutdown; every other request closes here.
                if not keep_open:
                    conn.close()
            if self._halted:
                return self.child.returncode if self.child.returncode is not None else 0

    def _handle_request(self, conn: socket.socket) -> bool:
        """Serve one control request. Returns ``True`` only for a confirmed HALT
        whose connection must stay open (barrier 2 EOF); every other outcome
        returns ``False`` so ``serve()`` closes the connection immediately."""
        assert self.child is not None
        line = protocol._read_line(conn, timeout=2.0)
        if line is None:
            protocol._send_json(conn, {"ok": False, "error": "empty request"})
            return False
        try:
            req = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            protocol._send_json(conn, {"ok": False, "error": "malformed request"})
            return False
        if not isinstance(req, dict):
            protocol._send_json(conn, {"ok": False, "error": "malformed request"})
            return False
        # Authenticate protocol + exact run token before ANY action. A wrong
        # token/protocol never signals and never mutates: it is refused.
        if req.get("protocol_version") != paths.PROTOCOL_VERSION:
            protocol._send_json(conn, {"ok": False, "error": "unsupported protocol"})
            return False
        presented = req.get("run_token")
        if not isinstance(presented, str) or not secrets.compare_digest(presented, self.payload.run_token):
            protocol._send_json(conn, {"ok": False, "error": "unauthenticated"})
            return False
        op = req.get("op")
        if op == "STATUS":
            protocol._send_json(
                conn,
                {"ok": True, "state": "running", "child_pid": self.child.pid, "wrapper_pid": os.getpid()},
            )
            return False
        if op == "HALT":
            return self._handle_halt(conn)
        protocol._send_json(conn, {"ok": False, "error": f"unknown op: {op!r}"})
        return False

    def _handle_halt(self, conn: socket.socket) -> bool:
        """Authenticated HALT with the full pre-response barrier.

        A successful ``halted`` response is sent ONLY after, in order: the owned
        native child tree is terminated/waited; the complete process group is
        proven empty by the group-level kernel predicate; the exact-token
        ``worker_exit`` is persisted; and the run directory / ZDOTDIR are cleaned.
        Any barrier failure sends an explicit ``ok=False`` refusal (the client
        reads it as ``termination_not_confirmed``) and closes normally. On success
        the connection is retained on ``self._halt_conn`` and closed only at
        wrapper shutdown, so the client observes EOF as barrier 2.
        """
        confirmed, returncode, error = self._perform_halt()
        if not confirmed:
            protocol._send_json(
                conn,
                {"ok": False, "state": "halt_not_confirmed", "returncode": returncode, "error": error},
            )
            return False
        self._halted = True
        self._halt_conn = conn
        protocol._send_json(conn, {"ok": True, "state": "halted", "returncode": returncode})
        return True

    def _perform_halt(self) -> tuple[bool, int | None, str | None]:
        """Drive the ordered HALT barriers. Returns ``(confirmed, rc, error)``.

        No SQL transaction spans any external wait: termination/group-drain
        happen first, then a short exit-persist transaction, then filesystem
        cleanup -- each a distinct step, none holding a DB connection open across
        a wait.
        """
        child = self.child
        assert child is not None
        # A start_new_session child is its own process-group leader, so the group
        # id equals its PID by construction and stays valid even if the leader
        # exits/gets reaped before os.getpgid() could observe it. Derive the group
        # id from the PID so the leader-exit/surviving-descendant race can never
        # null the target out and SKIP the mandatory group-level kernel predicate.
        pgid = child.pid
        returncode = process.terminate_process_tree(child, self.payload.kill_after_seconds)
        # Mandatory group proof, never skipped: killpg(pgid, 0) addresses the whole
        # native group and only an empty group raises ProcessLookupError, so a
        # surviving descendant (even after the leader was reaped) keeps this False
        # and refuses the HALT BEFORE any worker_exit persistence.
        if not process.process_group_empty(pgid, timeout=max(self.payload.kill_after_seconds, 0.0)):
            return False, returncode, "native process group not empty; termination not confirmed"
        if not self._record_exit(returncode, source="halt"):
            return False, returncode, "worker_exit persistence failed; termination not confirmed"
        if not self._halt_cleanup():
            return False, returncode, "run-directory/ZDOTDIR cleanup failed; termination not confirmed"
        return True, returncode, None

    def _halt_cleanup(self) -> bool:
        """Remove and CONFIRM the disappearance of every owned filesystem artifact
        before a confirmed HALT response.

        Refuses (returns ``False``) on ANY residue: the run directory, its control
        socket (nested in the run directory, verified as its own residue so a
        partial run-dir removal that leaves the bound socket cannot confirm), or
        the OWNED ZDOTDIR PARENT -- the ``agent-comms-zdotdir-*`` directory
        ``_cleanup_zdotdir`` actually removes, not merely the nested child ZDOTDIR
        path. A removal denied by the OS leaves residue and must never confirm
        while a supervisor artifact leaks.
        """
        ok = True
        if self.run_dir is not None:
            paths.cleanup_run_dir(self.run_dir)
            if paths._path_present(self.run_dir):
                ok = False
        if self.socket_path is not None and paths._path_present(self.socket_path):
            ok = False
        if self.payload.zdotdir:
            parent = zdotdir._cleanup_zdotdir(Path(self.payload.zdotdir))
            # Confirm the OWNED artifact (_cleanup_zdotdir's target) is gone, not
            # the nested child path. When the parent is not an owned zdotdir
            # wrapper nothing was removed, so fall back to the child path.
            artifact = parent if parent is not None else Path(self.payload.zdotdir)
            if paths._path_present(artifact):
                ok = False
        return ok

    def _record_exit(self, returncode: int | None, *, source: str) -> bool:
        """Persist same-run ``worker_exit`` exactly once. Returns whether the
        authoritative row is persisted (from this call or an earlier one).

        The HALT barrier requires this to be TRUE before a confirmed response:
        exit persistence failing (no matching same-run row, or a SQL error) must
        not be masked by the fact that the process died. Non-HALT callers
        (child/timeout/signal/cleanup) ignore the result; the parent reaper is
        their idempotent fallback, so a wrapper that cannot write never crashes
        before cleanup.
        """
        if self._exit_recorded:
            return self._exit_persisted
        self._exit_recorded = True
        try:
            self._exit_persisted = evidence.record_worker_exit(
                self.payload.db_path,
                self.payload.dispatch_id,
                self.payload.run_token,
                returncode=returncode,
                source=source,
            )
        except sqlite3.Error:
            self._exit_persisted = False
        return self._exit_persisted

    # -- termination / signals --------------------------------------------- #

    def _terminate_child(self, *, source: str) -> None:
        """Terminate and wait the owned child tree if still alive; record once.

        Idempotent: a child already reaped by ``serve()`` / HALT / TTL is a
        no-op. This is the wrapper terminating its OWN ``start_new_session``
        child, never adapter handle/PID signalling, so a wrapper that dies for
        any reason never leaves a supervisor-less native child alive.
        """
        child = self.child
        if child is None:
            return
        # The leader exiting is NOT proof the native group drained: a
        # SIGTERM-ignoring descendant can outlive the reaped leader. Always drive
        # group termination so teardown never SKIPS it because ``child.poll()`` is
        # already non-None and leaves that descendant orphaned; preserve the
        # leader's own exit code when ``poll()`` already observed it.
        observed = child.poll()
        tree_status = process.terminate_process_tree(child, self.payload.kill_after_seconds)
        returncode = observed if observed is not None else tree_status
        self._record_exit(returncode, source=source)

    def _on_terminating_signal(self, signum: int, frame: Any) -> None:
        # Robust parent-loss / adapter-timeout guard: an external SIGTERM (for
        # example the adapter abandoning a pre-READY spawn) must never kill only
        # the wrapper and leave its start_new_session child alive. Terminate and
        # wait the owned child tree, record truthful exit evidence, then exit so
        # the finally cleanup runs.
        self._terminate_child(source="signal")
        raise SystemExit(143)

    def install_signal_handlers(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}
        for signum in (signal.SIGTERM, signal.SIGHUP):
            try:
                previous[signum] = signal.signal(signum, self._on_terminating_signal)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass
        return previous

    def restore_signal_handlers(self, previous: dict[int, Any]) -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass

    # -- teardown ---------------------------------------------------------- #

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        # Orphan guard first: never leave a live start_new_session child behind
        # when the wrapper tears down for ANY reason (pre-READY failure,
        # READY-send failure, parent loss, or normal serve() exit). Idempotent
        # with serve()/HALT/TTL, which have already reaped the child.
        self._terminate_child(source="cleanup")
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
        if self.run_dir is not None:
            paths.cleanup_run_dir(self.run_dir)
        if self.payload.zdotdir:
            zdotdir._cleanup_zdotdir(Path(self.payload.zdotdir))

    def close_halt_connection(self) -> None:
        """Close a retained confirmed-HALT connection, delivering EOF (barrier 2).

        Called last in ``run_supervisor`` teardown -- after termination, exit
        persistence, and cleanup have all completed -- so the client sees EOF
        only when the wrapper is genuinely shutting down. A no-op when no HALT was
        confirmed.
        """
        conn = self._halt_conn
        if conn is None:
            return
        self._halt_conn = None
        try:
            conn.close()
        except OSError:
            pass


def run_supervisor(bootstrap_fd: int, child_command: Sequence[str]) -> int:
    """Wrapper entry point: bootstrap, supervise the child, clean up.

    Any failure before READY is delivered -- adapter timeout, READY-send
    failure, bootstrap parent loss, or a prepare/launch/merge error -- is a
    spawn failure: the finally cleanup terminates and waits any launched child
    so no supervisor-less native child is orphaned, cleans the attempted run
    directory, and READY is never sent, so the adapter records
    ``spawn_failed_message_landed`` rather than a false live row. Observed
    identity written by ``merge_observed`` is only ever added to (exit evidence),
    never overwritten, on such a failure.
    """
    boot = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=bootstrap_fd)
    supervisor: _Supervisor | None = None
    previous_handlers: dict[int, Any] | None = None
    try:
        raw = protocol._read_line(boot, timeout=30.0)
        if raw is None:
            raise paths.SupervisorError("no bootstrap payload received")
        payload = BootstrapPayload.from_json(raw)
        supervisor = _Supervisor(payload, child_command)
        # Install the parent-loss / adapter-timeout signal guard before any
        # child exists so a SIGTERM at any point tears the owned child down.
        previous_handlers = supervisor.install_signal_handlers()
        supervisor.prepare()
        supervisor.launch_child()
        supervisor.merge_observed()
        # READY delivery is the last pre-serve step. If it raises (the adapter
        # peer is already gone / the pipe is broken), the finally cleanup below
        # terminates the just-launched child, so a failed READY never leaves a
        # supervisor-less child alive.
        protocol._send_json(boot, supervisor.ready_ack())
        boot.close()
        boot = None  # type: ignore[assignment]
        return supervisor.serve()
    finally:
        if boot is not None:
            try:
                boot.close()
            except OSError:
                pass
        if supervisor is not None:
            supervisor.cleanup()
            if previous_handlers is not None:
                supervisor.restore_signal_handlers(previous_handlers)
            # Deliver EOF on a confirmed-HALT connection LAST, so the client's
            # barrier 2 fires only once the wrapper has finished tearing down and
            # is about to exit (barrier 3, the parent-owned wrapper reap).
            supervisor.close_halt_connection()
