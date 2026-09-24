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


if __name__ == "__main__":
    unittest.main()
