# MCP setup

`agent-comms` exposes a stdio MCP server. Each agent CLI launches it as a
subprocess, so there are no ports to manage and no conflicts between
terminals.

## Prerequisites

1. Install [uv](https://docs.astral.sh/uv/).
2. Sync the environment:
   ```sh
   uv sync
   ```
3. Set up a team; this registers the actors and binds the architect seat
   in the clients you name:
   ```sh
   .venv/bin/agent-comms setup --project-root /absolute/path/to/your/project \
       --team team-a --runtimes claude,codex --clients claude,codex
   ```
   `setup` runs the `claude mcp add` and `codex mcp add` commands shown
   below itself. The rest of this page is what it ran, for another MCP
   client, for a seat added by hand, and for `agent-comms doctor`, which
   checks the binding and prints the exact command to repair it.

`agent-comms-mcp` is a console script of the environment that holds the
package: `.venv/bin/agent-comms-mcp` in a checkout after `uv sync`, on `PATH`
when the package is installed with `uv tool install` or `pipx`. It runs on
that environment's interpreter, never through `uv run`, prints one
`agent-comms startup:` line to stderr so a client log can be matched to a
release, and refuses to start when the `mcp` package cannot be imported.
The examples below use the checkout form; an installed package uses
`"$(command -v agent-comms-mcp)"` instead of the `.venv/bin` path (a symlink
into the tool environment, which `doctor` accepts as the same script), or
the absolute path `setup` prints under `mcp.applied`.

## One server per seat, bound to one identity

The server takes `--actor-id` and binds every tool call to that actor. It
refuses to start for an actor that is not registered or not launchable.
Register it once per architect seat, with that seat's id, and never expose two
differently named `agent-comms` servers to the same session.

### Claude Code

```sh
cd /absolute/path/to/your/project
claude mcp add agent-comms --scope local -- /absolute/path/to/checkout/.venv/bin/agent-comms-mcp --actor-id team-a-architect
claude mcp list
```

Claude Code keeps a local-scope entry per project directory in
`~/.claude.json`, so the command runs from the project directory and the
seat belongs to that directory.

### Codex CLI

```sh
codex mcp add agent-comms -- /absolute/path/to/checkout/.venv/bin/agent-comms-mcp --actor-id team-a-architect
codex mcp list
```

Codex writes this to `~/.codex/config.toml`, one entry per user.

### One seat per scope

Those scopes are limits of the clients: Claude Code holds one `agent-comms`
seat per project directory and Codex one per user, so a second team cannot
share a seat. `setup` refuses a seat that is already bound to another actor
(or to the same architect on another ledger) and names the holder;
`setup --replace-seat` hands it over: for Claude `claude mcp remove
agent-comms --scope local` and then `add` (since `add` refuses a duplicate),
for Codex `add` alone (it overwrites). The registry's `seats` map records
which clients hold each architect's seat, so `doctor` checks those and stops
expecting a seat that changed hands.

### A ledger other than the default one

The server opens `~/.agent-comms/agent-comms.sqlite` unless told otherwise.
When the team was set up on another ledger (`--db`, or `AGENT_COMMS_DB` in
the shell) the seat's argv carries `--db /absolute/path/to/that/ledger` as
well, since the architect is registered there and not in the default
ledger. `setup` writes the absolute path; `doctor` treats a relative or
`~` path as a wrong binding, because the client starts the server without
a shell and from its own directory.

### Any other MCP client

Point it at the same command with the same `--actor-id` argument. The server
speaks plain stdio MCP and needs nothing else.

## Checking the binding

From inside the agent session, call the `whoami` tool. It returns the bound
actor. From a shell, `agent-comms doctor` reports each recorded seat as
`mcp:<architect>:<client>`: the actor it binds, the ledger, and the server
command, with the `mcp add` (or `remove` and `add`) line that repairs a
wrong one. `agent-comms actors` lists every registered actor.

## Workers

Worker seats are not configured this way. When an architect dispatches to a
worker, the runtime adapter starts the worker with a generated MCP
configuration and a restricted tool policy. Nothing needs to be added to the
worker's CLI by hand.
