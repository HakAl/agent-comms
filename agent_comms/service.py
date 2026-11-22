"""Run the monitor as a user service: launchd on macOS, systemd user units on Linux.

One interface, two backends. A service belongs to one install, which is its
runtime root and its ledger. The unit starts ``agent-comms-monitor`` directly,
never through a shell, so no shell startup file is read on the way.

What stopping the service means for workers: nothing. A worker that is already
running stays under its own supervisor, with its own time limit, and the next
monitor reconciles it over the control socket. The units say so explicitly
(``KillMode=process`` for systemd, ``AbandonProcessGroup`` for launchd) so a
service manager default does not decide it.

What the unit's environment binds: the service manager may hand the process
variables agent-comms never wrote (a systemd user manager's imported
environment, ``launchctl setenv``). Every variable that moves the runtime
root, the ledger or a directory under it is therefore written into the unit,
``AGENT_COMMS_HOME`` always with the root resolved at install time, the
others with the installer's value or empty, and an empty value means "not
set" to every resolver in ``paths``.

This is user-level only: nothing here needs root or writes a system unit.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paths
from .schema import ValidationError

LAUNCHD = "launchd"
SYSTEMD = "systemd"

LAUNCHD_LABEL = "com.agent-comms.monitor"
SYSTEMD_NAME = "agent-comms-monitor"

# Resolver overrides the monitor (or a worker launch it performs) reads. The
# unit writes each one: the installer's value when it was set, empty when not.
RESOLVER_ENV = (
    "AGENT_COMMS_DB",
    "AGENT_COMMS_CODEX_CUSTODY_ROOT",
    "AGENT_COMMS_DISPATCH_LOG_DIR",
    "AGENT_COMMS_SUPERVISOR_ROOT",
)

# Overrides that select another pin manifest, custody copy or digest. They are
# test and certification switches; a service always runs the shipped pins, so
# the unit writes each one empty whatever the installing shell holds.
CLEARED_ENV = (
    "AGENT_COMMS_RUNTIME_PINS",
    "AGENT_COMMS_CLAUDE_VERSIONS_DIR",
    "AGENT_COMMS_CLAUDE_PINNED_SHA256",
    "AGENT_COMMS_CODEX_VERSIONS_DIR",
    "AGENT_COMMS_CODEX_PINNED_SHA256",
)

# launchd waits this long before restarting a job that exited abnormally.
LAUNCHD_THROTTLE_SECONDS = 10
# `launchctl print` exits with this when the domain has no such service.
LAUNCHD_NOT_FOUND = 113
SYSTEMD_RESTART_SECONDS = 5

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        # No such tool (a container without systemd, say): a failed command
        # the caller reports, not a traceback.
        return subprocess.CompletedProcess(argv, 127, stdout="", stderr=f"{argv[0]}: {exc.strerror or exc}")


class ServiceError(ValidationError):
    """A service operation was refused or the service manager rejected it."""


def backend_for_platform(platform: str | None = None) -> str:
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return LAUNCHD
    if platform.startswith("linux"):
        return SYSTEMD
    raise ServiceError(
        f"no service manager support on {platform}: the monitor runs as a launchd user agent on macOS "
        "and a systemd user unit on Linux; elsewhere run agent-comms-monitor in a terminal"
    )


def _refuse_control_characters(name: str, value: str) -> None:
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ServiceError(f"{name} holds a control character or a line break, which a service unit cannot carry: {value!r}")


def checked_path_env(value: str) -> str:
    """``value`` as the PATH a unit may carry: every entry absolute.

    An empty entry means the working directory to a shell and to ``execvp``,
    so it is refused like any other relative entry."""
    entries = value.split(os.pathsep)
    if not value or any(not entry or not os.path.isabs(entry) for entry in entries):
        bad = [entry or "<empty>" for entry in entries if not entry or not os.path.isabs(entry)] or ["<empty>"]
        raise ServiceError(
            "PATH has an entry that is not an absolute directory (" + ", ".join(bad) + "); a service would resolve "
            "worker tools against its own working directory. Fix PATH in this shell and run install again"
        )
    _refuse_control_characters("PATH", value)
    return value


def is_default_root(runtime_root: Path) -> bool:
    """Whether ``runtime_root`` is the default runtime root, however it was named.

    Decided from the resolved root, not from whether ``AGENT_COMMS_HOME`` is
    set: a variable that spells out the default must not get a second service
    on the same ledger."""
    default = Path.home() / ".agent-comms"
    return os.path.normpath(runtime_root) == os.path.normpath(default) or os.path.realpath(runtime_root) == os.path.realpath(default)


def suffix_for(runtime_root: Path, *, default_root: bool) -> str:
    """Empty for the default runtime root, else a short stable name for this
    root, so two installs get two services."""
    if default_root:
        return ""
    return hashlib.sha256(str(runtime_root).encode()).hexdigest()[:10]


@dataclass(frozen=True)
class ServiceSpec:
    """One install's monitor service: what to run, as what, bound to which ledger."""

    runtime_root: Path
    default_root: bool
    db_path: Path
    db_explicit: bool
    human_actor_id: str
    program: Path
    path_env: str
    lang: str | None = None
    resolver_env: Mapping[str, str] = field(default_factory=dict)

    @property
    def suffix(self) -> str:
        return suffix_for(self.runtime_root, default_root=self.default_root)

    @property
    def log_file(self) -> Path:
        return self.runtime_root / "logs" / "monitor.log"

    def argv(self) -> list[str]:
        argv = [str(self.program)]
        if self.db_explicit:
            argv += ["--db", str(self.db_path)]
        return [*argv, "--human-actor-id", self.human_actor_id, "--log-file", str(self.log_file)]

    def environment(self) -> dict[str, str]:
        """What agent-comms sets for the process. Not its whole environment:
        the service manager adds its own (HOME, USER and so on)."""
        env = {
            "PATH": self.path_env,
            # Always the captured root, the default one included: the service
            # manager's HOME need not be the installing shell's, and an unset
            # variable would resolve the root from it.
            paths.HOME_ENV: str(self.runtime_root),
        }
        for name in RESOLVER_ENV:
            env[name] = self.resolver_env.get(name, "")
        for name in CLEARED_ENV:
            env[name] = ""
        if self.db_explicit:
            # The ledger rides on the command line; the variable must not
            # name a second one.
            env["AGENT_COMMS_DB"] = ""
        if self.lang:
            env["LANG"] = self.lang
        return env

    def validate(self) -> None:
        checked_path_env(self.path_env)
        for name, value in [("the monitor command", arg) for arg in self.argv()] + list(self.environment().items()):
            _refuse_control_characters(name, value)
        for name, path in (("the runtime root", self.runtime_root), ("the ledger", self.db_path), ("the monitor command", self.program)):
            if not path.is_absolute():
                raise ServiceError(f"{name} is not an absolute path: {path}")


def spec_from_environment(
    *,
    human_actor_id: str,
    db_path: Path,
    db_explicit: bool,
    environ: Mapping[str, str] | None = None,
    program: Path | None = None,
) -> ServiceSpec:
    """The spec for the install this process belongs to, read at install time."""
    environ = os.environ if environ is None else environ
    root = paths.runtime_root()
    resolver_env = {name: environ[name] for name in RESOLVER_ENV if name != "AGENT_COMMS_DB" and environ.get(name)}
    for name, value in resolver_env.items():
        if not os.path.isabs(os.path.expanduser(value)):
            raise ServiceError(f"{name}={value!r} is not an absolute path; a service has no shell directory to resolve it against")
        resolver_env[name] = os.path.expanduser(value)
    spec = ServiceSpec(
        runtime_root=root,
        default_root=is_default_root(root),
        db_path=Path(os.path.abspath(os.path.expanduser(str(db_path)))),
        db_explicit=db_explicit,
        human_actor_id=human_actor_id,
        program=paths.console_script("agent-comms-monitor") if program is None else program,
        path_env=environ.get("PATH", ""),
        lang=environ.get("LANG") or None,
        resolver_env=resolver_env,
    )
    spec.validate()
    return spec


# --- systemd serialization -------------------------------------------------


def _systemd_quote(value: str, *, command: bool) -> str:
    """``value`` as one double-quoted systemd word that reads back literally.

    ``%`` introduces a specifier everywhere in a unit file; ``$`` introduces a
    variable in a command's arguments but is literal in ``Environment=`` and
    in the program path, the first word of ``ExecStart=`` (systemd 257 runs
    ``a$$b/tool`` as written and fails with status 203)."""
    out = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        out = out.replace("$", "$$")
    return f'"{out}"'


def render_systemd_unit(spec: ServiceSpec) -> str:
    spec.validate()
    lines = [
        "# Written by `agent-comms service install`; run it again to change this file.",
        "[Unit]",
        "Description=agent-comms monitor" + (f" ({spec.runtime_root})".replace("%", "%%") if not spec.default_root else ""),
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(_systemd_quote(arg, command=index > 0) for index, arg in enumerate(spec.argv())),
    ]
    for name, value in sorted(spec.environment().items()):
        lines.append("Environment=" + _systemd_quote(f"{name}={value}", command=False))
    lines += [
        "Restart=on-failure",
        f"RestartSec={SYSTEMD_RESTART_SECONDS}",
        # Only the monitor is signalled on stop. Workers already running stay
        # with their supervisors; the next monitor reconciles them.
        "KillMode=process",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def render_launchd_plist(spec: ServiceSpec, label: str) -> bytes:
    spec.validate()
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": spec.argv(),
            "EnvironmentVariables": spec.environment(),
            "RunAtLoad": True,
            # Restart after a crash, not after a clean exit.
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": LAUNCHD_THROTTLE_SECONDS,
            "ProcessType": "Background",
            # Workers already running are not the job's to kill.
            "AbandonProcessGroup": True,
        },
        sort_keys=True,
    )


# --- state -----------------------------------------------------------------


@dataclass
class ManagerState:
    installed: bool
    loaded: bool = False
    running: bool = False
    pid: int | None = None
    last_exit: str | None = None
    enabled: bool | None = None
    linger: bool | None = None
    detail: str = ""
    start_limit_hit: bool = False


def _fail(action: str, completed: subprocess.CompletedProcess[str]) -> ServiceError:
    output = " ".join(part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip())
    return ServiceError(f"{action} failed (exit {completed.returncode}): {output or 'no output'}")


class LaunchdBackend:
    name = LAUNCHD

    def __init__(
        self,
        suffix: str = "",
        *,
        run: Runner = run_command,
        agents_dir: Path | None = None,
        uid: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.label = LAUNCHD_LABEL + (f".{suffix}" if suffix else "")
        self.run = run
        self.agents_dir = Path.home() / "Library" / "LaunchAgents" if agents_dir is None else agents_dir
        self.uid = os.getuid() if uid is None else uid
        self.sleep = sleep

    @property
    def unit_path(self) -> Path:
        return self.agents_dir / f"{self.label}.plist"

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{self.label}"

    def render(self, spec: ServiceSpec) -> bytes:
        return render_launchd_plist(spec, self.label)

    def _print(self) -> subprocess.CompletedProcess[str] | None:
        """The job's ``launchctl print`` result, or None when launchd says
        there is no such job. Any other failure is an error: a query that
        failed is not evidence that the job is gone."""
        printed = self.run(["launchctl", "print", self.target])
        if printed.returncode == 0:
            return printed
        if printed.returncode == LAUNCHD_NOT_FOUND or "Could not find service" in (printed.stderr or "") + (printed.stdout or ""):
            return None
        raise _fail(f"launchctl print {self.target}", printed)

    def state(self) -> ManagerState:
        state = ManagerState(installed=self.unit_path.is_file())
        printed = self._print()
        if printed is None:
            return state
        state.loaded = True
        # Only the job's own keys, one tab deep: the nested coalition blocks
        # further down reuse the key `state`.
        values: dict[str, str] = {}
        for line in printed.stdout.splitlines():
            if not line.startswith("\t") or line.startswith("\t\t"):
                continue
            key, separator, value = line.strip().partition(" = ")
            if separator:
                values.setdefault(key, value)
        state.detail = values.get("state", "")
        state.running = state.detail == "running"
        pid = values.get("pid", "")
        state.pid = int(pid) if state.running and pid.isdigit() else None
        last_exit = values.get("last exit code")
        if not state.running and last_exit and last_exit != "(never exited)":
            state.last_exit = last_exit
        return state

    def _bootout(self) -> None:
        if self._print() is None:
            return
        completed = self.run(["launchctl", "bootout", self.target])
        if completed.returncode != 0:
            raise _fail(f"launchctl bootout {self.target}", completed)
        # bootout returns before the job is gone, and a bootstrap that
        # arrives early fails with an input/output error.
        for _ in range(50):
            if self._print() is None:
                return
            self.sleep(0.2)
        raise ServiceError(f"{self.target} is still loaded 10 seconds after launchctl bootout")

    def _bootstrap(self) -> None:
        completed = self.run(["launchctl", "bootstrap", self.domain, str(self.unit_path)])
        if completed.returncode != 0:
            raise _fail(f"launchctl bootstrap {self.domain} {self.unit_path}", completed)

    def install(self, spec: ServiceSpec) -> None:
        content = self.render(spec)
        self._bootout()
        _write_private(self.unit_path, content)
        self._bootstrap()

    def start(self) -> None:
        if not self.unit_path.is_file():
            raise ServiceError(f"not installed: {self.unit_path} does not exist; run: agent-comms service install")
        if self._print() is None:
            # stop is bootout, which unloads the definition. RunAtLoad starts
            # a bootstrapped job; the kickstart that follows is a no-op then
            # and starts it if launchd deferred the load.
            self._bootstrap()
        completed = self.run(["launchctl", "kickstart", self.target])
        if completed.returncode != 0:
            raise _fail(f"launchctl kickstart {self.target}", completed)

    def stop(self) -> None:
        self._bootout()

    def uninstall(self) -> None:
        self._bootout()
        self.unit_path.unlink(missing_ok=True)


class SystemdBackend:
    name = SYSTEMD

    def __init__(
        self,
        suffix: str = "",
        *,
        run: Runner = run_command,
        unit_dir: Path | None = None,
        user: str | None = None,
    ) -> None:
        self.unit = SYSTEMD_NAME + (f"-{suffix}" if suffix else "") + ".service"
        self.run = run
        if unit_dir is None:
            config = os.environ.get("XDG_CONFIG_HOME")
            base = Path(config) if config and os.path.isabs(config) else Path.home() / ".config"
            unit_dir = base / "systemd" / "user"
        self.unit_dir = unit_dir
        self.user = user if user is not None else (os.environ.get("USER") or os.environ.get("LOGNAME") or str(os.getuid()))

    @property
    def unit_path(self) -> Path:
        return self.unit_dir / self.unit

    def render(self, spec: ServiceSpec) -> bytes:
        return render_systemd_unit(spec).encode()

    def _systemctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        argv = ["systemctl", "--user", *args]
        completed = self.run(argv)
        if check and completed.returncode != 0:
            raise _fail(" ".join(argv), completed)
        return completed

    def linger_command(self) -> str:
        return f"loginctl enable-linger {self.user}"

    def state(self) -> ManagerState:
        state = ManagerState(installed=self.unit_path.is_file())
        shown = self._systemctl(
            "show", self.unit,
            "--property=LoadState,ActiveState,SubState,MainPID,ExecMainStatus,Result,UnitFileState",
            check=False,
        )
        values = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
        if shown.returncode == 0:
            state.loaded = values.get("LoadState") == "loaded"
            state.running = values.get("ActiveState") == "active" and values.get("SubState") == "running"
            pid = values.get("MainPID", "0")
            state.pid = int(pid) if state.running and pid.isdigit() and int(pid) > 0 else None
            state.enabled = values.get("UnitFileState") == "enabled"
            state.detail = "/".join(part for part in (values.get("ActiveState"), values.get("SubState")) if part)
            result = values.get("Result", "")
            if not state.running and result and result != "success":
                state.last_exit = f"{result} (status {values.get('ExecMainStatus', '?')})"
                state.start_limit_hit = result == "start-limit-hit"
        linger = self.run(["loginctl", "show-user", self.user, "--property=Linger"])
        if linger.returncode == 0 and "=" in linger.stdout:
            state.linger = linger.stdout.strip().split("=", 1)[1] == "yes"
        return state

    def install(self, spec: ServiceSpec) -> None:
        content = self.render(spec)
        _write_private(self.unit_path, content)
        self._systemctl("daemon-reload")
        self._systemctl("enable", self.unit)
        self._systemctl("restart", self.unit)

    def start(self) -> None:
        if not self.unit_path.is_file():
            raise ServiceError(f"not installed: {self.unit_path} does not exist; run: agent-comms service install")
        self._systemctl("start", self.unit)

    def stop(self) -> None:
        self._systemctl("stop", self.unit)

    def uninstall(self) -> None:
        if self.unit_path.is_file() or self._systemctl("cat", self.unit, check=False).returncode == 0:
            # `--now` stops the unit only after the unit-file operation
            # succeeded, so a failure here can leave the monitor running: the
            # definition stays and the failure is reported.
            self._systemctl("disable", "--now", self.unit)
        else:
            # No definition is not "nothing running": a unit whose file was
            # removed by hand stays active in the manager. `cat` reads files,
            # so ask the manager for the runtime state and stop what is there.
            shown = self._systemctl("show", self.unit, "--property=ActiveState")
            active = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line).get("ActiveState")
            if active is None:
                raise ServiceError(f"systemctl --user show {self.unit} did not report ActiveState; removal cannot be confirmed")
            if active not in ("inactive", "failed"):
                self._systemctl("stop", self.unit)
        self.unit_path.unlink(missing_ok=True)
        self._systemctl("daemon-reload")


def _write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def make_backend(spec_suffix: str, *, platform: str | None = None, run: Runner = run_command):
    name = backend_for_platform(platform)
    return LaunchdBackend(spec_suffix, run=run) if name == LAUNCHD else SystemdBackend(spec_suffix, run=run)


# --- ledger view -----------------------------------------------------------


@dataclass
class LedgerView:
    exists: bool
    heartbeat: dict | None = None
    in_flight: int = 0
    error: str | None = None


def read_ledger_view(db_path: Path) -> LedgerView:
    """The heartbeat and the count of running dispatches, read without writing."""
    if not db_path.is_file():
        return LedgerView(exists=False)
    from .db import Database

    view = LedgerView(exists=True)
    try:
        with Database(db_path).read_only_connection_ctx() as conn:
            tables = {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
            if "monitor_heartbeat" in tables:
                row = conn.execute("select last_pass_at, pid from monitor_heartbeat where id = 1").fetchone()
                view.heartbeat = {"last_pass_at": row[0], "pid": row[1]} if row is not None else None
            if "dispatch_ledger" in tables:
                view.in_flight = int(
                    conn.execute("select count(*) from dispatch_ledger where status = 'in_flight'").fetchone()[0]
                )
    except sqlite3.Error as exc:
        view.error = str(exc)
    return view


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _heartbeat_fresh(heartbeat: dict | None, now: datetime | None) -> bool:
    from .monitor import heartbeat_is_fresh

    return heartbeat_is_fresh(heartbeat, now=now)


def foreign_monitor_pid(
    view: LedgerView,
    service_pid: int | None,
    *,
    alive: Callable[[int], bool] = pid_alive,
    now: datetime | None = None,
) -> int | None:
    """The pid of a live monitor on this ledger that the service does not own.

    A fresh heartbeat whose process is gone is nobody's: it does not count."""
    heartbeat = view.heartbeat
    if not heartbeat or not _heartbeat_fresh(heartbeat, now):
        return None
    pid = heartbeat.get("pid")
    if not isinstance(pid, int) or pid == service_pid or not alive(pid):
        return None
    return pid


def workers_note(view: LedgerView) -> str | None:
    if view.in_flight == 0:
        return None
    noun = "dispatch is" if view.in_flight == 1 else "dispatches are"
    return (
        f"{view.in_flight} {noun} in flight; workers already running are not stopped with the monitor. "
        "Each finishes or times out under its own supervisor, and a running monitor reconciles it"
    )


# --- operations ------------------------------------------------------------


def target_summary(*, runtime_root: Path, db_path: Path, db_explicit: bool) -> dict:
    return {
        "runtime_root": str(runtime_root),
        "ledger": str(db_path),
        "ledger_explicit": db_explicit,
    }


def install(
    spec: ServiceSpec,
    backend,
    *,
    alive: Callable[[int], bool] = pid_alive,
    now: datetime | None = None,
) -> dict:
    before = backend.state()
    view = read_ledger_view(spec.db_path)
    foreign = foreign_monitor_pid(view, before.pid, alive=alive, now=now)
    if foreign is not None:
        raise ServiceError(
            f"a monitor that this service does not own is running on {spec.db_path} (pid {foreign}); "
            "two monitors on one ledger is not supported. Stop that one first (Ctrl-C in its terminal), then install"
        )
    spec.log_file.parent.mkdir(parents=True, exist_ok=True)
    backend.install(spec)
    result = {
        "ok": True,
        "action": "install",
        "manager": backend.name,
        "unit": str(backend.unit_path),
        "command": spec.argv(),
        "environment": spec.environment(),
        "log_file": str(spec.log_file),
        **target_summary(runtime_root=spec.runtime_root, db_path=spec.db_path, db_explicit=spec.db_explicit),
        "notes": [
            "PATH above is this shell's, written into the unit; workers inherit it. Run install again after changing it",
        ],
    }
    note = workers_note(view)
    if note and before.running:
        result["notes"].append(note)
    if backend.name == SYSTEMD:
        state = backend.state()
        if state.linger is not True:
            result["notes"].append(
                "lingering is off for this user, so the monitor stops when your last session ends and starts at "
                f"your next login. To keep it running: {backend.linger_command()}"
            )
    return result


def start(backend) -> dict:
    backend.start()
    return {"ok": True, "action": "start", "manager": backend.name, "unit": str(backend.unit_path)}


def stop(backend, db_path: Path) -> dict:
    view = read_ledger_view(db_path)
    backend.stop()
    result = {"ok": True, "action": "stop", "manager": backend.name, "unit": str(backend.unit_path), "notes": []}
    note = workers_note(view)
    if note:
        result["notes"].append(note)
    return result


def uninstall(backend, db_path: Path) -> dict:
    view = read_ledger_view(db_path)
    backend.uninstall()
    result = {"ok": True, "action": "uninstall", "manager": backend.name, "unit": str(backend.unit_path), "notes": []}
    note = workers_note(view)
    if note:
        result["notes"].append(note)
    return result


def status(
    backend,
    *,
    runtime_root: Path,
    db_path: Path,
    db_explicit: bool,
    alive: Callable[[int], bool] = pid_alive,
    now: datetime | None = None,
) -> dict:
    state = backend.state()
    view = read_ledger_view(db_path)
    fresh = _heartbeat_fresh(view.heartbeat, now)
    heartbeat_pid = view.heartbeat.get("pid") if view.heartbeat else None
    owned = state.pid is not None and heartbeat_pid == state.pid
    problems: list[str] = []
    if not state.installed:
        problems.append(f"not installed ({backend.unit_path} does not exist); run: agent-comms service install")
    elif not state.running and state.start_limit_hit:
        # A plain start is refused again while the rate limit holds.
        problems.append(
            f"installed but not running; last exit: {state.last_exit}. It failed to start too often and the "
            f"manager gave up. Fix the cause (see {runtime_root / 'logs' / 'monitor.log'}), then run: "
            # Both by unit name, so the line works pasted into any shell,
            # whichever runtime root that shell selects.
            f"systemctl --user reset-failed {backend.unit} && systemctl --user start {backend.unit}"
        )
    elif not state.running:
        problems.append(
            "installed but not running"
            + (f"; last exit: {state.last_exit}" if state.last_exit else "")
            + "; run: agent-comms service start"
        )
    elif not fresh:
        problems.append("the service is running but the ledger has no fresh heartbeat from it yet")
    foreign = foreign_monitor_pid(view, state.pid, alive=alive, now=now)
    if foreign is not None:
        problems.append(
            f"a monitor outside the service is running on this ledger (pid {foreign})"
            + (f"; the fresh heartbeat is its, not the service's pid {state.pid}" if state.running else "")
        )
    elif state.running and fresh and not owned:
        # The usual case just after a restart: the previous process wrote it.
        problems.append(
            f"the fresh heartbeat is from pid {heartbeat_pid}, which is no longer running; "
            f"the service (pid {state.pid}) has not written its own yet"
        )
    if view.error:
        problems.append(f"the ledger could not be read: {view.error}")
    result = {
        "ok": not problems,
        "action": "status",
        "manager": backend.name,
        "unit": str(backend.unit_path),
        "installed": state.installed,
        "loaded": state.loaded,
        "running": state.running,
        "pid": state.pid,
        "last_exit": state.last_exit,
        "manager_state": state.detail,
        "heartbeat": {
            "last_pass_at": view.heartbeat.get("last_pass_at") if view.heartbeat else None,
            "pid": heartbeat_pid,
            "fresh": fresh,
            "from_service": owned,
        },
        "in_flight_dispatches": view.in_flight,
        **target_summary(runtime_root=runtime_root, db_path=db_path, db_explicit=db_explicit),
        "problems": problems,
    }
    if backend.name == SYSTEMD:
        result["enabled"] = state.enabled
        result["linger"] = state.linger
        if state.installed and state.linger is not True:
            result["linger_command"] = backend.linger_command()
    return result
