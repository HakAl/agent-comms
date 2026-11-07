"""Adapter-side bootstrap (socketpair handshake) and supervised spawn of the
wrapper process.
"""
from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import paths, protocol


@dataclass(frozen=True)
class SupervisedSpawn:
    popen: subprocess.Popen
    run_token: str
    control_socket: str
    child_pid: int
    wrapper_pid: int
    run_dir: str


def wrapper_script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "timeout_wrapper.py"


def spawn_supervised(
    child_command: Sequence[str],
    *,
    dispatch_id: str,
    db_path: str,
    ttl_seconds: float,
    kill_after_seconds: float,
    run_token: str | None = None,
    root: Path | None = None,
    zdotdir: str | None = None,
    expected_close_by: str | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    stdout: Any = None,
    stderr: Any = None,
    python_executable: str | None = None,
    ready_timeout: float = 30.0,
) -> SupervisedSpawn:
    """Launch the supervisor wrapper and block until READY.

    The run token travels only over the inherited ``socketpair`` bootstrap; it
    is never placed on argv or in the child's environment. The adapter keeps one
    peer and lists only the wrapper peer in ``pass_fds``. On any failure before
    READY the wrapper is terminated and a ``SupervisorError`` is raised.
    """
    import sys

    root = root if root is not None else paths.control_root()
    run_token = run_token or paths.new_run_token()
    python_executable = python_executable or sys.executable
    # The wrapper asserts this too, but its refusal reaches the parent only
    # as a missing READY; check here so the caller sees the byte count.
    problem = paths.control_root_problem(root)
    if problem:
        raise paths.SunPathTooLong(problem)

    adapter_peer, wrapper_peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    wrapper_argv = [
        python_executable,
        str(wrapper_script_path()),
        "--supervise",
        "--bootstrap-fd",
        str(wrapper_peer.fileno()),
        "--",
        *[str(part) for part in child_command],
    ]
    popen: subprocess.Popen | None = None
    try:
        popen = subprocess.Popen(
            wrapper_argv,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            pass_fds=(wrapper_peer.fileno(),),
            close_fds=True,
        )
        # The wrapper owns its peer now; the adapter retains only adapter_peer.
        wrapper_peer.close()
        payload = {
            "protocol_version": paths.PROTOCOL_VERSION,
            "dispatch_id": dispatch_id,
            "run_token": run_token,
            "db_path": str(db_path),
            "control_root": str(root),
            "ttl_seconds": float(ttl_seconds),
            "kill_after_seconds": float(kill_after_seconds),
            "zdotdir": zdotdir,
            "expected_close_by": expected_close_by,
        }
        # The adapter-side bootstrap send/read/parse can fail if the wrapper dies
        # early: a broken pipe / reset on send or read, a truncated or non-JSON
        # READY line, or an ack missing its required fields. Every such
        # OSError / EOF / decode failure is normalised to a typed pre-READY
        # SupervisorError (cause preserved via ``from``) so the adapter records
        # ``spawn_failed_message_landed`` instead of leaking a raw OSError; the
        # outer ``except BaseException`` still runs the safe wrapper teardown
        # (``_terminate_wrapper``) so no supervisor-less child is orphaned.
        try:
            protocol._send_json(adapter_peer, payload)
        except OSError as exc:
            raise paths.SupervisorError(
                f"supervisor bootstrap send failed before READY: {exc}"
            ) from exc
        try:
            line = protocol._read_line(adapter_peer, timeout=ready_timeout)
        except OSError as exc:
            raise paths.SupervisorError(
                f"supervisor bootstrap read failed before READY: {exc}"
            ) from exc
        if line is None:
            raise paths.SupervisorError("supervisor did not send READY before deadline")
        try:
            ack = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise paths.SupervisorError(
                f"supervisor READY handshake was not valid JSON: {exc}"
            ) from exc
        if not isinstance(ack, dict) or not ack.get("ready"):
            raise paths.SupervisorError(f"supervisor READY handshake failed: {ack!r}")
        try:
            return SupervisedSpawn(
                popen=popen,
                run_token=run_token,
                control_socket=str(ack.get("control_socket") or paths.control_socket_for(root, run_token)),
                child_pid=int(ack["child_pid"]),
                wrapper_pid=int(ack.get("wrapper_pid") or popen.pid),
                run_dir=str(paths.run_dir_for(root, run_token)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise paths.SupervisorError(
                f"supervisor READY handshake missing/invalid fields ({ack!r}): {exc}"
            ) from exc
    except BaseException:
        if popen is not None:
            _terminate_wrapper(popen)
        try:
            wrapper_peer.close()
        except OSError:
            pass
        raise
    finally:
        adapter_peer.close()


def _terminate_wrapper(popen: subprocess.Popen, grace_seconds: float = 5.0) -> None:
    """Tear a wrapper down (on a pre-READY failure) without orphaning its child.

    Sends the wrapper an ownership ``SIGTERM`` (``Popen.terminate``), never
    ``killpg`` of a handle-parsed PID and never a ``SIGKILL`` of the wrapper's
    session -- that would kill only the wrapper and orphan its separate
    ``start_new_session`` child. The wrapper's own signal handler terminates and
    waits its owned child tree before exiting. If the wrapper does not exit
    within the grace it is PRESERVED rather than force-killed into an orphan; its
    own hard TTL bounds the lifecycle and stage-2/manual owns dead-supervisor
    recovery.
    """
    if popen.poll() is not None:
        return
    try:
        popen.terminate()
    except OSError:
        pass
    try:
        popen.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:  # pragma: no cover - pathological wrapper
        pass
