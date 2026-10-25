"""Every schema migration takes a backup first, and an interrupted migration
changes nothing (ac-4ao.7).

``Database.init`` compares the on-disk shape with the reference shape of this
code. When applying the schema would change an existing ledger, it first
writes ``<db dir>/backups/<db name>.<stamp>.v<user_version>.bak`` with the
SQLite backup API. The whole schema sequence then runs in one transaction.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from agent_comms import db as db_module
from agent_comms.db import LEDGER_SCHEMA_VERSION, Database, backups_dir
from tests.substrate.test_cancellation_legacy_rebuild import (
    CANCELLED_AT,
    LEGACY_FK_DDL,
    LEGACY_GLOBAL_IDEM_DDL,
)


def _dump(path: Path) -> tuple[str, int]:
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return "\n".join(conn.iterdump()), conn.execute("pragma user_version").fetchone()[0]


def _tables(path: Path) -> set[str]:
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}


def _backups(path: Path) -> list[Path]:
    folder = backups_dir(path)
    return sorted(folder.iterdir()) if folder.exists() else []


def _write_legacy_fk_ledger(path: Path) -> None:
    """A populated pre-actors-FK ledger: the shape ``_migrate_message_actor_foreign_keys`` rebuilds."""
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(LEGACY_FK_DDL)
        conn.execute(
            "insert into agents(id, team, role, project_root, capabilities_json, last_seen_at)"
            " values('arch', 'team', 'architect', '/srv/p', '[]', ?)",
            (CANCELLED_AT,),
        )
        conn.execute(
            "insert into actors(id, kind, display_name, project_root, team, role, last_seen_at)"
            " values('arch', 'agent', 'arch', '/srv/p', 'team', 'architect', ?)",
            (CANCELLED_AT,),
        )
        conn.execute(
            "insert into messages(id, from_agent, subject, body, refs_json, priority, requires_ack, created_at)"
            " values('m1', 'arch', 'subject', 'body', '[]', 'normal', 0, ?)",
            (CANCELLED_AT,),
        )
        conn.execute(
            "insert into message_recipients(message_id, to_agent, status) values('m1', 'arch', 'sent')"
        )


class BackupBeforeMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "agent-comms.sqlite"

    def init(self) -> str:
        err = io.StringIO()
        with redirect_stderr(err):
            Database(self.path).init()
        return err.getvalue()

    def test_fresh_and_current_ledgers_take_no_backup(self) -> None:
        self.init()
        self.assertEqual(_backups(self.path), [])
        stderr = self.init()
        self.assertEqual(_backups(self.path), [])
        self.assertEqual(stderr, "")
        # A second process (fresh Database object) on a current ledger: still no backup.
        Database(self.path).init()
        self.assertEqual(_backups(self.path), [])

    def test_extra_tables_columns_and_indexes_are_not_a_migration(self) -> None:
        self.init()
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("create table operator_notes(id integer primary key, note text)")
            conn.execute("alter table handoffs add column operator_flag text")
            conn.execute("create index idx_operator on messages(subject)")
        self.init()
        self.assertEqual(_backups(self.path), [])

    def test_legacy_fk_shape_is_backed_up_before_the_rebuild(self) -> None:
        _write_legacy_fk_ledger(self.path)
        before = _dump(self.path)
        stderr = self.init()

        backups = _backups(self.path)
        self.assertEqual(len(backups), 1)
        self.assertIn(str(backups[0]), stderr)
        self.assertRegex(backups[0].name, r"^agent-comms\.sqlite\.[0-9TZ]+\.v0\.bak$")
        self.assertEqual(_dump(backups[0]), before, "the backup is the pre-migration ledger")
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("pragma user_version").fetchone()[0], LEDGER_SCHEMA_VERSION)
            self.assertEqual(conn.execute("select count(*) from messages").fetchone()[0], 1)
        # Migrated once; the next open changes nothing and takes no backup.
        self.init()
        self.assertEqual(len(_backups(self.path)), 1)

    def test_missing_column_or_index_is_backed_up(self) -> None:
        self.init()
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("drop index idx_handoffs_actor_created")
        self.init()
        self.assertEqual(len(_backups(self.path)), 1, "a missing index is a schema change")

        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            if sqlite3.sqlite_version_info < (3, 35, 0):
                self.skipTest("drop column needs SQLite 3.35")
            conn.execute("alter table codex_refresh_claims drop column dead_digest")
        self.init()
        self.assertEqual(len(_backups(self.path)), 2, "a missing column is a schema change")

    def test_global_idempotency_shape_is_backed_up(self) -> None:
        self.init()
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("pragma foreign_keys = off")
            conn.execute("drop table review_reply_snapshots")
            conn.execute("drop table review_dispatch_intents")
            conn.execute("drop table dispatch_payload_refs")
            conn.execute("drop table dispatch_ledger")
            conn.executescript(LEGACY_GLOBAL_IDEM_DDL)
        self.init()
        backups = _backups(self.path)
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0].name, rf"\.v{LEDGER_SCHEMA_VERSION}\.bak$")

    def test_review_intent_path_outside_init_backs_up_first(self) -> None:
        # Lookout F5: reviewing/intents.py adds the intent table without init.
        from agent_comms.reviewing import intents

        self.init()
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("pragma foreign_keys = off")
            conn.execute("drop table review_reply_snapshots")
            conn.execute("drop table review_dispatch_intents")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(intents.reconcile_in_ledger(self.path), [])
        self.assertEqual(len(_backups(self.path)), 1)
        self.assertIn("review_dispatch_intents", _tables(self.path))
        with redirect_stderr(io.StringIO()):
            intents.reconcile_in_ledger(self.path)
        self.assertEqual(len(_backups(self.path)), 1, "a no-op intent operation writes no backup")

    def test_extra_global_unique_index_is_a_rebuild_and_backed_up(self) -> None:
        # Lookout F6: the idempotency rebuild also fires on a forbidden extra.
        self.init()
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("create unique index legacy_global on dispatch_ledger(idempotency_key)")
        self.init()
        self.assertEqual(len(_backups(self.path)), 1)
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            self.assertNotIn("legacy_global", {r[0] for r in conn.execute("select name from sqlite_master where type='index'")})

    def test_failed_commit_leaves_the_object_uninitialized(self) -> None:
        # Lookout F7: a same-object retry after a failed commit must run again.
        _write_legacy_fk_ledger(self.path)
        database = Database(self.path)
        real = Database._apply_schema_body
        calls = []

        def body_then_fail(self_db, conn, declarations, legacy_mailbox):
            real(self_db, conn, declarations, legacy_mailbox)
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("simulated commit failure")

        with mock.patch.object(Database, "_apply_schema_body", body_then_fail):
            with redirect_stderr(io.StringIO()), self.assertRaisesRegex(sqlite3.OperationalError, "simulated"):
                database.init()
            self.assertFalse(database._initialized)
            with redirect_stderr(io.StringIO()):
                database.init()
        self.assertTrue(database._initialized)
        self.assertEqual(_dump(self.path)[1], LEDGER_SCHEMA_VERSION)

    def test_quoted_extra_table_and_index_names_are_tolerated(self) -> None:
        # Lookout F8: identifiers reach the pragmas quoted.
        self.init()
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('create table "operator notes"(note text, "odd ""name" text)')
            conn.execute('create unique index "odd index" on "operator notes"(note)')
            # Lookout round 2: the idempotency rebuild predicate reads dispatch_ledger's indexes too.
            conn.execute('create unique index "odd dispatch index" on dispatch_ledger(thread_ref)')
        self.init()
        self.assertEqual(_backups(self.path), [])
        self.assertIn('"odd dispatch index"', _dump(self.path)[0])

    def test_newer_version_on_an_explicit_open_is_not_a_migration(self) -> None:
        # Lookout F9: the stamp is only ever raised, so a newer stamp changes nothing.
        self.init()
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(f"pragma user_version = {LEDGER_SCHEMA_VERSION + 1}")
        for _ in range(2):
            self.init()
        self.assertEqual(_backups(self.path), [])
        self.assertEqual(_dump(self.path)[1], LEDGER_SCHEMA_VERSION + 1)

    def test_backups_never_overwrite(self) -> None:
        _write_legacy_fk_ledger(self.path)
        with mock.patch.object(db_module, "backup_stamp", return_value="20260101T000000Z"):
            self.init()
            with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute("drop index idx_handoffs_actor_created")
            self.init()
        names = [p.name for p in _backups(self.path)]
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all("20260101T000000Z" in name for name in names))

    def test_a_failed_backup_leaves_no_bak_file_and_no_migration(self) -> None:
        _write_legacy_fk_ledger(self.path)
        before = _dump(self.path)
        with mock.patch.object(db_module, "copy_database", side_effect=OSError("disk full")):
            with redirect_stderr(io.StringIO()), self.assertRaisesRegex(OSError, "disk full"):
                Database(self.path).init()
        self.assertEqual(_backups(self.path), [], "a half-written .bak must not look like a backup")
        self.assertEqual(_dump(self.path), before, "no migration without a backup")

    def test_restoring_a_backup_brings_the_old_rows_back(self) -> None:
        _write_legacy_fk_ledger(self.path)
        before = _dump(self.path)
        self.init()
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("delete from message_recipients")
            conn.execute("delete from messages")
        backup = _backups(self.path)[0]

        # The documented procedure: stop writers, copy the backup over the ledger, drop the sidecars.
        shutil.copy(backup, self.path)
        for sidecar in ("-wal", "-shm"):
            Path(f"{self.path}{sidecar}").unlink(missing_ok=True)
        self.assertEqual(_dump(self.path), before)
        self.init()
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("select count(*) from messages").fetchone()[0], 1)


class InterruptedMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "agent-comms.sqlite"

    def test_abort_midway_leaves_the_ledger_as_it_was_and_a_rerun_completes(self) -> None:
        _write_legacy_fk_ledger(self.path)
        before = _dump(self.path)
        original = Database._migrate_message_actor_foreign_keys

        def explode(self_db, conn):
            # Half of the rebuild has run when the process dies.
            conn.execute("alter table messages rename to messages_legacy_fk_agents")
            raise RuntimeError("simulated crash during migration")

        with mock.patch.object(Database, "_migrate_message_actor_foreign_keys", explode):
            with redirect_stderr(io.StringIO()), self.assertRaisesRegex(RuntimeError, "simulated crash"):
                Database(self.path).init()

        backups = _backups(self.path)
        self.assertEqual(len(backups), 1, "the backup was taken before any DDL")
        self.assertEqual(_dump(backups[0]), before)
        self.assertEqual(_dump(self.path), before, "the aborted migration changed nothing")
        self.assertFalse({t for t in _tables(self.path) if "legacy" in t})

        with mock.patch.object(Database, "_migrate_message_actor_foreign_keys", original):
            with redirect_stderr(io.StringIO()):
                Database(self.path).init()
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("pragma user_version").fetchone()[0], LEDGER_SCHEMA_VERSION)
            self.assertEqual(conn.execute("select count(*) from messages").fetchone()[0], 1)
            self.assertEqual(conn.execute("pragma foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
