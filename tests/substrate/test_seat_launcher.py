import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
# The console script is `agent_comms.seat:main`; the module form runs the same
# code on the same interpreter, whose sys.prefix is the install root it exports.
LAUNCHER = [sys.executable, "-m", "agent_comms.seat"]
INSTALL_ROOT = sys.prefix


class SeatLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.result = self.path / "result.json"
        self.probe = self.path / "probe.py"
        self.probe.write_text("import json,os,sys\njson.dump({'argv':sys.argv,'env':{k:os.environ.get(k) for k in ('AGENT_COMMS_ACTOR_ID','AGENT_COMMS_LAUNCH_KIND','AGENT_COMMS_INSTALL_ROOT')}},open(os.environ['PROBE_RESULT'],'w'))\n")
        self.config = self.path / "claude.json"

    def run_launcher(self, *args, cwd=None, config=None):
        env = os.environ.copy()
        env.update(PROBE_RESULT=str(self.result), AGENT_COMMS_CLAUDE_CONFIG=str(config or self.config), PYTHONPATH=str(ROOT))
        return subprocess.run([*LAUNCHER, *args], cwd=cwd or self.path, env=env, text=True, capture_output=True)

    def write_config(self, key, vectors):
        servers = {str(i): {"args": args} for i, args in enumerate(vectors)}
        self.config.write_text(json.dumps({"projects": {os.path.realpath(key): {"mcpServers": servers}}}))

    def test_l1_exec_env_argv_and_warn(self):
        proc = self.run_launcher("some-actor", "--", sys.executable, str(self.probe), "--flag=x", "-v")
        self.assertEqual(proc.returncode, 0); self.assertIn("warning", proc.stderr)
        got = json.loads(self.result.read_text())
        self.assertEqual(got["argv"], [str(self.probe), "--flag=x", "-v"])
        self.assertEqual(got["env"], {"AGENT_COMMS_ACTOR_ID":"some-actor", "AGENT_COMMS_LAUNCH_KIND":"architect_interactive", "AGENT_COMMS_INSTALL_ROOT":INSTALL_ROOT})

    def test_l2_optional_separator(self):
        proc = self.run_launcher("some-actor", sys.executable, str(self.probe), "x")
        self.assertEqual(proc.returncode, 0); self.assertEqual(json.loads(self.result.read_text())["argv"][-1], "x")

    def test_l3_bad_usage(self):
        for args in [(), ("",), ("some-actor",)]:
            with self.subTest(args=args):
                proc = self.run_launcher(*args); self.assertEqual(proc.returncode, 2); self.assertIn("usage:", proc.stderr)

    def test_l4_match(self):
        self.write_config(self.path, [["--actor-id", "some-actor"]])
        proc = self.run_launcher("some-actor", sys.executable, str(self.probe)); self.assertEqual(proc.returncode, 0); self.assertNotIn("warning", proc.stderr)

    def test_l5_mismatch(self):
        self.write_config(self.path, [["--actor-id", "other-actor"]])
        proc = self.run_launcher("some-actor", sys.executable, str(self.probe)); self.assertEqual(proc.returncode, 3); self.assertIn("some-actor", proc.stderr); self.assertIn("other-actor", proc.stderr); self.assertIn(str(self.config), proc.stderr)

    def test_l6_conflict(self):
        self.write_config(self.path, [["--actor-id", "a"], ["--actor-id", "b"]])
        self.assertEqual(self.run_launcher("a", sys.executable, str(self.probe)).returncode, 3)

    def test_l7_duplicate(self):
        self.write_config(self.path, [["--actor-id", "a", "--actor-id", "a"]])
        proc = self.run_launcher("a", sys.executable, str(self.probe)); self.assertEqual(proc.returncode, 3); self.assertIn("malformed=True", proc.stderr)

    def test_l8_symlink_realpath(self):
        real = self.path / "real"; real.mkdir(); link = self.path / "link"; link.symlink_to(real, target_is_directory=True)
        self.write_config(real, [["--actor-id", "other"]])
        self.assertEqual(self.run_launcher("wanted", sys.executable, str(self.probe), cwd=link).returncode, 3)

    def test_l9_unparseable(self):
        self.config.write_text("{")
        proc = self.run_launcher("a", sys.executable, str(self.probe)); self.assertEqual(proc.returncode, 0); self.assertIn("parse", proc.stderr)

    def test_l10_trailing_flag(self):
        self.write_config(self.path, [["x", "--actor-id"]])
        proc = self.run_launcher("a", sys.executable, str(self.probe)); self.assertEqual(proc.returncode, 3); self.assertIn("malformed=True", proc.stderr)

    def test_l11_runtime_not_found(self):
        missing = self.path / "nonexistent-runtime"
        self.write_config(self.path, [["--actor-id", "a"]])
        proc = self.run_launcher("a", str(missing))
        self.assertEqual(proc.returncode, 127)
        self.assertEqual(len(proc.stderr.splitlines()), 1)
        self.assertIn(str(missing), proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__": unittest.main()
