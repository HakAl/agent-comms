"""The architect seat in the user's own MCP clients: readers and commands.

Claude Code keeps a local-scope MCP entry per project directory in
``~/.claude.json`` (``projects[<cwd>].mcpServers``); Codex keeps one entry
per user in ``~/.codex/config.toml`` (``mcp_servers.<name>``). Nothing here
writes those files: the client CLIs are the supported writers, and this
module renders the exact ``claude mcp add`` / ``codex mcp add`` argv that
``agent-comms setup`` runs and ``agent-comms doctor`` prescribes, so the fix
a user is told to run is the command setup would have run.
"""

from __future__ import annotations

import json
import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SERVER_NAME = "agent-comms"
CLAUDE_CONFIG_ENV = "AGENT_COMMS_CLAUDE_CONFIG"
CLIENTS = ("claude", "codex")


@dataclass(frozen=True)
class ServerEntry:
    """One MCP server as a client config records it."""

    name: str
    command: str | None
    args: list
    env: dict


@dataclass
class ClientConfig:
    """What a client's config says about the servers in one seat scope.

    ``status`` is ``ok`` when the scope was read, ``missing`` when the file
    does not exist, ``unparseable`` when it could not be read or parsed and
    ``no_project`` when a Claude config has no entry for the project
    directory. ``entries`` lists every server in the scope, in file order; a
    project keyed both by its path and its realpath contributes both entries.
    """

    path: Path
    status: str
    error: str | None = None
    entries: list[ServerEntry] = field(default_factory=list)

    @property
    def servers(self) -> list[tuple[str, list]]:
        return [(entry.name, entry.args) for entry in self.entries]

    def server(self, name: str = SERVER_NAME) -> ServerEntry | None:
        """The first server named ``name``, else None."""
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None


def _entry(name: str, server: object) -> ServerEntry:
    if not isinstance(server, dict):
        return ServerEntry(str(name), None, [], {})
    args = server.get("args", [])
    env = server.get("env", {})
    command = server.get("command")
    return ServerEntry(
        str(name),
        str(command) if command is not None else None,
        list(args) if isinstance(args, list) else [],
        dict(env) if isinstance(env, dict) else {},
    )


def claude_config_path() -> Path:
    return Path(os.environ.get(CLAUDE_CONFIG_ENV, "~/.claude.json")).expanduser()


def codex_config_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    root = Path(home).expanduser() if home else Path.home() / ".codex"
    return root / "config.toml"


def read_claude_seat(config_path: Path, project_root: str | os.PathLike[str]) -> ClientConfig:
    """The local-scope servers Claude Code holds for ``project_root``."""
    try:
        data = json.loads(config_path.read_text())
    except FileNotFoundError:
        return ClientConfig(config_path, "missing")
    except (OSError, ValueError) as exc:
        # ValueError covers JSONDecodeError and a UnicodeDecodeError from read_text().
        return ClientConfig(config_path, "unparseable", str(exc))
    root = str(project_root)
    keys = {root, os.path.realpath(root)}
    projects = data.get("projects", {}) if isinstance(data, dict) else {}
    entries = [projects[key] for key in sorted(keys) if isinstance(projects, dict) and key in projects]
    if not entries:
        return ClientConfig(config_path, "no_project", f"no project entry for {sorted(keys)!r}")
    config = ClientConfig(config_path, "ok")
    for entry in entries:
        servers = entry.get("mcpServers", {}) if isinstance(entry, dict) else {}
        if not isinstance(servers, dict):
            continue
        for name, server in servers.items():
            config.entries.append(_entry(name, server))
    return config


def read_codex_seat(config_path: Path) -> ClientConfig:
    """The servers Codex holds for the user (``mcp_servers`` in config.toml)."""
    try:
        data = tomllib.loads(config_path.read_text())
    except FileNotFoundError:
        return ClientConfig(config_path, "missing")
    except (OSError, ValueError) as exc:
        # ValueError covers TOMLDecodeError and a UnicodeDecodeError from read_text().
        return ClientConfig(config_path, "unparseable", str(exc))
    config = ClientConfig(config_path, "ok")
    servers = data.get("mcp_servers", {})
    if isinstance(servers, dict):
        for name, server in servers.items():
            config.entries.append(_entry(name, server))
    return config


def flag_values(args: list, flag: str) -> tuple[list[str], bool]:
    """Every value of ``flag`` in ``args``, in both forms argparse accepts
    (``--flag value`` and ``--flag=value``), and whether the list is
    malformed: a repeated flag, or a flag with no value."""
    found: list[str] = []
    malformed = False
    prefix = flag + "="
    for index, value in enumerate(args):
        text = str(value)
        if text == flag:
            if index + 1 >= len(args):
                malformed = True
            else:
                found.append(str(args[index + 1]))
        elif text.startswith(prefix):
            found.append(text[len(prefix):])
    if len(found) > 1:
        malformed = True
    return found, malformed


def actor_ids_in(args: list) -> tuple[list[str], bool]:
    """Every ``--actor-id`` value in ``args`` and whether the list is malformed.

    Malformed means a repeated flag or a flag with no value; the seat
    launcher refuses both rather than guessing.
    """
    return flag_values(args, "--actor-id")


DB_ENV = "AGENT_COMMS_DB"


def binding(args: list, env: dict | None = None) -> dict[str, object]:
    """The identity a server argv binds: its ``--actor-id`` and the ledger.

    The ledger is ``--db`` when given, else the entry's ``AGENT_COMMS_DB``
    environment, else None (the default ledger), which is what the server
    itself resolves. ``malformed`` lists the flags that are repeated or
    have no value; a malformed argv binds nothing reliably.
    """
    actor_ids, actor_malformed = flag_values(args, "--actor-id")
    dbs, db_malformed = flag_values(args, "--db")
    malformed = [flag for flag, bad in (("--actor-id", actor_malformed), ("--db", db_malformed)) if bad]
    db: str | None = dbs[0] if dbs and not db_malformed else None
    if db is None and not db_malformed and env and env.get(DB_ENV):
        db = str(env[DB_ENV])
    return {
        "actor_id": actor_ids[0] if actor_ids and not actor_malformed else None,
        "db": db,
        "malformed": malformed,
    }


def server_argv(mcp_command: str | os.PathLike[str], actor_id: str, db_path: str | os.PathLike[str] | None = None) -> list[str]:
    """The argv a client runs for the seat: the server, its actor and, for an
    explicit ledger, the ledger path (the server opens the default one
    otherwise, where the actor may not exist)."""
    argv = [str(mcp_command), "--actor-id", actor_id]
    if db_path is not None:
        argv += ["--db", str(db_path)]
    return argv


def claude_add_command(argv: list[str]) -> list[str]:
    return ["claude", "mcp", "add", SERVER_NAME, "--scope", "local", "--", *argv]


def claude_remove_command() -> list[str]:
    return ["claude", "mcp", "remove", SERVER_NAME, "--scope", "local"]


def codex_add_command(argv: list[str]) -> list[str]:
    return ["codex", "mcp", "add", SERVER_NAME, "--", *argv]


def render_fix(client: str, project_root: str | os.PathLike[str], argv: list[str], *, replace: bool) -> str:
    """One shell line that binds the seat in ``client``.

    Claude's local scope is keyed by the working directory, so the line
    changes into the project first; a wrong binding is removed before it is
    added again because ``claude mcp add`` refuses duplicates. Codex's
    ``add`` overwrites, so it never needs a remove.
    """
    if client == "claude":
        parts = [f"cd {shlex.quote(str(project_root))}"]
        if replace:
            parts.append(shlex.join(claude_remove_command()))
        parts.append(shlex.join(claude_add_command(argv)))
        return " && ".join(parts)
    if client == "codex":
        return shlex.join(codex_add_command(argv))
    raise ValueError(f"unknown MCP client {client!r}; known: {', '.join(CLIENTS)}")
