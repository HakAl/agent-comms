# MCP setup

`agent-comms` exposes a stdio MCP server. Each agent CLI launches it as a
subprocess, so there are no ports to manage and no conflicts between
terminals.

## Prerequisites

1. Install [uv](https://docs.astral.sh/uv/).
2. Sync the environment with the MCP extra:
   ```sh
   uv sync --extra mcp
   ```
3. Describe your actors and register them:
   ```sh
   cp config/actors.example.json config/actors.json
   $EDITOR config/actors.json
   scripts/agent-comms bootstrap
   ```

The launcher `scripts/agent-comms-mcp` executes the checkout's `.venv`
directly and refuses to start if the environment is missing or the `mcp`
package is not installed.

## One server per seat, bound to one identity

The server takes `--actor-id` and binds every tool call to that actor. It
refuses to start for an actor that is not registered or not launchable.
Register it once per architect seat, with that seat's id, and never expose two
differently named `agent-comms` servers to the same session.

### Claude Code

```sh
claude mcp add agent-comms -- /absolute/path/to/scripts/agent-comms-mcp --actor-id team-a-architect
claude mcp list
```

### Codex CLI

```sh
codex mcp add agent-comms -- /absolute/path/to/scripts/agent-comms-mcp --actor-id team-a-architect
codex mcp list
```

Codex writes this to `~/.codex/config.toml`. Prefer a per-profile or
per-project entry over a global one so each seat keeps its own identity.

### Any other MCP client

Point it at the same launcher with the same `--actor-id` argument. The server
speaks plain stdio MCP and needs nothing else.

## Checking the binding

From inside the agent session, call the `whoami` tool. It returns the bound
actor. From a shell, `scripts/agent-comms actors` lists every registered
actor.

## Workers

Worker seats are not configured this way. When an architect dispatches to a
worker, the runtime adapter starts the worker with a generated MCP
configuration and a restricted tool policy. Nothing needs to be added to the
worker's CLI by hand.
