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
config and the project entry above. Its home's `config.toml` holds only
trust (its project, your home, and an `AGENT_COMMS_HOME` outside it); the
sandbox and approval policy are flags on the spawn. Before the run the
adapter checks what Codex resolves with everything the launch loads (the
home's config, a `--profile` in stored spawn args, the start directory and
every `-c`, in order); a dispatch whose binding differs fails before the
worker starts, naming the conflicting field. A home provisioned by an
older version still holds an `agent-comms` entry and a
`<worker>.config.toml` profile; both are harmless (the check verifies the
result), and `agent-comms provision-codex-home` rewrites the config. A
stored spawn that still passes `--profile <worker>` is updated by
`agent-comms rerender-spawn --runtime codex --apply`. A worker's stored
spawn args may not set `mcp_servers.agent-comms` (or the whole
`mcp_servers` table) with `-c`, since a later `-c` would win; such a
dispatch is refused. The worker runs the custody copy of the pinned Codex
(`agent-comms custody codex` fills it), never the `codex` on `PATH`; the
architect's seat above keeps using the `codex` you run yourself.

A Claude worker runs `claude -p --output-format stream-json --verbose`;
Claude 2.1.300 refuses stream-json output in print mode without
`--verbose`. A dispatch to a stored Claude spawn from before that flag
is refused before the worker starts, and the spawn is updated by
`agent-comms rerender-spawn --runtime claude --apply`.

A Codex worker runs the same PreToolUse policy hook as a Claude worker. The
hook is declared with `-c hooks.PreToolUse=...` on every run, and
`-c hooks.state=...` trusts exactly that hook by the hash Codex computes for
it, so nothing is written to the worker's home and
`--dangerously-bypass-hook-trust` is never used. Hooks shipped in the
worker's project stay untrusted and do not run. Before the run the adapter
asks Codex whether it would run the hook (`codex app-server`, `hooks/list`,
with everything the launch loads) and runs the hook once as Codex would, on
a call it must deny; either failing refuses the dispatch. Stored spawn args
may not set `hooks` with `-c` or pass `--dangerously-bypass-hook-trust`. The
hook confines `apply_patch` to the project, taking relative paths from the
turn's working directory as Codex does, and denies a patch that selects
another environment. Like Claude Code, Codex runs the tool anyway when a
hook crashes or times out mid-run, and it does not run PreToolUse for input
written to a command that is already running (`write_stdin`), so the
sandbox and the stripped credentials stay the boundary for file writes and
network.

## Claude and Codex differences

Each remaining difference in how agent-comms wires the two runtimes, and
why. A Codex limit names the Codex version or issue it was found on; the
pinned versions are in `agent_comms/runtime_pins.json`. Our choice can
change without waiting for Codex. One row is neither: the runtimes simply
have different file tools.

| Area | Claude Code | Codex | Why |
|---|---|---|---|
| Architect seat | `claude mcp add --scope local`, an entry per project directory in `~/.claude.json` | `[mcp_servers.agent-comms]` in `<project>/.codex/config.toml`, written by `setup` | Codex limit: `codex mcp add` has no scope option and writes the user config (0.160.0). Our choice: never edit `~/.codex/config.toml`. |
| Where the seat applies | the directory it was added in | a trusted project only; in git, any directory up to the git root; outside git, the start directory | Codex limit, observed with 0.157.0 ([Codex CLI](#codex-cli)). |
| Same server name in several config layers | one scope's entry wins whole | fields merge across layers, so `setup` pins every field | Codex limit, observed with 0.157.0. |
| `doctor` | checks the recorded local-scope entry | also asks Codex for the entry it resolves (`codex mcp get --json`) | Our choice, following from the row above: Claude's entry is the binding as written, and `claude mcp get` has no JSON output. |
| `agent-comms-seat` launcher | refuses to launch when the directory's seat binds another actor | no check | Our choice: not built for Codex; `doctor` covers it. |
| Worker MCP binding | `--strict-mcp-config --mcp-config`: only that server loads | `-c mcp_servers.agent-comms.*` on every run; other configured servers can still load; the effective binding is checked before the run | Codex limit: no option to load only the servers given (0.160.0). |
| Worker tool approval | `--permission-mode dontAsk` and an allowlist in `--settings` | `--ask-for-approval never`, and `default_tools_approval_mode = "approve"` for the agent-comms server | Codex limit: headless `codex exec` cancels an MCP tool call that needs approval (recorded before the Codex pin; not rechecked on 0.160.0). |
| Worker home and login | your Claude configuration and login | a home per worker holding trust only; `auth.json` links to one shared login, refreshed by the monitor after it expires | Codex limit: ChatGPT refresh tokens are single-use, so processes refreshing one login concurrently log each other out ([openai/codex-plugin-cc#789](https://github.com/openai/codex-plugin-cc/issues/789)). |
| Policy hook | declared in `--settings` | declared and trusted by hash with `-c` on every run; listed and run once before the run | Codex limit: a hook runs only once trusted (0.160.0). |
| Hook output on allow | the allow decision | nothing | Codex limit: Codex treats a bare allow as a failed hook (0.160.0). |
| Calls the hook sees | every tool call | every tool call except input to a running command (`write_stdin`) | Codex limit (0.160.0). |
| File tools the hook confines | `Read`, `Edit`, `MultiEdit`, `Write` paths | `apply_patch` headers, relative to the turn's directory | Neither: each runtime has its own file tools, and the hook confines those. |
| Reads outside the project | the sandbox denies named credential paths (`Glob` and `Grep` are not confined) | not confined: the `workspace-write` sandbox reads outside the workspace | Codex limit, observed with 0.160.0 on macOS. |

Shared by both: a PreToolUse hook that cannot start, crashes or times out
lets the tool run. Our hook denies on any error it catches itself, and a
Codex dispatch runs it once before the run; past that, the sandbox and the
stripped credentials are the boundary.
