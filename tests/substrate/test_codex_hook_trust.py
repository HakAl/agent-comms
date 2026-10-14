import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

from agent_comms import codex_hook
from agent_comms.store import WORKER_DISPATCH_POLICY

# Seen live: custody codex 0.160.0 `hooks/list` reported this currentHash for
# the probe command with timeout 30 (local/plans/ac-4ao.10.2-slice3c-worker-hook.md).
PROBE_COMMAND = "/usr/bin/python3 '/opt/x y/pre_tool_use.py'"
PROBE_HASH = "sha256:d94f01aedaeac4649123c3e00346464efb6103c240c5c883ad8484fe5ac77164"


def parse_override(override: str) -> dict:
    return tomllib.loads(override)


class TrustHashTest(unittest.TestCase):
    def test_the_hash_matches_what_codex_computed(self) -> None:
        self.assertEqual(codex_hook.trust_hash(PROBE_COMMAND, 30), PROBE_HASH)

    def test_the_hash_depends_on_command_and_timeout(self) -> None:
        self.assertNotEqual(codex_hook.trust_hash(PROBE_COMMAND, 31), PROBE_HASH)
        self.assertNotEqual(codex_hook.trust_hash(PROBE_COMMAND + " ", 30), PROBE_HASH)


class HookCommandTest(unittest.TestCase):
    def test_each_value_is_one_shell_word(self) -> None:
        command = codex_hook.hook_command(
            python="/opt/py thon/bin/python3",
            hook_path="/opt/it's here/pre_tool_use.py",
            policy_name=WORKER_DISPATCH_POLICY,
            project_root="/work/a b",
            hook_sha256="ab" * 32,
        )
        self.assertEqual(
            shlex.split(command),
            [
                "/opt/py thon/bin/python3", "/opt/it's here/pre_tool_use.py",
                "--runtime", "codex", "--policy", WORKER_DISPATCH_POLICY,
                "--project-root", "/work/a b", "--sha256", "ab" * 32,
            ],
        )


class ConfigOverridesTest(unittest.TestCase):
    def test_the_hook_and_its_trust_are_two_config_values(self) -> None:
        argv = codex_hook.config_overrides(PROBE_COMMAND)
        self.assertEqual([argv[0], argv[2]], ["-c", "-c"])
        self.assertEqual(len(argv), 4)
        hooks = parse_override(argv[1])["hooks"]["PreToolUse"]
        self.assertEqual(
            hooks,
            [{"hooks": [{"type": "command", "command": PROBE_COMMAND, "timeout": codex_hook.HOOK_TIMEOUT_SECONDS}]}],
        )
        state = parse_override(argv[3])["hooks"]["state"]
        self.assertEqual(
            state,
            {codex_hook.STATE_KEY: {"enabled": True, "trusted_hash": codex_hook.trust_hash(PROBE_COMMAND)}},
        )

    def test_a_command_with_quotes_and_backslashes_round_trips(self) -> None:
        command = 'python "a\\b" \'c\''
        argv = codex_hook.config_overrides(command)
        self.assertEqual(parse_override(argv[1])["hooks"]["PreToolUse"][0]["hooks"][0]["command"], command)


class TouchesHooksTest(unittest.TestCase):
    def test_overrides_naming_hooks(self) -> None:
        for value in (
            "hooks.PreToolUse=[]",
            'hooks.state={"k"={enabled=false}}',
            "hooks={}",
            " hooks . state = {}",
            "hooks.[broken=1",
        ):
            with self.subTest(value=value):
                self.assertTrue(codex_hook.touches_hooks(value))

    def test_other_overrides(self) -> None:
        for value in ("features.x=true", "mcp_servers.agent-comms.enabled=true", 'model="hooks"'):
            with self.subTest(value=value):
                self.assertFalse(codex_hook.touches_hooks(value))


def entry(**fields: object) -> dict:
    base = {
        "key": codex_hook.STATE_KEY,
        "eventName": "preToolUse",
        "command": PROBE_COMMAND,
        "enabled": True,
        "trustStatus": "trusted",
        "currentHash": codex_hook.trust_hash(PROBE_COMMAND),
    }
    base.update(fields)
    return base


class TrustProblemsTest(unittest.TestCase):
    def test_our_hook_trusted_and_enabled(self) -> None:
        self.assertEqual(codex_hook.trust_problems([entry()], PROBE_COMMAND), [])

    def test_refusals(self) -> None:
        cases = {
            "untrusted": [entry(trustStatus="untrusted")],
            "modified": [entry(trustStatus="modified")],
            "disabled": [entry(enabled=False)],
            "missing": [],
            "another command": [entry(command="true")],
        }
        for name, entries in cases.items():
            with self.subTest(name=name):
                self.assertTrue(codex_hook.trust_problems(entries, PROBE_COMMAND))


FAKE_CODEX = """\
import json, os, sys, time
mode = os.environ["FAKE_MODE"]
if mode == "argv":
    open(os.environ["FAKE_ARGV"], "w").write(json.dumps(sys.argv[1:]))
for line in sys.stdin:
    message = json.loads(line)
    if mode == "hang":
        time.sleep(60)
    if message.get("id") == 1:
        print(json.dumps({"id": 1, "result": {}}), flush=True)
    elif message.get("id") == 2:
        if mode == "garbage":
            print("not json", flush=True)
        elif mode == "error":
            print(json.dumps({"id": 2, "error": {"message": "nope"}}), flush=True)
        else:
            hooks = json.loads(os.environ["FAKE_HOOKS"])
            print(json.dumps({"method": "noise"}), flush=True)
            print(json.dumps({"id": 2, "result": {"data": [{"cwd": ".", "hooks": hooks}]}}), flush=True)
        time.sleep(60)
"""


class ListHooksTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.codex = self.root / "codex"
        self.codex.write_text(f"#!{sys.executable}\n{FAKE_CODEX}")
        self.codex.chmod(0o755)

    def list(self, mode: str, hooks: list | None = None, timeout: float = 5.0) -> tuple[list | None, str | None]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "FAKE_MODE": mode,
            "FAKE_HOOKS": json.dumps(hooks or []),
            "FAKE_ARGV": str(self.root / "argv.json"),
        }
        return codex_hook.list_hooks(
            str(self.codex), cwd=str(self.root), env=env, overrides=["-c", "a=1"], timeout=timeout
        )

    def test_returns_the_hooks_and_stops_the_server(self) -> None:
        started = time.monotonic()
        hooks, error = self.list("ok", [entry()])
        self.assertIsNone(error)
        self.assertEqual(hooks, [entry()])
        self.assertLess(time.monotonic() - started, 4)

    def test_passes_overrides_before_app_server(self) -> None:
        self.list("argv")
        self.assertEqual(json.loads((self.root / "argv.json").read_text()), ["-c", "a=1", "app-server"])

    def test_a_hung_server_times_out(self) -> None:
        started = time.monotonic()
        hooks, error = self.list("hang", timeout=1.0)
        self.assertIsNone(hooks)
        self.assertIn("timed out", error)
        self.assertLess(time.monotonic() - started, 5)

    def test_bad_answers_are_errors(self) -> None:
        for mode in ("garbage", "error"):
            with self.subTest(mode=mode):
                hooks, error = self.list(mode)
                self.assertIsNone(hooks)
                self.assertTrue(error)

    def test_a_missing_binary_is_an_error(self) -> None:
        hooks, error = codex_hook.list_hooks(str(self.root / "absent"), cwd=str(self.root), env={}, overrides=[])
        self.assertIsNone(hooks)
        self.assertTrue(error)


class SmokeRunTest(unittest.TestCase):
    def runner(self, stdout: str = "", returncode: int = 0, raises: Exception | None = None):  # type: ignore[no-untyped-def]
        calls = []

        def run(argv, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((argv, kwargs))
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

        return run, calls

    def test_runs_the_command_as_codex_does_and_requires_a_deny(self) -> None:
        deny = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}})
        run, calls = self.runner(deny)
        error = codex_hook.smoke_run("cmd x", cwd="/w", env={"SHELL": "/bin/zsh"}, runner=run)
        self.assertIsNone(error)
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["/bin/zsh", "-lc", "cmd x"])
        payload = json.loads(kwargs["input"])
        self.assertEqual(payload["tool_name"], codex_hook.SMOKE_TOOL)
        self.assertEqual(kwargs["cwd"], "/w")

    def test_anything_but_a_deny_is_an_error(self) -> None:
        allow = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}})
        for name, kwargs in {
            "silent": {"stdout": ""},
            "allow": {"stdout": allow},
            "garbage": {"stdout": "{"},
            "crash": {"stdout": "", "returncode": 1},
            "timeout": {"raises": subprocess.TimeoutExpired("x", 1)},
            "missing shell": {"raises": FileNotFoundError("sh")},
        }.items():
            with self.subTest(name=name):
                run, _ = self.runner(**kwargs)
                self.assertTrue(codex_hook.smoke_run("cmd", cwd="/w", env={}, runner=run))

    def test_without_shell_uses_bin_sh(self) -> None:
        run, calls = self.runner("")
        codex_hook.smoke_run("cmd", cwd="/w", env={}, runner=run)
        self.assertEqual(calls[0][0][:2], ["/bin/sh", "-lc"])


if __name__ == "__main__":
    unittest.main()
