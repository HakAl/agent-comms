import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_comms import paths
from agent_comms.store import WORKER_DISPATCH_POLICY


def patch(*lines: str) -> str:
    return "\n".join(["*** Begin Patch", *lines, "*** End Patch"]) + "\n"


class CodexHookModeTest(unittest.TestCase):
    """The policy hook as a Codex worker runs it: policy, project root and
    fingerprint come from arguments, and allow prints nothing, since Codex
    reports a bare allow as a failed hook and runs the tool anyway."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = str(Path(self._temp.name, "project").resolve())
        Path(self.root).mkdir()
        self.digest = hashlib.sha256(paths.hooks_path().read_bytes()).hexdigest()

    def argv(self, **overrides: str) -> list[str]:
        values = {"runtime": "codex", "policy": WORKER_DISPATCH_POLICY, "project-root": self.root, "sha256": self.digest}
        values.update({key.replace("_", "-"): value for key, value in overrides.items()})
        argv = [sys.executable, str(paths.hooks_path())]
        for key, value in values.items():
            argv += [f"--{key}", value]
        return argv

    def _without(self, name: str) -> list[str]:
        words = self.argv()[2:]
        at = words.index(name)
        return words[:at] + words[at + 2:]

    def run_hook(self, payload: object, argv: list[str] | None = None) -> subprocess.CompletedProcess[str]:
        # No policy variables in the env: the arguments alone must carry them.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("WAKE_POLICY", "AGENT_COMMS_"))}
        return subprocess.run(
            argv or self.argv(), input=json.dumps(payload), text=True, capture_output=True, check=True, env=env,
        )

    def call(self, tool_name: str, tool_input: object, **fields: object) -> dict:
        # Codex sends every hook the turn's working directory as cwd.
        return {"hook_event_name": "PreToolUse", "cwd": self.root, "tool_name": tool_name, "tool_input": tool_input, **fields}

    def assert_allowed(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.stdout, "")

    def assert_denied(self, result: subprocess.CompletedProcess[str], reason: str = "") -> None:
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn(reason, output["permissionDecisionReason"])

    def test_an_allowed_call_prints_nothing(self) -> None:
        self.assert_allowed(self.run_hook(self.call("Bash", {"command": "ls"})))
        self.assert_allowed(self.run_hook(self.call("mcp__agent-comms__read_message", {"message_id": "m"})))

    def test_policy_comes_from_arguments_not_env(self) -> None:
        for tool_name in ("mcp__agent-comms__dispatch_agent", "mcp__agent_comms__cancel_dispatch"):
            with self.subTest(tool_name=tool_name):
                self.assert_denied(self.run_hook(self.call(tool_name, {})), "forbids dispatch authority")

    def test_network_writes_are_denied(self) -> None:
        self.assert_denied(self.run_hook(self.call("Bash", {"command": "git push origin main"})), "network")

    def test_a_fingerprint_mismatch_denies(self) -> None:
        result = self.run_hook(self.call("Bash", {"command": "ls"}), self.argv(sha256="0" * 64))
        self.assert_denied(result, "fingerprint mismatch")

    def test_bad_arguments_fail_closed(self) -> None:
        for argv in (
            self.argv(runtime="gemini"),
            self.argv(policy="no_such_policy"),
            [*self.argv(), "--unknown", "x"],
            self.argv()[:-1],
            # Codex hands the hook no policy env, so each argument is required.
            *[[*self.argv()[:2], *self._without(name)] for name in ("--policy", "--project-root", "--sha256")],
        ):
            with self.subTest(argv=argv[2:]):
                self.assert_denied(self.run_hook(self.call("Bash", {"command": "ls"}), argv), "failed closed")

    def test_patches_inside_the_project_are_allowed(self) -> None:
        body = patch(
            "*** Add File: new.txt", "+hi",
            f"*** Update File: {self.root}/a.txt", "@@", "-x", "+y",
            "*** Update File: b.txt", "*** Move to: sub/c.txt", "@@", "-x", "+y",
            "*** Delete File: old.txt",
        )
        self.assert_allowed(self.run_hook(self.call("apply_patch", {"command": body})))

    def test_patches_touching_files_outside_the_project_are_denied(self) -> None:
        outside = str(Path(self._temp.name, "outside.txt"))
        cases = {
            "absolute add": patch(f"*** Add File: {outside}", "+x"),
            "traversal": patch("*** Update File: ../outside.txt", "@@", "-x", "+y"),
            "delete": patch(f"*** Delete File: {outside}"),
            "move out": patch("*** Update File: a.txt", f"*** Move to: {outside}", "@@", "-x", "+y"),
            "second file": patch("*** Add File: ok.txt", "+x", "*** Add File: ../../etc/x", "+x"),
            "indented header": patch(f"  *** Add File: {outside}", "+x"),
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                self.assert_denied(self.run_hook(self.call("apply_patch", {"command": body})), "outside")

    def test_unreadable_patches_are_denied(self) -> None:
        for tool_input in (
            {"command": "*** Begin Patch\n*** End Patch\n"},
            {"command": patch("*** Add File: ", "+x")},
            {"command": patch("*** Add File: ok.txt", "+x", "*** Delete File:   ")},
            {"command": 3}, {}, "patch",
        ):
            with self.subTest(tool_input=tool_input):
                self.assert_denied(self.run_hook(self.call("apply_patch", tool_input)), "apply_patch")

    def test_relative_patch_paths_resolve_against_the_turn_directory(self) -> None:
        # Codex resolves a relative header against the turn's cwd, not the
        # registered root (Lookout INTEGRATION-007 F2).
        sub, outside = Path(self.root, "sub"), Path(self._temp.name, "outside")
        sub.mkdir()
        outside.mkdir()
        self.assert_allowed(self.run_hook(self.call("apply_patch", {"command": patch("*** Add File: ../in.txt", "+x")}, cwd=str(sub))))
        self.assert_denied(
            self.run_hook(self.call("apply_patch", {"command": patch("*** Add File: proof.txt", "+x")}, cwd=str(outside))),
            "outside",
        )

    def test_patches_whose_directory_the_hook_cannot_establish_are_denied(self) -> None:
        body = patch("*** Add File: new.txt", "+x")
        for name, payload in {
            "no cwd": {key: value for key, value in self.call("apply_patch", {"command": body}).items() if key != "cwd"},
            "relative cwd": self.call("apply_patch", {"command": body}, cwd="sub"),
            "non-text cwd": self.call("apply_patch", {"command": body}, cwd=3),
            "other environment": self.call(
                "apply_patch", {"command": patch("*** Environment ID: remote", "*** Add File: new.txt", "+x")}
            ),
        }.items():
            with self.subTest(name=name):
                self.assert_denied(self.run_hook(payload), "apply_patch")


if __name__ == "__main__":
    unittest.main()
