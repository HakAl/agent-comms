import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import importlib
import io
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import mcp_server

ROOT = Path(__file__).resolve().parents[2]
RECOVERY = "uv sync"
ENTRY_POINTS = {
    "agent-comms": "agent_comms.cli:main",
    "agent-comms-mcp": "agent_comms.mcp_server:main",
    "agent-comms-monitor": "agent_comms.monitor:main",
    "agent-comms-seat": "agent_comms.seat:main",
}


class EntryPointTests(unittest.TestCase):
    """The four commands are console scripts of the package, not shell files."""

    def test_pyproject_declares_the_four_console_scripts(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["scripts"], ENTRY_POINTS)

    def test_every_entry_point_names_an_importable_callable(self):
        for name, target in ENTRY_POINTS.items():
            module_name, attribute = target.split(":")
            with self.subTest(command=name):
                module = importlib.import_module(module_name)
                self.assertTrue(callable(getattr(module, attribute)))

    def test_console_scripts_are_installed_next_to_the_interpreter(self):
        # The environment running the suite is the package's install; `uv sync`
        # writes these, and paths.mcp_command() resolves through them.
        bin_dir = Path(sys.executable).parent
        for name in ENTRY_POINTS:
            with self.subTest(command=name):
                self.assertTrue((bin_dir / name).is_file(), f"{bin_dir / name} missing; run {RECOVERY}")

    def test_no_shell_launchers_remain_under_scripts(self):
        leftovers = sorted(path.name for path in (ROOT / "scripts").glob("agent-comms*"))
        self.assertEqual(leftovers, [])


class McpEntryPointTests(unittest.TestCase):
    """agent-comms-mcp preflights its environment, reports, then serves."""

    def run_main(self, argv, **patches):
        calls = []

        class FakeServer:
            def run(self):
                calls.append("run")

        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", ["agent-comms-mcp", *argv]), mock.patch.object(
            mcp_server, "create_server", return_value=FakeServer()
        ) as create, contextlib.redirect_stderr(stderr), contextlib.ExitStack() as stack:
            for target, value in patches.items():
                stack.enter_context(mock.patch(target, **value))
            code = None
            try:
                mcp_server.main()
            except SystemExit as exc:
                code = exc.code
        return code, stderr.getvalue(), create, calls

    def test_startup_report_then_server_argv_preserved(self):
        code, stderr, create, calls = self.run_main(["--actor-id", "some-actor", "--db", "x.sqlite"])
        self.assertIsNone(code)
        self.assertIn("agent-comms startup: version=", stderr)
        self.assertNotIn("release_info=unknown", stderr)
        create.assert_called_once_with(db_path="x.sqlite", actor_id="some-actor", db_explicit=True)
        self.assertEqual(calls, ["run"])

    def test_startup_report_failure_falls_back_and_still_serves(self):
        code, stderr, _create, calls = self.run_main(
            ["--actor-id", "a"],
            **{"agent_comms.release.startup_report": {"side_effect": RuntimeError("no git")}},
        )
        self.assertIsNone(code)
        self.assertIn(mcp_server.STARTUP_REPORT_FALLBACK, stderr)
        self.assertEqual(calls, ["run"])

    def test_unregistered_actor_is_one_line_not_a_traceback(self):
        from agent_comms.schema import ValidationError

        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", ["agent-comms-mcp", "--actor-id", "nobody"]), mock.patch.object(
            mcp_server, "create_server", side_effect=ValidationError("unknown actor: nobody")
        ), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                mcp_server.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("agent-comms-mcp: unknown actor: nobody", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_missing_actor_id_is_a_usage_error_before_any_report(self):
        code, stderr, create, calls = self.run_main([])
        self.assertEqual(code, 2)
        self.assertIn(mcp_server.MISSING_ACTOR_ID_MESSAGE, stderr)
        self.assertNotIn("agent-comms startup", stderr)
        create.assert_not_called()
        self.assertEqual(calls, [])

    def test_missing_mcp_import_refuses_before_server(self):
        # A `mcp` module that fails to import shadows the real package through
        # PYTHONPATH, which is what a broken environment looks like from inside.
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "mcp.py").write_text("raise ImportError('shim: mcp absent')\n")
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join([temp_dir, str(ROOT)])
            proc = subprocess.run(
                [sys.executable, "-m", "agent_comms.mcp_server", "--actor-id", "a"],
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("cannot import mcp", proc.stderr)
        self.assertIn(RECOVERY, proc.stderr)
        self.assertIn(sys.executable, proc.stderr)
        self.assertNotIn("agent-comms startup", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class MonitorEntryPointTests(unittest.TestCase):
    def test_module_entry_point_runs_on_its_own_interpreter(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        proc = subprocess.run(
            [sys.executable, "-m", "agent_comms.monitor", "--help"],
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("agent-comms-monitor", proc.stdout)


if __name__ == "__main__":
    unittest.main()
