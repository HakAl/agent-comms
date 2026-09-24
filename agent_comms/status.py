from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .actors import ActorRegistry
from .clock import utc_now
from .db import Database
from . import param_leak


class StatusBoard:
    def __init__(self, db: Database, actors: ActorRegistry) -> None:
        self._db = db
        self._actors = actors

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
        self._db.init()
        for field, value in (
            ("agent_id", agent_id), ("summary", summary), ("blocked_on", blocked_on),
            ("next_step", next_step), ("dispatch_id", dispatch_id), ("thread_ref", thread_ref),
        ):
            if value is not None:
                param_leak.assert_no_parameter_leak(field, value)
        with self._db.connection() as conn:
            self._actors._require_agent(conn, agent_id)
            status_id = f"status_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
            now = utc_now()
            conn.execute(
                """
                insert into statuses(
                  id, agent_id, summary, current_files_json, blocked_on, next_step,
                  dispatch_id, thread_ref, created_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    status_id,
                    agent_id,
                    summary.strip(),
                    json.dumps(current_files),
                    blocked_on.strip(),
                    next_step.strip(),
                    dispatch_id,
                    thread_ref,
                    now,
                ),
            )
        return {"id": status_id, "agent_id": agent_id, "created_at": now}

    def list_status(self) -> list[dict]:
        self._db.init()
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                select s.*
                from statuses s
                join (
                  select agent_id, max(created_at) as max_created_at
                  from statuses
                  group by agent_id
                ) latest on latest.agent_id = s.agent_id and latest.max_created_at = s.created_at
                order by s.agent_id
                """
            ).fetchall()
        return [self._status_row(row) for row in rows]

    def _status_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "summary": row["summary"],
            "current_files": json.loads(row["current_files_json"]),
            "blocked_on": row["blocked_on"],
            "next_step": row["next_step"],
            "dispatch_id": row["dispatch_id"],
            "thread_ref": row["thread_ref"],
            "created_at": row["created_at"],
        }
