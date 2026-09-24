# Roadmap

## Where this is going

This repository ships the original mailbox: a local SQLite store, a CLI, and
an MCP server. The maintainer has since grown that mailbox, in a separate
private deployment, into a full local workflow that has run for hundreds of
hours of real use. This roadmap brings that workflow here as a public product
anyone can install.

The private deployment is a source, not an upstream. Behavior is ported here
with generic configuration, fresh tests, and none of the maintainer's paths,
teams, or history. After porting, this repository evolves on its own.

## What you get, in layers

Each layer works without the ones above it.

| Layer | What it does | Needs |
|-------|--------------|-------|
| 1. Mailbox | Agents send, read, and acknowledge messages. Each MCP server is bound to one identity, so an agent cannot pretend to be another. | Python, any MCP client |
| 2. Dispatch | An architect agent hands a bounded task to a worker agent. Workers get a time limit and restricted tools. Work that fails or times out goes to a dead-letter queue and the sender is told. | A monitor process; Claude Code or Codex for real work (a fake runtime exists for demos) |
| 3. Review and landing | Work goes through a tracked cycle: brief, implementation, review by a different model family, checks, then a human approval signed with an SSH key before it merges. Nothing pushes without that approval. | Claude Code and Codex, an SSH key |

Runtime logins cannot be refreshed ahead of time. Once a worker's login has
expired it is refreshed; if that fails, dispatch to that worker stops with a
clear message and `doctor` tells the user to log in again.

## Sequence

### Milestone 1: macOS

Port layers 1 to 3 so a new user on macOS can install and use them.

1. **Port and generalize.** Bring the code over with example configuration
   and neutral test fixtures. Values tied to one person (the human actor id,
   the integration branch name, project root variables, the approval signer)
   become configuration.
2. **Install without a checkout.** A versioned install (`uv tool` or `pipx`)
   works on its own. State, logs, and config live under `~/.agent-comms`, not
   in the source tree. MCP server, monitor, and seat launchers are installed
   commands. Runtime version pins ship inside the package.
3. **Setup and doctor.** `agent-comms setup` asks which runtimes you have,
   creates a small team for your project, and writes the MCP configuration.
   `agent-comms doctor` checks runtimes, logins, version pins, and monitor
   health, and says what to fix.
4. **Demo without logins.** A walkthrough dispatches a task to the fake
   runtime and shows the reply and final status, before any model account
   is needed.
5. **Runtime pins per platform.** Pins record a version per OS and CPU
   architecture, not one binary hash from one machine.
6. **Upgrades.** A mailbox from the original 0.1.0 release is imported or
   refused with a clear message. Every schema migration takes a backup first,
   and restoring from it is tested.
7. **CI.** The hermetic test suite runs on macOS for every change. Tests use
   temporary directories only, never the real home directory or source tree.
8. **Docs.** A quickstart, a concepts page (actor, architect, worker,
   dispatch, review cycle), and a runbook. No operational history.

**Done when** a person, or an agent following only this repository's docs,
on a clean Mac:

1. installs a release,
2. runs setup against a project,
3. picks their runtimes and creates a small team,
4. gets a clean `doctor` report,
5. runs the fake-runtime demo, then a real dispatch, review, and approval.

The human supplies logins and choices. Nothing else from the maintainer is
needed. The commands and results are saved with the release.

### Milestone 2: Linux

1. **Service manager.** The monitor runs under systemd user units as well as
   launchd, behind one interface.
2. **Remove macOS assumptions.** No reliance on `/private/tmp`, `/bin/pax`,
   `PlistBuddy`, or Homebrew paths.
3. **CI on Linux.** The same suite runs on Linux.
4. **Repeat the milestone 1 walkthrough** on a clean Linux machine.

### Later

- A redacted bug report export that users can preview before sharing.
- More runtimes (for example Gemini) once they pass the same certification.

## Testing against real runtimes

Most tests are hermetic and need no accounts. Tests that drive real Claude or
Codex need logins. When a login is missing those tests are skipped and
reported as skipped, not as passed. Support for a runtime version is claimed
only after its tests pass on that version.

## Not planned

A hosted or multi-user service, networking between machines, a dashboard, and
native Windows. The proposals in [DESIGN.md](DESIGN.md) are historical.
