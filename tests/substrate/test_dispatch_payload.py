"""Dispatch payload transport proof families (design §9 of
docs/dispatch/dispatch_payload_artifacts.md).

Every family from the governing design has a durable discovered test here:
exact-byte round trip, marker compatibility, MCP/admin input compatibility and
size boundaries, zero-write refusals, publication/SQL fault injection with
pre-rollback blob cleanup, existing-blob re-verification, replay without
source access, concurrent same-key and different-key/same-bytes behaviour,
typed adapter-zero preflight failures on immediate/queued/retry starts,
read-time corruption leaving the recipient copy ``sent``, authorization and
no-leak guarantees, legacy inline compatibility, the deliberate
compatibility-floor advance to ``user_version == 3`` (prior-version readers
refuse a payload-capable default ledger), the read-only ``payload-audit``
CLI, and the fail-closed skewed-restore fixture.

Negative fixtures are exercised directly (corrupted, replaced, truncated,
enlarged, missing, and wrong-type blobs; hostile paths; unstable sources), so
a green run includes the discrimination evidence the design requires.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import db as db_module
from agent_comms import payload
from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.mailbox import Mailbox
from agent_comms.schema import ValidationError
from agent_comms.store import Store, WORKER_DISPATCH_POLICY

ROOT = Path(__file__).resolve().parents[2]
HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"

# Distinctive exact payload: non-ASCII text, an astral-plane character, and a
# final newline that must survive byte-for-byte.
PAYLOAD_TEXT = "Exact payload: café über 🐍 naïve\nsecond line with trailing newline\n"
PAYLOAD_BYTES = PAYLOAD_TEXT.encode("utf-8")
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD_BYTES).hexdigest()
# A substring that could only come from the payload text itself.
LEAK_CANARY = "café über 🐍"


class CountingAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.contexts: list[DispatchContext] = []

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        self.contexts.append(context)
        if self.fail:
            raise RuntimeError("adapter spawn failed")
        return DispatchStart(
            spawn_handle=f"fake:{context.recipient['id']}:{context.dispatch['dispatch_id']}",
            observed_values={"adapter": "fake"},
        )

    def halt(self, spawn_handle: str, observed_values=None) -> None:
        return None


class PayloadHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        self.store.register_actor(HUMAN_ID, "human", "alice")
        self.store.register_agent_actor(
            "alpha-architect", "alpha", "architect", str(self.root / "alpha-architect"), []
        )
        self.store.register_agent_actor(
            "alpha-worker", "alpha", "worker", str(self.root / "alpha-worker"), [],
            owner="alpha-architect",
        )
        self.store.register_agent_actor(
            "echo-architect", "echo", "architect", str(self.root / "echo-architect"), []
        )
        self.store.register_agent_actor(
            "echo-worker", "echo", "worker", str(self.root / "echo-worker"), [],
            owner="echo-architect",
        )
        self.producer_root = self.root / "alpha-architect"
        self.producer_root.mkdir(exist_ok=True)
        self.worker_root = self.root / "alpha-worker"
        self.store_root = payload.store_root_for_db(self.db_path)

    def write_source(self, rel: str, data: bytes = PAYLOAD_BYTES) -> Path:
        path = self.producer_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def dispatch_file(
        self,
        key: str,
        rel: str = "brief.md",
        *,
        origin: str = "authored_brief",
        adapter: CountingAdapter | None = None,
        producer: str = "alpha-architect",
        target: str = "alpha-worker",
        source_root: str | None = None,
        override_reason: str | None = None,
    ) -> dict:
        return self.store.dispatch_agent(
            producer,
            target,
            key,
            f"Subject {key}",
            body_file=rel,
            payload_origin=origin,
            source_root=source_root,
            override_reason=override_reason,
            adapter_for_runtime=(lambda _runtime: adapter) if adapter is not None else None,
        )

    def blob_path(self, sha256_hex: str = PAYLOAD_SHA256) -> Path:
        return payload.blob_path(self.store_root, sha256_hex)

    def payload_ref(self, dispatch_id: str) -> sqlite3.Row | None:
        with self.store.connection() as conn:
            return conn.execute(
                "select * from dispatch_payload_refs where dispatch_id = ?", (dispatch_id,)
            ).fetchone()

    def recipient_status(self, message_id: str, actor: str = "alpha-worker") -> str:
        with self.store.connection() as conn:
            return conn.execute(
                "select status from message_recipients where message_id = ? and to_agent = ?",
                (message_id, actor),
            ).fetchone()["status"]

    def counts(self) -> dict:
        with self.store.connection() as conn:
            return {
                table: conn.execute(f"select count(*) as c from {table}").fetchone()["c"]
                for table in (
                    "messages",
                    "message_recipients",
                    "message_threads",
                    "dispatch_ledger",
                    "dispatch_payload_refs",
                )
            }

    def blob_files(self) -> list[Path]:
        blobs_root = self.store_root / "blobs" / "sha256"
        if not blobs_root.is_dir():
            return []
        return sorted(p for p in blobs_root.rglob("*") if p.is_file())

    def staging_files(self) -> list[Path]:
        staging = self.store_root / "staging"
        if not staging.is_dir():
            return []
        return sorted(staging.iterdir())

    def assert_zero_dispatch_writes(self) -> None:
        self.assertEqual(
            self.counts(),
            {
                "messages": 0,
                "message_recipients": 0,
                "message_threads": 0,
                "dispatch_ledger": 0,
                "dispatch_payload_refs": 0,
            },
        )
        semaphore = self.worker_root / ".agent-comms" / "alpha-worker" / "new_messages"
        self.assertFalse(semaphore.exists(), "refused dispatch must not write a semaphore")
        self.assertEqual(self.blob_files(), [])
        self.assertEqual(self.staging_files(), [])

    def corrupt_blob(self, data: bytes, sha256_hex: str = PAYLOAD_SHA256) -> None:
        blob = self.blob_path(sha256_hex)
        blob.write_bytes(data)

    def refusal(self, code: str, callable_, *args, **kwargs) -> ValidationError:
        with self.assertRaises(ValidationError) as caught:
            callable_(*args, **kwargs)
        self.assertTrue(
            str(caught.exception).startswith(code),
            f"expected failure code {code}, got: {caught.exception}",
        )
        return caught.exception


class RoundTripTest(PayloadHarness):
    """§9.1 exact bytes survive file -> blob -> SQL -> read_message."""

    def test_exact_bytes_survive_roundtrip(self) -> None:
        self.write_source("brief.md")
        dispatch = self.dispatch_file("rt-1")
        self.assertEqual(dispatch["status"], "queued")

        blob = self.blob_path()
        self.assertTrue(blob.is_file())
        self.assertEqual(blob.read_bytes(), PAYLOAD_BYTES)

        ref = self.payload_ref(dispatch["dispatch_id"])
        self.assertIsNotNone(ref)
        self.assertEqual(ref["storage_kind"], "sha256_utf8_v1")
        self.assertEqual(ref["payload_origin"], "authored_brief")
        self.assertEqual(ref["payload_sha256"], PAYLOAD_SHA256)
        self.assertEqual(ref["byte_count"], len(PAYLOAD_BYTES))
        self.assertEqual(ref["char_count"], len(PAYLOAD_TEXT))

        message = self.store.read_message("alpha-worker", dispatch["message_id"])
        self.assertEqual(message["body"], PAYLOAD_TEXT)
        self.assertEqual(message["body_storage"], "artifact")
        self.assertEqual(message["body_sha256"], PAYLOAD_SHA256)
        self.assertEqual(message["body_bytes"], len(PAYLOAD_BYTES))
        self.assertEqual(message["body_chars"], len(PAYLOAD_TEXT))
        self.assertEqual(self.recipient_status(dispatch["message_id"]), "read")

    def test_source_mutation_after_capture_cannot_alter_dispatch(self) -> None:
        source = self.write_source("brief.md")
        dispatch = self.dispatch_file("rt-2")
        source.write_bytes(b"edited afterwards\n")
        message = self.store.read_message("alpha-worker", dispatch["message_id"])
        self.assertEqual(message["body"], PAYLOAD_TEXT)


class MarkerTest(PayloadHarness):
    """§9.2 messages.body is the fixed marker and holds no payload text."""

    def test_messages_body_is_fixed_marker(self) -> None:
        self.write_source("brief.md")
        dispatch = self.dispatch_file("marker-1")
        with self.store.connection() as conn:
            stored_body = conn.execute(
                "select body from messages where id = ?", (dispatch["message_id"],)
            ).fetchone()["body"]
        self.assertEqual(stored_body, payload.ARTIFACT_BODY_MARKER)
        self.assertNotIn(LEAK_CANARY, stored_body)
        self.assertLess(len(stored_body.encode("utf-8")), 512)

    def test_inline_body_equal_to_marker_stays_inline(self) -> None:
        dispatch = self.store.dispatch_agent(
            "alpha-architect", "alpha-worker", "marker-inline",
            "Subject", payload.ARTIFACT_BODY_MARKER, [],
        )
        self.assertIsNone(self.payload_ref(dispatch["dispatch_id"]))
        message = self.store.read_message("alpha-worker", dispatch["message_id"])
        self.assertEqual(message["body_storage"], "inline")
        self.assertEqual(message["body"], payload.ARTIFACT_BODY_MARKER)


class InputCompatibilityTest(PayloadHarness):
    """§9.3 body/body_file/origin compatibility, roots, and size boundaries."""

    def _inline(self, key: str, body: str, **kwargs):
        return self.store.dispatch_agent(
            "alpha-architect", "alpha-worker", key, f"S {key}", body, [], **kwargs
        )

    def test_inline_cap_discriminates_at_boundary(self) -> None:
        limit = payload.INLINE_BODY_MAX_BYTES
        ok_minus = self._inline("cap-minus", "a" * (limit - 1))
        self.assertEqual(ok_minus["status"], "queued")
        ok_exact = self._inline("cap-exact", "a" * limit)
        self.assertEqual(ok_exact["status"], "queued")
        exc = self.refusal(
            "dispatch_body_too_large", self._inline, "cap-plus", "a" * (limit + 1)
        )
        self.assertIn("body_file", str(exc))

    def test_inline_cap_counts_utf8_bytes_not_characters(self) -> None:
        two_byte = "é" * (payload.INLINE_BODY_MAX_BYTES // 2)
        self.assertEqual(self._inline("cap-mb-ok", two_byte)["status"], "queued")
        self.refusal(
            "dispatch_body_too_large", self._inline, "cap-mb-over", two_byte + "x"
        )

    def test_file_cap_discriminates_at_boundary(self) -> None:
        limit = payload.FILE_BODY_MAX_BYTES
        self.write_source("minus.md", b"a" * (limit - 1))
        self.write_source("exact.md", b"a" * limit)
        self.write_source("plus.md", b"a" * (limit + 1))
        self.assertEqual(self.dispatch_file("fcap-minus", "minus.md")["status"], "queued")
        self.assertEqual(self.dispatch_file("fcap-exact", "exact.md")["status"], "queued")
        self.refusal("dispatch_payload_too_large", self.dispatch_file, "fcap-plus", "plus.md")
        with self.store.connection() as conn:
            count = conn.execute(
                "select count(*) as c from dispatch_ledger where idempotency_key = 'fcap-plus'"
            ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_body_input_conflict(self) -> None:
        self.write_source("brief.md")
        self.refusal(
            "dispatch_body_input_conflict",
            self.store.dispatch_agent,
            "alpha-architect", "alpha-worker", "conflict-both", "S", "inline", [],
            body_file="brief.md", payload_origin="authored_brief",
        )
        self.refusal(
            "dispatch_body_input_conflict",
            self.store.dispatch_agent,
            "alpha-architect", "alpha-worker", "conflict-neither", "S", None, [],
        )
        self.assert_zero_dispatch_writes()

    def test_payload_origin_closed_vocabulary(self) -> None:
        self.write_source("brief.md")
        self.refusal(
            "dispatch_payload_origin_invalid",
            self.store.dispatch_agent,
            "alpha-architect", "alpha-worker", "origin-missing", "S", None, [],
            body_file="brief.md",
        )
        self.refusal(
            "dispatch_payload_origin_invalid",
            self.dispatch_file, "origin-bad", origin="other",
        )
        self.refusal(
            "dispatch_payload_origin_invalid",
            self._inline, "origin-inline", "inline body",
            payload_origin="authored_brief",
        )
        for index, origin in enumerate(payload.PAYLOAD_ORIGINS):
            self.write_source(f"origin-{index}.md", f"origin body {index}\n".encode())
            dispatch = self.dispatch_file(f"origin-ok-{index}", f"origin-{index}.md", origin=origin)
            self.assertEqual(self.payload_ref(dispatch["dispatch_id"])["payload_origin"], origin)

    def test_agent_producer_explicit_root_must_match_registration(self) -> None:
        self.write_source("brief.md")
        ok = self.dispatch_file("root-match", source_root=str(self.producer_root))
        self.assertEqual(ok["status"], "queued")
        other = self.root / "elsewhere"
        other.mkdir()
        (other / "brief.md").write_bytes(PAYLOAD_BYTES)
        self.refusal(
            "dispatch_payload_path_invalid",
            self.dispatch_file, "root-mismatch", source_root=str(other),
        )
        self.refusal(
            "dispatch_payload_path_invalid",
            self.dispatch_file, "root-relative", source_root="relative/root",
        )

    def test_human_producer_requires_explicit_absolute_root_never_cwd(self) -> None:
        source_dir = self.root / "operator-briefs"
        source_dir.mkdir()
        (source_dir / "brief.md").write_bytes(PAYLOAD_BYTES)
        previous_cwd = os.getcwd()
        os.chdir(source_dir)
        try:
            # Even with the working directory containing the file, no explicit
            # root refuses: CWD is never inferred.
            self.refusal(
                "dispatch_payload_path_invalid",
                self.dispatch_file, "human-nocwd",
                producer=HUMAN_ID, override_reason="ops",
            )
        finally:
            os.chdir(previous_cwd)
        self.refusal(
            "dispatch_payload_path_invalid",
            self.dispatch_file, "human-relroot",
            producer=HUMAN_ID, override_reason="ops", source_root="operator-briefs",
        )
        ok = self.dispatch_file(
            "human-ok", producer=HUMAN_ID, override_reason="ops",
            source_root=str(source_dir),
        )
        self.assertEqual(ok["status"], "queued")
        message = self.store.read_message("alpha-worker", ok["message_id"])
        self.assertEqual(message["body"], PAYLOAD_TEXT)


class SourceRefusalTest(PayloadHarness):
    """§9.4 hostile/unusable sources leave zero dispatch writes."""

    def assert_refused(self, code: str, rel: str, key: str) -> None:
        self.refusal(code, self.dispatch_file, key, rel)
        self.assert_zero_dispatch_writes()

    def test_absolute_path_refuses(self) -> None:
        outside = self.root / "outside.md"
        outside.write_bytes(PAYLOAD_BYTES)
        self.assert_refused("dispatch_payload_path_invalid", str(outside), "abs")

    def test_traversal_refuses(self) -> None:
        (self.root / "escape.md").write_bytes(PAYLOAD_BYTES)
        self.assert_refused("dispatch_payload_path_invalid", "../escape.md", "dotdot")
        self.write_source("real.md")
        self.assert_refused("dispatch_payload_path_invalid", "sub/../real.md", "dotdot-mid")

    def test_zero_component_path_refuses(self) -> None:
        # Path(".") and Path("./") normalize to zero components: they name the
        # source root itself, and without the typed refusal they would reach
        # rel.parts[-1] as a raw IndexError.
        self.assert_refused("dispatch_payload_path_invalid", ".", "dot")
        self.assert_refused("dispatch_payload_path_invalid", "./", "dot-slash")

    def test_symlink_final_component_refuses(self) -> None:
        target = self.root / "target.md"
        target.write_bytes(PAYLOAD_BYTES)
        (self.producer_root / "link.md").symlink_to(target)
        self.assert_refused("dispatch_payload_source_unavailable", "link.md", "symlink-final")

    def test_symlink_directory_component_refuses(self) -> None:
        real_dir = self.root / "realdir"
        real_dir.mkdir()
        (real_dir / "brief.md").write_bytes(PAYLOAD_BYTES)
        (self.producer_root / "linkdir").symlink_to(real_dir)
        self.assert_refused(
            "dispatch_payload_source_unavailable", "linkdir/brief.md", "symlink-dir"
        )

    def test_missing_file_refuses(self) -> None:
        self.assert_refused("dispatch_payload_source_unavailable", "absent.md", "missing")

    def test_directory_refuses(self) -> None:
        (self.producer_root / "adir").mkdir()
        self.assert_refused("dispatch_payload_source_unavailable", "adir", "dir")

    def test_fifo_refuses(self) -> None:
        fifo = self.producer_root / "fifo.md"
        os.mkfifo(fifo)
        self.assert_refused("dispatch_payload_source_unavailable", "fifo.md", "fifo")

    def test_invalid_utf8_refuses(self) -> None:
        self.write_source("bad.md", b"ok start \xff\xfe bad")
        self.assert_refused("dispatch_payload_invalid_utf8", "bad.md", "badutf8")

    def test_nul_byte_refuses(self) -> None:
        self.write_source("nul.md", b"text with \x00 nul")
        self.assert_refused("dispatch_payload_invalid_utf8", "nul.md", "nul")

    def test_empty_file_refuses(self) -> None:
        self.write_source("empty.md", b"")
        self.assert_refused("dispatch_payload_invalid_utf8", "empty.md", "empty")

    def test_unstable_source_refuses(self) -> None:
        self.write_source("unstable.md")

        def unstable_fstat(fd: int):
            real = os.fstat(fd)
            return types.SimpleNamespace(
                st_size=real.st_size,
                st_mtime_ns=real.st_mtime_ns + 1,
                st_ctime_ns=real.st_ctime_ns,
                st_dev=real.st_dev,
                st_ino=real.st_ino,
                st_mode=real.st_mode,
            )

        with mock.patch.object(payload, "_fstat_after_read", unstable_fstat):
            self.refusal(
                "dispatch_payload_source_unstable",
                self.dispatch_file, "unstable", "unstable.md",
            )
        self.assert_zero_dispatch_writes()

    def test_identity_change_with_equal_size_and_mtime_refuses(self) -> None:
        # An equal-size same-mtime replacement (e.g. rename-over) still moves
        # st_ino/st_dev or st_ctime_ns; each alone must refuse as unstable.
        for field in ("st_ino", "st_dev", "st_ctime_ns"):
            with self.subTest(field=field):
                self.write_source("identity.md")

                def swapped_fstat(fd: int, _field: str = field):
                    real = os.fstat(fd)
                    values = {
                        "st_size": real.st_size,
                        "st_mtime_ns": real.st_mtime_ns,
                        "st_ctime_ns": real.st_ctime_ns,
                        "st_dev": real.st_dev,
                        "st_ino": real.st_ino,
                        "st_mode": real.st_mode,
                    }
                    values[_field] += 1
                    return types.SimpleNamespace(**values)

                with mock.patch.object(payload, "_fstat_after_read", swapped_fstat):
                    exc = self.refusal(
                        "dispatch_payload_source_unstable",
                        self.dispatch_file, f"identity-{field}", "identity.md",
                    )
                self.assertNotIn(LEAK_CANARY, str(exc))
                self.assert_zero_dispatch_writes()


class PublicationFaultTest(PayloadHarness):
    """§9.5 fault-injected publication/SQL failure and pre-rollback cleanup."""

    def test_sql_failure_unlinks_created_blob_before_rollback(self) -> None:
        self.write_source("brief.md")
        lock_probe: dict[str, bool] = {}
        real_unlink = os.unlink

        def probing_unlink(path, *args, **kwargs):
            # The pre-rollback cleanup unlink targets the digest leaf name
            # relative to the retained shard FD, never a full blob pathname.
            if kwargs.get("dir_fd") is not None and path == PAYLOAD_SHA256:
                probe = sqlite3.connect(self.db_path, timeout=0.05)
                try:
                    probe.execute("begin immediate")
                except sqlite3.OperationalError:
                    lock_probe["held_at_unlink"] = True
                else:
                    lock_probe["held_at_unlink"] = False
                    probe.rollback()
                finally:
                    probe.close()
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(
            Mailbox, "_insert_message", side_effect=RuntimeError("injected insert failure")
        ):
            with mock.patch.object(payload.os, "unlink", side_effect=probing_unlink):
                with self.assertRaises(RuntimeError):
                    self.dispatch_file("fault-sql", "brief.md")

        # The newly created final blob was unlinked while the BEGIN IMMEDIATE
        # write lock was still held, then the transaction rolled back.
        self.assertEqual(lock_probe.get("held_at_unlink"), True)
        self.assert_zero_dispatch_writes()

    def test_cleanup_failure_reports_measurable_orphan_residue(self) -> None:
        self.write_source("brief.md")
        real_unlink = os.unlink

        def failing_unlink(path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and path == PAYLOAD_SHA256:
                raise OSError(1, "injected unlink failure")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(
            Mailbox, "_insert_message", side_effect=RuntimeError("injected insert failure")
        ):
            with mock.patch.object(payload.os, "unlink", side_effect=failing_unlink):
                with self.assertRaises(payload.PayloadError) as caught:
                    self.dispatch_file("fault-cleanup", "brief.md")
        self.assertEqual(caught.exception.code, "cleanup_uncertain")
        self.assertIn(PAYLOAD_SHA256, str(caught.exception))
        self.assertIn("unlink", str(caught.exception))
        # The orphan is measurable through the read-only audit and nothing
        # else was committed.
        self.assertTrue(self.blob_path().is_file())
        report = payload.audit_store(self.db_path)
        self.assertFalse(report["ok"])
        self.assertEqual(report["unreferenced_blobs"]["count"], 1)
        self.assertEqual(report["unreferenced_blobs"]["bytes"], len(PAYLOAD_BYTES))
        self.assertEqual(self.counts()["dispatch_ledger"], 0)

    def test_sql_failure_cleanup_fsync_failure_reports_orphan_uncertainty(self) -> None:
        # The pre-rollback unlink succeeds but the shard-directory fsync that
        # makes the removal durable fails: the call must not claim clean
        # cleanup and must report the measurable uncertainty.
        self.write_source("brief.md")
        blob = self.blob_path()
        real_fsync_fd = payload._fsync_fd

        def failing_cleanup_fsync(fd: int) -> None:
            # The only fsync of the retained shard FD after the blob is gone
            # is the ledger's pre-rollback cleanup fsync. (The shard existence
            # guard skips the earlier created-level parent-FD fsyncs.)
            if blob.parent.is_dir() and not blob.exists() and payload._dir_identity(fd) == (
                os.stat(blob.parent).st_dev, os.stat(blob.parent).st_ino
            ):
                raise OSError(5, "injected cleanup fsync failure")
            real_fsync_fd(fd)

        with mock.patch.object(
            Mailbox, "_insert_message", side_effect=RuntimeError("injected insert failure")
        ):
            with mock.patch.object(payload, "_fsync_fd", side_effect=failing_cleanup_fsync):
                with self.assertRaises(payload.PayloadError) as caught:
                    self.dispatch_file("fault-cleanup-fsync", "brief.md")
        self.assertEqual(caught.exception.code, "cleanup_uncertain")
        message = str(caught.exception)
        self.assertIn(PAYLOAD_SHA256, message)
        self.assertIn("fsync", message)
        self.assertNotIn(LEAK_CANARY, message)
        # The unlink itself did happen and nothing was committed.
        self.assertFalse(blob.exists())
        self.assertEqual(self.counts()["dispatch_ledger"], 0)

    def test_sql_failure_cleanup_fsync_runs_under_write_lock(self) -> None:
        self.write_source("brief.md")
        blob = self.blob_path()
        lock_probe: dict[str, bool] = {}
        real_fsync_fd = payload._fsync_fd

        def probing_fsync(fd: int) -> None:
            if blob.parent.is_dir() and not blob.exists() and payload._dir_identity(fd) == (
                os.stat(blob.parent).st_dev, os.stat(blob.parent).st_ino
            ):
                probe = sqlite3.connect(self.db_path, timeout=0.05)
                try:
                    probe.execute("begin immediate")
                except sqlite3.OperationalError:
                    lock_probe["held_at_cleanup_fsync"] = True
                else:
                    lock_probe["held_at_cleanup_fsync"] = False
                    probe.rollback()
                finally:
                    probe.close()
            real_fsync_fd(fd)

        with mock.patch.object(
            Mailbox, "_insert_message", side_effect=RuntimeError("injected insert failure")
        ):
            with mock.patch.object(payload, "_fsync_fd", side_effect=probing_fsync):
                with self.assertRaises(RuntimeError):
                    self.dispatch_file("fault-cleanup-fsync-lock", "brief.md")
        # The durable-removal fsync ran while BEGIN IMMEDIATE was still held.
        self.assertEqual(lock_probe.get("held_at_cleanup_fsync"), True)
        self.assert_zero_dispatch_writes()

    def test_sql_failure_after_parent_swap_removes_only_created_inode(self) -> None:
        # Review F3: the adversary swaps the validated shard directory for a
        # symlink to a decoy directory AFTER publication but BEFORE the SQL
        # failure. A pathname-based cleanup would unlink the attacker's decoy
        # at the canonical digest path and strand the real blob; the retained
        # shard-FD cleanup removes exactly the inode this dispatch created
        # from the real (renamed) shard and never touches the decoy.
        self.write_source("brief.md")
        shard = self.blob_path().parent
        moved = self.root / "rollback-shard-moved"
        outside = self.root / "rollback-outside-shard"
        outside.mkdir()
        decoy = outside / PAYLOAD_SHA256
        decoy.write_bytes(b"attacker replacement bytes")

        def swap_then_fail(*args, **kwargs):
            shard.rename(moved)
            shard.symlink_to(outside)
            raise RuntimeError("injected insert failure")

        with mock.patch.object(Mailbox, "_insert_message", side_effect=swap_then_fail):
            with self.assertRaises(RuntimeError) as caught:
                self.dispatch_file("fault-parent-swap", "brief.md")
        self.assertEqual(str(caught.exception), "injected insert failure")
        # Only the created inode was removed, from the real renamed shard.
        self.assertFalse((moved / PAYLOAD_SHA256).exists())
        self.assertEqual(decoy.read_bytes(), b"attacker replacement bytes")
        self.assertEqual(self.counts()["dispatch_ledger"], 0)
        self.assertEqual(self.counts()["messages"], 0)
        self.assertEqual(self.staging_files(), [])

    def test_sql_failure_leaf_decoy_is_never_unlinked(self) -> None:
        # Review F3: the adversary replaces the created blob inside the real
        # shard with a decoy inode at the same digest name before the SQL
        # failure. The identity-guarded cleanup refuses to unlink the
        # replacement and reports the residue truthfully instead of claiming
        # clean cleanup or deleting attacker-chosen bytes.
        self.write_source("brief.md")
        blob = self.blob_path()

        def replace_then_fail(*args, **kwargs):
            blob.unlink()
            blob.write_bytes(b"attacker decoy at digest name")
            raise RuntimeError("injected insert failure")

        with mock.patch.object(Mailbox, "_insert_message", side_effect=replace_then_fail):
            with self.assertRaises(payload.PayloadError) as caught:
                self.dispatch_file("fault-leaf-decoy", "brief.md")
        self.assertEqual(caught.exception.code, "cleanup_uncertain")
        message = str(caught.exception)
        self.assertIn(PAYLOAD_SHA256, message)
        self.assertIn("orphan", message)
        self.assertIn("refusing to unlink the replacement", message)
        self.assertNotIn(LEAK_CANARY, message)
        # The decoy at the canonical digest name survives untouched.
        self.assertEqual(blob.read_bytes(), b"attacker decoy at digest name")
        self.assertEqual(self.counts()["dispatch_ledger"], 0)

    def test_failing_call_never_deletes_preexisting_verified_blob(self) -> None:
        self.write_source("brief.md")
        winner = self.dispatch_file("fault-winner", "brief.md")
        self.assertTrue(self.blob_path().is_file())

        with mock.patch.object(
            Mailbox, "_insert_message", side_effect=RuntimeError("injected insert failure")
        ):
            with self.assertRaises(RuntimeError):
                self.dispatch_file("fault-loser", "brief.md")

        # The pre-existing verified blob survives, the winner's dispatch is
        # intact, and its payload still resolves exactly.
        self.assertTrue(self.blob_path().is_file())
        self.assertEqual(
            self.store.read_message("alpha-worker", winner["message_id"])["body"],
            PAYLOAD_TEXT,
        )
        self.assertTrue(payload.audit_store(self.db_path)["ok"])


class ExistingBlobReuseTest(PayloadHarness):
    """§9.6 an existing blob is reused only after exact re-verification."""

    def test_corrupt_existing_destination_refuses_without_overwrite(self) -> None:
        self.write_source("brief.md")
        blob = self.blob_path()
        blob.parent.mkdir(mode=0o700, parents=True)
        wrong = b"not the payload bytes"
        blob.write_bytes(wrong)
        self.refusal("dispatch_payload_publish_failed", self.dispatch_file, "reuse-corrupt")
        self.assertEqual(blob.read_bytes(), wrong)
        self.assertEqual(self.counts()["dispatch_ledger"], 0)

    def test_wrong_type_destination_refuses_without_overwrite(self) -> None:
        self.write_source("brief.md")
        blob = self.blob_path()
        blob.parent.mkdir(mode=0o700, parents=True)
        blob.mkdir()
        self.refusal("dispatch_payload_publish_failed", self.dispatch_file, "reuse-dir")
        self.assertTrue(blob.is_dir())

    def test_symlink_destination_refuses_without_overwrite(self) -> None:
        self.write_source("brief.md")
        target = self.root / "elsewhere-bytes"
        target.write_bytes(PAYLOAD_BYTES)
        blob = self.blob_path()
        blob.parent.mkdir(mode=0o700, parents=True)
        blob.symlink_to(target)
        self.refusal("dispatch_payload_publish_failed", self.dispatch_file, "reuse-symlink")
        self.assertTrue(blob.is_symlink())

    def test_exact_existing_blob_is_reused(self) -> None:
        self.write_source("brief.md")
        blob = self.blob_path()
        blob.parent.mkdir(mode=0o700, parents=True)
        blob.write_bytes(PAYLOAD_BYTES)
        dispatch = self.dispatch_file("reuse-exact")
        self.assertEqual(dispatch["status"], "queued")
        self.assertEqual(len(self.blob_files()), 1)
        self.assertEqual(
            self.store.read_message("alpha-worker", dispatch["message_id"])["body"],
            PAYLOAD_TEXT,
        )


class LeafOpenRaceTest(PayloadHarness):
    """Reviewer F6: a leaf raced between its lstat and its open refuses promptly.

    Both post-publication leaf opens (existing-blob reuse re-verification and
    the shared authenticated load/preflight) open the leaf
    ``O_RDONLY|O_NOFOLLOW|O_NONBLOCK`` and treat the opened FD as
    authoritative: a FIFO or other non-regular object raced into place cannot
    block the open and refuses typed from the FD's own fstat, and a mutated
    or grown regular leaf refuses typed from the fstat size or the bounded
    hash, never from an unbounded read.
    """

    def existing_blob(self) -> Path:
        self.write_source("brief.md")
        blob = self.blob_path()
        blob.parent.mkdir(mode=0o700, parents=True)
        blob.write_bytes(PAYLOAD_BYTES)
        return blob

    def fifo_swap(self, blob: Path):
        def swap(rel_path: str) -> None:
            blob.unlink()
            os.mkfifo(blob)

        return swap

    def assert_zero_sql_rows(self) -> None:
        counts = self.counts()
        self.assertEqual(counts["messages"], 0)
        self.assertEqual(counts["message_recipients"], 0)
        self.assertEqual(counts["dispatch_ledger"], 0)
        self.assertEqual(counts["dispatch_payload_refs"], 0)

    def load_verified(self) -> str:
        return payload.load_verified_text(
            self.store_root,
            storage_kind=payload.STORAGE_KIND,
            payload_sha256=PAYLOAD_SHA256,
            byte_count=len(PAYLOAD_BYTES),
            char_count=len(PAYLOAD_TEXT),
        )

    def test_reuse_leaf_raced_to_fifo_refuses_with_zero_sql_rows(self) -> None:
        # Without O_NONBLOCK this open would block forever waiting for a FIFO
        # writer; the typed refusal is the promptness proof.
        blob = self.existing_blob()
        with mock.patch.object(payload, "_before_reuse_open", side_effect=self.fifo_swap(blob)):
            exc = self.refusal(
                "dispatch_payload_publish_failed", self.dispatch_file, "reuse-fifo-race"
            )
        self.assertNotIn(LEAK_CANARY, str(exc))
        self.assertTrue(stat.S_ISFIFO(os.lstat(blob).st_mode), "raced FIFO must stay untouched")
        self.assert_zero_sql_rows()

    def test_reuse_leaf_grown_after_lstat_refuses_with_zero_sql_rows(self) -> None:
        blob = self.existing_blob()

        def grow(rel_path: str) -> None:
            with open(blob, "ab") as handle:
                handle.write(b"x" * 4096)

        with mock.patch.object(payload, "_before_reuse_open", side_effect=grow):
            exc = self.refusal(
                "dispatch_payload_publish_failed", self.dispatch_file, "reuse-grow-race"
            )
        self.assertIn("refusing to overwrite", str(exc))
        self.assert_zero_sql_rows()

    def test_reuse_leaf_mutated_after_lstat_refuses_on_bounded_hash(self) -> None:
        # A same-size rewrite passes both size checks; only hashing the bytes
        # read from the authoritative FD catches it.
        blob = self.existing_blob()

        def mutate(rel_path: str) -> None:
            blob.write_bytes(b"Z" * len(PAYLOAD_BYTES))

        with mock.patch.object(payload, "_before_reuse_open", side_effect=mutate):
            exc = self.refusal(
                "dispatch_payload_publish_failed", self.dispatch_file, "reuse-mutate-race"
            )
        self.assertIn("hashes to", str(exc))
        self.assert_zero_sql_rows()

    def test_load_leaf_raced_to_fifo_refuses_not_regular(self) -> None:
        self.write_source("brief.md")
        self.dispatch_file("load-fifo-race")
        blob = self.blob_path()
        with mock.patch.object(payload, "_before_load_open", side_effect=self.fifo_swap(blob)):
            with self.assertRaises(payload.PayloadIntegrityError) as caught:
                self.load_verified()
        self.assertTrue(str(caught.exception).startswith("dispatch_payload_not_regular"))
        self.assertNotIn(LEAK_CANARY, str(caught.exception))

    def test_load_leaf_grown_after_lstat_refuses_size_mismatch(self) -> None:
        self.write_source("brief.md")
        self.dispatch_file("load-grow-race")
        blob = self.blob_path()

        def grow(rel_path: str) -> None:
            with open(blob, "ab") as handle:
                handle.write(b"x" * 4096)

        with mock.patch.object(payload, "_before_load_open", side_effect=grow):
            with self.assertRaises(payload.PayloadIntegrityError) as caught:
                self.load_verified()
        self.assertTrue(str(caught.exception).startswith("dispatch_payload_size_mismatch"))
        self.assertNotIn(LEAK_CANARY, str(caught.exception))

    def test_preflight_leaf_raced_to_fifo_invokes_adapter_zero_times(self) -> None:
        self.write_source("brief.md")
        dispatch = self.dispatch_file("preflight-fifo-race")
        blob = self.blob_path()
        adapter = CountingAdapter()
        with mock.patch.object(payload, "_before_load_open", side_effect=self.fifo_swap(blob)):
            self.store.start_queued_dispatches(lambda _runtime: adapter, limit=10)
        self.assertEqual(adapter.contexts, [])
        with self.store.connection() as conn:
            row = conn.execute(
                "select status, failure_reason from dispatch_ledger where dispatch_id = ?",
                (dispatch["dispatch_id"],),
            ).fetchone()
        self.assertEqual(row["status"], "spawn_failed_message_landed")
        self.assertTrue(row["failure_reason"].startswith("dispatch_payload_not_regular"))

    def test_read_message_leaf_raced_to_fifo_leaves_recipient_sent(self) -> None:
        self.write_source("brief.md")
        dispatch = self.dispatch_file("read-fifo-race")
        blob = self.blob_path()
        with mock.patch.object(payload, "_before_load_open", side_effect=self.fifo_swap(blob)):
            with self.assertRaises(payload.PayloadIntegrityError) as caught:
                self.store.read_message("alpha-worker", dispatch["message_id"])
        self.assertTrue(str(caught.exception).startswith("dispatch_payload_not_regular"))
        self.assertEqual(self.recipient_status(dispatch["message_id"]), "sent")

    def test_bounded_leaf_read_caps_at_expected_plus_one_byte(self) -> None:
        oversized = self.root / "oversized-leaf"
        oversized.write_bytes(b"a" * 4096)
        fd = os.open(oversized, os.O_RDONLY)
        try:
            data = payload._read_leaf_bounded(fd, 10)
        finally:
            os.close(fd)
        self.assertEqual(len(data), 11)


class ReplayTest(PayloadHarness):
    """§9.7 same-key replay never touches the source again."""

    def test_replay_after_edit_and_delete_returns_original_without_source_read(self) -> None:
        source = self.write_source("brief.md")
        first = self.dispatch_file("replay-1")
        source.write_bytes(b"edited source\n")

        def forbid_capture(*args, **kwargs):
            raise AssertionError("replay must not read the source file")

        with mock.patch.object(payload, "capture_source", side_effect=forbid_capture):
            replay_after_edit = self.dispatch_file("replay-1")
        self.assertEqual(replay_after_edit["dispatch_id"], first["dispatch_id"])

        source.unlink()
        with mock.patch.object(payload, "capture_source", side_effect=forbid_capture):
            replay_after_delete = self.dispatch_file("replay-1")
        self.assertEqual(replay_after_delete["dispatch_id"], first["dispatch_id"])
        self.assertEqual(
            self.store.read_message("alpha-worker", first["message_id"])["body"],
            PAYLOAD_TEXT,
        )

    def test_key_reuse_for_different_target_refuses_without_source_read(self) -> None:
        self.write_source("brief.md")
        self.dispatch_file("replay-target")

        def forbid_capture(*args, **kwargs):
            raise AssertionError("target-mismatch replay must not read the source file")

        with mock.patch.object(payload, "capture_source", side_effect=forbid_capture):
            with self.assertRaises(ValidationError) as caught:
                self.store.dispatch_agent(
                    "alpha-architect", "echo-worker", "replay-target", "S",
                    body_file="brief.md", payload_origin="authored_brief",
                )
        self.assertIn("reuse of a key for a different target", str(caught.exception))

    def test_capture_failure_reprobe_returns_concurrent_winner(self) -> None:
        self.write_source("brief.md")
        real_capture = payload.capture_source
        second_store = Store(self.db_path)
        winner_holder: dict[str, dict] = {}

        def racing_capture(source_root, body_file):
            # A concurrent first call commits between the initial probe and
            # this capture failure; the repeated probe must return it.
            with mock.patch.object(payload, "capture_source", real_capture):
                winner_holder["dispatch"] = second_store.dispatch_agent(
                    "alpha-architect", "alpha-worker", "reprobe-key", "S",
                    body_file="brief.md", payload_origin="authored_brief",
                )
            raise payload.PayloadError(
                "dispatch_payload_source_unavailable", "injected capture failure"
            )

        with mock.patch.object(payload, "capture_source", side_effect=racing_capture):
            result = self.dispatch_file("reprobe-key")
        self.assertEqual(result["dispatch_id"], winner_holder["dispatch"]["dispatch_id"])
        self.assertEqual(self.counts()["dispatch_ledger"], 1)

    def test_capture_failure_without_winner_surfaces_truthfully(self) -> None:
        self.refusal(
            "dispatch_payload_source_unavailable",
            self.dispatch_file, "reprobe-none", "absent.md",
        )
        self.assert_zero_dispatch_writes()


class ConcurrencyTest(PayloadHarness):
    """§9.8 and §9.9 concurrent same-key calls and same-bytes deduplication."""

    def test_concurrent_same_key_calls_produce_one_of_everything(self) -> None:
        self.write_source("brief.md")
        self.store.init()
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[BaseException] = []

        def race() -> None:
            local_store = Store(self.db_path)
            barrier.wait()
            try:
                results.append(
                    local_store.dispatch_agent(
                        "alpha-architect", "alpha-worker", "conc-key", "S",
                        body_file="brief.md", payload_origin="authored_brief",
                    )
                )
            except BaseException as exc:  # pragma: no cover - failure diagnostics
                errors.append(exc)

        threads = [threading.Thread(target=race) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["dispatch_id"], results[1]["dispatch_id"])
        counts = self.counts()
        self.assertEqual(counts["messages"], 1)
        self.assertEqual(counts["dispatch_ledger"], 1)
        self.assertEqual(counts["dispatch_payload_refs"], 1)
        self.assertEqual(len(self.blob_files()), 1)
        self.assertEqual(self.staging_files(), [])
        self.assertEqual(self.blob_path().read_bytes(), PAYLOAD_BYTES)

    def test_different_keys_same_bytes_share_one_blob(self) -> None:
        self.write_source("one.md")
        self.write_source("two.md")
        first = self.dispatch_file("dedup-1", "one.md")
        second = self.dispatch_file("dedup-2", "two.md", origin="generated_artifact")
        self.assertNotEqual(first["dispatch_id"], second["dispatch_id"])
        counts = self.counts()
        self.assertEqual(counts["dispatch_payload_refs"], 2)
        self.assertEqual(len(self.blob_files()), 1)
        for dispatch in (first, second):
            self.assertEqual(
                self.store.read_message("alpha-worker", dispatch["message_id"])["body"],
                PAYLOAD_TEXT,
            )


class PreflightTest(PayloadHarness):
    """§9.10 corrupted blobs invoke the adapter zero times on every start path."""

    def _queued_artifact_dispatch(self, key: str) -> dict:
        self.write_source(f"{key}.md", f"payload for {key}: {PAYLOAD_TEXT}".encode())
        return self.dispatch_file(key, f"{key}.md")

    def _dispatch_sha(self, dispatch: dict) -> str:
        return self.payload_ref(dispatch["dispatch_id"])["payload_sha256"]

    def _assert_spawn_failed(self, dispatch_id: str, code: str) -> None:
        with self.store.connection() as conn:
            row = conn.execute(
                "select status, failure_reason from dispatch_ledger where dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        self.assertEqual(row["status"], "spawn_failed_message_landed")
        self.assertTrue(
            row["failure_reason"].startswith(code),
            f"expected {code}, got: {row['failure_reason']}",
        )

    def test_queued_start_typed_failures_adapter_zero(self) -> None:
        cases = [
            ("missing", "dispatch_payload_missing", lambda blob, size: blob.unlink()),
            (
                "replaced",
                "dispatch_payload_digest_mismatch",
                lambda blob, size: blob.write_bytes(b"X" * size),
            ),
            (
                "truncated",
                "dispatch_payload_size_mismatch",
                lambda blob, size: blob.write_bytes(blob.read_bytes()[:-1]),
            ),
            (
                "enlarged",
                "dispatch_payload_size_mismatch",
                lambda blob, size: blob.write_bytes(blob.read_bytes() + b"x"),
            ),
        ]
        for key, code, corrupt in cases:
            with self.subTest(key=key):
                dispatch = self._queued_artifact_dispatch(f"pf-{key}")
                sha = self._dispatch_sha(dispatch)
                blob = self.blob_path(sha)
                corrupt(blob, blob.stat().st_size)
                adapter = CountingAdapter()
                self.store.start_queued_dispatches(lambda _runtime: adapter, limit=10)
                self.assertEqual(adapter.contexts, [])
                self._assert_spawn_failed(dispatch["dispatch_id"], code)

    def test_decode_mismatch_and_metadata_invalid(self) -> None:
        dispatch = self._queued_artifact_dispatch("pf-decode")
        sha = self._dispatch_sha(dispatch)
        blob = self.blob_path(sha)
        bad_bytes = b"\xff" * blob.stat().st_size
        blob.write_bytes(bad_bytes)
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_payload_refs set payload_sha256 = ? where dispatch_id = ?",
                (hashlib.sha256(bad_bytes).hexdigest(), dispatch["dispatch_id"]),
            )
        # Move the corrupted blob to its digest-consistent path so decode is
        # the first mismatch encountered.
        new_blob = self.blob_path(hashlib.sha256(bad_bytes).hexdigest())
        new_blob.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        blob.replace(new_blob)
        adapter = CountingAdapter()
        self.store.start_queued_dispatches(lambda _runtime: adapter, limit=10)
        self.assertEqual(adapter.contexts, [])
        self._assert_spawn_failed(dispatch["dispatch_id"], "dispatch_payload_decode_mismatch")

        invalid = self._queued_artifact_dispatch("pf-metadata")
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_payload_refs set payload_sha256 = 'NOT-HEX' where dispatch_id = ?",
                (invalid["dispatch_id"],),
            )
        adapter = CountingAdapter()
        self.store.start_queued_dispatches(lambda _runtime: adapter, limit=10)
        self.assertEqual(adapter.contexts, [])
        self._assert_spawn_failed(invalid["dispatch_id"], "dispatch_payload_metadata_invalid")

    def test_immediate_start_path_runs_preflight(self) -> None:
        dispatch = self._queued_artifact_dispatch("pf-immediate")
        sha = self._dispatch_sha(dispatch)
        self.blob_path(sha).unlink()
        adapter = CountingAdapter()
        # The same start method the inline-promotion path of dispatch_agent
        # delegates to.
        self.store._dispatch._start_dispatch_by_id(
            lambda _runtime: adapter, dispatch["dispatch_id"], ttl_seconds=60
        )
        self.assertEqual(adapter.contexts, [])
        self._assert_spawn_failed(dispatch["dispatch_id"], "dispatch_payload_missing")

    def test_retry_start_runs_preflight_and_recovers_after_restore(self) -> None:
        dispatch = self._queued_artifact_dispatch("pf-retry")
        sha = self._dispatch_sha(dispatch)
        blob = self.blob_path(sha)
        original = blob.read_bytes()
        blob.unlink()
        adapter = CountingAdapter()
        self.store.start_queued_dispatches(lambda _runtime: adapter, limit=10)
        self._assert_spawn_failed(dispatch["dispatch_id"], "dispatch_payload_missing")

        retry_adapter = CountingAdapter()
        self.store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: retry_adapter)
        self.assertEqual(retry_adapter.contexts, [])
        self._assert_spawn_failed(dispatch["dispatch_id"], "dispatch_payload_missing")

        blob.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        blob.write_bytes(original)
        recovered = self.store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: retry_adapter)
        self.assertEqual(recovered["status"], "in_flight")
        self.assertEqual(len(retry_adapter.contexts), 1)


class ReadIntegrityTest(PayloadHarness):
    """§9.11 read-time corruption leaves the recipient copy ``sent``."""

    def test_read_failure_leaves_sent_then_exact_read_advances(self) -> None:
        self.write_source("brief.md")
        dispatch = self.dispatch_file("read-corrupt")
        blob = self.blob_path()
        blob.write_bytes(b"Y" * len(PAYLOAD_BYTES))

        with self.assertRaises(payload.PayloadIntegrityError) as caught:
            self.store.read_message("alpha-worker", dispatch["message_id"])
        self.assertTrue(str(caught.exception).startswith("dispatch_payload_digest_mismatch"))
        self.assertEqual(self.recipient_status(dispatch["message_id"]), "sent")

        blob.write_bytes(PAYLOAD_BYTES)
        message = self.store.read_message("alpha-worker", dispatch["message_id"])
        self.assertEqual(message["body"], PAYLOAD_TEXT)
        self.assertEqual(self.recipient_status(dispatch["message_id"]), "read")


class MalformedCountMetadataTest(PayloadHarness):
    """Malformed SQLite count values fail typed at the shared metadata boundary.

    The ``integer`` count columns carry a positive check, but SQLite compares
    TEXT and REAL values as greater than zero, so rows such as
    ``byte_count = 'abc'`` can survive in the ledger. Every consumer must
    classify them as ``dispatch_payload_metadata_invalid`` instead of leaking
    a raw ``ValueError`` from an eager ``int(...)``.
    """

    MALFORMED = (("text", "abc"), ("real", 2.5))

    def _artifact(self, key: str) -> dict:
        self.write_source(f"{key}.md", f"payload for {key}: {PAYLOAD_TEXT}".encode())
        return self.dispatch_file(key, f"{key}.md")

    def _corrupt_count(self, dispatch_id: str, column: str, value) -> None:
        with self.store.connection() as conn:
            conn.execute(
                f"update dispatch_payload_refs set {column} = ? where dispatch_id = ?",
                (value, dispatch_id),
            )
        # SQLite retained the malformed value despite the positive check.
        self.assertEqual(self.payload_ref(dispatch_id)[column], value)

    def _malformed_artifacts(self, prefix: str) -> list[dict]:
        dispatches = []
        for column in ("byte_count", "char_count"):
            for label, value in self.MALFORMED:
                dispatch = self._artifact(f"{prefix}-{column}-{label}")
                self._corrupt_count(dispatch["dispatch_id"], column, value)
                dispatches.append(dispatch)
        return dispatches

    def test_audit_classifies_malformed_counts_and_continues(self) -> None:
        self._artifact("count-good")
        malformed = self._malformed_artifacts("count-audit")

        report = payload.audit_store(self.db_path)
        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 1)
        entries = report["referenced_missing_or_corrupt"]["entries"]
        self.assertEqual(
            report["referenced_missing_or_corrupt"]["count"], len(malformed)
        )
        self.assertEqual(
            {entry["code"] for entry in entries},
            {"dispatch_payload_metadata_invalid"},
        )
        self.assertEqual(
            {entry["reference_id"] for entry in entries},
            {dispatch["dispatch_id"] for dispatch in malformed},
        )
        self.assertEqual({entry["reference_kind"] for entry in entries}, {"dispatch"})

    def test_preflight_lands_typed_code_with_adapter_zero(self) -> None:
        for column in ("byte_count", "char_count"):
            for label, value in self.MALFORMED:
                with self.subTest(column=column, value=label):
                    dispatch = self._artifact(f"count-pf-{column}-{label}")
                    self._corrupt_count(dispatch["dispatch_id"], column, value)
                    adapter = CountingAdapter()
                    self.store.start_queued_dispatches(lambda _runtime: adapter, limit=10)
                    self.assertEqual(adapter.contexts, [])
                    with self.store.connection() as conn:
                        row = conn.execute(
                            "select status, failure_reason from dispatch_ledger "
                            "where dispatch_id = ?",
                            (dispatch["dispatch_id"],),
                        ).fetchone()
                    self.assertEqual(row["status"], "spawn_failed_message_landed")
                    self.assertTrue(
                        row["failure_reason"].startswith("dispatch_payload_metadata_invalid"),
                        f"expected dispatch_payload_metadata_invalid, got: {row['failure_reason']}",
                    )

    def test_read_message_raises_typed_error_and_leaves_sent(self) -> None:
        for column in ("byte_count", "char_count"):
            for label, value in self.MALFORMED:
                with self.subTest(column=column, value=label):
                    dispatch = self._artifact(f"count-read-{column}-{label}")
                    self._corrupt_count(dispatch["dispatch_id"], column, value)
                    with self.assertRaises(payload.PayloadIntegrityError) as caught:
                        self.store.read_message("alpha-worker", dispatch["message_id"])
                    self.assertEqual(caught.exception.code, "dispatch_payload_metadata_invalid")
                    self.assertEqual(self.recipient_status(dispatch["message_id"]), "sent")

    def test_boundary_rejects_boolean_and_non_integer_counts(self) -> None:
        for column in ("byte_count", "char_count"):
            for bad in (True, False, "12", "abc", 2.5, 3.0, None):
                with self.subTest(column=column, bad=bad):
                    kwargs = {"byte_count": len(PAYLOAD_BYTES), "char_count": len(PAYLOAD_TEXT)}
                    kwargs[column] = bad
                    with self.assertRaises(payload.PayloadIntegrityError) as caught:
                        payload.load_verified_text(
                            self.store_root,
                            storage_kind=payload.STORAGE_KIND,
                            payload_sha256=PAYLOAD_SHA256,
                            **kwargs,
                        )
                    self.assertEqual(
                        caught.exception.code, "dispatch_payload_metadata_invalid"
                    )


class AuthorizationAndLeakTest(PayloadHarness):
    """§9.12 payload access is recipient-only and text never leaks."""

    def test_other_actor_cannot_read_payload(self) -> None:
        self.write_source("brief.md")
        dispatch = self.dispatch_file("auth-1")
        for actor in ("echo-worker", "echo-architect", HUMAN_ID):
            with self.assertRaises(ValidationError):
                self.store.read_message(actor, dispatch["message_id"])

    def test_listings_and_errors_never_leak_payload_text(self) -> None:
        self.write_source("brief.md")
        dispatch = self.dispatch_file("leak-1")

        inbox = self.store.list_inbox("alpha-worker")
        self.assertNotIn(LEAK_CANARY, json.dumps(inbox, ensure_ascii=False))
        self.assertEqual(inbox[0]["body_storage"], "artifact")
        self.assertEqual(inbox[0]["body_snippet"], payload.ARTIFACT_SNIPPET)
        self.assertEqual(inbox[0]["body_bytes"], len(PAYLOAD_BYTES))
        self.assertEqual(inbox[0]["body_chars"], len(PAYLOAD_TEXT))

        unread = self.store.list_unread()
        self.assertNotIn(LEAK_CANARY, json.dumps(unread, ensure_ascii=False))

        waited = self.store.wait_for_reply("alpha-worker", timeout_seconds=0.0, full=True)
        dumped = json.dumps(waited, ensure_ascii=False)
        self.assertNotIn(LEAK_CANARY, dumped)
        artifact_rows = [m for m in waited["messages"] if m["id"] == dispatch["message_id"]]
        self.assertEqual(len(artifact_rows), 1)
        self.assertNotIn("body", artifact_rows[0])
        self.assertEqual(artifact_rows[0]["body_snippet"], payload.ARTIFACT_SNIPPET)

        # Preflight/read failure text reports identities, never payload text.
        self.blob_path().write_bytes(b"Z" * len(PAYLOAD_BYTES))
        adapter = CountingAdapter()
        self.store.start_queued_dispatches(lambda _runtime: adapter, limit=10)
        with self.store.connection() as conn:
            row = conn.execute(
                "select failure_reason, observed_values_json from dispatch_ledger where dispatch_id = ?",
                (dispatch["dispatch_id"],),
            ).fetchone()
        self.assertNotIn(LEAK_CANARY, row["failure_reason"])
        self.assertNotIn(LEAK_CANARY, row["observed_values_json"])
        with self.assertRaises(ValidationError) as caught:
            self.store.read_message("alpha-worker", dispatch["message_id"])
        self.assertNotIn(LEAK_CANARY, str(caught.exception))


class CompatibilityTest(PayloadHarness):
    """§9.13 legacy inline, mailbox, cancellation, and closeout are unchanged."""

    def test_inline_dispatch_round_trip_unchanged(self) -> None:
        dispatch = self.store.dispatch_agent(
            "alpha-architect", "alpha-worker", "compat-inline", "S", "plain inline body", []
        )
        self.assertIsNone(self.payload_ref(dispatch["dispatch_id"]))
        message = self.store.read_message("alpha-worker", dispatch["message_id"])
        self.assertEqual(message["body"], "plain inline body")
        self.assertEqual(message["body_storage"], "inline")

    def test_ordinary_send_message_unchanged(self) -> None:
        sent = self.store.send_message(
            "alpha-architect", ["alpha-worker"], "Ordinary", "ordinary body", []
        )
        message = self.store.read_message("alpha-worker", sent["id"])
        self.assertEqual(message["body"], "ordinary body")
        self.assertEqual(message["body_storage"], "inline")
        snippet_row = self.store.list_inbox("alpha-worker", unread_only=False)[0]
        self.assertIn("body_snippet", snippet_row)

    def test_artifact_dispatch_v2_closeout_and_cancellation_unchanged(self) -> None:
        self.write_source("brief.md")
        adapter = CountingAdapter()
        dispatch = self.dispatch_file("compat-close", adapter=adapter)
        self.assertEqual(dispatch["status"], "in_flight")
        self.store.read_message("alpha-worker", dispatch["message_id"])
        reply = self.store.send_message(
            "alpha-worker", ["alpha-architect"], "Done", "work summary",
            [], parent_message_id=dispatch["message_id"],
        )
        closed = self.store.close_dispatch(
            "alpha-worker",
            message_id=dispatch["message_id"],
            result="satisfied",
            reply_message_id=reply["id"],
            summary="done",
        )
        self.assertEqual(closed["status"], "closed")

        cancel_target = self.dispatch_file("compat-cancel")
        cancelled = self.store.request_cancellation(
            cancel_target["dispatch_id"],
            requesting_actor_id="alpha-architect",
            reason="no longer needed",
            authority="producer",
        )
        self.assertEqual(cancelled["status"], "cancelled")

    def test_key_drift_between_inline_and_file_modes_replays_existing(self) -> None:
        inline = self.store.dispatch_agent(
            "alpha-architect", "alpha-worker", "compat-drift", "S", "inline first", []
        )
        self.write_source("brief.md")
        replay = self.dispatch_file("compat-drift")
        self.assertEqual(replay["dispatch_id"], inline["dispatch_id"])
        self.assertIsNone(self.payload_ref(inline["dispatch_id"]))


class SchemaTest(PayloadHarness):
    """§9.14 (revised by review F1): the payload-capable ledger advances the
    durable compatibility floor to ``user_version == 3`` so a prior-version
    binary refuses it instead of skipping the payload preflight and handing
    workers the marker body; existing version-1 ledgers migrate safely."""

    def test_fresh_ledger_has_table_and_user_version_two(self) -> None:
        with self.store.connection() as conn:
            user_version = conn.execute("pragma user_version").fetchone()[0]
            table = conn.execute(
                "select name from sqlite_master where type='table' and name='dispatch_payload_refs'"
            ).fetchone()
        self.assertEqual(db_module.LEDGER_SCHEMA_VERSION, 3)
        self.assertEqual(user_version, db_module.LEDGER_SCHEMA_VERSION)
        self.assertIsNotNone(table)

    def test_version_one_ledger_migrates_to_current_floor(self) -> None:
        inline = self.store.dispatch_agent(
            "alpha-architect", "alpha-worker", "schema-legacy", "S", "legacy inline", []
        )
        with self.store.connection() as conn:
            conn.execute("drop table dispatch_payload_refs")
            conn.execute("pragma user_version = 1")
        upgraded = Store(self.db_path)
        upgraded.init()
        with upgraded.connection() as conn:
            user_version = conn.execute("pragma user_version").fetchone()[0]
            table = conn.execute(
                "select name from sqlite_master where type='table' and name='dispatch_payload_refs'"
            ).fetchone()
        self.assertEqual(user_version, db_module.LEDGER_SCHEMA_VERSION)
        self.assertIsNotNone(table)
        message = upgraded.read_message("alpha-worker", inline["message_id"])
        self.assertEqual(message["body"], "legacy inline")
        self.assertEqual(message["body_storage"], "inline")

    def test_prior_version_reader_refuses_payload_capable_default_ledger(self) -> None:
        # Deterministic prior-binary evidence: the recency guard shipped in
        # version-1 binaries is byte-identical to `_guard_ledger_schema_version`
        # with LEDGER_SCHEMA_VERSION == 2, so running that exact logic against
        # a message-payload-capable (user_version == 3) ledger proves the deployed
        # refusal without a second checkout.
        self.write_source("brief.md")
        self.dispatch_file("floor-artifact")
        with mock.patch.object(db_module, "LEDGER_SCHEMA_VERSION", 2):
            with self.assertRaises(ValidationError) as caught:
                db_module.Database(self.db_path, is_default_db_open=True).init()
        message = str(caught.exception)
        self.assertIn("newer", message)
        self.assertIn("ledger_user_version=3", message)
        self.assertIn("code_LEDGER_SCHEMA_VERSION=2", message)

    def test_current_reader_opens_payload_capable_default_ledger(self) -> None:
        # The same default-open path in THIS checkout accepts the ledger it
        # stamped, so the refusal above is the floor working, not a lockout.
        db_module.Database(self.db_path, is_default_db_open=True).init()


class PayloadAuditTest(PayloadHarness):
    """§9.15 the read-only audit distinguishes every class and deletes nothing."""

    def _artifact(self, key: str, text: str) -> tuple[dict, Path, bytes]:
        data = text.encode("utf-8")
        self.write_source(f"{key}.md", data)
        dispatch = self.dispatch_file(key, f"{key}.md")
        sha = self.payload_ref(dispatch["dispatch_id"])["payload_sha256"]
        return dispatch, self.blob_path(sha), data

    def test_audit_classifies_without_deleting(self) -> None:
        _good, good_blob, good_data = self._artifact("audit-good", "good payload\n")
        _missing, missing_blob, _ = self._artifact("audit-missing", "missing payload\n")
        _corrupt, corrupt_blob, corrupt_data = self._artifact("audit-corrupt", "corrupt payload\n")

        missing_blob.unlink()
        corrupt_blob.write_bytes(b"Q" * len(corrupt_data))

        # Layout-valid names: a digest-shaped orphan in its correct shard and
        # a uuid4().hex-shaped staging entry, so they count as unreferenced
        # blob / staging residue instead of irregular layout corruption.
        orphan = good_blob.parent / (good_blob.parent.name + "f" * 62)
        orphan.write_bytes(b"orphan bytes")
        staging_dir = self.store_root / "staging"
        staging_dir.mkdir(exist_ok=True)
        residue = staging_dir / ("0f" * 16)
        residue.write_bytes(b"staging residue bytes")

        report = payload.audit_store(self.db_path)
        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 1)
        self.assertEqual(report["referenced_good"]["bytes"], len(good_data))
        self.assertEqual(report["referenced_missing_or_corrupt"]["count"], 2)
        codes = {entry["code"] for entry in report["referenced_missing_or_corrupt"]["entries"]}
        self.assertEqual(codes, {"dispatch_payload_missing", "dispatch_payload_digest_mismatch"})
        self.assertEqual(report["unreferenced_blobs"]["count"], 1)
        self.assertEqual(report["unreferenced_blobs"]["bytes"], len(b"orphan bytes"))
        self.assertEqual(report["staging_residue"]["count"], 1)
        self.assertEqual(report["staging_residue"]["bytes"], len(b"staging residue bytes"))
        self.assertNotIn("good payload", json.dumps(report))

        # The audit deleted and repaired nothing.
        self.assertTrue(good_blob.is_file())
        self.assertFalse(missing_blob.exists())
        self.assertTrue(corrupt_blob.is_file())
        self.assertTrue(orphan.is_file())
        self.assertTrue(residue.is_file())

    def test_audit_cli_exit_codes(self) -> None:
        _good, blob, _data = self._artifact("audit-cli", "cli payload\n")
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = str(self.root / "pycache")

        def run_audit() -> subprocess.CompletedProcess:
            return subprocess.run(
                [
                    sys.executable, "-m", "agent_comms.cli",
                    "--db", str(self.db_path), "payload-audit",
                ],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
            )

        clean = run_audit()
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertTrue(json.loads(clean.stdout)["ok"])

        blob.unlink()
        broken = run_audit()
        self.assertEqual(broken.returncode, 1, broken.stderr)
        self.assertFalse(json.loads(broken.stdout)["ok"])

    def test_audit_on_pre_feature_ledger_is_clean(self) -> None:
        with self.store.connection() as conn:
            conn.execute("drop table dispatch_payload_refs")
        report = payload.audit_store(self.db_path)
        self.assertTrue(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 0)

    def test_audit_does_not_treat_malformed_payload_schema_as_legacy(self) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "alter table dispatch_payload_refs rename to dispatch_payload_refs_valid"
            )
            conn.execute("create table dispatch_payload_refs (dispatch_id text)")

        with self.assertRaisesRegex(sqlite3.OperationalError, "no such column"):
            payload.audit_store(self.db_path)


class PayloadAuditNoFollowTest(PayloadHarness):
    """F1: the audit walk never follows a symlink at any store level.

    Each fixture plants a decoy blob-shaped file outside the sibling store
    behind a symlinked level; the audit must classify the symlink as an
    irregular entry and report zero outside counts/bytes, proving the walk
    never traversed outside the store. The replacement-race fixture swaps a
    real shard for a symlink between the audit's directory listing and its
    no-follow open, through the ``_before_audit_entry`` seam.
    """

    DECOY = b"outside decoy bytes"

    def _artifact(self, key: str, text: str) -> tuple[Path, bytes]:
        data = text.encode("utf-8")
        self.write_source(f"{key}.md", data)
        dispatch = self.dispatch_file(key, f"{key}.md")
        sha = self.payload_ref(dispatch["dispatch_id"])["payload_sha256"]
        return self.blob_path(sha), data

    def _outside_shard(self, name: str) -> tuple[Path, Path]:
        outside = self.root / name
        outside.mkdir()
        decoy = outside / ("e" * 64)
        decoy.write_bytes(self.DECOY)
        return outside, decoy

    def _redirect_real_level(self, path: Path, name: str) -> Path:
        """Move real payload bytes outside the store and symlink to them."""
        outside = self.root / name
        path.rename(outside)
        os.symlink(outside, path)
        return outside

    def _assert_referenced_redirect_refused(
        self, report: dict, *, rel_path: str, outside_blob: Path
    ) -> None:
        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"], {"count": 0, "bytes": 0})
        self.assertEqual(report["referenced_missing_or_corrupt"]["count"], 1)
        self.assertEqual(
            report["irregular_entries"]["entries"],
            [{"path": rel_path, "kind": "symlink"}],
        )
        self.assertTrue(outside_blob.is_file())

    def test_clean_store_reports_zero_irregular_entries(self) -> None:
        self._artifact("audit-clean", "clean payload\n")
        report = payload.audit_store(self.db_path)
        self.assertTrue(report["ok"])
        self.assertEqual(report["irregular_entries"], {"count": 0, "entries": []})

    def test_referenced_blob_behind_symlinked_store_root_is_not_read(self) -> None:
        good_blob, _data = self._artifact("audit-store-root-ref", "store root payload\n")
        relative_blob = good_blob.relative_to(self.store_root)
        outside = self._redirect_real_level(self.store_root, "outside-store-root-ref")

        report = payload.audit_store(self.db_path)

        self._assert_referenced_redirect_refused(
            report, rel_path=".", outside_blob=outside / relative_blob
        )

    def test_referenced_blob_behind_symlinked_blobs_root_is_not_read(self) -> None:
        good_blob, _data = self._artifact("audit-blobs-ref", "blobs payload\n")
        blobs = self.store_root / "blobs"
        relative_blob = good_blob.relative_to(blobs)
        outside = self._redirect_real_level(blobs, "outside-blobs-ref")

        report = payload.audit_store(self.db_path)

        self._assert_referenced_redirect_refused(
            report, rel_path="blobs", outside_blob=outside / relative_blob
        )

    def test_referenced_blob_behind_symlinked_sha_root_is_not_read(self) -> None:
        good_blob, _data = self._artifact("audit-sha-ref", "sha root payload\n")
        sha_root = self.store_root / "blobs" / "sha256"
        relative_blob = good_blob.relative_to(sha_root)
        outside = self._redirect_real_level(sha_root, "outside-sha-ref")

        report = payload.audit_store(self.db_path)

        self._assert_referenced_redirect_refused(
            report, rel_path="blobs/sha256", outside_blob=outside / relative_blob
        )

    def test_referenced_blob_behind_symlinked_shard_is_not_read(self) -> None:
        good_blob, _data = self._artifact("audit-shard-ref", "referenced shard payload\n")
        shard = good_blob.parent
        outside = self._redirect_real_level(shard, "outside-shard-ref")

        report = payload.audit_store(self.db_path)

        self._assert_referenced_redirect_refused(
            report,
            rel_path=f"blobs/sha256/{shard.name}",
            outside_blob=outside / good_blob.name,
        )

    def test_symlinked_blobs_root_is_irregular_not_traversed(self) -> None:
        self.store_root.mkdir()
        outside = self.root / "outside-blobs"
        (outside / "sha256" / "ee").mkdir(parents=True)
        decoy = outside / "sha256" / "ee" / ("e" * 64)
        decoy.write_bytes(self.DECOY)
        os.symlink(outside, self.store_root / "blobs")

        report = payload.audit_store(self.db_path)
        self.assertFalse(report["ok"])
        self.assertEqual(report["unreferenced_blobs"], {"count": 0, "bytes": 0})
        self.assertEqual(
            report["irregular_entries"]["entries"],
            [{"path": "blobs", "kind": "symlink"}],
        )
        self.assertTrue(decoy.is_file())

    def test_symlinked_shard_is_irregular_not_traversed(self) -> None:
        good_blob, _data = self._artifact("audit-shard", "shard payload\n")
        outside, decoy = self._outside_shard("outside-shard")
        # A layout-valid hex shard name distinct from the real shard, so the
        # classification exercised is the symlink refusal, not the name gate.
        link_name = "00" if good_blob.parent.name != "00" else "11"
        os.symlink(outside, self.store_root / "blobs" / "sha256" / link_name)

        report = payload.audit_store(self.db_path)
        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 1)
        self.assertEqual(report["unreferenced_blobs"], {"count": 0, "bytes": 0})
        self.assertEqual(
            report["irregular_entries"]["entries"],
            [{"path": f"blobs/sha256/{link_name}", "kind": "symlink"}],
        )
        self.assertTrue(decoy.is_file())
        self.assertTrue(good_blob.is_file())

    def test_symlinked_staging_root_is_irregular_not_traversed(self) -> None:
        self._artifact("audit-staging", "staging payload\n")
        staging = self.store_root / "staging"
        os.rmdir(staging)
        outside = self.root / "outside-staging"
        outside.mkdir()
        residue = outside / "leftover"
        residue.write_bytes(self.DECOY)
        os.symlink(outside, staging)

        report = payload.audit_store(self.db_path)
        self.assertFalse(report["ok"])
        self.assertEqual(report["staging_residue"], {"count": 0, "bytes": 0})
        self.assertEqual(
            report["irregular_entries"]["entries"],
            [{"path": "staging", "kind": "symlink"}],
        )
        self.assertTrue(residue.is_file())

    def test_shard_replaced_by_symlink_mid_walk_fails_closed(self) -> None:
        good_blob, _data = self._artifact("audit-race", "race payload\n")
        shard = good_blob.parent
        shard_rel = f"blobs/sha256/{shard.name}"
        outside_blob = self.root / "outside-race" / good_blob.name

        def swap(rel_path: str) -> None:
            if rel_path == shard_rel and not shard.is_symlink():
                outside = self._redirect_real_level(shard, "outside-race")
                self.assertEqual(outside / good_blob.name, outside_blob)

        with mock.patch.object(payload, "_before_audit_entry", side_effect=swap):
            report = payload.audit_store(self.db_path)

        self._assert_referenced_redirect_refused(
            report, rel_path=shard_rel, outside_blob=outside_blob
        )

    def test_blob_replaced_by_symlink_during_referenced_open_fails_closed(self) -> None:
        good_blob, _data = self._artifact("audit-leaf-race", "leaf race payload\n")
        blob_rel = f"blobs/sha256/{good_blob.parent.name}/{good_blob.name}"
        outside_blob = self.root / "outside-leaf-race"

        def swap(rel_path: str) -> None:
            if rel_path == blob_rel and not good_blob.is_symlink():
                good_blob.rename(outside_blob)
                os.symlink(outside_blob, good_blob)

        with mock.patch.object(payload, "_before_audit_entry", side_effect=swap):
            report = payload.audit_store(self.db_path)

        self._assert_referenced_redirect_refused(
            report, rel_path=blob_rel, outside_blob=outside_blob
        )


class PayloadAuditDetachedChainTest(PayloadHarness):
    """Review F4: the audit cannot vouch for a detached snapshot.

    Retained directory FDs keep following the real directories after a
    rename, so an adversary who moves a canonical component out of the store
    AFTER the audit retained its FD could otherwise get ``ok=true`` for a
    hierarchy the canonical pathname no longer contains. Each fixture swaps
    one level (store root, ``blobs/sha256``, or the digest shard) for a
    symlinked decoy deterministically after the shard FD is retained but
    before the referenced-blob open; the audit must classify the detachment
    as an irregular ``detached`` entry, fail the reference closed, count
    nothing from the decoy, and report ``ok=false``.
    """

    def _artifact(self, key: str) -> tuple[Path, bytes]:
        data = f"detach payload {key}\n".encode()
        self.write_source(f"{key}.md", data)
        dispatch = self.dispatch_file(key, f"{key}.md")
        sha = self.payload_ref(dispatch["dispatch_id"])["payload_sha256"]
        return self.blob_path(sha), data

    def _swap_after_retention(self, level: Path, decoy_name: str) -> tuple[Path, Path]:
        """Build the decoy and return ``(moved, decoy_dir)`` rename targets."""
        moved = self.root / f"{decoy_name}-moved"
        decoy_dir = self.root / decoy_name
        decoy_dir.mkdir()
        return moved, decoy_dir

    def _run_swapped_audit(self, blob_rel: str, level: Path, moved: Path, decoy_dir: Path) -> dict:
        def swap(rel_path: str) -> None:
            if rel_path == blob_rel and not level.is_symlink():
                level.rename(moved)
                level.symlink_to(decoy_dir)

        with mock.patch.object(payload, "_before_audit_entry", side_effect=swap):
            return payload.audit_store(self.db_path)

    def _blob_rel(self, blob: Path) -> str:
        return f"blobs/sha256/{blob.parent.name}/{blob.name}"

    def test_store_root_detached_after_retention_forces_not_ok(self) -> None:
        blob, _data = self._artifact("detach-root")
        moved, decoy_dir = self._swap_after_retention(self.store_root, "detach-root-decoy")
        report = self._run_swapped_audit(self._blob_rel(blob), self.store_root, moved, decoy_dir)

        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"], {"count": 0, "bytes": 0})
        self.assertEqual(report["referenced_missing_or_corrupt"]["count"], 1)
        self.assertEqual(
            report["referenced_missing_or_corrupt"]["entries"][0]["code"],
            "dispatch_payload_missing",
        )
        self.assertEqual(
            report["irregular_entries"]["entries"],
            [{"path": ".", "kind": "detached"}],
        )
        # The real blob survives in the renamed store; the decoy gained
        # nothing and was never counted.
        self.assertTrue((moved / self._blob_rel(blob)).is_file())
        self.assertEqual(list(decoy_dir.iterdir()), [])
        self.assertEqual(report["unreferenced_blobs"], {"count": 0, "bytes": 0})

    def test_sha256_level_detached_after_retention_forces_not_ok(self) -> None:
        blob, _data = self._artifact("detach-sha")
        sha_root = self.store_root / "blobs" / "sha256"
        moved, decoy_dir = self._swap_after_retention(sha_root, "detach-sha-decoy")
        report = self._run_swapped_audit(self._blob_rel(blob), sha_root, moved, decoy_dir)

        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"], {"count": 0, "bytes": 0})
        self.assertEqual(report["referenced_missing_or_corrupt"]["count"], 1)
        self.assertEqual(
            report["irregular_entries"]["entries"],
            [{"path": "blobs/sha256", "kind": "detached"}],
        )
        self.assertTrue((moved / blob.parent.name / blob.name).is_file())
        self.assertEqual(list(decoy_dir.iterdir()), [])

    def test_shard_detached_after_retention_forces_not_ok(self) -> None:
        blob, _data = self._artifact("detach-shard")
        shard = blob.parent
        shard_rel = f"blobs/sha256/{shard.name}"
        moved, decoy_dir = self._swap_after_retention(shard, "detach-shard-decoy")
        decoy_blob = decoy_dir / blob.name
        decoy_blob.write_bytes(b"attacker decoy blob bytes")
        report = self._run_swapped_audit(self._blob_rel(blob), shard, moved, decoy_dir)

        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"], {"count": 0, "bytes": 0})
        self.assertEqual(report["referenced_missing_or_corrupt"]["count"], 1)
        # The detachment is recorded from the post-read chain check, and the
        # later enumeration independently refuses the symlink now sitting at
        # the canonical shard name.
        self.assertEqual(
            sorted(report["irregular_entries"]["entries"], key=lambda e: e["kind"]),
            [
                {"path": shard_rel, "kind": "detached"},
                {"path": shard_rel, "kind": "symlink"},
            ],
        )
        # The real blob survives in the renamed shard; the decoy was never
        # opened, counted, or deleted.
        self.assertTrue((moved / blob.name).is_file())
        self.assertEqual(decoy_blob.read_bytes(), b"attacker decoy blob bytes")
        self.assertEqual(report["unreferenced_blobs"], {"count": 0, "bytes": 0})


class PayloadAuditClosedLayoutTest(PayloadHarness):
    """F1: the audit enforces the closed store layout at every level.

    The store root may contain only ``blobs`` and ``staging``, ``blobs`` only
    ``sha256``, shards only correctly prefixed 64-hex digest names, and
    ``staging`` only ``uuid4().hex`` names. Unexpected names classify from
    the name alone (their targets survive untouched and are never opened),
    symlinked and non-regular entries at the root and ``blobs`` levels
    classify without being followed, and a fixed child that was listed but
    replaced or removed before its open is a replacement race that fails
    closed instead of being tolerated as an absent level.
    """

    DECOY = b"outside decoy bytes"

    def _artifact(self, key: str, text: str) -> tuple[Path, bytes]:
        data = text.encode("utf-8")
        self.write_source(f"{key}.md", data)
        dispatch = self.dispatch_file(key, f"{key}.md")
        sha = self.payload_ref(dispatch["dispatch_id"])["payload_sha256"]
        return self.blob_path(sha), data

    def _entries(self, report: dict) -> list[dict]:
        return report["irregular_entries"]["entries"]

    def test_unexpected_root_entries_are_irregular_and_untouched(self) -> None:
        self._artifact("layout-root", "root layout payload\n")
        intruder = self.store_root / "intruder.txt"
        intruder.write_bytes(b"intruder bytes")
        (self.store_root / "notes").mkdir()
        outside = self.root / "outside-root-decoy"
        outside.mkdir()
        decoy = outside / "decoy"
        decoy.write_bytes(self.DECOY)
        os.symlink(outside, self.store_root / "escape")

        report = payload.audit_store(self.db_path)

        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 1)
        self.assertEqual(
            self._entries(report),
            [
                {"path": "escape", "kind": "unexpected_name"},
                {"path": "intruder.txt", "kind": "unexpected_name"},
                {"path": "notes", "kind": "unexpected_name"},
            ],
        )
        self.assertTrue(intruder.is_file())
        self.assertTrue(decoy.is_file())

    def test_staging_as_regular_file_is_irregular(self) -> None:
        self._artifact("layout-staging-file", "staging file payload\n")
        staging = self.store_root / "staging"
        os.rmdir(staging)
        staging.write_bytes(b"not a directory")

        report = payload.audit_store(self.db_path)

        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 1)
        self.assertEqual(report["staging_residue"], {"count": 0, "bytes": 0})
        self.assertEqual(
            self._entries(report), [{"path": "staging", "kind": "not_a_directory"}]
        )

    def test_unexpected_blobs_entries_are_irregular_and_untouched(self) -> None:
        self._artifact("layout-blobs", "blobs layout payload\n")
        blobs = self.store_root / "blobs"
        junk = blobs / "junk"
        junk.write_bytes(b"junk bytes")
        (blobs / "tmp").mkdir()
        outside = self.root / "outside-blobs-decoy"
        outside.mkdir()
        decoy = outside / "decoy"
        decoy.write_bytes(self.DECOY)
        os.symlink(outside, blobs / "link")

        report = payload.audit_store(self.db_path)

        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 1)
        self.assertEqual(report["unreferenced_blobs"], {"count": 0, "bytes": 0})
        self.assertEqual(
            self._entries(report),
            [
                {"path": "blobs/junk", "kind": "unexpected_name"},
                {"path": "blobs/link", "kind": "unexpected_name"},
                {"path": "blobs/tmp", "kind": "unexpected_name"},
            ],
        )
        self.assertTrue(junk.is_file())
        self.assertTrue(decoy.is_file())

    def test_sha256_as_regular_file_fails_reference_closed(self) -> None:
        self._artifact("layout-sha-file", "sha file payload\n")
        sha_root = self.store_root / "blobs" / "sha256"
        shutil.rmtree(sha_root)
        sha_root.write_bytes(b"not a directory")

        report = payload.audit_store(self.db_path)

        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"], {"count": 0, "bytes": 0})
        self.assertEqual(report["referenced_missing_or_corrupt"]["count"], 1)
        self.assertEqual(
            self._entries(report),
            [{"path": "blobs/sha256", "kind": "not_a_directory"}],
        )

    def test_unexpected_shard_and_blob_names_are_irregular(self) -> None:
        good_blob, _data = self._artifact("layout-names", "shard names payload\n")
        sha_root = self.store_root / "blobs" / "sha256"
        shard = good_blob.parent

        bad_shard = sha_root / "zz"
        bad_shard.mkdir()
        hidden = bad_shard / ("e" * 64)
        hidden.write_bytes(b"never counted bytes")
        (sha_root / "abc").mkdir()

        other_prefix = "00" if shard.name != "00" else "11"
        wrong_shard_blob = shard / (other_prefix + "e" * 62)
        wrong_shard_blob.write_bytes(b"wrong shard bytes")
        not_hex = shard / "not-a-digest"
        not_hex.write_bytes(b"not hex bytes")
        upper = shard / (shard.name + "E" * 62)
        upper.write_bytes(b"upper bytes")

        report = payload.audit_store(self.db_path)

        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 1)
        # None of the layout-violating names is counted as an unreferenced
        # blob, and the hidden decoy inside the malformed shard was never
        # enumerated, so nothing outside the closed layout gains a benign
        # classification.
        self.assertEqual(report["unreferenced_blobs"], {"count": 0, "bytes": 0})
        expected = sorted(
            [
                {"path": "blobs/sha256/zz", "kind": "unexpected_name"},
                {"path": "blobs/sha256/abc", "kind": "unexpected_name"},
                {
                    "path": f"blobs/sha256/{shard.name}/{wrong_shard_blob.name}",
                    "kind": "unexpected_name",
                },
                {"path": f"blobs/sha256/{shard.name}/not-a-digest", "kind": "unexpected_name"},
                {"path": f"blobs/sha256/{shard.name}/{upper.name}", "kind": "unexpected_name"},
            ],
            key=lambda entry: entry["path"],
        )
        self.assertEqual(
            sorted(self._entries(report), key=lambda entry: entry["path"]), expected
        )
        self.assertTrue(hidden.is_file())
        self.assertTrue(wrong_shard_blob.is_file())

    def test_unexpected_staging_names_are_irregular(self) -> None:
        self._artifact("layout-staging-names", "staging names payload\n")
        staging = self.store_root / "staging"
        (staging / ("a" * 32)).mkdir()
        (staging / "leftover-token").write_bytes(b"residue bytes")
        valid = staging / ("b" * 32)
        valid.write_bytes(b"valid residue")

        report = payload.audit_store(self.db_path)

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["staging_residue"], {"count": 1, "bytes": len(b"valid residue")}
        )
        self.assertEqual(
            self._entries(report),
            [
                {"path": f"staging/{'a' * 32}", "kind": "not_a_regular_file"},
                {"path": "staging/leftover-token", "kind": "unexpected_name"},
            ],
        )

    def test_blobs_replaced_by_symlink_between_listing_and_open(self) -> None:
        good_blob, _data = self._artifact("layout-race-blobs", "blobs race payload\n")
        blobs = self.store_root / "blobs"
        relative_blob = good_blob.relative_to(blobs)
        outside = self.root / "outside-blobs-race"

        def swap(rel_path: str) -> None:
            if rel_path == "blobs" and not blobs.is_symlink():
                blobs.rename(outside)
                os.symlink(outside, blobs)

        with mock.patch.object(payload, "_before_audit_entry", side_effect=swap):
            report = payload.audit_store(self.db_path)

        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"], {"count": 0, "bytes": 0})
        self.assertEqual(report["referenced_missing_or_corrupt"]["count"], 1)
        self.assertEqual(
            self._entries(report), [{"path": "blobs", "kind": "symlink"}]
        )
        self.assertTrue((outside / relative_blob).is_file())

    def test_staging_removed_between_listing_and_open_fails_closed(self) -> None:
        self._artifact("layout-race-staging", "staging race payload\n")
        staging = self.store_root / "staging"

        def vanish(rel_path: str) -> None:
            if rel_path == "staging" and staging.is_dir():
                os.rmdir(staging)

        with mock.patch.object(payload, "_before_audit_entry", side_effect=vanish):
            report = payload.audit_store(self.db_path)

        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 1)
        self.assertEqual(
            self._entries(report), [{"path": "staging", "kind": "unavailable"}]
        )


class PayloadRefMetadataAuditTest(PayloadHarness):
    """F2: raw origin/timestamp metadata gates ``referenced_good``.

    The ``payload_origin`` CHECK constraint is bypassable (``pragma
    ignore_check_constraints``) and ``captured_at`` carries no shape CHECK,
    so the ledger can hold values off the closed vocabulary or the canonical
    ``clock.utc_now()`` timestamp shape; an SQL NULL is the one null-like
    shape SQLite refuses to construct (the pragma does not disable NOT
    NULL). Every constructible malformation must classify as the typed
    ``dispatch_payload_metadata_invalid`` instead of counting good or
    leaking a raw coercion error.
    """

    # The TEXT-affinity columns convert stored numerics to text, so the
    # constructible malformations are hostile strings plus raw BLOBs (stored
    # as-is); the BLOB whose bytes decode to valid content proves the
    # validator never eagerly coerces an untrusted value.
    BAD_ORIGINS = (
        "curated_artifact",
        "",
        "authored_brief ",
        "AUTHORED_BRIEF",
        "7",
        b"authored_brief",
    )
    BAD_TIMESTAMPS = (
        "",
        "None",
        "null",
        "0",
        "2026-08-08T09:04:28Z",
        "2026-08-08 09:04:28+00:00",
        "2026-08-08T09:04:28.123456+00:00",
        "2026-08-08T09:04:28+01:00",
        "2026-13-01T00:00:00+00:00",
        "2026-02-30T10:00:00+00:00",
        b"2026-08-08T09:04:28+00:00",
    )

    def _artifact(self, key: str) -> dict:
        self.write_source(f"{key}.md", f"payload for {key}\n".encode())
        return self.dispatch_file(key, f"{key}.md")

    def _force_column(self, dispatch_id: str, column: str, value) -> None:
        with self.store.connection() as conn:
            conn.execute("pragma ignore_check_constraints = on")
            try:
                conn.execute(
                    f"update dispatch_payload_refs set {column} = ? where dispatch_id = ?",
                    (value, dispatch_id),
                )
            finally:
                conn.execute("pragma ignore_check_constraints = off")
        # SQLite retained the malformed value despite the declared constraints.
        self.assertEqual(self.payload_ref(dispatch_id)[column], value)

    def _assert_all_metadata_invalid(self, report: dict, malformed: list[dict]) -> None:
        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 1)
        entries = report["referenced_missing_or_corrupt"]["entries"]
        self.assertEqual(report["referenced_missing_or_corrupt"]["count"], len(malformed))
        self.assertEqual(
            {entry["code"] for entry in entries},
            {"dispatch_payload_metadata_invalid"},
        )
        self.assertEqual(
            {entry["reference_id"] for entry in entries},
            {dispatch["dispatch_id"] for dispatch in malformed},
        )
        self.assertEqual({entry["reference_kind"] for entry in entries}, {"dispatch"})
        # The referenced digests were recorded before validation failed, so
        # the intact blobs of the malformed rows never drift into the benign
        # unreferenced classification.
        self.assertEqual(report["unreferenced_blobs"], {"count": 0, "bytes": 0})

    def test_audit_classifies_check_bypassed_origins(self) -> None:
        self._artifact("origin-good")
        malformed = []
        for index, value in enumerate(self.BAD_ORIGINS):
            dispatch = self._artifact(f"origin-bad-{index}")
            self._force_column(dispatch["dispatch_id"], "payload_origin", value)
            malformed.append(dispatch)

        report = payload.audit_store(self.db_path)
        self._assert_all_metadata_invalid(report, malformed)

    def test_audit_classifies_malformed_timestamps(self) -> None:
        self._artifact("captured-good")
        malformed = []
        for index, value in enumerate(self.BAD_TIMESTAMPS):
            dispatch = self._artifact(f"captured-bad-{index}")
            self._force_column(dispatch["dispatch_id"], "captured_at", value)
            malformed.append(dispatch)

        report = payload.audit_store(self.db_path)
        self._assert_all_metadata_invalid(report, malformed)

    def test_null_metadata_cannot_be_constructed(self) -> None:
        dispatch = self._artifact("metadata-null")
        with self.store.connection() as conn:
            conn.execute("pragma ignore_check_constraints = on")
            for column in ("payload_origin", "captured_at"):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        f"update dispatch_payload_refs set {column} = NULL "
                        "where dispatch_id = ?",
                        (dispatch["dispatch_id"],),
                    )
        report = payload.audit_store(self.db_path)
        self.assertTrue(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 1)


class McpAndAdminCliSurfaceTest(PayloadHarness):
    """§9.3 MCP and admin-CLI compatibility over the real surfaces.

    The seeded workers carry no runtime, so an inline-promoted start lands
    truthfully as ``spawn_failed_message_landed`` after the dispatch, message,
    and payload reference are durably committed; the payload plumbing under
    test is unaffected (the runtime is invoked zero times either way).
    """

    def base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("WAKE_POLICY", None)
        env.pop("WAKE_POLICY_VERSION", None)
        env.pop("AGENT_COMMS_DB", None)
        env["PYTHONPYCACHEPREFIX"] = str(self.root / "pycache")
        return env

    def call_mcp(self, args: list[str], calls: list[dict], env: dict[str, str]) -> list[dict]:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "payload-test", "version": "0.1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            *calls,
        ]
        stdin_payload = "\n".join(json.dumps(message) for message in messages) + "\n"
        result = subprocess.run(
            [sys.executable, "-m", "agent_comms.mcp_server", *args],
            cwd=ROOT,
            env=env,
            input=stdin_payload,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]

    def test_mcp_dispatch_agent_schema_and_file_mode(self) -> None:
        self.write_source("brief.md")
        env = self.base_env()
        args = ["--db", str(self.db_path), "--actor-id", "alpha-architect"]
        list_responses = self.call_mcp(
            args,
            [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}],
            env,
        )
        call_responses = self.call_mcp(
            args,
            [
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "dispatch_agent",
                        "arguments": {
                            "target_actor_id": "alpha-worker",
                            "idempotency_key": "mcp-file",
                            "subject": "File-backed over MCP",
                            "body_file": "brief.md",
                            "payload_origin": "authored_brief",
                        },
                    },
                },
            ],
            env,
        )
        tools_response = next(r for r in list_responses if r.get("id") == 2)
        tools = {tool["name"]: tool for tool in tools_response["result"]["tools"]}
        schema = json.dumps(tools["dispatch_agent"]["inputSchema"])
        self.assertIn("body_file", schema)
        self.assertIn("payload_origin", schema)
        # The source root comes only from the producer registration: no
        # caller-supplied root or identity field exists on the MCP surface.
        self.assertNotIn("source_root", schema)
        self.assertNotIn("producer_actor_id", schema)
        required = tools["dispatch_agent"]["inputSchema"].get("required", [])
        self.assertNotIn("body", required)
        self.assertNotIn("body_file", required)

        call_response = next(r for r in call_responses if r.get("id") == 3)
        text = "\n".join(item.get("text", "") for item in call_response["result"]["content"])
        dispatch = json.loads(text)
        self.assertEqual(dispatch["producer_actor_id"], "alpha-architect")
        ref = self.payload_ref(dispatch["dispatch_id"])
        self.assertEqual(ref["payload_sha256"], PAYLOAD_SHA256)
        self.assertEqual(
            self.store.read_message("alpha-worker", dispatch["message_id"])["body"],
            PAYLOAD_TEXT,
        )

    def test_mcp_traversal_refuses_with_zero_writes(self) -> None:
        (self.root / "escape.md").write_bytes(PAYLOAD_BYTES)
        env = self.base_env()
        args = ["--db", str(self.db_path), "--actor-id", "alpha-architect"]
        responses = self.call_mcp(
            args,
            [
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "dispatch_agent",
                        "arguments": {
                            "target_actor_id": "alpha-worker",
                            "idempotency_key": "mcp-traversal",
                            "subject": "hostile",
                            "body_file": "../escape.md",
                            "payload_origin": "authored_brief",
                        },
                    },
                },
            ],
            env,
        )
        response = next(r for r in responses if r.get("id") == 4)
        self.assertTrue(response["result"].get("isError"))
        text = "\n".join(item.get("text", "") for item in response["result"]["content"])
        self.assertIn("dispatch_payload_path_invalid", text)
        self.assert_zero_dispatch_writes()

    def _operator_env(self) -> dict[str, str]:
        home = self.root / "operator-home"
        secret_dir = home / ".agent-comms"
        secret_dir.mkdir(parents=True, exist_ok=True)
        secret_file = secret_dir / "admin-token"
        secret_file.write_text("operator-secret")
        secret_file.chmod(0o600)
        env = self.base_env()
        env["HOME"] = str(home)
        env["AGENT_COMMS_ADMIN_TOKEN"] = "operator-secret"
        return env

    def _admin_dispatch(self, env: dict[str, str], key: str, extra: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable, "-m", "agent_comms.cli",
                "--db", str(self.db_path),
                "admin", "dispatch",
                "--from-actor-id", HUMAN_ID,
                "--target-actor-id", "alpha-worker",
                "--idempotency-key", key,
                "--requested-policy", WORKER_DISPATCH_POLICY,
                "--override-reason", "incident",
                "--subject", f"Admin {key}",
                *extra,
            ],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        )

    def test_admin_cli_file_mode_explicit_root(self) -> None:
        source_dir = self.root / "operator-briefs"
        source_dir.mkdir()
        (source_dir / "brief.md").write_bytes(PAYLOAD_BYTES)
        env = self._operator_env()

        ok = self._admin_dispatch(
            env,
            "cli-file",
            [
                "--body-file", "brief.md",
                "--payload-origin", "verbatim_source",
                "--source-root", str(source_dir),
            ],
        )
        self.assertEqual(ok.returncode, 0, ok.stderr or ok.stdout)
        dispatch = json.loads(ok.stdout)
        ref = self.payload_ref(dispatch["dispatch_id"])
        self.assertEqual(ref["payload_origin"], "verbatim_source")
        self.assertEqual(
            self.store.read_message("alpha-worker", dispatch["message_id"])["body"],
            PAYLOAD_TEXT,
        )

    def test_admin_cli_file_mode_requires_explicit_root(self) -> None:
        source_dir = self.root / "operator-briefs"
        source_dir.mkdir()
        (source_dir / "brief.md").write_bytes(PAYLOAD_BYTES)
        env = self._operator_env()

        missing_root = self._admin_dispatch(
            env, "cli-noroot", ["--body-file", "brief.md", "--payload-origin", "authored_brief"]
        )
        self.assertEqual(missing_root.returncode, 2, missing_root.stdout)
        self.assertIn("dispatch_payload_path_invalid", missing_root.stdout)

        both = self._admin_dispatch(
            env,
            "cli-both",
            [
                "--body", "inline too",
                "--body-file", "brief.md",
                "--payload-origin", "authored_brief",
                "--source-root", str(source_dir),
            ],
        )
        self.assertEqual(both.returncode, 2, both.stdout)
        self.assertIn("dispatch_body_input_conflict", both.stdout)


class SkewedRestoreTest(PayloadHarness):
    """§9.16 a skewed restore refuses start/read and recovers only with the
    matching store generation."""

    def test_skewed_restore_fails_closed_until_matching_generation(self) -> None:
        self.write_source("brief.md")
        dispatch = self.dispatch_file("skew-1")
        recorded_sha = self.payload_ref(dispatch["dispatch_id"])["payload_sha256"]

        # Simulate restoring a newer database against an older payload store
        # generation that lacks the blob.
        skewed_away = self.root / "store-generation-newer"
        self.store_root.rename(skewed_away)

        adapter = CountingAdapter()
        self.store.start_queued_dispatches(lambda _runtime: adapter, limit=10)
        self.assertEqual(adapter.contexts, [])
        with self.store.connection() as conn:
            row = conn.execute(
                "select status, failure_reason from dispatch_ledger where dispatch_id = ?",
                (dispatch["dispatch_id"],),
            ).fetchone()
        self.assertEqual(row["status"], "spawn_failed_message_landed")
        self.assertTrue(row["failure_reason"].startswith("dispatch_payload_missing"))

        with self.assertRaises(payload.PayloadIntegrityError):
            self.store.read_message("alpha-worker", dispatch["message_id"])
        self.assertEqual(self.recipient_status(dispatch["message_id"]), "sent")

        # The recorded digest is preserved, never rewritten to look complete.
        self.assertEqual(
            self.payload_ref(dispatch["dispatch_id"])["payload_sha256"], recorded_sha
        )
        self.assertFalse(payload.audit_store(self.db_path)["ok"])

        # Restoring the matching store generation makes the same dispatch green.
        skewed_away.rename(self.store_root)
        self.assertTrue(payload.audit_store(self.db_path)["ok"])
        recovered = self.store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: adapter)
        self.assertEqual(recovered["status"], "in_flight")
        self.assertEqual(len(adapter.contexts), 1)
        message = self.store.read_message("alpha-worker", dispatch["message_id"])
        self.assertEqual(message["body"], PAYLOAD_TEXT)


class ContainmentWalkTest(PayloadHarness):
    """Review correction: the dir_fd walk refuses symlinked roots and cannot
    be redirected by swapping a component between verified opens."""

    def test_symlinked_source_root_refuses(self) -> None:
        real_root = self.root / "real-root"
        real_root.mkdir()
        (real_root / "brief.md").write_bytes(PAYLOAD_BYTES)
        link_root = self.root / "link-root"
        link_root.symlink_to(real_root)
        with self.assertRaises(payload.PayloadError) as caught:
            payload.capture_source(link_root, "brief.md")
        self.assertTrue(
            str(caught.exception).startswith("dispatch_payload_path_invalid"),
            f"unexpected refusal: {caught.exception}",
        )
        self.assertIn("symlink", str(caught.exception))

    def test_non_directory_source_root_refuses(self) -> None:
        flat = self.root / "flat-root"
        flat.write_bytes(b"not a directory")
        with self.assertRaises(payload.PayloadError) as caught:
            payload.capture_source(flat, "brief.md")
        self.assertTrue(
            str(caught.exception).startswith("dispatch_payload_path_invalid"),
            f"unexpected refusal: {caught.exception}",
        )

    def test_registered_root_replaced_by_symlink_refuses_dispatch(self) -> None:
        outside = self.root / "outside-root"
        outside.mkdir()
        (outside / "brief.md").write_bytes(b"attacker bytes outside the registered root\n")
        self.producer_root.rmdir()
        self.producer_root.symlink_to(outside)
        self.refusal(
            "dispatch_payload_path_invalid", self.dispatch_file, "symlink-root", "brief.md"
        )
        self.assert_zero_dispatch_writes()

    def test_intermediate_swap_between_component_opens_refuses(self) -> None:
        # The adversary swaps a/b to a symlink deterministically after the
        # walk has opened and verified "a" but before it opens "b". The
        # pathname-based walk this replaces would have followed the symlink
        # out of the source root; the dir_fd walk refuses instead.
        self.write_source("a/b/brief.md")
        outside = self.root / "outside-b"
        outside.mkdir()
        (outside / "brief.md").write_bytes(b"attacker bytes outside the root\n")
        swap_dir = self.producer_root / "a" / "b"

        def swap(rel_so_far: str) -> None:
            if rel_so_far == "a/b":
                swap_dir.rename(self.producer_root / "a" / "b-moved")
                swap_dir.symlink_to(outside)

        with mock.patch.object(payload, "_before_component_open", side_effect=swap):
            exc = self.refusal(
                "dispatch_payload_source_unavailable",
                self.dispatch_file, "swap-mid", "a/b/brief.md",
            )
        self.assertIn("symlink", str(exc))
        self.assert_zero_dispatch_writes()

    def test_leaf_swap_between_component_opens_refuses(self) -> None:
        self.write_source("a/brief.md")
        outside_file = self.root / "outside-brief.md"
        outside_file.write_bytes(b"attacker bytes outside the root\n")
        leaf = self.producer_root / "a" / "brief.md"

        def swap(rel_so_far: str) -> None:
            if rel_so_far == "a/brief.md":
                leaf.unlink()
                leaf.symlink_to(outside_file)

        with mock.patch.object(payload, "_before_component_open", side_effect=swap):
            exc = self.refusal(
                "dispatch_payload_source_unavailable",
                self.dispatch_file, "swap-leaf", "a/brief.md",
            )
        self.assertIn("symlink", str(exc))
        self.assert_zero_dispatch_writes()


class StoreChainNoFollowTest(PayloadHarness):
    """Review correction F1 (r2b): normal publication and load retain a verified
    O_DIRECTORY|O_NOFOLLOW directory-FD chain for the store hierarchy and
    perform the final link, existing-blob re-verification, durability, cleanup,
    and the blob open relative to the retained shard FD. Retained FDs prevent
    symlink traversal, but before returning success both publish and load
    re-open the canonical hierarchy no-follow and confirm the retained chain is
    still attached: a completed parent-component rename/replacement that detached
    the chain from the sibling store fails closed with the typed
    publish/integrity error instead of committing or returning a blob absent
    from the canonical digest path."""

    def test_publish_component_swap_fails_closed_and_cleans_only_linked_blob(self) -> None:
        # The adversary swaps the validated shard directory for a symlink to an
        # attacker directory outside the store after the shard FD is retained
        # but before os.link. The retained-FD link still creates the blob in the
        # real (renamed) shard and the retained-FD fsync makes it durable there,
        # but that directory is now detached from the canonical hierarchy, so the
        # canonical-attachment recheck must fail closed rather than let SQL commit
        # a reference to a blob absent from the canonical digest path. Cleanup
        # must remove exactly the blob this call linked (relative to the retained
        # shard FD) and never the attacker replacement reachable through the
        # swapped pathname.
        self.write_source("brief.md")
        capture = payload.capture_source(self.producer_root, "brief.md")
        staged = payload.stage_payload(self.store_root, capture.data)
        shard = self.blob_path().parent
        moved = self.root / "shard-moved"
        outside = self.root / "outside-shard"
        outside.mkdir()
        # An attacker replacement sitting at the canonical digest name behind the
        # swapped symlink; publish cleanup must never touch it.
        decoy = outside / capture.sha256
        decoy.write_bytes(b"attacker replacement bytes")

        def swap(rel_path: str) -> None:
            shard.rename(moved)
            shard.symlink_to(outside)

        with mock.patch.object(payload, "_before_final_link", side_effect=swap):
            with self.assertRaises(payload.PayloadError) as caught:
                payload.publish_final(self.store_root, staged, capture)
        self.assertTrue(str(caught.exception).startswith("dispatch_payload_publish_failed"))
        self.assertNotIn(LEAK_CANARY, str(caught.exception))
        # The blob this call linked into the renamed real shard was removed
        # relative to the retained shard FD; the attacker replacement reachable
        # only through the swapped pathname was never unlinked or overwritten.
        self.assertFalse((moved / capture.sha256).exists())
        self.assertEqual(decoy.read_bytes(), b"attacker replacement bytes")
        self.assertEqual(self.staging_files(), [])

    def test_publish_attachment_failure_unlink_failure_reports_detached_residue(self) -> None:
        # Same detachment as the decoy proof, but the FD-relative cleanup unlink
        # of the blob this call linked into the renamed shard now fails. The call
        # must not swallow that failure: it raises a typed publish error that
        # preserves the attachment-failure context and reports the orphan residue
        # plus the operational-audit limit (the detached shard is outside the
        # canonical hierarchy the audit walks). The attacker replacement reachable
        # only through the swapped pathname is never touched.
        self.write_source("brief.md")
        capture = payload.capture_source(self.producer_root, "brief.md")
        staged = payload.stage_payload(self.store_root, capture.data)
        shard = self.blob_path().parent
        moved = self.root / "shard-moved-unlink-fault"
        outside = self.root / "outside-shard-unlink-fault"
        outside.mkdir()
        decoy = outside / capture.sha256
        decoy.write_bytes(b"attacker replacement bytes")

        def swap(rel_path: str) -> None:
            shard.rename(moved)
            shard.symlink_to(outside)

        real_unlink = os.unlink

        def failing_unlink(path, *args, **kwargs):
            # Fail only the FD-relative cleanup unlink of the digest leaf, never
            # the staging discard (a plain pathname unlink with no dir_fd).
            if kwargs.get("dir_fd") is not None and path == capture.sha256:
                raise OSError(1, "injected cleanup unlink failure")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(payload, "_before_final_link", side_effect=swap):
            with mock.patch.object(payload.os, "unlink", side_effect=failing_unlink):
                with self.assertRaises(payload.PayloadError) as caught:
                    payload.publish_final(self.store_root, staged, capture)
        message = str(caught.exception)
        self.assertTrue(message.startswith("dispatch_payload_publish_failed"))
        self.assertNotIn(LEAK_CANARY, message)
        self.assertIn("orphan", message)
        self.assertIn("payload-audit", message)
        # The attachment-failure context is preserved, not replaced.
        self.assertIn("attached to the canonical payload store", message)
        # The unlink failed, so the residue is honestly still present; the
        # attacker replacement was never unlinked or overwritten.
        self.assertTrue((moved / capture.sha256).exists())
        self.assertEqual(decoy.read_bytes(), b"attacker replacement bytes")
        self.assertEqual(self.staging_files(), [])

    def test_publish_attachment_failure_cleanup_fsync_failure_reports_uncertainty(self) -> None:
        # Same detachment, the cleanup unlink succeeds, but the shard fsync that
        # makes the removal durable fails. The call must not claim clean cleanup:
        # it raises a typed publish error saying the unlink is not durable and the
        # residue may survive, preserving the attachment-failure context rather
        # than silently re-raising only the attachment mismatch.
        self.write_source("brief.md")
        capture = payload.capture_source(self.producer_root, "brief.md")
        staged = payload.stage_payload(self.store_root, capture.data)
        shard = self.blob_path().parent
        moved = self.root / "shard-moved-fsync-fault"
        outside = self.root / "outside-shard-fsync-fault"
        outside.mkdir()
        decoy = outside / capture.sha256
        decoy.write_bytes(b"attacker replacement bytes")

        def swap(rel_path: str) -> None:
            shard.rename(moved)
            shard.symlink_to(outside)

        real_fsync_fd = payload._fsync_fd
        calls = {"count": 0}

        def failing(fd: int) -> None:
            ordinal = calls["count"]
            calls["count"] += 1
            # 0,1,2 are the created-level parent-FD fsyncs for blobs, sha256,
            # and the shard (staging already exists from stage_payload); 3,4
            # are the post-link durability fsyncs (shard, sha256); 5 is the
            # cleanup durable-removal fsync of the shard FD on the attachment
            # failure path. Fail exactly the cleanup fsync.
            if ordinal == 5:
                raise OSError(5, "injected cleanup fsync failure")
            real_fsync_fd(fd)

        with mock.patch.object(payload, "_before_final_link", side_effect=swap):
            with mock.patch.object(payload, "_fsync_fd", side_effect=failing):
                with self.assertRaises(payload.PayloadError) as caught:
                    payload.publish_final(self.store_root, staged, capture)
        message = str(caught.exception)
        self.assertTrue(message.startswith("dispatch_payload_publish_failed"))
        self.assertNotIn(LEAK_CANARY, message)
        self.assertIn("not durable", message)
        self.assertIn("orphan", message)
        self.assertIn("attached to the canonical payload store", message)
        # The cleanup fsync ran exactly once after the three created-level and
        # two post-link fsyncs.
        self.assertEqual(calls["count"], 6)
        # The unlink itself did happen: the linked blob is gone from the renamed
        # shard, and the attacker replacement was never touched.
        self.assertFalse((moved / capture.sha256).exists())
        self.assertEqual(decoy.read_bytes(), b"attacker replacement bytes")
        self.assertEqual(self.staging_files(), [])

    def test_load_refuses_symlinked_shard_component(self) -> None:
        # A digest-valid blob is moved to an attacker directory outside the
        # store and the validated shard path is replaced by a symlink to it.
        # Digest verification alone would accept the byte-identical copy; only
        # the no-follow directory-FD chain refuses the out-of-store read.
        self.write_source("brief.md")
        self.dispatch_file("load-symlink-shard")
        shard = self.blob_path().parent
        outside = self.root / "outside-shard-dir"
        shutil.move(str(shard), str(outside))
        shard.symlink_to(outside)
        self.assertTrue((outside / PAYLOAD_SHA256).is_file())

        with self.assertRaises(payload.PayloadIntegrityError) as caught:
            payload.load_verified_text(
                self.store_root,
                storage_kind=payload.STORAGE_KIND,
                payload_sha256=PAYLOAD_SHA256,
                byte_count=len(PAYLOAD_BYTES),
                char_count=len(PAYLOAD_TEXT),
            )
        self.assertTrue(str(caught.exception).startswith("dispatch_payload_missing"))
        self.assertNotIn(LEAK_CANARY, str(caught.exception))

    def test_load_component_swap_fails_closed_after_retained_read(self) -> None:
        # The adversary swaps the validated shard for a symlink to an empty
        # directory after the shard FD is retained but before the leaf open. The
        # retained-FD open still reads and digest-verifies the real blob from the
        # renamed shard, but that directory is now detached from the canonical
        # hierarchy, so the canonical-attachment recheck must fail closed rather
        # than return payload text for a blob absent from the canonical digest
        # path. Load never deletes: the real blob survives in the renamed shard
        # and the decoy directory is never written into.
        self.write_source("brief.md")
        self.dispatch_file("load-swap")
        shard = self.blob_path().parent
        moved = self.root / "load-shard-moved"
        empty = self.root / "empty-shard"
        empty.mkdir()

        def swap(rel_path: str) -> None:
            shard.rename(moved)
            shard.symlink_to(empty)

        with mock.patch.object(payload, "_before_load_open", side_effect=swap):
            with self.assertRaises(payload.PayloadIntegrityError) as caught:
                payload.load_verified_text(
                    self.store_root,
                    storage_kind=payload.STORAGE_KIND,
                    payload_sha256=PAYLOAD_SHA256,
                    byte_count=len(PAYLOAD_BYTES),
                    char_count=len(PAYLOAD_TEXT),
                )
        self.assertTrue(str(caught.exception).startswith("dispatch_payload_missing"))
        self.assertNotIn(LEAK_CANARY, str(caught.exception))
        self.assertTrue((moved / PAYLOAD_SHA256).is_file())
        self.assertEqual(list(empty.iterdir()), [])


class StoreDurabilityTest(PayloadHarness):
    """Review corrections (incl. F5): a fresh store hierarchy is made durable
    with one fsync of the retained parent FD per created level (never a
    re-opened parent pathname), before publication or SQL commit returns."""

    # A fresh end-to-end dispatch performs exactly five created-level fsyncs
    # (store_root, staging, blobs, sha256, shard) before the two post-link
    # durability fsyncs of the retained shard and sha256 FDs.
    CREATED_LEVEL_FSYNCS = 5

    @staticmethod
    def _dir_identity(path: Path) -> tuple[int, int]:
        st = os.stat(path)
        return (st.st_dev, st.st_ino)

    def test_fresh_hierarchy_fsyncs_every_created_parent_fd_before_return(self) -> None:
        self.write_source("brief.md")
        fd_synced: list[tuple[int, int]] = []
        real_fsync_fd = payload._fsync_fd

        def recording_fd(fd: int) -> None:
            fd_synced.append(payload._dir_identity(fd))
            real_fsync_fd(fd)

        with mock.patch.object(payload, "_fsync_fd", side_effect=recording_fd):
            dispatch = self.dispatch_file("durable-fresh")
        self.assertEqual(dispatch["status"], "queued")

        store_root = self.store_root
        blobs = store_root / "blobs"
        sha256_dir = blobs / "sha256"
        shard = self.blob_path().parent
        # Every newly created store level fsyncs its RETAINED parent FD in
        # creation order before publication or the SQL commit relies on it
        # (F5: never a re-opened parent pathname), then post-link durability
        # fsyncs the retained shard and sha256 FDs the blob was linked into.
        self.assertEqual(
            fd_synced,
            [
                self._dir_identity(self.db_path.parent),  # store_root created
                self._dir_identity(store_root),           # staging/ created
                self._dir_identity(store_root),           # blobs/ created
                self._dir_identity(blobs),                # blobs/sha256 created
                self._dir_identity(sha256_dir),           # shard created
                self._dir_identity(shard),                # post-link shard
                self._dir_identity(sha256_dir),           # post-link sha256
            ],
        )
        # The pathname-based directory fsync is gone from the module surface.
        self.assertFalse(hasattr(payload, "_fsync_dir"))

    def test_any_created_level_fsync_failure_refuses_with_zero_committed_rows(self) -> None:
        # Fault-inject each of the five created-level parent-FD fsyncs in
        # order; every one must refuse the dispatch before anything commits,
        # leaving no SQL rows, no blob, and no staging residue.
        self.write_source("brief.md")
        real_fsync_fd = payload._fsync_fd
        for fail_ordinal in range(self.CREATED_LEVEL_FSYNCS):
            with self.subTest(fail_ordinal=fail_ordinal):
                calls = {"count": 0}

                def failing(fd: int, _ordinal: int = fail_ordinal) -> None:
                    ordinal = calls["count"]
                    calls["count"] += 1
                    if ordinal == _ordinal:
                        raise OSError(5, "injected fsync failure")
                    real_fsync_fd(fd)

                with mock.patch.object(payload, "_fsync_fd", side_effect=failing):
                    self.refusal(
                        "dispatch_payload_publish_failed",
                        self.dispatch_file, f"durable-created-fault-{fail_ordinal}",
                    )
                self.assertEqual(calls["count"], fail_ordinal + 1)
                self.assert_zero_dispatch_writes()
                # Reset to a fresh hierarchy so the next ordinal exercises the
                # same creation sequence.
                if self.store_root.exists():
                    shutil.rmtree(self.store_root)

    def test_each_post_link_fsync_failure_refuses_with_zero_committed_rows(self) -> None:
        # Fault-inject each of the two post-link durability fsyncs (retained
        # shard FD then retained sha256 FD, ordinals after the five
        # created-level fsyncs); every one must refuse before any commit. A
        # failure triggers the FD-relative cleanup unlink plus one
        # durable-removal fsync of the retained shard FD.
        self.write_source("brief.md")
        real_fsync_fd = payload._fsync_fd
        first_post_link = self.CREATED_LEVEL_FSYNCS
        for fail_ordinal in (first_post_link, first_post_link + 1):
            with self.subTest(fail_ordinal=fail_ordinal):
                calls = {"count": 0}

                def failing(fd: int, _ordinal: int = fail_ordinal) -> None:
                    ordinal = calls["count"]
                    calls["count"] += 1
                    if ordinal == _ordinal:
                        raise OSError(5, "injected fsync failure")
                    real_fsync_fd(fd)

                with mock.patch.object(payload, "_fsync_fd", side_effect=failing):
                    self.refusal(
                        "dispatch_payload_publish_failed",
                        self.dispatch_file, f"durable-postlink-fault-{fail_ordinal}",
                    )
                # The created-level and failing durability fsyncs up to the
                # failure, plus one successful cleanup durable-removal fsync.
                self.assertEqual(calls["count"], fail_ordinal + 1 + 1)
                self.assert_zero_dispatch_writes()
                if self.store_root.exists():
                    shutil.rmtree(self.store_root)


class StagingWriteTest(PayloadHarness):
    """Review correction: staging survives short ``os.write`` returns and
    refuses zero progress, verifying the staged size before fsync."""

    def test_partial_writes_stage_exact_bytes_end_to_end(self) -> None:
        self.write_source("brief.md")
        capture = payload.capture_source(self.producer_root, "brief.md")
        real_write = os.write

        def short_write(fd: int, data) -> int:
            return real_write(fd, bytes(data[:7]))

        with mock.patch.object(payload.os, "write", side_effect=short_write):
            staged = payload.stage_payload(self.store_root, capture.data)
        self.assertEqual(staged.path.read_bytes(), PAYLOAD_BYTES)

        published = payload.publish_final(self.store_root, staged, capture)
        self.addCleanup(published.close)
        self.assertTrue(published.created)
        self.assertEqual(
            payload.load_verified_text(
                self.store_root,
                storage_kind=payload.STORAGE_KIND,
                payload_sha256=capture.sha256,
                byte_count=capture.byte_count,
                char_count=capture.char_count,
            ),
            PAYLOAD_TEXT,
        )

    def test_zero_progress_write_refuses_and_cleans_staging(self) -> None:
        with mock.patch.object(payload.os, "write", return_value=0):
            with self.assertRaises(payload.PayloadError) as caught:
                payload.stage_payload(self.store_root, PAYLOAD_BYTES)
        message = str(caught.exception)
        self.assertTrue(message.startswith("dispatch_payload_publish_failed"))
        self.assertIn("zero progress", message)
        self.assertEqual(self.staging_files(), [])

    def test_write_overreporting_fails_staged_size_verification(self) -> None:
        # A write seam that claims completion without persisting bytes must be
        # caught by the fstat size verification before fsync/publication.
        with mock.patch.object(payload.os, "write", side_effect=lambda fd, data: len(data)):
            with self.assertRaises(payload.PayloadError) as caught:
                payload.stage_payload(self.store_root, PAYLOAD_BYTES)
        message = str(caught.exception)
        self.assertTrue(message.startswith("dispatch_payload_publish_failed"))
        self.assertIn(f"expected {len(PAYLOAD_BYTES)}", message)
        self.assertEqual(self.staging_files(), [])


class StagingCustodyTest(PayloadHarness):
    """Review F2: staging create/write/unlink and the final source link are
    FD-relative through retained O_DIRECTORY|O_NOFOLLOW custody.

    A component swap at the staging pathname can neither redirect the staged
    bytes into an attacker decoy nor let publication link an inode other than
    the verified staged one; a detached staging chain refuses with zero
    committed rows, and a staged-entry replacement is caught by independent
    verification of the newly linked inode before durability or SQL success.
    """

    def _swap_staging_for_decoy(self, suffix: str) -> tuple[Path, Path]:
        staging = self.store_root / "staging"
        moved = self.root / f"staging-moved-{suffix}"
        decoy = self.root / f"staging-decoy-{suffix}"
        decoy.mkdir()
        staging.rename(moved)
        staging.symlink_to(decoy)
        return moved, decoy

    def test_staging_dir_swap_at_create_refuses_decoy_untouched_zero_rows(self) -> None:
        # The adversary swaps the validated staging directory for a symlink to
        # a decoy directory after the staging FD chain is retained but before
        # the exclusive create. The old pathname create would have written the
        # payload into the decoy; the FD-relative create writes into the real
        # (renamed) directory, the decoy receives nothing, and the detached
        # staging chain refuses the dispatch before any SQL write, cleaning
        # the staged entry out of the real directory FD-relative.
        self.write_source("brief.md")
        swapped: dict[str, tuple[Path, Path]] = {}

        def swap(rel_path: str) -> None:
            if not swapped:
                swapped["dirs"] = self._swap_staging_for_decoy("create")

        with mock.patch.object(payload, "_before_staging_create", side_effect=swap):
            self.refusal(
                "dispatch_payload_publish_failed", self.dispatch_file, "staging-swap-create"
            )
        moved, decoy = swapped["dirs"]
        self.assertEqual(list(decoy.iterdir()), [])
        self.assertEqual(list(moved.iterdir()), [])
        self.assertEqual(self.blob_files(), [])
        self.assertEqual(self.counts()["dispatch_ledger"], 0)
        self.assertEqual(self.counts()["messages"], 0)

    def test_staging_dir_swap_during_write_refuses_decoy_untouched_zero_rows(self) -> None:
        # Same swap, landed deterministically between the exclusive create and
        # the first write. The retained file FD keeps writing the real inode
        # in the real directory; the decoy receives nothing and the detached
        # chain refuses with zero committed rows.
        self.write_source("brief.md")
        swapped: dict[str, tuple[Path, Path]] = {}
        real_write = os.write

        def swapping_write(fd: int, data):
            if not swapped:
                swapped["dirs"] = self._swap_staging_for_decoy("write")
            return real_write(fd, data)

        with mock.patch.object(payload.os, "write", side_effect=swapping_write):
            self.refusal(
                "dispatch_payload_publish_failed", self.dispatch_file, "staging-swap-write"
            )
        moved, decoy = swapped["dirs"]
        self.assertEqual(list(decoy.iterdir()), [])
        self.assertEqual(list(moved.iterdir()), [])
        self.assertEqual(self.blob_files(), [])
        self.assertEqual(self.counts()["dispatch_ledger"], 0)
        self.assertEqual(self.counts()["messages"], 0)

    def test_source_link_resolves_retained_staging_fd_not_swapped_pathname(self) -> None:
        # The adversary swaps the staging directory for a symlink to a decoy
        # directory carrying a same-named decoy entry after staging but before
        # the final link. A pathname link source would have published the
        # decoy bytes under the victim digest; the src_dir_fd link resolves
        # the retained real directory, publishes the exact verified inode,
        # and never reads the decoy.
        self.write_source("brief.md")
        capture = payload.capture_source(self.producer_root, "brief.md")
        staged = payload.stage_payload(self.store_root, capture.data)
        staging = self.store_root / "staging"
        moved = self.root / "staging-moved-link"
        decoy_dir = self.root / "staging-decoy-link"
        decoy_dir.mkdir()
        decoy = decoy_dir / staged.name
        decoy.write_bytes(b"attacker decoy bytes")

        def swap(rel_path: str) -> None:
            if not staging.is_symlink():
                staging.rename(moved)
                staging.symlink_to(decoy_dir)

        with mock.patch.object(payload, "_before_final_link", side_effect=swap):
            published = payload.publish_final(self.store_root, staged, capture)
        self.addCleanup(published.close)
        self.assertTrue(published.created)
        self.assertEqual(published.path.read_bytes(), PAYLOAD_BYTES)
        self.assertEqual(decoy.read_bytes(), b"attacker decoy bytes")

    def test_staged_entry_replacement_before_link_refuses_and_removes_link(self) -> None:
        # The adversary replaces the staged entry inside the REAL staging
        # directory with a different inode between write and link. The link
        # itself cannot be redirected, but the linked inode is no longer the
        # verified one: the independent post-link verification against the
        # retained staged-file FD must remove exactly the entry this call
        # linked and refuse before durability or SQL success, with zero
        # committed rows end to end.
        self.write_source("brief.md")
        capture = payload.capture_source(self.producer_root, "brief.md")
        staged = payload.stage_payload(self.store_root, capture.data)
        staged_entry = staged.path

        def replace(rel_path: str) -> None:
            if staged_entry.exists():
                staged_entry.unlink()
                staged_entry.write_bytes(b"Q" * len(PAYLOAD_BYTES))

        with mock.patch.object(payload, "_before_final_link", side_effect=replace):
            with self.assertRaises(payload.PayloadError) as caught:
                payload.publish_final(self.store_root, staged, capture)
        message = str(caught.exception)
        self.assertTrue(message.startswith("dispatch_payload_publish_failed"))
        self.assertIn("not the", message)
        self.assertNotIn(LEAK_CANARY, message)
        # The linked replacement was removed from the canonical digest path
        # and nothing was published or left staged.
        self.assertFalse(self.blob_path(capture.sha256).exists())
        self.assertEqual(self.blob_files(), [])
        self.assertEqual(self.staging_files(), [])


class RetryFactoryOrderTest(PayloadHarness):
    """Review correction: retry preflight precedes runtime-adapter
    construction, and a raising factory settles the row truthfully."""

    def _lineage_claimed_at(self, dispatch_id: str):
        with self.store.connection() as conn:
            return conn.execute(
                "select auth_lineage_claimed_at from dispatch_ledger where dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()["auth_lineage_claimed_at"]

    def test_corrupt_payload_constructs_zero_adapters_on_retry(self) -> None:
        self.write_source("brief.md")
        dispatch = self.dispatch_file("retry-factory-zero")
        self.blob_path().unlink()
        failed_start = CountingAdapter()
        self.store.start_queued_dispatches(lambda _runtime: failed_start, limit=10)
        self.assertEqual(failed_start.contexts, [])

        factory_calls: list[str] = []

        def counting_factory(runtime: str) -> CountingAdapter:
            factory_calls.append(runtime)
            return CountingAdapter()

        row = self.store.retry_spawn(dispatch["dispatch_id"], counting_factory)
        self.assertEqual(factory_calls, [])
        self.assertEqual(row["status"], "spawn_failed_message_landed")
        self.assertTrue(row["failure_reason"].startswith("dispatch_payload_missing"))
        self.assertIsNone(self._lineage_claimed_at(dispatch["dispatch_id"]))

    def test_raising_factory_settles_truthfully_and_releases_lineage(self) -> None:
        self.write_source("brief.md")
        dispatch = self.dispatch_file("retry-factory-raise")
        failing_adapter = CountingAdapter(fail=True)
        self.store.start_queued_dispatches(lambda _runtime: failing_adapter, limit=10)

        def exploding_factory(runtime: str) -> CountingAdapter:
            raise RuntimeError("injected factory explosion")

        row = self.store.retry_spawn(dispatch["dispatch_id"], exploding_factory)
        self.assertEqual(row["status"], "spawn_failed_message_landed")
        self.assertIn("injected factory explosion", row["failure_reason"])
        self.assertIsNone(self._lineage_claimed_at(dispatch["dispatch_id"]))

        # The released lineage lets a later healthy retry succeed.
        recovered_adapter = CountingAdapter()
        recovered = self.store.retry_spawn(
            dispatch["dispatch_id"], lambda _runtime: recovered_adapter
        )
        self.assertEqual(recovered["status"], "in_flight")
        self.assertEqual(len(recovered_adapter.contexts), 1)


class PostLinkDurabilityTest(PayloadHarness):
    """Review correction: a post-link durability failure unlinks the blob this
    call created under the held write lock, or reports measurable orphan
    residue when even that fails."""

    def _fail_post_link_fsync(self, target: str):
        # Post-link durability fsyncs a retained directory FD, so distinguish
        # the shard and sha256 targets by directory identity rather than by
        # pathname, and only fail while the blob is still present so the cleanup
        # durable-removal fsync of the shard FD is allowed to succeed.
        real_fsync_fd = payload._fsync_fd
        blob = self.blob_path()

        def identity(path: Path) -> tuple[int, int]:
            st = os.stat(path)
            return (st.st_dev, st.st_ino)

        def failing(fd: int) -> None:
            here = payload._dir_identity(fd)
            if blob.exists():
                if target == "shard" and here == identity(blob.parent):
                    raise OSError(5, "injected post-link fsync failure")
                if target == "sha256" and here == identity(blob.parent.parent):
                    raise OSError(5, "injected post-link fsync failure")
            real_fsync_fd(fd)

        return failing

    def test_each_post_link_fsync_failure_unlinks_created_blob(self) -> None:
        self.write_source("brief.md")
        for target in ("shard", "sha256"):
            with self.subTest(target=target):
                with mock.patch.object(
                    payload, "_fsync_fd", side_effect=self._fail_post_link_fsync(target)
                ):
                    exc = self.refusal(
                        "dispatch_payload_publish_failed",
                        self.dispatch_file, f"postlink-{target}",
                    )
                self.assertIn("durable", str(exc))
                self.assertNotIn("orphan", str(exc))
                self.assert_zero_dispatch_writes()

    def test_post_link_cleanup_unlink_runs_under_write_lock(self) -> None:
        self.write_source("brief.md")
        blob = self.blob_path()
        lock_probe: dict[str, bool] = {}
        real_unlink = os.unlink

        def probing_unlink(path, *args, **kwargs):
            # The FD-relative cleanup unlink targets the digest leaf name through
            # the retained shard dir_fd.
            if path == blob.name and "dir_fd" in kwargs:
                probe = sqlite3.connect(self.db_path, timeout=0.05)
                try:
                    probe.execute("begin immediate")
                except sqlite3.OperationalError:
                    lock_probe["held_at_unlink"] = True
                else:
                    lock_probe["held_at_unlink"] = False
                    probe.rollback()
                finally:
                    probe.close()
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(
            payload, "_fsync_fd", side_effect=self._fail_post_link_fsync("shard")
        ):
            with mock.patch.object(payload.os, "unlink", side_effect=probing_unlink):
                self.refusal(
                    "dispatch_payload_publish_failed", self.dispatch_file, "postlink-lock"
                )
        self.assertEqual(lock_probe.get("held_at_unlink"), True)
        self.assert_zero_dispatch_writes()

    def test_post_link_cleanup_fsync_failure_reports_orphan_uncertainty(self) -> None:
        # The post-link fsync fails, the cleanup unlink succeeds, but the
        # cleanup-directory fsync that makes the removal durable also fails:
        # the refusal must report the durability uncertainty, not clean
        # cleanup.
        self.write_source("brief.md")
        blob = self.blob_path()
        real_fsync_fd = payload._fsync_fd

        def shard_identity() -> tuple[int, int]:
            st = os.stat(blob.parent)
            return (st.st_dev, st.st_ino)

        def failing(fd: int) -> None:
            # The shard FD is fsynced post-link (blob present) and again by the
            # cleanup pass after the unlink (blob absent); fail both. (The
            # shard existence guard skips the earlier created-level fsyncs.)
            if blob.parent.is_dir() and payload._dir_identity(fd) == shard_identity():
                raise OSError(5, "injected shard fsync failure")
            real_fsync_fd(fd)

        with mock.patch.object(payload, "_fsync_fd", side_effect=failing):
            exc = self.refusal(
                "dispatch_payload_publish_failed", self.dispatch_file, "postlink-cleanup-fsync"
            )
        message = str(exc)
        self.assertIn("not durable", message)
        self.assertIn("orphan", message)
        self.assertIn("payload-audit", message)
        self.assertNotIn(LEAK_CANARY, message)
        self.assertFalse(blob.exists())
        self.assert_zero_dispatch_writes()

    def test_post_link_cleanup_fsync_runs_under_write_lock(self) -> None:
        self.write_source("brief.md")
        blob = self.blob_path()
        lock_probe: dict[str, bool] = {}
        real_fsync_fd = payload._fsync_fd

        def shard_identity() -> tuple[int, int]:
            st = os.stat(blob.parent)
            return (st.st_dev, st.st_ino)

        def probing(fd: int) -> None:
            if not blob.parent.is_dir():
                real_fsync_fd(fd)
                return
            here = payload._dir_identity(fd)
            if here == shard_identity() and blob.exists():
                raise OSError(5, "injected post-link fsync failure")
            if here == shard_identity() and not blob.exists():
                probe = sqlite3.connect(self.db_path, timeout=0.05)
                try:
                    probe.execute("begin immediate")
                except sqlite3.OperationalError:
                    lock_probe["held_at_cleanup_fsync"] = True
                else:
                    lock_probe["held_at_cleanup_fsync"] = False
                    probe.rollback()
                finally:
                    probe.close()
            real_fsync_fd(fd)

        with mock.patch.object(payload, "_fsync_fd", side_effect=probing):
            self.refusal(
                "dispatch_payload_publish_failed", self.dispatch_file, "postlink-cleanup-lock"
            )
        # The cleanup-durability fsync ran while the caller still held the
        # BEGIN IMMEDIATE write lock.
        self.assertEqual(lock_probe.get("held_at_cleanup_fsync"), True)
        self.assert_zero_dispatch_writes()

    def test_post_link_fsync_and_unlink_failure_reports_orphan(self) -> None:
        self.write_source("brief.md")
        blob = self.blob_path()
        real_unlink = os.unlink

        def failing_unlink(path, *args, **kwargs):
            if path == blob.name and "dir_fd" in kwargs:
                raise OSError(1, "injected unlink failure")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(
            payload, "_fsync_fd", side_effect=self._fail_post_link_fsync("shard")
        ):
            with mock.patch.object(payload.os, "unlink", side_effect=failing_unlink):
                with self.assertRaises(ValidationError) as caught:
                    self.dispatch_file("postlink-orphan")
        message = str(caught.exception)
        self.assertTrue(message.startswith("dispatch_payload_publish_failed"))
        self.assertIn("orphan", message)
        self.assertIn("payload-audit", message)
        # The stranded blob is truthfully reported and measurable; nothing
        # was committed.
        self.assertTrue(blob.is_file())
        report = payload.audit_store(self.db_path)
        self.assertFalse(report["ok"])
        self.assertEqual(report["unreferenced_blobs"]["count"], 1)
        self.assertEqual(self.counts()["dispatch_ledger"], 0)
