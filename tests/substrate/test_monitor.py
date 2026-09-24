from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import io
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.monitor import kick_stale_codex_lineages, run_monitor_loop
from agent_comms.store import Store

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"


class StubAdapter:
    def __init__(self) -> None:
        self.halted: list[str] = []

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        return DispatchStart(
            spawn_handle=f"stub:{context.recipient['id']}",
            observed_values={"adapter": "stub"},
        )

    def halt(self, spawn_handle: str, observed_values=None) -> None:
        self.halted.append(spawn_handle)


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


def force_overdue(store: Store, dispatch_id: str) -> None:
    with store.connection() as conn:
        conn.execute(
            "update dispatch_ledger set expected_close_by = ? where dispatch_id = ?",
            ("2000-01-01T00:00:00+00:00", dispatch_id),
        )


class MonitorCliTest(unittest.TestCase):
    def test_token_stale_starts_one_refresh_pass_and_records_failure(self) -> None:
        calls = []
        actions = [
            {"status": "token_stale", "lineage_key": "b"},
            {"status": "token_stale", "lineage_key": "b"},
            {"status": "token_stale", "lineage_key": "a"},
        ]
        result = kick_stale_codex_lineages(
            actions,
            kick_runner=lambda argv: calls.append(argv) or SimpleNamespace(returncode=113, stderr="launch failed"),
        )
        self.assertEqual([row["lineage_key"] for row in result], ["a", "b"])
        self.assertTrue(all(row["kick_rc"] == 113 for row in result))
        self.assertTrue(all(row["kick_stderr"] == "launch failed" for row in result))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:3], ["-m", "agent_comms.cli"])
        self.assertEqual(calls[0][-1], "refresh-codex-auth")

    def test_refresh_pass_uses_the_monitors_explicit_db(self) -> None:
        calls = []
        kick_stale_codex_lineages(
            [{"status": "token_stale", "lineage_key": "a"}],
            db_path=Path("/srv/other.sqlite"),
            kick_runner=lambda argv: calls.append(argv) or SimpleNamespace(returncode=0, stderr=""),
        )
        self.assertEqual(calls[0][-3:], ["--db", "/srv/other.sqlite", "refresh-codex-auth"])

    def test_default_runner_is_single_flight_and_detached(self) -> None:
        from agent_comms import monitor

        running = mock.Mock()
        running.poll.return_value = None
        with mock.patch.object(monitor, "_refresh_process", None), \
                mock.patch.object(monitor.subprocess, "Popen", return_value=running) as popen:
            first = monitor._default_kick_runner(["refresh"])
            second = monitor._default_kick_runner(["refresh"])
            self.assertEqual((first.returncode, second.returncode), (0, 0))
            self.assertEqual(popen.call_count, 1)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertIn("already running", second.stderr)
            running.poll.return_value = 0
            monitor._default_kick_runner(["refresh"])
            self.assertEqual(popen.call_count, 2)

    def test_kick_exception_is_an_action_and_never_raises(self) -> None:
        result = kick_stale_codex_lineages(
            [{"status": "token_stale", "lineage_key": "a"}],
            kick_runner=mock.Mock(side_effect=OSError("cannot start")),
        )
        self.assertEqual(result[0]["kick_rc"], None)
        self.assertIn("OSError", result[0]["kick_error"])

    def test_inline_monitor_path_without_token_stale_never_kicks(self) -> None:
        runner = mock.Mock()
        kick_stale_codex_lineages([{"status": "closed"}], kick_runner=runner)
        runner.assert_not_called()

    def test_monitor_once_reports_no_actions_on_empty_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_comms.monitor",
                    "--db",
                    str(root / "agent-comms.sqlite"),
                    "--once",
                ],
                text=True,
                capture_output=True,
                check=True,
            )

            self.assertEqual(json.loads(result.stdout), {"actions": []})

    def test_monitor_once_runs_one_pass_without_sleeping(self) -> None:
        output = io.StringIO()
        calls: list[tuple[Path, str | None]] = []

        def reconcile(db_path: Path, human_actor_id: str | None) -> list[dict]:
            calls.append((db_path, human_actor_id))
            return [{"dispatch_id": "dispatch-one", "status": "closed"}]

        def sleep(_seconds: float) -> None:
            raise AssertionError("one-shot monitor should not sleep")

        result = run_monitor_loop(
            Path("agent-comms.sqlite"),
            human_actor_id=HUMAN_ID,
            once=True,
            output=output,
            sleep=sleep,
            reconcile=reconcile,
            enrich=lambda _path: {},
        )

        self.assertEqual(result, 0)
        self.assertEqual(calls, [(Path("agent-comms.sqlite"), HUMAN_ID)])
        self.assertEqual(
            json.loads(output.getvalue()),
            {"actions": [{"dispatch_id": "dispatch-one", "status": "closed"}]},
        )

    def test_monitor_loop_can_be_bounded_for_tests(self) -> None:
        output = io.StringIO()
        sleeps: list[float] = []
        passes: list[int] = []

        def reconcile(_db_path: Path, _human_actor_id: str | None) -> list[dict]:
            passes.append(len(passes) + 1)
            return []

        run_monitor_loop(
            Path("agent-comms.sqlite"),
            interval=0.25,
            max_passes=3,
            output=output,
            sleep=sleeps.append,
            reconcile=reconcile,
            enrich=lambda _path: {},
        )

        self.assertEqual(passes, [1, 2, 3])
        self.assertEqual(sleeps, [0.25, 0.25])
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], [{"actions": []}] * 3)

    def test_overdue_in_flight_dispatch_reconciles_closed_when_trigger_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent("alpha-architect", "alpha-worker", "monitor-closed", "Work", "Body.", [])
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
            adapter = StubAdapter()
            started = store.start_queued_dispatches(lambda _runtime: adapter)
            force_overdue(store, started[0]["dispatch_id"])
            store.send_message(
                "alpha-worker",
                ["alpha-architect"],
                "Re: monitor-closed",
                "reply-body",
                [],
                parent_message_id=started[0]["message_id"],
            )
            store.close_message("alpha-worker", started[0]["message_id"], "done")
            closed = store._dispatch_by_idempotency_key_fresh("alpha-architect", "monitor-closed")
            self.assertEqual(closed["status"], "closed")

            actions = store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            self.assertEqual(actions, [])
            reconciled = store._dispatch_by_idempotency_key_fresh("alpha-architect", "monitor-closed")
            self.assertEqual(reconciled["status"], "closed")
            self.assertEqual(adapter.halted, [])

    def test_overdue_in_flight_dispatch_reconciles_dlq_and_pages_producer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            store.dispatch_agent("alpha-architect", "alpha-worker", "monitor-dlq", "Work", "Body.", [])
            adapter = StubAdapter()
            started = store.start_queued_dispatches(lambda _runtime: adapter)
            force_overdue(store, started[0]["dispatch_id"])

            actions = store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            self.assertEqual(
                [action["status"] for action in actions],
                ["dlq", "producer_paged"],
            )
            self.assertEqual(adapter.halted, [started[0]["spawn_handle"]])
            reconciled = store._dispatch_by_idempotency_key_fresh("alpha-architect", "monitor-dlq")
            self.assertEqual(reconciled["status"], "dlq")
            page = store.list_inbox("alpha-architect")[0]
            self.assertEqual(page["from"], HUMAN_ID)
            self.assertEqual(page["parent_message_id"], started[0]["message_id"])


if __name__ == "__main__":
    unittest.main()
