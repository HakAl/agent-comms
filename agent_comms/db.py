from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import sys
from pathlib import Path

from .schema import ValidationError

# Deployed-fleet compatibility floor, not a monotonic schema revision. Additive
# schema changes (`create table if not exists`, `_ensure_column`, and
# `create index if not exists`) must not bump this value. Raise it only for a
# deliberate breaking compatibility-floor change: every prior binary refuses to
# open a default ledger stamped with a newer version, so a bump forcibly
# retires older readers. This is the durable on-disk ledger floor, distinct
# from the dispatch CONTRACT_VERSION in code_identity.py (which tracks the
# consumer-visible dispatch contract of this checkout, not ledger bytes).
#
# Version 2: dispatch payload transport. A payload-capable ledger can hold
# artifact-backed dispatches whose `messages.body` is only the fixed marker; a
# prior (version-1) binary would skip the payload preflight and hand workers
# the marker body as if it were the payload, so payload-capable ledgers must
# make prior binaries refuse rather than silently misread. A default-ledger
# open refuses an existing positive floor below LEDGER_SCHEMA_VERSION before
# any DDL (`_guard_ledger_schema_version`); only an explicit non-default open
# still stamps a lower floor forward (`_stamp_ledger_schema_version`).
#
# Version 3: message payload binding. A file-backed ordinary message also
# stores only the fixed marker in messages.body, so every prior reader must
# refuse rather than return that marker as message content.
LEDGER_SCHEMA_VERSION = 3

# The 0.1.0 release (package version 0.1.0) kept a five-table mailbox with no
# actors and no user_version. Opening one in place would drop its workers
# (see ``_sync_agent_actors``: an agents-table worker without an actors row is
# a declined worker), so a 0.1.0 file is refused everywhere except the import
# path, which migrates a copy in memory with resolved worker owners.
V010_TABLES: dict[str, tuple[str, ...]] = {
    "agents": ("id", "team", "role", "project_root", "capabilities_json", "last_seen_at"),
    "messages": (
        "id", "from_agent", "subject", "body", "refs_json", "priority", "requires_ack", "created_at",
    ),
    "message_recipients": (
        "message_id", "to_agent", "status", "read_at", "acked_at", "closed_at", "ack_response",
    ),
    "message_threads": ("message_id", "parent_message_id"),
    "statuses": (
        "id", "agent_id", "summary", "current_files_json", "blocked_on", "next_step", "created_at",
    ),
}
IMPORT_COMMAND = "import-mailbox"

# The actors shape from before worker ownership. Creating it for a 0.1.0
# mailbox lets ``_migrate_worker_ownership`` run its tested backfill instead
# of the fresh DDL silently declining every worker.
PRE_OWNERSHIP_ACTORS_DDL = """
create table actors(
  id text primary key, kind text not null, display_name text not null,
  system_class text, system_instance text, project_root text, runtime text,
  spawn_json text, capabilities_json text not null default '[]', team text,
  role text, last_seen_at text not null, dispatch_cap integer not null default 4,
  protected integer not null default 0
)
"""


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("select name from sqlite_master where type = 'table'").fetchall()
    }


def application_tables(conn: sqlite3.Connection) -> set[str]:
    """Tables that are not SQLite's own (``sqlite_stat1`` after ANALYZE,
    ``sqlite_sequence`` and so on), which say nothing about the schema."""
    return {name for name in table_names(conn) if not name.startswith("sqlite_")}


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def is_0_1_0_mailbox(conn: sqlite3.Connection) -> bool:
    """True when the 0.1.0 five tables are present, ``actors`` is not, and
    there is no user_version. This is the hazard recognizer for the in-place
    guard: with the five tables and no actors row for its workers, applying
    the schema would decline every worker, so anything else alongside (extra
    columns, tables, triggers or views) does not make the file safe to open.
    The import path applies its own, stricter admission (exactly 0.1.0)."""
    if int(conn.execute("pragma user_version").fetchone()[0]) != 0:
        return False
    tables = application_tables(conn)
    return set(V010_TABLES) <= tables and "actors" not in tables


def backups_dir(db_path: Path) -> Path:
    """Where ``Database.init`` keeps pre-migration copies of ``db_path``."""
    return db_path.parent / "backups"


def backup_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def copy_database(conn: sqlite3.Connection, destination: Path) -> None:
    """Copy the whole database behind ``conn`` into ``destination`` with the
    sqlite backup API (consistent, and includes committed WAL frames)."""
    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
    finally:
        target.close()


def _execute_statements(conn: sqlite3.Connection, script: str) -> None:
    """Run a multi-statement script inside the caller's transaction.

    ``executescript`` commits any open transaction first and autocommits each
    statement, which would break the single-transaction schema application.
    Invariant for every script passed here: each statement ends with ``;`` at
    the end of its line and no line holds two statements, because the split
    is per complete line and ``execute`` takes one statement at a time.
    """
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement)
            statement = ""
    if statement.strip():
        conn.execute(statement)


def schema_shape(conn: sqlite3.Connection) -> dict:
    """The parts of a schema that ``Database.init`` can add: table columns by
    name, foreign-key edges, unique-index column tuples, and named indexes.
    Declared types are left out because no migration changes them."""
    tables: dict[str, dict] = {}
    for table in sorted(application_tables(conn)):
        quoted = quote_identifier(table)
        columns = {row[1] for row in conn.execute(f"pragma table_info({quoted})").fetchall()}
        foreign_keys = {
            (row[3], row[2], row[4])
            for row in conn.execute(f"pragma foreign_key_list({quoted})").fetchall()
        }
        tables[table] = {
            "columns": columns,
            "foreign_keys": foreign_keys,
            "unique": unique_index_columns(conn, table),
        }
    indexes = {
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type = 'index' and sql is not null"
        ).fetchall()
    }
    return {"tables": tables, "indexes": indexes}


def unique_index_columns(conn: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    unique: set[tuple[str, ...]] = set()
    for index_row in conn.execute(f"pragma index_list({quote_identifier(table)})").fetchall():
        if not index_row[2]:
            continue
        unique.add(tuple(
            row[2]
            for row in conn.execute(f"pragma index_info({quote_identifier(index_row[1])})").fetchall()
        ))
    return unique


def needs_idempotency_rebuild(conn: sqlite3.Connection) -> bool:
    """The predicate ``_migrate_idempotency_key_per_producer`` acts on: a
    global unique(idempotency_key) present, or the per-producer one absent."""
    unique = unique_index_columns(conn, "dispatch_ledger")
    return ("idempotency_key",) in unique or ("producer_actor_id", "idempotency_key") not in unique


def needs_message_actor_fk_rebuild(conn: sqlite3.Connection) -> bool:
    """The predicate ``_migrate_message_actor_foreign_keys`` acts on."""
    def targets_actors(table: str, column: str) -> bool:
        return any(
            row[3] == column and row[2] == "actors"
            for row in conn.execute(f"pragma foreign_key_list({quote_identifier(table)})").fetchall()
        )
    return not (targets_actors("messages", "from_agent") and targets_actors("message_recipients", "to_agent"))


def shape_is_missing(on_disk: dict, reference: dict) -> bool:
    """True when ``on_disk`` lacks anything in ``reference``; extras are ignored."""
    for table, expected in reference["tables"].items():
        actual = on_disk["tables"].get(table)
        if actual is None:
            return True
        if not expected["columns"] <= actual["columns"]:
            return True
        if not expected["foreign_keys"] <= actual["foreign_keys"]:
            return True
        if not expected["unique"] <= actual["unique"]:
            return True
    return not reference["indexes"] <= on_disk["indexes"]


_REFERENCE_SHAPE: dict | None = None


def reference_shape() -> dict:
    """The shape this code produces, computed once per process in memory."""
    global _REFERENCE_SHAPE
    if _REFERENCE_SHAPE is None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.row_factory = sqlite3.Row
            Database(Path(":memory:")).apply_schema(conn)
            _REFERENCE_SHAPE = schema_shape(conn)
        finally:
            conn.close()
    return _REFERENCE_SHAPE


def is_declined_worker(conn: sqlite3.Connection, agent_id: str) -> bool:
    """Return whether an agents worker cannot be represented as an owned actor."""
    row = conn.execute(
        """
        select a.role, ac.owner_actor_id
        from agents a
        left join actors ac on ac.id = a.id
        where a.id = ?
        """,
        (agent_id,),
    ).fetchone()
    return row is not None and row["role"] == "worker" and row["owner_actor_id"] is None


class Database:
    def __init__(self, db_path: Path, *, is_default_db_open: bool = False) -> None:
        self.db_path = db_path
        self.is_default_db_open = is_default_db_open
        # Construction is filesystem-effect-free: no parent directory is created
        # here. Directory creation is deferred to ``init()`` (the schema/write
        # gate) so a settlement dry-run's read-only connection and any preflight
        # refusal touch nothing on disk. See T7 mutation-free requirement.
        self._initialized = False

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        return conn

    def read_only_connection(self) -> sqlite3.Connection:
        """Open a strictly read-only connection that observes the live committed WAL.

        Used by the settlement dry-run so a preview reads the CURRENT committed
        on-disk snapshot -- including committed-but-uncheckpointed WAL frames --
        without creating the parent directory, the database file, or the schema,
        and without running schema initialization.

        ``mode=ro`` opens an existing database read-only and never creates it (an
        absent ledger raises rather than materializing one). It deliberately does
        NOT use ``immutable=1``: ``immutable`` tells SQLite the file cannot change,
        so it reads the main database image directly and IGNORES a concurrent
        writer's committed-but-uncheckpointed WAL frames -- which risks minting a
        settlement plan from a STALE snapshot. A normal ``mode=ro`` connection
        instead participates in WAL coordination and observes exactly the latest
        committed state an ordinary reader would. To do so SQLite may read (and,
        when required, create) the ``-wal`` / ``-shm`` sidecars purely to
        coordinate the read; that is SQLite-managed read coordination, not an
        application mutation of ledger content (no row, schema, or user_version is
        touched). ``cache=private`` pins a private page cache (never a
        process-wide shared cache) and ``query_only = on`` is belt-and-suspenders
        against any write.

        An explicit read transaction (``begin deferred`` + a canary read) pins one
        stable committed snapshot across the caller's reads and forces any
        inability to open/read the live WAL to surface HERE, so the preview refuses
        before issuing a plan rather than silently reading a stale image.
        """
        uri = f"file:{self.db_path}?mode=ro&cache=private"
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            # Manage transactions explicitly (autocommit) so the ``begin`` below is
            # the only transaction-control statement and pins the read snapshot.
            conn.isolation_level = None
            conn.execute("pragma query_only = on")
            conn.execute("begin deferred")
            # Force the read snapshot NOW (touching a real schema page) so a WAL
            # that cannot be opened/read safely raises here, before any plan.
            conn.execute("select count(*) from sqlite_master").fetchone()
        except Exception:
            conn.close()
            raise
        return conn

    def open_existing_read_write(self) -> sqlite3.Connection:
        """Open a read-write connection to an EXISTING ledger without creating it.

        ``mode=rw`` opens the database for reading and writing but, unlike the
        default create-on-open connection, refuses (rather than materializing) an
        absent ledger: a missing parent directory or database file raises instead
        of creating any parent, file, schema, or sidecar. Used by settlement
        execution, which must operate only on an existing ledger.
        """
        uri = f"file:{self.db_path}?mode=rw"
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def read_only_connection_ctx(self) -> Iterator[sqlite3.Connection]:
        conn = self.read_only_connection()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def existing_connection(self) -> Iterator[sqlite3.Connection]:
        """Context-managed read-write connection to an EXISTING ledger.

        Mirrors ``connection()`` -- ``with conn`` commits the body on success and
        rolls it back on error, then the connection is closed -- but opens the
        ledger through the non-creating ``mode=rw`` connector instead of the
        ordinary create-on-open ``connect()``. Settlement EXECUTION opens EVERY
        connection (registered-human actor authorization, successful-replay
        lookup, and the terminal transaction) through this context manager, so the
        end-to-end path can only ever operate on an already-existing ledger and
        never falls back to a create-capable connection: an absent database or
        parent -- including a ledger removed BETWEEN execution stages -- refuses
        loudly with a clean ``ValidationError`` and materializes nothing (no
        parent, database file, schema, or ``-wal`` / ``-shm`` sidecar). Only the
        open failure is translated; errors raised inside the transaction body
        propagate unchanged.
        """
        try:
            conn = self.open_existing_read_write()
        except sqlite3.Error as exc:
            raise ValidationError(
                "settlement requires an existing agent-comms ledger at "
                f"{self.db_path}; an absent database/parent refuses without creation"
            ) from exc
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        if self._initialized:
            return
        # First write/schema touch: materialize the parent directory just-in-time
        # (never at construction), so only a genuine write path creates it.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            ledger_schema_version = self._guard_ledger_schema_version(conn)
            if self._needs_migration(conn, ledger_schema_version):
                self._backup_before_migration(conn, ledger_schema_version)
            conn.execute("pragma journal_mode = wal")
            self.apply_schema(conn)
        self._initialized = True

    def _needs_migration(self, conn: sqlite3.Connection, ledger_schema_version: int) -> bool:
        """Would applying the schema change this existing database?

        Three things change a ledger: the version stamp (only ever raised, so
        a newer stamp on an explicit open is not a change), anything in the
        reference shape that is absent, and the rebuild migrations, whose own
        predicates also fire on a forbidden extra (a global unique index on
        idempotency_key next to the per-producer one).
        """
        if not table_names(conn):
            return False
        if ledger_schema_version < LEDGER_SCHEMA_VERSION:
            return True
        if shape_is_missing(schema_shape(conn), reference_shape()):
            return True
        return needs_idempotency_rebuild(conn) or needs_message_actor_fk_rebuild(conn)

    def _backup_before_migration(self, conn: sqlite3.Connection, ledger_schema_version: int) -> Path:
        """Copy the ledger with the sqlite backup API before any DDL runs.

        The name carries the stamp and the pre-migration user_version and is
        never reused: a collision gets a numeric suffix, and the file is
        claimed with an exclusive create so two processes cannot share one.
        """
        folder = backups_dir(self.db_path)
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{self.db_path.name}.{backup_stamp()}.v{ledger_schema_version}"
        attempt = 0
        while True:
            candidate = folder / (f"{stem}.bak" if attempt == 0 else f"{stem}-{attempt}.bak")
            try:
                with open(candidate, "xb"):
                    pass
                break
            except FileExistsError:
                attempt += 1
        try:
            copy_database(conn, candidate)
        except BaseException:
            # Never leave a half-written .bak that looks like a backup.
            for leftover in (candidate, Path(f"{candidate}-journal"), Path(f"{candidate}-wal"), Path(f"{candidate}-shm")):
                leftover.unlink(missing_ok=True)
            raise
        print(
            f"agent-comms: schema migration ahead for {self.db_path}; backup written to {candidate}",
            file=sys.stderr,
        )
        return candidate

    def apply_schema(
        self,
        conn: sqlite3.Connection,
        *,
        declarations: dict[str, str] | None = None,
        legacy_mailbox: bool = False,
    ) -> None:
        """Create or migrate the schema on ``conn`` in one transaction.

        ``journal_mode`` is the caller's business (it cannot change inside a
        transaction). Foreign-key enforcement is off for the duration because
        the rebuild migrations rename and recreate tables; each rebuild runs
        its own ``foreign_key_check``. Any error rolls everything back, so an
        interrupted migration leaves the file as it was.

        ``legacy_mailbox`` admits a 0.1.0 mailbox (see ``is_0_1_0_mailbox``)
        by creating the pre-ownership actors table first, so the worker
        ownership backfill runs with ``declarations`` instead of the fresh
        DDL declining every worker. Only the import path sets it.
        """
        previous_isolation = conn.isolation_level
        conn.isolation_level = None
        conn.execute("pragma foreign_keys = off")
        conn.execute("begin immediate")
        try:
            self._apply_schema_body(conn, declarations, legacy_mailbox)
            conn.execute("commit")
        except BaseException:
            if conn.in_transaction:
                conn.execute("rollback")
            raise
        finally:
            conn.execute("pragma foreign_keys = on")
            conn.isolation_level = previous_isolation

    def _apply_schema_body(
        self,
        conn: sqlite3.Connection,
        declarations: dict[str, str] | None,
        legacy_mailbox: bool,
    ) -> None:
        ledger_schema_version = int(conn.execute("pragma user_version").fetchone()[0])
        if legacy_mailbox and is_0_1_0_mailbox(conn):
            conn.execute(PRE_OWNERSHIP_ACTORS_DDL)
        _execute_statements(
            conn,
            """
                create table if not exists agents(
                  id text primary key,
                  team text not null,
                  role text not null,
                  project_root text not null,
                  capabilities_json text not null,
                  last_seen_at text not null
                );

                create table if not exists actors(
                  id text primary key,
                  kind text not null,
                  display_name text not null,
                  system_class text,
                  system_instance text,
                  project_root text,
                  runtime text,
                  spawn_json text,
                  capabilities_json text not null default '[]',
                  team text,
                  role text,
                  last_seen_at text not null,
                  dispatch_cap integer not null default 4,
                  owner_actor_id text references actors(id) on delete restrict,
                  check ((kind = 'agent' and role = 'worker' and owner_actor_id is not null)
                    or ((kind <> 'agent' or role <> 'worker') and owner_actor_id is null))
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
                  foreign key(from_agent) references actors(id)
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
                  foreign key(to_agent) references actors(id)
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
                  dispatch_id text,
                  thread_ref text,
                  created_at text not null,
                  foreign key(agent_id) references agents(id)
                );

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
                  auth_lineage_key text,
                  auth_lineage_claimed_at text,
                  result text check (case when result is null
                    then policy_version <> 'v2' or status <> 'closed'
                    else policy_version = 'v2' and result in ('satisfied','blocked') and status = 'closed'
                    end),
                  observed_values_json text not null default '{}',
                  unique(producer_actor_id, idempotency_key),
                  foreign key(parent_dispatch_id) references dispatch_ledger(dispatch_id),
                  foreign key(message_id) references messages(id),
                  foreign key(recipient_actor_id) references actors(id),
                  foreign key(producer_actor_id) references actors(id),
                  foreign key(originating_actor_id) references actors(id),
                  foreign key(policy_issued_by) references actors(id)
                );

                create table if not exists monitor_heartbeat(
                  id integer primary key check(id = 1),
                  last_pass_at text,
                  pid integer,
                  interval_seconds real,
                  monitor_version text,
                  last_stale_page_at text
                );

                create table if not exists handoffs(
                  id text primary key,
                  actor_id text not null,
                  body text not null,
                  refs_json text not null,
                  created_at text not null,
                  created_by_actor_id text not null,
                  supersedes_handoff_id text,
                  foreign key(actor_id) references actors(id),
                  foreign key(created_by_actor_id) references actors(id),
                  foreign key(supersedes_handoff_id) references handoffs(id)
                );

                create table if not exists codex_refresh_claims(
                  lineage_key text primary key,
                  holder text,
                  claimed_at text,
                  first_deferred_at text,
                  last_page_at text,
                  dead_reason text,
                  dead_at text,
                  dead_digest text
                );

                create index if not exists idx_message_recipients_inbox
                  on message_recipients(to_agent, status);

                create index if not exists idx_statuses_agent_created
                  on statuses(agent_id, created_at desc);

                create index if not exists idx_dispatch_ledger_status
                  on dispatch_ledger(status, expected_close_by);

                create index if not exists idx_dispatch_ledger_recipient
                  on dispatch_ledger(recipient_actor_id, status);

                create index if not exists idx_handoffs_actor_created
                  on handoffs(actor_id, created_at desc);
            """,
        )
        self._ensure_column(conn, "message_recipients", "closed_at", "text")
        # Additive stage-2 cancellation timestamp on both state machines. It
        # is added BEFORE the legacy rebuild migrations below so the renamed
        # legacy table always carries the column for the insert...select.
        self._ensure_column(conn, "message_recipients", "cancelled_at", "text")
        self._ensure_column(conn, "statuses", "dispatch_id", "text")
        self._ensure_column(conn, "statuses", "thread_ref", "text")
        for column in ("dead_reason", "dead_at", "dead_digest"):
            self._ensure_column(conn, "codex_refresh_claims", column, "text")
        self._ensure_column(conn, "dispatch_ledger", "override_reason", "text")
        self._ensure_column(conn, "dispatch_ledger", "auth_lineage_key", "text")
        self._ensure_column(conn, "dispatch_ledger", "auth_lineage_claimed_at", "text")
        self._ensure_column(
            conn,
            "dispatch_ledger",
            "result",
            "text check (case when result is null then policy_version <> 'v2' or status <> 'closed' "
            "else policy_version = 'v2' and result in ('satisfied','blocked') and status = 'closed' end)",
        )
        self._ensure_column(conn, "dispatch_ledger", "cancelled_at", "text")
        self._ensure_column(conn, "actors", "dispatch_cap", "integer not null default 4")
        self._ensure_column(conn, "actors", "protected", "integer not null default 0")
        self._sync_agent_actors(conn)
        self._migrate_worker_ownership(conn, declarations)
        self._migrate_message_actor_foreign_keys(conn)
        self._migrate_idempotency_key_per_producer(conn)
        # Dispatch payload transport metadata. Presence of a row is the
        # ONLY artifact-backed routing discriminator (never a marker,
        # filename, or body prefix). Created after the legacy rebuild
        # migrations so its foreign key always targets the final
        # dispatch_ledger table. The table create is idempotent, but the
        # payload capability is a breaking floor change: prior binaries
        # must not read a payload-capable ledger, so LEDGER_SCHEMA_VERSION
        # is 2 and `_stamp_ledger_schema_version` advances the on-disk
        # user_version accordingly.
        _execute_statements(
            conn,
            """
            create table if not exists dispatch_payload_refs(
              dispatch_id text primary key references dispatch_ledger(dispatch_id),
              storage_kind text not null check(storage_kind = 'sha256_utf8_v1'),
              payload_origin text not null check(payload_origin in (
                'authored_brief', 'generated_artifact', 'verbatim_source'
              )),
              payload_sha256 text not null,
              byte_count integer not null check(byte_count > 0),
              char_count integer not null check(char_count > 0),
              captured_at text not null
            );
            create table if not exists message_payload_refs(
              message_id text primary key references messages(id),
              storage_kind text not null check(storage_kind = 'sha256_utf8_v1'),
              payload_sha256 text not null,
              byte_count integer not null check(byte_count > 0),
              char_count integer not null check(char_count > 0),
              captured_at text not null
            );
            """
        )
        self.ensure_review_intent_schema(conn, backup=False)
        self.ensure_review_reply_snapshot_schema(conn)
        self._ensure_indexes(conn)
        self._stamp_ledger_schema_version(conn, ledger_schema_version)

    REVIEW_INTENT_INDEXES = ("idx_review_dispatch_intents_state", "idx_review_dispatch_intents_dispatch")

    def review_intent_schema_missing(self, conn: sqlite3.Connection) -> bool:
        if "review_dispatch_intents" not in table_names(conn):
            return True
        indexes = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'index'").fetchall()
        }
        return not set(self.REVIEW_INTENT_INDEXES) <= indexes

    def ensure_review_intent_schema(self, conn: sqlite3.Connection, *, backup: bool = True) -> None:
        """Create the additive review dispatch-intent table and indexes.

        Called outside ``init`` by the review-intent operations on an existing
        ledger, so it keeps the backup-before-migration promise itself: when
        the table or an index is missing from a ledger that already has
        tables, a backup is written first (``backup=False`` only inside
        ``apply_schema``, where ``init`` has already decided). No-op calls
        write nothing.

        Review dispatch-intent binding (dispatch contract 17, review evidence
        lifecycle Landing 1). Additive and idempotent: it never bumps
        LEDGER_SCHEMA_VERSION or the SQLite user_version. The single
        unconditional unique key is (producer_actor_id, idempotency_key); the
        check constraint pins the nullable pre-ledger/bound-state relationship
        so a prepared/active row is pre-ledger (dispatch_id null), an abandoned
        row carries its ``preledger_state='abandoned'`` classification plus an
        ``abandon_reason``, and a bound row carries its queued ledger
        dispatch_id with no residual pre-ledger marker. Expiry reconciliation
        abandons an UNPAIRED prepared row only after a read-only comparison
        against the durable ``dispatched`` JSON companion, so no SQL marker can
        misclassify a durable JSON+prepared crash pair. dispatch_id references
        the ledger row attached in the same transaction that CASes active ->
        bound. Exposed separately from
        ``init()`` so review-side intent operations can guarantee the table on
        an EXISTING ledger without running full schema initialization (which,
        among other things, re-syncs agent actors).
        """
        if backup and table_names(conn) and self.review_intent_schema_missing(conn):
            self._backup_before_migration(
                conn, int(conn.execute("pragma user_version").fetchone()[0])
            )
        _execute_statements(
            conn,
            """
                create table if not exists review_dispatch_intents(
                  producer_actor_id text not null,
                  idempotency_key text not null,
                  intent_id text not null unique,
                  state text not null check(state in (
                    'prepared', 'active', 'bound', 'abandoned'
                  )),
                  preledger_state text check(preledger_state in (
                    'prepared', 'active', 'abandoned'
                  )),
                  dispatch_id text references dispatch_ledger(dispatch_id),
                  recipient_actor_id text not null,
                  real_project_root text not null,
                  policy_name text not null,
                  policy_version text not null,
                  round_kind text not null,
                  digest text not null,
                  payload_json text not null,
                  attempt_count integer not null default 1,
                  abandon_reason text,
                  created_at text not null,
                  updated_at text not null,
                  prepared_at text not null,
                  activated_at text,
                  bound_at text,
                  abandoned_at text,
                  primary key(producer_actor_id, idempotency_key),
                  foreign key(recipient_actor_id) references actors(id),
                  foreign key(producer_actor_id) references actors(id),
                  check(case state
                    when 'prepared' then preledger_state = 'prepared'
                      and dispatch_id is null
                    when 'active' then preledger_state = 'active'
                      and dispatch_id is null
                    when 'bound' then preledger_state is null
                      and dispatch_id is not null
                    when 'abandoned' then preledger_state = 'abandoned'
                      and dispatch_id is null and abandon_reason is not null
                  end)
                );

                create index if not exists idx_review_dispatch_intents_state
                  on review_dispatch_intents(state, updated_at);

                create index if not exists idx_review_dispatch_intents_dispatch
                  on review_dispatch_intents(dispatch_id);
            """
        )

    def ensure_review_reply_snapshot_schema(self, conn: sqlite3.Connection) -> None:
        """Create the additive review reply-snapshot table and its dispatch index.

        Review evidence lifecycle Landing 2: one immutable snapshot row per
        review-bound implementation reply, keyed by ``reply_message_id`` and
        joined to the exact ledger and intent. It is additive and idempotent and
        never bumps LEDGER_SCHEMA_VERSION or the SQLite user_version; existing
        messages, closeouts, and intents remain readable. Check constraints
        require a nonnegative entry count, the implementation round kind, and
        nonempty identity/algorithm fields. Multiple replies may each own a
        snapshot; no column or query selects a "latest" row.
        """
        _execute_statements(
            conn,
            """
                create table if not exists review_reply_snapshots(
                  reply_message_id text primary key references messages(id),
                  dispatch_id text not null references dispatch_ledger(dispatch_id),
                  intent_id text not null references review_dispatch_intents(intent_id),
                  recipient_actor_id text not null references actors(id),
                  round_kind text not null check(round_kind = 'implementation'),
                  base_commit text not null check(length(base_commit) > 0),
                  base_tree text not null check(length(base_tree) > 0),
                  measured_head text not null check(length(measured_head) > 0),
                  snapshot_tree text not null check(length(snapshot_tree) > 0),
                  manifest_sha256 text not null check(length(manifest_sha256) > 0),
                  entry_count integer not null check(entry_count >= 0),
                  status_counts_json text not null,
                  boundary_probe_sha256 text not null check(length(boundary_probe_sha256) > 0),
                  snapshot_algorithm text not null check(length(snapshot_algorithm) > 0),
                  measured_at text not null
                );

                create index if not exists idx_review_reply_snapshots_dispatch
                  on review_reply_snapshots(dispatch_id);
            """
        )

    def _guard_ledger_schema_version(self, conn: sqlite3.Connection) -> int:
        ledger_schema_version = int(conn.execute("pragma user_version").fetchone()[0])
        if is_0_1_0_mailbox(conn):
            raise ValidationError(
                f"{self.db_path} is a 0.1.0 mailbox (five tables, no actors); opening it in place "
                "would drop its workers, so it is not migrated here; import it into a new ledger: "
                f"agent-comms --db <new ledger> {IMPORT_COMMAND} {self.db_path}"
            )
        if self.is_default_db_open and ledger_schema_version > LEDGER_SCHEMA_VERSION:
            raise ValidationError(
                "refusing to open default agent-comms ledger because it was written by a newer "
                "ledger schema; "
                f"ledger_user_version={ledger_schema_version}; "
                f"code_LEDGER_SCHEMA_VERSION={LEDGER_SCHEMA_VERSION}; "
                "upgrade this agent-comms checkout or open an explicit --db/AGENT_COMMS_DB override"
            )
        if (
            self.is_default_db_open
            and 0 < ledger_schema_version < LEDGER_SCHEMA_VERSION
        ):
            # Refusing here, before journal_mode/DDL/migrations/indexes/stamp,
            # keeps a lower-floor canonical ledger logically untouched: an
            # ordinary open must never activate an upward floor cutover.
            raise ValidationError(
                "refusing to open default agent-comms ledger stamped below this checkout's "
                "compatibility floor; "
                f"ledger_user_version={ledger_schema_version}; "
                f"code_LEDGER_SCHEMA_VERSION={LEDGER_SCHEMA_VERSION}; "
                "an ordinary open cannot perform the upward floor cutover; keep using "
                "floor-compatible agent-comms code until the separately governed cutover "
                "mechanism exists"
            )
        return ledger_schema_version

    def _stamp_ledger_schema_version(self, conn: sqlite3.Connection, ledger_schema_version: int) -> None:
        if ledger_schema_version < LEDGER_SCHEMA_VERSION:
            conn.execute(f"pragma user_version = {LEDGER_SCHEMA_VERSION}")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"pragma table_info({quote_identifier(table)})").fetchall()}
        if column not in columns:
            conn.execute(f"alter table {quote_identifier(table)} add column {column} {definition}")

    def _ensure_indexes(self, conn: sqlite3.Connection) -> None:
        _execute_statements(
            conn,
            """
            create index if not exists idx_message_recipients_inbox
              on message_recipients(to_agent, status);

            create index if not exists idx_statuses_agent_created
              on statuses(agent_id, created_at desc);

            create index if not exists idx_dispatch_ledger_status
              on dispatch_ledger(status, expected_close_by);

            create index if not exists idx_dispatch_ledger_recipient
              on dispatch_ledger(recipient_actor_id, status);

            create index if not exists idx_dispatch_ledger_auth_lineage
              on dispatch_ledger(auth_lineage_key, status, auth_lineage_claimed_at);

            create index if not exists idx_codex_refresh_claims_lease
              on codex_refresh_claims(holder, claimed_at);

            create index if not exists idx_handoffs_actor_created
              on handoffs(actor_id, created_at desc);
            """
        )

    def _migrate_worker_ownership(
        self,
        conn: sqlite3.Connection,
        declarations: dict[str, str] | None = None,
    ) -> None:
        """Rebuild actors with enforcing worker ownership, refusing ambiguous backfills."""
        columns = {row["name"] for row in conn.execute("pragma table_info(actors)")}
        if "owner_actor_id" in columns:
            return
        declarations = declarations or {}
        workers = conn.execute(
            "select id, team from actors where kind = 'agent' and role = 'worker' order by id"
        ).fetchall()
        owners: dict[str, str] = {}
        for worker in workers:
            architects = conn.execute(
                "select id from actors where kind = 'agent' and role = 'architect' and team = ? order by id",
                (worker["team"],),
            ).fetchall()
            if len(architects) == 1:
                owners[worker["id"]] = architects[0]["id"]
                continue
            declared = declarations.get(worker["id"])
            declared_owner = conn.execute(
                "select id from actors where id = ? and kind = 'agent' and role = 'architect'",
                (declared,),
            ).fetchone() if declared is not None else None
            if declared_owner is None:
                raise ValidationError(
                    f"worker ownership migration requires an explicit architect declaration for {worker['id']}"
                )
            owners[worker["id"]] = declared_owner["id"]

        # Nested in apply_schema's transaction, or its own when called directly.
        conn.execute("pragma foreign_keys = off")
        conn.execute("pragma legacy_alter_table = on")
        try:
            conn.execute("savepoint worker_ownership")
            conn.execute("alter table actors rename to actors_legacy_worker_ownership")
            conn.execute(
                """
                create table actors(
                  id text primary key, kind text not null, display_name text not null,
                  system_class text, system_instance text, project_root text, runtime text,
                  spawn_json text, capabilities_json text not null default '[]', team text,
                  role text, last_seen_at text not null, dispatch_cap integer not null default 4,
                  protected integer not null default 0,
                  owner_actor_id text references actors(id) on delete restrict,
                  check ((kind = 'agent' and role = 'worker' and owner_actor_id is not null)
                    or ((kind <> 'agent' or role <> 'worker') and owner_actor_id is null))
                )
                """
            )
            legacy_columns = {
                row["name"]
                for row in conn.execute("pragma table_info(actors_legacy_worker_ownership)")
            }
            legacy_expr = {
                "system_class": "system_class" if "system_class" in legacy_columns else "NULL",
                "system_instance": "system_instance" if "system_instance" in legacy_columns else "NULL",
                "runtime": "runtime" if "runtime" in legacy_columns else "NULL",
                "spawn_json": "spawn_json" if "spawn_json" in legacy_columns else "'{}'",
                "dispatch_cap": "dispatch_cap" if "dispatch_cap" in legacy_columns else "4",
                "protected": "protected" if "protected" in legacy_columns else "0",
            }
            conn.executemany(
                f"""
                insert into actors(id, kind, display_name, system_class, system_instance,
                  project_root, runtime, spawn_json, capabilities_json, team, role,
                  last_seen_at, dispatch_cap, protected, owner_actor_id)
                select id, kind, display_name, {legacy_expr['system_class']},
                  {legacy_expr['system_instance']}, project_root, {legacy_expr['runtime']},
                  {legacy_expr['spawn_json']}, capabilities_json, team, role, last_seen_at,
                  {legacy_expr['dispatch_cap']}, {legacy_expr['protected']}, ?
                from actors_legacy_worker_ownership where id = ?
                """,
                [(owners.get(row["id"]), row["id"]) for row in conn.execute(
                    "select id from actors_legacy_worker_ownership order by id"
                ).fetchall()],
            )
            conn.execute("drop table actors_legacy_worker_ownership")
            violations = conn.execute("pragma foreign_key_check").fetchall()
            if violations:
                raise ValidationError(f"worker ownership migration foreign key violations: {violations}")
            conn.execute("release worker_ownership")
        except Exception:
            conn.execute("rollback to worker_ownership")
            conn.execute("release worker_ownership")
            raise
        finally:
            conn.execute("pragma legacy_alter_table = off")
            conn.execute("pragma foreign_keys = on")

    def _migrate_message_actor_foreign_keys(self, conn: sqlite3.Connection) -> None:
        messages_fk_ok = any(
            row["from"] == "from_agent" and row["table"] == "actors"
            for row in conn.execute("pragma foreign_key_list(messages)").fetchall()
        )
        recipients_fk_ok = any(
            row["from"] == "to_agent" and row["table"] == "actors"
            for row in conn.execute("pragma foreign_key_list(message_recipients)").fetchall()
        )
        if messages_fk_ok and recipients_fk_ok:
            return

        conn.execute("pragma foreign_keys = off")
        conn.execute("pragma legacy_alter_table = on")
        try:
            conn.execute("savepoint message_actor_fk")
            _execute_statements(
                conn,
                """
                alter table messages rename to messages_legacy_fk_agents;

                create table messages(
                  id text primary key,
                  from_agent text not null,
                  subject text not null,
                  body text not null,
                  refs_json text not null,
                  priority text not null,
                  requires_ack integer not null,
                  created_at text not null,
                  foreign key(from_agent) references actors(id)
                );

                insert into messages(
                  id, from_agent, subject, body, refs_json, priority, requires_ack, created_at
                )
                select id, from_agent, subject, body, refs_json, priority, requires_ack, created_at
                from messages_legacy_fk_agents;

                alter table message_recipients rename to message_recipients_legacy_fk_agents;

                create table message_recipients(
                  message_id text not null,
                  to_agent text not null,
                  status text not null,
                  read_at text,
                  acked_at text,
                  closed_at text,
                  ack_response text,
                  cancelled_at text,
                  primary key(message_id, to_agent),
                  foreign key(message_id) references messages(id),
                  foreign key(to_agent) references actors(id)
                );

                insert into message_recipients(
                  message_id, to_agent, status, read_at, acked_at, closed_at, ack_response, cancelled_at
                )
                select message_id, to_agent, status, read_at, acked_at, closed_at, ack_response, cancelled_at
                from message_recipients_legacy_fk_agents;

                drop table message_recipients_legacy_fk_agents;
                drop table messages_legacy_fk_agents;
                """,
            )
            conn.execute("release message_actor_fk")
        except Exception:
            conn.execute("rollback to message_actor_fk")
            conn.execute("release message_actor_fk")
            raise
        finally:
            conn.execute("pragma legacy_alter_table = off")
            conn.execute("pragma foreign_keys = on")

    def _migrate_idempotency_key_per_producer(self, conn: sqlite3.Connection) -> None:
        # One shared, quoting reader for the predicate: ``_needs_migration``
        # and this rebuild must agree, and an operator's index with an odd
        # name must not break the open.
        if not needs_idempotency_rebuild(conn):
            return

        residue = conn.execute(
            """
            select name
            from sqlite_master
            where type = 'table' and name = 'dispatch_ledger_legacy_idem'
            """
        ).fetchone()
        if residue is not None:
            raise RuntimeError("found residue table dispatch_ledger_legacy_idem; refusing to overwrite")

        conn.execute("pragma foreign_keys = off")
        conn.execute("pragma legacy_alter_table = on")
        try:
            conn.execute("savepoint idempotency_per_producer")
            pre_count = conn.execute("select count(*) from dispatch_ledger").fetchone()[0]
            _execute_statements(
                conn,
                """
                alter table dispatch_ledger rename to dispatch_ledger_legacy_idem;

                create table dispatch_ledger(
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
                  auth_lineage_key text,
                  auth_lineage_claimed_at text,
                  result text check (case when result is null
                    then policy_version <> 'v2' or status <> 'closed'
                    else policy_version = 'v2' and result in ('satisfied','blocked') and status = 'closed'
                    end),
                  cancelled_at text,
                  observed_values_json text not null default '{}',
                  unique(producer_actor_id, idempotency_key),
                  foreign key(parent_dispatch_id) references dispatch_ledger(dispatch_id),
                  foreign key(message_id) references messages(id),
                  foreign key(recipient_actor_id) references actors(id),
                  foreign key(producer_actor_id) references actors(id),
                  foreign key(originating_actor_id) references actors(id),
                  foreign key(policy_issued_by) references actors(id)
                );

                insert into dispatch_ledger(
                  dispatch_id, parent_dispatch_id, idempotency_key, message_id,
                  thread_ref, spawn_handle, recipient_actor_id, producer_actor_id,
                  originating_actor_id, policy_name, policy_version, policy_issued_by,
                  expected_close_by, status, created_at, spawned_at, closed_at,
                  dlq_at, override_reason, failure_reason, auth_lineage_key,
                  auth_lineage_claimed_at, result, cancelled_at, observed_values_json
                )
                select
                  dispatch_id, parent_dispatch_id, idempotency_key, message_id,
                  thread_ref, spawn_handle, recipient_actor_id, producer_actor_id,
                  originating_actor_id, policy_name, policy_version, policy_issued_by,
                  expected_close_by, status, created_at, spawned_at, closed_at,
                  dlq_at, override_reason, failure_reason, auth_lineage_key,
                  auth_lineage_claimed_at, result, cancelled_at, observed_values_json
                from dispatch_ledger_legacy_idem;
                """,
            )
            post_count = conn.execute("select count(*) from dispatch_ledger").fetchone()[0]
            if post_count != pre_count:
                raise RuntimeError(
                    "dispatch_ledger idempotency migration row count mismatch: "
                    f"before={pre_count} after={post_count}"
                )
            fk_rows = conn.execute("pragma foreign_key_check").fetchall()
            if fk_rows:
                raise RuntimeError(f"dispatch_ledger idempotency migration foreign key check failed: {fk_rows}")
            conn.execute("drop table dispatch_ledger_legacy_idem")
            conn.execute("release idempotency_per_producer")
        except Exception:
            conn.execute("rollback to idempotency_per_producer")
            conn.execute("release idempotency_per_producer")
            raise
        finally:
            conn.execute("pragma legacy_alter_table = off")
            conn.execute("pragma foreign_keys = on")

    def _sync_agent_actors(self, conn: sqlite3.Connection) -> None:
        ownership_enabled = "owner_actor_id" in {
            row["name"] for row in conn.execute("pragma table_info(actors)")
        }
        for row in conn.execute("select * from agents").fetchall():
            if ownership_enabled and is_declined_worker(conn, row["id"]):
                continue
            self._upsert_agent_actor(
                conn,
                row["id"],
                row["team"],
                row["role"],
                row["project_root"],
                json.loads(row["capabilities_json"]),
                row["last_seen_at"],
                ownership_enabled=ownership_enabled,
            )

    def _upsert_agent_actor(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        team: str,
        role: str,
        project_root: str,
        capabilities: list[str],
        last_seen_at: str,
        *,
        ownership_enabled: bool,
    ) -> None:
        if not ownership_enabled:
            conn.execute(
                """
                insert into actors(
                  id, kind, display_name, project_root, runtime, spawn_json,
                  capabilities_json, team, role, last_seen_at
                )
                values(?, 'agent', ?, ?, NULL, '{}', ?, ?, ?, ?)
                on conflict(id) do update set
                  kind = 'agent', display_name = excluded.display_name,
                  project_root = excluded.project_root,
                  capabilities_json = excluded.capabilities_json,
                  team = excluded.team, role = excluded.role,
                  last_seen_at = excluded.last_seen_at
                """,
                (
                    agent_id, agent_id, str(Path(project_root).expanduser().resolve()),
                    json.dumps(capabilities), team, role, last_seen_at,
                ),
            )
            return
        conn.execute(
            """
            insert into actors(
              id, kind, display_name, project_root, runtime, spawn_json,
              capabilities_json, team, role, last_seen_at, owner_actor_id
            )
            values(?, 'agent', ?, ?, NULL, '{}', ?, ?, ?, ?,
                   (select owner_actor_id from actors where id = ?))
            on conflict(id) do update set
              kind = 'agent',
              display_name = excluded.display_name,
              project_root = excluded.project_root,
              capabilities_json = excluded.capabilities_json,
              team = excluded.team,
              role = excluded.role,
              last_seen_at = excluded.last_seen_at
            """,
            (
                agent_id,
                agent_id,
                str(Path(project_root).expanduser().resolve()),
                json.dumps(capabilities),
                team,
                role,
                last_seen_at,
                agent_id,
            ),
        )
