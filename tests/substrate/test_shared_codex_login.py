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

from agent_comms import cli, paths, provisioning, setup
from agent_comms.cli._helpers import bootstrap_store
from agent_comms.schema import ValidationError
from agent_comms.store import Store


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

    def test_login_command_for_a_lineage_key_under_another_name(self) -> None:
        # The operator's auth.json is itself a link to a file under another
        # name: the lineage key (realpath) is that file, and it is still the
        # operator's login, present or dangling.
        secrets = self.root / "secrets"
        secrets.mkdir()
        self.codex_home.mkdir()
        self.operator_auth.symlink_to(secrets / "login.json")
        self.assertEqual(paths.codex_login_command(secrets / "login.json"), "codex login")
        (secrets / "login.json").write_text("{}")
        self.assertEqual(paths.codex_login_command(secrets / "login.json"), "codex login")
        self.assertEqual(paths.codex_login_command(self.operator_auth), "codex login")
        # Another home's renamed file: no home can be inferred from a file
        # name, so the remedy names the file, not a made-up CODEX_HOME.
        other = self.root / "other" / "creds.json"
        other.parent.mkdir()
        other.write_text("{}")
        remedy = paths.codex_login_command(other)
        self.assertIn(str(other), remedy)
        self.assertIn("CODEX_HOME=<that home> codex login", remedy)
        self.assertNotIn(f"CODEX_HOME={shlex.quote(str(other))}", remedy)


class SharedLoginBoundaryTest(_Scratch):
    """The project root of a worker that shares the operator's login must stay
    outside that login, however the login is reached and whoever provisions."""

    def setUp(self) -> None:
        super().setUp()
        self.custody = self.root / "custody"
        env = mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_CUSTODY_ROOT": str(self.custody)})
        env.start()
        self.addCleanup(env.stop)
        self.project = self.root / "project"
        self.project.mkdir()

    def test_problem_names_the_login_file_a_symlinked_auth_resolves_to(self) -> None:
        self.assertIsNone(paths.shared_login_boundary_problem(self.project))
        # The home is elsewhere, but its auth.json points into the project.
        self.codex_home.mkdir()
        self.operator_auth.symlink_to(self.project / "credentials" / "login.json")
        problem = paths.shared_login_boundary_problem(self.project) or ""
        self.assertIn("your Codex login file", problem)
        self.assertIn(str(self.operator_auth), problem)
        with self.assertRaises(ValidationError) as caught:
            setup.refuse_project_root_overlap(self.project)
        self.assertIn("--project-root", str(caught.exception))
        # Present or dangling, the target is the credential.
        (self.project / "credentials").mkdir()
        (self.project / "credentials" / "login.json").write_text("{}")
        self.assertIn("your Codex login file", paths.shared_login_boundary_problem(self.project) or "")
        # A project beside the target is fine.
        self.assertIsNone(paths.shared_login_boundary_problem(self.root / "elsewhere"))

    def test_problem_names_the_home_and_the_runtime_state(self) -> None:
        for root, name in ((self.codex_home.parent, "your Codex home"), (self.codex_home / "sub", "your Codex home"),
                           (self.runtime_root / "x", "runtime root"), (self.custody, "custody root")):
            self.assertIn(name, paths.shared_login_boundary_problem(root) or "", root)

    def _registry(self) -> Path:
        config = self.root / "actors.json"
        config.write_text(json.dumps({"actors": {
            "01M36YTJV9XBW95S6ZWV47C4RG": {"kind": "human", "display_name": "alice"},
            "alpha-architect": {"kind": "agent", "display_name": "alpha-architect", "team": "alpha", "role": "architect", "project_root": str(self.project), "capabilities": []},
            "alpha-codex-worker": {"kind": "agent", "display_name": "alpha-codex-worker", "team": "alpha", "role": "worker", "owner": "alpha-architect", "runtime": "codex", "project_root": str(self.project), "capabilities": []},
        }}))
        return config

    def test_bootstrap_of_an_existing_registry_refuses_a_login_inside_the_project(self) -> None:
        # The registry predates the shared login; CODEX_HOME now sits inside
        # the worker's project. Setup would refuse this; bootstrap must too.
        config = self._registry()
        store = Store(self.root / "explicit.sqlite")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.project / "operator-codex")}):
            with self.assertRaises(ValidationError) as caught:
                bootstrap_store(store, config, codex_custody=True)
        message = str(caught.exception)
        self.assertIn("worker alpha-codex-worker", message)
        self.assertIn("your Codex home", message)
        self.assertFalse(self.link.is_symlink() or self.link.exists())
        self.assertFalse((self.custody / "default" / "alpha-codex-worker").exists())
        # With the login outside the project the same registry bootstraps.
        bootstrap_store(store, config, codex_custody=True)
        self.assertEqual(os.readlink(self.link), str(self.operator_auth))

    def test_rebinding_a_login_inside_the_project_is_refused_at_admission(self) -> None:
        # bind-codex-login takes no ledger, so it cannot see worker roots; the
        # spawn preflight of a managed home is where a later CODEX_HOME inside
        # the project is caught.
        from tests.codex_custody_support import install_fake_codex_custody
        from agent_comms.adapters import DispatchContext
        from agent_comms.adapters._base import SpawnFailed
        from agent_comms.adapters.codex import CodexAdapter
        from agent_comms.store import WORKER_DISPATCH_POLICY

        install_fake_codex_custody(self)
        config = self._registry()
        store = Store(self.root / "explicit.sqlite")
        bootstrap_store(store, config, codex_custody=True)
        actor = {a["id"]: a for a in store.list_actors()}["alpha-codex-worker"]
        home = self.custody / "default" / "alpha-codex-worker"
        self.assertTrue((home / "auth.json").is_symlink())
        context = DispatchContext(
            dispatch={"dispatch_id": "dispatch_20260802_120000_abcdef12", "policy_name": WORKER_DISPATCH_POLICY},
            recipient={"id": "alpha-codex-worker", "runtime": "codex", "project_root": str(self.project), "spawn": actor["spawn"]},
            message={"id": "m"},
            ttl_seconds=30,
            expected_close_by="2026-08-02T12:00:30+00:00",
            db_path=str(store._db.db_path),
        )
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.project / "operator-codex")}):
            with self.assertRaises(SpawnFailed) as caught:
                CodexAdapter()._preflight(context)
        message = str(caught.exception)
        self.assertIn("project root", message)
        self.assertIn("your Codex home", message)
        self.assertIn("re-register", message)
        # The shared link rebound from another shell, with a CODEX_HOME inside
        # the project, and a fresh login there; this launcher's own CODEX_HOME
        # is still the safe one. What the worker's auth.json chains into is
        # what is checked, not the launcher's environment.
        foreign = self.project / "operator-login" / "auth.json"
        foreign.parent.mkdir()
        foreign.write_text(json.dumps({"tokens": {}, "last_refresh": "2026-08-02T11:59:00+00:00"}))
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(foreign.parent)}):
            self.assertEqual(provisioning.link_shared_codex_auth(replace=True)["status"], "linked")
        self.assertEqual(os.path.realpath(home / "auth.json"), os.path.realpath(foreign))
        self.assertEqual(paths.operator_codex_auth(), self.operator_auth)
        with self.assertRaises(SpawnFailed) as caught:
            CodexAdapter()._preflight(context)
        self.assertIn("the Codex login the worker shares", str(caught.exception))
        # Provisioning keeps a foreign link it does not replace, so it checks
        # that credential too: the same registry no longer bootstraps.
        with self.assertRaises(ValidationError) as caught:
            bootstrap_store(Store(self.root / "second.sqlite"), config, codex_custody=True)
        self.assertIn("the Codex login the worker shares", str(caught.exception))
        # Rebound to the safe login, the worker is admitted again.
        self.assertEqual(provisioning.link_shared_codex_auth(replace=True)["status"], "linked")
        self.codex_home.mkdir(exist_ok=True)
        self.operator_auth.write_text(json.dumps({"tokens": {}, "last_refresh": "2026-08-02T11:59:00+00:00"}))
        with mock.patch.object(CodexAdapter, "_now_utc", return_value=__import__("datetime").datetime(2026, 8, 2, 12, 0, tzinfo=__import__("datetime").timezone.utc)):
            self.assertIn("codex_auth", CodexAdapter()._preflight(context) or {})


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

    def _login_appears_during_publication(self):
        # Another process (a legacy codex login, a second bind) writes a
        # regular file at the link between our inspection and our link.
        link = self.link

        def symlink_to(path, target, *args, **kwargs):
            if Path(path) == link:
                link.write_text('{"concurrent": true}')
                raise FileExistsError(str(link))
            return original(path, target, *args, **kwargs)

        original = Path.symlink_to
        return mock.patch.object(Path, "symlink_to", symlink_to)

    def test_a_login_appearing_at_an_absent_link_is_kept(self) -> None:
        with self._login_appears_during_publication():
            with self.assertRaises(ValidationError) as caught:
                provisioning.link_shared_codex_auth()
        self.assertIn("appeared while binding", str(caught.exception))
        self.assertTrue(self.link.is_file() and not self.link.is_symlink())
        self.assertEqual(self.link.read_text(), '{"concurrent": true}')
        self.assertEqual(sorted(p.name for p in self.link.parent.iterdir()), ["auth.json"])

    def test_a_login_appearing_after_the_move_aside_keeps_both(self) -> None:
        self.link.parent.mkdir(parents=True)
        self.link.write_text('{"separate": true}')
        with self._login_appears_during_publication():
            with self.assertRaises(ValidationError) as caught:
                provisioning.link_shared_codex_auth(replace=True)
        message = str(caught.exception)
        self.assertIn("appeared while binding", message)
        self.assertIn("--replace", message)
        asides = [p for p in self.link.parent.iterdir() if p.name.startswith("auth.json.separate-")]
        self.assertEqual(len(asides), 1)
        self.assertIn(str(asides[0]), message)
        self.assertEqual(asides[0].read_text(), '{"separate": true}')
        self.assertEqual(self.link.read_text(), '{"concurrent": true}')

    def test_replacing_a_link_elsewhere_leaves_no_aside_behind(self) -> None:
        self.link.parent.mkdir(parents=True)
        self.link.symlink_to(self.root / "elsewhere" / "auth.json")
        result = provisioning.link_shared_codex_auth(replace=True)
        self.assertEqual(result["status"], "linked")
        self.assertEqual(sorted(p.name for p in self.link.parent.iterdir()), ["auth.json"])

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
