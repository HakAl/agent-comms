# MCP Setup

`agent-comms` exposes a stdio MCP server. Each architect CLI launches the server as a subprocess, so there are no port conflicts across architect terminals.

## Prerequisites

1. Install [uv](https://docs.astral.sh/uv/) (used by the wrapper scripts).
2. Sync the optional MCP dependency:
   ```bash
   uv sync --extra mcp
   ```
3. Create your agent registry:
   ```bash
   cp config/agents.example.json config/agents.json
   # edit config/agents.json with your real team ids and project roots
   scripts/agent-comms bootstrap
   ```

## Add the MCP server to each CLI

The wrapper script `scripts/agent-comms-mcp` launches the stdio MCP server. Register it once per architect CLI.

### Claude Code

```bash
claude mcp add agent-comms -- /absolute/path/to/scripts/agent-comms-mcp
claude mcp list
```

### Codex CLI

```bash
codex mcp add agent-comms -- /absolute/path/to/scripts/agent-comms-mcp
codex mcp list
```

Codex also reads `~/.codex/config.toml`; the CLI command above writes the same config.

### Gemini CLI

```bash
gemini mcp add agent-comms /absolute/path/to/scripts/agent-comms-mcp
gemini mcp list
```

Gemini stores MCP config under `~/.gemini/settings.json`; use the CLI command above to avoid hand-editing the schema.

## Tested CLI versions

| CLI         | Version  |
|-------------|----------|
| Claude Code | 2.1.143  |
| Codex CLI   | 0.130.0  |
| Gemini CLI  | 0.42.0   |

## Identity caveat

In the current implementation, the MCP server is identity-neutral: the architect passes its `agent_id` as a tool argument on each call. Nothing prevents a confused architect from sending as another. See `docs/DESIGN.md` for the planned per-architect MCP subprocess pattern that closes this gap.
