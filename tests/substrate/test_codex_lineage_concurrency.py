import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_comms import codex_auth_refresh
from agent_comms.adapters import DispatchStart
from agent_comms.schema import ValidationError
from agent_comms.store import Store

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"


def jwt(exp):
    part = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"x.{part}.x"


class Adapter:
    def __init__(self):
        self.calls = []

    def dispatch(self, context):
        self.calls.append(context)
        return DispatchStart(f"fake:{context.dispatch['dispatch_id']}", {"fake": True})

    def halt(self, _handle, observed_values=None):
        pass


class CodexLineageConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "db.sqlite")
        self.store.register_actor(HUMAN_ID, "human", "operator")
        self.store.register_actor("arch", "agent", "arch", team="t", role="architect",
                                  project_root=str(self.root / "arch"), capabilities=[], dispatch_cap=4)
        self.home = self.root / "home"
        self.home.mkdir()
        self._write_token(7200)
        for actor in ("worker-a", "worker-b"):
            self.store.register_agent_actor(actor, "t", "worker", str(self.root / actor), [],
                runtime="codex", spawn={"env": {"CODEX_HOME": str(self.home)}}, owner="arch")

    def tearDown(self):
        self.temp.cleanup()

    def _write_token(self, remaining):
        exp = int(datetime.now(timezone.utc).timestamp()) + remaining
        (self.home / "auth.json").write_text(json.dumps({"access_token": jwt(exp)}))

    def _dispatch(self, actor, key, adapter=None, ttl=60):
        return self.store.dispatch_agent("arch", actor, key, "s", "b", [], ttl_seconds=ttl,
            adapter_for_runtime=(lambda _runtime: adapter) if adapter else None)

    def test_two_same_lineage_dispatches_inline_and_close_independently(self):
        adapter = Adapter()
        first = self._dispatch("worker-a", "one", adapter)
        second = self._dispatch("worker-b", "two", adapter)
        self.assertEqual([first["status"], second["status"]], ["in_flight", "in_flight"])
        self.assertEqual(len(adapter.calls), 2)
        self.assertNotIn("lineage_busy", json.dumps([first, second]))
        for actor, row in (("worker-a", first), ("worker-b", second)):
            reply = self.store.send_message(actor, ["arch"], "done", "done", [],
                                            parent_message_id=row["message_id"])
            self.store.close_dispatch(actor, message_id=row["message_id"], reply_message_id=reply["id"],
                                      result="satisfied", summary="done", delta=False)
        self.assertEqual([self.store._dispatch_by_idempotency_key_fresh("arch", k)["status"]
                          for k in ("one", "two")], ["closed", "closed"])

    def test_freshness_boundary_and_unparseable_token_fail_closed(self):
        with mock.patch.dict("os.environ", {"AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS": "900"}):
            self._write_token(960)
            boundary = self._dispatch("worker-a", "boundary", Adapter(), ttl=60)
            self.assertEqual(boundary["status"], "queued")
            self.assertEqual(boundary["lineage_gate_status"], "token_stale")
            (self.home / "auth.json").write_text(json.dumps({"access_token": "bad"}))
            malformed = self._dispatch("worker-b", "malformed", Adapter(), ttl=60)
            self.assertEqual(malformed["lineage_gate_status"], "token_stale")

    def test_actual_gate_is_strict_one_second_more_promotes(self):
        fixed = datetime(2026, 8, 28, tzinfo=timezone.utc)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed if tz else fixed.replace(tzinfo=None)

        exp = int(fixed.timestamp())
        with (
            mock.patch.dict(
                "os.environ",
                {"AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS": "900"},
            ),
            mock.patch("agent_comms.codex_auth_refresh.datetime", FrozenDatetime),
        ):
            stale_adapter = Adapter()
            (self.home / "auth.json").write_text(
                json.dumps({"access_token": jwt(exp + 960)})
            )
            stale = self._dispatch("worker-a", "equal-boundary", stale_adapter, ttl=60)
            self.assertEqual(stale["status"], "queued")
            self.assertEqual(stale["lineage_gate_status"], "token_stale")
            self.assertEqual(stale_adapter.calls, [])
            fresh_adapter = Adapter()
            (self.home / "auth.json").write_text(
                json.dumps({"access_token": jwt(exp + 961)})
            )
            fresh = self._dispatch("worker-b", "one-second-more", fresh_adapter, ttl=60)
            self.assertEqual(fresh["status"], "in_flight")
            self.assertEqual(len(fresh_adapter.calls), 1)

    def test_insufficient_live_token_waits_for_expiry_then_drains(self):
        lineage_key = os.path.realpath(self.home / "auth.json")
        with mock.patch.dict(
            "os.environ", {"AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS": "900"}
        ):
            self._write_token(960)
            row = self._dispatch("worker-a", "wait-expiry", Adapter(), ttl=60)
            self.assertEqual(row["status"], "queued")
            self.assertEqual(row["lineage_gate_status"], "token_stale")
            calls = []
            with mock.patch("shutil.which", return_value="/abs/codex"):
                attempt = codex_auth_refresh.refresh_if_due(
                    self.store,
                    lineage_key=lineage_key,
                    codex_home=str(self.home),
                    actor_count=1,
                    lineage_ordinal=0,
                    exec_runner=lambda home, binary: calls.append(home) or True,
                )
            self.assertEqual(attempt["outcome"], "not_due")
            self.assertEqual(calls, [])
            self.assertEqual(
                self.store._dispatch_by_idempotency_key_fresh("arch", "wait-expiry")[
                    "status"
                ],
                "queued",
            )
            self._write_token(0)

            def rotate(home, binary):
                self._write_token(7200)
                return True

            with mock.patch("shutil.which", return_value="/abs/codex"):
                rotated = codex_auth_refresh.refresh_if_due(
                    self.store,
                    lineage_key=lineage_key,
                    codex_home=str(self.home),
                    actor_count=1,
                    lineage_ordinal=0,
                    exec_runner=rotate,
                )
            self.assertEqual(rotated["outcome"], "refreshed")
            adapter = Adapter()
            actions = self.store.start_queued_dispatches(
                lambda _runtime: adapter, limit=4, ttl_seconds=60
            )
        self.assertEqual(sum(a["status"] == "in_flight" for a in actions), 1)
        self.assertEqual(len(adapter.calls), 1)

    def test_accept_time_satisfiability_boundary_is_before_write(self):
        # Static bound: seven-day minimum rotated-token lifetime (604800).
        with mock.patch.dict(
            "os.environ",
            {
                "AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS": "100",
            },
        ):
            with self.assertRaisesRegex(
                ValidationError, "maximum admissible ttl_seconds is 604699"
            ):
                self._dispatch("worker-a", "impossible", ttl=604700)
            self.assertEqual(self.store.list_dispatches(), [])
            adapter = Adapter()
            accepted = self._dispatch("worker-a", "maximal", adapter, ttl=604699)
        self.assertEqual(accepted["status"], "queued")
        self.assertEqual(accepted["lineage_gate_status"], "token_stale")
        self.assertEqual(adapter.calls, [])

    def test_unsatisfiable_promotion_pages_once_and_stays_queued(self):
        with mock.patch.dict("os.environ", {
            "AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS": "100",
        }):
            row = self._dispatch("worker-a", "unsatisfiable", ttl=604699)
        adapter = Adapter()
        with mock.patch.dict(
            "os.environ",
            {
                "AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS": "200",
            },
        ):
            passes = [
                self.store.start_queued_dispatches(
                    lambda _runtime: adapter, limit=4, ttl_seconds=604699
                )
                for _ in range(3)
            ]
        self.assertEqual(len(passes), 3)
        self.assertTrue(all(sum(a["status"] == "token_gate_unsatisfiable" for a in p) == 1
                            for p in passes))
        self.assertEqual(adapter.calls, [])
        current = self.store._dispatch_by_idempotency_key_fresh("arch", "unsatisfiable")
        self.assertEqual(current["status"], "queued")
        observed = current["observed_values"]
        self.assertIn("token_gate_unsatisfiable_paged_at", observed)
        self.assertIn("token_gate_unsatisfiable_page_message_id", observed)
        self.assertEqual(len(self.store.list_inbox("arch")), 1)

    def test_token_gate_and_queued_age_pages_coexist(self):
        with mock.patch.dict("os.environ", {
            "AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS": "100",
        }):
            first = self._dispatch("worker-a", "coexist-token", ttl=604699)
        with mock.patch.dict(
            "os.environ",
            {
                "AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS": "200",
            },
        ):
            self.store.start_queued_dispatches(
                lambda _runtime: Adapter(), ttl_seconds=604699
            )
        with self.store.connection() as conn:
            conn.execute("update dispatch_ledger set created_at=? where dispatch_id=?",
                         ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                          first["dispatch_id"]))
        with mock.patch.dict("os.environ", {"AGENT_COMMS_QUEUED_AGE_PAGE_SECONDS": "1"}):
            actions = self.store._dispatch._page_old_unheld_queued_codex_dispatches(HUMAN_ID)
        self.assertEqual(sum(a["status"] == "queued_age_producer_paged" for a in actions), 1)
        current = self.store._dispatch_by_idempotency_key_fresh("arch", "coexist-token")
        self.assertIn("token_gate_unsatisfiable_paged_at", current["observed_values"])
        self.assertIn("queued_age_producer_paged_at", current["observed_values"])

        with mock.patch.dict("os.environ", {
            "AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS": "100",
        }):
            second = self._dispatch("worker-b", "coexist-age", ttl=604699)
        with self.store.connection() as conn:
            observed = {
                "queued_age_producer_paged_at": datetime.now(timezone.utc).isoformat(),
                "queued_age_producer_page_message_id": "existing",
            }
            conn.execute(
                "update dispatch_ledger set observed_values_json=? where dispatch_id=?",
                (json.dumps(observed), second["dispatch_id"]),
            )
        with mock.patch.dict(
            "os.environ",
            {
                "AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS": "200",
            },
        ):
            actions = self.store.start_queued_dispatches(
                lambda _runtime: Adapter(), limit=4, ttl_seconds=604699
            )
        self.assertEqual(sum(a["status"] == "token_gate_unsatisfiable" for a in actions), 2)
        current = self.store._dispatch_by_idempotency_key_fresh("arch", "coexist-age")
        self.assertIn("token_gate_unsatisfiable_paged_at", current["observed_values"])

    def test_promotion_revalidates_satisfiability_under_divergent_environment(self):
        with mock.patch.dict("os.environ", {
            "AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS": "100",
        }):
            row = self._dispatch("worker-a", "divergent", ttl=604699)
        adapter = Adapter()
        with mock.patch.dict(
            "os.environ",
            {
                "AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS": "200",
            },
        ):
            actions = self.store.start_queued_dispatches(
                lambda _runtime: adapter, limit=4, ttl_seconds=604699
            )
        # Divergent margin configuration fails closed at promotion.
        self.assertEqual(actions[0]["status"], "token_gate_unsatisfiable")
        self.assertEqual(adapter.calls, [])
        self.assertEqual(self.store._dispatch_by_idempotency_key_fresh(
            "arch", "divergent"
        )["status"], "queued")
        self.assertEqual(len(self.store.list_inbox("arch")), 1)

    def test_active_refresh_lease_holds_then_release_promotes(self):
        queued = self._dispatch("worker-a", "lease")
        with self.store.connection() as conn:
            key = conn.execute("select auth_lineage_key from dispatch_ledger").fetchone()[0]
            conn.execute("insert into codex_refresh_claims(lineage_key,holder,claimed_at) values(?,?,?)",
                         (key, "holder", datetime.now(timezone.utc).isoformat()))
        # A second admission observes the active lease.
        held = self._dispatch("worker-b", "held", Adapter())
        self.assertEqual(held["lineage_gate_status"], "refresh_in_progress")
        with self.store.connection() as conn:
            conn.execute("update codex_refresh_claims set holder=null,claimed_at=null")
        adapter = Adapter()
        actions = self.store.start_queued_dispatches(lambda _runtime: adapter, limit=4, ttl_seconds=60)
        self.assertEqual(sum(a["status"] == "in_flight" for a in actions), 2)

    def test_post_release_burst_is_bounded_by_cap(self):
        for index in range(6):
            self._dispatch("worker-a" if index % 2 == 0 else "worker-b", f"q{index}")
        adapter = Adapter()
        actions = self.store.start_queued_dispatches(lambda _runtime: adapter, limit=16, ttl_seconds=60)
        self.assertEqual(sum(a["status"] == "in_flight" for a in actions), 4)
        self.assertEqual(sum(r["status"] == "queued" for r in self.store.list_dispatches()), 2)

    def test_legacy_claim_column_is_never_written_by_lifecycle(self):
        adapter = Adapter()
        row = self._dispatch("worker-a", "legacy", adapter)
        with self.store.connection() as conn:
            conn.execute("update dispatch_ledger set auth_lineage_claimed_at='legacy' where dispatch_id=?",
                         (row["dispatch_id"],))
        reply = self.store.send_message("worker-a", ["arch"], "done", "done", [],
                                        parent_message_id=row["message_id"])
        self.store.close_dispatch("worker-a", message_id=row["message_id"], reply_message_id=reply["id"],
                                  result="satisfied", summary="done", delta=False)
        with self.store.connection() as conn:
            value = conn.execute("select auth_lineage_claimed_at from dispatch_ledger where dispatch_id=?",
                                 (row["dispatch_id"],)).fetchone()[0]
        self.assertEqual(value, "legacy")


if __name__ == "__main__":
    unittest.main()
