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
  architect that dispatched it is paged. A monitor process reconciles in-flight dispatches.
- **Runtimes.** Workers run on `claude`, `codex`, or a `fake` runtime that
  needs no login and exists for demos and tests. Runtime versions are pinned
  per platform and certified by tests before dispatch trusts them.
- **Setup and doctor.** `agent-comms setup` creates a team for a project and
  binds the architect's seat in your own Claude Code and Codex;
  `agent-comms doctor` checks the install and prints the fix for every
  failing check.
- **Review and landing.** A review gate runner and a signed push approval
  check (`scripts/guarded-push`), so nothing pushes without a human approval
  signed with an SSH key. Landing needs two paths from the operator's shell:
  `AGENT_COMMS_MAIN`, the clean main-branch checkout reviewed work merges
  into, and `AGENT_COMMS_APPROVAL_SIGNERS_REPO`, the checkout whose history
  carries `config/approval-signers` (see `config/approval-signers.example`).
- **Gates.** `make gate` runs hygiene, lint, the isolated test suite, a
  fresh-clone preverify and an install smoke test of the wheel. `make hooks`
  installs a pre-push hook that runs hygiene and lint on the commits about
  to be published. CI mirrors the same checks on macOS.

Milestone 1 of [the roadmap](docs/ROADMAP.md) (macOS) is nearly complete;
what remains is the recorded release walkthrough. There is no PyPI release
yet: the package installs from a wheel built out of a clone.

## Documentation

Start with the quickstart; it needs no other page.

| Page | What it is for |
|---|---|
| [docs/quickstart.md](docs/quickstart.md) | Install, set up a team, check the install, run the no-login demo, then your first real dispatch |
| [docs/concepts.md](docs/concepts.md) | The vocabulary: runtime root, actors and roles, seats, the mailbox, dispatch and the monitor, runtimes, the MCP tools, the review cycle |
| [docs/runbook.md](docs/runbook.md) | Operating tasks: the doctor report, keeping the monitor up, logs, the DLQ, acting on a dispatch by hand, Codex logins and upgrades, backups and restore, a second install |
| [docs/mcp-setup.md](docs/mcp-setup.md) | What `setup` wires into each MCP client, seats for other clients, how workers are bound, and every Claude vs Codex difference |
| [docs/architect-prompt.md](docs/architect-prompt.md) | The operating rules to give an architect seat, and the CLI fallback |
| [docs/communication_contract.md](docs/communication_contract.md) | When to send, how to reply, how threads end |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Where this is going and what is not planned |
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

Every test process runs in a scratch home and never touches `~/.agent-comms`.
Tests that need a real runtime login are skipped and reported as skipped.
`make gate` runs every check a push must pass, and `make hooks` installs
the pre-push hook that enforces hygiene and lint at push time. See
[CONTRIBUTING.md](CONTRIBUTING.md) for setup, the gates, and how to report a
failure.

## License

[GPLv3](https://www.gnu.org/licenses/gpl-3.0.en.html).
