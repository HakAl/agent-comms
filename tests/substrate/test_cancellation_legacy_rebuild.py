"""Stage-2 T2: additive ``cancelled_at`` columns survive every legacy
table-copy/rebuild migration path.

``db.Database.init`` still contains two historical rebuild migrations that copy
a whole table into a freshly-created one:

- ``_migrate_message_actor_foreign_keys`` rebuilds ``messages`` and
  ``message_recipients`` to repoint their foreign keys at ``actors``;
- ``_migrate_idempotency_key_per_producer`` rebuilds ``dispatch_ledger`` to
  swap a global ``idempotency_key`` unique index for the per-producer one.

The reviewed brief requires the additive ``cancelled_at`` columns (and their
data) to be preserved through both copies rather than silently dropped. These
tests construct a legacy-shaped DB that already carries a ``cancelled_at`` value
AND the condition that triggers the rebuild, then run ``init`` and assert the
value survived and the migration still completed.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_comms.db import Database


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}


def _fk_targets(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[2] for row in conn.execute(f"pragma foreign_key_list({table})").fetchall()}


def _unique_index_columns(conn: sqlite3.Connection, table: str) -> list[list[str]]:
    result: list[list[str]] = []
    for index_row in conn.execute(f"pragma index_list({table})").fetchall():
        if not index_row["unique"]:
            continue
        cols = [r["name"] for r in conn.execute(f"pragma index_info({index_row['name']})").fetchall()]
        result.append(cols)
    return result


# Old shape: messages/message_recipients whose foreign keys still point at the
# legacy ``agents`` table. A ``cancelled_at`` column is already present with a
# non-null value, modelling a rolling window where a newer node wrote a
# cancellation before this even-older-FK artifact was rebuilt.
LEGACY_FK_DDL = """
create table agents(
  id text primary key, team text not null, role text not null,
  project_root text not null, capabilities_json text not null, last_seen_at text not null
);
create table actors(
  id text primary key, kind text not null, display_name text not null,
  system_class text, system_instance text, project_root text, runtime text, spawn_json text,
  capabilities_json text not null default '[]', team text, role text,
  last_seen_at text not null, dispatch_cap integer not null default 4
);
create table messages(
  id text primary key, from_agent text not null, subject text not null, body text not null,
  refs_json text not null, priority text not null, requires_ack integer not null, created_at text not null,
  foreign key(from_agent) references agents(id)
);
create table message_recipients(
  message_id text not null, to_agent text not null, status text not null,
  read_at text, acked_at text, closed_at text, ack_response text, cancelled_at text,
  primary key(message_id, to_agent),
  foreign key(message_id) references messages(id),
  foreign key(to_agent) references agents(id)
);
"""


# Old shape: dispatch_ledger with a GLOBAL unique(idempotency_key) index (the
# pre-per-producer shape) plus an already-present cancelled_at value.
LEGACY_GLOBAL_IDEM_DDL = """
create table dispatch_ledger(
  dispatch_id text primary key,
  parent_dispatch_id text,
  idempotency_key text not null unique,
  message_id text unique,
  thread_ref text not null,
  spawn_handle text,
  recipient_actor_id text not null,
  producer_actor_id text not null,
  originating_actor_id text not null,
  policy_name text not null,
  policy_version text not null,
  policy_issued_by text not null,
  expected_close_by text,
  status text not null,
  created_at text not null,
  spawned_at text,
  closed_at text,
  dlq_at text,
  override_reason text,
  failure_reason text,
  auth_lineage_key text,
  auth_lineage_claimed_at text,
  cancelled_at text,
  observed_values_json text not null default '{}',
  foreign key(parent_dispatch_id) references dispatch_ledger(dispatch_id),
  foreign key(message_id) references messages(id),
  foreign key(recipient_actor_id) references actors(id),
  foreign key(producer_actor_id) references actors(id),
  foreign key(originating_actor_id) references actors(id),
  foreign key(policy_issued_by) references actors(id)
);
"""

CANCELLED_AT = "2026-07-15T12:00:00+00:00"


class MessageRecipientsRebuildPreservesCancelledAtTest(unittest.TestCase):
    def test_fk_rebuild_carries_cancelled_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agent-comms.sqlite"
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                with conn:
                    conn.executescript(LEGACY_FK_DDL)
                    conn.execute(
                        "insert into agents(id, team, role, project_root, capabilities_json, last_seen_at)"
                        " values('arch', 'team', 'architect', ?, '[]', ?)",
                        (str(tmp), CANCELLED_AT),
                    )
                    conn.execute(
                        "insert into agents(id, team, role, project_root, capabilities_json, last_seen_at)"
                        " values('wrk', 'team', 'worker', ?, '[]', ?)",
                        (str(tmp), CANCELLED_AT),
                    )
                    conn.execute(
                        "insert into messages(id, from_agent, subject, body, refs_json, priority,"
                        " requires_ack, created_at) values('m1', 'wrk', 's', 'b', '[]', 'normal', 0, ?)",
                        (CANCELLED_AT,),
                    )
                    conn.execute(
                        "insert into message_recipients(message_id, to_agent, status, cancelled_at)"
                        " values('m1', 'wrk', 'cancelled', ?)",
                        (CANCELLED_AT,),
                    )
                # Precondition: the legacy FK points at agents, not actors.
                self.assertIn("agents", _fk_targets(conn, "message_recipients"))

            Database(db_path).init()

            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                # The rebuild happened (FK now points at actors) ...
                self.assertIn("actors", _fk_targets(conn, "message_recipients"))
                self.assertNotIn("agents", _fk_targets(conn, "message_recipients"))
                # ... and the additive column plus its value survived the copy.
                self.assertIn("cancelled_at", _columns(conn, "message_recipients"))
                row = conn.execute(
                    "select status, cancelled_at from message_recipients where message_id = 'm1'"
                ).fetchone()
                self.assertEqual(row["status"], "cancelled")
                self.assertEqual(row["cancelled_at"], CANCELLED_AT)


class DispatchLedgerRebuildPreservesCancelledAtTest(unittest.TestCase):
    def test_idempotency_rebuild_carries_cancelled_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agent-comms.sqlite"
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                with conn:
                    # Minimal supported actor shape so the rebuilt FK check passes.
                    conn.executescript(
                        "create table actors(id text primary key, kind text not null,"
                        " display_name text not null, system_class text, system_instance text,"
                        " project_root text, runtime text, spawn_json text,"
                        " capabilities_json text not null default '[]', team text, role text,"
                        " last_seen_at text not null,"
                        " dispatch_cap integer not null default 4);"
                    )
                    conn.execute(
                        "insert into actors(id, kind, display_name, project_root, team, role, last_seen_at)"
                        " values('a', 'agent', 'A', ?, 'team', 'architect', ?)",
                        (str(tmp), CANCELLED_AT),
                    )
                    conn.executescript(LEGACY_GLOBAL_IDEM_DDL)
                    conn.execute(
                        """
                        insert into dispatch_ledger(
                          dispatch_id, idempotency_key, thread_ref, recipient_actor_id,
                          producer_actor_id, originating_actor_id, policy_name, policy_version,
                          policy_issued_by, status, created_at, cancelled_at, observed_values_json
                        ) values('d1', 'k1', 't1', 'a', 'a', 'a',
                                 'worker_dispatch_readwrite_bounded', 'v1', 'a', 'cancelled', ?, ?, '{}')
                        """,
                        (CANCELLED_AT, CANCELLED_AT),
                    )
                # Precondition: a GLOBAL unique(idempotency_key) index exists.
                self.assertIn(["idempotency_key"], _unique_index_columns(conn, "dispatch_ledger"))

            Database(db_path).init()

            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                unique_cols = _unique_index_columns(conn, "dispatch_ledger")
                # The per-producer composite is now present and the bare global
                # idempotency index is gone: the rebuild ran.
                self.assertIn(["producer_actor_id", "idempotency_key"], unique_cols)
                self.assertNotIn(["idempotency_key"], unique_cols)
                # ... and cancelled_at plus its value survived the copy.
                self.assertIn("cancelled_at", _columns(conn, "dispatch_ledger"))
                row = conn.execute(
                    "select status, cancelled_at from dispatch_ledger where dispatch_id = 'd1'"
                ).fetchone()
                self.assertEqual(row["status"], "cancelled")
                self.assertEqual(row["cancelled_at"], CANCELLED_AT)


if __name__ == "__main__":
    unittest.main()
