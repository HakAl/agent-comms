from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

WORKER_POLICY = "worker_dispatch_readwrite_bounded"
OPERATOR_MAILBOX_POLICY = "operator_mailbox"

MCP_DENIED_TOOLS = {
    "dispatch_agent",
    "mcp__agent_comms__dispatch_agent",
    "mcp__agent-comms__dispatch_agent",
    "cancel_dispatch",
    "mcp__agent_comms__cancel_dispatch",
    "mcp__agent-comms__cancel_dispatch",
    "register_actor",
    "mcp__agent_comms__register_actor",
    "mcp__agent-comms__register_actor",
    "register_agent",
    "mcp__agent_comms__register_agent",
    "mcp__agent-comms__register_agent",
}

FILE_TOOLS = {"Read", "Edit", "MultiEdit", "Write"}
NETWORK_WRITE_PATTERNS = [
    re.compile(r"\bgh\s+pr\s+create\b"),
    re.compile(r"\bgh\s+release\s+create\b"),
    re.compile(r"\bgit\s+push\b"),
    re.compile(r"\b(curl|http|httpie)\b.*(?:-X|--request)\s+(POST|PUT|PATCH|DELETE)\b", re.IGNORECASE),
    re.compile(r"\b(ssh|scp|sftp|rsync)\b"),
]


def decision(value: str, reason: str = "") -> dict:
    output = {"hookEventName": "PreToolUse", "permissionDecision": value}
    if reason:
        output["permissionDecisionReason"] = reason
    return {"hookSpecificOutput": output}


def deny(reason: str) -> dict:
    return decision("deny", reason)


def allow() -> dict:
    return decision("allow")


def self_check(expected: str | None = None) -> str | None:
    if expected is None:
        expected = os.environ.get("AGENT_COMMS_POLICY_HOOK_SHA256", "")
    expected = expected.strip()
    if not expected:
        return None
    actual = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if actual != expected:
        return "WakePolicy hook fingerprint mismatch"
    return None


def path_from_input(tool_input: dict) -> str | None:
    for key in ("file_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return None


def is_inside_project(path_value: str, project_root: str, base: str | None = None) -> bool:
    """Whether ``path_value`` lies in ``project_root``; a relative path is
    taken from ``base`` (default: the root)."""
    if not project_root:
        return False
    try:
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            path = Path(base or project_root) / path
        path = path.resolve()
        root = Path(project_root).expanduser().resolve()
        return path == root or root in path.parents
    except OSError:
        return False


def bash_command(input_payload: dict) -> str:
    tool_input = input_payload.get("tool_input") or input_payload.get("input") or {}
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command", "")
    return command if isinstance(command, str) else ""


def blocks_network_write(command: str) -> bool:
    if not command:
        return False
    return any(pattern.search(command) for pattern in NETWORK_WRITE_PATTERNS)


PATCH_FILE_HEADERS = ("*** Add File:", "*** Update File:", "*** Delete File:", "*** Move to:")
# Selects another environment, whose working directory the hook is not told.
PATCH_ENVIRONMENT_HEADER = "*** Environment ID:"


def patch_paths(tool_input: dict) -> list[str] | None:
    """Every path a Codex ``apply_patch`` names (the move source is its
    ``Update File``); None when the patch names none, a header names an
    empty path, or the patch is not text."""
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    found = []
    for line in command.splitlines():
        line = line.strip()
        for header in PATCH_FILE_HEADERS:
            if line.startswith(header):
                found.append(line[len(header):].strip())
    if any(not path for path in found):
        return None
    return found or None


def evaluate(
    input_payload: dict,
    *,
    policy_name: str | None = None,
    project_root: str | None = None,
    expected_sha256: str | None = None,
) -> dict:
    if policy_name is None:
        policy_name = os.environ.get("WAKE_POLICY", "")
    if project_root is None:
        project_root = os.environ.get("AGENT_COMMS_PROJECT_ROOT", "")
    if policy_name not in {WORKER_POLICY, OPERATOR_MAILBOX_POLICY}:
        # Interactive architect sessions are not hook-restricted unless a spawn sets WAKE_POLICY.
        return allow()

    fingerprint_error = self_check(expected_sha256)
    if fingerprint_error:
        return deny(fingerprint_error)

    tool_name = input_payload.get("tool_name") or input_payload.get("toolName") or ""
    tool_input = input_payload.get("tool_input") or input_payload.get("input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    if (
        tool_name in MCP_DENIED_TOOLS
        or str(tool_name).endswith("__dispatch_agent")
    ):
        return deny(f"{policy_name} forbids dispatch authority from this session")

    if policy_name != WORKER_POLICY:
        return allow()

    if tool_name in FILE_TOOLS:
        path_value = path_from_input(tool_input)
        if path_value and not is_inside_project(path_value, project_root):
            return deny(f"{WORKER_POLICY} forbids file access outside AGENT_COMMS_PROJECT_ROOT")

    if tool_name == "apply_patch":
        # Codex resolves a relative patch path against the turn's cwd, which
        # the -C a worker starts in can move away from the project root.
        cwd = input_payload.get("cwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return deny(f"{WORKER_POLICY} denies an apply_patch from an unknown working directory")
        command = tool_input.get("command")
        if isinstance(command, str) and any(
            line.strip().startswith(PATCH_ENVIRONMENT_HEADER) for line in command.splitlines()
        ):
            return deny(f"{WORKER_POLICY} denies an apply_patch that selects another environment")
        patched = patch_paths(tool_input)
        if patched is None:
            return deny(f"{WORKER_POLICY} denies an apply_patch that names no file")
        if not all(is_inside_project(path, project_root, cwd) for path in patched):
            return deny(f"{WORKER_POLICY} forbids file access outside AGENT_COMMS_PROJECT_ROOT")

    if tool_name == "Bash" and blocks_network_write(bash_command(input_payload)):
        return deny(f"{WORKER_POLICY} forbids network/external write commands")

    return allow()


# Codex runs a hook as ``$SHELL -lc <command>`` with its own environment, so a
# Codex worker's hook takes the policy, project root and fingerprint as
# arguments. Codex reports a bare allow as a failed hook and runs the tool,
# so in Codex mode an allowed call prints nothing.
ARGUMENTS = ("--runtime", "--policy", "--project-root", "--sha256")
RUNTIMES = ("claude", "codex")


def parse_arguments(argv: list[str]) -> dict[str, str]:
    if len(argv) % 2:
        raise ValueError("arguments come in --name value pairs")
    values = dict(zip(argv[::2], argv[1::2]))
    unknown = sorted(set(values) - set(ARGUMENTS))
    if unknown or len(values) * 2 != len(argv):
        raise ValueError(f"unknown or repeated argument: {', '.join(unknown) or 'repeated'}")
    if values.get("--runtime", "claude") not in RUNTIMES:
        raise ValueError(f"unknown runtime {values['--runtime']!r}")
    if values.get("--runtime") == "codex":
        # Codex hands the hook no policy env to fall back on.
        missing = [name for name in ARGUMENTS[1:] if name not in values]
        if missing:
            raise ValueError(f"codex mode requires {', '.join(missing)}")
    if "--policy" in values and values["--policy"] not in {WORKER_POLICY, OPERATOR_MAILBOX_POLICY}:
        raise ValueError(f"unknown policy {values['--policy']!r}")
    return values


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
        input_payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(input_payload, dict):
            raise ValueError("hook input is not an object")
        result = evaluate(
            input_payload,
            policy_name=arguments.get("--policy"),
            project_root=arguments.get("--project-root"),
            expected_sha256=arguments.get("--sha256"),
        )
        if arguments.get("--runtime") == "codex" and result["hookSpecificOutput"]["permissionDecision"] == "allow":
            return 0
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        print(json.dumps(deny(f"WakePolicy hook failed closed: {exc}"), sort_keys=True))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
