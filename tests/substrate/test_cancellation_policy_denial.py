"""Stage-2 T1: bounded workers and the operator mailbox can neither discover nor
call the scoped ``cancel_dispatch`` MCP tool.

Two independent denial layers are proven here without any live MCP process:

* policy *compilation* -- ``cancel_dispatch`` is in ``mcp_denied_tools`` and every
  registered hook/tool-name variant is in ``hook_denied_tools`` for BOTH the
  worker and operator-mailbox policies, and it is absent from either policy's
  ``mcp_allowed_tools`` (so the scoped server never registers it);
* the runtime *hook* (``pre_tool_use``) denies each of the three supported
  cancel_dispatch tool-name variants under either policy, matching the
  ``dispatch_agent`` exact-name denial, while an unrelated server's own
  cancel_dispatch tool is left untouched by the cancel-dispatch rule.

The real MCP stdio discovery/call denial is proven separately in
``test_cancellation_mcp_surface.py``.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import subprocess
import sys
import tempfile
import unittest

from agent_comms.policies import OPERATOR_MAILBOX_POLICY, compile_policy
from agent_comms.store import WORKER_DISPATCH_POLICY

# Every tool-name spelling a runtime hook can see for the scoped tool: the bare
# FastMCP name plus both server-namespaced spellings the client emits.
CANCEL_TOOL_VARIANTS = (
    "cancel_dispatch",
    "mcp__agent_comms__cancel_dispatch",
    "mcp__agent-comms__cancel_dispatch",
)


class CancelDispatchPolicyCompilationTest(unittest.TestCase):
    def test_worker_policy_denies_and_never_allows_cancel_dispatch(self) -> None:
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        self.assertIn("cancel_dispatch", policy.mcp_denied_tools)
        self.assertNotIn("cancel_dispatch", policy.mcp_allowed_tools)
        for variant in CANCEL_TOOL_VARIANTS:
            with self.subTest(variant=variant):
                self.assertIn(variant, policy.hook_denied_tools)

    def test_operator_mailbox_policy_denies_and_never_allows_cancel_dispatch(self) -> None:
        policy = compile_policy(OPERATOR_MAILBOX_POLICY)
        self.assertIn("cancel_dispatch", policy.mcp_denied_tools)
        self.assertNotIn("cancel_dispatch", policy.mcp_allowed_tools)
        for variant in CANCEL_TOOL_VARIANTS:
            with self.subTest(variant=variant):
                self.assertIn(variant, policy.hook_denied_tools)

    def test_cancel_dispatch_denial_matches_dispatch_agent_shape(self) -> None:
        # The new surface must be denied with exactly the same breadth as the
        # existing dispatch authority tool -- bare name in mcp_denied_tools and
        # all three name spellings in hook_denied_tools -- for both policies.
        for name in (WORKER_DISPATCH_POLICY, OPERATOR_MAILBOX_POLICY):
            policy = compile_policy(name)
            with self.subTest(policy=name):
                self.assertLessEqual({"dispatch_agent", "cancel_dispatch"}, policy.mcp_denied_tools)
                self.assertLessEqual(set(CANCEL_TOOL_VARIANTS), policy.hook_denied_tools)


class CancelDispatchHookDenialTest(unittest.TestCase):
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

    def _env(self, policy_name: str, project_root: str) -> dict[str, str]:
        policy = compile_policy(policy_name)
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

    def test_hook_denies_every_cancel_variant_under_both_policies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            for policy_name in (WORKER_DISPATCH_POLICY, OPERATOR_MAILBOX_POLICY):
                env = self._env(policy_name, temp_dir)
                for tool_name in CANCEL_TOOL_VARIANTS:
                    with self.subTest(policy=policy_name, tool_name=tool_name):
                        output = self.run_hook({"tool_name": tool_name, "tool_input": {}}, env)
                        self.assert_denied(output)
                        self.assertIn(
                            "forbids dispatch",
                            output["hookSpecificOutput"]["permissionDecisionReason"],
                        )

    def test_hook_does_not_deny_unrelated_server_cancel_variant(self) -> None:
        # The cancel-dispatch denial is exact-name only (the three supported
        # spellings). An unrelated MCP server's cancel_dispatch tool is NOT
        # caught by the cancel-dispatch rule, and with no other rule applying to
        # it the hook allows it -- the narrow-denial correction this test guards.
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self._env(WORKER_DISPATCH_POLICY, temp_dir)
            output = self.run_hook(
                {"tool_name": "mcp__some-other-server__cancel_dispatch", "tool_input": {}}, env
            )
            self.assert_allowed(output)


if __name__ == "__main__":
    unittest.main()
