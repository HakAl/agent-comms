import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import argparse
import copy
import contextlib
import concurrent.futures
import io
import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import types
import unittest
import hashlib
import fcntl
import threading
import time
from pathlib import Path
from unittest import mock

from agent_comms import delta_manifest, mailbox, review
from agent_comms.reviewing import (
    approval,
    checks,
    contracts,
    git_evidence,
    ledger_evidence,
    rebind,
    store,
)
from agent_comms.store import Store


class ReviewToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.review_root = self.root / "reviews"
        self.ledger_path = self.root / "ledger.sqlite"
        fixture_store = Store(self.ledger_path)
        fixture_store.init()
        fixture_store.register_agent_actor(
            "gamma-architect", "agentcomms", "architect",
            str(self.root / "architect"), [],
        )
        # The review worktree IS the worker's registered root (real team
        # topology), so dispatch_agent's re-init resyncs to it and the
        # contract-17 active intent's real_project_root matches at dispatch.
        fixture_store.register_agent_actor(
            "gamma-codex-worker",
            "agentcomms",
            "worker",
            str(self.root / "repo"),
            [],
            owner="gamma-architect",
        )
        self.ledger_patch = mock.patch.object(review.runtime_paths, "db_path", return_value=self.ledger_path)
        self.ledger_patch.start()
        self.addCleanup(self.ledger_patch.stop)
        self._evidence_serial = 0
        # Real team topology: the integration checkout owns the history and
        # the review repo is a worktree of it on the source branch, so the
        # recorded source branch resolves in the integration repository and
        # the reviewed head shares ancestry with integration HEAD.
        self.integration = self.root / "integration"
        self.init_repo(self.integration)
        self.git("checkout", "-B", "integration-main", cwd=self.integration)
        (self.integration / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=self.integration)
        self.git("commit", "-m", "base", cwd=self.integration)
        # The initial committed content lands on integration BEFORE the review
        # worktree is created, so first dispatch starts at one clean common HEAD
        # (contract 17 mark-dispatched requires source HEAD == integration HEAD).
        # _bootstrap_worker_evidence supplies the later candidate commit.
        (self.integration / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=self.integration)
        self.git("commit", "-m", "initial", cwd=self.integration)
        self.repo = self.root / "repo"
        self.git("worktree", "add", "-b", "work-branch", str(self.repo), cwd=self.integration)
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            conn.execute(
                "update actors set project_root=? where id=?",
                (str(self.repo), "gamma-codex-worker"),
            )
        self.brief = self.root / "brief.md"
        self.brief.write_text(
            "# Test brief\n"
            "\n"
            "## Surface\n"
            "Touch points.\n"
            "\n"
            "## Anti-claims\n"
            "Out of scope.\n"
            "\n"
            "## Definition of Done\n"
            "Passes.\n"
            "\n"
            "## Process\n"
            "Architect commits.\n"
            "\n"
            "## Production surface\n"
            "- touches: none; reason: test fixture\n",
            encoding="utf-8",
        )
        self.dod = self.root / "dod.json"
        self.dod.write_text(
            json.dumps([{"id": "unit", "claim": "green check", "check_id": "green"}]),
            encoding="utf-8",
        )
        self.review_root_patch = mock.patch.object(
            store, "REVIEW_ROOT", self.review_root
        )
        self.review_root_patch.start()
        self.addCleanup(self.review_root_patch.stop)
        self.env_patch = mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(self.integration)}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def init_repo(self, repo: Path) -> None:
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)

    def run_review(self, *args: str, ok: bool = True) -> int:
        args = self._fixture_args(args)
        if args[0] in {"mark-executed", "mark-blocked", "mark-superseded"} and not any(
            value.startswith("--") and value not in {"--dispatch-id", "--note"} for value in args[1:]
        ):
            dispatch_id = args[args.index("--dispatch-id") + 1]
            self._bootstrap_worker_evidence(dispatch_id, args[0])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                rc = review.main(args)
            except SystemExit as exc:
                rc = int(exc.code) if isinstance(exc.code, int) else 1
        if ok and rc != 0:
            self.fail(f"review command failed: {args}\nstdout:\n{stdout.getvalue()}\nstderr:\n{stderr.getvalue()}")
        if not ok and rc == 0:
            self.fail(f"review command unexpectedly succeeded: {args}")
        return rc

    def run_review_capture(self, *args: str) -> tuple[int, str, str]:
        args = self._fixture_args(args)
        if args[0] in {"mark-executed", "mark-blocked", "mark-superseded"} and not any(
            value.startswith("--") and value not in {"--dispatch-id", "--note"} for value in args[1:]
        ):
            dispatch_id = args[args.index("--dispatch-id") + 1]
            self._bootstrap_worker_evidence(dispatch_id, args[0])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                rc = review.main(args)
            except SystemExit as exc:
                rc = int(exc.code) if isinstance(exc.code, int) else 1
        return rc, stdout.getvalue(), stderr.getvalue()

    def _fixture_args(self, args: tuple[str, ...]) -> tuple[str, ...]:
        """Supply newly required bindings for legacy lifecycle fixtures."""
        values = list(args)
        if values[0] == "status" and "--expected-repo-root" not in values:
            values.extend(("--expected-repo-root", str(review.REPO_ROOT)))
        if values[0] == "open":
            if "--expected-producer" not in values:
                values.extend(("--expected-producer", "gamma-architect"))
            if "--expected-recipient" not in values:
                values.extend(("--expected-recipient", "gamma-codex-worker"))
        if values[0] == "mark-dispatched" and "--idempotency-key" not in values:
            dispatch_id = values[values.index("--dispatch-id") + 1]
            values.extend(("--idempotency-key", f"fixture-{dispatch_id}"))
        return tuple(values)

    def _bootstrap_worker_evidence(self, record_id: str, verb: str) -> None:
        """Create closed scratch-ledger evidence for legacy happy-path calls."""
        path = self.review_root / f"{record_id}.json"
        if not path.exists():
            return
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("state") != "dispatched" or not record.get("intended_dispatches"):
            return
        repo = Path(record["repo"])
        if not record.get("target_branch") or record.get("target_branch") == "HEAD":
            return
        probe = subprocess.run(
            ["git", "status", "--porcelain", "--branch"], cwd=repo if repo.is_dir() else None,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        ) if repo.is_dir() else None
        if probe is None or probe.returncode != 0:
            return
        lines = probe.stdout.splitlines()
        if not lines or not lines[0].startswith("## ") or lines[0].startswith("## HEAD "):
            return
        if lines[0].removeprefix("## ").split("...", 1)[0] != record["target_branch"] or len(lines) > 1:
            return
        key = record["intended_dispatches"][-1]["idempotency_key"]
        # A correction round (prior worker_evidence bound) is incremental:
        # its delta roots at the prior reviewed_head, not the original base.
        round_base = record["reviewed_head"] if record.get("worker_evidence") else record["base_commit"]
        import sqlite3
        open_row = None
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select dispatch_id, status, message_id from dispatch_ledger "
                "where producer_actor_id=? and idempotency_key=?",
                (record["expected_producer"], key),
            ).fetchone()
            if row is not None and row["status"] != "closed":
                open_row = dict(row)
            elif row is not None:
                key = f"{key}-fixture-{self._evidence_serial + 1}"
                record["intended_dispatches"].append({
                    "attempt": len(record["intended_dispatches"]) + 1,
                    "idempotency_key": key,
                    "recorded_at": "2026-07-22T12:00:00Z",
                })
                path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        head = self.git("rev-parse", "HEAD", cwd=repo)
        if verb == "mark-executed" and head == round_base:
            marker = repo / ".review-fixture-work"
            marker.write_text(f"{record_id} attempt {len(record['intended_dispatches'])}\n", encoding="utf-8")
            self.git("add", marker.name, cwd=repo)
            self.git("commit", "-m", f"fixture work for {record_id}", cwd=repo)
            head = self.git("rev-parse", "HEAD", cwd=repo)
        base_tree = self.git("rev-parse", f"{round_base}^{{tree}}", cwd=repo)
        head_tree = self.git("rev-parse", f"{head}^{{tree}}", cwd=repo)
        manifest = subprocess.run(
            ["git", "diff-tree", "-r", "--no-renames", "--raw", "--abbrev=40", "-z", base_tree, head_tree],
            cwd=repo, check=True, stdout=subprocess.PIPE,
        ).stdout
        self._evidence_serial += 1
        if open_row is None:
            worker_id = f"dispatch_20260722_120000_{self._evidence_serial:08x}"
            message_id = f"msg-trigger-{self._evidence_serial}"
        else:
            worker_id = open_row["dispatch_id"]
            message_id = open_row["message_id"]
        reply_id = f"msg-reply-{self._evidence_serial}"
        result = "blocked" if verb == "mark-blocked" else "satisfied"
        delta = None
        if verb == "mark-executed":
            name_status = subprocess.run(
                ["git", "diff-tree", "-r", "--no-renames", "--name-status", "-z",
                 base_tree, head_tree],
                cwd=repo, stdout=subprocess.PIPE, check=True,
            ).stdout
            entries, status_counts = delta_manifest.parse_and_crosscheck(
                manifest, name_status
            )
            delta = {
                "base_commit": round_base, "snapshot_tree": head_tree,
                "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                "entries": entries, "status_counts": status_counts,
            }
        closeout = {"protocol": 1, "recorded_by": record["expected_recipient"],
                    "reply_message_id": reply_id, "delta": delta}
        now = "2026-07-22T12:00:00+00:00"
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            for actor in (record["expected_producer"], record["expected_recipient"]):
                conn.execute(
                    "insert or ignore into actors(id,kind,display_name,capabilities_json,last_seen_at) values(?, 'agent', ?, '[]', ?)",
                    (actor, actor, now),
                )
            if open_row is None:
                conn.execute(
                    "insert into messages(id,from_agent,subject,body,refs_json,priority,requires_ack,created_at) values(?,?, 'fixture','fixture','[]','normal',0,?)",
                    (message_id, record["expected_producer"], now),
                )
                conn.execute("insert into message_threads(message_id,parent_message_id) values(?,NULL)", (message_id,))
                conn.execute("insert into message_recipients(message_id,to_agent,status) values(?,?,'closed')", (message_id, record["expected_recipient"]))
            conn.execute(
                "insert into messages(id,from_agent,subject,body,refs_json,priority,requires_ack,created_at) values(?,?, 'fixture','fixture','[]','normal',0,?)",
                (reply_id, record["expected_recipient"], now),
            )
            conn.execute("insert into message_threads(message_id,parent_message_id) values(?,?)", (reply_id, message_id))
            conn.execute("insert into message_recipients(message_id,to_agent,status) values(?,?,'sent')", (reply_id, record["expected_producer"]))
            if open_row is None:
                conn.execute(
                    """insert into dispatch_ledger(dispatch_id,idempotency_key,message_id,thread_ref,
                       recipient_actor_id,producer_actor_id,originating_actor_id,policy_name,policy_version,
                       policy_issued_by,status,created_at,closed_at,result,observed_values_json)
                       values(?,?,?,?,?,?,?,?,? ,?,'closed',?,?,?,?)""",
                    (worker_id, key, message_id, message_id, record["expected_recipient"],
                     record["expected_producer"], record["expected_producer"], "fixture", "v2",
                     record["expected_producer"], now, now, result,
                     json.dumps({"closeout": closeout}, sort_keys=True)),
                )
            else:
                conn.execute(
                    "update dispatch_ledger set status='closed', closed_at=?, result=?, "
                    "observed_values_json=? where dispatch_id=?",
                    (now, result, json.dumps({"closeout": closeout}, sort_keys=True), worker_id),
                )

    def record(self, dispatch_id: str = "D1") -> dict:
        return json.loads((self.review_root / f"{dispatch_id}.json").read_text(encoding="utf-8"))

    def write_record(self, record: dict, dispatch_id: str = "D1") -> None:
        self.review_root.mkdir(parents=True, exist_ok=True)
        (self.review_root / f"{dispatch_id}.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return proc.stdout.strip()

    def generate_approval_key(self, name: str = "approval-key") -> Path:
        key_path = self.root / name
        if key_path.exists():
            index = 2
            while (self.root / f"{name}-{index}").exists():
                index += 1
            key_path = self.root / f"{name}-{index}"
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path), "-q"], check=True, stdout=subprocess.PIPE)
        return key_path

    def commit_signers(self, key_path: Path | None = None, repo: Path | None = None, ref: str | None = None) -> Path:
        repo = repo or self.repo
        ref = ref or review.APPROVAL_INTEGRATION_REF
        key_path = key_path or self.generate_approval_key()
        self.git("checkout", "-B", ref, cwd=repo)
        signers = repo / "config" / "approval-signers"
        signers.parent.mkdir(parents=True, exist_ok=True)
        signers.write_text(f"agent-comms-approver {(key_path.with_suffix('.pub')).read_text(encoding='utf-8')}", encoding="utf-8")
        self.git("add", "config/approval-signers", cwd=repo)
        self.git("commit", "-m", "approval signers", cwd=repo)
        return key_path

    def enable_approval_signing(self) -> Path:
        key_path = self.commit_signers(repo=self.integration)
        # Signers live on the pinned integration ref; the integration
        # checkout itself stays on its own branch (the ff destination).
        self.git("checkout", "integration-main", cwd=self.integration)
        # Signers are read from the pinned ref of the repository named by
        # AGENT_COMMS_APPROVAL_SIGNERS_REPO (here the integration checkout,
        # already named by AGENT_COMMS_MAIN in setUp); the facade keeps
        # REPO_ROOT for the status binding, so only that one rebinds.
        self.name_signers_repo(self.integration)
        patch = mock.patch.object(review, "REPO_ROOT", self.integration)
        patch.start()
        self.addCleanup(patch.stop)
        return key_path

    def name_signers_repo(self, repo: Path) -> None:
        env_patch = mock.patch.dict(os.environ, {"AGENT_COMMS_APPROVAL_SIGNERS_REPO": str(repo)})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def cycle_destination(self, integration: Path | None = None) -> tuple[str, str]:
        # Mirror approval.derive_cycle_destination against a chosen integration
        # checkout so a signed v2 approval binds exactly the repo_identity and
        # target_ref the gate-merge/verify consumers remeasure there.
        integration = integration or self.integration
        toplevel = self.git("rev-parse", "--show-toplevel", cwd=integration)
        repo_identity = f"{approval.LOCAL_WORKTREE_PREFIX}{Path(toplevel).resolve()}"
        target_ref = self.git("symbolic-ref", "--quiet", "HEAD", cwd=integration)
        return repo_identity, target_ref

    def sign_record_payload(
        self, record: dict, key_path: Path, integration: Path | None = None
    ) -> str:
        # Contract 18: the signed cycle payload binds the destination trio.
        repo_identity, target_ref = self.cycle_destination(integration)
        payload = review.approval_payload(
            record["dispatch_id"],
            record["approved_head"],
            record["brief_sha256"],
            repo_identity,
            target_ref,
        )
        return review.sign_approval_payload(payload, key_path)

    def v2_approval(
        self,
        record: dict,
        key_path: Path,
        *,
        integration: Path | None = None,
        approver: str = "human",
    ) -> dict:
        """A complete v2 cycle approval object bound to the destination that the
        chosen integration checkout will present at the consumer boundary."""
        repo_identity, target_ref = self.cycle_destination(integration)
        payload = review.approval_payload(
            record["dispatch_id"],
            record["approved_head"],
            record["brief_sha256"],
            repo_identity,
            target_ref,
        )
        return {
            "approver": approver,
            "mechanism": "dev-tty-presence+ssh-sig",
            "payload_version": review.CYCLE_PAYLOAD_VERSION,
            "repo_identity": repo_identity,
            "target_ref": target_ref,
            "signature": review.sign_approval_payload(payload, key_path),
        }

    def schema1_record(self, dispatch_id: str = "schema1", state: str = "review_clean") -> dict:
        return {
            "schema_version": 1,
            "dispatch_id": dispatch_id,
            "state": state,
            "repo": str(self.repo),
            "brief_path": str(self.brief),
            "dod": [],
            "findings": [],
            "gate_runs": [],
        }

    def schema1_verify_record(self, dispatch_id: str, state: str, key_path: Path) -> dict:
        head = self.git("rev-parse", "HEAD", cwd=self.integration)
        record = self.schema1_record(dispatch_id, state)
        record.update({
            "reviewed_head": head,
            "approved_head": head,
            "brief_sha256": hashlib.sha256(self.brief.read_bytes()).hexdigest(),
            "history": [{"event": "historical", "timestamp": "2026-01-01T00:00:00Z"}],
        })
        record["approval"] = self.v2_approval(record, key_path)
        return record

    def open_review(self, dispatch_id: str = "D1", max_respawns: int = 3, dod: Path | None = None) -> None:
        # Happy-path lifecycle helpers always model the worker executing in
        # this fixture's actual review worktree.
        self._set_worker_root(str(self.repo))
        self.run_review(
            "open",
            "--dispatch-id",
            dispatch_id,
            "--brief",
            str(self.brief),
            "--dod",
            str(dod or self.dod),
            "--repo",
            str(self.repo),
            "--max-respawns",
            str(max_respawns),
            "--expected-producer",
            "gamma-architect",
            "--expected-recipient",
            "gamma-codex-worker",
        )

    def test_open_refuses_review_repo_worker_root_mismatch_without_artifacts(self) -> None:
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            conn.execute(
                "update actors set project_root=? where id=?",
                (str(self.root / "worker"), "gamma-codex-worker"),
            )
        rc, _stdout, stderr = self.run_review_capture(
            "open", "--dispatch-id", "D-root-mismatch", "--brief", str(self.brief),
            "--dod", str(self.dod), "--repo", str(self.repo),
        )
        self.assertEqual(rc, 1)
        self.assertIn("review_repo_worker_root_mismatch", stderr)
        self.assertFalse((self.review_root / "D-root-mismatch.json").exists())
        self.assertFalse((self.review_root / "D-root-mismatch.lock").exists())
        self.assertFalse((self.review_root / "D-root-mismatch.summary.md").exists())

    def _set_worker_root(self, value: str | None, *, delete: bool = False) -> None:
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            if delete:
                conn.execute("delete from actors where id=?", ("gamma-codex-worker",))
            else:
                conn.execute(
                    "update actors set project_root=? where id=?",
                    (value, "gamma-codex-worker"),
                )

    def _assert_open_binding_failure(self, dispatch_id: str, diagnosis: str) -> None:
        rc, _stdout, stderr = self.run_review_capture(
            "open", "--dispatch-id", dispatch_id, "--brief", str(self.brief),
            "--dod", str(self.dod), "--repo", str(self.repo),
        )
        self.assertEqual(rc, 1)
        self.assertIn(diagnosis, stderr)
        for suffix in (".json", ".lock", ".summary.md"):
            self.assertFalse((self.review_root / f"{dispatch_id}{suffix}").exists())

    def test_open_binding_fails_closed_for_missing_actor_and_root(self) -> None:
        self._set_worker_root(None)
        self._assert_open_binding_failure("D-root-null", "review_repo_worker_root_missing")
        self._set_worker_root(str(self.repo), delete=True)
        self._assert_open_binding_failure("D-actor-missing", "review_repo_worker_root_actor_missing")

    def test_open_binding_normalizes_ledger_open_and_query_failures(self) -> None:
        import sqlite3
        with mock.patch.object(review.runtime_paths, "db_path", side_effect=RuntimeError("path unavailable")):
            self._assert_open_binding_failure("D-ledger-path", "review_repo_worker_root_ledger_path_failed")
        with mock.patch.object(review.sqlite3, "connect", side_effect=sqlite3.OperationalError("open denied")):
            self._assert_open_binding_failure("D-ledger-open", "review_repo_worker_root_ledger_open_failed")

        class BrokenQuery:
            row_factory = None
            def execute(self, statement, *_args):
                if statement == "PRAGMA query_only=ON":
                    return self
                raise sqlite3.OperationalError("query denied")
            def close(self):
                pass

        with mock.patch.object(review.sqlite3, "connect", return_value=BrokenQuery()):
            self._assert_open_binding_failure("D-ledger-query", "review_repo_worker_root_ledger_query_failed")

    def test_open_binding_normalizes_ledger_close_failure(self) -> None:
        import sqlite3
        worker_root = str(self.repo)

        class SuccessfulQuery:
            def fetchone(self):
                return {"project_root": worker_root}

        class BrokenClose:
            row_factory = None

            def execute(self, *_args):
                return SuccessfulQuery()

            def close(self):
                raise sqlite3.OperationalError("close denied")

        with mock.patch.object(review.sqlite3, "connect", return_value=BrokenClose()):
            self._assert_open_binding_failure(
                "D-ledger-close", "review_repo_worker_root_ledger_close_failed"
            )

    def test_open_binding_normalizes_configured_root_resolution_failure(self) -> None:
        bad_root = self.root / "bad-root"
        self._set_worker_root(str(bad_root))
        original_resolve = Path.resolve

        def resolve(path: Path, *args, **kwargs):
            if path == bad_root:
                raise OSError("resolution denied")
            return original_resolve(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            self._assert_open_binding_failure("D-root-resolve", "review_repo_worker_root_resolution_failed")

    def test_open_binding_accepts_symlink_equivalent_worker_root(self) -> None:
        link = self.root / "repo-link"
        link.symlink_to(self.repo, target_is_directory=True)
        self._set_worker_root(str(link))
        self.assertNotEqual(str(link), str(self.repo))
        self.run_review(
            "open", "--dispatch-id", "D-root-symlink", "--brief", str(self.brief),
            "--dod", str(self.dod), "--repo", str(self.repo),
            "--expected-recipient", "gamma-codex-worker",
        )
        record = self.record("D-root-symlink")
        self.assertEqual(record["state"], "drafted_brief")
        self.assertEqual(record["repo"], str(self.repo.resolve()))

    def _assert_transition_binding_precedes_brief_drift(self, verb: str) -> None:
        dispatch_id = f"D-drift-{verb}"
        if verb == "respawn":
            self.to_execution_reviewed(dispatch_id)
            finding_id = self.add_finding(dispatch_id)
        else:
            self.open_review(dispatch_id)
            self.run_review(
                "brief-check", "--dispatch-id", dispatch_id, "--clean", "--by", "codex",
                "--surface-verdict", "complete", "--surface-reason", "fixture complete",
            )
        argv = [verb, "--dispatch-id", dispatch_id]
        if verb == "mark-dispatched":
            argv.extend(("--idempotency-key", "drift-mark"))
        elif verb == "redispatch":
            self.run_review(
                "mark-dispatched", "--dispatch-id", dispatch_id,
                "--idempotency-key", "drift-original",
            )
            argv.extend(("--idempotency-key", "drift-redispatch", "--note", "retry"))
        else:
            argv.extend(("--finding", finding_id, "--respawn-dispatch-id", "dispatch_20260731_120000_00000001", "--note", "fix"))
        json_path = self.review_root / f"{dispatch_id}.json"
        summary_path = self.review_root / f"{dispatch_id}.summary.md"
        before_json, before_summary = json_path.read_bytes(), summary_path.read_bytes()
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn:
            before_rows = conn.execute("select count(*) from dispatch_ledger").fetchone()[0]
        self.brief.write_text(self.brief.read_text(encoding="utf-8") + "\ndrift\n", encoding="utf-8")
        self._set_worker_root(str(self.root / "wrong-worker-root"))
        rc, _stdout, stderr = self.run_review_capture(*argv)
        self.assertEqual(rc, 1)
        self.assertIn("review_repo_worker_root_mismatch", stderr)
        self.assertEqual(json_path.read_bytes(), before_json)
        self.assertEqual(summary_path.read_bytes(), before_summary)
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn:
            self.assertEqual(conn.execute("select count(*) from dispatch_ledger").fetchone()[0], before_rows)

    def test_mark_dispatched_binding_failure_wins_over_brief_drift(self) -> None:
        self._assert_transition_binding_precedes_brief_drift("mark-dispatched")

    def test_redispatch_binding_failure_wins_over_brief_drift(self) -> None:
        self._assert_transition_binding_precedes_brief_drift("redispatch")

    def test_respawn_binding_failure_wins_over_brief_drift(self) -> None:
        self._assert_transition_binding_precedes_brief_drift("respawn")

    def test_unset_landing_variables_name_themselves(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_COMMS_MAIN", None)
            os.environ.pop("AGENT_COMMS_APPROVAL_SIGNERS_REPO", None)
            with self.assertRaisesRegex(review.ReviewError, "AGENT_COMMS_MAIN is not set"):
                git_evidence.integration_checkout()
            with self.assertRaisesRegex(review.ReviewError, "AGENT_COMMS_APPROVAL_SIGNERS_REPO is not set"):
                approval.approval_signers_repo()
            with self.assertRaisesRegex(review.ReviewError, "AGENT_COMMS_APPROVAL_SIGNERS_REPO is not set"):
                approval.committed_approval_signers()

    def test_open_without_agent_comms_main_names_the_variable_and_writes_no_record(self) -> None:
        # No fallback to the agent-comms source tree: the integration checkout
        # is operator data, and an installed package has no checkout.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_COMMS_MAIN", None)
            rc, _stdout, stderr = self.run_review_capture(
                "open",
                "--dispatch-id",
                "D-integration",
                "--brief",
                str(self.brief),
                "--dod",
                str(self.dod),
                "--repo",
                str(self.repo),
            )
        self.assertNotEqual(rc, 0)
        self.assertIn("AGENT_COMMS_MAIN is not set", stderr)
        self.assertFalse((self.review_root / "D-integration.json").exists())

    def test_open_integration_guard_honors_agent_comms_main(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(self.integration)}):
            rc, _stdout, stderr = self.run_review_capture(
                "open",
                "--dispatch-id",
                "D-env-main",
                "--brief",
                str(self.brief),
                "--dod",
                str(self.dod),
                "--repo",
                str(self.integration),
            )
        self.assertNotEqual(rc, 0)
        self.assertIn("is the integration checkout that cycle-land merges INTO", stderr)
        self.assertFalse((self.review_root / "D-env-main.json").exists())

    def test_open_distinct_named_branch_git_checkout_succeeds(self) -> None:
        self.open_review("D-distinct")
        state = self.record("D-distinct")
        self.assertEqual(state["state"], "drafted_brief")
        self.assertEqual(state["target_branch"], "work-branch")
        self.assertIsNotNone(state["base_commit"])

    def test_open_missing_production_surface_writes_no_record_or_lock(self) -> None:
        self.brief.write_text(self.brief.read_text(encoding="utf-8").split("\n## Production surface\n", 1)[0], encoding="utf-8")
        self.run_review("open", "--dispatch-id", "D-missing-surface", "--brief", str(self.brief), "--dod", str(self.dod), "--repo", str(self.repo), ok=False)
        self.assertFalse((self.review_root / "D-missing-surface.json").exists())
        self.assertFalse((self.review_root / "D-missing-surface.lock").exists())

    def test_brief_check_surface_rider_required_coupled_and_recorded(self) -> None:
        self.open_review()
        before = self.record()
        self.run_review("brief-check", "--dispatch-id", "D1", "--clean", "--by", "codex", ok=False)
        self.assertEqual(self.record(), before)
        self.run_review("brief-check", "--dispatch-id", "D1", "--clean", "--by", "codex", "--surface-verdict", "complete", "--surface-reason", "", ok=False)
        self.assertEqual(self.record(), before)
        self.run_review("brief-check", "--dispatch-id", "D1", "--clean", "--by", "codex", "--surface-verdict", "incomplete", "--surface-reason", "missing runtime", ok=False)
        self.assertEqual(self.record(), before)
        self.run_review("brief-check", "--dispatch-id", "D1", "--finding", "missing runtime", "--by", "codex", "--surface-verdict", "incomplete", "--surface-reason", "real_runtime omitted")
        check = self.record()["brief_checks"][-1]
        self.assertEqual(check["surface_verdict"], "incomplete")
        self.assertEqual(check["surface_reason"], "real_runtime omitted")

    def test_brief_check_revalidates_production_surface_revision(self) -> None:
        self.open_review()
        text = self.brief.read_text(encoding="utf-8")
        self.brief.write_text(text.replace("- touches: none; reason: test fixture", "- touches: none"), encoding="utf-8")
        self.run_review("brief-check", "--dispatch-id", "D1", "--clean", "--by", "codex", "--surface-verdict", "complete", "--surface-reason", "complete", ok=False)
        self.assertEqual(self.record()["state"], "drafted_brief")

    def test_open_refuses_foreign_repo_with_integration_branch_name(self) -> None:
        with mock.patch.object(review, "REPO_ROOT", self.integration):
            shared_branch = self.git("branch", "--show-current", cwd=self.integration)
            foreign = self.root / "foreign-same-branch"
            self.init_repo(foreign)
            self.git("checkout", "-B", shared_branch, cwd=foreign)
            (foreign / "tracked.txt").write_text("foreign\n", encoding="utf-8")
            self.git("add", "tracked.txt", cwd=foreign)
            self.git("commit", "-m", "foreign", cwd=foreign)

            self.assertNotEqual(foreign.resolve(), self.integration.resolve())
            rc, _stdout, stderr = self.run_review_capture(
                "open",
                "--dispatch-id",
                "D-foreign",
                "--brief",
                str(self.brief),
                "--dod",
                str(self.dod),
                "--repo",
                str(foreign),
            )
        self.assertEqual(rc, 1)
        self.assertIn("review_repo_worker_root_mismatch", stderr)
        self.assertFalse((self.review_root / "D-foreign.json").exists())

    def test_open_requires_repo_argument(self) -> None:
        rc, _stdout, stderr = self.run_review_capture(
            "open",
            "--dispatch-id",
            "D-no-repo",
            "--brief",
            str(self.brief),
            "--dod",
            str(self.dod),
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("--repo", stderr)
        self.assertFalse((self.review_root / "D-no-repo.json").exists())

    def test_open_refuses_non_git_repo_and_writes_no_record(self) -> None:
        non_git = self.root / "non-git"
        non_git.mkdir()
        rc, _stdout, stderr = self.run_review_capture(
            "open",
            "--dispatch-id",
            "D-non-git",
            "--brief",
            str(self.brief),
            "--dod",
            str(self.dod),
            "--repo",
            str(non_git),
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("must be a git checkout on a named branch", stderr)
        self.assertFalse((self.review_root / "D-non-git.json").exists())

    def test_open_refuses_detached_head_and_writes_no_record(self) -> None:
        self.git("checkout", "--detach", "HEAD")
        rc, _stdout, stderr = self.run_review_capture(
            "open",
            "--dispatch-id",
            "D-detached",
            "--brief",
            str(self.brief),
            "--dod",
            str(self.dod),
            "--repo",
            str(self.repo),
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("must be a git checkout on a named branch", stderr)
        self.assertFalse((self.review_root / "D-detached.json").exists())

    def to_executed(self, dispatch_id: str = "D1", max_respawns: int = 3) -> None:
        self.open_review(dispatch_id, max_respawns=max_respawns)
        self.run_review("brief-check", "--dispatch-id", dispatch_id, "--clean", "--by", "codex", "--surface-verdict", "complete", "--surface-reason", "fixture declaration is complete")
        # Contract 17: the first implementation round dispatches at one clean
        # common HEAD, so bring integration up to the review worktree tip before
        # mark-dispatched; the candidate commit follows via worker evidence.
        self.align_integration()
        self.run_review("mark-dispatched", "--dispatch-id", dispatch_id)
        self.run_review("mark-executed", "--dispatch-id", dispatch_id)

    def to_execution_reviewed(self, dispatch_id: str = "D1", max_respawns: int = 3) -> None:
        self.to_executed(dispatch_id, max_respawns=max_respawns)
        self.run_review("gates", "--dispatch-id", dispatch_id, "--check", "green")

    def gate_run(self, verdict: str, check_id: str = "green") -> dict:
        return {
            "check_id": check_id,
            "argv_or_registry_name": check_id,
            "cwd": str(self.repo),
            "git_head": "head",
            "branch": "main",
            "env_policy": "redacted",
            "started_at": "2026-07-12T00:00:00Z",
            "ended_at": "2026-07-12T00:00:01Z",
            "timeout_s": 30,
            "exit_code": {"pass": 0, "fail": 1}.get(verdict),
            "stdout_excerpt": "",
            "stderr_excerpt": "",
            "verdict": verdict,
        }

    def add_finding(self, dispatch_id: str = "D1", severity: str = "blocking") -> str:
        rc, stdout, stderr = self.run_review_capture(
            "finding",
            "--dispatch-id",
            dispatch_id,
            "--severity",
            severity,
            "--loc",
            "x",
            "--problem",
            "bad",
            "--impact",
            "breaks",
            "--fix",
            "fix",
        )
        self.assertEqual(rc, 0, stderr)
        return stdout.strip()

    def test_t12_happy_path_end_to_end_state_sequence(self) -> None:
        key_path = self.enable_approval_signing()
        self.open_review()
        self.assertEqual(self.record()["state"], "drafted_brief")
        self.run_review("brief-check", "--dispatch-id", "D1", "--clean", "--by", "codex", "--surface-verdict", "complete", "--surface-reason", "fixture declaration is complete")
        self.assertEqual(self.record()["state"], "brief_reviewed")
        self.run_review("mark-dispatched", "--dispatch-id", "D1")
        self.assertEqual(self.record()["state"], "dispatched")
        self.run_review("mark-executed", "--dispatch-id", "D1")
        self.assertEqual(self.record()["state"], "executed")
        self.run_review("gates", "--dispatch-id", "D1", "--check", "green")
        self.assertEqual(self.record()["state"], "execution_reviewed")
        self.run_review("clean", "--dispatch-id", "D1")
        self.assertEqual(self.record()["state"], "review_clean")
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(key_path),
            )
        state = self.record()
        self.assertEqual(state["state"], "human_approved")
        self.assertEqual(state["approved_head"], state["reviewed_head"])
        self.assertEqual(state["approval"]["mechanism"], "dev-tty-presence+ssh-sig")
        self.assertIn("BEGIN SSH SIGNATURE", state["approval"]["signature"])
        self.run_review("gate-merge", "--dispatch-id", "D1")
        self.assertEqual(self.record()["state"], "merge_eligible")
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        self.run_review("verify", "--dispatch-id", "D1", "--by", "architect")
        state = self.record()
        self.assertEqual(state["state"], "verified")
        # No new cycle persists "merged" before integration: neither the
        # final state nor any recorded event claims it.
        self.assertNotIn("merged", {event.get("result") for event in state["history"]})
        gate_events = [event for event in state["history"] if event["event"] == "gate-merge"]
        self.assertEqual([event["result"] for event in gate_events], ["merge_eligible"])

    def test_schema1_summary_and_status_are_read_only(self) -> None:
        for index, state in enumerate(("merge_eligible", "review_clean", "merged", "verified"), 1):
            dispatch_id = f"schema1-read-{index}"
            record = self.schema1_record(dispatch_id, state)
            self.write_record(record, dispatch_id)
            path = self.review_root / f"{dispatch_id}.json"
            before = path.read_bytes()
            rc, stdout, stderr = self.run_review_capture("summary", "--dispatch-id", dispatch_id)
            self.assertEqual(rc, 0, stderr)
            self.assertIn("diagnosis: prior_schema_read_only", stdout)
            rc, stdout, stderr = self.run_review_capture("status", "--dispatch-id", dispatch_id)
            self.assertEqual(rc, 0, stderr)
            self.assertEqual(json.loads(stdout)["diagnosis"], "prior_schema_read_only")
            self.assertEqual(path.read_bytes(), before)

    def test_schema1_mutation_refuses_before_effects(self) -> None:
        record = self.schema1_record("schema1-mutate")
        self.write_record(record, "schema1-mutate")
        path = self.review_root / "schema1-mutate.json"
        before = path.read_bytes()
        called = mock.Mock()
        with self.assertRaisesRegex(review.ReviewError, "prior_schema_read_only"):
            review.locked_update("schema1-mutate", lambda value: called())
        called.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_schema1_recover_binding_refuses_before_sweep_or_claim_change(self) -> None:
        record = self.schema1_record("schema1-binding")
        self.write_record(record, "schema1-binding")
        bindings = self.review_root / "bindings"
        staging = self.review_root / ".binding-staging"
        bindings.mkdir(parents=True)
        staging.mkdir(parents=True)
        worker_id = "dispatch_20260731_200000_00000099"
        claim = bindings / worker_id
        claim.write_text(json.dumps({
            "worker_dispatch_id": worker_id, "record_id": "schema1-binding", "state": "pending"
        }), encoding="utf-8")
        staged = staging / "claim-preserve"
        staged.write_text("preserve", encoding="utf-8")
        before_record, before_claim = (
            (self.review_root / "schema1-binding.json").read_bytes(), claim.read_bytes()
        )
        rc, _, stderr = self.run_review_capture(
            "recover-binding", "--worker-dispatch-id", worker_id
        )
        self.assertEqual(rc, 1)
        self.assertIn("prior_schema_read_only", stderr)
        self.assertEqual((self.review_root / "schema1-binding.json").read_bytes(), before_record)
        self.assertEqual(claim.read_bytes(), before_claim)
        self.assertEqual(staged.read_text(encoding="utf-8"), "preserve")

    def test_schema1_verify_recovers_terminal_state_without_schema2_lineage(self) -> None:
        key = self.enable_approval_signing()
        schema2_lineage = {
            "expected_producer", "expected_recipient", "intended_dispatches", "worker_evidence",
            "blocked_dispatches", "superseded_dispatches", "blocked_redispatch_count",
            "max_blocked_redispatches",
        }
        for state in ("merge_eligible", "merged"):
            dispatch_id = f"schema1-verify-{state}"
            record = self.schema1_verify_record(dispatch_id, state, key)
            prefix = copy.deepcopy(record["history"])
            self.write_record(record, dispatch_id)
            self.run_review("verify", "--dispatch-id", dispatch_id, "--by", "architect")
            result = self.record(dispatch_id)
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["state"], "verified")
            self.assertEqual(result["history"][:len(prefix)], prefix)
            self.assertTrue(schema2_lineage.isdisjoint(result))

    def test_schema1_verify_revalidates_signature_before_git_without_mutation(self) -> None:
        key = self.enable_approval_signing()
        record = self.schema1_verify_record("schema1-bad-signature", "merge_eligible", key)
        record["approval"]["signature"] = "invalid-signature"
        self.write_record(record, "schema1-bad-signature")
        json_path = self.review_root / "schema1-bad-signature.json"
        summary_path = self.review_root / "schema1-bad-signature.md"
        summary_path.write_text("historical summary bytes\n", encoding="utf-8")
        before_json, before_summary = json_path.read_bytes(), summary_path.read_bytes()
        with mock.patch.object(approval, "integration_checkout") as git_probe:
            rc, _, stderr = self.run_review_capture(
                "verify", "--dispatch-id", "schema1-bad-signature", "--by", "architect"
            )
        self.assertEqual(rc, 1)
        self.assertIn("approval signature verification failed", stderr)
        git_probe.assert_not_called()
        self.assertEqual(json_path.read_bytes(), before_json)
        self.assertEqual(summary_path.read_bytes(), before_summary)

    def test_brief_mutation_resets_from_each_pre_approval_state(self) -> None:
        for target_state in ["brief_reviewed", "dispatched", "executed", "execution_reviewed"]:
            dispatch_id = f"D_{target_state}"
            self.open_review(dispatch_id)
            self.run_review("brief-check", "--dispatch-id", dispatch_id, "--clean", "--by", "codex", "--surface-verdict", "complete", "--surface-reason", "fixture declaration is complete")
            if target_state in {"dispatched", "executed", "execution_reviewed"}:
                self.align_integration()  # contract 17: dispatch at clean common HEAD
                self.run_review("mark-dispatched", "--dispatch-id", dispatch_id)
            if target_state in {"executed", "execution_reviewed"}:
                self.run_review("mark-executed", "--dispatch-id", dispatch_id)
            if target_state == "execution_reviewed":
                self.run_review("gates", "--dispatch-id", dispatch_id, "--check", "green")
            self.assertEqual(self.record(dispatch_id)["state"], target_state)

            original = self.brief.read_text(encoding="utf-8")
            self.brief.write_text(original.replace("\n", "   \r\n"), encoding="utf-8")
            self.run_review("status", "--dispatch-id", dispatch_id)
            self.assertEqual(self.record(dispatch_id)["state"], target_state)
            self.brief.write_text(f"{original}material edit\n", encoding="utf-8")
            self.run_review("mark-dispatched", "--dispatch-id", dispatch_id, ok=False)
            self.assertEqual(self.record(dispatch_id)["state"], "brief_revised")
            self.brief.write_text(original, encoding="utf-8")

    def test_clean_rejected_with_open_blocking_or_should(self) -> None:
        self.to_execution_reviewed()
        self.run_review(
            "finding",
            "--dispatch-id",
            "D1",
            "--severity",
            "blocking",
            "--loc",
            "x",
            "--problem",
            "bad",
            "--impact",
            "breaks",
            "--fix",
            "fix",
        )
        self.run_review("clean", "--dispatch-id", "D1", ok=False)
        self.run_review("respawn", "--dispatch-id", "D1", "--finding", "F1", "--respawn-dispatch-id", self._make_correction_dispatch("clean-reject-round-2"), "--note", "retry")
        self.run_review("mark-executed", "--dispatch-id", "D1")
        self.run_review("gates", "--dispatch-id", "D1", "--check", "green")
        self.run_review("resolve", "--dispatch-id", "D1", "--finding", "F1", "--resolution-note", "fixed")
        self.run_review(
            "finding",
            "--dispatch-id",
            "D1",
            "--severity",
            "nit",
            "--loc",
            "y",
            "--problem",
            "minor",
            "--impact",
            "small",
            "--fix",
            "later",
        )
        self.run_review("clean", "--dispatch-id", "D1", ok=False)
        self.run_review("defer", "--dispatch-id", "D1", "--finding", "F2", "--note", "non-blocking")
        self.run_review("clean", "--dispatch-id", "D1")
        self.assertEqual(self.record()["state"], "review_clean")

    def test_failed_gate_then_recovery_reaches_clean(self) -> None:
        self.open_review()
        self.run_review("brief-check", "--dispatch-id", "D1", "--clean", "--by", "codex", "--surface-verdict", "complete", "--surface-reason", "fixture declaration is complete")
        self.run_review("mark-dispatched", "--dispatch-id", "D1")
        self.run_review("mark-executed", "--dispatch-id", "D1")

        failed_run = {
            "check_id": "green",
            "argv_or_registry_name": "green",
            "cwd": str(self.repo),
            "git_head": "head",
            "branch": "main",
            "env_policy": "redacted",
            "started_at": "2026-05-31T00:00:00Z",
            "ended_at": "2026-05-31T00:00:01Z",
            "timeout_s": 30,
            "exit_code": 1,
            "stdout_excerpt": "",
            "stderr_excerpt": "",
            "verdict": "fail",
        }
        passed_run = dict(failed_run, exit_code=0, verdict="pass")
        with mock.patch.object(
            checks, "run_check", side_effect=[failed_run, passed_run]
        ):
            self.assertEqual(
                self.run_review(
                    "gates", "--dispatch-id", "D1", "--check", "green", ok=False
                ),
                1,
            )
            self.run_review(
                "finding",
                "--dispatch-id",
                "D1",
                "--severity",
                "blocking",
                "--loc",
                "x",
                "--problem",
                "bad",
                "--impact",
                "breaks",
                "--fix",
                "fix",
            )
            self.run_review("respawn", "--dispatch-id", "D1", "--finding", "F1", "--respawn-dispatch-id", self._make_correction_dispatch("gate-recovery-round-2"), "--note", "retry")
            self.run_review("mark-executed", "--dispatch-id", "D1")
            self.run_review("gates", "--dispatch-id", "D1", "--check", "green")

        self.run_review("resolve", "--dispatch-id", "D1", "--finding", "F1", "--resolution-note", "fixed")
        self.run_review("clean", "--dispatch-id", "D1")
        state = self.record()
        self.assertEqual(state["state"], "review_clean")
        self.assertEqual([run["verdict"] for run in state["gate_runs"]], ["fail", "pass"])

    def test_nonpassing_gate_can_rerun_from_execution_reviewed(self) -> None:
        for verdict in ("fail", "timeout"):
            with self.subTest(verdict=verdict):
                dispatch_id = f"D_rerun_{verdict}"
                self.to_executed(dispatch_id)
                with mock.patch.object(
                    checks,
                    "run_check",
                    side_effect=[self.gate_run(verdict), self.gate_run("pass")],
                ):
                    self.assertEqual(
                        self.run_review(
                            "gates",
                            "--dispatch-id",
                            dispatch_id,
                            "--check",
                            "green",
                            ok=False,
                        ),
                        1,
                    )
                    self.assertEqual(
                        self.record(dispatch_id)["state"], "execution_reviewed"
                    )
                    self.run_review(
                        "gates", "--dispatch-id", dispatch_id, "--check", "green"
                    )
                state = self.record(dispatch_id)
                self.assertEqual(state["state"], "execution_reviewed")
                self.assertEqual([run["verdict"] for run in state["gate_runs"]], [verdict, "pass"])
                if verdict == "timeout":
                    self.assertIsNone(state["gate_runs"][0]["exit_code"])
                self.run_review("clean", "--dispatch-id", dispatch_id)
                self.assertEqual(self.record(dispatch_id)["state"], "review_clean")

    def test_gate_rerun_latest_failure_blocks_clean(self) -> None:
        self.to_executed("D_mask")
        with mock.patch.object(
            checks,
            "run_check",
            side_effect=[self.gate_run("pass"), self.gate_run("fail")],
        ):
            self.run_review("gates", "--dispatch-id", "D_mask", "--check", "green")
            self.assertEqual(
                self.run_review(
                    "gates", "--dispatch-id", "D_mask", "--check", "green", ok=False
                ),
                1,
            )
        state = self.record("D_mask")
        self.assertEqual([run["verdict"] for run in state["gate_runs"]], ["pass", "fail"])
        self.run_review("clean", "--dispatch-id", "D_mask", ok=False)
        self.assertEqual(self.record("D_mask")["state"], "execution_reviewed")

    def test_unittest_check_uses_long_default_timeout_with_override(self) -> None:
        repo = self.root / "nongit"
        repo.mkdir()
        record = {"repo": str(repo), "dod": []}
        completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(review.subprocess, "run", return_value=completed) as run:
            default_run = review.run_check("unittest", record)
            explicit_run = review.run_check("unittest", record, 7)
        self.assertEqual(default_run["timeout_s"], 600)
        self.assertEqual(explicit_run["timeout_s"], 7)
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 600)
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], 7)

    def test_extra_check_uses_900_second_default_with_override(self) -> None:
        repo = self.root / "nongit-extra"
        repo.mkdir()
        record = {"repo": str(repo), "dod": [{"check_id": "extra", "argv": ["true"]}]}
        completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(review.subprocess, "run", return_value=completed) as run:
            default_run = review.run_check("extra", record)
            explicit_run = review.run_check("extra", record, 7)
        self.assertEqual(default_run["timeout_s"], 900)
        self.assertEqual(explicit_run["timeout_s"], 7)
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 900)
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], 7)

    def test_respawn_binds_multiple_findings_as_one_round(self) -> None:
        self.to_execution_reviewed()
        self.assertEqual(self.add_finding("D1"), "F1")
        self.assertEqual(self.add_finding("D1"), "F2")
        correction_id = self._make_correction_dispatch("respawn-multi-round-2")
        self.run_review(
            "respawn",
            "--dispatch-id",
            "D1",
            "--finding",
            "F1",
            "--finding",
            "F2",
            "--respawn-dispatch-id",
            correction_id,
            "--note",
            "fix both",
        )
        state = self.record()
        self.assertEqual(state["state"], "dispatched")
        self.assertEqual(state["respawn_count"], 1)
        by_id = {finding["id"]: finding for finding in state["findings"]}
        self.assertEqual(by_id["F1"]["resolved_by_dispatch_id"], correction_id)
        self.assertEqual(by_id["F2"]["resolved_by_dispatch_id"], correction_id)
        self.assertEqual(state["intended_dispatches"][-1]["respawn_dispatch_id"], correction_id)
        self.assertEqual(state["intended_dispatches"][-1]["idempotency_key"], "respawn-multi-round-2")
        respawn_events = [event for event in state["history"] if event["event"] == "respawn"]
        self.assertEqual(len(respawn_events), 1)
        self.assertEqual(respawn_events[0]["finding_ids"], ["F1", "F2"])

    def test_respawn_mixed_valid_invalid_findings_rejects_atomically(self) -> None:
        for label, finding_ids in [("missing", ["F1", "F404"]), ("repeated", ["F1", "F1"]), ("non_open", ["F1", "F2"])]:
            with self.subTest(invalid=label):
                dispatch_id = f"D_atomic_{label}"
                self.to_execution_reviewed(dispatch_id)
                self.add_finding(dispatch_id, severity="blocking")
                self.add_finding(dispatch_id, severity="nit")
                if label == "non_open":
                    self.run_review("defer", "--dispatch-id", dispatch_id, "--finding", "F2", "--note", "later")
                before = self.record(dispatch_id)
                argv = ["respawn", "--dispatch-id", dispatch_id]
                for finding_id in finding_ids:
                    argv.extend(["--finding", finding_id])
                argv.extend(["--respawn-dispatch-id", "D2", "--note", "mixed"])
                self.run_review(*argv, ok=False)
                state = self.record(dispatch_id)
                self.assertEqual(state, before)
                self.assertEqual(state["state"], "execution_reviewed")
                self.assertEqual(state["respawn_count"], 0)
                by_id = {finding["id"]: finding for finding in state["findings"]}
                self.assertIsNone(by_id["F1"]["resolved_by_dispatch_id"])
                self.assertEqual([event for event in state["history"] if event["event"] == "respawn"], [])

    def test_respawn_rejects_comma_separated_finding_value(self) -> None:
        self.to_execution_reviewed()
        self.assertEqual(self.add_finding("D1"), "F1")
        self.assertEqual(self.add_finding("D1"), "F2")
        before = self.record()
        rc, _stdout, stderr = self.run_review_capture(
            "respawn", "--dispatch-id", "D1", "--finding", "F1,F2", "--respawn-dispatch-id", "D2", "--note", "comma"
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("finding not found: F1,F2", stderr)
        self.assertEqual(self.record(), before)

    def test_single_finding_respawn_cli_remains_compatible(self) -> None:
        self.to_execution_reviewed()
        self.add_finding("D1")
        correction_id = self._make_correction_dispatch("respawn-single-round-2")
        self.run_review("respawn", "--dispatch-id", "D1", "--finding", "F1", "--respawn-dispatch-id", correction_id, "--note", "retry")
        state = self.record()
        self.assertEqual(state["state"], "dispatched")
        self.assertEqual(state["respawn_count"], 1)
        self.assertEqual(state["findings"][0]["resolved_by_dispatch_id"], correction_id)

        self.to_execution_reviewed("D_nonopen")
        self.add_finding("D_nonopen", severity="nit")
        self.run_review("defer", "--dispatch-id", "D_nonopen", "--finding", "F1", "--note", "later")
        before = self.record("D_nonopen")
        self.run_review(
            "respawn", "--dispatch-id", "D_nonopen", "--finding", "F1", "--respawn-dispatch-id", "D2", "--note", "retry", ok=False
        )
        self.assertEqual(self.record("D_nonopen"), before)

    def test_multi_finding_respawn_cap_counts_rounds(self) -> None:
        self.to_execution_reviewed()
        self.add_finding("D1")
        self.add_finding("D1")
        round2_id = self._make_correction_dispatch("respawn-cap-round-2")
        self.run_review(
            "respawn",
            "--dispatch-id",
            "D1",
            "--finding",
            "F1",
            "--finding",
            "F2",
            "--respawn-dispatch-id",
            round2_id,
            "--note",
            "one",
        )
        state = self.record()
        self.assertEqual(state["respawn_count"], 1)
        by_id = {finding["id"]: finding for finding in state["findings"]}
        self.assertEqual(by_id["F1"]["resolved_by_dispatch_id"], round2_id)
        self.assertEqual(by_id["F2"]["resolved_by_dispatch_id"], round2_id)
        self.run_review("mark-executed", "--dispatch-id", "D1")
        self.run_review("gates", "--dispatch-id", "D1", "--check", "green")
        round3_id = self._make_correction_dispatch("respawn-cap-round-3")
        self.run_review("respawn", "--dispatch-id", "D1", "--finding", "F1", "--respawn-dispatch-id", round3_id, "--note", "two")
        self.assertEqual(self.record()["respawn_count"], 2)
        self.run_review("mark-executed", "--dispatch-id", "D1")
        self.run_review("gates", "--dispatch-id", "D1", "--check", "green")
        round4_id = self._make_correction_dispatch("respawn-cap-round-4")
        self.run_review("respawn", "--dispatch-id", "D1", "--finding", "F1", "--respawn-dispatch-id", round4_id, "--note", "three")
        self.assertEqual(self.record()["respawn_count"], 3)
        self.run_review("mark-executed", "--dispatch-id", "D1")
        self.run_review("gates", "--dispatch-id", "D1", "--check", "green")
        self.run_review("respawn", "--dispatch-id", "D1", "--finding", "F1", "--respawn-dispatch-id", "D5", "--note", "four")
        state = self.record()
        self.assertEqual(state["state"], "escalated")
        self.assertEqual(state["history"][-1]["event"], "respawn-cap")
        self.assertEqual(state["respawn_count"], 3)

    def test_respawn_at_cap_escalates_before_invalid_finding_validation(self) -> None:
        self.to_execution_reviewed("D_cap", max_respawns=0)
        self.run_review("respawn", "--dispatch-id", "D_cap", "--finding", "F404", "--respawn-dispatch-id", "D2", "--note", "invalid")
        state = self.record("D_cap")
        self.assertEqual(state["state"], "escalated")
        self.assertEqual(state["history"][-1]["event"], "respawn-cap")
        self.assertEqual(state["respawn_count"], 0)

    def test_respawn_cap_escalates(self) -> None:
        self.to_execution_reviewed()
        self.run_review(
            "finding",
            "--dispatch-id",
            "D1",
            "--severity",
            "should",
            "--loc",
            "x",
            "--problem",
            "bad",
            "--impact",
            "risk",
            "--fix",
            "fix",
        )
        self.run_review("respawn", "--dispatch-id", "D1", "--finding", "F1", "--respawn-dispatch-id", self._make_correction_dispatch("respawn-esc-round-2"), "--note", "one")
        self.run_review("mark-executed", "--dispatch-id", "D1")
        self.run_review("gates", "--dispatch-id", "D1", "--check", "green")
        self.run_review("respawn", "--dispatch-id", "D1", "--finding", "F1", "--respawn-dispatch-id", self._make_correction_dispatch("respawn-esc-round-3"), "--note", "two")
        self.run_review("mark-executed", "--dispatch-id", "D1")
        self.run_review("gates", "--dispatch-id", "D1", "--check", "green")
        self.run_review("respawn", "--dispatch-id", "D1", "--finding", "F1", "--respawn-dispatch-id", self._make_correction_dispatch("respawn-esc-round-4"), "--note", "three")
        self.run_review("mark-executed", "--dispatch-id", "D1")
        self.run_review("gates", "--dispatch-id", "D1", "--check", "green")
        self.run_review("respawn", "--dispatch-id", "D1", "--finding", "F1", "--respawn-dispatch-id", "D5", "--note", "four")
        self.assertEqual(self.record()["state"], "escalated")
        self.run_review("mark-executed", "--dispatch-id", "D1", ok=False)
        self.run_review("unblock", "--dispatch-id", "D1", "--by", "human", "--action", "raise-cap", "--note", "allow one")
        self.assertEqual(self.record()["state"], "execution_reviewed")

    def _make_correction_dispatch(self, key: str) -> str:
        dispatch_id = Store(self.ledger_path).dispatch_agent(
            "gamma-architect", "gamma-codex-worker", key,
            "correction dispatch", "correction dispatch", [],
        )["dispatch_id"]
        # dispatch_agent re-initializes the store, which restores the worker
        # actor's registered project_root; rebind it to the review worktree.
        self._set_worker_root(str(self.repo))
        return dispatch_id

    def _reviewed_round_one(self, dispatch_id: str, key: str) -> tuple[str, str]:
        """Round 1 executed with real changed-head evidence, then reviewed
        with one open blocking finding; returns (round1_head, finding_id)."""
        self._prepare_result_review(dispatch_id, key)
        (self.repo / f"{dispatch_id}-round1.txt").write_text("round 1 work\n", encoding="utf-8")
        delta = self._snapshot_repo_delta()
        self._install_closeout(key, delta=delta)
        self._commit_all(f"round 1 work for {dispatch_id}")
        rc, _, stderr = self._run_review_raw("mark-executed", "--dispatch-id", dispatch_id)
        self.assertEqual((rc, stderr), (0, ""))
        self.run_review("gates", "--dispatch-id", dispatch_id, "--check", "green")
        finding_id = self.add_finding(dispatch_id)
        return self.record(dispatch_id)["reviewed_head"], finding_id

    def test_respawn_correction_binds_new_dispatch_and_advances_reviewed_head(self) -> None:
        dispatch_id = "RSPE"
        round1_head, finding_id = self._reviewed_round_one(dispatch_id, "rspe-round-1")
        record = self.record(dispatch_id)
        round1_evidence = copy.deepcopy(record["worker_evidence"])
        round1_worker_id = round1_evidence[0]["worker_dispatch_id"]

        correction_id = self._make_correction_dispatch("rspe-round-2")
        (self.repo / "rspe-correction.txt").write_text("round 2 correction\n", encoding="utf-8")
        correction_delta = self._snapshot_repo_delta()
        self.assertEqual(correction_delta["base_commit"], round1_head)
        self._install_closeout("rspe-round-2", delta=correction_delta)
        self._commit_all("round 2 correction")
        round2_head = self.git("rev-parse", "HEAD", cwd=self.repo)
        self.assertNotEqual(round2_head, round1_head)

        self.run_review(
            "respawn", "--dispatch-id", dispatch_id, "--finding", finding_id,
            "--respawn-dispatch-id", correction_id, "--note", "fix finding",
        )
        record = self.record(dispatch_id)
        self.assertEqual(record["state"], "dispatched")
        self.assertEqual(record["findings"][0]["resolved_by_dispatch_id"], correction_id)

        rc, _, stderr = self._run_review_raw("mark-executed", "--dispatch-id", dispatch_id)
        self.assertEqual((rc, stderr), (0, ""))
        record = self.record(dispatch_id)
        self.assertEqual(record["state"], "executed")
        self.assertEqual(record["reviewed_head"], round2_head)
        self.assertEqual(len(record["intended_dispatches"]), 2)
        correction_intent = record["intended_dispatches"][1]
        self.assertEqual(correction_intent["attempt"], 2)
        self.assertEqual(correction_intent["respawn_dispatch_id"], correction_id)
        self.assertEqual(len(record["worker_evidence"]), 2)
        self.assertEqual(record["worker_evidence"][0], round1_evidence[0])
        correction_evidence = record["worker_evidence"][1]
        self.assertEqual(correction_evidence["worker_dispatch_id"], correction_id)
        self.assertNotEqual(correction_evidence["worker_dispatch_id"], round1_worker_id)
        self.assertEqual(correction_evidence["intent_attempt"], 2)
        verification = correction_evidence["delta_verification"]
        self.assertEqual(
            verification["snapshot_tree"],
            self.git("rev-parse", f"{round2_head}^{{tree}}", cwd=self.repo),
        )
        incremental_manifest = subprocess.run(
            ["git", "diff-tree", "-r", "--no-renames", "--raw", "--abbrev=40", "-z",
             f"{round1_head}^{{tree}}", f"{round2_head}^{{tree}}"],
            cwd=self.repo, check=True, stdout=subprocess.PIPE,
        ).stdout
        self.assertEqual(
            verification["manifest_sha256"],
            hashlib.sha256(incremental_manifest).hexdigest(),
        )
        claim = json.loads((self.review_root / "bindings" / correction_id).read_text(encoding="utf-8"))
        self.assertEqual(claim["state"], "bound")
        self.assertEqual(claim["record_id"], dispatch_id)

    def test_correction_round_advances_gate_epoch(self) -> None:
        dispatch_id = "matrix-correction"
        _round1_head, finding_id = self._reviewed_round_one(dispatch_id, "matrix-round-1")
        record = self.record(dispatch_id)
        record["gate_runs"].extend(
            self.gate_run("pass", name) | {"epoch": 0} for name in ("check-a", "check-b")
        )
        self.write_record(record, dispatch_id)
        correction_id = self._make_correction_dispatch("matrix-round-2")
        (self.repo / "matrix-correction.txt").write_text("correction\n", encoding="utf-8")
        correction_delta = self._snapshot_repo_delta()
        self._install_closeout("matrix-round-2", delta=correction_delta)
        self._commit_all("matrix correction")
        self.run_review(
            "respawn", "--dispatch-id", dispatch_id, "--finding", finding_id,
            "--respawn-dispatch-id", correction_id, "--note", "fix",
        )
        rc, _, stderr = self._run_review_raw("mark-executed", "--dispatch-id", dispatch_id)
        self.assertEqual((rc, stderr), (0, ""))
        record = self.record(dispatch_id)
        self.assertEqual(record["gate_epoch"], 1)
        event = [item for item in record["history"] if item["event"] == "correction-gate-epoch"][-1]
        self.assertEqual((event["old_gate_epoch"], event["new_gate_epoch"]), (0, 1))
        self.run_review("gates", "--dispatch-id", dispatch_id, "--check", "green")
        with mock.patch.object(review, "REPO_ROOT", self.integration):
            self.run_review("clean", "--dispatch-id", dispatch_id, ok=False)

    def test_unknown_check_prevalidation_leaves_no_partial_state(self) -> None:
        self.to_executed("matrix-atomic")
        before = self.record_bytes("matrix-atomic")
        with mock.patch.object(checks, "run_check") as runner:
            self.run_review(
                "gates", "--dispatch-id", "matrix-atomic", "--check", "green", "unknown",
                ok=False,
            )
        runner.assert_not_called()
        self.assertEqual(self.record_bytes("matrix-atomic"), before)

    def test_clean_green_skip_and_red_precedence(self) -> None:
        names = ["check-a", "check-b"]
        for case in ("green", "skip", "red-skip"):
            with self.subTest(case=case):
                dispatch_id = f"matrix-{case}"
                self.to_execution_reviewed(dispatch_id)
                record = self.record(dispatch_id)
                if case == "green":
                    record["gate_runs"].extend(self.gate_run("pass", name) | {"epoch": 0} for name in names)
                else:
                    record["skips"].extend({
                        "check_id": name, "epoch": 0, "reason": "fixture", "risk": "test-only",
                        "actor": "fixture", "timestamp": review.utc_now(),
                    } for name in names)
                    if case == "red-skip":
                        record["gate_runs"].append(self.gate_run("fail", names[0]) | {"epoch": 0})
                self.write_record(record, dispatch_id)
                self.run_review("clean", "--dispatch-id", dispatch_id, ok=case != "red-skip")

    def test_respawn_refuses_absent_mismatched_malformed_or_reused_dispatch(self) -> None:
        dispatch_id = "RSPN"
        _, finding_id = self._reviewed_round_one(dispatch_id, "rspn-round-1")
        round1_worker_id = self._worker_id_for_key("rspn-round-1")
        actor_mismatch_id = self._make_correction_dispatch("rspn-actor-mismatch")
        malformed_key_id = self._make_correction_dispatch("rspn-malformed-key")
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            conn.execute(
                "update dispatch_ledger set recipient_actor_id='someone-else' where dispatch_id=?",
                (actor_mismatch_id,),
            )
            conn.execute(
                "update dispatch_ledger set idempotency_key='' where dispatch_id=?",
                (malformed_key_id,),
            )
        record_path = self.review_root / f"{dispatch_id}.json"
        before = record_path.read_bytes()
        for respawn_id, needle in (
            ("not-a-dispatch-id", "invalid_worker_dispatch_id"),
            ("dispatch_20260808_000000_00feed00", "respawn_dispatch_not_found"),
            (actor_mismatch_id, "respawn_dispatch_actor_mismatch"),
            (malformed_key_id, "respawn_dispatch_malformed"),
            (round1_worker_id, "respawn_intent_reused"),
        ):
            with self.subTest(needle=needle):
                rc, _, stderr = self.run_review_capture(
                    "respawn", "--dispatch-id", dispatch_id, "--finding", finding_id,
                    "--respawn-dispatch-id", respawn_id, "--note", "invalid",
                )
                self.assertEqual(rc, 1)
                self.assertIn(needle, stderr)
                self.assertEqual(record_path.read_bytes(), before)

    def test_mark_executed_refuses_wrong_correction_base_snapshot_or_manifest(self) -> None:
        for variant, needle in (
            ("base", "delta_base_mismatch"),
            ("snapshot", "delta_mismatch: reviewed_head tree"),
            ("manifest", "delta_mismatch: recomputed manifest differs"),
        ):
            with self.subTest(variant=variant):
                dispatch_id, key = f"RSPD-{variant}", f"rspd-{variant}-round-2"
                _, finding_id = self._reviewed_round_one(dispatch_id, f"rspd-{variant}-round-1")
                correction_id = self._make_correction_dispatch(key)
                (self.repo / f"{dispatch_id}-fix.txt").write_text("correction\n", encoding="utf-8")
                delta = self._snapshot_repo_delta()
                if variant == "base":
                    delta["base_commit"] = self.record(dispatch_id)["base_commit"]
                elif variant == "manifest":
                    delta["manifest_sha256"] = hashlib.sha256(b"tampered").hexdigest()
                self._install_closeout(key, delta=delta)
                if variant == "snapshot":
                    (self.repo / f"{dispatch_id}-extra.txt").write_text("not in snapshot\n", encoding="utf-8")
                self._commit_all(f"correction for {dispatch_id}")
                self.run_review(
                    "respawn", "--dispatch-id", dispatch_id, "--finding", finding_id,
                    "--respawn-dispatch-id", correction_id, "--note", "fix",
                )
                record_path = self.review_root / f"{dispatch_id}.json"
                before = record_path.read_bytes()
                rc, _, stderr = self._run_review_raw("mark-executed", "--dispatch-id", dispatch_id)
                self.assertEqual(rc, 1)
                self.assertIn(needle, stderr)
                self.assertEqual(record_path.read_bytes(), before)
                self.assertFalse((self.review_root / "bindings" / correction_id).exists())

    def test_intent_respawn_dispatch_id_is_optional_and_validated(self) -> None:
        self._prepare_result_review("RSPS", "rsps-round-1")
        record = self.record("RSPS")
        review.validate_record(record)
        record["intended_dispatches"][-1]["respawn_dispatch_id"] = "dispatch_20260722_120000_0000abcd"
        review.validate_record(record)
        record["intended_dispatches"][-1]["respawn_dispatch_id"] = "D2"
        with self.assertRaises(review.ReviewError) as ctx:
            review.validate_record(record)
        self.assertIn("respawn_dispatch_id", str(ctx.exception))

    def test_gate_merge_blocks_unapproved(self) -> None:
        for state in ["drafted_brief", "brief_reviewed", "dispatched", "executed", "execution_reviewed", "review_clean"]:
            dispatch_id = f"G_{state}"
            self.open_review(dispatch_id)
            if state in {"brief_reviewed", "dispatched", "executed", "execution_reviewed", "review_clean"}:
                self.run_review("brief-check", "--dispatch-id", dispatch_id, "--clean", "--by", "codex", "--surface-verdict", "complete", "--surface-reason", "fixture declaration is complete")
            if state in {"dispatched", "executed", "execution_reviewed", "review_clean"}:
                self.align_integration()  # contract 17: dispatch at clean common HEAD
                self.run_review("mark-dispatched", "--dispatch-id", dispatch_id)
            if state in {"executed", "execution_reviewed", "review_clean"}:
                self.run_review("mark-executed", "--dispatch-id", dispatch_id)
            if state in {"execution_reviewed", "review_clean"}:
                self.run_review("gates", "--dispatch-id", dispatch_id, "--check", "green")
            if state == "review_clean":
                self.run_review("clean", "--dispatch-id", dispatch_id)
            self.run_review("gate-merge", "--dispatch-id", dispatch_id, ok=False)

    def test_signed_approve_then_merge(self) -> None:
        key_path = self.enable_approval_signing()
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(key_path),
            )
        state = self.record()
        self.assertEqual(state["approval"]["mechanism"], "dev-tty-presence+ssh-sig")
        self.assertIn("BEGIN SSH SIGNATURE", state["approval"]["signature"])
        # Contract 18 proof family 2: approve records exactly the signed v2
        # destination trio measured from the configured integration checkout.
        expected_identity, expected_ref = self.cycle_destination()
        self.assertEqual(
            state["approval"]["payload_version"], review.CYCLE_PAYLOAD_VERSION
        )
        self.assertEqual(state["approval"]["repo_identity"], expected_identity)
        self.assertEqual(state["approval"]["target_ref"], expected_ref)
        self.assertEqual(expected_ref, "refs/heads/integration-main")
        self.run_review("gate-merge", "--dispatch-id", "D1")
        self.assertEqual(self.record()["state"], "merge_eligible")

    def test_forged_state_without_signature_refused(self) -> None:
        self.enable_approval_signing()
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        state = self.record()
        head = self.git("rev-parse", "HEAD")
        state["state"] = "human_approved"
        state["reviewed_head"] = head
        state["approved_head"] = head
        state["approval"] = {"approver": "forged", "mechanism": "dev-tty-presence"}
        self.write_record(state)
        self.run_review("gate-merge", "--dispatch-id", "D1", ok=False)
        self.assertEqual(self.record()["state"], "human_approved")

    def test_wrong_key_refused(self) -> None:
        self.enable_approval_signing()
        attacker_key = self.generate_approval_key("attacker-key")
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        state = self.record()
        head = self.git("rev-parse", "HEAD")
        state["state"] = "human_approved"
        state["reviewed_head"] = head
        state["approved_head"] = head
        state["approval"] = self.v2_approval(state, attacker_key, approver="attacker")
        self.write_record(state)
        self.run_review("gate-merge", "--dispatch-id", "D1", ok=False)

    def test_tampered_binding_refused(self) -> None:
        self.approve_ready("D1")
        state = self.record()
        state["brief_sha256"] = "0" * 64
        self.write_record(state)
        self.run_review("gate-merge", "--dispatch-id", "D1", ok=False)

        self.approve_ready("D2")
        (self.repo / "tracked.txt").write_text("after approval\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "after approval")
        self.run_review("gate-merge", "--dispatch-id", "D2", ok=False)

    def test_missing_or_empty_signers_fail_closed(self) -> None:
        key_path = self.generate_approval_key()
        self.name_signers_repo(self.integration)
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        state = self.record()
        head = self.git("rev-parse", "HEAD")
        state["state"] = "human_approved"
        state["reviewed_head"] = head
        state["approved_head"] = head
        state["approval"] = self.v2_approval(state, key_path)
        self.write_record(state)
        self.run_review("gate-merge", "--dispatch-id", "D1", ok=False)

        self.git("checkout", "-B", review.APPROVAL_INTEGRATION_REF, cwd=self.integration)
        signers = self.integration / "config" / "approval-signers"
        signers.parent.mkdir(parents=True, exist_ok=True)
        signers.write_text(" \n\t\n", encoding="utf-8")
        self.git("add", "config/approval-signers", cwd=self.integration)
        self.git("commit", "-m", "empty approval signers", cwd=self.integration)
        state = self.record()
        head = self.git("rev-parse", "HEAD")
        state["reviewed_head"] = head
        state["approved_head"] = head
        state["approval"] = self.v2_approval(state, key_path)
        self.write_record(state)
        self.run_review("gate-merge", "--dispatch-id", "D1", ok=False)

    def test_signing_failure_no_partial_state(self) -> None:
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--dispatch-id",
                "D1",
                "--key",
                str(self.root / "missing-key"),
                ok=False,
            )
        state = self.record()
        self.assertEqual(state["state"], "review_clean")
        self.assertIsNone(state["approval"])

    def test_payload_format_pinned(self) -> None:
        # Contract 18 proof family 1: the exact v2 payload bytes, in canonical
        # UTF-8 line order, now bind the destination trio.
        payload = review.approval_payload(
            "dispatch_1",
            "a" * 40,
            "b" * 64,
            "local-worktree-v1:/srv/integration",
            "refs/heads/main",
        )
        self.assertEqual(
            payload,
            (
                "agent-comms-approval-v2\n"
                "dispatch_id=dispatch_1\n"
                f"approved_head={'a' * 40}\n"
                f"brief_sha256={'b' * 64}\n"
                "repo_identity=local-worktree-v1:/srv/integration\n"
                "target_ref=refs/heads/main\n"
            ).encode("utf-8"),
        )
        # Empty and CR/LF-bearing destination fields (including a newline-bearing
        # legal Unix path) refuse before serialization; they can never become an
        # extra signed-payload line.
        for repo_identity, target_ref in (
            ("", "refs/heads/main"),
            ("local-worktree-v1:/srv/integration", ""),
            ("local-worktree-v1:/srv/in\ntegration", "refs/heads/main"),
            ("local-worktree-v1:/srv/integration", "refs/heads/ma\rin"),
        ):
            with self.assertRaises(review.ReviewError):
                review.approval_payload(
                    "dispatch_1", "a" * 40, "b" * 64, repo_identity, target_ref
                )

    def test_repo_retarget_refused(self) -> None:
        operator_key = self.enable_approval_signing()
        attacker_key = self.generate_approval_key("attacker-key")
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        state = self.record()

        foreign = self.root / "foreign"
        foreign.mkdir()
        subprocess.run(["git", "init"], cwd=foreign, check=True, stdout=subprocess.PIPE)
        self.git("config", "user.email", "test@example.invalid", cwd=foreign)
        self.git("config", "user.name", "Test User", cwd=foreign)
        (foreign / "tracked.txt").write_text("foreign\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=foreign)
        self.git("commit", "-m", "foreign", cwd=foreign)
        foreign_head = self.git("rev-parse", "HEAD", cwd=foreign)
        foreign_state = dict(
            state,
            state="human_approved",
            repo=str(foreign),
            reviewed_head=foreign_head,
            approved_head=foreign_head,
        )
        foreign_state["approval"] = self.v2_approval(
            foreign_state, attacker_key, approver="attacker"
        )
        self.write_record(foreign_state)
        rc, _stdout, stderr = self.run_review_capture("gate-merge", "--dispatch-id", "D1")
        self.assertNotEqual(rc, 0)
        self.assertIn("approval signature verification failed", stderr)
        self.assertEqual(self.record()["state"], "human_approved")

        sibling = self.root / "sibling"
        self.git("worktree", "add", "-b", "attacker-branch", str(sibling))
        self.git("config", "user.email", "test@example.invalid", cwd=sibling)
        self.git("config", "user.name", "Test User", cwd=sibling)
        (sibling / "config").mkdir(exist_ok=True)
        (sibling / "config" / "approval-signers").write_text(
            f"agent-comms-approver {(attacker_key.with_suffix('.pub')).read_text(encoding='utf-8')}", encoding="utf-8"
        )
        self.git("add", "config/approval-signers", cwd=sibling)
        self.git("commit", "-m", "attacker signers", cwd=sibling)
        sibling_head = self.git("rev-parse", "HEAD", cwd=sibling)
        sibling_state = dict(
            state,
            state="human_approved",
            repo=str(sibling),
            reviewed_head=sibling_head,
            approved_head=sibling_head,
        )
        sibling_state["approval"] = self.v2_approval(
            sibling_state, attacker_key, approver="attacker"
        )
        self.write_record(sibling_state)
        rc, _stdout, stderr = self.run_review_capture("gate-merge", "--dispatch-id", "D1")
        self.assertNotEqual(rc, 0)
        self.assertIn("approval signature verification failed", stderr)
        self.assertEqual(self.record()["state"], "human_approved")
        self.assertTrue(operator_key.exists())

    def make_team_checkouts(self) -> tuple[Path, Path, str]:
        """A team integration checkout with its own review worktree, distinct
        from the shared agent-comms module checkout that holds the signers."""
        team_main = self.root / "team-main"
        self.init_repo(team_main)
        self.git("checkout", "-B", "team-integration", cwd=team_main)
        (team_main / "team.txt").write_text("team base\n", encoding="utf-8")
        self.git("add", "team.txt", cwd=team_main)
        self.git("commit", "-m", "team base", cwd=team_main)
        team_repo = self.root / "team-repo"
        self.git("worktree", "add", "-b", "work-branch", str(team_repo), cwd=team_main)
        (team_repo / "team.txt").write_text("team work\n", encoding="utf-8")
        self.git("add", "team.txt", cwd=team_repo)
        self.git("commit", "-m", "team work", cwd=team_repo)
        team_head = self.git("rev-parse", "HEAD", cwd=team_repo)
        return team_main, team_repo, team_head

    def test_cross_repo_gate_merge_accepts_valid_operator_signature(self) -> None:
        operator_key = self.enable_approval_signing()
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        state = self.record()

        team_main, team_repo, team_head = self.make_team_checkouts()
        cross_repo_state = dict(
            state,
            state="human_approved",
            repo=str(team_repo),
            reviewed_head=team_head,
            approved_head=team_head,
        )
        cross_repo_state["approval"] = self.v2_approval(
            cross_repo_state, operator_key, integration=team_main
        )
        self.write_record(cross_repo_state)

        with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(team_main)}):
            self.run_review("gate-merge", "--dispatch-id", "D1")
        self.assertEqual(self.record()["state"], "merge_eligible")

    def test_malformed_approval_arms_fail_closed(self) -> None:
        self.enable_approval_signing()
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        base = self.record()
        head = self.git("rev-parse", "HEAD")
        for index, approval_payload in enumerate(
            [None, "bad", {"signature": ""}, {"signature": " \n\t"}, {"signature": 7}],
            start=1,
        ):
            state = dict(
                base,
                state="human_approved",
                reviewed_head=head,
                approved_head=head,
                approval=approval_payload,
            )
            self.write_record(state)
            self.run_review("gate-merge", "--dispatch-id", "D1", ok=False)
            self.assertEqual(self.record()["state"], "human_approved", index)

    def test_ambient_head_not_anchor_refused(self) -> None:
        operator_key = self.generate_approval_key("operator-key")
        attacker_key = self.generate_approval_key("attacker-key")
        self.commit_signers(operator_key, repo=self.integration)
        self.git("checkout", "-B", "hostile-head", cwd=self.integration)
        (self.integration / "config" / "approval-signers").write_text(
            f"agent-comms-approver {(attacker_key.with_suffix('.pub')).read_text(encoding='utf-8')}", encoding="utf-8"
        )
        self.git("add", "config/approval-signers", cwd=self.integration)
        self.git("commit", "-m", "hostile ambient signers", cwd=self.integration)
        self.name_signers_repo(self.integration)
        # Keep the reviewed head fast-forwardable from the hostile ambient
        # HEAD so the operator-key case still exercises signer resolution
        # from the pinned ref, not from whatever HEAD has checked out.
        self.git("merge", "--no-edit", "hostile-head", cwd=self.repo)

        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        state = self.record()
        head = self.git("rev-parse", "HEAD")
        state["state"] = "human_approved"
        state["reviewed_head"] = head
        state["approved_head"] = head
        state["approval"] = self.v2_approval(state, attacker_key, approver="attacker")
        self.write_record(state)
        self.run_review("gate-merge", "--dispatch-id", "D1", ok=False)

        state = self.record()
        state["approval"] = self.v2_approval(state, operator_key)
        self.write_record(state)
        self.run_review("gate-merge", "--dispatch-id", "D1")
        self.assertEqual(self.record()["state"], "merge_eligible")

    def approve_ready(self, dispatch_id: str = "D1") -> Path:
        key_path = self.enable_approval_signing()
        self.to_execution_reviewed(dispatch_id)
        self.run_review("clean", "--dispatch-id", dispatch_id)
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--dispatch-id",
                dispatch_id,
                "--approver",
                "human",
                "--key",
                str(key_path),
            )
        return key_path

    def work_commit(self, text: str) -> str:
        (self.repo / "tracked.txt").write_text(text, encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", text.strip())
        return self.git("rev-parse", "HEAD")

    def integration_commit(self, filename: str, text: str = "advance\n") -> str:
        (self.integration / filename).write_text(text, encoding="utf-8")
        self.git("add", filename, cwd=self.integration)
        self.git("commit", "-m", f"integration {filename}", cwd=self.integration)
        return self.git("rev-parse", "HEAD", cwd=self.integration)

    def approve_and_gate(self, dispatch_id: str, key_path: Path) -> None:
        self.to_execution_reviewed(dispatch_id)
        self.run_review("clean", "--dispatch-id", dispatch_id)
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--dispatch-id",
                dispatch_id,
                "--approver",
                "human",
                "--key",
                str(key_path),
            )
        self.run_review("gate-merge", "--dispatch-id", dispatch_id)
        self.assertEqual(self.record(dispatch_id)["state"], "merge_eligible")

    def refuse_approve_before_tty(self, dispatch_id: str, key_path: Path, needle: str) -> str:
        before = self.record(dispatch_id)
        with (
            mock.patch.object(approval, "read_tty_confirmation") as tty,
            mock.patch.object(review, "sign_approval_payload") as sign,
        ):
            rc, _stdout, stderr = self.run_review_capture(
                "approve", "--dispatch-id", dispatch_id, "--approver", "human", "--key", str(key_path)
            )
        self.assertNotEqual(rc, 0)
        tty.assert_not_called()
        sign.assert_not_called()
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("CalledProcessError", stderr)
        self.assertIn("review: error:", stderr)
        self.assertIn(needle, stderr)
        state = self.record(dispatch_id)
        self.assertEqual(state, before)
        self.assertEqual(state["state"], "review_clean")
        self.assertIsNone(state["approval"])
        self.assertEqual([event for event in state["history"] if event["event"] == "approve"], [])
        return stderr

    def test_t1_happy_preflight_approval_binds_reviewed_head(self) -> None:
        key_path = self.enable_approval_signing()
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(key_path),
            )
        state = self.record()
        self.assertEqual(state["state"], "human_approved")
        self.assertEqual(state["approved_head"], state["reviewed_head"])
        review.verify_approval_signature(state)

    def test_t2_diverged_integration_refuses_approve_before_tty(self) -> None:
        key_path = self.enable_approval_signing()
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        self.integration_commit("diverge.txt")
        work_tip = self.git("rev-parse", "HEAD")
        integration_tip = self.git("rev-parse", "HEAD", cwd=self.integration)
        self.refuse_approve_before_tty("D1", key_path, "ancestor")
        self.assertEqual(self.git("rev-parse", "HEAD"), work_tip)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.integration), integration_tip)

    def test_t3_preflight_refusal_matrix_before_tty(self) -> None:
        key_path = self.enable_approval_signing()

        def fresh_clean(dispatch_id: str) -> None:
            self.to_execution_reviewed(dispatch_id)
            self.run_review("clean", "--dispatch-id", dispatch_id)

        with self.subTest(case="dirty_review_worktree"):
            fresh_clean("D3-dirty-repo")
            (self.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            try:
                self.refuse_approve_before_tty("D3-dirty-repo", key_path, str(self.repo))
            finally:
                (self.repo / "untracked.txt").unlink()

        with self.subTest(case="dirty_integration_checkout"):
            fresh_clean("D3-dirty-main")
            (self.integration / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            try:
                self.refuse_approve_before_tty("D3-dirty-main", key_path, str(self.integration))
            finally:
                (self.integration / "untracked.txt").unlink()

        with self.subTest(case="review_head_mismatch"):
            fresh_clean("D3-head")
            self.work_commit("moved after review\n")
            self.refuse_approve_before_tty("D3-head", key_path, "reviewed_head")

        with self.subTest(case="source_branch_tip_mismatch"):
            fresh_clean("D3-branch-tip")
            reviewed = self.record("D3-branch-tip")["reviewed_head"]
            self.git("checkout", "--detach")
            self.git("branch", "-f", "work-branch", "integration-main")
            try:
                self.refuse_approve_before_tty("D3-branch-tip", key_path, "source branch work-branch")
            finally:
                self.git("branch", "-f", "work-branch", reviewed)
                self.git("checkout", "work-branch")

        with self.subTest(case="missing_integration_checkout"):
            fresh_clean("D3-missing-main")
            with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(self.root / "no-such-dir")}):
                self.refuse_approve_before_tty("D3-missing-main", key_path, "not a git checkout")

        with self.subTest(case="non_git_integration_checkout"):
            non_git = self.root / "non-git-main"
            non_git.mkdir()
            with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(non_git)}):
                self.refuse_approve_before_tty("D3-missing-main", key_path, "not a git checkout")

        with self.subTest(case="missing_reviewed_head"):
            fresh_clean("D3-reviewed")
            state = self.record("D3-reviewed")
            state["reviewed_head"] = "0" * 40
            self.write_record(state, "D3-reviewed")
            self.refuse_approve_before_tty("D3-reviewed", key_path, "reviewed_head")

    def test_t3b_missing_object_ref_typed_refusals(self) -> None:
        key_path = self.enable_approval_signing()
        self.to_execution_reviewed("D3b")
        self.run_review("clean", "--dispatch-id", "D3b")

        unrelated = self.root / "unrelated-main"
        self.init_repo(unrelated)
        self.git("checkout", "-B", "main", cwd=unrelated)
        (unrelated / "file.txt").write_text("x\n", encoding="utf-8")
        self.git("add", "file.txt", cwd=unrelated)
        self.git("commit", "-m", "unrelated", cwd=unrelated)

        with self.subTest(case="unrelated_integration_lacks_reviewed_object"):
            with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(unrelated)}):
                self.refuse_approve_before_tty("D3b", key_path, "not an object")

        with self.subTest(case="missing_recorded_source_branch"):
            state = self.record("D3b")
            saved_branch = state["target_branch"]
            state["target_branch"] = "no-such-branch"
            self.write_record(state, "D3b")
            try:
                self.refuse_approve_before_tty("D3b", key_path, "no-such-branch")
            finally:
                state = self.record("D3b")
                state["target_branch"] = saved_branch
                self.write_record(state, "D3b")

        with self.subTest(case="unresolvable_integration_head"):
            empty = self.root / "empty-main"
            self.init_repo(empty)
            with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(empty)}):
                self.refuse_approve_before_tty("D3b", key_path, "unresolvable")

    def test_t4_gate_merge_records_merge_eligible_not_merged(self) -> None:
        self.approve_ready()
        approval_before = self.record()["approval"]
        self.run_review("gate-merge", "--dispatch-id", "D1")
        state = self.record()
        self.assertEqual(state["state"], "merge_eligible")
        self.assertEqual(state["approval"], approval_before)
        gate_events = [event for event in state["history"] if event["event"] == "gate-merge"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0]["result"], "merge_eligible")
        self.assertNotIn("merged", json.dumps(gate_events[0]))

    def test_t5_approval_to_gate_race_refuses_and_preserves_approval(self) -> None:
        self.approve_ready()
        self.integration_commit("race.txt")
        before = self.record()
        work_tip = self.git("rev-parse", "HEAD")
        integration_tip = self.git("rev-parse", "HEAD", cwd=self.integration)
        rc, _stdout, stderr = self.run_review_capture("gate-merge", "--dispatch-id", "D1")
        self.assertNotEqual(rc, 0)
        self.assertIn("ancestor", stderr)
        state = self.record()
        self.assertEqual(state, before)
        self.assertEqual(state["state"], "human_approved")
        self.assertEqual(state["approval"], before["approval"])
        self.assertEqual([event for event in state["history"] if event["event"] == "gate-merge"], [])
        self.assertEqual(self.git("rev-parse", "HEAD"), work_tip)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.integration), integration_tip)

    def test_t6_signature_checks_precede_landing_preflight(self) -> None:
        self.enable_approval_signing()
        attacker_key = self.generate_approval_key("attacker-key")
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        # Diverge integration so the preflight would also fail; a forged
        # signature must still refuse for signature reasons, not ancestry.
        self.integration_commit("diverge.txt")
        state = self.record()
        head = self.git("rev-parse", "HEAD")
        state["state"] = "human_approved"
        state["reviewed_head"] = head
        state["approved_head"] = head
        state["approval"] = self.v2_approval(state, attacker_key, approver="attacker")
        self.write_record(state)
        rc, _stdout, stderr = self.run_review_capture("gate-merge", "--dispatch-id", "D1")
        self.assertNotEqual(rc, 0)
        self.assertIn("approval signature verification failed", stderr)
        self.assertNotIn("ancestor", stderr)
        self.assertEqual(self.record()["state"], "human_approved")

    def test_t7_verify_refuses_before_integration(self) -> None:
        key_path = self.enable_approval_signing()
        self.approve_and_gate("D1", key_path)
        before = self.record()
        rc, _stdout, stderr = self.run_review_capture("verify", "--dispatch-id", "D1", "--by", "architect")
        self.assertNotEqual(rc, 0)
        self.assertIn("ancestor", stderr)
        state = self.record()
        self.assertEqual(state, before)
        self.assertEqual(state["state"], "merge_eligible")
        self.assertNotIn("verification", state)
        self.assertEqual([event for event in state["history"] if event["event"] in {"merge-observed", "verify"}], [])

    def test_t8_verify_after_actual_fast_forward(self) -> None:
        key_path = self.enable_approval_signing()
        self.approve_and_gate("D1", key_path)
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        integration_tip = self.git("rev-parse", "HEAD", cwd=self.integration)
        self.run_review("verify", "--dispatch-id", "D1", "--by", "architect")
        state = self.record()
        self.assertEqual(state["state"], "verified")
        events = [event["event"] for event in state["history"]]
        self.assertIn("merge-observed", events)
        self.assertLess(events.index("merge-observed"), events.index("verify"))
        observed = next(event for event in state["history"] if event["event"] == "merge-observed")
        self.assertEqual(observed["approved_head"], state["approved_head"])
        self.assertEqual(observed["integration_head"], integration_tip)

    def test_t9_verify_integration_ancestry(self) -> None:
        key_path = self.enable_approval_signing()

        # Equality: integration HEAD is exactly the approved head.
        self.approve_and_gate("D9-equal", key_path)
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        self.run_review("verify", "--dispatch-id", "D9-equal", "--by", "architect")
        self.assertEqual(self.record("D9-equal")["state"], "verified")

        # Descendant: a later change landed after the approved head.
        self.work_commit("second cycle\n")
        self.approve_and_gate("D9-descendant", key_path)
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        self.integration_commit("later.txt")
        self.run_review("verify", "--dispatch-id", "D9-descendant", "--by", "architect")
        self.assertEqual(self.record("D9-descendant")["state"], "verified")

        # Non-descendant: eligible but never integrated, integration diverged.
        self.git("merge", "--no-edit", "integration-main", cwd=self.repo)
        self.work_commit("third cycle\n")
        self.approve_and_gate("D9-diverged", key_path)
        self.integration_commit("diverge.txt")
        before = self.record("D9-diverged")
        rc, _stdout, stderr = self.run_review_capture("verify", "--dispatch-id", "D9-diverged", "--by", "architect")
        self.assertNotEqual(rc, 0)
        self.assertIn("ancestor", stderr)
        self.assertEqual(self.record("D9-diverged"), before)

        # Configured integration checkout that does not hold the approved head.
        # Under contract 18 the destination binding is rechecked before any
        # ancestry/object probe, so pointing AGENT_COMMS_MAIN at an unrelated
        # checkout refuses for destination mismatch (a stricter, earlier failure
        # than the old "not an object" ancestry error) and mutates nothing.
        unrelated = self.root / "t9-unrelated"
        self.init_repo(unrelated)
        self.git("checkout", "-B", "main", cwd=unrelated)
        (unrelated / "file.txt").write_text("x\n", encoding="utf-8")
        self.git("add", "file.txt", cwd=unrelated)
        self.git("commit", "-m", "unrelated", cwd=unrelated)
        with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(unrelated)}):
            rc, _stdout, stderr = self.run_review_capture("verify", "--dispatch-id", "D9-diverged", "--by", "architect")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("Traceback", stderr)
        self.assertIn("cycle approval destination mismatch", stderr)
        self.assertEqual(self.record("D9-diverged"), before)

    def test_t10_legacy_merged_record_remains_verifiable(self) -> None:
        self.approve_ready("D10")
        state = self.record("D10")
        state["state"] = "merged"
        self.write_record(state, "D10")
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        self.integration_commit("later.txt")
        # A legacy "merged" record verifies against the current integration HEAD.
        self.run_review("verify", "--dispatch-id", "D10", "--by", "architect")
        self.assertEqual(self.record("D10")["state"], "verified")

        # Legacy records still require approved_head == reviewed_head.
        tampered = self.record("D10")
        tampered["dispatch_id"] = "D10b"
        tampered["state"] = "merged"
        tampered.pop("verification", None)
        tampered["approved_head"] = self.git("rev-parse", "integration-main", cwd=self.integration)
        self.write_record(tampered, "D10b")
        rc, _stdout, stderr = self.run_review_capture("verify", "--dispatch-id", "D10b", "--by", "architect")
        self.assertNotEqual(rc, 0)
        self.assertIn("reviewed_head", stderr)
        self.assertEqual(self.record("D10b")["state"], "merged")

    def test_t11_multi_repo_integration_configuration(self) -> None:
        operator_key = self.enable_approval_signing()
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        state = self.record()

        team_main, team_repo, team_head = self.make_team_checkouts()
        cross = dict(state, state="human_approved", repo=str(team_repo), reviewed_head=team_head, approved_head=team_head)
        cross["approval"] = self.v2_approval(cross, operator_key, integration=team_main)
        self.write_record(cross)

        # Misconfigured main (the shared checkout, unrelated to the team
        # repo) refuses loudly with a typed error.
        before = self.record()
        rc, _stdout, stderr = self.run_review_capture("gate-merge", "--dispatch-id", "D1")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("Traceback", stderr)
        self.assertIn("review: error:", stderr)
        self.assertEqual(self.record(), before)

        # Correctly configured main grants eligibility while signature
        # verification still reads the centralized shared-install signers.
        with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(team_main)}):
            self.run_review("gate-merge", "--dispatch-id", "D1")
        self.assertEqual(self.record()["state"], "merge_eligible")

    # ----- Contract 18 cycle-approval destination-binding proof families -----

    def _clone_integration(self, name: str) -> Path:
        """A separate integration checkout on the same named branch. Distinct
        canonical path, so its derived repo_identity differs from self.integration."""
        other = self.root / name
        self.git("clone", str(self.integration), str(other))
        self.git("checkout", "-B", "integration-main", cwd=other)
        return other

    def test_family3_valid_approval_replayed_in_other_checkout_refused(self) -> None:
        operator_key = self.enable_approval_signing()
        # Approve (human_approved) bound to self.integration on integration-main.
        self.to_execution_reviewed("D1")
        self.run_review("clean", "--dispatch-id", "D1")
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(operator_key),
            )

        already = self._clone_integration(
            "family3-already"
        )  # holds approved_head object
        stranger = self.root / "family3-stranger"  # a checkout needing fast-forward
        self.init_repo(stranger)
        self.git("checkout", "-B", "integration-main", cwd=stranger)
        (stranger / "s.txt").write_text("s\n", encoding="utf-8")
        self.git("add", "s.txt", cwd=stranger)
        self.git("commit", "-m", "stranger base", cwd=stranger)

        # gate-merge (from human_approved) replayed at either alternate refuses on
        # destination mismatch and mutates nothing.
        before = self.record("D1")
        for alt in (already, stranger):
            with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(alt)}):
                rc, _out, err = self.run_review_capture(
                    "gate-merge", "--dispatch-id", "D1"
                )
            self.assertNotEqual(rc, 0)
            self.assertIn("cycle approval destination mismatch", err)
            self.assertEqual(self.record("D1"), before)

        # Gate correctly and fast-forward so the ONLY remaining obstacle to
        # verify is the destination binding.
        self.run_review("gate-merge", "--dispatch-id", "D1")
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        eligible = self.record("D1")

        for alt in (already, stranger):
            alt_head = self.git("rev-parse", "HEAD", cwd=alt)
            with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(alt)}):
                rc, _out, err = self.run_review_capture(
                    "verify", "--dispatch-id", "D1", "--by", "architect"
                )
                self.assertNotEqual(rc, 0)
                self.assertIn("cycle approval destination mismatch", err)
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=alt), alt_head)
            self.assertEqual(self.record("D1"), eligible)
            self.assertNotEqual(self.record("D1")["state"], "verified")

    def test_family4_target_ref_change_alone_refused(self) -> None:
        operator_key = self.enable_approval_signing()
        self.approve_and_gate("D1", operator_key)
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        before = self.record("D1")
        # Same canonical repo_identity, only the checked-out branch (target_ref)
        # changes: verification still refuses with a destination mismatch.
        self.git(
            "branch", "-m", "integration-main", "renamed-main", cwd=self.integration
        )
        rc, _out, err = self.run_review_capture(
            "verify", "--dispatch-id", "D1", "--by", "architect"
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("cycle approval destination mismatch", err)
        self.assertIn("refs/heads/integration-main", err)
        self.assertIn("refs/heads/renamed-main", err)
        self.assertEqual(self.record("D1"), before)

    def test_family6_record_tamper_and_destination_change_refuse_without_mutation(
        self,
    ) -> None:
        operator_key = self.enable_approval_signing()
        self.approve_and_gate("D1", operator_key)
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        baseline = self.record("D1")

        # Editing a signed approval member invalidates the signature.
        tampered = copy.deepcopy(baseline)
        tampered["approval"]["repo_identity"] = "local-worktree-v1:/srv/elsewhere"
        self.write_record(tampered, "D1")
        rc, _out, err = self.run_review_capture(
            "verify", "--dispatch-id", "D1", "--by", "architect"
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("approval signature verification failed", err)
        self.assertEqual(self.record("D1"), tampered)

        # The authentic approval plus a changed configured destination reports a
        # destination mismatch (not a signature failure) and mutates nothing.
        self.write_record(baseline, "D1")
        self.git("branch", "-m", "integration-main", "moved-main", cwd=self.integration)
        rc, _out, err = self.run_review_capture(
            "verify", "--dispatch-id", "D1", "--by", "architect"
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("cycle approval destination mismatch", err)
        self.assertEqual(self.record("D1"), baseline)

    def test_family7_legacy_unversioned_approval_refuses_across_schemas(self) -> None:
        operator_key = self.enable_approval_signing()
        self.approve_and_gate("D1", operator_key)
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        v2 = self.record("D1")
        legacy = {
            "approver": "human",
            "mechanism": "dev-tty-presence+ssh-sig",
            "signature": v2["approval"]["signature"],
        }
        remedy = "review approve --replace-legacy"

        # schema-2 merge_eligible: direct verify refuses.
        rec = copy.deepcopy(v2)
        rec["approval"] = dict(legacy)
        rec["state"] = "merge_eligible"
        self.write_record(rec, "D1")
        before = self.record("D1")
        rc, _out, err = self.run_review_capture(
            "verify", "--dispatch-id", "D1", "--by", "architect"
        )
        self.assertNotEqual(rc, 0)
        self.assertIn(remedy, err)
        self.assertEqual(self.record("D1"), before)

        # schema-2 human_approved: gate-merge refuses.
        rec_h = copy.deepcopy(v2)
        rec_h["approval"] = dict(legacy)
        rec_h["state"] = "human_approved"
        self.write_record(rec_h, "D1")
        before_h = self.record("D1")
        rc, _out, err = self.run_review_capture("gate-merge", "--dispatch-id", "D1")
        self.assertNotEqual(rc, 0)
        self.assertIn(remedy, err)
        self.assertEqual(self.record("D1"), before_h)

        # schema-2 merged replayed through a repointed AGENT_COMMS_MAIN: still
        # refuses, and the alternate integration checkout stays unmodified.
        other = self._clone_integration("family7-other")
        other_head = self.git("rev-parse", "HEAD", cwd=other)
        rec_m = copy.deepcopy(v2)
        rec_m["approval"] = dict(legacy)
        rec_m["state"] = "merged"
        self.write_record(rec_m, "D1")
        before_m = self.record("D1")
        with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(other)}):
            rc, _out, err = self.run_review_capture(
                "verify", "--dispatch-id", "D1", "--by", "architect"
            )
        self.assertNotEqual(rc, 0)
        self.assertIn(remedy, err)
        self.assertEqual(self.record("D1"), before_m)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=other), other_head)

        # schema-1 legacy record: verify still refuses with the same remedy.
        s1 = self.schema1_verify_record("S1", "merge_eligible", operator_key)
        s1["approval"] = dict(legacy)
        self.write_record(s1, "S1")
        s1_before = self.record("S1")
        rc, _out, err = self.run_review_capture(
            "verify", "--dispatch-id", "S1", "--by", "architect"
        )
        self.assertNotEqual(rc, 0)
        self.assertIn(remedy, err)
        self.assertEqual(self.record("S1"), s1_before)

    def test_family8_replace_legacy_premerge_merged_and_already_v2(self) -> None:
        operator_key = self.enable_approval_signing()
        self.approve_and_gate("D1", operator_key)
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        v2 = self.record("D1")
        # A genuine unversioned v1 approval carries an unrelated signature; the
        # replace-legacy path never verifies it, so a placeholder is faithful and
        # keeps the freshly signed v2 signature distinct from it.
        legacy = {
            "approver": "human",
            "mechanism": "dev-tty-presence+ssh-sig",
            "signature": "-----BEGIN SSH SIGNATURE-----\nlegacy-v1-unversioned\n-----END SSH SIGNATURE-----\n",
        }

        # (a) Premerge legacy -> replace-legacy re-signs a fresh v2 approval,
        #     archives the old one, and returns to human_approved.
        rec = copy.deepcopy(v2)
        rec["approval"] = dict(legacy)
        rec["state"] = "human_approved"
        self.write_record(rec, "D1")
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--replace-legacy",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(operator_key),
            )
        after = self.record("D1")
        self.assertEqual(after["state"], "human_approved")
        self.assertEqual(
            after["approval"]["payload_version"], review.CYCLE_PAYLOAD_VERSION
        )
        self.assertNotEqual(after["approval"]["signature"], legacy["signature"])
        self.assertEqual(after["superseded_approvals"][-1]["approval"], legacy)
        self.assertEqual(
            after["superseded_approvals"][-1]["prior_state"], "human_approved"
        )
        self.run_review("gate-merge", "--dispatch-id", "D1")
        self.assertEqual(self.record("D1")["state"], "merge_eligible")

        # (b) An already-v2 approval refuses replacement without mutation.
        v2_now = self.record("D1")
        rc, _out, err = self.run_review_capture(
            "approve",
            "--replace-legacy",
            "--dispatch-id",
            "D1",
            "--approver",
            "human",
            "--key",
            str(operator_key),
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("already carries the v2 destination binding", err)
        self.assertEqual(self.record("D1"), v2_now)

        # (c) Merged recovery after the source checkout and branch are gone; the
        #     recovery reads only the configured integration destination.
        merged = copy.deepcopy(v2)
        merged["approval"] = dict(legacy)
        merged["state"] = "merged"
        self.write_record(merged, "D1")
        self.git("worktree", "remove", "--force", str(self.repo))
        self.git("branch", "-D", "work-branch", cwd=self.integration)
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--replace-legacy",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(operator_key),
            )
        recovered = self.record("D1")
        self.assertEqual(recovered["state"], "merged")
        self.assertEqual(
            recovered["approval"]["payload_version"], review.CYCLE_PAYLOAD_VERSION
        )
        self.assertNotEqual(recovered["approval"]["signature"], legacy["signature"])

        # (d) A declined confirmation leaves the legacy record untouched.
        declined = copy.deepcopy(v2)
        declined["approval"] = dict(legacy)
        declined["state"] = "merged"
        self.write_record(declined, "D1")
        before_decline = self.record("D1")
        with mock.patch.object(approval, "read_tty_confirmation", return_value="NO"):
            rc, _out, _err = self.run_review_capture(
                "approve",
                "--replace-legacy",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(operator_key),
            )
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.record("D1"), before_decline)

    def test_family9_schema_validation_all_or_none_and_legacy_unchanged(self) -> None:
        operator_key = self.enable_approval_signing()
        self.approve_and_gate("D1", operator_key)
        v2 = self.record("D1")

        # A well-formed v2 approval and a legacy approval with none of the trio
        # both validate and round-trip under schema 2.
        contracts.validate_schema2_record(v2)
        legacy = copy.deepcopy(v2)
        legacy["approval"] = {"approver": "human", "mechanism": "m", "signature": "s"}
        contracts.validate_schema2_record(legacy)

        # A partial trio, an invalid version, and a malformed field each refuse.
        partial = copy.deepcopy(v2)
        del partial["approval"]["target_ref"]
        with self.assertRaisesRegex(review.ReviewError, "all-or-none"):
            contracts.validate_schema2_record(partial)
        bad_version = copy.deepcopy(v2)
        bad_version["approval"]["payload_version"] = "agent-comms-approval-v1"
        with self.assertRaisesRegex(review.ReviewError, "payload_version"):
            contracts.validate_schema2_record(bad_version)
        bad_identity = copy.deepcopy(v2)
        bad_identity["approval"]["repo_identity"] = "/no-prefix"
        with self.assertRaisesRegex(review.ReviewError, "repo_identity"):
            contracts.validate_schema2_record(bad_identity)

        # The closed schema-2 approval validator still rejects an unknown member.
        unknown = copy.deepcopy(v2)
        unknown["approval"]["stowaway"] = "x"
        with self.assertRaises(review.ReviewError):
            contracts.validate_schema2_record(unknown)
        self.assertEqual(v2["schema_version"], 2)

        # Schema-1 stays a loose reader: no approval and a loose legacy approval
        # both validate unchanged; only a present v2 trio is checked all-or-none.
        s1 = {
            "schema_version": 1,
            "dispatch_id": "S1",
            "state": "merge_eligible",
            "repo": str(self.repo),
            "brief_path": str(self.brief),
            "dod": [],
            "findings": [],
            "gate_runs": [],
        }
        contracts.validate_schema1_record(dict(s1))
        contracts.validate_schema1_record(
            dict(s1, approval={"approver": "h", "signature": "s", "extra": "loose-ok"})
        )
        with self.assertRaisesRegex(review.ReviewError, "all-or-none"):
            contracts.validate_schema1_record(
                dict(s1, approval={"payload_version": review.CYCLE_PAYLOAD_VERSION})
            )

    # ----- Contract 18 required executable proofs: derivation, approve, replace -----

    def test_derive_cycle_destination_refuses_embedded_newline_path(self) -> None:
        # Contract 18 proof family 1, the load-bearing part: a legal Unix checkout
        # path may itself contain an embedded newline. derive_cycle_destination is
        # exercised against a REAL initialized integration checkout whose on-disk
        # toplevel path carries that newline; it must refuse at the source, before
        # any payload serialization, so the interior newline can never survive to
        # become an extra signed-payload line. A direct approval_payload CR/LF unit
        # cannot reach this guard, which fires while deriving the destination.
        newline_root = self.root / "embedded\nnewline-checkout"
        self.init_repo(newline_root)
        self.git("checkout", "-B", "integration-main", cwd=newline_root)
        (newline_root / "f.txt").write_text("x\n", encoding="utf-8")
        self.git("add", "f.txt", cwd=newline_root)
        self.git("commit", "-m", "base", cwd=newline_root)
        # The path is genuinely on disk with the newline, not a synthesized string:
        # git's own toplevel report still carries it (interior, not trailing).
        toplevel = self.git("rev-parse", "--show-toplevel", cwd=newline_root)
        self.assertIn("\n", toplevel)
        with (
            mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(newline_root)}),
            mock.patch.object(
                approval, "approval_payload", side_effect=AssertionError("serialized")
            ) as payload,
        ):
            with self.assertRaises(review.ReviewError) as ctx:
                approval.derive_cycle_destination("approve")
        # Refused for the newline-bearing repo_identity, before serialization.
        self.assertIn("invalid destination field repo_identity", str(ctx.exception))
        payload.assert_not_called()

    def test_approve_prompt_displays_destination_and_remeasures_twice(self) -> None:
        # Contract 18 proof family 2: ordinary approve displays the destination in
        # the TTY prompt, remeasures the destination/preflight at both boundaries,
        # and records only after the same destination is observed twice.
        key = self.enable_approval_signing()
        self.to_execution_reviewed("D1")
        self.run_review("clean", "--dispatch-id", "D1")
        repo_identity, target_ref = self.cycle_destination()
        real_derive = approval.derive_cycle_destination
        contexts: list[str] = []

        def counting_derive(context: str = "cycle destination") -> tuple[str, str]:
            contexts.append(context)
            return real_derive(context)

        prompts: list[str] = []

        def capture_tty(prompt: str, *_a, **_k) -> str:
            prompts.append(prompt)
            return "APPROVE"

        with (
            mock.patch.object(
                approval, "derive_cycle_destination", side_effect=counting_derive
            ),
            mock.patch.object(
                approval, "read_tty_confirmation", side_effect=capture_tty
            ),
        ):
            self.run_review(
                "approve",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(key),
            )
        # Remeasured at both boundaries, once before the human is asked and once
        # after, so a destination that drifts across the prompt is caught.
        self.assertEqual(contexts, ["approve", "approve"])
        self.assertEqual(len(prompts), 1)
        self.assertIn(f"repo_identity: {repo_identity}", prompts[0])
        self.assertIn(f"target_ref:    {target_ref}", prompts[0])
        after = self.record("D1")
        self.assertEqual(after["state"], "human_approved")
        self.assertEqual(after["approval"]["repo_identity"], repo_identity)
        self.assertEqual(after["approval"]["target_ref"], target_ref)
        review.verify_approval_signature(after)

    def test_approve_destination_change_between_confirmation_and_signing_refuses(
        self,
    ) -> None:
        # Contract 18 proof family 2, refusal edge: a destination change between
        # the operator's confirmation and signing refuses, and the record, its
        # history, and its (absent) approval are all left untouched with no
        # signing call.
        key = self.enable_approval_signing()
        self.to_execution_reviewed("D1")
        self.run_review("clean", "--dispatch-id", "D1")
        before = self.record("D1")

        def drift_then_confirm(_prompt: str, *_a, **_k) -> str:
            # The integration checkout is retargeted to a different named branch
            # after the prompt is shown but before the signature is produced.
            self.git(
                "branch", "-m", "integration-main", "drifted-main", cwd=self.integration
            )
            return "APPROVE"

        with (
            mock.patch.object(
                approval, "read_tty_confirmation", side_effect=drift_then_confirm
            ),
            mock.patch.object(
                approval, "sign_approval_payload", wraps=approval.sign_approval_payload
            ) as sign,
        ):
            rc, _out, err = self.run_review_capture(
                "approve",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(key),
            )
        self.assertNotEqual(rc, 0)
        self.assertIn(
            "integration destination changed while awaiting confirmation", err
        )
        self.assertNotIn("Traceback", err)
        sign.assert_not_called()
        after = self.record("D1")
        self.assertEqual(after, before)
        self.assertEqual(after["state"], "review_clean")
        self.assertIsNone(after["approval"])
        self.assertEqual([e for e in after["history"] if e["event"] == "approve"], [])

    def _placeholder_legacy_approval(self) -> dict:
        # A genuine unversioned v1 approval object. The replace-legacy path never
        # verifies its signature, so a fixed placeholder is faithful and stays
        # distinct from any freshly signed v2 signature.
        return {
            "approver": "human",
            "mechanism": "dev-tty-presence+ssh-sig",
            "signature": "-----BEGIN SSH SIGNATURE-----\nlegacy-v1-unversioned\n-----END SSH SIGNATURE-----\n",
        }

    def test_family8_replace_legacy_merge_eligible_returns_to_human_approved(
        self,
    ) -> None:
        # Contract 18 replacement coverage: a successful schema-2 merge_eligible
        # replacement re-signs a fresh v2 approval, archives the legacy one with
        # its merge_eligible prior_state, and returns to human_approved so the gate
        # runs again.
        operator_key = self.enable_approval_signing()
        self.approve_and_gate("D1", operator_key)
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        v2 = self.record("D1")
        legacy = self._placeholder_legacy_approval()
        rec = copy.deepcopy(v2)
        rec["approval"] = dict(legacy)
        rec["state"] = "merge_eligible"
        self.write_record(rec, "D1")
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--replace-legacy",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(operator_key),
            )
        after = self.record("D1")
        self.assertEqual(after["state"], "human_approved")
        self.assertEqual(
            after["approval"]["payload_version"], review.CYCLE_PAYLOAD_VERSION
        )
        self.assertNotEqual(after["approval"]["signature"], legacy["signature"])
        self.assertEqual(after["superseded_approvals"][-1]["approval"], legacy)
        self.assertEqual(
            after["superseded_approvals"][-1]["prior_state"], "merge_eligible"
        )
        self.run_review("gate-merge", "--dispatch-id", "D1")
        self.assertEqual(self.record("D1")["state"], "merge_eligible")

    def test_family8_replace_legacy_schema1_preserves_schema_version(self) -> None:
        # Contract 18 replacement coverage: at least one successful schema-1
        # replacement re-signs the destination binding while preserving the
        # historical schema_version and terminal merged state.
        operator_key = self.enable_approval_signing()
        head = self.git("rev-parse", "HEAD", cwd=self.integration)
        legacy = self._placeholder_legacy_approval()
        s1 = self.schema1_record("S1", "merged")
        s1.update(
            {
                "reviewed_head": head,
                "approved_head": head,
                "brief_sha256": hashlib.sha256(self.brief.read_bytes()).hexdigest(),
                "history": [
                    {"event": "historical", "timestamp": "2026-01-01T00:00:00Z"}
                ],
                "approval": dict(legacy),
            }
        )
        self.write_record(s1, "S1")
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--replace-legacy",
                "--dispatch-id",
                "S1",
                "--approver",
                "human",
                "--key",
                str(operator_key),
            )
        after = self.record("S1")
        self.assertEqual(after["schema_version"], 1)
        self.assertEqual(after["state"], "merged")
        self.assertEqual(
            after["approval"]["payload_version"], review.CYCLE_PAYLOAD_VERSION
        )
        self.assertNotEqual(after["approval"]["signature"], legacy["signature"])
        self.assertEqual(after["superseded_approvals"][-1]["prior_state"], "merged")

    def test_family8_replace_legacy_representative_refusals_are_no_ops(self) -> None:
        # Contract 18 replacement coverage: representative wrong-state, changed
        # premerge source, dirty required checkout, missing integrated object,
        # destination-drift, and signing-failure refusals each mutate nothing.
        operator_key = self.enable_approval_signing()
        legacy = self._placeholder_legacy_approval()

        def assert_no_op(dispatch_id, key, needle, tty):
            before = self.record(dispatch_id)
            with (
                mock.patch.object(approval, "read_tty_confirmation", side_effect=tty),
                mock.patch.object(
                    approval,
                    "sign_approval_payload",
                    wraps=approval.sign_approval_payload,
                ) as sign,
            ):
                rc, _out, err = self.run_review_capture(
                    "approve",
                    "--replace-legacy",
                    "--dispatch-id",
                    dispatch_id,
                    "--approver",
                    "human",
                    "--key",
                    str(key),
                )
            self.assertNotEqual(rc, 0)
            self.assertIn(needle, err)
            self.assertNotIn("Traceback", err)
            self.assertEqual(self.record(dispatch_id), before)
            return sign

        # Reach merge_eligible then fast-forward integration so both premerge and
        # merged legacy inputs are derivable from one v2 record.
        self.approve_and_gate("D1", operator_key)
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        v2 = self.record("D1")

        def legacy_record(state: str) -> dict:
            rec = copy.deepcopy(v2)
            rec["approval"] = dict(legacy)
            rec["state"] = state
            return rec

        def always_approve(*_a, **_k) -> str:
            return "APPROVE"

        # Wrong state: a terminal verified record is not a replaceable legacy state.
        self.write_record(legacy_record("verified"), "D1")
        sign = assert_no_op(
            "D1", operator_key, "is not a replaceable legacy state", always_approve
        )
        sign.assert_not_called()

        # Changed premerge source: the review worktree HEAD moved away from the
        # recorded reviewed_head; refuses before the human is asked or anything signed.
        self.write_record(legacy_record("human_approved"), "D1")
        self.work_commit("premerge drift\n")
        sign = assert_no_op("D1", operator_key, "reviewed_head", always_approve)
        sign.assert_not_called()
        # Restore the worktree HEAD to the reviewed_head for the remaining cases.
        self.git("reset", "--hard", v2["reviewed_head"])

        # Destination drift: the integration branch is retargeted between the
        # confirmation and signing; refuses after the prompt with no signing call.
        self.write_record(legacy_record("human_approved"), "D1")

        def drift(_prompt: str, *_a, **_k) -> str:
            self.git(
                "branch", "-m", "integration-main", "drifted-main", cwd=self.integration
            )
            return "APPROVE"

        sign = assert_no_op(
            "D1", operator_key, "destination changed while awaiting confirmation", drift
        )
        sign.assert_not_called()
        self.git(
            "branch", "-m", "drifted-main", "integration-main", cwd=self.integration
        )

        # Signing failure: valid premerge input, but the signing key is absent, so
        # the real signer refuses after both destination remeasures agree.
        self.write_record(legacy_record("human_approved"), "D1")
        before = self.record("D1")
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            rc, _out, err = self.run_review_capture(
                "approve",
                "--replace-legacy",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(self.root / "absent-key"),
            )
        self.assertNotEqual(rc, 0)
        self.assertIn("approval signing key unavailable", err)
        self.assertEqual(self.record("D1"), before)

        # Dirty required checkout: the merged replacement remeasure refuses a dirty
        # integration checkout before the human is asked.
        self.write_record(legacy_record("merged"), "D1")
        (self.integration / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        sign = assert_no_op("D1", operator_key, "working tree is dirty", always_approve)
        sign.assert_not_called()
        (self.integration / "dirty.txt").unlink()

        # Missing integrated object: the merged replacement remeasure refuses when
        # the configured integration checkout does not hold the approved head.
        unrelated = self.root / "unrelated-integration"
        self.init_repo(unrelated)
        self.git("checkout", "-B", "integration-main", cwd=unrelated)
        (unrelated / "u.txt").write_text("u\n", encoding="utf-8")
        self.git("add", "u.txt", cwd=unrelated)
        self.git("commit", "-m", "unrelated base", cwd=unrelated)
        self.write_record(legacy_record("merged"), "D1")
        before = self.record("D1")
        with (
            mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(unrelated)}),
            mock.patch.object(
                approval, "read_tty_confirmation", return_value="APPROVE"
            ),
            mock.patch.object(
                approval, "sign_approval_payload", wraps=approval.sign_approval_payload
            ) as sign,
        ):
            rc, _out, err = self.run_review_capture(
                "approve",
                "--replace-legacy",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(operator_key),
            )
        self.assertNotEqual(rc, 0)
        self.assertIn("not an object", err)
        sign.assert_not_called()
        self.assertEqual(self.record("D1"), before)

    def test_gate_merge_blocks_dirty_tree(self) -> None:
        self.approve_ready()
        (self.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        self.run_review("gate-merge", "--dispatch-id", "D1", ok=False)

    def test_gate_merge_blocks_head_mismatch(self) -> None:
        self.approve_ready()
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "after approval"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        self.run_review("gate-merge", "--dispatch-id", "D1", ok=False)

    def test_approve_does_not_read_stdin(self) -> None:
        key_path = self.enable_approval_signing()
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        with mock.patch("sys.stdin") as stdin:
            stdin.readline.return_value = "APPROVE\n"
            with mock.patch.object(
                approval, "read_tty_confirmation", return_value="NO"
            ):
                self.run_review(
                    "approve", "--dispatch-id", "D1", "--key", str(key_path), ok=False
                )
        self.assertEqual(self.record()["state"], "review_clean")
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--dispatch-id",
                "D1",
                "--approver",
                "human",
                "--key",
                str(key_path),
            )
        self.assertEqual(self.record()["state"], "human_approved")

    def test_tty_confirmation_reads_from_pty(self) -> None:
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        os.write(master, b"APPROVE\n")
        self.assertEqual(review.read_tty_confirmation("prompt: ", tty_path=os.ttyname(slave)), "APPROVE")

    def test_tty_confirmation_without_tty_fails_cleanly(self) -> None:
        with self.assertRaisesRegex(review.ReviewError, "approval requires an interactive terminal"):
            review.read_tty_confirmation("prompt: ", tty_path=str(self.root / "missing-dir" / "tty"))

    def test_approve_without_tty_fails_cleanly_and_keeps_review_clean(self) -> None:
        self.to_execution_reviewed()
        self.run_review("clean", "--dispatch-id", "D1")
        real_open = open

        def open_without_tty(path, *args, **kwargs):
            if path == "/dev/tty":
                raise OSError("no controlling terminal")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=open_without_tty):
            self.run_review("approve", "--dispatch-id", "D1", "--approver", "human", ok=False)
        self.assertEqual(self.record()["state"], "review_clean")

    def test_concurrent_mutations_do_not_lose_updates(self) -> None:
        self.to_execution_reviewed()
        script = """
import sys
from pathlib import Path
from agent_comms import review
from agent_comms.reviewing import store
store.REVIEW_ROOT = Path(sys.argv[1])
raise SystemExit(review.main([
    "finding", "--dispatch-id", "D1", "--severity", "nit",
    "--loc", sys.argv[2], "--problem", sys.argv[3],
    "--impact", "impact", "--fix", "fix",
]))
"""
        env = dict(os.environ)
        env["PYTHONPYCACHEPREFIX"] = str(self.root / "pycache")
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.review_root), f"loc-{i}", f"problem-{i}"],
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(2)
        ]
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, stdout + stderr)
        state = self.record()
        review.validate_record(state)
        self.assertEqual(len(state["findings"]), 2)
        self.assertEqual({finding["id"] for finding in state["findings"]}, {"F1", "F2"})

    # ----- safe-rebind-after-clean-001 helpers -----

    def record_bytes(self, dispatch_id: str = "D1") -> bytes:
        return (self.review_root / f"{dispatch_id}.json").read_bytes()

    def align_integration(self) -> str:
        """Fast-forward integration-main to the work-branch tip so the next
        cycle's base_commit is in integration ancestry (real team topology)."""
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        return self.git("rev-parse", "HEAD", cwd=self.integration)

    def to_dispatched(self, dispatch_id: str = "D1", dod: Path | None = None) -> None:
        self.open_review(dispatch_id, dod=dod)
        self.run_review("brief-check", "--dispatch-id", dispatch_id, "--clean", "--by", "codex", "--surface-verdict", "complete", "--surface-reason", "fixture declaration is complete")
        self.align_integration()  # contract 17: dispatch at one clean common HEAD
        self.run_review("mark-dispatched", "--dispatch-id", dispatch_id)

    def to_clean_with_work(self, dispatch_id: str = "D1", text: str = "feature work\n", dod: Path | None = None, checks: tuple[str, ...] = ("green",)) -> str:
        """Full governed flow with a real worker commit: align integration,
        open, brief-check, dispatch, commit work, mark-executed, gates, clean.
        Returns the reviewed (worker) head."""
        self.align_integration()
        self.to_dispatched(dispatch_id, dod=dod)
        worked = self.work_commit(text)
        self.run_review("mark-executed", "--dispatch-id", dispatch_id)
        self.run_review("gates", "--dispatch-id", dispatch_id, "--check", *checks)
        self.run_review("clean", "--dispatch-id", dispatch_id)
        return worked

    def advance_and_rebase(self, filename: str = "advance.txt") -> tuple[str, str]:
        """Integration advances by an unrelated commit; the architect updates
        and measures integration first, then rebases the source branch onto
        that exact tip. Returns (new integration head, new source head)."""
        new_base = self.integration_commit(filename)
        self.git("rebase", "integration-main")
        return new_base, self.git("rev-parse", "HEAD")

    def refuse_rebind(self, dispatch_id: str, needle: str) -> str:
        before = self.record_bytes(dispatch_id)
        rc, _stdout, stderr = self.run_review_capture("rebind", "--dispatch-id", dispatch_id)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("Traceback", stderr)
        self.assertIn("review: error:", stderr)
        self.assertIn(needle, stderr)
        self.assertEqual(self.record_bytes(dispatch_id), before)
        return stderr

    def refuse_mark_executed(self, dispatch_id: str, needle: str) -> str:
        before = self.record_bytes(dispatch_id)
        rc, _stdout, stderr = self.run_review_capture("mark-executed", "--dispatch-id", dispatch_id)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("Traceback", stderr)
        self.assertIn(needle, stderr)
        self.assertEqual(self.record_bytes(dispatch_id), before)
        state = self.record(dispatch_id)
        self.assertEqual(state["state"], "dispatched")
        self.assertIsNone(state["reviewed_head"])
        self.assertNotIn("trigger_closed", state)
        self.assertEqual([event for event in state["history"] if event["event"] == "mark-executed"], [])
        return stderr

    def two_check_dod(self) -> Path:
        dod = self.root / "dod-two.json"
        dod.write_text(
            json.dumps(
                [
                    {"id": "unit", "claim": "green check", "check_id": "green"},
                    {"id": "sweep", "claim": "extra check", "check_id": "extra", "argv": ["true"]},
                ]
            ),
            encoding="utf-8",
        )
        return dod

    # ----- evidence-binding-002 T1-T17 -----

    def evidence_dod(self, *, required: bool = False, legacy: str = "") -> Path:
        dod = self.root / f"dod-evidence-{len(list(self.root.glob('dod-evidence-*')))}.json"
        dod.write_text(
            json.dumps(
                [
                    {"id": "unit", "claim": "green", "check_id": "green"},
                    {"id": "runtime", "claim": "external cert", "check_id": "runtime-cert", "required": required, "evidence": legacy},
                ]
            ),
            encoding="utf-8",
        )
        return dod

    def evidence_log(self, text: str = "cert green\n") -> Path:
        path = self.root / f"cert-{len(list(self.root.glob('cert-*')))}.log"
        path.write_text(text, encoding="utf-8")
        return path

    def attach_evidence(self, dispatch_id: str, log: Path, **extra: str) -> tuple[int, str, str]:
        record = self.record(dispatch_id)
        argv = [
            "evidence", "--dispatch-id", dispatch_id, "--criterion-id", extra.get("criterion_id", "runtime"),
            "--log", str(log), "--runtime-version", extra.get("runtime_version", "codex 1"),
            "--counts", extra.get("counts", "10 tests, 0 failures"), "--head", extra.get("head", record["reviewed_head"]),
            "--by", "architect",
        ]
        return self.run_review_capture(*argv)

    def evidence_clean(self, dispatch_id: str = "D-ev", *, required: bool = False, legacy: str = "") -> Path:
        dod = self.evidence_dod(required=required, legacy=legacy)
        if required:
            self.align_integration(); self.to_dispatched(dispatch_id, dod=dod); self.work_commit(f"{dispatch_id}\n")
            self.run_review("mark-executed", "--dispatch-id", dispatch_id)
            self.run_review("gates", "--dispatch-id", dispatch_id, "--check", "green", "--skip", "runtime-cert", "--reason", "external cert", "--risk", "bound below")
            self.run_review("clean", "--dispatch-id", dispatch_id)
        else:
            self.to_clean_with_work(dispatch_id, text=f"{dispatch_id}\n", dod=dod, checks=("green",))
        return dod

    def test_evidence_t1_required_false_missing_refuses_before_tty(self) -> None:
        key = self.enable_approval_signing()
        self.evidence_clean("D-ev1")
        with mock.patch.object(approval, "read_tty_confirmation") as tty:
            rc, _out, err = self.run_review_capture("approve", "--dispatch-id", "D-ev1", "--key", str(key))
        self.assertNotEqual(rc, 0); self.assertIn("runtime", err); self.assertIn("missing evidence payload", err); tty.assert_not_called()

    def test_evidence_t2_required_true_skip_does_not_bind(self) -> None:
        key = self.enable_approval_signing(); self.evidence_clean("D-ev2", required=True)
        with mock.patch.object(approval, "read_tty_confirmation") as tty:
            rc, _out, err = self.run_review_capture("approve", "--dispatch-id", "D-ev2", "--key", str(key))
        self.assertNotEqual(rc, 0); self.assertIn("missing evidence payload", err); tty.assert_not_called()

    def test_evidence_t3_attach_hash_and_approve(self) -> None:
        key = self.enable_approval_signing(); self.evidence_clean("D-ev3"); log = self.evidence_log()
        rc, _out, err = self.attach_evidence("D-ev3", log); self.assertEqual(rc, 0, err)
        payload = self.record("D-ev3")["dod"][1]["evidence_payload"]
        self.assertEqual(
            payload["log_sha256"], review.hashlib.sha256(log.read_bytes()).hexdigest()
        )
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review("approve", "--dispatch-id", "D-ev3", "--key", str(key))

    def test_evidence_t4_tamper_refuses_with_both_digests(self) -> None:
        key = self.enable_approval_signing(); self.evidence_clean("D-ev4"); log = self.evidence_log(); self.attach_evidence("D-ev4", log)
        expected = self.record("D-ev4")["dod"][1]["evidence_payload"]["log_sha256"]; log.write_text("tampered\n", encoding="utf-8")
        measured = review.hashlib.sha256(log.read_bytes()).hexdigest()
        rc, _out, err = self.run_review_capture("approve", "--dispatch-id", "D-ev4", "--key", str(key))
        self.assertNotEqual(rc, 0); self.assertIn(expected, err); self.assertIn(measured, err)

    def test_evidence_t5_removed_log_refuses(self) -> None:
        key = self.enable_approval_signing(); self.evidence_clean("D-ev5"); log = self.evidence_log(); self.attach_evidence("D-ev5", log); log.unlink()
        rc, _out, err = self.run_review_capture("approve", "--dispatch-id", "D-ev5", "--key", str(key))
        self.assertNotEqual(rc, 0); self.assertIn(f"evidence log missing {log.resolve()}", err)

    def test_evidence_t6_input_refusals_are_typed(self) -> None:
        self.evidence_clean("D-ev6"); head = self.record("D-ev6")["reviewed_head"]
        cases = [("--log", str(self.root / "missing")), ("--runtime-version", " "), ("--counts", " "), ("--head", "BAD")]
        for flag, value in cases:
            argv = ["evidence", "--dispatch-id", "D-ev6", "--criterion-id", "runtime", "--log", str(self.evidence_log()), "--runtime-version", "v", "--counts", "c", "--head", head]
            argv[argv.index(flag) + 1] = value
            rc, _out, err = self.run_review_capture(*argv)
            self.assertNotEqual(rc, 0); self.assertIn(flag, err); self.assertNotIn("Traceback", err)
        unreadable = self.evidence_log()
        with mock.patch.object(review.os, "access", return_value=False):
            rc, _out, err = self.attach_evidence("D-ev6", unreadable)
        self.assertNotEqual(rc, 0); self.assertIn("--log", err); self.assertNotIn("Traceback", err)
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(review.ReviewError, "cannot read evidence log"):
                review.file_sha256(unreadable, "test")

    def test_evidence_t7_unknown_and_executable_targets_refuse(self) -> None:
        self.evidence_clean("D-ev7"); log = self.evidence_log()
        for criterion, needles in (("missing", ("runtime",)), ("unit", ("executable check_ids", "evidence-only check_ids"))):
            rc, _out, err = self.attach_evidence("D-ev7", log, criterion_id=criterion)
            self.assertNotEqual(rc, 0)
            for needle in needles: self.assertIn(needle, err)

    def test_evidence_t8_state_gate(self) -> None:
        self.open_review("D-ev8", dod=self.evidence_dod()); log = self.evidence_log(); head = "a" * 40
        for state in ("drafted_brief", "brief_reviewed", "dispatched"):
            record = self.record("D-ev8"); record["state"] = state; record["reviewed_head"] = head; self.write_record(record, "D-ev8")
            rc, _out, err = self.attach_evidence("D-ev8", log); self.assertNotEqual(rc, 0); self.assertIn("partial execution evidence", err)
        self.to_clean_with_work("D-ev8-bound", text="evidence state fixture\n",
                                dod=self.evidence_dod(), checks=("green",))
        for state in ("executed", "execution_reviewed", "review_clean"):
            record = self.record("D-ev8-bound"); record["state"] = state; self.write_record(record, "D-ev8-bound")
            rc, _out, err = self.attach_evidence("D-ev8-bound", log); self.assertEqual(rc, 0, err)

    def test_evidence_t9_reattach_preserves_detached_history(self) -> None:
        self.evidence_clean("D-ev9"); first = self.evidence_log("first\n"); second = self.evidence_log("second\n")
        self.attach_evidence("D-ev9", first); old = dict(self.record("D-ev9")["dod"][1]["evidence_payload"]); self.attach_evidence("D-ev9", second)
        events = [e for e in self.record("D-ev9")["history"] if e["event"] == "evidence"]
        self.assertEqual(len(events), 2); self.assertEqual(events[0]["payload"], old); self.assertNotEqual(events[1]["payload"], old)

    def test_evidence_t10_head_validation_precedes_record_io_and_wrong_head_refuses(self) -> None:
        log = self.evidence_log(); missing_lock = self.review_root / "NO-RECORD.lock"
        rc, _out, err = self.run_review_capture("evidence", "--dispatch-id", "NO-RECORD", "--criterion-id", "runtime", "--log", str(log), "--runtime-version", "v", "--counts", "c", "--head", "BAD")
        self.assertNotEqual(rc, 0); self.assertIn("--head", err); self.assertFalse(missing_lock.exists())
        self.evidence_clean("D-ev10"); wrong = self.git("rev-parse", "HEAD~1")
        rc, _out, err = self.attach_evidence("D-ev10", log, head=wrong); self.assertNotEqual(rc, 0); self.assertIn(wrong, err); self.assertIn(self.record("D-ev10")["reviewed_head"], err)

    def test_evidence_t11_executable_only_approves_without_payload(self) -> None:
        key = self.enable_approval_signing()
        dod = self.root / "dod-extra-only.json"
        dod.write_text(json.dumps([{"id": "sweep", "claim": "extra check", "check_id": "extra", "argv": ["true"]}]), encoding="utf-8")
        self.to_clean_with_work("D-ev11", dod=dod, checks=("extra",))
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review("approve", "--dispatch-id", "D-ev11", "--key", str(key))

    def test_evidence_t12_legacy_string_ignored_and_untouched(self) -> None:
        key = self.enable_approval_signing(); self.evidence_clean("D-ev12", legacy="human says green")
        rc, _out, err = self.run_review_capture("approve", "--dispatch-id", "D-ev12", "--key", str(key)); self.assertNotEqual(rc, 0)
        self.attach_evidence("D-ev12", self.evidence_log()); self.assertEqual(self.record("D-ev12")["dod"][1]["evidence"], "human says green")

    def test_evidence_t13_system_python_malformed_head(self) -> None:
        checkout = Path(__file__).resolve().parents[2]
        proc = subprocess.run([sys.executable, "-m", "agent_comms.review", "evidence", "--dispatch-id", "NO-EVIDENCE-T13", "--criterion-id", "x", "--log", str(self.evidence_log()), "--runtime-version", "v", "--counts", "c", "--head", "BAD"], cwd=checkout, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 1); self.assertIn("review: error:", proc.stderr); self.assertIn("--head", proc.stderr)
        self.assertFalse((checkout / "local" / "dispatch" / "reviews" / "NO-EVIDENCE-T13.lock").exists())

    def test_evidence_t14_gate_merge_reverifies_tamper_and_accepts_untampered(
        self,
    ) -> None:
        key = self.enable_approval_signing()
        self.evidence_clean("D-ev14")
        log = self.evidence_log()
        self.attach_evidence("D-ev14", log)
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review("approve", "--dispatch-id", "D-ev14", "--key", str(key))
        log.write_text("tamper\n", encoding="utf-8")
        rc, _out, err = self.run_review_capture("gate-merge", "--dispatch-id", "D-ev14")
        self.assertNotEqual(rc, 0)
        self.assertIn("sha256 mismatch", err)
        log.write_text("cert green\n", encoding="utf-8")
        self.run_review("gate-merge", "--dispatch-id", "D-ev14")
        self.assertEqual(self.record("D-ev14")["state"], "merge_eligible")

    def test_evidence_t15_open_unknown_enum_leaves_no_artifacts(self) -> None:
        dod = self.root / "bad-dod.json"; dod.write_text(json.dumps([{"id": "custom", "check_id": "custom-cert"}]), encoding="utf-8")
        rc, _out, err = self.run_review_capture("open", "--dispatch-id", "D-ev15", "--brief", str(self.brief), "--dod", str(dod), "--repo", str(self.repo))
        self.assertNotEqual(rc, 0)
        for needle in ("custom", "custom-cert", "executable check_ids", "evidence-only check_ids"): self.assertIn(needle, err)
        self.assertFalse((self.review_root / "D-ev15.json").exists()); self.assertFalse((self.review_root / "D-ev15.lock").exists())

    def test_evidence_t16_open_accepts_mixed_enums(self) -> None:
        self.open_review("D-ev16", dod=self.evidence_dod()); self.assertEqual([c["check_id"] for c in self.record("D-ev16")["dod"]], ["green", "runtime-cert"])

    def test_evidence_t17_stale_payload_head_refuses_approve_and_gate_merge(self) -> None:
        self.evidence_clean("D-ev17"); self.attach_evidence("D-ev17", self.evidence_log()); record = self.record("D-ev17"); old = record["reviewed_head"]; new = "f" * 40; record["reviewed_head"] = new; self.write_record(record, "D-ev17")
        rc, _out, err = self.run_review_capture("approve", "--dispatch-id", "D-ev17")
        self.assertNotEqual(rc, 0)
        self.assertIn(old, err)
        self.assertIn(new, err)
        record = self.record("D-ev17")
        record["state"] = "human_approved"
        record["approved_head"] = new
        self.write_record(record, "D-ev17")
        with (
            mock.patch.object(approval, "run_git", return_value=""),
            mock.patch.object(approval, "git_head", return_value=new),
            mock.patch.object(approval, "verify_cycle_merge_authorization"),
        ):
            rc, _out, err = self.run_review_capture(
                "gate-merge", "--dispatch-id", "D-ev17"
            )
        self.assertNotEqual(rc, 0)
        self.assertIn(old, err)
        self.assertIn(new, err)

    # ----- T1: mark-executed commit-first prevention -----

    def test_t1_mark_executed_dirty_matrix(self) -> None:
        self.to_dispatched("D-t1")

        with self.subTest(case="tracked_edit"):
            (self.repo / "tracked.txt").write_text("tracked edit\n", encoding="utf-8")
            try:
                self.refuse_mark_executed("D-t1", "dirty")
            finally:
                self.git("checkout", "--", "tracked.txt")

        with self.subTest(case="staged_edit"):
            (self.repo / "tracked.txt").write_text("staged edit\n", encoding="utf-8")
            self.git("add", "tracked.txt")
            try:
                self.refuse_mark_executed("D-t1", "dirty")
            finally:
                self.git("reset", "--", "tracked.txt")
                self.git("checkout", "--", "tracked.txt")

        with self.subTest(case="untracked_file"):
            (self.repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            try:
                self.refuse_mark_executed("D-t1", "dirty")
            finally:
                (self.repo / "untracked.txt").unlink()

        with self.subTest(case="clean_committed_head_binds"):
            worked = self.work_commit("committed work\n")
            self.run_review("mark-executed", "--dispatch-id", "D-t1")
            state = self.record("D-t1")
            self.assertEqual(state["state"], "executed")
            self.assertEqual(state["reviewed_head"], worked)
            self.assertTrue(state["trigger_closed"])

    # ----- T2: mark-executed topology refusals -----

    def test_t2_mark_executed_topology_refusals(self) -> None:
        self.to_dispatched("D-t2")
        pristine = self.record("D-t2")

        with self.subTest(case="missing_repo"):
            state = dict(pristine, repo=str(self.root / "no-such-repo"))
            self.write_record(state, "D-t2")
            self.refuse_mark_executed("D-t2", "not a git checkout")

        with self.subTest(case="non_git_repo"):
            plain = self.root / "plain-dir"
            plain.mkdir()
            state = dict(pristine, repo=str(plain))
            self.write_record(state, "D-t2")
            self.refuse_mark_executed("D-t2", "not a git checkout")

        with self.subTest(case="missing_recorded_target_branch"):
            state = dict(pristine, target_branch=None)
            self.write_record(state, "D-t2")
            self.refuse_mark_executed("D-t2", "not a named branch")

        with self.subTest(case="unresolvable_head"):
            unborn = self.root / "unborn-repo"
            self.init_repo(unborn)
            self.git("checkout", "-b", "work-branch", cwd=unborn)
            state = dict(pristine, repo=str(unborn))
            self.write_record(state, "D-t2")
            self.refuse_mark_executed("D-t2", "unresolvable")

        self.write_record(pristine, "D-t2")

        with self.subTest(case="detached_head"):
            self.git("checkout", "--detach", "HEAD")
            try:
                self.refuse_mark_executed("D-t2", "recorded source branch")
            finally:
                self.git("checkout", "work-branch")

        with self.subTest(case="wrong_branch"):
            self.git("checkout", "-b", "other-branch")
            try:
                self.refuse_mark_executed("D-t2", "recorded source branch")
            finally:
                self.git("checkout", "work-branch")
                self.git("branch", "-D", "other-branch")

        with self.subTest(case="reviewed_head_option_removed"):
            before = self.record_bytes("D-t2")
            head = self.git("rev-parse", "HEAD")
            rc, _stdout, stderr = self.run_review_capture(
                "mark-executed", "--dispatch-id", "D-t2", "--reviewed-head", head
            )
            self.assertNotEqual(rc, 0)
            self.assertIn("unrecognized arguments", stderr)
            self.assertEqual(self.record_bytes("D-t2"), before)

    # ----- T3: happy rebind from review_clean -----

    def test_t3_rebind_happy_from_review_clean(self) -> None:
        worked = self.to_clean_with_work("D-t3")
        before = self.record("D-t3")
        self.assertEqual(before["state"], "review_clean")
        old_base = before["base_commit"]
        new_base, new_head = self.advance_and_rebase()
        self.assertNotEqual(new_head, worked)

        rc, stdout, stderr = self.run_review_capture("rebind", "--dispatch-id", "D-t3")
        self.assertEqual(rc, 0, stderr)
        line = stdout.strip()
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn(worked, line)
        self.assertIn(new_head, line)
        self.assertIn("gate epoch 1", line)

        state = self.record("D-t3")
        self.assertEqual(state["state"], "executed")
        self.assertEqual(state["base_commit"], new_base)
        self.assertEqual(state["reviewed_head"], new_head)
        self.assertEqual(state["gate_epoch"], 1)
        self.assertIsNone(state["approval"])
        self.assertIsNone(state["approved_head"])
        # Frozen bindings survive rebind untouched.
        for field in ("dispatch_id", "brief_path", "brief_sha256", "dod", "findings", "respawn_count", "target_branch", "trigger_closed"):
            self.assertEqual(state[field], before[field], field)
        # Prior gate runs and history are preserved append-only.
        self.assertEqual(state["gate_runs"][: len(before["gate_runs"])], before["gate_runs"])
        self.assertEqual(state["history"][: len(before["history"])], before["history"])
        events = [event for event in state["history"] if event["event"] == "rebind"]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["old_base"], old_base)
        self.assertEqual(event["new_base"], new_base)
        self.assertEqual(event["old_reviewed_head"], worked)
        self.assertEqual(event["new_reviewed_head"], new_head)
        self.assertEqual(event["prior_state"], "review_clean")
        self.assertEqual(event["old_gate_epoch"], 0)
        self.assertEqual(event["new_gate_epoch"], 1)
        self.assertRegex(event["canonical_diff_digest"], r"^[0-9a-f]{64}$")
        self.assertTrue(event["stable_patch_id"])
        self.assertNotEqual(event["stable_patch_id"], "unavailable")

    # ----- T4: rebind recovery from approved/eligible with superseded approval -----

    def test_t4_rebind_from_approved_and_eligible_preserves_superseded_approval(self) -> None:
        key_path = self.enable_approval_signing()
        for index, target_state in enumerate(["human_approved", "merge_eligible"]):
            with self.subTest(state=target_state):
                dispatch_id = f"D-t4-{target_state}"
                self.to_clean_with_work(dispatch_id, text=f"work {target_state}\n")
                with mock.patch.object(
                    approval, "read_tty_confirmation", return_value="APPROVE"
                ):
                    self.run_review(
                        "approve",
                        "--dispatch-id",
                        dispatch_id,
                        "--approver",
                        "human",
                        "--key",
                        str(key_path),
                    )
                if target_state == "merge_eligible":
                    self.run_review("gate-merge", "--dispatch-id", dispatch_id)
                before = self.record(dispatch_id)
                self.assertEqual(before["state"], target_state)
                prior_approval = before["approval"]
                prior_head = before["approved_head"]
                new_base, new_head = self.advance_and_rebase(f"advance-t4-{index}.txt")

                self.run_review("rebind", "--dispatch-id", dispatch_id)
                state = self.record(dispatch_id)
                self.assertEqual(state["state"], "executed")
                self.assertEqual(state["base_commit"], new_base)
                self.assertEqual(state["reviewed_head"], new_head)
                self.assertEqual(state["gate_epoch"], 1)
                self.assertIsNone(state["approval"])
                self.assertIsNone(state["approved_head"])
                superseded = state["superseded_approvals"]
                self.assertEqual(len(superseded), 1)
                self.assertEqual(superseded[0]["approval"], prior_approval)
                self.assertEqual(superseded[0]["approved_head"], prior_head)
                self.assertEqual(superseded[0]["prior_state"], target_state)
                self.assertIn("BEGIN SSH SIGNATURE", superseded[0]["approval"]["signature"])
                event = next(e for e in state["history"] if e["event"] == "rebind")
                self.assertEqual(event["prior_state"], target_state)

    # ----- T5: rebind state refusals -----

    def test_t5_rebind_state_refusals(self) -> None:
        self.to_clean_with_work("D-t5")
        pristine = self.record("D-t5")
        refused_states = [
            "drafted_brief",
            "brief_revised",
            "brief_reviewed",
            "dispatched",
            "executed",
            "execution_reviewed",
            "escalated",
            "merged",
            "verified",
        ]
        for target_state in refused_states:
            with self.subTest(state=target_state):
                state = dict(pristine, state=target_state)
                self.write_record(state, "D-t5")
                self.refuse_rebind("D-t5", f"state {target_state} not allowed")

    # ----- T6: rebind ancestry attacks -----

    def test_t6_rebind_ancestry_attacks(self) -> None:
        with self.subTest(case="unrelated_integration"):
            self.to_clean_with_work("D-t6a", text="t6a work\n")
            self.advance_and_rebase("advance-t6a.txt")
            unrelated = self.root / "t6-unrelated"
            self.init_repo(unrelated)
            self.git("checkout", "-B", "main", cwd=unrelated)
            (unrelated / "file.txt").write_text("x\n", encoding="utf-8")
            self.git("add", "file.txt", cwd=unrelated)
            self.git("commit", "-m", "unrelated", cwd=unrelated)
            with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(unrelated)}):
                self.refuse_rebind("D-t6a", "work-branch")

        with self.subTest(case="old_base_beyond_integration_is_refused"):
            # No align: the recorded base sits on work-branch beyond integration
            # HEAD, so blessing it would launder an unseen base. Under contract
            # 17 this base-ahead case is unreachable at the old ancestry check --
            # the earlier reviewed-change identity guard fires first, because a
            # base beyond integration cannot reproduce the reviewed diff. It is
            # still refused (production is not weakened); only the exact contract
            # -17 boundary that catches it moved.
            self.to_dispatched("D-t6b")
            self.work_commit("t6b work\n")
            self.run_review("mark-executed", "--dispatch-id", "D-t6b")
            self.run_review("gates", "--dispatch-id", "D-t6b", "--check", "green")
            self.run_review("clean", "--dispatch-id", "D-t6b")
            self.work_commit("t6b new head\n")
            self.refuse_rebind("D-t6b", "the rebased change is not the reviewed change")

        with self.subTest(case="new_head_not_based_on_integration"):
            self.to_clean_with_work("D-t6c", text="t6c work\n")
            self.integration_commit("advance-t6c.txt")
            self.work_commit("t6c unrebased head\n")
            self.refuse_rebind("D-t6c", "not an ancestor of the new reviewed head")
            # Recover the branch for later subtests.
            self.git("rebase", "integration-main")

        with self.subTest(case="missing_old_objects_fail_closed"):
            self.to_clean_with_work("D-t6d", text="t6d work\n")
            self.advance_and_rebase("advance-t6d.txt")
            state = self.record("D-t6d")
            for field in ("base_commit", "reviewed_head"):
                forged = dict(state)
                forged[field] = "0" * 40
                self.write_record(forged, "D-t6d")
                self.refuse_rebind("D-t6d", "not an object in the review")
            self.write_record(state, "D-t6d")

        with self.subTest(case="malformed_integration_head"):
            empty = self.root / "t6-empty-main"
            self.init_repo(empty)
            with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(empty)}):
                self.refuse_rebind("D-t6d", "unresolvable")

        with self.subTest(case="same_head_no_op"):
            self.to_clean_with_work("D-t6f", text="t6f work\n")
            self.refuse_rebind("D-t6f", "already equals")

    # ----- T7: rebind identity attacks -----

    def tamper_after_rebase(self, dispatch_id: str, filename: str, tamper) -> None:
        self.to_clean_with_work(dispatch_id, text=f"{dispatch_id} work\n")
        self.integration_commit(filename)
        self.git("rebase", "integration-main")
        tamper()
        self.git("commit", "--amend", "--no-edit", "-a")

    def test_t7_rebind_identity_attacks(self) -> None:
        with self.subTest(case="content_change"):
            self.tamper_after_rebase(
                "D-t7a", "advance-t7a.txt",
                lambda: (self.repo / "tracked.txt").write_text("tampered content\n", encoding="utf-8"),
            )
            self.refuse_rebind("D-t7a", "digest")

        with self.subTest(case="whitespace_change"):
            self.tamper_after_rebase(
                "D-t7b", "advance-t7b.txt",
                lambda: (self.repo / "tracked.txt").write_text("D-t7b work \n", encoding="utf-8"),
            )
            self.refuse_rebind("D-t7b", "digest")

        with self.subTest(case="path_and_rename_change"):
            def rename() -> None:
                self.git("mv", "tracked.txt", "renamed.txt")

            self.tamper_after_rebase("D-t7c", "advance-t7c.txt", rename)
            self.refuse_rebind("D-t7c", "digest")
            self.git("mv", "renamed.txt", "tracked.txt")
            self.git("commit", "--amend", "--no-edit")

        with self.subTest(case="mode_change"):
            self.tamper_after_rebase(
                "D-t7d", "advance-t7d.txt",
                lambda: (self.repo / "tracked.txt").chmod(0o755),
            )
            self.refuse_rebind("D-t7d", "digest")
            (self.repo / "tracked.txt").chmod(0o644)
            self.git("commit", "--amend", "--no-edit", "-a")

        with self.subTest(case="binary_change"):
            self.align_integration()
            self.to_dispatched("D-t7e")
            (self.repo / "blob.bin").write_bytes(b"\x00\x01\x02")
            self.git("add", "blob.bin")
            self.git("commit", "-m", "binary work")
            self.run_review("mark-executed", "--dispatch-id", "D-t7e")
            self.run_review("gates", "--dispatch-id", "D-t7e", "--check", "green")
            self.run_review("clean", "--dispatch-id", "D-t7e")
            self.integration_commit("advance-t7e.txt")
            self.git("rebase", "integration-main")
            (self.repo / "blob.bin").write_bytes(b"\x00\x01\x03")
            self.git("add", "blob.bin")
            self.git("commit", "--amend", "--no-edit")
            self.refuse_rebind("D-t7e", "digest")

        with self.subTest(case="empty_old_diff"):
            self.align_integration()
            self.to_dispatched("D-t7f")
            self.run_review("mark-executed", "--dispatch-id", "D-t7f")
            self.run_review("gates", "--dispatch-id", "D-t7f", "--check", "green")
            self.run_review("clean", "--dispatch-id", "D-t7f")
            empty = self.record("D-t7f")
            empty["reviewed_head"] = empty["base_commit"]
            self.write_record(empty, "D-t7f")
            self.work_commit("t7f late work\n")
            self.refuse_rebind("D-t7f", "is empty")

    def test_t7_rebind_asymmetric_patch_identity_refuses(self) -> None:
        # Exactly one available stable patch identity must refuse, not
        # silently fall back to "unavailable" and discard the known side.
        self.to_clean_with_work("D-t7g", text="t7g work\n")
        self.advance_and_rebase("advance-t7g.txt")
        valid_id = "ab" * 20
        with mock.patch.object(
            rebind, "stable_patch_id", side_effect=[valid_id, None]
        ) as patched:
            stderr = self.refuse_rebind("D-t7g", "asymmetric")
        self.assertEqual(patched.call_count, 2)
        self.assertIn(f"old {valid_id}", stderr)
        self.assertIn("new unavailable", stderr)

    def test_t7_rebind_empty_new_diff_refuses(self) -> None:
        # Integration absorbs the reviewed work, then the source branch
        # gains only an allow-empty commit atop that tip: the new net
        # change is empty and rebind must refuse.
        self.to_clean_with_work("D-t7h", text="t7h work\n")
        self.align_integration()
        self.git("commit", "--allow-empty", "-m", "empty atop integration")
        self.refuse_rebind("D-t7h", "nothing to rebind")

    def digest_probe_commit(self) -> tuple[str, str]:
        """A probe change whose pinned presentation is sensitive to every
        hostile knob below: blank context lines (suppressBlankEmpty), a
        non-ASCII path (quotePath), and enough surrounding text for context
        and heuristic knobs to have somewhere to bite."""
        parent = self.git("rev-parse", "HEAD")
        (self.repo / "probe.txt").write_text(
            "alpha\n\nbeta\ngamma\ndelta\n\nepsilon\nzeta\n", encoding="utf-8")
        (self.repo / "próbe-ü.txt").write_text("unicode path\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "digest probe base")
        base = self.git("rev-parse", "HEAD")
        (self.repo / "probe.txt").write_text(
            "alpha\n\nbeta\ngamma\ndelta edited\n\nepsilon\nzeta\n", encoding="utf-8")
        (self.repo / "próbe-ü.txt").write_text("unicode path edited\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "digest probe edit")
        head = self.git("rev-parse", "HEAD")
        self.addCleanup(self.git, "reset", "--hard", parent)
        return base, head

    def test_t7_digest_deterministic_and_driverless(self) -> None:
        base, head = self.digest_probe_commit()
        first = review.canonical_diff_digest("test", self.repo, base, head)
        second = review.canonical_diff_digest("test", self.repo, base, head)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        # An empty diff digests to something distinct from a real change.
        self.assertNotEqual(first, review.canonical_diff_digest("test", self.repo, head, head))
        clean_patch = review.pinned_patch_bytes("test", self.repo, base, head)
        clean_id = review.stable_patch_id("test", self.repo, clean_patch)
        self.assertTrue(clean_id)

        # Hostile repository configuration, attribute-driven diff driver and
        # textconv sentinels, and hostile ambient environment must all lose
        # to the pinned command line: identical digest and stable patch ID,
        # and no sentinel side effect ever fires.
        driver_sentinel = self.root / "driver-ran.sentinel"
        textconv_sentinel = self.root / "textconv-ran.sentinel"
        external_sentinel = self.root / "external-ran.sentinel"
        external_script = self.root / "external-diff.sh"
        external_script.write_text(f"#!/bin/sh\ntouch {external_sentinel}\n", encoding="utf-8")
        external_script.chmod(0o755)
        order_file = self.root / "hostile.order"
        order_file.write_text("*.txt\n", encoding="utf-8")
        (self.repo / ".gitattributes").write_text("*.txt diff=evil filter=evil\n", encoding="utf-8")
        hostile_config = (
            ("diff.evil.command", f"touch {driver_sentinel} #"),
            ("diff.evil.textconv", f"touch {textconv_sentinel} #"),
            ("filter.evil.clean", f"touch {textconv_sentinel} #"),
            ("diff.algorithm", "patience"),
            ("diff.indentHeuristic", "true"),
            ("diff.orderFile", str(order_file)),
            ("diff.suppressBlankEmpty", "true"),
            ("diff.mnemonicPrefix", "true"),
            ("diff.noprefix", "true"),
            ("core.bigFileThreshold", "1"),
            ("core.quotePath", "false"),
            ("core.abbrev", "4"),
            ("core.pager", f"touch {driver_sentinel} #"),
            ("patchid.verbatim", "true"),
            ("patchid.stable", "false"),
        )
        hostile_env = {
            "GIT_EXTERNAL_DIFF": str(external_script),
            "GIT_CONFIG_PARAMETERS": "'diff.algorithm=histogram' 'core.quotePath=false'",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.bigFileThreshold",
            "GIT_CONFIG_VALUE_0": "1",
        }
        for key, value in hostile_config:
            self.git("config", key, value)
        try:
            for diff_opts in ("-u0", "-u1"):
                with self.subTest(git_diff_opts=diff_opts):
                    with mock.patch.dict(os.environ, {**hostile_env, "GIT_DIFF_OPTS": diff_opts}):
                        hostile_patch = review.pinned_patch_bytes("test", self.repo, base, head)
                        hostile_digest = review.canonical_diff_digest("test", self.repo, base, head)
                        hostile_id = review.stable_patch_id("test", self.repo, hostile_patch)
                    self.assertEqual(hostile_patch, clean_patch)
                    self.assertEqual(hostile_digest, first)
                    self.assertEqual(hostile_id, clean_id)
        finally:
            (self.repo / ".gitattributes").unlink()
            for key, _value in hostile_config:
                self.git("config", "--unset", key)
        for sentinel in (driver_sentinel, textconv_sentinel, external_sentinel):
            self.assertFalse(sentinel.exists(), sentinel.name)

    # ----- review-rebind-canonical-digest-001: offset-neutral identity -----

    def git_bytes_out(self, repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
        proc = subprocess.run(
            ["git", *args], cwd=repo, input=stdin,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        return proc.stdout

    def legacy_raw_digest(self, repo: Path, base: str, head: str) -> str:
        return hashlib.sha256(self.git_bytes_out(
            repo, "diff-tree", "-r", "-z", "--no-renames", "--full-index", base, head
        )).hexdigest()

    def fixture_stable_patch_id(self, repo: Path, base: str, head: str) -> str:
        patch = self.git_bytes_out(repo, "diff-tree", "-r", "-p", "--no-renames", "--full-index", base, head)
        return self.git_bytes_out(repo, "patch-id", "--stable", stdin=patch).split()[0].decode("ascii")

    def clean_to_shared_file_edit(self, dispatch_id: str) -> None:
        """Governed flow to review_clean where the reviewed change edits the
        bottom of a 12-line shared file that integration also owns."""
        self.align_integration()
        shared = "".join(f"line {i}\n" for i in range(1, 13))
        (self.integration / "shared.txt").write_text(shared, encoding="utf-8")
        self.git("add", "shared.txt", cwd=self.integration)
        self.git("commit", "-m", "shared baseline", cwd=self.integration)
        self.git("rebase", "integration-main")
        self.to_dispatched(dispatch_id)
        lines = (self.repo / "shared.txt").read_text(encoding="utf-8").splitlines(keepends=True)
        lines[-1] = "line 12 reviewed\n"
        (self.repo / "shared.txt").write_text("".join(lines), encoding="utf-8")
        self.git("add", "shared.txt")
        self.git("commit", "-m", "reviewed bottom edit")
        self.run_review("mark-executed", "--dispatch-id", dispatch_id)
        self.run_review("gates", "--dispatch-id", dispatch_id, "--check", "green")
        self.run_review("clean", "--dispatch-id", dispatch_id)

    def upstream_same_file_top_insert(self) -> tuple[str, str]:
        """Integration inserts content earlier in the shared file, then the
        source branch cleanly rebases onto that exact tip."""
        lines = (self.integration / "shared.txt").read_text(encoding="utf-8").splitlines(keepends=True)
        (self.integration / "shared.txt").write_text("".join(["inserted upstream\n", *lines]), encoding="utf-8")
        self.git("add", "shared.txt", cwd=self.integration)
        self.git("commit", "-m", "upstream earlier edit in shared.txt", cwd=self.integration)
        new_base = self.git("rev-parse", "HEAD", cwd=self.integration)
        self.git("rebase", "integration-main")
        return new_base, self.git("rev-parse", "HEAD")

    def test_rebind_accepts_same_file_earlier_upstream_edit(self) -> None:
        """agent-comms-4qz: after a clean rebase over an earlier upstream edit
        in the same reviewed file, the stable patch ID is preserved while both
        blob object IDs change; rebind must accept the unchanged patch."""
        self.clean_to_shared_file_edit("D-4qz")
        before = self.record("D-4qz")
        old_base, old_head = before["base_commit"], before["reviewed_head"]
        new_base, new_head = self.upstream_same_file_top_insert()

        # Measured premise: this is the shared-file case, not a generic
        # rebase. The stable patch identity survives, but both blob IDs in
        # the raw tree stream changed, so the legacy raw-tree digest differs.
        self.assertEqual(
            self.fixture_stable_patch_id(self.repo, old_base, old_head),
            self.fixture_stable_patch_id(self.integration, new_base, new_head),
        )
        self.assertNotEqual(
            self.legacy_raw_digest(self.repo, old_base, old_head),
            self.legacy_raw_digest(self.integration, new_base, new_head),
        )

        rc, stdout, stderr = self.run_review_capture("rebind", "--dispatch-id", "D-4qz")
        self.assertEqual(rc, 0, stderr)
        self.assertIn(new_head, stdout)
        state = self.record("D-4qz")
        self.assertEqual(state["state"], "executed")
        self.assertEqual(state["base_commit"], new_base)
        self.assertEqual(state["reviewed_head"], new_head)

    @staticmethod
    def synthetic_patch(*, old_oid: str = "a" * 40, new_oid: str = "b" * 40,
                        old_start: int = 10, new_start: int = 10,
                        path: str = "f.txt", mode: str = " 100644",
                        old_count: str = ",7", new_count: str = ",7",
                        deleted: str = "old line", added: str = "new line",
                        context_tail: str = " ctx6\n",
                        suffix: str = " def anchor():") -> bytes:
        return (
            f"diff --git a/{path} b/{path}\n"
            f"index {old_oid}..{new_oid}{mode}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -{old_start}{old_count} +{new_start}{new_count} @@{suffix}\n"
            " ctx1\n ctx2\n ctx3\n"
            f"-{deleted}\n"
            f"+{added}\n"
            " ctx4\n ctx5\n"
        ).encode("ascii") + context_tail.encode("ascii")

    @staticmethod
    def canonical_digest_of(patch: bytes) -> str:
        return hashlib.sha256(review.canonicalize_patch_bytes("test", patch)).hexdigest()

    def test_canonicalizer_is_offset_and_blob_neutral_but_content_bound(self) -> None:
        base = self.synthetic_patch()
        moved = self.synthetic_patch(old_oid="c" * 40, new_oid="d" * 40,
                                     old_start=42, new_start=43, suffix="")
        self.assertEqual(self.canonical_digest_of(base), self.canonical_digest_of(moved))
        canon = review.canonicalize_patch_bytes("test", base)
        self.assertIn(b"index !oid!..!oid! 100644\n", canon)
        self.assertIn(b"@@ -7 +7 @@\n", canon)
        # An omitted count means exactly 1 in both spellings.
        self.assertEqual(
            self.canonical_digest_of(self.synthetic_patch(old_count="", new_count="")),
            self.canonical_digest_of(self.synthetic_patch(old_count=",1", new_count=",1")),
        )
        for variant in (
            dict(old_count=",8"),
            dict(new_count=""),
            dict(path="g.txt"),
            dict(mode=" 100755"),
            dict(mode=""),
            dict(context_tail=" ctx6X\n"),
            dict(deleted="old lin_"),
            dict(added="new lin_"),
        ):
            with self.subTest(variant=variant):
                self.assertNotEqual(
                    self.canonical_digest_of(base),
                    self.canonical_digest_of(self.synthetic_patch(**variant)),
                )

    def test_canonicalizer_refuses_malformed_candidate_metadata(self) -> None:
        for bad in (
            b"index deadbeef\n",
            b"index gg..hh 100644\n",
            b"index " + b"a" * 40 + b".." + b"b" * 40 + b"  100644\n",
            b"index " + b"a" * 40 + b".." + b"b" * 40 + b" 999999\n",
            b"@@ -banana +1 @@\n",
            b"@@ -1 +1@@\n",
            b"@@  -1 +1 @@\n",
            # 0x0d is not a line boundary: a CRLF candidate fails the LF
            # grammar and refuses instead of matching without the CR.
            b"index " + b"a" * 40 + b".." + b"b" * 40 + b"\r\n",
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(review.ReviewError, "patch stream"):
                review.canonicalize_patch_bytes("test", b"header\n" + bad)
        for tail in (b"index " + b"a" * 40 + b".." + b"b" * 40, b"@@ -1 +1 @@"):
            with self.subTest(tail=tail), self.assertRaisesRegex(review.ReviewError, "unterminated"):
                review.canonicalize_patch_bytes("test", b"header\n" + tail)

    def test_canonicalizer_never_reclassifies_cr_embedded_body_bytes(self) -> None:
        # Reviewed added/deleted bytes containing a carriage return followed
        # by index-like bytes stay part of their physical line: bound
        # verbatim, never treated as metadata.
        for prefix in (b"+", b"-"):
            with self.subTest(prefix=prefix):
                cr_line = prefix + b"payload\rindex " + b"e" * 40 + b".." + b"f" * 40 + b" 100644\n"
                template = self.synthetic_patch(added="marker") if prefix == b"+" else self.synthetic_patch(deleted="marker")
                patch = template.replace(prefix + b"marker\n", cr_line)
                canon = review.canonicalize_patch_bytes("test", patch)
                self.assertIn(cr_line, canon)
                self.assertNotIn(b"payload\rindex !oid!", canon)
                tampered = patch.replace(b"e" * 40, b"9" * 40)
                self.assertNotEqual(self.canonical_digest_of(patch), self.canonical_digest_of(tampered))

    def test_canonical_digest_binds_binary_preimage_after_oid_neutralization(self) -> None:
        # Two binary ranges with identical new bytes but different old bytes
        # must keep different digests: after index OIDs are neutralized, the
        # reverse binary-patch payload still binds the pre-image.
        scratch = self.root / "binary-scratch"
        self.init_repo(scratch)
        self.git("checkout", "-B", "main", cwd=scratch)
        blob = scratch / "blob.bin"
        new_bytes = b"\x00new payload\x01"
        ranges = []
        for tag, old_bytes in (("A", b"\x00old payload A\x02"), ("B", b"\x00old payload B\x03")):
            blob.write_bytes(old_bytes)
            self.git("add", "blob.bin", cwd=scratch)
            self.git("commit", "-m", f"old {tag}", cwd=scratch)
            base = self.git("rev-parse", "HEAD", cwd=scratch)
            blob.write_bytes(new_bytes)
            self.git("add", "blob.bin", cwd=scratch)
            self.git("commit", "-m", f"new {tag}", cwd=scratch)
            ranges.append((base, self.git("rev-parse", "HEAD", cwd=scratch)))
        patches = [review.pinned_patch_bytes("test", scratch, base, head) for base, head in ranges]
        for patch in patches:
            self.assertIn(b"GIT binary patch", patch)
        self.assertNotEqual(
            hashlib.sha256(review.canonicalize_patch_bytes("test", patches[0])).hexdigest(),
            hashlib.sha256(review.canonicalize_patch_bytes("test", patches[1])).hexdigest(),
        )

    def test_rebind_under_hostile_config_records_clean_environment_digest(self) -> None:
        self.clean_to_shared_file_edit("D-hostile")
        before = self.record("D-hostile")
        clean_digest = review.canonical_diff_digest(
            "test", self.repo, before["base_commit"], before["reviewed_head"])
        clean_id = review.stable_patch_id(
            "test", self.repo,
            review.pinned_patch_bytes("test", self.repo, before["base_commit"], before["reviewed_head"]))
        self.upstream_same_file_top_insert()
        # Hostile knobs in the shared repository config reach both the review
        # worktree and the integration checkout; hostile ambient environment
        # reaches every child. The successful rebind must still record the
        # clean-environment digest and stable patch identity.
        hostile = (
            ("diff.algorithm", "patience"),
            ("diff.indentHeuristic", "true"),
            ("diff.suppressBlankEmpty", "true"),
            ("diff.mnemonicPrefix", "true"),
            ("diff.noprefix", "true"),
            ("core.bigFileThreshold", "1"),
            ("core.quotePath", "false"),
            ("core.abbrev", "4"),
            ("patchid.verbatim", "true"),
            ("patchid.stable", "false"),
        )
        for key, value in hostile:
            self.git("config", key, value)
        try:
            with mock.patch.dict(os.environ, {"GIT_DIFF_OPTS": "-u0"}):
                self.run_review("rebind", "--dispatch-id", "D-hostile")
        finally:
            for key, _value in hostile:
                self.git("config", "--unset", key)
        event = next(e for e in self.record("D-hostile")["history"] if e["event"] == "rebind")
        self.assertEqual(event["canonical_diff_digest"], clean_digest)
        self.assertEqual(event["stable_patch_id"], clean_id)
        self.assertEqual(event["canonical_diff_algorithm"], "git-patch-offset-neutral-v1")

    def test_rebind_succeeds_with_one_sided_worktree_local_hostile_knobs(self) -> None:
        # A repository-local knob applied to only one side (per-worktree
        # config) must not desynchronize the two pinned patch streams.
        self.git("config", "extensions.worktreeConfig", "true", cwd=self.integration)
        for dispatch_id, side in (("D-wt-repo", self.repo), ("D-wt-int", self.integration)):
            with self.subTest(side=str(side.name)):
                self.clean_to_shared_file_edit(dispatch_id)
                self.upstream_same_file_top_insert()
                knobs = (("core.bigFileThreshold", "1"), ("diff.algorithm", "patience"), ("diff.noprefix", "true"))
                for key, value in knobs:
                    self.git("config", "--worktree", key, value, cwd=side)
                try:
                    self.run_review("rebind", "--dispatch-id", dispatch_id)
                finally:
                    for key, _value in knobs:
                        self.git("config", "--worktree", "--unset", key, cwd=side)
                self.assertEqual(self.record(dispatch_id)["state"], "executed")

    def test_rebind_refuses_one_sided_info_attributes_drift_atomically(self) -> None:
        # Attribute sources are intentionally live: representation drift on
        # one side is a conservative refusal whose remedy is a new governed
        # review, never acceptance of changed content.
        self.clean_to_shared_file_edit("D-attr")
        self.upstream_same_file_top_insert()
        clone = self.root / "integration-attr"
        self.git("clone", str(self.integration), str(clone), cwd=self.root)
        self.git("branch", "work-branch", "origin/work-branch", cwd=clone)
        self.assertEqual(
            self.git("rev-parse", "HEAD", cwd=clone),
            self.git("rev-parse", "HEAD", cwd=self.integration),
        )
        attrs = clone / ".git" / "info" / "attributes"
        attrs.parent.mkdir(exist_ok=True)
        attrs.write_text("shared.txt -diff\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGENT_COMMS_MAIN": str(clone)}):
            stderr = self.refuse_rebind("D-attr", "canonical diff digests differ")
        self.assertIn("run a new governed review", stderr)

    def test_rebind_history_versions_algorithm_and_legacy_events_stay_valid(self) -> None:
        self.to_clean_with_work("D-alg", text="alg work\n")
        before = self.record("D-alg")
        clean_digest = review.canonical_diff_digest(
            "test", self.repo, before["base_commit"], before["reviewed_head"])
        self.advance_and_rebase("advance-alg.txt")
        self.run_review("rebind", "--dispatch-id", "D-alg")
        state = self.record("D-alg")
        event = next(e for e in state["history"] if e["event"] == "rebind")
        self.assertRegex(event["canonical_diff_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(event["canonical_diff_digest"], clean_digest)
        self.assertEqual(event["canonical_diff_algorithm"], "git-patch-offset-neutral-v1")

        # Strip the member to model a pre-versioning legacy event: the record
        # stays valid, and the next rebind versions only its own new event
        # while the legacy event stays append-only and untouched.
        index = state["history"].index(event)
        del state["history"][index]["canonical_diff_algorithm"]
        self.write_record(state, "D-alg")
        review.validate_record(self.record("D-alg"))
        legacy_event = copy.deepcopy(self.record("D-alg")["history"][index])
        self.run_review("gates", "--dispatch-id", "D-alg", "--check", "green")
        self.run_review("clean", "--dispatch-id", "D-alg")
        self.advance_and_rebase("advance-alg-2.txt")
        self.run_review("rebind", "--dispatch-id", "D-alg")
        events = [e for e in self.record("D-alg")["history"] if e["event"] == "rebind"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0], legacy_event)
        self.assertNotIn("canonical_diff_algorithm", events[0])
        self.assertEqual(events[1]["canonical_diff_algorithm"], "git-patch-offset-neutral-v1")

    def test_rebind_patch_id_mismatch_leaves_record_byte_identical(self) -> None:
        self.to_clean_with_work("D-pid", text="pid work\n")
        self.advance_and_rebase("advance-pid.txt")
        with mock.patch.object(
            rebind, "stable_patch_id", side_effect=["a" * 40, "b" * 40]
        ):
            self.refuse_rebind("D-pid", "stable patch identities differ")

    # ----- T8: rebind dirty/ref matrix -----

    def test_t8_rebind_dirty_and_ref_matrix(self) -> None:
        self.to_clean_with_work("D-t8", text="t8 work\n")
        self.advance_and_rebase("advance-t8.txt")
        repo_head = self.git("rev-parse", "HEAD")
        integration_head = self.git("rev-parse", "HEAD", cwd=self.integration)

        with self.subTest(case="dirty_source"):
            (self.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            try:
                self.refuse_rebind("D-t8", "dirty")
            finally:
                (self.repo / "untracked.txt").unlink()

        with self.subTest(case="dirty_integration"):
            (self.integration / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            try:
                self.refuse_rebind("D-t8", "dirty")
            finally:
                (self.integration / "untracked.txt").unlink()

        with self.subTest(case="wrong_current_source_branch"):
            self.git("checkout", "--detach", "HEAD")
            try:
                self.refuse_rebind("D-t8", "recorded source branch")
            finally:
                self.git("checkout", "work-branch")

        with self.subTest(case="recorded_source_tip_mismatch"):
            foreign = self.root / "t8-foreign"
            self.init_repo(foreign)
            self.git("checkout", "-B", "work-branch", cwd=foreign)
            (foreign / "file.txt").write_text("foreign\n", encoding="utf-8")
            self.git("add", "file.txt", cwd=foreign)
            self.git("commit", "-m", "foreign", cwd=foreign)
            state = self.record("D-t8")
            forged = dict(state, repo=str(foreign))
            self.write_record(forged, "D-t8")
            try:
                self.refuse_rebind("D-t8", "does not equal")
            finally:
                self.write_record(state, "D-t8")

        # No Git mutation anywhere in the matrix.
        self.assertEqual(self.git("rev-parse", "HEAD"), repo_head)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "work-branch")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.integration), integration_head)

    # ----- T9: gate freshness across epochs -----

    def test_t9_gate_epoch_freshness(self) -> None:
        dod = self.two_check_dod()
        self.to_clean_with_work("D-t9", text="t9 work\n", dod=dod, checks=("green", "extra"))
        self.advance_and_rebase("advance-t9.txt")
        self.run_review("rebind", "--dispatch-id", "D-t9")
        state = self.record("D-t9")
        self.assertEqual(state["gate_epoch"], 1)
        self.assertEqual({run["check_id"] for run in state["gate_runs"] if run["epoch"] == 0}, {"green", "extra"})

        # A partial current-epoch pass cannot borrow the old epoch's green.
        self.run_review("gates", "--dispatch-id", "D-t9", "--check", "green")
        self.run_review("clean", "--dispatch-id", "D-t9", ok=False)
        state = self.record("D-t9")
        self.assertEqual(state["state"], "execution_reviewed")
        old_runs = [run for run in state["gate_runs"] if run["epoch"] == 0]
        self.assertEqual({run["verdict"] for run in old_runs}, {"pass"})

        # Completing the active epoch restores review_clean.
        self.run_review("gates", "--dispatch-id", "D-t9", "--check", "extra")
        self.run_review("clean", "--dispatch-id", "D-t9")
        self.assertEqual(self.record("D-t9")["state"], "review_clean")

    def test_t9_pre_rebind_skip_cannot_satisfy_new_epoch(self) -> None:
        dod = self.two_check_dod()
        self.align_integration()
        self.to_dispatched("D-t9s", dod=dod)
        self.work_commit("t9s work\n")
        self.run_review("mark-executed", "--dispatch-id", "D-t9s")
        self.run_review(
            "gates", "--dispatch-id", "D-t9s", "--check", "green",
            "--skip", "extra", "--reason", "waived", "--risk", "low",
        )
        self.run_review("clean", "--dispatch-id", "D-t9s")
        self.assertEqual(self.record("D-t9s")["state"], "review_clean")
        self.assertEqual(self.record("D-t9s")["skips"][0]["epoch"], 0)

        self.advance_and_rebase("advance-t9s.txt")
        self.run_review("rebind", "--dispatch-id", "D-t9s")

        # The epoch-0 skip cannot satisfy the post-rebind epoch.
        self.run_review("gates", "--dispatch-id", "D-t9s", "--check", "green")
        self.run_review("clean", "--dispatch-id", "D-t9s", ok=False)

        # A fresh skip under existing policy can.
        self.run_review(
            "gates", "--dispatch-id", "D-t9s", "--check", "green",
            "--skip", "extra", "--reason", "waived again", "--risk", "low",
        )
        state = self.record("D-t9s")
        self.assertEqual([skip["epoch"] for skip in state["skips"]], [0, 1])
        self.run_review("clean", "--dispatch-id", "D-t9s")
        self.assertEqual(self.record("D-t9s")["state"], "review_clean")

    # ----- T10: rerun semantics and legacy epoch backcompat -----

    def test_t10_same_epoch_rerun_keeps_latest_run_semantics(self) -> None:
        self.to_executed("D-t10r")
        with mock.patch.object(
            checks,
            "run_check",
            side_effect=[self.gate_run("fail"), self.gate_run("pass")],
        ):
            self.assertEqual(
                self.run_review(
                    "gates", "--dispatch-id", "D-t10r", "--check", "green", ok=False
                ),
                1,
            )
            self.run_review("clean", "--dispatch-id", "D-t10r", ok=False)
            self.run_review("gates", "--dispatch-id", "D-t10r", "--check", "green")
        state = self.record("D-t10r")
        self.assertEqual([run["epoch"] for run in state["gate_runs"]], [0, 0])
        self.run_review("clean", "--dispatch-id", "D-t10r")
        self.assertEqual(self.record("D-t10r")["state"], "review_clean")

    def test_t10_legacy_records_without_epoch_behave_as_epoch_zero(self) -> None:
        dod = self.two_check_dod()
        self.to_dispatched("D-t10l", dod=dod)
        self.run_review("mark-executed", "--dispatch-id", "D-t10l")
        self.run_review(
            "gates", "--dispatch-id", "D-t10l", "--check", "green",
            "--skip", "extra", "--reason", "waived", "--risk", "low",
        )
        state = self.record("D-t10l")
        state.pop("gate_epoch", None)
        for run in state["gate_runs"]:
            run.pop("epoch", None)
        for skip in state["skips"]:
            skip.pop("epoch", None)
        self.write_record(state, "D-t10l")
        self.run_review("clean", "--dispatch-id", "D-t10l")
        self.assertEqual(self.record("D-t10l")["state"], "review_clean")

    # ----- T11: audit atomicity -----

    def test_t11_rebind_success_audit_is_complete(self) -> None:
        self.to_clean_with_work("D-t11", text="t11 work\n")
        self.advance_and_rebase("advance-t11.txt")
        self.run_review("rebind", "--dispatch-id", "D-t11")
        state = self.record("D-t11")
        events = [event for event in state["history"] if event["event"] == "rebind"]
        self.assertEqual(len(events), 1)
        for field in (
            "old_base", "new_base", "old_reviewed_head", "new_reviewed_head",
            "canonical_diff_digest", "stable_patch_id", "prior_state",
            "old_gate_epoch", "new_gate_epoch",
        ):
            self.assertIn(field, events[0])
        # No approval existed, so nothing is archived as superseded.
        self.assertEqual(state.get("superseded_approvals", []), [])

    def test_t11_rebind_failure_leaves_record_unchanged_with_brief_constant(self) -> None:
        self.to_clean_with_work("D-t11f", text="t11f work\n")
        # Same-head no-op refusal: byte-identical record afterwards.
        self.refuse_rebind("D-t11f", "already equals")

    def test_t11_brief_mutation_still_transitions_to_brief_revised(self) -> None:
        self.to_clean_with_work("D-t11b", text="t11b work\n")
        self.advance_and_rebase("advance-t11b.txt")
        original = self.brief.read_text(encoding="utf-8")
        self.brief.write_text(f"{original}material edit\n", encoding="utf-8")
        try:
            self.run_review("rebind", "--dispatch-id", "D-t11b", ok=False)
            state = self.record("D-t11b")
            self.assertEqual(state["state"], "brief_revised")
            self.assertEqual([event for event in state["history"] if event["event"] == "rebind"], [])
        finally:
            self.brief.write_text(original, encoding="utf-8")

    def test_t11_executed_brief_revision_preserves_provenance_and_appends_new_cycle(self) -> None:
        self.to_clean_with_work("D-t11-provenance", text="paid-for work\n")
        before = self.record("D-t11-provenance")
        preserved = {
            name: copy.deepcopy(before[name])
            for name in ("worker_evidence", "reviewed_head", "trigger_closed", "history", "intended_dispatches")
        }
        original = self.brief.read_text(encoding="utf-8")
        self.brief.write_text(f"{original}material provenance revision\n", encoding="utf-8")
        try:
            self.run_review("rebind", "--dispatch-id", "D-t11-provenance", ok=False)
            revised = self.record("D-t11-provenance")
            self.assertEqual(revised["state"], "brief_revised")
            self.assertEqual(revised["state_before_brief_revised"], "review_clean")
            for name, value in preserved.items():
                self.assertEqual(revised[name], value)

            for missing in ("worker_evidence", "reviewed_head", "trigger_closed"):
                damaged = copy.deepcopy(revised)
                damaged[missing] = [] if missing == "worker_evidence" else None
                with self.subTest(missing=missing), self.assertRaisesRegex(
                    review.ReviewError, "execution lineage requires"
                ):
                    review.validate_record(damaged)
            damaged = copy.deepcopy(revised)
            damaged["worker_evidence"] = []
            damaged["reviewed_head"] = None
            damaged.pop("trigger_closed")
            with self.assertRaisesRegex(review.ReviewError, "execution lineage requires"):
                review.validate_record(damaged)

            self.run_review(
                "brief-check", "--dispatch-id", "D-t11-provenance", "--clean", "--by", "codex",
                "--surface-verdict", "complete", "--surface-reason", "fixture declaration is complete",
            )
            self.align_integration()  # contract 17: second cycle dispatches at clean common HEAD
            self.run_review(
                "mark-dispatched", "--dispatch-id", "D-t11-provenance",
                "--idempotency-key", "D-t11-provenance-second",
            )
            redispatched = self.record("D-t11-provenance")
            self.assertEqual(redispatched["worker_evidence"], preserved["worker_evidence"])
            self.assertEqual(redispatched["reviewed_head"], preserved["reviewed_head"])
            self.assertIs(redispatched["trigger_closed"], True)
            self.assertEqual(redispatched["history"][:len(preserved["history"])], preserved["history"])
            self.assertEqual(len(redispatched["intended_dispatches"]), len(preserved["intended_dispatches"]) + 1)
            self.assertEqual(redispatched["intended_dispatches"][-1]["attempt"], 2)
        finally:
            self.brief.write_text(original, encoding="utf-8")

    # ----- T12: end-to-end rebind cycle -----

    def test_t12_end_to_end_rebind_to_verified(self) -> None:
        key_path = self.enable_approval_signing()
        self.to_clean_with_work("D-t12", text="t12 work\n")
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--dispatch-id",
                "D-t12",
                "--approver",
                "human",
                "--key",
                str(key_path),
            )
        first_approval = self.record("D-t12")["approval"]
        first_head = self.record("D-t12")["approved_head"]

        # Integration advances after the clean review; safe recovery.
        new_base, new_head = self.advance_and_rebase("advance-t12.txt")
        self.run_review("rebind", "--dispatch-id", "D-t12")
        self.assertEqual(self.record("D-t12")["state"], "executed")

        # Fresh gates, clean, fresh approval, gate-merge, actual ff, verify.
        self.run_review("gates", "--dispatch-id", "D-t12", "--check", "green")
        self.run_review("clean", "--dispatch-id", "D-t12")
        with mock.patch.object(
            approval, "read_tty_confirmation", return_value="APPROVE"
        ):
            self.run_review(
                "approve",
                "--dispatch-id",
                "D-t12",
                "--approver",
                "human",
                "--key",
                str(key_path),
            )
        self.run_review("gate-merge", "--dispatch-id", "D-t12")
        self.git("merge", "--ff-only", "work-branch", cwd=self.integration)
        self.run_review("verify", "--dispatch-id", "D-t12", "--by", "architect")

        state = self.record("D-t12")
        self.assertEqual(state["state"], "verified")
        self.assertEqual(state["reviewed_head"], new_head)
        self.assertEqual(state["base_commit"], new_base)
        # Both approvals durably distinguishable.
        self.assertEqual(len(state["superseded_approvals"]), 1)
        self.assertEqual(state["superseded_approvals"][0]["approval"], first_approval)
        self.assertEqual(state["superseded_approvals"][0]["approved_head"], first_head)
        self.assertEqual(state["approved_head"], new_head)
        self.assertNotEqual(state["approval"]["signature"], first_approval["signature"])
        # Both epochs durably distinguishable.
        self.assertEqual(sorted({run["epoch"] for run in state["gate_runs"]}), [0, 1])
        self.assertEqual(state["gate_epoch"], 1)

    def test_summary_is_regenerated_by_cli_only(self) -> None:
        self.open_review()
        summary = self.review_root / "D1.summary.md"
        self.assertIn("drafted_brief", summary.read_text(encoding="utf-8"))
        summary.write_text("manual drift\n", encoding="utf-8")
        self.run_review("brief-check", "--dispatch-id", "D1", "--clean", "--by", "codex", "--surface-verdict", "complete", "--surface-reason", "fixture declaration is complete")
        text = summary.read_text(encoding="utf-8")
        self.assertIn("brief_reviewed", text)
        self.assertNotIn("manual drift", text)

    def _assert_result_semantics_primitives(self) -> None:
        self.assertIsNotNone(review.build_parser())
        self.assertEqual(review.SCHEMA_VERSION, 2)
        self.assertTrue(review.WORKER_DISPATCH_ID_RE.fullmatch("dispatch_20260722_120000_00000001"))

    def _run_review_raw(self, *args: str) -> tuple[int, dict | None, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                rc = review.main(args)
            except SystemExit as exc:
                rc = int(exc.code) if isinstance(exc.code, int) else 1
        text = stdout.getvalue()
        return rc, json.loads(text) if text.strip().startswith("{") else None, stderr.getvalue()

    def _prepare_result_review(self, dispatch_id: str, key: str) -> None:
        self.open_review(dispatch_id)
        self.run_review(
            "brief-check", "--dispatch-id", dispatch_id, "--clean", "--by", "codex",
            "--surface-verdict", "complete", "--surface-reason", "fixture complete",
        )
        # Contract 17: dispatch at one clean common HEAD; any pre-committed
        # fixture base becomes that base and the result commit is the candidate.
        self.align_integration()
        self.run_review(
            "mark-dispatched", "--dispatch-id", dispatch_id,
            "--idempotency-key", key,
        )

    def _worker_id_for_key(self, key: str) -> str:
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            row = conn.execute(
                "select dispatch_id from dispatch_ledger "
                "where producer_actor_id=? and idempotency_key=?",
                ("gamma-architect", key),
            ).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def _set_worker_result(self, key: str, result: str) -> None:
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            conn.execute(
                "update dispatch_ledger set result=? where producer_actor_id=? "
                "and idempotency_key=?",
                (result, "gamma-architect", key),
            )

    def _snapshot_repo_delta(self) -> dict:
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "update actors set project_root=? where id=?",
                (str(self.repo), "gamma-codex-worker"),
            )
            return Store(self.ledger_path)._mailbox._snapshot_delta(
                conn, "gamma-codex-worker"
            )

    def _install_closeout(
        self, key: str, *, delta: dict | None, artifacts: list[dict] | None = None
    ) -> None:
        import sqlite3
        store = Store(self.ledger_path)
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            exists = conn.execute(
                "select 1 from dispatch_ledger where producer_actor_id=? "
                "and idempotency_key=?",
                ("gamma-architect", key),
            ).fetchone()
        if exists is None:
            store.dispatch_agent(
                "gamma-architect", "gamma-codex-worker", key,
                "fixture dispatch", "fixture dispatch", [],
            )
        worker_id = self._worker_id_for_key(key)
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            message_id = conn.execute(
                "select message_id from dispatch_ledger where dispatch_id=?",
                (worker_id,),
            ).fetchone()[0]
            reply_id = f"msg-reply-{worker_id}"
            now = "2026-07-23T12:00:00+00:00"
            conn.execute(
                "insert into messages(id,from_agent,subject,body,refs_json,priority,"
                "requires_ack,created_at) values(?,?, 'fixture','fixture','[]','normal',0,?)",
                (reply_id, "gamma-codex-worker", now),
            )
            conn.execute(
                "insert into message_threads(message_id,parent_message_id) values(?,?)",
                (reply_id, message_id),
            )
            conn.execute(
                "insert into message_recipients(message_id,to_agent,status) values(?,?,'sent')",
                (reply_id, "gamma-architect"),
            )
            closeout = {
                "protocol": 1,
                "recorded_by": "gamma-codex-worker",
                "reply_message_id": reply_id,
                "delta": delta,
                "artifacts": artifacts or [],
            }
            conn.execute(
                "update dispatch_ledger set status='closed', result='satisfied', "
                "closed_at=?, observed_values_json=? where dispatch_id=?",
                (now, json.dumps({"closeout": closeout}, sort_keys=True), worker_id),
            )

    def _commit_all(self, message: str) -> None:
        self.git("add", "-A", cwd=self.repo)
        self.git("commit", "-m", message, cwd=self.repo)

    def test_N4_binding_unverified(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(Path, "read_text", side_effect=AssertionError("record read")):
            rc, payload, _ = self._run_review_raw("status", "--dispatch-id", "missing")
        self.assertEqual(rc, 1)
        self.assertEqual(payload["diagnosis"], "binding_unverified")
        self.assertIsNone(payload["binding"]["record_path"])
        self.assertNotIn("record_not_found", json.dumps(payload))

    def test_N5_wrong_store_precedes_io(self):
        decoy = self.review_root / "decoy.json"
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_text('{"would":"look valid enough to tempt I/O"}\n', encoding="utf-8")
        wrong = self.root / "wrong-store"
        real_read = Path.read_text
        reads = []
        def audited_read(path, *args, **kwargs):
            reads.append(Path(path))
            return real_read(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", audited_read):
            rc, payload, _ = self._run_review_raw(
                "status", "--expected-repo-root", str(wrong), "--dispatch-id", "decoy"
            )
        self.assertEqual(rc, 1)
        self.assertEqual(payload["diagnosis"], "wrong_store")
        self.assertIsNone(payload["binding"]["record_path"])
        self.assertEqual(reads, [])

    def test_N6_record_not_found_after_binding(self):
        reads = []
        real_read = Path.read_text
        def audited_read(path, *args, **kwargs):
            reads.append(Path(path))
            return real_read(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", audited_read):
            rc, payload, _ = self._run_review_raw(
                "status", "--expected-repo-root", str(review.REPO_ROOT),
                "--dispatch-id", "definitely-absent",
            )
        self.assertEqual(rc, 1)
        self.assertEqual(payload["diagnosis"], "record_not_found")
        self.assertEqual(payload["binding"]["actual_repo_root"],
                         payload["binding"]["expected_repo_root"])
        self.assertEqual(len(reads), 1)

    def test_N10_derived_intent_refusals(self):
        self.open_review("N10")
        record = self.record("N10")
        record["state"] = "dispatched"
        record["intended_dispatches"] = []
        self.write_record(record, "N10")
        rc, _, stderr = self._run_review_raw("mark-executed", "--dispatch-id", "N10")
        self.assertEqual(rc, 1)
        self.assertIn("intended_dispatch_unrecorded", stderr)

        record["intended_dispatches"] = [{
            "attempt": 1, "idempotency_key": "absent-key",
            "recorded_at": "2026-07-22T12:00:00Z",
        }]
        self.write_record(record, "N10")
        rc, _, stderr = self._run_review_raw("mark-executed", "--dispatch-id", "N10")
        self.assertEqual(rc, 1)
        self.assertIn("intended_dispatch_not_found", stderr)

        self._bootstrap_worker_evidence("N10", "mark-executed")
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            conn.execute(
                "update dispatch_ledger set policy_version='v1', result=NULL "
                "where idempotency_key='absent-key'"
            )
        rc, _, stderr = self._run_review_raw("mark-executed", "--dispatch-id", "N10")
        self.assertEqual(rc, 1)
        self.assertIn("result_not_satisfied", stderr)
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            conn.execute(
                "update dispatch_ledger set policy_version='v2', result='satisfied', observed_values_json='{}' "
                "where idempotency_key='absent-key'"
            )
        rc, _, stderr = self._run_review_raw("mark-executed", "--dispatch-id", "N10")
        self.assertEqual(rc, 1)
        self.assertIn("closeout_missing", stderr)

        parser = review.build_parser()
        for verb in ("mark-executed", "mark-blocked", "mark-superseded"):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args([verb, "--dispatch-id", "N10",
                                       "--worker-dispatch-id", "dispatch_20260722_120000_00000001"])
    def test_N12_global_single_use_binding(self):
        # Contract 17 makes (producer, idempotency_key) globally single-use at
        # the intent layer: the first record claims the key at mark-dispatched
        # and a second, distinct record cannot reuse it. Its differing canonical
        # payload (a different record_id) conflicts permanently on the single
        # elected row, before any second binding could ever exist -- the
        # unconditional producer/key single-row/single-use rule.
        shared_key = "n12-shared"
        self._prepare_result_review("N12-a", shared_key)
        self.open_review("N12-b")
        self.run_review(
            "brief-check",
            "--dispatch-id",
            "N12-b",
            "--clean",
            "--by",
            "codex",
            "--surface-verdict",
            "complete",
            "--surface-reason",
            "fixture complete",
        )
        self.align_integration()
        rc, _, stderr = self.run_review_capture(
            "mark-dispatched",
            "--dispatch-id",
            "N12-b",
            "--idempotency-key",
            shared_key,
        )
        self.assertEqual(rc, 1)
        self.assertIn("review_intent_conflict", stderr)
        self.assertIn("conflict is permanent", stderr)
        # The single elected row still belongs to the first record; the second
        # never advanced past brief_reviewed and no second intent row exists.
        self.assertEqual(self.record("N12-a")["state"], "dispatched")
        self.assertEqual(self.record("N12-b")["state"], "brief_reviewed")
        import sqlite3

        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn:
            rows = conn.execute(
                "select count(*) from review_dispatch_intents "
                "where producer_actor_id=? and idempotency_key=?",
                ("gamma-architect", shared_key),
            ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_N14_blocked_redispatch_cap(self):
        self._prepare_result_review("N14", "n14-round-1")
        self.run_review("mark-blocked", "--dispatch-id", "N14", "--note", "blocked 1")
        self.assertEqual(self.record("N14")["state"], "dispatch_blocked")
        rc, _, stderr = self._run_review_raw("mark-executed", "--dispatch-id", "N14")
        self.assertEqual(rc, 1)
        self.assertIn("state dispatch_blocked not allowed", stderr)
        rc, _, stderr = self._run_review_raw(
            "redispatch", "--dispatch-id", "N14", "--note", "reuse",
            "--idempotency-key", "n14-round-1",
        )
        self.assertEqual(rc, 1)
        self.assertIn("intent_key_reused", stderr)

        for round_number in range(2, 5):
            self.run_review(
                "redispatch", "--dispatch-id", "N14",
                "--note", f"round {round_number}",
                "--idempotency-key", f"n14-round-{round_number}",
            )
            self.assertEqual(self.record("N14")["state"], "dispatched")
            self.run_review(
                "mark-blocked", "--dispatch-id", "N14",
                "--note", f"blocked {round_number}",
            )
        self.assertEqual(self.record("N14")["blocked_redispatch_count"], 3)
        self.run_review(
            "redispatch", "--dispatch-id", "N14", "--note", "over cap",
            "--idempotency-key", "n14-round-5",
        )
        final = self.record("N14")
        self.assertEqual(final["state"], "escalated")
        self.assertEqual(final["blocked_redispatch_count"], 3)
        self.assertNotIn("n14-round-5", {
            item["idempotency_key"] for item in final["intended_dispatches"]
        })
    def test_N15_actor_intent_mismatch(self):
        self.open_review("N15")
        self.run_review("brief-check", "--dispatch-id", "N15", "--clean", "--by", "codex",
                        "--surface-verdict", "complete", "--surface-reason", "fixture complete")
        self.run_review("mark-dispatched", "--dispatch-id", "N15", "--idempotency-key", "n15-key")
        self._bootstrap_worker_evidence("N15", "mark-executed")
        record = self.record("N15")
        record["expected_recipient"] = "immutable-other-recipient"
        self.write_record(record, "N15")
        rc, _, stderr = self._run_review_raw("mark-executed", "--dispatch-id", "N15")
        self.assertEqual(rc, 1)
        self.assertIn("intent_mismatch: ledger actors differ", stderr)
        parser = review.build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["mark-executed", "--dispatch-id", "N15",
                                   "--expected-recipient", "gamma-codex-worker"])

    def test_schema2_malformed_record_matrix_is_typed(self):
        self.open_review("schema-matrix")
        base = self.record("schema-matrix")
        base["brief_sha256"] = "a" * 64
        base["approved_head"] = "b" * 40
        base["intended_dispatches"] = [{
            "attempt": 1, "idempotency_key": "matrix-key",
            "recorded_at": "2026-07-22T12:00:00Z",
        }]
        closeout = {
            "protocol": 1, "recorded_by": base["expected_recipient"],
            "reply_message_id": "msg-reply", "delta": None,
        }
        common = {
            "worker_dispatch_id": "dispatch_20260722_120000_00000001",
            "intent_attempt": 1, "idempotency_key": "matrix-key",
            "ledger_db": "/tmp/ledger.sqlite", "closeout": closeout,
            "verified_at": "2026-07-22T12:00:00Z",
        }
        base["worker_evidence"] = [{
            **common, "producer": base["expected_producer"],
            "recipient": base["expected_recipient"], "status": "closed",
            "result": "satisfied",
            "delta_verification": {
                "snapshot_tree": "c" * 40, "reviewed_head_tree": "c" * 40,
                "manifest_sha256": "d" * 64, "entries": 1,
                "status_counts": {"M": 1},
            },
            "artifact_bindings": [{
                "real_path": "/tmp/artifact", "remeasured_sha256": "e" * 64,
                "binding": "filesystem",
            }],
        }]
        base["blocked_dispatches"] = [{**common, "note": "blocked"}]
        base["superseded_dispatches"] = [{**common, "note": "superseded"}]
        base["brief_checks"] = [{
            "by": "codex", "verdict": "clean", "finding": None,
            "surface_verdict": "complete", "surface_reason": "complete",
            "timestamp": "2026-07-22T12:00:00Z", "brief_sha256": "a" * 64,
        }]
        base["findings"] = [{
            "id": "F1", "severity": "blocking", "loc": "x.py:1",
            "problem": "p", "impact": "i", "fix": "f", "status": "open",
            "resolved_by_dispatch_id": None, "resolution_note": None,
        }]
        base["gate_runs"] = [{
            "check_id": "green", "argv_or_registry_name": "green",
            "cwd": str(self.repo), "git_head": None, "branch": None,
            "env_policy": "redacted", "started_at": "2026-07-22T12:00:00Z",
            "ended_at": "2026-07-22T12:00:01Z", "timeout_s": 30,
            "exit_code": 0, "stdout_excerpt": "", "stderr_excerpt": "",
            "verdict": "pass", "epoch": 0,
        }]
        base["skips"] = [{
            "check_id": "extra", "reason": "r", "risk": "low",
            "actor": "architect", "timestamp": "2026-07-22T12:00:00Z",
            "epoch": 0,
        }]
        base["approval"] = {
            "approver": "human", "mechanism": "signed",
            "timestamp": "2026-07-22T12:00:00Z", "approved_head": "b" * 40,
            "signature": "sig",
        }
        import copy
        cases = {
            "top-level": lambda r: r.__setitem__("unknown", True),
            "brief-check": lambda r: r["brief_checks"][0].__setitem__("verdict", "maybe"),
            "dod": lambda r: r["dod"][0].__setitem__("required", "yes"),
            "finding": lambda r: r["findings"][0].__setitem__("severity", "urgent"),
            "gate": lambda r: r["gate_runs"][0].__setitem__("exit_code", "zero"),
            "skip": lambda r: r["skips"][0].__setitem__("epoch", -1),
            "history": lambda r: r["history"][0].__setitem__("timestamp", None),
            "approval": lambda r: r["approval"].__setitem__("signature", None),
            "intent-order": lambda r: r["intended_dispatches"][0].__setitem__("attempt", 2),
            "worker-result": lambda r: r["worker_evidence"][0].__setitem__("result", "blocked"),
            "worker-delta": lambda r: r["worker_evidence"][0]["delta_verification"].__setitem__("entries", 0),
            "artifact": lambda r: r["worker_evidence"][0]["artifact_bindings"][0].__setitem__("binding", "guess"),
            "closeout": lambda r: r["blocked_dispatches"][0]["closeout"].__setitem__("protocol", 2),
            "closeout-blocked-reason": lambda r: r["blocked_dispatches"][0]["closeout"].__setitem__("blocked_reason", 7),
            "closeout-caller-payload": lambda r: r["blocked_dispatches"][0]["closeout"].__setitem__("caller_payload_sha256", "A" * 64),
            "closeout-recorded-at": lambda r: r["blocked_dispatches"][0]["closeout"].__setitem__("recorded_at", ""),
            "blocked": lambda r: r["blocked_dispatches"][0].__setitem__("note", 7),
            "superseded": lambda r: r["superseded_dispatches"][0].__setitem__("verified_at", None),
            "counter": lambda r: r.__setitem__("blocked_redispatch_count", 4),
            "actor": lambda r: r.__setitem__("expected_producer", ""),
            "vocabulary": lambda r: r.__setitem__("state", "done"),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                record = copy.deepcopy(base)
                mutate(record)
                self.write_record(record, "schema-matrix")
                rc, payload, stderr = self._run_review_raw(
                    "status", "--expected-repo-root", str(review.REPO_ROOT),
                    "--dispatch-id", "schema-matrix",
                )
                self.assertEqual((rc, payload["diagnosis"]), (1, "record_invalid"))
                self.assertNotIn("Traceback", stderr)

    def test_schema2_unhashable_prior_brief_revision_states_are_record_invalid(self):
        self.open_review("prior-state-unhashable")
        base = self.record("prior-state-unhashable")
        for value in ([], {}):
            with self.subTest(value=value):
                record = dict(base)
                record["state_before_brief_revised"] = value
                self.write_record(record, "prior-state-unhashable")
                rc, payload, stderr = self._run_review_raw(
                    "status",
                    "--expected-repo-root", str(review.REPO_ROOT),
                    "--dispatch-id", "prior-state-unhashable",
                )
                self.assertEqual((rc, payload["diagnosis"]), (1, "record_invalid"))
                self.assertNotIn("Traceback", stderr)

    def test_schema2_execution_evidence_state_matrix_is_typed(self):
        self.open_review("execution-state-matrix")
        base = self.record("execution-state-matrix")
        base["brief_sha256"] = "a" * 64
        base["reviewed_head"] = "b" * 40
        base["trigger_closed"] = True
        base["intended_dispatches"] = [{
            "attempt": 1, "idempotency_key": "execution-state-key",
            "recorded_at": "2026-07-22T12:00:00Z",
        }]
        base["worker_evidence"] = [{
            "worker_dispatch_id": "dispatch_20260722_120000_00000001",
            "intent_attempt": 1, "idempotency_key": "execution-state-key",
            "ledger_db": "/tmp/ledger.sqlite",
            "closeout": {
                "protocol": 1, "recorded_by": base["expected_recipient"],
                "reply_message_id": "msg-reply", "delta": None,
            },
            "verified_at": "2026-07-22T12:00:00Z",
            "producer": base["expected_producer"],
            "recipient": base["expected_recipient"], "status": "closed",
            "result": "satisfied",
            "delta_verification": {
                "snapshot_tree": "c" * 40, "reviewed_head_tree": "c" * 40,
                "manifest_sha256": "d" * 64, "entries": 1,
                "status_counts": {"M": 1},
            },
            "artifact_bindings": [],
        }]
        import copy
        for state in review.EXECUTION_BOUND_STATES:
            with self.subTest(state=state, shape="valid"):
                record = copy.deepcopy(base)
                record["state"] = state
                self.write_record(record, "execution-state-matrix")
                rc, payload, stderr = self._run_review_raw(
                    "status", "--expected-repo-root", str(review.REPO_ROOT),
                    "--dispatch-id", "execution-state-matrix",
                )
                self.assertEqual((rc, payload["diagnosis"]), (0, "ok"))
                self.assertNotIn("Traceback", stderr)
            for missing in ("reviewed_head", "worker_evidence", "trigger_closed"):
                with self.subTest(state=state, missing=missing):
                    record = copy.deepcopy(base)
                    record["state"] = state
                    if missing == "reviewed_head":
                        record[missing] = None
                    elif missing == "worker_evidence":
                        record[missing] = []
                    else:
                        record.pop(missing)
                    self.write_record(record, "execution-state-matrix")
                    rc, payload, stderr = self._run_review_raw(
                        "status", "--expected-repo-root", str(review.REPO_ROOT),
                        "--dispatch-id", "execution-state-matrix",
                    )
                    self.assertEqual((rc, payload["diagnosis"]), (1, "record_invalid"))
                    self.assertNotIn("Traceback", stderr)
        for state in review.REVIEW_STATES - review.EXECUTION_BOUND_STATES:
            with self.subTest(state=state, shape="preserved-complete-evidence"):
                record = copy.deepcopy(base)
                record["state"] = state
                if state in {"dispatch_blocked", "dispatch_superseded"}:
                    field = {
                        "dispatch_blocked": "blocked_dispatches",
                        "dispatch_superseded": "superseded_dispatches",
                    }[state]
                    record[field] = [{
                        key: record["worker_evidence"][0][key]
                        for key in (
                            "worker_dispatch_id", "intent_attempt", "idempotency_key",
                            "ledger_db", "closeout", "verified_at",
                        )
                    }]
                    record[field][0]["note"] = "state-matrix fixture"
                self.write_record(record, "execution-state-matrix")
                rc, payload, stderr = self._run_review_raw(
                    "status", "--expected-repo-root", str(review.REPO_ROOT),
                    "--dispatch-id", "execution-state-matrix",
                )
                self.assertEqual((rc, payload["diagnosis"]), (0, "ok"))
                self.assertNotIn("Traceback", stderr)
            with self.subTest(state=state, shape="partial-evidence"):
                record = copy.deepcopy(base)
                record["state"] = state
                record["worker_evidence"] = []
                self.write_record(record, "execution-state-matrix")
                rc, payload, stderr = self._run_review_raw(
                    "status", "--expected-repo-root", str(review.REPO_ROOT),
                    "--dispatch-id", "execution-state-matrix",
                )
                self.assertEqual((rc, payload["diagnosis"]), (1, "record_invalid"))
                self.assertNotIn("Traceback", stderr)

    def test_schema2_delta_status_summary_matrix_is_typed(self):
        self.open_review("delta-summary-matrix")
        base = self.record("delta-summary-matrix")
        base["brief_sha256"] = "a" * 64
        base["reviewed_head"] = "b" * 40
        base["trigger_closed"] = True
        base["state"] = "executed"
        base["intended_dispatches"] = [{
            "attempt": 1, "idempotency_key": "delta-summary-key",
            "recorded_at": "2026-07-22T12:00:00Z",
        }]
        base["worker_evidence"] = [{
            "worker_dispatch_id": "dispatch_20260722_120000_00000001",
            "intent_attempt": 1, "idempotency_key": "delta-summary-key",
            "ledger_db": "/tmp/ledger.sqlite",
            "closeout": {
                "protocol": 1, "recorded_by": base["expected_recipient"],
                "reply_message_id": "msg-reply", "delta": None,
            },
            "verified_at": "2026-07-22T12:00:00Z",
            "producer": base["expected_producer"],
            "recipient": base["expected_recipient"], "status": "closed",
            "result": "satisfied",
            "delta_verification": {
                "snapshot_tree": "c" * 40, "reviewed_head_tree": "c" * 40,
                "manifest_sha256": "d" * 64, "entries": 3,
                "status_counts": {"A": 1, "M": 2},
            },
            "artifact_bindings": [],
        }]
        import copy
        invalid = {
            "empty": (3, {}),
            "unknown": (3, {"Q": 3}),
            "zero": (3, {"M": 0}),
            "negative": (3, {"M": -1}),
            "bool": (1, {"M": True}),
            "non-integer": (1, {"M": 1.0}),
            "sum-mismatch": (3, {"M": 2}),
        }
        for name, (entries, counts) in invalid.items():
            with self.subTest(name=name):
                record = copy.deepcopy(base)
                delta = record["worker_evidence"][0]["delta_verification"]
                delta["entries"], delta["status_counts"] = entries, counts
                self.write_record(record, "delta-summary-matrix")
                rc, payload, stderr = self._run_review_raw(
                    "status", "--expected-repo-root", str(review.REPO_ROOT),
                    "--dispatch-id", "delta-summary-matrix",
                )
                self.assertEqual((rc, payload["diagnosis"]), (1, "record_invalid"))
                self.assertNotIn("Traceback", stderr)
    def test_N21_intended_row_selection(self):
        self._prepare_result_review("N21", "n21-intended")
        self._bootstrap_worker_evidence("N21", "mark-executed")
        intended_id = self._worker_id_for_key("n21-intended")
        store = Store(self.ledger_path)
        decoy = store.dispatch_agent(
            "gamma-architect", "gamma-codex-worker", "n21-decoy",
            "decoy", "decoy", [],
        )
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            observed = conn.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id=?",
                (intended_id,),
            ).fetchone()[0]
            conn.execute(
                "update dispatch_ledger set status='closed', closed_at=created_at, "
                "result='satisfied', observed_values_json=? where dispatch_id=?",
                (observed, decoy["dispatch_id"]),
            )
        rc, _, stderr = self._run_review_raw(
            "mark-executed", "--dispatch-id", "N21",
        )
        self.assertEqual((rc, stderr), (0, ""))
        evidence = self.record("N21")["worker_evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["worker_dispatch_id"], intended_id)
        self.assertNotEqual(evidence[0]["worker_dispatch_id"], decoy["dispatch_id"])
    def test_N22_unchanged_artifact_deltaless(self):
        self._prepare_result_review("N22", "n22-deltaless")
        tracked = self.repo / "tracked.txt"
        digest = hashlib.sha256(tracked.read_bytes()).hexdigest()
        self._install_closeout(
            "n22-deltaless",
            delta=None,
            artifacts=[{
                "path": "tracked.txt", "real_path": str(tracked),
                "sha256": digest, "bytes": tracked.stat().st_size,
                "remeasured_sha256": digest, "remeasured_bytes": tracked.stat().st_size,
            }],
        )
        rc, _, stderr = self._run_review_raw(
            "mark-executed", "--dispatch-id", "N22"
        )
        self.assertEqual(rc, 1)
        self.assertIn("deltaless_closeout_for_code_review", stderr)
        self.assertEqual(self.record("N22")["state"], "dispatched")

    def test_N23_external_artifact_deltaless(self):
        self._prepare_result_review("N23", "n23-deltaless")
        external = self.root / "outside-review-repo.txt"
        external.write_text("supplementary only\n", encoding="utf-8")
        digest = hashlib.sha256(external.read_bytes()).hexdigest()
        self._install_closeout(
            "n23-deltaless",
            delta=None,
            artifacts=[{
                "path": str(external), "real_path": str(external),
                "sha256": digest, "bytes": external.stat().st_size,
                "remeasured_sha256": digest, "remeasured_bytes": external.stat().st_size,
            }],
        )
        rc, _, stderr = self._run_review_raw(
            "mark-executed", "--dispatch-id", "N23"
        )
        self.assertEqual(rc, 1)
        self.assertIn("deltaless_closeout_for_code_review", stderr)
        self.assertEqual(self.record("N23")["worker_evidence"], [])

    def test_N24_later_commit_divergence(self):
        for variant in ("adds", "omits", "changes"):
            with self.subTest(variant=variant):
                dispatch_id, key = f"N24-{variant}", f"n24-{variant}"
                self._prepare_result_review(dispatch_id, key)
                tracked = self.repo / "tracked.txt"
                tracked.write_text(f"snapshot-{variant}\n", encoding="utf-8")
                if variant == "omits":
                    (self.repo / "snapshot-only.txt").write_text("must be committed\n", encoding="utf-8")
                delta = self._snapshot_repo_delta()
                self._install_closeout(key, delta=delta)
                if variant == "adds":
                    (self.repo / "later-only.txt").write_text("not snapshotted\n", encoding="utf-8")
                elif variant == "omits":
                    (self.repo / "snapshot-only.txt").unlink()
                else:
                    tracked.write_text("different later bytes\n", encoding="utf-8")
                self._commit_all(f"divergent {variant}")
                rc, _, stderr = self._run_review_raw(
                    "mark-executed", "--dispatch-id", dispatch_id
                )
                self.assertEqual(rc, 1)
                self.assertIn("delta_mismatch", stderr)

    def test_N24b_delta_counts_are_recomputed_before_any_mutation(self):
        for field, wrong_value in (("entries", 2), ("status_counts", {"M": 99})):
            with self.subTest(field=field):
                dispatch_id, key = f"N24b-{field}", f"n24b-{field}"
                self._prepare_result_review(dispatch_id, key)
                (self.repo / f"{field}.txt").write_text("delta\n", encoding="utf-8")
                delta = self._snapshot_repo_delta()
                delta[field] = wrong_value
                self._install_closeout(key, delta=delta)
                self._commit_all(f"commit {field} mismatch fixture")
                record_path = self.review_root / f"{dispatch_id}.json"
                bindings = self.review_root / "bindings"
                before_record = record_path.read_bytes()
                before_bindings = (
                    sorted((path.name, path.read_bytes()) for path in bindings.iterdir())
                    if bindings.exists() else []
                )
                rc, _, stderr = self._run_review_raw(
                    "mark-executed", "--dispatch-id", dispatch_id
                )
                self.assertEqual(rc, 1)
                self.assertIn("delta_mismatch", stderr)
                self.assertEqual(record_path.read_bytes(), before_record)
                after_bindings = (
                    sorted((path.name, path.read_bytes()) for path in bindings.iterdir())
                    if bindings.exists() else []
                )
                self.assertEqual(after_bindings, before_bindings)

    def test_N24c_mark_executed_rejects_pathname_initial_status_oracle(self):
        self.git("mv", "tracked.txt", "A-leading.txt", cwd=self.repo)
        self._commit_all("install crossed manifest pathname")
        self._prepare_result_review("N24c", "n24c-crossed-status")
        (self.repo / "A-leading.txt").write_text("metadata status is M\n", encoding="utf-8")
        self._commit_all("modify A-leading pathname")
        record = self.record("N24c")
        base_tree = self.git(
            "rev-parse", f"{record['base_commit']}^{{tree}}", cwd=self.repo
        )
        head = self.git("rev-parse", "HEAD", cwd=self.repo)
        head_tree = self.git("rev-parse", f"{head}^{{tree}}", cwd=self.repo)
        manifest = subprocess.run(
            ["git", "diff-tree", "-r", "--raw", "--abbrev=40", "-z",
             base_tree, head_tree],
            cwd=self.repo, check=True, stdout=subprocess.PIPE,
        ).stdout
        self.assertIn(b" M\0A-leading.txt\0", manifest)
        self._install_closeout("n24c-crossed-status", delta={
            "base_commit": record["base_commit"],
            "snapshot_tree": head_tree,
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "entries": 1,
            "status_counts": {"A": 1},
        })
        rc, _, stderr = self._run_review_raw(
            "mark-executed", "--dispatch-id", "N24c"
        )
        self.assertEqual(rc, 1)
        self.assertIn("delta_mismatch: derived delta counts disagree", stderr)
        self.assertEqual(self.record("N24c")["state"], "dispatched")

    def _prepare_seven_entry_result_review(
        self, dispatch_id: str, key: str
    ) -> dict[str, str]:
        base_files = {
            "A.txt": "delete crossed-name file\n",
            "rename-old.txt": "rename contents\n",
            "modify.txt": "before\n",
            "type.txt": "regular before typechange\n",
        }
        for name, contents in base_files.items():
            (self.repo / name).write_text(contents, encoding="utf-8")
        self._commit_all("seven-entry fixture base")
        self._prepare_result_review(dispatch_id, key)
        (self.repo / "A.txt").unlink()
        (self.repo / "rename-old.txt").rename(self.repo / "D-renamed.txt")
        (self.repo / "modify.txt").write_text("after\n", encoding="utf-8")
        (self.repo / "type.txt").unlink()
        (self.repo / "type.txt").symlink_to("modify.txt")
        (self.repo / "added-one.txt").write_text("one\n", encoding="utf-8")
        (self.repo / "added-two.txt").write_text("two\n", encoding="utf-8")
        self._commit_all("seven-entry fixture result")
        self._bootstrap_worker_evidence(dispatch_id, "mark-executed")
        return {
            "A.txt": "D",
            "D-renamed.txt": "A",
            "added-one.txt": "A",
            "added-two.txt": "A",
            "modify.txt": "M",
            "rename-old.txt": "D",
            "type.txt": "T",
        }

    def _review_durable_state(self, dispatch_id: str) -> tuple[bytes, tuple, tuple]:
        record = (self.review_root / f"{dispatch_id}.json").read_bytes()
        bindings_dir = self.review_root / "bindings"
        bindings = tuple(
            sorted(
                (path.name, path.read_bytes())
                for path in bindings_dir.iterdir()
            )
        ) if bindings_dir.exists() else ()
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn:
            ledger = tuple(conn.execute(
                "select dispatch_id,status,result,observed_values_json "
                "from dispatch_ledger order by dispatch_id"
            ))
        return record, bindings, ledger

    def _review_with_git_fault(
        self, fault: str
    ) -> tuple[int, str, list[str], tuple, tuple]:
        dispatch_id = f"R5-{fault}"
        self._prepare_seven_entry_result_review(dispatch_id, f"r5-{fault}")
        before = self._review_durable_state(dispatch_id)
        real = subprocess.run
        calls: list[str] = []

        def faulting(command, *args, **kwargs):
            is_raw = "--raw" in command
            is_names = "--name-status" in command
            if is_raw or is_names:
                calls.append("raw" if is_raw else "name_status")
                if fault == "raw_command" and is_raw:
                    raise subprocess.CalledProcessError(2, command)
                if fault == "name_status_command" and is_names:
                    raise subprocess.CalledProcessError(2, command)
                completed = real(command, *args, **kwargs)
                if fault == "malformed_raw" and is_raw:
                    completed.stdout = b"malformed\0"
                elif fault == "malformed_name_status" and is_names:
                    completed.stdout = b"malformed\0"
                elif fault == "disagreement" and is_raw:
                    completed.stdout = completed.stdout.replace(b" A\0", b" M\0", 1)
                return completed
            return real(command, *args, **kwargs)

        with mock.patch.object(subprocess, "run", side_effect=faulting):
            rc, _, stderr = self._run_review_raw(
                "mark-executed", "--dispatch-id", dispatch_id
            )
        return rc, stderr, calls, before, self._review_durable_state(dispatch_id)

    def test_a2_reviewer_g080_mutant_is_rejected_without_transition(self):
        self._prepare_seven_entry_result_review("A2", "a2-reviewer-mutant")
        before = self._review_durable_state("A2")
        self.assertEqual(
            self.record("A2")["state"], "dispatched"
        )
        mutant = lambda _manifest: (
            14, {"M": 2, "A": 4, "D": 4, "r": 2, "T": 1, "t": 1}
        )
        with mock.patch.object(delta_manifest, "parse_raw_z_manifest", mutant):
            rc, _, stderr = self._run_review_raw(
                "mark-executed", "--dispatch-id", "A2"
            )
        self.assertEqual(rc, 1)
        self.assertIn("delta_mismatch: derived delta counts disagree", stderr)
        self.assertEqual(self._review_durable_state("A2"), before)

    def test_a3_unpatched_reviewer_control_agrees(self):
        expected_paths = self._prepare_seven_entry_result_review(
            "A3", "a3-reviewer-control"
        )
        record = self.record("A3")
        base_tree = self.git(
            "rev-parse", f"{record['base_commit']}^{{tree}}", cwd=self.repo
        )
        head_tree = self.git("rev-parse", "HEAD^{tree}", cwd=self.repo)
        stream = subprocess.run(
            ["git", "diff-tree", "-r", "--no-renames", "--name-status",
             "-z", base_tree, head_tree],
            cwd=self.repo, check=True, stdout=subprocess.PIPE,
        ).stdout
        tokens = stream[:-1].split(b"\0")
        actual_paths = {
            tokens[index + 1].decode(): tokens[index].decode()
            for index in range(0, len(tokens), 2)
        }
        self.assertEqual(actual_paths, expected_paths)
        closeout = self.record("A3")
        rc, _, stderr = self._run_review_raw(
            "mark-executed", "--dispatch-id", "A3"
        )
        self.assertEqual((rc, stderr), (0, ""))
        executed = self.record("A3")
        verification = executed["worker_evidence"][-1]["delta_verification"]
        self.assertEqual(
            (verification["entries"], verification["status_counts"]),
            (7, {"A": 3, "D": 2, "M": 1, "T": 1}),
        )

    def test_reviewer_raw_command_failure_is_typed_and_atomic(self):
        rc, stderr, calls, before, after = self._review_with_git_fault(
            "raw_command"
        )
        self.assertEqual(rc, 1)
        self.assertIn("delta_manifest_command_failed", stderr)
        self.assertEqual(calls, ["raw"])
        self.assertEqual(after, before)

    def test_reviewer_name_status_command_failure_is_typed_and_atomic(self):
        rc, stderr, calls, before, after = self._review_with_git_fault(
            "name_status_command"
        )
        self.assertEqual(rc, 1)
        self.assertIn("delta_name_status_command_failed", stderr)
        self.assertEqual(calls, ["raw", "name_status"])
        self.assertEqual(after, before)

    def test_reviewer_malformed_raw_is_typed_and_atomic(self):
        rc, stderr, calls, before, after = self._review_with_git_fault(
            "malformed_raw"
        )
        self.assertEqual(rc, 1)
        self.assertIn("delta_manifest_malformed", stderr)
        self.assertEqual(calls, ["raw", "name_status"])
        self.assertEqual(after, before)

    def test_reviewer_malformed_name_status_is_typed_and_atomic(self):
        rc, stderr, calls, before, after = self._review_with_git_fault(
            "malformed_name_status"
        )
        self.assertEqual(rc, 1)
        self.assertIn("delta_name_status_malformed", stderr)
        self.assertEqual(calls, ["raw", "name_status"])
        self.assertEqual(after, before)

    def test_reviewer_cross_derivation_disagreement_is_typed_and_atomic(self):
        rc, stderr, calls, before, after = self._review_with_git_fault(
            "disagreement"
        )
        self.assertEqual(rc, 1)
        self.assertIn("delta_mismatch: derived delta counts disagree", stderr)
        self.assertEqual(calls, ["raw", "name_status"])
        self.assertEqual(after, before)

    def test_N25_deletion_binding(self):
        self._prepare_result_review("N25", "n25-delete")
        deleted = self.repo / "tracked.txt"
        deleted.unlink()
        delta = self._snapshot_repo_delta()
        self._install_closeout("n25-delete", delta=delta)
        deleted.write_text("restored instead of deleted\n", encoding="utf-8")
        self._commit_all("restore snapshotted deletion")
        rc, _, stderr = self._run_review_raw(
            "mark-executed", "--dispatch-id", "N25"
        )
        self.assertEqual(rc, 1)
        self.assertIn("delta_mismatch", stderr)

    def test_N26_rename_binding(self):
        self._prepare_result_review("N26-good", "n26-good")
        (self.repo / "tracked.txt").rename(self.repo / "renamed.txt")
        delta = self._snapshot_repo_delta()
        self._install_closeout("n26-good", delta=delta)
        self._commit_all("identical snapshotted rename")
        rc, _, stderr = self._run_review_raw(
            "mark-executed", "--dispatch-id", "N26-good"
        )
        self.assertEqual((rc, stderr), (0, ""))
        self.assertEqual(self.record("N26-good")["state"], "executed")

        self._prepare_result_review("N26-bad", "n26-bad")
        (self.repo / "renamed.txt").rename(self.repo / "snapshot-target.txt")
        delta = self._snapshot_repo_delta()
        self._install_closeout("n26-bad", delta=delta)
        (self.repo / "snapshot-target.txt").rename(self.repo / "different-target.txt")
        self._commit_all("different rename target")
        rc, _, stderr = self._run_review_raw(
            "mark-executed", "--dispatch-id", "N26-bad"
        )
        self.assertEqual(rc, 1)
        self.assertIn("delta_mismatch", stderr)

    def test_N27_mode_and_type_binding(self):
        self._prepare_result_review("N27-mode", "n27-mode")
        tracked = self.repo / "tracked.txt"
        tracked.chmod(0o755)
        delta = self._snapshot_repo_delta()
        self._install_closeout("n27-mode", delta=delta)
        self._commit_all("commit snapshotted mode")
        tracked.chmod(0o644)
        self._commit_all("revert snapshotted mode")
        rc, _, stderr = self._run_review_raw(
            "mark-executed", "--dispatch-id", "N27-mode"
        )
        self.assertEqual(rc, 1)
        self.assertIn("delta_mismatch", stderr)

        self._prepare_result_review("N27-type", "n27-type")
        tracked.unlink()
        tracked.symlink_to("different-target.txt")
        delta = self._snapshot_repo_delta()
        self._install_closeout("n27-type", delta=delta)
        tracked.unlink()
        tracked.write_text("regular file instead\n", encoding="utf-8")
        self._commit_all("change snapshotted object type")
        rc, _, stderr = self._run_review_raw(
            "mark-executed", "--dispatch-id", "N27-type"
        )
        self.assertEqual(rc, 1)
        self.assertIn("delta_mismatch", stderr)
    def test_N28_binding_crash_recovery(self):
        bindings = self.review_root / "bindings"
        bindings.mkdir(parents=True)

        def worker_id(serial: int) -> str:
            return f"dispatch_20260722_120000_{serial:08x}"

        def claim(serial: int, record_id: str, state: str = "pending") -> Path:
            path = bindings / worker_id(serial)
            path.write_text(json.dumps({
                "worker_dispatch_id": worker_id(serial),
                "record_id": record_id,
                "state": state,
                "claimed_at": "2026-07-22T12:00:00Z",
            }), encoding="utf-8")
            return path

        self.open_review("N28-orphan")
        orphan = claim(0x28, "N28-orphan")
        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", worker_id(0x28),
        )
        self.assertEqual((rc, stderr), (0, ""))
        self.assertFalse(orphan.exists())
        history = self.record("N28-orphan")["history"]
        self.assertEqual(
            sum(item.get("event") == "recover-binding-orphan-removed"
                for item in history), 1,
        )
        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", worker_id(0x28),
        )
        self.assertEqual(rc, 1)
        self.assertIn("binding_not_found", stderr)

        self._prepare_result_review("N28-evidence", "n28-evidence")
        self._bootstrap_worker_evidence("N28-evidence", "mark-blocked")
        rc, _, stderr = self._run_review_raw(
            "mark-blocked", "--dispatch-id", "N28-evidence",
        )
        self.assertEqual((rc, stderr), (0, ""))
        evidence_id = self._worker_id_for_key("n28-evidence")
        evidence_claim = bindings / evidence_id
        pending = json.loads(evidence_claim.read_text())
        pending.pop("bound_at", None)
        pending["state"] = "pending"
        review._atomic_claim_write(evidence_claim, pending)
        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", evidence_id,
        )
        self.assertEqual((rc, stderr), (0, ""))
        self.assertTrue(evidence_claim.exists(), "evidence-backed claim was removed")
        finalized = json.loads(evidence_claim.read_text(encoding="utf-8"))
        self.assertEqual(finalized["state"], "bound")
        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", evidence_id,
        )
        self.assertEqual((rc, stderr), (0, ""))
        history = self.record("N28-evidence")["history"]
        self.assertEqual(
            sum(item.get("event") == "recover-binding-finalized"
                for item in history), 1,
        )
        self.assertEqual(json.loads(evidence_claim.read_text()), finalized)

        malformed_id = worker_id(0x2A)
        malformed = bindings / malformed_id
        malformed.write_bytes(b"{not-json")
        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", malformed_id,
        )
        self.assertEqual(rc, 1)
        self.assertIn("binding_metadata_malformed", stderr)
        self.assertEqual(malformed.read_bytes(), b"{not-json")
        self.assertFalse((bindings / "quarantine").exists())
        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", malformed_id,
            "--quarantine-malformed",
        )
        self.assertEqual((rc, stderr), (0, ""))
        reservation = json.loads(malformed.read_text(encoding="utf-8"))
        self.assertEqual(reservation["state"], "quarantined")
        self.assertEqual(Path(reservation["forensics"]).read_bytes(), b"{not-json")

        self.open_review("N28-bound")
        bound = claim(0x2B, "N28-bound", "bound")
        before = bound.read_bytes()
        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", worker_id(0x2B),
        )
        self.assertEqual((rc, stderr), (0, ""))
        self.assertEqual(bound.read_bytes(), before)

        invalid = "../dispatch_20260722_120000_0000002c"
        with mock.patch.object(Path, "mkdir",
                               side_effect=AssertionError("path derived")):
            rc, _, stderr = self._run_review_raw(
                "recover-binding", "--worker-dispatch-id", invalid,
            )
        self.assertEqual(rc, 1)
        self.assertIn("invalid_worker_dispatch_id", stderr)

    def test_N29_recovery_waits_for_binder(self):
        bindings = self.review_root / "bindings"
        bindings.mkdir(parents=True)
        lock_path = bindings / ".lock"

        def sigkill_binder(record_id: str, worker_id: str, *, with_evidence: bool) -> Path:
            claim_path = bindings / worker_id
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    record = review.read_record(record_id)
                    evidence = {"row": {"dispatch_id": worker_id}}
                    with review._binding_claim(record, evidence):
                        if with_evidence:
                            record["intended_dispatches"].append({
                                "attempt": 1,
                                "idempotency_key": "n29-finalize",
                                "recorded_at": review.utc_now(),
                            })
                            record.setdefault("blocked_dispatches", []).append(
                                {
                                    "worker_dispatch_id": worker_id,
                                    "intent_attempt": 1,
                                    "idempotency_key": "n29-finalize",
                                    "ledger_db": str(self.ledger_path),
                                    "closeout": {
                                        "protocol": 1,
                                        "recorded_by": "gamma-codex-worker",
                                        "reply_message_id": "msg_n29_finalize",
                                        "delta": None,
                                    },
                                    "note": "SIGKILL recovery fixture",
                                    "verified_at": review.utc_now(),
                                }
                            )
                            record["updated_at"] = review.utc_now()
                            review.persist(review.review_paths(record_id), record)
                        os.write(write_fd, b"R")
                        signal.pause()
                except BaseException as exc:
                    os.write(write_fd, f"E:{exc}".encode())
                finally:
                    os._exit(70)

            os.close(write_fd)
            try:
                ready, _, _ = select.select([read_fd], [], [], 5)
                self.assertTrue(ready, "binder child did not publish readiness")
                child_status = os.read(read_fd, 4096)
                self.assertEqual(child_status, b"R", f"binder child failed: {child_status!r}")
                self.assertEqual(
                    json.loads(claim_path.read_text(encoding="utf-8"))["state"],
                    "pending",
                )
                with lock_path.open("a+") as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.kill(pid, signal.SIGKILL)
                waited_pid, status = os.waitpid(pid, 0)
                self.assertEqual(waited_pid, pid)
                self.assertTrue(os.WIFSIGNALED(status), f"child status was {status}")
                self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
                pid = None
            finally:
                os.close(read_fd)
                if pid is not None:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)

            with lock_path.open("a+") as released:
                fcntl.flock(released.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return claim_path

        orphan_id = "dispatch_20260722_120000_00000029"
        self.open_review("N29-orphan")
        orphan_claim = sigkill_binder("N29-orphan", orphan_id, with_evidence=False)
        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", orphan_id,
        )
        self.assertEqual((rc, stderr), (0, ""))
        self.assertFalse(orphan_claim.exists(), "SIGKILL orphan survived recovery")
        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", orphan_id,
        )
        self.assertEqual(rc, 1)
        self.assertIn("binding_not_found", stderr)
        orphan_history = self.record("N29-orphan")["history"]
        self.assertEqual(
            sum(item.get("event") == "recover-binding-orphan-removed"
                and item.get("worker_dispatch_id") == orphan_id
                for item in orphan_history),
            1,
        )

        finalize_id = "dispatch_20260722_120000_0000002a"
        self.open_review("N29-finalize")
        finalize_claim = sigkill_binder(
            "N29-finalize", finalize_id, with_evidence=True,
        )
        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", finalize_id,
        )
        self.assertEqual((rc, stderr), (0, ""))
        finalized = json.loads(finalize_claim.read_text(encoding="utf-8"))
        self.assertEqual(finalized["state"], "bound")
        self.assertEqual(finalized["worker_dispatch_id"], finalize_id)
        history_after_finalize = self.record("N29-finalize")["history"]
        for _ in range(2):
            rc, _, stderr = self._run_review_raw(
                "recover-binding", "--worker-dispatch-id", finalize_id,
            )
            self.assertEqual((rc, stderr), (0, ""))
        finalize_history = self.record("N29-finalize")["history"]
        self.assertEqual(finalize_history, history_after_finalize)
        self.assertEqual(
            sum(item.get("event") == "recover-binding-finalized"
                and item.get("worker_dispatch_id") == finalize_id
                for item in finalize_history),
            1,
        )
    def test_N30_absent_intent_retry_contract(self):
        self._prepare_result_review("N30", "n30-initial")
        rc, _, stderr = self._run_review_raw(
            "redispatch", "--dispatch-id", "N30", "--note", "not authorized",
            "--idempotency-key", "n30-fresh",
        )
        self.assertEqual(rc, 1)
        self.assertIn("active_dispatch_absent", stderr)
        self.assertEqual(len(self.record("N30")["intended_dispatches"]), 1)

        store = Store(self.ledger_path)
        def retry_initial():
            return store.dispatch_agent(
                "gamma-architect", "gamma-codex-worker", "n30-initial",
                "initial", "initial", [],
            )["dispatch_id"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            dispatch_ids = list(pool.map(lambda _: retry_initial(), range(2)))
        self.assertEqual(len(set(dispatch_ids)), 1)
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.ledger_path)) as conn, conn:
            count = conn.execute(
                "select count(*) from dispatch_ledger where producer_actor_id=? "
                "and idempotency_key=?",
                ("gamma-architect", "n30-initial"),
            ).fetchone()[0]
        self.assertEqual(count, 1)
        self._set_worker_root(str(self.repo))
        rc, _, stderr = self._run_review_raw(
            "redispatch", "--dispatch-id", "N30", "--note", "still not authorized",
            "--idempotency-key", "n30-fresh",
        )
        self.assertEqual(rc, 1)
        self.assertIn("active_dispatch_live", stderr)
        self.assertEqual(len(self.record("N30")["intended_dispatches"]), 1)

    def test_N31_superseded_workflow(self):
        self._prepare_result_review("N31", "n31-round-1")
        self._bootstrap_worker_evidence("N31", "mark-superseded")
        rc, _, stderr = self._run_review_raw(
            "redispatch", "--dispatch-id", "N31", "--note", "bare",
            "--idempotency-key", "n31-bare",
        )
        self.assertEqual(rc, 1)
        self.assertIn("active_dispatch_satisfied", stderr)
        rc, _, stderr = self._run_review_raw(
            "mark-superseded", "--dispatch-id", "N31", "--note", "needs another round",
        )
        self.assertEqual((rc, stderr), (0, ""))
        self.assertEqual(self.record("N31")["state"], "dispatch_superseded")
        rc, _, stderr = self._run_review_raw("mark-executed", "--dispatch-id", "N31")
        self.assertEqual(rc, 1)
        self.assertIn("state dispatch_superseded not allowed", stderr)

        for round_number in range(2, 5):
            self.run_review(
                "redispatch", "--dispatch-id", "N31",
                "--note", f"round {round_number}",
                "--idempotency-key", f"n31-round-{round_number}",
            )
            self.assertEqual(self.record("N31")["state"], "dispatched")
            self.run_review(
                "mark-superseded", "--dispatch-id", "N31",
                "--note", f"superseded {round_number}",
            )
        self.assertEqual(self.record("N31")["blocked_redispatch_count"], 3)
        self.run_review(
            "redispatch", "--dispatch-id", "N31", "--note", "over cap",
            "--idempotency-key", "n31-round-5",
        )
        final = self.record("N31")
        self.assertEqual(final["state"], "escalated")
        self.assertEqual(final["blocked_redispatch_count"], 3)
    def test_N32_snapshot_store_isolation(self):
        self._prepare_result_review("N32", "n32-isolation")
        tracked = self.repo / "tracked.txt"
        tracked.write_text("snapshot mutation\n", encoding="utf-8")
        (self.repo / "included-untracked.txt").write_text("included\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (self.repo / "ignored.txt").write_text("excluded\n", encoding="utf-8")
        executable = self.repo / "executable-untracked"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        (self.repo / "untracked-link").symlink_to("included-untracked.txt")
        staged = self.repo / "staged.txt"
        staged.write_bytes(b"staged bytes\n")
        self.git("add", "staged.txt", cwd=self.repo)

        def worktree_manifest() -> list[tuple]:
            entries = []
            for path in sorted(self.repo.rglob("*")):
                relative = path.relative_to(self.repo)
                if relative.parts[0] == ".git":
                    continue
                stat = path.lstat()
                mode = stat.st_mode
                if path.is_symlink():
                    target = os.readlink(path)
                    payload = os.fsencode(target)
                    kind = "symlink"
                elif path.is_file():
                    payload = path.read_bytes()
                    target = None
                    kind = "file"
                else:
                    payload = b""
                    target = None
                    kind = "directory"
                entries.append((
                    relative.as_posix(), kind, mode, bool(mode & 0o111), target,
                    len(payload), hashlib.sha256(payload).hexdigest(),
                ))
            return entries

        git_dir = Path(self.git("rev-parse", "--absolute-git-dir", cwd=self.repo))
        common_dir = Path(self.git(
            "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.repo
        ))
        self.assertNotEqual(git_dir, common_dir)
        self.assertTrue((self.repo / ".git").is_file())

        def administrative_state() -> dict:
            return {
                "head": self.git("rev-parse", "HEAD", cwd=self.repo),
                "head_file": (git_dir / "HEAD").read_bytes(),
                "refs": self.git("show-ref", cwd=self.repo),
                "status": self.git(
                    "status", "--porcelain=v1", "-uall", cwd=self.repo
                ),
                "index": hashlib.sha256((git_dir / "index").read_bytes()).hexdigest(),
                "objects": sorted(
                    (
                        str(path.relative_to(common_dir / "objects")),
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
                    for path in (common_dir / "objects").rglob("*") if path.is_file()
                ),
                "manifest": worktree_manifest(),
            }

        before = administrative_state()
        write_tree_env = {}
        real_run = git_evidence.run_git_fchdir

        def capture_snapshot_env(worktree_fd, args, env, *rest, **kwargs):
            if list(args)[:1] == ["write-tree"]:
                write_tree_env.update(env)
            return real_run(worktree_fd, args, env, *rest, **kwargs)

        with mock.patch(
            "agent_comms.reviewing.git_evidence.run_git_fchdir",
            side_effect=capture_snapshot_env,
        ):
            delta = self._snapshot_repo_delta()
        after = administrative_state()
        self.assertEqual(
            after["manifest"], before["manifest"],
            "snapshot changed byte-faithful worktree manifest",
        )
        self.assertEqual(
            {key: value for key, value in after.items() if key != "manifest"},
            {key: value for key, value in before.items() if key != "manifest"},
            "snapshot changed linked-worktree git administrative state",
        )

        temp_index = Path(write_tree_env["GIT_INDEX_FILE"])
        temp_objects = Path(write_tree_env["GIT_OBJECT_DIRECTORY"])
        alternate_objects = Path(
            write_tree_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"]
        )
        self.assertTrue(temp_index.is_absolute())
        self.assertTrue(temp_objects.is_absolute())
        self.assertFalse(temp_index.is_relative_to(self.repo))
        self.assertFalse(temp_objects.is_relative_to(self.repo))
        self.assertFalse(
            temp_index.is_relative_to(common_dir),
            "snapshot index is inside real git common dir",
        )
        self.assertFalse(temp_objects.is_relative_to(common_dir))
        self.assertEqual(alternate_objects, common_dir / "objects")
        self.assertFalse(temp_index.exists())
        self.assertFalse(temp_objects.exists())

        self.assertNotIn("ignored.txt", after["status"])
        self.assertIn("included-untracked.txt", after["status"])
        self.assertIn("staged.txt", after["status"])
        self.assertNotIn(delta["snapshot_tree"], set(self.git(
            "cat-file", "--batch-all-objects", "--batch-check=%(objectname)",
            cwd=self.repo,
        ).splitlines()))

        self._install_closeout("n32-isolation", delta=delta)
        self._commit_all("commit isolated snapshot exactly")
        rc, _, stderr = self._run_review_raw(
            "mark-executed", "--dispatch-id", "N32"
        )
        self.assertEqual((rc, stderr), (0, ""))
        evidence = self.record("N32")["worker_evidence"][0]["delta_verification"]
        self.assertEqual(evidence["snapshot_tree"], delta["snapshot_tree"])
    def test_N33_quarantine_reserves_claim(self):
        bindings = self.review_root / "bindings"
        bindings.mkdir(parents=True)
        worker_id = "dispatch_20260722_120000_00000033"
        claim_path = bindings / worker_id
        malformed = b"\x00malformed forensic claim\n"
        claim_path.write_bytes(malformed)

        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", worker_id,
            "--quarantine-malformed",
        )
        self.assertEqual((rc, stderr), (0, ""))
        reservation = json.loads(claim_path.read_text(encoding="utf-8"))
        self.assertEqual(reservation["worker_dispatch_id"], worker_id)
        self.assertEqual(reservation["state"], "quarantined")
        forensic = Path(reservation["forensics"])
        self.assertEqual(forensic.read_bytes(), malformed)

        evidence = {"row": {"dispatch_id": worker_id}}
        with self.assertRaisesRegex(review.ReviewError, "evidence_quarantined"):
            with review._binding_claim({"dispatch_id": "N33-binder"}, evidence):
                self.fail("quarantined id was rebound")
        claim_path.write_bytes(json.dumps(reservation).encode())
        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", worker_id,
            "--quarantine-malformed",
        )
        self.assertEqual((rc, stderr), (0, ""))
        self.assertEqual(json.loads(claim_path.read_text()), reservation)
        self.assertEqual(forensic.read_bytes(), malformed)

        crash_id = "dispatch_20260722_120000_00000034"
        crash_path = bindings / crash_id
        crash_path.write_bytes(b"crash-forensics")
        real_atomic_write = review._atomic_claim_write

        def crash_before_reservation(path, payload):
            if path == crash_path and payload.get("state") == "quarantined":
                raise RuntimeError("simulated crash before reservation")
            return real_atomic_write(path, payload)

        with mock.patch.object(
            ledger_evidence, "_atomic_claim_write", side_effect=crash_before_reservation
        ):
            with self.assertRaisesRegex(
                RuntimeError, "simulated crash before reservation"
            ):
                review.command_recover_binding(argparse.Namespace(
                    worker_dispatch_id=crash_id,
                    quarantine_malformed=True,
                ))
        self.assertTrue(crash_path.exists(), "crash freed quarantined id")
        with self.assertRaisesRegex(review.ReviewError,
                                    "binding_metadata_malformed"):
            with review._binding_claim(
                {"dispatch_id": "N33-competitor"},
                {"row": {"dispatch_id": crash_id}},
            ):
                self.fail("competitor rebound crash-reserved id")
        rc, _, stderr = self._run_review_raw(
            "recover-binding", "--worker-dispatch-id", crash_id,
            "--quarantine-malformed",
        )
        self.assertEqual((rc, stderr), (0, ""))
        crash_reservation = json.loads(crash_path.read_text())
        self.assertEqual(crash_reservation["state"], "quarantined")
        self.assertEqual(
            Path(crash_reservation["forensics"]).read_bytes(),
            b"crash-forensics",
        )


if __name__ == "__main__":
    unittest.main()
