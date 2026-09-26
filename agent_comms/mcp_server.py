from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path
from typing import Any

from .adapters.registry import adapter_for
from . import paths
from .policies import active_policy_from_env
from .schema import ValidationError
from .store import Store, WORKER_DISPATCH_POLICY

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - exercised manually when MCP is absent.
    FastMCP = None  # type: ignore[assignment]


def require_mcp() -> Any:
    if FastMCP is None:
        raise SystemExit(
            "The MCP Python package is not installed. Install it in this environment "
            "before running the stdio server, or use `python3 -m agent_comms.cli` as the fallback."
        )
    return FastMCP


MISSING_ACTOR_ID_MESSAGE = (
    "agent-comms MCP is identity-scoped per S0 and must be launched with "
    "--actor-id <registered-actor>; see docs/mcp-setup.md"
)


def create_server(db_path: str | None = None, actor_id: str | None = None, *, db_explicit: bool | None = None) -> Any:
    raw_actor_id = actor_id or ""
    if not raw_actor_id.strip():
        raise ValidationError(MISSING_ACTOR_ID_MESSAGE)
    actor_id = raw_actor_id
    if db_explicit is None:
        db_explicit = db_path is not None or bool(os.environ.get("AGENT_COMMS_DB"))
    resolved_db_path = Path(db_path) if db_path is not None else paths.db_path()

    fast_mcp = require_mcp()
    mcp = fast_mcp("agent-comms")
    store = Store(resolved_db_path, is_default_db_open=not db_explicit)
    active_policy = active_policy_from_env(os.environ)
    store.require_launchable_actor(actor_id)
    scoped_actor_id = actor_id

    def tool_allowed(name: str) -> bool:
        return active_policy is None or name in active_policy.mcp_allowed_tools

    if tool_allowed("whoami"):
        @mcp.tool()
        def whoami() -> dict:
            """Return the identity bound to this scoped MCP process."""
            return store.whoami(scoped_actor_id)

    if tool_allowed("list_agents"):
        @mcp.tool()
        def list_agents() -> list[dict]:
            """List known agent identities."""
            return store.list_agents()

    if tool_allowed("list_actors"):
        @mcp.tool()
        def list_actors() -> list[dict]:
            """List known actor identities."""
            return store.list_actors()

    if tool_allowed("list_status"):
        @mcp.tool()
        def list_status() -> list[dict]:
            """List the latest status posted by each actor."""
            return store.list_status()

    if tool_allowed("send_message"):
        @mcp.tool()
        def send_message(
            to_agents: list[str],
            subject: str,
            body: str | None = None,
            refs: list[dict] | None = None,
            priority: str = "normal",
            requires_ack: bool = False,
            parent_message_id: str | None = None,
            body_file: str | None = None,
        ) -> dict:
            """Send a compact message with optional file references."""
            if active_policy and active_policy.send_message_mode == "reply_only" and not parent_message_id:
                raise ValidationError("worker policy permits send_message only as a reply with parent_message_id")
            return store.send_message(
                from_agent=actor_id,
                to_agents=to_agents,
                subject=subject,
                body=body,
                refs=refs or [],
                priority=priority,
                requires_ack=requires_ack,
                parent_message_id=parent_message_id,
                body_file=body_file,
            )

    if tool_allowed("dispatch_agent") and (active_policy is None or "dispatch_agent" not in active_policy.mcp_denied_tools):
        @mcp.tool()
        def dispatch_agent(
            target_actor_id: str,
            idempotency_key: str,
            subject: str,
            body: str | None = None,
            refs: list[dict] | None = None,
            body_file: str | None = None,
            payload_origin: str | None = None,
        ) -> dict:
            """Dispatch an own-team worker under the v1 bounded worker policy.

            Supply exactly one logical body: inline ``body`` (bounded) or a
            repository-relative ``body_file`` resolved under this producer's
            registered project root, with a required ``payload_origin`` of
            authored_brief, generated_artifact, or verbatim_source. The
            source root comes only from the authenticated producer
            registration, never from request JSON or the process CWD.
            """
            return store.dispatch_agent(
                producer_actor_id=actor_id,
                target_actor_id=target_actor_id,
                idempotency_key=idempotency_key,
                subject=subject,
                body=body,
                refs=refs or [],
                body_file=body_file,
                payload_origin=payload_origin,
                requested_policy=WORKER_DISPATCH_POLICY,
                adapter_for_runtime=adapter_for,
            )

    if tool_allowed("cancel_dispatch") and (active_policy is None or "cancel_dispatch" not in active_policy.mcp_denied_tools):
        @mcp.tool()
        def cancel_dispatch(dispatch_id: str, reason: str) -> dict:
            """Cancel one of the current actor's own dispatches; drives authenticated termination of reachable in-flight work.

            The requesting identity and producer authority come exclusively from the
            scoped ``--actor-id`` process, never from the request, so this tool has
            no caller-supplied actor/identity/authority/admin/credential field.
            """
            return store.request_cancellation(
                dispatch_id,
                requesting_actor_id=actor_id,
                reason=reason,
                authority="producer",
                adapter_for_runtime=adapter_for,
            )

    if tool_allowed("list_inbox"):
        @mcp.tool()
        def list_inbox(
            unread_only: bool = True,
            include_closed: bool = False,
            limit: int = 20,
        ) -> list[dict]:
            """List messages addressed to the current actor with snippets and body_chars."""
            return store.list_inbox(actor_id, unread_only=unread_only, include_closed=include_closed, limit=limit)

    if tool_allowed("read_message"):
        @mcp.tool()
        def read_message(message_id: str) -> dict:
            """Read a message and mark it read for the current actor; returns the full body."""
            return store.read_message(actor_id, message_id)

    if tool_allowed("ack_message"):
        @mcp.tool()
        def ack_message(message_id: str, response: str = "") -> dict:
            """Acknowledge a message with an optional short response."""
            return store.ack_message(actor_id, message_id, response)

    if tool_allowed("close_message"):
        @mcp.tool()
        def close_message(message_id: str, response: str = "") -> dict:
            """Close a recipient's copy of a message, marking it read if needed."""
            return store.close_message(actor_id, message_id, response)

    if tool_allowed("close_dispatch"):
        @mcp.tool()
        def close_dispatch(
            message_id: str,
            result: str,
            reply_message_id: str,
            summary: str,
            artifacts: list[dict] | None = None,
            delta: bool = False,
            blocked_reason: str = "",
        ) -> dict:
            """Atomically settle a v2 dispatch from verified worker evidence."""
            return store.close_dispatch(
                actor_id,
                message_id=message_id,
                result=result,
                reply_message_id=reply_message_id,
                summary=summary,
                artifacts=artifacts,
                delta=delta,
                blocked_reason=blocked_reason,
            )

    if tool_allowed("wait_for_reply"):
        @mcp.tool()
        def wait_for_reply(
            after_message_id: str | None = None,
            timeout_seconds: float = 30.0,
            poll_interval_seconds: float = 1.0,
            full: bool = False,
        ) -> dict:
            """Wait for unread messages with snippets; full=True inlines full bodies."""
            return store.wait_for_reply(
                actor_id,
                after_message_id=after_message_id,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                full=full,
            )

    if tool_allowed("post_status"):
        @mcp.tool()
        def post_status(
            summary: str,
            current_files: list[str] | None = None,
            blocked_on: str = "",
            next_step: str = "",
            dispatch_id: str | None = None,
            thread_ref: str | None = None,
        ) -> dict:
            """Publish the current actor's status."""
            return store.post_status(actor_id, summary, current_files or [], blocked_on, next_step, dispatch_id, thread_ref)

    if tool_allowed("post_handoff"):
        @mcp.tool()
        def post_handoff(body: str, refs: list[dict] | None = None) -> dict:
            """Post a durable handoff snapshot for the current actor."""
            return store.post_handoff(
                actor_id,
                body,
                refs or [],
                created_by_actor_id=actor_id,
            )

    if tool_allowed("read_handoff"):
        @mcp.tool()
        def read_handoff(actor_id: str | None = None) -> dict | None:
            """Read the latest handoff snapshot for the current actor or an explicitly named actor."""
            target_actor_id = actor_id or scoped_actor_id
            return store.read_handoff(target_actor_id)

    return mcp


MISSING_MCP_MESSAGE = (
    f"agent-comms-mcp: {sys.executable} cannot import mcp; recover with: uv sync "
    "(a checkout) or reinstall the package"
)
STARTUP_REPORT_FALLBACK = "agent-comms startup: release_info=unknown"


def preflight_mcp_import() -> None:
    """Refuse to start when the ``mcp`` package is absent from this interpreter.

    ``mcp`` is a dependency, so this only fails for a broken environment; the
    message names the interpreter and the recovery instead of letting the
    server die on the first tool registration.
    """
    try:
        importlib.import_module("mcp")
    except ImportError:
        print(MISSING_MCP_MESSAGE, file=sys.stderr)
        raise SystemExit(1)


def print_startup_report() -> None:
    """One stderr line an MCP client's log can be matched against a release.

    A failure to compute the report is not a reason to refuse service, so it
    degrades to a fixed line rather than raising.
    """
    try:
        from .release import startup_report

        line = startup_report()
    except Exception:
        line = STARTUP_REPORT_FALLBACK
    print(line, file=sys.stderr)


def main() -> None:
    """Console-script entry point for ``agent-comms-mcp``.

    Preflights the environment, reports the release to stderr, then serves
    stdio MCP for the bound actor.
    """
    preflight_mcp_import()

    class ActionableArgumentParser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            if "--actor-id" in message:
                message = f"{message}. {MISSING_ACTOR_ID_MESSAGE}"
            super().error(message)

    parser = ActionableArgumentParser(prog="agent-comms-mcp")
    parser.add_argument("--db", default=None, help="SQLite database path")
    parser.add_argument("--actor-id", required=True, help="Bound actor identity for this MCP server")
    args = parser.parse_args()
    print_startup_report()
    try:
        server = create_server(
            db_path=args.db,
            actor_id=args.actor_id,
            db_explicit=args.db is not None or bool(os.environ.get("AGENT_COMMS_DB")),
        )
    except ValidationError as exc:
        # An unregistered or unlaunchable actor is an operator error, reported
        # as one line rather than a traceback in the MCP client's log.
        print(f"agent-comms-mcp: {exc}", file=sys.stderr)
        raise SystemExit(1)
    server.run()


if __name__ == "__main__":
    main()
