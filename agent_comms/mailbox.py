from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agent_comms import param_leak

from . import payload
from .actors import ActorRegistry, agent_project_roots
from .clock import utc_now
from .db import Database
from .reviewing import reply_snapshots
from .schema import ValidationError, identity_to_path_segment, validate_priority, validate_refs


SNIPPET_CHARS = 240
CLOSEOUT_PROTOCOL_VERSION = 1


def _fold(text: str) -> str:
    return " ".join(text.split())


class Mailbox:
    def __init__(self, db: Database, actors: ActorRegistry) -> None:
        self._db = db
        self._actors = actors

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
        self._db.init()
        if (body is None) == (body_file is None):
            raise ValidationError("exactly one of body or body_file must be supplied")
        for field, value in (
            ("from_agent", from_agent), ("subject", subject), ("body", body),
            ("body_file", body_file),
            ("priority", priority), ("parent_message_id", parent_message_id),
        ):
            if value is not None:
                param_leak.assert_no_parameter_leak(field, value)
        priority = validate_priority(priority)
        subject = subject.strip()
        if body is not None:
            body = body.strip()
        if not subject:
            raise ValidationError("subject must not be empty")
        if body is not None and not body:
            raise ValidationError("body must not be empty")
        if not to_agents:
            raise ValidationError("to_agents must not be empty")

        capture: payload.CapturedPayload | None = None
        staged: payload.StagedPayload | None = None
        published: payload.PublishedBlob | None = None
        store_root = payload.store_root_for_db(self._db.db_path)
        if body_file is not None:
            with self._db.connection() as conn:
                sender = self._actors._actor_row_by_id(conn, from_agent)
            root = sender["project_root"]
            if not root:
                raise ValidationError(
                    "dispatch_payload_path_invalid: file-backed send_message requires the "
                    f"sender's registered project_root and {from_agent} has none registered"
                )
            capture = payload.capture_source(Path(root), body_file)
            staged = payload.stage_payload(store_root, capture.data)

        # Review evidence-lifecycle boundary: SQL-only classify the pending
        # reply, then (only on a bound implementation match) measure the stable
        # delta snapshot BEFORE opening any write transaction. Classification is
        # cheap SQL, so an ordinary message runs zero snapshot Git commands, and
        # no Git subprocess runs while the write lock is held.
        reply_binding: reply_snapshots.ReplyBinding | None = None
        reply_snapshot: dict | None = None
        if parent_message_id is not None:
            with self._db.connection() as conn:
                reply_binding = reply_snapshots.classify_reply(
                    conn,
                    sender=from_agent,
                    to_agents=to_agents,
                    parent_message_id=parent_message_id,
                )
            if reply_binding is not None:
                reply_snapshot = reply_snapshots.capture(reply_binding)

        try:
            with self._db.connection() as conn:
                conn.execute("begin immediate")
                try:
                    self._actors._require_actor(conn, from_agent)
                    for to_agent in to_agents:
                        self._actors._require_actor(conn, to_agent)
                    if capture is not None:
                        published = payload.publish_final(store_root, staged, capture)
                        staged = None
                    message = self._insert_message(
                        conn,
                        from_agent,
                        to_agents,
                        subject,
                        payload.ARTIFACT_BODY_MARKER if capture is not None else body,
                        refs,
                        priority,
                        requires_ack,
                        parent_message_id,
                    )
                    if capture is not None:
                        conn.execute(
                            """insert into message_payload_refs(
                                 message_id, storage_kind, payload_sha256, byte_count,
                                 char_count, captured_at) values(?, ?, ?, ?, ?, ?)""",
                            (
                                message["id"],
                                payload.STORAGE_KIND,
                                capture.sha256,
                                capture.byte_count,
                                capture.char_count,
                                utc_now(),
                            ),
                        )
                    if reply_binding is not None:
                        # SQL-only revalidate the exact binding and insert the
                        # immutable snapshot row atomically with the message; any
                        # drift refuses so neither the reply nor the snapshot is
                        # published and no semaphore is written.
                        reply_snapshots.revalidate(
                            conn,
                            reply_binding,
                            sender=from_agent,
                            to_agents=to_agents,
                            parent_message_id=parent_message_id,
                        )
                        reply_snapshots.insert_snapshot_row(
                            conn,
                            reply_binding,
                            reply_snapshot,
                            reply_message_id=message["id"],
                        )
                    conn.commit()
                except Exception as exc:
                    payload.cleanup_failed_publication(published, exc)
                    conn.rollback()
                    raise
        finally:
            if published is not None:
                published.close()
            payload.discard_staging(staged)

        self._write_semaphores(message["recipient_roots"], to_agents, message["id"], message["created_at"])
        return {"id": message["id"], "from": from_agent, "to": to_agents, "created_at": message["created_at"]}

    def list_inbox(self, agent_id: str, unread_only: bool = True, include_closed: bool = False, limit: int = 20) -> list[dict]:
        self._db.init()
        with self._db.connection() as conn:
            self._actors._require_actor(conn, agent_id)
            clauses = ["mr.to_agent = ?"]
            params: list[object] = [agent_id]
            if unread_only:
                clauses.append("mr.status = 'sent'")
            if not include_closed:
                # ``closed`` and the stage-2 ``cancelled`` are both terminal
                # delivery states: a default listing hides a withdrawn (cancelled)
                # obligation exactly like a closed one.
                clauses.append("mr.status not in ('closed', 'cancelled')")
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
            payload_refs = {row["id"]: self._payload_ref_for_message(conn, row["id"]) for row in rows}
        return [self._message_row(row, full=False, payload_ref=payload_refs[row["id"]]) for row in rows]

    def list_unread(self, limit: int = 100) -> list[dict]:
        self._db.init()
        with self._db.connection() as conn:
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
            payload_refs = {row["id"]: self._payload_ref_for_message(conn, row["id"]) for row in rows}
        return [self._message_row(row, full=False, payload_ref=payload_refs[row["id"]]) for row in rows]

    def read_message(self, agent_id: str, message_id: str) -> dict:
        self._db.init()
        param_leak.assert_no_parameter_leak("agent_id", agent_id)
        param_leak.assert_no_parameter_leak("message_id", message_id)
        now = utc_now()
        with self._db.connection() as conn:
            self._require_recipient(conn, agent_id, message_id)
            # Artifact resolution completes BEFORE the sent -> read transition
            # is committed: a typed resolution failure raises here, the
            # transaction aborts, and the recipient copy stays 'sent'. The
            # bytes returned are the same bytes verified in this call.
            payload_ref = self._payload_ref_for_message(conn, message_id)
            resolved_body: str | None = None
            if payload_ref is not None:
                resolved_body = payload.load_verified_text(
                    payload.store_root_for_db(self._db.db_path),
                    storage_kind=str(payload_ref["storage_kind"]),
                    payload_sha256=str(payload_ref["payload_sha256"]),
                    byte_count=payload_ref["byte_count"],
                    char_count=payload_ref["char_count"],
                )
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
        message = self._message_row(row, full=True, payload_ref=payload_ref)
        if payload_ref is not None:
            message["body"] = resolved_body
            message.pop("body_snippet", None)
        return message

    def ack_message(self, agent_id: str, message_id: str, response: str) -> dict:
        self._db.init()
        for field, value in (("agent_id", agent_id), ("message_id", message_id), ("response", response)):
            param_leak.assert_no_parameter_leak(field, value)
        now = utc_now()
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            self._require_recipient(conn, agent_id, message_id)
            self._refuse_cancelled_copy(conn, agent_id, message_id, action="acknowledge")
            self._refuse_v2_trigger(conn, agent_id, message_id)
            self._require_reply_before_terminate(
                conn, agent_id, message_id, action="acknowledge", response=response
            )
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
            cursor = conn.execute(
                """
                update dispatch_ledger
                set status = 'closed',
                    closed_at = ?,
                    observed_values_json = json_set(
                      coalesce(nullif(observed_values_json, ''), '{}'),
                      '$.recipient_closed_at',
                      ?,
                      '$.legacy_recipient_terminal_close',
                      json('true')
                    )
                where message_id = ?
                  and recipient_actor_id = ?
                  and status = 'in_flight'
                  and policy_version = 'v1'
                """,
                (now, now, message_id, agent_id),
            )
            if cursor.rowcount == 0:
                self._record_close_ledger_mismatch(conn, agent_id, message_id, now)
        return {"message_id": message_id, "agent_id": agent_id, "status": "acknowledged", "acked_at": now}

    def close_message(self, agent_id: str, message_id: str, response: str = "") -> dict:
        """Close a recipient's copy of a message, marking it read if needed."""
        self._db.init()
        for field, value in (("agent_id", agent_id), ("message_id", message_id), ("response", response)):
            param_leak.assert_no_parameter_leak(field, value)
        now = utc_now()
        with self._db.connection() as conn:
            conn.execute("begin immediate")
            self._require_recipient(conn, agent_id, message_id)
            self._refuse_cancelled_copy(conn, agent_id, message_id, action="close")
            self._refuse_v2_trigger(conn, agent_id, message_id)
            self._require_reply_before_terminate(
                conn, agent_id, message_id, action="close", response=response
            )
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
            cursor = conn.execute(
                """
                update dispatch_ledger
                set status = 'closed',
                    closed_at = ?,
                    observed_values_json = json_set(
                      coalesce(nullif(observed_values_json, ''), '{}'),
                      '$.recipient_closed_at',
                      ?,
                      '$.legacy_recipient_terminal_close',
                      json('true')
                    )
                where message_id = ?
                  and recipient_actor_id = ?
                  and status = 'in_flight'
                  and policy_version = 'v1'
                """,
                (now, now, message_id, agent_id),
            )
            if cursor.rowcount == 0:
                self._record_close_ledger_mismatch(conn, agent_id, message_id, now)
        return {"message_id": message_id, "agent_id": agent_id, "status": "closed", "closed_at": now}

    def _refuse_v2_trigger(self, conn: sqlite3.Connection, agent_id: str, message_id: str) -> None:
        row = conn.execute(
            "select policy_version from dispatch_ledger where message_id=? and recipient_actor_id=?",
            (message_id, agent_id),
        ).fetchone()
        if row is not None and row["policy_version"] == "v2":
            raise ValidationError(
                f"message {message_id} is a v2 dispatch trigger; reply first, then close_dispatch binding your reply"
            )

    def close_dispatch(
        self,
        agent_id: str,
        *,
        message_id: str,
        result: str,
        reply_message_id: str,
        summary: str,
        artifacts: list[dict] | None = None,
        delta: bool = False,
        blocked_reason: str = "",
    ) -> dict:
        """Verify evidence and atomically settle one v2 dispatch."""
        self._db.init()
        for field, value in (
            ("agent_id", agent_id), ("message_id", message_id), ("result", result),
            ("reply_message_id", reply_message_id), ("summary", summary),
            ("blocked_reason", blocked_reason),
        ):
            param_leak.assert_no_parameter_leak(field, value)
        summary = summary.strip()
        blocked_reason = blocked_reason.strip()
        caller_payload = {
            "message_id": message_id, "result": result,
            "reply_message_id": reply_message_id, "summary": summary,
            "artifacts": artifacts or [], "delta": bool(delta),
            "blocked_reason": blocked_reason,
        }
        payload_bytes = json.dumps(caller_payload, sort_keys=True, separators=(",", ":")).encode()
        payload_sha = hashlib.sha256(payload_bytes).hexdigest()
        with self._db.connection() as conn:
            self._actors._actor_row_by_id(conn, agent_id)
            row = conn.execute("select * from dispatch_ledger where message_id=?", (message_id,)).fetchone()
            if row is None:
                raise ValidationError("not_a_dispatch_trigger")
            if row["recipient_actor_id"] != agent_id:
                raise ValidationError("wrong_actor")
            observed = json.loads(row["observed_values_json"] or "{}")
            if row["status"] != "in_flight":
                prior = observed.get("closeout")
                if prior and prior.get("caller_payload_sha256") == payload_sha:
                    return self._closeout_response(row)
                if prior:
                    raise ValidationError("closeout_conflict")
                raise ValidationError("already_settled")
            if row["policy_version"] != "v2":
                raise ValidationError("close_dispatch requires a v2 dispatch trigger")
            if result not in {"satisfied", "blocked"}:
                raise ValidationError("invalid_result")
            if not summary or len(summary) > 2000:
                raise ValidationError("summary must be non-empty and at most 2000 characters")
            if (result == "blocked") != bool(blocked_reason):
                raise ValidationError("blocked_reason must be non-empty iff result is blocked")
            reply = conn.execute(
                """select m.from_agent, mt.parent_message_id
                   from messages m join message_threads mt on mt.message_id=m.id
                   where m.id=?""", (reply_message_id,),
            ).fetchone()
            producer_copy = conn.execute(
                "select 1 from message_recipients where message_id=? and to_agent=?",
                (reply_message_id, row["producer_actor_id"]),
            ).fetchone()
            if reply is None or reply["from_agent"] != agent_id or reply["parent_message_id"] != message_id or producer_copy is None:
                raise ValidationError("wrong_thread")
            measured_artifacts = self._measure_artifacts(conn, agent_id, artifacts or [])
            # Review-bound implementation dispatches consume the immutable
            # snapshot published with the exact reply; the caller's legacy delta
            # flag cannot enable, disable, or replace that evidence. Non-review v2
            # dispatches keep the current delta=False / single live delta=True
            # behavior exactly.
            review_intent = reply_snapshots.bound_implementation_intent(
                conn, row["dispatch_id"]
            )
            if review_intent is not None:
                measured_delta = reply_snapshots.consume_for_close(
                    conn,
                    dispatch_id=row["dispatch_id"],
                    intent_id=review_intent["intent_id"],
                    reply_message_id=reply_message_id,
                    recipient=agent_id,
                    result=result,
                )
            else:
                measured_delta = self._snapshot_delta(conn, agent_id) if delta else None
            now = utc_now()
            closeout = {
                "protocol": CLOSEOUT_PROTOCOL_VERSION, "result": result, "reply_message_id": reply_message_id,
                "summary": summary, "blocked_reason": blocked_reason,
                "artifacts": measured_artifacts, "delta": measured_delta,
                "caller_payload_sha256": payload_sha, "recorded_at": now,
                "recorded_by": agent_id,
            }
            conn.execute("begin immediate")
            current = conn.execute("select status from dispatch_ledger where dispatch_id=?", (row["dispatch_id"],)).fetchone()
            if current["status"] != "in_flight":
                raise ValidationError("concurrent_settlement")
            cursor = conn.execute(
                """update dispatch_ledger set observed_values_json=json_set(
                     coalesce(nullif(observed_values_json, ''), '{}'),
                     '$.closeout',
                     json(?)
                   ), result=?, status='closed',
                   closed_at=?
                   where dispatch_id=? and status='in_flight'""",
                (json.dumps(closeout, sort_keys=True), result, now, row["dispatch_id"]),
            )
            if cursor.rowcount != 1:
                raise ValidationError("concurrent_settlement")
            conn.execute(
                """update message_recipients set status='closed', read_at=coalesce(read_at,?), closed_at=?
                   where message_id=? and to_agent=?""", (now, now, message_id, agent_id),
            )
            return {"dispatch_id": row["dispatch_id"], "message_id": message_id, "result": result,
                    "status": "closed", "closed_at": now, "closeout_recorded": True}

    @staticmethod
    def _closeout_response(row: sqlite3.Row) -> dict:
        return {"dispatch_id": row["dispatch_id"], "message_id": row["message_id"], "result": row["result"],
                "status": "closed", "closed_at": row["closed_at"], "closeout_recorded": True}

    def _measure_artifacts(self, conn: sqlite3.Connection, agent_id: str, artifacts: list[dict]) -> list[dict]:
        if len(artifacts) > 32:
            raise ValidationError("too_many_artifacts")
        actor = conn.execute("select project_root from actors where id=?", (agent_id,)).fetchone()
        root = Path(actor["project_root"]).resolve()
        measured = []
        for claim in artifacts:
            path = Path(str(claim.get("path", "")))
            candidate = path if path.is_absolute() else root / path
            try:
                real = candidate.resolve(strict=True)
            except FileNotFoundError as exc:
                raise ValidationError("artifact_missing") from exc
            if not real.is_relative_to(root) or not real.is_file():
                raise ValidationError("artifact_path_escape")
            digest = hashlib.sha256()
            size = 0
            with real.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            if claim.get("sha256") != digest.hexdigest() or claim.get("bytes") != size:
                raise ValidationError("artifact_mismatch")
            measured.append({**claim, "real_path": str(real), "remeasured_sha256": digest.hexdigest(),
                             "remeasured_bytes": size, "remeasured_at": utc_now()})
        return measured

    def _snapshot_delta(self, conn: sqlite3.Connection, agent_id: str) -> dict:
        # Legacy single-capture close delta for a non-review v2 dispatch. The
        # Git plumbing lives in reviewing.reply_snapshots (the cohesive snapshot
        # owner); this adapter only resolves the worker root.
        root = conn.execute(
            "select project_root from actors where id=?", (agent_id,)
        ).fetchone()["project_root"]
        return reply_snapshots.legacy_close_delta(Path(root))

    def _record_close_ledger_mismatch(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        message_id: str,
        now: str,
    ) -> None:
        row = conn.execute(
            """
            select status, observed_values_json
            from dispatch_ledger
            where message_id = ? and recipient_actor_id = ?
            """,
            (message_id, agent_id),
        ).fetchone()
        if row is None or row["status"] == "closed":
            return
        observed = json.loads(row["observed_values_json"] or "{}")
        observed["close_ledger_status_mismatch"] = row["status"]
        observed["close_ledger_status_mismatch_at"] = now
        conn.execute(
            """
            update dispatch_ledger
            set observed_values_json = ?
            where message_id = ? and recipient_actor_id = ?
            """,
            (json.dumps(observed, sort_keys=True), message_id, agent_id),
        )

    def wait_for_reply(
        self,
        agent_id: str,
        after_message_id: str | None = None,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 1.0,
        full: bool = False,
    ) -> dict:
        self._db.init()
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            messages = self._list_unread_for_wait(agent_id, after_message_id, full=full)
            if messages:
                return {"timed_out": False, "messages": messages}
            if time.monotonic() >= deadline:
                return {"timed_out": True, "messages": []}
            time.sleep(max(poll_interval_seconds, 0.1))

    def _insert_message(
        self,
        conn: sqlite3.Connection,
        from_actor: str,
        to_agents: list[str],
        subject: str,
        body: str,
        refs: list[dict],
        priority: str,
        requires_ack: bool,
        parent_message_id: str | None,
    ) -> dict:
        refs = validate_refs(refs, agent_project_roots(conn))
        if parent_message_id:
            self._require_message(conn, parent_message_id)

        message_id = f"msg_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        now = utc_now()
        conn.execute(
            """
            insert into messages(id, from_agent, subject, body, refs_json, priority, requires_ack, created_at)
            values(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, from_actor, subject, body, json.dumps(refs), priority, int(requires_ack), now),
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
        recipient_roots = {
            row["id"]: row["project_root"]
            for row in conn.execute(
                f"""
                select id, project_root
                from actors
                where kind = 'agent'
                  and project_root is not null
                  and id in ({','.join('?' for _ in to_agents)})
                """,
                to_agents,
            ).fetchall()
        }
        return {"id": message_id, "created_at": now, "recipient_roots": recipient_roots}

    def _require_message(self, conn: sqlite3.Connection, message_id: str) -> None:
        if conn.execute("select 1 from messages where id = ?", (message_id,)).fetchone() is None:
            raise ValidationError(f"unknown message: {message_id}")

    def _require_reply_before_terminate(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        message_id: str,
        *,
        action: str,
        response: str,
    ) -> None:
        trigger = conn.execute(
            "select producer_actor_id from dispatch_ledger where message_id = ? limit 1",
            (message_id,),
        ).fetchone()
        current = conn.execute(
            "select status from message_recipients where to_agent = ? and message_id = ?",
            (agent_id, message_id),
        ).fetchone()
        if current is None or current["status"] in ("closed", "acknowledged"):
            return
        if trigger is None:
            if not response.strip():
                return
            sender = conn.execute(
                "select from_agent from messages where id = ?",
                (message_id,),
            ).fetchone()["from_agent"]
            if agent_id == sender:
                return
            recipient = sender
        else:
            recipient = trigger["producer_actor_id"]
        reply_exists = conn.execute(
            """
            select 1
            from messages m
            join message_threads mt on mt.message_id = m.id
            join message_recipients mr on mr.message_id = m.id
            where m.from_agent = ?
              and mt.parent_message_id = ?
              and mr.to_agent = ?
            limit 1
            """,
            (agent_id, message_id, recipient),
        ).fetchone()
        if reply_exists is not None:
            return
        tool = "close_message" if action == "close" else "ack_message"
        if trigger is None:
            raise ValidationError(
                f"cannot {action} message {message_id} with a response: close/ack responses "
                f"are not visible to the sender {recipient!r}. Either {tool} with an empty "
                f"response (lightweight receipt), or first send the response as a threaded "
                f"reply, send_message(to_agents=[{recipient!r}], "
                f"parent_message_id={message_id!r}, ...), then {tool}."
            )
        raise ValidationError(
            f"cannot {action} dispatch trigger {message_id} without first "
            f"replying to the producer {recipient}; call "
            f"send_message(to_agents=[{recipient!r}], "
            f"parent_message_id={message_id!r}, ...) before {tool}"
        )

    def _require_recipient(self, conn: sqlite3.Connection, agent_id: str, message_id: str) -> None:
        self._actors._actor_row_by_id(conn, agent_id)
        row = conn.execute(
            "select 1 from message_recipients where to_agent = ? and message_id = ?",
            (agent_id, message_id),
        ).fetchone()
        if row is None:
            raise ValidationError(f"message {message_id} is not addressed to {agent_id}")

    def _refuse_cancelled_copy(
        self, conn: sqlite3.Connection, agent_id: str, message_id: str, *, action: str
    ) -> None:
        """Refuse read/ack/close-style rewrites of a terminal ``cancelled`` copy.

        A cancelled recipient copy is a withdrawn obligation (a confirmed
        dispatch cancellation or the explicit admin unconfirmed settlement). It
        is terminal like ``closed``: ack/close must not resurrect it or rewrite
        it to a live/closed transport status.
        """
        row = conn.execute(
            "select status from message_recipients where to_agent = ? and message_id = ?",
            (agent_id, message_id),
        ).fetchone()
        if row is not None and row["status"] == "cancelled":
            raise ValidationError(
                f"cannot {action} message {message_id}: the copy for {agent_id} is "
                "cancelled (a withdrawn dispatch) and is terminal"
            )

    def _list_unread_for_wait(self, agent_id: str, after_message_id: str | None, *, full: bool) -> list[dict]:
        with self._db.connection() as conn:
            self._actors._require_actor(conn, agent_id)
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
            payload_refs = {row["id"]: self._payload_ref_for_message(conn, row["id"]) for row in rows}
        return [self._message_row(row, full=full, payload_ref=payload_refs[row["id"]]) for row in rows]

    def _write_semaphores(
        self,
        recipient_roots: dict[str, str],
        to_agents: list[str],
        message_id: str,
        delivered_at: str,
    ) -> None:
        for to_agent in to_agents:
            if to_agent not in recipient_roots:
                continue
            self._write_new_messages_semaphore(
                to_agent,
                Path(recipient_roots[to_agent]),
                message_id,
                delivered_at,
            )

    def _write_new_messages_semaphore(
        self,
        agent_id: str,
        project_root: Path,
        message_id: str,
        delivered_at: str,
    ) -> None:
        # Wake signal only: the authoritative unread set is message_recipients.
        # Last-write-wins is intentional because receivers call list_inbox on wake.
        semaphore_dir = project_root / ".agent-comms" / identity_to_path_segment(agent_id)
        semaphore_dir.mkdir(parents=True, exist_ok=True)
        gitignore_path = semaphore_dir.parent / ".gitignore"
        if not gitignore_path.exists():
            gitignore_temp_path = semaphore_dir.parent / f".gitignore.{os.getpid()}.{uuid4().hex}.tmp"
            gitignore_temp_path.write_text("*\n")
            gitignore_temp_path.replace(gitignore_path)
        semaphore_path = semaphore_dir / "new_messages"
        temp_path = semaphore_dir / f".new_messages.{os.getpid()}.{uuid4().hex}.tmp"
        payload = {
            "agent_id": agent_id,
            "updated_at": utc_now(),
            "messages": [
                {
                    "message_id": message_id,
                    "delivered_at": delivered_at,
                }
            ],
        }
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temp_path.replace(semaphore_path)

    def _payload_ref_for_message(self, conn: sqlite3.Connection, message_id: str) -> sqlite3.Row | None:
        """The additive payload metadata row for a message's dispatch, if any.

        Presence of this row is the ONLY artifact-backed discriminator; the
        fixed body marker is informational and never parsed.
        """
        rows = conn.execute(
            """
            select 'message' as binding_kind, mpr.storage_kind,
                   NULL as payload_origin, mpr.payload_sha256,
                   mpr.byte_count, mpr.char_count, mpr.captured_at
            from message_payload_refs mpr where mpr.message_id = ?
            union all
            select 'dispatch' as binding_kind, dpr.storage_kind,
                   dpr.payload_origin, dpr.payload_sha256,
                   dpr.byte_count, dpr.char_count, dpr.captured_at
            from dispatch_payload_refs dpr
            join dispatch_ledger d on d.dispatch_id = dpr.dispatch_id
            where d.message_id = ?
            """,
            (message_id, message_id),
        ).fetchall()
        if len(rows) > 1:
            raise payload.PayloadError(
                "payload_binding_ambiguous",
                f"message {message_id} has both message and dispatch payload bindings",
            )
        return rows[0] if rows else None

    def _message_row(
        self, row: sqlite3.Row, *, full: bool, payload_ref: sqlite3.Row | None = None
    ) -> dict:
        message = {
            "id": row["id"],
            "from": row["from_agent"],
            "to": row["to_agent"],
            "subject": row["subject"],
            "refs": json.loads(row["refs_json"]),
            "priority": row["priority"],
            "requires_ack": bool(row["requires_ack"]),
            "status": row["status"],
            "parent_message_id": row["parent_message_id"],
            "created_at": row["created_at"],
            "body_chars": len(row["body"]),
        }
        if payload_ref is not None:
            # Bounded artifact-backed projection: recorded metadata plus the
            # fixed snippet. Blob text is never resolved here; exact
            # resolution is exclusive to authenticated read_message, which
            # overlays the verified body itself.
            message["body_storage"] = "artifact"
            message["body_sha256"] = payload_ref["payload_sha256"]
            message["body_bytes"] = payload_ref["byte_count"]
            message["body_chars"] = payload_ref["char_count"]
            message["body_snippet"] = payload.ARTIFACT_SNIPPET
            return message
        message["body_storage"] = "inline"
        if full:
            message["body"] = row["body"]
        else:
            message["body_snippet"] = _fold(row["body"])[:SNIPPET_CHARS]
        return message
