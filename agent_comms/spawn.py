from __future__ import annotations

import json
import difflib
import shlex
import sys

from .policies import WORKER_DISPATCH_POLICY_VERSION, compile_policy
from .schema import ValidationError
from .store import WORKER_DISPATCH_POLICY

ALLOWED_RUNTIMES = ("claude", "codex", "fake")
CODEX_ARGS_INTRODUCED = ("--json",)


def worker_prompt_slot(args: list, marker: str) -> int | None:
    """Find the shared placeholder or final baked bootstrap prompt."""
    string_args = [str(arg) for arg in args]
    indexes = [
        index for index, arg in enumerate(string_args) if arg == "{worker_prompt}"
    ]
    if len(indexes) > 1:
        raise RuntimeError("spawn args must include exactly one worker prompt slot")
    if indexes:
        return indexes[0]
    if string_args and marker in string_args[-1]:
        return len(string_args) - 1
    return None


def rerender_spawn_block(actor: dict) -> tuple[str, dict]:
    """Accept exactly the existing template prefix, preserving the prompt and env."""
    if actor.get("runtime") != "codex":
        raise ValidationError("wrong runtime: rerender-spawn requires codex")
    stored = actor.get("spawn")
    if not isinstance(stored, dict) or not stored:
        raise ValidationError("no stored spawn block")
    args = stored.get("args")
    env = stored.get("env", {})
    if not isinstance(args, list) or not isinstance(env, dict):
        raise ValidationError("stored spawn args or env has an invalid shape")
    rendered = render_spawn("codex", actor["id"], codex_home=env.get("CODEX_HOME"))
    marker = compile_policy(WORKER_DISPATCH_POLICY).bootstrap_marker
    try:
        slot = worker_prompt_slot(args, marker)
    except RuntimeError as exc:
        raise ValidationError(str(exc)) from exc
    if slot is None:
        raise ValidationError("no worker prompt slot")
    if slot != len(args) - 1:
        raise ValidationError("worker prompt slot is not the last element")
    if stored.get("command") != rendered["command"]:
        raise ValidationError("stored command differs from template command")
    rendered_slot = worker_prompt_slot(rendered["args"], marker)
    prefix = rendered["args"][:rendered_slot]
    if args[:slot] == prefix:
        return "no-op", stored
    if args[:slot] != [arg for arg in prefix if arg not in CODEX_ARGS_INTRODUCED]:
        raise ValidationError(
            f"stored prefix differs beyond introduced elements: stored={args[:slot]!r}; rendered={prefix!r}"
        )
    rendered["args"][rendered_slot] = args[slot]
    rendered["env"] = env
    return "update", rendered


def rerender_spawns(
    store,
    *,
    actor_id=None,
    runtime=None,
    apply=False,
    yes=False,
    override_protected=None,
) -> dict:
    """Plan and optionally apply the operator's bounded template update."""
    from .cli._helpers import require_unprotected_or_override

    if (actor_id is None) == (runtime is None):
        raise ValidationError("select one actor or --runtime codex")
    if runtime is not None and runtime != "codex":
        raise ValidationError("wrong runtime: rerender-spawn requires codex")
    actors = (
        [actor for actor in store.list_actors() if actor["id"] == actor_id]
        if actor_id is not None
        else [actor for actor in store.list_actors() if actor.get("runtime") == runtime]
    )
    if actor_id is not None and not actors:
        raise ValidationError(f"unknown actor: {actor_id}")
    results = []
    for actor in actors:
        entry = {"actor_id": actor["id"]}
        try:
            status, block = rerender_spawn_block(actor)
            entry.update(status=status, spawn=block)
            before = json.dumps(actor["spawn"], indent=2, sort_keys=True).splitlines(
                keepends=True
            )
            after = json.dumps(block, indent=2, sort_keys=True).splitlines(
                keepends=True
            )
            entry["diff"] = "".join(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile=f"{actor['id']}/stored",
                    tofile=f"{actor['id']}/rendered",
                )
            )
            if entry["diff"]:
                print(entry["diff"])
        except ValidationError as exc:
            entry.update(status="refused", reason=str(exc), diff="")
        results.append(entry)
    if apply and runtime is not None and not yes:
        if not sys.stdin.isatty():
            raise ValidationError("fleet-wide --apply requires --yes on non-tty stdin")
        print("Apply these spawn updates? Type yes:", flush=True)
        if sys.stdin.readline().strip() != "yes":
            raise ValidationError("fleet-wide --apply confirmation refused")
    if apply:
        for entry in results:
            if entry["status"] != "update":
                continue
            try:
                override = require_unprotected_or_override(
                    store, entry["actor_id"], override_protected
                )
                if override is not None:
                    entry["override_protected"] = override
                if not store.update_actor_spawn(entry["actor_id"], entry["spawn"]):
                    raise ValidationError("actor disappeared before spawn update")
                reread = next(
                    (
                        actor
                        for actor in store.list_actors()
                        if actor["id"] == entry["actor_id"]
                    ),
                    {},
                )
                entry["verified"] = reread.get("spawn") == entry["spawn"]
                print(
                    f"{entry['actor_id']}: stored spawn equals written block: {entry['verified']}"
                )
            except ValidationError as exc:
                entry.update(status="refused", reason=str(exc))
    return {
        "actors": results,
        "counts": {
            status: sum(entry["status"] == status for entry in results)
            for status in ("no-op", "update", "refused")
        },
    }


# The reply+close+exit instruction below is COOPERATIVE guidance to the worker
# model, not a protocol boundary and not mechanically enforced by this text. It
# is prevention only: it asks a well-behaved one-shot worker to close its own
# trigger on every terminal outcome so a finished dispatch does not sit
# in_flight until TTL. A worker that ignores it, crashes, hits a capacity
# failure, or errors out is recovered mechanically instead, by the per-dispatch
# supervisor and the monitor reconciliation path (dead-worker terminalization),
# never by prompt compliance. Do not read this string as a guarantee that the
# trigger was closed.
DEFAULT_WORKER_PROMPT = (
    "WakePolicy=worker_dispatch_readwrite_bounded. You are actor {actor_id}. "
    "Call mcp__agent-comms__list_inbox, find message {message_id}, read it via "
    "read_message, then perform exactly the work it describes. Reach one terminal "
    "outcome, then always finish the protocol the same way no matter what that "
    "outcome is - whether the work succeeded, you are BLOCKED, you refuse, or "
    "nothing needed to change: send exactly one precise reply via send_message "
    "with parent_message_id={message_id} that states the outcome and summarizes "
    "exactly what you changed (files + what) or why nothing changed, then close "
    "the triggering message via close_message, then exit. Closing the trigger "
    "only records that you reached a terminal outcome and reported it; it never "
    "claims the requested work succeeded. Never exit while the trigger is still "
    "open."
)

V2_WORKER_PROMPT = (
    "WakePolicy=worker_dispatch_readwrite_bounded. You are actor {actor_id}. "
    "Call mcp__agent-comms__list_inbox, find message {message_id}, read it via read_message, "
    "then perform exactly the work it describes. Reach one terminal outcome and send exactly "
    "one precise threaded reply via send_message with parent_message_id={message_id}. Then call "
    "close_dispatch binding that reply with the truthful result: satisfied, or blocked with a "
    "non-empty reason; set delta=True iff your work is the worktree delta. Never call close_message "
    "for this v2 trigger and never exit with it open. Closeout records the outcome, not correctness."
)


def render_spawn(runtime: str, actor_id: str, *, codex_home: str | None = None) -> dict:
    """Render a runtime spawn block for an agent actor.

    This is intentionally pure: it only returns placeholder-bearing data for
    the runtime adapters to resolve at dispatch time.
    """
    runtime = runtime.strip()
    if runtime not in ALLOWED_RUNTIMES:
        allowed = ", ".join(ALLOWED_RUNTIMES)
        raise ValidationError(f"unsupported runtime {runtime!r}; allowed: {allowed}")

    if runtime == "codex":
        return _codex_spawn(codex_home)
    if runtime == "claude":
        return _claude_spawn()
    return _fake_spawn()


def _codex_spawn(codex_home: str | None) -> dict:
    env_home = codex_home if codex_home is not None else "{codex_home}"
    return {
        "command": "codex",
        "args": [
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--profile",
            "{actor_id}",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "workspace-write",
            "{worker_prompt}",
        ],
        "env": {"CODEX_HOME": env_home},
    }


def _claude_spawn() -> dict:
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
                        "AGENT_COMMS_PROJECT_ROOT": "{project_root}",
                        "WAKE_POLICY": WORKER_DISPATCH_POLICY,
                        "WAKE_POLICY_VERSION": WORKER_DISPATCH_POLICY_VERSION,
                    },
                }
            }
        }
    )


def claude_settings_for_policy(policy, hooks_path: str, python: str, *, quote: bool = True) -> str:  # type: ignore[no-untyped-def]
    """Render the Claude ``--settings`` JSON for a compiled policy.

    The PreToolUse hook runs as a shell command, so at dispatch both the
    interpreter and the hook path are ``shlex.quote``d: an install under a
    path with a space is one argument, not two. The spawn template passes
    the ``{python}`` and ``{hooks_path}`` placeholders with ``quote=False``;
    the adapter re-renders the real values at dispatch.
    """
    if quote:
        hook_command = f"{shlex.quote(python)} {shlex.quote(hooks_path)}"
    else:
        hook_command = f"{python} {hooks_path}"
    allowed_tools = [
        f"mcp__agent-comms__{tool_name}"
        for tool_name in sorted(policy.mcp_allowed_tools)
    ] + sorted(policy.builtin_allowed_tools)
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {
                            "command": hook_command,
                            "type": "command",
                        }
                    ],
                    "matcher": "",
                }
            ]
        },
        "permissions": {"allow": allowed_tools},
    }
    if policy.claude_sandbox is not None and policy.claude_sandbox.enabled:
        sandbox = policy.claude_sandbox
        filesystem = {"allowWrite": list(sandbox.allow_write)}
        if sandbox.deny_read:
            filesystem["denyRead"] = list(sandbox.deny_read)
        if sandbox.allow_read:
            filesystem["allowRead"] = list(sandbox.allow_read)
        settings["sandbox"] = {
            "enabled": sandbox.enabled,
            "failIfUnavailable": sandbox.fail_if_unavailable,
            "allowUnsandboxedCommands": sandbox.allow_unsandboxed_commands,
            "filesystem": filesystem,
            "network": {"allowedDomains": list(sandbox.allowed_domains)},
        }
    # The allow-list is resolved live at dispatch from the compiled policy,
    # not frozen in spawn_json. It is harness convenience, not the boundary:
    # the PreToolUse hook remains the boundary. These built-ins are already
    # hook-permitted; Read/Edit/MultiEdit/Write are hook-path-confined to
    # project_root. The OS sandbox confines Bash writes to cwd and, for Claude
    # workers, denies Bash reads of named credential paths as a non-overridable
    # floor while allowing the rest of home so worker toolchains can run.
    # Toolchain read is restored; credential read remains denied. Remaining
    # tracked residuals: Claude Glob/Grep tool reads are neither hook-confined
    # nor sandboxed, so they can still read outside project_root; Codex has no
    # read-confinement (full-disk read; openai/codex#11316). Both are tracked
    # in tech-debt.
    return json.dumps(
        settings,
        sort_keys=True,
        separators=(",", ":"),
    )


def _claude_settings() -> str:
    return claude_settings_for_policy(
        compile_policy(WORKER_DISPATCH_POLICY), "{hooks_path}", "{python}", quote=False
    )


def _fake_spawn() -> dict:
    # {python} resolves at dispatch time to the interpreter running the
    # dispatch, which can import agent_comms by definition; a bare "python3"
    # only worked when the project environment happened to be first on PATH.
    policy = compile_policy(WORKER_DISPATCH_POLICY)
    return {
        "command": "{python}",
        "args": [
            "-m",
            "agent_comms.adapters.fake_worker",
            "--actor-id",
            "{actor_id}",
            "--message-id",
            "{message_id}",
            "--db",
            "{db_path}",
            policy.bootstrap_marker,
        ],
    }


def _escaped_json(value: dict) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    placeholders = (
        "actor_id",
        "message_id",
        "db_path",
        "project_root",
        "codex_home",
        "mcp_command",
        "hooks_path",
        "claude_binary",
        "python",
    )
    sentinels = {name: f"@@AGENT_COMMS_PLACEHOLDER_{index}@@" for index, name in enumerate(placeholders)}
    for name, sentinel in sentinels.items():
        text = text.replace("{" + name + "}", sentinel)
    text = text.replace("{", "{{").replace("}", "}}")
    for name, sentinel in sentinels.items():
        text = text.replace(sentinel, "{" + name + "}")
    return text
