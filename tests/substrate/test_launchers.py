import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI_LAUNCHER = ROOT / "scripts" / "agent-comms"
MCP_LAUNCHER = ROOT / "scripts" / "agent-comms-mcp"
RECOVERY = "uv sync --extra mcp"

# Stands in for .venv/bin/python: logs every invocation, answers the
# `import mcp` probe and the startup_report probe per env knobs.
SHIM = """#!/bin/sh
printf '%s\\n' "$*" >> "$SHIM_LOG"
case "$*" in
  *"import mcp"*) exit "${SHIM_MCP_EXIT:-0}" ;;
  *startup_report*) echo "shim-startup-report" >&2; exit "${SHIM_REPORT_EXIT:-0}" ;;
esac
exit "${SHIM_EXIT:-0}"
"""


class LauncherTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checkout = Path(self.tmp.name) / "checkout"
        scripts = self.checkout / "scripts"
        scripts.mkdir(parents=True)
        for source in (CLI_LAUNCHER, MCP_LAUNCHER):
            target = scripts / source.name
            shutil.copy(source, target)
            target.chmod(0o755)
        self.shim_log = Path(self.tmp.name) / "shim.log"

    def add_venv(self):
        venv_bin = self.checkout / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        shim = venv_bin / "python"
        shim.write_text(SHIM)
        shim.chmod(0o755)

    def run_launcher(self, name, args=(), **env_overrides):
        env = os.environ.copy()
        env["SHIM_LOG"] = str(self.shim_log)
        env.update(env_overrides)
        return subprocess.run(
            [str(self.checkout / "scripts" / name), *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )

    def shim_calls(self):
        if not self.shim_log.exists():
            return []
        return self.shim_log.read_text().splitlines()


class CliLauncherTests(LauncherTestBase):
    def test_missing_venv_refuses_with_wrapper_recovery(self):
        proc = self.run_launcher("agent-comms", ["inbox"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn(".venv/bin/python", proc.stderr)
        self.assertIn(RECOVERY, proc.stderr)
        self.assertEqual(self.shim_calls(), [])

    def test_executes_venv_python_preserving_argv(self):
        self.add_venv()
        proc = self.run_launcher("agent-comms", ["inbox", "some-actor", "--all"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.shim_calls(),
            ["-m agent_comms.cli inbox some-actor --all"],
        )

    def test_exit_status_propagates(self):
        self.add_venv()
        proc = self.run_launcher("agent-comms", ["inbox"], SHIM_EXIT="5")
        self.assertEqual(proc.returncode, 5)


class McpLauncherTests(LauncherTestBase):
    def test_missing_venv_refuses_with_wrapper_recovery(self):
        proc = self.run_launcher("agent-comms-mcp", ["--actor-id", "a"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn(".venv/bin/python", proc.stderr)
        self.assertIn(RECOVERY, proc.stderr)
        self.assertEqual(self.shim_calls(), [])

    def test_missing_mcp_import_refuses_before_server(self):
        self.add_venv()
        proc = self.run_launcher(
            "agent-comms-mcp", ["--actor-id", "a"], SHIM_MCP_EXIT="1"
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cannot import mcp", proc.stderr)
        self.assertIn(RECOVERY, proc.stderr)
        for call in self.shim_calls():
            self.assertNotIn("agent_comms.mcp_server", call)

    def test_startup_report_then_server_argv_preserved(self):
        self.add_venv()
        proc = self.run_launcher(
            "agent-comms-mcp", ["--actor-id", "some-actor", "--db", "x.sqlite"]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("shim-startup-report", proc.stderr)
        self.assertNotIn("release_info=unknown", proc.stderr)
        self.assertEqual(
            self.shim_calls()[-1],
            "-m agent_comms.mcp_server --actor-id some-actor --db x.sqlite",
        )

    def test_startup_report_failure_falls_back_and_still_serves(self):
        self.add_venv()
        proc = self.run_launcher(
            "agent-comms-mcp", ["--actor-id", "a"], SHIM_REPORT_EXIT="3"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("agent-comms startup: release_info=unknown", proc.stderr)
        self.assertIn(
            "-m agent_comms.mcp_server --actor-id a", self.shim_calls()[-1]
        )


class LauncherContentTests(unittest.TestCase):
    """Static guarantees: no uv at launch, no temp cache, venv execution."""

    def test_launchers_execute_venv_and_never_touch_uv(self):
        for path in (CLI_LAUNCHER, MCP_LAUNCHER):
            text = path.read_text()
            with self.subTest(launcher=path.name):
                self.assertIn(".venv/bin/python", text)
                self.assertIn(RECOVERY, text)
                self.assertNotIn("uv run", text)
                self.assertNotIn("/private/tmp/uv-cache", text)
                self.assertNotIn("UV_CACHE_DIR", text)


if __name__ == "__main__":
    unittest.main()
