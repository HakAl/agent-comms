from __future__ import annotations

from pathlib import Path
from typing import Any

from .cli import DEFAULT_DB
from .store import Store

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


def create_server(db_path: str = str(DEFAULT_DB)) -> Any:
    fast_mcp = require_mcp()
    mcp = fast_mcp("agent-comms")
    store = Store(Path(db_path))

    @mcp.tool()
    def register_agent(
        agent_id: str,
        team: str,
        role: str,
        project_root: str,
        capabilities: list[str],
    ) -> dict:
        """Register or refresh an architect identity."""
        return store.register_agent(agent_id, team, role, project_root, capabilities)

    @mcp.tool()
    def list_agents() -> list[dict]:
        """List known architect identities."""
        return store.list_agents()

    @mcp.tool()
    def send_message(
        from_agent: str,
        to_agents: list[str],
        subject: str,
        body: str,
        refs: list[dict] | None = None,
        priority: str = "normal",
        requires_ack: bool = False,
        parent_message_id: str | None = None,
    ) -> dict:
        """Send a compact message with optional file references."""
        return store.send_message(
            from_agent=from_agent,
            to_agents=to_agents,
            subject=subject,
            body=body,
            refs=refs or [],
            priority=priority,
            requires_ack=requires_ack,
            parent_message_id=parent_message_id,
        )

    @mcp.tool()
    def list_inbox(
        agent_id: str,
        unread_only: bool = True,
        include_closed: bool = False,
        limit: int = 20,
    ) -> list[dict]:
        """List messages addressed to an architect."""
        return store.list_inbox(agent_id, unread_only=unread_only, include_closed=include_closed, limit=limit)

    @mcp.tool()
    def read_message(agent_id: str, message_id: str) -> dict:
        """Read a message and mark it read for the architect."""
        return store.read_message(agent_id, message_id)

    @mcp.tool()
    def ack_message(agent_id: str, message_id: str, response: str = "") -> dict:
        """Acknowledge a message with an optional short response."""
        return store.ack_message(agent_id, message_id, response)

    @mcp.tool()
    def close_message(agent_id: str, message_id: str, response: str = "") -> dict:
        """Close a recipient's copy of a message, marking it read if needed."""
        return store.close_message(agent_id, message_id, response)

    @mcp.tool()
    def wait_for_reply(
        agent_id: str,
        after_message_id: str | None = None,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 1.0,
    ) -> dict:
        """Wait for a new unread message or reply until timeout."""
        return store.wait_for_reply(agent_id, after_message_id, timeout_seconds, poll_interval_seconds)

    @mcp.tool()
    def post_status(
        agent_id: str,
        summary: str,
        current_files: list[str] | None = None,
        blocked_on: str = "",
        next_step: str = "",
    ) -> dict:
        """Publish the architect's current status."""
        return store.post_status(agent_id, summary, current_files or [], blocked_on, next_step)

    @mcp.tool()
    def list_status() -> list[dict]:
        """List the latest status posted by each architect."""
        return store.list_status()

    return mcp


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
