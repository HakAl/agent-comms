from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_comms.hooks import session_start
from agent_comms.store import Store


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "hooks" / "architect-session-start.sh"
ORIENTATION_SENTINEL = "coordinate its workers through agent-comms"


class SessionStartHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "local").mkdir()

    def run_module(
        self,
        *,
        environment: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        env = os.environ.copy()
        for key in (
            "AGENT_COMMS_DB",
            "AGENT_COMMS_LAUNCH_KIND",
            "AGENT_COMMS_ACTOR_ID",
            "AGENT_COMMS_INSTALL_ROOT",
        ):
            env.pop(key, None)
        env["CLAUDE_PROJECT_DIR"] = str(self.root)
        env["PYTHONPATH"] = str(ROOT)
        env.update(environment or {})
        proc = subprocess.run(
            [sys.executable, "-m", "agent_comms.hooks.session_start"],
            cwd=cwd or ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn(ORIENTATION_SENTINEL, context)
        return proc, context

    def write_handoff(self, text: str) -> None:
        (self.root / "local" / "ARCH-HANDOFF.md").write_text(text)

    def make_store(self, marker: str) -> Path:
        db = self.root / f"{marker}.sqlite"
        store = Store(db)
        store.register_agent("fixture-architect", "fixture", "architect", str(self.root), [])
        store.post_handoff(
            "fixture-architect",
            marker,
            [],
            created_by_actor_id="fixture-architect",
        )
        return db

    def board_environment(self, db: Path) -> dict[str, str]:
        return {
            "AGENT_COMMS_DB": str(db),
            "AGENT_COMMS_LAUNCH_KIND": "architect_interactive",
            "AGENT_COMMS_ACTOR_ID": "fixture-architect",
            "AGENT_COMMS_INSTALL_ROOT": str(ROOT),
        }

    def test_line_budget_boundary_uses_named_policy(self) -> None:
        self.assertEqual(session_start.HANDOFF_LINE_BUDGET, 300)
        self.write_handoff("line\n" * 300)
        _, context = self.run_module()
        self.assertNotIn("over budget", context)
        self.write_handoff("line\n" * 301)
        _, context = self.run_module()
        self.assertIn("301 lines", context)

    def test_current_wrap_headings_trip_budget(self) -> None:
        self.write_handoff("\n".join(f"## SESSION 2026-08-0{i} (g11{i}): wrap" for i in range(1, 4)))
        _, context = self.run_module()
        self.assertIn("3 session wraps", context)

    def test_wrap_boundary_and_dead_form(self) -> None:
        self.write_handoff("## SESSION 2026-08-01 (g110): one\n## SESSION 2026-08-02 (g111): two\n")
        _, context = self.run_module()
        self.assertNotIn("over budget", context)
        self.write_handoff("## >>> SESSION WRAP\n" * 4)
        _, context = self.run_module()
        self.assertNotIn("over budget", context)

    def test_board_override_gating_and_cwd_independence(self) -> None:
        first = self.make_store("FIRST_STORE_MARKER")
        second = self.make_store("SECOND_STORE_MARKER")
        env = self.board_environment(second)
        for cwd in (ROOT, self.root):
            with self.subTest(cwd=cwd):
                _, context = self.run_module(environment=env, cwd=cwd)
                self.assertIn("SECOND_STORE_MARKER", context)
                self.assertNotIn("FIRST_STORE_MARKER", context)

        _, context = self.run_module(environment={"AGENT_COMMS_DB": str(second)})
        self.assertNotIn("SECOND_STORE_MARKER", context)
        for missing in ("AGENT_COMMS_LAUNCH_KIND", "AGENT_COMMS_ACTOR_ID", "AGENT_COMMS_INSTALL_ROOT"):
            partial = env.copy()
            partial.pop(missing)
            with self.subTest(missing=missing):
                _, context = self.run_module(environment=partial)
                self.assertNotIn("SECOND_STORE_MARKER", context)
        wrong_kind = env | {"AGENT_COMMS_LAUNCH_KIND": "worker"}
        _, context = self.run_module(environment=wrong_kind)
        self.assertNotIn("SECOND_STORE_MARKER", context)
        self.assertNotEqual(first, second)

    def test_board_precedence_only_when_local_handoff_exists(self) -> None:
        db = self.make_store("BOARD_MARKER")
        _, context = self.run_module(environment=self.board_environment(db))
        self.assertNotIn("PRECEDENCE:", context)
        self.write_handoff("archive\n")
        _, context = self.run_module(environment=self.board_environment(db))
        self.assertLess(context.index("PRECEDENCE:"), context.index("BOARD_MARKER"))

    def test_main_reports_traceback_before_valid_fallback(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(session_start, "build_context", side_effect=RuntimeError("injected failure")),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            session_start.main()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], session_start.FALLBACK_CONTEXT)
        self.assertIn("Traceback", stderr.getvalue())
        self.assertIn("injected failure", stderr.getvalue())

    def test_board_failure_preserves_orientation_and_budget_warning(self) -> None:
        self.write_handoff("line\n" * 301)
        stderr = io.StringIO()
        with (
            mock.patch.object(session_start, "_board_text", side_effect=RuntimeError("board failure")),
            mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.root)}),
            contextlib.redirect_stderr(stderr),
        ):
            context = session_start.build_context()
        self.assertIn(ORIENTATION_SENTINEL, context)
        self.assertIn("301 lines", context)
        self.assertIn("Traceback", stderr.getvalue())
        self.assertIn("board failure", stderr.getvalue())

    def test_stub_invocation_and_executed_delegation(self) -> None:
        env = os.environ.copy()
        for key in (
            "AGENT_COMMS_DB",
            "AGENT_COMMS_LAUNCH_KIND",
            "AGENT_COMMS_ACTOR_ID",
            "AGENT_COMMS_INSTALL_ROOT",
        ):
            env.pop(key, None)
        env["CLAUDE_PROJECT_DIR"] = str(self.root)
        env["AGENT_COMMS_PYTHON"] = sys.executable
        proc = subprocess.run([str(HOOK)], env=env, text=True, capture_output=True)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")

        shadow = self.root / "shadow"
        (shadow / "agent_comms" / "hooks").mkdir(parents=True)
        (shadow / "agent_comms" / "__init__.py").write_text("")
        (shadow / "agent_comms" / "hooks" / "__init__.py").write_text("")
        (shadow / "agent_comms" / "hooks" / "session_start.py").write_text(
            "raise RuntimeError('shadow import failure')\n"
        )
        broken = subprocess.run([str(HOOK)], cwd=shadow, env=env, text=True, capture_output=True)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(broken.stdout)


if __name__ == "__main__":
    unittest.main()
