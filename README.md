# agent-comms

A local mailbox for coordinating multiple terminal-based AI coding agents — Claude Code, Codex CLI, Gemini CLI, or anything else that speaks MCP.

When you run several agent CLIs in parallel (one per team or one per repo) and they need to coordinate, you end up relaying messages by hand: "team A found X, tell team B." `agent-comms` replaces that relay with a tiny local SQLite mailbox that every CLI can read and write through standard MCP tools.

## Development direction

This checkout still implements the original mailbox. The full local workflow
(bounded dispatch, review by a different model family, and signed human
approval) already runs in the maintainer's own deployment and is being ported
here, **macOS first, then Linux**. Until that lands, those capabilities are not
in this repository.

Start with [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) to help.
[The roadmap](docs/ROADMAP.md) describes the layers, milestones, and release
checks. Basic messaging remains useful on its own and does not require both
Claude and Codex.

## What it is today

- A **SQLite mailbox** (`messages`, `statuses`, `agents` tables) with WAL enabled.
- A **stdio MCP server** exposing 10 tools: `send_message`, `list_inbox`, `read_message`, `ack_message`, `close_message`, `wait_for_reply`, `post_status`, `list_status`, `register_agent`, `list_agents`.
- A **CLI** (`agent-comms`) for human inspection and scripting.
- **No daemons, no networking, no cloud.** Everything runs locally on one workstation.

## Current boundaries

- Not an orchestrator. Architects still drive themselves.
- Not a replacement for A2A or BeeAI ACP. The schema borrows ideas (agent cards, refs, tasks) but the implementation is intentionally smaller.
- Not a message queue. There's no broker, no fan-out, no retries.
- Not opinionated about which CLI you use. It's just a mailbox.

## Status

| Component | State in this checkout |
|-----------|------------------------|
| SQLite mailbox + CLI | Implemented |
| MCP stdio server | Implemented; caller-supplied identity |
| Architect prompt integration | Available in `docs/architect-prompt.md` |
| Full local execution workflow and reliable upgrades | Planned in [ROADMAP.md](docs/ROADMAP.md) |

The original tmux/dashboard/A2A proposals in [DESIGN.md](docs/DESIGN.md) are
historical context, not the current implementation queue. Platform certification
is a release goal, not a claim made by this status table.

Known gap: per-architect MCP identity is server-derived only in design. Today the architect passes its `agent_id` per call; nothing structurally prevents spoofing. Fine for a trusted single-workstation setup; consequential if you ever expose this beyond localhost.

## Quickstart

```bash
# 1. Install dependencies
uv sync --extra mcp

# 2. Create your agent registry
cp config/agents.example.json config/agents.json
$EDITOR config/agents.json    # set your real team ids and project_root paths

# 3. Bootstrap the mailbox
scripts/agent-comms bootstrap

# 4. Smoke test
scripts/agent-comms send \
    --from-agent team-a-architect \
    --to team-b-architect \
    --subject "test" \
    --body "hello from team-a"

scripts/agent-comms inbox team-b-architect
```

To wire into a CLI, see [`docs/mcp-setup.md`](docs/mcp-setup.md).

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Claude Code │     │ Codex CLI   │     │ Gemini CLI  │
│ (team-a)    │     │ (team-b)    │     │ (team-c)    │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │ stdio MCP         │ stdio MCP         │ stdio MCP
       ▼                   ▼                   ▼
┌─────────────────────────────────────────────────────┐
│             agent-comms MCP server                  │
│                                                     │
│   send_message / list_inbox / ack / post_status /   │
│   wait_for_reply / list_status / ...                │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ SQLite (WAL)    │
              │ data/agent-     │
              │ comms.sqlite    │
              └─────────────────┘
```

Each architect calls the same set of MCP tools. The mailbox is the source of truth; architects pull rather than push. `wait_for_reply` provides a blocking idle pattern for synchronous handoffs.

## Tool surface

| Tool              | Purpose                                       |
|-------------------|-----------------------------------------------|
| `register_agent`  | Register or refresh an architect identity     |
| `list_agents`     | List all registered architects                |
| `send_message`    | Send a message with optional file refs        |
| `list_inbox`      | List messages addressed to an architect       |
| `read_message`    | Read and mark-read a message                  |
| `ack_message`     | Acknowledge with optional response            |
| `close_message`   | Close a recipient's copy of a message         |
| `wait_for_reply`  | Block until a new message arrives or timeout  |
| `post_status`     | Publish current files / blockers / next step  |
| `list_status`     | Get the latest status from each architect     |

## Operating model

Architects are expected to:

1. Check inbox at session start.
2. Use file refs (paths + summaries) instead of pasting large content.
3. Acknowledge messages that affect their lane.
4. Post status before ending substantial work.

This is enforced by prompt discipline, not by the mailbox. See [`docs/architect-prompt.md`](docs/architect-prompt.md) for the operating snippet to paste into each architect's system prompt or AGENTS.md.

For loop prevention and message discipline, see [`docs/communication_contract.md`](docs/communication_contract.md). For per-CLI MCP and hook configuration notes, see [`docs/harness_integration.md`](docs/harness_integration.md).

## Comparison to nearby projects

| Project | What it does | Why agent-comms is different |
|---------|--------------|------------------------------|
| [Claude Code Agent Teams](https://code.claude.com/docs/en/agent-teams) | Tmux-spawned teammates with shared task list and mailbox | Claude-only; agent-comms is vendor-neutral |
| [awslabs/cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator) | Supervisor/worker via MCP with per-terminal state | Heavier framework; agent-comms is just the mailbox |
| [Dicklesworthstone/mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail) | FastMCP + SQLite + git inbox/outbox | Adds git layer; agent-comms is plain SQLite |
| [njbrake/agent-of-empires](https://github.com/njbrake/agent-of-empires) | Tmux session manager for multi-CLI agents | Manages sessions; agent-comms only handles messages |

If you want a full session manager with TUI/web dashboard, look at Agent of Empires or CAO. If you want a small, vendor-neutral mailbox you can drop into any setup, this is it.

## Testing

```bash
make test
```

Every test process runs in a scratch home and never touches `~/.agent-comms`.
Tests that need a real runtime login are skipped. `make gate` runs every
check a push must pass. See
[the contributor test instructions](CONTRIBUTING.md#development-setup-and-tests)
and [the gates](CONTRIBUTING.md#gates).

## License

[GPLv3](https://www.gnu.org/licenses/gpl-3.0.en.html).
