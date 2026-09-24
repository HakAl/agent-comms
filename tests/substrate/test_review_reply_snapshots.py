"""Behavior tests for the review evidence-lifecycle reply snapshot (Landing 2).

A review-bound implementation reply atomically publishes an immutable delta
snapshot; ``close_dispatch`` consumes the snapshot named by the exact reply
independent of the caller's legacy ``delta`` flag; and ordinary / non-review v2
delivery is unchanged. The DoD's numbered proofs (1-11) are mapped onto the test
names below.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import review
from agent_comms.db import Database, LEDGER_SCHEMA_VERSION
from agent_comms.reviewing import git_evidence, reply_snapshots
from agent_comms.store import Store
from agent_comms.reviewing import store as reviewing_store


class ReplySnapshotHarness(unittest.TestCase):
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
            "worker", "team", "worker", str(self.worker_root), [], owner="architect"
        )
        self._branch = self._init_review_repo()

    # -- git / repo helpers --------------------------------------------------

    def _git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=self.worker_root, text=True
        ).strip()

    def _init_review_repo(self) -> str:
        subprocess.run(["git", "init", "-q"], cwd=self.worker_root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@example.invalid"],
            cwd=self.worker_root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], cwd=self.worker_root, check=True
        )
        subprocess.run(
            ["git", "checkout", "-qb", "worker-test"], cwd=self.worker_root, check=True
        )
        (self.worker_root / ".gitignore").write_text(".agent-comms/\n")
        (self.worker_root / "seed").write_text("seed\n")
        subprocess.run(["git", "add", "-A"], cwd=self.worker_root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "base"], cwd=self.worker_root, check=True
        )
        return "worker-test"

    def _make_changes(self) -> None:
        (self.worker_root / "added.txt").write_text("new\n")
        (self.worker_root / "seed").write_text("changed\n")

    def _commit_candidate(self) -> None:
        subprocess.run(["git", "add", "-A"], cwd=self.worker_root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "candidate"], cwd=self.worker_root, check=True
        )

    # -- review CLI harness --------------------------------------------------

    def _review(self, *args: str) -> tuple[int, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                rc = review.main(args)
            except SystemExit as exc:
                rc = int(exc.code) if isinstance(exc.code, int) else 1
        return rc, stderr.getvalue()

    def _prepare_review(self, record_id: str, key: str) -> Path:
        review_root = self.root / "reviews"
        integration = self.root / "integration"
        if not integration.exists():
            subprocess.run(
                ["git", "clone", "-q", str(self.worker_root), str(integration)],
                check=True,
            )
        brief = self.root / f"{record_id}.md"
        brief.write_text(
            "# T\n\n## Surface\nT.\n\n## Anti-claims\nNone.\n\n"
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
            mock.patch.object(
                review.runtime_paths, "db_path", return_value=self.store.db_path
            )
        )
        self.enterContext(
            mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(integration)})
        )
        rc, err = self._review(
            "open",
            "--dispatch-id",
            record_id,
            "--brief",
            str(brief),
            "--dod",
            str(dod),
            "--repo",
            str(self.worker_root),
            "--expected-producer",
            "architect",
            "--expected-recipient",
            "worker",
        )
        self.assertEqual((rc, err), (0, ""))
        rc, err = self._review(
            "brief-check",
            "--dispatch-id",
            record_id,
            "--clean",
            "--by",
            "codex",
            "--surface-verdict",
            "complete",
            "--surface-reason",
            "ok",
        )
        self.assertEqual((rc, err), (0, ""))
        rc, err = self._review(
            "mark-dispatched", "--dispatch-id", record_id, "--idempotency-key", key
        )
        self.assertEqual((rc, err), (0, ""))
        return review_root / f"{record_id}.json"

    def _dispatch(self, key: str) -> dict:
        dispatch = self.store.dispatch_agent("architect", "worker", key, key, key, [])
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status='in_flight', policy_version='v2', "
                "spawned_at=created_at, expected_close_by='2099-01-01T00:00:00+00:00' "
                "where dispatch_id=?",
                (dispatch["dispatch_id"],),
            )
        return dispatch

    def _bind_dispatch(self, record_id: str, key: str) -> dict:
        """Full review-bound implementation dispatch on the current worktree."""
        self._prepare_review(record_id, key)
        return self._dispatch(key)

    def _reply(self, dispatch: dict, *, parent: str | None = None) -> dict:
        return self.store.send_message(
            "worker",
            ["architect"],
            "reply",
            "reply",
            [],
            parent_message_id=dispatch["message_id"] if parent is None else parent,
        )

    def _snapshot_rows(self, dispatch: dict | None = None) -> list[sqlite3.Row]:
        with self.store.connection() as conn:
            if dispatch is None:
                return conn.execute("select * from review_reply_snapshots").fetchall()
            return conn.execute(
                "select * from review_reply_snapshots where dispatch_id=?",
                (dispatch["dispatch_id"],),
            ).fetchall()

    def _closeout(self, dispatch: dict) -> dict:
        with self.store.connection() as conn:
            row = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id=?",
                (dispatch["dispatch_id"],),
            ).fetchone()
        return json.loads(row["observed_values_json"])["closeout"]

    def _mark_executed(self, record_id: str) -> tuple[int, str]:
        return self._review("mark-executed", "--dispatch-id", record_id)


class ReplySnapshotPublicationTest(ReplySnapshotHarness):
    def test_bound_reply_publishes_snapshot_row(self) -> None:
        dispatch = self._bind_dispatch("R1", "r1")
        base = self._git("rev-parse", "HEAD")
        self._make_changes()
        reply = self._reply(dispatch)
        rows = self._snapshot_rows(dispatch)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["reply_message_id"], reply["id"])
        self.assertEqual(row["recipient_actor_id"], "worker")
        self.assertEqual(row["round_kind"], "implementation")
        self.assertEqual(row["base_commit"], base)
        self.assertEqual(row["measured_head"], base)
        self.assertEqual(row["entry_count"], 2)
        self.assertEqual(json.loads(row["status_counts_json"]), {"A": 1, "M": 1})
        self.assertRegex(row["snapshot_tree"], r"^[0-9a-f]{40}$")
        self.assertNotEqual(row["snapshot_tree"], self._git("rev-parse", "HEAD^{tree}"))

    def test_ordinary_message_publishes_no_snapshot_and_no_git(self) -> None:
        # Proof 10 (part): an ordinary message runs zero snapshot Git children.
        calls: list[tuple] = []
        real = git_evidence.run_git_fchdir

        def record(*a, **k):
            calls.append(a)
            return real(*a, **k)

        self.store.send_message("architect", ["worker"], "hello", "hi", [])
        with mock.patch.object(git_evidence, "run_git_fchdir", record):
            self.store.send_message(
                "architect",
                ["worker"],
                "plain",
                "again",
                [],
            )
        self.assertEqual(calls, [])
        self.assertEqual(self._snapshot_rows(), [])

    def test_non_bound_reply_is_ordinary(self) -> None:
        # A v2 in-flight dispatch with NO bound intent: reply stays ordinary.
        dispatch = self._dispatch("plain-v2")
        self._make_changes()
        reply = self._reply(dispatch)
        self.assertEqual(self._snapshot_rows(dispatch), [])
        # And a plain non-dispatch parent stays ordinary too.
        parent = self.store.send_message("architect", ["worker"], "q", "q?", [])
        self.store.send_message(
            "worker", ["architect"], "re", "re", [], parent_message_id=parent["id"]
        )
        self.assertEqual(self._snapshot_rows(), [])
        self.assertTrue(reply["id"])


class ReplySnapshotCloseoutTest(ReplySnapshotHarness):
    def _close(self, dispatch: dict, reply: dict, **kw) -> dict:
        args = {
            "message_id": dispatch["message_id"],
            "result": "satisfied",
            "reply_message_id": reply["id"],
            "summary": "done",
        }
        args.update(kw)
        return self.store.close_dispatch("worker", **args)

    def test_proof1_delta_false_still_records_and_mark_executed_succeeds(self) -> None:
        dispatch = self._bind_dispatch("P1", "p1")
        base = self._git("rev-parse", "HEAD")
        self._make_changes()
        reply = self._reply(dispatch)
        closed = self._close(dispatch, reply, delta=False)
        self.assertEqual(closed["result"], "satisfied")
        delta = self._closeout(dispatch)["delta"]
        self.assertEqual(delta["base_commit"], base)
        self.assertEqual(delta["entries"], 2)
        self._commit_candidate()
        self.assertEqual(self._git("rev-parse", "HEAD^{tree}"), delta["snapshot_tree"])
        rc, err = self._mark_executed("P1")
        self.assertEqual((rc, err), (0, ""))

    def test_proof2_architect_commit_before_close_consumes_presnapshot(self) -> None:
        dispatch = self._bind_dispatch("P2", "p2")
        self._make_changes()
        reply = self._reply(dispatch)
        # Architect commits the exact candidate BEFORE close; the worktree is now
        # clean, so a live snapshot would be empty. Close must consume the named
        # pre-commit snapshot and never touch the worktree.
        self._commit_candidate()
        self.assertEqual(self._git("status", "--porcelain"), "")
        closed = self._close(dispatch, reply, delta=True)
        self.assertEqual(closed["result"], "satisfied")
        delta = self._closeout(dispatch)["delta"]
        self.assertEqual(delta["entries"], 2)
        self.assertEqual(self._git("rev-parse", "HEAD^{tree}"), delta["snapshot_tree"])
        rc, err = self._mark_executed("P2")
        self.assertEqual((rc, err), (0, ""))

    def test_proof3_post_reply_mutation_makes_mark_executed_refuse(self) -> None:
        dispatch = self._bind_dispatch("P3", "p3")
        self._make_changes()
        reply = self._reply(dispatch)
        self._close(dispatch, reply, delta=False)
        # A different commit than the snapshotted candidate.
        (self.worker_root / "added.txt").write_text("tampered\n")
        self._commit_candidate()
        rc, err = self._mark_executed("P3")
        self.assertNotEqual(rc, 0)
        self.assertIn("delta_mismatch", err)

    def test_proof4_satisfied_empty_refuses_blocked_binds_empty(self) -> None:
        dispatch = self._bind_dispatch("P4", "p4")
        reply = self._reply(dispatch)  # no worktree changes: empty snapshot
        rows = self._snapshot_rows(dispatch)
        self.assertEqual(rows[0]["entry_count"], 0)
        with self.assertRaises(reply_snapshots.ReplySnapshotError) as ctx:
            self._close(dispatch, reply, result="satisfied", delta=False)
        self.assertEqual(ctx.exception.code, "reply_snapshot_empty_delta_for_satisfied")
        # A blocked close binds the same empty snapshot.
        closed = self._close(
            dispatch,
            reply,
            result="blocked",
            delta=False,
            blocked_reason="cannot proceed",
        )
        self.assertEqual(closed["result"], "blocked")
        self.assertEqual(self._closeout(dispatch)["delta"]["entries"], 0)

    def test_proof7_close_consumes_exact_named_reply_never_latest(self) -> None:
        dispatch = self._bind_dispatch("P7", "p7")
        self._make_changes()
        first = self._reply(dispatch)
        (self.worker_root / "second.txt").write_text("second\n")
        second = self._reply(dispatch)
        rows = {r["reply_message_id"]: r for r in self._snapshot_rows(dispatch)}
        self.assertEqual(set(rows), {first["id"], second["id"]})
        self.assertNotEqual(
            rows[first["id"]]["snapshot_tree"], rows[second["id"]]["snapshot_tree"]
        )
        # Closing on the FIRST reply consumes the first snapshot, not the latest.
        self._close(dispatch, first, delta=False)
        delta = self._closeout(dispatch)["delta"]
        self.assertEqual(delta["snapshot_tree"], rows[first["id"]]["snapshot_tree"])
        self.assertEqual(delta["entries"], rows[first["id"]]["entry_count"])

    def test_wrong_reply_snapshot_evidence_refuses_before_settlement(self) -> None:
        dispatch = self._bind_dispatch("PW", "pw")
        self._make_changes()
        reply = self._reply(dispatch)
        # A reply that carries no snapshot (fabricated thread) refuses.
        other = self.store.send_message(
            "worker",
            ["architect"],
            "x",
            "x",
            [],
            parent_message_id=dispatch["message_id"],
        )
        with self.store.connection() as conn:
            conn.execute(
                "delete from review_reply_snapshots where reply_message_id=?",
                (other["id"],),
            )
        with self.assertRaises(reply_snapshots.ReplySnapshotError) as ctx:
            self._close(dispatch, other, delta=False)
        self.assertEqual(ctx.exception.code, "reply_snapshot_missing")
        # The real reply still settles.
        self._close(dispatch, reply, delta=False)
        self.assertEqual(self._closeout(dispatch)["delta"]["entries"], 2)


class ReplySnapshotRefusalTest(ReplySnapshotHarness):
    def test_proof6_malformed_intent_member_refuses_no_fallback(self) -> None:
        dispatch = self._bind_dispatch("F6", "f6")
        self._make_changes()
        # Corrupt the bound intent payload (rename a canonical member) so the
        # digest no longer reconstructs: a partially bound match must refuse and
        # never fall back to ordinary delivery.
        with self.store.connection() as conn:
            row = conn.execute(
                "select payload_json from review_dispatch_intents where dispatch_id=?",
                (dispatch["dispatch_id"],),
            ).fetchone()
            payload = json.loads(row["payload_json"])
            payload["source_branch_RENAMED"] = payload.pop("source_branch")
            conn.execute(
                "update review_dispatch_intents set payload_json=? where dispatch_id=?",
                (json.dumps(payload), dispatch["dispatch_id"]),
            )
        with self.assertRaises(reply_snapshots.ReplySnapshotError) as ctx:
            self._reply(dispatch)
        self.assertIn("reply_snapshot_", ctx.exception.code)
        self.assertEqual(self._snapshot_rows(dispatch), [])
        with self.store.connection() as conn:
            msgs = conn.execute(
                "select count(*) c from message_threads where parent_message_id=?",
                (dispatch["message_id"],),
            ).fetchone()["c"]
        self.assertEqual(msgs, 0)

    def test_proof5_capture_instability_leaves_no_rows(self) -> None:
        dispatch = self._bind_dispatch("F5", "f5")
        self._make_changes()
        before = tuple(self._durable_state())
        real = reply_snapshots._measured_snapshot
        seen = {"n": 0}

        def flaky(*a, **k):
            seen["n"] += 1
            result = real(*a, **k)
            if seen["n"] == 2:  # perturb the second isolated snapshot
                result = dict(result, entries=result["entries"] + 1)
            return result

        with mock.patch.object(reply_snapshots, "_measured_snapshot", flaky):
            with self.assertRaises(reply_snapshots.ReplySnapshotError) as ctx:
                self._reply(dispatch)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.code, "reply_snapshot_unstable")
        self.assertEqual(tuple(self._durable_state()), before)

    def test_sql_revalidation_failure_leaves_no_rows(self) -> None:
        dispatch = self._bind_dispatch("FR", "fr")
        self._make_changes()
        before = tuple(self._durable_state())

        def drift(conn, binding, **kw):
            raise reply_snapshots.ReplySnapshotError(
                "reply_snapshot_revalidation_failed", "x", retryable=True
            )

        with mock.patch.object(reply_snapshots, "revalidate", drift):
            with self.assertRaises(reply_snapshots.ReplySnapshotError):
                self._reply(dispatch)
        self.assertEqual(tuple(self._durable_state()), before)

    def _durable_state(self):
        with self.store.connection() as conn:
            return tuple(conn.iterdump())


class ReplySnapshotIsolationTest(ReplySnapshotHarness):
    def test_proof10_two_scans_no_status_no_real_writes(self) -> None:
        dispatch = self._bind_dispatch("I1", "i1")
        self._make_changes()
        git_dir = Path(self._git("rev-parse", "--git-dir"))
        if not git_dir.is_absolute():
            git_dir = self.worker_root / git_dir
        index_before = (git_dir / "index").read_bytes()
        objects_before = sorted(
            p.name for p in (git_dir / "objects").rglob("*") if p.is_file()
        )

        calls: list[list[str]] = []
        real = git_evidence.run_git_fchdir

        def record(worktree_fd, args, env, *a, **k):
            calls.append(list(args))
            return real(worktree_fd, args, env, *a, **k)

        insert_at = {"count": None}
        real_insert = reply_snapshots.insert_snapshot_row

        def counting_insert(*a, **k):
            insert_at["count"] = len(calls)
            return real_insert(*a, **k)

        with mock.patch.object(git_evidence, "run_git_fchdir", record):
            with mock.patch.object(
                reply_snapshots, "insert_snapshot_row", counting_insert
            ):
                self._reply(dispatch)

        add_scans = [c for c in calls if c[:2] == ["add", "-A"]]
        self.assertEqual(len(add_scans), 2)
        self.assertFalse(any("status" in c for c in calls))
        # No Git subprocess runs after the write transaction begins (the snapshot
        # row insert sees the final, frozen git call count).
        self.assertEqual(insert_at["count"], len(calls))
        # The real index and object store are never written.
        self.assertEqual((git_dir / "index").read_bytes(), index_before)
        objects_after = sorted(
            p.name for p in (git_dir / "objects").rglob("*") if p.is_file()
        )
        self.assertEqual(objects_after, objects_before)

    def test_proof11_mark_executed_uses_durable_objects_after_temp_discard(
        self,
    ) -> None:
        dispatch = self._bind_dispatch("I2", "i2")
        self._make_changes()
        reply = self._reply(dispatch)
        snapshot_tree = self._snapshot_rows(dispatch)[0]["snapshot_tree"]
        # The temporary snapshot tree is a content identity, not a durable object:
        # it is not dereferenceable from the repository after capture.
        missing = subprocess.run(
            ["git", "cat-file", "-e", snapshot_tree],
            cwd=self.worker_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.store.close_dispatch(
            "worker",
            message_id=dispatch["message_id"],
            result="satisfied",
            reply_message_id=reply["id"],
            summary="done",
            delta=False,
        )
        # The architect commits the exact bytes; mark-executed reconstructs the
        # delta from durable base and committed-head objects only.
        self._commit_candidate()
        self.assertEqual(self._git("rev-parse", "HEAD^{tree}"), snapshot_tree)
        rc, err = self._mark_executed("I2")
        self.assertEqual((rc, err), (0, ""))


class ReplySnapshotSchemaTest(ReplySnapshotHarness):
    def test_proof9_additive_table_on_old_ledger_keeps_floor_three(self) -> None:
        # A fresh ledger created before Landing 2 (no reply-snapshot table) gains
        # it additively while user_version and LEDGER_SCHEMA_VERSION stay at 3.
        db = Database(self.root / "old.sqlite")
        with db.connection() as conn:
            conn.executescript(
                "create table messages(id text primary key);pragma user_version=3;"
            )
            self.assertNotIn(
                "review_reply_snapshots",
                {
                    r[0]
                    for r in conn.execute(
                        "select name from sqlite_master where type='table'"
                    )
                },
            )
        db2 = Database(self.root / "old.sqlite")
        with db2.connection() as conn:
            db2.ensure_review_reply_snapshot_schema(conn)
            tables = {
                r[0]
                for r in conn.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
            self.assertIn("review_reply_snapshots", tables)
            self.assertEqual(conn.execute("pragma user_version").fetchone()[0], 3)
        self.assertEqual(LEDGER_SCHEMA_VERSION, 3)

    def test_snapshot_row_constraints(self) -> None:
        with self.store.connection() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "insert into review_reply_snapshots(reply_message_id, dispatch_id, "
                    "intent_id, recipient_actor_id, round_kind, base_commit, base_tree, "
                    "measured_head, snapshot_tree, manifest_sha256, entry_count, "
                    "status_counts_json, boundary_probe_sha256, snapshot_algorithm, "
                    "measured_at) values('m','d','i','worker','correction','b','t','h',"
                    "'s','x',0,'{}','p','a','now')"
                )


class ReplySnapshotCustodyTest(ReplySnapshotHarness):
    """DoD 11: the retained checkout descriptor and revalidated Git metadata
    directories bind capture to the original inode. A mid-capture rename or symlink
    swap either continues against the retained inode or refuses retryably, never
    adopting replacement bytes; the child uses fchdir/pass_fds and never preexec_fn
    or a server-process cwd mutation."""

    def _swap_before_first_add(self, action) -> object:
        """Wrap ``run_git_fchdir`` so ``action`` runs once just before the first
        ``git add -A`` worktree scan, then delegates to the real fchdir/exec child."""
        real = git_evidence.run_git_fchdir
        fired = {"done": False}

        def wrapper(worktree_fd, args, env, *a, **k):
            if not fired["done"] and list(args)[:2] == ["add", "-A"]:
                fired["done"] = True
                action()
            return real(worktree_fd, args, env, *a, **k)

        return wrapper

    def test_run_git_fchdir_reads_retained_inode_after_rename(self) -> None:
        # Retain the worktree descriptor, then rename it away and drop a DIFFERENT
        # repo at its old pathname: the fchdir/exec child reads the retained original
        # inode's HEAD, never the replacement repo's re-resolved pathname.
        original_head = self._git("rev-parse", "HEAD")
        fd, _ident = git_evidence.open_retained_dir(self.worker_root)
        try:
            os.rename(self.worker_root, self.root / "renamed_orig")
            other = self.worker_root
            other.mkdir()
            for cmd in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "e@example.invalid"],
                ["git", "config", "user.name", "E"],
            ):
                subprocess.run(cmd, cwd=other, check=True)
            (other / "f").write_text("x\n")
            subprocess.run(["git", "add", "-A"], cwd=other, check=True)
            subprocess.run(["git", "commit", "-qm", "other"], cwd=other, check=True)
            other_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=other, text=True
            ).strip()
            self.assertNotEqual(original_head, other_head)
            head = (
                git_evidence.run_git_fchdir(
                    fd, ["rev-parse", "HEAD"], git_evidence.clean_git_env()
                )
                .decode()
                .strip()
            )
            self.assertEqual(head, original_head)  # retained inode, not replacement
        finally:
            os.close(fd)

    def test_rename_to_different_checkout_refuses_retryably(self) -> None:
        # Rename the checkout and point its old pathname at a DIFFERENT directory:
        # identity revalidation sees the inode drift and refuses retryably; no
        # replacement bytes are ever adopted and nothing is published.
        dispatch = self._bind_dispatch("C2", "c2")
        self._make_changes()
        renamed = self.root / "renamed_worker2"
        decoy = self.root / "decoy_checkout"
        decoy.mkdir()

        def swap() -> None:
            os.rename(self.worker_root, renamed)
            os.symlink(decoy, self.worker_root)

        with mock.patch.object(
            git_evidence, "run_git_fchdir", self._swap_before_first_add(swap)
        ):
            with self.assertRaises(reply_snapshots.ReplySnapshotError) as ctx:
                self._reply(dispatch)
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("git_custody", ctx.exception.code)
        self.assertEqual(self._snapshot_rows(dispatch), [])
        os.unlink(self.worker_root)
        os.rename(renamed, self.worker_root)

    def test_git_dir_drift_refuses_retryably(self) -> None:
        # Equivalent Git-dir drift: swap the retained .git for a different inode
        # between steps; the custody refuses retryably and publishes nothing.
        dispatch = self._bind_dispatch("C3", "c3")
        self._make_changes()
        git_dir = self.worker_root / ".git"
        moved = self.root / "moved_git"
        decoy = self.root / "decoy_git"
        decoy.mkdir()

        def swap() -> None:
            os.rename(git_dir, moved)
            os.symlink(decoy, git_dir)

        with mock.patch.object(
            git_evidence, "run_git_fchdir", self._swap_before_first_add(swap)
        ):
            with self.assertRaises(reply_snapshots.ReplySnapshotError) as ctx:
                self._reply(dispatch)
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("git_custody", ctx.exception.code)
        self.assertEqual(self._snapshot_rows(dispatch), [])
        os.unlink(git_dir)
        os.rename(moved, git_dir)

    def test_fchdir_child_no_preexec_and_server_cwd_stable(self) -> None:
        # The child binds the retained descriptor with fchdir/pass_fds, never
        # preexec_fn, and the server process cwd is untouched across a capture.
        source = Path(git_evidence.__file__).read_text()
        self.assertNotIn("preexec_fn=", source)  # never passed to subprocess
        self.assertIn("pass_fds=", source)
        self.assertIn("fchdir", git_evidence._FCHDIR_EXEC_CHILD)
        self.assertNotIn("os.chdir", source)
        cwd_before = os.getcwd()
        dispatch = self._bind_dispatch("C4", "c4")
        self._make_changes()
        self._reply(dispatch)
        self.assertEqual(os.getcwd(), cwd_before)
        self.assertEqual(len(self._snapshot_rows(dispatch)), 1)


class BoundedIndexDigestTest(unittest.TestCase):
    """DoD 12: the real-index digest streams from one bounded, no-follow,
    nonblocking descriptor. Missing yields the absent sentinel; every irregular or
    raced input refuses with a typed custody error the caller treats as retryable."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _assert_refuses(self, path, code: str) -> None:
        with self.assertRaises(git_evidence.GitCustodyError) as ctx:
            git_evidence.bounded_regular_file_digest(path)
        self.assertIn(code, ctx.exception.code)

    def test_absent_returns_sentinel(self) -> None:
        self.assertEqual(
            git_evidence.bounded_regular_file_digest(self.dir / "nope"),
            git_evidence.INDEX_DIGEST_ABSENT,
        )

    def test_regular_file_matches_sha256(self) -> None:
        path = self.dir / "index"
        path.write_bytes(b"index-bytes")
        self.assertEqual(
            git_evidence.bounded_regular_file_digest(path),
            hashlib.sha256(b"index-bytes").hexdigest(),
        )

    def test_symlink_refuses(self) -> None:
        target = self.dir / "real"
        target.write_bytes(b"x")
        link = self.dir / "link"
        os.symlink(target, link)
        self._assert_refuses(link, "git_index_unreadable")

    def test_fifo_refuses(self) -> None:
        fifo = self.dir / "fifo"
        os.mkfifo(fifo)
        self._assert_refuses(fifo, "git_index_not_regular")

    def test_directory_refuses(self) -> None:
        self._assert_refuses(self.dir, "git_index_not_regular")

    def test_char_device_refuses(self) -> None:
        self._assert_refuses(Path("/dev/null"), "git_index_not_regular")

    def test_oversized_refuses(self) -> None:
        path = self.dir / "big"
        path.write_bytes(b"0123456789")
        with self.assertRaises(git_evidence.GitCustodyError) as ctx:
            git_evidence.bounded_regular_file_digest(path, max_bytes=4)
        self.assertIn("git_index_oversized", ctx.exception.code)

    def _drift_fstat(
        self,
        *,
        ino_delta: int = 0,
        size_delta: int = 0,
        mtime_delta: int = 0,
        ctime_delta: int = 0,
    ):
        real_fstat = os.fstat
        seen = {"n": 0}

        class _Stat:
            def __init__(self, base) -> None:
                self.st_mode = base.st_mode
                self.st_dev = base.st_dev
                self.st_ino = base.st_ino + ino_delta
                self.st_size = base.st_size + size_delta
                self.st_mtime_ns = base.st_mtime_ns + mtime_delta
                self.st_ctime_ns = base.st_ctime_ns + ctime_delta

        def drifting(fd):
            base = real_fstat(fd)
            seen["n"] += 1
            return _Stat(base) if seen["n"] >= 2 else base  # only the post-read fstat

        return drifting

    def test_size_drift_during_read_refuses(self) -> None:
        # A concurrent writer grows the file across the read: the post-EOF size check
        # refuses rather than trust a torn measurement.
        path = self.dir / "index"
        path.write_bytes(b"stable-content")
        with mock.patch.object(
            git_evidence.os, "fstat", self._drift_fstat(size_delta=64)
        ):
            with self.assertRaises(git_evidence.GitCustodyError) as ctx:
                git_evidence.bounded_regular_file_digest(path)
        self.assertIn("git_index_raced", ctx.exception.code)

    def test_inode_drift_after_read_refuses(self) -> None:
        path = self.dir / "index"
        path.write_bytes(b"stable-content")
        with mock.patch.object(
            git_evidence.os, "fstat", self._drift_fstat(ino_delta=1)
        ):
            with self.assertRaises(git_evidence.GitCustodyError) as ctx:
                git_evidence.bounded_regular_file_digest(path)
        self.assertIn("git_index_raced", ctx.exception.code)

    def test_ctime_drift_after_read_refuses(self) -> None:
        # ctime_ns is load-bearing for a same-size in-place mutation: even when
        # device, inode, size, and mtime are unchanged, a ctime change the owner
        # cannot mask with utime refuses.
        path = self.dir / "index"
        path.write_bytes(b"stable-content")
        with mock.patch.object(
            git_evidence.os, "fstat", self._drift_fstat(ctime_delta=1)
        ):
            with self.assertRaises(git_evidence.GitCustodyError) as ctx:
                git_evidence.bounded_regular_file_digest(path)
        self.assertIn("git_index_raced", ctx.exception.code)

    def test_mtime_drift_after_read_refuses(self) -> None:
        # mtime_ns is defense in depth alongside the load-bearing ctime check.
        path = self.dir / "index"
        path.write_bytes(b"stable-content")
        with mock.patch.object(
            git_evidence.os, "fstat", self._drift_fstat(mtime_delta=1)
        ):
            with self.assertRaises(git_evidence.GitCustodyError) as ctx:
                git_evidence.bounded_regular_file_digest(path)
        self.assertIn("git_index_raced", ctx.exception.code)

    def test_same_size_inplace_mutation_during_read_refuses_via_ctime(self) -> None:
        # A same-uid writer overwrites the index in place with the SAME size during
        # the streamed read and restores st_mtime_ns with utime. Device, inode,
        # size, and mtime all match afterward, so only the ctime_ns the writer
        # cannot restore forces the refusal.
        path = self.dir / "index"
        original = b"A" * 4096
        path.write_bytes(original)
        st_before = os.stat(path)
        real_read = os.read
        fired = {"done": False}

        def mutating_read(fd, n):
            chunk = real_read(fd, n)
            if chunk and not fired["done"]:
                fired["done"] = True
                with open(path, "r+b") as handle:
                    handle.seek(0)
                    handle.write(b"B" * len(original))  # same-size in-place mutation
                os.utime(path, ns=(st_before.st_atime_ns, st_before.st_mtime_ns))
            return chunk

        with mock.patch.object(git_evidence.os, "read", mutating_read):
            with self.assertRaises(git_evidence.GitCustodyError) as ctx:
                git_evidence.bounded_regular_file_digest(path)
        self.assertIn("git_index_raced", ctx.exception.code)
        st_after = os.stat(path)
        self.assertEqual(st_after.st_size, st_before.st_size)  # same size
        self.assertEqual(st_after.st_mtime_ns, st_before.st_mtime_ns)  # mtime restored
        self.assertNotEqual(  # only ctime discriminates the in-place mutation
            st_after.st_ctime_ns, st_before.st_ctime_ns
        )

    def test_index_digest_is_descriptor_relative_to_retained_git_dir(self) -> None:
        # Open the real Git dir as a retained descriptor and digest ``index``
        # relative to it. Then PERSISTENTLY rename the Git dir away and drop a decoy
        # directory carrying a different index at the old pathname: the digest still
        # reads the retained inode's index, never the replacement pathname's.
        git_dir = self.dir / ".git"
        git_dir.mkdir()
        (git_dir / "index").write_bytes(b"real-index-bytes")
        expected = hashlib.sha256(b"real-index-bytes").hexdigest()
        fd, _identity = git_evidence.open_retained_dir(git_dir)
        try:
            os.rename(git_dir, self.dir / "renamed_git")
            decoy = self.dir / ".git"
            decoy.mkdir()
            (decoy / "index").write_bytes(b"decoy-index-bytes")
            digest = git_evidence.bounded_regular_file_digest("index", dir_fd=fd)
            self.assertEqual(digest, expected)  # retained inode, not the decoy path
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
