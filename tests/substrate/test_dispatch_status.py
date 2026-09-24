import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_comms import cli
from agent_comms.spawn import render_spawn
from agent_comms.schema import ValidationError
from agent_comms.store import Store


def seed_dispatch_actors(store: Store, root: Path) -> None:
    store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
    store.register_agent_actor("alpha-worker", "alpha", "worker", str(root / "alpha-worker"), [], owner="alpha-architect")


def seed_two_dispatches(store: Store, db_path: Path) -> tuple[str, str]:
    older = store.dispatch_agent(
        "alpha-architect",
        "alpha-worker",
        "dispatch-status-older",
        "Older",
        "Older body.",
        [],
    )
    newer = store.dispatch_agent(
        "alpha-architect",
        "alpha-worker",
        "dispatch-status-newer",
        "Newer",
        "Newer body.",
        [],
    )
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute(
                """
                update dispatch_ledger
                set created_at = ?, status = ?, failure_reason = NULL, expected_close_by = NULL
                where dispatch_id = ?
                """,
                ("2026-05-29T10:00:00+00:00", "queued", older["dispatch_id"]),
            )
            conn.execute(
                """
                update dispatch_ledger
                set created_at = ?, status = ?, failure_reason = ?, expected_close_by = ?
                where dispatch_id = ?
                """,
                (
                    "2026-05-29T11:00:00+00:00",
                    "dlq",
                    "timeout",
                    "2026-05-29T10:30:00+00:00",
                    newer["dispatch_id"],
                ),
            )
    return older["dispatch_id"], newer["dispatch_id"]


class DispatchStatusTest(unittest.TestCase):
    def test_unfiltered_returns_both_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            store = Store(db_path)
            seed_dispatch_actors(store, root)
            older_id, newer_id = seed_two_dispatches(store, db_path)

            dispatches = store.list_dispatches()

            self.assertEqual([row["dispatch_id"] for row in dispatches], [newer_id, older_id])
            self.assertEqual(
                set(dispatches[0]),
                {
                    "dispatch_id",
                    "recipient_actor_id",
                    "producer_actor_id",
                    "status",
                    "result",
                    "override_reason",
                    "failure_reason",
                    "expected_close_by",
                    "created_at",
                    "observed_values",
                    # Stage-2 joined-projection reporting integration: the
                    # recipient transport status plus the normalized outcome.
                    "transport_status",
                    "outcome",
                },
            )
            self.assertEqual(dispatches[0]["status"], "dlq")
            self.assertEqual(dispatches[0]["failure_reason"], "timeout")
            # The projection exposes the outcome without inferring one state
            # machine from the other; the dlq row here has no cancellation.
            self.assertEqual(dispatches[0]["outcome"], "dlq")
            self.assertEqual(dispatches[1]["status"], "queued")
            self.assertEqual(dispatches[1]["outcome"], "queued")

    def test_status_filter_returns_dlq_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            store = Store(db_path)
            seed_dispatch_actors(store, root)
            _, newer_id = seed_two_dispatches(store, db_path)

            dispatches = store.list_dispatches(status="dlq")

            self.assertEqual([row["dispatch_id"] for row in dispatches], [newer_id])
            self.assertEqual(dispatches[0]["status"], "dlq")

    def test_limit_truncates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            store = Store(db_path)
            seed_dispatch_actors(store, root)
            _, newer_id = seed_two_dispatches(store, db_path)

            dispatches = store.list_dispatches(limit=1)

            self.assertEqual([row["dispatch_id"] for row in dispatches], [newer_id])

    def test_non_positive_limit_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            with self.assertRaisesRegex(ValidationError, "limit must be at least 1"):
                store.list_dispatches(limit=0)


# The exact (ledger_status, transport_status) -> normalized outcome projections
# the compact/JSON dispatch-status surface must render truthfully. Every pair is
# mechanically reachable and never infers one state machine from the other.
PROJECTIONS = (
    ("cancelled", "cancelled", "confirmed_cancel"),
    ("cancelled", "closed", "confirmed_cancel_transport_closed_first"),
    ("dlq", "sent", "dlq"),
    ("dlq", "cancelled", "operator_settled_termination_unconfirmed"),
    ("closed", "closed", "closed"),
)

# The compact table column header, in the exact pinned order: the two INDEPENDENT
# state machines, their joined outcome, and the v2 checked result lead right
# after status, then the pre-existing columns unchanged.
COMPACT_COLUMNS = (
    "created",
    "status",
    "transport",
    "outcome",
    "result",
    "recipient",
    "producer",
    "override_reason",
    "failure",
    "expected_close_by",
    "dispatch_id",
)


class DispatchStatusCliProjectionBase(unittest.TestCase):
    """Drives the REAL ``dispatch-status`` CLI (``agent_comms.cli.run``)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        seed_dispatch_actors(self.store, self.root)

    def _seed_projection(self, key: str, ledger_status: str, transport_status: str, created_at: str) -> str:
        d = self.store.dispatch_agent(
            "alpha-architect", "alpha-worker", key, f"S {key}", f"B {key}", []
        )
        did = d["dispatch_id"]
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                # Seeded raw (ledger, transport) pairs are the LEGACY projection
                # surface: a result-less ``closed`` ledger row only exists on a
                # v1 dispatch (the v2 CHECK requires a checked result at close).
                conn.execute(
                    "update dispatch_ledger set status = ?, created_at = ?, "
                    "policy_version = 'v1' where dispatch_id = ?",
                    (ledger_status, created_at, did),
                )
                conn.execute(
                    "update message_recipients set status = ? where message_id = ? and to_agent = ?",
                    (transport_status, d["message_id"], "alpha-worker"),
                )
        return did

    def _seed_all(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for index, (ledger_status, transport_status, outcome) in enumerate(PROJECTIONS):
            did = self._seed_projection(
                f"proj-{index}",
                ledger_status,
                transport_status,
                f"2026-05-29T10:0{index}:00+00:00",
            )
            mapping[outcome] = did
        return mapping

    def _run(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cli.run(["--db", str(self.db_path), "dispatch-status", *argv])
        return rc, buffer.getvalue()

    def _seed_malformed_codex_actor(self) -> None:
        actor_id = "broken-codex-worker"
        spawn = render_spawn("codex", actor_id)
        self.store.register_agent_actor(
            actor_id,
            "alpha",
            "worker",
            str(self.root / actor_id),
            [],
            runtime="codex",
            spawn=spawn,
            owner="alpha-architect",
        )
        with self.store._db.connection() as conn:
            conn.execute(
                "update actors set spawn_json = '{}' where id = ?", (actor_id,)
            )


class DispatchStatusCliJsonTest(DispatchStatusCliProjectionBase):
    def test_malformed_observed_values_preserves_bad_and_good_rows_in_both_views(self):
        good_id, bad_id = seed_two_dispatches(self.store, self.db_path)
        observed = {
            "worker_log": "/fixture/worker.log",
            "codex_auth": {"read_status": "ok"},
        }
        for malformed in ("not JSON", "[]", "null", "42"):
            with self.subTest(malformed=malformed):
                with self.store.connection() as conn:
                    conn.execute(
                        "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                        (json.dumps(observed), good_id),
                    )
                    conn.execute(
                        "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
                        (malformed, bad_id),
                    )
                rows = self.store.list_dispatches()
                rc, output = self._run(["--json"])
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(output), rows)
                by_id = {row["dispatch_id"]: row for row in rows}
                self.assertEqual(set(by_id), {good_id, bad_id})
                self.assertEqual(by_id[good_id]["observed_values"], observed)
                self.assertNotIn("observed_values_malformed", by_id[good_id])
                self.assertEqual(by_id[bad_id]["observed_values"], {})
                self.assertIs(by_id[bad_id]["observed_values_malformed"], True)
                rc, output = self._run([])
                self.assertEqual(rc, 0)
                self.assertIn(f"{bad_id} observed_values=MALFORMED", output)
                self.assertIn(good_id, output)

    def test_defect_returns_object_and_exit_three(self) -> None:
        self._seed_all()
        self._seed_malformed_codex_actor()
        rc, text = self._run(["--json"])
        self.assertEqual(rc, 3)
        payload = json.loads(text)
        self.assertEqual(set(payload), {"dispatches", "actor_defects"})
        self.assertEqual(payload["actor_defects"][0]["code"], "missing_codex_home")

    def test_json_shape_unchanged_and_projections_truthful(self) -> None:
        mapping = self._seed_all()
        rc, text = self._run(["--json"])
        self.assertEqual(rc, 0)
        rows = {row["dispatch_id"]: row for row in json.loads(text)}
        # The JSON shape is unchanged and already carries the joined-projection
        # keys: status, transport_status, outcome (plus the pre-existing fields).
        sample = rows[mapping["closed"]]
        self.assertIn("status", sample)
        self.assertIn("transport_status", sample)
        self.assertIn("outcome", sample)
        # Every exact projection renders truthfully, never inferring one state
        # machine from the other.
        for ledger_status, transport_status, outcome in PROJECTIONS:
            row = rows[mapping[outcome]]
            self.assertEqual(row["status"], ledger_status, outcome)
            self.assertEqual(row["transport_status"], transport_status, outcome)
            self.assertEqual(row["outcome"], outcome)

    def test_status_filter_by_ledger_status_preserved(self) -> None:
        mapping = self._seed_all()
        rc, text = self._run(["--status", "dlq", "--json"])
        self.assertEqual(rc, 0)
        rows = json.loads(text)
        # Filtering is by LEDGER status: both dlq rows (a plain dlq/sent and the
        # operator-settled dlq/cancelled) are returned; the cancelled/closed rows
        # are not.
        got = {row["dispatch_id"]: row["outcome"] for row in rows}
        self.assertEqual(
            got,
            {
                mapping["dlq"]: "dlq",
                mapping["operator_settled_termination_unconfirmed"]: "operator_settled_termination_unconfirmed",
            },
        )

    def test_limit_and_bad_limit_preserved(self) -> None:
        self._seed_all()
        rc, text = self._run(["--limit", "2", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(text)), 2)
        # A non-positive limit is the pre-existing ValidationError refusal (rc 2).
        rc, text = self._run(["--limit", "0", "--json"])
        self.assertEqual(rc, 2)
        self.assertIn("limit must be at least 1", json.loads(text)["error"])


class DispatchStatusCliCompactTest(DispatchStatusCliProjectionBase):
    def test_dead_credential_human_only_and_before_custody(self):
        self._seed_all()
        rc, before = self._run([])
        self.assertNotIn("codex-credential-dead", before)
        _, json_before = self._run(["--json"])
        with self.store.connection() as conn:
            conn.execute(
                "insert into codex_refresh_claims(lineage_key,dead_reason,dead_at,dead_digest) values('lineage','expired','stamp','digest')"
            )
        rc, text = self._run([])
        line = "codex-credential-dead lineage reason=expired since=stamp  remedy=run codex login for that CODEX_HOME"
        self.assertEqual(rc, 0)
        self.assertEqual(text, before + line + "\n")
        self.assertEqual(self._run(["--json"]), (0, json_before))
        self._seed_malformed_codex_actor()
        rc, text = self._run([])
        self.assertEqual(rc, 3)
        self.assertLess(text.index(line), text.index("custody-defect"))
        rc, text = self._run(["--json"])
        self.assertEqual(rc, 3)
        self.assertEqual(set(json.loads(text)), {"dispatches", "actor_defects"})

    def test_defect_prints_complete_table_then_defect_and_exits_three(self) -> None:
        mapping = self._seed_all()
        self._seed_malformed_codex_actor()
        rc, text = self._run([])
        self.assertEqual(rc, 3)
        self.assertIn(mapping["closed"], text)
        self.assertIn(
            "custody-defect broken-codex-worker missing_codex_home  remedy=",
            text,
        )

    def test_compact_header_has_exact_column_order(self) -> None:
        self._seed_all()
        rc, text = self._run([])
        self.assertEqual(rc, 0)
        header = text.splitlines()[0].split()
        self.assertEqual(tuple(header), COMPACT_COLUMNS)

    def test_compact_rows_render_transport_and_outcome(self) -> None:
        mapping = self._seed_all()
        rc, text = self._run([])
        self.assertEqual(rc, 0)
        lines = text.splitlines()
        by_did = {}
        for line in lines[2:]:  # skip header + separator
            fields = line.split()
            if fields:
                by_did[fields[-1]] = fields  # dispatch_id is the last column
        for ledger_status, transport_status, outcome in PROJECTIONS:
            fields = by_did[mapping[outcome]]
            # Columns: created status transport outcome recipient producer ...
            self.assertEqual(fields[1], ledger_status, outcome)
            self.assertEqual(fields[2], transport_status, outcome)
            self.assertEqual(fields[3], outcome)

    def test_compact_status_filter_preserved(self) -> None:
        mapping = self._seed_all()
        rc, text = self._run(["--status", "cancelled"])
        self.assertEqual(rc, 0)
        # Only the two cancelled-ledger rows appear; each shows its distinct
        # joined outcome, never a plain "closed"/"cancelled".
        dids = {line.split()[-1] for line in text.splitlines()[2:] if line.split()}
        self.assertEqual(
            dids,
            {mapping["confirmed_cancel"], mapping["confirmed_cancel_transport_closed_first"]},
        )
        self.assertIn("confirmed_cancel_transport_closed_first", text)


if __name__ == "__main__":
    unittest.main()
