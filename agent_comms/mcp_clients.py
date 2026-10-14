"""The architect seat in the user's own MCP clients: one implementation per
client, behind one interface that setup, ``bind-seat`` and doctor use.

Claude Code keeps a local-scope MCP entry per project directory in
``~/.claude.json`` (``projects[<cwd>].mcpServers``); its CLI is the
supported writer, so the Claude seat runs ``claude mcp add/remove``. Codex
loads ``<project>/.codex/config.toml`` in a trusted project, and only that
file is per directory, so the Codex seat writes our server's tables there
(every other table in the file is preserved and checked unchanged) and
keeps the file out of commits through the exclude file git reports. Codex
merges a server's fields across config layers, so the project entry pins
every field that matters and doctor checks the merged result
(``codex_binding``).

Nothing here decides what to print as a fix: every seat repair is
``agent-comms bind-seat``, rendered by the caller.
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import codex_binding
from .codex_binding import SERVER_NAME

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Which = Callable[[str], "str | None"]
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
HOME_ENV = "AGENT_COMMS_HOME"


def binding(args: list, env: dict | None = None) -> dict[str, object]:
    """The identity a server argv binds: its ``--actor-id``, ledger and home.

    The ledger is ``--db`` when given, else the entry's ``AGENT_COMMS_DB``
    environment, else None (the default ledger), which is what the server
    itself resolves. The home (the install's runtime root) is ``--home``,
    else the entry's ``AGENT_COMMS_HOME``, else None (``~/.agent-comms``).
    ``malformed`` lists the flags that are repeated or have no value; a
    malformed argv binds nothing reliably.
    """
    actor_ids, actor_malformed = flag_values(args, "--actor-id")
    dbs, db_malformed = flag_values(args, "--db")
    homes, home_malformed = flag_values(args, "--home")
    malformed = [
        flag
        for flag, bad in (("--actor-id", actor_malformed), ("--db", db_malformed), ("--home", home_malformed))
        if bad
    ]
    db: str | None = dbs[0] if dbs and not db_malformed else None
    if db is None and not db_malformed and env and env.get(DB_ENV):
        db = str(env[DB_ENV])
    home: str | None = homes[0] if homes and not home_malformed else None
    if home is None and not home_malformed and env and env.get(HOME_ENV):
        home = str(env[HOME_ENV])
    return {
        "actor_id": actor_ids[0] if actor_ids and not actor_malformed else None,
        "db": db,
        "home": home,
        "malformed": malformed,
    }


def same_ledger(stored: str | None, expected: str | None) -> bool:
    """Whether a persisted ``--db`` binding names the expected ledger.

    ``None`` is the default ledger and only matches ``None``. A stored path
    is taken as written, since the client execs the server without a shell:
    a relative path resolves from the client's own working directory and a
    ``~`` is never expanded, so neither matches. Two absolute spellings of
    one file (through a symlinked directory, say) match.
    """
    if stored is None or expected is None:
        return stored == expected
    stored = str(stored)
    if not os.path.isabs(stored):
        return False
    return os.path.realpath(stored) == os.path.realpath(os.path.expanduser(str(expected)))


def same_home(stored: str | None, expected: str | os.PathLike[str] | None) -> bool:
    """Whether a persisted ``--home`` binding names the expected install.

    ``None`` is the default root and only matches ``None``; otherwise as
    ``same_ledger``: absolute spellings of one directory match.
    """
    return same_ledger(stored, None if expected is None else str(expected))


def same_command(stored: str | None, expected: str | os.PathLike[str]) -> bool:
    """Whether a persisted server command is the expected console script.

    The client execs the command as written, so a bare name or a relative
    path (resolved from the client's PATH or working directory) never
    matches. Two absolute spellings of one file match: an installed package
    puts a symlink to the script on PATH (``uv tool install``), and a seat
    added by hand with that path runs the same server.
    """
    if stored is None:
        return False
    stored = str(stored)
    if not os.path.isabs(stored):
        return False
    return os.path.realpath(stored) == os.path.realpath(os.path.expanduser(str(expected)))


def server_argv(
    mcp_command: str | os.PathLike[str],
    actor_id: str,
    db_path: str | os.PathLike[str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> list[str]:
    """The argv a client runs for the seat: the server, its actor, for an
    explicit ledger the ledger path (the server opens the default one
    otherwise, where the actor may not exist) and, for an install with its
    own ``AGENT_COMMS_HOME``, that home. Both travel in argv, never only in
    env: Codex merges env tables across config layers but replaces argv."""
    argv = [str(mcp_command), "--actor-id", actor_id]
    if db_path is not None:
        argv += ["--db", str(db_path)]
    if home is not None:
        argv += ["--home", str(home)]
    return argv


def claude_add_command(argv: list[str]) -> list[str]:
    return ["claude", "mcp", "add", SERVER_NAME, "--scope", "local", "--", *argv]


def claude_remove_command() -> list[str]:
    return ["claude", "mcp", "remove", SERVER_NAME, "--scope", "local"]


# --- the seat interface ------------------------------------------------------


class SeatRefused(ValueError):
    """A seat that cannot be written as it stands, even with --replace-seat."""


@dataclass(frozen=True)
class SeatTarget:
    """Who the seat binds and where: the inputs every client derives from."""

    actor_id: str
    project_root: Path
    mcp_command: str
    db_path: Path
    db_explicit: bool
    home: Path | None


@dataclass
class SeatState:
    """A seat as found, before anything is written.

    ``status`` is ``bound`` (ours, left alone), ``absent``, ``taken`` (a
    different binding; ``holder`` is the actor it binds, ``problems`` says
    how it differs) or ``refused`` (cannot be written; ``problems`` says why).
    """

    status: str
    where: Path
    holder: str | None = None
    problems: list[str] = field(default_factory=list)


@dataclass
class SeatResult:
    """What applying a seat did: ``failure`` is None on success."""

    commands: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    failure: str | None = None


@dataclass(frozen=True)
class Finding:
    """One doctor finding for a seat: ``suffix`` extends the check id; ``fix``
    is ``"rebind"`` or ``"replace"`` for the bind-seat line, else literal."""

    suffix: str
    status: str
    detail: str
    fix: str | None = None


def binding_problems(found: ServerEntry, target: SeatTarget, expected_db: str | None) -> list[str]:
    """How a stored entry's identity differs from the target's."""
    bound = binding(found.args, found.env)
    problems = []
    if bound["malformed"]:
        problems.append(f"malformed arguments (repeated or valueless {', '.join(bound['malformed'])})")
    if bound["actor_id"] != target.actor_id:
        problems.append(f"bound to actor {bound['actor_id']!r}")
    if not same_ledger(bound["db"], expected_db):
        problems.append(f"bound to ledger {bound['db']!r}, expected {expected_db!r}")
    expected_home = None if target.home is None else str(target.home)
    if not same_home(bound["home"], expected_home):
        problems.append(f"bound to home {bound['home']!r}, expected {expected_home!r}")
    if not same_command(found.command, target.mcp_command):
        problems.append(f"runs {found.command!r}, expected {target.mcp_command!r}")
    return problems


def _holder(found: ServerEntry) -> str | None:
    return binding(found.args, found.env)["actor_id"]  # type: ignore[return-value]


def _run_commands(run: Runner, commands: list[list[str]], cwd: str | None) -> tuple[list[list[str]], str | None]:
    done: list[list[str]] = []
    for command in commands:
        try:
            result = run(command, cwd=cwd, text=True, capture_output=True, check=False, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return done, f"{shlex.join(command)}: {exc}"
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-400:]
            return done, f"{shlex.join(command)} exited {result.returncode}: {tail}"
        done.append(list(command))
    return done, None


class ClaudeSeat:
    """A local-scope entry in ``~/.claude.json``, written by ``claude mcp``."""

    client = "claude"

    def argv(self, target: SeatTarget) -> list[str]:
        return server_argv(target.mcp_command, target.actor_id, target.db_path if target.db_explicit else None, target.home)

    def read(self, project_root: Path) -> ClientConfig:
        return read_claude_seat(claude_config_path(), project_root)

    def inspect(self, target: SeatTarget, *, which: Which, run: Runner) -> SeatState:
        config = self.read(target.project_root)
        found = config.server() if config.status == "ok" else None
        if found is None:
            return SeatState("absent", config.path)
        problems = binding_problems(found, target, binding(self.argv(target))["db"])  # type: ignore[arg-type]
        if problems:
            return SeatState("taken", config.path, _holder(found), problems)
        return SeatState("bound", config.path, target.actor_id)

    def apply(self, target: SeatTarget, state: SeatState, *, which: Which, run: Runner) -> SeatResult:
        if which("claude") is None:
            return SeatResult(failure="claude is not on PATH")
        commands = ([claude_remove_command()] if state.status == "taken" else []) + [claude_add_command(self.argv(target))]
        done, failure = _run_commands(run, commands, str(target.project_root))
        return SeatResult(commands=done if failure else commands, failure=failure)

    def diagnose(self, target: SeatTarget, *, which: Which, run: Runner) -> list[Finding]:
        config = self.read(target.project_root)
        found = config.server() if config.status == "ok" else None
        if found is None:
            why = {
                "missing": f"no claude config at {config.path}",
                "unparseable": f"{config.path}: {config.error}",
                "no_project": f"{config.path} has no entry for {target.project_root}",
            }.get(config.status, f"{config.path} has no {SERVER_NAME} server")
            return [Finding("", "fail", f"{target.actor_id} has no claude seat: {why}", "rebind")]
        problems = binding_problems(found, target, binding(self.argv(target))["db"])  # type: ignore[arg-type]
        if problems:
            return [Finding("", "fail", f"{target.actor_id} claude seat in {config.path}: {'; '.join(problems)}", "replace")]
        return [Finding("", "ok", f"{target.actor_id} claude seat in {config.path} binds {target.actor_id}")]


# --- the Codex project seat ------------------------------------------------


def codex_project_config_path(project_root: Path) -> Path:
    return Path(project_root) / ".codex" / "config.toml"


def _header_path(line: str) -> tuple[list[str], bool] | None:
    """The key path of a table header line and whether it is an array of
    tables; None when the line is not a header (tomllib decides, so quoted
    keys and escapes are read exactly as Codex reads them)."""
    if not line.lstrip().startswith("["):
        return None
    try:
        parsed = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    node: object = parsed
    while isinstance(node, dict) and len(node) == 1:
        key = next(iter(node))
        path.append(key)
        node = node[key]
    if isinstance(node, list):
        return path, True
    if node != {} or not path:
        return None
    return path, False


def _without_ours(data: dict) -> dict:
    data = copy.deepcopy(data)
    servers = data.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop(SERVER_NAME, None)
        if not servers:
            # Ours was the only server: an empty table and none are the same settings.
            del data["mcp_servers"]
    return data


def rewrite_codex_project_text(text: str, block: str) -> str:
    """``text`` with our server's tables replaced by ``block``.

    Only the tables we write (``[mcp_servers.agent-comms]`` and its
    subtables) are removed, each from its header to the next header that is
    not ours; comment lines in them stay. Our server in any other form (an
    inline table, dotted keys, an array of tables) is refused rather than
    edited. The result must parse, hold ``block``'s server, and leave every
    other key exactly as it was, or nothing is returned.
    """
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SeatRefused(f"it does not parse as TOML ({exc}); fix it by hand") from exc
    kept: list[str] = []
    ours = False
    for line in text.splitlines():
        header = _header_path(line)
        if header is not None:
            path, array = header
            if path[:2] == ["mcp_servers", SERVER_NAME]:
                if array:
                    raise SeatRefused(f"it defines mcp_servers.{SERVER_NAME} as an array of tables, which agent-comms did not write; remove it by hand")
                ours = True
                continue
            ours = False
        if ours and not line.lstrip().startswith("#"):
            continue
        kept.append(line)
    remainder = "\n".join(kept).strip("\n")
    try:
        rest = tomllib.loads(remainder)
    except tomllib.TOMLDecodeError as exc:
        raise SeatRefused(f"it does not parse once our tables are removed ({exc}); fix it by hand") from exc
    if SERVER_NAME in (rest.get("mcp_servers") or {}):
        raise SeatRefused(f"it defines mcp_servers.{SERVER_NAME} as an inline table or dotted keys, which agent-comms did not write; remove it by hand")
    result = (remainder + "\n\n" + block) if remainder else block
    after = tomllib.loads(result)
    if _without_ours(after) != _without_ours(before) or after["mcp_servers"][SERVER_NAME] != tomllib.loads(block)["mcp_servers"][SERVER_NAME]:
        raise SeatRefused("rewriting it would change other settings; fix it by hand")
    return result


@dataclass(frozen=True)
class GitExclusion:
    """Where the project's seat file is excluded from commits, if anywhere."""

    exclude_file: Path | None
    line: str | None
    tracked: bool = False
    reason: str | None = None
    outside_git: bool = False


def git_exclusion(project_root: Path, *, which: Which, run: Runner) -> GitExclusion:
    """The exclude file git reports for ``project_root`` and the anchored
    line for its seat file; a project outside git, or no git, has none."""
    if which("git") is None:
        return GitExclusion(None, None, reason="git is not on PATH")

    def git(*args: str) -> str | None:
        try:
            result = run(["git", "-C", str(project_root), *args], text=True, capture_output=True, check=False, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    top = git("rev-parse", "--show-toplevel")
    if not top:
        return GitExclusion(None, None, reason=f"{project_root} is not in a git work tree", outside_git=True)
    exclude = git("rev-parse", "--git-path", "info/exclude")
    if not exclude:
        return GitExclusion(None, None, reason="git reports no exclude file")
    exclude_path = Path(exclude) if os.path.isabs(exclude) else Path(project_root) / exclude
    seat = codex_project_config_path(Path(os.path.realpath(project_root)))
    relative = os.path.relpath(seat, os.path.realpath(top)).replace(os.sep, "/")
    tracked = git("ls-files", "--error-unmatch", "--", ".codex/config.toml") is not None
    if "\n" in relative or "\r" in relative:
        return GitExclusion(exclude_path, None, tracked, reason=f"{relative!r} cannot be written as a git exclude pattern")
    return GitExclusion(exclude_path, "/" + git_literal_pattern(relative), tracked)


def git_literal_pattern(path: str) -> str:
    """``path`` as a git ignore pattern that matches only itself: the glob
    characters and the escape character are escaped. The pattern starts with
    ``/``, so a leading ``!`` or ``#`` needs nothing."""
    return "".join("\\" + char if char in "\\*?[" else char for char in path)


def git_ignores(project_root: Path, relative: str, *, run: Runner) -> bool:
    """Whether git now ignores ``relative`` in ``project_root``."""
    try:
        result = run(["git", "-C", str(project_root), "check-ignore", "-q", "--no-index", "--", relative], text=True, capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def codex_trusted(project_root: Path, config_path: Path | None = None) -> bool | None:
    """Whether the user's Codex config trusts ``project_root`` (or its
    realpath); None when the config cannot be read."""
    path = config_path or codex_config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    for key in dict.fromkeys((str(project_root), os.path.realpath(project_root))):
        entry = projects.get(key)
        if isinstance(entry, dict) and entry.get("trust_level") == "trusted":
            return True
    return False


class CodexSeat:
    """An ``[mcp_servers.agent-comms]`` entry in ``<project>/.codex/config.toml``."""

    client = "codex"

    def binding(self, target: SeatTarget) -> codex_binding.Binding:
        # The ledger is always in argv: a lower config layer may set
        # AGENT_COMMS_DB, and the env value here only blanks it.
        args = ["--actor-id", target.actor_id, "--db", str(target.db_path)]
        if target.home is not None:
            args += ["--home", str(target.home)]
        env = codex_binding.server_env(
            AGENT_COMMS_ACTOR_ID=target.actor_id,
            AGENT_COMMS_DB=str(target.db_path),
            AGENT_COMMS_HOME=None if target.home is None else str(target.home),
            AGENT_COMMS_PROJECT_ROOT=str(target.project_root),
        )
        return codex_binding.Binding(target.mcp_command, args, str(target.project_root), env)

    def expected_entry(self, target: SeatTarget) -> dict:
        binding = self.binding(target)
        return {**binding.pinned(), "env": dict(binding.env)}

    def read(self, project_root: Path) -> ClientConfig:
        return read_codex_seat(codex_project_config_path(project_root))

    def inspect(self, target: SeatTarget, *, which: Which, run: Runner) -> SeatState:
        path = codex_project_config_path(target.project_root)
        exclusion = git_exclusion(target.project_root, which=which, run=run)
        if exclusion.tracked:
            return SeatState("refused", path, problems=[f"{path} is tracked by git; agent-comms writes only an untracked file (untrack it with git rm --cached, or bind the seat in another client)"])
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return SeatState("absent", path)
        except (OSError, ValueError) as exc:
            return SeatState("refused", path, problems=[f"{path} cannot be read: {exc}"])
        try:
            rewrite_codex_project_text(text, codex_binding.entry_toml(self.binding(target)))
        except SeatRefused as exc:
            return SeatState("refused", path, problems=[f"{path}: {exc}"])
        data = tomllib.loads(text)
        stored = (data.get("mcp_servers") or {}).get(SERVER_NAME)
        if stored is None:
            return SeatState("absent", path)
        found = _entry(SERVER_NAME, stored)
        expected = self.expected_entry(target)
        problems = binding_problems(found, target, str(target.db_path))
        stored_n, expected_n = codex_binding.normalized_entry(stored), codex_binding.normalized_entry(expected)
        if not problems and stored_n != expected_n:
            differing = sorted(key for key in set(stored_n) | set(expected_n) if stored_n.get(key) != expected_n.get(key))
            problems.append(f"differs from the entry agent-comms writes in {', '.join(differing)}")
        if problems:
            return SeatState("taken", path, _holder(found), problems)
        return SeatState("bound", path, target.actor_id)

    def apply(self, target: SeatTarget, state: SeatState, *, which: Which, run: Runner) -> SeatResult:
        path = codex_project_config_path(target.project_root)
        exclusion = git_exclusion(target.project_root, which=which, run=run)
        if exclusion.tracked:
            return SeatResult(failure=f"{path} is tracked by git")
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        except (OSError, ValueError) as exc:
            return SeatResult(failure=f"{path} cannot be read: {exc}")
        try:
            rendered = rewrite_codex_project_text(text, codex_binding.entry_toml(self.binding(target)))
        except SeatRefused as exc:
            return SeatResult(failure=f"{path}: {exc}")
        if exclusion.exclude_file is not None and exclusion.line is None:
            return SeatResult(failure=f"{path}: {exclusion.reason}")
        notes = []
        try:
            if exclusion.exclude_file is not None and exclusion.line is not None:
                # Excluded before it exists, so the file is never committable;
                # git itself confirms the line matches before anything is written.
                notes.append(_add_exclude_line(exclusion.exclude_file, exclusion.line))
                if not git_ignores(target.project_root, ".codex/config.toml", run=run):
                    return SeatResult(notes=notes, failure=f"git does not ignore {path} after adding {exclusion.line} to {exclusion.exclude_file}; not written")
            else:
                notes.append(f"{exclusion.reason}; nothing excludes {path} from version control")
            if exclusion.outside_git:
                # Codex looks for .codex/ from the cwd up to the git root;
                # without one it reads only the directory it starts in.
                notes.append(f"outside git, Codex reads the seat only when started in {target.project_root}, not a subdirectory")
            path.parent.mkdir(exist_ok=True)
            _write_atomic(path, rendered)
        except OSError as exc:
            return SeatResult(notes=notes, failure=f"writing {path}: {exc}")
        notes.append(f"wrote [mcp_servers.{SERVER_NAME}] in {path}")
        if codex_trusted(target.project_root) is not True:
            notes.append(f"Codex loads it once the project is trusted: open codex in {target.project_root} and accept the trust prompt")
        return SeatResult(notes=notes)

    def diagnose(self, target: SeatTarget, *, which: Which, run: Runner) -> list[Finding]:
        actor = target.actor_id
        state = self.inspect(target, which=which, run=run)
        if state.status == "refused":
            return [Finding("", "fail", f"{actor} codex seat: {'; '.join(state.problems)}", "; ".join(state.problems))]
        if state.status == "absent":
            legacy = read_codex_seat(codex_config_path()).server()
            if legacy is not None:
                return [Finding("", "warn", f"{actor} has no project codex seat in {state.where}; the user-level {SERVER_NAME} entry in {codex_config_path()} applies in every directory", "rebind")]
            return [Finding("", "fail", f"{actor} has no codex seat: no {SERVER_NAME} server in {state.where}", "rebind")]
        if state.status == "taken":
            return [Finding("", "fail", f"{actor} codex seat in {state.where}: {'; '.join(state.problems)}", "replace")]
        findings = [Finding("", "ok", f"{actor} codex seat in {state.where} binds {actor}")]
        codex = which("codex")
        if codex is None:
            findings.append(Finding(":effective", "skip", "codex is not on PATH, so the binding Codex resolves was not checked"))
            return findings
        observed, error = codex_binding.resolve(run, codex, cwd=str(target.project_root))
        if observed is None:
            trusted = codex_trusted(target.project_root)
            if trusted is False:
                fix = f"open codex in {target.project_root} and accept the trust prompt (Codex loads .codex/config.toml only in a trusted project)"
                return findings + [Finding(":effective", "fail", f"{error}; {codex_config_path()} does not trust {target.project_root}", fix)]
            return findings + [Finding(":effective", "fail", str(error), f"run codex mcp get {SERVER_NAME} --json in {target.project_root} and fix what it reports")]
        problems = codex_binding.effective_problems(observed, self.binding(target))
        if problems:
            if codex_trusted(target.project_root) is False:
                fix = f"open codex in {target.project_root} and accept the trust prompt (Codex loads .codex/config.toml only in a trusted project)"
            else:
                fix = (
                    f"remove or rename the {SERVER_NAME} entry that sets these fields in another Codex config layer "
                    f"({codex_config_path()}, or a .codex/config.toml above {target.project_root}); agent-comms never edits those"
                )
            return findings + [Finding(":effective", "fail", f"Codex resolves {SERVER_NAME} in {target.project_root} differently from {state.where}: {'; '.join(problems)}", fix)]
        return findings + [Finding(":effective", "ok", f"codex resolves {SERVER_NAME} in {target.project_root} to {actor}")]


def _add_exclude_line(exclude_file: Path, line: str) -> str:
    try:
        text = exclude_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    if line in text.splitlines():
        return f"{exclude_file} already excludes {line}"
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if not text or text.endswith("\n") else "\n"
    with exclude_file.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}{line}\n")
    return f"added {line} to {exclude_file}"


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.new")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


SEATS: dict[str, ClaudeSeat | CodexSeat] = {"claude": ClaudeSeat(), "codex": CodexSeat()}
