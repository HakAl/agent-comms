from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import db, paths, provisioning, release
from agent_comms.cli.commands import provision_codex_home
from agent_comms.schema import ValidationError
from agent_comms.store import Store


UNKNOWN_GIT = {
    "git_commit": "unknown",
    "git_branch": "unknown",
    "git_describe": "unknown",
    "git_exact_tag": None,
    "git_head_state": "unknown",
    "pin_worktree": "unknown",
}


class LauncherProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.custody = self.root / "custody"
        self.home = self.custody / "worker"
        self.store = Store(self.root / "ledger.sqlite")
        self.store.init()
        self.args = types.SimpleNamespace(
            actor_id="alpha-codex-worker",
            project_root=str(self.root),
            codex_home=str(self.home),
            override_protected=None,
        )
        auth = self.root / "shared-auth.json"
        auth.write_text("{}")
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {"AGENT_COMMS_CODEX_CUSTODY_ROOT": str(self.custody)},
            )
        )
        self.enterContext(
            mock.patch.object(paths, "runtime_codex_auth_source", return_value=auth)
        )
        self.enterContext(
            mock.patch.object(paths, "codex_auth_source", return_value=auth)
        )

    def _assert_written(self, result):
        expected = [
            self.home / "config.toml",
            self.home / f"{self.args.actor_id}.config.toml",
            self.home / "auth.json",
        ]
        self.assertEqual(result["written"], [str(path) for path in expected])
        for path in expected:
            self.assertTrue(path.is_file(), str(path))

    def _success(self, production):
        self.store._db.is_default_db_open = production
        controlled = {
            "repo_root": "/collector-root-must-not-be-copied",
            "git_commit": "123456abcdef",
            "git_branch": "fixture-branch",
            "git_describe": "fixture-tag-3-g123456abcdef",
            "git_exact_tag": "fixture-tag",
            "git_head_state": "fixture-state",
            "pin_worktree": True,
        }
        with mock.patch.object(
            release, "repo_git_info", return_value=controlled
        ) as collect:
            result = provision_codex_home.handle(self.store, self.args)
        self._assert_written(result)
        self.assertIn("launcher", result)
        self.assertEqual(
            result["launcher"],
            {
                **{key: controlled[key] for key in UNKNOWN_GIT},
                "command": str(paths.mcp_command()),
                "repo_root": str(paths.REPO_ROOT),
                "ledger_schema_version": db.LEDGER_SCHEMA_VERSION,
            },
        )
        collect.assert_called_once_with(paths.REPO_ROOT)

    def test_success_production(self):
        self._success(True)

    def test_success_scratch(self):
        self._success(False)

    def _refusal(self, exception, message, writer):
        result = None
        with mock.patch.object(release, "repo_git_info") as collect, writer as write:
            with self.assertRaisesRegex(exception, message):
                result = provision_codex_home.handle(self.store, self.args)
            collect.assert_not_called()
        self.assertFalse(self.home.exists())
        self.assertNotIn("launcher", result or {})
        return write

    def test_protected_refuses_production_and_scratch(self):
        self.store.register_agent_actor(
            "alpha-architect", "alpha", "architect", str(self.root), []
        )
        self.store.register_agent_actor(
            self.args.actor_id,
            "alpha",
            "worker",
            str(self.root),
            [],
            owner="alpha-architect",
            protected=True,
        )
        for production in (True, False):
            with self.subTest(production=production):
                self.store._db.is_default_db_open = production
                write = self._refusal(
                    ValidationError,
                    "protected",
                    mock.patch.object(provisioning, "write_codex_home"),
                )
                write.assert_not_called()

    def test_production_outside_custody_refuses(self):
        self.store._db.is_default_db_open = True
        self.home = self.root / "outside"
        self.args.codex_home = str(self.home)
        write = self._refusal(
            ValidationError,
            "outside the runtime custody root",
            mock.patch.object(
                provisioning, "write_codex_home", wraps=provisioning.write_codex_home
            ),
        )
        write.assert_called_once()

    def _assert_unknown(self, result):
        self._assert_written(result)
        self.assertIn("launcher", result)
        self.assertEqual(
            {key: result["launcher"][key] for key in UNKNOWN_GIT}, UNKNOWN_GIT
        )

    def test_git_commands_unavailable_still_provisions(self):
        with mock.patch.object(release, "_run_git", return_value=None):
            result = provision_codex_home.handle(self.store, self.args)
        self._assert_unknown(result)

    def test_collector_exception_still_provisions(self):
        with mock.patch.object(
            release, "repo_git_info", side_effect=RuntimeError("git failed")
        ):
            result = provision_codex_home.handle(self.store, self.args)
        self._assert_unknown(result)
