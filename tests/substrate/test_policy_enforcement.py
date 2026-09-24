import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_comms.policies import OPERATOR_MAILBOX_POLICY, compile_policy
from agent_comms.store import WORKER_DISPATCH_POLICY


class PolicyEnforcementTest(unittest.TestCase):
    def run_hook(self, payload: dict, env: dict[str, str]) -> dict:
        result = subprocess.run(
            [sys.executable, "-m", "agent_comms.hooks.pre_tool_use"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
            env=env,
        )
        return json.loads(result.stdout)

    def policy_env(self, project_root: str) -> dict[str, str]:
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        env = os.environ.copy()
        env.update(policy.env)
        env["AGENT_COMMS_PROJECT_ROOT"] = project_root
        return env

    def operator_policy_env(self, project_root: str) -> dict[str, str]:
        policy = compile_policy(OPERATOR_MAILBOX_POLICY)
        env = os.environ.copy()
        env.update(policy.env)
        env["AGENT_COMMS_PROJECT_ROOT"] = project_root
        return env

    def assert_denied(self, output: dict) -> None:
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def assert_allowed(self, output: dict) -> None:
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_hook_denies_worker_dispatch_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            for tool_name in (
                "mcp__agent_comms__dispatch_agent",
                "mcp__agent-comms__dispatch_agent",
                "mcp__agent_comms__register_actor",
                "mcp__agent-comms__register_actor",
                "mcp__agent_comms__register_agent",
                "mcp__agent-comms__register_agent",
            ):
                with self.subTest(tool_name=tool_name):
                    output = self.run_hook(
                        {"tool_name": tool_name, "tool_input": {}},
                        self.policy_env(temp_dir),
                    )

                    self.assert_denied(output)
                    self.assertIn("forbids dispatch", output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_hook_denies_operator_mailbox_dispatch_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = self.run_hook(
                {"tool_name": "mcp__agent-comms__dispatch_agent", "tool_input": {}},
                self.operator_policy_env(temp_dir),
            )

            self.assert_denied(output)
            self.assertIn("operator_mailbox forbids dispatch", output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_hook_bounds_file_tools_to_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inside = root / "src" / "x.py"
            outside = root.parent / "outside.py"

            for tool_name in ("Read", "Edit", "MultiEdit", "Write"):
                with self.subTest(tool_name=tool_name, path="inside"):
                    self.assert_allowed(
                        self.run_hook(
                            {"tool_name": tool_name, "tool_input": {"file_path": str(inside)}},
                            self.policy_env(str(root)),
                        )
                    )
                with self.subTest(tool_name=tool_name, path="outside"):
                    output = self.run_hook(
                        {"tool_name": tool_name, "tool_input": {"file_path": str(outside)}},
                        self.policy_env(str(root)),
                    )
                    self.assert_denied(output)
                    self.assertIn(
                        "forbids file access outside",
                        output["hookSpecificOutput"]["permissionDecisionReason"],
                    )

    def test_hook_bounds_file_tools_with_path_input_to_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inside = root / "src"
            outside = root.parent / "outside"

            for tool_name in ("Read", "Edit", "MultiEdit"):
                with self.subTest(tool_name=tool_name, path_key="path", path="inside"):
                    self.assert_allowed(
                        self.run_hook(
                            {"tool_name": tool_name, "tool_input": {"path": str(inside)}},
                            self.policy_env(str(root)),
                        )
                    )
                with self.subTest(tool_name=tool_name, path_key="path", path="outside"):
                    self.assert_denied(
                        self.run_hook(
                            {"tool_name": tool_name, "tool_input": {"path": str(outside)}},
                            self.policy_env(str(root)),
                        )
                    )

    def test_hook_denies_network_write_bash_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assert_denied(
                self.run_hook(
                    {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
                    self.policy_env(temp_dir),
                )
            )
            self.assert_denied(
                self.run_hook(
                    {"tool_name": "Bash", "tool_input": {"command": "curl -X POST https://example.test"}},
                    self.policy_env(temp_dir),
                )
            )
            self.assert_allowed(
                self.run_hook(
                    {"tool_name": "Bash", "tool_input": {"command": "git status --short"}},
                    self.policy_env(temp_dir),
                )
            )

    def test_t1_hook_allows_heredoc_with_apostrophe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assert_allowed(
                self.run_hook(
                    {
                        "tool_name": "Bash",
                        "tool_input": {
                            "command": "cat > notes.py <<'EOF'\n# key order: it's fixed\nEOF"
                        },
                    },
                    self.policy_env(temp_dir),
                )
            )

    def test_t2_hook_allows_unbalanced_quote_without_network_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assert_allowed(
                self.run_hook(
                    {
                        "tool_name": "Bash",
                        "tool_input": {"command": 'echo "unterminated'},
                    },
                    self.policy_env(temp_dir),
                )
            )

    def test_t3_hook_denies_network_patterns_in_unparseable_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            for command in (
                "cat > notes.py <<'EOF'\n# key order: it's fixed\nEOF\ngit push origin main",
                "curl -X POST https://example.test -d 'it",
            ):
                with self.subTest(command=command):
                    self.assert_denied(
                        self.run_hook(
                            {"tool_name": "Bash", "tool_input": {"command": command}},
                            self.policy_env(temp_dir),
                        )
                    )

    def test_t4_hook_denies_existing_network_write_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            for command in (
                "gh pr create",
                "gh release create --draft v1",
                "git push",
                "curl --request PUT https://example.test",
                "rsync -a . host:/x",
            ):
                with self.subTest(command=command):
                    self.assert_denied(
                        self.run_hook(
                            {"tool_name": "Bash", "tool_input": {"command": command}},
                            self.policy_env(temp_dir),
                        )
                    )

    def test_hook_fails_closed_on_fingerprint_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.policy_env(temp_dir)
            env["AGENT_COMMS_POLICY_HOOK_SHA256"] = "not-the-hook"

            output = self.run_hook({"tool_name": "Read", "tool_input": {}}, env)

            self.assert_denied(output)
            self.assertIn("fingerprint mismatch", output["hookSpecificOutput"]["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()
