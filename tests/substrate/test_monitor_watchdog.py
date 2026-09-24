from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_comms.monitor as monitor
from agent_comms.store import Store


HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"


def seeded_store(db_path: Path) -> Store:
    store = Store(db_path)
    store.register_actor(HUMAN_ID, "human", "alice")
    return store


class RaisingHeartbeat:
    def monitor_heartbeat(self):
        raise RuntimeError("heartbeat read failed")


class MonitorWatchdogTest(unittest.TestCase):
    def test_T1_fresh_heartbeat_has_no_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "db.sqlite"
            store = seeded_store(db)
            store.upsert_monitor_heartbeat(interval_seconds=15, monitor_version="test")
            with mock.patch.object(Path, "home", return_value=root):
                self.assertEqual(monitor.check_heartbeat(db, HUMAN_ID), 0)
            self.assertEqual(store.list_inbox(HUMAN_ID, unread_only=False), [])
            self.assertFalse((root / ".agent-comms/logs/monitor-watchdog.page.log").exists())

    def test_T2_stale_heartbeat_pages_through_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "db.sqlite"
            store = seeded_store(db)
            self.assertEqual(monitor.check_heartbeat(db, HUMAN_ID), 1)
            self.assertEqual(len(store.list_inbox(HUMAN_ID, unread_only=False)), 1)

    def test_T3_evaluation_error_is_in_database_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "db.sqlite"
            store = seeded_store(db)
            with mock.patch.object(monitor, "Store", side_effect=[RaisingHeartbeat(), store]):
                self.assertEqual(monitor.check_heartbeat(db, HUMAN_ID), 1)
            message = store.read_message(HUMAN_ID, store.list_inbox(HUMAN_ID, unread_only=False)[0]["id"])
            self.assertIn("RuntimeError: heartbeat read failed", message["body"])

    def test_T4_evaluation_and_page_failure_use_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "directory"
            db.mkdir()
            with mock.patch.object(Path, "home", return_value=root):
                self.assertEqual(monitor.check_heartbeat(db, HUMAN_ID), 2)
            record = json.loads((root / ".agent-comms/logs/monitor-watchdog.page.log").read_text())
            self.assertEqual(record["db_path"], str(db))
            self.assertTrue((root / ".agent-comms/logs/monitor-watchdog.page.marker").exists())

    def test_T5_fallback_is_throttled_for_an_hour(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(Path, "home", return_value=root):
                monitor._watchdog_fallback("db", None, RuntimeError("one"), None, HUMAN_ID)
                log = root / ".agent-comms/logs/monitor-watchdog.page.log"
                before = log.read_text()
                monitor._watchdog_fallback("db", None, RuntimeError("two"), str(root), HUMAN_ID)
            self.assertEqual(log.read_text(), before)
            self.assertFalse((root / ".agent-comms/monitor-watchdog/new_messages").exists())

    def test_T6_fallback_root_gets_atomic_synthetic_semaphore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            project = root / "project"
            with mock.patch.object(Path, "home", return_value=home):
                self.assertEqual(
                    monitor._watchdog_fallback("db", None, RuntimeError("page"), str(project), HUMAN_ID), 2
                )
            semaphore_dir = project / ".agent-comms/monitor-watchdog"
            payload = json.loads((semaphore_dir / "new_messages").read_text())
            self.assertEqual(payload["agent_id"], HUMAN_ID)
            self.assertEqual(semaphore_dir.name, "monitor-watchdog")
            self.assertEqual(payload["messages"][0]["message_id"], "monitor-watchdog-fallback")
            self.assertEqual(list(semaphore_dir.glob("*.tmp")), [])

    def test_T7_no_fallback_root_is_log_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(Path, "home", return_value=root):
                self.assertEqual(monitor._watchdog_fallback("db", None, OSError("down"), None, HUMAN_ID), 2)
            self.assertTrue((root / ".agent-comms/logs/monitor-watchdog.page.log").exists())

    def test_T8_absent_heartbeat_pages(self) -> None:
        self.test_T2_stale_heartbeat_pages_through_database()

    def test_T9a_malformed_heartbeat_pages_as_stale(self) -> None:
        heartbeat = {"last_pass_at": "not-iso"}
        self.assertFalse(monitor.heartbeat_is_fresh(heartbeat))

    def test_T9b_naive_heartbeat_comparison_is_paged_as_evaluation_error(self) -> None:
        eval_store = mock.Mock()
        eval_store.monitor_heartbeat.return_value = {"last_pass_at": "2026-07-10T00:00:00"}
        page_store = mock.Mock()
        page_store.claim_monitor_stale_page.return_value = "now"
        with mock.patch.object(monitor, "Store", side_effect=[eval_store, page_store]):
            self.assertEqual(monitor.check_heartbeat(Path("db"), HUMAN_ID), 1)
        self.assertIn("TypeError", page_store.send_message.call_args.args[3])

    def test_T10_throttled_database_claim_still_exits_one(self) -> None:
        evaluation = mock.Mock()
        evaluation.monitor_heartbeat.return_value = None
        page = mock.Mock()
        page.claim_monitor_stale_page.return_value = None
        with mock.patch.object(monitor, "Store", side_effect=[evaluation, page]):
            self.assertEqual(monitor.check_heartbeat(Path("db"), HUMAN_ID), 1)
        page.send_message.assert_not_called()

    def test_T12_check_main_never_configures_logger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "db"
            db.mkdir()
            with mock.patch.object(Path, "home", return_value=root), mock.patch.object(
                monitor, "configure_file_logger"
            ) as logger:
                self.assertEqual(monitor.main(["--db", str(db), "--human-actor-id", HUMAN_ID, "--check-heartbeat"]), 2)
            logger.assert_not_called()

    def test_T13_keyboard_interrupt_returns_zero(self) -> None:
        with mock.patch.object(monitor, "check_heartbeat", side_effect=KeyboardInterrupt):
            self.assertEqual(monitor.main(["--db", "db", "--human-actor-id", HUMAN_ID, "--check-heartbeat"]), 0)

    def test_T14_fallback_operations_are_best_effort(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            failures = [
                mock.patch.object(Path, "mkdir", side_effect=OSError("mkdir")),
                mock.patch.object(Path, "open", side_effect=OSError("open")),
                mock.patch.object(Path, "touch", side_effect=OSError("touch")),
                mock.patch.object(Path, "replace", side_effect=OSError("rename")),
                mock.patch.object(Path, "write_text", side_effect=OSError("write")),
            ]
            for failure in failures:
                with self.subTest(failure=failure), mock.patch.object(Path, "home", return_value=root), failure:
                    self.assertEqual(
                        monitor._watchdog_fallback("db", None, RuntimeError("page"), str(root), HUMAN_ID), 2
                    )
                marker = root / ".agent-comms/logs/monitor-watchdog.page.marker"
                marker.unlink(missing_ok=True)

    def test_T14c_marker_stat_failure_still_appends_page_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / ".agent-comms/logs/monitor-watchdog.page.marker"
            marker.parent.mkdir(parents=True)
            marker.touch()
            original_stat = Path.stat

            def fail_marker_stat(path: Path, *args, **kwargs):
                if path == marker:
                    raise OSError("stat")
                return original_stat(path, *args, **kwargs)

            with mock.patch.object(Path, "home", return_value=root), mock.patch.object(
                Path, "stat", autospec=True, side_effect=fail_marker_stat
            ):
                self.assertEqual(monitor._watchdog_fallback("db", None, RuntimeError("page"), None, HUMAN_ID), 2)
            page_log = root / ".agent-comms/logs/monitor-watchdog.page.log"
            self.assertEqual(len(page_log.read_text().splitlines()), 1)

    def test_T14_error_serialization_and_root_resolution_are_best_effort(self) -> None:
        class BadError(Exception):
            def __str__(self):
                raise RuntimeError("cannot stringify")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(Path, "home", return_value=root):
                self.assertEqual(monitor._watchdog_fallback("db", BadError(), BadError(), "~no_such_user", HUMAN_ID), 2)
            record = json.loads((root / ".agent-comms/logs/monitor-watchdog.page.log").read_text())
            self.assertEqual(record["page_error"], "<exception summary unavailable>")

    def test_T15_db_path_resolution_failure_uses_unresolved_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(Path, "home", return_value=root), mock.patch.object(
                monitor.paths, "db_path", side_effect=RuntimeError("unresolved")
            ):
                self.assertEqual(monitor.main(["--human-actor-id", HUMAN_ID, "--check-heartbeat"]), 2)
            record = json.loads((root / ".agent-comms/logs/monitor-watchdog.page.log").read_text())
            self.assertEqual(record["db_path"], "<unresolved>")

    def test_T16_check_heartbeat_rejects_log_file_but_loop_accepts_it(self) -> None:
        with mock.patch.object(monitor, "configure_file_logger") as logger, mock.patch.object(
            monitor, "Store"
        ) as store, self.assertRaises(SystemExit):
            monitor.main(["--human-actor-id", HUMAN_ID, "--check-heartbeat", "--log-file", "x"])
        logger.assert_not_called()
        store.assert_not_called()
        with mock.patch.object(monitor, "configure_file_logger", return_value=mock.Mock()), mock.patch.object(
            monitor, "run_monitor_loop", return_value=0
        ):
            self.assertEqual(monitor.main(["--once", "--log-file", "x"]), 0)


if __name__ == "__main__":
    unittest.main()
