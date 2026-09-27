import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import cli, demo, doctor, mcp_clients, paths, runtime_pins, supervisor
from agent_comms.adapters import registry as adapter_registry
from agent_comms.onboarding import render_spawn
from agent_comms.doctor import LedgerRef
from agent_comms.schema import ValidationError
from agent_comms.store import Store

HUMAN = "01M36YTJV9XBW95S6ZWV47C4RG"


class _Scratch(unittest.TestCase):
    """A runtime root, registry and ledger under a temporary directory."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "my project"
        self.project.mkdir()
        self.db = self.root / "ledger dir" / "ledger.sqlite"
        self.db.parent.mkdir()
        # The registry stays at its default path (bootstrap treats any other
        # path as hand-edited and applies the protected-actor check), under
        # the isolation guard's scratch HOME; each test removes it.
        self.registry = paths.actors_config_path()
        self.addCleanup(self.registry.unlink, missing_ok=True)
        env = mock.patch.dict(
            os.environ,
            {
                mcp_clients.CLAUDE_CONFIG_ENV: str(self.root / "claude.json"),
                "CODEX_HOME": str(self.root / "codex"),
                runtime_pins.CLAUDE_VERSIONS_DIR_ENV: str(self.root / "custody"),
                "AGENT_COMMS_CODEX_CUSTODY_ROOT": str(self.root / "codex-homes"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.wait_for_workers)

    def cli(self, *argv) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.run(["--db", str(self.db), *argv])
        return rc, json.loads(out.getvalue()), err.getvalue()

    def setup_team(self, team: str) -> None:
        rc, result, _ = self.cli(
            "setup", "--project-root", str(self.project), "--team", team, "--runtimes", "fake",
            "--clients", "none", "--human-id", HUMAN, "--yes",
        )
        self.assertEqual(rc, 0, result)

    def rows(self, table: str) -> list[dict]:
        store = Store(self.db)
        with store.connection() as conn:
            return [dict(row) for row in conn.execute(f"select * from {table}")]

    def wait_for_workers(self) -> None:
        # A wrapper still registered would be reaped into a deleted ledger.
        registry = supervisor.reaper_registry()
        deadline = time.monotonic() + 30
        while registry.pending() and time.monotonic() < deadline:
            registry.reap_ready()
            time.sleep(0.1)


class DemoEndToEndTest(_Scratch):
    """The real fake worker, spawned through the supervisor."""

    def test_demo_right_after_setup_shows_dispatch_reply_and_final_status(self) -> None:
        self.setup_team("team-a")
        rc, result, err = self.cli("demo")
        self.assertEqual(rc, 0, result)
        self.assertEqual(set(result), {"ok", "dispatch", "reply", "final", "worker_log", "worker_reaped"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["worker_reaped"])
        self.assertEqual(supervisor.reaper_registry().pending(), 0)
        (row,) = self.rows("dispatch_ledger")
        self.assertEqual(json.loads(row["observed_values_json"])["reaper_exit"]["source"], "background_reap")
        dispatch = result["dispatch"]
        self.assertEqual(
            {key: dispatch[key] for key in ("from", "to", "team", "subject", "body")},
            {"from": "team-a-architect", "to": "team-a-fake-worker", "team": "team-a",
             "subject": demo.DEMO_SUBJECT, "body": demo.DEMO_BODY},
        )
        self.assertTrue(dispatch["dispatch_id"] and dispatch["message_id"])
        self.assertEqual(result["reply"]["from"], "team-a-fake-worker")
        self.assertEqual(result["reply"]["body"], "fake-reply: PONG")
        self.assertEqual(result["reply"]["parent_message_id"], dispatch["message_id"])
        self.assertEqual((result["final"]["status"], result["final"]["result"]), ("closed", "satisfied"))
        self.assertEqual(result["worker_log"], str(paths.dispatch_log_dir()))
        lines = err.splitlines()
        self.assertEqual(len(lines), 3, err)
        self.assertTrue(lines[0].startswith(f"dispatched {dispatch['dispatch_id']}: team-a-architect -> team-a-fake-worker"))
        self.assertTrue(lines[1].startswith(f"worker replied {result['reply']['message_id']}"))
        self.assertEqual(lines[2], "dispatch closed: satisfied")

        # No monitor heartbeat, and doctor stays clean: the monitor check warns.
        self.assertEqual(self.rows("monitor_heartbeat"), [])
        rc, report, _ = self.cli("doctor")
        self.assertEqual(rc, 0, report)
        check = next(c for c in report["checks"] if c["id"] == "monitor")
        self.assertEqual(check["status"], doctor.WARN)
        self.assertIn("all to fake workers and finished", check["detail"])

    def test_demo_runs_again_with_a_new_dispatch(self) -> None:
        self.setup_team("team-a")
        first = self.cli("demo")
        second = self.cli("demo")
        self.assertEqual((first[0], second[0]), (0, 0))
        self.assertNotEqual(first[1]["dispatch"]["dispatch_id"], second[1]["dispatch"]["dispatch_id"])
        self.assertEqual(len(self.rows("dispatch_ledger")), 2)

    def test_demo_leaves_unrelated_queued_native_work_untouched(self) -> None:
        # DEMO-001 F1: an eligible queued claude dispatch of another team sits
        # beside the demo. The demo must not start, probe or settle it: no
        # native adapter is even constructed, and its row and messages are
        # byte-for-byte what they were.
        self.setup_team("demo-team")
        store = Store(self.db)
        store.register_agent_actor("other-architect", "other", "architect", str(self.project), [])
        store.register_agent_actor(
            "other-claude-worker", "other", "worker", str(self.project), [], runtime="claude",
            owner="other-architect", spawn=render_spawn("claude", "other-claude-worker"),
        )
        queued = store.dispatch_agent(
            "other-architect", "other-claude-worker", "existing-native-task",
            "Existing native work", "A task unrelated to the demo.", [],
        )
        self.assertEqual(queued["status"], "queued")

        def unrelated() -> tuple[list[dict], list[dict]]:
            with store.connection() as conn:
                row = [dict(r) for r in conn.execute("select * from dispatch_ledger where dispatch_id = ?", (queued["dispatch_id"],))]
                messages = [dict(r) for r in conn.execute(
                    "select * from message_recipients where to_agent in ('other-claude-worker', 'other-architect') order by message_id, to_agent"
                )]
            return row, messages

        before = unrelated()
        constructed: list[str] = []

        class NativeSentinel:
            def __init__(self) -> None:
                constructed.append("claude")
                raise AssertionError("the demo reached a native adapter")

        with mock.patch.dict(adapter_registry.ADAPTERS_BY_RUNTIME, {"claude": NativeSentinel, "codex": NativeSentinel}):
            rc, result, _ = self.cli("demo", "--team", "demo-team")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["dispatch"]["to"], "demo-team-fake-worker")
        self.assertEqual(constructed, [])
        self.assertEqual(unrelated(), before)

    def test_the_dispatch_adapter_factory_refuses_every_runtime_but_fake(self) -> None:
        for runtime in ("claude", "codex"):
            with self.assertRaises(ValidationError):
                demo._fake_only(runtime)
        self.assertEqual(type(demo._fake_only("fake")).__name__, "FakeAdapter")

    def test_team_picks_among_several_fake_workers(self) -> None:
        self.setup_team("team-a")
        self.setup_team("team-b")
        rc, result, _ = self.cli("demo", "--team", "team-b")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["dispatch"]["to"], "team-b-fake-worker")


class DemoRefusalTest(_Scratch):
    """Every refusal comes before any message or dispatch row is written."""

    def assert_nothing_dispatched(self) -> None:
        self.assertEqual(self.rows("dispatch_ledger"), [])
        self.assertEqual(self.rows("messages"), [])

    def test_no_fake_worker_is_refused_with_a_setup_fix(self) -> None:
        Store(self.db).init()
        rc, result, _ = self.cli("demo")
        self.assertEqual(rc, 2)
        self.assertIn("no fake worker is registered", result["error"])
        self.assertIn(
            f"agent-comms --db '{self.db}' setup --project-root <project-root> --team demo "
            "--runtimes fake --clients none --yes",
            result["error"],
        )
        self.assert_nothing_dispatched()

    def test_several_fake_workers_without_team_are_refused_one_fix_per_team(self) -> None:
        self.setup_team("team-a")
        self.setup_team("team-b")
        rc, result, _ = self.cli("demo")
        self.assertEqual(rc, 2)
        self.assertIn(f"agent-comms --db '{self.db}' demo --team team-a", result["error"])
        self.assertIn(f"agent-comms --db '{self.db}' demo --team team-b", result["error"])
        self.assert_nothing_dispatched()

    def test_team_without_a_fake_worker_is_refused(self) -> None:
        self.setup_team("team-a")
        rc, result, _ = self.cli("demo", "--team", "other")
        self.assertEqual(rc, 2)
        self.assertIn("team 'other' has no fake worker", result["error"])
        self.assert_nothing_dispatched()

    def test_timeout_must_be_a_positive_whole_number(self) -> None:
        for value in ("0", "-1", "1.5", "x"):
            with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
                cli.run(["--db", str(self.db), "demo", "--timeout", value])
            self.assertEqual(raised.exception.code, 2, value)


class ResolveTargetTest(unittest.TestCase):
    """Target choice and the rendered fixes, on actor rows alone."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime_root = Path(self.tmp.name)
        patch = mock.patch.object(paths, "runtime_root", return_value=self.runtime_root)
        patch.start()
        self.addCleanup(patch.stop)
        self.ledger = LedgerRef(Path("/ledgers/l.sqlite"), False)

    @staticmethod
    def agent(actor_id, team, role, runtime=None, project_root=None):
        return {"id": actor_id, "kind": "agent", "team": team, "role": role, "runtime": runtime,
                "project_root": project_root, "owner_actor_id": f"{team}-architect" if role == "worker" else None}

    def test_only_a_fake_worker_is_ever_a_target(self) -> None:
        actors = [
            self.agent("t-architect", "t", "architect", project_root=self.tmp.name),
            self.agent("t-codex-worker", "t", "worker", "codex"),
            self.agent("t-claude-worker", "t", "worker", "claude"),
        ]
        with self.assertRaises(ValidationError) as raised:
            demo.resolve_target(actors, None, self.ledger)
        self.assertIn("no fake worker", str(raised.exception))
        with self.assertRaises(ValidationError):
            demo.resolve_target(actors, "t", self.ledger)
        actors.append(self.agent("t-fake-worker", "t", "worker", "fake"))
        self.assertEqual(demo.resolve_target(actors, None, self.ledger)["id"], "t-fake-worker")

    def test_setup_fix_uses_the_architects_root_and_a_free_team_name(self) -> None:
        actors = [self.agent("demo-architect", "demo", "architect", project_root=self.tmp.name)]
        fix = demo.setup_fix(actors, self.ledger)
        self.assertEqual(
            fix,
            f"agent-comms setup --project-root {self.tmp.name} --team demo-2 --runtimes fake --clients none --yes",
        )
        # A team taken only in the registry file counts as taken too.
        paths.actors_config_path().write_text(json.dumps({"actors": {"x": {"team": "demo-2"}}}))
        self.assertIn("--team demo-3", demo.setup_fix(actors, self.ledger))
        # An explicit ledger is carried; a root that is gone falls back to the placeholder.
        gone = [self.agent("a-architect", "a", "architect", project_root=str(self.runtime_root / "gone"))]
        explicit = LedgerRef(Path("/ledgers/l.sqlite"), True)
        self.assertTrue(demo.setup_fix(gone, explicit).startswith(
            "agent-comms --db /ledgers/l.sqlite setup --project-root <project-root> --team demo "
        ))

    def test_team_with_two_fake_workers_is_refused(self) -> None:
        actors = [self.agent("t-fake-1", "t", "worker", "fake"), self.agent("t-fake-2", "t", "worker", "fake")]
        with self.assertRaises(ValidationError) as raised:
            demo.resolve_target(actors, "t", self.ledger)
        self.assertIn("2 fake workers (t-fake-1, t-fake-2)", str(raised.exception))


class DemoFailureTest(_Scratch):
    """A dispatch that does not end closed/satisfied exits 3 with fixes."""

    def run_with_row(self, row: dict, **kwargs) -> tuple[int, dict, str]:
        self.setup_team("team-a")
        with mock.patch.object(demo, "_row", return_value=row):
            return self.cli("demo", *kwargs.get("argv", ()))

    def test_timeout_reports_the_row_and_fixes(self) -> None:
        self.setup_team("team-a")
        ticks = iter([0.0, 0.0, 5.0])
        store = Store(self.db)
        with mock.patch.object(demo, "_row", return_value={"status": "in_flight", "result": None, "failure_reason": None}):
            with self.assertRaises(demo.DemoFailed) as raised:
                demo.run_demo(
                    store, db_path=self.db, db_explicit=True, timeout_seconds=2,
                    clock=lambda: next(ticks), sleep=lambda _s: None,
                    progress=io.StringIO(),
                )
        payload = raised.exception.payload
        self.assertFalse(payload["ok"])
        self.assertIn("still in_flight after 2s", payload["error"])
        # An unfinished row is the monitor's to start or settle; its command comes first.
        self.assertEqual(payload["fixes"][:3], [
            f"agent-comms-monitor --db '{self.db}' --human-actor-id {HUMAN}",
            f"agent-comms --db '{self.db}' dispatch-status",
            f"agent-comms --db '{self.db}' doctor",
        ])

    def test_non_satisfied_close_exits_3(self) -> None:
        rc, result, err = self.run_with_row({"status": "dlq", "result": None, "failure_reason": "boom"})
        self.assertEqual(rc, 3)
        self.assertFalse(result["ok"])
        self.assertIn("ended dlq/None", result["error"])
        self.assertFalse(any(fix.startswith("agent-comms-monitor") for fix in result["fixes"]))
        self.assertEqual(result["final"]["failure_reason"], "boom")
        self.assertIn("demo failed", err)


if __name__ == "__main__":
    unittest.main()
