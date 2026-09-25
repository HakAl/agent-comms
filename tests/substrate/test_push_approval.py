import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import hashlib
import importlib.util
import io
import json
import importlib.machinery
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import delta_manifest, push_approval, review
from agent_comms.reviewing import store


class PushApprovalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.records = self.root / "push-approvals"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test User")
        (self.repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "initial")
        self.push_root_patch = mock.patch.object(push_approval, "PUSH_APPROVAL_ROOT", self.records)
        self.push_root_patch.start()
        self.addCleanup(self.push_root_patch.stop)

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

    def run_push_approval(self, *args: str, ok: bool = True) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = push_approval.main(args)
        if ok and rc != 0:
            self.fail(f"push-approval failed: {args}\nstdout:\n{stdout.getvalue()}\nstderr:\n{stderr.getvalue()}")
        if not ok and rc == 0:
            self.fail(f"push-approval unexpectedly succeeded: {args}")
        return rc, stdout.getvalue(), stderr.getvalue()

    def generate_approval_key(self, name: str = "approval-key") -> Path:
        key_path = self.root / name
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path), "-q"], check=True, stdout=subprocess.PIPE)
        return key_path

    def commit_signers(self, key_path: Path | None = None) -> Path:
        key_path = key_path or self.generate_approval_key()
        self.git("checkout", "-B", review.APPROVAL_INTEGRATION_REF)
        signers = self.repo / "config" / "approval-signers"
        signers.parent.mkdir(parents=True, exist_ok=True)
        signers.write_text(f"agent-comms-approver {(key_path.with_suffix('.pub')).read_text(encoding='utf-8')}", encoding="utf-8")
        self.git("add", "config/approval-signers")
        self.git("commit", "-m", "approval signers")
        self.git("checkout", "-B", "work")
        patch = mock.patch.object(review, "REPO_ROOT", self.repo)
        patch.start()
        self.addCleanup(patch.stop)
        main_patch = mock.patch.dict(
            os.environ,
            {"AGENT_COMMS_MAIN": str(self.repo), "AGENT_COMMS_APPROVAL_SIGNERS_REPO": str(self.repo)},
        )
        main_patch.start()
        self.addCleanup(main_patch.stop)
        return key_path

    def sign_push_payload(
        self,
        key_path: Path,
        *,
        head: str | None = None,
        target_ref: str = "refs/heads/work",
        repo_identity: str = "https://example.invalid/repo.git",
        approver: str = "human",
        approved_at: str = "2026-06-13T01:31:08Z",
        namespace: str = push_approval.PUSH_APPROVAL_NAMESPACE,
    ) -> tuple[bytes, str]:
        payload = push_approval.push_approval_payload(head or self.git("rev-parse", "HEAD"), target_ref, repo_identity, approver, approved_at)
        return payload, review.sign_approval_payload(payload, key_path, namespace=namespace)

    def write_push_record(self, key_path: Path, **overrides: object) -> Path:
        head = str(overrides.pop("approved_head", self.git("rev-parse", "HEAD")))
        target_ref = str(overrides.pop("target_ref", "refs/heads/work"))
        repo_identity = str(overrides.pop("repo_identity", "https://example.invalid/repo.git"))
        approver = str(overrides.pop("approver", "human"))
        approved_at = str(overrides.pop("approved_at", "2026-06-13T01:31:08Z"))
        _payload, signature = self.sign_push_payload(
            key_path,
            head=head,
            target_ref=target_ref,
            repo_identity=repo_identity,
            approver=approver,
            approved_at=approved_at,
        )
        record = {
            "schema_version": 1,
            "kind": "push-approval",
            "approved_head": head,
            "target_ref": target_ref,
            "repo_identity": repo_identity,
            "approver": approver,
            "approved_at": approved_at,
            "signature": signature,
            "consensus_refs": ["msg_1"],
            "note": "reviewed",
        }
        record.update(overrides)
        self.records.mkdir(parents=True, exist_ok=True)
        path = self.records / f"{push_approval.record_id(approved_at, head)}.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def test_t1_payload_validation_and_golden(self) -> None:
        head = "a" * 40
        payload = push_approval.push_approval_payload(head, "refs/heads/main", "git@example.invalid:x/repo.git", "human", "2026-06-13T01:31:08Z")
        self.assertEqual(
            payload,
            (
                "agent-comms-push-approval-v1\n"
                f"approved_head={head}\n"
                "target_ref=refs/heads/main\n"
                "repo_identity=git@example.invalid:x/repo.git\n"
                "approver=human\n"
                "approved_at=2026-06-13T01:31:08Z\n"
            ).encode("utf-8"),
        )
        for bad_head in ("a" * 39, "g" * 40, "A" * 40):
            with self.assertRaises(review.ReviewError):
                push_approval.push_approval_payload(bad_head, "refs/heads/main", "url", "human", "now")
        with self.assertRaises(review.ReviewError):
            push_approval.push_approval_payload(head, "heads/main", "url", "human", "now")
        for kwargs in (
            {"repo_identity": ""},
            {"approver": "hu\nman"},
            {"approved_at": "now\r"},
        ):
            fields = {"repo_identity": "url", "approver": "human", "approved_at": "now"}
            fields.update(kwargs)
            with self.assertRaises(review.ReviewError):
                push_approval.push_approval_payload(head, "refs/heads/main", fields["repo_identity"], fields["approver"], fields["approved_at"])

    def test_t2_create_verify_roundtrip_and_required_bindings(self) -> None:
        key_path = self.commit_signers()
        head = self.git("rev-parse", "HEAD")
        with mock.patch.object(review, "read_tty_confirmation", return_value="APPROVE"), mock.patch.object(
            push_approval, "read_tty_confirmation", review.read_tty_confirmation
        ), mock.patch.object(push_approval, "utc_now", return_value="2026-06-13T01:31:08Z"):
            _rc, stdout, _stderr = self.run_push_approval(
                "create",
                "--head",
                head,
                "--target-ref",
                "refs/heads/work",
                "--repo-identity",
                "https://example.invalid/repo.git",
                "--approver",
                "human",
                "--key",
                str(key_path),
            )
        record = json.loads(stdout)
        path = self.records / f"{push_approval.record_id(record['approved_at'], head)}.json"
        self.assertTrue(path.exists())
        self.run_push_approval(
            "verify",
            "--record",
            str(path),
            "--head",
            head,
            "--target-ref",
            "refs/heads/work",
            "--repo-identity",
            "https://example.invalid/repo.git",
        )
        for missing in ("--head", "--target-ref", "--repo-identity"):
            args = [
                "verify",
                "--record",
                str(path),
                "--head",
                head,
                "--target-ref",
                "refs/heads/work",
                "--repo-identity",
                "https://example.invalid/repo.git",
            ]
            index = args.index(missing)
            del args[index : index + 2]
            with self.assertRaises(SystemExit):
                push_approval.main(args)

    def test_t3_mutual_exclusion_namespace_version_and_kind_wall(self) -> None:
        key_path = self.commit_signers()
        head = self.git("rev-parse", "HEAD")
        cycle_payload = review.approval_payload(
            "D1",
            head,
            "b" * 64,
            "local-worktree-v1:/srv/integration",
            "refs/heads/work",
        )
        cycle_sig = review.sign_approval_payload(cycle_payload, key_path)
        path = self.records / "push_20260613_013108_cycle.json"
        self.records.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "push-approval",
                    "approved_head": head,
                    "target_ref": "refs/heads/work",
                    "repo_identity": "https://example.invalid/repo.git",
                    "approver": "human",
                    "approved_at": "2026-06-13T01:31:08Z",
                    "signature": cycle_sig,
                    "consensus_refs": [],
                    "note": "",
                }
            ),
            encoding="utf-8",
        )
        _rc, _out, stderr = self.run_push_approval(
            "verify",
            "--record",
            str(path),
            "--head",
            head,
            "--target-ref",
            "refs/heads/work",
            "--repo-identity",
            "https://example.invalid/repo.git",
            ok=False,
        )
        self.assertIn("push approval signature verification failed", stderr)

        _payload, push_sig = self.sign_push_payload(key_path, head=head)
        review_record = {"dispatch_id": "D1", "approved_head": head, "brief_sha256": "b" * 64, "approval": {"signature": push_sig}}
        with self.assertRaises(review.ReviewError):
            review.verify_approval_signature(review_record)
        review_root = self.root / "reviews"
        review_root.mkdir()
        base_commit = self.git("rev-parse", f"{head}^")
        base_tree = self.git("rev-parse", f"{base_commit}^{{tree}}")
        reviewed_head_tree = self.git("rev-parse", f"{head}^{{tree}}")
        manifest = subprocess.run(
            ["git", "diff-tree", "-r", "--no-renames", "--raw", "--abbrev=40", "-z", base_tree, reviewed_head_tree],
            cwd=self.repo,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        name_status = subprocess.run(
            ["git", "diff-tree", "-r", "--no-renames", "--name-status", "-z",
             base_tree, reviewed_head_tree],
            cwd=self.repo, stdout=subprocess.PIPE, check=True,
        ).stdout
        entries, status_counts = delta_manifest.parse_and_crosscheck(
            manifest, name_status
        )
        full_review_record = {
            "schema_version": review.SCHEMA_VERSION,
            "dispatch_id": "D1",
            "state": "human_approved",
            "repo": str(self.repo),
            "base_commit": base_commit,
            "target_branch": "work",
            "expected_producer": "gamma-architect",
            "expected_recipient": "gamma-codex-worker",
            "intended_dispatches": [{
                "attempt": 1,
                "idempotency_key": "push-approval-fixture-D1",
                "recorded_at": "2026-06-13T01:31:08Z",
            }],
            "worker_evidence": [{
                "worker_dispatch_id": "dispatch_20260613_013108_00000001",
                "intent_attempt": 1,
                "idempotency_key": "push-approval-fixture-D1",
                "ledger_db": str(self.root / "ledger.sqlite"),
                "closeout": {
                    "protocol": 1,
                    "recorded_by": "gamma-codex-worker",
                    "reply_message_id": "msg_20260613_013108_fixture_reply",
                    "delta": {
                        "base_commit": base_commit,
                        "snapshot_tree": reviewed_head_tree,
                        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                        "entries": entries,
                        "status_counts": status_counts,
                    },
                    "result": "satisfied",
                    "summary": "fixture execution completed",
                    "recorded_at": "2026-06-13T01:31:08Z",
                },
                "verified_at": "2026-06-13T01:31:08Z",
                "producer": "gamma-architect",
                "recipient": "gamma-codex-worker",
                "status": "closed",
                "result": "satisfied",
                "delta_verification": {
                    "snapshot_tree": reviewed_head_tree,
                    "reviewed_head_tree": reviewed_head_tree,
                    "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                    "entries": entries,
                    "status_counts": status_counts,
                },
                "artifact_bindings": [],
            }],
            "blocked_dispatches": [],
            "superseded_dispatches": [],
            "blocked_redispatch_count": 0,
            "max_blocked_redispatches": 0,
            "brief_path": str(self.root / "brief.md"),
            "brief_sha256": "b" * 64,
            "brief_revision": 1,
            "brief_checks": [],
            "dod": [],
            "findings": [],
            "gate_runs": [],
            "skips": [],
            "history": [],
            "reviewed_head": head,
            "approved_head": head,
            "approval": {
                "approver": "human",
                "mechanism": "ssh-keygen",
                # A well-formed v2 destination binding so gate-merge reaches
                # signature verification: the push-namespace signature must then
                # fail the cycle signature check (the cross-kind wall), not merely
                # be rejected as an unbound legacy approval.
                "payload_version": review.CYCLE_PAYLOAD_VERSION,
                "repo_identity": "local-worktree-v1:/srv/integration",
                "target_ref": "refs/heads/work",
                "signature": push_sig,
            },
            "respawn_count": 0,
            "max_respawns": 0,
            "trigger_closed": True,
            "created_at": "2026-06-13T01:31:08Z",
            "updated_at": "2026-06-13T01:31:08Z",
        }
        (review_root / "D1.json").write_text(json.dumps(full_review_record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with mock.patch.object(store, "REVIEW_ROOT", review_root):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = review.main(["gate-merge", "--dispatch-id", "D1"])
            self.assertNotEqual(rc, 0)
            self.assertIn("approval signature verification failed", stderr.getvalue())

        push_payload, _sig = self.sign_push_payload(key_path, head=head, namespace=review.APPROVAL_NAMESPACE)
        with self.assertRaises(review.ReviewError):
            review.verify_approval_payload_signature(push_payload, review.sign_approval_payload(push_payload, key_path), namespace=push_approval.PUSH_APPROVAL_NAMESPACE)
        with self.assertRaises(review.ReviewError):
            review.verify_approval_payload_signature(cycle_payload, review.sign_approval_payload(cycle_payload, key_path, namespace=push_approval.PUSH_APPROVAL_NAMESPACE))

        wrong_kind = self.records / "push_20260613_013109_wrong.json"
        wrong_kind.write_text(json.dumps({"kind": "cycle-review", "signature": "not checked"}), encoding="utf-8")
        _rc, _out, stderr = self.run_push_approval(
            "verify",
            "--record",
            str(wrong_kind),
            "--head",
            head,
            "--target-ref",
            "refs/heads/work",
            "--repo-identity",
            "https://example.invalid/repo.git",
            ok=False,
        )
        self.assertIn("wrong kind", stderr)
        self.assertNotIn("signature verification", stderr)

    def test_t4_bindings_replay_and_advisory_tamper(self) -> None:
        key_path = self.commit_signers()
        head = self.git("rev-parse", "HEAD")
        path = self.write_push_record(key_path, approved_head=head)
        self.run_push_approval("verify", "--record", str(path), "--head", head, "--target-ref", "refs/heads/work", "--repo-identity", "https://example.invalid/repo.git")
        for args, needle in (
            (["--head", "b" * 40, "--target-ref", "refs/heads/work", "--repo-identity", "https://example.invalid/repo.git"], "approved_head mismatch"),
            (["--head", head, "--target-ref", "refs/heads/main", "--repo-identity", "https://example.invalid/repo.git"], "target_ref mismatch"),
            (["--head", head, "--target-ref", "refs/heads/work", "--repo-identity", "ssh://other"], "repo_identity mismatch"),
        ):
            _rc, _out, stderr = self.run_push_approval("verify", "--record", str(path), *args, ok=False)
            self.assertIn(needle, stderr)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["consensus_refs"] = ["msg_tampered"]
        record["note"] = "tampered"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.run_push_approval("verify", "--record", str(path), "--head", head, "--target-ref", "refs/heads/work", "--repo-identity", "https://example.invalid/repo.git")

    def test_t5_write_once_collision_refuses(self) -> None:
        key_path = self.commit_signers()
        head = self.git("rev-parse", "HEAD")
        with mock.patch.object(push_approval, "read_tty_confirmation", return_value="APPROVE"), mock.patch.object(
            push_approval, "utc_now", return_value="2026-06-13T01:31:08Z"
        ):
            self.run_push_approval("create", "--head", head, "--target-ref", "refs/heads/work", "--repo-identity", "https://example.invalid/repo.git", "--approver", "human", "--key", str(key_path))
            _rc, _out, stderr = self.run_push_approval(
                "create",
                "--head",
                head,
                "--target-ref",
                "refs/heads/work",
                "--repo-identity",
                "https://example.invalid/repo.git",
                "--approver",
                "human",
                "--key",
                str(key_path),
                ok=False,
            )
        self.assertIn("already exists", stderr)
        self.assertEqual(len(list(self.records.glob("push_*.json"))), 1)

    def test_t6_guarded_push_end_to_end_scan_and_detached_head(self) -> None:
        key_path = self.commit_signers()
        origin = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", str(origin)], check=True, stdout=subprocess.PIPE)
        clone = self.root / "clone"
        subprocess.run(["git", "clone", str(origin), str(clone)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.git("config", "user.email", "test@example.invalid", cwd=clone)
        self.git("config", "user.name", "Test User", cwd=clone)
        self.git("checkout", "-B", "feature", cwd=clone)
        (clone / "tracked.txt").write_text("feature\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=clone)
        self.git("commit", "-m", "feature", cwd=clone)
        head = self.git("rev-parse", "refs/heads/feature", cwd=clone)
        self.write_push_record(key_path, approved_head="0" * 40, target_ref="refs/heads/stale", repo_identity=str(origin), approved_at="2026-06-13T01:31:07Z")
        (self.records / "push_20260613_013106_bad.json").write_text("{bad", encoding="utf-8")
        (self.records / "push_20260613_013104_binary.json").write_bytes(b"\xff\xfe\x00\x01 not utf8")
        (self.records / "push_20260613_013105_foreign.json").write_text(json.dumps({"kind": "cycle-review"}), encoding="utf-8")
        loader = importlib.machinery.SourceFileLoader("guarded_push", str(Path(__file__).parents[2] / "scripts" / "guarded-push"))
        spec = importlib.util.spec_from_loader("guarded_push", loader)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        guarded_push = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guarded_push)

        with mock.patch.object(push_approval, "PUSH_APPROVAL_ROOT", self.records):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = guarded_push.main(["--repo", str(clone), "--remote", "origin", "--ref", "refs/heads/feature"])
            self.assertNotEqual(rc, 0)
            self.assertIn("no matching push approval record among", stderr.getvalue())
            self.assertIn("candidates", stderr.getvalue())
            self.assertIn("push_20260613_013104_binary.json: push approval record is unreadable", stderr.getvalue())
            absent = subprocess.run(["git", "show-ref", "--verify", "refs/heads/feature"], cwd=origin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(absent.returncode, 0)

            self.write_push_record(key_path, approved_head=head, target_ref="refs/heads/feature", repo_identity=str(origin), approved_at="2026-06-13T01:31:09Z")
            rc = guarded_push.main(["--repo", str(clone), "--remote", "origin", "--ref", "refs/heads/feature"])
            self.assertEqual(rc, 0)
            self.assertEqual(self.git("rev-parse", "refs/heads/feature", cwd=origin), head)

            self.git("checkout", "--detach", "HEAD", cwd=clone)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = guarded_push.main(["--repo", str(clone), "--remote", "origin"])
            self.assertNotEqual(rc, 0)
            self.assertIn("detached HEAD", stderr.getvalue())

    def test_t7_confirmation_refusal_writes_nothing(self) -> None:
        key_path = self.commit_signers()
        head = self.git("rev-parse", "HEAD")
        with mock.patch.object(push_approval, "read_tty_confirmation", return_value="NO"):
            self.run_push_approval("create", "--head", head, "--target-ref", "refs/heads/work", "--repo-identity", "https://example.invalid/repo.git", "--approver", "human", "--key", str(key_path), ok=False)
        self.assertFalse(self.records.exists())

    def test_t8_existing_review_path_still_uses_cycle_namespace(self) -> None:
        key_path = self.commit_signers()
        head = self.git("rev-parse", "HEAD")
        repo_identity = "local-worktree-v1:/srv/integration"
        target_ref = "refs/heads/work"
        payload = review.approval_payload(
            "D1", head, "b" * 64, repo_identity, target_ref
        )
        record = {
            "dispatch_id": "D1",
            "approved_head": head,
            "brief_sha256": "b" * 64,
            "approval": {
                "payload_version": review.CYCLE_PAYLOAD_VERSION,
                "repo_identity": repo_identity,
                "target_ref": target_ref,
                "signature": review.sign_approval_payload(payload, key_path),
            },
        }
        review.verify_approval_signature(record)
