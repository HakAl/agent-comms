import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_comms.db import Database
from agent_comms.schema import ValidationError


V105_DISPATCH_LEDGER_DDL = """
create table if not exists dispatch_ledger(
  dispatch_id text primary key,
  parent_dispatch_id text,
  idempotency_key text not null,
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
  observed_values_json text not null default '{}',
  unique(producer_actor_id, idempotency_key),
  foreign key(parent_dispatch_id) references dispatch_ledger(dispatch_id),
  foreign key(message_id) references messages(id),
  foreign key(recipient_actor_id) references actors(id),
  foreign key(producer_actor_id) references actors(id),
  foreign key(originating_actor_id) references actors(id),
  foreign key(policy_issued_by) references actors(id)
);
"""


def dispatch_ledger_columns(db_path: Path) -> set[str]:
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        return {row[1] for row in conn.execute("pragma table_info(dispatch_ledger)").fetchall()}


def has_auth_lineage_index(db_path: Path) -> bool:
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """
            select 1
            from sqlite_master
            where type = 'index'
              and name = 'idx_dispatch_ledger_auth_lineage'
            """
        ).fetchone()
    return row is not None


def build_v105_fixture(db_path: Path) -> None:
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.executescript(V105_DISPATCH_LEDGER_DDL)


class DatabaseInitUpgradeTest(unittest.TestCase):
    def test_init_upgrades_v105_dispatch_ledger_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent-comms.sqlite"
            build_v105_fixture(db_path)

            self.assertNotIn("auth_lineage_key", dispatch_ledger_columns(db_path))

            Database(db_path).init()

            columns = dispatch_ledger_columns(db_path)
            self.assertIn("auth_lineage_key", columns)
            self.assertIn("auth_lineage_claimed_at", columns)
            self.assertTrue(has_auth_lineage_index(db_path))

            Database(db_path).init()

            columns = dispatch_ledger_columns(db_path)
            self.assertIn("auth_lineage_key", columns)
            self.assertIn("auth_lineage_claimed_at", columns)
            self.assertTrue(has_auth_lineage_index(db_path))

    def test_init_creates_auth_lineage_columns_and_index_on_fresh_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent-comms.sqlite"

            Database(db_path).init()

            columns = dispatch_ledger_columns(db_path)
            self.assertIn("auth_lineage_key", columns)
            self.assertIn("auth_lineage_claimed_at", columns)
            self.assertTrue(has_auth_lineage_index(db_path))

    def test_upgraded_auth_lineage_columns_are_usable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent-comms.sqlite"
            build_v105_fixture(db_path)
            Database(db_path).init()

            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                with conn:
                    conn.execute("pragma foreign_keys = on")
                    conn.execute(
                        """
                        insert into actors(id, kind, display_name, last_seen_at)
                        values('actor-1', 'agent', 'Actor One', '2026-07-09T00:00:00+00:00')
                        """
                    )
                    conn.execute(
                        """
                        insert into dispatch_ledger(
                          dispatch_id,
                          idempotency_key,
                          thread_ref,
                          recipient_actor_id,
                          producer_actor_id,
                          originating_actor_id,
                          policy_name,
                          policy_version,
                          policy_issued_by,
                          status,
                          created_at,
                          auth_lineage_key,
                          auth_lineage_claimed_at
                        )
                        values(
                          'dispatch-1',
                          'key-1',
                          'thread-1',
                          'actor-1',
                          'actor-1',
                          'actor-1',
                          'worker_dispatch_readwrite_bounded',
                          '1',
                          'actor-1',
                          'running',
                          '2026-07-09T00:00:00+00:00',
                          'lineage-1',
                          '2026-07-09T00:00:01+00:00'
                        )
                        """
                    )
                    conn.execute(
                        """
                        update dispatch_ledger
                        set auth_lineage_key = ?,
                            auth_lineage_claimed_at = ?
                        where dispatch_id = ?
                        """,
                        ("lineage-2", "2026-07-09T00:00:02+00:00", "dispatch-1"),
                    )
                    row = conn.execute(
                        """
                        select auth_lineage_key, auth_lineage_claimed_at
                        from dispatch_ledger
                        where dispatch_id = 'dispatch-1'
                        """
                    ).fetchone()

            self.assertEqual(tuple(row), ("lineage-2", "2026-07-09T00:00:02+00:00"))


class DatabaseConstructionFilesystemPurityTest(unittest.TestCase):
    """Constructing a Database is filesystem-effect-free; only init() creates dirs.

    T7 mutation-free requirement: a settlement credential/CLI refusal and a
    read-only dry-run construct a Database (via Store) but must touch nothing on
    disk. Directory creation is therefore deferred from construction to the first
    write/schema touch in ``init()``.
    """

    def test_construction_does_not_create_parent_directory_or_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            absent_parent = Path(tmp) / "missing" / "nested"
            db_path = absent_parent / "agent-comms.sqlite"

            db = Database(db_path)
            # Construction created NOTHING: no parent directory, no database file,
            # and no WAL/SHM sidecar.
            self.assertFalse(absent_parent.exists())
            self.assertFalse(db_path.exists())
            self.assertFalse(db_path.with_name("agent-comms.sqlite-wal").exists())
            self.assertFalse(db_path.with_name("agent-comms.sqlite-shm").exists())

            # The first write/schema touch (init) materializes the directory and DB.
            db.init()
            self.assertTrue(absent_parent.exists())
            self.assertTrue(db_path.exists())

    def test_read_only_connection_never_creates_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agent-comms.sqlite"
            db = Database(db_path)
            # A read-only connection against an absent ledger raises rather than
            # materializing the file, so an absent-ledger dry-run stays pure.
            with self.assertRaises(sqlite3.OperationalError):
                db.read_only_connection()
            self.assertFalse(db_path.exists())


class ReadOnlyConnectionLiveWalTest(unittest.TestCase):
    """The settlement dry-run read-only connection observes the LIVE committed WAL.

    Regression: an ``immutable=1`` connection reads the STALE main database image
    and ignores a concurrent writer's committed-but-uncheckpointed WAL frames, so
    a preview could mint a settlement plan from a stale snapshot. A ``mode=ro``
    connection instead observes exactly the latest committed state an ordinary
    reader sees. This is the exact uncheckpointed-WAL reproduction at the DB seam.
    """

    def test_read_only_connection_observes_committed_uncheckpointed_wal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agent-comms.sqlite"
            db = Database(db_path)
            db.init()
            # Seed a row and checkpoint it into the MAIN image, emptying the WAL.
            with db.connection() as conn:
                conn.execute(
                    "insert into actors(id, kind, display_name, last_seen_at) "
                    "values('a1', 'agent', 'old-main-image', '2026-07-18T00:00:00+00:00')"
                )
            with db.connection() as conn:
                conn.execute("pragma wal_checkpoint(truncate)")

            # A writer commits a CONFLICTING update ONLY into the WAL and stays
            # open with autocheckpoint disabled, so the committed value is never
            # folded into the main database image.
            writer = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
            self.addCleanup(writer.close)
            writer.execute("pragma wal_autocheckpoint = 0")
            writer.execute("begin immediate")
            writer.execute(
                "update actors set display_name = 'WAL-CURRENT-COMMITTED' where id = 'a1'"
            )
            writer.execute("commit")

            # An ordinary live reader observes the NEW committed value (via the WAL).
            ordinary = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            try:
                self.assertEqual(
                    ordinary.execute(
                        "select display_name from actors where id = 'a1'"
                    ).fetchone()[0],
                    "WAL-CURRENT-COMMITTED",
                )
            finally:
                ordinary.close()

            # The regression itself: an ``immutable=1`` connection reads the STALE
            # main image, ignoring the committed WAL frame.
            immutable = sqlite3.connect(
                f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=10
            )
            try:
                self.assertEqual(
                    immutable.execute(
                        "select display_name from actors where id = 'a1'"
                    ).fetchone()[0],
                    "old-main-image",
                )
            finally:
                immutable.close()

            # The fixed read-only connection observes the committed WAL value, so a
            # preview built on it can never mint a plan from the stale main image.
            conn = db.read_only_connection()
            try:
                self.assertEqual(
                    conn.execute(
                        "select display_name from actors where id = 'a1'"
                    ).fetchone()[0],
                    "WAL-CURRENT-COMMITTED",
                )
            finally:
                conn.close()


class ExistingConnectionNonCreatingTest(unittest.TestCase):
    """The mode=rw existing-ledger connector settlement execution reopens with.

    T7 end-to-end requirement: settlement execution opens EVERY connection through
    ``existing_connection()`` (non-creating ``mode=rw``), never the create-capable
    connector. Each of these is a distinct reopen stage; a ledger absent at ANY of
    them -- including one removed between stages -- must refuse loudly and recreate
    nothing, while an ordinary authorized ``init()`` write path still materializes
    the parent and schema.
    """

    def _sidecars(self, db_path: Path) -> list[Path]:
        return [
            db_path.with_name(db_path.name + "-wal"),
            db_path.with_name(db_path.name + "-shm"),
        ]

    def _assert_nothing_on_disk(self, parent: Path, db_path: Path) -> None:
        self.assertFalse(db_path.exists())
        for sidecar in self._sidecars(db_path):
            self.assertFalse(sidecar.exists())
        if parent.exists():
            self.assertEqual(list(parent.iterdir()), [])

    def test_existing_connection_refuses_absent_ledger_and_parent_without_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # An absent ledger under an existing parent: the raw connector raises
            # and the context manager translates it into a clean ValidationError
            # (the CLI maps it to a refusal), materializing nothing.
            db_path = Path(tmp) / "agent-comms.sqlite"
            db = Database(db_path)
            with self.assertRaises(sqlite3.OperationalError):
                db.open_existing_read_write()
            with self.assertRaises(ValidationError):
                with db.existing_connection():
                    pass
            self._assert_nothing_on_disk(Path(tmp), db_path)

            # An absent PARENT directory refuses identically and creates no parent.
            absent_parent = Path(tmp) / "missing" / "nested"
            nested_db = absent_parent / "agent-comms.sqlite"
            with self.assertRaises(ValidationError):
                with Database(nested_db).existing_connection():
                    pass
            self.assertFalse(absent_parent.exists())
            self.assertFalse(nested_db.exists())

    def test_existing_connection_reopen_after_deletion_refuses_without_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agent-comms.sqlite"
            db = Database(db_path)
            db.init()
            self.assertTrue(db_path.exists())

            # First reopen stage succeeds against the live ledger and writes.
            with db.existing_connection() as conn:
                conn.execute(
                    "insert into actors(id, kind, display_name, last_seen_at) "
                    "values('a1', 'agent', 'A One', '2026-07-18T00:00:00+00:00')"
                )

            # The ledger is removed BETWEEN reopen stages (its sidecars too).
            db_path.unlink()
            for sidecar in self._sidecars(db_path):
                if sidecar.exists():
                    sidecar.unlink()

            # The next reopen stage refuses loudly and NEVER recreates the database
            # (or a schema/sidecar) through a create-capable fallback.
            with self.assertRaises(ValidationError):
                with db.existing_connection() as conn:
                    conn.execute("select count(*) from actors")
            self.assertFalse(db_path.exists())
            for sidecar in self._sidecars(db_path):
                self.assertFalse(sidecar.exists())

    def test_init_still_creates_parent_and_schema_for_authorized_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            absent_parent = Path(tmp) / "missing" / "nested"
            db_path = absent_parent / "agent-comms.sqlite"
            db = Database(db_path)

            # An unauthorized existing-ledger reopen refuses and creates nothing...
            with self.assertRaises(ValidationError):
                with db.existing_connection():
                    pass
            self.assertFalse(absent_parent.exists())

            # ...but the ordinary authorized write path (init) still materializes
            # the parent directory and the full schema in place.
            db.init()
            self.assertTrue(absent_parent.exists())
            self.assertTrue(db_path.exists())
            self.assertIn("auth_lineage_key", dispatch_ledger_columns(db_path))

            # And once the ledger exists, the non-creating connector reopens it.
            with db.existing_connection() as conn:
                self.assertEqual(
                    conn.execute("select count(*) from dispatch_ledger").fetchone()[0], 0
                )


if __name__ == "__main__":
    unittest.main()
