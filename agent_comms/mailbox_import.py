"""Import a 0.1.0 mailbox into a new ledger (``agent-comms import-mailbox``).

The 0.1.0 release stored five tables and no actors at
``<checkout>/data/agent-comms.sqlite``. Opening such a file with current code
would run the schema in place and drop every worker (an agents-table worker
without an actors row is a declined worker), so ``Database`` refuses it and
this module is the only path in: validate the source read-only, resolve each
worker's owner, migrate a copy in memory, check it, then write the target.
The source is never written.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .db import LEDGER_SCHEMA_VERSION, Database
from .ledger_shape import (
    IMPORT_COMMAND,
    V010_TABLES,
    application_tables,
    quote_identifier,
    table_names,
    unique_index_columns,
)
from .schema import ValidationError

OWNER_FLAG_SYNTAX = "--owner WORKER=ARCHITECT"

# The schema the 0.1.0 release wrote (its ``Database.init``), kept verbatim so
# admission can compare keys and constraints, not just column names: the
# tables that survive migration keep whatever constraints the source had, and
# ``Database.init`` cannot add a missing primary key or foreign key later.
V010_SCHEMA_DDL = """
create table agents(
  id text primary key,
  team text not null,
  role text not null,
  project_root text not null,
  capabilities_json text not null,
  last_seen_at text not null
);
create table messages(
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
create table message_recipients(
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
create table message_threads(
  message_id text primary key,
  parent_message_id text,
  foreign key(message_id) references messages(id),
  foreign key(parent_message_id) references messages(id)
);
create table statuses(
  id text primary key,
  agent_id text not null,
  summary text not null,
  current_files_json text not null,
  blocked_on text,
  next_step text,
  created_at text not null,
  foreign key(agent_id) references agents(id)
);
"""
_V010_CONSTRAINTS: dict[str, dict] | None = None


def normalize_sql(sql: str) -> str:
    """Case and whitespace folded so two spellings of one definition compare equal."""
    text = re.sub(r"\s+", " ", sql.strip().lower())
    return re.sub(r"\s*([(),])\s*", r"\1", text)


def table_constraints(conn: sqlite3.Connection, table: str) -> dict:
    """Columns in order with type, not-null, default and primary-key position;
    foreign-key edges with their actions; unique column tuples; and the
    normalized ``create table`` text, which is the only place a CHECK clause
    (or anything else the pragmas do not report) shows up."""
    quoted = quote_identifier(table)
    definition = conn.execute(
        "select sql from sqlite_master where type = 'table' and name = ?", (table,)
    ).fetchone()
    return {
        "columns": tuple(
            (row[1], (row[2] or "").lower(), bool(row[3]), row[4], row[5])
            for row in conn.execute(f"pragma table_info({quoted})").fetchall()
        ),
        "foreign keys": sorted(
            (row[3], row[2], row[4], row[5], row[6])
            for row in conn.execute(f"pragma foreign_key_list({quoted})").fetchall()
        ),
        # An index on an expression reports no column name; keep it comparable.
        "unique constraints": sorted(
            tuple(column if column is not None else "<expression>" for column in columns)
            for columns in unique_index_columns(conn, table)
        ),
        "definition": normalize_sql(definition[0]) if definition and definition[0] else "",
    }


def v010_constraints() -> dict[str, dict]:
    global _V010_CONSTRAINTS
    if _V010_CONSTRAINTS is None:
        reference = sqlite3.connect(":memory:")
        try:
            reference.executescript(V010_SCHEMA_DDL)
            _V010_CONSTRAINTS = {table: table_constraints(reference, table) for table in V010_TABLES}
        finally:
            reference.close()
    return _V010_CONSTRAINTS


@dataclass(frozen=True)
class ImportPlan:
    source: Path
    target: Path
    owners: dict[str, str]
    declarations: dict[str, str] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)


def parse_owner_flags(values: list[str] | None) -> dict[str, str]:
    declarations: dict[str, str] = {}
    for value in values or []:
        worker, separator, architect = value.partition("=")
        if not separator or not worker.strip() or not architect.strip():
            raise ValidationError(f"{OWNER_FLAG_SYNTAX} expected, got --owner {value!r}")
        if worker.strip() in declarations:
            raise ValidationError(f"--owner names {worker.strip()} twice")
        declarations[worker.strip()] = architect.strip()
    return declarations


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValidationError(f"0.1.0 mailbox {path} does not exist or is not a file")
    # A percent-encoded file URI: a literal '#' or '?' in the name would
    # otherwise end the path early and open (or create) a different file.
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("select count(*) from sqlite_master").fetchone()
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise ValidationError(f"{path} does not open as a SQLite database: {exc}") from exc
    return conn


def _version_mismatch(conn: sqlite3.Connection) -> str | None:
    user_version = int(conn.execute("pragma user_version").fetchone()[0])
    if user_version != 0:
        return f"user_version is {user_version}; a 0.1.0 mailbox has none (0)"
    return None


def _table_set_mismatch(conn: sqlite3.Connection) -> str | None:
    tables = application_tables(conn)
    expected = set(V010_TABLES)
    missing = sorted(expected - tables)
    extra = sorted(tables - expected)
    # Only SQLite's own bookkeeping tables may sit beside the five.
    extra += sorted(
        name for name in table_names(conn) - tables
        if not name.startswith("sqlite_stat") and name != "sqlite_sequence"
    )
    parts = []
    if missing:
        parts.append(f"missing tables: {', '.join(missing)}")
    if extra:
        parts.append(f"unexpected tables: {', '.join(extra)}")
    if not parts:
        return None
    return "; ".join(parts) + f" (0.1.0 has exactly: {', '.join(sorted(expected))})"


def _column_mismatch(conn: sqlite3.Connection) -> str | None:
    for table, columns in V010_TABLES.items():
        actual = [row[1] for row in conn.execute(f"pragma table_info({quote_identifier(table)})").fetchall()]
        lacking = [column for column in columns if column not in actual]
        if lacking:
            return f"table {table} lacks 0.1.0 columns: {', '.join(lacking)}"
        # The rebuild migrations copy only the known columns; anything else
        # would be dropped silently, so it is refused instead.
        surplus = [column for column in actual if column not in columns]
        if surplus:
            return f"table {table} has columns 0.1.0 did not: {', '.join(surplus)}"
    return None


def _constraint_mismatch(conn: sqlite3.Connection) -> str | None:
    # Same column names, different keys or constraints (a dropped primary
    # key, a missing foreign key, an added unique or CHECK): the table would
    # survive migration as it is, and init cannot repair it.
    for table, expected in v010_constraints().items():
        actual = table_constraints(conn, table)
        differing = [aspect for aspect, value in expected.items() if actual[aspect] != value]
        if differing:
            return f"table {table} does not have the 0.1.0 {', '.join(differing)}"
    return None


def _executable_objects(conn: sqlite3.Connection) -> str | None:
    rows = conn.execute(
        "select type, name from sqlite_master where type in ('trigger', 'view') order by type, name"
    ).fetchall()
    if rows:
        return "0.1.0 wrote no triggers or views, found: " + ", ".join(f"{row[0]} {row[1]}" for row in rows)
    return None


_ADMISSION_CHECKS = (
    _version_mismatch,
    _table_set_mismatch,
    _column_mismatch,
    _constraint_mismatch,
    _executable_objects,
)


def describe_0_1_0_mismatch(conn: sqlite3.Connection) -> str | None:
    """Why this database is not a 0.1.0 mailbox, or None when it is one."""
    for check in _ADMISSION_CHECKS:
        mismatch = check(conn)
        if mismatch is not None:
            return mismatch
    return None


def _default_owners(rows: list[sqlite3.Row]) -> tuple[dict[str, str], list[str]]:
    """Workers whose team has exactly one architect map to it; the rest are ambiguous."""
    architects_by_team: dict[str, list[str]] = {}
    for row in rows:
        if row["role"] == "architect":
            architects_by_team.setdefault(row["team"], []).append(row["id"])
    owners: dict[str, str] = {}
    ambiguous: list[str] = []
    for row in rows:
        if row["role"] != "worker":
            continue
        candidates = architects_by_team.get(row["team"], [])
        if len(candidates) == 1:
            owners[row["id"]] = candidates[0]
        else:
            ambiguous.append(row["id"])
    return owners, ambiguous


def _declaration_problems(
    roles: dict[str, str], owners: dict[str, str], ambiguous: list[str], declarations: dict[str, str]
) -> list[str]:
    problems: list[str] = []
    for worker, architect in declarations.items():
        if roles.get(worker) != "worker":
            problems.append(f"{worker} is not a worker in the mailbox")
        elif worker not in ambiguous:
            problems.append(
                f"{worker} already resolves to {owners[worker]} (its team's only architect); drop its --owner"
            )
        if roles.get(architect) != "architect":
            problems.append(f"{architect} is not an architect in the mailbox")
    return problems


def resolve_owners(conn: sqlite3.Connection, declarations: dict[str, str]) -> dict[str, str]:
    """Owner for every 0.1.0 worker: the team's sole architect, else a declaration.

    Declarations are checked strictly so a typo is never silently ignored:
    the worker must exist with role worker and be ambiguous (zero or several
    team architects), and the architect must exist with role architect.
    """
    rows = conn.execute("select id, team, role from agents order by id").fetchall()
    roles = {row["id"]: row["role"] for row in rows}
    owners, ambiguous = _default_owners(rows)
    problems = _declaration_problems(roles, owners, ambiguous, declarations)
    if problems:
        raise ValidationError("; ".join(problems))
    unresolved = sorted(worker for worker in ambiguous if worker not in declarations)
    if unresolved:
        hints = " ".join(f"--owner {worker}=<architect>" for worker in unresolved)
        raise ValidationError(
            f"no single architect in the team of worker(s) {', '.join(unresolved)}; "
            f"declare each owner: {hints}"
        )
    owners.update({worker: declarations[worker] for worker in ambiguous})
    return owners


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {table: conn.execute(f"select count(*) from {table}").fetchone()[0] for table in V010_TABLES}


def _target_sidecars(target: Path) -> tuple[Path, ...]:
    return (Path(f"{target}-wal"), Path(f"{target}-shm"), Path(f"{target}-journal"))


def _target_obstacles(target: Path) -> list[Path]:
    # lexists: a dangling symlink is an entry too, and writing through it
    # would create a file somewhere else.
    return [path for path in (target, *_target_sidecars(target)) if os.path.lexists(path)]


def plan_import(source: Path, target: Path, declarations: dict[str, str]) -> ImportPlan:
    """Validate everything before anything touches disk."""
    source = Path(source)
    target = Path(target)
    obstacles = _target_obstacles(target)
    if obstacles:
        raise ValidationError(
            f"target ledger exists: {', '.join(str(path) for path in obstacles)}; move it aside, "
            "or import into a fresh runtime root (AGENT_COMMS_HOME) or --db path"
        )
    with contextlib.closing(_open_read_only(source)) as conn:
        mismatch = describe_0_1_0_mismatch(conn)
        if mismatch is not None:
            raise ValidationError(f"{source} is not a 0.1.0 mailbox: {mismatch}")
        owners = resolve_owners(conn, declarations)
        rows = _row_counts(conn)
    return ImportPlan(source=source, target=target, owners=owners, declarations=dict(declarations), rows=rows)


def run_import(plan: ImportPlan) -> dict:
    """Migrate a copy of the source in memory, verify it, then write the target."""
    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    try:
        with contextlib.closing(_open_read_only(plan.source)) as source:
            source.backup(memory)
        # Admission is judged on the snapshot that is actually migrated, so a
        # source edited between planning and now cannot slip past the checks.
        mismatch = describe_0_1_0_mismatch(memory)
        if mismatch is not None:
            raise ValidationError(f"{plan.source} is not a 0.1.0 mailbox: {mismatch}")
        if resolve_owners(memory, plan.declarations) != plan.owners:
            raise ValidationError(f"{plan.source} changed since planning; rerun the import")
        Database(Path(":memory:")).apply_schema(memory, declarations=dict(plan.owners), legacy_mailbox=True)
        _verify(memory, plan)
        plan.target.parent.mkdir(parents=True, exist_ok=True)
        _publish(memory, plan.target)
    finally:
        memory.close()
    return {
        "ok": True,
        "source": str(plan.source),
        "target": str(plan.target),
        "rows": dict(plan.rows),
        "workers": dict(plan.owners),
        "next": "run agent-comms setup to add your human actor and MCP seats, then agent-comms doctor",
    }


def _staging_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.import-{os.getpid()}-{secrets.token_hex(4)}.tmp")


def _claim_staging(staging: Path) -> None:
    try:
        with open(staging, "xb"):
            pass
    except FileExistsError as exc:
        raise ValidationError(f"import staging file {staging} already exists; remove it and rerun") from exc


def _write_staging(memory: sqlite3.Connection, staging: Path) -> None:
    destination = sqlite3.connect(staging)
    try:
        memory.backup(destination)
    finally:
        destination.close()


def _link_into_place(staging: Path, target: Path) -> None:
    """``os.link`` fails on any existing entry, so it never replaces one."""
    obstacles = _target_obstacles(target)
    if not obstacles:
        try:
            os.link(staging, target)
            return
        except FileExistsError:
            obstacles = [target]
    raise ValidationError(
        f"target ledger {', '.join(str(path) for path in obstacles)} appeared while importing; "
        "nothing was written to it; rerun once it is gone"
    )


def _publish(memory: sqlite3.Connection, target: Path) -> None:
    """Complete the ledger under a staging name this call owns, then link it
    to the target name without ever replacing an existing entry.

    The sqlite backup API replaces whatever a connection opens, so it only
    ever runs on the private staging file (unpredictable name, exclusive
    create, same directory). The obstacle check right before the link keeps
    a ``-wal``/``-shm``/``-journal`` that appeared since planning from being
    paired with the new file. On any failure only the staging file and the
    journal sqlite may have made for it are removed.
    """
    staging = _staging_path(target)
    _claim_staging(staging)
    try:
        _write_staging(memory, staging)
        _link_into_place(staging, target)
    finally:
        staging.unlink(missing_ok=True)
        Path(f"{staging}-journal").unlink(missing_ok=True)


def _verify(conn: sqlite3.Connection, plan: ImportPlan) -> None:
    rows = _row_counts(conn)
    if rows != plan.rows:
        raise ValidationError(f"row counts changed during migration: before {plan.rows}, after {rows}")
    violations = conn.execute("pragma foreign_key_check").fetchall()
    if violations:
        raise ValidationError(f"migrated mailbox has foreign key violations: {[tuple(row) for row in violations]}")
    owners = dict(
        conn.execute("select id, owner_actor_id from actors where kind = 'agent' and role = 'worker'").fetchall()
    )
    if owners != plan.owners:
        raise ValidationError(f"worker owners after migration {owners} differ from the plan {plan.owners}")
    version = int(conn.execute("pragma user_version").fetchone()[0])
    if version != LEDGER_SCHEMA_VERSION:
        raise ValidationError(f"migrated mailbox is at user_version {version}, expected {LEDGER_SCHEMA_VERSION}")
    # A successful import produces an ordinary current ledger: the next open
    # must find nothing to migrate (and so take no backup).
    if Database(Path(":memory:"))._needs_migration(conn, version):
        raise ValidationError("migrated mailbox still differs from the current ledger schema; refusing to publish it")


__all__ = [
    "IMPORT_COMMAND",
    "ImportPlan",
    "OWNER_FLAG_SYNTAX",
    "V010_SCHEMA_DDL",
    "describe_0_1_0_mismatch",
    "normalize_sql",
    "parse_owner_flags",
    "plan_import",
    "resolve_owners",
    "run_import",
    "table_constraints",
    "v010_constraints",
]
