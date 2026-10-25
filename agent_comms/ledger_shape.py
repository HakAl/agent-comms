"""What a ledger file looks like, read through SQLite's pragmas.

Pure functions over an open connection: the application tables, their shape
(columns, foreign keys, unique indexes), the predicates the rebuild
migrations act on, whether a reference shape is missing from an on-disk one,
the 0.1.0 mailbox recognizer, and the backup file helpers ``Database.init``
uses before a migration. Nothing here writes to the ledger.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

# The tables the 0.1.0 release kept (no actors, no user_version); see the
# guard in db.py and the import in mailbox_import.py.
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


def execute_statements(conn: sqlite3.Connection, script: str) -> None:
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


def reference_shape_from(apply_schema: Callable[[sqlite3.Connection], None]) -> dict:
    """The shape ``apply_schema`` produces on an empty in-memory database."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.row_factory = sqlite3.Row
        apply_schema(conn)
        return schema_shape(conn)
    finally:
        conn.close()
