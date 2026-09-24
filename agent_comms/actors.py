from __future__ import annotations

import json
import os
import shlex
import sqlite3
from pathlib import Path

from .clock import utc_now
from .db import Database, is_declined_worker
from .schema import ValidationError, validate_actor_id_for_kind, validate_kind

OPERATOR_ACTOR_ENV = "AGENT_COMMS_OPERATOR_ACTOR"


def agent_project_roots(conn: sqlite3.Connection) -> list[Path]:
    return [
        Path(row["project_root"])
        for row in conn.execute(
            "select distinct project_root from actors where kind = 'agent' "
            "and project_root is not null order by project_root"
        ).fetchall()
    ]


class ActorRegistry:
    def __init__(self, db: Database) -> None:
        self._db = db

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
        result = self.register_actor(
            agent_id,
            "agent",
            agent_id,
            team=team,
            role=role,
            project_root=project_root,
            capabilities=capabilities,
            owner=owner,
        )
        return {"agent_id": agent_id, "team": team, "last_seen_at": result["last_seen_at"]}

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
        self._db.init()
        kind = validate_kind(kind)
        actor_id = validate_actor_id_for_kind(actor_id, kind)
        display_name = display_name.strip()
        if not display_name:
            raise ValidationError("display_name must not be empty")
        if kind != "agent":
            if any([team, role, project_root, runtime, spawn, capabilities, owner]):
                raise ValidationError("non-agent actors must not carry agent metadata")
        elif not team or not role or not project_root:
            raise ValidationError("agent actors require team, role, and project_root")
        if role == "worker" and not owner:
            raise ValidationError("worker actors require owner")
        if role != "worker" and owner is not None:
            raise ValidationError("owner is only valid for worker actors")
        if dispatch_cap is not None and dispatch_cap < 1:
            raise ValidationError("dispatch_cap must be at least 1")
        now = utc_now()
        project_root_value = str(Path(project_root).expanduser().resolve()) if project_root else None
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            try:
                if owner is not None:
                    owner_row = self._actor_row_by_id(conn, owner)
                    if owner_row["kind"] != "agent" or owner_row["role"] != "architect":
                        raise ValidationError("worker owner must name an agent architect")
                if kind == "agent":
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
                        (
                            actor_id,
                            team,
                            role,
                            project_root_value,
                            json.dumps(capabilities or []),
                            now,
                        ),
                    )

                actor_values = (
                    actor_id,
                    kind,
                    display_name,
                    system_class,
                    system_instance,
                    project_root_value,
                    runtime,
                    json.dumps(spawn or {}),
                    json.dumps(capabilities or []),
                    team,
                    role,
                    now,
                    dispatch_cap if dispatch_cap is not None else 4,
                    int(bool(protected)) if protected is not None else 0,
                )
                conn.execute(
                    """
                    insert into actors(
                      id, kind, display_name, system_class, system_instance,
                      project_root, runtime, spawn_json, capabilities_json,
                      team, role, last_seen_at, dispatch_cap, protected, owner_actor_id
                    )
                    values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(id) do update set
                      kind = excluded.kind,
                      display_name = excluded.display_name,
                      system_class = excluded.system_class,
                      system_instance = excluded.system_instance,
                      project_root = excluded.project_root,
                      runtime = excluded.runtime,
                      spawn_json = excluded.spawn_json,
                      capabilities_json = excluded.capabilities_json,
                      team = excluded.team,
                      role = excluded.role,
                      last_seen_at = excluded.last_seen_at,
                      dispatch_cap = case when ? then excluded.dispatch_cap else actors.dispatch_cap end,
                      protected = case when ? then excluded.protected else actors.protected end
                      , owner_actor_id = excluded.owner_actor_id
                    """,
                    (*actor_values, owner, dispatch_cap is not None, protected is not None),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"actor_id": actor_id, "kind": kind, "last_seen_at": now}

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
        result = self.register_actor(
            agent_id,
            "agent",
            display_name or agent_id,
            team=team,
            role=role,
            project_root=project_root,
            runtime=runtime,
            spawn=spawn,
            capabilities=capabilities,
            protected=protected,
            owner=owner,
        )
        return {"agent_id": agent_id, "team": team, "last_seen_at": result["last_seen_at"]}

    def list_agents(self) -> list[dict]:
        self._db.init()
        with self._db.connection() as conn:
            rows = conn.execute("select * from agents order by team, id").fetchall()
        return [self._agent_row(row) for row in rows]

    def list_actors(self) -> list[dict]:
        self._db.init()
        with self._db.connection() as conn:
            rows = conn.execute("select * from actors order by kind, display_name, id").fetchall()
        return [self._actor_row(row) for row in rows]

    def whoami(self, actor_id: str) -> dict:
        self._db.init()
        with self._db.connection() as conn:
            row = self._actor_row_by_id(conn, actor_id)
            result = {key: row[key] for key in ("id", "kind", "team", "role")}
            if row["role"] == "architect":
                result["owned_worker_ids"] = [r["id"] for r in conn.execute(
                    "select id from actors where owner_actor_id = ? order by id", (actor_id,)
                )]
            return result

    def transfer_worker(self, worker: str, owner: str) -> dict:
        self._db.init()
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            worker_row = self._actor_row_by_id(conn, worker)
            owner_row = self._actor_row_by_id(conn, owner)
            if worker_row["kind"] != "agent" or worker_row["role"] != "worker":
                raise ValidationError("transfer subject must be an agent worker")
            if owner_row["kind"] != "agent" or owner_row["role"] != "architect":
                raise ValidationError("worker owner must name an agent architect")
            conn.execute("update actors set owner_actor_id = ? where id = ?", (owner, worker))
        return {"worker": worker, "owner": owner}

    def require_agent(self, agent_id: str) -> None:
        self._db.init()
        with self._db.connection() as conn:
            self._require_agent(conn, agent_id)

    def require_launchable_actor(self, actor_id: str) -> None:
        self._db.init()
        with self._db.connection() as conn:
            row = self._actor_row_by_id(conn, actor_id)
            if row["kind"] not in {"agent", "human"}:
                raise ValidationError(f"actor is not launchable: {actor_id} kind={row['kind']}")

    def require_human(self, actor_id: str | None) -> str:
        """Validate an explicitly supplied operator id as a registered human actor.

        Returns the same id when it names an actor that is both registered AND
        ``kind == 'human'``. A missing (``None``/empty), unknown, or non-human
        (e.g. agent-kind) id raises ``ValidationError`` loudly. This never selects
        another human by sort order: the caller must terminate the requested
        operator interaction rather than silently pick a different operator.
        """
        if not actor_id:
            raise ValidationError("a human operator actor id is required and was not supplied")
        self._db.init()
        with self._db.connection() as conn:
            row = self._actor_row_by_id(conn, actor_id)
            if row["kind"] != "human":
                raise ValidationError(
                    f"operator actor {actor_id} is kind={row['kind']}, not human"
                )
        return actor_id

    def resolve_operator_human(self) -> str:
        """Return the operator human from configuration, never a fixed id.

        ``AGENT_COMMS_OPERATOR_ACTOR`` names the operator explicitly and must be
        a registered human. Without it, the single registered human is the
        operator. No human, or several humans without the override, refuses
        rather than guessing.
        """
        configured = os.environ.get(OPERATOR_ACTOR_ENV, "").strip()
        if configured:
            return self.require_human(configured)
        self._db.init()
        with self._db.connection() as conn:
            rows = conn.execute("select id from actors where kind = 'human' order by id").fetchall()
        if not rows:
            raise ValidationError("no human actor is registered; register the operator in the roster")
        if len(rows) > 1:
            raise ValidationError(
                f"{len(rows)} human actors are registered; set {OPERATOR_ACTOR_ENV} "
                "to the operator's actor id"
            )
        return rows[0]["id"]

    def _actor_row_by_id(
        self, conn: sqlite3.Connection, actor_id: str, *, allow_declined: bool = False
    ) -> sqlite3.Row:
        if not allow_declined and is_declined_worker(conn, actor_id):
            raise ValidationError(self._declined_worker_message(conn, actor_id))
        row = conn.execute("select * from actors where id = ?", (actor_id,)).fetchone()
        if row is None:
            raise ValidationError(f"unknown actor: {actor_id}")
        return row

    def _require_actor(self, conn: sqlite3.Connection, actor_id: str) -> None:
        self._actor_row_by_id(conn, actor_id)

    def _require_agent(self, conn: sqlite3.Connection, agent_id: str) -> None:
        if conn.execute("select 1 from agents where id = ?", (agent_id,)).fetchone() is None:
            raise ValidationError(f"unknown agent: {agent_id}")
        self._actor_row_by_id(conn, agent_id)

    def _declined_worker_message(self, conn: sqlite3.Connection, actor_id: str) -> str:
        row = conn.execute(
            "select id, team, project_root from agents where id = ?", (actor_id,)
        ).fetchone()
        repair = " ".join(
            shlex.quote(str(value))
            for value in (
                "agent-comms", "register", row["id"], "--team", row["team"],
                "--role", "worker", "--owner", "<architect-id>",
                "--project-root", row["project_root"],
            )
        )
        return (
            f"declined worker: {actor_id}\n"
            "Repair template (fill in <architect-id> and run as a human):\n"
            f"{repair}"
        )

    def _first_human_actor_id(self) -> str | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "select id from actors where kind = 'human' order by display_name, id limit 1"
            ).fetchone()
        return None if row is None else row["id"]

    def actor_protection(self, actor_id: str) -> dict | None:
        self._db.init()
        with self._db.connection() as conn:
            row = conn.execute(
                "select id, team, protected from actors where id = ?",
                (actor_id,),
            ).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "team": row["team"], "protected": bool(row["protected"])}

    def _agent_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "team": row["team"],
            "role": row["role"],
            "project_root": row["project_root"],
            "capabilities": json.loads(row["capabilities_json"]),
            "last_seen_at": row["last_seen_at"],
        }

    def _actor_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "display_name": row["display_name"],
            "system_class": row["system_class"],
            "system_instance": row["system_instance"],
            "project_root": row["project_root"],
            "runtime": row["runtime"],
            "spawn": json.loads(row["spawn_json"] or "{}"),
            "capabilities": json.loads(row["capabilities_json"] or "[]"),
            "team": row["team"],
            "role": row["role"],
            "last_seen_at": row["last_seen_at"],
            "dispatch_cap": row["dispatch_cap"],
            "protected": bool(row["protected"]),
            "owner_actor_id": row["owner_actor_id"],
        }
