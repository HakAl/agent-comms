"""``agent-comms setup``: from a clean install to a working team in one command.

Setup takes a project root, the runtimes the user has and a team name. It
writes the actor registry (``~/.agent-comms/actors.json``), registers the
team in the ledger, provisions what the codex workers need (their homes and,
best effort, the custody copy of the pinned Codex they run), and binds the
architect seat in the user's own MCP clients through ``mcp_clients``: Claude
through ``claude mcp add``, Codex in the project's ``.codex/config.toml``.
``agent-comms bind-seat`` binds one seat of an existing team the same way,
and is the fix setup and doctor print for any seat. No file is edited by hand.

Nothing is written before the read-only preflight passes: a client seat
already bound to another actor (or to another ledger) is refused with the
holder and ``--replace-seat``; a team that already exists in the registry is
refused before the registry is touched. Every step is reported in the JSON
result, which ends with the doctor checks run in-process so one command says
what is still missing. ``ok`` reflects setup's own steps: it is false when a
seat could not be bound (listed under ``mcp.pending`` with the reason and
the ``bind-seat`` line to run).

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
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from . import doctor, mcp_clients, paths, supervisor
from .cli._helpers import bootstrap_store, load_actor_config
from .custody import fill_codex_custody
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


def seat_target(architect: str, project_root: Path, *, db_path: Path, db_explicit: bool) -> mcp_clients.SeatTarget:
    try:
        mcp_command = paths.mcp_command()
    except FileNotFoundError as exc:
        raise ValidationError(str(exc)) from exc
    return mcp_clients.SeatTarget(architect, project_root, str(mcp_command), db_path, db_explicit, paths.home_override())


def seat_fix(ledger: doctor.LedgerRef, architect: str, client: str, *, replace: bool) -> str:
    """The one line that binds ``client``'s seat for ``architect``."""
    return ledger.cli("bind-seat", architect, "--client", client, *(["--replace-seat"] if replace else []))


def inspect_seats(target: mcp_clients.SeatTarget, clients: list[str], *, replace_seat: bool, which, run) -> dict[str, mcp_clients.SeatState]:
    """Each client's seat as found; refuses before anything is written when
    one cannot be written, or is taken and ``replace_seat`` is not given."""
    states = {client: mcp_clients.SEATS[client].inspect(target, which=which, run=run) for client in clients}
    refused = [f"{client} ({state.where}): {'; '.join(state.problems)}" for client, state in states.items() if state.status == "refused"]
    if refused:
        raise ValidationError(f"seat cannot be written: {'; '.join(refused)}")
    taken = [f"{client} ({state.where}): {'; '.join(state.problems)}" for client, state in states.items() if state.status == "taken"]
    if taken and not replace_seat:
        raise ValidationError(
            f"seat already taken: {'; '.join(taken)}; rerun with --replace-seat to hand it over to {target.actor_id}, "
            "or choose other --clients"
        )
    return states


def record_seat(document: dict, architect: str, client: str, holder: str | None) -> None:
    """Note in the registry that ``architect`` holds ``client``'s seat, and
    that ``holder`` (the previous one) no longer does, so doctor checks
    exactly the seats that exist."""
    seats = document.get("seats")
    if not isinstance(seats, dict):
        seats = document["seats"] = {}
    if holder and holder != architect and isinstance(seats.get(holder), list):
        seats[holder] = [item for item in seats[holder] if item != client]
    held = seats.get(architect)
    held = list(held) if isinstance(held, list) else []
    if client not in held:
        held.append(client)
    seats[architect] = held


def apply_seats(
    target: mcp_clients.SeatTarget,
    states: dict[str, mcp_clients.SeatState],
    ledger: doctor.LedgerRef,
    *,
    which,
    run,
) -> tuple[list[dict], list[dict]]:
    """Bind every seat not already ours: (applied, pending)."""
    applied: list[dict] = []
    pending: list[dict] = []
    for client, state in states.items():
        seat = mcp_clients.SEATS[client]
        if state.status == "bound":
            applied.append({"client": client, "commands": [], "notes": [], "detail": f"already bound to {target.actor_id}"})
            continue
        result = seat.apply(target, state, which=which, run=run)
        if result.failure is None:
            applied.append({"client": client, "commands": result.commands, "notes": result.notes})
            continue
        # What is left to do decides the flag: a remove that already
        # succeeded leaves an absent seat, and the repair is the add alone.
        now = seat.inspect(target, which=which, run=run)
        fix = seat_fix(ledger, target.actor_id, client, replace=now.status == "taken")
        pending.append({"client": client, "reason": result.failure, "notes": result.notes, "fix": fix})
    return applied, pending


def load_registry(config_path: Path) -> dict:
    try:
        document = json.loads(config_path.read_text())
    except FileNotFoundError as exc:
        raise ValidationError(f"no registry at {config_path}; run agent-comms setup first") from exc
    except (OSError, ValueError) as exc:
        raise ValidationError(f"{config_path} cannot be read as JSON: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("actors", {}), dict):
        raise ValidationError(f"{config_path} has no actors object; fix or remove it")
    return document


def bind_seat(
    *,
    db_path: Path,
    db_explicit: bool,
    architect: str,
    client: str,
    replace_seat: bool = False,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = subprocess.run,
) -> dict:
    """``agent-comms bind-seat``: bind one client's seat for a registered
    architect and record it in the registry, as setup does."""
    db_path = Path(os.path.abspath(os.path.expanduser(str(db_path))))
    if client not in mcp_clients.SEATS:
        raise ValidationError(f"--client must be one of {', '.join(mcp_clients.CLIENTS)}; got {client!r}")
    config_path = paths.actors_config_path()
    document = load_registry(config_path)
    entry = document.get("actors", {}).get(architect)
    if not isinstance(entry, dict) or entry.get("role") != "architect":
        raise ValidationError(f"{architect} is not an architect in {config_path}")
    root = Path(str(entry.get("project_root") or ""))
    if not entry.get("project_root") or not root.is_dir():
        raise ValidationError(f"{architect}'s project_root {entry.get('project_root')!r} is not a directory")
    seats = document.get("seats", {})
    if not isinstance(seats, dict) or not isinstance(seats.get(architect, []), list):
        raise ValidationError(f"seats.{architect} in {config_path} is not a list of client names; fix it first")
    target = seat_target(architect, root, db_path=db_path, db_explicit=db_explicit)
    states = inspect_seats(target, [client], replace_seat=replace_seat, which=which, run=run)
    ledger = doctor.LedgerRef(db_path, db_explicit)
    applied, pending = apply_seats(target, states, ledger, which=which, run=run)
    record_seat(document, architect, client, states[client].holder)
    _write_atomic(config_path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return {
        "ok": not pending,
        "architect": architect,
        "client": client,
        "project_root": str(root),
        "registry": str(config_path),
        "runtime_root": str(paths.runtime_root()),
        "mcp": {"applied": applied, "pending": pending},
        "next": [item["fix"] for item in pending],
    }


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
    too_long = supervisor.control_root_problem()
    if too_long:
        raise ValidationError(too_long)

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

    target = seat_target(architect, root, db_path=db_path, db_explicit=db_explicit)
    states = inspect_seats(target, clients, replace_seat=replace_seat, which=which, run=run)

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
    if not isinstance(document.get("seats"), dict):
        document["seats"] = {}
    document["seats"][architect] = []
    for client, state in states.items():
        record_seat(document, architect, client, state.holder)
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

    # Best effort: a missing custody copy fails dispatch and doctor names
    # the fill command, so setup records why and carries on.
    codex_custody = None
    if "codex" in workers:
        try:
            filled = fill_codex_custody(which=which, run=run)
            codex_custody = {"present": True, "path": filled["path"], "status": filled["status"]}
        except (ValidationError, PlatformNotCertified, OSError, ValueError) as exc:
            codex_custody = {"present": False, "reason": str(exc)}

    # --- the seats ---------------------------------------------------------
    ledger = doctor.LedgerRef(db_path, db_explicit)
    applied, pending = apply_seats(target, states, ledger, which=which, run=run)

    # --- what is still missing: the doctor checks, in-process ---------------
    report = doctor.run_doctor(db_path=db_path, db_explicit=db_explicit, executable=executable, which=which, run=run, now=now)
    steps = [ledger.cli("doctor"), *(item["fix"] for item in pending), *report["fixes"]]
    return {
        "ok": not pending,
        "registry": str(config_path),
        "team": team,
        "project_root": str(root),
        "runtime_root": str(paths.runtime_root()),
        "runtimes": runtimes,
        "clients": clients,
        "actors": [architect, *workers.values()],
        "human": {"id": human, "display_name": actors[human].get("display_name", human), "reused": reused},
        "codex_homes": codex_homes,
        "claude_custody": claude_custody,
        "codex_custody": codex_custody,
        "mcp": {"applied": applied, "pending": pending},
        "doctor": {"ok": report["ok"], "fixes": report["fixes"]},
        "next": list(dict.fromkeys(steps)),
    }
