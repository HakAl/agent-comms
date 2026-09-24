"""Slice 1 DoD: no hardcoded in-repo paths; placeholders/env resolve correctly.

Guards the provisioning-layer path-relativization work:
- the grep gate (zero hardcoded operator paths in tracked code/config),
- spawn placeholders resolve to repo-derived paths (Category A),
- ``project_root`` env expansion is backward-compatible and loud on unset,
- codex-home config is generated from derived paths, never baked.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import paths, provisioning
from agent_comms.adapters import DispatchContext
from agent_comms.adapters.claude import ClaudeAdapter
from agent_comms.adapters.codex import CodexAdapter
from agent_comms.cli import bootstrap_store, expand_path_value
from agent_comms.onboarding import onboard_worker
from agent_comms.policies import WORKER_DISPATCH_POLICY_VERSION, compile_policy
from agent_comms.schema import ValidationError
from agent_comms.store import Store, WORKER_DISPATCH_POLICY
from tests import dispatch_cell_harness

ROOT = Path(__file__).resolve().parents[2]
# Operative code/config that must carry zero hardcoded absolute paths. Docs and
# gitignored local/ may use example paths; they are not loaded at runtime.
OPERATIVE_TREES = ["agent_comms", "scripts", "config", "tests", "pyproject.toml"]
# Built non-literally so this test file does not itself trip the gate it defines.
HOME_ROOT_NEEDLE = "/" + "Users/"
HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"
ARCHITECT_ID = "alpha-architect"


def _context(recipient: dict, db_path: Path) -> DispatchContext:
    return DispatchContext(
        dispatch={"dispatch_id": "d-test", "policy_name": WORKER_DISPATCH_POLICY},
        recipient=recipient,
        message={"id": "m-test"},
        ttl_seconds=45,
        expected_close_by="2026-01-01T00:00:00Z",
        db_path=str(db_path),
    )


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")


def _make_minimal_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init")
    (repo / "README.md").write_text("fixture\n")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=Test User", "-c", "user.email=test@example.invalid", "commit", "-m", "init")
    return repo


class GrepGateTest(unittest.TestCase):
    def test_no_hardcoded_absolute_paths_in_operative_tree(self) -> None:
        result = subprocess.run(
            ["git", "grep", "-l", HOME_ROOT_NEEDLE, "--", *OPERATIVE_TREES],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.stdout.strip(),
            "",
            f"hardcoded absolute paths still tracked in:\n{result.stdout}",
        )


class ExpandPathValueTest(unittest.TestCase):
    def test_plain_absolute_path_unchanged(self) -> None:
        # Backward-compat: existing configs with absolute paths resolve as-is.
        self.assertEqual(expand_path_value("/opt/legacy/abs"), "/opt/legacy/abs")

    def test_repo_root_var_is_always_available(self) -> None:
        self.assertEqual(expand_path_value("${AGENT_COMMS_ROOT}"), str(paths.REPO_ROOT))

    @mock.patch.dict(os.environ, {"PROJECT_A_ROOT": "/srv/ds"})
    def test_operator_var_expands(self) -> None:
        self.assertEqual(expand_path_value("${PROJECT_A_ROOT}/local-llm"), "/srv/ds/local-llm")

    def test_unset_var_fails_loudly(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValidationError, "unresolved environment variable"):
                expand_path_value("${NOPE_NOT_SET}/x")


class SpawnPlaceholderResolutionTest(unittest.TestCase):
    """Category A: in-repo paths in spawn config resolve from the repo, not literals."""

    @mock.patch.dict(
        os.environ,
        {
            "PROJECT_A_ROOT": "/srv/project-a",
            "PROJECT_C_ROOT": "/srv/team-c",
        },
    )
    def _bootstrap(self) -> dict:
        store = Store(Path(self._tmp) / "agent-comms.sqlite")
        bootstrap_store(store, ROOT / "tests" / "fixtures" / "roster.json")
        return {actor["id"]: actor for actor in store.list_actors()}

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = self._tmpdir.name
        self.actors = self._bootstrap()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_claude_spawn_resolves_repo_paths_and_env_root(self) -> None:
        worker = self.actors["alpha-claude-worker"]
        adapter = ClaudeAdapter()
        context = _context(worker, paths.db_path())
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        resolved = adapter._resolved_spawn_args(context, worker["spawn"], policy)

        # No unresolved placeholder or ${ENV} token survives.
        joined = " ".join(resolved)
        for token in ("{db_path}", "{mcp_command}", "{hooks_path}", "{project_root}", "${"):
            self.assertNotIn(token, joined)

        # _format_arg already unescaped {{ }} -> { } via str.format; parse directly.
        mcp_config = json.loads(resolved[resolved.index("--mcp-config") + 1])
        server = mcp_config["mcpServers"]["agent-comms"]
        self.assertEqual(server["command"], str(paths.mcp_command()))
        self.assertEqual(server["args"][server["args"].index("--db") + 1], str(paths.db_path()))
        self.assertEqual(server["env"]["AGENT_COMMS_PROJECT_ROOT"], "/srv/project-a")

        settings = json.loads(resolved[resolved.index("--settings") + 1])
        hook_cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(hook_cmd, f"python3 {paths.hooks_path()}")

    def test_codex_spawn_env_codex_home_resolves_from_repo(self) -> None:
        alpha_worker = self.actors["alpha-codex-worker"]
        team_c_worker = self.actors["team-c-codex-worker"]
        adapter = CodexAdapter()
        from agent_comms.policies import compile_policy

        alpha_env = adapter._spawn_env(
            alpha_worker["spawn"], _context(alpha_worker, paths.db_path()), compile_policy(WORKER_DISPATCH_POLICY)
        )
        team_c_env = adapter._spawn_env(
            team_c_worker["spawn"], _context(team_c_worker, paths.db_path()), compile_policy(WORKER_DISPATCH_POLICY)
        )
        self.assertEqual(alpha_env["CODEX_HOME"], str(paths.codex_home("alpha-codex-worker")))
        self.assertEqual(
            team_c_env["CODEX_HOME"], str(paths.codex_home("team-c-codex-worker"))
        )
        self.assertNotEqual(alpha_env["CODEX_HOME"], team_c_env["CODEX_HOME"])

    def test_fake_worker_project_root_is_repo_root(self) -> None:
        self.assertEqual(self.actors["alpha-fake-worker"]["project_root"], str(paths.REPO_ROOT))


class CodexProvisioningTest(unittest.TestCase):
    def test_paths_codex_home_is_per_actor(self) -> None:
        self.assertEqual(
            paths.codex_home("alpha-codex-worker"),
            paths.REPO_ROOT / "config" / "codex-home-alpha-codex-worker",
        )
        self.assertEqual(
            paths.codex_home("team-c-codex-worker"),
            paths.REPO_ROOT / "config" / "codex-home-team-c-codex-worker",
        )
        self.assertNotEqual(
            paths.codex_home("alpha-codex-worker"),
            paths.codex_home("team-c-codex-worker"),
        )
        self.assertEqual(
            paths.codex_auth_source(),
            paths.REPO_ROOT / "config" / "codex-home" / "auth.json",
        )

    def test_generated_base_config_derives_mcp_command_and_takes_root(self) -> None:
        text = provisioning.render_codex_base_config("alpha-codex-worker", "/srv/ds")
        self.assertIn(f'command = "{paths.mcp_command()}"', text)
        self.assertIn('args = ["--actor-id", "alpha-codex-worker"]', text)
        self.assertIn('AGENT_COMMS_PROJECT_ROOT = "/srv/ds"', text)
        self.assertIn('[projects."/srv/ds"]', text)
        self.assertIn('default_tools_approval_mode = "approve"', text)
        # No unresolved template placeholder leaks into the generated config.
        for token in ("{mcp_command}", "{actor_id}", "{project_root}", "{codex_home}"):
            self.assertNotIn(token, text)
        config = tomllib.loads(text)
        self.assertEqual(
            config["mcp_servers"]["agent-comms"]["env"]["WAKE_POLICY_VERSION"],
            WORKER_DISPATCH_POLICY_VERSION,
        )

    def test_cell_harness_codex_config_uses_worker_policy_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cert_home = root / "cert-home"
            cert_home.mkdir()
            (cert_home / "auth.json").write_text("{}")
            with mock.patch.object(
                dispatch_cell_harness,
                "codex_cert_home",
                return_value=cert_home,
            ):
                harness = dispatch_cell_harness.make_codex_harness(root / "cell")
                config = tomllib.loads(
                    (root / "cell" / "codex-home" / "config.toml").read_text()
                )
                harness.close()

        self.assertEqual(
            config["mcp_servers"]["agent-comms"]["env"]["WAKE_POLICY_VERSION"],
            WORKER_DISPATCH_POLICY_VERSION,
        )

    def test_write_codex_home_materializes_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "codex-home"
            written = provisioning.write_codex_home(
                home, "alpha-codex-worker", "/srv/ds", auth_source=None
            )
            self.assertEqual(
                {p.name for p in written},
                {"config.toml", "alpha-codex-worker.config.toml"},
            )
            profile = (home / "alpha-codex-worker.config.toml").read_text()
            self.assertIn('sandbox_mode = "workspace-write"', profile)
            base = (home / "config.toml").read_text()
            self.assertIn('args = ["--actor-id", "alpha-codex-worker"]', base)
            self.assertIn('AGENT_COMMS_ACTOR_ID = "alpha-codex-worker"', base)

    def test_write_codex_home_isolates_sibling_actor_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            home_a = tmp / "codex-home-actor-a"
            home_b = tmp / "codex-home-actor-b"
            provisioning.write_codex_home(home_a, "actor-a", "/srv/a", auth_source=None)
            provisioning.write_codex_home(home_b, "actor-b", "/srv/b", auth_source=None)

            base_a = (home_a / "config.toml").read_text()
            self.assertIn('args = ["--actor-id", "actor-a"]', base_a)
            self.assertNotIn("actor-b", base_a)
            base_b = (home_b / "config.toml").read_text()
            self.assertIn('args = ["--actor-id", "actor-b"]', base_b)
            self.assertNotIn("actor-a", base_b)

            profile_a = (home_a / "actor-a.config.toml").read_text()
            self.assertIn('[projects."/srv/a"]', profile_a)
            self.assertNotIn("/srv/b", profile_a)

    def test_write_codex_home_symlinks_auth_when_source_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            auth_dir = tmp / "shared-auth"
            auth_dir.mkdir()
            auth_source = auth_dir / "auth.json"
            auth_source.write_text('{"fake": "auth"}')
            home = tmp / "codex-home-actor-x"

            written = provisioning.write_codex_home(home, "actor-x", "/srv/x", auth_source=auth_source)

            target = home / "auth.json"
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), auth_source.resolve())
            self.assertEqual(target.read_text(), '{"fake": "auth"}')
            self.assertIn(target, written)

            provisioning.write_codex_home(home, "actor-x", "/srv/x", auth_source=auth_source)
            self.assertEqual(target.resolve(), auth_source.resolve())

    def test_write_codex_home_warns_on_missing_auth_source_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            auth_source = tmp / "does-not-exist.json"
            home = tmp / "codex-home-actor-y"
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                provisioning.write_codex_home(home, "actor-y", "/srv/y", auth_source=auth_source)

            self.assertTrue((home / "config.toml").is_file())
            self.assertTrue((home / "actor-y.config.toml").is_file())
            self.assertFalse((home / "auth.json").exists())
            self.assertIn(str(auth_source), stderr.getvalue())
            self.assertIn("warning", stderr.getvalue().lower())

    def test_write_codex_home_preserves_regular_file_at_auth_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            home = tmp / "codex-home-actor-z"
            home.mkdir()
            target = home / "auth.json"
            target.write_text('{"operator": "auth"}')
            auth_dir = tmp / "shared-auth"
            auth_dir.mkdir()
            auth_source = auth_dir / "auth.json"
            auth_source.write_text('{"shared": "auth"}')
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                provisioning.write_codex_home(home, "actor-z", "/srv/z", auth_source=auth_source)

            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_text(), '{"operator": "auth"}')
            self.assertIn("warning", stderr.getvalue().lower())
            self.assertIn("regular file", stderr.getvalue().lower())

    def test_write_codex_home_replaces_dangling_symlink_at_auth_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            home = tmp / "codex-home-actor-d"
            home.mkdir()
            target = home / "auth.json"
            target.symlink_to(tmp / "missing-target.json")
            auth_dir = tmp / "shared-auth"
            auth_dir.mkdir()
            auth_source = auth_dir / "auth.json"
            auth_source.write_text('{"shared": "auth"}')

            provisioning.write_codex_home(home, "actor-d", "/srv/d", auth_source=auth_source)

            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), auth_source.resolve())
            self.assertEqual(target.read_text(), '{"shared": "auth"}')
            self.assertEqual(list(home.glob(".auth.json.*.new")), [])

    def test_write_codex_home_rejects_noncanonical_before_mutation(self) -> None:
        bad_actor_ids = ["a/b", "..", "nul", " padded ", "Alpha-Worker"]
        for actor_id in bad_actor_ids:
            with self.subTest(actor_id=actor_id):
                with tempfile.TemporaryDirectory() as temp_dir:
                    home = Path(temp_dir) / "codex-home"

                    with self.assertRaises(ValidationError):
                        provisioning.write_codex_home(home, actor_id, "/srv/ds")

                    self.assertFalse(home.exists())

    def test_paths_codex_home_rejects_noncanonical_actor_id(self) -> None:
        with self.assertRaisesRegex(ValidationError, "must not be empty"):
            paths.codex_home("")

        bad_actor_ids = [".", "..", "foo/bar", "a\\b", "x\0y", "Alpha-Codex-Worker"]
        for actor_id in bad_actor_ids:
            with self.subTest(actor_id=actor_id):
                with self.assertRaisesRegex(ValidationError, "canonical agent id form"):
                    paths.codex_home(actor_id)

        self.assertEqual(
            paths.codex_home("alpha-codex-worker"),
            paths.REPO_ROOT / "config" / "codex-home-alpha-codex-worker",
        )


class CodexOnboardingProvisioningTest(unittest.TestCase):
    def test_onboard_worker_codex_links_auth_to_shared_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            repo = _make_minimal_repo(tmp)
            worktree_root = tmp / "worktrees"
            worktree_root.mkdir()
            store = Store(tmp / "agent-comms.sqlite")
            store.register_actor(HUMAN_ID, "human", "alice")
            store.register_agent_actor(ARCHITECT_ID, "alpha", "architect", str(repo), [])
            shared_auth = tmp / "shared-auth" / "auth.json"
            shared_auth.parent.mkdir()
            shared_auth.write_text('{"shared": "auth"}')

            with mock.patch.object(paths, "codex_auth_source", return_value=shared_auth):
                result = onboard_worker(
                    store,
                    team="team-x",
                    runtime="codex",
                    actor_id="team-x-codex-worker",
                    project_root="/srv/team-x",
                    worktree_root=str(worktree_root),
                    repo_root=repo,
                    owner=ARCHITECT_ID,
                )

            target = Path(result["codex_home"]) / "auth.json"
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), shared_auth.resolve())
            actor = next(actor for actor in store.list_actors() if actor["id"] == "team-x-codex-worker")
            self.assertEqual(actor["spawn"]["env"]["CODEX_HOME"], result["codex_home"])

            missing_repo = _make_minimal_repo(tmp / "missing")
            missing_worktree_root = tmp / "missing-worktrees"
            missing_worktree_root.mkdir()
            missing_store = Store(tmp / "missing-agent-comms.sqlite")
            missing_store.register_actor(HUMAN_ID, "human", "alice")
            missing_store.register_agent_actor(ARCHITECT_ID, "alpha", "architect", str(missing_repo), [])
            missing_auth = tmp / "missing-auth" / "auth.json"
            missing_stderr = io.StringIO()

            with mock.patch.object(paths, "codex_auth_source", return_value=missing_auth):
                with contextlib.redirect_stderr(missing_stderr):
                    missing_result = onboard_worker(
                        missing_store,
                        team="team-x",
                        runtime="codex",
                        actor_id="team-x-missing-codex-worker",
                        project_root="/srv/team-y",
                        worktree_root=str(missing_worktree_root),
                        repo_root=missing_repo,
                        owner=ARCHITECT_ID,
                    )

            self.assertFalse((Path(missing_result["codex_home"]) / "auth.json").exists())
            self.assertIn("warning", missing_stderr.getvalue().lower())
            self.assertIn(str(missing_auth), missing_stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
