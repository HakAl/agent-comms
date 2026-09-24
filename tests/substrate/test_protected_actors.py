from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import argparse
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_comms.cli import build_parser, bootstrap_store
from agent_comms.cli.commands import admin, provision_codex_home, register
from agent_comms.onboarding import onboard_worker
from agent_comms.schema import ValidationError
from agent_comms.store import Store, WORKER_DISPATCH_POLICY

ROOT = Path(__file__).resolve().parents[2]
# Stands in for the canonical roster so tests never read a real one.
DEFAULT_CONFIG = ROOT / "tests" / "fixtures" / "roster.json"
HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"


def _columns(db_path: Path, table: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return {row["name"]: dict(row) for row in conn.execute(f"pragma table_info({table})").fetchall()}
    finally:
        conn.close()


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "README.md").write_text("fixture\n")
    shutil.copytree(
        ROOT / "agent_comms",
        repo / "agent_comms",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _git(repo, "add", "README.md", "agent_comms")
    _git(repo, "-c", "user.name=Test User", "-c", "user.email=test@example.invalid", "commit", "-m", "init")
    return repo


def _seed_human_and_agents(store: Store, root: Path, *, protected_worker: bool = False) -> None:
    store.register_actor(HUMAN_ID, "human", "alice")
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "architect"), [])
    store.register_agent_actor(
        "alpha-worker",
        "alpha",
        "worker",
        str(root / "worker"),
        [],
        runtime="fake",
        spawn={},
        protected=protected_worker,
        owner="alpha-architect",
    )


class ProtectedActorsTest(unittest.TestCase):
    def test_t1_migration_adds_protected_column_with_default_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fresh_db = root / "fresh.sqlite"
            Store(fresh_db).init()
            columns = _columns(fresh_db, "actors")
            self.assertIn("protected", columns)
            self.assertEqual(columns["protected"]["type"].lower(), "integer")
            self.assertEqual(columns["protected"]["notnull"], 1)
            self.assertEqual(int(columns["protected"]["dflt_value"]), 0)

            precolumn_db = root / "precolumn.sqlite"
            conn = sqlite3.connect(precolumn_db)
            conn.executescript(
                """
                create table agents(
                  id text primary key,
                  team text not null,
                  role text not null,
                  project_root text not null,
                  capabilities_json text not null,
                  last_seen_at text not null
                );
                create table actors(
                  id text primary key,
                  kind text not null,
                  display_name text not null,
                  system_class text,
                  system_instance text,
                  project_root text,
                  runtime text,
                  spawn_json text,
                  capabilities_json text not null default '[]',
                  team text,
                  role text,
                  last_seen_at text not null,
                  dispatch_cap integer not null default 4
                );
                """
            )
            conn.commit()
            conn.close()

            Store(precolumn_db).init()
            Store(precolumn_db).init()
            columns = _columns(precolumn_db, "actors")
            self.assertIn("protected", columns)
            self.assertEqual(int(columns["protected"]["dflt_value"]), 0)

    @mock.patch.dict(
        os.environ,
        {
            "PROJECT_A_ROOT": "/srv/project-a",
            "PROJECT_C_ROOT": "/srv/team-c",
            "AGENT_COMMS_ROOT": "/srv/agent-comms",
        },
    )
    def test_t2_bootstrap_default_true_explicit_false_and_non_agent_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "actors.json"
            config.write_text(
                json.dumps(
                    {
                        "actors": {
                            "test-architect": {
                                "kind": "agent",
                                "team": "test",
                                "role": "architect",
                                "project_root": str(root),
                            },
                            "default-agent": {
                                "kind": "agent",
                                "team": "test",
                                "role": "worker",
                                "project_root": str(root),
                                "owner": "test-architect",
                            },
                            "explicit-false": {
                                "kind": "agent",
                                "team": "test",
                                "role": "worker",
                                "project_root": str(root),
                                "protected": False,
                                "owner": "test-architect",
                            },
                            "explicit-true": {
                                "kind": "agent",
                                "team": "test",
                                "role": "worker",
                                "project_root": str(root),
                                "protected": True,
                                "owner": "test-architect",
                            },
                        }
                    }
                )
            )
            store = Store(root / "agent-comms.sqlite")
            bootstrap_store(store, config)
            actors = {actor["id"]: actor for actor in store.list_actors()}
            self.assertTrue(actors["default-agent"]["protected"])
            self.assertFalse(actors["explicit-false"]["protected"])
            self.assertTrue(actors["explicit-true"]["protected"])

            bad_config = root / "bad.json"
            bad_config.write_text(json.dumps({"actors": {HUMAN_ID: {"kind": "human", "display_name": "alice", "protected": True}}}))
            with self.assertRaisesRegex(ValidationError, "protected"):
                bootstrap_store(Store(root / "bad.sqlite"), bad_config)

            real_store = Store(root / "real.sqlite")
            bootstrap_store(real_store, ROOT / "tests" / "fixtures" / "roster.json")
            real_agents = [actor for actor in real_store.list_actors() if actor["kind"] == "agent"]
            canonical_agent_count = sum(
                1 for entry in json.loads((ROOT / "tests" / "fixtures" / "roster.json").read_text())["actors"].values()
                if entry.get("kind", "agent") == "agent"
            )
            self.assertEqual(len(real_agents), canonical_agent_count)
            self.assertTrue(all(actor["protected"] for actor in real_agents))

    def test_t3_register_preserves_and_explicitly_sets_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            store.register_agent_actor("alpha-architect", "alpha", "architect", str(root), [])
            store.register_agent_actor("alpha-worker", "alpha", "worker", str(root), [], protected=True, owner="alpha-architect")
            store.register_agent_actor("alpha-worker", "alpha", "worker", str(root / "again"), [], owner="alpha-architect")
            self.assertTrue(store.actor_protection("alpha-worker")["protected"])

            store.register_agent_actor("alpha-worker", "alpha", "worker", str(root / "again"), [], protected=False, owner="alpha-architect")
            self.assertFalse(store.actor_protection("alpha-worker")["protected"])
            store.register_agent_actor("alpha-worker", "alpha", "worker", str(root / "again"), [], protected=True, owner="alpha-architect")
            self.assertTrue(store.actor_protection("alpha-worker")["protected"])

            store.register_agent_actor("fresh-worker", "alpha", "worker", str(root), [], owner="alpha-architect")
            self.assertFalse(store.actor_protection("fresh-worker")["protected"])

    def test_t4_provision_guard_override_unregistered_and_required_actor_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            store.register_agent_actor("alpha-architect", "alpha", "architect", str(root), [])
            store.register_agent_actor("alpha-worker", "alpha", "worker", str(root), [], protected=True, owner="alpha-architect")

            args = SimpleNamespace(
                actor_id="alpha-worker",
                project_root=str(root),
                codex_home=str(root / "codex-home"),
                override_protected=None,
            )
            with self.assertRaisesRegex(ValidationError, "alpha-worker.*alpha.*--override-protected"):
                provision_codex_home.handle(store, args)
            self.assertFalse((root / "codex-home").exists())

            args.override_protected = "repair auth"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = provision_codex_home.handle(store, args)
            self.assertIn("repair auth", stderr.getvalue())
            self.assertEqual(result["override_protected"]["actor_id"], "alpha-worker")
            self.assertIn(str(root / "codex-home" / "config.toml"), result["written"])

            args.actor_id = "unregistered-worker"
            args.codex_home = str(root / "unregistered-home")
            args.override_protected = None
            result = provision_codex_home.handle(store, args)
            self.assertNotIn("override_protected", result)

            with self.assertRaises(SystemExit):
                build_parser().parse_args(["provision-codex-home", "--project-root", str(root)])

    def test_t5_onboard_guard_override_and_new_worker_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = _make_repo(root)
            worktree_root = root / "worktrees"
            worktree_root.mkdir()
            store = Store(root / "agent-comms.sqlite")
            store.register_agent_actor("alpha-architect", "alpha", "architect", str(repo), [])
            store.register_agent_actor("alpha-worker", "alpha", "worker", str(repo), [], protected=True, owner="alpha-architect")

            with self.assertRaisesRegex(ValidationError, "alpha-worker.*alpha.*--override-protected"):
                onboard_worker(
                    store,
                    team="alpha",
                    runtime="fake",
                    actor_id="alpha-worker",
                    worktree_root=str(worktree_root),
                    repo_root=repo,
                    owner="alpha-architect",
                )
            self.assertFalse((worktree_root / "agent-comms-alpha-worker").exists())

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                override = onboard_worker(
                    store,
                    team="alpha",
                    runtime="fake",
                    actor_id="alpha-worker",
                    worktree_root=str(worktree_root),
                    repo_root=repo,
                    override_protected="replace worker",
                    owner="alpha-architect",
                )
            self.assertIn("replace worker", stderr.getvalue())
            self.assertEqual(override["override_protected"]["actor_id"], "alpha-worker")

            new_result = onboard_worker(
                store,
                team="alpha",
                runtime="fake",
                actor_id="alpha-new-worker",
                worktree_root=str(worktree_root),
                repo_root=repo,
                owner="alpha-architect",
            )
            self.assertEqual(new_result["actor_id"], "alpha-new-worker")
            self.assertTrue(store.actor_protection("alpha-new-worker")["protected"])

    def test_t6_register_guard_override_and_fresh_register_default_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            store.register_agent_actor("alpha-architect", "alpha", "architect", str(root), [])
            store.register_agent_actor("alpha-worker", "alpha", "worker", str(root), [], protected=True, owner="alpha-architect")
            args = SimpleNamespace(
                agent_id="alpha-worker",
                team="alpha",
                role="worker",
                project_root=str(root / "again"),
                capability=[],
                runtime=None,
                spawn=None,
                spawn_json=None,
                override_protected=None,
                owner="alpha-architect",
            )
            with self.assertRaisesRegex(ValidationError, "alpha-worker.*alpha.*--override-protected"):
                register.handle(store, args)

            args.override_protected = "rename root"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = register.handle(store, args)
            self.assertIn("rename root", stderr.getvalue())
            self.assertEqual(result["override_protected"]["actor_id"], "alpha-worker")
            self.assertTrue(store.actor_protection("alpha-worker")["protected"])

            args.agent_id = "fresh-worker"
            args.override_protected = None
            result = register.handle(store, args)
            self.assertNotIn("override_protected", result)
            self.assertFalse(store.actor_protection("fresh-worker")["protected"])

    @mock.patch.dict(
        os.environ,
        {
            "PROJECT_A_ROOT": "/srv/project-a",
            "PROJECT_C_ROOT": "/srv/team-c",
        },
    )
    @mock.patch("agent_comms.cli._helpers.DEFAULT_CONFIG", DEFAULT_CONFIG)
    def test_t7_canonical_bootstrap_exempt_noncanonical_requires_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            store.register_agent_actor("alpha-architect", "old", "architect", str(root), [], protected=True)

            bootstrap_store(store, DEFAULT_CONFIG)
            self.assertEqual(store.actor_protection("alpha-architect")["team"], "alpha")

            copy_config = root / "actors-copy.json"
            copy_config.write_text(DEFAULT_CONFIG.read_text())
            with self.assertRaisesRegex(ValidationError, "--override-protected"):
                bootstrap_store(store, copy_config)

            overrides = []
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                bootstrap_store(store, copy_config, override_protected="refresh from copy", override_records=overrides)
            self.assertIn("refresh from copy", stderr.getvalue())
            self.assertTrue(any(record["actor_id"] == "alpha-architect" for record in overrides))

    def test_t8_a1_notice_only_and_a2_dispatch_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            _seed_human_and_agents(store, root, protected_worker=True)
            args = SimpleNamespace(
                admin_command="dispatch",
                from_actor_id=HUMAN_ID,
                target_actor_id="alpha-worker",
                idempotency_key="admin-1",
                requested_policy=WORKER_DISPATCH_POLICY,
                override_reason="operator reason",
                subject="subject",
                body="body",
                ref=[],
                ref_summary=[],
            )
            dispatch_result = {"status": "queued", "recipient_actor_id": "alpha-worker"}
            with mock.patch.object(admin, "require_admin_credential"), mock.patch.object(
                store,
                "dispatch_agent",
                return_value=dispatch_result,
            ):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    result = admin.handle(store, args)
            self.assertEqual(result, dispatch_result)
            self.assertIn("alpha-worker", stderr.getvalue())
            self.assertIn("alpha", stderr.getvalue())

            store.register_agent_actor(
                "echo-worker", "echo", "worker", str(root / "echo"), [],
                runtime="fake", spawn={}, owner="alpha-architect",
            )
            args.target_actor_id = "echo-worker"
            with mock.patch.object(admin, "require_admin_credential"), mock.patch.object(
                store,
                "dispatch_agent",
                return_value=dispatch_result,
            ):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    admin.handle(store, args)
            out = stderr.getvalue()
            self.assertNotIn("protected actor notice:", out)
            self.assertNotIn("dispatch target echo-worker", out)

            a2 = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "a2-protected",
                "subject",
                "body",
                [],
            )
            self.assertEqual(a2["status"], "queued")
            self.assertEqual(a2["recipient_actor_id"], "alpha-worker")


if __name__ == "__main__":
    unittest.main()
