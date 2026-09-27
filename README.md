# agent-comms

A local mailbox and bounded dispatch system for terminal AI coding agents such
as Claude Code and Codex CLI. Everything runs on one workstation against one
SQLite file. There is no daemon, no network service, and no cloud.

Agents talk to it through a stdio MCP server that is bound to one identity per
process. Humans and scripts use the `agent-comms` CLI.

## What is in this checkout

- **Mailbox.** Messages, acknowledgements, status posts, handoff snapshots,
  and a blocking `wait_for_reply`. Each MCP server process is started with
  `--actor-id`, so an agent cannot send as anyone else.
- **Dispatch.** An architect hands a bounded task to a worker it owns. The
  worker runs under a supervisor with a time limit and a restricted tool
  surface. Work that fails or times out lands in a dead-letter queue and the
  human actor is paged. A monitor process reconciles in-flight dispatches.
- **Runtimes.** Workers can run on `claude`, `codex`, or a `fake` runtime
  that needs no login and exists for demos and tests.
- **Review and landing.** A review gate runner and a signed push approval
  check (`scripts/guarded-push`), so nothing pushes without a human approval
  signed with an SSH key. Landing needs two paths from the operator's shell:
  `AGENT_COMMS_MAIN`, the clean main-branch checkout reviewed work merges
  into, and `AGENT_COMMS_APPROVAL_SIGNERS_REPO`, the checkout whose history
  carries `config/approval-signers` (see `config/approval-signers.example`).
- **Gates.** `make gate` runs hygiene, lint, the isolated test suite, and a
  fresh-clone preverify. `make hooks` installs a pre-push hook that runs
  hygiene and lint on the commits about to be published and refuses the
  push on a finding. CI mirrors the same checks on macOS.

## What is not here yet

Milestone 1 of [the roadmap](docs/ROADMAP.md) is in progress. The pieces
that still need to land before a new user can run this without a checkout:

- Upgrades from the original 0.1.0 mailbox.
- A proper quickstart, concepts page, and runbook. This README is the
  interim version.

Known gaps in the current dispatch path are tracked in the maintainer's
issue tracker and summarized at the end of the walkthrough below.

## Install without a checkout

Requirements: macOS, Python 3.11 or newer, Git, and
[uv](https://docs.astral.sh/uv/) (or `pipx`). There is no PyPI release yet;
the package installs from a wheel built out of a clone or from a git tag.

```sh
git clone https://github.com/HakAl/agent-comms.git
uv build --project agent-comms --out-dir wheels
uv tool install wheels/agent_comms-*.whl
rm -rf agent-comms wheels      # the install does not depend on the source tree
agent-comms version
```

`uv tool install` puts `agent-comms`, `agent-comms-mcp`, `agent-comms-monitor`
and `agent-comms-seat` on `PATH`. Everything the installed commands read or
write lives under `~/.agent-comms` (`AGENT_COMMS_DB` moves the ledger); the
runtime pins ship inside the package, so `agent-comms version` works without
a checkout and reports its git fields as `unknown`. From here the quickstart
below applies from its step 2 with `agent-comms` in place of
`.venv/bin/agent-comms`. `make install-smoke` is the check that this path
works; it runs in CI on every change.

## Quickstart from a checkout

Requirements: macOS, Python 3.11 or newer, Git, and
[uv](https://docs.astral.sh/uv/).

```sh
# 1. Sync the environment; this puts agent-comms, agent-comms-mcp,
#    agent-comms-monitor and agent-comms-seat under .venv/bin
uv sync

# 2. Set up a team for your project. This writes the actor registry
#    (~/.agent-comms/actors.json: you, one architect, one worker per
#    runtime), registers them in the mailbox at
#    ~/.agent-comms/agent-comms.sqlite, provisions the codex worker's home,
#    and binds the architect seat in your own Claude Code and Codex CLIs
#    through their `mcp add` commands. No file is edited by hand. On a
#    terminal it asks for anything left out; --yes takes the defaults.
#    The human id below is the one the walkthrough further down uses.
.venv/bin/agent-comms setup --project-root /absolute/path/to/your/project \
    --team team-a --runtimes claude,codex,fake \
    --human-id 01M36YTJV9XBW95S6ZWV47C4RG

# 3. Check the install. Every failing check carries the command that fixes it.
.venv/bin/agent-comms doctor

# 4. Watch one dispatch end to end, before any model login: the architect
#    dispatches to the team's fake worker, which replies and closes it.
.venv/bin/agent-comms demo
```

The commands are console scripts of the environment that holds the package:
`.venv/bin/<command>` in a checkout, plain `agent-comms` and friends on `PATH`
when the package is installed.

On a terminal, `setup` asks for anything left out, with the runtimes it
detects on the machine as the suggested answer; `--yes` takes the defaults
without asking, and off a terminal an omitted `--project-root` or
`--runtimes` is refused by name. `--clients claude,codex` chooses which of
your CLIs get the architect seat (default: the native runtimes chosen; `none`
for no seat), `--human` and `--human-id` name you (default: your login name
and a generated id, printed under `human`), and a second team on the same
machine reuses the registered human. A team that already exists is refused;
`agent-comms bootstrap` re-registers a registry edited by hand
(`config/actors.example.json` shows the shape, and `--config` names another
file). Worker entries declare a `runtime`; the spawn command is rendered from
it, never written by hand.

To connect another agent CLI, or to see what `setup` ran, see
[docs/mcp-setup.md](docs/mcp-setup.md). Each seat gets one MCP server started
with its own `--actor-id`.

## Check the install

`agent-comms doctor` checks the install, the runtime root, the registry and
the ledger, the platform pins, each runtime the team uses (binary, digest,
version, login), every codex worker's home and any login that refresh could
not recover, the monitor heartbeat, the architect's MCP seats and the admin
token. It writes nothing and repairs nothing: every failing check carries the
command or step that fixes it. The output is one JSON report:

```json
{
  "ok": false,
  "platform": "darwin-arm64",
  "checks": [
    {"id": "install", "status": "ok", "detail": "agent-comms-mcp, agent-comms-monitor, agent-comms-seat under ~/.local/share/uv/tools/agent-comms/bin", "fix": null},
    {"id": "ledger", "status": "ok", "detail": "~/.agent-comms/agent-comms.sqlite: schema 3, 1 human, 1 architect, 2 worker", "fix": null},
    {"id": "runtime:codex:home:team-a-codex-worker", "status": "fail",
     "detail": "team-a-codex-worker: ~/.agent-comms/codex-homes/default/team-a-codex-worker: auth.json last_refresh is stale",
     "fix": "CODEX_HOME=~/.agent-comms/codex-homes/default/team-a-codex-worker codex login"},
    {"id": "monitor", "status": "warn", "detail": "no monitor heartbeat yet and nothing has been dispatched", "fix": "agent-comms-monitor --human-actor-id 01M36YTJV9XBW95S6ZWV47C4RG"}
  ],
  "fixes": ["CODEX_HOME=~/.agent-comms/codex-homes/default/team-a-codex-worker codex login"]
}
```

Paths in a real report are absolute. The exit code is 0 when no check
failed and 3 otherwise, and `fixes` lists the fix of every failing check in
order, so an agent can branch on the exit code and act on the fixes without
reading the details. A `warn` (no monitor while nothing has been dispatched
or only the demo's finished fake dispatches exist; a missing admin token)
does not fail the report; with no monitor running, the first dispatch to a
claude or codex worker, or fake work still queued or in flight, does; a `skip` names what was not
applicable (no codex worker registered, say). An expired Codex login that
refresh could not recover is its own check, `runtime:codex:login:<lineage>`,
whose fix is the login command for that home. `doctor --clients claude,codex`
checks those seats whether or not setup recorded them.

## Try a dispatch without any login

`agent-comms demo` dispatches one task from a team's architect to that
team's fake worker, drives it until it is terminal, and shows each step:

```sh
.venv/bin/agent-comms demo
# stderr, as it happens:
#   dispatched dispatch_...: team-a-architect -> team-a-fake-worker, 'demo: ping'
#   worker replied msg_...: 'fake-reply: PONG'
#   dispatch closed: satisfied
```

Stdout is one JSON object: `dispatch` (ids, sender, recipient, subject,
body), `reply` (the worker's message, parented to the dispatch), `final`
(the row's `status` and `result`), `worker_log` (where worker logs land) and
`worker_reaped`, true once the worker's wrapper exited and its exit was
recorded (the demo waits up to 10 seconds for that; false does not fail the
demo). It exits 0 when the dispatch closed `satisfied` with a reply, 3 with
`fixes` otherwise, and 2 when it cannot start. With several
fake workers registered, `--team` picks one; with none, the refusal prints
the `setup` command that adds a fake-only team. `--timeout` (default 60
seconds) bounds the wait. The dispatch starts the fake worker at once, and
the worker replies and closes its own row, so no monitor is needed; the demo
only watches that row. It writes no monitor heartbeat, so `doctor` stays
clean. A row that is still queued or in flight at the timeout is the
monitor's to start or settle, and the failure's first fix is the monitor
command.

The demo only ever dispatches to a worker whose registered runtime is
`fake`, which runs no model and only replies; it cannot start a claude or
codex worker. It never runs a ledger-wide reconcile pass, so other queued
work on the ledger, native or not, is left to the monitor. It dispatches as
the architect, exactly as the architect's `dispatch_agent` MCP tool does, so
no admin token is involved.

### The same dispatch by hand

This is what the demo does, through the operator override command, which
needs an admin token because there is no architect session in the loop.

```sh
# One-time operator credential, mode 600
(umask 077; head -c 32 /dev/urandom | xxd -p -c 64 > ~/.agent-comms/admin-token)
export AGENT_COMMS_ADMIN_TOKEN="$(cat ~/.agent-comms/admin-token)"

# Dispatch from the architect to the fake worker setup created for team-a
.venv/bin/agent-comms admin dispatch \
    --from-actor-id team-a-architect \
    --target-actor-id team-a-fake-worker \
    --idempotency-key demo-1 \
    --requested-policy worker_dispatch_readwrite_bounded \
    --override-reason "fake runtime demo" \
    --subject ping --body "Reply with PONG."

# Reconcile until the dispatch reaches a terminal state. The human id is
# the one given to setup (or the one it generated, printed under "human";
# doctor's monitor check prints this whole command)
.venv/bin/agent-comms-monitor --human-actor-id 01M36YTJV9XBW95S6ZWV47C4RG \
    --interval 1 --max-passes 15

# Inspect the outcome and the worker's reply
.venv/bin/agent-comms dispatch-status
.venv/bin/agent-comms inbox team-a-architect
```

Expected result: the dispatch row shows `closed` with result `satisfied`,
and the architect's inbox holds a reply from the fake worker parented to the
dispatch message. The fake worker runs on the same interpreter as the
process that dispatched it, so nothing needs to be on `PATH`.

Worker logs for each dispatch land under `~/.agent-comms/logs/dispatch/`
(`AGENT_COMMS_DISPATCH_LOG_DIR` moves them).

## MCP tool surface

| Tool | Purpose |
|------|---------|
| `whoami` | The identity bound to this server process |
| `list_actors`, `list_agents` | Known actors and agent registrations |
| `send_message` | Send a message with optional file refs |
| `list_inbox`, `read_message` | Read messages addressed to this actor |
| `ack_message`, `close_message` | Acknowledge with a response; close a copy |
| `wait_for_reply` | Block until a new message arrives or a timeout |
| `post_status`, `list_status` | Publish and read current status |
| `post_handoff`, `read_handoff` | Durable handoff snapshots between sessions |
| `dispatch_agent` | Hand a bounded task to an owned worker |
| `cancel_dispatch` | Terminate one of this actor's in-flight dispatches |
| `close_dispatch` | A worker reports its result and delta |

A worker runs under a policy that hides every tool it is not allowed to use.

## Operating model

Architects check their inbox at session start, pass file refs instead of
pasted content, acknowledge messages that affect their lane, and post status
before ending substantial work. The default is not to message.

The prompt snippet for an architect seat is in
[docs/architect-prompt.md](docs/architect-prompt.md). The message and reply
discipline that keeps threads from looping is in
[docs/communication_contract.md](docs/communication_contract.md).

## Boundaries

- One workstation. State lives under `~/.agent-comms`; nothing listens on a
  port.
- Claude Code and Codex CLI are the runtimes with adapters and certification
  tests. Any MCP client can use the mailbox.
- Not a session manager or dashboard. It does not spawn or arrange your
  terminals.
- Not a message queue. There is no broker or fan-out.

## Testing

```sh
make test
```

Every test process runs in a scratch home and never touches `~/.agent-comms`.
Tests that need a real runtime login are skipped and reported as skipped.
`make gate` runs every check a push must pass, and `make hooks` installs
the pre-push hook that enforces hygiene and lint at push time. See
[CONTRIBUTING.md](CONTRIBUTING.md) for setup, the gates, and how to report a
failure.

## License

[GPLv3](https://www.gnu.org/licenses/gpl-3.0.en.html).
