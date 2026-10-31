"""Workers share the operator's own Codex login (ac-zz9).

``paths.operator_codex_auth`` names the operator's ``auth.json``;
``provisioning.link_shared_codex_auth`` points the runtime's shared auth file
at it, dangling until the first ``codex login``; ``bind-codex-login`` wraps
that for existing installs; every login fix names ``codex login`` when the
path chains into the operator's home.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import cli, paths, provisioning
from agent_comms.schema import ValidationError


class _Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime_root = self.root / "runtime"
        self.codex_home = self.root / "codex"
        env = mock.patch.dict(
            os.environ,
            {paths.HOME_ENV: str(self.runtime_root), "CODEX_HOME": str(self.codex_home)},
        )
        env.start()
        self.addCleanup(env.stop)
        self.link = paths.runtime_codex_auth_source()
        self.operator_auth = self.codex_home / "auth.json"


class OperatorCodexAuthTest(_Scratch):
    def test_follows_codex_home_then_the_home_directory(self) -> None:
        self.assertEqual(paths.operator_codex_home(), self.codex_home)
        self.assertEqual(paths.operator_codex_auth(), self.operator_auth)
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            self.assertEqual(paths.operator_codex_home(), Path.home() / ".codex")
            self.assertEqual(paths.operator_codex_auth(), Path.home() / ".codex" / "auth.json")
        with mock.patch.dict(os.environ, {"CODEX_HOME": "~/elsewhere"}):
            self.assertEqual(paths.operator_codex_home(), Path.home() / "elsewhere")

    def test_problem_when_the_login_would_live_inside_agent_comms(self) -> None:
        self.assertIsNone(paths.operator_codex_auth_problem(self.operator_auth))
        inside = self.runtime_root / "codex-auth" / "auth.json"
        self.assertIn("runtime root", paths.operator_codex_auth_problem(inside) or "")
        nested = self.runtime_root / "nested" / "auth.json"
        self.assertIn("runtime root", paths.operator_codex_auth_problem(nested) or "")
        custody = paths.codex_custody_root() / "default" / "w" / "auth.json"
        self.assertIsNotNone(paths.operator_codex_auth_problem(custody))
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_CUSTODY_ROOT": str(self.root / "custody")}):
            self.assertIn("custody root", paths.operator_codex_auth_problem(self.root / "custody" / "auth.json") or "")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.runtime_root / "codex-auth")}):
            self.assertIsNotNone(paths.operator_codex_auth_problem())

    def test_problem_sees_through_a_symlinked_home(self) -> None:
        self.runtime_root.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.runtime_root)
        self.assertIsNotNone(paths.operator_codex_auth_problem(alias / "codex-auth" / "auth.json"))

    def test_login_command_names_the_operator_login_only_when_the_path_chains_into_it(self) -> None:
        self.assertEqual(paths.codex_login_command(self.codex_home), "codex login")
        self.assertEqual(paths.codex_login_command(self.operator_auth), "codex login")
        other = self.root / "other home"
        self.assertEqual(paths.codex_login_command(other), f"CODEX_HOME={shlex.quote(str(other))} codex login")
        self.assertEqual(paths.codex_login_command(other / "auth.json"), f"CODEX_HOME={shlex.quote(str(other))} codex login")
        # A worker home whose auth.json chains through the shared link, even
        # before the operator has logged in, is the operator's login.
        self.link.parent.mkdir(parents=True)
        self.link.symlink_to(self.operator_auth)
        worker = self.root / "homes" / "w"
        worker.mkdir(parents=True)
        (worker / "auth.json").symlink_to(self.link)
        self.assertEqual(paths.codex_login_command(worker), "codex login")
        self.assertEqual(paths.codex_login_command(Path(os.path.realpath(worker / "auth.json"))), "codex login")
        # A regular file in the worker home is that home's own login.
        (worker / "auth.json").unlink()
        (worker / "auth.json").write_text("{}")
        self.assertEqual(paths.codex_login_command(worker), f"CODEX_HOME={shlex.quote(str(worker))} codex login")


class LinkSharedCodexAuthTest(_Scratch):
    def test_links_dangling_before_the_first_login_and_leaves_it_alone_after(self) -> None:
        result = provisioning.link_shared_codex_auth()
        self.assertEqual(result["status"], "linked")
        self.assertEqual(result["source"], str(self.operator_auth))
        self.assertFalse(result["source_present"])
        self.assertTrue(self.link.is_symlink())
        self.assertFalse(self.link.exists())
        self.assertEqual(os.readlink(self.link), str(self.operator_auth))
        # The login completes the link with no further agent-comms command.
        self.codex_home.mkdir()
        self.operator_auth.write_text(json.dumps({"tokens": {}}))
        self.assertTrue(self.link.is_file())
        again = provisioning.link_shared_codex_auth()
        self.assertEqual(again["status"], "already")
        self.assertTrue(again["source_present"])
        self.assertEqual(os.readlink(self.link), str(self.operator_auth))

    def test_already_through_a_symlinked_operator_home(self) -> None:
        real_home = self.root / "real codex"
        real_home.mkdir()
        (real_home / "auth.json").write_text("{}")
        self.codex_home.symlink_to(real_home)
        provisioning.link_shared_codex_auth()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(real_home)}):
            self.assertEqual(provisioning.link_shared_codex_auth()["status"], "already")

    def test_regular_file_is_kept_unless_replaced_and_then_moved_aside(self) -> None:
        self.link.parent.mkdir(parents=True)
        self.link.write_text('{"separate": true}')
        result = provisioning.link_shared_codex_auth()
        self.assertEqual(result["status"], "separate")
        self.assertTrue(self.link.is_file() and not self.link.is_symlink())
        result = provisioning.link_shared_codex_auth(replace=True)
        self.assertEqual(result["status"], "linked")
        aside = Path(result["moved_aside"])
        self.assertEqual(aside.parent, self.link.parent)
        self.assertTrue(aside.name.startswith("auth.json.separate-"))
        self.assertEqual(aside.read_text(), '{"separate": true}')
        self.assertEqual(os.readlink(self.link), str(self.operator_auth))

    def test_link_elsewhere_is_kept_unless_replaced(self) -> None:
        self.link.parent.mkdir(parents=True)
        elsewhere = self.root / "elsewhere" / "auth.json"
        self.link.symlink_to(elsewhere)
        result = provisioning.link_shared_codex_auth()
        self.assertEqual(result["status"], "elsewhere")
        self.assertEqual(result["current"], str(elsewhere))
        self.assertEqual(os.readlink(self.link), str(elsewhere))
        result = provisioning.link_shared_codex_auth(replace=True)
        self.assertEqual(result["status"], "linked")
        self.assertEqual(result["replaced"], str(elsewhere))
        self.assertNotIn("moved_aside", result)
        self.assertEqual(os.readlink(self.link), str(self.operator_auth))

    def test_refuses_a_login_inside_the_runtime_root_before_writing(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.runtime_root / "codex-auth")}):
            with self.assertRaises(ValidationError) as caught:
                provisioning.link_shared_codex_auth()
        self.assertIn("runtime root", str(caught.exception))
        self.assertFalse(self.link.parent.exists())

    def test_refuses_a_directory_at_the_link(self) -> None:
        self.link.mkdir(parents=True)
        with self.assertRaises(ValidationError):
            provisioning.link_shared_codex_auth(replace=True)
        self.assertTrue(self.link.is_dir())


class BindCodexLoginCommandTest(_Scratch):
    def run_cli(self, *argv: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cli.run(list(argv))
        return rc, json.loads(buffer.getvalue())

    def test_binds_without_a_ledger_and_reports(self) -> None:
        rc, result = self.run_cli("bind-codex-login")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["status"], "linked")
        self.assertEqual(result["link"], str(self.link))
        self.assertEqual(os.readlink(self.link), str(self.operator_auth))
        self.assertFalse(paths.db_path().exists())
        rc, result = self.run_cli("bind-codex-login")
        self.assertEqual((rc, result["status"]), (0, "already"))

    def test_separate_login_needs_replace(self) -> None:
        self.link.parent.mkdir(parents=True)
        self.link.write_text("{}")
        rc, result = self.run_cli("bind-codex-login")
        self.assertEqual(rc, 2)
        self.assertIn("--replace", result["error"])
        self.assertTrue(self.link.is_file() and not self.link.is_symlink())
        rc, result = self.run_cli("bind-codex-login", "--replace")
        self.assertEqual((rc, result["status"]), (0, "linked"), result)
        self.assertTrue(Path(result["moved_aside"]).is_file())

    def test_link_elsewhere_needs_replace(self) -> None:
        self.link.parent.mkdir(parents=True)
        self.link.symlink_to(self.root / "elsewhere")
        rc, result = self.run_cli("bind-codex-login")
        self.assertEqual(rc, 2)
        self.assertIn("--replace", result["error"])
        rc, result = self.run_cli("bind-codex-login", "--replace")
        self.assertEqual((rc, result["status"]), (0, "linked"), result)

    def test_refused_codex_home_is_an_error(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.runtime_root / "codex-auth")}):
            rc, result = self.run_cli("bind-codex-login")
        self.assertEqual(rc, 2)
        self.assertIn("runtime root", result["error"])


if __name__ == "__main__":
    unittest.main()
