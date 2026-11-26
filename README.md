# agent-comms

A local mailbox and bounded dispatch system for terminal AI coding agents such
as Claude Code and Codex CLI. Everything runs on one workstation against one
SQLite file. There is no daemon, no network service, and no cloud.

Agents talk to it through a stdio MCP server that is bound to one identity per
process. Humans and scripts use the `agent-comms` CLI.

## Why

With several terminal agents open, you are the message bus: you copy a task
from one window into another, wait, copy the answer back, and remember who
was asked what. Nothing bounds the agent you handed the task to, and nothing
tells you when it stalled. agent-comms gives the agents a mailbox to talk
through, and gives one of them, the architect, a way to hand a task to a
worker that runs under a time limit and a restricted tool surface. Its reply
comes back on the same thread. The monitor pages the architect if the worker
fails to start or the dispatch lands in the dead-letter queue (DLQ), as one
that timed out or whose worker died does.

```text
  you (operator)
       |
       v
  architect  (your Claude Code or Codex session)
       |   ^
       |   |  reply on the dispatch thread
       v   |
  dispatch: one task, a time limit, restricted tools
       |   ^
       v   |
  worker  (claude, codex or fake, under a supervisor)

  monitor: reconciles in-flight dispatches and pages the
           architect when a worker fails to start or a
           dispatch lands in the DLQ
```

This repository also lands its own changes through a review gate and a
guarded push that verifies an approval signed with an SSH key. That is a
workflow run from a clone, not part of the installed commands, and a
dispatch does not depend on it:

```text
  reviewed commit --> approval signed with your SSH key
                  --> scripts/guarded-push --> remote
```

## Try it without a login

The `fake` runtime runs the whole dispatch path with no model account. With
Python 3.11 or newer, Git and [uv](https://docs.astral.sh/uv/):

```sh
mkdir agent-comms-build && cd agent-comms-build &&
    git clone https://github.com/HakAl/agent-comms.git &&
    uv build --project agent-comms --out-dir wheels &&
    uv tool install wheels/agent_comms-*.whl &&
    agent-comms version
```

If the last step says `agent-comms` is not found, uv's tool directory is not
on `PATH`: run `uv tool update-shell`, open a new terminal, and run
`agent-comms version` again. Then:

```sh
mkdir -p "$HOME/demo-project"
agent-comms setup --project-root "$HOME/demo-project" --team demo \
    --runtimes fake --clients "" --human alice --yes
agent-comms doctor
agent-comms demo
```

`demo` has the architect dispatch one task to the fake worker and prints
each step to stderr:

```text
dispatched dispatch_...: demo-architect -> demo-fake-worker, 'demo: ping'
worker replied msg_...: 'fake-reply: PONG'
dispatch closed: satisfied
```

State goes under `~/.agent-comms`. [The quickstart](docs/quickstart.md)
explains each step and goes on to a real dispatch with Claude Code and Codex.

## What is in this checkout

- **Mailbox.** Messages, acknowledgements, status posts, handoff snapshots,
  and a blocking `wait_for_reply`. Each MCP server process is started with
  `--actor-id`, so an agent cannot send as anyone else.
- **Dispatch.** An architect hands a bounded task to a worker it owns. The
  worker runs under a supervisor with a time limit and a restricted tool
  surface. Work that fails or times out lands in a dead-letter queue and the
  architect that dispatched it is paged. A monitor process reconciles
  in-flight dispatches; `agent-comms service` runs it as a user service (a
  systemd user unit on Linux, a launchd agent on macOS).
- **Runtimes.** Workers run on `claude`, `codex`, or a `fake` runtime that
  needs no login and exists for demos and tests. Runtime versions are pinned
  per platform and certified by tests before dispatch trusts them. Certified
  today: Codex and Claude Code, each on `darwin-arm64` and `linux-aarch64`.
  No other platform is claimed; a dispatch on one is refused
  before the worker starts.
- **Setup and doctor.** `agent-comms setup` creates a team for a project and
  binds the architect's seat in your own Claude Code and Codex;
  `agent-comms doctor` checks the install and prints the fix for every
  failing check. Workers use the logins you already have: no login is
  specific to agent-comms, and a machine that already runs Claude Code and
  Codex needs none.
- **Review and landing.** A review gate runner and a signed push approval
  check (`scripts/guarded-push`), so a push made through it needs a human
  approval signed with an SSH key. These run from a clone and are not among
  the installed commands; a plain `git push` is not intercepted. Landing
  needs two paths from the operator's shell:
  `AGENT_COMMS_MAIN`, the clean main-branch checkout reviewed work merges
  into, and `AGENT_COMMS_APPROVAL_SIGNERS_REPO`, the checkout whose history
  carries `config/approval-signers` (see `config/approval-signers.example`).
- **Gates.** `make gate` runs hygiene, lint, the isolated test suite, a
  fresh-clone preverify and an install smoke test of the wheel. `make hooks`
  installs a pre-push hook that runs hygiene and lint on the commits about
  to be published. CI runs hygiene, lint, the suite and the install smoke
  test on macOS (preverify stays local: CI already is a fresh clone), the
  `make claude-seat` check, and the suite again on Linux.

Release 0.2.0 was walked through on a clean Mac against milestone 1 of
[the roadmap](docs/ROADMAP.md) (macOS); the commands and results are in
[docs/releases/0.2.0-macos-walkthrough.md](docs/releases/0.2.0-macos-walkthrough.md).
Every criterion was met except a clean `doctor` report with native runtimes:
one seat check failed on a bug fixed since and now rerun by CI on every
change, while the seat worked. The 0.3.0 candidate was walked through
the same way on a clean Debian 13 machine (aarch64) against milestone 2
(Linux), with the monitor as a systemd user service, and met every
criterion; one input, the Codex login, was copied in and not made on that
machine:
[docs/releases/0.3.0-linux-walkthrough.md](docs/releases/0.3.0-linux-walkthrough.md).
There is no PyPI
release yet: the package installs from a wheel built out of a clone.

## Documentation

Start with the quickstart; it needs no other page.

| Page | What it is for |
|---|---|
| [docs/quickstart.md](docs/quickstart.md) | Install, set up a team, check the install, run the no-login demo, then your first real dispatch |
| [docs/concepts.md](docs/concepts.md) | The vocabulary: runtime root, actors and roles, seats, the mailbox, dispatch and the monitor, runtimes, the MCP tools, the review cycle |
| [docs/runbook.md](docs/runbook.md) | Operating tasks: the doctor report, keeping the monitor up, logs, the DLQ, acting on a dispatch by hand, the approval signing key, reviewing and landing a dispatch, Codex logins, worker shells and your startup files, runtime upgrades, backups and restore, a second install |
| [docs/mcp-setup.md](docs/mcp-setup.md) | What `setup` wires into each MCP client, seats for other clients, how workers are bound, and every Claude vs Codex difference |
| [docs/architect-prompt.md](docs/architect-prompt.md) | The operating rules to give an architect seat, and the CLI fallback |
| [docs/communication_contract.md](docs/communication_contract.md) | When to send, how to reply, how threads end |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Where this is going and what is not planned |
| [docs/releases/0.2.0-macos-walkthrough.md](docs/releases/0.2.0-macos-walkthrough.md) | The 0.2.0 walkthrough on a clean Mac: every command and result, a verdict per milestone criterion, the departures and findings |
| [docs/releases/0.3.0-linux-walkthrough.md](docs/releases/0.3.0-linux-walkthrough.md) | The 0.3.0 walkthrough on a clean Debian 13 machine (aarch64), the same shape, with the systemd user service |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, the gates, runtime pins and certification, review, failure reports |
| [AGENTS.md](AGENTS.md) | How to work on this repository |

Every command shown in these pages is parsed against the real CLI and must
be exercised by a test or by the install smoke test
(`tests/substrate/test_docs_commands.py`).

## Boundaries

- One workstation. State lives under `~/.agent-comms`; nothing listens on a
  port.
- Claude Code and Codex CLI are the runtimes with adapters and certification
  tests. Any MCP client can use the mailbox.
- A worker keeps your `HOME`. The shells Codex itself starts for a worker's
  commands load none of your shell startup files, and zsh reads an empty
  owned `ZDOTDIR`. Claude Code sources your `.bashrc` or `.zshrc` once per
  run to copy functions, aliases and options; what those files export is
  kept out of a Claude worker's commands. That is launch hygiene, not a read boundary: a shell the
  worker starts itself as `bash -l`, `bash -i` or `sh -l` reads the startup
  files in `HOME`. The limits are in
  [docs/runbook.md](docs/runbook.md#worker-shells-and-your-startup-files).
- The MCP server trusts the `--actor-id` it is started with. Identity is a
  process boundary on one workstation, not authentication; do not expose it
  beyond one.
- Not a session manager or dashboard. It does not spawn or arrange your
  terminals.
- Not a message queue. There is no broker or fan-out.

## Testing

```sh
make test
```

Every test process runs in a scratch home and never touches `~/.agent-comms`;
how the guard works, and its switches, are in
[CONTRIBUTING.md](CONTRIBUTING.md#development-setup-and-tests). A test that
needs something the host lacks (a runtime login, a certified pin for the
platform, a tool such as `git`, `codex` or `bash` on `PATH`, `/bin/zsh`, or an
`AF_UNIX` bind the sandbox denies) is skipped and reported as skipped, never
counted as passing. `make gate` runs every check a push must pass, and
`make hooks` installs the pre-push hook that enforces hygiene and lint at
push time. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the gates, and
how to report a failure.

## License

[GPLv3](https://www.gnu.org/licenses/gpl-3.0.en.html).
