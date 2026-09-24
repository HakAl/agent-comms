from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import json

from .actors import ActorRegistry
from .clock import utc_now
from .db import Database
from .dispatch_ledger import (
    DispatchLedger,
    WORKER_DISPATCH_POLICY,  # noqa: F401 - compatibility re-export
    WORKER_DISPATCH_TTL_SECONDS,  # noqa: F401 - compatibility re-export
)
from .handoff import HandoffBoard
from .mailbox import Mailbox
from .status import StatusBoard


class Store:
    def __init__(self, db_path: Path, *, is_default_db_open: bool = False) -> None:
        self._db = Database(db_path, is_default_db_open=is_default_db_open)
        self._actors = ActorRegistry(self._db)
        self._mailbox = Mailbox(self._db, self._actors)
        self._status = StatusBoard(self._db, self._actors)
        self._dispatch = DispatchLedger(self._db, self._actors, self._mailbox)
        self._handoff = HandoffBoard(self._db, self._actors)

    @property
    def db_path(self) -> Path:
        return self._db.db_path

    def connect(self) -> sqlite3.Connection:
        return self._db.connect()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self._db.connection() as conn:
            yield conn

    def init(self) -> None:
        self._db.init()

    def register_agent(
        self,
        agent_id: str,
        team: str,
        role: str,
        project_root: str,
        capabilities: list[str],
        *,
        owner: str | None = None,
    ) -> dict:
        return self._actors.register_agent(
            agent_id, team, role, project_root, capabilities, owner=owner
        )

    def register_actor(
        self,
        actor_id: str,
        kind: str,
        display_name: str,
        *,
        team: str | None = None,
        role: str | None = None,
        project_root: str | None = None,
        runtime: str | None = None,
        spawn: dict | None = None,
        capabilities: list[str] | None = None,
        system_class: str | None = None,
        system_instance: str | None = None,
        dispatch_cap: int | None = None,
        protected: bool | None = None,
        owner: str | None = None,
    ) -> dict:
        return self._actors.register_actor(
            actor_id,
            kind,
            display_name,
            team=team,
            role=role,
            project_root=project_root,
            runtime=runtime,
            spawn=spawn,
            capabilities=capabilities,
            system_class=system_class,
            system_instance=system_instance,
            dispatch_cap=dispatch_cap,
            protected=protected,
            owner=owner,
        )

    def register_agent_actor(
        self,
        agent_id: str,
        team: str,
        role: str,
        project_root: str,
        capabilities: list[str],
        *,
        display_name: str | None = None,
        runtime: str | None = None,
        spawn: dict | None = None,
        protected: bool | None = None,
        owner: str | None = None,
    ) -> dict:
        return self._actors.register_agent_actor(
            agent_id,
            team,
            role,
            project_root,
            capabilities,
            display_name=display_name,
            runtime=runtime,
            spawn=spawn,
            protected=protected,
            owner=owner,
        )

    def whoami(self, actor_id: str) -> dict:
        return self._actors.whoami(actor_id)

    def transfer_worker(self, worker: str, owner: str) -> dict:
        return self._actors.transfer_worker(worker, owner)

    def list_agents(self) -> list[dict]:
        return self._actors.list_agents()

    def list_actors(self) -> list[dict]:
        return self._actors.list_actors()

    def update_actor_spawn(self, actor_id: str, spawn: dict) -> bool:
        """Update only a registered actor's spawn block."""
        self._db.init()
        with self._db.connection() as conn:
            cursor = conn.execute(
                "UPDATE actors SET spawn_json = ? WHERE id = ?",
                (json.dumps(spawn, sort_keys=True), actor_id),
            )
        return cursor.rowcount == 1

    def list_dispatches(self, *args, **kwargs):
        return self._dispatch.list_dispatches(*args, **kwargs)

    def project_dispatch(self, *args, **kwargs):
        return self._dispatch.project_dispatch(*args, **kwargs)

    def require_agent(self, agent_id: str) -> None:
        return self._actors.require_agent(agent_id)

    def require_launchable_actor(self, actor_id: str) -> None:
        return self._actors.require_launchable_actor(actor_id)

    def actor_protection(self, actor_id: str) -> dict | None:
        return self._actors.actor_protection(actor_id)

    def send_message(
        self,
        from_agent: str,
        to_agents: list[str],
        subject: str,
        body: str | None,
        refs: list[dict],
        priority: str = "normal",
        requires_ack: bool = False,
        parent_message_id: str | None = None,
        *,
        body_file: str | None = None,
    ) -> dict:
        return self._mailbox.send_message(
            from_agent,
            to_agents,
            subject,
            body,
            refs,
            priority,
            requires_ack,
            parent_message_id,
            body_file=body_file,
        )

    def dispatch_agent(self, *args, **kwargs):
        return self._dispatch.dispatch_agent(*args, **kwargs)

    def start_queued_dispatches(self, *args, **kwargs):
        return self._dispatch.start_queued_dispatches(*args, **kwargs)

    def retry_spawn(self, *args, **kwargs):
        return self._dispatch.retry_spawn(*args, **kwargs)

    def reconcile_dispatches(self, *args, **kwargs):
        return self._dispatch.reconcile_dispatches(*args, **kwargs)

    def request_cancellation(self, *args, **kwargs):
        return self._dispatch.request_cancellation(*args, **kwargs)

    def settle_dispatch_preview(self, *args, **kwargs):
        return self._dispatch.settle_dispatch_preview(*args, **kwargs)

    def settle_dispatch_execute(self, *args, **kwargs):
        return self._dispatch.settle_dispatch_execute(*args, **kwargs)

    def worker_usage_candidates(self, *args, **kwargs):
        return self._dispatch.worker_usage_candidates(*args, **kwargs)

    def write_worker_usage(self, *args, **kwargs):
        return self._dispatch.write_worker_usage(*args, **kwargs)

    def upsert_monitor_heartbeat(self, *, interval_seconds: float, monitor_version: str) -> dict:
        self._db.init()
        now = utc_now()
        pid = os.getpid()
        with self._db.connection() as conn:
            conn.execute(
                """
                insert into monitor_heartbeat(
                  id, last_pass_at, pid, interval_seconds, monitor_version, last_stale_page_at
                )
                values(1, ?, ?, ?, ?, NULL)
                on conflict(id) do update set
                  last_pass_at = excluded.last_pass_at,
                  pid = excluded.pid,
                  interval_seconds = excluded.interval_seconds,
                  monitor_version = excluded.monitor_version
                """,
                (now, pid, interval_seconds, monitor_version),
            )
        return {
            "last_pass_at": now,
            "pid": pid,
            "interval_seconds": interval_seconds,
            "monitor_version": monitor_version,
        }

    def monitor_heartbeat(self) -> dict | None:
        self._db.init()
        with self._db.connection() as conn:
            row = conn.execute(
                """
                select last_pass_at, pid, interval_seconds, monitor_version, last_stale_page_at
                from monitor_heartbeat
                where id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "last_pass_at": row["last_pass_at"],
            "pid": row["pid"],
            "interval_seconds": row["interval_seconds"],
            "monitor_version": row["monitor_version"],
            "last_stale_page_at": row["last_stale_page_at"],
        }

    def claim_monitor_stale_page(self, *, stale_page_interval_seconds: int = 3600) -> str | None:
        self._db.init()
        now = utc_now()
        throttle_before = (datetime.now(timezone.utc) - timedelta(seconds=stale_page_interval_seconds)).isoformat(
            timespec="seconds"
        )
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            conn.execute(
                """
                insert into monitor_heartbeat(id, last_stale_page_at)
                values(1, NULL)
                on conflict(id) do nothing
                """
            )
            cursor = conn.execute(
                """
                update monitor_heartbeat
                set last_stale_page_at = ?
                where id = 1
                  and (
                    last_stale_page_at is null
                    or last_stale_page_at <= ?
                  )
                """,
                (now, throttle_before),
            )
            if cursor.rowcount != 1:
                return None
        return now

    def list_inbox(self, agent_id: str, unread_only: bool = True, include_closed: bool = False, limit: int = 20) -> list[dict]:
        return self._mailbox.list_inbox(agent_id, unread_only, include_closed, limit)

    def list_unread(self, limit: int = 100) -> list[dict]:
        return self._mailbox.list_unread(limit)

    def read_message(self, agent_id: str, message_id: str) -> dict:
        return self._mailbox.read_message(agent_id, message_id)

    def ack_message(self, agent_id: str, message_id: str, response: str) -> dict:
        return self._mailbox.ack_message(agent_id, message_id, response)

    def close_message(self, agent_id: str, message_id: str, response: str = "") -> dict:
        return self._mailbox.close_message(agent_id, message_id, response)

    def close_dispatch(self, agent_id: str, **kwargs) -> dict:
        return self._mailbox.close_dispatch(agent_id, **kwargs)

    def wait_for_reply(
        self,
        agent_id: str,
        after_message_id: str | None = None,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 1.0,
        full: bool = False,
    ) -> dict:
        return self._mailbox.wait_for_reply(
            agent_id,
            after_message_id=after_message_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            full=full,
        )

    def post_status(
        self,
        agent_id: str,
        summary: str,
        current_files: list[str],
        blocked_on: str = "",
        next_step: str = "",
        dispatch_id: str | None = None,
        thread_ref: str | None = None,
    ) -> dict:
        return self._status.post_status(
            agent_id,
            summary,
            current_files,
            blocked_on,
            next_step,
            dispatch_id,
            thread_ref,
        )

    def list_status(self) -> list[dict]:
        return self._status.list_status()

    def post_handoff(
        self,
        actor_id: str,
        body: str,
        refs: list[dict] | None = None,
        *,
        created_by_actor_id: str,
    ) -> dict:
        return self._handoff.post_handoff(
            actor_id,
            body,
            refs,
            created_by_actor_id=created_by_actor_id,
        )

    def read_handoff(self, actor_id: str) -> dict | None:
        return self._handoff.read_handoff(actor_id)

    def list_handoffs(self, actor_id: str) -> list[dict]:
        return self._handoff.list_handoffs(actor_id)

    def session_start_handoff(self, actor_id: str) -> str:
        return self._handoff.session_start_text(actor_id)

    def _dispatch_by_idempotency_key_fresh(self, *args, **kwargs):
        return self._dispatch._dispatch_by_idempotency_key_fresh(*args, **kwargs)
