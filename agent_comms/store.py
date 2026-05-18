from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .schema import ValidationError, validate_priority, validate_refs


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        return conn

    def init(self) -> None:
        if self._initialized:
            return
        with self.connect() as conn:
            conn.execute("pragma journal_mode = wal")
            conn.executescript(
                """
                create table if not exists agents(
                  id text primary key,
                  team text not null,
                  role text not null,
                  project_root text not null,
                  capabilities_json text not null,
                  last_seen_at text not null
                );

                create table if not exists messages(
                  id text primary key,
                  from_agent text not null,
                  subject text not null,
                  body text not null,
                  refs_json text not null,
                  priority text not null,
                  requires_ack integer not null,
                  created_at text not null,
                  foreign key(from_agent) references agents(id)
                );

                create table if not exists message_recipients(
                  message_id text not null,
                  to_agent text not null,
                  status text not null,
                  read_at text,
                  acked_at text,
                  closed_at text,
                  ack_response text,
                  primary key(message_id, to_agent),
                  foreign key(message_id) references messages(id),
                  foreign key(to_agent) references agents(id)
                );

                create table if not exists message_threads(
                  message_id text primary key,
                  parent_message_id text,
                  foreign key(message_id) references messages(id),
                  foreign key(parent_message_id) references messages(id)
                );

                create table if not exists statuses(
                  id text primary key,
                  agent_id text not null,
                  summary text not null,
                  current_files_json text not null,
                  blocked_on text,
                  next_step text,
                  created_at text not null,
                  foreign key(agent_id) references agents(id)
                );

                create index if not exists idx_message_recipients_inbox
                  on message_recipients(to_agent, status);

                create index if not exists idx_statuses_agent_created
                  on statuses(agent_id, created_at desc);
                """
            )
            self._ensure_column(conn, "message_recipients", "closed_at", "text")
        self._initialized = True

    def register_agent(
        self,
        agent_id: str,
        team: str,
        role: str,
        project_root: str,
        capabilities: list[str],
    ) -> dict:
        self.init()
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                insert into agents(id, team, role, project_root, capabilities_json, last_seen_at)
                values(?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                  team = excluded.team,
                  role = excluded.role,
                  project_root = excluded.project_root,
                  capabilities_json = excluded.capabilities_json,
                  last_seen_at = excluded.last_seen_at
                """,
                (agent_id, team, role, str(Path(project_root).expanduser().resolve()), json.dumps(capabilities), now),
            )
        return {"agent_id": agent_id, "team": team, "last_seen_at": now}

    def list_agents(self) -> list[dict]:
        self.init()
        with self.connect() as conn:
            rows = conn.execute("select * from agents order by team, id").fetchall()
        return [self._agent_row(row) for row in rows]

    def send_message(
        self,
        from_agent: str,
        to_agents: list[str],
        subject: str,
        body: str,
        refs: list[dict],
        priority: str = "normal",
        requires_ack: bool = False,
        parent_message_id: str | None = None,
    ) -> dict:
        self.init()
        priority = validate_priority(priority)
        subject = subject.strip()
        body = body.strip()
        if not subject:
            raise ValidationError("subject must not be empty")
        if not body:
            raise ValidationError("body must not be empty")
        if not to_agents:
            raise ValidationError("to_agents must not be empty")

        with self.connect() as conn:
            self._require_agent(conn, from_agent)
            for to_agent in to_agents:
                self._require_agent(conn, to_agent)
            project_roots = [
                Path(row["project_root"])
                for row in conn.execute("select project_root from agents").fetchall()
            ]
            refs = validate_refs(refs, project_roots)
            if parent_message_id:
                self._require_message(conn, parent_message_id)

            message_id = f"msg_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
            now = utc_now()
            conn.execute(
                """
                insert into messages(id, from_agent, subject, body, refs_json, priority, requires_ack, created_at)
                values(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, from_agent, subject, body, json.dumps(refs), priority, int(requires_ack), now),
            )
            conn.execute(
                "insert into message_threads(message_id, parent_message_id) values(?, ?)",
                (message_id, parent_message_id),
            )
            for to_agent in to_agents:
                conn.execute(
                    "insert into message_recipients(message_id, to_agent, status) values(?, ?, 'sent')",
                    (message_id, to_agent),
                )

        return {"id": message_id, "from": from_agent, "to": to_agents, "created_at": now}

    def list_inbox(self, agent_id: str, unread_only: bool = True, include_closed: bool = False, limit: int = 20) -> list[dict]:
        self.init()
        with self.connect() as conn:
            self._require_agent(conn, agent_id)
            clauses = ["mr.to_agent = ?"]
            params: list[object] = [agent_id]
            if unread_only:
                clauses.append("mr.status = 'sent'")
            if not include_closed:
                clauses.append("mr.status != 'closed'")
            params.append(limit)
            rows = conn.execute(
                f"""
                select m.*, mr.to_agent, mr.status, mt.parent_message_id
                from message_recipients mr
                join messages m on m.id = mr.message_id
                left join message_threads mt on mt.message_id = m.id
                where {' and '.join(clauses)}
                order by m.created_at desc
                limit ?
                """,
                params,
            ).fetchall()
        return [self._message_row(row) for row in rows]

    def list_unread(self, limit: int = 100) -> list[dict]:
        self.init()
        with self.connect() as conn:
            rows = conn.execute(
                """
                select m.*, mr.to_agent, mr.status, mt.parent_message_id
                from message_recipients mr
                join messages m on m.id = mr.message_id
                left join message_threads mt on mt.message_id = m.id
                where mr.status = 'sent'
                order by
                  case m.priority
                    when 'blocker' then 0
                    when 'high' then 1
                    when 'normal' then 2
                    else 3
                  end,
                  m.created_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [self._message_row(row) for row in rows]

    def read_message(self, agent_id: str, message_id: str) -> dict:
        self.init()
        now = utc_now()
        with self.connect() as conn:
            self._require_recipient(conn, agent_id, message_id)
            conn.execute(
                """
                update message_recipients
                set status = case when status = 'sent' then 'read' else status end,
                    read_at = coalesce(read_at, ?)
                where to_agent = ? and message_id = ?
                """,
                (now, agent_id, message_id),
            )
            row = conn.execute(
                """
                select m.*, mr.to_agent, mr.status, mt.parent_message_id
                from message_recipients mr
                join messages m on m.id = mr.message_id
                left join message_threads mt on mt.message_id = m.id
                where mr.to_agent = ? and mr.message_id = ?
                """,
                (agent_id, message_id),
            ).fetchone()
        return self._message_row(row)

    def ack_message(self, agent_id: str, message_id: str, response: str) -> dict:
        self.init()
        now = utc_now()
        with self.connect() as conn:
            self._require_recipient(conn, agent_id, message_id)
            conn.execute(
                """
                update message_recipients
                set status = 'acknowledged',
                    read_at = coalesce(read_at, ?),
                    acked_at = ?,
                    ack_response = ?
                where to_agent = ? and message_id = ?
                """,
                (now, now, response.strip(), agent_id, message_id),
            )
        return {"message_id": message_id, "agent_id": agent_id, "status": "acknowledged", "acked_at": now}

    def close_message(self, agent_id: str, message_id: str, response: str = "") -> dict:
        """Close a recipient's copy of a message, marking it read if needed."""
        self.init()
        now = utc_now()
        with self.connect() as conn:
            self._require_recipient(conn, agent_id, message_id)
            conn.execute(
                """
                update message_recipients
                set status = 'closed',
                    read_at = coalesce(read_at, ?),
                    closed_at = ?,
                    ack_response = case when ? = '' then ack_response else ? end
                where to_agent = ? and message_id = ?
                """,
                (now, now, response.strip(), response.strip(), agent_id, message_id),
            )
        return {"message_id": message_id, "agent_id": agent_id, "status": "closed", "closed_at": now}

    def wait_for_reply(
        self,
        agent_id: str,
        after_message_id: str | None = None,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 1.0,
    ) -> dict:
        self.init()
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            messages = self._list_unread_for_wait(agent_id, after_message_id)
            if messages:
                return {"timed_out": False, "messages": messages}
            if time.monotonic() >= deadline:
                return {"timed_out": True, "messages": []}
            time.sleep(max(poll_interval_seconds, 0.1))

    def post_status(
        self,
        agent_id: str,
        summary: str,
        current_files: list[str],
        blocked_on: str = "",
        next_step: str = "",
    ) -> dict:
        self.init()
        with self.connect() as conn:
            self._require_agent(conn, agent_id)
            status_id = f"status_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
            now = utc_now()
            conn.execute(
                """
                insert into statuses(id, agent_id, summary, current_files_json, blocked_on, next_step, created_at)
                values(?, ?, ?, ?, ?, ?, ?)
                """,
                (status_id, agent_id, summary.strip(), json.dumps(current_files), blocked_on.strip(), next_step.strip(), now),
            )
        return {"id": status_id, "agent_id": agent_id, "created_at": now}

    def list_status(self) -> list[dict]:
        self.init()
        with self.connect() as conn:
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

    def _require_agent(self, conn: sqlite3.Connection, agent_id: str) -> None:
        if conn.execute("select 1 from agents where id = ?", (agent_id,)).fetchone() is None:
            raise ValidationError(f"unknown agent: {agent_id}")

    def _require_message(self, conn: sqlite3.Connection, message_id: str) -> None:
        if conn.execute("select 1 from messages where id = ?", (message_id,)).fetchone() is None:
            raise ValidationError(f"unknown message: {message_id}")

    def _require_recipient(self, conn: sqlite3.Connection, agent_id: str, message_id: str) -> None:
        row = conn.execute(
            "select 1 from message_recipients where to_agent = ? and message_id = ?",
            (agent_id, message_id),
        ).fetchone()
        if row is None:
            raise ValidationError(f"message {message_id} is not addressed to {agent_id}")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"alter table {table} add column {column} {definition}")

    def _list_unread_for_wait(self, agent_id: str, after_message_id: str | None) -> list[dict]:
        with self.connect() as conn:
            self._require_agent(conn, agent_id)
            clauses = ["mr.to_agent = ?", "mr.status = 'sent'"]
            params: list[object] = [agent_id]
            if after_message_id:
                clauses.append("mt.parent_message_id = ?")
                params.append(after_message_id)
            rows = conn.execute(
                f"""
                select m.*, mr.to_agent, mr.status, mt.parent_message_id
                from message_recipients mr
                join messages m on m.id = mr.message_id
                left join message_threads mt on mt.message_id = m.id
                where {' and '.join(clauses)}
                order by m.created_at desc
                limit 20
                """,
                params,
            ).fetchall()
        return [self._message_row(row) for row in rows]

    def _agent_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "team": row["team"],
            "role": row["role"],
            "project_root": row["project_root"],
            "capabilities": json.loads(row["capabilities_json"]),
            "last_seen_at": row["last_seen_at"],
        }

    def _message_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "from": row["from_agent"],
            "to": row["to_agent"],
            "subject": row["subject"],
            "body": row["body"],
            "refs": json.loads(row["refs_json"]),
            "priority": row["priority"],
            "requires_ack": bool(row["requires_ack"]),
            "status": row["status"],
            "parent_message_id": row["parent_message_id"],
            "created_at": row["created_at"],
        }

    def _status_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "summary": row["summary"],
            "current_files": json.loads(row["current_files_json"]),
            "blocked_on": row["blocked_on"],
            "next_step": row["next_step"],
            "created_at": row["created_at"],
        }
