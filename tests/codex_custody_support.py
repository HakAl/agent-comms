"""A fake Codex custody bundle for hermetic tests.

Codex dispatch runs ``<custody>/<pinned version>/bin/codex`` and checks the
bundle digest against the pin first. A test that dispatches a Codex actor
builds a small bundle here and points the adapter at it with the two
production overrides, as the Claude tests do with their custody binary.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms.runtime_pins import (
    CODEX_PINNED_SHA256_ENV,
    CODEX_VERSIONS_DIR_ENV,
    bundle_sha256,
    codex_pin,
)


def default_codex_script() -> str:
    return (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        f"  printf 'codex-cli {codex_pin().version}\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )


def fake_codex_bundle(versions_dir: Path, script: str | None = None) -> dict[str, str]:
    """Write a bundle whose ``bin/codex`` runs ``script``; return the env that pins it."""
    bundle = versions_dir / codex_pin().version
    binary = bundle / "bin" / "codex"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(script if script is not None else default_codex_script())
    binary.chmod(0o755)
    return codex_custody_env(versions_dir)


def codex_custody_env(versions_dir: Path) -> dict[str, str]:
    """The overrides that pin the bundle already in ``versions_dir``."""
    return {
        CODEX_VERSIONS_DIR_ENV: str(versions_dir),
        CODEX_PINNED_SHA256_ENV: bundle_sha256(versions_dir / codex_pin().version),
    }


def python_shim_script() -> str:
    """A ``bin/codex`` that hands its arguments to this interpreter, for tests
    whose stand-in worker is ``python -c ...``."""
    return f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'


# The worker binding preflight runs ``codex -c key=value ... mcp get
# agent-comms --json``. With no other config layer, Codex resolves what the
# -c values set, so the stand-in prints that and needs no real Codex.
_MCP_GET_STAND_IN = """
import json, sys, tomllib

def merge(into, table):
    for key, value in table.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict) and key != "env":
            merge(into[key], value)
        else:
            into[key] = value

args, config = sys.argv[1:], {}
while args[:1] in (["-c"], ["--config"], ["-p"], ["--profile"]):
    if args[0] in ("-c", "--config"):  # a profile layer is not modelled
        merge(config, tomllib.loads(args[1]))
    args = args[2:]
if args[:2] != ["mcp", "get"]:
    sys.exit(0)
server = config.get("mcp_servers", {}).get(args[2])
if server is None:
    sys.exit("no such server")
print(json.dumps({
    "name": args[2],
    "enabled": server.get("enabled", True),
    "disabled_reason": None,
    "transport": {
        "type": "stdio",
        "command": server.get("command"),
        "args": server.get("args", []),
        "env": server.get("env"),
        "env_vars": server.get("env_vars", []),
        "cwd": server.get("cwd"),
    },
    "enabled_tools": server.get("enabled_tools"),
    "disabled_tools": server.get("disabled_tools", []),
    "startup_timeout_sec": None,
    "tool_timeout_sec": None,
}))
"""


def mcp_get_codex_script() -> str:
    """A ``bin/codex`` that answers ``codex mcp get`` from its ``-c`` values,
    for tests that dispatch a Codex worker without a real Codex."""
    return f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(_MCP_GET_STAND_IN)} "$@"\n'


def forwarding_codex_script() -> str:
    """A ``bin/codex`` that runs the ``codex`` on PATH, for tests that ask
    the real Codex what it resolves; skip them when it is absent."""
    real = shutil.which("codex")
    if real is None:
        return "#!/bin/sh\necho 'codex CLI unavailable' >&2\nexit 127\n"
    return f'#!/bin/sh\nexec {shlex.quote(real)} "$@"\n'


def install_fake_codex_custody(test: unittest.TestCase, script: str | None = None) -> Path:
    """Give ``test`` a fake custody bundle for its whole run; return ``bin/codex``.

    The bundle lives in a temporary directory removed at cleanup, and the
    pinning overrides are patched into the environment until then.
    """
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    versions_dir = Path(temp.name) / "codex-custody"
    patch = mock.patch.dict(os.environ, fake_codex_bundle(versions_dir, script))
    patch.start()
    test.addCleanup(patch.stop)
    return versions_dir / codex_pin().version / "bin" / "codex"
