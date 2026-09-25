"""Executable proofs for file-backed ``send_message`` bodies."""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import code_identity, db as db_module, payload
from agent_comms.mailbox import Mailbox
from agent_comms.schema import ValidationError
from agent_comms.store import Store


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/send-message-marker-collision-claude-v1.txt"
FIXTURE_SHA = "91e6b17ce61330c74c5f02510a15e8ceb667d36830d9c9c4bcaf8ab630992355"


class MessagePayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "ledger.sqlite"
        self.sender_root = self.root / "sender"
        self.sender_root.mkdir()
        self.store = Store(self.db_path)
        self.store.register_agent_actor(
            "alpha-architect", "alpha", "architect", str(self.sender_root), []
        )
        self.store.register_agent_actor(
            "alpha-worker",
            "alpha",
            "worker",
            str(self.root / "worker"),
            [],
            owner="alpha-architect",
        )

    def write(self, name: str = "body.txt", data: bytes | None = None) -> Path:
        path = self.sender_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(FIXTURE.read_bytes() if data is None else data)
        return path

    def send(self, name: str = "body.txt") -> dict:
        return self.store.send_message(
            "alpha-architect", ["alpha-worker"], "subject", None, [], body_file=name
        )

    def counts(self) -> tuple[int, ...]:
        with self.store.connection() as conn:
            return tuple(
                conn.execute(f"select count(*) from {table}").fetchone()[0]
                for table in (
                    "messages",
                    "message_recipients",
                    "message_threads",
                    "message_payload_refs",
                )
            )

    def test_exactly_one_input_is_required_before_writes(self) -> None:
        """spec proof 1"""
        before = self.counts()
        for body, body_file in ((None, None), ("inline", "body.txt")):
            with (
                self.subTest(body=body, body_file=body_file),
                self.assertRaises(ValidationError),
            ):
                self.store.send_message(
                    "alpha-architect",
                    ["alpha-worker"],
                    "subject",
                    body,
                    [],
                    body_file=body_file,
                )
            self.assertEqual(self.counts(), before)

    def test_source_refusal_classes(self) -> None:
        """spec proof 2"""
        self.write("empty", b"")
        self.write("bad", b"\xff")
        self.write("nul", b"a\0b")
        self.write("large", b"x" * (payload.FILE_BODY_MAX_BYTES + 1))
        (self.sender_root / "dir").mkdir()
        os.symlink(self.sender_root / "body.txt", self.sender_root / "link")
        (self.sender_root / "real-dir").mkdir()
        self.write("real-dir/body.txt")
        os.symlink(self.sender_root / "real-dir", self.sender_root / "linkdir")
        cases = [
            "/absolute",
            "../escape",
            "missing",
            "empty",
            "bad",
            "nul",
            "large",
            "dir",
            "link",
            "linkdir/body.txt",
        ]
        for name in cases:
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.send(name)
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.sender_root / "fifo")
            with self.assertRaises(ValidationError):
                self.send("fifo")
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_unstable_source_refuses(self) -> None:
        """spec proof 3"""
        source = self.write()
        real = os.stat(source)
        altered = type(
            "Stat",
            (),
            {
                name: getattr(real, name)
                for name in (
                    "st_mode",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                    "st_dev",
                    "st_ino",
                )
            },
        )()
        altered.st_ino += 1
        with (
            mock.patch.object(payload, "_fstat_after_read", return_value=altered),
            self.assertRaises(ValidationError),
        ):
            self.send()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_source_changes_do_not_change_message(self) -> None:
        """spec proof 5"""
        source = self.write()
        expected = source.read_text()
        message = self.send()
        source.write_text("changed")
        source.unlink()
        self.assertEqual(
            self.store.read_message("alpha-worker", message["id"])["body"], expected
        )

    def test_worker_nonreply_refuses_before_resolution(self) -> None:
        """spec proof 6"""
        worker_root = self.root / "worker"
        worker_root.mkdir()
        (worker_root / "body.txt").write_bytes(FIXTURE.read_bytes())
        from agent_comms.mcp_server import create_server

        class FakeMCP:
            def __init__(self, _name: str) -> None:
                self.tools = {}

            def tool(self):
                def decorate(func):
                    self.tools[func.__name__] = func
                    return func

                return decorate

        with (
            mock.patch("agent_comms.mcp_server.require_mcp", return_value=FakeMCP),
            mock.patch.dict(
                os.environ,
                {
                    "WAKE_POLICY": "worker_dispatch_readwrite_bounded",
                    "WAKE_POLICY_VERSION": "v2",
                },
            ),
        ):
            server = create_server(db_path=str(self.db_path), actor_id="alpha-worker")
        with (
            mock.patch(
                "agent_comms.payload.capture_source",
                side_effect=AssertionError("capture_source must not be called"),
            ),
            self.assertRaises(ValidationError) as caught,
        ):
            server.tools["send_message"](
                ["alpha-architect"], "subject", None, [], body_file="body.txt"
            )
        self.assertIn("permits send_message only as a reply", str(caught.exception))
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        parent = self.store.send_message(
            "alpha-architect", ["alpha-worker"], "parent", "inline", []
        )
        reply = server.tools["send_message"](
            ["alpha-architect"],
            "subject",
            None,
            [],
            parent_message_id=parent["id"],
            body_file="body.txt",
        )
        self.assertEqual(
            self.store.read_message("alpha-architect", reply["id"])["body"],
            FIXTURE.read_text(),
        )

    def test_floor_three_schema_and_prior_reader_refusal(self) -> None:
        """spec proof 7"""
        self.write()
        self.send()
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("pragma user_version").fetchone()[0], 3)
            self.assertIsNotNone(
                conn.execute("select 1 from message_payload_refs").fetchone()
            )
        with mock.patch.object(db_module, "LEDGER_SCHEMA_VERSION", 2):
            with self.assertRaises(ValidationError) as caught:
                db_module.Database(self.db_path, is_default_db_open=True).init()
        self.assertIn("ledger_user_version=3", str(caught.exception))
        self.assertIn("code_LEDGER_SCHEMA_VERSION=2", str(caught.exception))

        fresh_db = self.root / "fresh.sqlite"
        fresh = Store(fresh_db)
        fresh.register_agent_actor(
            "fresh-architect", "fresh", "architect", str(self.root / "fresh"), []
        )
        fresh.register_agent_actor(
            "fresh-worker",
            "fresh",
            "worker",
            str(self.root / "fresh-worker"),
            [],
            owner="fresh-architect",
        )
        inline = fresh.send_message(
            "fresh-architect", ["fresh-worker"], "inline", "original body", []
        )
        with sqlite3.connect(fresh_db) as conn:
            conn.execute("drop table message_payload_refs")
            conn.execute("pragma user_version = 2")
        Store(fresh_db).init()
        with sqlite3.connect(fresh_db) as conn:
            self.assertEqual(conn.execute("pragma user_version").fetchone()[0], 3)
            self.assertIsNotNone(
                conn.execute(
                    "select 1 from sqlite_master where type='table' and name='message_payload_refs'"
                ).fetchone()
            )
        restored = fresh.read_message("fresh-worker", inline["id"])
        self.assertEqual(restored["body_storage"], "inline")
        self.assertEqual(restored["body"], "original body")

    def test_atomic_refusals_and_existing_blob_reuse(self) -> None:
        """spec proof 8a"""
        source = self.write()
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        store_root = payload.store_root_for_db(self.db_path)
        blob = payload.blob_path(store_root, digest)
        before = self.counts()
        semaphore = (
            self.root / "worker" / ".agent-comms" / "alpha-worker" / "new_messages"
        )
        for body, body_file in ((None, None), ("inline", "body.txt")):
            with self.assertRaises(ValidationError):
                self.store.send_message(
                    "alpha-architect",
                    ["alpha-worker"],
                    "subject",
                    body,
                    [],
                    body_file=body_file,
                )
            self.assertEqual(self.counts(), before)
            self.assertFalse(blob.exists())
            self.assertFalse(semaphore.exists())

        with (
            mock.patch.object(
                payload, "publish_final", side_effect=RuntimeError("boom")
            ),
            self.assertRaises(RuntimeError),
        ):
            self.send()
        self.assertEqual(self.counts(), before)
        self.assertFalse(blob.exists())
        self.assertEqual(list((store_root / "staging").iterdir()), [])
        self.assertFalse(semaphore.exists())

        with (
            mock.patch.object(
                Mailbox, "_insert_message", side_effect=RuntimeError("boom")
            ),
            self.assertRaises(RuntimeError),
        ):
            self.send()
        self.assertEqual(self.counts(), before)
        self.assertFalse(blob.exists())
        self.assertEqual(list((store_root / "staging").iterdir()), [])
        self.assertFalse(semaphore.exists())

        first = self.send()
        expected = blob.read_bytes()
        after_first = self.counts()
        semaphore.unlink()
        self.write("same.txt", expected)
        with (
            mock.patch.object(
                Mailbox, "_insert_message", side_effect=RuntimeError("boom")
            ),
            self.assertRaises(RuntimeError),
        ):
            self.send("same.txt")
        self.assertEqual(self.counts(), after_first)
        self.assertEqual(blob.read_bytes(), expected)
        self.assertEqual(
            self.store.read_message("alpha-worker", first["id"])["body"],
            expected.decode("utf-8"),
        )
        self.assertEqual(list((store_root / "staging").iterdir()), [])
        self.assertFalse(semaphore.exists())

    def test_cleanup_uncertainty_is_typed(self) -> None:
        """spec proof 8b"""
        source = self.write()
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        store_root = payload.store_root_for_db(self.db_path)
        blob = payload.blob_path(store_root, digest)

        injections = ("identity", "unlink")
        for injection in injections:
            with self.subTest(injection=injection):
                real_unlink = payload.os.unlink

                def fail_digest(path, *args, **kwargs):
                    if kwargs.get("dir_fd") is not None and str(path) == digest:
                        raise OSError("injected unlink")
                    return real_unlink(path, *args, **kwargs)

                cleanup_patch = (
                    mock.patch.object(
                        payload,
                        "rollback_created_blob",
                        side_effect=payload.PayloadError(
                            "dispatch_payload_publish_failed",
                            "injected identity check failure",
                        ),
                    )
                    if injection == "identity"
                    else mock.patch.object(
                        payload.os, "unlink", side_effect=fail_digest
                    )
                )
                with (
                    mock.patch.object(
                        Mailbox, "_insert_message", side_effect=RuntimeError("boom")
                    ),
                    cleanup_patch,
                    self.assertRaises(payload.PayloadError) as caught,
                ):
                    self.send()
                self.assertEqual(caught.exception.code, "cleanup_uncertain")
                self.assertIn(injection, str(caught.exception))
                self.assertEqual(self.counts(), (0, 0, 0, 0))
                report = payload.audit_store(self.db_path)
                self.assertFalse(report["ok"])
                self.assertEqual(report["unreferenced_blobs"]["count"], 1)
                blob.unlink()

        real_fsync_fd = payload._fsync_fd

        def fail_shard_fsync(fd: int) -> None:
            if (
                blob.parent.is_dir()
                and not blob.exists()
                and payload._dir_identity(fd)
                == (
                    os.stat(blob.parent).st_dev,
                    os.stat(blob.parent).st_ino,
                )
            ):
                raise OSError(5, "injected cleanup fsync failure")
            real_fsync_fd(fd)

        with (
            mock.patch.object(
                Mailbox, "_insert_message", side_effect=RuntimeError("boom")
            ),
            mock.patch.object(payload, "_fsync_fd", side_effect=fail_shard_fsync),
            self.assertRaises(payload.PayloadError) as caught,
        ):
            self.send()
        self.assertEqual(caught.exception.code, "cleanup_uncertain")
        self.assertIn("fsync", str(caught.exception))
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertFalse(blob.exists())
        semaphore = (
            self.root / "worker" / ".agent-comms" / "alpha-worker" / "new_messages"
        )
        self.assertFalse(semaphore.exists())

    def test_audit_unions_message_and_dispatch_references(self) -> None:
        """spec proof 9"""
        self.write()
        message = self.send()
        report = payload.audit_store(self.db_path)
        self.assertTrue(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 1)
        with self.store.connection() as conn:
            ref = conn.execute(
                "select * from message_payload_refs where message_id=?",
                (message["id"],),
            ).fetchone()
            created_at = conn.execute(
                "select created_at from messages where id=?", (message["id"],)
            ).fetchone()[0]
            conn.execute(
                "insert into dispatch_ledger(dispatch_id,idempotency_key,message_id,thread_ref,recipient_actor_id,producer_actor_id,originating_actor_id,policy_name,policy_version,policy_issued_by,status,created_at,closed_at,observed_values_json) values(?,?,?,?,?,?,?,?,?,?, 'closed',?,?, '{}')",
                (
                    "dispatch_audit",
                    "audit",
                    message["id"],
                    message["id"],
                    "alpha-worker",
                    "alpha-architect",
                    "alpha-architect",
                    "worker_dispatch_readwrite_bounded",
                    "v1",
                    "alpha-architect",
                    created_at,
                    created_at,
                ),
            )
            conn.execute(
                "insert into dispatch_payload_refs(dispatch_id,storage_kind,payload_sha256,byte_count,char_count,payload_origin,captured_at) values(?,?,?,?,?,?,?)",
                (
                    "dispatch_audit",
                    ref["storage_kind"],
                    ref["payload_sha256"],
                    ref["byte_count"],
                    ref["char_count"],
                    "authored_brief",
                    ref["captured_at"],
                ),
            )
        report = payload.audit_store(self.db_path)
        self.assertTrue(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 2)
        with self.store.connection() as conn:
            conn.execute("pragma ignore_check_constraints = on")
            conn.execute(
                "update dispatch_payload_refs set payload_origin='invalid' where dispatch_id='dispatch_audit'"
            )
        report = payload.audit_store(self.db_path)
        self.assertFalse(report["ok"])
        self.assertEqual(report["referenced_good"]["count"], 1)
        bad_dispatch = report["referenced_missing_or_corrupt"]["entries"][0]
        self.assertEqual(
            (bad_dispatch["reference_kind"], bad_dispatch["reference_id"]),
            ("dispatch", "dispatch_audit"),
        )
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_payload_refs set payload_origin='authored_brief' where dispatch_id='dispatch_audit'"
            )
        payload.blob_path(
            payload.store_root_for_db(self.db_path), ref["payload_sha256"]
        ).write_bytes(b"corrupt")
        bad = next(
            entry
            for entry in payload.audit_store(self.db_path)[
                "referenced_missing_or_corrupt"
            ]["entries"]
            if entry["reference_kind"] == "message"
        )
        self.assertEqual(
            (bad["reference_kind"], bad["reference_id"]), ("message", message["id"])
        )

    def test_dual_binding_refuses_all_read_paths(self) -> None:
        """spec proof 10"""
        self.write()
        message = self.send()
        with self.store.connection() as conn:
            now = conn.execute(
                "select created_at from messages where id=?", (message["id"],)
            ).fetchone()[0]
            conn.execute(
                "insert into dispatch_ledger(dispatch_id,idempotency_key,message_id,thread_ref,recipient_actor_id,producer_actor_id,originating_actor_id,policy_name,policy_version,policy_issued_by,status,created_at,closed_at,observed_values_json) values(?,?,?,?,?,?,?,?,?,?, 'closed',?,?, '{}')",
                (
                    "dispatch_dual",
                    "dual",
                    message["id"],
                    message["id"],
                    "alpha-worker",
                    "alpha-architect",
                    "alpha-architect",
                    "worker_dispatch_readwrite_bounded",
                    "v1",
                    "alpha-architect",
                    now,
                    now,
                ),
            )
            ref = conn.execute(
                "select * from message_payload_refs where message_id=?",
                (message["id"],),
            ).fetchone()
            conn.execute(
                "insert into dispatch_payload_refs(dispatch_id,storage_kind,payload_sha256,byte_count,char_count,payload_origin,captured_at) values(?,?,?,?,?,?,?)",
                (
                    "dispatch_dual",
                    ref["storage_kind"],
                    ref["payload_sha256"],
                    ref["byte_count"],
                    ref["char_count"],
                    "authored_brief",
                    ref["captured_at"],
                ),
            )
        for call in (
            lambda: self.store.read_message("alpha-worker", message["id"]),
            lambda: self.store.list_inbox("alpha-worker", unread_only=False),
            lambda: self.store.list_unread(),
            lambda: self.store.wait_for_reply("alpha-worker", timeout_seconds=0),
        ):
            with self.assertRaises(payload.PayloadError) as caught:
                call()
            self.assertEqual(caught.exception.code, "payload_binding_ambiguous")
            with self.store.connection() as conn:
                status = conn.execute(
                    "select status from message_recipients where message_id=? and to_agent='alpha-worker'",
                    (message["id"],),
                ).fetchone()[0]
            self.assertEqual(status, "sent")

    def test_orphan_and_staging_are_non_green(self) -> None:
        """audit predicate"""
        root = payload.store_root_for_db(self.db_path)
        orphan = payload.blob_path(root, "a" * 64)
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan")
        self.assertFalse(payload.audit_store(self.db_path)["ok"])
        orphan.unlink()
        staging = root / "staging" / ("b" * 32)
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"staging")
        self.assertFalse(payload.audit_store(self.db_path)["ok"])

    def test_contract_version_digest_and_spec_a(self) -> None:
        """contract identity"""
        self.assertEqual(code_identity.CONTRACT_VERSION, 23)
        self.assertEqual(
            code_identity.contract_surface_digest(),
            code_identity.CONTRACT_SURFACE_DIGEST,
        )
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_SHA)
