# Runbook

Operating an install, task by task: when you need it, what to run, what you
should see. Commands are the installed ones on `PATH`; from a checkout use
`.venv/bin/<command>`. Everything here is behavior the tests or the install
walkthrough check; nothing is a story about any particular deployment.

The example human id `01M36YTJV9XBW95S6ZWV47C4RG` stands for the one
`setup` printed under `human` on your machine.

## Read a doctor report

**When:** after any setup change, when a dispatch misbehaves, before asking
for help.

```sh
agent-comms doctor
```

One JSON object: `ok`, `platform`, `checks` and `fixes`. Exit 0 when no
check failed, 3 otherwise. Each check has an `id`, a `status` (`ok`, `warn`,
`skip`, `fail`), a `detail` and, for failures, a `fix`: the command or step
that repairs it. `fixes` repeats every failing check's fix in order, so you
(or an agent) can act on the list without reading the details. Doctor writes
nothing and repairs nothing.

| Check id | What it looks at | Typical fix |
|---|---|---|
| `install` | the four console scripts beside the interpreter | reinstall |
| `runtime_root` | the root exists, is writable, and `AGENT_COMMS_HOME` fits | `mkdir`, `chmod`, or a shorter `AGENT_COMMS_HOME` |
| `registry` | `actors.json` loads and lists actors | the `setup` or `bootstrap` command it prints |
| `ledger` | the ledger opens, its schema is known, one human and an architect exist | `setup`; upgrade the package for a newer schema |
| `platform` | a certified pin exists for this platform and each native runtime | see Runtime pins in CONTRIBUTING |
| `runtime:claude` | custody copy, digest, version, login | `install` command printed; `claude` login |
| `runtime:codex`, `runtime:codex:auth` | custody copy and the shared login file | `agent-comms custody codex`; `codex login` |
| `runtime:codex:home:<worker>` | each worker home and its link to the login | the printed `codex login` with that `CODEX_HOME` |
| `runtime:codex:login:<lineage>` | a login that expired and refresh could not recover | the same `codex login` |
| `monitor` | heartbeat present, fresh, and its pid alive | the monitor command |
| `mcp:<architect>:<client>` | the seat each client records, and for Codex what it resolves | the `bind-seat` line printed; trust the project in Codex |
| `admin_token` | `admin-token` present, mode 600 | see Operator credential |

`warn` never fails the report: no monitor while nothing has been dispatched
(or only finished demo dispatches exist), or a missing admin token. `skip`
names what did not apply (no Codex worker registered, say).
`doctor --clients claude,codex` checks those seats whether or not `setup`
recorded them. The report is also where the exact command for most of the
tasks below comes from, with your paths filled in.

## Keep the monitor running

**When:** always, while any claude or codex worker may be dispatched.

Foreground, in its own terminal:

```sh
agent-comms-monitor --human-actor-id 01M36YTJV9XBW95S6ZWV47C4RG
```

Each pass starts queued dispatches, reconciles exited supervisors, releases
expired rows to the dead-letter queue and pages their architects, kicks a
Codex login refresh when dispatch reported a stale token, and writes a heartbeat.
The default interval is five seconds (`--interval`). `--log-file PATH`
writes the passes to a rotating log instead of the terminal. Ctrl-C stops
it cleanly.

From a scheduler, one pass per invocation:

```sh
agent-comms-monitor --human-actor-id 01M36YTJV9XBW95S6ZWV47C4RG --once
```

`--once` returns once the pass is done, which for a freshly queued dispatch
means after the worker is up, not after it finishes; a later pass reconciles
the exit. A pass every minute or two keeps the heartbeat fresh (it counts as
stale after five minutes without a pass). Know what `doctor` makes of this:
its `monitor` check wants a fresh heartbeat and the pid that wrote it still
running, and a `--once` pass exits as soon as it is done, so between
scheduled passes `doctor` fails that check with the foreground command above
as the fix. The install walkthrough ends on exactly that state after its
`--once` passes, which is why its last `doctor` exits 3. Under a scheduler,
use the watchdog below, which checks freshness only, rather than `doctor` to
tell whether passes are happening.

A watchdog for the monitor itself:

```sh
agent-comms-monitor --human-actor-id 01M36YTJV9XBW95S6ZWV47C4RG --check-heartbeat
```

It exits 0 while the heartbeat is fresh. When it is stale or absent it sends
you a `[monitor stale]` message (priority blocker, requires ack), at most
once an hour, and exits 1. If that page cannot be written (the ledger itself
is unreadable, say) it exits 2 instead and records the alarm outside the
database: a line in `logs/monitor-watchdog.page.log` under the runtime root,
throttled by a marker file beside it, and, when `--fallback-page-root DIR` is
given, a `new_messages` semaphore under `DIR/.agent-comms/monitor-watchdog/`
for a client that watches that directory. There is no service manager yet;
running the monitor under launchd is a milestone 2 item in the
[roadmap](ROADMAP.md).

## Find the logs

**When:** a worker did something unexpected, or a dispatch ended in the DLQ.

- Each dispatch writes `logs/dispatch/<dispatch_id>.log` (what the worker
  printed) and `<dispatch_id>.events.jsonl` (the supervisor's events) under
  the runtime root. `AGENT_COMMS_DISPATCH_LOG_DIR` moves them.
- `dispatch-status` prints each row's `worker_log` and `worker_events`
  paths below the table once they are known; the demo prints `worker_log` in
  its JSON.
- The monitor writes to its terminal or to `--log-file`.
- The MCP server prints one `agent-comms startup:` line to stderr when a
  client starts it, which the client's own MCP log keeps; it names the
  release so a client log can be matched to an install.

## Inspect dispatches and the DLQ

**When:** waiting on a dispatch, or after a page.

```sh
agent-comms dispatch-status
agent-comms dispatch-status --status dlq
agent-comms dispatch-status --json
```

The table lists recent rows with their status and result (`--limit`
changes how many). A row is terminal as `closed` (result `satisfied`,
among others), `dlq`, `spawn_failed_message_landed` or `cancelled`;
`queued` and `in_flight` are the monitor's to move. `--status dlq` is the
dead-letter view. Under the table, `codex-credential-dead` lines name a Codex
login lineage the refresh could not recover and `custody-defect` lines name
a worker whose stored spawn is broken, each with its remedy; the JSON form
returns the rows, and exits 3 with the defects when there are any.

When a row lands in the DLQ, or its worker never started, the monitor pages
the architect that dispatched it, not you: a message from your human actor to
the architect, subject `[DLQ] dispatch_agent <reason>: <worker>`, priority
blocker, threaded under the dispatch message, carrying the row's status, its
failure reason and the action to take. It lands in the architect's inbox next
to the worker's replies, so the architect session sees it on its next inbox
check or `wait_for_reply`. From the shell you can read it as the architect:

```sh
agent-comms inbox team-a-architect
agent-comms read team-a-architect <message-id>
agent-comms ack team-a-architect <message-id>
```

`agent-comms wait <actor-id> --timeout 90` blocks for the next message to an
actor and returns `messages` and `timed_out`. The one page addressed to you
is the watchdog's `[monitor stale]` message (above); read it with the same
commands and your human id.

## Act on a dispatch by hand

**When:** a dispatch must be cancelled, settled, or started from the shell
with no architect session in the loop.

These are the `admin` commands. They need the operator credential
(below); the read side never does. `--from-actor-id` names the actor the
command acts as, and each verb has its rule: `cancel-dispatch` and
`settle-dispatch` accept your human id only; `dispatch` takes the producing
architect's id, since the row needs a producer that owns the worker; `send`
takes any registered actor.

Cancel one that is queued or in flight (the worker is terminated if it is
reachable):

```sh
agent-comms admin cancel-dispatch --from-actor-id 01M36YTJV9XBW95S6ZWV47C4RG \
    --dispatch-id <dispatch-id> --reason "superseded by a new brief"
```

Settle a row the monitor could not: first a dry run, which changes nothing
and prints a sealed plan (valid for five minutes), then the execution of
exactly that plan, with the literal acknowledgement that the worker's
termination was not confirmed:

```sh
agent-comms admin settle-dispatch --from-actor-id 01M36YTJV9XBW95S6ZWV47C4RG \
    --dispatch-id <dispatch-id> --dry-run --reason "worker host rebooted"
agent-comms admin settle-dispatch --from-actor-id 01M36YTJV9XBW95S6ZWV47C4RG \
    --dispatch-id <dispatch-id> --execute-plan <plan> --release-with-termination-unconfirmed
```

Dispatch without an architect, as the install walkthrough does (the
override reason is recorded on the row):

```sh
agent-comms admin dispatch --from-actor-id team-a-architect --target-actor-id team-a-fake-worker \
    --idempotency-key by-hand-1 --requested-policy worker_dispatch_readwrite_bounded \
    --override-reason "operator smoke test" --subject ping --body "Reply with PONG."
```

A row whose worker never started (`spawn_failed_message_landed`) can be
respawned with `agent-comms admin retry-spawn --dispatch-id <id>` once the
cause named in the failure message is fixed. `agent-comms admin send` sends a
message as any actor, for the rare case the mailbox needs an operator's hand.

## Operator credential

**When:** before the first `admin` command, or when `doctor` warns about
`admin_token`.

```sh
root="${AGENT_COMMS_HOME:-$HOME/.agent-comms}"
(umask 077; head -c 32 /dev/urandom | xxd -p -c 64 > "$root/admin-token")
export AGENT_COMMS_ADMIN_TOKEN="$(cat "$root/admin-token")"
```

The file must be mode 600 and live at `admin-token` under the runtime root
of the install the command is for: `~/.agent-comms`, or `AGENT_COMMS_HOME`
when that is set, which is why the recipe resolves the root first instead of
writing under `~/.agent-comms`; the variable must match the file. An `admin` command, and `rerender-spawn
--apply`, refuse without both and never print the token. Architect sessions
dispatch through their seat and never need it; neither does the demo.

## Codex logins

**When:** `doctor` fails `runtime:codex:login:<lineage>` or
`runtime:codex:home:<worker>`, `dispatch-status` prints a
`codex-credential-dead` line, or a Codex dispatch fails with a login error.

Logins cannot be refreshed ahead of time: a Codex token is refreshed after
it expires, by the monitor, when dispatch reports it stale. When that
refresh fails, dispatch to that worker stops with the reason, and `doctor`
prints the login command for the home concerned, of the form
`CODEX_HOME=<runtime root>/codex-homes/default/<worker> codex login`. Run
it, then `doctor` again. Worker homes link to one shared login, so one login
usually clears every worker of the team.

To see which workers share a login, and to run one refresh pass yourself:

```sh
agent-comms codex-auth-targets
agent-comms refresh-codex-auth
```

`codex-auth-targets` groups Codex actors by login lineage without printing
any path or secret. `refresh-codex-auth` runs the same pass the monitor
would and exits nonzero when a refresh failed.

## Codex and Claude upgrades

**When:** `doctor` fails `runtime:codex` or `runtime:claude` after a runtime
updated itself, or a dispatch is refused for a stored spawn.

Workers run the pinned version from custody, so your own `codex` or `claude`
updating changes nothing for them until the pin changes. When a new version
has been certified and pinned (`make certify`, see
[CONTRIBUTING.md](../CONTRIBUTING.md)), fill the custody copy from the
install on `PATH`; `--replace` swaps out a copy that no longer matches:

```sh
agent-comms custody codex --replace
```

For Claude, `doctor` prints the `install` step for the certified binary.
When the rendered worker command line changed between releases (a flag such
as Claude's `--verbose`, or a Codex profile that is no longer used), a
dispatch to a stale stored spawn is refused before the worker starts, and
the spawn is re-rendered from the runtime; `--apply` needs the operator
credential:

```sh
agent-comms rerender-spawn --runtime codex
agent-comms rerender-spawn --runtime codex --apply
```

Without `--apply` it prints what would change.

## Backups, restore, and upgrading from 0.1.0

**When:** before and after a package upgrade, or when a ledger must be
rolled back.

Every schema migration takes a backup first. When a release opens an existing
ledger whose schema it will change, it copies the ledger with the SQLite
backup API to `backups/<ledger name>.<UTC stamp>.v<old user_version>.bak`
beside the ledger, reports the path on stderr, and runs the whole migration
in one transaction; an interrupted migration leaves the ledger as it was and
the next open completes it. Opens that change nothing take no backup.

To restore: stop the monitor and every seat, copy the backup over the
ledger, delete the `-wal` and `-shm` files beside it, and reopen with the
release you were running before the upgrade, whose schema the backup has
(the `v<n>` in its name is the stamp that release left). The release that
wrote the backup is the one that was about to migrate it: reopening with that
release migrates again, after another backup, which is fine when the point
was to recover rows rather than to go back a release. Two refusals to know
about: a release refuses to open the default ledger when a newer release
stamped it, and also when the stamp is below its own compatibility floor (a
floor bump is a deliberate break, and the message says to keep using
floor-compatible code); `doctor` reports the same. An explicit `--db` path
opens either, and migrates the older one forward.

The original 0.1.0 release kept its mailbox at `data/agent-comms.sqlite`
inside the checkout, with five tables and no actors. Current code refuses to
open such a file in place and tells you to import it:

```sh
agent-comms import-mailbox /path/to/old-checkout/data/agent-comms.sqlite
```

The target is the ledger of the invocation (the default, or
`AGENT_COMMS_HOME` / `--db`), which must not exist yet. The import reads the
old file and never writes it, resolves every worker's owner (the only
architect in its team; `--owner WORKER=ARCHITECT` where a team has none or
several), migrates a copy in memory, checks row counts and foreign keys, and
only then writes the target through a staging file linked into place. Only
an unmodified 0.1.0 mailbox is imported; anything added to it is refused by
name. Run `agent-comms setup` next: it adds your human actor and the seats,
which 0.1.0 did not have.

## A second install

**When:** a development checkout beside a live install, or two independent
ledgers on one machine.

Set `AGENT_COMMS_HOME` to an absolute path of at most 62 bytes for every
command of the second install:

```sh
mkdir -p "$HOME/dev-project"
AGENT_COMMS_HOME="$HOME/.agent-comms-dev" agent-comms setup --project-root "$HOME/dev-project" \
    --team dev --runtimes fake --clients "" --yes
AGENT_COMMS_HOME="$HOME/.agent-comms-dev" agent-comms doctor
```

Registry, ledger, logs, custody, worker homes and supervisor sockets all live
under it, and nothing under `~/.agent-comms` is read or written. Seats bound
from that install carry `--home` so the client starts the server in the same
install, and `doctor` treats a missing `--home` as a wrong binding. Claude
and Codex logins stay where they are. The test suite does the same with a
scratch home on every run.

## Report a failure

Collect the package revision (`agent-comms version`), the `doctor` report,
the `dispatch-status` row and the dispatch's log and events files, and
follow the sanitizing advice in
[CONTRIBUTING.md](../CONTRIBUTING.md#useful-failure-reports): no auth files,
tokens, full mailbox, or runtime homes. A redacted bug report export is
planned, not available.
