import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import paths
from agent_comms.cli import bootstrap_store, load_actor_config
from agent_comms.policies import compile_policy
from agent_comms.schema import ValidationError
from agent_comms.adapters import DispatchContext
from agent_comms.adapters.claude import ClaudeAdapter
from agent_comms.store import Store, WORKER_DISPATCH_POLICY

ROOT = Path(__file__).resolve().parents[2]


def _context(recipient: dict, db_path: Path) -> DispatchContext:
    return DispatchContext(
        dispatch={"dispatch_id": "dispatch-test", "policy_name": WORKER_DISPATCH_POLICY},
        recipient=recipient,
        message={"id": "msg-test"},
        ttl_seconds=45,
        expected_close_by="2026-01-01T00:00:00Z",
        db_path=str(db_path),
    )


class ActorConfigTest(unittest.TestCase):
    def test_bootstrap_loads_canonical_actors_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "actors.json"
            config.write_text(
                json.dumps(
                    {
                        "actors": {
                            "01M36YTJV9XBW95S6ZWV47C4RG": {"kind": "human", "display_name": "alice"},
                            "alpha-architect": {
                                "kind": "agent",
                                "display_name": "alpha-architect",
                                "team": "alpha",
                                "role": "architect",
                                "project_root": str(root),
                                "capabilities": ["signal-design"],
                            },
                        }
                    }
                )
            )
            store = Store(root / "agent-comms.sqlite")

            registered = bootstrap_store(store, config)

            self.assertEqual({row["actor_id"] for row in registered if "actor_id" in row}, {"01M36YTJV9XBW95S6ZWV47C4RG"})
            self.assertEqual({row["agent_id"] for row in registered if "agent_id" in row}, {"alpha-architect"})
            actors = {actor["id"]: actor for actor in store.list_actors()}
            self.assertEqual(actors["01M36YTJV9XBW95S6ZWV47C4RG"]["kind"], "human")
            self.assertEqual(actors["alpha-architect"]["role"], "architect")
            self.assertIsNone(actors["alpha-architect"]["runtime"])
            self.assertEqual(actors["alpha-architect"]["spawn"], {})

    def test_missing_registry_fails_loudly_instead_of_registering_nobody(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ValidationError, "actor registry not found at .*actors.example.json"):
                load_actor_config(root / "actors.json")
            # A checkout that still has the pre-move registry gets told where it went.
            checkout = root / "checkout"
            (checkout / "config").mkdir(parents=True)
            (checkout / "config" / "actors.json").write_text("{}")
            with mock.patch.object(paths, "REPO_ROOT", checkout):
                with self.assertRaisesRegex(ValidationError, "copy .*checkout/config/actors.json there or pass --config"):
                    load_actor_config(root / "actors.json")

    def test_legacy_agents_json_is_normalized_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "agents.json"
            canonical = root / "actors.json"
            legacy.write_text(
                json.dumps(
                    {
                        "agents": {
                            "alpha-architect": {
                                "team": "alpha",
                                "role": "architect",
                                "project_root": str(root),
                                "capabilities": ["signal-design"],
                            }
                        }
                    }
                )
            )

            config = load_actor_config(canonical)

            self.assertTrue(canonical.exists())
            self.assertEqual(config["actors"]["alpha-architect"]["kind"], "agent")
            normalized = json.loads(canonical.read_text())
            self.assertIn("alpha-architect", normalized["actors"])

    def test_bootstrap_registers_owners_before_workers_whatever_the_file_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "actors.json"
            config.write_text(
                json.dumps(
                    {
                        "actors": {
                            "a-worker": {"kind": "agent", "display_name": "a-worker", "team": "t", "role": "worker", "runtime": "fake", "owner": "z-architect", "project_root": str(root), "capabilities": []},
                            "z-architect": {"kind": "agent", "display_name": "z-architect", "team": "t", "role": "architect", "project_root": str(root), "capabilities": []},
                        }
                    }
                )
            )
            store = Store(root / "agent-comms.sqlite")
            registered = bootstrap_store(store, config)
            self.assertEqual([row["agent_id"] for row in registered], ["z-architect", "a-worker"])
            self.assertEqual({actor["id"] for actor in store.list_actors()}, {"a-worker", "z-architect"})

    def test_non_agent_actor_without_display_name_is_refused_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "actors.json"
            config.write_text(json.dumps({"actors": {"01M36YTJV9XBW95S6ZWV47C4RG": {"kind": "human"}}}))
            with self.assertRaisesRegex(ValidationError, "human actor 01M36YTJV9XBW95S6ZWV47C4RG requires display_name"):
                bootstrap_store(Store(root / "agent-comms.sqlite"), config)

    def test_non_agent_actor_with_spawn_metadata_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "actors.json"
            config.write_text(
                json.dumps(
                    {
                        "actors": {
                            "01M36YTJV9XBW95S6ZWV47C4RG": {
                                "kind": "human",
                                "display_name": "alice",
                                "spawn": {"command": "claude"},
                            }
                        }
                    }
                )
            )
            store = Store(root / "agent-comms.sqlite")

            with self.assertRaisesRegex(ValidationError, "must not define: spawn"):
                bootstrap_store(store, config)

    def test_cli_bootstrap_warns_on_legacy_agents_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "agents.json"
            canonical = root / "actors.json"
            legacy.write_text(
                json.dumps(
                    {
                        "agents": {
                            "alpha-architect": {
                                "team": "alpha",
                                "role": "architect",
                                "project_root": str(root),
                                "capabilities": [],
                            }
                        }
                    }
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_comms.cli",
                    "--db",
                    str(root / "agent-comms.sqlite"),
                    "bootstrap",
                    "--config",
                    str(canonical),
                ],
                text=True,
                capture_output=True,
                check=True,
            )

            self.assertIn("deprecated", result.stderr)
            self.assertTrue(canonical.exists())
            self.assertIn("alpha-architect", result.stdout)

    @mock.patch.dict(
        os.environ,
        {
            "PROJECT_A_ROOT": "/srv/project-a",
            "PROJECT_C_ROOT": "/srv/team-c",
        },
    )
    def test_bootstrap_loads_real_canonical_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "agent-comms.sqlite")

            registered = bootstrap_store(store, ROOT / "tests" / "fixtures" / "roster.json")

            self.assertEqual(len(registered), 12)
            actors = {actor["id"]: actor for actor in store.list_actors()}
            self.assertEqual(actors["01M36YTJV9XBW95S6ZWV47C4RG"]["display_name"], "alice")
            self.assertEqual(actors["delta-architect"]["role"], "architect")
            self.assertEqual(actors["delta-architect"]["team"], "delta")
            self.assertEqual(actors["delta-claude-worker"]["runtime"], "claude")
            self.assertTrue(actors["delta-architect"]["project_root"].endswith("/Documents/delta"))
            self.assertEqual(actors["01M36YTJV9XBW95S6ZWV47C4RG"]["kind"], "human")
            self.assertEqual(actors["alpha-architect"]["kind"], "agent")
            self.assertEqual(actors["alpha-fake-worker"]["runtime"], "fake")
            self.assertEqual(actors["alpha-claude-worker"]["runtime"], "claude")
            self.assertEqual(actors["alpha-codex-worker"]["runtime"], "codex")
            self.assertEqual(actors["team-c-architect"]["kind"], "agent")
            self.assertEqual(actors["team-c-architect"]["role"], "architect")
            self.assertEqual(actors["team-c-architect"]["team"], "team-c")
            self.assertEqual(actors["team-c-codex-worker"]["runtime"], "codex")
            self.assertEqual(actors["team-c-codex-worker"]["role"], "worker")

            claude_args = actors["alpha-claude-worker"]["spawn"]["args"]
            settings_arg = claude_args[claude_args.index("--settings") + 1]
            self.assertEqual(settings_arg, "{claude_settings}")
            adapter = ClaudeAdapter()
            policy = compile_policy(WORKER_DISPATCH_POLICY)
            resolved_args = adapter._resolved_spawn_args(
                _context(actors["alpha-claude-worker"], Path(temp_dir) / "agent-comms.sqlite"),
                actors["alpha-claude-worker"]["spawn"],
                policy,
            )
            settings = json.loads(resolved_args[resolved_args.index("--settings") + 1])
            allowed_tools = settings["permissions"]["allow"]
            self.assertEqual(
                allowed_tools,
                [f"mcp__agent-comms__{tool_name}" for tool_name in sorted(policy.mcp_allowed_tools)]
                + sorted(policy.builtin_allowed_tools),
            )
            self.assertNotIn("mcp__agent-comms__dispatch_agent", allowed_tools)
            self.assertNotIn("mcp__agent-comms__register_actor", allowed_tools)
            self.assertNotIn("mcp__agent-comms__register_agent", allowed_tools)

    def test_bootstrap_codex_custody_provisions_the_worker_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            custody = root / "custody"
            source = root / "shared" / "auth.json"  # absent: the link dangles until codex login
            config = root / "actors.json"
            config.write_text(
                json.dumps(
                    {
                        "actors": {
                            "01M36YTJV9XBW95S6ZWV47C4RG": {"kind": "human", "display_name": "alice"},
                            "alpha-architect": {"kind": "agent", "display_name": "alpha-architect", "team": "alpha", "role": "architect", "project_root": str(root), "capabilities": []},
                            "alpha-codex-worker": {"kind": "agent", "display_name": "alpha-codex-worker", "team": "alpha", "role": "worker", "owner": "alpha-architect", "runtime": "codex", "project_root": str(root), "capabilities": []},
                        }
                    }
                )
            )
            with mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_CUSTODY_ROOT": str(custody)}), \
                    mock.patch.object(paths, "runtime_codex_auth_source", return_value=source):
                explicit = Store(root / "explicit.sqlite")
                bootstrap_store(explicit, config, codex_custody=True)
                home = custody / "default" / "alpha-codex-worker"
                actors = {actor["id"]: actor for actor in explicit.list_actors()}
                self.assertEqual(actors["alpha-codex-worker"]["spawn"]["env"]["CODEX_HOME"], str(home))
                self.assertTrue((home / "config.toml").is_file())
                self.assertTrue((home / "alpha-codex-worker.config.toml").is_file())
                self.assertTrue((home / "auth.json").is_symlink())
                self.assertFalse((home / "auth.json").exists())
                self.assertEqual(os.readlink(home / "auth.json"), str(source.resolve()))
                # SETUP-002 F1: the worker's only ledger binding is its config.toml.
                args = tomllib.loads((home / "config.toml").read_text())["mcp_servers"]["agent-comms"]["args"]
                self.assertEqual(args, ["--actor-id", "alpha-codex-worker", "--db", os.path.abspath(str(root / "explicit.sqlite"))])
                # An explicit ledger keeps the legacy placeholder unless told otherwise.
                legacy = Store(root / "legacy.sqlite")
                bootstrap_store(legacy, config)
                actors = {actor["id"]: actor for actor in legacy.list_actors()}
                self.assertEqual(actors["alpha-codex-worker"]["spawn"]["env"]["CODEX_HOME"], "{codex_home}")
                # The default ledger derives custody, so bootstrap after setup does not downgrade the worker.
                default = Store(root / "default.sqlite", is_default_db_open=True)
                bootstrap_store(default, config)
                actors = {actor["id"]: actor for actor in default.list_actors()}
                self.assertEqual(actors["alpha-codex-worker"]["spawn"]["env"]["CODEX_HOME"], str(home))
                args = tomllib.loads((home / "config.toml").read_text())["mcp_servers"]["agent-comms"]["args"]
                self.assertEqual(args, ["--actor-id", "alpha-codex-worker"])
                with contextlib.redirect_stderr(io.StringIO()):
                    bootstrap_store(default, config, codex_custody=False, override_protected="test: legacy form")
                actors = {actor["id"]: actor for actor in default.list_actors()}
                self.assertEqual(actors["alpha-codex-worker"]["spawn"]["env"]["CODEX_HOME"], "{codex_home}")

    def test_register_actor_rejects_dispatch_cap_below_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")

            with self.assertRaises(ValidationError) as raised:
                store.register_actor(
                    "test-arch",
                    "agent",
                    "test-arch",
                    team="t",
                    role="architect",
                    project_root=str(root),
                    capabilities=[],
                    dispatch_cap=0,
                )

            self.assertIn("dispatch_cap", str(raised.exception))
            self.assertIn("at least 1", str(raised.exception))
            store.register_actor(
                "test-arch",
                "agent",
                "test-arch",
                team="t",
                role="architect",
                project_root=str(root),
                capabilities=[],
                dispatch_cap=1,
            )


if __name__ == "__main__":
    unittest.main()
