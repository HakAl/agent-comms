# Quickstart

From nothing to a dispatch that a worker answers, on one Mac. Part 1 needs no
model login and takes a few minutes; it is the path CI proves on every change
(`make install-smoke`). Part 2 adds your real runtimes.

Requirements: macOS, Python 3.11 or newer, Git, and
[uv](https://docs.astral.sh/uv/). Words such as actor, team, seat and
dispatch are defined in [concepts.md](concepts.md); you do not need that
page to finish this one.

## Part 1: install, check, demo

### 1. Install

There is no PyPI release yet; the package installs from a wheel built out of
a clone. The clone is only needed to build the wheel, so it goes in a new
directory of its own:

```sh
mkdir agent-comms-build && cd agent-comms-build &&
    git clone https://github.com/HakAl/agent-comms.git &&
    uv build --project agent-comms --out-dir wheels &&
    uv tool install wheels/agent_comms-*.whl &&
    agent-comms version
```

The steps are chained with `&&`, so each runs only if the one before it
succeeded: `mkdir` refuses a directory that already exists and then nothing
else runs, so nothing of yours is reused or removed. Once `version` works,
`agent-comms-build` can be deleted.
`uv tool install` links `agent-comms`, `agent-comms-mcp`,
`agent-comms-monitor` and `agent-comms-seat` into uv's executable directory.
If it warns that this directory is not on `PATH`, run `uv tool update-shell`
and open a new terminal (or add the directory it names to `PATH` in the
current one) before going on. pipx users can install the same wheel with
`pipx install wheels/agent_comms-*.whl` in place of the `uv tool install`
line. `version` prints the release, the platform key it resolved and the
certified runtime version per runtime for it; its git fields read `unknown`,
since there is no checkout. Everything the commands read or write lives
under `~/.agent-comms` (`AGENT_COMMS_HOME` moves it).

Working from a checkout instead: `uv sync` puts the same four commands under
`.venv/bin`; use `.venv/bin/agent-comms` wherever this page says
`agent-comms`.

### 2. Set up a team on the fake runtime

```sh
mkdir -p "$HOME/demo-project"
agent-comms setup --project-root "$HOME/demo-project" --team demo \
    --runtimes fake --clients "" --human alice --yes
```

This writes the registry (you as the human actor, one architect, one fake
worker), registers them in the ledger, and prints what it did as JSON. Note
the id under `human`: the monitor is started with it later. The project root
is where the team works and must exist; any directory of yours will do for
the demo. `--clients ""` binds no MCP seat yet (the fake runtime has no
client), `--yes` takes defaults instead of asking.

On a terminal without `--yes`, `setup` asks for anything left out and
suggests the runtimes it detects on the machine.

### 3. Check the install

```sh
agent-comms doctor
```

One JSON report, exit code 0. Each check is `ok`, `warn`, `skip` or `fail`,
and every `fail` carries the command that fixes it, collected again under
`fixes`. Right now nothing fails: the native runtimes, the platform pin and
the architect's seat are `skip` (the team does not use them yet), and the
monitor and the admin token are `warn` (no monitor has run and no token
exists, and neither is needed for the demo). A `fail` here means step 2 did
not finish; run the fix it names and rerun `doctor` until it exits 0. The [runbook](runbook.md) explains the report
in full.

### 4. Run the demo

```sh
agent-comms demo
```

The architect dispatches one task to the fake worker, the worker replies and
closes the dispatch, and each step is printed to stderr as it happens:

```text
dispatched dispatch_...: demo-architect -> demo-fake-worker, 'demo: ping'
worker replied msg_...: 'fake-reply: PONG'
dispatch closed: satisfied
```

Stdout is one JSON object with the dispatch, the reply (threaded under the
dispatch message), the final row (`closed`, `satisfied`), where the worker's
log landed, and `worker_reaped`. Exit 0 means the dispatch closed satisfied
with a reply. The demo drives its one dispatch itself, so no monitor and no
admin token are involved, and `doctor` stays clean afterwards.

You have now seen the whole dispatch path with no model account. Everything
in Part 2 is the same path with a real runtime.

## Part 2: your runtimes

### 5. Set up a team on Claude Code and Codex

Run `setup` again for your real project, with the runtimes you have. The
human you registered in step 2 is reused.

```sh
agent-comms setup --project-root /absolute/path/to/your/project --team team-a \
    --runtimes claude,codex
```

Drop a runtime you do not have (`--runtimes claude` alone is fine). For each
native runtime, `setup` also:

- binds the architect's seat in your own CLI of that runtime, so a `claude`
  or `codex` session you open in that project directory has the mailbox
  tools: `claude mcp add` for Claude Code, `<project>/.codex/config.toml`
  (kept out of git) for Codex. `--clients claude,codex` chooses explicitly;
- provisions each Codex worker's home and copies the pinned Codex into
  custody, since workers never run the `codex` on `PATH`.

### 6. Doctor until clean

```sh
agent-comms doctor
```

With native runtimes the report checks more: the platform's pins, each
runtime's custody copy, version and login, every Codex worker home, the
architect's seats in each client, the monitor heartbeat and the admin token.
Expect failures on a fresh machine, and treat `fixes` as the to-do list: a
Codex login for a worker home is `CODEX_HOME=<home> codex login` with the
home filled in; a missing custody copy is `agent-comms custody codex`; a
Codex project that is not trusted yet is "open `codex` in the project once
and accept the trust prompt". Run each fix, rerun `doctor`, stop when it
exits 0. The remaining warning about the monitor goes away in the next step.

On a machine where nothing has signed in yet, expect three logins, each with
your own account: the Claude custody copy (the `auth login` command the fix
prints), your own `codex login` (the architect seat in your terminal and the
trust prompt use it), and the workers' shared Codex home (the printed
`CODEX_HOME=<runtime root>/codex-auth codex login`). Create that directory
with `mkdir -p` before the shared login; `setup` does not create it yet and
Codex refuses a `CODEX_HOME` that does not exist. On a machine without a
browser, add `--device-auth` to each `codex login` and finish the sign-in
from another device. The Claude custody copy is a file you place: the fix
names the version and the path, and the installer keeps that version's
binary under `~/.local/share/claude/versions/`, so copy it from there with
`install -m 0755`. The release walkthrough in
[releases/0.2.0-macos-walkthrough.md](releases/0.2.0-macos-walkthrough.md)
shows each of these as it was run.

### 7. Start the monitor

Real workers do not drive themselves. In a second terminal, with the human id
`setup` printed in step 2 (the monitor warning in `doctor` prints this exact
command):

```sh
agent-comms-monitor --human-actor-id 01M36YTJV9XBW95S6ZWV47C4RG
```

It starts queued dispatches, reconciles finished ones, pages the architect
when its work lands in the dead-letter queue, and writes a heartbeat
`doctor` checks.
Leave it running while you work; stop it with Ctrl-C. `--once` runs a single
pass instead, for a scheduler. See the [runbook](runbook.md) for keeping it
up.

### 8. The first real dispatch

Open the architect seat: a `claude` or `codex` session in the project
directory from step 5, where `setup` bound the seat. From a checkout, or to
be sure the directory's seat is the one you expect, the launcher checks and
then starts the client:

```sh
cd /absolute/path/to/your/project
agent-comms-seat team-a-architect -- claude
```

Give the session the operating rules in
[architect-prompt.md](architect-prompt.md) (paste them, or put them in the
project's agent instructions). Then ask it for the dispatch in your own
words, for example: "Call whoami, then dispatch_agent to team-a-codex-worker
with idempotency key first-1, subject 'hello', body 'Reply with the
repository's top-level directory names.' and wait_for_reply."

What happens:

1. `dispatch_agent` writes the dispatch; the monitor starts the worker under
   its supervisor with the bounded policy.
2. The worker does the task, sends its reply threaded under the dispatch,
   and calls `close_dispatch` with its result.
3. The reply reaches the architect's inbox; `wait_for_reply` returns it.

From a third terminal you can watch the row and read the reply:

```sh
agent-comms dispatch-status
agent-comms inbox team-a-architect
```

`dispatch-status` shows the row go `queued`, `in_flight`, then `closed`
with result `satisfied`. A row that lands in `dlq` or
`spawn_failed_message_landed` instead sends the architect a page naming the
reason, which shows up in the inbox above; the [runbook](runbook.md) covers
what to do.

## Where next

- [runbook.md](runbook.md): keeping the monitor up, reading `doctor`,
  logs, the DLQ, Codex logins and upgrades, backups.
- [mcp-setup.md](mcp-setup.md): what `setup` wired into each client, seats
  for other MCP clients, and how Claude and Codex workers differ.
- [concepts.md](concepts.md): the vocabulary and the model behind it.
