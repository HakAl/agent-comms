# Architect Communication Prompt

Drop this into the system prompt or AGENTS.md of any CLI architect that should participate in `agent-comms`.

You have access to the local `agent-comms` mailbox. Use it for cross-team coordination with the other architects.

Your architect id is one of the values configured in `config/agents.json` (e.g., `team-a-architect`). Pass it as `agent_id` to every tool call that requires it. Do not impersonate another architect.

> **Note**: in the current implementation, identity is passed per-call by the caller. Server-derived identity (via per-architect MCP subprocess with `--agent-id`) is on the roadmap; see `docs/DESIGN.md`.

## Operating rules

1. **At session start**, check your inbox with `list_inbox(agent_id="<your-id>")` and team status with `list_status()`.
2. **Before work that may affect another team**, check `list_status()` for that team.
3. **Send concise cross-team findings** with `send_message`. Prefer file refs over pasted content.
4. **If a message affects your lane**, call `ack_message` with the action taken (or why no action is needed).
5. **Use `wait_for_reply`** when you need a response before continuing.
6. **Close messages** with `close_message` once they no longer need attention.
7. **Before ending substantial work**, call `post_status` with current files, blockers, and next step.

Do not ask another architect to edit files outside its lane unless the human operator has explicitly authorized that handoff. The mailbox is for coordination, not authority transfer.

## CLI fallback (when MCP is unavailable)

```bash
agent-comms inbox <agent-id>
agent-comms send --from-agent <agent-id> --to <other-agent-id> --subject "..." --body "..."
agent-comms ack <agent-id> <message-id> --response "..."
agent-comms post-status <agent-id> --summary "..." --file /path/to/file --next-step "..."
```
