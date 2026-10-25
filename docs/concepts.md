# Concepts

What the words in the other pages mean, and how the pieces fit. Everything
here is one workstation, one SQLite ledger, no network service.

## The runtime root

Every installed command reads and writes under one directory, the runtime
root: `~/.agent-comms` by default, or the absolute path in `AGENT_COMMS_HOME`
(at most 62 bytes, since supervisor sockets live under it). A second install
with its own `AGENT_COMMS_HOME` shares nothing with the first. Two files
matter most:

- **The registry**, `actors.json`: who exists. Written by `setup`, never by
  hand; `bootstrap` re-registers one that was edited.
- **The ledger**, `agent-comms.sqlite`: messages, dispatches, status posts,
  handoffs, the monitor heartbeat. `AGENT_COMMS_DB` or `--db` names another
  file; every schema migration takes a backup into `backups/` beside it
  first.

Also under the root: `logs/dispatch/` (one log and one event stream per
dispatch), `codex-homes/` (a home per Codex worker), `runtime-custody/` (the
copy of the pinned Codex that workers run), `admin-token` (the operator
credential), and the supervisor's control sockets.

## Actors

An actor is an identity the ledger knows. Two kinds:

- **Human.** You. Registered once by `setup` with a display name and an id
  (a ULID unless you pass `--human-id`). The monitor sends its pages in the
  human's name, the watchdog's stale-heartbeat page is addressed to the
  human, and `admin cancel-dispatch` and `admin settle-dispatch` act only as
  the human.
- **Agent.** A model session. Agents have a **role**, an **id**, a **team**
  and a **project root**.

Roles:

- **Architect.** An interactive session, yours to drive, that coordinates
  with other architects and dispatches bounded work to the workers it owns.
  One per team.
- **Worker.** A headless session started by a dispatch and gone when the
  dispatch ends. Each worker is registered with a **runtime** (`claude`,
  `codex` or `fake`) and an owner, its team's architect. Its command line is
  rendered from the runtime, never written by hand.

A **team** is one architect and its workers, named by `setup --team`, working
in one project root. Teams on the same machine share the ledger, so their
architects can message each other; they never share workers.

## Seats

An agent talks to the ledger through `agent-comms-mcp`, a stdio MCP server
started with `--actor-id`. Every tool call that process makes is bound to that
actor; there is no way to send as someone else from inside it. A **seat** is
one such server registered in one MCP client for one project directory.
`setup` binds the architect's seat in your Claude Code (`claude mcp add`) and
Codex (`<project>/.codex/config.toml`); `bind-seat` does it later for another
client; `agent-comms-seat <actor-id> -- claude` launches a client in a
directory after checking the seat there is the actor you asked for. Workers
are not seated this way: a dispatch starts the worker with a generated MCP
configuration and a restricted tool list. [mcp-setup.md](mcp-setup.md) has
the details per client.

The current server trusts the `--actor-id` it is given. Identity is a
process boundary on one workstation, not authentication.

## The mailbox

A **message** has a sender, recipients, a subject, a body, optional file
**refs** (paths, with a summary each; preferred over pasted content), a
priority, and `requires_ack`. A recipient's copy moves through `sent`,
`read`, `acknowledged`, `closed`:

- **read** happens when the recipient fetches the body.
- **ack** is the recipient's receipt. An ack with a response text is refused
  until a threaded reply carried that text to the sender, because ack and
  close responses are not visible to the sender.
- **close** hides the copy from the recipient's inbox.
- **wait** blocks until a new message arrives or a timeout passes, and
  returns what arrived.

A **status** post is an actor's current summary, files, blocker and next
step, readable by anyone. A **handoff** is a durable snapshot one session
leaves for the next one in the same seat. The discipline that keeps threads
from looping (when to send, when to reply, when to stop) is
[communication_contract.md](communication_contract.md); the prompt that
installs it in an architect seat is [architect-prompt.md](architect-prompt.md).

## Dispatch

A **dispatch** is a bounded task an architect hands to a worker it owns, with
the `dispatch_agent` tool. It is a message to the worker plus a row in the
dispatch ledger with an idempotency key, a **policy** and a time limit.

- The policy, `worker_dispatch_readwrite_bounded`, decides which tools the
  worker sees. Workers cannot dispatch, cancel or register; their
  `send_message` is reply-only. A pre-tool-use hook adds tripwires: it
  denies the runtime's file tools (Claude's Read, Edit and Write, Codex's
  `apply_patch`) a path outside the project root, and denies shell commands
  that match a fixed list of network and external-write patterns. It does
  not see every read (a `cat` in the shell, or a search tool, is not a file
  tool), so containment is the runtime's own sandbox and the stripped
  environment the worker starts with, which keep credentials and the network
  away from it; [mcp-setup.md](mcp-setup.md) records what each runtime does
  and does not enforce.
- The worker runs under a **supervisor**, a process that starts the runtime
  with the policy, answers on an authenticated control socket under the
  runtime root, and records how the worker exited. A dispatch that is not
  terminal when its time limit (one hour) passes is released to the
  dead-letter queue.
- The worker reports with the `close_dispatch` tool: a result, a reply
  message threaded under the dispatch, a summary, and whether it changed
  anything. Both land in the architect's inbox.
- A dispatch is **terminal** as `closed` (with a result such as
  `satisfied`), `dlq`, `spawn_failed_message_landed` (the worker never
  started, and a message saying so reached the architect) or `cancelled`.
  The **DLQ** is the set of `dlq` rows; whenever a row lands there (or its
  worker never started) the monitor pages the architect that dispatched it,
  in the human's name, with a `[DLQ] dispatch_agent ...` message threaded
  under the dispatch.

The **monitor**, `agent-comms-monitor`, is the process that moves rows the
worker no longer can: it starts queued dispatches, reconciles exited
supervisors, releases expired rows, pages for the DLQ and kicks a Codex
login refresh when a token has gone stale. It writes a heartbeat the
`doctor` reads; after five minutes without one the heartbeat is stale. It
runs in the foreground, or once per invocation with `--once` from a
scheduler. Only the `fake` runtime's demo drives itself without it.

The operator can act on a dispatch from the shell with `agent-comms admin`
(`dispatch`, `cancel-dispatch`, `settle-dispatch`, `retry-spawn`, `send`),
which needs the admin token because no architect session is in the loop. The
read side (`dispatch-status`, `inbox`, `read`, `wait`, `status`) needs none.

## Runtimes

A runtime is what a worker runs on: `claude` (Claude Code), `codex` (Codex
CLI) or `fake`, which runs no model, replies to whatever it is sent and
exists so the install can be proven before any login.

- **Pins.** `agent_comms/runtime_pins.json` ships in the package and names,
  per platform (`darwin-arm64`, ...), the runtime version whose certification
  tests passed and the digest of what passed. A dispatch to a platform or
  version without a pin is refused with the reason. `make certify` is how a
  version gets pinned.
- **Custody.** A Codex worker runs a copy of the pinned Codex under the
  runtime root, filled by `custody codex`, never the `codex` on `PATH`
  (which updates itself). A Claude worker runs the certified Claude binary
  from custody, checked against the pinned digest before every run.
- **Homes and logins.** Each Codex worker gets a home that holds trust
  settings only and links to one shared login; the monitor refreshes that
  login after it expires, and when it cannot, `doctor` says which home to
  log in again. Claude workers use your Claude login.

The two runtimes are wired differently in places; the table at the end of
[mcp-setup.md](mcp-setup.md) lists each difference and why.

## The four commands

An install puts four commands on `PATH` (a checkout has the same four under
`.venv/bin` after `uv sync`):

| Command | Who runs it | What it is |
|---|---|---|
| `agent-comms` | you, scripts | setup, doctor, demo, the read side, the admin side |
| `agent-comms-mcp` | your MCP client | the seat server, one process per identity |
| `agent-comms-monitor` | you or a scheduler | the reconciler described above |
| `agent-comms-seat` | you | launches a client in a directory under a checked seat |

## MCP tools

What an architect seat sees. A worker sees the subset its policy allows.

| Tool | Purpose |
|------|---------|
| `whoami` | The identity bound to this server process |
| `list_actors`, `list_agents` | Known actors and agent registrations |
| `send_message` | Send a message with optional file refs |
| `list_inbox`, `read_message` | Read messages addressed to this actor |
| `ack_message`, `close_message` | Acknowledge with a receipt; close a copy |
| `wait_for_reply` | Block until a new message arrives or a timeout |
| `post_status`, `list_status` | Publish and read current status |
| `post_handoff`, `read_handoff` | Durable handoff snapshots between sessions |
| `dispatch_agent` | Hand a bounded task to an owned worker |
| `cancel_dispatch` | Terminate one of this actor's in-flight dispatches |
| `close_dispatch` | A worker reports its result and delta |

## The review and landing cycle

The third layer of the [roadmap](ROADMAP.md) is how this repository lands
its own changes: a brief, an implementation, a review by a different model
family, the gates (`make gate`), then a push approval signed with an SSH key
that a guarded push verifies before anything reaches the remote. The code
for it is in the package (`agent_comms.review`, `agent_comms.push_approval`)
and the guarded push is `scripts/guarded-push`, but these are checkout
workflows run as Python modules from a clone, not subcommands of the four
installed commands. [CONTRIBUTING.md](../CONTRIBUTING.md) describes the gates
and the review expectations that apply to a contribution.
