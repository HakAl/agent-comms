"""Stage-2 T7: the pinned credentialed admin ``settle-dispatch`` interaction.

Exercises the REAL CLI parser + handler (``agent_comms.cli.run``) against
temporary databases for BOTH the dry-run preview and the sealed execution, plus
the store-level production entry points where deterministic clock control is
needed (expiry / replay-after-expiry).

An operator emergency-settlement releases a dispatch whose sanctioned
cancellation is stuck ``requested`` with termination UNCONFIRMED. It is NOT a
cancel: it commits the exceptional ``dlq``/``cancelled`` terminal under
``operator_settled_termination_unconfirmed`` (observed
``termination_result=termination_not_confirmed``) WITHOUT halting, signalling,
deleting residue, claiming the native child died, or marking the cancellation
confirmed.

This module pins:

* the dry-run credential gate, human-actor requirement, eligibility, mechanical
  DB + filesystem non-mutation, the canonical five-minute sealed plan, and the
  literal execution command with NO caller-copied snapshot/expected-value args;
* the parser rejecting a missing release flag and any invented expected-value
  argument, and the two disjoint modes;
* execution's independent credential + plan re-verification, exact snapshot
  recheck, terminal ``dlq``/``cancelled`` write, complete audit, bounded
  evidence, cap/lineage release, the single producer blocker notice, and the
  tamper / forgery / mismatch / drift / expiry-before-first-success refusals;
* exact-replay idempotency (including after plan expiry) and the ``never expose
  the credential/HMAC secret`` guarantee.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from agent_comms import cli
from agent_comms.cli import _helpers
from agent_comms.cli import settlement_plan as sp
from agent_comms.dispatch_ledger import (
    CancellationConflictError,
    SETTLEMENT_FAILURE_REASON,
    SETTLEMENT_KEY,
    SETTLEMENT_TERMINATION_RESULT,
)
from agent_comms.store import Store

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"
OTHER_HUMAN_ID = "01J00000000000000000000002"
ARCH_ID = "arch"
WORKER_ID = "wrk"
VALID_TOKEN = "operator-secret"
CLAIMED_AT = "2026-07-18T04:00:00+00:00"


class _RaisingAdapter:
    """A runtime adapter whose HALT never confirms termination."""

    def halt(self, spawn_handle, observed):  # noqa: ANN001 - test double
        raise RuntimeError("supervisor unreachable")


def _raising_adapter_for(_runtime):  # noqa: ANN001 - test double
    return _RaisingAdapter()


class SettlementCliBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        self.store.register_actor(HUMAN_ID, "human", "alice")
        self.store.register_actor(OTHER_HUMAN_ID, "human", "pat")
        self.store.register_agent_actor(ARCH_ID, "alpha", "architect", str(self.tmp / "arch"), [])
        self.store.register_agent_actor(
            WORKER_ID,
            "alpha",
            "worker",
            str(self.tmp / "wrk"),
            [],
            runtime="stub",
            spawn={"command": "stub"},
            owner=ARCH_ID,
        )

        self.token_path = self.tmp / "admin-token"
        self.token_path.write_text(VALID_TOKEN)
        os.chmod(self.token_path, 0o600)
        patcher = mock.patch.object(_helpers, "ADMIN_TOKEN_PATH", self.token_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self._saved_token = os.environ.get("AGENT_COMMS_ADMIN_TOKEN")
        self.addCleanup(self._restore_token)
        os.environ["AGENT_COMMS_ADMIN_TOKEN"] = VALID_TOKEN

    def _restore_token(self) -> None:
        if self._saved_token is None:
            os.environ.pop("AGENT_COMMS_ADMIN_TOKEN", None)
        else:
            os.environ["AGENT_COMMS_ADMIN_TOKEN"] = self._saved_token

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- run helpers ---------------------------------------------------- #
    def _run(self, argv: list[str]) -> tuple[int, dict, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cli.run(["--db", str(self.db_path), *argv])
        text = buffer.getvalue().strip()
        payload = json.loads(text) if text else {}
        return rc, payload, text

    def _dry_run(self, dispatch_id: str, *, reason: str = "operator withdraw", actor: str = HUMAN_ID):
        return self._run(
            [
                "admin",
                "settle-dispatch",
                "--from-actor-id",
                actor,
                "--dispatch-id",
                dispatch_id,
                "--reason",
                reason,
                "--dry-run",
            ]
        )

    def _execute(self, dispatch_id: str, plan: str, *, actor: str = HUMAN_ID, release: bool = True, reason=None):
        argv = [
            "admin",
            "settle-dispatch",
            "--from-actor-id",
            actor,
            "--dispatch-id",
            dispatch_id,
            "--execute-plan",
            plan,
        ]
        if release:
            argv.append("--release-with-termination-unconfirmed")
        if reason is not None:
            argv += ["--reason", reason]
        return self._run(argv)

    # --- state helpers -------------------------------------------------- #
    def _queued(self, key: str) -> dict:
        return self.store.dispatch_agent(ARCH_ID, WORKER_ID, key, f"S {key}", f"B {key}", [])

    def _claim(self, dispatch_id: str) -> None:
        with self.store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set auth_lineage_claimed_at = ? where dispatch_id = ?",
                (CLAIMED_AT, dispatch_id),
            )

    def _make_pending(self, key: str, *, in_flight: bool = False, run_token: str | None = None) -> str:
        """A queued(claimed)/in_flight row carrying a pending ``requested`` admin cancel."""
        d = self._queued(key)
        did = d["dispatch_id"]
        if in_flight:
            token = run_token or f"rt-{key}"
            observed = {"run_token": token, "control_socket": f"sock-{key}"}
            with self.store._db.connection() as conn:
                conn.execute(
                    """
                    update dispatch_ledger
                    set status = 'in_flight',
                        spawn_handle = ?,
                        expected_close_by = ?,
                        auth_lineage_claimed_at = ?,
                        observed_values_json = ?
                    where dispatch_id = ?
                    """,
                    (
                        f"sup:{WORKER_ID}:{did}",
                        "2026-07-18T05:00:00+00:00",
                        CLAIMED_AT,
                        json.dumps(observed, sort_keys=True),
                        did,
                    ),
                )
        else:
            self._claim(did)
        # No adapter -> the request is recorded PENDING and never driven, so it
        # stays ``requested`` with termination unconfirmed.
        result = self.store.request_cancellation(
            did, requesting_actor_id=HUMAN_ID, reason=f"cancel {key}", authority="admin"
        )
        self.assertEqual(result["cancellation_state"], "requested")
        return did

    def _store_preview(self, dispatch_id: str, *, reason: str = "operator withdraw", actor: str = HUMAN_ID, issued=None, nonce=None) -> dict:
        return self.store.settle_dispatch_preview(
            dispatch_id,
            actor_id=actor,
            reason=reason,
            secret=VALID_TOKEN,
            issued_at=issued,
            nonce=nonce,
        )

    def _ledger(self, dispatch_id: str):
        with self.store._db.connection() as conn:
            return conn.execute(
                "select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
            ).fetchone()

    def _observed(self, dispatch_id: str) -> dict:
        return json.loads(self._ledger(dispatch_id)["observed_values_json"] or "{}")

    def _transport(self, dispatch_id: str) -> str | None:
        row = self._ledger(dispatch_id)
        with self.store._db.connection() as conn:
            r = conn.execute(
                "select status from message_recipients where message_id = ? and to_agent = ?",
                (row["message_id"], row["recipient_actor_id"]),
            ).fetchone()
        return None if r is None else r["status"]

    def _producer_notices(self, phrase: str = "ledger released; termination not confirmed"):
        with self.store._db.connection() as conn:
            rows = conn.execute(
                """
                select m.id, m.subject, m.body, m.priority, m.from_agent, m.requires_ack
                from messages m
                join message_recipients r on r.message_id = m.id
                where r.to_agent = ?
                order by m.created_at, m.id
                """,
                (ARCH_ID,),
            ).fetchall()
        return [r for r in rows if phrase in r["body"]]

    def _db_fingerprint(self) -> tuple:
        with self.store._db.connection() as conn:
            ledger = conn.execute(
                "select dispatch_id, status, failure_reason, dlq_at, cancelled_at, "
                "auth_lineage_claimed_at, observed_values_json "
                "from dispatch_ledger order by dispatch_id"
            ).fetchall()
            recips = conn.execute(
                "select message_id, to_agent, status, cancelled_at "
                "from message_recipients order by message_id, to_agent"
            ).fetchall()
            msg_count = conn.execute("select count(*) as c from messages").fetchone()["c"]
        return (
            tuple(tuple(r) for r in ledger),
            tuple(tuple(r) for r in recips),
            msg_count,
        )

    def _files(self) -> set:
        return {p.name for p in self.tmp.iterdir()}

    def _quiesce_ledger(self) -> None:
        """Checkpoint-truncate the WAL so the ledger is a quiescent existing DB.

        Models the realistic dry-run precondition: the preview reads an existing,
        settled-to-disk ledger. After a TRUNCATE checkpoint the -wal is empty and
        the read-only (``mode=ro``) preview connection opens and reads the main
        database directly without creating or mutating any sidecar.
        """
        with self.store._db.connection() as conn:
            conn.execute("pragma wal_checkpoint(truncate)")

    def _main_db_sha256(self) -> str:
        """SHA-256 of the MAIN database file bytes (application ledger image).

        Excludes the SQLite-managed ``-wal``/``-shm`` read-coordination sidecars: a
        ``mode=ro`` reader that observes the live committed WAL may touch those, but
        it never mutates the application ledger image, user_version, or rows.
        """
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    # SQLite's own read-coordination sidecars: a dry-run's live-WAL read may
    # create/remove these, but it creates no application artifact of its own.
    _SIDECARS = frozenset({"agent-comms.sqlite-wal", "agent-comms.sqlite-shm"})

    def _logical_snapshot(self) -> tuple:
        """user_version + schema + ledger/recipient rows, read strictly read-only."""
        with self.store._db.read_only_connection_ctx() as conn:
            user_version = conn.execute("pragma user_version").fetchone()[0]
            schema = tuple(
                row[0]
                for row in conn.execute(
                    "select sql from sqlite_master order by type, name"
                ).fetchall()
            )
            ledger = conn.execute(
                "select dispatch_id, status, failure_reason, dlq_at, cancelled_at, "
                "auth_lineage_claimed_at, observed_values_json "
                "from dispatch_ledger order by dispatch_id"
            ).fetchall()
            recips = conn.execute(
                "select message_id, to_agent, status, cancelled_at "
                "from message_recipients order by message_id, to_agent"
            ).fetchall()
            msg_count = conn.execute("select count(*) as c from messages").fetchone()["c"]
        return (
            user_version,
            schema,
            tuple(tuple(r) for r in ledger),
            tuple(tuple(r) for r in recips),
            msg_count,
        )


class SettlementDryRunTest(SettlementCliBase):
    def test_dry_run_emits_canonical_sealed_plan_and_snapshot(self) -> None:
        did = self._make_pending("dry", in_flight=True)
        rc, payload, text = self._dry_run(did, reason="  operator withdraw  ")
        self.assertEqual(rc, 0)
        self.assertEqual(payload["mode"], "dry_run")
        # Canonical shell-safe v1 wire form.
        self.assertRegex(payload["plan"], r"^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
        # The reason is normalized (trimmed) and bound in the plan.
        self.assertEqual(payload["reason"], "operator withdraw")
        snap = payload["snapshot"]
        # The complete signed snapshot binds producer + recipient identity, ledger
        # status, transport status, spawn handle, run-token fingerprint, and the
        # ENTIRE cancellation-request object (not merely its state).
        self.assertEqual(snap["producer_actor_id"], ARCH_ID)
        self.assertEqual(snap["recipient_actor_id"], WORKER_ID)
        self.assertEqual(snap["dispatch_status"], "in_flight")
        self.assertEqual(snap["transport_status"], "sent")
        self.assertEqual(snap["spawn_handle"], f"sup:{WORKER_ID}:{did}")
        self.assertIsInstance(snap["cancellation_request"], dict)
        self.assertEqual(snap["cancellation_request"]["state"], "requested")
        self.assertEqual(snap["cancellation_request"]["authority"], "admin")
        self.assertEqual(snap["cancellation_request"]["requested_by"], HUMAN_ID)
        # The snapshot binds a run-token FINGERPRINT, never the raw token.
        self.assertRegex(snap["run_token_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertNotIn("rt-dry", text)
        # The sealed plan re-verifies under the real credential and re-exposes the
        # exact snapshot and a five-minute expiry.
        claim = sp.verify_plan(secret=VALID_TOKEN, plan=payload["plan"], now=payload["issued_at"])
        self.assertEqual(claim["snapshot"], snap)
        self.assertEqual(claim["actor_id"], HUMAN_ID)
        self.assertEqual(claim["dispatch_id"], did)
        expiry = datetime.fromisoformat(payload["issued_at"]) + timedelta(seconds=300)
        self.assertEqual(payload["expires_at"], expiry.isoformat(timespec="seconds"))

    def test_dry_run_execution_command_is_literal_and_flag_only(self) -> None:
        did = self._make_pending("cmd", in_flight=True)
        _, payload, _ = self._dry_run(did)
        cmd = payload["execution_command"]
        self.assertIn("settle-dispatch", cmd)
        self.assertIn(f"--from-actor-id {HUMAN_ID}", cmd)
        self.assertIn(f"--dispatch-id {did}", cmd)
        # The pinned execution flag is exactly ``--execute-plan``; the old
        # ``--plan`` spelling is gone from the generated command.
        self.assertIn(f"--execute-plan {payload['plan']}", cmd)
        self.assertNotIn("--plan ", cmd)
        self.assertIn("--release-with-termination-unconfirmed", cmd)
        # NO caller-copied snapshot / expected-value arguments are printed.
        self.assertNotIn("--snapshot", cmd)
        self.assertNotIn("--expected", cmd)
        self.assertNotIn("--dispatch-status", cmd)
        # The credential is read from the token file, never inlined.
        self.assertNotIn(VALID_TOKEN, cmd)

    def test_dry_run_execution_command_shell_quotes_metacharacter_human_id(self) -> None:
        # A VALID human actor ID may carry shell metacharacters; the generated
        # command must shell-safe quote every opaque value so it cannot break out.
        evil_id = "alice; rm -rf $HOME && echo pwned`id`"
        self.store.register_actor(evil_id, "human", "evil-but-valid")
        did = self._make_pending("metachar", in_flight=True)
        _, payload, _ = self._dry_run(did, actor=evil_id)
        cmd = payload["execution_command"]
        import shlex

        self.assertIn(f"--from-actor-id {shlex.quote(evil_id)}", cmd)
        self.assertIn(f"--execute-plan {shlex.quote(payload['plan'])}", cmd)
        # The raw, unquoted metacharacter ID never appears as a bare token.
        self.assertNotIn(f"--from-actor-id {evil_id} ", cmd)

    def test_dry_run_does_not_mutate_ledger_content_or_create_artifacts(self) -> None:
        did = self._make_pending("nomut", in_flight=True)
        before_db = self._db_fingerprint()
        before_files = self._files()
        rc, payload, _ = self._dry_run(did)
        self.assertEqual(rc, 0)
        # No application/ledger mutation: rows, statuses, and audits are identical.
        self.assertEqual(self._db_fingerprint(), before_db)
        # SQLite may add/remove its own WAL/SHM read-coordination sidecars, but the
        # dry run creates NO application artifact of its own; nothing else changes.
        self.assertLessEqual(self._files() - before_files, self._SIDECARS)
        self.assertLessEqual(before_files - self._files(), self._SIDECARS)
        self.assertNotIn(SETTLEMENT_KEY, self._observed(did))
        self.assertEqual(self._ledger(did)["status"], "in_flight")

    def test_dry_run_does_not_mutate_ledger_content_against_existing_ledger(self) -> None:
        did = self._make_pending("bytepure", in_flight=True)
        # Quiesce to a stable on-disk ledger, then prove the read-only preview
        # mutates NOTHING in the application ledger: the MAIN database image bytes,
        # user_version, schema, and rows are identical afterwards. A ``mode=ro``
        # live-WAL read may touch the SQLite-managed -wal/-shm sidecars only; that
        # is read coordination, not a ledger mutation.
        self._quiesce_ledger()
        before_main = self._main_db_sha256()
        before_logical = self._logical_snapshot()
        rc, payload, _ = self._dry_run(did)
        self.assertEqual(rc, 0)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(self._main_db_sha256(), before_main)
        self.assertEqual(self._logical_snapshot(), before_logical)

    def test_preview_reflects_committed_wal_not_stale_main_image(self) -> None:
        # The exact uncheckpointed-WAL reproduction at the preview seam: a preview
        # must bind the LIVE committed cancellation, never a stale main image.
        did = self._make_pending("walrepro", in_flight=True)
        # 1. Checkpoint the OLD (eligible) cancellation into the MAIN image.
        self._quiesce_ledger()
        stale = self._store_preview(did)["snapshot"]["cancellation_request"]["reason"]
        self.assertEqual(stale, "cancel walrepro")

        # 2. Commit a CONFLICTING cancellation update ONLY into the WAL, with a
        #    writer that stays open (autocheckpoint disabled) so it is never folded
        #    into the main image.
        observed = self._observed(did)
        observed["cancellation"]["reason"] = "WAL-CURRENT-COMMITTED"
        writer = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("pragma wal_autocheckpoint = 0")
        writer.execute("begin immediate")
        writer.execute(
            "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
            (json.dumps(observed, sort_keys=True), did),
        )
        writer.execute("commit")

        # 3. An ordinary live reader observes the NEW committed value (via the WAL).
        reader = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        try:
            live = reader.execute(
                "select observed_values_json from dispatch_ledger where dispatch_id = ?",
                (did,),
            ).fetchone()[0]
        finally:
            reader.close()
        self.assertEqual(json.loads(live)["cancellation"]["reason"], "WAL-CURRENT-COMMITTED")

        # 4. The preview must bind the WAL-CURRENT-COMMITTED value, never the stale
        #    main image, and the sealed plan re-verifies against that live snapshot.
        preview = self._store_preview(did)
        self.assertEqual(
            preview["snapshot"]["cancellation_request"]["reason"], "WAL-CURRENT-COMMITTED"
        )
        claim = sp.verify_plan(secret=VALID_TOKEN, plan=preview["plan"], now=preview["issued_at"])
        self.assertEqual(
            claim["snapshot"]["cancellation_request"]["reason"], "WAL-CURRENT-COMMITTED"
        )

    def test_dry_run_requires_registered_human(self) -> None:
        did = self._make_pending("nonhuman", in_flight=True)
        rc, payload, _ = self._dry_run(did, actor=ARCH_ID)
        self.assertEqual(rc, 2)
        self.assertIn("human", payload["error"])
        self.assertNotIn(SETTLEMENT_KEY, self._observed(did))

    def test_dry_run_refuses_ineligible_states(self) -> None:
        # A queued row with NO cancellation is ineligible.
        clean = self._queued("clean")["dispatch_id"]
        rc, payload, _ = self._dry_run(clean)
        self.assertEqual(rc, 2)
        self.assertIn("no pending unconfirmed cancellation", payload["error"])
        # A terminal (closed) row is ineligible and never relabelled. A v2 row
        # is only ``closed`` with a checked result (ledger CHECK).
        closed = self._make_pending("closedish", in_flight=True)
        with self.store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status = 'closed', result = 'satisfied' "
                "where dispatch_id = ?",
                (closed,),
            )
        rc, payload, _ = self._dry_run(closed)
        self.assertEqual(rc, 2)
        self.assertIn("closed", payload["error"])

    def test_dry_run_requires_reason_and_rejects_execution_flags(self) -> None:
        did = self._make_pending("badflags", in_flight=True)
        # Missing --reason under --dry-run.
        rc, payload, _ = self._run(
            ["admin", "settle-dispatch", "--from-actor-id", HUMAN_ID, "--dispatch-id", did, "--dry-run"]
        )
        self.assertEqual(rc, 2)
        self.assertIn("--reason", payload["error"])
        # --dry-run refuses execution-only flags.
        rc, payload, _ = self._run(
            [
                "admin",
                "settle-dispatch",
                "--from-actor-id",
                HUMAN_ID,
                "--dispatch-id",
                did,
                "--reason",
                "x",
                "--dry-run",
                "--release-with-termination-unconfirmed",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertIn("--dry-run does not accept", payload["error"])


class SettlementParserTest(SettlementCliBase):
    def test_parser_rejects_invented_expected_value_arguments(self) -> None:
        did = self._make_pending("invent", in_flight=True)
        _, payload, _ = self._dry_run(did)
        plan = payload["plan"]
        # An invented --expected-* / --snapshot argument is an unrecognized
        # argument the parser rejects (SystemExit), never a silently-accepted
        # caller-copied expected value.
        for bad in (
            ["--expected-dispatch-status", "in_flight"],
            ["--snapshot", "{}"],
            ["--expected-run-token", "rt-invent"],
        ):
            with self.subTest(bad=bad[0]):
                with self.assertRaises(SystemExit):
                    cli.run(
                        [
                            "--db",
                            str(self.db_path),
                            "admin",
                            "settle-dispatch",
                            "--from-actor-id",
                            HUMAN_ID,
                            "--dispatch-id",
                            did,
                            "--execute-plan",
                            plan,
                            "--release-with-termination-unconfirmed",
                            *bad,
                        ]
                    )

    def test_parser_rejects_the_old_plan_spelling(self) -> None:
        # The incorrect ``--plan`` spelling is removed: the parser rejects it as
        # an unrecognized argument (SystemExit), never a silent alias for
        # ``--execute-plan``.
        did = self._make_pending("oldspell", in_flight=True)
        _, payload, _ = self._dry_run(did)
        with self.assertRaises(SystemExit):
            cli.run(
                [
                    "--db",
                    str(self.db_path),
                    "admin",
                    "settle-dispatch",
                    "--from-actor-id",
                    HUMAN_ID,
                    "--dispatch-id",
                    did,
                    "--plan",
                    payload["plan"],
                    "--release-with-termination-unconfirmed",
                ]
            )

    def test_parser_rejects_both_modes_together(self) -> None:
        # ``--dry-run`` and ``--execute-plan`` are a pinned, mutually exclusive
        # required group: presenting both is a parser error (SystemExit).
        did = self._make_pending("bothmodes", in_flight=True)
        _, payload, _ = self._dry_run(did)
        with self.assertRaises(SystemExit):
            cli.run(
                [
                    "--db",
                    str(self.db_path),
                    "admin",
                    "settle-dispatch",
                    "--from-actor-id",
                    HUMAN_ID,
                    "--dispatch-id",
                    did,
                    "--dry-run",
                    "--execute-plan",
                    payload["plan"],
                ]
            )

    def test_parser_requires_exactly_one_mode(self) -> None:
        # Neither mode -> the required mutually exclusive group errors (SystemExit).
        did = self._make_pending("nomode", in_flight=True)
        with self.assertRaises(SystemExit):
            cli.run(
                [
                    "--db",
                    str(self.db_path),
                    "admin",
                    "settle-dispatch",
                    "--from-actor-id",
                    HUMAN_ID,
                    "--dispatch-id",
                    did,
                    "--reason",
                    "x",
                ]
            )

    def test_parser_requires_from_actor_and_dispatch_in_both_modes(self) -> None:
        with self.assertRaises(SystemExit):
            cli.run(["--db", str(self.db_path), "admin", "settle-dispatch", "--dry-run", "--reason", "x"])
        with self.assertRaises(SystemExit):
            cli.run(["--db", str(self.db_path), "admin", "settle-dispatch", "--from-actor-id", HUMAN_ID])

    def test_execute_missing_release_ack_refuses_in_preflight_before_store(self) -> None:
        # A pinned execute request without the literal release acknowledgement is
        # an invalid mode combination refused in preflight (rc 2), before Store.
        did = self._make_pending("norelack", in_flight=True)
        _, payload, _ = self._dry_run(did)
        rc, out, _ = self._run(
            [
                "admin",
                "settle-dispatch",
                "--from-actor-id",
                HUMAN_ID,
                "--dispatch-id",
                did,
                "--execute-plan",
                payload["plan"],
            ]
        )
        self.assertEqual(rc, 2)
        self.assertIn("--release-with-termination-unconfirmed", out["error"])
        self.assertEqual(self._ledger(did)["status"], "in_flight")
        self.assertNotIn(SETTLEMENT_KEY, self._observed(did))


class SettlementExecuteHappyPathTest(SettlementCliBase):
    def test_full_cli_dry_run_then_execute_settles_to_dlq_cancelled(self) -> None:
        did = self._make_pending("happy", in_flight=True)
        _, preview, _ = self._dry_run(did, reason="stuck cancellation, release")
        plan = preview["plan"]

        rc, result, text = self._execute(did, plan)
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "dlq")
        self.assertEqual(result["outcome"], "operator_settled_termination_unconfirmed")
        self.assertEqual(result["failure_reason"], SETTLEMENT_FAILURE_REASON)
        self.assertEqual(result["termination_result"], SETTLEMENT_TERMINATION_RESULT)
        self.assertTrue(result["settled"])
        self.assertEqual(result["settled_by"], HUMAN_ID)
        self.assertTrue(result["lineage_released"])
        self.assertNotIn(VALID_TOKEN, text)

        row = self._ledger(did)
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(row["failure_reason"], SETTLEMENT_FAILURE_REASON)
        self.assertIsNotNone(row["dlq_at"])
        self.assertEqual(row["auth_lineage_claimed_at"], "2026-07-18T04:00:00+00:00")
        self.assertEqual(self._transport(did), "cancelled")

        observed = self._observed(did)
        self.assertEqual(observed["termination_result"], SETTLEMENT_TERMINATION_RESULT)
        # The cancellation is NEVER marked confirmed by a settlement.
        self.assertEqual(observed["cancellation"]["state"], "requested")

        audit = observed[SETTLEMENT_KEY]
        self.assertEqual(audit["actor_id"], HUMAN_ID)
        self.assertEqual(audit["reason"], "stuck cancellation, release")
        self.assertEqual(audit["snapshot"], preview["snapshot"])
        self.assertEqual(audit["plan_fingerprint"], sp.plan_fingerprint(plan))
        self.assertTrue(audit["nonce"])
        self.assertEqual(audit["failure_reason"], SETTLEMENT_FAILURE_REASON)
        self.assertEqual(audit["termination_result"], SETTLEMENT_TERMINATION_RESULT)
        self.assertEqual(
            audit["partial_evidence"]["classification"], "operator_settlement_partial_work"
        )
        # The durable audit binds the producer-notice message ID, and the result
        # surfaces the same identity: a durable claim carries its durable notice.
        notice_id = audit["producer_notice_message_id"]
        self.assertTrue(notice_id)
        self.assertEqual(result["producer_notice_message_id"], notice_id)
        notices = self._producer_notices()
        self.assertEqual([n["id"] for n in notices], [notice_id])
        # The verified secret is never persisted anywhere in the row.
        self.assertNotIn(VALID_TOKEN, json.dumps(observed))

    def test_execute_sends_exactly_one_producer_blocker_notice(self) -> None:
        did = self._make_pending("notice", in_flight=True)
        _, preview, _ = self._dry_run(did)
        self._execute(did, preview["plan"])
        notices = self._producer_notices()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["priority"], "blocker")
        self.assertEqual(notices[0]["from_agent"], HUMAN_ID)
        self.assertIn("ledger released; termination not confirmed", notices[0]["body"])
        self.assertNotIn(VALID_TOKEN, notices[0]["body"])

    def test_execute_settles_a_queued_claimed_pending_row(self) -> None:
        did = self._make_pending("queuedpend", in_flight=False)
        _, preview, _ = self._dry_run(did)
        self.assertEqual(preview["snapshot"]["dispatch_status"], "queued")
        self.assertIsNone(preview["snapshot"]["spawn_handle"])
        self.assertIsNone(preview["snapshot"]["run_token_fingerprint"])
        rc, result, _ = self._execute(did, preview["plan"])
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "dlq")
        self.assertEqual(self._ledger(did)["status"], "dlq")


class SettlementExecuteRefusalTest(SettlementCliBase):
    def _plan_for(self, key: str) -> tuple[str, str]:
        did = self._make_pending(key, in_flight=True)
        _, preview, _ = self._dry_run(did)
        return did, preview["plan"]

    def test_missing_release_flag_refuses_without_mutation(self) -> None:
        did, plan = self._plan_for("norelease")
        rc, payload, _ = self._execute(did, plan, release=False)
        self.assertEqual(rc, 2)
        self.assertIn("--release-with-termination-unconfirmed", payload["error"])
        self.assertEqual(self._ledger(did)["status"], "in_flight")
        self.assertNotIn(SETTLEMENT_KEY, self._observed(did))

    def test_execution_rejects_caller_supplied_reason(self) -> None:
        did, plan = self._plan_for("callerreason")
        rc, payload, _ = self._execute(did, plan, reason="smuggled")
        self.assertEqual(rc, 2)
        self.assertIn("does not accept a caller-supplied --reason", payload["error"])
        self.assertEqual(self._ledger(did)["status"], "in_flight")

    def test_tampered_plan_refuses_without_mutation(self) -> None:
        did, plan = self._plan_for("tamper")
        version, payload_seg, sig = plan.split(".")
        flipped = payload_seg[:-1] + ("A" if payload_seg[-1] != "A" else "B")
        rc, payload, _ = self._execute(did, f"{version}.{flipped}.{sig}")
        self.assertEqual(rc, 2)
        self.assertEqual(self._ledger(did)["status"], "in_flight")
        self.assertNotIn(SETTLEMENT_KEY, self._observed(did))

    def test_forged_plan_under_attacker_key_refuses(self) -> None:
        did = self._make_pending("forge", in_flight=True)
        _, preview, _ = self._dry_run(did)
        forged = sp.build_plan(
            secret="attacker-key",
            actor_id=HUMAN_ID,
            dispatch_id=did,
            reason="malicious",
            snapshot=preview["snapshot"],
            issued_at=preview["issued_at"],
            nonce="deadbeefdeadbeefdeadbeefdeadbeef",
        )
        rc, payload, _ = self._execute(did, forged)
        self.assertEqual(rc, 2)
        self.assertEqual(self._ledger(did)["status"], "in_flight")
        self.assertNotIn(SETTLEMENT_KEY, self._observed(did))

    def test_actor_and_dispatch_mismatch_refuse(self) -> None:
        did, plan = self._plan_for("mismatch")
        other = self._make_pending("mismatch2", in_flight=True)
        # Same plan, different --from-actor-id -> actor mismatch.
        rc, _payload, _ = self._execute(did, plan, actor=OTHER_HUMAN_ID)
        self.assertEqual(rc, 2)
        self.assertEqual(self._ledger(did)["status"], "in_flight")
        # Same plan, different --dispatch-id -> dispatch mismatch.
        rc, _payload, _ = self._execute(other, plan)
        self.assertEqual(rc, 2)
        self.assertEqual(self._ledger(other)["status"], "in_flight")

    def test_snapshot_drift_refuses_without_mutation(self) -> None:
        did = self._make_pending("drift", in_flight=True, run_token="rt-orig")
        _, preview, _ = self._dry_run(did)
        # A new run stamps a different token AFTER the plan was issued.
        observed = self._observed(did)
        observed["run_token"] = "rt-new"
        with self.store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                (json.dumps(observed, sort_keys=True), did),
            )
        rc, payload, _ = self._execute(did, preview["plan"])
        self.assertEqual(rc, 2)
        self.assertIn("drift", payload["error"])
        self.assertEqual(self._ledger(did)["status"], "in_flight")
        self.assertNotIn(SETTLEMENT_KEY, self._observed(did))

    def test_bad_credential_refuses_before_any_settlement(self) -> None:
        did, plan = self._plan_for("badcred")
        os.environ["AGENT_COMMS_ADMIN_TOKEN"] = "wrong-secret"
        rc, payload, _ = self._execute(did, plan)
        self.assertEqual(rc, 2)
        self.assertIn("operator credential", payload["error"])
        self.assertEqual(self._ledger(did)["status"], "in_flight")
        self.assertNotIn(SETTLEMENT_KEY, self._observed(did))


class SettlementReplayAndExpiryTest(SettlementCliBase):
    """Deterministic clock control via the store-level production entry points."""

    ISSUED = "2026-07-18T12:00:00+00:00"

    def _preview(self, did: str, *, issued: str, nonce: str = "n0n0n0n0n0n0n0n0n0n0n0n0n0n0n0n0"):
        return self.store.settle_dispatch_preview(
            did,
            actor_id=HUMAN_ID,
            reason="release stuck cancellation",
            secret=VALID_TOKEN,
            issued_at=issued,
            nonce=nonce,
        )

    def test_expiry_before_first_success_refuses_without_mutation(self) -> None:
        did = self._make_pending("expired", in_flight=True)
        preview = self._preview(did, issued=self.ISSUED)
        with self.assertRaises(sp.PlanExpiredError):
            self.store.settle_dispatch_execute(
                did,
                actor_id=HUMAN_ID,
                plan=preview["plan"],
                secret=VALID_TOKEN,
                release_ack=True,
                now="2026-07-18T12:05:01+00:00",
            )
        self.assertEqual(self._ledger(did)["status"], "in_flight")
        self.assertNotIn(SETTLEMENT_KEY, self._observed(did))

    def test_exact_replay_returns_winner_including_after_expiry(self) -> None:
        did = self._make_pending("replay", in_flight=True)
        preview = self._preview(did, issued=self.ISSUED)
        plan = preview["plan"]
        first = self.store.settle_dispatch_execute(
            did, actor_id=HUMAN_ID, plan=plan, secret=VALID_TOKEN, release_ack=True,
            now="2026-07-18T12:01:00+00:00",
        )
        self.assertEqual(first["status"], "dlq")
        winner_fp = first["plan_fingerprint"]
        dlq_at = self._ledger(did)["dlq_at"]

        # Replay LONG after expiry still returns the stored winner, no mutation.
        replay = self.store.settle_dispatch_execute(
            did, actor_id=HUMAN_ID, plan=plan, secret=VALID_TOKEN, release_ack=True,
            now="2026-07-18T13:00:00+00:00",
        )
        self.assertEqual(replay["status"], "dlq")
        self.assertEqual(replay["plan_fingerprint"], winner_fp)
        # No re-settlement: dlq_at unchanged, and still exactly one notice.
        self.assertEqual(self._ledger(did)["dlq_at"], dlq_at)
        self.assertEqual(len(self._producer_notices()), 1)

    def test_different_plan_after_settlement_is_a_loud_no_op(self) -> None:
        did = self._make_pending("loudloser", in_flight=True)
        first_preview = self._preview(did, issued=self.ISSUED, nonce="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.store.settle_dispatch_execute(
            did, actor_id=HUMAN_ID, plan=first_preview["plan"], secret=VALID_TOKEN,
            release_ack=True, now="2026-07-18T12:01:00+00:00",
        )
        # A DIFFERENT (freshly issued, unexpired) plan replayed against the settled
        # dlq row refuses loudly and never relabels the winner.
        second_preview_plan = sp.build_plan(
            secret=VALID_TOKEN,
            actor_id=HUMAN_ID,
            dispatch_id=did,
            reason="second attempt",
            snapshot=first_preview["snapshot"],
            issued_at="2026-07-18T12:30:00+00:00",
            nonce="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        with self.assertRaises(Exception):
            self.store.settle_dispatch_execute(
                did, actor_id=HUMAN_ID, plan=second_preview_plan, secret=VALID_TOKEN,
                release_ack=True, now="2026-07-18T12:31:00+00:00",
            )
        row = self._ledger(did)
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(row["failure_reason"], SETTLEMENT_FAILURE_REASON)


class SettlementSnapshotDriftMatrixTest(SettlementCliBase):
    """Execution re-derives and exactly compares EVERY signed snapshot field."""

    ISSUED = "2026-07-18T12:00:00+00:00"
    NOW = "2026-07-18T12:01:00+00:00"

    @staticmethod
    def _drift_value(value):
        if isinstance(value, dict):
            drifted = dict(value)
            drifted["__drift__"] = True
            return drifted
        if value is None:
            return "sentinel-drift"
        if isinstance(value, str):
            return value + "-DRIFT"
        return "sentinel-drift"

    def test_execution_refuses_drift_in_every_signed_field(self) -> None:
        did = self._make_pending("driftmatrix", in_flight=True)
        base = self._store_preview(did, issued=self.ISSUED, nonce="d" * 32)["snapshot"]
        # Every signed field is bound and independently compared: a plan minted
        # (with the REAL credential) whose snapshot drifts in exactly ONE field is
        # refused without mutation, proving each field participates in the compare.
        self.assertEqual(
            set(base),
            {
                "producer_actor_id",
                "recipient_actor_id",
                "dispatch_status",
                "transport_status",
                "spawn_handle",
                "run_token_fingerprint",
                "cancellation_request",
            },
        )
        for index, field in enumerate(sorted(base)):
            with self.subTest(field=field):
                drifted = dict(base)
                drifted[field] = self._drift_value(base[field])
                plan = sp.build_plan(
                    secret=VALID_TOKEN,
                    actor_id=HUMAN_ID,
                    dispatch_id=did,
                    reason="drift attempt",
                    snapshot=drifted,
                    issued_at=self.ISSUED,
                    nonce=f"{index:032d}",
                )
                with self.assertRaises(CancellationConflictError):
                    self.store.settle_dispatch_execute(
                        did,
                        actor_id=HUMAN_ID,
                        plan=plan,
                        secret=VALID_TOKEN,
                        release_ack=True,
                        now=self.NOW,
                    )
                self.assertEqual(self._ledger(did)["status"], "in_flight")
                self.assertNotIn(SETTLEMENT_KEY, self._observed(did))
        # An exact (undrifted) plan still settles afterwards: refusals never
        # mutated the row.
        exact = self._store_preview(did, issued=self.ISSUED, nonce="e" * 32)
        result = self.store.settle_dispatch_execute(
            did, actor_id=HUMAN_ID, plan=exact["plan"], secret=VALID_TOKEN,
            release_ack=True, now=self.NOW,
        )
        self.assertEqual(result["status"], "dlq")


class SettlementReplayAuthorizationTest(SettlementCliBase):
    """A stored winner is returned ONLY after re-validating sig/binding/human."""

    ISSUED = "2026-07-18T12:00:00+00:00"

    def _settle(self, key: str):
        did = self._make_pending(key, in_flight=True)
        preview = self._store_preview(did, issued=self.ISSUED, nonce="a" * 32)
        first = self.store.settle_dispatch_execute(
            did, actor_id=HUMAN_ID, plan=preview["plan"], secret=VALID_TOKEN,
            release_ack=True, now="2026-07-18T12:01:00+00:00",
        )
        self.assertEqual(first["status"], "dlq")
        return did, preview["plan"], first

    def test_actor_mismatched_replay_never_obtains_the_winner(self) -> None:
        did, plan, first = self._settle("actormismatch")
        # Same committed plan, different explicit actor -> actor-binding mismatch
        # raises BEFORE the replay winner can be returned.
        with self.assertRaises(sp.PlanMismatchError):
            self.store.settle_dispatch_execute(
                did, actor_id=OTHER_HUMAN_ID, plan=plan, secret=VALID_TOKEN,
                release_ack=True, now="2026-07-18T12:02:00+00:00",
            )
        self.assertEqual(len(self._producer_notices()), 1)

    def test_bad_credential_replay_never_obtains_the_winner(self) -> None:
        did, plan, first = self._settle("badcredreplay")
        # Presenting the exact committed plan under the WRONG secret fails the
        # signature check before the replay winner is returned.
        with self.assertRaises(sp.PlanSignatureError):
            self.store.settle_dispatch_execute(
                did, actor_id=HUMAN_ID, plan=plan, secret="attacker-key",
                release_ack=True, now="2026-07-18T12:02:00+00:00",
            )
        self.assertEqual(len(self._producer_notices()), 1)

    def test_non_human_actor_replay_never_obtains_the_winner(self) -> None:
        did, plan, first = self._settle("nonhumanreplay")
        # Demote the operator's registered kind after settlement; even the exact
        # committed plan is refused because the actor is no longer a human.
        with self.store._db.connection() as conn:
            conn.execute("update actors set kind = 'system' where id = ?", (HUMAN_ID,))
        with self.assertRaises(Exception):
            self.store.settle_dispatch_execute(
                did, actor_id=HUMAN_ID, plan=plan, secret=VALID_TOKEN,
                release_ack=True, now="2026-07-18T12:02:00+00:00",
            )
        self.assertEqual(len(self._producer_notices()), 1)

    def test_exact_replay_after_expiry_revalidates_then_returns_winner(self) -> None:
        did, plan, first = self._settle("replayafterexpiry")
        # Long after the five-minute window, the EXACT committed plan still returns
        # the stored winner (after passing sig/binding/human revalidation), with no
        # second mutation and still exactly one producer notice.
        dlq_at = self._ledger(did)["dlq_at"]
        replay = self.store.settle_dispatch_execute(
            did, actor_id=HUMAN_ID, plan=plan, secret=VALID_TOKEN,
            release_ack=True, now="2026-07-18T13:30:00+00:00",
        )
        self.assertEqual(replay["status"], "dlq")
        self.assertEqual(replay["plan_fingerprint"], first["plan_fingerprint"])
        self.assertEqual(replay["producer_notice_message_id"], first["producer_notice_message_id"])
        self.assertEqual(self._ledger(did)["dlq_at"], dlq_at)
        self.assertEqual(len(self._producer_notices()), 1)


class SettlementAtomicNoticeTest(SettlementCliBase):
    def test_notice_insertion_failure_rolls_back_settlement(self) -> None:
        did = self._make_pending("noticefail", in_flight=True)
        preview = self._store_preview(did)
        # If the in-transaction notice insert fails, the ENTIRE settlement rolls
        # back: no durable claim, no terminal write, and no orphan notice.
        with mock.patch.object(
            self.store._dispatch,
            "_insert_settlement_notice",
            side_effect=RuntimeError("notice insert boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.settle_dispatch_execute(
                    did, actor_id=HUMAN_ID, plan=preview["plan"], secret=VALID_TOKEN,
                    release_ack=True,
                )
        row = self._ledger(did)
        self.assertEqual(row["status"], "in_flight")
        self.assertIsNone(row["dlq_at"])
        self.assertNotIn(SETTLEMENT_KEY, self._observed(did))
        self.assertEqual(self._transport(did), "sent")
        self.assertEqual(len(self._producer_notices()), 0)
        # A subsequent clean execution still settles and produces exactly one notice.
        result = self.store.settle_dispatch_execute(
            did, actor_id=HUMAN_ID, plan=preview["plan"], secret=VALID_TOKEN,
            release_ack=True,
        )
        self.assertEqual(result["status"], "dlq")
        self.assertEqual(len(self._producer_notices()), 1)


class SettlementBoundedEvidenceTest(SettlementCliBase):
    """T8: the operator-settlement partial evidence shares the bounded shape."""

    def test_settlement_partial_evidence_shares_bounded_shape_distinct_classification(self) -> None:
        did = self._make_pending("boundedsettle", in_flight=True)
        message_id = self._ledger(did)["message_id"]
        for i in range(12):
            self.store.send_message(
                WORKER_ID,
                [ARCH_ID],
                f"Re {i}",
                f"secret-settle-{i}",
                [],
                parent_message_id=message_id,
            )
        _, preview, _ = self._dry_run(did)
        rc, _result, _ = self._execute(did, preview["plan"])
        self.assertEqual(rc, 0)

        evidence = self._observed(did)[SETTLEMENT_KEY]["partial_evidence"]
        # Distinct classification: an operator settlement never masquerades as a
        # clean worker exit or a confirmed cancellation.
        self.assertEqual(evidence["classification"], "operator_settlement_partial_work")
        # Shared bounded shape: at most ten stored ids plus the EXACT total, and
        # never a persisted reply body.
        self.assertEqual(len(evidence["reply_message_ids"]), 10)
        self.assertEqual(evidence["reply_total_count"], 12)
        blob = json.dumps(evidence)
        for i in range(12):
            self.assertNotIn(f"secret-settle-{i}", blob)


class SettlementPreflightBeforeStoreTest(unittest.TestCase):
    """Credential and CLI-mode refusals occur BEFORE any Store/Database effect."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        # A db path whose PARENT directory does not exist. A refusal before Store
        # construction must not create the parent, the DB, or any sidecar.
        self.absent_parent = self.tmp / "missing" / "nested"
        self.db_path = self.absent_parent / "agent-comms.sqlite"

        self.token_path = self.tmp / "admin-token"
        self.token_path.write_text(VALID_TOKEN)
        os.chmod(self.token_path, 0o600)
        patcher = mock.patch.object(_helpers, "ADMIN_TOKEN_PATH", self.token_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._saved = os.environ.get("AGENT_COMMS_ADMIN_TOKEN")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            os.environ.pop("AGENT_COMMS_ADMIN_TOKEN", None)
        else:
            os.environ["AGENT_COMMS_ADMIN_TOKEN"] = self._saved

    def _run(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cli.run(["--db", str(self.db_path), *argv])
        text = buffer.getvalue().strip()
        return rc, (json.loads(text) if text else {})

    def _assert_nothing_created(self) -> None:
        self.assertFalse(self.absent_parent.exists())
        self.assertFalse(self.db_path.exists())
        self.assertFalse(self.db_path.with_name("agent-comms.sqlite-wal").exists())
        self.assertFalse(self.db_path.with_name("agent-comms.sqlite-shm").exists())

    def test_bad_credential_dry_run_refuses_without_filesystem_effect(self) -> None:
        os.environ["AGENT_COMMS_ADMIN_TOKEN"] = "wrong-secret"
        rc, out = self._run(
            [
                "admin", "settle-dispatch",
                "--from-actor-id", HUMAN_ID,
                "--dispatch-id", "dispatch_x",
                "--reason", "release",
                "--dry-run",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertIn("operator credential", out["error"])
        self._assert_nothing_created()

    def test_invalid_mode_refuses_without_filesystem_effect(self) -> None:
        os.environ["AGENT_COMMS_ADMIN_TOKEN"] = VALID_TOKEN
        # Valid credential but a dry-run with no --reason: an invalid mode that
        # refuses in preflight, before Store/Database construction.
        rc, out = self._run(
            [
                "admin", "settle-dispatch",
                "--from-actor-id", HUMAN_ID,
                "--dispatch-id", "dispatch_x",
                "--dry-run",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertIn("--reason", out["error"])
        self._assert_nothing_created()

    def test_execute_malformed_plan_absent_parent_creates_nothing(self) -> None:
        os.environ["AGENT_COMMS_ADMIN_TOKEN"] = VALID_TOKEN
        # A credentialed, well-formed CLI EXECUTE request (with the release ack)
        # whose plan is malformed passes preflight and constructs the Store, but the
        # plan is validated BEFORE any Database init/connection: the malformed plan
        # refuses (rc 2) and the absent parent/DB/sidecar are never created.
        rc, out = self._run(
            [
                "admin", "settle-dispatch",
                "--from-actor-id", HUMAN_ID,
                "--dispatch-id", "dispatch_x",
                "--execute-plan", "not-a-valid-plan",
                "--release-with-termination-unconfirmed",
            ]
        )
        self.assertEqual(rc, 2)
        self._assert_nothing_created()

    def test_execute_valid_plan_absent_ledger_refuses_without_creation(self) -> None:
        os.environ["AGENT_COMMS_ADMIN_TOKEN"] = VALID_TOKEN
        # The parent directory exists but the ledger file does not. A VALIDLY
        # signed, unexpired plan passes plan validation, but settlement operates
        # only on an EXISTING ledger: the existing-file mode=rw connection refuses
        # the absent database WITHOUT creating the file, schema, or any sidecar.
        self.absent_parent.mkdir(parents=True)
        plan = sp.build_plan(
            secret=VALID_TOKEN,
            actor_id=HUMAN_ID,
            dispatch_id="dispatch_x",
            reason="release stuck cancellation",
            snapshot={"dispatch_status": "in_flight"},
            issued_at="2026-07-18T12:00:00+00:00",
            nonce="n" * 32,
        )
        rc, out = self._run(
            [
                "admin", "settle-dispatch",
                "--from-actor-id", HUMAN_ID,
                "--dispatch-id", "dispatch_x",
                "--execute-plan", plan,
                "--release-with-termination-unconfirmed",
            ]
        )
        self.assertEqual(rc, 2)
        # The refusal creates nothing: the parent stays empty and no DB/WAL/SHM
        # file materializes.
        self.assertTrue(self.absent_parent.exists())
        self.assertEqual(list(self.absent_parent.iterdir()), [])
        self.assertFalse(self.db_path.exists())
        self.assertFalse(self.db_path.with_name("agent-comms.sqlite-wal").exists())
        self.assertFalse(self.db_path.with_name("agent-comms.sqlite-shm").exists())


if __name__ == "__main__":
    unittest.main()
