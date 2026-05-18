# Harness Integration Notes

The agent harnesses already provide useful configuration surfaces. Prefer those before adding more runtime machinery to `agent-comms`.

Checked locally:

- Claude Code `2.1.143`: MCP management, `--mcp-config`, `--append-system-prompt`, settings, hooks/plugins, `--tmux`, `--name`.
- Codex CLI `0.130.0`: `~/.codex/config.toml`, MCP server config, tool approval config, profiles/config overrides, remote-control.
- Gemini CLI `0.42.0`: MCP management, hooks management, settings, session ids, extensions.

## Principle

Use the harness config to launch each architect with the correct identity and operating prompt.

Do not rely on an architect choosing its own identity at runtime.

Preferred shape:

```text
harness config -> agent-comms MCP subprocess --agent-id <architect-id>
```

The MCP server process owns identity for that session. Architect tools derive `from_type=architect` and `from_id=<agent-id>` from process config.

## Per-Architect MCP

Each architect session should expose exactly one `agent-comms` MCP server, launched with that architect's id:

```bash
./scripts/agent-comms-mcp --agent-id team-a-architect
./scripts/agent-comms-mcp --agent-id team-b-architect
./scripts/agent-comms-mcp --agent-id team-c-architect
./scripts/agent-comms-mcp --agent-id team-d-architect
```

Do not expose four differently named architect MCP servers to one agent session. That gives the model an opportunity to pick the wrong identity.

Operator and dashboard paths are different: they may send as `from_type=operator`, `from_id=human-operator`.

## Current Local State

Codex already has a global MCP entry:

```toml
[mcp_servers.agent-comms]
command = "/path/to/agent-comms/scripts/agent-comms-mcp"
```

This is useful for early testing, but it does not yet provide per-architect identity. Before relying on server-derived sender identity, replace global unscoped config with per-session or per-profile config that passes `--agent-id`.

Claude and Gemini currently report no configured MCP servers from their CLI list commands. Add their per-architect MCP config through harness commands or per-session config files.

## Hooks And Receiver State

Use harness hooks where they exist to update advisory receiver state:

```bash
/path/to/agent-comms/scripts/agent-comms receiver-state team-a-architect --state idle
/path/to/agent-comms/scripts/agent-comms receiver-state team-a-architect --state busy
/path/to/agent-comms/scripts/agent-comms receiver-state team-a-architect --state blocked
```

Receiver state is advisory, not authoritative. Hooks improve wake safety, but the mailbox and `.agent-comms/<agent-id>/new_messages` semaphore remain the reliable delivery path.

Suggested mapping:

- Session start: `list_inbox`, `list_status`, receiver state `busy`.
- Before long idle/pause: `post_status(... blocked_on="idle")`, receiver state `idle`.
- Tool approval or permission gate: receiver state `blocked`.
- Stop/notification hooks: `post_status` or receiver state update if the harness exposes enough context.

Claude and Gemini have explicit hook surfaces. Codex config should be checked for equivalent hook or notification features before writing custom polling.

## Prompt Injection Boundary

Harness config can prime behavior, but should stay short. Detailed rules belong in:

```text
docs/communication_contract.md
```

AGENTS.md should only say:

- which architect id applies,
- check inbox/status on start,
- default is not to message,
- check the semaphore at natural breakpoints,
- post status before stopping substantial work.

## Implementation Order

1. Add `--agent-id` support to `agent-comms-mcp`.
2. Create one launcher/config snippet per architect per harness.
3. Replace unscoped Codex MCP config with scoped profile/session config.
4. Add Claude/Gemini per-architect MCP config.
5. Add hook shims only after MCP identity is working.
6. Keep tmux wakeup optional and idle-gated.
