"""The ``agent-comms`` MCP server as Codex resolves it, and the check that
the resolved server is the one we meant.

Codex merges a server's fields across its config layers (user config,
project ``.codex/config.toml``, ``-c`` overrides): ``args`` and arrays are
replaced whole, but ``env`` keys from a lower layer survive and fields a
higher layer leaves unset (``enabled``, ``cwd``, ``env_vars``,
``enabled_tools``, ...) are inherited. So the architect's project seat and
a worker's per-run ``-c`` binding both pin every field that decides identity
and availability, and both are verified through ``codex mcp get --json``,
which is Codex's own merge, never a re-implementation of it.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field

SERVER_NAME = "agent-comms"
# Every variable the server or its hooks read for identity, ledger, install
# and policy. A pinned entry sets each one, to "" when it does not apply
# (the server treats an empty value as unset), so a lower layer's value for
# the same key cannot leak in.
SERVER_ENV_KEYS = (
    "AGENT_COMMS_ACTOR_ID",
    "AGENT_COMMS_DB",
    "AGENT_COMMS_HOME",
    "AGENT_COMMS_PROJECT_ROOT",
    "WAKE_POLICY",
    "WAKE_POLICY_VERSION",
)
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def toml_str(value: object) -> str:
    """``value`` as a TOML basic string.

    JSON's escapes for quote, backslash and control characters are valid in
    TOML, but its ``\\uXXXX`` surrogate pairs for characters beyond the BMP
    are not, so non-ASCII stays literal (the file is UTF-8). DEL, which JSON
    leaves raw and TOML forbids, is escaped.
    """
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")


def toml_array(values: list) -> str:
    return "[" + ", ".join(toml_str(value) for value in values) + "]"


def toml_inline_table(values: dict) -> str:
    return "{" + ", ".join(f"{toml_str(key)} = {toml_str(value)}" for key, value in values.items()) + "}"


@dataclass(frozen=True)
class Binding:
    """What the ``agent-comms`` server must resolve to for one actor."""

    command: str
    args: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)

    def pinned(self) -> dict[str, object]:
        """Every field we set, in the order the project entry writes them."""
        return {
            "command": self.command,
            "args": list(self.args),
            "cwd": self.cwd,
            "enabled": True,
            "disabled_tools": [],
            "env_vars": [],
        }


def server_env(**values: str | None) -> dict[str, str]:
    """``SERVER_ENV_KEYS`` with the given values and "" for the rest."""
    unknown = set(values) - set(SERVER_ENV_KEYS)
    if unknown:
        raise ValueError(f"not a server env key: {', '.join(sorted(unknown))}")
    return {key: str(values.get(key) or "") for key in SERVER_ENV_KEYS}


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return toml_array(value)
    return toml_str(value)


def entry_toml(binding: Binding) -> str:
    """The ``[mcp_servers.agent-comms]`` block a project seat holds."""
    lines = [f"[mcp_servers.{SERVER_NAME}]"]
    lines += [f"{key} = {_toml_value(value)}" for key, value in binding.pinned().items()]
    lines += ["", f"[mcp_servers.{SERVER_NAME}.env]"]
    lines += [f"{key} = {toml_str(value)}" for key, value in binding.env.items()]
    return "\n".join(lines) + "\n"


def config_overrides(binding: Binding, *, extra: dict[str, object] | None = None) -> list[str]:
    """The ``-c key=value`` pairs that bind the server for one run."""
    prefix = f"mcp_servers.{SERVER_NAME}"
    values = {**binding.pinned(), **(extra or {})}
    argv: list[str] = []
    for key, value in values.items():
        argv += ["-c", f"{prefix}.{key}={_toml_value(value)}"]
    argv += ["-c", f"{prefix}.env={toml_inline_table(binding.env)}"]
    return argv


PATH_FLAGS = ("--db", "--home")
PATH_ENV_KEYS = ("AGENT_COMMS_DB", "AGENT_COMMS_HOME", "AGENT_COMMS_PROJECT_ROOT")


def _real(value: object) -> object:
    """An absolute path by file identity; anything else as it is."""
    if isinstance(value, str) and os.path.isabs(value):
        return os.path.realpath(value)
    return value


def normalized_args(args: object) -> object:
    """``args`` with the values of the path flags compared by file identity."""
    if not isinstance(args, list):
        return args
    result = list(args)
    for index, value in enumerate(result):
        if index > 0 and result[index - 1] in PATH_FLAGS:
            result[index] = _real(value)
    return result


def normalized_env(env: object) -> object:
    if not isinstance(env, dict):
        return env
    return {key: _real(value) if key in PATH_ENV_KEYS else value for key, value in env.items()}


def normalized_entry(entry: dict) -> dict:
    """A server table with every path field compared by file identity."""
    result = dict(entry)
    for key in ("command", "cwd"):
        if key in result:
            result[key] = _real(result[key])
    if "args" in result:
        result["args"] = normalized_args(result["args"])
    if "env" in result:
        result["env"] = normalized_env(result["env"])
    return result


def _same_path(observed: object, expected: str) -> bool:
    if not isinstance(observed, str) or not os.path.isabs(observed):
        return False
    return os.path.realpath(observed) == os.path.realpath(expected)


def effective_problems(observed: dict, binding: Binding) -> list[str]:
    """How the server Codex resolved differs from ``binding``; empty when it
    is the one we meant. ``observed`` is ``codex mcp get --json`` output."""
    problems: list[str] = []
    transport = observed.get("transport") if isinstance(observed.get("transport"), dict) else {}
    if transport.get("type") != "stdio":
        problems.append(f"transport is {transport.get('type')!r}, expected 'stdio'")
    if not _same_path(transport.get("command"), binding.command):
        problems.append(f"command is {transport.get('command')!r}, expected {binding.command!r}")
    if normalized_args(transport.get("args")) != normalized_args(binding.args):
        problems.append(f"args are {transport.get('args')!r}, expected {binding.args!r}")
    if observed.get("enabled") is not True:
        problems.append(f"enabled is {observed.get('enabled')!r}")
    if not _same_path(transport.get("cwd"), binding.cwd):
        problems.append(f"cwd is {transport.get('cwd')!r}, expected {binding.cwd!r}")
    if observed.get("enabled_tools") is not None:
        problems.append(f"enabled_tools restricts the tools to {observed.get('enabled_tools')!r}")
    if observed.get("disabled_tools") not in (None, []):
        problems.append(f"disabled_tools is {observed.get('disabled_tools')!r}")
    if transport.get("env_vars") not in (None, []):
        problems.append(f"env_vars forwards {transport.get('env_vars')!r}")
    env = transport.get("env") if isinstance(transport.get("env"), dict) else {}
    observed_env, expected_env = normalized_env(env), normalized_env(binding.env)
    for key in sorted(set(env) | set(binding.env)):
        if key not in binding.env:
            problems.append(f"env {key}={env[key]!r} is set")
        elif observed_env.get(key) != expected_env[key]:  # type: ignore[union-attr,index]
            problems.append(f"env {key} is {env.get(key)!r}, expected {binding.env[key]!r}")
    return problems


def get_argv(codex: str, overrides: list[str] | None = None) -> list[str]:
    return [codex, *(overrides or []), "mcp", "get", SERVER_NAME, "--json"]


# Options of ``codex`` and ``codex exec`` (0.157.0) that take a value. Of
# these, -c/--config, -p/--profile and -C/--cd change which configuration
# Codex loads; the rest are skipped with their value.
_CONFIG, _PROFILE, _CD = ("-c", "--config"), ("-p", "--profile"), ("-C", "--cd")
_VALUE_SHORTS = frozenset("cpCmsaio")
_VALUE_LONGS = frozenset({
    "--config", "--profile", "--cd", "--model", "--sandbox", "--ask-for-approval", "--image",
    "--add-dir", "--output-last-message", "--output-schema", "--color", "--local-provider",
    "--thread-source", "--remote", "--remote-auth-token-env", "--enable", "--disable",
})


@dataclass
class LaunchOptions:
    """What a ``codex`` argv sets that decides the configuration it loads."""

    overrides: list[str] = field(default_factory=list)  # each -c value, in order
    profile: str | None = None
    cd: str | None = None
    flags: list[str] = field(default_factory=list)  # each long option that takes no value


def launch_options(args: list[str]) -> LaunchOptions:
    """The ``-c`` values, profile and start directory in ``args``, read as
    Codex's parser reads them (a later profile or directory wins)."""
    options = LaunchOptions()
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == "--":
            break
        if token.startswith("--"):
            name, eq, inline = token.partition("=")
            if name not in _VALUE_LONGS:
                options.flags.append(name)
                continue
        elif token.startswith("-") and len(token) > 1 and token[1] in _VALUE_SHORTS:
            name, inline = token[:2], token[2:].removeprefix("=")
            eq = "=" if inline else ""
        else:
            continue
        if not eq:
            if index >= len(args):
                break
            inline = args[index]
            index += 1
        if name in _CONFIG:
            options.overrides.append(inline)
        elif name in _PROFILE:
            options.profile = inline
        elif name in _CD:
            options.cd = inline
    return options


def overrides_server(override: str) -> bool:
    """Whether a ``-c key=value`` replaces or edits our server's entry (the
    whole ``mcp_servers`` table counts). A key that does not parse is read
    as ours when it names ``mcp_servers`` at all."""
    key = override.partition("=")[0].strip()
    try:
        path = tomllib.loads(f"{key} = 0")
    except tomllib.TOMLDecodeError:
        return "mcp_servers" in key
    servers = path.get("mcp_servers")
    return servers == 0 or (isinstance(servers, dict) and SERVER_NAME in servers)


def resolve(
    run: Runner,
    codex: str,
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    overrides: list[str] | None = None,
    timeout: float = 30.0,
) -> tuple[dict | None, str | None]:
    """``codex mcp get --json`` from ``cwd``: (the server, None) or (None, why)."""
    argv = get_argv(codex, overrides)
    try:
        result = run(argv, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{codex} mcp get failed: {exc}"
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-300:]
        return None, f"codex resolves no {SERVER_NAME} server from {cwd} ({tail or f'exit {result.returncode}'})"
    try:
        observed = json.loads(result.stdout)
    except ValueError as exc:
        return None, f"{codex} mcp get printed no JSON: {exc}"
    if not isinstance(observed, dict):
        return None, f"{codex} mcp get printed {type(observed).__name__}, not an object"
    return observed, None
