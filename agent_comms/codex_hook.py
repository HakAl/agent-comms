"""The policy hook as a Codex worker runs it, declared and trusted per run.

Codex runs a non-managed hook only when its config state holds a
``trusted_hash`` equal to the hook's current hash. A worker declares the
hook with ``-c hooks.PreToolUse=...`` and trusts exactly that hook with
``-c hooks.state=...``; any other command, timeout or Codex hashing leaves
it untrusted, and the dispatch preflight refuses. The hash and the state
key follow Codex 0.160.0 (``codex-rs/hooks/src/engine/discovery.rs``
``hook_hash``; ``codex-rs/config/src/fingerprint.rs`` ``version_for_toml``),
and certification rechecks them against the pinned build.

``hooks/list`` proves trust and enablement, never that the hook runs; Codex
runs the tool anyway when a hook cannot start, crashes, times out or prints
something it cannot parse. So the preflight also runs the rendered command
once, as Codex would, and requires a deny for a call the policy forbids.
"""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shlex
import subprocess
import time
import tomllib

from .codex_binding import Runner, toml_str

HOOK_TIMEOUT_SECONDS = 30
# ``-c`` values form Codex's session-flags layer, whose hooks Codex keys by
# this synthetic source path; ours is the first group and handler.
STATE_KEY = "/<session-flags>/config.toml:pre_tool_use:0:0"
SMOKE_TOOL = "mcp__agent-comms__dispatch_agent"
# Codex 0.160 runs enabled hooks without checking trust under this flag.
BYPASS_TRUST_FLAG = "--dangerously-bypass-hook-trust"


def hook_command(*, python: str, hook_path: str, policy_name: str, project_root: str, hook_sha256: str) -> str:
    """The shell command Codex runs: the hook takes its policy, root and
    fingerprint as arguments, since Codex hands a hook its own env."""
    words = [
        python, hook_path, "--runtime", "codex", "--policy", policy_name,
        "--project-root", project_root, "--sha256", hook_sha256,
    ]
    return " ".join(shlex.quote(word) for word in words)


def _canonical(value: object) -> object:
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def trust_hash(command: str, timeout: int = HOOK_TIMEOUT_SECONDS) -> str:
    """Codex's hash of a PreToolUse command hook with no matcher: SHA-256 of
    key-sorted, compact JSON of its normalized identity (unset optional
    fields are absent, as TOML serialization drops them)."""
    identity = {
        "event_name": "pre_tool_use",
        "hooks": [{"type": "command", "command": command, "timeout": timeout, "async": False}],
    }
    encoded = json.dumps(_canonical(identity), separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def config_overrides(command: str) -> list[str]:
    """The ``-c`` pairs that declare the hook and trust exactly it. State
    merges field by field across layers, so ``enabled`` is set as well: a
    lower layer's ``enabled = false`` cannot switch the hook off. The state
    is one inline table because a dotted ``-c`` key splits on the ``.`` in
    ``config.toml``."""
    hook = f"{{type = {toml_str('command')}, command = {toml_str(command)}, timeout = {HOOK_TIMEOUT_SECONDS}}}"
    state = f"{{enabled = true, trusted_hash = {toml_str(trust_hash(command))}}}"
    return [
        "-c", f"hooks.PreToolUse=[{{hooks = [{hook}]}}]",
        "-c", f"hooks.state={{{toml_str(STATE_KEY)} = {state}}}",
    ]


def touches_hooks(override: str) -> bool:
    """Whether a ``-c key=value`` sets anything under ``hooks``. A key that
    does not parse is read as ours when it names ``hooks`` at all."""
    key = override.partition("=")[0].strip()
    try:
        path = tomllib.loads(f"{key} = 0")
    except tomllib.TOMLDecodeError:
        return "hooks" in key
    return "hooks" in path


def trust_problems(entries: list, command: str) -> list[str]:
    """Why Codex would not run our hook, from its ``hooks/list`` entries."""
    ours = [entry for entry in entries if isinstance(entry, dict) and entry.get("key") == STATE_KEY]
    if not ours:
        return [f"codex lists no hook under {STATE_KEY}"]
    entry = ours[0]
    problems = []
    if entry.get("command") != command:
        problems.append(f"the listed hook runs {entry.get('command')!r}, not the policy hook")
    if entry.get("enabled") is not True:
        problems.append("the hook is disabled")
    if entry.get("trustStatus") != "trusted":
        problems.append(
            f"the hook is {entry.get('trustStatus')!r}, not trusted "
            f"(codex hash {entry.get('currentHash')}, ours {trust_hash(command)})"
        )
    return problems


def list_hooks(
    codex: str,
    *,
    cwd: str,
    env: dict[str, str],
    overrides: list[str],
    timeout: float = 5.0,
) -> tuple[list | None, str | None]:
    """``hooks/list`` for ``cwd`` from ``codex app-server``: (hooks, None) or
    (None, why). The server answers over stdio and exits on end of input
    before answering, so it is read up to the answer, then stopped."""
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": {"name": "agent-comms", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {"cwds": [cwd]}},
    ]
    try:
        process = subprocess.Popen(
            [codex, *overrides, "app-server"], cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return None, f"{codex} app-server failed to start: {exc}"
    try:
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write("".join(json.dumps(request) + "\n" for request in requests).encode())
            process.stdin.flush()
        except OSError as exc:
            return None, f"{codex} app-server closed its input: {exc}"
        deadline = time.monotonic() + timeout
        buffer = b""
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                    except ValueError:
                        return None, f"{codex} app-server printed a line that is not JSON"
                    if isinstance(message, dict) and message.get("id") == 2:
                        return _hooks_from(message)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return None, f"{codex} app-server hooks/list timed out after {timeout:g}s"
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    return None, f"{codex} app-server exited before answering hooks/list"
                buffer += chunk
    finally:
        process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _hooks_from(message: dict) -> tuple[list | None, str | None]:
    if "error" in message:
        return None, f"codex hooks/list failed: {message['error']}"
    data = (message.get("result") or {}).get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        return None, "codex hooks/list answered without one entry for the worker directory"
    hooks = data[0].get("hooks")
    if not isinstance(hooks, list):
        return None, "codex hooks/list answered without a hooks list"
    return hooks, None


def smoke_run(
    command: str,
    *,
    cwd: str,
    env: dict[str, str],
    runner: Runner = subprocess.run,
    timeout: float = HOOK_TIMEOUT_SECONDS,
) -> str | None:
    """Run ``command`` as Codex runs a hook (``$SHELL -lc``) on a call every
    worker policy denies; None when it denies, otherwise why not."""
    shell = env.get("SHELL") or "/bin/sh"
    payload = {
        "hook_event_name": "PreToolUse", "tool_name": SMOKE_TOOL, "tool_input": {},
        "cwd": cwd, "session_id": "agent-comms-preflight",
    }
    try:
        result = runner(
            [shell, "-lc", command], input=json.dumps(payload), cwd=cwd, env=env,
            text=True, capture_output=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"the policy hook did not run: {exc}"
    if result.returncode != 0:
        tail = (result.stderr or "").strip()[-300:]
        return f"the policy hook exited {result.returncode}: {tail}"
    try:
        decision = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
    except (ValueError, KeyError, TypeError):
        return f"the policy hook printed no decision for {SMOKE_TOOL}: {result.stdout.strip()[-300:]!r}"
    if decision != "deny":
        return f"the policy hook answered {decision!r} for {SMOKE_TOOL}, not deny"
    return None
