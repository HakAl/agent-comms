# Architect Communication Relay — Design

> **Status**: this document is the full design. Phases 1–3 are shipped today. Phases 4–6 are designed but not implemented. See the per-phase status markers and the rollout plan at the bottom.

## Goal

Automate communication between several architect teams (typical examples below as `team-a`, `team-b`, `team-c`, `team-d`):

- `team-a`
- `team-b`
- `team-c`
- `team-d`

Each team has an architect running in its own CLI session and delegating to other agents. Without coordination, a human operator must relay cross-team findings by hand. The goal is to replace that manual relay with a local, auditable message bus that each architect can use without merging the teams into one orchestrator.

## Recommendation

Do not build a new agent protocol.

Use existing protocol concepts and build only the small local adapter needed for this workstation:

- Use **A2A-style concepts** for agent identity, capabilities, messages, tasks, and artifacts.
- Expose local communication through an **MCP server**, because agent CLIs already know how to call tools better than they know how to become peer network services.
- Store messages in **SQLite** for durability, simple inspection, and offline operation.
- Treat dashboard sends as **operator-authored mailbox messages**, not as a separate note system.

This keeps the implementation small while preserving a path to real A2A servers later.

The highest-priority rule is: **the default is not to message**. The system is successful only if it reduces coordination load instead of creating a new communication workload.

## Non-Goals

- Do not centralize all teams into one multi-agent framework.
- Do not allow one architect to directly edit another team's files through the relay.
- Do not make the relay decide research conclusions, merge code, or arbitrate conflicts.
- Do not copy private corpora or large generated artifacts between projects.
- Do not require cloud services, Slack, Discord, Matrix, or hosted brokers.

## Existing Options Considered

### A2A / Agent2Agent

A2A is the best conceptual fit for independent agents communicating across system boundaries. It defines useful ideas such as agent cards, messages, tasks, and artifacts.

Tradeoff: using A2A directly works best when every architect is exposed as an A2A server/client. The current workflow is terminal-oriented, so direct A2A adoption would require more wrapper code than a local mailbox.

### BeeAI ACP

ACP is also aimed at agent-to-agent and human-agent messaging. Current public materials indicate ACP has moved toward A2A alignment.

Tradeoff: useful reference, but it adds framework commitment without solving the immediate terminal integration problem.

### MCP

MCP is not an agent-to-agent protocol, but it is the best integration layer for this workflow. It lets every architect call shared tools like `send_message` and `check_inbox`.

Tradeoff: MCP supplies the tool surface, not the message semantics. We define a small schema for that.

### Generic Brokers

Redis Streams, NATS, Matrix, Slack, or Discord can move messages.

Tradeoff: they do not solve agent identity, file references, task summaries, acknowledgements, or project-boundary policy. SQLite is enough for the first local version.

### Agent of Empires

Agent of Empires is the strongest prior-art match for the operator UX. It already provides a TUI, web dashboard, multi-CLI support, tmux-backed session survival, status detection, and `aoe send <session> <message>` for injecting messages into running agent sessions.

There are two viable paths:

- **Adopt AoE wholesale**: get the dashboard and tmux session manager immediately, but migrate away from the current SQLite+MCP mailbox semantics and adopt AoE's data model.
- **Steal the tmux pattern**: keep the mailbox schema that already models your project coordination (`priority`, `refs`, `requires_ack`, threads, statuses), and add a small tmux wakeup layer.

Recommendation: steal the tmux pattern for now. The mailbox semantics matter because cross-team your project coordination needs durable message refs, ack state, status trails, and a neutral MCP tool surface across Claude Code, Codex CLI, and Gemini CLI. AoE remains a useful comparison point, and adopting it later should stay an explicit option if maintaining a local dashboard becomes more expensive than migrating.

## Proposed Architecture

```text
<repo-root>
  agent_comms/
    mcp_server.py      # MCP server entrypoint
    store.py           # SQLite persistence
    schema.py          # message and agent validation
    cli.py             # optional human inspection commands
  data/
    agent-comms.sqlite # local runtime database, gitignored
    attachments/       # optional local copies, disabled by default
  config/
    agents.yaml        # known architect identities and project roots
  docs/
    architect-prompt.md # instructions to paste into architect sessions
```

The relay runs locally and exposes MCP tools. Architect CLIs connect to the MCP server. Humans can inspect the same state through a CLI or direct SQLite reads.

The operating contract for when to message, when not to reply, and how to avoid loops lives in `docs/communication_contract.md`. The contract is intentionally non-binding, but the dashboard should surface contract drift so the human can intervene before coordination loops form.

Harness-specific configuration notes live in `docs/harness_integration.md`. Prefer Claude/Codex/Gemini native MCP, prompt, hook, and session config before adding new receiver machinery to `agent-comms`.

## Agent Registry

Initial `config/agents.yaml`:

```yaml
agents:
  team-c-architect:
    team: team-c
    role: architect
    project_root: /path/to/team-c/repo
    capabilities:
      - local-model-eval
      - model-serving
      - provenance-review
      - cross-team-summary

  team-a-architect:
    team: team-a
    role: architect
    project_root: /path/to/team-a/repo
    capabilities:
      - signal-design
      - audit-scaffold
      - classifier-review

  team-d-architect:
    team: team-d
    role: architect
    project_root: /path/to/team-a/repo
    capabilities:
      - l1-analysis
      - cross-family-eval
      - pipeline-integration
      - release-review

  team-b-architect:
    team: team-b
    role: architect
    project_root: /path/to/team-a/repo
    capabilities:
      - route-analysis
      - corpus-stratification
      - eval-review
```

## Message Schema

Messages should be compact and path-oriented. Large content stays in the owning repo.

```json
{
  "id": "msg_20260517_153012_lab_sensor_01",
  "from_type": "architect",
  "from_id": "team-c-architect",
  "to": ["team-a-architect"],
  "parent_message_id": null,
  "subject": "ZH route finding relevant to sensor false positives",
  "body": "Short summary of what changed and why the recipient should care.",
  "refs": [
    {
      "display_path": "/path/to/team-a/repo/parapet/implement/research-findings/zh_route.md",
      "resolved_path": "/path/to/team-a/repo/parapet/implement/research-findings/zh_route.md",
      "exists_at_send": true,
      "summary": "Route analysis notes; see section on register-paired benign samples."
    }
  ],
  "priority": "normal",
  "requires_ack": true,
  "valid_until": null,
  "created_at": "2026-05-17T15:30:12-04:00"
}
```

There is no global message status. Lifecycle state is recipient-scoped in `message_recipients` and derived for display. This avoids ambiguity for broadcasts where one recipient has read the message, another has acknowledged it, and another has not seen it yet.

### Expiry Semantics

`valid_until` is advisory, not destructive.

- Visibility: expired messages are hidden or de-emphasized by default in inbox/dashboard views unless `include_expired` is true.
- Wake behavior: expired messages should not trigger semaphores, tmux wakeups, or human notifications unless forced by the operator.
- Status behavior: expiry does not auto-close or auto-ack a message.
- Contract behavior: expired unread messages are not contract violations unless they were `high` or `blocker` and crossed the configured ack SLA before expiry.
- Audit behavior: expired messages and their events remain queryable.
- Operator behavior: the dashboard can force-close expired stale messages with an audit event.

Supported priorities:

- `low`
- `normal`
- `high`
- `blocker`

Supported statuses:

- `sent`
- `read`
- `acknowledged`
- `closed`

## MCP Tool Surface

### `register_agent`

Registers or refreshes an architect identity.

Inputs:

- `agent_id`
- `team`
- `role`
- `project_root`
- `capabilities`

This is an administrative/bootstrap operation. Normal message sending must not trust a free-form `from_agent` parameter.

### `list_agents`

Lists registered architects and their capabilities.

Inputs:

- `capability`
- `team`

Use this for capability discovery before asking the human who owns an area. For example, an architect blocked on local serving can query for `model-serving`; an architect blocked on classifier behavior can query for `signal-design`.

### `get_agent_capabilities`

Returns one architect's team, project root, and capabilities.

Inputs:

- `agent_id`

### Per-Architect MCP Identity

Stdio MCP does not provide a trusted per-call client identity. Every call through one stdio pipe is just input from that subprocess. Do not model architect identity as a runtime `bind_session` tool over stdio.

Instead, run one MCP server subprocess per architect with identity fixed at launch:

```bash
agent-comms-mcp --agent-id team-b-architect
agent-comms-mcp --agent-id team-a-architect
agent-comms-mcp --agent-id team-c-architect
agent-comms-mcp --agent-id team-d-architect
```

The server stores the configured `agent_id` in process memory and derives `from_agent` from that value for all architect-scoped tools. A confused architect can call `send_message`, but cannot claim another `from_agent` through tool arguments.

Operator-facing CLI and dashboard endpoints may still accept explicit architect ids for local administrative actions. Those paths are not the architect MCP path.

### `send_message`

Sends a message to one or more architects.

Inputs:

- `to_agents`
- `subject`
- `body`
- `refs`
- `priority`
- `requires_ack`
- `valid_until`

Server-derived fields:

- `from_type = "architect"`
- `from_id = <process --agent-id>`
- `created_at`

### `reply_message`

Sends a threaded reply to an existing message.

Inputs:

- `parent_message_id`
- `to_agents`
- `body`
- `refs`
- `priority`
- `requires_ack`
- `valid_until`

Server-derived fields:

- `from_type = "architect"`
- `from_id = <process --agent-id>`
- `created_at`

`send_message` may also accept `parent_message_id`, but a distinct `reply_message` tool keeps the common clarifying-question workflow explicit.

### `broadcast_message`

Sends one message to all architects or to selected teams.

Inputs:

- `teams`
- `subject`
- `body`
- `refs`
- `priority`
- `requires_ack`
- `valid_until`

Server-derived fields:

- `from_type = "architect"`
- `from_id = <process --agent-id>`
- `created_at`

### Operator Dashboard Send

The dashboard is an administrative control surface, not an architect. It may send messages explicitly as the human operator.

Endpoint:

```text
POST /messages
```

Inputs:

- `to_agents`
- `subject`
- `body`
- `refs`
- `priority`
- `requires_ack`
- `valid_until`
- `wake_policy`: `auto` | `skip` | `force`

Server-derived fields:

- `from_type = "operator"`
- `from_id = "human-operator"`
- `created_at`
- `message_id`

Dashboard sends must enter the same inbox, ack, close, semaphore, wakeup, and audit path as architect messages. Do not create a separate dashboard-note channel.

### `list_inbox`

Lists messages addressed to the caller.

Inputs:

- `unread_only`
- `include_closed`
- `since`
- `include_expired`
- `limit`

### `wait_for_reply`

Optional convenience wrapper that blocks until the caller receives a new unread message, a reply in a thread, or a timeout expires.

This must not be required for correctness because MCP clients may enforce tool-call timeouts differently. The robust interface is `list_inbox(since=...)` plus short polling from the architect prompt or dashboard. Keep `wait_for_reply` only after validating the active Claude/Codex/Gemini clients tolerate the timeout values used in practice.

Intended split:

- `wait_for_reply`: active, bounded wait when an architect is synchronously blocked on a specific reply.
- `.agent-comms/<agent_id>/new_messages`: passive between-task signal checked at natural breakpoints.

Inputs:

- `after_message_id`
- `timeout_seconds`
- `poll_interval_seconds`

### `read_message`

Returns full message details and marks the message read for the caller.

Inputs:

- `message_id`

### `ack_message`

Acknowledges a message and records a short response.

Inputs:

- `message_id`
- `response`

### `close_message`

Closes a message for the recipient once it no longer needs attention.

Inputs:

- `message_id`
- `response`

### `post_status`

Publishes a current team status without targeting a specific recipient.

Inputs:

- `summary`
- `current_files`
- `blocked_on`
- `next_step`

### `list_status`

Returns latest status from all teams.

Inputs:

- `teams`

Caller-scoped MCP tools derive the acting architect from the per-architect MCP subprocess `--agent-id`. Operator-facing CLI and dashboard endpoints may accept explicit architect ids, but those paths should be treated as local administrative control surfaces.

## Architect Operating Rules

Each architect gets the same communication instructions:

```text
Follow docs/communication_contract.md. The default is not to message.

At the start of a session, register yourself with agent-comms and check your inbox.

Before doing work that may affect another team, check recent status for that team.

Send only for handoffs, blockers, assumption changes, external signals, contract changes, or human-requested coordination.

Use refs instead of pasting large files or generated artifacts.

Do not ask another architect to edit files outside its lane unless the human operator explicitly asks for that.

If a received message affects your lane, acknowledge it with the action you took or the reason no action is needed.

Before every major task or after any long-running command, check for your local `.agent-comms/<agent_id>/new_messages` semaphore in your project root. If it exists, call `list_inbox`, handle relevant messages, then clear the semaphore.

Before finishing a substantial task, post status with current files, blockers, and next step.
```

## Persistence Model

SQLite tables:

```sql
agents(
  id text primary key,
  team text not null,
  role text not null,
  project_root text not null,
  capabilities_json text not null,
  last_seen_at text not null
)

messages(
  id text primary key,
  parent_message_id text,
  from_type text not null, -- architect, operator, system
  from_id text not null,
  subject text not null,
  body text not null,
  refs_json text not null,
  priority text not null,
  requires_ack integer not null,
  valid_until text,
  created_at text not null
)

message_recipients(
  message_id text not null,
  to_agent text not null,
  status text not null,
  read_at text,
  acked_at text,
  closed_at text,
  ack_response text,
  primary key(message_id, to_agent)
)

statuses(
  id text primary key,
  agent_id text not null,
  summary text not null,
  current_files_json text not null,
  blocked_on text,
  next_step text,
  created_at text not null
)

message_events(
  id text primary key,
  message_id text not null,
  agent_id text not null,
  event_type text not null, -- sent, read, acked, closed, replied, woke, wake_failed
  body text,
  created_at text not null
)
```

`message_events` is append-only audit state. Current recipient status stays in `message_recipients` for efficient inbox queries, while `message_events` records how the state changed and captures wakeup attempts.

## SQLite Operational Assumptions

SQLite is acceptable for this single-workstation workflow, but concurrent MCP subprocesses, dashboard reads, CLI commands, wakeup writes, semaphore updates, and polling require discipline.

Required pragmas:

```sql
pragma journal_mode = wal;
pragma busy_timeout = 5000;
```

Operational rules:

- Keep all writes in short transactions.
- Do not hold long-lived read transactions.
- Prefer raw SQLite or SQLAlchemy Core-style explicit SQL.
- Do not introduce async ORM complexity.
- Do not compute dashboard state by replaying all events on every request.
- Query current state from normalized tables, then use events for audit detail.

Concurrency verification:

- Start all four per-architect MCP subprocesses against the same DB.
- Run concurrent `list_inbox`, `send_message`, `post_status`, and dashboard reads.
- Fail the smoke test on any `database is locked` error.

## Event Retention

`message_events` is append-only for auditability, but it must not become the operational source of truth or grow without bound.

Retention policy:

- Keep current recipient state in normalized tables indefinitely.
- Keep recent events in SQLite for a configurable window, for example 30 days.
- Preserve `high` and `blocker` events longer or indefinitely.
- Archive older low/normal events to compressed NDJSON under `data/archive/`.
- Dashboard queries should prefer current-state tables and only load event history for a selected thread.

Compaction must preserve message ids, recipient status, final ack/close state, and high/blocker audit trails.

## Boundary And Safety Rules

- The relay stores file paths and summaries, not file contents by default.
- Attachments are disabled in v1. If needed later, only copy explicit small files into `data/attachments/`.
- The relay does not execute shell commands on behalf of architects.
- The relay does not write into architect project roots except for the explicit semaphore path described below.
- The relay resolves and canonicalizes path refs before storing them.
- The relay should reject refs whose resolved path is outside configured project roots unless `allow_external_refs` is explicitly enabled.
- Store `display_path`, `resolved_path`, and `exists_at_send` for each ref. Optionally store `sha256` for small files if later audit needs exact-content identity.
- Path canonicalization assumes a single local workstation. If the workflow moves across machines, refs need a workspace-relative or repo-relative layer instead of relying only on absolute resolved paths.
- All writes are local to `data/`.
- Semaphore writes are the only intentional writes into architect project roots. They are limited to `.agent-comms/<agent_id>/new_messages` under the recipient's configured `project_root`, and contain only message ids plus timestamps.

## Human Inspection

Optional CLI commands:

```text
agent-comms agents
agent-comms inbox team-a-architect
agent-comms unread
agent-comms status
agent-comms thread msg_20260517_153012_lab_sensor_01
```

This lets the human operator inspect the relay without entering each architect session.

## File-Based Semaphores

File-based semaphores provide a safe pull-based notification path. They complement tmux wakeups rather than replacing the mailbox.

On message delivery, the relay writes or updates this file under each recipient's configured project root:

```text
<project_root>/.agent-comms/<agent_id>/new_messages
```

Example contents:

```json
{
  "messages": [
    {
      "message_id": "msg_20260517_153012_abcd1234",
      "created_at": "2026-05-17T15:30:12-04:00"
    }
  ],
  "updated_at": "2026-05-17T15:30:12-04:00"
}
```

The semaphore is not authoritative. It only tells the architect to call `list_inbox`. The SQLite mailbox remains the source of truth.

Architect prompt rule:

```text
Before every major task, after long-running commands, and before finishing a session,
check whether .agent-comms/<your-agent-id>/new_messages exists in your project root.
If it exists, call list_inbox, handle relevant messages, then clear the semaphore.
```

Clearing can happen automatically when `list_inbox` returns unread messages for the architect. A manual clear command should also exist for recovery:

```text
agent-comms clear-semaphore team-a-architect
```

Do not ask architects to manually delete arbitrary files. The clear operation should resolve the configured project root and remove only `<project_root>/.agent-comms/<agent_id>/new_messages`.

Benefits:

- Safe pull signal: no text is injected into a live terminal.
- Per-architect path avoids collisions when multiple architects share one project root.
- Works even when tmux wakeup is skipped because the receiver is busy or not idle.
- Survives missed terminal notifications.
- Gives the dashboard a visible "pending local signal" state.

Tradeoff: this still depends on prompt discipline. It is a heartbeat check, not an interrupt.

## Tmux Wakeup Layer

MCP is client-pull. It lets an architect call `list_inbox`, but it does not reliably push an interrupt into an already-running Claude/Codex/Gemini terminal. The wakeup layer fills that gap by binding each architect id to a tmux target and injecting a fixed wakeup prompt into the pane after a message is stored.

This is deliberately a best-effort nudge, not a guaranteed delivery mechanism. The mailbox is still the source of truth:

```text
New agent-comms message msg_20260517_153012_abcd1234 for team-a-architect.
Call read_message(agent_id="team-a-architect", message_id="msg_20260517_153012_abcd1234").
```

The injected text must never include message subject, body, refs, or sender-provided content. It includes only fixed template text plus validated `agent_id` and `message_id`. This keeps dashboard-to-terminal injection from becoming a prompt-injection path.

### Receiver Concept

A tmux target is not enough. It is only an address. The relay also needs a receiver record that describes the live CLI process behind that address and whether it is eligible to receive injected wakeups.

The receiver is the runtime endpoint for an architect:

```text
architect id -> receiver -> tmux target
```

Example:

```text
team-a-architect
  receiver_id: recv_sensor_20260517_01
  transport: tmux
  target: agent-comms:team-a
  cli: claude
  state: idle
  last_heartbeat_at: 2026-05-17T15:30:12-04:00
```

Receiver states:

- `unknown`: registered target exists, but no recent heartbeat/status.
- `idle`: receiver reports it is at or near a prompt and eligible for wakeup.
- `busy`: receiver is working; store message but do not inject.
- `blocked`: receiver is waiting on human/tool approval; store message but do not inject unless forced.
- `closed`: receiver is known stale; wakeup must fail clearly.

Receiver state is advisory, not authoritative. It is a safety hint for whether tmux injection is worth attempting. The system must remain correct when receiver state is stale, missing, or wrong because mailbox delivery and semaphores are the source of truth.

PTY injection is only safe when the receiving CLI is at an input prompt. If the architect is mid-tool-call, at a permission gate, or inside another prompt, injected text may land in the wrong buffer.

Mitigation:

- Prefer waking only when the recipient has an active receiver with state `idle` and a fresh heartbeat/status, for example within 10 minutes.
- If no fresh idle receiver exists, store the message and skip tmux wakeup. The recipient will catch it on the next inbox check.
- Architect prompt must say: if a wakeup line appears mid-task, finish the current step before reading the referenced message.

This makes the failure mode "no wake" instead of "garbled terminal injection."

Receiver state can be updated by any of these mechanisms:

- Architect prompt discipline: `post_status(blocked_on="idle")` before pausing.
- CLI hooks where available: Claude/Codex/Gemini shims call `agent-comms receiver-state <agent-id> --state idle|busy|blocked`.
- Manual operator command from the dashboard or CLI.

Heartbeat is implicit. Any receiver state update refreshes `last_heartbeat_at`; there is no separate heartbeat tool in the first implementation. `post_status(blocked_on="idle")` may also refresh the receiver to `state = idle` if a receiver exists for that architect.

Do not make wakeup correctness depend on hooks existing for every CLI. Hooks improve freshness; inbox polling remains the fallback.

### Receiver Binding Model

Add runtime receiver bindings from architect id to tmux target:

```text
agent-comms receiver bind team-b-architect --tmux-target agent-comms:team-b --cli codex
agent-comms receivers
agent-comms wake team-b-architect --message-id msg_...
```

Store receivers in SQLite so the dashboard and CLI share state:

```sql
receivers(
  agent_id text primary key,
  receiver_id text not null,
  transport text not null,        -- "tmux"
  target text not null,           -- e.g. "agent-comms:team-b"
  cli text,                       -- "claude", "codex", "gemini", or null
  state text not null,            -- unknown, idle, busy, blocked, closed
  last_heartbeat_at text,
  source text not null,           -- "runtime" or "config"
  updated_at text not null
)
```

Optionally allow stable defaults in `config/agents.json`:

```json
{
  "agents": {
    "team-a-architect": {
      "team": "team-a",
      "receiver": {
        "transport": "tmux",
        "target": "agent-comms:team-a",
        "cli": "claude"
      }
    }
  }
}
```

Bootstrap registers config defaults with `source = "config"`. A live `receiver bind` command writes `source = "runtime"` and overrides config. This supports stable tmux layouts while still allowing manual correction when panes move.

If the tmux layout is not stable across reboots, use runtime receivers only and require a rebind step after reboot.

### Wakeup Execution

Before sending keys, validate the target:

```bash
tmux has-session -t "$target"
tmux list-panes -t "$target"
```

Then send text and Enter as separate operations. Prior art reports setups where `tmux send-keys -t "$target" "msg" C-m` drops Enter; the defensive sequence is:

```bash
tmux send-keys -t "$target" "$wakeup_text"
sleep 0.1
tmux send-keys -t "$target" C-m
```

The implementation should use `subprocess.run([...])` with argument arrays, not shell interpolation.

Wake eligibility checks run before target validation:

1. Load receiver for recipient.
2. Require `state = idle`, unless `--force` is used.
3. Require fresh `last_heartbeat_at`, unless `--force` is used.
4. Validate tmux target exists.
5. Deduplicate.
6. Send fixed wakeup text.

Future prompt-detection experiments may use `tmux capture-pane` to inspect terminal state before injection, but this is not part of the initial contract. Prompt detection is shell- and CLI-specific and should not replace receiver state.

### Dedupe

Wakeups must be idempotent for a short window. If the dashboard retries or the operator clicks twice, do not inject duplicate prompts.

Store wake attempts:

```sql
wakeups(
  id text primary key,
  agent_id text not null,
  message_id text not null,
  target text not null,
  status text not null,           -- "sent", "skipped_duplicate", "failed"
  error text,
  created_at text not null
)
```

Before wakeup, check the latest successful wake for `(agent_id, message_id, target)` inside the same transaction used to record the attempt. Skip if it is inside a short dedupe window, for example 120 seconds. Allow an explicit `--force` flag for manual retries.

Do not rely on `primary key(agent_id, message_id, created_at)` for dedupe; it only makes attempts unique, not duplicate-resistant.

### Failure Handling

Wakeup failures must not roll back the mailbox write. The correct behavior is:

1. Store the message.
2. Write/update the recipient's `.agent-comms/<agent_id>/new_messages` semaphore.
3. Attempt wakeup for each bound recipient when receiver state allows.
4. Record wakeup success/failure.
5. Return message ids, semaphore results, and wakeup results to the dashboard or CLI.

If a tmux target is missing, return an actionable error:

```text
No live tmux target for team-a-architect: agent-comms:team-a.
Rebind with: agent-comms receiver bind team-a-architect --tmux-target <target>
```

## Rollout Plan

### Phase 1: Local CLI Mailbox

**Status**: Shipped.

Build the SQLite schema and a simple CLI.

Acceptance criteria:

- Register all four architect identities.
- Send a message from `team-c-architect` to `team-a-architect`.
- List unread messages for `team-a-architect`.
- Acknowledge the message.
- Close a message once handled.
- Support nonblocking inbox polling with `since`.
- Show latest status for all teams.

### Phase 2: MCP Server

**Status**: Shipped — except for per-architect MCP identity (`--agent-id` at launch), which remains designed. Today the architect passes `agent_id` per call.

Expose the same operations as MCP tools.

Acceptance criteria:

- Architect CLI can call `list_inbox`.
- Architect CLI can call `send_message`.
- Each architect uses a per-architect MCP subprocess launched with `--agent-id`.
- MCP caller identity is derived from the subprocess `--agent-id`, not a free `from_agent` argument.
- Tool output is compact enough to paste into an agent context without flooding it.
- Failed validation returns actionable errors.
- `list_agents` and `get_agent_capabilities` support capability discovery without asking the human.

### Phase 3: Architect Prompt Integration

**Status**: Shipped. See `docs/architect-prompt.md`.

Add a short prompt snippet for each team architect.

Acceptance criteria:

- Every architect checks inbox at session start.
- Every architect posts status before ending a substantial task.
- Cross-team findings include at least one file ref or an explicit "no file ref" explanation.
- Every architect checks `.agent-comms/<agent_id>/new_messages` before major tasks, after long-running commands, and before ending a session.
- On wakeup, architects read exactly the referenced message id.
- If a wakeup line appears mid-task, architects finish the current step before reading the referenced message.
- Architects know that a tmux wakeup is a signal to call `read_message`, not a substitute for mailbox state.

### Phase 4: Tmux Wakeup

**Status**: Designed, not implemented.

Add tmux binding and wake commands.

Acceptance criteria:

- `bind` stores a tmux target for an architect.
- `receivers` lists configured receiver targets, CLI type, state, freshness, and source.
- Receiver state updates refresh `last_heartbeat_at`.
- `wake` checks receiver state and freshness before target validation.
- `wake` validates the tmux target before injection.
- `wake` sends text and Enter as two separate `tmux send-keys` calls.
- Wakeup text contains only fixed template text, `agent_id`, and `message_id`.
- `wake` defaults to idle-gated behavior and skips injection without a fresh idle receiver unless `--force` is used.
- Repeated wakeups for the same `(agent_id, message_id)` are deduped inside the configured window.
- Missing tmux targets produce clear errors and do not affect stored messages.
- Message delivery writes a per-architect file semaphore even when tmux wakeup is skipped.
- Existing databases migrate cleanly to add `receivers` and `wakeups` tables.
- A smoke test runs all four per-architect MCP subprocesses concurrently against the same SQLite database.

### Phase 5: Dashboard

**Status**: Designed, not implemented.

Add a local Starlette dashboard over the same SQLite mailbox. Starlette is already present through the `mcp[cli]` dependency tree, so using it avoids adding another web framework.

Acceptance criteria:

- Shows unread, requires-ack, blocker/high, and recently closed messages.
- Highlights unacked `blocker` and `high` messages aggressively.
- Flags `blocker` messages that remain unacked beyond the configured window as communication contract violations.
- Hides or de-emphasizes expired messages by default while preserving audit history.
- Shows latest status by architect.
- Sends direct messages and broadcasts.
- Sends as `from_type = "operator"`, `from_id = "human-operator"`.
- `POST /messages` validates recipients, canonicalizes refs, writes SQLite state, writes semaphores, applies `wake_policy`, and returns per-recipient delivery/wakeup status.
- Allows the human to force-ack or force-close a stuck message with an audit event.
- On send, attempts tmux wakeup for each recipient with a binding.
- Shows wakeup result per recipient.
- Shows whether each recipient has a pending `.agent-comms/<agent_id>/new_messages` semaphore.
- Shows communication contract warnings: over-budget threads, no-evidence messages, broadcast reply storms, stale semaphores, and repeated wakeups.
- Can trigger a human-visible notification for `high` or `blocker` messages, such as macOS notification, terminal bell, or dashboard sound.

### Phase 6: Two-Week Operational Test

**Status**: Pending Phases 4 and 5.

Run the system in live coordination for two weeks before adding A2A compatibility or richer workflow features.

Measure:

- Messages per day.
- Ack latency by priority.
- Ignored or expired unread messages.
- False-positive wakeups.
- Communication loops and over-budget threads.
- Inbox size growth.
- Human force-ack/force-close events.
- Semaphore usefulness: messages noticed through semaphore vs tmux wake vs normal inbox check.
- `database is locked` occurrences.

Success criteria:

- Human copy/paste coordination decreases.
- Message volume stays low.
- Most messages meet the communication contract send criteria.
- No recurring wakeup corruption.
- No SQLite lock instability.
- Dashboard messaging uses the same mailbox path as architect messaging.

## Implementation Estimate

The risky part is not protocol. The risks are operational discipline, SQLite contention, and over-modeling human workflow. The relay only works if the communication contract keeps message volume low.

## Open Questions

- Should messages be append-only forever, or should there be a local retention policy?
- Should acknowledgements be mandatory for `high` and `blocker` messages?
- What ack SLA should trigger dashboard contract violations for `blocker` and `high` messages?
- Is the tmux layout stable enough to store default receivers in `config/agents.json`, or should all receiver bindings be runtime-only after each reboot?
- Should dashboard send automatically wake all recipients, or should wakeup be opt-in per message?
- What dedupe window is right for live operation: 60, 120, or 300 seconds?
- Should blocker-priority messages trigger a human-visible notification such as a macOS notification, terminal bell, or dashboard sound?
