"""The ``tests`` package must move every test process into a scratch home.

Every test module imports ``tests``, so the guard in ``tests/isolation.py``
runs before any ``agent_comms`` module can compute a home-relative path. These
tests check the guard in-process (this process must already be isolated) and
through subprocesses that start from a fake "real" home, so that even a bare
``python -m unittest`` invocation never touches the operator's ``~/.agent-comms``.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from agent_comms import paths, supervisor
from tests import isolation

REPO_ROOT = Path(__file__).resolve().parents[2]

PROBE = textwrap.dedent(
    """
    import json, os
    from pathlib import Path
    import tests
    from agent_comms import paths, supervisor
    watched = ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
    print(json.dumps({
        "home": os.environ.get("HOME"),
        "path_home": str(Path.home()),
        "env": {
            name: value for name, value in os.environ.items()
            if name.startswith("AGENT_COMMS_") or name in watched
        },
        "paths": {
            "DEFAULT_DB": str(paths.DEFAULT_DB),
            "db_path": str(paths.db_path()),
            "runtime_root": str(paths.runtime_root()),
            "codex_custody_root": str(paths.codex_custody_root()),
            "_DEFAULT_CONTROL_ROOT": str(supervisor._DEFAULT_CONTROL_ROOT),
            "control_root": str(supervisor.control_root()),
        },
    }))
    """
)


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


class IsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="isolation-")
        self.addCleanup(self.temp.cleanup)
        self.fake_home = Path(self.temp.name) / "home"
        (self.fake_home / ".agent-comms").mkdir(parents=True)
        (self.fake_home / ".agent-comms" / "admin-token").write_text("secret\n")
        self.fake_snapshot = _tree(self.fake_home)

    def run_probe(self, code: str = PROBE, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.fake_home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for name in ("TMPDIR", "TEMP", "TMP"):
            if name in os.environ:
                env[name] = os.environ[name]
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def probe(self, **extra_env: str) -> dict:
        result = self.run_probe(**extra_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_fake_home_untouched(self) -> None:
        self.assertEqual(_tree(self.fake_home), self.fake_snapshot)

    def assert_outside_fake_home(self, report: dict) -> None:
        self.assertNotEqual(report["home"], str(self.fake_home))
        self.assertNotEqual(report["path_home"], str(self.fake_home))
        for name, value in report["paths"].items():
            self.assertFalse(
                Path(value).is_relative_to(self.fake_home),
                f"{name} still resolves inside the real home: {value}",
            )

    def test_this_process_is_isolated(self) -> None:
        if os.environ.get(isolation.LIVE_HOME_ENV) == "1":
            self.skipTest("operator opted into the live home")
        home = Path(os.environ["HOME"])
        self.assertTrue(home.is_dir())
        self.assertEqual(os.environ.get(isolation.SCRATCH_HOME_ENV), str(home))
        protected = isolation.protected_roots()
        self.assertTrue(protected)
        candidates = {
            "Path.home()": Path.home(),
            "paths.DEFAULT_DB": paths.DEFAULT_DB,
            "paths.runtime_root()": paths.runtime_root(),
            "paths.db_path()": paths.db_path(),
            "paths.codex_custody_root()": paths.codex_custody_root(),
            "supervisor._DEFAULT_CONTROL_ROOT": supervisor._DEFAULT_CONTROL_ROOT,
            "supervisor.control_root()": supervisor.control_root(),
        }
        for name, candidate in candidates.items():
            for root in protected:
                self.assertFalse(
                    isolation.is_within(candidate, root),
                    f"{name} resolves inside the protected root {root}: {candidate}",
                )
        self.assertNotIn(Path.home(), {root.parent for root in protected})

    def test_bare_invocation_is_isolated_and_strips_ambient_overrides(self) -> None:
        report = self.probe(
            AGENT_COMMS_DB=str(self.fake_home / ".agent-comms" / "agent-comms.sqlite"),
            AGENT_COMMS_CODEX_CUSTODY_ROOT=str(self.fake_home / ".agent-comms" / "codex-homes"),
            AGENT_COMMS_SUPERVISOR_ROOT=str(self.fake_home / ".agent-comms" / "run" / "s"),
            AGENT_COMMS_ADMIN_TOKEN="secret",
            CODEX_HOME=str(self.fake_home / ".codex"),
            CLAUDE_CONFIG_DIR=str(self.fake_home / ".claude"),
        )
        self.assert_outside_fake_home(report)
        self.assert_fake_home_untouched()
        env = report["env"]
        for name in (
            "AGENT_COMMS_DB",
            "AGENT_COMMS_CODEX_CUSTODY_ROOT",
            "AGENT_COMMS_ADMIN_TOKEN",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
        ):
            self.assertNotIn(name, env)
        self.assertEqual(env[isolation.SCRATCH_HOME_ENV], report["home"])
        self.assertEqual(report["paths"]["control_root"], env[isolation.SUPERVISOR_ROOT_ENV])
        self.assertNotEqual(report["paths"]["control_root"], report["paths"]["_DEFAULT_CONTROL_ROOT"])
        # The scratch directories are removed when the process exits.
        self.assertFalse(Path(report["home"]).exists())
        self.assertFalse(Path(env[isolation.SUPERVISOR_ROOT_ENV]).exists())

    def test_keep_scratch_flag_preserves_scratch_dirs(self) -> None:
        result = self.run_probe(**{isolation.KEEP_SCRATCH_ENV: "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        home = Path(report["home"])
        root = Path(report["env"][isolation.SUPERVISOR_ROOT_ENV])
        self.addCleanup(shutil.rmtree, home, True)
        self.addCleanup(shutil.rmtree, root, True)
        self.assertTrue(home.is_dir())
        self.assertTrue(root.is_dir())
        self.assertIn(str(home), result.stderr)
        self.assert_fake_home_untouched()

    def test_live_home_opt_out_keeps_the_real_home_for_cell_targets(self) -> None:
        db = str(self.fake_home / ".agent-comms" / "agent-comms.sqlite")
        code = 'import sys\nsys.argv = ["unittest", "discover", "-s", "tests/cells", "-t", "."]\n' + PROBE
        result = self.run_probe(code, **{isolation.LIVE_HOME_ENV: "1", "AGENT_COMMS_DB": db})
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["home"], str(self.fake_home))
        self.assertEqual(report["env"]["AGENT_COMMS_DB"], db)
        self.assertNotIn(isolation.SCRATCH_HOME_ENV, report["env"])
        self.assert_fake_home_untouched()

    def test_live_home_accepts_cell_discovery_with_a_pattern(self) -> None:
        for argv in (
            '["unittest", "discover", "-s", "tests/cells", "-p", "test_*.py", "-t", "."]',
            '["unittest", "tests.cells.test_cell_fake", "-k", "fake"]',
            '["unittest", "tests/cells/test_cell_fake.py"]',
        ):
            with self.subTest(argv=argv):
                code = f"import sys\nsys.argv = {argv}\n" + PROBE
                result = self.run_probe(code, **{isolation.LIVE_HOME_ENV: "1"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["home"], str(self.fake_home))

    def test_live_home_is_refused_for_substrate_targets(self) -> None:
        for argv in (
            '["unittest", "discover", "-s", "tests/substrate", "-t", "."]',
            '["unittest", "discover", "-s", "tests", "-t", "."]',
            '["unittest", "discover", "-s", "tests/cells/../substrate", "-t", "."]',
            '["unittest", "tests/cells/../substrate/test_store.py"]',
            '["unittest", "tests.cells_extra.test_x"]',
            '["unittest", "tests.substrate.test_store", "tests.cells.test_cell_fake"]',
            '["unittest"]',
        ):
            with self.subTest(argv=argv):
                code = f"import sys\nsys.argv = {argv}\n" + PROBE
                result = self.run_probe(code, **{isolation.LIVE_HOME_ENV: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(isolation.LIVE_HOME_ENV, result.stderr)
                self.assertIn("tests/cells", result.stderr)
        self.assert_fake_home_untouched()

    def test_child_of_isolated_process_keeps_its_own_environment(self) -> None:
        # A test may build a child env with its own HOME (see operator_env in
        # tests/substrate/test_dispatch.py). The inherited marker tells the
        # child it is already inside an isolated tree: nothing is stripped or
        # re-created.
        db = str(self.fake_home / ".agent-comms" / "agent-comms.sqlite")
        report = self.probe(
            **{
                isolation.SCRATCH_HOME_ENV: str(Path(self.temp.name) / "parent-scratch"),
                "AGENT_COMMS_DB": db,
                "AGENT_COMMS_ADMIN_TOKEN": "secret",
                "CODEX_HOME": str(self.fake_home / ".codex"),
            }
        )
        self.assertEqual(report["home"], str(self.fake_home))
        self.assertEqual(report["env"]["AGENT_COMMS_DB"], db)
        self.assertEqual(report["env"]["AGENT_COMMS_ADMIN_TOKEN"], "secret")
        self.assertEqual(report["env"]["CODEX_HOME"], str(self.fake_home / ".codex"))
        self.assertNotIn(isolation.SUPERVISOR_ROOT_ENV, report["env"])

    def test_stale_marker_with_the_account_home_does_not_bypass_isolation(self) -> None:
        # A marker left in a shell must not be trusted when HOME is the real
        # account home. The probe only imports modules; nothing is written.
        account_home = isolation._passwd_home()
        if account_home is None:
            self.skipTest("no passwd database on this platform")
        for stale in (str(Path(self.temp.name) / "stale"), str(account_home)):
            with self.subTest(marker=stale):
                report = self.probe(
                    HOME=str(account_home),
                    **{isolation.SCRATCH_HOME_ENV: stale, "AGENT_COMMS_DB": "x"},
                )
                self.assertNotEqual(report["home"], str(account_home))
                self.assertNotIn("AGENT_COMMS_DB", report["env"])
                self.assertEqual(report["env"][isolation.SCRATCH_HOME_ENV], report["home"])
                for name, value in report["paths"].items():
                    self.assertFalse(
                        Path(value).is_relative_to(account_home / ".agent-comms"),
                        f"{name} resolves inside the account home: {value}",
                    )

    def test_refuses_when_agent_comms_is_imported_first(self) -> None:
        result = self.run_probe("import agent_comms.paths\nimport tests\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("agent_comms.paths", result.stderr)
        self.assertIn("discover -s tests -t .", result.stderr)
        self.assert_fake_home_untouched()

    def test_discovery_with_tests_as_top_level_dir_still_isolates(self) -> None:
        # ``unittest discover -s tests`` (no ``-t``) imports ``substrate`` as a
        # top-level package before any test module; its ``__init__`` must pull
        # the guard in first.
        code = textwrap.dedent(
            """
            import os, sys
            sys.path.insert(0, "tests")
            import substrate
            import cells
            import tests
            print(os.environ["HOME"])
            """
        )
        result = self.run_probe(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout.strip(), str(self.fake_home))
        self.assert_fake_home_untouched()

    def test_discovery_with_substrate_as_top_level_dir_still_isolates(self) -> None:
        # ``unittest discover -s tests/substrate`` (no ``-t``) imports test
        # modules as top-level modules and never loads any package __init__.
        # Each module's own first import must bring the guard in.
        code = textwrap.dedent(
            """
            import os, sys
            sys.path.insert(0, "tests/substrate")
            import test_store
            from agent_comms import paths
            print(os.environ["HOME"])
            print(paths.DEFAULT_DB)
            """
        )
        result = self.run_probe(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        home, default_db = result.stdout.strip().splitlines()
        self.assertNotEqual(home, str(self.fake_home))
        self.assertFalse(Path(default_db).is_relative_to(self.fake_home))
        self.assert_fake_home_untouched()

    def test_every_test_module_imports_the_guard_first(self) -> None:
        import ast

        offenders = []
        for path in sorted(REPO_ROOT.glob("tests/**/test_*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            first = None
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                    continue
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    first = node
                    break
            names = [alias.name for alias in first.names] if isinstance(first, ast.Import) else []
            if names != ["tests.isolation"]:
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(
            offenders,
            [],
            "these test modules must start with 'import tests.isolation' before any other "
            "import (run local/tools/add-isolation-import.py or add the line by hand)",
        )

    def test_every_import_time_home_constant_is_isolated(self) -> None:
        # Module-level constants derived from Path.home() are frozen at import
        # time; the guard must have redirected HOME before that. Enumerate them
        # from source so a new constant cannot appear unchecked.
        import importlib
        import re

        pattern = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=.*Path\.home\(\)", re.M)
        found = {}
        for path in sorted((REPO_ROOT / "agent_comms").rglob("*.py")):
            for match in pattern.finditer(path.read_text()):
                module = ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
                found[f"{module}:{match.group('name')}"] = (module, match.group("name"))
        self.assertEqual(
            sorted(found),
            [
                "agent_comms.cli._helpers:ADMIN_TOKEN_PATH",
                "agent_comms.paths:DEFAULT_DB",
                "agent_comms.supervisor:_DEFAULT_CONTROL_ROOT",
            ],
        )
        protected = isolation.protected_roots()
        for key, (module, name) in found.items():
            value = getattr(importlib.import_module(module), name)
            for root in protected:
                self.assertFalse(
                    isolation.is_within(value, root),
                    f"{key} resolves inside the protected root {root}: {value}",
                )


if __name__ == "__main__":
    unittest.main()
