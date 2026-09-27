"""``agent-comms demo``: one dispatch to a fake worker, start to finish.

The demo runs right after ``agent-comms setup`` with no model login. It
dispatches a fixed task from a team's architect to that team's fake worker,
exactly as the architect's own ``dispatch_agent`` MCP tool does (which
starts the worker at once), then watches that one row until it is terminal,
and reports the dispatch, the worker's reply and the final status.

Only a worker whose registered runtime is ``fake`` is ever a target. The
target is resolved from the ledger and checked before anything is written,
there is no way to name another actor, and the adapter factory handed to
the dispatch refuses every other runtime. The demo never runs a ledger-wide
reconcile pass (that is the monitor's job; a pass would also start other
queued work, native workers included), so it reads only its own row and
reaps only the worker wrapper it spawned. It writes no monitor heartbeat.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from . import paths, supervisor
from .adapters.registry import adapter_for
from .dispatch_ledger import WORKER_DISPATCH_POLICY, is_dispatch_terminal
from .doctor import LedgerRef
from .schema import ValidationError
from .store import Store

DEMO_SUBJECT = "demo: ping"
DEMO_BODY = "Reply with PONG."
DEFAULT_TIMEOUT_SECONDS = 60
POLL_SECONDS = 0.5
REAP_SECONDS = 10


class DemoFailed(Exception):
    """The dispatch did not end closed/satisfied; ``payload`` is the report."""

    def __init__(self, payload: dict) -> None:
        super().__init__(payload.get("error", "demo failed"))
        self.payload = payload


def _registry_teams() -> set[str]:
    try:
        document = json.loads(paths.actors_config_path().read_text())
    except (OSError, ValueError):
        return set()
    actors = document.get("actors") if isinstance(document, dict) else None
    if not isinstance(actors, dict):
        return set()
    return {entry["team"] for entry in actors.values() if isinstance(entry, dict) and isinstance(entry.get("team"), str)}


def _free_team(taken: set[str]) -> str:
    name, n = "demo", 1
    while name in taken:
        n += 1
        name = f"demo-{n}"
    return name


def setup_fix(actors: list[dict], ledger: LedgerRef) -> str:
    """The setup command that adds a fake-only team this demo can use."""
    taken = _registry_teams() | {actor["team"] for actor in actors if actor.get("team")}
    roots = sorted({
        actor["project_root"] for actor in actors
        if actor.get("kind") == "agent" and actor.get("role") == "architect" and actor.get("project_root")
    })
    root = roots[0] if len(roots) == 1 and Path(roots[0]).is_dir() else "<project-root>"
    return ledger.cli(
        "setup", "--project-root", root, "--team", _free_team(taken),
        "--runtimes", "fake", "--clients", "none", "--yes",
    )


def resolve_target(actors: list[dict], team: str | None, ledger: LedgerRef) -> dict:
    """The one fake worker the demo may dispatch to, or a refusal with its fix."""
    fakes = [
        actor for actor in actors
        if actor.get("kind") == "agent" and actor.get("role") == "worker" and actor.get("runtime") == "fake"
    ]
    if team is not None:
        in_team = [actor for actor in fakes if actor.get("team") == team]
        if len(in_team) == 1:
            return in_team[0]
        if not in_team:
            raise ValidationError(
                f"team {team!r} has no fake worker; the demo only dispatches to a fake worker. "
                f"Add a fake-only team: {setup_fix(actors, ledger)}"
            )
        raise ValidationError(
            f"team {team!r} has {len(in_team)} fake workers ({', '.join(a['id'] for a in in_team)}); "
            "keep one in the registry and rerun agent-comms bootstrap"
        )
    if len(fakes) == 1:
        return fakes[0]
    if not fakes:
        raise ValidationError(
            "no fake worker is registered; the demo only dispatches to a fake worker. "
            f"Add a fake-only team: {setup_fix(actors, ledger)}"
        )
    fixes = "; ".join(ledger.cli("demo", "--team", t) for t in sorted({a.get("team") or "" for a in fakes}))
    raise ValidationError(f"{len(fakes)} fake workers are registered; pick a team: {fixes}")


def _row(store: Store, dispatch_id: str) -> dict:
    with store.connection() as conn:
        row = conn.execute(
            "select status, result, failure_reason from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
        ).fetchone()
    return dict(row) if row is not None else {"status": "missing", "result": None, "failure_reason": None}


def _reply(store: Store, message_id: str, worker: str) -> dict | None:
    with store.connection() as conn:
        row = conn.execute(
            "select m.id, m.from_agent, m.subject, m.body from messages m "
            "join message_threads t on t.message_id = m.id "
            "where t.parent_message_id = ? and m.from_agent = ? order by m.created_at, m.id limit 1",
            (message_id, worker),
        ).fetchone()
    if row is None:
        return None
    return {"message_id": row["id"], "from": row["from_agent"], "subject": row["subject"],
            "body": row["body"], "parent_message_id": message_id}


def _reap(clock: Callable[[], float], sleep: Callable[[float], None]) -> bool:
    """Reap this process's exited worker wrappers before the command exits.

    The background reaper thread is a daemon, so a CLI that returns at once
    would leave the wrapper's exit proof unrecorded. Bounded; ``False`` when a
    wrapper is still registered at the deadline.
    """
    registry = supervisor.reaper_registry()
    deadline = clock() + REAP_SECONDS
    while True:
        registry.reap_ready()
        if registry.pending() == 0:
            return True
        if clock() >= deadline:
            return False
        sleep(0.1)


def _fake_only(runtime: str):
    """The adapter factory for the demo's dispatch: fake, or nothing."""
    if runtime != "fake":
        raise ValidationError(f"the demo only dispatches to a fake worker, not {runtime}")
    return adapter_for(runtime)


def _operator(store: Store) -> str | None:
    # For the monitor fix only; doctor prints a placeholder the same way.
    try:
        return store._actors.resolve_operator_human()
    except ValidationError:
        return None


def run_demo(
    store: Store,
    *,
    db_path: Path,
    db_explicit: bool,
    team: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    progress: TextIO | None = None,
) -> dict:
    """Dispatch to the fake worker and wait for its row to be terminal."""
    if timeout_seconds < 1:
        raise ValidationError("--timeout must be at least 1 second")
    out = progress if progress is not None else sys.stderr
    ledger = LedgerRef(db_path, db_explicit)
    target = resolve_target(store.list_actors(), team, ledger)
    architect = target.get("owner_actor_id")
    if not architect:
        raise ValidationError(f"fake worker {target['id']} has no owning architect; rerun agent-comms bootstrap")
    key = f"demo-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    dispatched = store.dispatch_agent(
        producer_actor_id=architect,
        target_actor_id=target["id"],
        idempotency_key=key,
        subject=DEMO_SUBJECT,
        body=DEMO_BODY,
        refs=[],
        requested_policy=WORKER_DISPATCH_POLICY,
        adapter_for_runtime=_fake_only,
    )
    dispatch_id = dispatched["dispatch_id"]
    message_id = dispatched.get("message_id") or dispatched["message"]["id"]
    print(f"dispatched {dispatch_id}: {architect} -> {target['id']}, {DEMO_SUBJECT!r}", file=out, flush=True)

    # The fake worker replies and closes its own row. Nothing here settles
    # a worker that dies or a row left queued: that needs the monitor.
    row = _row(store, dispatch_id)
    deadline = clock() + timeout_seconds
    while not is_dispatch_terminal(row["status"]) and clock() < deadline:
        sleep(POLL_SECONDS)
        row = _row(store, dispatch_id)

    terminal = is_dispatch_terminal(row["status"])
    reaped = _reap(clock, sleep) if terminal else False
    reply = _reply(store, message_id, target["id"])
    if reply is not None:
        print(f"worker replied {reply['message_id']}: {reply['body']!r}", file=out, flush=True)
    report = {
        "ok": row["status"] == "closed" and row["result"] == "satisfied" and reply is not None,
        "dispatch": {
            "dispatch_id": dispatch_id, "message_id": message_id, "from": architect,
            "to": target["id"], "team": target.get("team"), "subject": DEMO_SUBJECT, "body": DEMO_BODY,
        },
        "reply": reply,
        "final": row,
        "worker_log": str(paths.dispatch_log_dir()),
        "worker_reaped": reaped,
    }
    if report["ok"]:
        print(f"dispatch closed: {row['result']}", file=out, flush=True)
        return report
    report["error"] = (
        f"dispatch {dispatch_id} still {row['status']} after {timeout_seconds:g}s" if not terminal
        else f"dispatch {dispatch_id} ended {row['status']}/{row['result']}"
        + ("" if reply is not None else " with no reply from the worker")
    )
    report["fixes"] = [
        # A row still queued or in flight is the monitor's to start or settle.
        *([ledger.monitor(_operator(store) or "<human-actor-id>")] if not terminal else []),
        ledger.cli("dispatch-status"),
        ledger.cli("doctor"),
        f"worker logs: {report['worker_log']}",
    ]
    print(f"demo failed: {report['error']}", file=out, flush=True)
    raise DemoFailed(report)


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "DemoFailed", "resolve_target", "run_demo", "setup_fix"]
