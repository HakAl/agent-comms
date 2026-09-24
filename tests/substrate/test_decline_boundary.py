from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import tempfile
import unittest
from pathlib import Path

from agent_comms.adapters import DispatchStart
from agent_comms.cli import settlement_plan
from agent_comms.schema import ValidationError
from agent_comms.store import Store


DECLINED_ENTRY_POINTS = (
    ("require_agent", lambda s, a, owner, fx: s.require_agent(a)),
    ("require_launchable_actor", lambda s, a, owner, fx: s.require_launchable_actor(a)),
    ("post_status", lambda s, a, owner, fx: s.post_status(a, "probe", [])),
    ("send_message", lambda s, a, owner, fx: s.send_message(a, [owner], "probe", "probe", [])),
    ("list_inbox", lambda s, a, owner, fx: s.list_inbox(a)),
    ("whoami", lambda s, a, owner, fx: s.whoami(a)),
    ("read_handoff", lambda s, a, owner, fx: s.read_handoff(a)),
    ("post_handoff", lambda s, a, owner, fx: s.post_handoff(a, "probe", created_by_actor_id=owner)),
    ("transfer_worker", lambda s, a, owner, fx: s.transfer_worker(a, owner)),
    ("read_message", lambda s, a, owner, fx: s.read_message(a, fx["message_id"])),
    ("ack_message", lambda s, a, owner, fx: s.ack_message(a, fx["message_id"], "")),
    ("close_message", lambda s, a, owner, fx: s.close_message(a, fx["message_id"])),
    ("close_dispatch", lambda s, a, owner, fx: s.close_dispatch(
        a, message_id=fx["close_message_id"], result="satisfied",
        reply_message_id="unused", summary="probe",
    )),
    ("session_start_handoff", lambda s, a, owner, fx: s.session_start_handoff(a)),
    ("request_cancellation", lambda s, a, owner, fx: s.request_cancellation(
        fx["producer_dispatch_id"], requesting_actor_id=a, reason="probe", authority="producer",
    )),
    ("wait_for_reply", lambda s, a, owner, fx: s.wait_for_reply(a, timeout_seconds=0)),
    ("list_handoffs", lambda s, a, owner, fx: s.list_handoffs(a)),
    ("dispatch_agent", lambda s, a, owner, fx: s.dispatch_agent(
        a, fx["child"], "declined-probe", "probe", "probe", [],
    )),
    ("settle_dispatch_preview", lambda s, a, owner, fx: s.settle_dispatch_preview(
        fx["close_dispatch_id"], actor_id=a, reason="probe", secret=fx["secret"],
    )),
    ("settle_dispatch_execute", lambda s, a, owner, fx: s.settle_dispatch_execute(
        fx["close_dispatch_id"], actor_id=a, plan=fx["plan"], secret=fx["secret"],
        release_ack=True,
    )),
)

EXCLUDED = {
    "connect": "plumbing; takes no identity of any kind",
    "connection": "plumbing; takes no identity of any kind",
    "db_path": "plumbing; takes no identity of any kind",
    "init": "plumbing; takes no identity of any kind",
    "register_agent": "repair path; refusal would make a declined worker unrepairable",
    "register_actor": "repair path; refusal would make a declined worker unrepairable",
    "register_agent_actor": "repair path; refusal would make a declined worker unrepairable",
    "actor_protection": "subject-actor read, not an acting identity",
    "list_actors": "fleet-wide read with no acting identity",
    "list_agents": "fleet-wide read with no acting identity",
    "list_status": "fleet-wide read with no acting identity",
    "list_dispatches": "fleet-wide read with no acting identity",
    "list_unread": "fleet-wide read with no acting identity",
    "project_dispatch": "takes no acting identity",
    "start_queued_dispatches": "monitor drain with no acting identity",
    "retry_spawn": "infra drain with no acting identity",
    "reconcile_dispatches": "infra drain whose optional human id is an operator sender, not a worker caller",
    "worker_usage_candidates": "keyed by dispatch id; takes no acting identity",
    "update_actor_spawn": "operator CLI-only targeted actor metadata update; takes no acting identity",
    "write_worker_usage": "keyed by dispatch id; takes no acting identity",
    "upsert_monitor_heartbeat": "monitor infrastructure with no acting identity",
    "monitor_heartbeat": "monitor infrastructure with no acting identity",
    "claim_monitor_stale_page": "monitor infrastructure with no acting identity",
}


class RecordingAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def dispatch(self, context):
        self.calls += 1
        if self.fail:
            raise RuntimeError("adapter spawn failed")
        return DispatchStart(spawn_handle="fake", observed_values={"run_token": "probe"})

    def halt(self, spawn_handle, observed_values):
        return None


class DeclineBoundaryTests(unittest.TestCase):
    def _store(self, root: Path, shape: str) -> tuple[Store, str, str, dict]:
        store = Store(root / "ledger.sqlite")
        owner = "probe-architect"
        actor = f"declined-{shape}"
        store.register_agent_actor(owner, "probe", "architect", str(root / owner), [])
        store.register_agent_actor(actor, "probe", "architect", str(root / actor), [])
        child = f"child-{shape}"
        store.register_agent_actor(
            child, "probe", "worker", str(root / child), [], owner=actor,
            runtime="claude", spawn={"command": ["true"]},
        )
        message = store.send_message(owner, [actor], "probe", "probe", [])
        producer_dispatch = store.dispatch_agent(
            actor, child, f"producer-{shape}", "probe", "probe", [],
        )
        with store.connection() as conn:
            conn.execute(
                "update actors set role = 'worker', owner_actor_id = ? where id = ?",
                (owner, actor),
            )
            conn.execute("update agents set role = 'worker' where id = ?", (actor,))
            conn.commit()
        close_dispatch = store.dispatch_agent(
            owner, actor, f"close-{shape}", "probe", "probe", [],
        )
        with store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status = 'in_flight', auth_lineage_claimed_at = ? "
                "where dispatch_id = ?",
                ("2026-08-05T00:00:00+00:00", close_dispatch["dispatch_id"]),
            )
            conn.commit()
        secret = "decline-boundary-secret"
        plan = settlement_plan.build_plan(
            secret=secret, actor_id=actor, dispatch_id=close_dispatch["dispatch_id"],
            reason="probe", snapshot={}, issued_at="2026-08-05T00:00:00+00:00", nonce="probe",
        )
        with store.connection() as conn:
            conn.execute(
                "update actors set role = 'architect', owner_actor_id = null where id = ?",
                (actor,),
            )
            conn.execute("update agents set role = 'worker' where id = ?", (actor,))
            conn.commit()
            if shape == "absent":
                conn.execute("pragma foreign_keys = off")
                conn.execute("delete from actors where id = ?", (actor,))
                conn.commit()
        return store, actor, owner, {
            "message_id": message["id"],
            "close_message_id": close_dispatch["message_id"],
            "close_dispatch_id": close_dispatch["dispatch_id"],
            "producer_dispatch_id": producer_dispatch["dispatch_id"],
            "child": child,
            "secret": secret,
            "plan": plan,
        }

    @staticmethod
    def _database_snapshot(store: Store) -> tuple:
        def tagged(value):
            if value is None:
                return ("null", "")
            if isinstance(value, bytes):
                return ("blob", value.hex())
            return (type(value).__name__, repr(value))

        store.init()
        with store.connection() as conn:
            tables = sorted(
                row[0] for row in conn.execute(
                    "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                )
            )
            return tuple(
                (table, tuple(sorted((tuple(tagged(value) for value in row) for row in conn.execute(
                    f'select * from "{table}"'
                )))))
                for table in tables
            )

    def test_every_public_store_member_is_classified(self) -> None:
        public = {name for name in dir(Store) if not name.startswith("_")}
        enumerated = {name for name, _invoke in DECLINED_ENTRY_POINTS}
        excluded = set(EXCLUDED)
        overlap = enumerated & excluded
        unclassified = public - enumerated - excluded
        print(f"PUBLIC_MEMBERS:{len(public)}:{','.join(sorted(public))}")
        print(f"ENUMERATED_MEMBERS:{len(enumerated)}:{','.join(sorted(enumerated))}")
        print(f"EXCLUDED_MEMBERS:{len(excluded)}:{','.join(sorted(excluded))}")
        print(f"CLASSIFICATION_OVERLAP:{len(overlap)}:{','.join(sorted(overlap))}")
        print(f"UNCLASSIFIED_MEMBERS:{len(unclassified)}:{','.join(sorted(unclassified))}")
        self.assertTrue(enumerated)
        self.assertTrue(excluded)
        self.assertFalse(overlap)
        self.assertTrue(
            all(
                isinstance(reason, str) and reason.strip()
                for reason in EXCLUDED.values()
            )
        )
        self.assertEqual(len(public), 43)
        self.assertEqual(public, enumerated | excluded)

    def test_declined_entry_points_refuse_both_shapes(self) -> None:
        examined = refused = 0
        for shape in ("absent", "ownerless"):
            for name, invoke in DECLINED_ENTRY_POINTS:
                with self.subTest(shape=shape, entry_point=name), tempfile.TemporaryDirectory() as td:
                    store, actor, owner, fixtures = self._store(Path(td), shape)
                    before = self._database_snapshot(store)
                    examined += 1
                    with self.assertRaises(ValidationError) as raised:
                        invoke(store, actor, owner, fixtures)
                    refused += 1
                    self.assertIn(actor, str(raised.exception))
                    self.assertIn(actor, {row["id"] for row in store.list_agents()})
                    self.assertEqual(self._database_snapshot(store), before)
        expected = len(DECLINED_ENTRY_POINTS) * 2
        print(f"DECLINE_ENTRYPOINTS_EXAMINED:{examined}")
        print(f"DECLINE_ENTRYPOINTS_REFUSED:{refused}")
        self.assertGreater(expected, 0)
        self.assertEqual((examined, refused), (expected, expected))

    def test_legitimate_recipients_remain_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = Store(root / "ledger.sqlite")
            human = "01M36YTJV9XBW95S6ZWV47C4RG"
            owner = "compat-architect"
            worker = "compat-worker"
            store.register_actor(human, "human", "operator")
            store.register_agent_actor(owner, "probe", "architect", str(root / owner), [])
            store.register_agent_actor(
                worker, "probe", "worker", str(root / worker), [], owner=owner,
                runtime="claude", spawn={"command": ["true"]},
            )
            for recipient in (human, worker):
                message = store.send_message(owner, [recipient], "compat", "compat", [])
                read = store.read_message(recipient, message["id"])
                self.assertEqual(read["status"], "read")
        print("LEGITIMATE_RECIPIENT_COMPATIBILITY:human_and_owned_agent_passed")

    def _dispatch_store(self, root: Path):
        store = Store(root / "ledger.sqlite")
        store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "operator")
        store.register_agent_actor("probe-architect", "probe", "architect", str(root / "a"), [])
        store.register_agent_actor(
            "probe-worker", "probe", "worker", str(root / "w"), [],
            owner="probe-architect", runtime="claude", spawn={"command": ["true"]},
        )
        row = store.dispatch_agent("probe-architect", "probe-worker", "key", "subject", "body", [])
        return store, row

    @staticmethod
    def _decline(store: Store) -> None:
        with store.connection() as conn:
            conn.execute(
                "update actors set role = 'architect', owner_actor_id = null where id = 'probe-worker'"
            )
            conn.commit()

    def test_declined_drain_paths_fail_closed(self) -> None:
        examined = 0
        with tempfile.TemporaryDirectory() as td:
            store, row = self._dispatch_store(Path(td))
            self._decline(store)
            adapter = RecordingAdapter()
            actions = store.start_queued_dispatches(lambda _runtime: adapter)
            self.assertEqual(adapter.calls, 0)
            self.assertIn("lineage_resolution_failed", {a["status"] for a in actions})
            self.assertEqual(len(store.list_inbox("probe-architect")), 1)
            print("DRAIN_DECLINED_QUEUED:fail_closed_not_spawned")
            examined += 1

        with tempfile.TemporaryDirectory() as td:
            store, row = self._dispatch_store(Path(td))
            failing = RecordingAdapter(fail=True)
            store.start_queued_dispatches(lambda _runtime: failing)
            self._decline(store)
            retry = RecordingAdapter()
            result = store.retry_spawn(row["dispatch_id"], lambda _runtime: retry)
            self.assertEqual(result["status"], "spawn_failed_message_landed")
            self.assertEqual(retry.calls, 0)
            print("DRAIN_DECLINED_RETRY_SPAWN:fail_closed_not_spawned")
            examined += 1

        with tempfile.TemporaryDirectory() as td:
            store, row = self._dispatch_store(Path(td))
            self._decline(store)
            with store.connection() as conn:
                context = store._dispatch._dispatch_context_by_id(
                    conn, row["dispatch_id"], 60, recheck_codex_gates=False
                )
            self.assertIsNone(context)
            print("DRAIN_DECLINED_CONTEXT_BY_ID:none_not_raised")
            examined += 1

        print(f"DRAIN_PATHS_EXAMINED:{examined}")
        self.assertEqual(examined, 3)


if __name__ == "__main__":
    unittest.main()
