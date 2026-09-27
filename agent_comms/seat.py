"""Launch an interactive architect seat with explicit substrate identity.

Console-script entry point for ``agent-comms-seat``. It checks that the
project's registered MCP server agrees with the requested actor, exports the
seat identity to the runtime, and execs it. ``AGENT_COMMS_INSTALL_ROOT`` is
this interpreter's ``sys.prefix``: the ``.venv`` of a checkout or the tool
venv of an installed package. Hooks started by the runtime use it to find the
same interpreter, and ``review status`` compares its own prefix against it.
"""

from __future__ import annotations

import os
import sys

from .mcp_clients import actor_ids_in, claude_config_path, read_claude_seat

INSTALL_ROOT = sys.prefix


def usage() -> int:
    print("usage: agent-comms-seat <actor-id> [--] <runtime argv...>", file=sys.stderr)
    return 2


def guard(actor_id: str) -> bool:
    config = read_claude_seat(claude_config_path(), os.getcwd())
    config_path = config.path
    if config.status == "missing":
        print(f"warning: expected actor id {actor_id!r}; no config at {config_path}", file=sys.stderr)
        return True
    if config.status == "unparseable":
        print(f"warning: expected actor id {actor_id!r}; could not parse {config_path}: {config.error}", file=sys.stderr)
        return True
    if config.status == "no_project":
        cwd = os.getcwd()
        keys = {cwd, os.path.realpath(cwd)}
        print(
            f"warning: expected actor id {actor_id!r}; no project entry in {config_path} "
            f"for {sorted(keys)!r}",
            file=sys.stderr,
        )
        return True

    found = []
    malformed = False
    for _name, args in config.servers:
        ids, bad = actor_ids_in(args)
        found.extend(ids)
        malformed = malformed or bad

    distinct = set(found)
    if malformed or distinct != {actor_id}:
        print(
            f"refusing launch: requested actor id {actor_id!r}; discovered ids "
            f"{sorted(distinct)!r} in {config_path}; malformed={malformed}",
            file=sys.stderr,
        )
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if not args or not args[0]:
        return usage()
    actor_id = args[0]
    runtime = args[1:]
    if runtime[:1] == ["--"]:
        runtime = runtime[1:]
    if not runtime:
        return usage()
    if not guard(actor_id):
        return 3
    os.environ["AGENT_COMMS_ACTOR_ID"] = actor_id
    os.environ["AGENT_COMMS_LAUNCH_KIND"] = "architect_interactive"
    os.environ["AGENT_COMMS_INSTALL_ROOT"] = INSTALL_ROOT
    try:
        os.execvp(runtime[0], runtime)
    except OSError as exc:
        print(f"agent-comms-seat: failed to execute {runtime[0]!r}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
