# Agent Communication Contract

This is a non-binding operating contract for `agent-comms`. It exists to reduce coordination load without creating message loops, incentive drift, or context bloat.

The mailbox is for durable handoffs and externally useful coordination. It is not for conversation, self-review, status chatter, or debate for its own sake.

## North Stars

1. External signals beat opinions.
2. Messages must reduce future coordination work.
3. Every message needs a concrete recipient action or a clear "FYI, no action required" label.
4. Replies should terminate threads, not extend them.
5. The default is not to message.
6. Context stays in files; messages carry pointers and short deltas.

## Send Criteria

Send a message only when at least one criterion is true:

- `handoff`: another architect must take or avoid a concrete action.
- `blocker`: your lane is blocked on another lane.
- `assumption_change`: a prior cross-team assumption changed.
- `external_signal`: a command, test, eval, schema check, or artifact produced a result another lane needs.
- `contract_change`: a shared interface, schema, path, command, dataset definition, or acceptance criterion changed.
- `human_requested`: the human explicitly asked you to coordinate.

Do not send a message for:

- Generic progress updates with no recipient action.
- "Looks good" validation.
- Self-review or requests for self-review.
- Mirroring a message back to the sender unless you are acknowledging with a concrete action.
- Asking another architect to think about a topic without a bounded question.
- Large pasted context that already exists in a file.

## Message Shape

Every message should fit this shape:

```text
Kind: handoff | blocker | assumption_change | external_signal | contract_change | human_requested | fyi
Action: exact requested action, or "none"
Why now: one sentence
Evidence: command/output/file ref, or "none"
Refs: paths only, with short summaries
Ack: required | optional | not_needed
Stop condition: when this thread should end
```

Hard limits:

- Body target: 5 lines.
- Body maximum: 12 lines.
- File refs maximum: 5.
- Do not paste logs unless the log excerpt is the artifact and is under 20 lines.
- Do not include message subject/body in tmux wakeups.

## Reply Rules

Reply only when the reply changes state:

- You accepted the handoff and state the next action.
- You rejected or cannot act, with a reason.
- You need one bounded clarification.
- You provide the requested external signal.
- You close the loop.

Do not reply with:

- Thanks-only messages.
- Agreement without action.
- Restating the sender's content.
- Speculation.
- New unrelated work.

One clarification round is the default. If a thread needs a second clarification, escalate to the human or convert the issue into a shared file/task with explicit criteria.

## Loop Prevention

Each message has a thread budget:

- `fyi`: zero replies expected.
- `handoff`: one ack plus one close.
- `blocker`: one clarification round maximum before human escalation.
- `external_signal`: one result reply, then close.
- `contract_change`: one ack per affected architect, then close.

Agents must not broadcast replies to broadcasts unless explicitly requested. A broadcast ack should go only to the sender, and only if `requires_ack` is true.

Agents must not create a new thread to continue a closed thread unless a new external signal exists.

## Verification Standard

Claims should be grounded in executable or observable evidence whenever possible.

Preferred evidence:

- Test/build/lint command and result.
- Eval command and metric path.
- JSON/schema validation output.
- Git diff/stat path.
- File path plus section summary.
- Before/after delta.

Weak evidence:

- "I reviewed."
- "Looks right."
- "Seems consistent."
- "Probably."

Weak evidence should not trigger cross-team work unless the human requested it.

## Context Hygiene

Messages should point to durable artifacts, not carry them.

Use:

```text
refs: [{path: "...", summary: "what to inspect"}]
```

Avoid:

- Full markdown reports in message bodies.
- Long command output.
- Repeated background already present in prior messages.
- Quoting entire prior messages.

When replying, reference the original `message_id` and only describe the delta.

## Semaphore And Wakeup Behavior

`.agent-comms/<agent_id>/new_messages` is a pull signal, not content. On seeing it:

1. Call `list_inbox`.
2. Read only messages addressed to your architect id.
3. Act only on messages that meet the send criteria.
4. Let `list_inbox` clear the semaphore automatically, or clear it through `agent-comms clear-semaphore` if needed.

Tmux wakeups are nudges. If a wakeup appears mid-task, finish the current step before reading the referenced message.

## Human Escalation

Escalate to the human instead of continuing agent-to-agent messaging when:

- A thread exceeds its budget.
- Two architects disagree about ownership.
- A message requests cross-lane file edits.
- A blocker affects release/eval criteria.
- The needed evidence is subjective rather than executable.
- The next message would mostly restate prior context.

## Dashboard Contract Checks

The dashboard should make protocol drift visible:

- Threads over budget.
- Messages requiring ack but not acknowledged.
- Messages with no refs and no evidence.
- Broadcasts with many replies.
- Reopened closed threads.
- Repeated wakeups for the same message.
- Stale semaphores.
- Unacked `high` or `blocker` messages past the configured window.

These checks should warn, not block. The contract is non-binding, but violations should be visible.
