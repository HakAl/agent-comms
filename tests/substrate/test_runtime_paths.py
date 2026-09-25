from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import paths
from agent_comms.mcp_server import create_server


class FakeMCP:
    def __init__(self, _name: str) -> None:
        self.tools = []

    def tool(self):
        def decorate(func):
            self.tools.append(func.__name__)
            return func

        return decorate


class RuntimePathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.canonical = self.home / ".agent-comms" / "agent-comms.sqlite"
        self._patchers = [
            mock.patch.dict(
                os.environ,
                {
                    "HOME": str(self.home),
                    "AGENT_COMMS_ADMIN_TOKEN": "operator-secret",
                },
                clear=False,
            ),
            mock.patch.object(paths, "REPO_ROOT", self.repo),
        ]
        for patcher in self._patchers:
            patcher.start()
        os.environ.pop("AGENT_COMMS_DB", None)
        token_path = self.home / ".agent-comms" / "admin-token"
        token_path.parent.mkdir(parents=True)
        token_path.write_text("operator-secret")
        os.chmod(token_path, 0o600)

    def tearDown(self) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmpdir.cleanup()

    def test_runtime_paths_and_mcp_default_are_not_import_time_captured(self) -> None:
        self.assertEqual(paths.db_path(), self.canonical)
        override = self.root / "override.sqlite"
        with mock.patch.dict(os.environ, {"AGENT_COMMS_DB": str(override)}):
            self.assertEqual(paths.db_path(), override)

        captured = {}

        class FakeStore:
            def __init__(self, db_path: Path, *, is_default_db_open: bool = False) -> None:
                captured["db_path"] = db_path
                captured["is_default_db_open"] = is_default_db_open

            def require_launchable_actor(self, actor_id: str) -> None:
                captured["actor_id"] = actor_id

        with mock.patch("agent_comms.mcp_server.require_mcp", return_value=FakeMCP), mock.patch(
            "agent_comms.mcp_server.Store", FakeStore
        ):
            create_server(actor_id="architect")
        self.assertEqual(captured["db_path"], self.canonical)
        self.assertTrue(captured["is_default_db_open"])

    def test_dispatch_logs_live_under_the_runtime_root_not_the_checkout(self) -> None:
        # Regression for ac-cca: logs landed in <checkout>/logs/dispatch, so a
        # test run dirtied the source tree and an installed package without a
        # checkout had nowhere to write.
        log_dir = paths.dispatch_log_dir()
        self.assertEqual(log_dir, self.home / ".agent-comms" / "logs" / "dispatch")
        self.assertTrue(log_dir.is_dir())
        self.assertFalse(log_dir.is_relative_to(self.repo))
        log = paths.dispatch_log_path("dispatch_20260609_011536_cdd9b013")
        self.assertEqual(log.parent, log_dir.resolve())
        self.assertEqual(paths.dispatch_events_path("dispatch_20260609_011536_cdd9b013").parent, log_dir.resolve())

    def test_config_review_and_approval_roots_live_under_the_runtime_root(self) -> None:
        # ac-4ao.2: nothing an installed command reads or writes may resolve
        # under the source tree or the package directory.
        runtime = self.home / ".agent-comms"
        self.assertEqual(paths.actors_config_path(), runtime / "actors.json")
        self.assertEqual(paths.review_root(), runtime / "dispatch" / "reviews")
        self.assertEqual(paths.push_approval_root(), runtime / "dispatch" / "push-approvals")
        for resolved in (paths.actors_config_path(), paths.review_root(), paths.push_approval_root()):
            self.assertFalse(resolved.is_relative_to(self.repo), resolved)
            self.assertFalse(resolved.is_relative_to(paths.PACKAGE_ROOT), resolved)

    def test_hook_script_is_package_relative_not_checkout_relative(self) -> None:
        hook = paths.hooks_path()
        self.assertEqual(hook, paths.PACKAGE_ROOT / "hooks" / "pre_tool_use.py")
        self.assertTrue(hook.is_file())
        self.assertFalse(hook.is_relative_to(self.repo))

    def test_dispatch_log_dir_honours_the_environment_override(self) -> None:
        override = self.root / "elsewhere" / "dispatch-logs"
        with mock.patch.dict(os.environ, {"AGENT_COMMS_DISPATCH_LOG_DIR": str(override)}):
            self.assertEqual(paths.dispatch_log_dir(), override)
            self.assertTrue(override.is_dir())
            self.assertEqual(paths.dispatch_log_path("dispatch_20260609_011536_cdd9b013").parent, override.resolve())


if __name__ == "__main__":
    unittest.main()
