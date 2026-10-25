"""Importing a 0.1.0 mailbox (ac-4ao.7).

The 0.1.0 release kept its mailbox at ``<checkout>/data/agent-comms.sqlite``
with five tables and no actors. ``agent-comms import-mailbox PATH`` copies it
into the ledger of the invocation, resolves worker ownership, and migrates it
in memory before writing the target. Opening such a file directly refuses and
points at the command.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_comms import mailbox_import
from agent_comms.cli import run
from agent_comms.db import LEDGER_SCHEMA_VERSION, Database
from agent_comms.schema import ValidationError
from agent_comms.store import Store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "v0_1_0" / "agent-comms.sqlite"
V010_TABLES = ("agents", "messages", "message_recipients", "message_threads", "statuses")


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _counts(path: Path) -> dict[str, int]:
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return {table: conn.execute(f"select count(*) from {table}").fetchone()[0] for table in V010_TABLES}


def _cli(*argv: str) -> tuple[int, dict | None, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = run(list(argv))
    text = out.getvalue().strip()
    payload = json.loads(text) if text else None
    return code, payload, err.getvalue()


class ImportMailboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = self.tmp / "old" / "agent-comms.sqlite"
        self.source.parent.mkdir()
        shutil.copy(FIXTURE, self.source)
        self.target = self.tmp / "home" / "agent-comms.sqlite"

    def test_fixture_imports_with_owned_worker_and_clean_foreign_keys(self) -> None:
        before = _sha256(self.source)
        plan = mailbox_import.plan_import(self.source, self.target, {})
        report = mailbox_import.run_import(plan)

        self.assertEqual(_sha256(self.source), before, "the source is never written")
        self.assertEqual(report["rows"], _counts(self.source))
        self.assertEqual(_counts(self.target), _counts(self.source))
        with contextlib.closing(sqlite3.connect(self.target)) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(conn.execute("pragma user_version").fetchone()[0], LEDGER_SCHEMA_VERSION)
            self.assertEqual(conn.execute("pragma foreign_key_check").fetchall(), [])
            owners = dict(conn.execute("select id, owner_actor_id from actors where role = 'worker'").fetchall())
            self.assertEqual(owners, {"team-a-worker": "team-a-architect"})
            actor_ids = {row["id"] for row in conn.execute("select id from actors")}
            self.assertEqual(
                actor_ids, {"team-a-architect", "team-a-worker", "team-b-architect", "team-b-reviewer"}
            )
            self.assertEqual(conn.execute("select owner_actor_id from actors where id = 'team-b-reviewer'").fetchone()[0], None)
        self.assertNotIn("backups", {p.name for p in self.target.parent.iterdir()}, "a fresh target needs no backup")

        # The imported ledger is an ordinary ledger afterwards: a default open works and the worker is live.
        store = Store(self.target, is_default_db_open=True)
        store.init()
        self.assertEqual(store.whoami("team-a-worker")["id"], "team-a-worker")
        # Its 0.1.0 mail is reachable: the acknowledged handoff is in the full inbox listing.
        inbox = store.list_inbox("team-a-worker", unread_only=False)
        self.assertEqual([m["status"] for m in inbox], ["acknowledged"])

    def test_cli_import_reports_counts_and_next_step(self) -> None:
        code, payload, err = _cli("--db", str(self.target), "import-mailbox", str(self.source))
        self.assertEqual(code, 0, (payload, err))
        self.assertEqual(payload["rows"], _counts(self.source))
        self.assertEqual(payload["workers"], {"team-a-worker": "team-a-architect"})
        self.assertIn("setup", payload["next"])
        self.assertTrue(self.target.exists())

    def test_cli_refuses_an_existing_target_or_sidecar_without_touching_it(self) -> None:
        self.target.parent.mkdir()
        self.target.write_bytes(b"not a ledger")
        code, payload, _ = _cli("--db", str(self.target), "import-mailbox", str(self.source))
        self.assertEqual(code, 2)
        self.assertIn(str(self.target), payload["error"])
        self.assertEqual(self.target.read_bytes(), b"not a ledger")

        self.target.unlink()
        stale = Path(f"{self.target}-wal")
        stale.write_bytes(b"stale")
        code, payload, _ = _cli("--db", str(self.target), "import-mailbox", str(self.source))
        self.assertEqual(code, 2)
        self.assertIn("-wal", payload["error"])
        self.assertFalse(self.target.exists())
        self.assertEqual(stale.read_bytes(), b"stale")

    def test_refuses_a_source_that_is_not_a_0_1_0_mailbox(self) -> None:
        current = self.tmp / "current.sqlite"
        Database(current).init()
        with self.assertRaisesRegex(ValidationError, "0.1.0"):
            mailbox_import.plan_import(current, self.target, {})
        self.assertFalse(self.target.exists())

        missing_table = self.tmp / "partial.sqlite"
        shutil.copy(FIXTURE, missing_table)
        with contextlib.closing(sqlite3.connect(missing_table)) as conn:
            conn.execute("drop table statuses")
            conn.commit()
        with self.assertRaisesRegex(ValidationError, "statuses"):
            mailbox_import.plan_import(missing_table, self.target, {})

        with self.assertRaisesRegex(ValidationError, "does not exist|no such|not found"):
            mailbox_import.plan_import(self.tmp / "absent.sqlite", self.target, {})

    def test_ambiguous_worker_needs_a_declaration_and_declarations_are_checked(self) -> None:
        with contextlib.closing(sqlite3.connect(self.source)) as conn:
            conn.execute(
                "insert into agents(id, team, role, project_root, capabilities_json, last_seen_at)"
                " values('team-a-architect-2', 'team-a', 'architect', '/srv/project-a', '[]', '2025-01-02T09:00:00+00:00')"
            )
            conn.commit()

        with self.assertRaisesRegex(ValidationError, r"team-a-worker.*--owner"):
            mailbox_import.plan_import(self.source, self.target, {})
        for bad in (
            {"team-a-worker": "team-b-reviewer"},  # not an architect
            {"team-a-worker": "nobody"},  # unknown architect
            {"nobody": "team-a-architect"},  # unknown worker
            {"team-b-reviewer": "team-a-architect"},  # not a worker
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                mailbox_import.plan_import(self.source, self.target, bad)
        self.assertFalse(self.target.exists())

        plan = mailbox_import.plan_import(self.source, self.target, {"team-a-worker": "team-a-architect-2"})
        report = mailbox_import.run_import(plan)
        self.assertEqual(report["workers"], {"team-a-worker": "team-a-architect-2"})

    def test_declaration_for_a_worker_that_already_resolves_is_refused(self) -> None:
        with self.assertRaisesRegex(ValidationError, "team-a-worker"):
            mailbox_import.plan_import(self.source, self.target, {"team-a-worker": "team-a-architect"})

    def test_cli_owner_flag_and_bad_syntax(self) -> None:
        code, payload, _ = _cli("--db", str(self.target), "import-mailbox", str(self.source), "--owner", "team-a-worker")
        self.assertEqual(code, 2)
        self.assertIn("WORKER=ARCHITECT", payload["error"])
        self.assertFalse(self.target.exists())

    def test_analyzed_mailbox_is_still_refused_in_place_and_still_imports(self) -> None:
        # Lookout F1: ANALYZE adds sqlite_stat1, which is SQLite's bookkeeping, not schema.
        with contextlib.closing(sqlite3.connect(self.source)) as conn:
            conn.execute("analyze")
            conn.commit()
            self.assertIn("sqlite_stat1", {r[0] for r in conn.execute("select name from sqlite_master where type='table'")})
        before = _sha256(self.source)
        with self.assertRaisesRegex(ValidationError, "import-mailbox"):
            Database(self.source).init()
        self.assertEqual(_sha256(self.source), before)
        self.assertFalse((self.source.parent / "backups").exists())
        report = mailbox_import.run_import(mailbox_import.plan_import(self.source, self.target, {}))
        self.assertEqual(report["workers"], {"team-a-worker": "team-a-architect"})
        with contextlib.closing(sqlite3.connect(self.target)) as conn:
            self.assertEqual(conn.execute("pragma foreign_key_check").fetchall(), [])

    def test_unsupported_schema_in_the_source_is_refused(self) -> None:
        # Lookout F4: extra columns would be dropped by the rebuilds; triggers and views would carry over.
        cases = {
            "extra column": ("alter table messages add column operator_annotation text", "operator_annotation"),
            "trigger": ("create trigger t after insert on statuses begin delete from messages; end", "trigger t"),
            "view": ("create view v as select id from messages", "view v"),
            "extra table": ("create table notes(id integer primary key)", "notes"),
        }
        for label, (ddl, expected) in cases.items():
            with self.subTest(label):
                path = self.tmp / f"{label.replace(' ', '-')}.sqlite"
                shutil.copy(FIXTURE, path)
                with contextlib.closing(sqlite3.connect(path)) as conn:
                    conn.execute(ddl)
                    conn.commit()
                with self.assertRaisesRegex(ValidationError, expected):
                    mailbox_import.plan_import(path, self.target, {})
                # Admission is judged again on the snapshot that is migrated.
                plan = mailbox_import.plan_import(self.source, self.target, {})
                with mock.patch.object(mailbox_import, "_open_read_only", lambda _p: _ro(path)):
                    with self.assertRaisesRegex(ValidationError, expected):
                        mailbox_import.run_import(plan)
                self.assertFalse(self.target.exists())

    def test_source_names_with_uri_characters_are_read_as_named(self) -> None:
        # Lookout F3: '#' and '?' in a path must not end the URI early.
        for name in ("legacy#copy.sqlite", "what?.sqlite", "pct%20name.sqlite"):
            with self.subTest(name):
                folder = self.tmp / name.replace(".sqlite", "")
                folder.mkdir()
                source = folder / name
                shutil.copy(FIXTURE, source)
                target = folder / "new.sqlite"
                report = mailbox_import.run_import(mailbox_import.plan_import(source, target, {}))
                self.assertEqual(report["rows"]["messages"], 5)
                self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted([name, "new.sqlite"]))

    def test_target_that_appears_during_import_is_not_overwritten(self) -> None:
        # Lookout F2: the final write must own the target it writes.
        plan = mailbox_import.plan_import(self.source, self.target, {})
        original_verify = mailbox_import._verify

        def verify_then_race(conn, plan_):
            original_verify(conn, plan_)
            self.target.parent.mkdir(exist_ok=True)
            with contextlib.closing(sqlite3.connect(self.target)) as other:
                other.execute("create table competing(x)")
                other.commit()

        with mock.patch.object(mailbox_import, "_verify", verify_then_race):
            with self.assertRaisesRegex(ValidationError, "appeared"):
                mailbox_import.run_import(plan)
        with contextlib.closing(sqlite3.connect(self.target)) as conn:
            self.assertEqual({r[0] for r in conn.execute("select name from sqlite_master where type='table'")}, {"competing"})

    def test_dangling_symlink_target_is_refused(self) -> None:
        self.target.parent.mkdir()
        elsewhere = self.tmp / "elsewhere" / "ledger.sqlite"
        self.target.symlink_to(elsewhere)
        with self.assertRaisesRegex(ValidationError, str(self.target)):
            mailbox_import.plan_import(self.source, self.target, {})
        self.assertFalse(elsewhere.parent.exists())
        self.target.unlink()
        Path(f"{self.target}-shm").symlink_to(elsewhere)
        with self.assertRaisesRegex(ValidationError, "-shm"):
            mailbox_import.plan_import(self.source, self.target, {})
        self.assertFalse(elsewhere.parent.exists())

    def test_legacy_mailbox_with_an_extra_table_is_still_refused_in_place(self) -> None:
        # Lookout F1 (round 2): the guard recognizes the hazard (five tables, no actors),
        # not an exact table set; the importer stays strict and refuses the extension.
        with contextlib.closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("create table notes(id integer primary key)")
        before = _sha256(self.source)
        for default in (True, False):
            with self.subTest(default=default), self.assertRaisesRegex(ValidationError, "import-mailbox"):
                Database(self.source, is_default_db_open=default).init()
        self.assertEqual(_sha256(self.source), before)
        self.assertFalse((self.source.parent / "backups").exists())
        with self.assertRaisesRegex(ValidationError, "notes"):
            mailbox_import.plan_import(self.source, self.target, {})
        self.assertFalse(self.target.exists())

    def test_source_with_different_keys_or_constraints_is_refused(self) -> None:
        # Lookout F4 (round 2): same column names but other constraints survive migration unrepaired.
        cases = {
            "statuses without a primary key": (
                """
                create table statuses_new(
                  id text, agent_id text not null, summary text not null, current_files_json text not null,
                  blocked_on text, next_step text, created_at text not null,
                  foreign key(agent_id) references agents(id)
                );
                insert into statuses_new select * from statuses;
                insert into statuses_new select * from statuses;
                drop table statuses;
                alter table statuses_new rename to statuses;
                """,
                r"statuses does not have the 0\.1\.0 columns",
            ),
            "messages without the agents foreign key": (
                """
                create table messages_new(
                  id text primary key, from_agent text not null, subject text not null, body text not null,
                  refs_json text not null, priority text not null, requires_ack integer not null, created_at text not null
                );
                insert into messages_new select * from messages;
                drop table messages;
                alter table messages_new rename to messages;
                """,
                r"messages does not have the 0\.1\.0 foreign keys",
            ),
            "extra unique on agents": (
                "create unique index agents_team_role on agents(team, role);",
                r"agents does not have the 0\.1\.0 unique constraints",
            ),
            # Lookout round 3: a CHECK is invisible to the pragmas and would break post_status later.
            "statuses with a check": (
                """
                create table statuses_new(
                  id text primary key, agent_id text not null, summary text not null check(length(summary) < 50),
                  current_files_json text not null, blocked_on text, next_step text, created_at text not null,
                  foreign key(agent_id) references agents(id)
                );
                insert into statuses_new select * from statuses;
                drop table statuses;
                alter table statuses_new rename to statuses;
                """,
                r"statuses does not have the 0\.1\.0 definition",
            ),
            # Lookout round 3: an expression index has no column name; refuse, do not crash.
            "expression unique index": (
                "create unique index expression_idx on statuses(lower(id));",
                r"statuses does not have the 0\.1\.0 unique constraints",
            ),
        }
        for label, (script, expected) in cases.items():
            with self.subTest(label):
                path = self.tmp / f"{label.replace(' ', '-')}.sqlite"
                shutil.copy(FIXTURE, path)
                with contextlib.closing(sqlite3.connect(path)) as conn:
                    conn.executescript(script)
                    conn.commit()
                with self.assertRaisesRegex(ValidationError, expected):
                    mailbox_import.plan_import(path, self.target, {})
                plan = mailbox_import.plan_import(self.source, self.target, {})
                with mock.patch.object(mailbox_import, "_open_read_only", lambda _p: _ro(path)):
                    with self.assertRaisesRegex(ValidationError, expected):
                        mailbox_import.run_import(plan)
                self.assertFalse(self.target.exists())
                self.assertFalse(self.target.parent.exists() and any(self.target.parent.iterdir()))

    def test_imported_ledger_is_current_so_the_next_open_takes_no_backup(self) -> None:
        mailbox_import.run_import(mailbox_import.plan_import(self.source, self.target, {}))
        for default in (True, False):
            Database(self.target, is_default_db_open=default).init()
        self.assertEqual(sorted(p.name for p in self.target.parent.iterdir()), ["agent-comms.sqlite"])

    def test_writer_populating_the_target_after_staging_starts_is_not_erased(self) -> None:
        # Lookout F2 (round 2): the backup API only ever touches the private staging
        # file; publication links it to the target name and never replaces an entry.
        plan = mailbox_import.plan_import(self.source, self.target, {})
        real_write = mailbox_import._write_staging

        def write_then_race(memory, staging):
            real_write(memory, staging)
            with contextlib.closing(sqlite3.connect(self.target)) as other, other:
                other.execute("create table valuable(x)")
                other.execute("insert into valuable values(1)")

        with mock.patch.object(mailbox_import, "_write_staging", write_then_race):
            with self.assertRaisesRegex(ValidationError, "appeared"):
                mailbox_import.run_import(plan)
        with contextlib.closing(sqlite3.connect(f"file:{self.target}?mode=ro", uri=True)) as conn:
            self.assertEqual(conn.execute("select x from valuable").fetchall(), [(1,)])
        self.assertEqual(sorted(p.name for p in self.target.parent.iterdir()), ["agent-comms.sqlite"])

    def test_sidecar_appearing_before_publication_is_refused_and_kept(self) -> None:
        # Lookout F2 (round 2): sidecars are checked at the publication boundary, not only at planning.
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix):
                target = self.tmp / f"home{suffix}" / "agent-comms.sqlite"
                plan = mailbox_import.plan_import(self.source, target, {})
                sidecar = Path(f"{target}{suffix}")
                real_write = mailbox_import._write_staging

                def write_then_sidecar(memory, staging, sidecar=sidecar):
                    real_write(memory, staging)
                    sidecar.write_bytes(b"late")

                with mock.patch.object(mailbox_import, "_write_staging", write_then_sidecar):
                    with self.assertRaisesRegex(ValidationError, suffix):
                        mailbox_import.run_import(plan)
                self.assertFalse(target.exists())
                self.assertEqual(sidecar.read_bytes(), b"late")
                self.assertEqual([p.name for p in target.parent.iterdir()], [sidecar.name])

    def test_opening_a_0_1_0_mailbox_refuses_with_the_import_hint(self) -> None:
        before = _sha256(self.source)
        for default in (True, False):
            with self.subTest(default=default), self.assertRaisesRegex(ValidationError, "import-mailbox"):
                Database(self.source, is_default_db_open=default).init()
        self.assertEqual(_sha256(self.source), before)
        self.assertFalse((self.source.parent / "backups").exists())

        code, payload, _ = _cli("--db", str(self.source), "init")
        self.assertEqual(code, 2)
        self.assertIn("import-mailbox", payload["error"])


if __name__ == "__main__":
    unittest.main()
