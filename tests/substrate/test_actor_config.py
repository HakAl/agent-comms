import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
