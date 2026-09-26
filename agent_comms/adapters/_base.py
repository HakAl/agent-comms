from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import DispatchContext, DispatchStart
from .. import paths, supervisor
from ..policies import compile_policy, scoped_env
from ..runtime_pins import claude_binary_path
from ..spawn import DEFAULT_WORKER_PROMPT, claude_settings_for_policy

SPAWN_GRACE_SECONDS = 2.0
# SIGTERM -> grace -> SIGKILL window the per-dispatch supervisor enforces on the
# native runtime child at the hard TTL.
KILL_AFTER_SECONDS = 30.0
# The authenticated HALT client must wait long enough for the wrapper's full
# TERM -> grace -> KILL -> wait escalation (up to ~2 * KILL_AFTER_SECONDS) plus
# its acknowledgement, so an ignore-TERM child returns a real HALT ack instead
# of a deterministic client timeout that would be misread as unconfirmed.
HALT_IO_TIMEOUT_SECONDS = 2 * KILL_AFTER_SECONDS + 10.0
# The exact phrase every unconfirmed release carries: the ledger is released but
# native-child death is NOT claimed. Kept identical to the ledger's phrase.
TERMINATION_NOT_CONFIRMED_PHRASE = "ledger released; termination not confirmed"
ALLOWED_BASE_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TZ",
    }
)


class SpawnFailed(RuntimeError):
    """Worker process failed during spawn startup."""


class SupervisorUnreachable(RuntimeError):
    """The per-dispatch supervisor could not be authenticated/reached.

    Never inferred as process death: the caller (hard-TTL reconciliation) may
    still DLQ/release at the deadline, but must report ``termination not
    confirmed`` rather than claiming native-child death.
    """


# Typed adapter status per Spec C. ``status`` never signals; it reports what the
# same-run SQL exit report and authenticated STATUS socket say, and defaults to
# ``supervisor_unreachable`` (never "dead") when it cannot prove liveness.
ADAPTER_STATUS_STATES = frozenset({"exited", "running", "supervisor_unreachable"})


@dataclass(frozen=True)
class AdapterStatus:
    state: str
    returncode: int | None = None
    detail: str | None = None


def _same_run_exit_evidence(observed: dict) -> dict | None:
    """Return same-run child exit evidence from observed values, or ``None``.

    An exit report (``worker_exit`` or ``reaper_exit``) counts as termination
    evidence ONLY when it is a dict whose nonempty ``run_token`` exactly equals
    the current observed ``run_token``. Stale (an older run's token), missing, or
    malformed evidence returns ``None`` so ``status`` reports
    ``supervisor_unreachable`` and authenticated ``halt`` stays unconfirmed:
    object existence alone is never enough, so a retry / new run of the same
    dispatch is never settled on an older run's exit object. Shared by both
    ``status`` and ``_authenticated_halt`` so the predicate cannot drift.
    """
    current = observed.get("run_token")
    if not isinstance(current, str) or not current:
        return None
    for key in ("worker_exit", "reaper_exit"):
        evidence = observed.get(key)
        if not isinstance(evidence, dict):
            continue
        token = evidence.get("run_token")
        if isinstance(token, str) and token and secrets.compare_digest(token, current):
            return evidence
    return None


def _configured_spawn_grace_seconds() -> float:
    try:
        return float(os.environ.get("AGENT_COMMS_SPAWN_GRACE_SECONDS", str(SPAWN_GRACE_SECONDS)))
    except ValueError:
        return SPAWN_GRACE_SECONDS


class ProcessSpawnAdapter:
    """Shared command-template runner for subprocess-backed runtimes."""

    runtime_label: str = ""
    supported_runtimes: tuple[str, ...] = ()
    # Placeholders are system-generated/config-derived trusted ids only (never
    # caller-supplied message content), per Spec A's anti-injection rule. The
    # path placeholders are derived from the repo location so no in-repo path is
    # ever hardcoded in spawn config.
    allowed_placeholders = (
        "actor_id",
        "message_id",
        "policy_name",
        "project_root",
        "db_path",
        "mcp_command",
        "hooks_path",
        "codex_home",
        "claude_binary",
        "python",
        "worker_prompt",
    )

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen] = {}
        self._zdotdirs: dict[str, Path] = {}

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        preflight_observed = self._preflight(context) or {}
        recipient = context.recipient
        if recipient.get("runtime") not in self.supported_runtimes:
            raise RuntimeError(
                f"recipient runtime {recipient.get('runtime')!r} "
                f"not supported by {type(self).__name__}"
            )

        spawn = recipient.get("spawn") or {}
        raw_command = spawn.get("command")
        if not raw_command:
            raise RuntimeError(f"{self.runtime_label} recipient requires spawn.command")
        command = self._format_arg(str(raw_command), context)
        self._validate_rendered_command(command, context)

        policy = compile_policy(str(context.dispatch["policy_name"]))
        args = self._resolved_spawn_args(context, spawn, policy)
        prompt_index = (
            self._worker_prompt_arg_index(spawn.get("args", []) or [], policy)
            if self._uses_live_worker_prompt(context)
            else None
        )
        separate_stdout = self._separate_stdout(context, args, prompt_index)
        if policy.bootstrap_marker not in " ".join(args):
            raise RuntimeError(f"spawn args must include bootstrap marker {policy.bootstrap_marker}")

        zdotdir = self._worker_zdotdir(context)
        env = self._build_env(context, policy, spawn, zdotdir=zdotdir)

        # Conservative spawn-time janitor over the protected control root only:
        # it removes just the run directories whose versioned owner marker names
        # a SQL-terminal run. It never decides lifecycle from the filesystem and
        # never blocks a spawn, so a sweep failure is swallowed on purpose.
        try:
            supervisor.janitor_sweep(str(context.db_path))
        except Exception:
            pass

        # The native runtime child; the supervisor wrapper carries the TTL and run
        # token over the inherited socketpair bootstrap, never on this argv.
        child_command = [command, *args]
        worker_log = self._worker_log_path(context)
        log_file = None
        events_file = None
        worker_events = None
        try:
            try:
                log_file = worker_log.open("a")
                if separate_stdout:
                    worker_events = paths.dispatch_events_path(
                        str(context.dispatch["dispatch_id"])
                    )
                    events_file = worker_events.open("a")
            except OSError as exc:
                self._cleanup_zdotdir(zdotdir)
                raise SpawnFailed(
                    f"worker artifact open failed: {exc}; log={worker_log}"
                ) from exc
            try:
                spawned = supervisor.spawn_supervised(
                    child_command,
                    dispatch_id=str(context.dispatch["dispatch_id"]),
                    db_path=str(context.db_path),
                    ttl_seconds=float(context.ttl_seconds),
                    kill_after_seconds=KILL_AFTER_SECONDS,
                    zdotdir=str(zdotdir),
                    expected_close_by=context.expected_close_by,
                    env=env,
                    cwd=str(recipient["project_root"]),
                    stdout=events_file if events_file is not None else log_file,
                    stderr=log_file,
                )
            except supervisor.SupervisorError as exc:
                # Pre-READY failure: the supervisor cleaned its own attempted run
                # directory and never sent READY. The adapter owns the zdotdir it
                # created, so it cleans that here and surfaces a spawn failure, so
                # the ledger records spawn_failed_message_landed rather than a
                # false live row.
                self._cleanup_zdotdir(zdotdir)
                raise SpawnFailed(
                    f"supervised worker did not reach READY: {exc}; log={worker_log}"
                ) from exc
            except Exception:
                self._cleanup_zdotdir(zdotdir)
                raise
        finally:
            if events_file is not None:
                events_file.close()
            if log_file is not None:
                log_file.close()

        # Post-READY: the supervisor wrapper owns run-directory and zdotdir
        # teardown on child exit / HALT / TTL, and the one module-global parent
        # reaper is the idempotent fallback for both. The recorded control
        # identity makes status()/halt() authenticate over the socket rather than
        # parse a PID or signal a process group.
        handle = f"{self.runtime_label}:{recipient['id']}:{spawned.popen.pid}"
        self._processes[handle] = spawned.popen
        self._zdotdirs[handle] = zdotdir
        supervisor.reaper_registry().register(
            handle,
            spawned.popen,
            db_path=str(context.db_path),
            dispatch_id=str(context.dispatch["dispatch_id"]),
            run_token=spawned.run_token,
            run_dir=spawned.run_dir,
            zdotdir=str(zdotdir),
            # Revision 7 F2: pin the exact terminal-proof identities at READY from
            # the existing SupervisedSpawn, never rediscovered. Both the wrapper
            # and the native child are launched start_new_session, so each
            # process-group id equals its PID.
            control_socket=spawned.control_socket,
            wrapper_pgid=spawned.wrapper_pid,
            child_pgid=spawned.child_pid,
        )
        return DispatchStart(
            spawn_handle=handle,
            observed_values={
                "adapter": self.runtime_label,
                "pid": spawned.wrapper_pid,
                "wrapper_pid": spawned.wrapper_pid,
                "child_pid": spawned.child_pid,
                "run_token": spawned.run_token,
                "control_socket": spawned.control_socket,
                "protocol_version": supervisor.PROTOCOL_VERSION,
                "expected_close_by": context.expected_close_by,
                "worker_log": str(worker_log),
                **(
                    {"worker_events": str(worker_events)}
                    if worker_events is not None
                    else {}
                ),
                **preflight_observed,
            },
        )

    def _separate_stdout(
        self,
        context: DispatchContext,
        resolved_args: list[str],
        prompt_index: int | None,
    ) -> bool:
        return False

    def _preflight(self, context: DispatchContext) -> dict | None:
        return None

    def _validate_rendered_command(self, command: str, context: DispatchContext) -> None:
        return None

    def _build_env(  # type: ignore[no-untyped-def]
        self, context: DispatchContext, policy, spawn: dict, *, zdotdir: Path | None = None
    ) -> dict[str, str]:
        base = {key: os.environ[key] for key in ALLOWED_BASE_ENV_NAMES if key in os.environ}
        env = scoped_env(base, policy)
        env.update(self._spawn_env(spawn, context, policy))
        env["ZDOTDIR"] = str(zdotdir if zdotdir is not None else self._worker_zdotdir(context))
        env.update(
            {
                "AGENT_COMMS_ACTOR_ID": str(context.recipient["id"]),
                "AGENT_COMMS_DISPATCH_ID": str(context.dispatch["dispatch_id"]),
                "AGENT_COMMS_MESSAGE_ID": str(context.message["id"]),
                "AGENT_COMMS_PROJECT_ROOT": str(context.recipient["project_root"]),
            }
        )
        # Test-only extra child environment (e.g. a child-pid capture file). The
        # supervisor's ``launch_child`` inherits this env, so the native child
        # sees it. Production returns {}.
        env.update(self._extra_child_env(context))
        return env

    def _worker_log_path(self, context: DispatchContext) -> Path:
        """Log path the wrapper (and its inherited child) write to.

        Default is the repo dispatch log; test harnesses override it to capture
        output without the removed legacy timeout wrapper.
        """
        return paths.dispatch_log_path(str(context.dispatch["dispatch_id"]))

    def _extra_child_env(self, context: DispatchContext) -> dict[str, str]:
        """Extra environment merged into the supervised child. Default: none."""
        return {}

    @staticmethod
    def _worker_zdotdir(context: DispatchContext) -> Path:
        actor_id = str(context.recipient["id"])
        safe_actor_id = "".join(char if char.isalnum() or char in "._-" else "_" for char in actor_id)
        parent = Path(tempfile.mkdtemp(prefix=f"agent-comms-zdotdir-{safe_actor_id}-"))
        zdotdir = parent / "empty-zdotdir"
        zdotdir.mkdir(mode=0o500)
        return zdotdir

    @staticmethod
    def _cleanup_zdotdir(zdotdir: Path) -> None:
        parent = zdotdir.parent
        if not parent.name.startswith("agent-comms-zdotdir-"):
            return
        shutil.rmtree(parent, ignore_errors=True)

    def _spawn_env(self, spawn: dict, context: DispatchContext, policy) -> dict[str, str]:  # type: ignore[no-untyped-def]
        raw_env = spawn.get("env", {}) or {}
        if not isinstance(raw_env, dict):
            raise RuntimeError("spawn.env must be an object")

        env: dict[str, str] = {}
        for key, value in raw_env.items():
            env_key = str(key)
            if self._is_forbidden_spawn_env_key(env_key, policy):
                raise RuntimeError(f"spawn.env key {env_key} is forbidden by policy")
            env[env_key] = self._format_arg(str(value), context)
        return env

    def _resolved_spawn_args(self, context: DispatchContext, spawn: dict, policy) -> list[str]:  # type: ignore[no-untyped-def]
        raw_args = spawn.get("args", []) or []
        if not isinstance(raw_args, list):
            raise RuntimeError("spawn.args must be an array")

        is_claude = context.recipient.get("runtime") == "claude" and self.runtime_label == "claude"
        uses_live_worker_prompt = self._uses_live_worker_prompt(context)
        worker_prompt_index = self._worker_prompt_arg_index(raw_args, policy) if uses_live_worker_prompt else None
        resolved: list[str] = []
        skip_next_settings_value = False
        for index, raw_arg in enumerate(raw_args):
            arg = str(raw_arg)
            if is_claude and skip_next_settings_value:
                resolved.append(arg)
                skip_next_settings_value = False
                continue
            if uses_live_worker_prompt and index == worker_prompt_index:
                resolved.append(arg)
                skip_next_settings_value = is_claude and arg == "--settings"
                continue
            resolved.append(self._format_arg(arg, context))
            skip_next_settings_value = is_claude and arg == "--settings"

        if uses_live_worker_prompt and worker_prompt_index is None:
            raise RuntimeError(
                f"{self.runtime_label} spawn args must include a worker prompt slot with "
                f"bootstrap marker {policy.bootstrap_marker}"
            )
        if uses_live_worker_prompt and worker_prompt_index is not None:
            resolved[worker_prompt_index] = self._live_worker_prompt(context)

        if is_claude:
            try:
                settings_index = resolved.index("--settings") + 1
            except ValueError:
                settings_index = None
            if settings_index is not None:
                if settings_index >= len(resolved):
                    raise RuntimeError("claude spawn args require a value after --settings")
                resolved[settings_index] = claude_settings_for_policy(
                    policy, str(paths.hooks_path()), sys.executable
                )
        return resolved

    def _uses_live_worker_prompt(self, context: DispatchContext) -> bool:
        runtime = context.recipient.get("runtime")
        return runtime in {"claude", "codex"} and runtime == self.runtime_label

    @staticmethod
    def _worker_prompt_arg_index(raw_args: list, policy) -> int | None:  # type: ignore[no-untyped-def]
        from ..spawn import worker_prompt_slot

        return worker_prompt_slot(raw_args, policy.bootstrap_marker)

    @staticmethod
    def _live_worker_prompt(context: DispatchContext) -> str:
        from ..spawn import V2_WORKER_PROMPT
        template = V2_WORKER_PROMPT if context.dispatch.get("policy_version") == "v2" else DEFAULT_WORKER_PROMPT
        return template.format(
            actor_id=str(context.recipient["id"]),
            message_id=str(context.message["id"]),
        )

    @staticmethod
    def _is_forbidden_spawn_env_key(key: str, policy) -> bool:  # type: ignore[no-untyped-def]
        return (
            key.startswith("AGENT_COMMS_")
            or key in policy.stripped_env_names
            or any(key.startswith(prefix) for prefix in policy.stripped_env_prefixes)
        )

    def status(self, spawn_handle: str, observed_values: dict | None = None) -> AdapterStatus:
        """Typed liveness for a dispatch. Never signals.

        Ordering per Spec C:
        - a same-run SQL exit report (``worker_exit``/``reaper_exit`` whose
          nonempty run_token exactly equals the current observed run_token) ->
          ``exited``. Stale/missing/malformed exit evidence is ignored here;
        - authenticated same-token socket STATUS says running -> ``running``;
        - missing supervisor identity, or connection/protocol/token failure ->
          ``supervisor_unreachable`` (never inferred dead).
        """
        observed = dict(observed_values or {})
        exit_evidence = _same_run_exit_evidence(observed)
        if exit_evidence is not None:
            return AdapterStatus(
                state="exited",
                returncode=exit_evidence.get("returncode"),
                detail=f"same-run exit evidence: {exit_evidence}",
            )
        control_socket = observed.get("control_socket")
        run_token = observed.get("run_token")
        if not isinstance(control_socket, str) or not isinstance(run_token, str):
            return AdapterStatus(
                state="supervisor_unreachable",
                detail="no supervisor control identity in observed values",
            )
        result = supervisor.probe_status(control_socket, run_token)
        if result.ok and result.state == "running":
            return AdapterStatus(state="running")
        return AdapterStatus(
            state="supervisor_unreachable",
            detail=f"supervisor STATUS not confirmed: {result.error or result.state}",
        )

    def halt(self, spawn_handle: str, observed_values: dict | None = None) -> None:
        if not isinstance(spawn_handle, str):
            raise RuntimeError(f"invalid {self.runtime_label} spawn_handle: {spawn_handle}")

        observed = dict(observed_values or {})
        control_socket = observed.get("control_socket")
        run_token = observed.get("run_token")
        if isinstance(control_socket, str) and isinstance(run_token, str):
            self._authenticated_halt(spawn_handle, control_socket, run_token, observed)
            return
        # No supervisor control identity: the dispatch cannot be authenticated,
        # so termination is unconfirmed and owned by stage-2/manual recovery.
        # There is deliberately NO handle-parsed PID / killpg fallback -- that
        # categorical Spec C boundary (missing identity is unreachable, never a
        # PID signal) is exactly what this stage enforces.
        raise SupervisorUnreachable(
            f"no supervisor control identity for {spawn_handle}; "
            f"{TERMINATION_NOT_CONFIRMED_PHRASE}"
        )

    def _authenticated_halt(
        self, spawn_handle: str, control_socket: str, run_token: str, observed: dict
    ) -> None:
        """Authenticated HALT on the exact recorded socket. No ps, no killpg.

        The client I/O timeout is aligned with the wrapper's TERM/KILL grace so
        an ignore-TERM child returns a real acknowledgement rather than a
        deterministic timeout. Success is reported only after a matching
        termination acknowledgement, or -- as the SQL fallback -- the exact
        version-1 COMPLETE ``$.reaper_exit`` proof whose ``run_token`` exactly
        equals the current observed run_token (Revision 7 F2). A bare same-run
        ``$.worker_exit`` is child-exit evidence only: ``status`` may still
        report the child exited from it, but it never confirms a HALT because it
        does not prove registered-wrapper reap, native-group drain, or
        owned-artifact cleanup. Missing/incomplete/false/stale/wrong-token
        evidence never confirms; otherwise this raises ``SupervisorUnreachable``
        and the caller must not claim native-child death.
        """
        result = supervisor.request_halt(control_socket, run_token, io_timeout=HALT_IO_TIMEOUT_SECONDS)
        confirmed = result.ok and result.state == "halted"
        if not confirmed and supervisor.complete_reaper_proof(observed.get("reaper_exit"), run_token) is not None:
            confirmed = True
        if not confirmed:
            raise SupervisorUnreachable(
                f"authenticated halt did not confirm termination for {spawn_handle}: "
                f"{result.error or result.state}; {TERMINATION_NOT_CONFIRMED_PHRASE}"
            )
        self._cleanup_zdotdir_for_handle(spawn_handle)

    def owned_zdotdir_parent(self, spawn_handle: str) -> Path:
        """Revision 7 F4: resolve THIS run's owned ZDOTDIR parent strictly from
        the exact ``spawn_handle -> zdotdir`` association this adapter instance
        recorded at spawn.

        Read-only accessor over the private map. It is NOT a public / MCP /
        admin surface. It requires exactly one mapping for the exact handle and
        validates the owned-parent naming boundary (``agent-comms-zdotdir-*``);
        a missing handle or a mapping whose parent violates that boundary FAILS
        LOUDLY. It never globs the shared temp root, uses recency, or a
        before/after ownership delta -- so unrelated historical actor-named
        directories can neither satisfy nor poison the current-run barrier.
        """
        zdotdir = self._zdotdirs.get(spawn_handle)
        if zdotdir is None:
            raise LookupError(
                f"no owned zdotdir mapping for spawn handle {spawn_handle!r}; "
                "current-run ownership cannot be resolved"
            )
        parent = Path(zdotdir).parent
        if not parent.name.startswith("agent-comms-zdotdir-"):
            raise ValueError(
                f"resolved zdotdir parent {parent.name!r} violates the owned-parent "
                "naming boundary for the current run"
            )
        return parent

    def _cleanup_zdotdir_for_handle(self, spawn_handle: str) -> None:
        zdotdir = self._zdotdirs.pop(spawn_handle, None)
        if zdotdir is not None:
            self._cleanup_zdotdir(zdotdir)

    def _format_arg(self, arg: str, context: DispatchContext) -> str:
        values = {
            "actor_id": str(context.recipient["id"]),
            "message_id": str(context.message["id"]),
            "policy_name": str(context.dispatch["policy_name"]),
            "project_root": str(context.recipient["project_root"]),
            "db_path": str(context.db_path),
            "mcp_command": str(paths.mcp_command()),
            "hooks_path": str(paths.hooks_path()),
            "codex_home": str(paths.codex_home(str(context.recipient["id"]))),
            "claude_binary": str(claude_binary_path()),
            # The dispatching process's own interpreter: whatever can run this
            # adapter can import the package, on PATH or not.
            "python": sys.executable,
        }
        try:
            return arg.format(**values)
        except KeyError as exc:
            allowed = ", ".join(self.allowed_placeholders)
            raise RuntimeError(f"unsupported spawn placeholder {exc}; allowed: {allowed}") from exc
