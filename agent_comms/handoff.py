from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .actors import ActorRegistry, agent_project_roots
from .db import Database
from . import param_leak
from .schema import ValidationError, require_non_empty, validate_refs

HANDOFF_WARNING = (
    "This handoff is a prior-session hypothesis, not live state. Verify versions, "
    "commits, dispatch rows, and pending work before repeating them as current."
)


class HandoffBoard:
    def __init__(self, db: Database, actors: ActorRegistry) -> None:
        self._db = db
        self._actors = actors

    def post_handoff(
        self,
        actor_id: str,
        body: str,
        refs: list[dict] | None = None,
        *,
        created_by_actor_id: str,
    ) -> dict:
        self._db.init()
        for field, value in (
            ("actor_id", actor_id), ("body", body),
            ("created_by_actor_id", created_by_actor_id),
        ):
            param_leak.assert_no_parameter_leak(field, value)
        body = require_non_empty(body, "body")
        with self._db.connection() as conn:
            self._actors._require_actor(conn, actor_id)
            self._actors._require_actor(conn, created_by_actor_id)
            refs = self._validate_refs(conn, refs or [])
            supersedes = self._latest_handoff_row(conn, actor_id)
            handoff_id = f"handoff_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
            now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            conn.execute(
                """
                insert into handoffs(
                  id, actor_id, body, refs_json, created_at,
                  created_by_actor_id, supersedes_handoff_id
                )
                values(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff_id,
                    actor_id,
                    body,
                    json.dumps(refs),
                    now,
                    created_by_actor_id,
                    None if supersedes is None else supersedes["id"],
                ),
            )
            row = self._handoff_row_by_id(conn, handoff_id)
        return self._handoff_row(row)

    def read_handoff(self, actor_id: str) -> dict | None:
        self._db.init()
        with self._db.connection() as conn:
            self._actors._require_actor(conn, actor_id)
            row = self._latest_handoff_row(conn, actor_id)
        return None if row is None else self._handoff_row(row)

    def list_handoffs(self, actor_id: str) -> list[dict]:
        self._db.init()
        with self._db.connection() as conn:
            self._actors._require_actor(conn, actor_id)
            rows = conn.execute(
                """
                select *
                from handoffs
                where actor_id = ?
                order by created_at desc, id desc
                """,
                (actor_id,),
            ).fetchall()
        return [self._handoff_row(row) for row in rows]

    def session_start_text(self, actor_id: str) -> str:
        handoff = self.read_handoff(actor_id)
        if handoff is None:
            return ""
        refs = "\n".join(
            f"- {ref['path']}" + (f" - {ref['summary']}" if ref.get("summary") else "")
            for ref in handoff["refs"]
        )
        refs_block = f"\n\nRefs:\n{refs}" if refs else ""
        return (
            f"{HANDOFF_WARNING}\n\n"
            f"Handoff artifact: {handoff['id']} created_at={handoff['created_at']} "
            f"actor_id={handoff['actor_id']} created_by_actor_id={handoff['created_by_actor_id']}\n\n"
            f"{handoff['body']}"
            f"{refs_block}"
        )

    def _validate_refs(self, conn: sqlite3.Connection, refs: list[dict]) -> list[dict[str, str]]:
        return validate_refs(refs, agent_project_roots(conn))

    def _latest_handoff_row(self, conn: sqlite3.Connection, actor_id: str) -> sqlite3.Row | None:
        return conn.execute(
            """
            select *
            from handoffs
            where actor_id = ?
            order by created_at desc, id desc
            limit 1
            """,
            (actor_id,),
        ).fetchone()

    def _handoff_row_by_id(self, conn: sqlite3.Connection, handoff_id: str) -> sqlite3.Row:
        row = conn.execute("select * from handoffs where id = ?", (handoff_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"handoff row disappeared: {handoff_id}")
        return row

    def _handoff_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "actor_id": row["actor_id"],
            "body": row["body"],
            "refs": json.loads(row["refs_json"]),
            "created_at": row["created_at"],
            "created_by_actor_id": row["created_by_actor_id"],
            "supersedes_handoff_id": row["supersedes_handoff_id"],
        }


def session_start_text_from_env(store, env: dict[str, str] | None = None) -> str:
    environment = os.environ if env is None else env
    actor_id = environment.get("AGENT_COMMS_ACTOR_ID", "").strip()
    if not actor_id:
        return ""
    try:
        return store.session_start_handoff(actor_id)
    except ValidationError:
        return ""
