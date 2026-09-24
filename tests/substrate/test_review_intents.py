"""Review dispatch-intent binding (dispatch contract 17, Landing 1).

Proves the durable ``review_dispatch_intents`` substrate end to end: the
additive schema and unconditional producer/key identity, deterministic
canonical payload digests, the prepared/active/bound/abandoned state machine
with a non-expiring prepared state, active-only expiry, and single-row retry,
the mark-dispatched crash-ordered SQL -> tri-state durable probe -> JSON ->
re-read -> CAS sequence with its exact, absent, unknown, and mismatch
outcomes, clean-baseline and identity refusals with exact paths,
transactional dispatch-time consumption with zero effects on refusal,
ordinary-dispatch and legacy compatibility, the read-only status view, and
the contract-17 code-identity classification.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import ast
import contextlib
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_comms import code_identity, review
from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.db import Database, LEDGER_SCHEMA_VERSION
from agent_comms.policies import (
    WORKER_DISPATCH_POLICY,
    WORKER_DISPATCH_POLICY_VERSION,
)
from agent_comms.schema import ValidationError
from agent_comms.store import Store
from agent_comms.reviewing import intents, rounds
from agent_comms.reviewing import store as reviewing_store
from agent_comms.reviewing.contracts import ReviewError, validate_record

ARCHITECT = "gamma-architect"
WORKER = "gamma-codex-worker"


class FailingSpawnAdapter:
    def dispatch(self, context: DispatchContext) -> DispatchStart:
        raise RuntimeError("adapter spawn failed")

    def halt(self, spawn_handle: str, observed_values=None) -> None:
        return None


def _payload(**overrides) -> dict:
    fields = {
        "producer_actor_id": ARCHITECT,
        "idempotency_key": "k1",
        "recipient_actor_id": WORKER,
        "real_project_root": "/repo",
        "policy_name": WORKER_DISPATCH_POLICY,
        "policy_version": WORKER_DISPATCH_POLICY_VERSION,
        "round_kind": intents.ROUND_KIND_IMPLEMENTATION,
        "record_id": "R1",
        "brief_sha256": "a" * 64,
        "dod_sha256": "b" * 64,
        "source_branch": "work-branch",
        "source_head": "c" * 40,
        "source_tree": "d" * 40,
        "integration_head": "c" * 40,
        "integration_tree": "d" * 40,
        "base_commit": "c" * 40,
        "base_tree": "d" * 40,
    }
    fields.update(overrides)
    return intents.canonical_payload(**fields)


def _shift(ts: str, seconds: int) -> str:
    return (datetime.fromisoformat(ts) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


class CanonicalIdentityTest(unittest.TestCase):
    def test_digest_is_deterministic_and_field_ordering_free(self) -> None:
        one = _payload()
        two = json.loads(json.dumps(dict(reversed(list(one.items())))))
        self.assertEqual(intents.payload_digest(one), intents.payload_digest(two))
        self.assertEqual(
            intents.intent_id_for(intents.payload_digest(one)),
            f"rvi_{intents.payload_digest(one)}",
        )

    def test_every_field_drift_changes_the_digest(self) -> None:
        base = intents.payload_digest(_payload())
        for name in intents.PAYLOAD_FIELDS:
            if name == "payload_version":
                continue
            with self.subTest(field=name):
                drifted = _payload(**{name: "drifted-value"})
                self.assertNotEqual(intents.payload_digest(drifted), base)

    def test_payload_validation_refuses_missing_unknown_and_empty(self) -> None:
        with self.assertRaisesRegex(ValidationError, "review_intent_payload_invalid"):
            intents.canonical_payload(producer_actor_id=ARCHITECT)
        with self.assertRaisesRegex(ValidationError, "review_intent_payload_invalid"):
            _payload(extra_field="x")
        with self.assertRaisesRegex(ValidationError, "review_intent_payload_invalid"):
            _payload(policy_name="")
        bad_version = dict(_payload(), payload_version=2)
        with self.assertRaisesRegex(ValidationError, "review_intent_payload_invalid"):
            intents.canonical_payload_bytes(bad_version)


class SchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "ledger.sqlite"
        self.store = Store(self.db_path)
        self.store.init()
        self.store.register_agent_actor(
            ARCHITECT, "agentcomms", "architect", str(self.root / "arch"), []
        )
        self.store.register_agent_actor(
            WORKER, "agentcomms", "worker", str(self.root / "wrk"), [], owner=ARCHITECT
        )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        return conn

    def test_schema_is_additive_with_unchanged_user_version(self) -> None:
        with contextlib.closing(self._conn()) as conn:
            columns = {
                row[1]
                for row in conn.execute(
                    "pragma table_info(review_dispatch_intents)"
                ).fetchall()
            }
            self.assertLessEqual(
                {
                    "producer_actor_id",
                    "idempotency_key",
                    "intent_id",
                    "state",
                    "preledger_state",
                    "dispatch_id",
                    "recipient_actor_id",
                    "real_project_root",
                    "policy_name",
                    "policy_version",
                    "round_kind",
                    "digest",
                    "payload_json",
                    "attempt_count",
                },
                columns,
            )
            version = int(conn.execute("pragma user_version").fetchone()[0])
        self.assertEqual(version, LEDGER_SCHEMA_VERSION)
        self.assertEqual(version, 3)
        Database(self.db_path).init()  # idempotent re-init keeps the floor
        with contextlib.closing(self._conn()) as conn:
            self.assertEqual(int(conn.execute("pragma user_version").fetchone()[0]), 3)

    def test_producer_key_uniqueness_is_unconditional(self) -> None:
        with contextlib.closing(self._conn()) as conn, conn:
            intents.prepare(conn, _payload(), NOW)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    insert into review_dispatch_intents(
                      producer_actor_id, idempotency_key, intent_id, state,
                      preledger_state, recipient_actor_id, real_project_root,
                      policy_name, policy_version, round_kind, digest,
                      payload_json, created_at, updated_at, prepared_at
                    )
                    values(?, 'k1', 'rvi_other', 'prepared', 'prepared', ?, '/r',
                           'p', 'v', 'implementation', 'd', '{}', ?, ?, ?)
                    """,
                    (ARCHITECT, WORKER, NOW, NOW, NOW),
                )

    def test_state_check_constraints_pin_preledger_and_dispatch_binding(self) -> None:
        bad_rows = (
            ("prepared", None, "dx"),  # prepared must be pre-ledger
            ("active", "prepared", None),  # active preledger marker must match
            ("bound", None, None),  # bound requires its ledger row
            ("abandoned", "prepared", None),  # abandoned holds no marker
        )
        with contextlib.closing(self._conn()) as conn:
            for index, (state, preledger, dispatch_id) in enumerate(bad_rows):
                with (
                    self.subTest(state=state),
                    self.assertRaises(sqlite3.IntegrityError),
                ):
                    conn.execute(
                        """
                        insert into review_dispatch_intents(
                          producer_actor_id, idempotency_key, intent_id, state,
                          preledger_state, dispatch_id, recipient_actor_id,
                          real_project_root, policy_name, policy_version,
                          round_kind, digest, payload_json, created_at,
                          updated_at, prepared_at
                        )
                        values(?, ?, ?, ?, ?, ?, ?, '/r', 'p', 'v',
                               'implementation', 'd', '{}', ?, ?, ?)
                        """,
                        (
                            ARCHITECT,
                            f"bad-{index}",
                            f"rvi_bad_{index}",
                            state,
                            preledger,
                            dispatch_id,
                            WORKER,
                            NOW,
                            NOW,
                            NOW,
                        ),
                    )

    def test_ensure_schema_does_not_resync_agent_actors(self) -> None:
        with contextlib.closing(self._conn()) as conn, conn:
            conn.execute(
                "update actors set project_root='/moved' where id=?", (WORKER,)
            )
        db = Database(self.db_path)
        conn = db.open_existing_read_write()
        try:
            db.ensure_review_intent_schema(conn)
        finally:
            conn.close()
        with contextlib.closing(self._conn()) as conn:
            root = conn.execute(
                "select project_root from actors where id=?", (WORKER,)
            ).fetchone()[0]
        self.assertEqual(root, "/moved")


class StateMachineTest(SchemaTest):
    def _prepare(self, payload=None, now=NOW) -> dict:
        with contextlib.closing(self._conn()) as conn, conn:
            return intents.prepare(conn, payload or _payload(), now)

    def _count(self) -> int:
        with contextlib.closing(self._conn()) as conn:
            return conn.execute(
                "select count(*) from review_dispatch_intents"
            ).fetchone()[0]

    def test_prepare_exact_replay_and_permanent_payload_conflict(self) -> None:
        row = self._prepare()
        self.assertEqual((row["state"], row["attempt_count"]), ("prepared", 1))
        replay = self._prepare()
        self.assertEqual(replay["intent_id"], row["intent_id"])
        self.assertEqual(replay["attempt_count"], 1)
        self.assertEqual(self._count(), 1)
        with self.assertRaisesRegex(ValidationError, "review_intent_conflict"):
            self._prepare(_payload(dod_sha256="f" * 64))
        self.assertEqual(self._count(), 1)

    def test_abandoned_retry_reuses_the_single_row_and_counts_attempts(self) -> None:
        row = self._prepare()
        with contextlib.closing(self._conn()) as conn, conn:
            activated_at = _shift(NOW, 10)
            intents.activate(conn, ARCHITECT, "k1", row["digest"], activated_at)
            abandoned = intents.reconcile(
                conn, _shift(activated_at, intents.ACTIVE_TTL_SECONDS + 1)
            )
        self.assertEqual(abandoned[0]["expired_state"], "active")
        with self.assertRaisesRegex(ValidationError, "review_intent_conflict"):
            self._prepare(_payload(dod_sha256="f" * 64))  # conflict is permanent
        retried = self._prepare()
        self.assertEqual((retried["state"], retried["attempt_count"]), ("prepared", 2))
        self.assertEqual(self._count(), 1)

    def test_prepared_never_expires_and_active_expires_at_fifteen_minutes(self) -> None:
        row = self._prepare()
        with contextlib.closing(self._conn()) as conn, conn:
            # Prepared rows are non-expiring and non-dispatchable: no deadline,
            # and reconciliation leaves them prepared at any age.
            self.assertIsNone(intents.expiry_deadline(row))
            self.assertEqual(
                intents.reconcile(conn, _shift(NOW, intents.ACTIVE_TTL_SECONDS * 100)),
                [],
            )
            activated_at = _shift(NOW, 10)
            active = intents.activate(
                conn, ARCHITECT, "k1", row["digest"], activated_at
            )
            self.assertEqual(active["state"], "active")
            # The active clock starts at activation, not preparation.
            self.assertEqual(
                intents.reconcile(
                    conn, _shift(activated_at, intents.ACTIVE_TTL_SECONDS - 1)
                ),
                [],
            )
            expired = intents.reconcile(
                conn, _shift(activated_at, intents.ACTIVE_TTL_SECONDS + 1)
            )
        self.assertEqual(expired[0]["expired_state"], "active")
        self.assertEqual(expired[0]["abandon_reason"], "active_ttl_expired")

    def test_activate_is_an_exact_cas_and_idempotent_recovery(self) -> None:
        row = self._prepare()
        with contextlib.closing(self._conn()) as conn, conn:
            with self.assertRaisesRegex(
                ValidationError, "review_intent_activate_failed"
            ):
                intents.activate(conn, ARCHITECT, "k1", "0" * 64, NOW)
            intents.activate(conn, ARCHITECT, "k1", row["digest"], NOW)
            again = intents.activate(conn, ARCHITECT, "k1", row["digest"], NOW)
        self.assertEqual(again["state"], "active")

    def test_match_for_binding_refuses_non_active_and_every_mismatch(self) -> None:
        row = self._prepare()
        with contextlib.closing(self._conn()) as conn, conn:
            self.assertIsNone(
                intents.match_for_binding(
                    conn,
                    ARCHITECT,
                    "absent-key",
                    recipient_actor_id=WORKER,
                    real_project_root="/repo",
                    policy_name=WORKER_DISPATCH_POLICY,
                    policy_version=WORKER_DISPATCH_POLICY_VERSION,
                )
            )
            with self.assertRaisesRegex(ValidationError, "review_intent_not_active"):
                intents.match_for_binding(
                    conn,
                    ARCHITECT,
                    "k1",
                    recipient_actor_id=WORKER,
                    real_project_root="/repo",
                    policy_name=WORKER_DISPATCH_POLICY,
                    policy_version=WORKER_DISPATCH_POLICY_VERSION,
                )
            intents.activate(conn, ARCHITECT, "k1", row["digest"], NOW)
            for field, value in (
                ("recipient_actor_id", "other-worker"),
                ("real_project_root", "/elsewhere"),
                ("policy_name", "other_policy"),
                ("policy_version", "v9"),
            ):
                kwargs = {
                    "recipient_actor_id": WORKER,
                    "real_project_root": "/repo",
                    "policy_name": WORKER_DISPATCH_POLICY,
                    "policy_version": WORKER_DISPATCH_POLICY_VERSION,
                    field: value,
                }
                with (
                    self.subTest(field=field),
                    self.assertRaisesRegex(ValidationError, "review_intent_mismatch"),
                ):
                    intents.match_for_binding(conn, ARCHITECT, "k1", **kwargs)
            conn.execute(
                "update review_dispatch_intents set payload_json=? "
                "where idempotency_key='k1'",
                (json.dumps(dict(_payload(), dod_sha256="f" * 64), sort_keys=True),),
            )
            with self.assertRaisesRegex(ValidationError, "review_intent_mismatch"):
                intents.match_for_binding(
                    conn,
                    ARCHITECT,
                    "k1",
                    recipient_actor_id=WORKER,
                    real_project_root="/repo",
                    policy_name=WORKER_DISPATCH_POLICY,
                    policy_version=WORKER_DISPATCH_POLICY_VERSION,
                )


class ReviewIntentEnv(unittest.TestCase):
    """Integration checkout plus review worktree staged at the SAME clean head."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.review_root = self.root / "reviews"
        self.ledger = self.root / "ledger.sqlite"
        self.store = Store(self.ledger)
        self.store.init()
        self.store.register_agent_actor(
            ARCHITECT, "agentcomms", "architect", str(self.root / "architect"), []
        )
        self.store.register_agent_actor(
            WORKER,
            "agentcomms",
            "worker",
            str(self.root / "worker"),
            [],
            owner=ARCHITECT,
        )
        self.integration = self.root / "integration"
        self.integration.mkdir()
        self._git("init", self.integration)
        self._git("config", "user.email", "t@t.invalid", self.integration)
        self._git("config", "user.name", "T", self.integration)
        self._git("checkout", "-B", "integration-main", self.integration)
        (self.integration / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.integration / ".gitignore").write_text("ignored-file\n", encoding="utf-8")
        self._git("add", "-A", self.integration)
        self._git("commit", "-m", "base", self.integration)
        self.repo = self.root / "repo"
        self._git(
            "worktree", "add", "-b", "work-branch", str(self.repo), self.integration
        )
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.execute(
                "update actors set project_root=? where id=?", (str(self.repo), WORKER)
            )
        self.brief = self.root / "brief.md"
        self.brief.write_text(
            "# Brief\n\n## Surface\nx\n\n## Anti-claims\nx\n\n"
            "## Definition of Done\nx\n\n## Process\nx\n\n"
            "## Production surface\n- touches: none; reason: test\n",
            encoding="utf-8",
        )
        self.dod = self.root / "dod.json"
        self.dod.write_text(
            json.dumps([{"id": "unit", "claim": "c", "check_id": "green"}]),
            encoding="utf-8",
        )
        for patcher in (
            mock.patch.object(
                review.runtime_paths, "db_path", return_value=self.ledger
            ),
            mock.patch.object(reviewing_store, "REVIEW_ROOT", self.review_root),
            mock.patch.object(review, "REVIEW_ROOT", self.review_root),
            mock.patch.dict(
                "os.environ", {"AGENT_COMMS_MAIN": str(self.integration)}, clear=False
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _git(self, *args) -> str:
        *rest, cwd = args
        return subprocess.run(
            ["git", *rest],
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = review.main(list(argv))
            except SystemExit as exc:
                rc = int(exc.code) if isinstance(exc.code, int) else 1
        return rc, out.getvalue(), err.getvalue()

    def open_and_review(self, record_id: str, *, inline: bool = False) -> None:
        dod_args = (
            ["--dod-section", "- inline criterion"]
            if inline
            else ["--dod", str(self.dod)]
        )
        rc, _out, err = self.cli(
            "open",
            "--dispatch-id",
            record_id,
            "--brief",
            str(self.brief),
            *dod_args,
            "--repo",
            str(self.repo),
            "--expected-producer",
            ARCHITECT,
            "--expected-recipient",
            WORKER,
        )
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.cli(
            "brief-check",
            "--dispatch-id",
            record_id,
            "--clean",
            "--by",
            "codex",
            "--surface-verdict",
            "complete",
            "--surface-reason",
            "r",
        )
        self.assertEqual(rc, 0, err)

    def mark_dispatched(self, record_id: str, key: str) -> tuple[int, str]:
        rc, _out, err = self.cli(
            "mark-dispatched", "--dispatch-id", record_id, "--idempotency-key", key
        )
        return rc, err

    def record(self, record_id: str) -> dict:
        return json.loads(
            (self.review_root / f"{record_id}.json").read_text(encoding="utf-8")
        )

    def intent_row(self, key: str):
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "select * from review_dispatch_intents "
                "where producer_actor_id=? and idempotency_key=?",
                (ARCHITECT, key),
            ).fetchone()

    def shift_intent(self, key: str, column: str, seconds: int) -> None:
        row = self.intent_row(key)
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.execute(
                f"update review_dispatch_intents set {column}=? "
                "where producer_actor_id=? and idempotency_key=?",
                (_shift(row[column], seconds), ARCHITECT, key),
            )


class MarkDispatchedFlowTest(ReviewIntentEnv):
    def test_mark_dispatched_binds_and_activates_the_exact_intent(self) -> None:
        self.open_and_review("R1")
        rc, err = self.mark_dispatched("R1", "k1")
        self.assertEqual(rc, 0, err)
        record = self.record("R1")
        validate_record(record)
        entry = record["intended_dispatches"][-1]
        row = self.intent_row("k1")
        self.assertEqual(record["state"], "dispatched")
        self.assertEqual(entry["round_kind"], "implementation")
        self.assertEqual(entry["intent_id"], row["intent_id"])
        self.assertEqual(entry["intent_digest"], row["digest"])
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["preledger_state"], "active")
        self.assertIsNone(row["dispatch_id"])
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["policy_name"], WORKER_DISPATCH_POLICY)
        self.assertEqual(payload["policy_version"], WORKER_DISPATCH_POLICY_VERSION)
        self.assertEqual(payload["recipient_actor_id"], WORKER)
        self.assertEqual(payload["base_commit"], record["base_commit"])
        self.assertEqual(payload["brief_sha256"], record["brief_sha256"])
        self.assertEqual(
            payload["dod_sha256"], record["brief_checks"][-1]["dod_sha256"]
        )
        self.assertEqual(intents.payload_digest(payload), row["digest"])

    def test_inline_open_binds_marker_and_deterministic_digest(self) -> None:
        self.open_and_review("R-inline", inline=True)
        record = self.record("R-inline")
        open_event = record["history"][0]
        self.assertEqual(open_event["dod_source"], "inline")
        self.assertIsNone(record["dod_path"])
        self.assertIsNone(record["dod_sha256"])  # inline cannot drift or refresh
        self.assertEqual(
            open_event["dod_sha256"], record["brief_checks"][-1]["dod_sha256"]
        )
        rc, err = self.mark_dispatched("R-inline", "k-inline")
        self.assertEqual(rc, 0, err)

    def test_brief_and_dod_drift_move_pre_approval_states_to_brief_revised(
        self,
    ) -> None:
        self.open_and_review("R2")
        self.dod.write_text(
            json.dumps([{"id": "unit2", "claim": "c2", "check_id": "green"}]),
            encoding="utf-8",
        )
        rc, err = self.mark_dispatched("R2", "k2")
        self.assertEqual(rc, 1)
        record = self.record("R2")
        self.assertEqual(record["state"], "brief_revised")
        self.assertIsNone(self.intent_row("k2"))
        # brief-check reloads the changed file-backed DoD as the reviewed pair.
        rc, _out, err = self.cli(
            "brief-check",
            "--dispatch-id",
            "R2",
            "--clean",
            "--by",
            "codex",
            "--surface-verdict",
            "complete",
            "--surface-reason",
            "r",
        )
        self.assertEqual(rc, 0, err)
        record = self.record("R2")
        self.assertEqual(record["dod"][0]["id"], "unit2")
        rc, err = self.mark_dispatched("R2", "k2")
        self.assertEqual(rc, 0, err)

    def test_dirty_baseline_refuses_with_exact_paths_and_no_cleanup(self) -> None:
        self.open_and_review("R3")
        (self.repo / "untracked-one").write_text("x\n", encoding="utf-8")
        (self.repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
        (self.repo / "staged-one").write_text("y\n", encoding="utf-8")
        self._git("add", "staged-one", self.repo)
        before = self.record("R3")
        rc, err = self.mark_dispatched("R3", "k3")
        self.assertEqual(rc, 1)
        self.assertIn("is not a clean baseline", err)
        self.assertIn("staged=['staged-one']", err)
        self.assertIn("tracked=['tracked.txt']", err)
        self.assertIn("untracked=['untracked-one']", err)
        self.assertTrue((self.repo / "untracked-one").exists())  # no cleanup
        self.assertEqual(self.record("R3"), before)  # atomic refusal
        self.assertIsNone(self.intent_row("k3"))

    def test_ignored_paths_never_block_the_clean_baseline(self) -> None:
        self.open_and_review("R4")
        (self.repo / "ignored-file").write_text("x\n", encoding="utf-8")
        rc, err = self.mark_dispatched("R4", "k4")
        self.assertEqual(rc, 0, err)

    def test_source_ahead_of_integration_refuses(self) -> None:
        self.open_and_review("R5")
        (self.repo / "tracked.txt").write_text("ahead\n", encoding="utf-8")
        self._git("add", "-A", self.repo)
        self._git("commit", "-m", "ahead", self.repo)
        rc, err = self.mark_dispatched("R5", "k5")
        self.assertEqual(rc, 1)
        self.assertIn(
            "the first implementation source HEAD must equal the integration HEAD",
            err,
        )
        self.assertIsNone(self.intent_row("k5"))

    def test_wrong_branch_base_root_actor_and_key_refuse_atomically(self) -> None:
        self.open_and_review("R6")
        before = self.record("R6")
        cases = []

        self._git("checkout", "-b", "other-branch", self.repo)
        cases.append(("branch", *self.mark_dispatched("R6", "k6")))
        self._git("checkout", "work-branch", self.repo)

        with contextlib.closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.execute(
                "update actors set project_root=? where id=?",
                (str(self.root / "elsewhere"), WORKER),
            )
        cases.append(("root", *self.mark_dispatched("R6", "k6")))
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.execute(
                "update actors set project_root=? where id=?",
                (str(self.repo), WORKER),
            )

        ghost = dict(before, expected_recipient="ghost-worker")
        (self.review_root / "R6.json").write_text(
            json.dumps(ghost, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        cases.append(("actor", *self.mark_dispatched("R6", "k6")))
        (self.review_root / "R6.json").write_text(
            json.dumps(before, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        cases.append(("key", *self.mark_dispatched("R6", "   ")))

        expected_errors = {
            "branch": "not the recorded named source branch",
            "root": "review_repo_worker_root_mismatch",
            "actor": "review_repo_worker_root_actor_missing",
            "key": "idempotency_key must be non-empty",
        }
        for label, rc, err in cases:
            with self.subTest(case=label):
                self.assertEqual(rc, 1)
                self.assertIn(expected_errors[label], err)
        self.assertEqual(self.record("R6"), before)
        self.assertIsNone(self.intent_row("k6"))

    def test_advanced_together_rebinds_base_without_refusing(self) -> None:
        # Integration and source advance together after open: the first active
        # intent rebinds the draft base to the derived clean common HEAD instead
        # of refusing on draft-base equality (contract 17, revision 6).
        self.open_and_review("R8")
        (self.repo / "tracked.txt").write_text("advance\n", encoding="utf-8")
        self._git("add", "-A", self.repo)
        self._git("commit", "-m", "advance", self.repo)
        self._git("merge", "--ff-only", "work-branch", self.integration)
        new_head = self._git("rev-parse", "HEAD", self.repo)
        self.assertNotEqual(new_head, self.record("R8")["base_commit"])  # base drifted
        rc, err = self.mark_dispatched("R8", "k8")
        self.assertEqual(rc, 0, err)
        record = self.record("R8")
        self.assertEqual(record["base_commit"], new_head)  # rebound, not refused
        row = self.intent_row("k8")
        self.assertEqual(row["state"], "active")
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["base_commit"], new_head)
        self.assertEqual(payload["source_head"], new_head)
        self.assertEqual(payload["integration_head"], new_head)

    def test_unbound_reviewed_pair_refuses(self) -> None:
        self.open_and_review("R7")
        record = self.record("R7")
        del record["brief_checks"][-1]["dod_sha256"]  # legacy check entry
        (self.review_root / "R7.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        rc, err = self.mark_dispatched("R7", "k7")
        self.assertEqual(rc, 1)
        self.assertIn("the reviewed brief/DoD pair is not bound", err)


class CrashRecoveryTest(ReviewIntentEnv):
    def test_absent_durable_entry_repeats_the_json_write_and_recovers(self) -> None:
        # Crash BEFORE durable JSON persistence: the retry's probe reads a
        # definite absence, so it repeats the atomic JSON write, re-reads, and
        # only then CASes the same prepared row active.
        self.open_and_review("C1")
        with (
            mock.patch.object(rounds, "persist", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            self.mark_dispatched("C1", "ck1")
        self.assertEqual(self.record("C1")["state"], "brief_reviewed")
        row = self.intent_row("ck1")
        self.assertEqual(row["state"], "prepared")
        self.assertEqual(
            rounds._probe_durable_round(reviewing_store.review_paths("C1"), dict(row)),
            rounds.PROBE_ABSENT,
        )
        rc, err = self.mark_dispatched("C1", "ck1")  # exact replay, then CAS
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.record("C1")["state"], "dispatched")
        row = self.intent_row("ck1")
        self.assertEqual((row["state"], row["attempt_count"]), ("active", 1))

    def _crash_before_cas(self, record_id: str, key: str) -> None:
        self.open_and_review(record_id)
        with (
            mock.patch.object(
                intents, "activate_in_ledger", side_effect=OSError("crash")
            ),
            self.assertRaises(OSError),
        ):
            self.mark_dispatched(record_id, key)
        self.assertEqual(self.record(record_id)["state"], "dispatched")
        self.assertEqual(self.intent_row(key)["state"], "prepared")

    def test_exact_durable_pair_resumes_activation_on_retry(self) -> None:
        self._crash_before_cas("C2", "ck2")
        self.assertEqual(
            rounds._probe_durable_round(
                reviewing_store.review_paths("C2"), dict(self.intent_row("ck2"))
            ),
            rounds.PROBE_EXACT,
        )
        rc, err = self.mark_dispatched("C2", "ck2")
        self.assertEqual(rc, 0, err)
        row = self.intent_row("ck2")
        self.assertEqual((row["state"], row["attempt_count"]), ("active", 1))
        self.assertEqual(len(self.record("C2")["intended_dispatches"]), 1)

    def test_prepared_crash_pair_never_expires_and_recovers_at_any_age(self) -> None:
        # A crash after durable JSON persistence but before the activate CAS
        # leaves a prepared row whose companion is on disk. Prepared has no
        # TTL: reconciliation leaves the pair prepared at any age, and the
        # exact retry activates it in place without incrementing the attempt.
        self._crash_before_cas("C3", "ck3")
        for column in ("prepared_at", "updated_at"):
            self.shift_intent("ck3", column, -(intents.ACTIVE_TTL_SECONDS * 4))
        now = _shift(NOW, intents.ACTIVE_TTL_SECONDS * 8)
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(intents.reconcile(conn, now), [])
        self.assertEqual(self.intent_row("ck3")["state"], "prepared")
        rc, err = self.mark_dispatched("C3", "ck3")  # reconciles (skips), CASes
        self.assertEqual(rc, 0, err)
        row = self.intent_row("ck3")
        self.assertEqual((row["state"], row["attempt_count"]), ("active", 1))
        self.assertEqual(len(self.record("C3")["intended_dispatches"]), 1)

    def test_aged_unpaired_prepared_row_stays_prepared_and_recovers(self) -> None:
        # Crash BEFORE durable JSON persistence, then arbitrary time passes: a
        # prepared SQL row with no round companion on disk is still never
        # abandoned by reconciliation (prepared has no TTL and no background
        # pass probes it); only the exact mark-dispatched retry advances it.
        self.open_and_review("C6")
        with (
            mock.patch.object(rounds, "persist", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            self.mark_dispatched("C6", "ck6")
        self.assertEqual(self.intent_row("ck6")["state"], "prepared")
        for column in ("prepared_at", "updated_at"):
            self.shift_intent("ck6", column, -(intents.ACTIVE_TTL_SECONDS * 4))
        now = _shift(NOW, intents.ACTIVE_TTL_SECONDS * 8)
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(intents.reconcile(conn, now), [])
        self.assertEqual(self.intent_row("ck6")["state"], "prepared")
        rc, err = self.mark_dispatched("C6", "ck6")
        self.assertEqual(rc, 0, err)
        row = self.intent_row("ck6")
        self.assertEqual((row["state"], row["attempt_count"]), ("active", 1))

    def test_unknown_durable_state_fails_closed_without_mutation(self) -> None:
        self._crash_before_cas("C7", "ck7")
        record_bytes = (self.review_root / "C7.json").read_bytes()
        with mock.patch.object(
            rounds, "_probe_durable_round", return_value=rounds.PROBE_UNKNOWN
        ):
            rc, err = self.mark_dispatched("C7", "ck7")
        self.assertEqual(rc, 1)
        self.assertIn("mark_dispatched_durable_unknown", err)
        self.assertEqual(self.intent_row("ck7")["state"], "prepared")
        self.assertEqual((self.review_root / "C7.json").read_bytes(), record_bytes)

    def test_probe_classifies_unreadable_record_bytes_as_unknown(self) -> None:
        self._crash_before_cas("C8", "ck8")
        paths = reviewing_store.review_paths("C8")
        row = dict(self.intent_row("ck8"))
        self.assertEqual(rounds._probe_durable_round(paths, row), rounds.PROBE_EXACT)
        paths.json.write_text("{not json", encoding="utf-8")
        self.assertEqual(rounds._probe_durable_round(paths, row), rounds.PROBE_UNKNOWN)

    def test_mismatched_durable_entry_is_a_hard_conflict_never_absent(self) -> None:
        self._crash_before_cas("C9", "ck9")
        record = self.record("C9")
        # Drift one companion field while keeping the recorded key and digest,
        # so only the probe's full canonical rebuild can catch the mismatch.
        record["intended_dispatches"][-1]["source_branch"] = "forged-branch"
        (self.review_root / "C9.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        rc, err = self.mark_dispatched("C9", "ck9")
        self.assertEqual(rc, 1)
        self.assertIn("mark_dispatched_durable_conflict", err)
        self.assertEqual(self.intent_row("ck9")["state"], "prepared")  # no mutation

    def test_recovery_rerun_after_activation_is_idempotent(self) -> None:
        self.open_and_review("C4")
        rc, err = self.mark_dispatched("C4", "ck4")
        self.assertEqual(rc, 0, err)
        rc, err = self.mark_dispatched("C4", "ck4")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.record("C4")["intended_dispatches"]), 1)
        self.assertEqual(self.intent_row("ck4")["state"], "active")

    def test_recovery_with_a_different_key_refuses(self) -> None:
        self.open_and_review("C5")
        rc, err = self.mark_dispatched("C5", "ck5")
        self.assertEqual(rc, 0, err)
        rc, err = self.mark_dispatched("C5", "ck5-other")
        self.assertEqual(rc, 1)
        self.assertIn("mark_dispatched_recovery_mismatch", err)


class DispatchConsumptionTest(ReviewIntentEnv):
    def _message_count(self) -> int:
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn:
            return conn.execute("select count(*) from messages").fetchone()[0]

    def _ledger_count(self) -> int:
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn:
            return conn.execute("select count(*) from dispatch_ledger").fetchone()[0]

    def test_ordinary_dispatch_without_intent_is_preserved(self) -> None:
        dispatch = self.store.dispatch_agent(ARCHITECT, WORKER, "plain", "s", "b", [])
        self.assertEqual(dispatch["status"], "queued")
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn:
            self.assertEqual(
                conn.execute("select count(*) from review_dispatch_intents").fetchone()[
                    0
                ],
                0,
            )

    def test_active_intent_binds_in_the_queued_row_transaction(self) -> None:
        self.open_and_review("B1")
        rc, err = self.mark_dispatched("B1", "bk1")
        self.assertEqual(rc, 0, err)
        dispatch = self.store.dispatch_agent(ARCHITECT, WORKER, "bk1", "s", "b", [])
        row = self.intent_row("bk1")
        self.assertEqual(row["state"], "bound")
        self.assertIsNone(row["preledger_state"])
        self.assertEqual(row["dispatch_id"], dispatch["dispatch_id"])
        replay = self.store.dispatch_agent(ARCHITECT, WORKER, "bk1", "s", "b", [])
        self.assertEqual(replay["dispatch_id"], dispatch["dispatch_id"])
        self.assertEqual(self._ledger_count(), 1)  # bound replay, no fan-out
        self.assertEqual(self.intent_row("bk1")["state"], "bound")

    def test_non_active_and_mismatched_intents_refuse_with_zero_effects(self) -> None:
        self.open_and_review("B2")
        with (
            mock.patch.object(
                intents, "activate_in_ledger", side_effect=OSError("crash")
            ),
            self.assertRaises(OSError),
        ):
            self.mark_dispatched("B2", "bk2")
        self.assertEqual(self.intent_row("bk2")["state"], "prepared")
        messages, rows = self._message_count(), self._ledger_count()
        with self.assertRaisesRegex(ValidationError, "review_intent_not_active"):
            self.store.dispatch_agent(ARCHITECT, WORKER, "bk2", "s", "b", [])
        self.assertEqual(
            (self._message_count(), self._ledger_count()), (messages, rows)
        )
        rc, err = self.mark_dispatched("B2", "bk2")
        self.assertEqual(rc, 0, err)
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.execute(
                "update review_dispatch_intents set recipient_actor_id=? "
                "where idempotency_key='bk2'",
                (ARCHITECT,),
            )
        with self.assertRaisesRegex(ValidationError, "review_intent_mismatch"):
            self.store.dispatch_agent(ARCHITECT, WORKER, "bk2", "s", "b", [])
        self.assertEqual(
            (self._message_count(), self._ledger_count()), (messages, rows)
        )
        self.assertEqual(self.intent_row("bk2")["state"], "active")

    def test_expired_active_intent_is_reconciled_and_refused_at_dispatch(self) -> None:
        self.open_and_review("B3")
        rc, err = self.mark_dispatched("B3", "bk3")
        self.assertEqual(rc, 0, err)
        for column in ("activated_at", "updated_at"):
            self.shift_intent("bk3", column, -(intents.ACTIVE_TTL_SECONDS + 60))
        with self.assertRaisesRegex(ValidationError, "review_intent_not_active"):
            self.store.dispatch_agent(ARCHITECT, WORKER, "bk3", "s", "b", [])
        self.assertEqual(self.intent_row("bk3")["state"], "abandoned")
        self.assertEqual(self._ledger_count(), 0)

    def test_spawn_failure_still_consumes_the_bound_intent(self) -> None:
        self.open_and_review("B4")
        rc, err = self.mark_dispatched("B4", "bk4")
        self.assertEqual(rc, 0, err)
        self.store.dispatch_agent(ARCHITECT, WORKER, "bk4", "s", "b", [])
        started = self.store.start_queued_dispatches(
            lambda _runtime: FailingSpawnAdapter()
        )
        self.assertEqual(started[0]["status"], "spawn_failed_message_landed")
        row = self.intent_row("bk4")
        self.assertEqual(row["state"], "bound")
        self.assertEqual(row["dispatch_id"], started[0]["dispatch_id"])

    def test_bound_intent_keeps_legacy_delta_close_path_unchanged(self) -> None:
        self.open_and_review("B5")
        rc, err = self.mark_dispatched("B5", "bk5")
        self.assertEqual(rc, 0, err)
        dispatch = self.store.dispatch_agent(ARCHITECT, WORKER, "bk5", "s", "b", [])
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status='in_flight', "
                "spawned_at=created_at, expected_close_by='2099-01-01T00:00:00+00:00' "
                "where dispatch_id=?",
                (dispatch["dispatch_id"],),
            )
        (self.repo / "work-product").write_text("delta\n", encoding="utf-8")
        reply = self.store.send_message(
            WORKER,
            [ARCHITECT],
            "done",
            "done",
            [],
            parent_message_id=dispatch["message_id"],
        )
        closed = self.store.close_dispatch(
            WORKER,
            message_id=dispatch["message_id"],
            result="satisfied",
            reply_message_id=reply["id"],
            summary="complete",
            delta=True,
        )
        self.assertEqual(closed["result"], "satisfied")
        self.assertEqual(self.intent_row("bk5")["state"], "bound")

    def test_monitor_reconciliation_abandons_and_touches_no_review_json(self) -> None:
        self.open_and_review("B6")
        rc, err = self.mark_dispatched("B6", "bk6")
        self.assertEqual(rc, 0, err)
        record_bytes = (self.review_root / "B6.json").read_bytes()
        for column in ("activated_at", "updated_at"):
            self.shift_intent("bk6", column, -(intents.ACTIVE_TTL_SECONDS + 60))
        actions = self.store.reconcile_dispatches(
            lambda _runtime: FailingSpawnAdapter()
        )
        abandoned = [
            action
            for action in actions
            if action.get("status") == "review_intent_abandoned"
        ]
        self.assertEqual(abandoned[0]["idempotency_key"], "bk6")
        self.assertEqual(abandoned[0]["expired_state"], "active")
        self.assertEqual(self.intent_row("bk6")["state"], "abandoned")
        self.assertEqual((self.review_root / "B6.json").read_bytes(), record_bytes)

    def test_status_reports_the_derived_intent_view_read_only(self) -> None:
        self.open_and_review("B7")
        rc, err = self.mark_dispatched("B7", "bk7")
        self.assertEqual(rc, 0, err)
        record_bytes = (self.review_root / "B7.json").read_bytes()
        rc, out, err = self.cli(
            "status",
            "--dispatch-id",
            "B7",
            "--expected-repo-root",
            str(review.REPO_ROOT),
        )
        self.assertEqual(rc, 0, err)
        view = json.loads(out)["intent"]
        row = self.intent_row("bk7")
        self.assertEqual(view["state"], "active")
        self.assertEqual(view["intent_id"], row["intent_id"])
        self.assertEqual(view["expires_at"], intents.expiry_deadline(dict(row)))
        self.assertFalse(view["expired"])
        self.assertIn("dispatch_agent", view["remedy"])
        self.assertIsNone(view["dispatch_id"])
        self.assertEqual((self.review_root / "B7.json").read_bytes(), record_bytes)


class LegacyCompatibilityTest(ReviewIntentEnv):
    def test_legacy_and_mixed_age_intent_entries_validate(self) -> None:
        self.open_and_review("L1")
        rc, err = self.mark_dispatched("L1", "lk1")
        self.assertEqual(rc, 0, err)
        record = self.record("L1")
        legacy_entry = {
            "attempt": 2,
            "idempotency_key": "legacy-key",
            "recorded_at": "2026-01-01T00:00:00Z",
        }
        record["intended_dispatches"].append(legacy_entry)
        validate_record(record)  # mixed-age list: new companions plus legacy
        # A full companion missing one field is a partial (all-or-nothing) shape.
        broken = json.loads(json.dumps(record))
        del broken["intended_dispatches"][0]["intent_id"]
        with self.assertRaisesRegex(ReviewError, "intent binding"):
            validate_record(broken)
        # A legacy entry that grows a single companion field is likewise partial.
        broken = json.loads(json.dumps(record))
        broken["intended_dispatches"][1]["intent_digest"] = "e" * 64
        with self.assertRaisesRegex(ReviewError, "intent binding"):
            validate_record(broken)

    def test_cold_start_ledger_stays_openable_and_dispatches_ordinarily(self) -> None:
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn:
            self.assertEqual(
                int(conn.execute("pragma user_version").fetchone()[0]),
                LEDGER_SCHEMA_VERSION,
            )
        dispatch = self.store.dispatch_agent(ARCHITECT, WORKER, "cold", "s", "b", [])
        self.assertEqual(dispatch["status"], "queued")

    def test_prior_loaded_stale_surface_refuses_before_any_write(self) -> None:
        rows = None
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn:
            rows = conn.execute("select count(*) from dispatch_ledger").fetchone()[0]
        with (
            mock.patch.object(
                code_identity, "LOADED_SURFACE", {"agent_comms/db.py": "0" * 64}
            ),
            mock.patch.object(code_identity, "LOADED_CODE_IDENTITY", "f" * 64),
            mock.patch.object(code_identity, "LOADED_CONTRACT_VERSION", 16),
        ):
            with self.assertRaises(code_identity.StaleModuleError):
                self.store.dispatch_agent(ARCHITECT, WORKER, "stale", "s", "b", [])
        with contextlib.closing(sqlite3.connect(self.ledger)) as conn:
            self.assertEqual(
                conn.execute("select count(*) from dispatch_ledger").fetchone()[0],
                rows,
            )


class CodeIdentityGovernanceTest(unittest.TestCase):
    def test_server_imported_reviewing_modules_included_governing_unexcluded(
        self,
    ) -> None:
        # intents.py (Landing 1) plus the Landing 2 server-imported pair
        # reply_snapshots.py and its git_evidence.py custody dependency are all
        # included, governing, and never masked by the broad reviewing/** exclusion.
        for relpath in (
            "agent_comms/reviewing/intents.py",
            "agent_comms/reviewing/reply_snapshots.py",
            "agent_comms/reviewing/git_evidence.py",
        ):
            with self.subTest(relpath=relpath):
                self.assertIn(relpath, code_identity.INCLUDED_EXPLICIT)
                self.assertIn(relpath, code_identity.CONTRACT_GOVERNING)
                self.assertTrue(code_identity.is_included_surface(relpath))
                self.assertIsNone(code_identity.exclusion_reason(relpath))

    def test_non_included_reviewing_modules_keep_the_broad_exclusion(self) -> None:
        reason = code_identity.exclusion_reason("agent_comms/reviewing/rounds.py")
        self.assertIsNotNone(reason)
        self.assertNotIn(
            "agent_comms/reviewing/rounds.py", code_identity.CONTRACT_GOVERNING
        )

    def test_contract_version_nineteen_and_digest_declared(self) -> None:
        self.assertEqual(code_identity.CONTRACT_VERSION, 21)
        self.assertEqual(code_identity.current_contract_version(), 21)
        self.assertEqual(
            code_identity.contract_surface_digest(),
            code_identity.CONTRACT_SURFACE_DIGEST,
        )

    def test_rounds_edges_are_declared_and_intents_imports_no_sibling(self) -> None:
        contract = json.loads(
            (Path(__file__).parent / "review_decomposition_contract.json").read_text(
                encoding="utf-8"
            )
        )
        edges = {tuple(edge) for edge in contract["architecture_allowed_edges"]}
        self.assertEqual(
            {edge for edge in edges if edge[0] == "rounds"},
            {
                ("rounds", "briefs"),
                ("rounds", "contracts"),
                ("rounds", "git_evidence"),
                ("rounds", "intents"),
                ("rounds", "ledger_evidence"),
                ("rounds", "store"),
            },
        )
        self.assertEqual({edge for edge in edges if edge[0] == "intents"}, set())
        source = (
            Path(code_identity.SURFACE_ROOT) / "agent_comms/reviewing/intents.py"
        ).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", "") or ""
                names = ".".join(alias.name for alias in node.names)
                self.assertNotIn("reviewing", f"{module}.{names}")


if __name__ == "__main__":
    unittest.main()
