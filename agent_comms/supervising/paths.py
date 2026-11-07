"""Control-root paths, run identity and the owner marker.

Constants, the two supervisor exceptions, the protected control root, strict run
directories, ``sun_path`` sizing and the versioned cleanup-ownership marker.
A leaf: nothing here imports the rest of the package except the runtime root.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path

from ..clock import utc_now


# Bumped together with the machine contract. STATUS/HALT peers refuse a protocol
# they do not recognise rather than guess.
PROTOCOL_VERSION = 1
# Versioned cleanup-ownership marker written into each run directory. The
# spawn-time janitor removes a run directory only when this marker parses and
# names a SQL-terminal dispatch/run token; a bumped version that this code does
# not understand is preserved loudly.
MARKER_VERSION = 1

# macOS caps AF_UNIX ``sun_path`` at 104 bytes including the terminating NUL, so
# the usable encoded path length is 103 bytes. We assert against this ceiling on
# every platform so a socket path that would bind on Linux (108) but not on
# macOS fails loudly and identically everywhere, before READY.
SUN_PATH_MAX_BYTES = 103

# Test-only override for the protected control root. Production always uses the
# default under the runtime root so the janitor and negative-cell sandbox
# boundary have a single known location per install.
CONTROL_ROOT_ENV = "AGENT_COMMS_SUPERVISOR_ROOT"

# A run directory is named by its 32-hex-char run token; the control socket is a
# single short "s" inside it. Both are strict so the janitor never has to reason
# about arbitrary names.
_STRICT_RUN_DIR_RE = re.compile(r"^[0-9a-f]{32}$")
_MARKER_NAME = "owner.json"
_SOCKET_NAME = "s"
# What a run directory and its socket add to the control root: "/<32 hex>/s".
_RUN_SOCKET_SUFFIX_BYTES = 1 + 32 + 1 + len(_SOCKET_NAME)
# ... and to the runtime root, through "/run/s".
_CONTROL_SOCKET_SUFFIX_BYTES = len("/run/s") + _RUN_SOCKET_SUFFIX_BYTES

# Same bounded busy-timeout discipline as ``Database`` (10s connect timeout,
# WAL already enabled on the ledger). Wrapper transactions stay short and never
# hold the DB open across socket I/O, wait, signal escalation, or cleanup.
_SQLITE_TIMEOUT_SECONDS = 10.0
_SQLITE_BUSY_TIMEOUT_MS = 5000

READY_TOKEN = "ready"
_MAX_CONTROL_LINE_BYTES = 65536


class SupervisorError(RuntimeError):
    """A supervisor invariant could not be met."""


class SunPathTooLong(SupervisorError):
    """The bound control-socket path would exceed the AF_UNIX byte limit."""


def default_control_root() -> Path:
    """The protected control root of this install: ``<runtime root>/run/s``."""
    from ..paths import runtime_root

    return runtime_root() / "run" / "s"


def control_root() -> Path:
    override = os.environ.get(CONTROL_ROOT_ENV)
    if override:
        return Path(override)
    return default_control_root()


def control_root_problem(root: Path | None = None) -> str | None:
    """Why control sockets under ``root`` cannot bind, with the fix, or None.

    Checked before anything is spawned (setup, doctor, ``spawn_supervised``)
    so an over-long root is a refusal with the byte count, not a READY
    timeout from the wrapper.
    """
    root = root if root is not None else control_root()
    length = encoded_path_len(root) + _RUN_SOCKET_SUFFIX_BYTES
    if length <= SUN_PATH_MAX_BYTES:
        return None
    if os.environ.get(CONTROL_ROOT_ENV):
        fix = f"a shorter {CONTROL_ROOT_ENV}"
    else:
        fix = f"a shorter AGENT_COMMS_HOME (at most {SUN_PATH_MAX_BYTES - _CONTROL_SOCKET_SUFFIX_BYTES} bytes)"
    return (
        f"control sockets under {root} would be {length} bytes, over the AF_UNIX limit of "
        f"{SUN_PATH_MAX_BYTES}; recover with: {fix}"
    )


def new_run_token() -> str:
    """A cryptographically random 32-hex-char run token / run-dir name."""
    return secrets.token_hex(16)


def is_strict_run_dir_name(name: str) -> bool:
    return _STRICT_RUN_DIR_RE.fullmatch(name) is not None


def run_dir_for(root: Path, run_token: str) -> Path:
    if not is_strict_run_dir_name(run_token):
        raise ValueError(f"run token is not a strict run-dir name: {run_token!r}")
    return root / run_token


def control_socket_for(root: Path, run_token: str) -> Path:
    return run_dir_for(root, run_token) / _SOCKET_NAME


def encoded_path_len(path: Path | str) -> int:
    return len(os.fsencode(str(path)))


def assert_sun_path_ok(path: Path | str) -> None:
    length = encoded_path_len(path)
    if length > SUN_PATH_MAX_BYTES:
        raise SunPathTooLong(
            f"control socket path is {length} bytes, exceeds AF_UNIX limit of "
            f"{SUN_PATH_MAX_BYTES}: {str(path)!r}; "
            "recover with: shorten the supervisor control root"
        )


def create_run_dir(root: Path, run_token: str) -> Path:
    """Create the strict, ``0700`` run directory under the protected root."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Never widen an existing root, but re-assert 0700 on the directory we own.
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    run_dir = run_dir_for(root, run_token)
    run_dir.mkdir(mode=0o700)
    os.chmod(run_dir, 0o700)
    return run_dir


def write_owner_marker(run_dir: Path, *, dispatch_id: str, run_token: str, wrapper_pid: int) -> Path:
    marker = {
        "marker_version": MARKER_VERSION,
        "dispatch_id": dispatch_id,
        "run_token": run_token,
        "wrapper_pid": wrapper_pid,
        "created_at": utc_now(),
    }
    path = run_dir / _MARKER_NAME
    path.write_text(json.dumps(marker, sort_keys=True))
    os.chmod(path, 0o600)
    return path


def read_owner_marker(run_dir: Path) -> dict | None:
    path = run_dir / _MARKER_NAME
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def cleanup_run_dir(run_dir: Path) -> None:
    """Remove a strict run directory without ever following a symlink."""
    if run_dir.is_symlink():
        return
    if not is_strict_run_dir_name(run_dir.name):
        return
    shutil.rmtree(run_dir, ignore_errors=True)


def _path_present(path: Path) -> bool:
    """Whether a filesystem path still exists (a leftover symlink counts too).

    Used by HALT cleanup confirmation: a path we cannot even stat (an lstat
    ``OSError``) is treated as PRESENT so confirmation fails closed on residue it
    cannot rule out, never confirming blind while an artifact may leak.
    """
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return True
