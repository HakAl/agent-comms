"""The Claude runtime: template, setup and doctor."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from ..checks import FAIL, OK, SKIP, Runner, check, run_quietly, sh, version_from, version_matches
from ..policies import WORKER_DISPATCH_POLICY_VERSION
from ..runtime_pins import CLAUDE_PINNED_SHA256_ENV, RuntimePin, claude_versions_dir, custody_binary_sha256
from ..spawn import _escaped_json
from ..store import WORKER_DISPATCH_POLICY


def render_spawn() -> dict:
    return {
        "command": "{claude_binary}",
        "args": [
            "-p",
            "--no-session-persistence",
            "--output-format",
            "stream-json",
            "--strict-mcp-config",
            "--mcp-config",
            _claude_mcp_config(),
            "--settings",
            "{claude_settings}",
            "--permission-mode",
            "dontAsk",
            "{worker_prompt}",
        ],
    }


def _claude_mcp_config() -> str:
    return _escaped_json(
        {
            "mcpServers": {
                "agent-comms": {
                    "alwaysLoad": True,
                    "args": ["--db", "{db_path}", "--actor-id", "{actor_id}"],
                    "command": "{mcp_command}",
                    "env": {
                        "AGENT_COMMS_ACTOR_ID": "{actor_id}",
                        "AGENT_COMMS_HOME": "{agent_comms_home}",
                        "AGENT_COMMS_PROJECT_ROOT": "{project_root}",
                        "WAKE_POLICY": WORKER_DISPATCH_POLICY,
                        "WAKE_POLICY_VERSION": WORKER_DISPATCH_POLICY_VERSION,
                    },
                }
            }
        }
    )


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
    result = run_quietly(run, [str(binary), "--version"])
    if isinstance(result, Exception):
        return check(check_id, FAIL, f"could not execute {binary} --version: {result}", install)
    installed, output = version_from(result)
    if result.returncode != 0 or installed is None:
        return check(check_id, FAIL, f"{binary} --version exited {result.returncode}: {output!r}", install)
    if not version_matches(installed, pin):
        return check(check_id, FAIL, f"pinned Claude version drifted: expected {pin.version}, got {installed}; path={binary}", install)
    login = sh(binary, "auth", "login")
    result = run_quietly(run, [str(binary), "auth", "status"])
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


class ClaudeRuntime:
    name = "claude"
    capabilities = ("implementation",)
    pinned = True
    fallback = False
    monitored = True
    rerender = None

    @property
    def adapter_class(self) -> type:
        from ..adapters.claude import ClaudeAdapter

        return ClaudeAdapter

    def adapter(self):  # type: ignore[no-untyped-def]
        return self.adapter_class()

    def detect(self, which: Callable[[str], str | None], pin: RuntimePin | None) -> bool:
        """The pinned binary is in custody (Claude is not run from PATH)."""
        return pin is not None and (claude_versions_dir() / pin.version).is_file()

    def render_spawn(self, actor_id: str, *, codex_home: str | None = None) -> dict:
        return render_spawn()

    def provision(self, worker_id, *, pin, pin_error, which, run) -> dict:  # type: ignore[no-untyped-def]
        if pin is None:
            return {"claude_custody": {"version": None, "path": None, "present": False, "detail": pin_error}}
        binary = claude_versions_dir() / pin.version
        return {"claude_custody": {"version": pin.version, "path": str(binary), "present": binary.is_file()}}

    def provision_absent(self) -> dict:
        return {"claude_custody": None}

    def doctor_checks(self, pin, snapshot, *, run, now, ledger) -> list[dict]:  # type: ignore[no-untyped-def]
        return [check_claude(pin, run=run)]

    def doctor_skip(self) -> dict | None:
        return check("runtime:claude", SKIP, "no claude worker registered")


RUNTIME = ClaudeRuntime()
