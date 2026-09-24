import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import ast
import contextlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import review
from agent_comms.reviewing import execution, ledger_evidence, rounds


class ReviewLedgerOpenPolicyTest(unittest.TestCase):
    def test_review_ledger_opens_are_centralized_and_bounded(self) -> None:
        # After Extraction 3 the sole query-only ledger open lives in
        # ledger_evidence.py; execution.py's redispatch/respawn reach it as an
        # imported collaborator, so the ownership model follows the moved source.
        ledger_tree = ast.parse(
            Path(ledger_evidence.__file__).read_text(encoding="utf-8")
        )
        execution_tree = ast.parse(Path(execution.__file__).read_text(encoding="utf-8"))
        # command_redispatch moved to rounds.py with contract-17 intent binding;
        # its ledger-open ownership follows the moved source.
        rounds_tree = ast.parse(Path(rounds.__file__).read_text(encoding="utf-8"))
        parents = {}
        for parent in ast.walk(ledger_tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        calls = [
            node
            for node in ast.walk(ledger_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sqlite3"
            and node.func.attr == "connect"
        ]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        owner = parents[call]
        while not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents[owner]
        self.assertEqual(owner.name, "_open_ledger_for_reading")
        self.assertIn("mode=rw", ast.unparse(call.args[0]))
        self.assertNotIn("mode=rwc", ast.unparse(call.args[0]))
        timeout = next(keyword.value for keyword in call.keywords if keyword.arg == "timeout")
        self.assertIsInstance(timeout, ast.Constant)
        self.assertEqual(timeout.value, 10.0)
        functions = {
            node.name: node
            for tree in (ledger_tree, execution_tree, rounds_tree)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        for name in (
            "require_review_repo_worker_root",
            "_derive_worker_evidence",
            "command_redispatch",
            "command_respawn",
        ):
            references = {
                node.id for node in ast.walk(functions[name]) if isinstance(node, ast.Name)
            }
            self.assertIn("_open_ledger_for_reading", references, name)


class ReviewLedgerOpenBehaviorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "ledger.sqlite"

    @staticmethod
    def error(exc: sqlite3.Error) -> review.ReviewError:
        return review.ReviewError(f"site_open_failed: {exc}")

    def initialize(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("pragma journal_mode=wal")
            conn.execute("create table sample(value text)")
            conn.execute("insert into sample values ('committed')")

    def assert_query_green(self) -> None:
        conn = review._open_ledger_for_reading(self.db, self.error)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("select value from sample").fetchone()[0], "committed")

    def test_opens_with_no_sidecars(self) -> None:
        self.initialize()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.db}{suffix}")
            if sidecar.exists():
                sidecar.unlink()
        self.assertFalse(Path(f"{self.db}-wal").exists())
        self.assertFalse(Path(f"{self.db}-shm").exists())
        self.assert_query_green()

    def test_missing_ledger_fails_closed_with_site_remedy(self) -> None:
        missing = self.root / "missing" / "ledger.sqlite"
        record = {
            "expected_recipient": "gamma-codex-worker",
            "repo": str(self.root),
        }
        with mock.patch.object(review.runtime_paths, "db_path", return_value=missing):
            with self.assertRaises(review.ReviewError) as raised:
                review.require_review_repo_worker_root(record)
        message = str(raised.exception)
        self.assertIn("review_repo_worker_root_ledger_open_failed", message)
        self.assertIn(str(missing), message)
        self.assertIn("WAL open requires write access", message)
        self.assertFalse(missing.exists())

    def _dead_writer(self) -> None:
        read_fd, write_fd = os.pipe()
        script = (
            "import os, sqlite3, sys\n"
            "db, fd = sys.argv[1], int(sys.argv[2])\n"
            "c = sqlite3.connect(db)\n"
            "c.execute('pragma journal_mode=wal')\n"
            "c.execute('pragma wal_autocheckpoint=0')\n"
            "c.execute('create table sample(value text)')\n"
            "c.execute(\"insert into sample values ('committed')\")\n"
            "c.commit()\n"
            "os.write(fd, b'1')\n"
            "os._exit(0)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(self.db), str(write_fd)],
            pass_fds=(write_fd,),
        )
        os.close(write_fd)
        try:
            self.assertEqual(os.read(read_fd, 1), b"1")
        finally:
            os.close(read_fd)
        self.assertEqual(proc.wait(), 0)

    def test_opens_with_stale_dead_writer_sidecars(self) -> None:
        self._dead_writer()
        self.assertTrue(Path(f"{self.db}-wal").exists())
        self.assertTrue(Path(f"{self.db}-shm").exists())
        self.assert_query_green()

    def test_opens_with_orphaned_wal_without_shm(self) -> None:
        self._dead_writer()
        Path(f"{self.db}-shm").unlink()
        self.assertTrue(Path(f"{self.db}-wal").exists())
        self.assertFalse(Path(f"{self.db}-shm").exists())
        self.assert_query_green()

    def test_query_only_and_timeout(self) -> None:
        self.initialize()
        conn = review._open_ledger_for_reading(self.db, self.error)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("pragma busy_timeout").fetchone()[0], 10000)
        with self.assertRaises(sqlite3.Error):
            conn.execute("insert into sample values ('forbidden')")

    def test_post_connect_setup_failure_closes_and_preserves_cause(self) -> None:
        setup_error = sqlite3.OperationalError("pragma denied")
        connection = mock.MagicMock()
        connection.execute.side_effect = setup_error
        connection.close.side_effect = sqlite3.OperationalError("close denied")
        with mock.patch.object(review.sqlite3, "connect", return_value=connection):
            with self.assertRaises(review.ReviewError) as raised:
                review._open_ledger_for_reading(self.db, self.error)
        connection.close.assert_called_once_with()
        self.assertIs(raised.exception.__cause__, setup_error)


if __name__ == "__main__":
    unittest.main()
