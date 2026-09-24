from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from agent_comms import __version__, code_identity, db, paths
from agent_comms.release import release_info, repo_git_info
from agent_comms.schema import ValidationError
from agent_comms.store import Store


class ReleaseVersionTest(unittest.TestCase):
    def test_pyproject_version_matches_package_version(self) -> None:
        pyproject = tomllib.loads((paths.REPO_ROOT / "pyproject.toml").read_text())

        self.assertEqual(pyproject["project"]["version"], __version__)
        self.assertNotEqual(__version__, "0.1.0")

    def test_release_info_and_cli_resolve_from_repo_root_not_cwd(self) -> None:
        expected_cell_versions = json.loads(
            (paths.REPO_ROOT / "tests" / "cells" / "cell_versions.json").read_text()
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = Path.cwd()
            os.chdir(temp_dir)
            try:
                info = release_info()
                env = os.environ.copy()
                env.pop("WAKE_POLICY", None)
                env.pop("WAKE_POLICY_VERSION", None)
                pythonpath = str(paths.REPO_ROOT)
                if env.get("PYTHONPATH"):
                    pythonpath = os.pathsep.join((pythonpath, env["PYTHONPATH"]))
                env["PYTHONPATH"] = pythonpath
                result = subprocess.run(
                    [sys.executable, "-m", "agent_comms.cli", "version"],
                    cwd=temp_dir,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
            finally:
                os.chdir(previous_cwd)

        cli_info = json.loads(result.stdout)
        for payload in (info, cli_info):
            self.assertEqual(payload["version"], __version__)
            self.assertEqual(payload["code_identity"], code_identity.LOADED_CODE_IDENTITY)
            self.assertEqual(payload["contract_version"], code_identity.CONTRACT_VERSION)
            self.assertEqual(payload["certified_runtimes"], expected_cell_versions)
            self.assertIn("codex", payload["certified_runtimes"])
            self.assertIn("claude", payload["certified_runtimes"])
            self.assertEqual(payload["repo_root"], str(paths.REPO_ROOT))
            self.assertIn(payload["git_head_state"], {"live", "detached", "unknown"})
            self.assertIn("git_describe", payload)
            self.assertIn("git_exact_tag", payload)
            self.assertIn("pin_worktree", payload)

    def test_release_version_files_remain_off_dispatch_surface(self) -> None:
        self.assertFalse(code_identity.is_included_surface("agent_comms/__init__.py"))
        self.assertIsNotNone(code_identity.exclusion_reason("agent_comms/__init__.py"))
        self.assertFalse(code_identity.is_included_surface("pyproject.toml"))

    def test_release_legibility_stays_off_dispatch_surface(self) -> None:
        self.assertFalse(code_identity.is_included_surface("agent_comms/release.py"))
        self.assertIsNotNone(code_identity.exclusion_reason("agent_comms/release.py"))

    def test_repo_git_info_distinguishes_live_from_tagged_detached(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live = root / "live"
            tagged = root / "tagged"
            self._init_git_repo(live)
            subprocess.run(
                ["git", "clone", str(live), str(tagged)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "-C", str(tagged), "checkout", "v-test"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            live_info = repo_git_info(live)
            tagged_info = repo_git_info(tagged)

        self.assertEqual(live_info["git_head_state"], "live")
        self.assertFalse(live_info["pin_worktree"])
        self.assertEqual(tagged_info["git_head_state"], "detached")
        self.assertEqual(tagged_info["git_exact_tag"], "v-test")
        self.assertTrue(tagged_info["pin_worktree"])

    def _init_git_repo(self, root: Path) -> None:
        root.mkdir()
        subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test User"], check=True)
        (root / "file.txt").write_text("content\n")
        subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "-C", str(root), "tag", "v-test"], check=True)


class LedgerSchemaVersionTest(unittest.TestCase):
    @contextmanager
    def _default_paths(self, db_path: Path) -> Iterator[None]:
        with patch.object(paths, "canonical_db_path", return_value=db_path):
            yield

    def _store_for(self, db_path: Path, *, is_default_db_open: bool) -> Store:
        return Store(db_path, is_default_db_open=is_default_db_open)

    def _set_user_version(self, db_path: Path, version: int) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                conn.execute(f"pragma user_version = {version}")

    def _get_user_version(self, db_path: Path) -> int:
        with closing(sqlite3.connect(db_path)) as conn:
            return int(conn.execute("pragma user_version").fetchone()[0])

    def _table_names(self, db_path: Path) -> set[str]:
        with closing(sqlite3.connect(db_path)) as conn:
            return {
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
                ).fetchall()
            }

    def _add_synthetic_additive_delta(self, db_path: Path) -> None:
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                conn.execute("alter table messages add column additive_reader_ignored text")
                conn.execute("create index if not exists idx_messages_additive_subject on messages(subject)")
                conn.execute(f"pragma user_version = {db.LEDGER_SCHEMA_VERSION}")

    def _assert_store_can_read_and_write_known_tables(self, store: Store, root: Path) -> None:
        store.register_agent_actor(
            "writer-architect",
            "compat",
            "architect",
            str(root / "writer"),
            [],
            runtime="codex",
        )
        store.register_agent_actor(
            "reader-worker",
            "compat",
            "worker",
            str(root / "reader"),
            [],
            runtime="codex",
            owner="writer-architect",
        )
        message = store.send_message(
            "writer-architect",
            ["reader-worker"],
            "compat write",
            "body",
            [],
            requires_ack=True,
        )
        status = store.post_status("writer-architect", "ok", [], next_step="done")
        with store.connection() as conn:
            conn.execute(
                """
                insert into dispatch_ledger(
                  dispatch_id, parent_dispatch_id, idempotency_key, message_id,
                  thread_ref, spawn_handle, recipient_actor_id, producer_actor_id,
                  originating_actor_id, policy_name, policy_version, policy_issued_by,
                  expected_close_by, status, created_at, observed_values_json
                )
                values(
                  'dispatch_20260630_000000_compat1', NULL, 'compat-key', ?, ?,
                  NULL, 'reader-worker', 'writer-architect', 'writer-architect',
                  'worker_dispatch_readwrite_bounded', 'v1', 'writer-architect',
                  NULL, 'queued', '2026-06-30T00:00:00+00:00', '{}'
                )
                """,
                (message["id"], message["id"]),
            )

        self.assertTrue(any(actor["id"] == "reader-worker" for actor in store.list_actors()))
        self.assertTrue(any(row["id"] == status["id"] for row in store.list_status()))
        self.assertEqual(store.read_message("reader-worker", message["id"])["id"], message["id"])
        self.assertTrue(
            any(
                row["dispatch_id"] == "dispatch_20260630_000000_compat1"
                for row in store.list_dispatches(status="queued")
            )
        )

    def _build_floor2_shaped_ledger(self, db_path: Path, root: Path) -> None:
        """Physically floor-2-shaped fixture: full floor-3 schema and seeded
        representative rows, minus the floor-3-only message_payload_refs
        table, stamped user_version=2. Not merely a floor-3 schema carrying
        a floor-2 marker."""
        with self._default_paths(db_path):
            store = self._store_for(db_path, is_default_db_open=True)
            store.init()
            self._assert_store_can_read_and_write_known_tables(store, root)
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                conn.execute("drop table message_payload_refs")
                conn.execute("pragma user_version = 2")

    def _logical_snapshot(self, db_path: Path) -> dict[str, object]:
        """Complete logical application/schema state: floor, journal mode,
        schema SQL, per-table columns and indexes, and application rows.
        Engine-owned recovery/sidecar coordination is outside the claim."""
        with closing(sqlite3.connect(db_path)) as conn:
            tables = sorted(
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type = 'table'"
                ).fetchall()
            )
            return {
                "user_version": int(conn.execute("pragma user_version").fetchone()[0]),
                "journal_mode": conn.execute("pragma journal_mode").fetchone()[0],
                "sqlite_master": sorted(
                    tuple(row)
                    for row in conn.execute(
                        "select type, name, tbl_name, sql from sqlite_master"
                    ).fetchall()
                ),
                "table_info": {
                    table: [
                        tuple(row)
                        for row in conn.execute(
                            f'pragma table_info("{table}")'
                        ).fetchall()
                    ]
                    for table in tables
                },
                "indexes": {
                    table: sorted(
                        row[1]
                        for row in conn.execute(
                            f'pragma index_list("{table}")'
                        ).fetchall()
                    )
                    for table in tables
                },
                "rows": {
                    table: [
                        tuple(row)
                        for row in conn.execute(
                            f'select * from "{table}" order by rowid'
                        ).fetchall()
                    ]
                    for table in tables
                    if not table.startswith("sqlite_")
                },
            }

    def test_default_open_refuses_lower_positive_floor_before_any_ddl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            self._build_floor2_shaped_ledger(db_path, root)
            self.assertNotIn("message_payload_refs", self._table_names(db_path))
            before = self._logical_snapshot(db_path)
            self.assertEqual(before["user_version"], 2)

            with self._default_paths(db_path):
                with self.assertRaises(ValidationError) as refusal:
                    self._store_for(db_path, is_default_db_open=True).init()

            message = str(refusal.exception)
            self.assertIn("ledger_user_version=2", message)
            self.assertIn(
                f"code_LEDGER_SCHEMA_VERSION={db.LEDGER_SCHEMA_VERSION}", message
            )
            self.assertIn(
                "ordinary open cannot perform the upward floor cutover", message
            )
            self.assertIn("floor-compatible", message)
            self.assertNotIn("fix-ledger-floor", message)
            # Direct pre-DDL proof: a post-DDL refusal would have recreated
            # the floor-3-only table before raising.
            self.assertNotIn("message_payload_refs", self._table_names(db_path))
            self.assertEqual(self._logical_snapshot(db_path), before)

    def test_explicit_db_open_lower_floor_still_stamps_forward(self) -> None:
        # Characterizes the acknowledged gzg residual, not a safety claim:
        # explicit --db/AGENT_COMMS_DB opens bypass the default-ledger guard.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent-comms.sqlite"
            self._set_user_version(db_path, db.LEDGER_SCHEMA_VERSION - 1)

            self._store_for(db_path, is_default_db_open=False).init()

            self.assertEqual(self._get_user_version(db_path), db.LEDGER_SCHEMA_VERSION)

    def test_default_open_fails_closed_when_ledger_is_newer_than_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            synthetic_breaking_floor = db.LEDGER_SCHEMA_VERSION + 1
            self._set_user_version(db_path, synthetic_breaking_floor)

            with self._default_paths(db_path):
                with self.assertRaisesRegex(
                    ValidationError,
                    "newer ledger schema.*upgrade this agent-comms checkout",
                ):
                    self._store_for(db_path, is_default_db_open=True).init()

    def test_default_open_equal_version_opens_normally(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent-comms.sqlite"
            self._set_user_version(db_path, db.LEDGER_SCHEMA_VERSION)

            with self._default_paths(db_path):
                self._store_for(db_path, is_default_db_open=True).init()

            self.assertEqual(self._get_user_version(db_path), db.LEDGER_SCHEMA_VERSION)

    def test_additive_schema_evolution_keeps_ledger_marker_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            Store(db_path, is_default_db_open=True).init()
            self._add_synthetic_additive_delta(db_path)
            self.assertIn("handoffs", self._table_names(db_path))

            with self._default_paths(db_path):
                store = self._store_for(db_path, is_default_db_open=True)
                store.init()
                self._assert_store_can_read_and_write_known_tables(store, root)

            self.assertEqual(self._get_user_version(db_path), db.LEDGER_SCHEMA_VERSION)

    def test_default_open_unmarked_ledger_initializes_and_stamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent-comms.sqlite"
            self._set_user_version(db_path, 0)

            with self._default_paths(db_path):
                self._store_for(db_path, is_default_db_open=True).init()

            self.assertEqual(self._get_user_version(db_path), db.LEDGER_SCHEMA_VERSION)

    def test_explicit_db_override_allows_newer_ledger_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent-comms.sqlite"
            synthetic_breaking_floor = db.LEDGER_SCHEMA_VERSION + 1
            self._set_user_version(db_path, synthetic_breaking_floor)

            self._store_for(db_path, is_default_db_open=False).init()

            self.assertEqual(self._get_user_version(db_path), synthetic_breaking_floor)
