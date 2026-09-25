from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import json
import hashlib
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import paths, runtime_pins, supervisor
from agent_comms.adapters import DispatchContext
from agent_comms.adapters.claude import (
    ClaudeAdapter,
    ClaudeSpawnRowNotPinnedError,
    RuntimePinDigestMismatch,
    RuntimePinDrift,
    RuntimePinUnavailable,
)
from agent_comms.adapters.codex import CodexAdapter
from agent_comms.adapters.fake import FakeAdapter
from agent_comms.cli import bootstrap_store
from agent_comms.policies import (
    CREDENTIAL_READ_DENY,
    WORKER_DISPATCH_POLICY_VERSION,
    ClaudeSandbox,
    CompiledPolicy,
    compile_policy,
)
from agent_comms.schema import ValidationError
from agent_comms.spawn import DEFAULT_WORKER_PROMPT, _claude_settings, _escaped_json, claude_settings_for_policy, render_spawn
from agent_comms.store import Store, WORKER_DISPATCH_POLICY

ROOT = Path(__file__).resolve().parents[2]
BUILTIN_ALLOWED_TOOLS = frozenset({"Bash", "Edit", "Glob", "Grep", "MultiEdit", "Read", "TodoWrite", "Write"})


def _context(recipient: dict, db_path: Path) -> DispatchContext:
    return DispatchContext(
        dispatch={"dispatch_id": "dispatch_20260609_011536_cdd9b013", "policy_name": WORKER_DISPATCH_POLICY},
        recipient=recipient,
        message={"id": "msg-test"},
        ttl_seconds=45,
        expected_close_by="2026-01-01T00:00:00Z",
        db_path=str(db_path),
    )


def _version_result(version: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["/tmp/claude", "--version"],
        returncode,
        stdout=f"Claude Code {version}\n",
        stderr="",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_claude_pin_stub(versions_dir: Path) -> Path:
    pinned_binary = versions_dir / runtime_pins.CLAUDE_PINNED_VERSION
    pinned_binary.parent.mkdir(parents=True)
    pinned_binary.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        f"  printf 'Claude Code {runtime_pins.CLAUDE_PINNED_VERSION}\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exec \"$@\"\n"
    )
    pinned_binary.chmod(0o755)
    return pinned_binary


def _live_process() -> mock.Mock:
    process = mock.Mock()
    process.pid = 4321
    process.wait.side_effect = subprocess.TimeoutExpired(["worker"], 2.0)
    return process


@contextlib.contextmanager
def _supervised_capture(captured: dict, pid: int = 4321):
    """Patch the supervised spawn boundary and capture the child command.

    The pinned-binary resolution reaches ``supervisor.spawn_supervised`` now
    (the adapter no longer direct-``subprocess.Popen``s the child), so the
    resolved runtime command is ``child_command[0]`` of the supervised spawn.
    Hermetic: no wrapper, no AF_UNIX bind.
    """
    run_token = "a" * 32

    def _spawn(child_command, **_kwargs):  # type: ignore[no-untyped-def]
        captured["child_command"] = [str(part) for part in child_command]
        return supervisor.SupervisedSpawn(
            popen=types.SimpleNamespace(pid=pid),
            run_token=run_token,
            control_socket=f"/nonexistent/run/s/{run_token}/s",
            child_pid=9000,
            wrapper_pid=pid,
            run_dir=f"/nonexistent/run/s/{run_token}",
        )

    with mock.patch.object(supervisor, "spawn_supervised", side_effect=_spawn) as spawn, mock.patch.object(
        supervisor, "reaper_registry", return_value=mock.MagicMock()
    ), mock.patch.object(supervisor, "janitor_sweep", return_value=[]):
        yield spawn


class RenderSpawnTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        with mock.patch.dict(
            os.environ,
            {
                "PROJECT_A_ROOT": str(self.tmp / "project-a"),
                "PROJECT_C_ROOT": str(self.tmp / "team-c"),
            },
        ):
            store = Store(self.tmp / "agent-comms.sqlite")
            bootstrap_store(store, ROOT / "tests" / "fixtures" / "roster.json")
            self.store = store
            self.actors = {actor["id"]: actor for actor in store.list_actors()}

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_generated_spawn_matches_certified_snapshot(self) -> None:
        snapshot = json.loads(
            (ROOT / "tests" / "substrate" / "fixtures" / "certified_spawn_v2.json").read_text()
        )
        # Criterion 1: the frozen rendering gains only --json after exec.
        codex_args = snapshot["alpha-codex-worker"]["args"]
        codex_args.insert(codex_args.index("exec") + 1, "--json")
        cases = [
            ("alpha-codex-worker", "codex"),
            ("alpha-claude-worker", "claude"),
            ("alpha-fake-worker", "fake"),
        ]

        for actor_id, runtime in cases:
            with self.subTest(actor_id=actor_id):
                self.assertEqual(render_spawn(runtime, actor_id), snapshot[actor_id])
        worker_snapshot = str(snapshot)
        self.assertNotIn('"WAKE_POLICY_VERSION":"v1"', worker_snapshot)

    def test_claude_spawn_carries_fresh_actor_id(self) -> None:
        actor_id = "team2-claude-worker"
        legacy_actor_id = "alpha-claude-worker"
        recipient = dict(self.actors[legacy_actor_id])
        recipient["id"] = actor_id
        recipient["spawn"] = render_spawn("claude", actor_id)

        adapter = ClaudeAdapter()
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        resolved_args = adapter._resolved_spawn_args(
            _context(recipient, self.tmp / "agent-comms.sqlite"), recipient["spawn"], policy
        )
        mcp_config_text = resolved_args[resolved_args.index("--mcp-config") + 1]
        mcp_config = json.loads(mcp_config_text)
        server = mcp_config["mcpServers"]["agent-comms"]

        self.assertEqual(server["args"][server["args"].index("--actor-id") + 1], actor_id)
        self.assertEqual(server["env"]["AGENT_COMMS_ACTOR_ID"], actor_id)
        self.assertEqual(
            server["env"]["WAKE_POLICY_VERSION"],
            WORKER_DISPATCH_POLICY_VERSION,
        )
        self.assertNotIn(legacy_actor_id, mcp_config_text)

    def test_rejects_unknown_runtime_loudly(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unsupported runtime 'bogus'.*claude, codex, fake"):
            render_spawn("bogus", "alpha-bogus-worker")

    def test_worker_policy_builtin_allowlist(self) -> None:
        policy = compile_policy(WORKER_DISPATCH_POLICY)

        self.assertIsInstance(policy.builtin_allowed_tools, frozenset)
        self.assertEqual(policy.builtin_allowed_tools, BUILTIN_ALLOWED_TOOLS)
        self.assertEqual(
            CompiledPolicy(
                name="future-valid-policy",
                version="v1",
                mcp_allowed_tools=frozenset(),
                mcp_denied_tools=frozenset(),
                hook_denied_tools=frozenset(),
                stripped_env_prefixes=(),
                stripped_env_names=frozenset(),
            ).builtin_allowed_tools,
            frozenset(),
        )
        with self.assertRaisesRegex(ValueError, "unknown policy: <unknown>"):
            compile_policy("<unknown>")

    def test_claude_spawn_template_carries_dispatch_settings_placeholder(self) -> None:
        spawn = render_spawn("claude", "alpha-claude-worker")
        args = spawn["args"]

        self.assertEqual(spawn["command"], "{claude_binary}")
        self.assertEqual(args[args.index("--settings") + 1], "{claude_settings}")

    def test_claude_binary_path_honours_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(self.tmp / "claude-versions")}):
            self.assertEqual(
                runtime_pins.claude_binary_path(),
                self.tmp / "claude-versions" / runtime_pins.CLAUDE_PINNED_VERSION,
            )

    def test_claude_versions_dir_default_is_runtime_custody(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("pathlib.Path.home", return_value=self.tmp / "home"):
                self.assertEqual(
                    runtime_pins.claude_versions_dir(),
                    self.tmp / "home" / ".agent-comms" / "runtime-custody",
                )

    def test_claude_preflight_fails_closed_when_pin_missing(self) -> None:
        recipient = dict(self.actors["alpha-claude-worker"])
        recipient["spawn"] = render_spawn("claude", "alpha-claude-worker")
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(self.tmp / "missing")}):
            with self.assertRaisesRegex(RuntimePinUnavailable, runtime_pins.CLAUDE_PINNED_VERSION):
                ClaudeAdapter()._preflight(_context(recipient, self.tmp / "agent-comms.sqlite"))

    def test_claude_preflight_fails_closed_on_version_drift(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        recipient = dict(self.actors["alpha-claude-worker"])
        recipient["spawn"] = render_spawn("claude", "alpha-claude-worker")

        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            adapter = ClaudeAdapter(
                version_runner=lambda _binary: _version_result("2.1.190"),
                expected_sha256=_sha256(pinned_binary),
            )
            with self.assertRaisesRegex(RuntimePinDrift, f"expected {runtime_pins.CLAUDE_PINNED_VERSION}"):
                adapter._preflight(_context(recipient, self.tmp / "agent-comms.sqlite"))

    def test_claude_preflight_fails_closed_on_digest_mismatch_before_version_exec(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        _write_claude_pin_stub(versions_dir)
        recipient = dict(self.actors["alpha-claude-worker"])
        recipient["spawn"] = render_spawn("claude", "alpha-claude-worker")
        calls = []

        def version_runner(binary: Path) -> subprocess.CompletedProcess[str]:
            calls.append(binary)
            return _version_result(runtime_pins.CLAUDE_PINNED_VERSION)

        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            adapter = ClaudeAdapter(version_runner=version_runner, expected_sha256="f" * 64)
            with self.assertRaisesRegex(RuntimePinDigestMismatch, "expected sha256"):
                adapter._preflight(_context(recipient, self.tmp / "agent-comms.sqlite"))

        self.assertEqual(calls, [])

    def test_claude_preflight_passes_on_pinned_version(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        recipient = dict(self.actors["alpha-claude-worker"])
        recipient["spawn"] = render_spawn("claude", "alpha-claude-worker")

        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            ClaudeAdapter(
                version_runner=lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION),
                expected_sha256=_sha256(pinned_binary),
            )._preflight(_context(recipient, self.tmp / "agent-comms.sqlite"))

    def test_claude_preflight_honours_hash_env_override(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        recipient = dict(self.actors["alpha-claude-worker"])
        recipient["spawn"] = render_spawn("claude", "alpha-claude-worker")

        with mock.patch.dict(
            os.environ,
            {
                "AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir),
                "AGENT_COMMS_CLAUDE_PINNED_SHA256": _sha256(pinned_binary),
            },
        ):
            ClaudeAdapter(
                version_runner=lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION),
            )._preflight(_context(recipient, self.tmp / "agent-comms.sqlite"))

    def test_claude_dispatch_resolves_command_to_pinned_binary(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        recipient = dict(self.actors["alpha-claude-worker"])
        recipient["spawn"] = render_spawn("claude", "alpha-claude-worker")
        captured = {}

        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            with mock.patch.object(paths, "REPO_ROOT", self.tmp):
                with _supervised_capture(captured):
                    ClaudeAdapter(
                        version_runner=lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION),
                        expected_sha256=_sha256(pinned_binary),
                    ).dispatch(_context(recipient, self.tmp / "agent-comms.sqlite"))

        resolved_command = captured["child_command"][0]
        self.assertEqual(resolved_command, str(pinned_binary))
        self.assertNotEqual(resolved_command, "claude")
        self.assertNotEqual(resolved_command, "{claude_binary}")

    def test_claude_dispatch_accepts_literal_pinned_binary_command(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        recipient = dict(self.actors["alpha-claude-worker"])
        spawn = render_spawn("claude", "alpha-claude-worker")
        recipient["spawn"] = dict(spawn, command=str(pinned_binary))
        captured = {}

        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            with mock.patch.object(paths, "REPO_ROOT", self.tmp):
                with _supervised_capture(captured):
                    ClaudeAdapter(
                        version_runner=lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION),
                        expected_sha256=_sha256(pinned_binary),
                    ).dispatch(_context(recipient, self.tmp / "agent-comms.sqlite"))

        resolved_command = captured["child_command"][0]
        self.assertEqual(resolved_command, str(pinned_binary))

    def test_claude_dispatch_accepts_symlink_to_pinned_binary(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        alias = self.tmp / "claude-alias"
        alias.symlink_to(pinned_binary)
        recipient = dict(self.actors["alpha-claude-worker"])
        spawn = render_spawn("claude", "alpha-claude-worker")
        recipient["spawn"] = dict(spawn, command=str(alias))
        captured = {}

        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            with mock.patch.object(paths, "REPO_ROOT", self.tmp):
                with _supervised_capture(captured):
                    ClaudeAdapter(
                        version_runner=lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION),
                        expected_sha256=_sha256(pinned_binary),
                    ).dispatch(_context(recipient, self.tmp / "agent-comms.sqlite"))

        resolved_command = captured["child_command"][0]
        self.assertEqual(resolved_command, str(alias))
        self.assertEqual(alias.resolve(), pinned_binary.resolve())

    def test_claude_dispatch_rejects_symlink_to_different_executable(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        other_binary = self.tmp / "other-claude"
        other_binary.write_text("#!/bin/sh\nexit 0\n")
        other_binary.chmod(0o755)
        alias = versions_dir / "alias-to-other"
        alias.symlink_to(other_binary)
        recipient = dict(self.actors["alpha-claude-worker"])
        spawn = render_spawn("claude", "alpha-claude-worker")
        recipient["spawn"] = dict(spawn, command=str(alias))

        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            adapter = ClaudeAdapter(
                version_runner=lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION),
                expected_sha256=_sha256(pinned_binary),
            )
            with self.assertRaisesRegex(ClaudeSpawnRowNotPinnedError, "render_spawn\\('claude', actor_id\\)"):
                adapter.dispatch(_context(recipient, self.tmp / "agent-comms.sqlite"))

    def test_claude_dispatch_rejects_stale_literal_command(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        recipient = dict(self.actors["alpha-claude-worker"])
        recipient["spawn"] = render_spawn("claude", "alpha-claude-worker")
        recipient["spawn"] = dict(recipient["spawn"], command=sys.executable)

        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            adapter = ClaudeAdapter(
                version_runner=lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION),
                expected_sha256=_sha256(pinned_binary),
            )
            with self.assertRaisesRegex(
                ClaudeSpawnRowNotPinnedError,
                "render_spawn\\('claude', actor_id\\)",
            ):
                adapter.dispatch(_context(recipient, self.tmp / "agent-comms.sqlite"))

    def test_claude_dispatch_rejects_stale_command_even_when_args_contain_pinned_path(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        recipient = dict(self.actors["alpha-claude-worker"])
        spawn = render_spawn("claude", "alpha-claude-worker")
        recipient["spawn"] = dict(
            spawn,
            command=sys.executable,
            args=list(spawn["args"]) + [str(pinned_binary), "{claude_binary}"],
        )

        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            adapter = ClaudeAdapter(
                version_runner=lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION),
                expected_sha256=_sha256(pinned_binary),
            )
            with self.assertRaisesRegex(ClaudeSpawnRowNotPinnedError, "render_spawn\\('claude', actor_id\\)"):
                adapter.dispatch(_context(recipient, self.tmp / "agent-comms.sqlite"))

    def test_claude_dispatch_rejects_truthy_non_string_commands_with_pinning_error(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)

        for command in (["claude"], 123):
            with self.subTest(command=command):
                recipient = dict(self.actors["alpha-claude-worker"])
                recipient["spawn"] = dict(render_spawn("claude", "alpha-claude-worker"), command=command)
                with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
                    adapter = ClaudeAdapter(
                        version_runner=lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION),
                        expected_sha256=_sha256(pinned_binary),
                    )
                    with self.assertRaisesRegex(ClaudeSpawnRowNotPinnedError, "render_spawn\\('claude', actor_id\\)"):
                        adapter.dispatch(_context(recipient, self.tmp / "agent-comms.sqlite"))

    def test_claude_dispatch_rejects_falsy_command_as_missing(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        recipient = dict(self.actors["alpha-claude-worker"])
        recipient["spawn"] = dict(render_spawn("claude", "alpha-claude-worker"), command=0)

        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            adapter = ClaudeAdapter(
                version_runner=lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION),
                expected_sha256=_sha256(pinned_binary),
            )
            with self.assertRaisesRegex(RuntimeError, "claude recipient requires spawn.command"):
                adapter.dispatch(_context(recipient, self.tmp / "agent-comms.sqlite"))

    def test_claude_retry_spawn_reuses_stale_command_guard(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        stale_spawn = dict(render_spawn("claude", "alpha-claude-worker"), command=sys.executable)
        self.store.register_actor(
            "alpha-claude-worker",
            "agent",
            "alpha-claude-worker",
            team="alpha",
            role="worker",
            project_root=str(self.tmp),
            runtime="claude",
            spawn=stale_spawn,
            capabilities=[],
            owner="alpha-architect",
        )

        def adapter_for_runtime(_runtime: str) -> ClaudeAdapter:
            return ClaudeAdapter(
                version_runner=lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION),
                expected_sha256=_sha256(pinned_binary),
            )

        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            dispatch = self.store.dispatch_agent(
                "alpha-architect",
                "alpha-claude-worker",
                "stale-claude-retry",
                "Worker dispatch",
                "Exercise retry guard.",
                [],
                adapter_for_runtime=adapter_for_runtime,
                ttl_seconds=5,
            )
            retried = self.store.retry_spawn(
                dispatch["dispatch_id"],
                adapter_for_runtime,
                ttl_seconds=5,
            )

        self.assertEqual(dispatch["status"], "spawn_failed_message_landed")
        self.assertIn("render_spawn('claude', actor_id)", dispatch["failure_reason"])
        self.assertEqual(retried["status"], "spawn_failed_message_landed")
        self.assertIn("render_spawn('claude', actor_id)", retried["failure_reason"])

    def test_claude_dispatch_case_variant_command_matches_observed_platform_resolution(self) -> None:
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        variant = versions_dir.with_name(versions_dir.name.upper()) / pinned_binary.name
        variant_resolves_to_pin = variant.resolve() == pinned_binary.resolve()
        recipient = dict(self.actors["alpha-claude-worker"])
        spawn = render_spawn("claude", "alpha-claude-worker")
        recipient["spawn"] = dict(spawn, command=str(variant))

        # This pins the host/Python path-resolution behavior we measured here:
        # accept only when the case-variant path resolves to the custody binary.
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            adapter = ClaudeAdapter(
                version_runner=lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION),
                expected_sha256=_sha256(pinned_binary),
            )
            if variant_resolves_to_pin:
                with mock.patch.object(paths, "REPO_ROOT", self.tmp):
                    with _supervised_capture({}):
                        adapter.dispatch(_context(recipient, self.tmp / "agent-comms.sqlite"))
            else:
                with self.assertRaisesRegex(ClaudeSpawnRowNotPinnedError, "render_spawn\\('claude', actor_id\\)"):
                    adapter.dispatch(_context(recipient, self.tmp / "agent-comms.sqlite"))

    def test_temp_claude_adapter_resolves_command_to_pinned_binary(self) -> None:
        from tests.dispatch_cell_harness import TempClaudeAdapter

        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        recipient = dict(self.actors["alpha-claude-worker"])
        recipient["spawn"] = render_spawn("claude", "alpha-claude-worker")
        captured: dict = {}

        adapter = TempClaudeAdapter(child_pid_file=self.tmp / "child.pid")
        adapter._version_runner = lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION)
        adapter._expected_sha256 = _sha256(pinned_binary)
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            with mock.patch.object(paths, "REPO_ROOT", self.tmp):
                with _supervised_capture(captured):
                    adapter.dispatch(_context(recipient, self.tmp / "agent-comms.sqlite"))

        resolved_command = captured["child_command"][0]
        self.assertEqual(resolved_command, str(pinned_binary))
        self.assertNotEqual(resolved_command, "claude")
        self.assertNotEqual(resolved_command, "{claude_binary}")

    def test_temp_claude_adapter_rejects_stale_literal_command(self) -> None:
        from tests.dispatch_cell_harness import TempClaudeAdapter

        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        recipient = dict(self.actors["alpha-claude-worker"])
        recipient["spawn"] = render_spawn("claude", "alpha-claude-worker")
        recipient["spawn"] = dict(recipient["spawn"], command=sys.executable)

        adapter = TempClaudeAdapter(child_pid_file=self.tmp / "child.pid")
        adapter._version_runner = lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION)
        adapter._expected_sha256 = _sha256(pinned_binary)
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            with self.assertRaisesRegex(ClaudeSpawnRowNotPinnedError, "alpha-claude-worker"):
                adapter.dispatch(_context(recipient, self.tmp / "agent-comms.sqlite"))

    def test_claude_cell_harness_rejects_stale_literal_command(self) -> None:
        from tests.dispatch_cell_harness import make_claude_harness

        root = self.tmp / "harness"
        root.mkdir()
        versions_dir = self.tmp / "claude-versions"
        pinned_binary = _write_claude_pin_stub(versions_dir)
        stale_spawn = render_spawn("claude", "alpha-claude-worker")
        stale_spawn = dict(stale_spawn, command=sys.executable)

        with mock.patch.dict(os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}):
            harness = make_claude_harness(root, child_pid_file=self.tmp / "child.pid", spawn=stale_spawn)
            harness.adapter._version_runner = lambda _binary: _version_result(runtime_pins.CLAUDE_PINNED_VERSION)
            harness.adapter._expected_sha256 = _sha256(pinned_binary)

            dispatch = harness.dispatch_and_wait(
                idempotency_key="stale-claude-command",
                ttl_seconds=5,
                timeout_seconds=5,
            )

        self.assertEqual(dispatch["status"], "spawn_failed_message_landed")
        self.assertIn("render_spawn('claude', actor_id)", dispatch["failure_reason"])

    def test_cell_versions_claude_pin_matches_runtime_source(self) -> None:
        cell_versions = json.loads((ROOT / "tests" / "cells" / "cell_versions.json").read_text())

        self.assertEqual(cell_versions["claude"]["last_verified"], runtime_pins.CLAUDE_PINNED_VERSION)
        self.assertEqual(cell_versions["claude"]["sha256"], runtime_pins.CLAUDE_PINNED_SHA256)

    def test_claude_settings_allows_policy_tools_only(self) -> None:
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        settings = json.loads(_claude_settings())
        allowed_tools = settings["permissions"]["allow"]
        expected_mcp_tools = [f"mcp__agent-comms__{tool_name}" for tool_name in sorted(policy.mcp_allowed_tools)]

        self.assertEqual(allowed_tools, expected_mcp_tools + sorted(BUILTIN_ALLOWED_TOOLS))
        for tool_name in expected_mcp_tools:
            self.assertIn(tool_name, allowed_tools)
        for tool_name in BUILTIN_ALLOWED_TOOLS:
            self.assertIn(tool_name, allowed_tools)
        for denied in ("dispatch_agent", "register_actor", "register_agent"):
            with self.subTest(denied=denied):
                self.assertNotIn(denied, allowed_tools)
                self.assertNotIn(f"mcp__agent-comms__{denied}", allowed_tools)
                self.assertNotIn(f"mcp__agent_comms__{denied}", allowed_tools)

    def test_claude_settings_preserves_hooks_and_permissions_shape(self) -> None:
        settings = json.loads(_claude_settings())

        self.assertEqual(
            settings["hooks"],
            {
                "PreToolUse": [
                    {
                        "hooks": [
                            {
                                "command": "{python} {hooks_path}",
                                "type": "command",
                            }
                        ],
                        "matcher": "",
                    }
                ]
            },
        )
        self.assertEqual(
            settings["permissions"]["allow"],
            [
                f"mcp__agent-comms__{tool_name}"
                for tool_name in sorted(compile_policy(WORKER_DISPATCH_POLICY).mcp_allowed_tools)
            ]
            + sorted(BUILTIN_ALLOWED_TOOLS),
        )

    def test_claude_settings_emits_policy_sandbox(self) -> None:
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        settings = json.loads(_claude_settings())
        filesystem = settings["sandbox"]["filesystem"]

        self.assertIsNotNone(policy.claude_sandbox)
        assert policy.claude_sandbox is not None
        self.assertEqual(policy.claude_sandbox.deny_read, CREDENTIAL_READ_DENY)
        self.assertEqual(policy.claude_sandbox.allow_read, ())

        self.assertEqual(
            settings["sandbox"],
            {
                "enabled": True,
                "failIfUnavailable": True,
                "allowUnsandboxedCommands": False,
                "filesystem": {"allowWrite": ["."], "denyRead": list(CREDENTIAL_READ_DENY)},
                "network": {"allowedDomains": []},
            },
        )
        self.assertEqual(filesystem["allowWrite"], ["."])
        self.assertEqual(filesystem["denyRead"], list(CREDENTIAL_READ_DENY))
        self.assertEqual(settings["sandbox"]["network"]["allowedDomains"], [])
        self.assertNotIn("allowRead", filesystem)
        self.assertNotIn("~/", policy.claude_sandbox.deny_read)
        self.assertNotIn("~/", filesystem["denyRead"])
        for denied_path in CREDENTIAL_READ_DENY:
            self.assertIn(denied_path, policy.claude_sandbox.deny_read)
            self.assertIn(denied_path, filesystem["denyRead"])

    def test_claude_settings_sandbox_is_policy_sourced(self) -> None:
        base = compile_policy(WORKER_DISPATCH_POLICY)
        policy = CompiledPolicy(
            name=base.name,
            version=base.version,
            mcp_allowed_tools=base.mcp_allowed_tools,
            mcp_denied_tools=base.mcp_denied_tools,
            hook_denied_tools=base.hook_denied_tools,
            stripped_env_prefixes=base.stripped_env_prefixes,
            stripped_env_names=base.stripped_env_names,
            builtin_allowed_tools=base.builtin_allowed_tools,
            env=base.env,
            bootstrap_marker=base.bootstrap_marker,
            hook_path=base.hook_path,
            hook_sha256=base.hook_sha256,
            claude_sandbox=ClaudeSandbox(
                enabled=True,
                fail_if_unavailable=False,
                allow_unsandboxed_commands=True,
                allow_write=("./x",),
                allowed_domains=("example.com",),
                deny_read=("~/secret",),
                allow_read=("./ok",),
            ),
        )

        settings = json.loads(claude_settings_for_policy(policy, "{hooks_path}", "{python}", quote=False))

        self.assertEqual(
            settings["sandbox"],
            {
                "enabled": True,
                "failIfUnavailable": False,
                "allowUnsandboxedCommands": True,
                "filesystem": {"allowWrite": ["./x"], "denyRead": ["~/secret"], "allowRead": ["./ok"]},
                "network": {"allowedDomains": ["example.com"]},
            },
        )

    def test_claude_settings_omits_empty_read_sandbox_keys(self) -> None:
        base = compile_policy(WORKER_DISPATCH_POLICY)
        policy = CompiledPolicy(
            name=base.name,
            version=base.version,
            mcp_allowed_tools=base.mcp_allowed_tools,
            mcp_denied_tools=base.mcp_denied_tools,
            hook_denied_tools=base.hook_denied_tools,
            stripped_env_prefixes=base.stripped_env_prefixes,
            stripped_env_names=base.stripped_env_names,
            builtin_allowed_tools=base.builtin_allowed_tools,
            env=base.env,
            bootstrap_marker=base.bootstrap_marker,
            hook_path=base.hook_path,
            hook_sha256=base.hook_sha256,
            claude_sandbox=ClaudeSandbox(
                enabled=True,
                fail_if_unavailable=True,
                allow_unsandboxed_commands=False,
                allow_write=(".",),
                allowed_domains=(),
            ),
        )

        settings = json.loads(claude_settings_for_policy(policy, "{hooks_path}", "{python}", quote=False))

        filesystem = settings["sandbox"]["filesystem"]
        self.assertNotIn("denyRead", filesystem)
        self.assertNotIn("allowRead", filesystem)

    def test_claude_settings_omits_disabled_or_absent_sandbox(self) -> None:
        base = compile_policy(WORKER_DISPATCH_POLICY)

        for label, sandbox in (
            ("none", None),
            (
                "disabled",
                ClaudeSandbox(
                    enabled=False,
                    fail_if_unavailable=True,
                    allow_unsandboxed_commands=False,
                    allow_write=(".",),
                    allowed_domains=(),
                ),
            ),
        ):
            with self.subTest(label=label):
                policy = CompiledPolicy(
                    name=base.name,
                    version=base.version,
                    mcp_allowed_tools=base.mcp_allowed_tools,
                    mcp_denied_tools=base.mcp_denied_tools,
                    hook_denied_tools=base.hook_denied_tools,
                    stripped_env_prefixes=base.stripped_env_prefixes,
                    stripped_env_names=base.stripped_env_names,
                    builtin_allowed_tools=base.builtin_allowed_tools,
                    env=base.env,
                    bootstrap_marker=base.bootstrap_marker,
                    hook_path=base.hook_path,
                    hook_sha256=base.hook_sha256,
                    claude_sandbox=sandbox,
                )

                self.assertNotIn(
                    "sandbox",
                    json.loads(claude_settings_for_policy(policy, "{hooks_path}", "{python}", quote=False)),
                )

    def test_claude_dispatch_resolves_settings_for_fresh_and_stale_spawn(self) -> None:
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        fresh_spawn = render_spawn("claude", "alpha-claude-worker")
        stale_spawn = dict(fresh_spawn)
        stale_spawn["args"] = list(fresh_spawn["args"])
        stale_spawn["args"][stale_spawn["args"].index("--settings") + 1] = _escaped_json(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "command": "python3 {hooks_path}",
                                    "type": "command",
                                }
                            ],
                            "matcher": "",
                        }
                    ]
                },
                "permissions": {
                    "allow": [
                        f"mcp__agent-comms__{tool_name}"
                        for tool_name in sorted(policy.mcp_allowed_tools)
                    ]
                },
            }
        )
        recipient = dict(self.actors["alpha-claude-worker"])
        adapter = ClaudeAdapter()

        for label, spawn in (("fresh", fresh_spawn), ("stale", stale_spawn)):
            with self.subTest(label=label):
                recipient["spawn"] = spawn
                resolved_args = adapter._resolved_spawn_args(
                    _context(recipient, self.tmp / "agent-comms.sqlite"), spawn, policy
                )
                settings = json.loads(resolved_args[resolved_args.index("--settings") + 1])
                allowed_tools = settings["permissions"]["allow"]
                expected_mcp_tools = [
                    f"mcp__agent-comms__{tool_name}"
                    for tool_name in sorted(policy.mcp_allowed_tools)
                ]

                self.assertEqual(allowed_tools, expected_mcp_tools + sorted(BUILTIN_ALLOWED_TOOLS))
                for tool_name in BUILTIN_ALLOWED_TOOLS:
                    self.assertIn(tool_name, allowed_tools)

    def test_claude_dispatch_settings_track_live_policy(self) -> None:
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        live_policy = CompiledPolicy(
            name=policy.name,
            version=policy.version,
            mcp_allowed_tools=policy.mcp_allowed_tools,
            mcp_denied_tools=policy.mcp_denied_tools,
            hook_denied_tools=policy.hook_denied_tools,
            stripped_env_prefixes=policy.stripped_env_prefixes,
            stripped_env_names=policy.stripped_env_names,
            builtin_allowed_tools=frozenset({"Read", "Write", "LiveOnlyTool"}),
            env=policy.env,
            bootstrap_marker=policy.bootstrap_marker,
            hook_path=policy.hook_path,
            hook_sha256=policy.hook_sha256,
        )
        recipient = dict(self.actors["alpha-claude-worker"])
        recipient["spawn"] = render_spawn("claude", "alpha-claude-worker")
        adapter = ClaudeAdapter()

        resolved_args = adapter._resolved_spawn_args(
            _context(recipient, self.tmp / "agent-comms.sqlite"),
            recipient["spawn"],
            live_policy,
        )
        settings = json.loads(resolved_args[resolved_args.index("--settings") + 1])

        self.assertIn("LiveOnlyTool", settings["permissions"]["allow"])

    def _resolved_args_for(self, runtime: str, spawn: dict) -> list[str]:
        actor_id = f"alpha-{runtime}-worker"
        recipient = dict(self.actors[actor_id])
        recipient["spawn"] = spawn
        adapter = {"claude": ClaudeAdapter(), "codex": CodexAdapter(), "fake": FakeAdapter()}[runtime]
        return adapter._resolved_spawn_args(
            _context(recipient, self.tmp / "agent-comms.sqlite"),
            spawn,
            compile_policy(WORKER_DISPATCH_POLICY),
        )

    def _worker_prompt_for(self, runtime: str, spawn: dict) -> str:
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        prompts = [
            arg
            for arg in self._resolved_args_for(runtime, spawn)
            if policy.bootstrap_marker in arg and "mcp__agent-comms__list_inbox" in arg
        ]
        self.assertEqual(len(prompts), 1)
        return prompts[0]

    def test_worker_prompt_placeholder_resolves_live_for_codex_and_claude(self) -> None:
        for runtime in ("codex", "claude"):
            with self.subTest(runtime=runtime):
                prompt = self._worker_prompt_for(runtime, render_spawn(runtime, f"alpha-{runtime}-worker"))

                self.assertEqual(
                    prompt,
                    DEFAULT_WORKER_PROMPT.format(
                        actor_id=f"alpha-{runtime}-worker",
                        message_id="msg-test",
                    ),
                )

    def test_stale_literal_worker_prompt_resolves_to_live_prompt(self) -> None:
        stale_prompt = (
            "WakePolicy=worker_dispatch_readwrite_bounded. You are actor {actor_id}. "
            "Call mcp__agent-comms__list_inbox, find message {message_id}, read it via "
            "read_message, perform the work it describes (a trivial echo/summary in this "
            "hello-world), reply via send_message with parent_message_id={message_id}, "
            "then close the triggering message via close_message, then exit."
        )

        for runtime in ("codex", "claude"):
            with self.subTest(runtime=runtime):
                spawn = render_spawn(runtime, f"alpha-{runtime}-worker")
                spawn["args"] = list(spawn["args"])
                spawn["args"][-1] = stale_prompt

                prompt = self._worker_prompt_for(runtime, spawn)

                self.assertEqual(
                    prompt,
                    DEFAULT_WORKER_PROMPT.format(
                        actor_id=f"alpha-{runtime}-worker",
                        message_id="msg-test",
                    ),
                )
                self.assertNotIn("hello-world", prompt)
                self.assertNotIn("trivial echo", prompt)

    def test_live_worker_prompt_tracks_source_constant(self) -> None:
        replacement = DEFAULT_WORKER_PROMPT + " Source sentinel {actor_id} {message_id}."
        with mock.patch("agent_comms.adapters._base.DEFAULT_WORKER_PROMPT", replacement):
            prompt = self._worker_prompt_for("codex", render_spawn("codex", "alpha-codex-worker"))

        self.assertIn("Source sentinel alpha-codex-worker msg-test.", prompt)

    def test_worker_prompt_ids_are_resolved(self) -> None:
        prompt = self._worker_prompt_for("claude", render_spawn("claude", "alpha-claude-worker"))

        self.assertIn("alpha-claude-worker", prompt)
        self.assertIn("msg-test", prompt)
        self.assertNotIn("{actor_id}", prompt)
        self.assertNotIn("{message_id}", prompt)
        self.assertNotIn("{worker_prompt}", prompt)

    def test_malformed_codex_and_claude_spawn_without_prompt_slot_rejects(self) -> None:
        for runtime in ("codex", "claude"):
            with self.subTest(runtime=runtime):
                spawn = render_spawn(runtime, f"alpha-{runtime}-worker")
                spawn["args"] = list(spawn["args"])
                spawn["args"][-1] = "markerless final arg"

                with self.assertRaisesRegex(RuntimeError, "bootstrap marker"):
                    self._resolved_args_for(runtime, spawn)

    def test_worker_prompt_placeholder_wins_over_incidental_marker_arg(self) -> None:
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        for runtime in ("codex", "claude"):
            with self.subTest(runtime=runtime):
                spawn = render_spawn(runtime, f"alpha-{runtime}-worker")
                spawn["args"] = list(spawn["args"])
                incidental = f"incidental {policy.bootstrap_marker} marker"
                spawn["args"].insert(1, incidental)

                resolved = self._resolved_args_for(runtime, spawn)
                prompt_args = [
                    arg
                    for arg in resolved
                    if policy.bootstrap_marker in arg and "mcp__agent-comms__list_inbox" in arg
                ]

                self.assertEqual(len(prompt_args), 1)
                self.assertIn(incidental, resolved)
                self.assertNotIn("{worker_prompt}", resolved)

    def test_incidental_marker_without_prompt_slot_is_rejected(self) -> None:
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        for runtime in ("codex", "claude"):
            with self.subTest(runtime=runtime):
                spawn = render_spawn(runtime, f"alpha-{runtime}-worker")
                spawn["args"] = list(spawn["args"])
                spawn["args"].insert(1, f"incidental {policy.bootstrap_marker} marker")
                spawn["args"][-1] = "markerless final arg"

                with self.assertRaisesRegex(RuntimeError, "bootstrap marker"):
                    self._resolved_args_for(runtime, spawn)

    def test_fake_bare_marker_is_not_replaced_by_live_worker_prompt(self) -> None:
        spawn = render_spawn("fake", "alpha-fake-worker")
        resolved = self._resolved_args_for("fake", spawn)

        self.assertEqual(resolved[-1], compile_policy(WORKER_DISPATCH_POLICY).bootstrap_marker)
        self.assertNotEqual(
            resolved[-1],
            DEFAULT_WORKER_PROMPT.format(actor_id="alpha-fake-worker", message_id="msg-test"),
        )

    def test_resolved_spawn_and_cell_harness_have_no_hello_world_prompt_text(self) -> None:
        import tests.dispatch_cell_harness as harness

        for runtime in ("codex", "claude"):
            with self.subTest(runtime=runtime):
                resolved = " ".join(
                    self._resolved_args_for(runtime, render_spawn(runtime, f"alpha-{runtime}-worker"))
                ).lower()
                self.assertNotIn("hello-world", resolved)
                self.assertNotIn("trivial echo", resolved)
                self.assertNotIn("trivial summary", resolved)

        harness_text = (ROOT / "tests" / "dispatch_cell_harness.py").read_text().lower()
        self.assertNotIn("hello-world", harness_text)
        self.assertNotIn("trivial echo", harness_text)
        self.assertFalse(hasattr(harness, "DEFAULT_CODEX_PROMPT"))
        self.assertFalse(hasattr(harness, "DEFAULT_CLAUDE_PROMPT"))


if __name__ == "__main__":
    unittest.main()
