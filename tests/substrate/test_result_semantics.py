from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import ast
import json
import hashlib
import contextlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_comms import code_identity, delta_manifest, mailbox, policies, review
from agent_comms.adapters import DispatchContext
from agent_comms.adapters.codex import CodexAdapter
from agent_comms.db import Database, LEDGER_SCHEMA_VERSION
from agent_comms.schema import ValidationError
from agent_comms.spawn import DEFAULT_WORKER_PROMPT, V2_WORKER_PROMPT, render_spawn
from agent_comms.store import Store
from agent_comms.reviewing import git_evidence
from agent_comms.reviewing import store as reviewing_store


class ResultSchemaTest(unittest.TestCase):
    def test_delta_manifest_raw_z_counts_metadata_not_pathnames(self) -> None:
        old = b"0" * 40
        new = b"1" * 40
        metadata = lambda status: b":100644 100644 " + old + b" " + new + b" " + status
        manifest = (
            metadata(b"A") + b"\0D-leading\0"
            + metadata(b"A") + b"\0D-second\0"
            + metadata(b"M") + b"\0A-leading\tand\nnewline\0"
            + metadata(b"D") + b"\0M-leading\0"
        )
        self.assertEqual(delta_manifest.parse_raw_z_manifest(manifest), (4, {"A": 2, "M": 1, "D": 1}))

    def test_delta_manifest_raw_z_rejects_malformed_or_truncated_streams(self) -> None:
        metadata = b":100644 100644 " + b"0" * 40 + b" " + b"1" * 40 + b" M"
        malformed = (
            b"",
            metadata,
            metadata + b"\0",
            metadata + b"\0path\0extra\0",
            b"path\0" + metadata + b"\0",
            metadata[:-1] + b"Q\0path\0",
        )
        for manifest in malformed:
            with self.subTest(manifest=manifest):
                with self.assertRaisesRegex(ValidationError, "delta_manifest_malformed"):
                    delta_manifest.parse_raw_z_manifest(manifest)

    def test_delta_manifest_raw_z_rejects_r100_three_token_record(self) -> None:
        manifest = (
            b":100644 100644 "
            + b"0" * 40
            + b" "
            + b"1" * 40
            + b" R100\0old-path\0new-path\0"
        )
        with self.assertRaisesRegex(ValidationError, "delta_manifest_malformed"):
            delta_manifest.parse_raw_z_manifest(manifest)

    def test_delta_name_status_counts_and_all_allowed_statuses(self) -> None:
        stream = b"".join(
            status.encode() + b"\0" + f"{status}-path".encode() + b"\0"
            for status in sorted(delta_manifest.RAW_GIT_STATUS_CODES)
        )
        self.assertEqual(
            delta_manifest.derive_name_status_counts(stream),
            (7, {status: 1 for status in sorted(delta_manifest.RAW_GIT_STATUS_CODES)}),
        )

    def test_delta_name_status_rejects_malformed_streams(self) -> None:
        for stream in (b"", b"M", b"M\0", b"M\0path", b"Q\0path\0", b"MM\0path\0"):
            with self.subTest(stream=stream):
                with self.assertRaisesRegex(ValidationError, "delta_name_status_malformed"):
                    delta_manifest.derive_name_status_counts(stream)

    def test_delta_crosscheck_rejects_disagreement(self) -> None:
        metadata = b":100644 100644 " + b"0" * 40 + b" " + b"1" * 40 + b" M"
        with self.assertRaisesRegex(ValidationError, "delta_manifest_disagreement"):
            delta_manifest.parse_and_crosscheck(
                metadata + b"\0path\0", b"A\0path\0"
            )

    def test_delta_parser_namespace_hygiene(self) -> None:
        self.assertFalse(hasattr(mailbox, "_parse_raw_z_manifest"))
        self.assertFalse(hasattr(review, "_parse_raw_z_manifest"))

    def test_d7_contract_versions_advanced_together(self) -> None:
        # Advanced 12 -> 13 for the Codex-home custody migration (dispatch/start
        # gate + schema change), then 13 -> 14 for worker ownership, then
        # 14 -> 15 for dispatch payload transport, then 15 -> 16 for the
        # payload compatibility floor and custody corrections, then 16 -> 17
        # for dispatch-time review intent binding (review evidence lifecycle
        # Landing 1), then 17 -> 18 for file-backed send_message bodies and
        # message payload binding, then 18 -> 19 for cycle approval destination
        # binding (contract 19, the agent-comms-approval-v2 payload), then
        # 19 -> 20 for review evidence lifecycle Landing 2 (the stable reply
        # snapshot and exact closeout consumption), then 20 -> 21 for
        # structured codex worker output (the `--json` argv and the split
        # worker_events artifact; an equal version would let a loaded process
        # consume the new argv with the old single-log adapter).
        # LEDGER_SCHEMA_VERSION (a distinct on-disk axis) advanced 1 -> 2 with
        # contract 16, stayed at 2 under contract 17 because the intent table is
        # additive, and advanced 2 -> 3 with contract 18 for message payload
        # binding; it stays at 3 under contracts 19 and 20 because approval
        # binding and the reply-snapshot table are additive, and prior readers
        # refuse a payload-capable ledger; it stays at 3 under contract 21
        # because no on-disk ledger change is involved.
        self.assertEqual(code_identity.CONTRACT_VERSION, 22)
        self.assertEqual(policies.WORKER_DISPATCH_POLICY_VERSION, "v2")
        self.assertEqual(review.SCHEMA_VERSION, 2)
        self.assertEqual(mailbox.CLOSEOUT_PROTOCOL_VERSION, 1)
        self.assertEqual(
            LEDGER_SCHEMA_VERSION,
            3,
            "ledger schema floor advances only for a deliberate breaking change",
        )

    def test_n34_case_guard_truth_table_and_schema_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "ledger.sqlite")
            store.init()
            with store.connection() as conn:
                self.assertEqual(conn.execute("pragma user_version").fetchone()[0], LEDGER_SCHEMA_VERSION)
                sql = conn.execute(
                    "select sql from sqlite_master where type='table' and name='dispatch_ledger'"
                ).fetchone()[0].lower()
                self.assertIn("check (case when result is null", sql)
                base = {
                    "dispatch_id": "dispatch_20260725_120000_00000001",
                    "idempotency_key": "n34",
                    "thread_ref": "n34",
                    "recipient_actor_id": "worker",
                    "producer_actor_id": "architect",
                    "originating_actor_id": "architect",
                    "policy_name": "worker_dispatch_readwrite_bounded",
                    "policy_version": "v1",
                    "policy_issued_by": "architect",
                    "status": "closed",
                    "created_at": "2026-07-25T12:00:00+00:00",
                    "result": None,
                }
                conn.execute(
                    "insert into actors(id,kind,display_name,capabilities_json,last_seen_at) "
                    "values('architect','agent','architect','[]','x'),"
                    "('worker','agent','worker','[]','x')"
                )
                sql_insert = """
                    insert into dispatch_ledger(
                      dispatch_id,idempotency_key,thread_ref,recipient_actor_id,
                      producer_actor_id,originating_actor_id,policy_name,
                      policy_version,policy_issued_by,status,created_at,result
                    ) values(
                      :dispatch_id,:idempotency_key,:thread_ref,:recipient_actor_id,
                      :producer_actor_id,:originating_actor_id,:policy_name,
                      :policy_version,:policy_issued_by,:status,:created_at,:result
                    )
                """
                conn.execute(sql_insert, base)  # v1 closed + NULL
                legal = (
                    ("v2", "in_flight", None),
                    ("v2", "closed", "satisfied"),
                    ("v2", "closed", "blocked"),
                )
                for index, (version, status, result) in enumerate(legal, 2):
                    conn.execute(
                        sql_insert,
                        dict(
                            base,
                            dispatch_id=f"dispatch_20260725_120000_{index:08x}",
                            idempotency_key=f"n34-{index}",
                            thread_ref=f"n34-{index}",
                            policy_version=version,
                            status=status,
                            result=result,
                        ),
                    )
                illegal = (
                    ("v2", "closed", None),
                    ("v2", "in_flight", "satisfied"),
                    ("v1", "closed", "satisfied"),
                    ("v1", "closed", "blocked"),
                    ("v1", "closed", "other"),
                )
                for index, (version, status, result) in enumerate(illegal, 20):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(
                            sql_insert,
                            dict(
                                base,
                                dispatch_id=f"dispatch_20260725_120000_{index:08x}",
                                idempotency_key=f"n34-{index}",
                                thread_ref=f"n34-{index}",
                                policy_version=version,
                                status=status,
                                result=result,
                            ),
                        )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "update dispatch_ledger set status='in_flight' "
                        "where idempotency_key='n34-3'"
                    )

    def test_legacy_global_idempotency_migration_preserves_rows_and_result_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite"
            columns = (
                "dispatch_id", "parent_dispatch_id", "idempotency_key", "message_id",
                "thread_ref", "spawn_handle", "recipient_actor_id", "producer_actor_id",
                "originating_actor_id", "policy_name", "policy_version", "policy_issued_by",
                "expected_close_by", "status", "created_at", "spawned_at", "closed_at",
                "dlq_at", "override_reason", "failure_reason", "auth_lineage_key",
                "auth_lineage_claimed_at", "observed_values_json",
            )
            with sqlite3.connect(path) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute(
                    """
                    create table actors(
                      id text primary key,
                      kind text not null,
                      display_name text not null,
                      system_class text,
                      system_instance text,
                      team text,
                      role text,
                      project_root text,
                      capabilities_json text not null default '[]',
                      wake_policy text,
                      runtime_kind text,
                      runtime_config_json text,
                      dispatch_cap integer not null default 4,
                      protected integer not null default 0,
                      active integer not null default 1,
                      created_at text not null,
                      last_seen_at text not null
                    )
                    """
                )
                actor_ids = tuple(
                    f"{prefix}-{index}"
                    for index in (1, 2)
                    for prefix in ("worker", "architect", "origin", "issuer")
                )
                conn.executemany(
                    "insert into actors(id,kind,display_name,created_at,last_seen_at) "
                    "values(?,'agent',?,'created','seen')",
                    ((actor_id, actor_id) for actor_id in actor_ids),
                )
                conn.execute(
                    """
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
                      observed_values_json text not null default '{}'
                    )
                    """
                )
                rows = [
                    (
                        f"dispatch_legacy_{index}", None, f"legacy-key-{index}", None,
                        f"thread-{index}", f"spawn-{index}", f"worker-{index}",
                        f"architect-{index}", f"origin-{index}", "legacy-policy", "v1",
                        f"issuer-{index}", f"deadline-{index}", "in_flight",
                        f"created-{index}", f"spawned-{index}", None, None,
                        f"override-{index}", f"failure-{index}", f"lineage-{index}",
                        f"claimed-{index}", f'{{"index":{index}}}',
                    )
                    for index in (1, 2)
                ]
                placeholders = ",".join("?" for _ in columns)
                conn.executemany(
                    f"insert into dispatch_ledger({','.join(columns)}) values({placeholders})",
                    rows,
                )
                before = [
                    tuple(row[column] for column in columns)
                    for row in conn.execute("select * from dispatch_ledger order by dispatch_id")
                ]

            Database(path).init()

            with sqlite3.connect(path) as conn:
                conn.row_factory = sqlite3.Row
                unique_indexes = {
                    tuple(
                        column["name"]
                        for column in conn.execute(f"pragma index_info({index['name']})")
                    )
                    for index in conn.execute("pragma index_list(dispatch_ledger)")
                    if index["unique"]
                }
                self.assertIn(("producer_actor_id", "idempotency_key"), unique_indexes)
                self.assertNotIn(("idempotency_key",), unique_indexes)
                self.assertIn(
                    "result",
                    {row["name"] for row in conn.execute("pragma table_info(dispatch_ledger)")},
                )
                after = [
                    tuple(row[column] for column in columns)
                    for row in conn.execute("select * from dispatch_ledger order by dispatch_id")
                ]
                self.assertEqual(after, before)
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into dispatch_ledger(
                          dispatch_id,idempotency_key,thread_ref,recipient_actor_id,
                          producer_actor_id,originating_actor_id,policy_name,
                          policy_version,policy_issued_by,status,created_at,result
                        ) values('dispatch_bad','bad','bad','worker','architect','architect',
                          'legacy-policy','v1','architect','closed','created','satisfied')
                        """
                    )

    def test_result_column_is_additive_on_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite"
            store = Store(path)
            store.init()
            with store.connection() as conn:
                columns = {row["name"] for row in conn.execute("pragma table_info(dispatch_ledger)")}
                self.assertIn("result", columns)
                self.assertEqual(
                    conn.execute("pragma user_version").fetchone()[0], LEDGER_SCHEMA_VERSION
                )

    def test_new_dispatch_uses_compiled_worker_policy_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "ledger.sqlite")
            store.register_agent_actor(
                "architect", "team", "architect", str(root / "architect"), []
            )
            store.register_agent_actor(
                "worker", "team", "worker", str(root / "worker"), [],
                owner="architect",
            )

            dispatch = store.dispatch_agent(
                "architect", "worker", "policy-version", "subject", "body", []
            )
            with store.connection() as conn:
                row = conn.execute(
                    "select policy_version from dispatch_ledger where dispatch_id=?",
                    (dispatch["dispatch_id"],),
                ).fetchone()
            compiled = policies.compile_policy(policies.WORKER_DISPATCH_POLICY)

            self.assertEqual(
                compiled.version,
                row["policy_version"],
            )
            self.assertEqual(
                row["policy_version"],
                compiled.env["WAKE_POLICY_VERSION"],
            )
            self.assertEqual(compiled.env["WAKE_POLICY_VERSION"], "v2")

    def test_worker_policy_version_has_one_authoritative_definition(self) -> None:
        package_root = Path(policies.__file__).resolve().parents[1]
        assignments = []
        for path in package_root.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        node.targets
                        if isinstance(node, ast.Assign)
                        else [node.target]
                    )
                if any(
                    isinstance(target, ast.Name)
                    and target.id == "WORKER_DISPATCH_POLICY_VERSION"
                    for target in targets
                ):
                    assignments.append(path.relative_to(package_root).as_posix())

        self.assertEqual(assignments, ["policies/__init__.py"])
        ledger_tree = ast.parse(
            (package_root / "dispatch_ledger.py").read_text()
        )
        self.assertTrue(
            any(
                isinstance(node, ast.ImportFrom)
                and node.module == "policies"
                and any(
                    alias.name == "WORKER_DISPATCH_POLICY_VERSION"
                    for alias in node.names
                )
                for node in ledger_tree.body
            )
        )
        claude_args = render_spawn("claude", "worker")["args"]
        claude_config = json.loads(
            claude_args[claude_args.index("--mcp-config") + 1].format(
                db_path="/tmp/db",
                actor_id="worker",
                mcp_command="/tmp/mcp",
                project_root="/tmp/project",
            )
        )
        self.assertEqual(
            claude_config["mcpServers"]["agent-comms"]["env"]["WAKE_POLICY_VERSION"],
            policies.WORKER_DISPATCH_POLICY_VERSION,
        )

        from agent_comms import provisioning
        import tomllib
        from tests import dispatch_cell_harness

        provisioned = tomllib.loads(
            provisioning.render_codex_base_config("worker", "/tmp/project")
        )
        self.assertEqual(
            provisioned["mcp_servers"]["agent-comms"]["env"]["WAKE_POLICY_VERSION"],
            policies.WORKER_DISPATCH_POLICY_VERSION,
        )
        self.assertIn(
            'f\'WAKE_POLICY_VERSION = "{WORKER_DISPATCH_POLICY_VERSION}"\'',
            Path(dispatch_cell_harness.__file__).read_text(),
        )
        for path in (
            package_root / "spawn.py",
            package_root / "provisioning.py",
            Path(dispatch_cell_harness.__file__),
        ):
            self.assertNotIn(
                'WAKE_POLICY_VERSION = "v1"',
                path.read_text(),
                path.as_posix(),
            )


class CoreResultSemanticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker_root = self.root / "worker"
        self.worker_root.mkdir()
        self.store = Store(self.root / "ledger.sqlite")
        self.human_id = "01M36YTJV9XBW95S6ZWV47C4RG"
        self.store.register_actor(self.human_id, "human", "human")
        self.store.register_agent_actor(
            "architect", "team", "architect", str(self.root / "architect"), []
        )
        self.store.register_agent_actor(
            "worker", "team", "worker", str(self.worker_root), [],
            owner="architect",
        )

    def _dispatch(self, key: str, *, version: str = "v2") -> dict:
        dispatch = self.store.dispatch_agent(
            "architect", "worker", key, key, key, []
        )
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status='in_flight', policy_version=?, "
                "spawned_at=created_at, expected_close_by='2099-01-01T00:00:00+00:00' "
                "where dispatch_id=?",
                (version, dispatch["dispatch_id"]),
            )
        return dispatch

    def _reply(self, dispatch: dict, *, sender: str = "worker", parent: str | None = None) -> dict:
        return self.store.send_message(
            sender,
            ["architect"],
            "terminal",
            "terminal",
            [],
            parent_message_id=dispatch["message_id"] if parent is None else parent,
        )

    def _close(self, dispatch: dict, **overrides) -> dict:
        reply = overrides.pop("reply", None) or self._reply(dispatch)
        args = {
            "message_id": dispatch["message_id"],
            "result": "satisfied",
            "reply_message_id": reply["id"],
            "summary": "complete",
        }
        args.update(overrides)
        return self.store.close_dispatch("worker", **args)

    def _row(self, dispatch: dict) -> sqlite3.Row:
        with self.store.connection() as conn:
            return conn.execute(
                "select * from dispatch_ledger where dispatch_id=?",
                (dispatch["dispatch_id"],),
            ).fetchone()

    def _git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=self.worker_root, text=True
        ).strip()

    def _init_review_repo(self) -> str:
        subprocess.run(["git", "init", "-q"], cwd=self.worker_root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=self.worker_root, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=self.worker_root, check=True
        )
        subprocess.run(
            ["git", "checkout", "-qb", "worker-test"], cwd=self.worker_root, check=True
        )
        return "worker-test"

    @staticmethod
    def _g080_mutant(_manifest: bytes) -> tuple[int, dict[str, int]]:
        return 14, {"M": 2, "A": 4, "D": 4, "r": 2, "T": 1, "t": 1}

    def _install_seven_entry_fixture(self) -> dict[str, str]:
        """Install the live seven-entry tree pair and return its path oracle."""
        self._init_review_repo()
        base_files = {
            "A.txt": "delete crossed-name file\n",
            "rename-old.txt": "rename contents\n",
            "modify.txt": "before\n",
            "type.txt": "regular before typechange\n",
        }
        for name, contents in base_files.items():
            (self.worker_root / name).write_text(contents, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.worker_root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "base"], cwd=self.worker_root, check=True
        )
        (self.worker_root / "A.txt").unlink()
        (self.worker_root / "rename-old.txt").rename(
            self.worker_root / "D-renamed.txt"
        )
        (self.worker_root / "modify.txt").write_text("after\n", encoding="utf-8")
        (self.worker_root / "type.txt").unlink()
        (self.worker_root / "type.txt").symlink_to("modify.txt")
        (self.worker_root / "added-one.txt").write_text("one\n", encoding="utf-8")
        (self.worker_root / "added-two.txt").write_text("two\n", encoding="utf-8")
        # Exact oracle: the real move is deliberately measured with
        # --no-renames as D(rename-old.txt) + A(D-renamed.txt).
        return {
            "A.txt": "D",
            "D-renamed.txt": "A",
            "added-one.txt": "A",
            "added-two.txt": "A",
            "modify.txt": "M",
            "rename-old.txt": "D",
            "type.txt": "T",
        }

    def _writer_durable_state(self) -> tuple[str, ...]:
        with self.store.connection() as conn:
            return tuple(conn.iterdump())

    def _snapshot_with_git_fault(
        self, fault: str
    ) -> tuple[BaseException, list[str], tuple[str, ...], tuple[str, ...]]:
        self._install_seven_entry_fixture()
        before = self._writer_durable_state()
        # Faults are injected at the fchdir/exec Git child (the legacy delta now runs
        # over the same retained custody as the review capture path); a command fault
        # raises the custody error the diff-tree callers translate to the legacy code.
        real = git_evidence.run_git_fchdir
        calls: list[str] = []

        def faulting(worktree_fd, args, env, *rest, **kwargs):
            argv = list(args)
            is_raw = "--raw" in argv
            is_names = "--name-status" in argv
            if is_raw:
                calls.append("raw")
                if fault == "raw_command":
                    raise git_evidence.GitCustodyError(
                        "git_custody_command_failed", "raw"
                    )
                output = real(worktree_fd, args, env, *rest, **kwargs)
                if fault == "malformed_raw":
                    return b"malformed\0"
                if fault == "disagreement":
                    return output.replace(b" A\0", b" M\0", 1)
                return output
            if is_names:
                calls.append("name_status")
                if fault == "name_status_command":
                    raise git_evidence.GitCustodyError(
                        "git_custody_command_failed", "ns"
                    )
                output = real(worktree_fd, args, env, *rest, **kwargs)
                if fault == "malformed_name_status":
                    return b"malformed\0"
                return output
            return real(worktree_fd, args, env, *rest, **kwargs)

        with mock.patch.object(git_evidence, "run_git_fchdir", side_effect=faulting):
            with self.store.connection() as conn:
                try:
                    self.store._mailbox._snapshot_delta(conn, "worker")
                except BaseException as exc:
                    error = exc
                else:
                    self.fail(f"{fault} unexpectedly succeeded")
        return error, calls, before, self._writer_durable_state()

    def test_a1_writer_g080_mutant_is_rejected(self) -> None:
        self._install_seven_entry_fixture()
        before = self._writer_durable_state()
        with self.store.connection() as conn:
            with mock.patch.object(
                delta_manifest, "parse_raw_z_manifest", self._g080_mutant
            ):
                with self.assertRaisesRegex(
                    ValidationError, "delta_manifest_disagreement"
                ):
                    self.store._mailbox._snapshot_delta(conn, "worker")
        self.assertEqual(self._writer_durable_state(), before)

    def test_a3_unpatched_writer_control_agrees(self) -> None:
        expected_paths = self._install_seven_entry_fixture()
        base_tree = self._git("rev-parse", "HEAD^{tree}")
        subprocess.run(["git", "add", "-A"], cwd=self.worker_root, check=True)
        snapshot_tree = self._git("write-tree")
        raw = subprocess.run(
            ["git", "diff-tree", "-r", "--no-renames", "--raw",
             "--abbrev=40", "-z", base_tree, snapshot_tree],
            cwd=self.worker_root, stdout=subprocess.PIPE, check=True,
        ).stdout
        name_status = subprocess.run(
            ["git", "diff-tree", "-r", "--no-renames", "--name-status",
             "-z", base_tree, snapshot_tree],
            cwd=self.worker_root, stdout=subprocess.PIPE, check=True,
        ).stdout
        tokens = name_status[:-1].split(b"\0")
        actual_paths = {
            tokens[index + 1].decode(): tokens[index].decode()
            for index in range(0, len(tokens), 2)
        }
        self.assertEqual(actual_paths, expected_paths)
        self.assertEqual(
            delta_manifest.parse_and_crosscheck(raw, name_status),
            (7, {"A": 3, "D": 2, "M": 1, "T": 1}),
        )
        with self.store.connection() as conn:
            delta = self.store._mailbox._snapshot_delta(conn, "worker")
        self.assertEqual(
            (delta["entries"], delta["status_counts"]),
            (7, {"A": 3, "D": 2, "M": 1, "T": 1}),
        )

    def test_writer_raw_command_failure_is_typed_and_atomic(self) -> None:
        error, calls, before, after = self._snapshot_with_git_fault("raw_command")
        self.assertIsInstance(error, ValidationError)
        self.assertIn("delta_manifest_command_failed", str(error))
        self.assertEqual(calls, ["raw"])
        self.assertEqual(after, before)

    def test_writer_name_status_command_failure_is_typed_and_atomic(self) -> None:
        error, calls, before, after = self._snapshot_with_git_fault(
            "name_status_command"
        )
        self.assertIsInstance(error, ValidationError)
        self.assertIn("delta_name_status_command_failed", str(error))
        self.assertEqual(calls, ["raw", "name_status"])
        self.assertEqual(after, before)

    def test_writer_malformed_raw_is_typed_and_atomic(self) -> None:
        error, calls, before, after = self._snapshot_with_git_fault("malformed_raw")
        self.assertIsInstance(error, ValidationError)
        self.assertEqual(str(error), "delta_manifest_malformed")
        self.assertEqual(calls, ["raw", "name_status"])
        self.assertEqual(after, before)

    def test_writer_malformed_name_status_is_typed_and_atomic(self) -> None:
        error, calls, before, after = self._snapshot_with_git_fault(
            "malformed_name_status"
        )
        self.assertIsInstance(error, ValidationError)
        self.assertEqual(str(error), "delta_name_status_malformed")
        self.assertEqual(calls, ["raw", "name_status"])
        self.assertEqual(after, before)

    def test_writer_cross_derivation_disagreement_is_typed_and_atomic(self) -> None:
        error, calls, before, after = self._snapshot_with_git_fault("disagreement")
        self.assertIsInstance(error, ValidationError)
        self.assertEqual(str(error), "delta_manifest_disagreement")
        self.assertEqual(calls, ["raw", "name_status"])
        self.assertEqual(after, before)

    def _review_command(self, *args: str) -> tuple[int, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                rc = review.main(args)
            except SystemExit as exc:
                rc = int(exc.code) if isinstance(exc.code, int) else 1
        return rc, stderr.getvalue()

    def _prepare_review(self, record_id: str, key: str) -> Path:
        review_root = self.root / "reviews"
        # Contract-17 mark-dispatched derives a real clean integration
        # checkout whose HEAD equals the review worktree HEAD; stage it as a
        # clone of the worker repo at its current (clean, committed) baseline.
        integration = self.root / "integration"
        if not integration.exists():
            subprocess.run(
                ["git", "clone", "-q", str(self.worker_root), str(integration)],
                check=True,
            )
        brief = self.root / f"{record_id}.md"
        brief.write_text(
            "# Test\n\n## Surface\nTest.\n\n## Anti-claims\nNone.\n\n"
            "## Definition of Done\nPass.\n\n## Process\nReview.\n\n"
            "## Production surface\n- touches: none; reason: fixture\n"
        )
        dod = self.root / f"{record_id}.json"
        dod.write_text(
            json.dumps([{"id": "unit", "claim": "pass", "check_id": "green"}])
        )
        self.enterContext(
            mock.patch.object(reviewing_store, "REVIEW_ROOT", review_root)
        )
        self.enterContext(
            mock.patch.object(review.runtime_paths, "db_path", return_value=self.store.db_path)
        )
        self.enterContext(
            mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(self.root / "integration")})
        )
        rc, error = self._review_command(
            "open", "--dispatch-id", record_id, "--brief", str(brief),
            "--dod", str(dod), "--repo", str(self.worker_root),
            "--expected-producer", "architect", "--expected-recipient", "worker",
        )
        self.assertEqual((rc, error), (0, ""))
        rc, error = self._review_command(
            "brief-check", "--dispatch-id", record_id, "--clean", "--by", "codex",
            "--surface-verdict", "complete", "--surface-reason", "fixture complete",
        )
        self.assertEqual((rc, error), (0, ""))
        rc, error = self._review_command(
            "mark-dispatched", "--dispatch-id", record_id, "--idempotency-key", key,
        )
        self.assertEqual((rc, error), (0, ""))
        return review_root / f"{record_id}.json"

    def test_p1_uncommitted_worker_delta_is_snapshotted_on_production_closeout(self) -> None:
        self._init_review_repo()
        (self.worker_root / ".gitignore").write_text("ignored\n.agent-comms/\n")
        (self.worker_root / "modify").write_text("base\n")
        (self.worker_root / "delete").write_text("delete\n")
        (self.worker_root / "rename-source").write_text("rename\n")
        (self.worker_root / "mode").write_text("mode\n")
        subprocess.run(["git", "add", "-A"], cwd=self.worker_root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.worker_root, check=True)
        base = self._git("rev-parse", "HEAD")
        record_path = self._prepare_review("P1", "p1-complete")
        (self.worker_root / "modify").write_text("changed\n")
        (self.worker_root / "delete").unlink()
        (self.worker_root / "rename-source").rename(self.worker_root / "rename-target")
        (self.worker_root / "mode").chmod(0o755)
        (self.worker_root / "untracked").write_text("new\n")
        (self.worker_root / "ignored").write_text("excluded\n")
        self.assertEqual(self._git("rev-parse", "HEAD"), base)
        status = self._git("status", "--porcelain", "--untracked-files=all")
        for marker in (" M modify", "D delete", " D rename-source", " M mode", "?? rename-target", "?? untracked"):
            self.assertIn(marker, status)
        self.assertNotIn("ignored", status)
        dispatch = self._dispatch("p1-complete")
        closed = self._close(dispatch, delta=True)
        self.assertEqual(closed["result"], "satisfied")
        closeout = json.loads(self._row(dispatch)["observed_values_json"])["closeout"]
        delta = closeout["delta"]
        self.assertEqual(delta["base_commit"], base)
        self.assertEqual(delta["entries"], 6)
        self.assertEqual(delta["status_counts"], {"A": 2, "D": 2, "M": 2})
        self.assertRegex(delta["snapshot_tree"], r"^[0-9a-f]{40}$")
        self.assertRegex(delta["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(delta["snapshot_tree"], self._git("rev-parse", "HEAD^{tree}"))
        subprocess.run(["git", "add", "-A"], cwd=self.worker_root, check=True)
        subprocess.run(["git", "commit", "-qm", "architect exact snapshot"], cwd=self.worker_root, check=True)
        self.assertEqual(self._git("rev-parse", "HEAD^{tree}"), delta["snapshot_tree"])
        self.assertEqual((self.worker_root / "ignored").read_text(), "excluded\n")
        self.assertEqual(self._git("status", "--porcelain"), "")
        rc, error = self._review_command("mark-executed", "--dispatch-id", "P1")
        self.assertEqual((rc, error), (0, ""))
        record = json.loads(record_path.read_text())
        self.assertEqual(record["state"], "executed")
        evidence = record["worker_evidence"][0]
        self.assertEqual(evidence["worker_dispatch_id"], dispatch["dispatch_id"])
        self.assertEqual(evidence["delta_verification"], {
            "snapshot_tree": delta["snapshot_tree"],
            "reviewed_head_tree": delta["snapshot_tree"],
            "manifest_sha256": delta["manifest_sha256"],
            "entries": delta["entries"],
            "status_counts": delta["status_counts"],
        })

    def test_n1_reply_without_closeout_ttl_is_dlq_not_success(self) -> None:
        dispatch = self._dispatch("n1")
        self._reply(dispatch)
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set expected_close_by='2000-01-01T00:00:00+00:00' "
                "where dispatch_id=?",
                (dispatch["dispatch_id"],),
            )
        class Adapter:
            def halt(self, *_args) -> None:
                return None
        self.store.reconcile_dispatches(lambda _runtime: Adapter(), human_actor_id=self.human_id)
        self.assertEqual(self._row(dispatch)["status"], "dlq")
        self.assertIsNone(self._row(dispatch)["result"])

    def test_n2_wrong_actor_refuses_without_mutation(self) -> None:
        dispatch = self._dispatch("n2")
        self.store.register_agent_actor("other", "team", "worker", str(self.root / "other"), [], owner="architect")
        with self.assertRaisesRegex(ValidationError, "wrong_actor"):
            self.store.close_dispatch(
                "other", message_id=dispatch["message_id"], result="satisfied",
                reply_message_id="missing", summary="forged",
            )
        self.assertEqual(self._row(dispatch)["status"], "in_flight")

    def test_n3_wrong_thread_refuses(self) -> None:
        dispatch = self._dispatch("n3")
        reply = self.store.send_message("worker", ["architect"], "x", "x", [])
        with self.assertRaisesRegex(ValidationError, "wrong_thread"):
            self._close(dispatch, reply=reply)
        self.assertEqual(self._row(dispatch)["status"], "in_flight")

    def test_closeout_preserves_production_observations_written_after_initial_read(self) -> None:
        dispatch = self._dispatch("concurrent-observations")
        reply = self._reply(dispatch)
        self.store.register_agent_actor(
            "other", "team", "worker", str(self.root / "other"), [],
            owner="architect",
        )
        mismatch_reply = self.store.send_message(
            "other",
            ["architect"],
            "wrong actor",
            "wrong actor",
            [],
            parent_message_id=dispatch["message_id"],
        )
        original_measure = self.store._mailbox._measure_artifacts
        crossed_window = []

        def append_observations(conn, agent_id, artifacts):
            crossed_window.append("entered")
            self.assertTrue(
                self.store.write_worker_usage(
                    dispatch["dispatch_id"], {"input_tokens": 17, "output_tokens": 23}
                )
            )

            class Adapter:
                def halt(self, *_args) -> None:
                    return None

            actions = self.store.reconcile_dispatches(
                lambda _runtime: Adapter(), human_actor_id=self.human_id
            )
            self.assertIn(
                {
                    "dispatch_id": dispatch["dispatch_id"],
                    "status": "reply_actor_mismatch",
                },
                actions,
            )
            with self.store.connection() as observer:
                observed_during_window = json.loads(
                    observer.execute(
                        "select observed_values_json from dispatch_ledger "
                        "where dispatch_id=?",
                        (dispatch["dispatch_id"],),
                    ).fetchone()["observed_values_json"]
                )
            self.assertEqual(
                observed_during_window["worker_usage"],
                {"input_tokens": 17, "output_tokens": 23},
            )
            self.assertEqual(
                observed_during_window["reply_actor_mismatch"]["message_id"],
                mismatch_reply["id"],
            )
            crossed_window.append("writers_committed")
            return original_measure(conn, agent_id, artifacts)

        with mock.patch.object(
            self.store._mailbox,
            "_measure_artifacts",
            side_effect=append_observations,
        ):
            self.store.close_dispatch(
                "worker",
                message_id=dispatch["message_id"],
                result="satisfied",
                reply_message_id=reply["id"],
                summary="complete",
            )

        self.assertEqual(crossed_window, ["entered", "writers_committed"])
        row = self._row(dispatch)
        observed = json.loads(row["observed_values_json"])
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["result"], "satisfied")
        self.assertEqual(
            observed["worker_usage"],
            {"input_tokens": 17, "output_tokens": 23},
        )
        self.assertEqual(
            observed["reply_actor_mismatch"]["message_id"],
            mismatch_reply["id"],
        )
        self.assertEqual(observed["closeout"]["reply_message_id"], reply["id"])

    def test_n7_tampered_artifact_refuses(self) -> None:
        dispatch = self._dispatch("n7")
        artifact = self.worker_root / "artifact"
        artifact.write_bytes(b"actual")
        with self.assertRaisesRegex(ValidationError, "artifact_mismatch"):
            self._close(
                dispatch,
                artifacts=[{"path": "artifact", "sha256": "0" * 64, "bytes": 6}],
            )
        self.assertEqual(self._row(dispatch)["status"], "in_flight")

    def test_n8_artifact_is_rechecked_at_review_binding(self) -> None:
        self._assert_n8_stale_artifact("working-tree")

    def test_n8_committed_artifact_blob_is_rechecked_at_review_binding(self) -> None:
        self._assert_n8_stale_artifact("committed-blob")

    def _assert_n8_stale_artifact(self, variant: str) -> None:
        self._init_review_repo()
        (self.worker_root / ".gitignore").write_text("ignored-artifact\n.agent-comms/\n")
        (self.worker_root / "base").write_text("base\n")
        if variant == "working-tree":
            (self.worker_root / "tracked-artifact").write_bytes(b"before")
        subprocess.run(["git", "add", "-A"], cwd=self.worker_root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.worker_root, check=True)
        key = f"n8-{variant}"
        record_path = self._prepare_review(f"N8-{variant}", key)
        (self.worker_root / "work").write_text("intended\n")
        artifact = (
            self.worker_root / "tracked-artifact"
            if variant == "working-tree"
            else self.worker_root / "ignored-artifact"
        )
        if variant == "committed-blob":
            artifact.write_bytes(b"before")
        dispatch = self._dispatch(key)
        self._close(
            dispatch, delta=True, artifacts=[{
                "path": str(artifact) if variant == "working-tree" else artifact.name,
                "sha256": hashlib.sha256(b"before").hexdigest(), "bytes": 6,
            }],
        )
        subprocess.run(["git", "add", "work"], cwd=self.worker_root, check=True)
        subprocess.run(["git", "commit", "-qm", "architect snapshot"], cwd=self.worker_root, check=True)
        if variant == "working-tree":
            artifact.write_bytes(b"after")
            subprocess.run(
                ["git", "update-index", "--assume-unchanged", "tracked-artifact"],
                cwd=self.worker_root, check=True,
            )
        self.assertEqual(self._git("status", "--porcelain"), "")
        before = json.loads(record_path.read_text())
        rc, error = self._review_command(
            "mark-executed", "--dispatch-id", f"N8-{variant}"
        )
        self.assertEqual(rc, 1)
        self.assertIn("stale_artifact_evidence", error)
        after = json.loads(record_path.read_text())
        self.assertEqual(after["state"], "dispatched")
        self.assertEqual(after["worker_evidence"], [])
        self.assertEqual(after["history"], before["history"])

    def test_n9_symlink_escape_refuses(self) -> None:
        dispatch = self._dispatch("n9")
        outside = self.root / "outside"
        outside.write_bytes(b"x")
        (self.worker_root / "link").symlink_to(outside)
        with self.assertRaisesRegex(ValidationError, "artifact_path_escape"):
            self._close(
                dispatch,
                artifacts=[{
                    "path": "link",
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                    "bytes": 1,
                }],
            )

    def test_n11_closeout_replay_and_conflict(self) -> None:
        dispatch = self._dispatch("n11")
        reply = self._reply(dispatch)
        first = self._close(dispatch, reply=reply)
        replay = self._close(dispatch, reply=reply)
        self.assertEqual(replay, first)
        with self.assertRaisesRegex(ValidationError, "closeout_conflict"):
            self._close(dispatch, reply=reply, summary="different")

    def test_n13_delta_contract_distinguishes_snapshot_and_no_snapshot(self) -> None:
        dispatch = self._dispatch("n13-none")
        self._close(dispatch, artifacts=[], delta=False)
        self.assertIsNone(
            json.loads(self._row(dispatch)["observed_values_json"])["closeout"]["delta"]
        )
        subprocess.run(["git", "init", "-q"], cwd=self.worker_root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.worker_root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.worker_root, check=True)
        (self.worker_root / "base").write_text("base")
        subprocess.run(["git", "add", "base"], cwd=self.worker_root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.worker_root, check=True)
        (self.worker_root / "delta").write_text("delta")
        delta_dispatch = self._dispatch("n13-delta")
        self._close(delta_dispatch, artifacts=[], delta=True)
        self.assertIsNotNone(
            json.loads(self._row(delta_dispatch)["observed_values_json"])["closeout"]["delta"]
        )

    def test_n16_v1_and_v2_terminal_verbs_key_per_row(self) -> None:
        v2 = self._dispatch("n16-v2")
        self._reply(v2)
        with self.assertRaisesRegex(ValidationError, "v2 dispatch trigger"):
            self.store.close_message("worker", v2["message_id"], "done")
        with self.assertRaisesRegex(ValidationError, "v2 dispatch trigger"):
            self.store.ack_message("worker", v2["message_id"], "done")
        v1 = self._dispatch("n16-v1", version="v1")
        self._reply(v1)
        self.store.close_message("worker", v1["message_id"], "done")
        row = self._row(v1)
        self.assertEqual(row["status"], "closed")
        self.assertTrue(json.loads(row["observed_values_json"])["legacy_recipient_terminal_close"])

    def test_n17_v2_legacy_terminal_is_protocol_dlq_and_pages_producer(self) -> None:
        dispatch = self._dispatch("n17")
        with self.store.connection() as conn:
            conn.execute(
                "update message_recipients set status='closed', closed_at='x' "
                "where message_id=? and to_agent='worker'",
                (dispatch["message_id"],),
            )
            conn.execute(
                "update dispatch_ledger set expected_close_by='2000-01-01T00:00:00+00:00' "
                "where dispatch_id=?",
                (dispatch["dispatch_id"],),
            )
        actions = self.store.reconcile_dispatches(
            lambda _runtime: mock.Mock(), human_actor_id=self.human_id
        )
        row = self._row(dispatch)
        self.assertEqual((row["status"], row["failure_reason"], row["result"]),
                         ("dlq", "closeout_missing_protocol_failure", None))
        self.assertTrue(any(action["status"] == "producer_paged" for action in actions))

    def test_n18_v1_created_after_v2_still_uses_v1_protocol(self) -> None:
        v2 = self._dispatch("n18-v2")
        self._close(v2)
        v1 = self._dispatch("n18-v1", version="v1")
        self._reply(v1)
        self.store.ack_message("worker", v1["message_id"], "done")
        self.assertEqual((self._row(v2)["result"], self._row(v1)["result"]),
                         ("satisfied", None))

    def test_n19_database_check_is_behavioral(self) -> None:
        # The exhaustive shipped-schema truth table is exercised above; this
        # production closeout proves its legal v2 terminal leg.
        dispatch = self._dispatch("n19")
        self._close(dispatch)
        self.assertEqual((self._row(dispatch)["status"], self._row(dispatch)["result"]),
                         ("closed", "satisfied"))

    def test_n20_old_style_v1_operations_and_schema_version(self) -> None:
        dispatch = self._dispatch("n20", version="v1")
        self._reply(dispatch)
        self.store.close_message("worker", dispatch["message_id"], "legacy")
        with self.store.connection() as conn:
            self.assertEqual(
                conn.execute("pragma user_version").fetchone()[0], LEDGER_SCHEMA_VERSION
            )
        self.assertEqual(self._row(dispatch)["status"], "closed")

    def test_n35_legacy_transactions_roll_back_atomically_on_v2(self) -> None:
        # S1: byte-exact pre-W1 mailbox.close_message transaction shape.
        for version in ("v2", "v1"):
            with self.subTest(shape="mailbox-close", version=version):
                dispatch = self._dispatch(f"n35-close-{version}", version=version)
                with self.store.connection() as conn:
                    before = tuple(conn.execute(
                        "select * from message_recipients where message_id=? and to_agent='worker'",
                        (dispatch["message_id"],),
                    ).fetchone())
                    conn.execute("begin immediate")
                    conn.execute(
                        "update message_recipients set status='closed', "
                        "read_at=coalesce(read_at,?), closed_at=?, "
                        "ack_response=case when ?='' then ack_response else ? end "
                        "where to_agent=? and message_id=?",
                        ("close-now", "close-now", "done", "done", "worker", dispatch["message_id"]),
                    )
                    try:
                        conn.execute(
                            "update dispatch_ledger set status='closed', closed_at=?, "
                            "observed_values_json=json_set(coalesce(nullif(observed_values_json,''),'{}'),"
                            "'$.recipient_closed_at',?) where message_id=? and "
                            "recipient_actor_id=? and status='in_flight'",
                            ("close-now", "close-now", dispatch["message_id"], "worker"),
                        )
                        conn.commit()
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        if version == "v1":
                            raise
                    after = tuple(conn.execute(
                        "select * from message_recipients where message_id=? and to_agent='worker'",
                        (dispatch["message_id"],),
                    ).fetchone())
                row = self._row(dispatch)
                if version == "v2":
                    self.assertEqual(after, before)
                    self.assertEqual((row["status"], row["result"], row["closed_at"]),
                                     ("in_flight", None, None))
                else:
                    self.assertNotEqual(after, before)
                    self.assertEqual((row["status"], row["result"], row["closed_at"]),
                                     ("closed", None, "close-now"))

        # S2: byte-exact pre-W1 mailbox.ack_message transaction shape.
        for version in ("v2", "v1"):
            with self.subTest(shape="mailbox-ack", version=version):
                dispatch = self._dispatch(f"n35-ack-{version}", version=version)
                with self.store.connection() as conn:
                    before = tuple(conn.execute(
                        "select * from message_recipients where message_id=? and to_agent='worker'",
                        (dispatch["message_id"],),
                    ).fetchone())
                    conn.execute("begin immediate")
                    conn.execute(
                        "update message_recipients set status='acknowledged', "
                        "read_at=coalesce(read_at,?), acked_at=?, ack_response=? "
                        "where to_agent=? and message_id=?",
                        ("ack-now", "ack-now", "done", "worker", dispatch["message_id"]),
                    )
                    try:
                        conn.execute(
                            "update dispatch_ledger set status='closed', closed_at=?, "
                            "observed_values_json=json_set(coalesce(nullif(observed_values_json,''),'{}'),"
                            "'$.recipient_closed_at',?) where message_id=? and "
                            "recipient_actor_id=? and status='in_flight'",
                            ("ack-now", "ack-now", dispatch["message_id"], "worker"),
                        )
                        conn.commit()
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        if version == "v1":
                            raise
                    after = tuple(conn.execute(
                        "select * from message_recipients where message_id=? and to_agent='worker'",
                        (dispatch["message_id"],),
                    ).fetchone())
                row = self._row(dispatch)
                if version == "v2":
                    self.assertEqual(after, before)
                    self.assertEqual((row["status"], row["result"], row["closed_at"]),
                                     ("in_flight", None, None))
                else:
                    self.assertNotEqual(after, before)
                    self.assertEqual((row["status"], row["result"], row["closed_at"]),
                                     ("closed", None, "ack-now"))

        # S3: byte-exact pre-W1 TTL monitor lines 1172-1182.
        for version in ("v2", "v1"):
            with self.subTest(shape="ttl-monitor", version=version):
                dispatch = self._dispatch(f"n35-ttl-{version}", version=version)
                observed = {"closed_reconciled_at": "ttl-now"}
                with self.store.connection() as conn:
                    before = tuple(conn.execute(
                        "select * from message_recipients where message_id=? and to_agent='worker'",
                        (dispatch["message_id"],),
                    ).fetchone())
                    conn.execute("begin immediate")
                    conn.execute(
                        "update message_recipients set status='closed', closed_at='ttl-now' "
                        "where message_id=? and to_agent='worker'", (dispatch["message_id"],)
                    )
                    try:
                        conn.execute(
                            "update dispatch_ledger set status='closed', "
                            "closed_at=coalesce(closed_at,?), observed_values_json=?, "
                            "auth_lineage_claimed_at=NULL where dispatch_id=? and status='in_flight'",
                            ("ttl-now", json.dumps(observed, sort_keys=True), dispatch["dispatch_id"]),
                        )
                        conn.commit()
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        if version == "v1":
                            raise
                    after = tuple(conn.execute(
                        "select * from message_recipients where message_id=? and to_agent='worker'",
                        (dispatch["message_id"],),
                    ).fetchone())
                row = self._row(dispatch)
                if version == "v2":
                    self.assertEqual(after, before)
                    self.assertEqual((row["status"], row["result"], row["closed_at"],
                                      row["auth_lineage_claimed_at"]),
                                     ("in_flight", None, None, None))
                else:
                    self.assertNotEqual(after, before)
                    self.assertEqual((row["status"], row["result"], row["closed_at"],
                                      json.loads(row["observed_values_json"])),
                                     ("closed", None, "ttl-now", observed))

        # S4: byte-exact pre-W1 liveness monitor lines 1324-1334.
        for version in ("v2", "v1"):
            with self.subTest(shape="liveness-monitor", version=version):
                dispatch = self._dispatch(f"n35-live-{version}", version=version)
                observed = {"liveness_reconciled_closed_at": "live-now"}
                with self.store.connection() as conn:
                    before = tuple(conn.execute(
                        "select * from message_recipients where message_id=? and to_agent='worker'",
                        (dispatch["message_id"],),
                    ).fetchone())
                    conn.execute("begin immediate")
                    conn.execute(
                        "update message_recipients set status='acknowledged', acked_at='live-now' "
                        "where message_id=? and to_agent='worker'", (dispatch["message_id"],)
                    )
                    try:
                        conn.execute(
                            "update dispatch_ledger set status='closed', "
                            "closed_at=coalesce(closed_at,?), observed_values_json=?, "
                            "auth_lineage_claimed_at=NULL where dispatch_id=? and status='in_flight'",
                            ("live-now", json.dumps(observed, sort_keys=True), dispatch["dispatch_id"]),
                        )
                        conn.commit()
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        if version == "v1":
                            raise
                    after = tuple(conn.execute(
                        "select * from message_recipients where message_id=? and to_agent='worker'",
                        (dispatch["message_id"],),
                    ).fetchone())
                row = self._row(dispatch)
                if version == "v2":
                    self.assertEqual(after, before)
                    self.assertEqual((row["status"], row["result"], row["closed_at"],
                                      row["auth_lineage_claimed_at"]),
                                     ("in_flight", None, None, None))
                else:
                    self.assertNotEqual(after, before)
                    self.assertEqual((row["status"], row["result"], row["closed_at"],
                                      json.loads(row["observed_values_json"])),
                                     ("closed", None, "live-now", observed))

    def test_n36_prompt_is_policy_keyed_for_placeholder_literal_and_fake(self) -> None:
        policy = policies.compile_policy("worker_dispatch_readwrite_bounded")
        adapter = CodexAdapter()
        for version, expected in (("v1", DEFAULT_WORKER_PROMPT), ("v2", V2_WORKER_PROMPT)):
            context = DispatchContext(
                dispatch={
                    "policy_version": version,
                    "policy_name": "worker_dispatch_readwrite_bounded",
                },
                recipient={"id": "worker", "runtime": "codex", "project_root": str(self.worker_root)},
                message={"id": "msg-test"},
                ttl_seconds=1,
                expected_close_by="x",
                db_path=str(self.root / "ledger.sqlite"),
            )
            for spawn in (
                render_spawn("codex", "worker"),
                {"args": ["codex", DEFAULT_WORKER_PROMPT], "env": {}},
            ):
                resolved = adapter._resolved_spawn_args(context, spawn, policy)
                self.assertIn(expected.format(actor_id="worker", message_id="msg-test"), resolved)
        fake_dispatch = self._dispatch("n36-fake")
        with mock.patch.object(sys, "argv", [
            "fake_worker", "--actor-id", "worker", "--message-id",
            fake_dispatch["message_id"], "--db", str(self.root / "ledger.sqlite"),
        ]):
            from agent_comms.adapters import fake_worker
            self.assertEqual(fake_worker.main(), 0)
        self.assertEqual((self._row(fake_dispatch)["status"], self._row(fake_dispatch)["result"]),
                         ("closed", "satisfied"))


if __name__ == "__main__":
    unittest.main()
