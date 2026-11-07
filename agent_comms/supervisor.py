"""Per-dispatch process supervisor for the dead-worker terminalization stage.

This module promotes the old best-effort ``timeout_wrapper.py`` into an
authenticated per-dispatch supervisor. Each dispatch gets one supervisor
process (the "wrapper") that:

- receives its run token and identity over an inherited ``socket.socketpair``
  bootstrap, never over argv or the native runtime child's environment, so the
  token cannot leak to the runtime child;
- owns a bounded process group containing exactly the native runtime child;
- binds an authenticated Unix-domain control socket under a fixed, protected,
  ``0700`` control root (``<runtime root>/run/s``) with a strict, short name so
  the encoded ``sun_path`` stays within the macOS 103-byte limit;
- serves read-only ``STATUS`` and authenticated ``HALT`` requests that carry the
  exact same run token;
- enforces the hard TTL with ``SIGTERM`` -> grace -> ``SIGKILL`` -> ``wait``;
- writes truthful, same-run SQL exit evidence into the dispatch's observed
  values using the same WAL / bounded busy-timeout discipline as ``Database``;
- and cleans up its socket, strict run directory, and ZDOTDIR on the way out.

The run token persisted into observed values is NOT claimed to be a same-uid
secret. It is a same-run correlation/authentication nonce. The capability
boundary is the OS sandbox denial of the protected control root: a bounded
runtime worker cannot create/connect/write there, so it cannot address any run
other than (at most) its own supervisor, and even then can do no more than exit
its own dispatch.

This module is a facade: the implementation lives in ``agent_comms.supervising``
(``paths``, ``process``, ``zdotdir``, ``evidence``, ``reaper``, ``protocol``,
``server``, ``spawn``, ``janitor``) and every name is re-exported here for the
wrapper entry point and for readers. Patch the owning module, not this one:
callers inside the package look names up on the owning module.
"""

from __future__ import annotations

import os  # noqa: F401 - tests patch supervisor.os.* (the stdlib module itself)
import socket  # noqa: F401 - tests patch supervisor.socket.* (the stdlib module itself)

from .terminal_statuses import DISPATCH_TERMINAL_STATUSES
from .supervising.paths import (
    CONTROL_ROOT_ENV,
    MARKER_VERSION,
    PROTOCOL_VERSION,
    READY_TOKEN,
    SUN_PATH_MAX_BYTES,
    SunPathTooLong,
    SupervisorError,
    _CONTROL_SOCKET_SUFFIX_BYTES,
    _MARKER_NAME,
    _MAX_CONTROL_LINE_BYTES,
    _RUN_SOCKET_SUFFIX_BYTES,
    _SOCKET_NAME,
    _SQLITE_BUSY_TIMEOUT_MS,
    _SQLITE_TIMEOUT_SECONDS,
    _STRICT_RUN_DIR_RE,
    _path_present,
    assert_sun_path_ok,
    cleanup_run_dir,
    control_root,
    control_root_problem,
    control_socket_for,
    create_run_dir,
    default_control_root,
    encoded_path_len,
    is_strict_run_dir_name,
    new_run_token,
    read_owner_marker,
    run_dir_for,
    write_owner_marker,
)
from .supervising.process import (
    _drain_group,
    _process_group_gone,
    process_group_empty,
    terminate_process_tree,
)
from .supervising.zdotdir import (
    _cleanup_zdotdir,
    _owned_zdotdir_parent,
)
from .supervising.evidence import (
    REAP_EVIDENCE_SOURCES,
    REAP_PROOF_VERSION,
    REAP_SOURCE_BACKGROUND_REAP,
    REAP_SOURCE_HALT_FINALIZE,
    _REAP_COMPLETE_TOP_KEYS,
    _REAP_OWNED_ARTIFACT_KEYS,
    _connect,
    _owned_artifacts_all_absent,
    complete_reaper_proof,
    confirmed_termination_evidence,
    dispatch_cleanup_state,
    dispatch_terminal_state,
    merge_supervisor_observed,
    record_reaper_exit,
    record_registered_wrapper_reap,
    record_worker_exit,
)
from .supervising.reaper import (
    ReaperRegistry,
    _REAP_GROUP_VERIFY_TIMEOUT_SECONDS,
    _REGISTRY,
    _REGISTRY_LOCK,
    _ReaperEntry,
    reaper_registry,
)
from .supervising.protocol import (
    ControlResult,
    _client_request,
    _halt_over_connection,
    _read_line,
    _send_json,
    _wait_for_eof,
    probe_status,
    request_halt,
)
from .supervising.server import (
    BootstrapPayload,
    _Supervisor,
    run_supervisor,
)
from .supervising.spawn import (
    SupervisedSpawn,
    _terminate_wrapper,
    spawn_supervised,
    wrapper_script_path,
)
from .supervising.janitor import (
    janitor_sweep,
)

__all__ = [
    "BootstrapPayload",
    "CONTROL_ROOT_ENV",
    "ControlResult",
    "DISPATCH_TERMINAL_STATUSES",
    "MARKER_VERSION",
    "PROTOCOL_VERSION",
    "READY_TOKEN",
    "REAP_EVIDENCE_SOURCES",
    "REAP_PROOF_VERSION",
    "REAP_SOURCE_BACKGROUND_REAP",
    "REAP_SOURCE_HALT_FINALIZE",
    "ReaperRegistry",
    "SUN_PATH_MAX_BYTES",
    "SunPathTooLong",
    "SupervisedSpawn",
    "SupervisorError",
    "_CONTROL_SOCKET_SUFFIX_BYTES",
    "_MARKER_NAME",
    "_MAX_CONTROL_LINE_BYTES",
    "_REAP_COMPLETE_TOP_KEYS",
    "_REAP_GROUP_VERIFY_TIMEOUT_SECONDS",
    "_REAP_OWNED_ARTIFACT_KEYS",
    "_REGISTRY",
    "_REGISTRY_LOCK",
    "_RUN_SOCKET_SUFFIX_BYTES",
    "_ReaperEntry",
    "_SOCKET_NAME",
    "_SQLITE_BUSY_TIMEOUT_MS",
    "_SQLITE_TIMEOUT_SECONDS",
    "_STRICT_RUN_DIR_RE",
    "_Supervisor",
    "_cleanup_zdotdir",
    "_client_request",
    "_connect",
    "_drain_group",
    "_halt_over_connection",
    "_owned_artifacts_all_absent",
    "_owned_zdotdir_parent",
    "_path_present",
    "_process_group_gone",
    "_read_line",
    "_send_json",
    "_terminate_wrapper",
    "_wait_for_eof",
    "assert_sun_path_ok",
    "cleanup_run_dir",
    "complete_reaper_proof",
    "confirmed_termination_evidence",
    "control_root",
    "control_root_problem",
    "control_socket_for",
    "create_run_dir",
    "default_control_root",
    "dispatch_cleanup_state",
    "dispatch_terminal_state",
    "encoded_path_len",
    "is_strict_run_dir_name",
    "janitor_sweep",
    "merge_supervisor_observed",
    "new_run_token",
    "probe_status",
    "process_group_empty",
    "read_owner_marker",
    "reaper_registry",
    "record_reaper_exit",
    "record_registered_wrapper_reap",
    "record_worker_exit",
    "request_halt",
    "run_dir_for",
    "run_supervisor",
    "spawn_supervised",
    "terminate_process_tree",
    "wrapper_script_path",
    "write_owner_marker",
]
