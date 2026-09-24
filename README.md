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
  signed with an SSH key.
- **Gates.** `make gate` runs hygiene, lint, the isolated test suite, and a
  fresh-clone preverify. `make hooks` installs a pre-push hook that runs
  hygiene and lint on the commits about to be published and refuses the
  push on a finding. CI mirrors the same checks on macOS.

## What is not here yet

Milestone 1 of [the roadmap](docs/ROADMAP.md) is in progress. The pieces
that still need to land before a new user can run this without a checkout:

- An installable package with installed launchers. Today everything runs
  from a synced development checkout.
- `agent-comms setup` and `agent-comms doctor`.
- Runtime version pins per platform, and upgrades from the original 0.1.0
  mailbox.
- A proper quickstart, concepts page, and runbook. This README is the
  interim version.

Known gaps in the current dispatch path are tracked in the maintainer's
issue tracker and summarized at the end of the walkthrough below.

## Quickstart from a checkout

Requirements: macOS, Python 3.11 or newer, Git, and
[uv](https://docs.astral.sh/uv/).

```sh
# 1. Sync the environment (the MCP extra is required for the server)
uv sync --extra mcp

# 2. Describe your actors: one human, one architect per team, and workers
cp config/actors.example.json config/actors.json
$EDITOR config/actors.json
export PROJECT_A_ROOT=/absolute/path/to/your/project   # referenced by the example

# 3. Register them in the mailbox at ~/.agent-comms/agent-comms.sqlite
scripts/agent-comms bootstrap
scripts/agent-comms actors
```

`config/actors.json` is ignored by git. Worker entries declare a `runtime`;
the spawn command is rendered from it, never written by hand. Project roots
may use `~` and `${ENV}` expansion.

To connect an agent CLI, see [docs/mcp-setup.md](docs/mcp-setup.md). Each
seat gets one MCP server started with its own `--actor-id`.

## Try a dispatch without any login

This exercises the whole dispatch path with the `fake` runtime. It uses the
operator override command, which needs an admin token, because there is no
architect session in the loop. In normal use the architect calls the
`dispatch_agent` MCP tool instead and no token is involved.

```sh
# The fake worker is launched as `python3 -m agent_comms.adapters.fake_worker`,
# so the project environment must be first on PATH for this walkthrough.
export PATH="$PWD/.venv/bin:$PATH"

# One-time operator credential, mode 600
(umask 077; head -c 32 /dev/urandom | xxd -p -c 64 > ~/.agent-comms/admin-token)
export AGENT_COMMS_ADMIN_TOKEN="$(cat ~/.agent-comms/admin-token)"

# Dispatch from the example architect to the example fake worker
scripts/agent-comms admin dispatch \
    --from-actor-id team-a-architect \
    --target-actor-id team-a-fake-worker \
    --idempotency-key demo-1 \
    --requested-policy worker_dispatch_readwrite_bounded \
    --override-reason "fake runtime demo" \
    --subject ping --body "Reply with PONG."

# Reconcile until the dispatch reaches a terminal state
scripts/agent-comms-monitor --human-actor-id 01M36YTJV9XBW95S6ZWV47C4RG \
    --interval 1 --max-passes 15

# Inspect the outcome and the worker's reply
scripts/agent-comms dispatch-status
scripts/agent-comms inbox team-a-architect
```

Expected result: the dispatch row shows `closed` with result `satisfied`,
and the architect's inbox holds a reply from the fake worker parented to the
dispatch message. If the worker log shows `No module named 'agent_comms'`,
the PATH step above was skipped.

Worker logs for each dispatch land under `~/.agent-comms/logs/dispatch/`
(`AGENT_COMMS_DISPATCH_LOG_DIR` moves them). One thing about this walkthrough
is a known defect, not design: the fake worker depends on `python3` resolving
to an interpreter that can import this package. The installable package work
fixes it.

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
