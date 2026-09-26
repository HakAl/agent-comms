from __future__ import annotations

import json
import os
import re
import sys
import traceback
from pathlib import Path

from agent_comms import paths
from agent_comms.handoff import session_start_text_from_env
from agent_comms.store import Store


# The fixed-rules split moved the large rules corpus out of the working handoff.
HANDOFF_LINE_BUDGET = 300
HANDOFF_WRAP_BUDGET = 2

STATIC_CONTEXT = (
    "You are the architect for this project and you coordinate its workers through "
    "agent-comms. Before acting, read AGENTS.md and local/ARCH-HANDOFF.md (the lean "
    "working brief; older sessions are in local/ARCH-HANDOFF-archive.md). Delegate "
    "work and reviews via dispatch_agent (cross-family dispatch through the ledger "
    "is the review mechanism), not a generic subagent; use Agent/Explore only for "
    "your own read-only research, never to review your own work product."
)
FALLBACK_CONTEXT = (
    "You are the architect for this project. Read AGENTS.md and "
    "local/ARCH-HANDOFF.md before acting."
)


def _project_root() -> Path:
    # Claude Code names the project directory; without it the hook runs from
    # that directory as its cwd. The package location is never the project.
    configured = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(configured) if configured else Path.cwd()


def _brief_metrics(brief: Path) -> tuple[int, int]:
    try:
        text = brief.read_text()
    except (OSError, UnicodeError):
        return 0, 0
    lines = text.count("\n")
    wraps = len(
        re.findall(
            r"^## SESSION \d{4}-\d{2}-\d{2} \(g\d+\):",
            text,
            re.MULTILINE,
        )
    )
    return lines, wraps


def _board_text() -> str:
    environment = os.environ
    if not (
        environment.get("AGENT_COMMS_LAUNCH_KIND") == "architect_interactive"
        and environment.get("AGENT_COMMS_ACTOR_ID", "").strip()
        and environment.get("AGENT_COMMS_INSTALL_ROOT", "").strip()
    ):
        return ""
    explicit_db = bool(environment.get("AGENT_COMMS_DB"))
    store = Store(paths.db_path(), is_default_db_open=not explicit_db)
    return session_start_text_from_env(store)


def build_context() -> str:
    brief = _project_root() / "local" / "ARCH-HANDOFF.md"
    lines, wraps = _brief_metrics(brief)
    context = STATIC_CONTEXT
    if lines > HANDOFF_LINE_BUDGET or wraps > HANDOFF_WRAP_BUDGET:
        context += (
            f" WARNING: the working brief is {lines} lines / {wraps} session wraps, "
            f"over budget (<={HANDOFF_LINE_BUDGET} lines, <={HANDOFF_WRAP_BUDGET} wraps). "
            "Prune the oldest wrap(s) verbatim to local/ARCH-HANDOFF-archive.md before "
            "proceeding."
        )

    try:
        handoff = _board_text()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        handoff = ""
    if handoff:
        if brief.is_file():
            handoff = (
                "PRECEDENCE: the handoffs-board text below is the current handoff; "
                "local/ARCH-HANDOFF.md is a transitional archive, do not read it as state.\n"
                + handoff
            )
        context += "\n\n" + handoff
    return context


def _contract(context: str) -> dict[str, dict[str, str]]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def main() -> None:
    try:
        output = _contract(build_context())
    except Exception:
        traceback.print_exc(file=sys.stderr)
        output = _contract(FALLBACK_CONTEXT)
    print(json.dumps(output, separators=(",", ":")))


if __name__ == "__main__":
    main()
