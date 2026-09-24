from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import io
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from agent_comms.adapters import DispatchContext, DispatchStart
import agent_comms.monitor as monitor
from agent_comms.store import Store

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"
REPO_ROOT = Path(__file__).resolve().parents[2]


class StubAdapter:
    def dispatch(self, context: DispatchContext) -> DispatchStart:
        return DispatchStart(spawn_handle=f"stub:{context.recipient['id']}", observed_values={"adapter": "stub"})

    def halt(self, _spawn_handle: str, observed_values=None) -> None:
        return None


def seed_dispatch_actors(store: Store, root: Path) -> None:
    store.register_actor(HUMAN_ID, "human", "alice")
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
    store.register_agent_actor(
        "alpha-worker",
        "alpha",
        "worker",
        str(root / "alpha-worker"),
        [],
        runtime="stub",
        spawn={"command": "stub"},
        owner="alpha-architect",
    )


def make_dlq_dispatch(store: Store, idem: str = "idem-dlq") -> dict:
    store.dispatch_agent("alpha-architect", "alpha-worker", idem, "Work", "Body.", [])
    started = store.start_queued_dispatches(lambda _runtime: StubAdapter())[0]
    with store.connection() as conn:
        conn.execute(
            """
            update dispatch_ledger
            set status = 'dlq',
                failure_reason = 'timeout',
                observed_values_json = '{}'
            where dispatch_id = ?
            """,
            (started["dispatch_id"],),
        )
    return store._dispatch_by_idempotency_key_fresh("alpha-architect", idem)


class MonitorContinuousTest(unittest.TestCase):
    def test_atomic_page_claim_allows_one_page_for_same_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = make_dlq_dispatch(store)

            first = store._dispatch._page_producer_for_dispatch(dispatch, HUMAN_ID)
            second = store._dispatch._page_producer_for_dispatch(dispatch, HUMAN_ID)

            self.assertEqual(first["status"], "producer_paged")
            self.assertIsNone(second)
            producer_inbox = store.list_inbox("alpha-architect", unread_only=False)
            self.assertEqual(len(producer_inbox), 1)
            page = store.read_message("alpha-architect", producer_inbox[0]["id"])
            self.assertEqual(page["from"], HUMAN_ID)
            self.assertEqual(page["parent_message_id"], dispatch["message_id"])

    def test_observed_key_written_during_page_send_survives_record_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = make_dlq_dispatch(store)
            original_send = store._dispatch._mailbox.send_message

            def send_and_write_concurrent_key(*args, **kwargs):
                with store.connection() as conn:
                    conn.execute(
                        """
                        update dispatch_ledger
                        set observed_values_json = json_set(observed_values_json, '$.concurrent_key', 'survived')
                        where dispatch_id = ?
                        """,
                        (dispatch["dispatch_id"],),
                    )
                return original_send(*args, **kwargs)

            with mock.patch.object(store._dispatch._mailbox, "send_message", side_effect=send_and_write_concurrent_key):
                page = store._dispatch._page_producer_for_dispatch(dispatch, HUMAN_ID)

            self.assertEqual(page["status"], "producer_paged")
            observed = store._dispatch_by_idempotency_key_fresh("alpha-architect", "idem-dlq")["observed_values"]
            self.assertEqual(observed["concurrent_key"], "survived")
            self.assertIn("producer_page_message_id", observed)

    def test_abandoned_claim_repages_but_fresh_claim_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            abandoned = make_dlq_dispatch(store, "abandoned")
            fresh = make_dlq_dispatch(store, "fresh")
            with store.connection() as conn:
                conn.execute(
                    """
                    update dispatch_ledger
                    set observed_values_json = json_set(observed_values_json, '$.producer_page_claimed_at', ?)
                    where dispatch_id = ?
                    """,
                    ("2000-01-01T00:00:00+00:00", abandoned["dispatch_id"]),
                )
                conn.execute(
                    """
                    update dispatch_ledger
                    set observed_values_json = json_set(observed_values_json, '$.producer_page_claimed_at', ?)
                    where dispatch_id = ?
                    """,
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), fresh["dispatch_id"]),
                )

            self.assertEqual(store._dispatch._page_producer_for_dispatch(abandoned, HUMAN_ID)["status"], "producer_paged")
            self.assertIsNone(store._dispatch._page_producer_for_dispatch(fresh, HUMAN_ID))

    def test_legacy_human_page_key_is_terminal_for_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = make_dlq_dispatch(store)
            with store.connection() as conn:
                conn.execute(
                    """
                    update dispatch_ledger
                    set observed_values_json = json_set(observed_values_json, '$.human_paged_at', ?)
                    where dispatch_id = ?
                    """,
                    ("2026-07-03T00:00:00+00:00", dispatch["dispatch_id"]),
                )

            actions = store.reconcile_dispatches(lambda _runtime: StubAdapter(), human_actor_id=HUMAN_ID)

            self.assertNotIn("producer_paged", [action["status"] for action in actions])
            self.assertEqual(store.list_inbox("alpha-architect", unread_only=False), [])

    def test_producer_page_does_not_trigger_reply_actor_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = make_dlq_dispatch(store)

            store.reconcile_dispatches(lambda _runtime: StubAdapter(), human_actor_id=HUMAN_ID)
            with store.connection() as conn:
                conn.execute("update dispatch_ledger set status = 'in_flight' where dispatch_id = ?", (dispatch["dispatch_id"],))
            actions = store.reconcile_dispatches(lambda _runtime: StubAdapter(), human_actor_id=HUMAN_ID)

            self.assertNotIn("reply_actor_mismatch", [action["status"] for action in actions])

    def test_producer_page_does_not_satisfy_worker_close_reply_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = make_dlq_dispatch(store)
            with store.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set policy_version = 'v1' where dispatch_id = ?",
                    (dispatch["dispatch_id"],),
                )
                self.assertEqual(
                    conn.execute(
                        "select policy_version from dispatch_ledger where dispatch_id = ?",
                        (dispatch["dispatch_id"],),
                    ).fetchone()["policy_version"],
                    "v1",
                )

            store.reconcile_dispatches(lambda _runtime: StubAdapter(), human_actor_id=HUMAN_ID)

            with self.assertRaisesRegex(Exception, "without first replying"):
                store.close_message("alpha-worker", dispatch["message_id"], "done")

    def test_wait_for_reply_after_trigger_returns_producer_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = make_dlq_dispatch(store)

            store.reconcile_dispatches(lambda _runtime: StubAdapter(), human_actor_id=HUMAN_ID)
            result = store.wait_for_reply(
                "alpha-architect",
                after_message_id=dispatch["message_id"],
                timeout_seconds=0.01,
                poll_interval_seconds=0.01,
                full=True,
            )

            self.assertFalse(result["timed_out"])
            self.assertEqual(len(result["messages"]), 1)
            self.assertEqual(result["messages"][0]["from"], HUMAN_ID)

    def test_heartbeat_upsert_and_check_heartbeat_pages_with_throttle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            store = Store(db_path)
            seed_dispatch_actors(store, root)

            self.assertEqual(monitor.check_heartbeat(db_path, HUMAN_ID), 1)
            self.assertEqual(len(store.list_inbox(HUMAN_ID, unread_only=False)), 1)
            self.assertEqual(monitor.check_heartbeat(db_path, HUMAN_ID), 1)
            self.assertEqual(len(store.list_inbox(HUMAN_ID, unread_only=False)), 1)

            heartbeat = store.upsert_monitor_heartbeat(interval_seconds=15, monitor_version="test")
            self.assertEqual(heartbeat["interval_seconds"], 15)
            self.assertEqual(monitor.check_heartbeat(db_path, HUMAN_ID), 0)

    def test_stdout_contract_unchanged_without_log_file_and_heartbeat_advances(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent-comms.sqlite"
            output = io.StringIO()
            heartbeat_calls: list[float] = []

            monitor.run_monitor_loop(
                db_path,
                interval=0.25,
                once=True,
                output=output,
                reconcile=lambda _path, _human: [],
                heartbeat=lambda _path, interval: heartbeat_calls.append(interval) or {},
            )

            self.assertEqual(output.getvalue(), '{"actions": []}\n')
            self.assertEqual(heartbeat_calls, [0.25])

    def test_log_file_rotation_caps_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "monitor.log"
            log_file.write_text("x" * 120)
            err_file = Path(temp_dir) / "monitor.err.log"
            err_file.write_text("e" * 120)
            with mock.patch.object(monitor, "LOG_MAX_BYTES", 80):
                logger = monitor.configure_file_logger(log_file)
                monitor.run_monitor_loop(
                    Path(temp_dir) / "agent-comms.sqlite",
                    once=True,
                    logger=logger,
                    reconcile=lambda _path, _human: [{"status": "changed"}],
                )

            self.assertLessEqual(log_file.stat().st_size, 80)
            self.assertEqual(err_file.read_text(), "")
            self.assertTrue((Path(temp_dir) / "monitor.log.1").exists())

    def test_agent_comms_monitor_honors_python_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            record = root / "argv.txt"
            stub = root / "python-stub"
            stub.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" > \"$AGENT_COMMS_RECORD\"\n"
                "exit 0\n"
            )
            stub.chmod(0o755)
            env = {"AGENT_COMMS_PYTHON": str(stub), "AGENT_COMMS_RECORD": str(record), "PATH": "/usr/bin:/bin"}
            subprocess.run(
                ["scripts/agent-comms-monitor", "--check-heartbeat"],
                cwd=REPO_ROOT,
                env=env,
                check=True,
            )

            self.assertEqual(record.read_text().splitlines(), ["-m", "agent_comms.monitor", "--check-heartbeat"])


if __name__ == "__main__":
    unittest.main()
