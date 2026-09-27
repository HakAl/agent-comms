"""``agent-comms setup``: from a clean install to a working team in one command.

Setup takes a project root, the runtimes the user has and a team name. It
writes the actor registry (``~/.agent-comms/actors.json``), registers the
team in the ledger, provisions what the codex workers need, and binds the
architect seat in the user's own MCP clients by running ``claude mcp add``
and ``codex mcp add`` (the client CLIs are the supported writers of their
config files; see ``mcp_clients``). No file is edited by hand.

Nothing is written before the read-only preflight passes: a client seat
already bound to another actor (or to another ledger) is refused with the
holder and ``--replace-seat``; a team that already exists in the registry is
refused before the registry is touched. Every step is reported in the JSON
result, which ends with the doctor checks run in-process so one command says
what is still missing. ``ok`` reflects setup's own steps: it is false when a
client command could not be applied (that command is listed under
``mcp.pending`` with the reason and the exact line to run).

Prompts: with an ``ask`` function every omitted argument is asked for with
its default shown; without one (``--yes``, or no terminal) an omitted
required argument is refused naming the flag, and the rest take their
defaults. Tests drive setup with flags and an injected ``ask``.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from . import doctor, mcp_clients, paths
from .cli._helpers import bootstrap_store, load_actor_config
from .runtime_pins import PlatformNotCertified, claude_versions_dir, current_platform, pin_for
from .schema import ValidationError, validate_actor_id_for_kind
from .spawn import ALLOWED_RUNTIMES
from .store import Store

# The example registry's capabilities, so setup writes what a hand-copied
# config/actors.example.json would have said.
CAPABILITIES = {
    "architect": ["planning", "review"],
    "claude": ["implementation"],
    "codex": ["implementation", "review"],
    "fake": ["demo"],
}
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
Ask = Callable[[str, str], str]
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def new_ulid(now_ms: int | None = None) -> str:
    """A ULID-shaped id: 10 chars of Crockford base32 timestamp, 16 random.

    The example registry names its human this way; a human id only has to be
    opaque (it must not encode its kind), so the shape is a convention, not
    a requirement.
    """
    stamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    chars = []
    for _ in range(10):
        chars.append(CROCKFORD[stamp & 31])
        stamp >>= 5
    return "".join(reversed(chars)) + "".join(secrets.choice(CROCKFORD) for _ in range(16))


def team_from_path(project_root: str | os.PathLike[str]) -> str:
    """The project directory's name lowered to the agent-id alphabet."""
    return re.sub(r"[^a-z0-9]+", "-", Path(project_root).name.lower()).strip("-")


def parse_list(value: object) -> list[str] | None:
    """``None`` stays None (not given); a string splits on commas; blanks drop."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    else:
        items = [item.strip() for item in str(value).split(",")]
    return [item for item in items if item]


def detect_runtimes(which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """The native runtimes this machine appears to have; ``fake`` when none."""
    found = []
    try:
        pin = pin_for("claude", current_platform())
        if (claude_versions_dir() / pin.version).is_file():
            found.append("claude")
    except PlatformNotCertified:
        pass
    if which("codex"):
        found.append("codex")
    return found or ["fake"]


def console_ask(prompt: str, default: str) -> str:
    answer = input(f"{prompt} [{default}]: ").strip()
    return answer or default


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.new")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _seed_from_live_ledger(store: Store, db_path: Path) -> None:
    """Register the live ledger's non-worker actors in the throwaway store.

    A worker in the registry may name an owner that lives only in the live
    ledger (registered by hand, say); the live bootstrap accepts it, so the
    dry run must too. Best effort and read-only on the live ledger: when it
    is absent or unreadable the dry run starts from nothing, which is what
    the live bootstrap would start from as well.
    """
    if not db_path.exists():
        return
    try:
        snapshot = doctor.read_ledger(db_path)
    except (sqlite3.Error, ValueError, OSError):
        return
    for actor in snapshot.actors:
        try:
            if actor["kind"] != "agent":
                store.register_actor(actor["id"], actor["kind"], "seed")
            elif actor.get("role") != "worker":
                store.register_agent_actor(actor["id"], actor.get("team") or "seed", actor.get("role") or "architect", actor.get("project_root") or "/", [])
        except (ValidationError, sqlite3.Error):
            continue


def _dry_run_bootstrap(rendered: str, config_path: Path, db_path: Path) -> None:
    """Bootstrap the candidate registry into a throwaway ledger.

    Every refusal ``bootstrap_store`` would raise against the live ledger (an
    existing worker without an owner, a forbidden field, an owner that is not
    an architect, ...) is raised here first, so the live registry is never
    replaced by a document that then fails to register. The throwaway ledger
    starts from the live ledger's owners, so a worker whose owner is already
    registered is valid here exactly when it is valid there. Nothing outside
    a temporary directory is touched: no custody home, no client, no ledger.
    """
    with tempfile.TemporaryDirectory(prefix="agent-comms-setup-") as temp_dir:
        candidate = Path(temp_dir) / "actors.json"
        candidate.write_text(rendered)
        store = Store(Path(temp_dir) / "dry-run.sqlite")
        _seed_from_live_ledger(store, db_path)
        try:
            bootstrap_store(store, candidate, codex_custody=False)
        except (ValidationError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as exc:
            raise ValidationError(f"the registry would not bootstrap: {exc}; fix {config_path} before adding a team") from exc


def _read_seat(client: str, project_root: Path) -> mcp_clients.ClientConfig:
    if client == "claude":
        return mcp_clients.read_claude_seat(mcp_clients.claude_config_path(), project_root)
    return mcp_clients.read_codex_seat(mcp_clients.codex_config_path())


def run_setup(
    store: Store,
    *,
    db_path: Path,
    db_explicit: bool,
    project_root: str | None,
    team: str | None = None,
    runtimes: list[str] | None = None,
    clients: list[str] | None = None,
    human_name: str | None = None,
    human_id: str | None = None,
    replace_seat: bool = False,
    ask: Ask | None = None,
    executable: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = subprocess.run,
    now=None,
) -> dict:
    executable = executable or sys.executable
    # The ledger path is persisted in client configs that run from other
    # directories, so it is absolute from here on.
    db_path = Path(os.path.abspath(os.path.expanduser(str(db_path))))

    # --- arguments (asked for when omitted and an ``ask`` is available) ---
    if project_root is None:
        if ask is None:
            raise ValidationError("pass --project-root <directory> (no terminal to ask on)")
        project_root = ask("Project root", os.getcwd())
    root = Path(project_root).expanduser()
    if not root.is_dir():
        raise ValidationError(f"--project-root {project_root} is not a directory")
    root = root.resolve()
    if team is None:
        default_team = team_from_path(root)
        team = ask("Team name", default_team) if ask is not None else default_team
    team = team.strip()
    if not team:
        raise ValidationError(f"pass --team <name>: the project directory name {root.name!r} gives no usable team name")
    if runtimes is None:
        detected = detect_runtimes(which)
        if ask is None:
            raise ValidationError(
                f"pass --runtimes <comma-separated subset of {','.join(ALLOWED_RUNTIMES)}> "
                f"(detected on this machine: {','.join(detected)})"
            )
        runtimes = parse_list(ask(f"Runtimes to use, comma-separated from {', '.join(ALLOWED_RUNTIMES)}", ",".join(detected)))
    runtimes = list(dict.fromkeys(runtimes or []))
    unknown = [runtime for runtime in runtimes if runtime not in ALLOWED_RUNTIMES]
    if unknown or not runtimes:
        raise ValidationError(f"--runtimes must name at least one of {', '.join(ALLOWED_RUNTIMES)}; got {','.join(runtimes) or '(none)'}")
    if clients is None:
        default_clients = [runtime for runtime in runtimes if runtime in mcp_clients.CLIENTS]
        if ask is not None:
            # A blank answer takes the default, so the explicit "no clients" answer is the word none.
            clients = parse_list(ask(f"MCP clients to bind the architect seat in ({', '.join(mcp_clients.CLIENTS)}; none for no seat)", ",".join(default_clients) or "none"))
        else:
            clients = default_clients
    clients = list(dict.fromkeys(clients or []))
    if [client.lower() for client in clients] == ["none"]:
        clients = []
    unknown = [client for client in clients if client not in mcp_clients.CLIENTS]
    if unknown:
        raise ValidationError(f"--clients must be a subset of {', '.join(mcp_clients.CLIENTS)}; unknown: {', '.join(unknown)}")
    architect = f"{team}-architect"
    workers = {runtime: f"{team}-{runtime}-worker" for runtime in runtimes}
    for actor_id in (architect, *workers.values()):
        try:
            validate_actor_id_for_kind(actor_id, "agent")
        except ValidationError as exc:
            raise ValidationError(f"--team {team!r} makes an invalid actor id {actor_id!r}: {exc}") from exc

    # --- read-only preflight: the registry, the human, the seats -----------
    config_path = paths.actors_config_path()
    legacy = config_path.with_name("agents.json")
    if config_path.exists():
        try:
            document = json.loads(config_path.read_text())
        except (OSError, ValueError) as exc:
            raise ValidationError(f"{config_path} cannot be read as JSON: {exc}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("actors", {}), dict):
            raise ValidationError(f"{config_path} has no actors object; fix or remove it")
        for actor_id, entry in document.get("actors", {}).items():
            # What bootstrap_store will refuse; caught here so the registry is not written first.
            if not isinstance(entry, dict):
                raise ValidationError(f"{config_path}: actor {actor_id} is not an object; fix it before adding a team")
            if entry.get("kind", "agent") != "agent" and not entry.get("display_name"):
                raise ValidationError(f"{config_path}: {entry.get('kind')} actor {actor_id} has no display_name; fix it before adding a team")
        try:
            # What bootstrap will load: an entry that does not expand fails here, before the write.
            load_actor_config(config_path)
        except (ValidationError, ValueError) as exc:
            raise ValidationError(f"{config_path} does not load as it is: {exc}; fix it before adding a team") from exc
    elif legacy.exists():
        raise ValidationError(f"legacy registry {legacy} present; run agent-comms bootstrap to migrate it to {config_path} first")
    else:
        document = {"actors": {}}
    actors = document.setdefault("actors", {})
    same_team = sorted(actor_id for actor_id, entry in actors.items() if isinstance(entry, dict) and entry.get("team") == team)
    clash = sorted({architect, *workers.values()} & set(actors))
    if same_team or clash:
        raise ValidationError(
            f"team {team!r} already exists in {config_path} ({', '.join(same_team or clash)}); "
            "choose another --team, or rerun agent-comms bootstrap to re-register it"
        )
    humans = [actor_id for actor_id, entry in actors.items() if isinstance(entry, dict) and entry.get("kind") == "human"]
    if humans:
        # The registered human stays the operator; a second human is never added, so nothing is asked.
        human = humans[0]
        reused = True
    else:
        if human_name is None:
            default_name = getpass.getuser()
            human_name = ask("Your display name", default_name) if ask is not None else default_name
        human_name = human_name.strip()
        if not human_name:
            raise ValidationError("--human must not be empty")
        if human_id is None:
            generated = new_ulid()
            human_id = ask("Your actor id (opaque; the default is a fresh ULID)", generated) if ask is not None else generated
        human_id = human_id.strip()
        validate_actor_id_for_kind(human_id, "human")
        if human_id in actors:
            raise ValidationError(f"--human-id {human_id!r} is already an actor in {config_path} ({actors[human_id].get('kind', 'agent')}); choose another id")
        if human_id in {architect, *workers.values()}:
            raise ValidationError(f"--human-id {human_id!r} collides with the new team's actor of that name; choose another id")
        human = human_id
        reused = False

    try:
        mcp_command = paths.mcp_command()
    except FileNotFoundError as exc:
        raise ValidationError(str(exc)) from exc
    argv = mcp_clients.server_argv(mcp_command, architect, db_path if db_explicit else None)
    wanted = mcp_clients.binding(argv)
    existing: dict[str, bool | None] = {}  # client -> True (other binding), False (absent), None (already ours)
    holders: dict[str, str | None] = {}
    taken = []
    for client in clients:
        config = _read_seat(client, root)
        found = config.server() if config.status == "ok" else None
        if found is None:
            existing[client] = False
            continue
        bound = mcp_clients.binding(found.args, found.env)
        problems = []
        if bound["malformed"]:
            problems.append(f"malformed arguments (repeated or valueless {', '.join(bound['malformed'])})")
        if bound["actor_id"] != architect:
            problems.append(f"bound to actor {bound['actor_id']!r}")
        elif not mcp_clients.same_ledger(bound["db"], wanted["db"]):
            problems.append(f"binds {architect} to ledger {bound['db']!r}, expected {wanted['db']!r}")
        if found.command is None or Path(found.command).expanduser() != mcp_command:
            problems.append(f"runs {found.command!r}, expected {str(mcp_command)!r}")
        if not problems:
            existing[client] = None
            continue
        existing[client] = True
        holders[client] = bound["actor_id"]
        taken.append(f"{client} ({config.path}): {'; '.join(problems)}")
    if taken and not replace_seat:
        raise ValidationError(
            f"seat already taken: {'; '.join(taken)}; rerun with --replace-seat to hand it over to {architect}, "
            "or choose other --clients"
        )

    # --- registry ------------------------------------------------------------
    if not reused:
        actors[human] = {"kind": "human", "display_name": human_name}
    actors[architect] = {
        "kind": "agent",
        "display_name": architect,
        "team": team,
        "role": "architect",
        "project_root": str(root),
        "capabilities": list(CAPABILITIES["architect"]),
    }
    for runtime, worker in workers.items():
        actors[worker] = {
            "kind": "agent",
            "display_name": worker,
            "team": team,
            "role": "worker",
            "owner": architect,
            "runtime": runtime,
            "project_root": str(root),
            "capabilities": list(CAPABILITIES[runtime]),
        }
    seats = document.get("seats")
    if not isinstance(seats, dict):
        seats = document["seats"] = {}
    for client, holder in holders.items():
        # The seat changes hands: doctor stops expecting it for the previous architect.
        if holder in seats and isinstance(seats[holder], list):
            seats[holder] = [item for item in seats[holder] if item != client]
    seats[architect] = list(clients)
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    _dry_run_bootstrap(rendered, config_path, db_path)
    paths.runtime_root().mkdir(parents=True, exist_ok=True)
    _write_atomic(config_path, rendered)

    # --- ledger and worker homes ------------------------------------------
    bootstrap_store(store, config_path, codex_custody=True)
    codex_homes = [str(paths.provisioned_codex_home(workers["codex"]))] if "codex" in workers else []

    claude_custody = None
    if "claude" in workers:
        try:
            pin = pin_for("claude", current_platform())
            binary = claude_versions_dir() / pin.version
            claude_custody = {"version": pin.version, "path": str(binary), "present": binary.is_file()}
        except PlatformNotCertified as exc:
            claude_custody = {"version": None, "path": None, "present": False, "detail": str(exc)}

    # --- the seats, through the client CLIs --------------------------------
    applied: list[dict] = []
    pending: list[dict] = []
    for client in clients:
        state = existing[client]
        if state is None:
            applied.append({"client": client, "commands": [], "detail": f"already bound to {architect}"})
            continue
        if client == "claude":
            commands = ([mcp_clients.claude_remove_command()] if state else []) + [mcp_clients.claude_add_command(argv)]
            cwd: str | None = str(root)
        else:
            commands = [mcp_clients.codex_add_command(argv)]
            cwd = None
        fix = mcp_clients.render_fix(client, root, argv, replace=bool(state))
        if which(client) is None:
            pending.append({"client": client, "reason": f"{client} is not on PATH", "fix": fix})
            continue
        failure = None
        done: list[list[str]] = []
        for command in commands:
            try:
                result = run(command, cwd=cwd, text=True, capture_output=True, check=False, timeout=60)
            except (OSError, subprocess.SubprocessError) as exc:
                failure = f"{shlex.join(command)}: {exc}"
                break
            if result.returncode != 0:
                tail = (result.stderr or result.stdout or "").strip()[-400:]
                failure = f"{shlex.join(command)} exited {result.returncode}: {tail}"
                break
            done.append(list(command))
        if failure is not None:
            # A remove that already succeeded is not repeated: the repair is then the add alone.
            removed = any(command[1:3] == ["mcp", "remove"] for command in done)
            pending.append({"client": client, "reason": failure, "fix": mcp_clients.render_fix(client, root, argv, replace=bool(state) and not removed)})
        else:
            applied.append({"client": client, "commands": [list(command) for command in commands]})

    # --- what is still missing: the doctor checks, in-process ---------------
    report = doctor.run_doctor(db_path=db_path, db_explicit=db_explicit, executable=executable, which=which, run=run, now=now)
    ledger = doctor.LedgerRef(db_path, db_explicit)
    steps = [ledger.cli("doctor"), *(item["fix"] for item in pending), *report["fixes"]]
    return {
        "ok": not pending,
        "registry": str(config_path),
        "team": team,
        "project_root": str(root),
        "runtimes": runtimes,
        "clients": clients,
        "actors": [architect, *workers.values()],
        "human": {"id": human, "display_name": actors[human].get("display_name", human), "reused": reused},
        "codex_homes": codex_homes,
        "claude_custody": claude_custody,
        "mcp": {"applied": applied, "pending": pending},
        "doctor": {"ok": report["ok"], "fixes": report["fixes"]},
        "next": list(dict.fromkeys(steps)),
    }
