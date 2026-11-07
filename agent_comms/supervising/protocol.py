"""Control protocol over the authenticated Unix-domain socket (framed
newline-delimited JSON): STATUS probe and authenticated HALT, client side.
"""
from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import dataclass
from typing import Any

from . import paths, reaper


def _read_line(conn: socket.socket, *, timeout: float) -> bytes | None:
    conn.settimeout(timeout)
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = conn.recv(4096)
        except (socket.timeout, TimeoutError):
            return None
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b"\n" in chunk or total >= paths._MAX_CONTROL_LINE_BYTES:
            break
    data = b"".join(chunks)
    if not data:
        return None
    line, _, _ = data.partition(b"\n")
    return line


def _send_json(conn: socket.socket, payload: dict[str, Any]) -> None:
    conn.sendall((json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))


@dataclass(frozen=True)
class ControlResult:
    """Outcome of a STATUS/HALT client request.

    ``ok`` is only true when the peer authenticated us and answered. Any
    connect/protocol/token failure yields ``ok=False`` with a diagnostic and is
    never interpreted as process death by the caller.
    """

    ok: bool
    state: str | None = None
    returncode: int | None = None
    error: str | None = None


def _client_request(
    control_socket: str,
    run_token: str,
    op: str,
    *,
    connect_timeout: float,
    io_timeout: float,
) -> ControlResult:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:  # pragma: no cover - AF_UNIX always present on posix
        return ControlResult(ok=False, error=f"socket: {exc}")
    try:
        client.settimeout(connect_timeout)
        client.connect(control_socket)
        client.settimeout(io_timeout)
        _send_json(client, {"protocol_version": paths.PROTOCOL_VERSION, "run_token": run_token, "op": op})
        line = _read_line(client, timeout=io_timeout)
        if line is None:
            return ControlResult(ok=False, error="no response")
        resp = json.loads(line.decode("utf-8"))
        if not isinstance(resp, dict):
            return ControlResult(ok=False, error="malformed response")
        return ControlResult(
            ok=bool(resp.get("ok")),
            state=resp.get("state"),
            returncode=resp.get("returncode"),
            error=resp.get("error"),
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return ControlResult(ok=False, error=str(exc))
    finally:
        client.close()


def probe_status(
    control_socket: str,
    run_token: str,
    *,
    connect_timeout: float = 2.0,
    io_timeout: float = 2.0,
) -> ControlResult:
    """Read-only STATUS. Never signals."""
    return _client_request(
        control_socket, run_token, "STATUS", connect_timeout=connect_timeout, io_timeout=io_timeout
    )


def _wait_for_eof(conn: socket.socket, *, timeout: float) -> bool:
    """Read the connection until the peer closes it (EOF). Barrier 2.

    Returns ``True`` on a clean EOF within ``timeout``; ``False`` if the peer
    keeps the connection open past the deadline (a halted response WITHOUT
    wrapper shutdown, which must not confirm). Any bytes after the response line
    are drained and ignored -- only the close matters.
    """
    conn.settimeout(timeout)
    while True:
        try:
            chunk = conn.recv(4096)
        except (socket.timeout, TimeoutError):
            return False
        except OSError:
            return False
        if not chunk:
            return True


def _halt_over_connection(
    conn: socket.socket,
    run_token: str,
    wrapper_popen: subprocess.Popen | None,
    *,
    io_timeout: float,
    wrapper_wait_timeout: float,
) -> ControlResult:
    """Drive the three client-side HALT barriers over an open control socket.

    Confirmation (``ok=True``) requires ALL of, in order:
      1. a valid authenticated ``halted`` response;
      2. EOF on this same connection (the wrapper closes it only at shutdown);
      3. the exact registered wrapper ``Popen`` exiting/reaping (parent-owned).
    A missing/failed response, response-without-EOF, EOF-without-valid-response,
    absent wrapper handle, or wrapper-wait timeout each stays unconfirmed.
    """
    try:
        _send_json(conn, {"protocol_version": paths.PROTOCOL_VERSION, "run_token": run_token, "op": "HALT"})
        line = _read_line(conn, timeout=io_timeout)
        if line is None:
            return ControlResult(ok=False, error="no response")
        resp = json.loads(line.decode("utf-8"))
        if not isinstance(resp, dict):
            return ControlResult(ok=False, error="malformed response")
        if not resp.get("ok") or resp.get("state") != "halted":
            return ControlResult(
                ok=False,
                state=resp.get("state"),
                returncode=resp.get("returncode"),
                error=resp.get("error") or "halt not confirmed",
            )
        returncode = resp.get("returncode")
        if not _wait_for_eof(conn, timeout=io_timeout):
            return ControlResult(
                ok=False,
                state="halted",
                returncode=returncode,
                error="halted response without wrapper EOF; termination not confirmed",
            )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return ControlResult(ok=False, error=str(exc))
    # Barrier 3: positive parent-owned evidence the exact wrapper exited/reaped.
    if wrapper_popen is None:
        return ControlResult(
            ok=False,
            state="halted",
            returncode=returncode,
            error="no registered wrapper process to reap; termination not confirmed",
        )
    try:
        wrapper_popen.wait(timeout=wrapper_wait_timeout)
    except subprocess.TimeoutExpired:
        return ControlResult(
            ok=False,
            state="halted",
            returncode=returncode,
            error="wrapper did not exit before deadline; termination not confirmed",
        )
    return ControlResult(ok=True, state="halted", returncode=returncode)


def request_halt(
    control_socket: str,
    run_token: str,
    *,
    connect_timeout: float = 2.0,
    io_timeout: float = 30.0,
    registry: "reaper.ReaperRegistry | None" = None,
    wrapper_popen: subprocess.Popen | None = None,
    wrapper_wait_timeout: float | None = None,
) -> ControlResult:
    """Authenticated HALT with a wrapper-exit barrier.

    The wrapper terminates/waits its own child, proves the group empty, persists
    exact-token exit evidence, and cleans up BEFORE its confirmed response; this
    client only reports ``ok=True`` once it has also observed EOF on the same
    connection AND reaped the EXACT registered wrapper ``Popen``. The wrapper
    handle is found by run token in the process-global reaper registry the
    spawning adapter already populates (or supplied directly for tests), so the
    adapter call site needs no change. After the barriers pass, the finalize of
    the claimed entry durably persists the exact registered-wrapper reap
    (``$.reaper_exit``, ``source="halt_finalize"``) BEFORE ``ok=True`` is
    returned, so the terminal cancellation CAS stays downstream of every HALT
    barrier and its durable evidence. Any barrier miss -- unreachable socket,
    server-side refusal, response without EOF, wrapper-wait timeout, or reap
    evidence persistence failure -- is a truthful ``termination_not_confirmed``,
    never death inferred from a socket round-trip alone.
    """
    reg = None
    claimed_entry = None
    if wrapper_popen is None:
        reg = registry if registry is not None else reaper.reaper_registry()
        # Atomically claim sole ownership of the exact registered wrapper so the
        # background reaper does not also process it while this HALT runs. A miss
        # (no entry) leaves ``wrapper_popen`` None -> barrier 3 refuses truthfully.
        claimed_entry = reg.claim_by_run_token(run_token)
        if claimed_entry is not None:
            wrapper_popen = claimed_entry.popen
    if wrapper_wait_timeout is None:
        wrapper_wait_timeout = io_timeout

    disposed = False

    def _dispose(result: ControlResult) -> ControlResult:
        # A confirmed HALT owns the exact-wrapper reap; finalize must durably
        # persist that reap ($.reaper_exit, source="halt_finalize") BEFORE the
        # claimed entry may be dropped, and ok=True is returned only downstream
        # of that persistence. A persistence failure (or an unconfirmed HALT)
        # releases the claim so the exact registered wrapper stays retryable by
        # a later HALT or the background reaper -- never stranded, and the only
        # exact Popen association is never discarded before durable evidence
        # exists.
        nonlocal disposed
        disposed = True
        if reg is None or claimed_entry is None:
            return result
        if not result.ok:
            reg.release_claim(claimed_entry)
            return result
        if reg.finalize_claimed(claimed_entry):
            return result
        return ControlResult(
            ok=False,
            state=result.state,
            returncode=result.returncode,
            error="registered-wrapper reap evidence persistence failed; termination not confirmed",
        )

    try:
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as exc:  # pragma: no cover - AF_UNIX always present on posix
            return _dispose(ControlResult(ok=False, error=f"socket: {exc}"))
        try:
            client.settimeout(connect_timeout)
            client.connect(control_socket)
            client.settimeout(io_timeout)
            result = _halt_over_connection(
                client,
                run_token,
                wrapper_popen,
                io_timeout=io_timeout,
                wrapper_wait_timeout=wrapper_wait_timeout,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            result = ControlResult(ok=False, error=str(exc))
        finally:
            client.close()
        return _dispose(result)
    finally:
        # Never strand the claim: an exception OUTSIDE the caught set (an
        # unexpected wrapper-wait failure) still releases ownership so the
        # idempotent fallback reaper can reap the exact wrapper. The exception
        # continues to propagate; on every normal path ``_dispose`` already ran.
        if not disposed and reg is not None and claimed_entry is not None:
            reg.release_claim(claimed_entry)
