from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

WORKER_DISPATCH_POLICY = "worker_dispatch_readwrite_bounded"
WORKER_DISPATCH_POLICY_VERSION = "v2"
OPERATOR_MAILBOX_POLICY = "operator_mailbox"
OPERATOR_MAILBOX_POLICY_VERSION = "v1"
CREDENTIAL_READ_DENY = (
    "~/.agent-comms",
    "~/.ssh",
    "~/.aws",
    "~/.azure",
    "~/.config/gcloud",
    "~/.config/gh",
    "~/.codex",
    "~/.claude",
    "~/.gnupg",
    "~/.netrc",
    "~/.git-credentials",
    "~/.docker",
    "~/.kube",
    "~/.npmrc",
    "~/.pypirc",
    "~/.cargo/credentials.toml",
)


@dataclass(frozen=True)
class ClaudeSandbox:
    enabled: bool
    fail_if_unavailable: bool
    allow_unsandboxed_commands: bool
    allow_write: tuple[str, ...]
    allowed_domains: tuple[str, ...]
    deny_read: tuple[str, ...] = ()
    allow_read: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompiledPolicy:
    name: str
    version: str
    mcp_allowed_tools: frozenset[str]
    mcp_denied_tools: frozenset[str]
    hook_denied_tools: frozenset[str]
    stripped_env_prefixes: tuple[str, ...]
    stripped_env_names: frozenset[str]
    send_message_mode: str = "reply_only"
    builtin_allowed_tools: frozenset[str] = field(default_factory=frozenset)
    env: dict[str, str] = field(default_factory=dict)
    bootstrap_marker: str = ""
    hook_path: Path | None = None
    hook_sha256: str | None = None
    claude_sandbox: ClaudeSandbox | None = None


def compile_policy(name: str) -> CompiledPolicy:
    hook_path = Path(__file__).resolve().parents[1] / "hooks" / "pre_tool_use.py"
    hook_sha256 = hashlib.sha256(hook_path.read_bytes()).hexdigest() if hook_path.exists() else ""
    denied_mcp_tools = frozenset(
        {"dispatch_agent", "cancel_dispatch", "register_actor", "register_agent"}
    )
    denied_hook_tools = frozenset(
        {
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
    )
    mailbox_tools = frozenset(
        {
            "list_inbox",
            "read_message",
            "ack_message",
            "close_message",
            "send_message",
            "post_status",
            "wait_for_reply",
        }
    )

    if name == OPERATOR_MAILBOX_POLICY:
        bootstrap_marker = f"WakePolicy={OPERATOR_MAILBOX_POLICY}"
        return CompiledPolicy(
            name=OPERATOR_MAILBOX_POLICY,
            version=OPERATOR_MAILBOX_POLICY_VERSION,
            mcp_allowed_tools=mailbox_tools | {"list_actors", "read_handoff"},
            mcp_denied_tools=denied_mcp_tools,
            hook_denied_tools=denied_hook_tools,
            stripped_env_prefixes=("AWS_", "AZURE_", "CLOUDSDK_", "GOOGLE_", "GCP_"),
            stripped_env_names=frozenset(
                {
                    "ANTHROPIC_API_KEY",
                    "AGENT_COMMS_ADMIN_TOKEN",
                    "GH_TOKEN",
                    "GITHUB_TOKEN",
                    "OPENAI_API_KEY",
                    "SSH_AGENT_PID",
                    "SSH_AUTH_SOCK",
                }
            ),
            send_message_mode="unrestricted",
            env={
                "WAKE_POLICY": OPERATOR_MAILBOX_POLICY,
                "WAKE_POLICY_VERSION": OPERATOR_MAILBOX_POLICY_VERSION,
                "AGENT_COMMS_POLICY_BOOTSTRAP_MARKER": bootstrap_marker,
                "AGENT_COMMS_POLICY_HOOK_SHA256": hook_sha256,
                "AGENT_COMMS_POLICY_HOOK_PATH": str(hook_path),
            },
            bootstrap_marker=bootstrap_marker,
            hook_path=hook_path,
            hook_sha256=hook_sha256,
            claude_sandbox=None,
        )

    if name != WORKER_DISPATCH_POLICY:
        raise ValueError(f"unknown policy: {name}")

    bootstrap_marker = f"WakePolicy={WORKER_DISPATCH_POLICY}"
    return CompiledPolicy(
        name=WORKER_DISPATCH_POLICY,
        version=WORKER_DISPATCH_POLICY_VERSION,
        mcp_allowed_tools=mailbox_tools | {"close_dispatch", "whoami"},
        mcp_denied_tools=denied_mcp_tools,
        hook_denied_tools=denied_hook_tools,
        builtin_allowed_tools=frozenset(
            {
                "Read",
                "Edit",
                "Write",
                "MultiEdit",
                "Glob",
                "Grep",
                "TodoWrite",
                "Bash",
            }
        ),
        # PATH is intentionally preserved: workers need local tools for reads and diffs;
        # credentials are stripped so external writes fail even if binaries exist.
        stripped_env_prefixes=("AWS_", "AZURE_", "CLOUDSDK_", "GOOGLE_", "GCP_"),
        stripped_env_names=frozenset(
            {
                "ANTHROPIC_API_KEY",
                "AGENT_COMMS_ADMIN_TOKEN",
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "OPENAI_API_KEY",
                "SSH_AGENT_PID",
                "SSH_AUTH_SOCK",
            }
        ),
        send_message_mode="reply_only",
        env={
            "WAKE_POLICY": WORKER_DISPATCH_POLICY,
            "WAKE_POLICY_VERSION": WORKER_DISPATCH_POLICY_VERSION,
            "AGENT_COMMS_POLICY_BOOTSTRAP_MARKER": bootstrap_marker,
            "AGENT_COMMS_POLICY_HOOK_SHA256": hook_sha256,
            "AGENT_COMMS_POLICY_HOOK_PATH": str(hook_path),
        },
        bootstrap_marker=bootstrap_marker,
        hook_path=hook_path,
        hook_sha256=hook_sha256,
        claude_sandbox=ClaudeSandbox(
            enabled=True,
            fail_if_unavailable=True,
            allow_unsandboxed_commands=False,
            allow_write=(".",),
            allowed_domains=(),
            deny_read=CREDENTIAL_READ_DENY,
            allow_read=(),
        ),
    )


def active_policy_from_env(env: dict[str, str]) -> CompiledPolicy | None:
    policy_name = env.get("WAKE_POLICY", "").strip()
    if not policy_name:
        return None
    return compile_policy(policy_name)


def scoped_env(base_env: dict[str, str], policy: CompiledPolicy) -> dict[str, str]:
    scoped = {
        key: value
        for key, value in base_env.items()
        if key not in policy.stripped_env_names
        and not any(key.startswith(prefix) for prefix in policy.stripped_env_prefixes)
    }
    scoped.update(policy.env)
    return scoped
