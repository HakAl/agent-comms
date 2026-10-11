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
   `setup` runs the `claude mcp add` command and writes the Codex project
   file shown below itself. The rest of this page is what it did, for
   another MCP client, for a seat added by hand, and for
   `agent-comms doctor`, which checks the binding and prints the exact
   command to repair it. For a team that already exists,
   `agent-comms bind-seat <architect> --client claude|codex` binds one
   client and records it.

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

`setup` writes an `[mcp_servers.agent-comms]` entry to
`<project>/.codex/config.toml` and never touches `~/.codex/config.toml`:

```toml
[mcp_servers.agent-comms]
command = "/absolute/path/to/checkout/.venv/bin/agent-comms-mcp"
args = ["--actor-id", "team-a-architect", "--db", "/absolute/path/to/home/.agent-comms/agent-comms.sqlite"]
cwd = "/absolute/path/to/your/project"
enabled = true
disabled_tools = []
env_vars = []

[mcp_servers.agent-comms.env]
AGENT_COMMS_ACTOR_ID = "team-a-architect"
AGENT_COMMS_DB = "/absolute/path/to/home/.agent-comms/agent-comms.sqlite"
AGENT_COMMS_HOME = ""
AGENT_COMMS_PROJECT_ROOT = "/absolute/path/to/your/project"
WAKE_POLICY = ""
WAKE_POLICY_VERSION = ""
```

Codex merges an entry field by field across its config layers, so the
entry pins every field a user-level entry of the same name could otherwise
leak in (`env`, `env_vars`, `cwd`, `enabled`, `disabled_tools`); an empty
variable reads as unset. The ledger is always named, in `--db` and in
`AGENT_COMMS_DB`, even the default one, since another layer could set it. Other
settings already in the file are kept; a file that defines `agent-comms`
in a form `setup` did not write (an inline table, dotted keys) is refused
rather than edited.

Codex reads the file only in a trusted project: open `codex` in the
project once and accept the trust prompt. It then finds the entry from the
project root and, in a git project, from any subdirectory, since it looks
for `.codex/` up to the git root. Outside git it reads only the directory
it starts in, so a non-git project is seated at its root only. In another
directory the entry does not apply.

In a git project the file is added to the exclude file git reports
(`git rev-parse --git-path info/exclude`, which also covers worktrees),
never to `.gitignore`. A project that tracks `.codex/config.toml` is
refused with the reason. A non-git project gets the entry and a note that
nothing excludes it.

### One seat per scope

Claude Code and Codex each hold one `agent-comms` seat per project
directory, so two teams in one project cannot share a seat. `setup` and
`bind-seat` refuse a seat that is already bound to another actor (or to
the same architect on another ledger or install) and name the holder;
`--replace-seat` hands it over: for Claude `claude mcp remove agent-comms
--scope local` and then `add` (since `add` refuses a duplicate), for Codex
a rewrite of the project entry. A seat already bound to the same architect
is left alone. The registry's `seats` map records which clients hold each
architect's seat, so `doctor` checks those and stops expecting a seat that
changed hands.

### A ledger other than the default one

The server opens `~/.agent-comms/agent-comms.sqlite` unless told otherwise.
When the team was set up on another ledger (`--db`, or `AGENT_COMMS_DB` in
the shell) the seat's argv carries `--db /absolute/path/to/that/ledger` as
well, since the architect is registered there and not in the default
ledger. `setup` writes the absolute path; `doctor` treats a relative or
`~` path as a wrong binding, because the client starts the server without
a shell and from its own directory.

### A second install (`AGENT_COMMS_HOME`)

With `AGENT_COMMS_HOME` set, the registry, ledger, logs, keys, custody
directories and supervisor sockets all live under it instead of
`~/.agent-comms`, and nothing under `~/.agent-comms` is read or written.
The seat's argv carries `--home /absolute/path` so the client starts the
server in the same install; `doctor` treats a missing or different
`--home` as a wrong binding, and every fix it prints sets the variable.
The path must be absolute and at most 62 bytes, since the supervisor's
control sockets live under it; `setup` and `doctor` refuse a longer one
with the byte count. Claude and Codex logins stay where they are.

### Any other MCP client

Point it at the same command with the same `--actor-id` argument. The server
speaks plain stdio MCP and needs nothing else.

## Checking the binding

From inside the agent session, call the `whoami` tool. It returns the bound
actor. From a shell, `agent-comms doctor` reports each recorded seat as
`mcp:<architect>:<client>`: the actor it binds, the ledger, and the server
command, with the `bind-seat` line that repairs a wrong one. For Codex it
also asks Codex what it resolves in the project (`codex mcp get
agent-comms --json`, no login, no model call) and reports it as
`mcp:<architect>:codex:effective`: a project Codex does not trust, or a
field another config layer changes, fails with the step that fixes it.
Without `codex` on `PATH` that check is skipped. `agent-comms actors`
lists every registered actor.

## Workers

Worker seats are not configured this way. When an architect dispatches to a
worker, the runtime adapter starts the worker with a generated MCP
configuration and a restricted tool policy. Nothing needs to be added to the
worker's CLI by hand. A Codex worker is bound with `-c
mcp_servers.agent-comms.*` overrides on every run, which outrank its home's
config and the project entry above, and before the run the adapter checks
what Codex resolves with everything the launch loads (its profile, start
directory and every `-c`, in order); a dispatch whose binding differs fails
before the worker starts, naming the conflicting field. A worker's stored
spawn args may not set `mcp_servers.agent-comms` (or the whole
`mcp_servers` table) with `-c`, since a later `-c` would win; such a
dispatch is refused. The worker runs the custody copy of the pinned Codex
(`agent-comms custody codex` fills it), never the `codex` on `PATH`; the
architect's seat above keeps using the `codex` you run yourself.
