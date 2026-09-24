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


def self_check() -> str | None:
    expected = os.environ.get("AGENT_COMMS_POLICY_HOOK_SHA256", "").strip()
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


def is_inside_project(path_value: str, project_root: str) -> bool:
    if not project_root:
        return False
    try:
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            path = Path(project_root) / path
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


def evaluate(input_payload: dict) -> dict:
    policy_name = os.environ.get("WAKE_POLICY", "")
    if policy_name not in {WORKER_POLICY, OPERATOR_MAILBOX_POLICY}:
        # Interactive architect sessions are not hook-restricted unless a spawn sets WAKE_POLICY.
        return allow()

    fingerprint_error = self_check()
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
        project_root = os.environ.get("AGENT_COMMS_PROJECT_ROOT", "")
        if path_value and not is_inside_project(path_value, project_root):
            return deny(f"{WORKER_POLICY} forbids file access outside AGENT_COMMS_PROJECT_ROOT")

    if tool_name == "Bash" and blocks_network_write(bash_command(input_payload)):
        return deny(f"{WORKER_POLICY} forbids network/external write commands")

    return allow()


def main() -> int:
    try:
        input_payload = json.loads(sys.stdin.read() or "{}")
        print(json.dumps(evaluate(input_payload), sort_keys=True))
    except Exception as exc:
        print(json.dumps(deny(f"WakePolicy hook failed closed: {exc}"), sort_keys=True))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
