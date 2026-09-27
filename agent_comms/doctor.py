"""``agent-comms doctor``: check an install and say exactly what to fix.

Every check is a dict ``{"id", "status", "detail", "fix"}`` with ``status``
one of ``ok``, ``fail``, ``warn`` or ``skip`` and ``fix`` a command or a
sentence, or None. The report is ``{"ok", "platform", "checks", "fixes"}``
where ``ok`` is "no check failed" and ``fixes`` is the fix of every failing
check in order, so an agent can branch on the exit code and act on the
fixes without reading the details. ``warn`` does not fail the report.

Doctor never creates anything. It opens the ledger read-only and only when
the file exists, never runs the schema, and shells out only to the runtime
binaries it is checking (``--version``, ``auth status``). Each check takes
what it needs as arguments so tests inject paths, a ``which`` and a
subprocess runner instead of patching the world.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import mcp_clients, paths
from .actors import OPERATOR_ACTOR_ENV
from .cli._helpers import load_actor_config
from .codex_home import preflight_home_snapshot
from .db import LEDGER_SCHEMA_VERSION, Database
from .monitor import heartbeat_is_fresh
from .runtime_pins import (
    CLAUDE_PINNED_SHA256_ENV,
    PlatformNotCertified,
    RuntimePin,
    claude_versions_dir,
    current_platform,
    custody_binary_sha256,
    pin_for,
)
from .schema import ValidationError

OK, FAIL, WARN, SKIP = "ok", "fail", "warn", "skip"
CONSOLE_SCRIPTS = ("agent-comms-mcp", "agent-comms-monitor", "agent-comms-seat")
SEMVER_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def check(check_id: str, status: str, detail: str, fix: str | None = None) -> dict:
    if status not in (OK, FAIL, WARN, SKIP):
        raise ValueError(f"unknown check status {status!r}")
    return {"id": check_id, "status": status, "detail": detail, "fix": fix}


def report(platform: str, checks: list[dict]) -> dict:
    return {
        "ok": not any(item["status"] == FAIL for item in checks),
        "platform": platform,
        "checks": checks,
        "fixes": [item["fix"] for item in checks if item["status"] == FAIL and item["fix"]],
    }


_PLACEHOLDER = re.compile(r"^<[^<>]+>$")


def sh(*argv: object) -> str:
    """A shell line from an argv, quoting every word except ``<placeholders>``."""
    return " ".join(str(arg) if _PLACEHOLDER.match(str(arg)) else shlex.quote(str(arg)) for arg in argv)


@dataclass(frozen=True)
class LedgerRef:
    """The ledger doctor was pointed at, so every repair targets the same one.

    ``explicit`` mirrors the CLI: ``--db`` or ``AGENT_COMMS_DB`` was given.
    A repair rendered for an explicit ledger carries ``--db``; one for the
    default ledger carries nothing, which is what the consumer commands
    resolve to.
    """

    path: Path
    explicit: bool

    def db_args(self) -> list[str]:
        return ["--db", str(self.path)] if self.explicit else []

    def cli(self, *args: object) -> str:
        return sh("agent-comms", *self.db_args(), *args)

    def monitor(self, human: str) -> str:
        return sh("agent-comms-monitor", *self.db_args(), "--human-actor-id", human)


def _ledger(db_path: Path, ledger: LedgerRef | None) -> LedgerRef:
    return ledger if ledger is not None else LedgerRef(db_path, False)


# --- ledger snapshot -------------------------------------------------------


@dataclass
class LedgerSnapshot:
    """What one read-only pass over the ledger saw."""

    schema_version: int
    actors: list[dict] = field(default_factory=list)
    heartbeat: dict | None = None
    dispatch_count: int = 0
    dead_credentials: list[dict] = field(default_factory=list)

    def humans(self) -> list[dict]:
        return [actor for actor in self.actors if actor["kind"] == "human"]

    def by_role(self, role: str) -> list[dict]:
        return [actor for actor in self.actors if actor["kind"] == "agent" and actor["role"] == role]

    def workers(self, runtime: str) -> list[dict]:
        return [actor for actor in self.by_role("worker") if actor.get("runtime") == runtime]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("select 1 from sqlite_master where type = 'table' and name = ?", (name,)).fetchone()
    return row is not None


def read_ledger(db_path: Path) -> LedgerSnapshot:
    """Read the ledger without creating, initializing or writing it.

    Raises ``sqlite3.Error`` when the file cannot be opened read-only.
    """
    database = Database(db_path)
    with database.read_only_connection_ctx() as conn:
        snapshot = LedgerSnapshot(schema_version=int(conn.execute("pragma user_version").fetchone()[0]))
        if _table_exists(conn, "actors"):
            for row in conn.execute(
                "select id, kind, role, runtime, team, project_root, spawn_json from actors order by id"
            ):
                snapshot.actors.append(dict(row))
        if _table_exists(conn, "monitor_heartbeat"):
            row = conn.execute(
                "select last_pass_at, pid, interval_seconds, monitor_version from monitor_heartbeat where id = 1"
            ).fetchone()
            snapshot.heartbeat = dict(row) if row is not None else None
        if _table_exists(conn, "dispatch_ledger"):
            snapshot.dispatch_count = int(conn.execute("select count(*) from dispatch_ledger").fetchone()[0])
        if _table_exists(conn, "codex_refresh_claims"):
            snapshot.dead_credentials = [
                dict(row)
                for row in conn.execute(
                    "select lineage_key, dead_reason, dead_at from codex_refresh_claims "
                    "where dead_reason is not null order by lineage_key"
                )
            ]
    return snapshot


# --- the checks ------------------------------------------------------------


def check_install(executable: str = sys.executable) -> dict:
    bin_dir = Path(executable).parent
    missing = [name for name in CONSOLE_SCRIPTS if not (bin_dir / name).is_file()]
    if missing:
        return check(
            "install",
            FAIL,
            f"{', '.join(missing)} not installed next to {executable}",
            "a checkout recovers with: uv sync; an installed package by reinstalling it",
        )
    return check("install", OK, f"{', '.join(CONSOLE_SCRIPTS)} under {bin_dir}")


def check_runtime_root(root: Path) -> dict:
    if not root.exists() and not root.is_symlink():
        return check("runtime_root", FAIL, f"{root} does not exist", sh("mkdir", "-p", root))
    if not root.is_dir():
        return check("runtime_root", FAIL, f"{root} is not a directory", f"{sh('rm', '-f', root)} && {sh('mkdir', '-p', root)}")
    if not os.access(root, os.W_OK):
        return check("runtime_root", FAIL, f"{root} is not writable", sh("chmod", "u+w", root))
    return check("runtime_root", OK, f"{root} exists and is writable")


def _setup_fix(ledger: LedgerRef, project_root: str | None = None) -> str:
    return ledger.cli("setup", "--project-root", project_root or "<project-root>", "--runtimes", "<claude,codex,fake>")


def check_registry(config_path: Path, ledger: LedgerRef | None = None) -> tuple[dict, dict | None]:
    """The registry check and, when it loads, the loaded document."""
    ledger = ledger or LedgerRef(paths.db_path(), False)
    if not config_path.exists():
        detail = f"no actor registry at {config_path}"
        legacy = config_path.with_name("agents.json")
        if legacy.exists():
            return check("registry", FAIL, f"{detail}; legacy {legacy} present", f"{ledger.cli('bootstrap')} (migrates agents.json)"), None
        return check("registry", FAIL, detail, _setup_fix(ledger)), None
    try:
        config = load_actor_config(config_path)
    except ValidationError as exc:
        return check("registry", FAIL, f"{config_path} does not load: {exc}", str(exc)), None
    except ValueError as exc:
        return check("registry", FAIL, f"{config_path} is not valid JSON: {exc}", f"fix the JSON in {config_path}"), None
    except OSError as exc:
        return check("registry", FAIL, f"{config_path} cannot be read: {exc}", sh("chmod", "u+r", config_path)), None
    actors = config.get("actors", {}) if isinstance(config, dict) else {}
    if not isinstance(actors, dict) or not actors:
        return check("registry", FAIL, f"{config_path} lists no actors", _setup_fix(ledger)), None
    teams = sorted({str(entry.get("team")) for entry in actors.values() if isinstance(entry, dict) and entry.get("team")})
    return check("registry", OK, f"{len(actors)} actors, teams: {', '.join(teams) or 'none'} in {config_path}"), config


def _schema_refusal(schema_version: int, db_path: Path) -> str:
    return (
        f"the ledger at {db_path} was written by a newer ledger schema "
        f"(ledger_user_version={schema_version}, code_LEDGER_SCHEMA_VERSION={LEDGER_SCHEMA_VERSION})"
    )


def check_ledger(db_path: Path, ledger: LedgerRef | None = None) -> tuple[dict, LedgerSnapshot | None]:
    ledger = _ledger(db_path, ledger)
    if not db_path.exists():
        return check("ledger", FAIL, f"no ledger at {db_path}", f"{ledger.cli('bootstrap')} (after {ledger.cli('setup')})"), None
    try:
        snapshot = read_ledger(db_path)
    except sqlite3.Error as exc:
        return check("ledger", FAIL, f"{db_path} does not open read-only: {exc}", f"check the file and its directory: {db_path}"), None
    if snapshot.schema_version > LEDGER_SCHEMA_VERSION:
        return check("ledger", FAIL, _schema_refusal(snapshot.schema_version, db_path), "upgrade agent-comms to a release that knows this ledger schema"), snapshot
    humans = snapshot.humans()
    operator = os.environ.get(OPERATOR_ACTOR_ENV, "").strip()
    if operator and operator not in {actor["id"] for actor in humans}:
        return check("ledger", FAIL, f"{OPERATOR_ACTOR_ENV}={operator} is not a registered human", f"register {operator} as a human, or unset {OPERATOR_ACTOR_ENV}"), snapshot
    if not humans:
        return check("ledger", FAIL, "no human actor is registered", _setup_fix(ledger)), snapshot
    if len(humans) > 1 and not operator:
        ids = ", ".join(actor["id"] for actor in humans)
        return check("ledger", FAIL, f"{len(humans)} human actors are registered ({ids})", f"export {OPERATOR_ACTOR_ENV}=<one of them>"), snapshot
    architects = snapshot.by_role("architect")
    if not architects:
        return check("ledger", FAIL, "no architect is registered", _setup_fix(ledger)), snapshot
    detail = (
        f"{db_path}: schema {snapshot.schema_version}, {len(humans)} human, "
        f"{len(architects)} architect, {len(snapshot.by_role('worker'))} worker"
    )
    return check("ledger", OK, detail), snapshot


def runtimes_in_use(snapshot: LedgerSnapshot | None, config: dict | None) -> set[str]:
    """Runtimes of the registered workers (ledger first, registry as well)."""
    runtimes: set[str] = set()
    if snapshot is not None:
        runtimes.update(actor["runtime"] for actor in snapshot.by_role("worker") if actor.get("runtime"))
    if config is not None:
        for entry in config.get("actors", {}).values():
            if isinstance(entry, dict) and entry.get("runtime"):
                runtimes.add(str(entry["runtime"]))
    return runtimes


def check_platform(platform: str, runtimes: set[str]) -> tuple[dict, dict[str, RuntimePin]]:
    """The platform check and the pins it resolved."""
    wanted = sorted(runtimes & {"claude", "codex"})
    if not wanted:
        return check("platform", SKIP, f"{platform}: no claude or codex worker registered"), {}
    pins: dict[str, RuntimePin] = {}
    for runtime in wanted:
        try:
            pins[runtime] = pin_for(runtime, platform)
        except PlatformNotCertified as exc:
            return check("platform", FAIL, str(exc).split("; recover with:")[0], str(exc)), pins
    versions = ", ".join(f"{runtime} {pin.version}" for runtime, pin in pins.items())
    return check("platform", OK, f"{platform} certified: {versions}"), pins


def _version_from(result: subprocess.CompletedProcess[str]) -> tuple[str | None, str]:
    output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
    match = SEMVER_RE.search(output)
    return (match.group(1) if match else None), output


def _version_matches(installed: str, pin: RuntimePin) -> bool:
    if pin.boundary == "minor":
        return installed.split(".")[:2] == pin.version.split(".")[:2]
    return installed == pin.version


def _run_quietly(run: Runner, argv: list[str]) -> subprocess.CompletedProcess[str] | Exception:
    try:
        return run(argv, text=True, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return exc


def check_claude(
    pin: RuntimePin | None,
    *,
    run: Runner = subprocess.run,
    binary: Path | None = None,
) -> dict:
    check_id = "runtime:claude"
    if pin is None:
        return check(check_id, SKIP, "platform is not certified for claude (see platform)")
    binary = binary or claude_versions_dir() / pin.version
    recover = "or re-certify and bump the pin"
    install = f"install Claude Code {pin.version} at {binary}, {recover}"
    if not binary.is_file():
        return check(
            check_id,
            FAIL,
            f"pinned Claude binary is missing; expected claude {pin.version}; path={binary}",
            f"download Claude Code {pin.version} and place the executable at {binary}, {recover}",
        )
    expected = os.environ.get(CLAUDE_PINNED_SHA256_ENV) or pin.sha256 or ""
    try:
        actual = custody_binary_sha256(binary)
    except OSError as exc:
        return check(check_id, FAIL, f"pinned Claude binary at {binary} cannot be read: {exc}", sh("chmod", "u+rx", binary))
    if actual != expected:
        return check(
            check_id,
            FAIL,
            f"pinned Claude digest mismatch: expected sha256 {expected}, got {actual}; path={binary}",
            f"populate runtime custody at {binary} with the certified Claude {pin.version} binary, {recover}",
        )
    result = _run_quietly(run, [str(binary), "--version"])
    if isinstance(result, Exception):
        return check(check_id, FAIL, f"could not execute {binary} --version: {result}", install)
    installed, output = _version_from(result)
    if result.returncode != 0 or installed is None:
        return check(check_id, FAIL, f"{binary} --version exited {result.returncode}: {output!r}", install)
    if not _version_matches(installed, pin):
        return check(check_id, FAIL, f"pinned Claude version drifted: expected {pin.version}, got {installed}; path={binary}", install)
    login = sh(binary, "auth", "login")
    result = _run_quietly(run, [str(binary), "auth", "status"])
    if isinstance(result, Exception):
        return check(check_id, FAIL, f"could not execute {binary} auth status: {result}", login)
    logged_in = False
    if result.returncode == 0:
        try:
            logged_in = bool(json.loads(result.stdout).get("loggedIn"))
        except (ValueError, AttributeError):
            logged_in = False
    if not logged_in:
        return check(check_id, FAIL, f"claude {installed} at {binary} is not logged in", login)
    return check(check_id, OK, f"claude {installed} at {binary}, digest certified, logged in")


def _codex_home_of(actor: dict) -> tuple[Path | None, str | None]:
    try:
        spawn = json.loads(actor.get("spawn_json") or "{}")
    except (TypeError, ValueError):
        return None, "malformed spawn_json"
    env = spawn.get("env") if isinstance(spawn, dict) else None
    value = env.get("CODEX_HOME") if isinstance(env, dict) else None
    if not value:
        return None, "no spawn.env.CODEX_HOME"
    try:
        return paths.resolve_codex_home(actor["id"], str(value)), None
    except ValidationError as exc:
        return None, str(exc)


def check_codex(
    pin: RuntimePin | None,
    workers: list[dict],
    dead_credentials: list[dict],
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = subprocess.run,
    auth_source: Path | None = None,
    now: datetime | None = None,
    ledger: LedgerRef | None = None,
) -> list[dict]:
    """``runtime:codex`` plus one entry per worker home and per dead login."""
    check_id = "runtime:codex"
    ledger = ledger or LedgerRef(paths.db_path(), False)
    if pin is None:
        return [check(check_id, SKIP, "platform is not certified for codex (see platform)")]
    codex = which("codex")
    if codex is None:
        return [check(check_id, FAIL, "codex is not on PATH", "install the Codex CLI so that `codex` is on PATH, then rerun doctor")]
    install = f"install codex {pin.version} (certified for {pin.platform}), or re-certify and bump the pin"
    result = _run_quietly(run, [codex, "--version"])
    if isinstance(result, Exception):
        return [check(check_id, FAIL, f"could not execute {codex} --version: {result}", install)]
    installed, output = _version_from(result)
    if result.returncode != 0 or installed is None:
        return [check(check_id, FAIL, f"{codex} --version exited {result.returncode}: {output!r}", install)]
    if not _version_matches(installed, pin):
        return [check(check_id, FAIL, f"codex version drifted: expected {pin.version} ({pin.boundary}), got {installed}; path={codex}", install)]
    checks: list[dict] = []
    source = (auth_source or paths.runtime_codex_auth_source()).expanduser()
    if not source.is_file():
        checks.append(check(check_id, FAIL, f"codex {installed} at {codex}; shared auth {source} is missing", _codex_login(source.parent)))
    else:
        checks.append(check(check_id, OK, f"codex {installed} at {codex}; shared auth {source} present"))
    for worker in workers:
        worker_id = worker["id"]
        home_id = f"runtime:codex:home:{worker_id}"
        home, why = _codex_home_of(worker)
        if home is None:
            checks.append(check(home_id, FAIL, f"{worker_id}: {why}", f"re-register {worker_id} so its spawn carries CODEX_HOME: {ledger.cli('bootstrap')}"))
            continue
        ok, reason, _snapshot = preflight_home_snapshot(home, actor_id=worker_id, now=now)
        if ok:
            checks.append(check(home_id, OK, f"{worker_id}: {home} provisioned, auth fresh"))
            continue
        if reason and reason.startswith("auth.json"):
            checks.append(check(home_id, FAIL, f"{worker_id}: {home}: {reason}", _codex_login(home)))
            continue
        # The repair names the inspected home and the inspected ledger, so it
        # cannot land on the default home or the default ledger by accident.
        checks.append(
            check(
                home_id,
                FAIL,
                f"{worker_id}: {home}: {reason}; the worker is protected, so the repair needs an override reason",
                ledger.cli(
                    "provision-codex-home",
                    "--actor-id", worker_id,
                    "--project-root", worker.get("project_root") or "<project-root>",
                    "--codex-home", home,
                    "--override-protected", "doctor: restore the missing codex home",
                ),
            )
        )
    for row in dead_credentials:
        lineage = str(row["lineage_key"])
        checks.append(
            check(
                f"runtime:codex:login:{lineage}",
                FAIL,
                f"expired login (refresh failed: {row['dead_reason']} since {row['dead_at']}); lineage {lineage}",
                _codex_login(Path(lineage).parent),
            )
        )
    return checks


def _codex_login(home: Path | str) -> str:
    return f"CODEX_HOME={shlex.quote(str(home))} codex login"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def check_monitor(
    snapshot: LedgerSnapshot,
    *,
    pid_alive: Callable[[int], bool] = _pid_alive,
    now: datetime | None = None,
    ledger: LedgerRef | None = None,
) -> dict:
    ledger = ledger or LedgerRef(paths.db_path(), False)
    humans = snapshot.humans()
    operator = os.environ.get(OPERATOR_ACTOR_ENV, "").strip()
    human = operator or (humans[0]["id"] if len(humans) == 1 else "<human-actor-id>")
    fix = ledger.monitor(human)
    heartbeat = snapshot.heartbeat
    if heartbeat is None or not heartbeat.get("last_pass_at"):
        if snapshot.dispatch_count == 0:
            return check("monitor", WARN, "no monitor heartbeat yet and nothing has been dispatched", fix)
        return check("monitor", FAIL, f"no monitor heartbeat; {snapshot.dispatch_count} dispatches recorded", fix)
    if not heartbeat_is_fresh(heartbeat, now=now or datetime.now(timezone.utc)):
        return check("monitor", FAIL, f"monitor heartbeat is stale (last pass {heartbeat['last_pass_at']})", fix)
    pid = heartbeat.get("pid")
    if not isinstance(pid, int) or pid <= 0 or not pid_alive(pid):
        return check("monitor", FAIL, f"monitor heartbeat is fresh (last pass {heartbeat['last_pass_at']}) but monitor pid {pid} is not running", fix)
    return check("monitor", OK, f"monitor pid {pid} running, last pass {heartbeat['last_pass_at']}")


def check_mcp(
    architects: list[dict],
    seats: dict,
    *,
    db_path: Path,
    db_explicit: bool,
    clients: list[str] | None = None,
    executable: str = sys.executable,
    claude_config: Path | None = None,
    codex_config: Path | None = None,
) -> list[dict]:
    """One ``mcp:<architect>:<client>`` entry per recorded (or requested) seat."""
    checks: list[dict] = []
    if not architects:
        return [check("mcp", SKIP, "no architect registered (see ledger)")]
    mcp_command = Path(executable).parent / "agent-comms-mcp"
    claude_config = claude_config or mcp_clients.claude_config_path()
    codex_config = codex_config or mcp_clients.codex_config_path()
    ledger = LedgerRef(db_path, db_explicit)
    for architect in architects:
        actor_id = architect["id"]
        project_root = architect.get("project_root") or ""
        recorded = seats.get(actor_id)
        if clients is not None:
            wanted = list(clients)
        elif recorded is None or (isinstance(recorded, list) and all(isinstance(item, str) for item in recorded)):
            wanted = list(recorded or [])
        else:
            checks.append(
                check(
                    f"mcp:{actor_id}",
                    FAIL,
                    f"seats entry for {actor_id} is {recorded!r}, not a list of client names",
                    f"set seats.{actor_id} in {paths.actors_config_path()} to a list such as [\"claude\", \"codex\"], or rerun {ledger.cli('setup')}",
                )
            )
            continue
        if not wanted:
            checks.append(check(f"mcp:{actor_id}", SKIP, f"no seats recorded for {actor_id}; run setup or add the entry by hand"))
            continue
        if not project_root:
            checks.append(check(f"mcp:{actor_id}", FAIL, f"{actor_id} has no project_root, so its seat cannot be located", f"re-register {actor_id} with a project_root: {ledger.cli('bootstrap')}"))
            continue
        expected = mcp_clients.server_argv(mcp_command, actor_id, db_path if db_explicit else None)
        wanted_binding = mcp_clients.binding(expected)
        for client in wanted:
            check_id = f"mcp:{actor_id}:{client}"
            if client not in mcp_clients.CLIENTS:
                checks.append(check(check_id, FAIL, f"unknown MCP client {client!r}", f"use one of: {', '.join(mcp_clients.CLIENTS)}"))
                continue
            if client == "claude":
                config = mcp_clients.read_claude_seat(claude_config, project_root)
            else:
                config = mcp_clients.read_codex_seat(codex_config)
            found = config.server() if config.status == "ok" else None
            if found is None:
                why = {
                    "missing": f"no {client} config at {config.path}",
                    "unparseable": f"{config.path}: {config.error}",
                    "no_project": f"{config.path} has no entry for {project_root}",
                }.get(config.status, f"{config.path} has no {mcp_clients.SERVER_NAME} server")
                checks.append(check(check_id, FAIL, f"{actor_id} has no {client} seat: {why}", mcp_clients.render_fix(client, project_root, expected, replace=False)))
                continue
            bound = mcp_clients.binding(found.args, found.env)
            problems = []
            if bound["malformed"]:
                problems.append(f"malformed arguments (repeated or valueless {', '.join(bound['malformed'])})")
            if bound["actor_id"] != actor_id:
                problems.append(f"bound to actor {bound['actor_id']!r}")
            if bound["db"] != wanted_binding["db"]:
                problems.append(f"bound to ledger {bound['db']!r}, expected {wanted_binding['db']!r}")
            command = found.command
            if command is None or Path(command).expanduser() != mcp_command:
                problems.append(f"runs {command!r}, expected {str(mcp_command)!r}")
            if problems:
                checks.append(check(check_id, FAIL, f"{actor_id} {client} seat in {config.path}: {'; '.join(problems)}", mcp_clients.render_fix(client, project_root, expected, replace=True)))
                continue
            checks.append(check(check_id, OK, f"{actor_id} {client} seat in {config.path} binds {actor_id}"))
    return checks


def check_admin_token(token_path: Path) -> dict:
    fix = f"(umask 077; head -c 32 /dev/urandom | xxd -p -c 64 > {shlex.quote(str(token_path))})"
    if not token_path.is_file():
        return check("admin_token", WARN, f"no admin token at {token_path} (needed for the operator dispatch path and the fake demo)", fix)
    if os.stat(token_path).st_mode & 0o077:
        return check("admin_token", WARN, f"{token_path} is not mode 600", sh("chmod", "600", token_path))
    return check("admin_token", OK, f"{token_path} present, mode 600")


# --- the whole report ------------------------------------------------------


def run_doctor(
    *,
    db_path: Path,
    db_explicit: bool,
    clients: list[str] | None = None,
    executable: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = subprocess.run,
    pid_alive: Callable[[int], bool] = _pid_alive,
    now: datetime | None = None,
) -> dict:
    executable = executable or sys.executable
    platform = current_platform()
    ledger = LedgerRef(db_path, db_explicit)
    checks: list[dict] = [check_install(executable), check_runtime_root(paths.runtime_root())]
    registry_check, config = check_registry(paths.actors_config_path(), ledger)
    checks.append(registry_check)
    ledger_check, snapshot = check_ledger(db_path, ledger)
    checks.append(ledger_check)
    runtimes = runtimes_in_use(snapshot, config)
    platform_check, pins = check_platform(platform, runtimes)
    checks.append(platform_check)
    if "claude" in runtimes:
        checks.append(check_claude(pins.get("claude"), run=run))
    else:
        checks.append(check("runtime:claude", SKIP, "no claude worker registered"))
    if "codex" in runtimes:
        checks.extend(
            check_codex(
                pins.get("codex"),
                snapshot.workers("codex") if snapshot else [],
                snapshot.dead_credentials if snapshot else [],
                which=which,
                run=run,
                now=now,
                ledger=ledger,
            )
        )
    else:
        checks.append(check("runtime:codex", SKIP, "no codex worker registered"))
    if snapshot is not None and snapshot.schema_version <= LEDGER_SCHEMA_VERSION:
        # A roster problem (several humans, no architect) fails the ledger
        # check but the ledger itself was read, so the checks that only need
        # its rows still run and report.
        checks.append(check_monitor(snapshot, pid_alive=pid_alive, now=now, ledger=ledger))
        seats = config.get("seats", {}) if isinstance(config, dict) and isinstance(config.get("seats"), dict) else {}
        checks.extend(
            check_mcp(
                snapshot.by_role("architect"),
                seats,
                db_path=db_path,
                db_explicit=db_explicit,
                clients=clients,
                executable=executable,
            )
        )
    else:
        checks.append(check("monitor", SKIP, "ledger unavailable (see ledger)"))
        checks.append(check("mcp", SKIP, "ledger unavailable (see ledger)"))
    checks.append(check_admin_token(paths.runtime_root() / "admin-token"))
    return report(platform, checks)
