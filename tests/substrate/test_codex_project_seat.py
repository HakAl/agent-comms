import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import base64
import contextlib
import functools
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.codex_custody_support import forwarding_codex_script, install_fake_codex_custody
from agent_comms import cli, codex_binding, codex_hook, doctor, mcp_clients, paths, provisioning, setup, supervisor
from agent_comms.adapters import DispatchContext
from agent_comms.adapters.codex import CodexAdapter, McpBindingMismatch, PolicyHookUnverified
from agent_comms.policies import WORKER_DISPATCH_POLICY_VERSION
from agent_comms.spawn import render_spawn
from agent_comms.store import WORKER_DISPATCH_POLICY

from tests.substrate.test_setup import HUMAN, FakeClients

MCP_COMMAND = str(Path(sys.executable).parent / "agent-comms-mcp")
BLOCK = codex_binding.entry_toml(codex_binding.Binding("/bin/m", ["--actor-id", "a"], "/p", codex_binding.server_env(AGENT_COMMS_ACTOR_ID="a")))
REAL_CODEX = shutil.which("codex")
REAL_GIT = shutil.which("git")


def _completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def resolved(binding: codex_binding.Binding, **changes) -> dict:
    """What ``codex mcp get --json`` prints for ``binding``, with ``changes``."""
    observed = {
        "name": "agent-comms",
        "enabled": True,
        "disabled_reason": None,
        "transport": {"type": "stdio", "command": binding.command, "args": list(binding.args), "env": dict(binding.env), "env_vars": [], "cwd": binding.cwd},
        "enabled_tools": None,
        "disabled_tools": [],
        "startup_timeout_sec": None,
        "tool_timeout_sec": None,
    }
    for key, value in changes.items():
        if key in observed["transport"]:
            observed["transport"][key] = value
        else:
            observed[key] = value
    return observed


class StubCodex:
    """A ``codex mcp get`` runner that prints ``observed`` and records calls."""

    def __init__(self, observed: dict | None = None, returncode: int = 0, stderr: str = "") -> None:
        self.observed = observed
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        return _completed(argv, self.returncode, stdout=json.dumps(self.observed) if self.observed is not None else "", stderr=self.stderr)


class StubHooks:
    """A ``hooks/list`` lister and a hook runner standing in for Codex: by
    default Codex lists the policy hook trusted and the hook denies."""

    DENY = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}})

    def __init__(self, *, entry: dict | None = None, listed: list | None = None, error: str | None = None,
                 stdout: str = DENY, returncode: int = 0) -> None:
        self.entry, self.listed, self.error = entry or {}, listed, error
        self.stdout, self.returncode = stdout, returncode
        self.list_calls: list[dict] = []
        self.run_calls: list[dict] = []

    def lister(self, codex, **kwargs):
        self.list_calls.append({"codex": codex, **kwargs})
        if self.error is not None:
            return None, self.error
        if self.listed is not None:
            return self.listed, None
        # The command Codex lists is the one the overrides declared.
        command = _declared_hook_command(kwargs["overrides"])
        return [{"key": codex_hook.STATE_KEY, "command": command, "enabled": True, "trustStatus": "trusted", **self.entry}], None

    def runner(self, argv, **kwargs):
        self.run_calls.append({"argv": list(argv), **kwargs})
        return _completed(argv, self.returncode, stdout=self.stdout, stderr="")


def _declared_hook_command(overrides: list[str]) -> str:
    """The command the ``-c hooks.PreToolUse=...`` value declares."""
    [value] = [value for value in overrides if value.startswith("hooks.PreToolUse=")]
    return tomllib.loads(value)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


class RewriteTest(unittest.TestCase):
    """The project file keeps everything but our server's tables."""

    def ours(self, text: str) -> dict:
        return tomllib.loads(text)["mcp_servers"]["agent-comms"]

    def test_empty_file_becomes_our_block(self) -> None:
        self.assertEqual(mcp_clients.rewrite_codex_project_text("", BLOCK), BLOCK)

    def test_other_tables_and_comments_are_kept_and_ours_replaced(self) -> None:
        text = (
            '# my settings\nmodel = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n\n'
            '[mcp_servers.agent-comms]\ncommand = "/old"\nargs = ["--actor-id", "old"]\nstartup_timeout_sec = 5\n'
            '# kept note\n\n[mcp_servers."agent-comms".env]\nAGENT_COMMS_DB = "/old.sqlite"\n\n'
            '[profiles.fast]\nmodel = "o4"\n'
        )
        result = mcp_clients.rewrite_codex_project_text(text, BLOCK)
        data = tomllib.loads(result)
        self.assertEqual(data["model"], "o3")
        self.assertEqual(data["mcp_servers"]["other"], {"command": "x"})
        self.assertEqual(data["profiles"], {"fast": {"model": "o4"}})
        self.assertEqual(self.ours(result), self.ours(BLOCK))
        self.assertIn("# my settings", result)
        self.assertIn("# kept note", result)
        # Rewriting what we wrote changes nothing.
        self.assertEqual(mcp_clients.rewrite_codex_project_text(result, BLOCK), result)

    def test_forms_we_did_not_write_are_refused(self) -> None:
        cases = {
            "inline": '[mcp_servers]\nagent-comms = { command = "x" }\n',
            "dotted": '[mcp_servers]\nagent-comms.command = "x"\n',
            "top-level dotted": 'mcp_servers.agent-comms.command = "x"\n',
            "array": '[[mcp_servers.agent-comms]]\ncommand = "x"\n',
            "broken": "[mcp_servers.agent-comms\n",
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(mcp_clients.SeatRefused):
                mcp_clients.rewrite_codex_project_text(text, BLOCK)

    def test_a_header_inside_a_multiline_string_is_not_mistaken_for_a_table(self) -> None:
        # The line looks like our header, but it is data; the check after the
        # edit catches the changed string and refuses rather than corrupt it.
        text = 'notes = """\n[mcp_servers.agent-comms]\n"""\n'
        with self.assertRaises(mcp_clients.SeatRefused):
            mcp_clients.rewrite_codex_project_text(text, BLOCK)


class _Project(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.project = self.root / "proj"
        self.project.mkdir()
        self.db = self.root / "ledger.sqlite"
        self.codex_home = self.root / "codex"
        env = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home), mcp_clients.CLAUDE_CONFIG_ENV: str(self.root / "claude.json")})
        env.start()
        self.addCleanup(env.stop)
        self.seat = mcp_clients.SEATS["codex"]

    def target(self, actor_id: str = "t-architect", project: Path | None = None) -> mcp_clients.SeatTarget:
        return mcp_clients.SeatTarget(actor_id, project or self.project, MCP_COMMAND, self.db, True, None)

    def which_git(self, name: str) -> str | None:
        return REAL_GIT if name == "git" else None

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", *args], cwd=cwd or self.project, check=True, capture_output=True, text=True).stdout.strip()


class CodexSeatTest(_Project):
    def test_entry_pins_every_field_that_decides_identity(self) -> None:
        result = self.seat.apply(self.target(), mcp_clients.SeatState("absent", self.project), which=lambda name: None, run=subprocess.run)
        self.assertIsNone(result.failure)
        entry = tomllib.loads((self.project / ".codex" / "config.toml").read_text(encoding="utf-8"))["mcp_servers"]["agent-comms"]
        self.assertEqual(entry, {
            "command": MCP_COMMAND,
            "args": ["--actor-id", "t-architect", "--db", str(self.db)],
            "cwd": str(self.project),
            "enabled": True,
            "disabled_tools": [],
            "env_vars": [],
            "env": {
                "AGENT_COMMS_ACTOR_ID": "t-architect", "AGENT_COMMS_DB": str(self.db), "AGENT_COMMS_HOME": "",
                "AGENT_COMMS_PROJECT_ROOT": str(self.project), "WAKE_POLICY": "", "WAKE_POLICY_VERSION": "",
            },
        })
        self.assertEqual(self.seat.inspect(self.target(), which=lambda name: None, run=subprocess.run).status, "bound")
        self.assertIn("git is not on PATH; nothing excludes", result.notes[0])

    def test_home_travels_in_argv_and_env(self) -> None:
        home = self.root / "h"
        target = mcp_clients.SeatTarget("t-architect", self.project, MCP_COMMAND, self.db, False, home)
        binding = self.seat.binding(target)
        self.assertEqual(binding.args[-2:], ["--home", str(home)])
        self.assertEqual(binding.env["AGENT_COMMS_HOME"], str(home))

    def test_inspect_states(self) -> None:
        none = dict(which=lambda name: None, run=subprocess.run)
        self.assertEqual(self.seat.inspect(self.target(), **none).status, "absent")
        path = self.project / ".codex" / "config.toml"
        path.parent.mkdir()
        path.write_text('model = "o3"\n', encoding="utf-8")
        self.assertEqual(self.seat.inspect(self.target(), **none).status, "absent")
        self.seat.apply(self.target("other-architect"), mcp_clients.SeatState("absent", path), **none)
        state = self.seat.inspect(self.target(), **none)
        self.assertEqual((state.status, state.holder), ("taken", "other-architect"))
        path.write_text('[mcp_servers]\nagent-comms = { command = "x" }\n', encoding="utf-8")
        state = self.seat.inspect(self.target(), **none)
        self.assertEqual(state.status, "refused")
        self.assertIn("inline table or dotted keys", state.problems[0])

    @unittest.skipIf(REAL_GIT is None, "git is not on PATH")
    def test_git_project_excludes_the_file_once_and_refuses_a_tracked_one(self) -> None:
        self.git("init", "-q")
        for _ in range(2):
            result = self.seat.apply(self.target(), mcp_clients.SeatState("absent", self.project), which=self.which_git, run=subprocess.run)
            self.assertIsNone(result.failure, result)
        exclude = Path(self.git("rev-parse", "--absolute-git-dir")) / "info" / "exclude"
        self.assertEqual([line for line in exclude.read_text().splitlines() if "codex" in line], ["/.codex/config.toml"])
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), "")
        # Tracked: refused, and left alone.
        self.git("add", "-f", ".codex/config.toml")
        before = (self.project / ".codex" / "config.toml").read_text()
        state = self.seat.inspect(self.target(), which=self.which_git, run=subprocess.run)
        self.assertEqual(state.status, "refused")
        self.assertIn("tracked by git", state.problems[0])
        self.assertIsNotNone(self.seat.apply(self.target("x"), state, which=self.which_git, run=subprocess.run).failure)
        self.assertEqual((self.project / ".codex" / "config.toml").read_text(), before)

    @unittest.skipIf(REAL_GIT is None, "git is not on PATH")
    def test_subdirectory_project_and_worktree_use_the_exclude_file_git_reports(self) -> None:
        repo = self.root / "repo"
        sub = repo / "sub"
        sub.mkdir(parents=True)
        self.git("init", "-q", cwd=repo)
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x", cwd=repo)
        exclusion = mcp_clients.git_exclusion(sub, which=self.which_git, run=subprocess.run)
        self.assertEqual(exclusion.line, "/sub/.codex/config.toml")
        self.assertEqual(os.path.realpath(exclusion.exclude_file), os.path.realpath(repo / ".git" / "info" / "exclude"))
        tree = self.root / "tree"
        self.git("worktree", "add", "-q", str(tree), cwd=repo)
        self.assertTrue((tree / ".git").is_file())
        result = self.seat.apply(self.target(project=tree), mcp_clients.SeatState("absent", tree), which=self.which_git, run=subprocess.run)
        self.assertIsNone(result.failure, result)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all", cwd=tree), "")

    def test_the_documented_entry_is_the_one_setup_writes(self) -> None:
        # INTEGRATION-004 F3: the docs example had lost --db.
        text = (Path(__file__).resolve().parents[2] / "docs" / "mcp-setup.md").read_text(encoding="utf-8")
        block = text.split("### Codex CLI", 1)[1].split("```toml\n", 1)[1].split("```", 1)[0]
        documented = tomllib.loads(block)["mcp_servers"]["agent-comms"]
        target = mcp_clients.SeatTarget(
            "team-a-architect", Path("/absolute/path/to/your/project"), documented["command"],
            Path("/absolute/path/to/home/.agent-comms/agent-comms.sqlite"), True, None,
        )
        self.assertEqual(documented, self.seat.expected_entry(target))

    @unittest.skipIf(REAL_GIT is None, "git is not on PATH")
    def test_glob_characters_in_the_path_are_excluded_literally(self) -> None:
        # INTEGRATION-004 F2: /app[1]/.codex/config.toml matched app1, not app[1].
        repo = self.root / "repo"
        repo.mkdir()
        self.git("init", "-q", cwd=repo)
        for name in ("app[1]", "a*b", "q?", "back\\slash", "!bang", "#hash"):
            with self.subTest(name):
                project = repo / name
                project.mkdir()
                result = self.seat.apply(self.target(project=project), mcp_clients.SeatState("absent", project), which=self.which_git, run=subprocess.run)
                self.assertIsNone(result.failure, result)
                self.assertTrue((project / ".codex" / "config.toml").is_file())
                self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all", cwd=repo), "")
        self.assertEqual(mcp_clients.git_literal_pattern("a[1]/*?\\b"), "a\\[1]/\\*\\?\\\\b")

    def test_an_exclusion_git_does_not_confirm_writes_nothing(self) -> None:
        exclude = self.root / "exclude"

        def run(argv, **kwargs):
            if "check-ignore" in argv:
                return _completed(argv, 1)
            if "--show-toplevel" in argv:
                return _completed(argv, stdout=str(self.project))
            if "--git-path" in argv:
                return _completed(argv, stdout=str(exclude))
            return _completed(argv, 1)

        result = self.seat.apply(self.target(), mcp_clients.SeatState("absent", self.project), which=self.which_git, run=run)
        self.assertIn("git does not ignore", result.failure)
        self.assertFalse((self.project / ".codex" / "config.toml").exists())

    def test_non_git_project_gets_the_entry_and_a_note(self) -> None:
        def run(argv, **kwargs):
            return _completed(argv, 128, stderr="fatal: not a git repository")

        result = self.seat.apply(self.target(), mcp_clients.SeatState("absent", self.project), which=lambda name: "/usr/bin/git", run=run)
        self.assertIsNone(result.failure)
        self.assertIn("is not in a git work tree; nothing excludes", result.notes[0])
        self.assertIn("only when started in", result.notes[1])
        self.assertTrue((self.project / ".codex" / "config.toml").is_file())


class DoctorEffectiveTest(_Project):
    def setUp(self) -> None:
        super().setUp()
        self.seat.apply(self.target(), mcp_clients.SeatState("absent", self.project), which=lambda name: None, run=subprocess.run)
        self.binding = self.seat.binding(self.target())

    def diagnose(self, runner, which=lambda name: "/opt/bin/codex" if name == "codex" else None) -> dict:
        return {finding.suffix: finding for finding in self.seat.diagnose(self.target(), which=which, run=runner)}

    def trust(self) -> None:
        self.codex_home.mkdir(exist_ok=True)
        (self.codex_home / "config.toml").write_text(f'[projects.{json.dumps(str(self.project))}]\ntrust_level = "trusted"\n')

    def test_matching_resolution_passes_and_runs_from_the_project(self) -> None:
        runner = StubCodex(resolved(self.binding))
        findings = self.diagnose(runner)
        self.assertEqual((findings[""].status, findings[":effective"].status), ("ok", "ok"))
        [call] = runner.calls
        self.assertEqual(call["argv"], ["/opt/bin/codex", "mcp", "get", "agent-comms", "--json"])
        self.assertEqual(call["cwd"], str(self.project))

    def test_every_merged_field_that_differs_fails_with_the_other_layer_fix(self) -> None:
        self.trust()
        cases = {
            "enabled": (dict(enabled=False), "enabled is False"),
            "args": (dict(args=["--actor-id", "x"]), "args are"),
            "env leak": (dict(env={**self.binding.env, "OTHER": "y"}), "env OTHER='y' is set"),
            "env value": (dict(env={**self.binding.env, "WAKE_POLICY": "p"}), "env WAKE_POLICY is 'p'"),
            "cwd": (dict(cwd="/elsewhere"), "cwd is '/elsewhere'"),
            "enabled_tools": (dict(enabled_tools=["whoami"]), "enabled_tools restricts"),
            "disabled_tools": (dict(disabled_tools=["whoami"]), "disabled_tools is"),
            "env_vars": (dict(env_vars=["AGENT_COMMS_DB"]), "env_vars forwards"),
            "transport": (dict(type="streamable_http"), "transport is 'streamable_http'"),
        }
        for name, (changes, expected) in cases.items():
            with self.subTest(name):
                finding = self.diagnose(StubCodex(resolved(self.binding, **changes)))[":effective"]
                self.assertEqual(finding.status, "fail")
                self.assertIn(expected, finding.detail)
                self.assertIn("remove or rename", finding.fix)

    def test_untrusted_project_gets_the_trust_fix(self) -> None:
        finding = self.diagnose(StubCodex(returncode=1, stderr="Error: No MCP server named 'agent-comms' found."))[":effective"]
        self.assertEqual(finding.status, "fail")
        self.assertIn("does not trust", finding.detail)
        self.assertIn("accept the trust prompt", finding.fix)
        user_entry = resolved(self.binding, args=["--actor-id", "someone"])
        finding = self.diagnose(StubCodex(user_entry))[":effective"]
        self.assertIn("accept the trust prompt", finding.fix)

    def test_codex_missing_is_a_skip(self) -> None:
        runner = StubCodex(resolved(self.binding))
        finding = self.diagnose(runner, which=lambda name: None)[":effective"]
        self.assertEqual(finding.status, "skip")
        self.assertEqual(runner.calls, [])


class BindSeatTest(_Project):
    """``agent-comms bind-seat`` for an existing team, through the CLI."""

    def setUp(self) -> None:
        super().setUp()
        self.clients = FakeClients(self.root / "claude.json", self.codex_home / "config.toml")
        injected = functools.partial(setup.run_setup, which=self.clients.which, run=self.clients.run)
        bind = functools.partial(setup.bind_seat, which=self.clients.which, run=self.clients.run)
        check = functools.partial(doctor.run_doctor, which=self.clients.which, run=self.clients.run)
        for module, name, value in ((setup, "run_setup", injected), (setup, "bind_seat", bind), (doctor, "run_doctor", check)):
            patch = mock.patch.object(module, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.registry = paths.actors_config_path()
        self.addCleanup(self.registry.unlink, missing_ok=True)
        self.addCleanup(lambda: shutil.rmtree(paths.runtime_codex_auth_source().parent, ignore_errors=True))

    def cli(self, *argv) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cli.run(["--db", str(self.db), *argv])
        return rc, json.loads(buffer.getvalue())

    def test_binds_each_client_records_it_and_hands_seats_over(self) -> None:
        rc, result = self.cli("setup", "--project-root", str(self.project), "--team", "t", "--runtimes", "fake", "--clients", "", "--human-id", HUMAN, "--yes")
        self.assertEqual(rc, 0, result)
        for client in ("codex", "claude"):
            rc, result = self.cli("bind-seat", "t-architect", "--client", client)
            self.assertEqual(rc, 0, result)
        self.assertEqual(json.loads(self.registry.read_text())["seats"], {"t-architect": ["codex", "claude"]})
        rc, result = self.cli("doctor")
        by_id = {item["id"]: item for item in result["checks"]}
        self.assertEqual(by_id["mcp:t-architect:codex"]["status"], doctor.OK)
        self.assertEqual(by_id["mcp:t-architect:codex:effective"]["status"], doctor.OK)
        self.assertEqual(by_id["mcp:t-architect:claude"]["status"], doctor.OK)
        # Rebinding what is already ours does nothing.
        rc, result = self.cli("bind-seat", "t-architect", "--client", "codex")
        self.assertEqual(result["mcp"]["applied"][0]["detail"], "already bound to t-architect")
        # A second team in the same project: refused, then handed over.
        rc, result = self.cli("setup", "--project-root", str(self.project), "--team", "u", "--runtimes", "fake", "--clients", "", "--yes")
        self.assertEqual(rc, 0, result)
        rc, result = self.cli("bind-seat", "u-architect", "--client", "codex")
        self.assertEqual(rc, 2, result)
        self.assertIn("bound to actor 't-architect'", result["error"])
        self.assertIn("--replace-seat", result["error"])
        rc, result = self.cli("bind-seat", "u-architect", "--client", "codex", "--replace-seat")
        self.assertEqual(rc, 0, result)
        self.assertEqual(json.loads(self.registry.read_text())["seats"], {"t-architect": ["claude"], "u-architect": ["codex"]})
        entry = tomllib.loads((self.project / ".codex" / "config.toml").read_text())["mcp_servers"]["agent-comms"]
        self.assertEqual(entry["args"][:2], ["--actor-id", "u-architect"])

    def test_refusals(self) -> None:
        rc, result = self.cli("bind-seat", "t-architect", "--client", "codex")
        self.assertEqual(rc, 2)
        self.assertIn("no registry", result["error"])
        self.cli("setup", "--project-root", str(self.project), "--team", "t", "--runtimes", "fake", "--clients", "", "--human-id", HUMAN, "--yes")
        rc, result = self.cli("bind-seat", "t-fake-worker", "--client", "codex")
        self.assertIn("is not an architect", result["error"])
        rc, result = self.cli("bind-seat", "t-architect", "--client", "cursor")
        self.assertIn("--client must be one of claude, codex", result["error"])

    def test_claude_not_on_path_is_pending_with_the_same_line(self) -> None:
        self.cli("setup", "--project-root", str(self.project), "--team", "t", "--runtimes", "fake", "--clients", "", "--human-id", HUMAN, "--yes")
        self.clients.on_path.discard("claude")
        rc, result = self.cli("bind-seat", "t-architect", "--client", "claude")
        self.assertEqual(rc, 3, result)
        [pending] = result["mcp"]["pending"]
        self.assertEqual(pending["reason"], "claude is not on PATH")
        self.assertEqual(pending["fix"], f"agent-comms --db {self.db} bind-seat t-architect --client claude")


def _fresh_codex_home(codex_home: Path) -> Path:
    codex_home.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int((now + timedelta(days=365)).timestamp())}).encode()).decode().rstrip("=")
    (codex_home / "auth.json").write_text(json.dumps({"access_token": f"x.{payload}.x", "last_refresh": now.isoformat()}))
    return codex_home


class LaunchOptionsTest(unittest.TestCase):
    def test_reads_options_as_codex_does(self) -> None:
        launch = codex_binding.launch_options([
            "-c", "a=1", "--ask-for-approval", "-c", "exec", "--config=b=2", "-cc=3", "-c=d=4",
            "-p", "one", "--profile=two", "-Cdir", "-m", "--config", "prompt -c e=5", "--", "-c", "f=6",
        ])
        # "-c" after --ask-for-approval is its value, as is "--config" after -m.
        self.assertEqual(launch.overrides, ["a=1", "b=2", "c=3", "d=4"])
        self.assertEqual((launch.profile, launch.cd), ("two", "dir"))

    def test_which_overrides_set_our_server(self) -> None:
        ours = ['mcp_servers.agent-comms.args=[]', ' mcp_servers . "agent-comms" . env = {}', "mcp_servers={}", "mcp_servers.agent-comms={}", "mcp_servers.[=1"]
        theirs = ['mcp_servers.other.command="x"', 'model="o3"', "features.x=true"]
        for value in ours:
            self.assertTrue(codex_binding.overrides_server(value), value)
        for value in theirs:
            self.assertFalse(codex_binding.overrides_server(value), value)


class WorkerBindingTest(_Project):
    """A Codex dispatch binds the worker's server with -c and checks it first."""

    def setUp(self) -> None:
        super().setUp()
        self.codex_binary = install_fake_codex_custody(self)
        self.worker_home = _fresh_codex_home(self.root / "worker-home")
        self.context = DispatchContext(
            dispatch={"dispatch_id": "dispatch_20261003_120000_abcdef12", "policy_name": WORKER_DISPATCH_POLICY},
            recipient={
                "id": "t-codex-worker",
                "runtime": "codex",
                "project_root": str(self.project),
                "spawn": render_spawn("codex", "t-codex-worker", codex_home=str(self.worker_home)),
            },
            message={"id": "m"},
            ttl_seconds=30,
            expected_close_by="2026-10-03T12:00:30+00:00",
            db_path=str(self.db),
        )

    def dispatch(self, runner: StubCodex, hooks: StubHooks | None = None):
        hooks = hooks or StubHooks()
        adapter = CodexAdapter(mcp_runner=runner, hook_lister=hooks.lister, hook_runner=hooks.runner)
        spawned = supervisor.SupervisedSpawn(
            popen=types.SimpleNamespace(pid=4300), run_token="a" * 32, control_socket="/s", child_pid=4301, wrapper_pid=4300, run_dir="/r"
        )
        with mock.patch.object(paths, "dispatch_log_path", return_value=self.root / "worker.log"), \
                mock.patch.object(paths, "dispatch_events_path", return_value=self.root / "events.jsonl"), \
                mock.patch.object(supervisor, "janitor_sweep", return_value=[]), \
                mock.patch.object(supervisor, "reaper_registry", return_value=mock.MagicMock()), \
                mock.patch.object(supervisor, "spawn_supervised", return_value=spawned) as spawn:
            result = adapter.dispatch(self.context)
        return adapter, spawn, result

    def test_overrides_pin_the_worker_and_outrank_the_template(self) -> None:
        binding = CodexAdapter().mcp_binding(self.context)
        self.assertEqual(binding.args, ["--actor-id", "t-codex-worker", "--db", str(self.db)])
        self.assertEqual(binding.cwd, str(self.project))
        self.assertEqual(binding.env["WAKE_POLICY"], WORKER_DISPATCH_POLICY)
        self.assertEqual(binding.env["WAKE_POLICY_VERSION"], WORKER_DISPATCH_POLICY_VERSION)
        runner = StubCodex(resolved(binding))
        _adapter, spawn, result = self.dispatch(runner)
        child = spawn.call_args.args[0]
        overrides = [
            *codex_binding.config_overrides(binding, extra={"default_tools_approval_mode": "approve"}),
            *codex_hook.config_overrides(CodexAdapter.policy_hook_command(self.context)),
        ]
        self.assertEqual(child[0], str(self.codex_binary))
        self.assertEqual(child[1:1 + len(overrides)], overrides)
        self.assertEqual(child[1 + len(overrides):][:2], ["--ask-for-approval", "never"])
        # Each -c value is TOML Codex parses back to the binding.
        values = {}
        for flag, pair in zip(overrides[::2], overrides[1::2]):
            self.assertEqual(flag, "-c")
            if pair.startswith("hooks."):
                continue
            key, _, value = pair.partition("=")
            values[key.removeprefix("mcp_servers.agent-comms.")] = tomllib.loads(f"v = {value}")["v"]
        self.assertEqual(values["args"], binding.args)
        self.assertEqual(values["env"], binding.env)
        self.assertEqual((values["enabled"], values["cwd"], values["disabled_tools"], values["env_vars"]), (True, str(self.project), [], []))
        # The check ran codex itself, as the worker will: its home, cwd and overrides.
        [call] = runner.calls
        self.assertEqual(call["argv"], [str(self.codex_binary), *overrides, "mcp", "get", "agent-comms", "--json"])
        self.assertEqual(call["cwd"], str(self.project))
        self.assertEqual(call["env"]["CODEX_HOME"], str(self.worker_home))
        self.assertEqual(result.observed_values["codex_mcp_binding"], "verified")
        self.assertEqual(result.observed_values["codex_policy_hook"], "verified")
        # The prompt is still the last argument and separate stdout still applies.
        self.assertIn("t-codex-worker", child[-1])

    def test_a_mismatch_or_a_failed_check_starts_nothing(self) -> None:
        binding = CodexAdapter().mcp_binding(self.context)
        for runner, expected in (
            (StubCodex(resolved(binding, enabled_tools=["whoami"])), "enabled_tools restricts"),
            (StubCodex(resolved(binding, env={**binding.env, "OTHER": "1"})), "env OTHER='1' is set"),
            (StubCodex(returncode=1, stderr="boom"), "worker MCP binding not verified"),
            (StubCodex({"not": "a server"}), "transport is None"),
        ):
            with self.subTest(expected), self.assertRaisesRegex(McpBindingMismatch, expected):
                self.dispatch(runner)

    def with_stored(self, *extra: str) -> None:
        spawn = self.context.recipient["spawn"]
        args = list(spawn["args"])
        at = args.index("exec") + 1
        self.context.recipient["spawn"] = {**spawn, "args": [*args[:at], *extra, *args[at:]]}

    def test_stored_options_that_set_our_server_start_nothing(self) -> None:
        # INTEGRATION-004 F1: a later -c wins over the per-run binding.
        binding = CodexAdapter().mcp_binding(self.context)
        stored = {
            "dotted": ["-c", 'mcp_servers.agent-comms.args=["--actor-id","t-architect"]'],
            "long inline": ['--config=mcp_servers.agent-comms.enabled=false'],
            # Stored args are format templates, so a literal brace is doubled.
            "short inline": ['-cmcp_servers."agent-comms".env={{}}'],
            "whole table": ["--config", 'mcp_servers={{"agent-comms"={{command="/x"}}}}'],
        }
        for name, extra in stored.items():
            with self.subTest(name):
                self.setUp()
                self.with_stored(*extra)
                runner = StubCodex(resolved(binding))
                with self.assertRaisesRegex(McpBindingMismatch, "stored spawn args for t-codex-worker set agent-comms with -c"), \
                        mock.patch.object(supervisor, "spawn_supervised") as spawn:
                    self.dispatch(runner)
                spawn.assert_not_called()
                self.assertEqual(runner.calls, [])

    def test_the_check_loads_what_the_launch_loads(self) -> None:
        binding = CodexAdapter().mcp_binding(self.context)
        self.with_stored("-c", 'model="o3"', "--profile=other", "-C", "sub", "--config", "mcp_servers.other.command=\"/y\"")
        runner = StubCodex(resolved(binding))
        self.dispatch(runner)
        [call] = runner.calls
        overrides = [
            *codex_binding.config_overrides(binding, extra={"default_tools_approval_mode": "approve"}),
            *codex_hook.config_overrides(CodexAdapter.policy_hook_command(self.context)),
        ]
        # The template passes no --profile (slice 3b), so a stored one is the
        # profile the launch loads; stored -c follow ours, in launch order.
        self.assertEqual(call["argv"], [
            str(self.codex_binary), "-p", "other", *overrides, "-c", 'model="o3"', "-c", 'mcp_servers.other.command="/y"',
            "mcp", "get", "agent-comms", "--json",
        ])
        self.assertEqual(call["cwd"], str(self.project / "sub"))

    def test_the_policy_hook_is_checked_as_the_launch_loads_it(self) -> None:
        self.with_stored("-c", 'model="o3"', "-C", "sub")
        hooks = StubHooks()
        runner = StubCodex(resolved(CodexAdapter().mcp_binding(self.context)))
        _adapter, _spawn, result = self.dispatch(runner, hooks)
        [listed] = hooks.list_calls
        [mcp_call] = runner.calls
        # hooks/list loads what codex mcp get loaded: same binary, overrides, cwd, env.
        self.assertEqual(listed["codex"], str(self.codex_binary))
        self.assertEqual([listed["codex"], *listed["overrides"], "mcp", "get", "agent-comms", "--json"], mcp_call["argv"])
        self.assertEqual((listed["cwd"], listed["env"]), (mcp_call["cwd"], mcp_call["env"]))
        # The hook ran once, as Codex runs it, on a call it must deny.
        [ran] = hooks.run_calls
        command = CodexAdapter.policy_hook_command(self.context)
        self.assertEqual(ran["argv"][1:], ["-lc", command])
        self.assertEqual(json.loads(ran["input"])["tool_name"], codex_hook.SMOKE_TOOL)
        self.assertNotEqual(ran["env"]["ZDOTDIR"], os.environ.get("ZDOTDIR"))
        self.assertEqual(result.observed_values["codex_policy_hook"], "verified")

    def test_the_hook_command_carries_policy_root_and_fingerprint(self) -> None:
        words = shlex.split(CodexAdapter.policy_hook_command(self.context))
        self.assertEqual(words[:2], [sys.executable, str(paths.hooks_path())])
        self.assertEqual(words[2:8], ["--runtime", "codex", "--policy", WORKER_DISPATCH_POLICY, "--project-root", str(self.project)])
        self.assertEqual(words[8:], ["--sha256", hashlib.sha256(paths.hooks_path().read_bytes()).hexdigest()])

    def test_a_hook_codex_would_not_run_starts_nothing(self) -> None:
        binding = CodexAdapter().mcp_binding(self.context)
        for hooks, expected in (
            (StubHooks(entry={"trustStatus": "untrusted"}), "'untrusted', not trusted"),
            (StubHooks(entry={"trustStatus": "modified"}), "'modified', not trusted"),
            (StubHooks(entry={"enabled": False}), "the hook is disabled"),
            (StubHooks(listed=[]), "codex lists no hook"),
            (StubHooks(error="app-server hooks/list timed out after 5s"), "not verified: app-server hooks/list timed out"),
            (StubHooks(stdout=""), "does not run: the policy hook printed no decision"),
            (StubHooks(stdout=StubHooks.DENY.replace("deny", "allow")), "answered 'allow'"),
            (StubHooks(returncode=127), "exited 127"),
        ):
            with self.subTest(expected), self.assertRaisesRegex(PolicyHookUnverified, expected), \
                    mock.patch.object(supervisor, "spawn_supervised") as spawn:
                self.dispatch(StubCodex(resolved(binding)), hooks)
            spawn.assert_not_called()

    def test_stored_options_that_set_hooks_start_nothing(self) -> None:
        binding = CodexAdapter().mcp_binding(self.context)
        for extra in (
            ["-c", 'hooks.state={{"k"={{enabled=false}}}}'],
            ["--config=hooks.PreToolUse=[]"],
            ["--config", "hooks={{}}"],
        ):
            with self.subTest(extra):
                self.setUp()
                self.with_stored(*extra)
                runner, hooks = StubCodex(resolved(binding)), StubHooks()
                with self.assertRaisesRegex(PolicyHookUnverified, "stored spawn args for t-codex-worker set hooks"), \
                        mock.patch.object(supervisor, "spawn_supervised") as spawn:
                    self.dispatch(runner, hooks)
                spawn.assert_not_called()
                self.assertEqual((runner.calls, hooks.list_calls, hooks.run_calls), ([], [], []))

    def test_stored_hook_trust_bypass_starts_nothing(self) -> None:
        # The operator chose exact-hash trust, never the bypass, which would
        # also admit every untrusted hook (Lookout INTEGRATION-007 F1).
        for extra in (["--dangerously-bypass-hook-trust"], ["--json", "--dangerously-bypass-hook-trust=true"]):
            with self.subTest(extra):
                self.setUp()
                self.with_stored(*extra)
                runner, hooks = StubCodex(resolved(CodexAdapter().mcp_binding(self.context))), StubHooks()
                with self.assertRaisesRegex(PolicyHookUnverified, "--dangerously-bypass-hook-trust"), \
                        mock.patch.object(supervisor, "spawn_supervised") as spawn:
                    self.dispatch(runner, hooks)
                spawn.assert_not_called()
                self.assertEqual((runner.calls, hooks.list_calls, hooks.run_calls), ([], [], []))

    def test_home_is_bound_when_set(self) -> None:
        home = self.root / "h"
        with mock.patch.dict(os.environ, {paths.HOME_ENV: str(home)}):
            binding = CodexAdapter().mcp_binding(self.context)
        self.assertEqual(binding.args[-2:], ["--home", str(home)])
        self.assertEqual(binding.env["AGENT_COMMS_HOME"], str(home))


@unittest.skipIf(REAL_CODEX is None, "codex is not on PATH")
class RealCodexResolutionTest(_Project):
    """What Codex itself resolves (``codex mcp get --json``, no login, no
    model call) with scratch homes and projects."""

    def setUp(self) -> None:
        super().setUp()
        install_fake_codex_custody(self, forwarding_codex_script())
        self.user_config = self.codex_home / "config.toml"
        self.codex_home.mkdir()
        self.seat.apply(self.target(), mcp_clients.SeatState("absent", self.project), which=lambda name: None, run=subprocess.run)
        self.binding = self.seat.binding(self.target())

    def env(self, codex_home: Path | None = None) -> dict:
        return {"PATH": os.environ.get("PATH", ""), "HOME": str(self.root), "CODEX_HOME": str(codex_home or self.codex_home)}

    def resolve(self, cwd: Path, overrides=None, codex_home: Path | None = None):
        return codex_binding.resolve(subprocess.run, REAL_CODEX, cwd=str(cwd), env=self.env(codex_home), overrides=overrides)

    def write_user(self, extra: str = "", trusted: bool = True) -> None:
        text = (
            '[mcp_servers.agent-comms]\ncommand = "/bin/echo"\nargs = ["--actor-id", "user-level"]\n'
            f'enabled = false\ncwd = {json.dumps(str(self.root))}\nenv_vars = ["AGENT_COMMS_DB"]\n{extra}'
            '[mcp_servers.agent-comms.env]\nWAKE_POLICY = "other"\nAGENT_COMMS_DB = "/other.sqlite"\n\n'
        )
        if trusted:
            text += f'[projects.{json.dumps(str(self.project))}]\ntrust_level = "trusted"\n'
        self.user_config.write_text(text)

    @unittest.skipIf(REAL_GIT is None, "git is not on PATH")
    def test_project_entry_wins_over_a_differing_user_entry(self) -> None:
        subprocess.run([REAL_GIT, "init", "-q", str(self.project)], check=True)
        self.write_user()
        for cwd in (self.project, self.project / "sub"):
            cwd.mkdir(exist_ok=True)
            observed, error = self.resolve(cwd)
            self.assertIsNone(error)
            self.assertEqual(codex_binding.effective_problems(observed, self.binding), [], observed)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        observed, _ = self.resolve(elsewhere)
        self.assertEqual(observed["transport"]["args"], ["--actor-id", "user-level"])

    def test_outside_git_only_the_project_root_is_seated(self) -> None:
        self.write_user()
        observed, _ = self.resolve(self.project)
        self.assertEqual(codex_binding.effective_problems(observed, self.binding), [], observed)
        (self.project / "sub").mkdir()
        observed, _ = self.resolve(self.project / "sub")
        self.assertEqual(observed["transport"]["args"], ["--actor-id", "user-level"])

    def test_tool_restriction_from_the_user_layer_is_reported(self) -> None:
        self.write_user('enabled_tools = ["whoami"]\n')
        observed, _ = self.resolve(self.project)
        self.assertEqual(codex_binding.effective_problems(observed, self.binding), ["enabled_tools restricts the tools to ['whoami']"])

    def test_untrusted_project_resolves_the_user_entry(self) -> None:
        self.write_user(trusted=False)
        observed, _ = self.resolve(self.project)
        problems = codex_binding.effective_problems(observed, self.binding)
        self.assertTrue(any(problem.startswith("args are") for problem in problems), problems)

    def test_worker_in_a_seated_project_is_bound_only_by_its_overrides(self) -> None:
        # INTEGRATION-001 F1: the worker's home trusts the project, so the
        # architect's project entry would rebind the worker without -c.
        worker_home = self.root / "worker-home"
        worker_home.mkdir()
        (worker_home / "config.toml").write_text(provisioning.render_codex_base_config(str(self.project)))
        observed, _ = self.resolve(self.project, codex_home=worker_home)
        self.assertEqual(observed["transport"]["args"][:2], ["--actor-id", "t-architect"])
        context = DispatchContext(
            dispatch={"dispatch_id": "d", "policy_name": WORKER_DISPATCH_POLICY},
            recipient={"id": "t-codex-worker", "runtime": "codex", "project_root": str(self.project)},
            message={"id": "m"}, ttl_seconds=30, expected_close_by="", db_path=str(self.db),
        )
        adapter = CodexAdapter()
        overrides = adapter._runtime_prefix_args(context)
        observed, error = self.resolve(self.project, overrides=overrides, codex_home=worker_home)
        self.assertIsNone(error)
        self.assertEqual(codex_binding.effective_problems(observed, adapter.mcp_binding(context)), [], observed)

    def test_native_codex_trusts_exactly_the_policy_hook(self) -> None:
        worker_home = self.root / "worker-home"
        worker_home.mkdir()
        # A lower layer switching our hook off: the per-run state sets enabled too.
        (worker_home / "config.toml").write_text(
            provisioning.render_codex_base_config(str(self.project))
            + f"\n[hooks.state.{json.dumps(codex_hook.STATE_KEY)}]\nenabled = false\n"
        )
        context = DispatchContext(
            dispatch={"dispatch_id": "d", "policy_name": WORKER_DISPATCH_POLICY},
            recipient={"id": "t-codex-worker", "runtime": "codex", "project_root": str(self.project)},
            message={"id": "m"}, ttl_seconds=30, expected_close_by="", db_path=str(self.db),
        )
        overrides = CodexAdapter()._runtime_prefix_args(context)
        command = CodexAdapter.policy_hook_command(context)

        def problems(values: list[str], expected_command: str = command) -> list[str]:
            entries, error = codex_hook.list_hooks(REAL_CODEX, cwd=str(self.project), env=self.env(worker_home), overrides=values)
            self.assertIsNone(error)
            return codex_hook.trust_problems(entries, expected_command)

        self.assertEqual(problems(overrides), [])
        # The same trust for another command: Codex sees the change.
        other = command + " --extra"
        tampered = [value.replace(json.dumps(command)[1:-1], json.dumps(other)[1:-1], 1) if value.startswith("hooks.PreToolUse=") else value
                    for value in overrides]
        self.assertTrue(any("'modified', not trusted" in problem for problem in problems(tampered, other)))

    def native_dispatch_ok(self, worker_home: Path, *stored: str) -> None:
        """The binding check passes: the dispatch reaches the worker's start."""
        with self.assertRaisesRegex(AssertionError, "^started$"):
            self.native_dispatch(worker_home, *stored, expect_start=True)

    def native_dispatch(self, worker_home: Path, *stored: str, expect_start: bool = False):
        spawn = render_spawn("codex", "t-codex-worker", codex_home=str(worker_home))
        args = list(spawn["args"])
        at = args.index("exec") + 1
        spawn = {**spawn, "args": [*args[:at], *stored, *args[at:]]}
        context = DispatchContext(
            dispatch={"dispatch_id": "dispatch_20261003_120000_abcdef12", "policy_name": WORKER_DISPATCH_POLICY},
            recipient={"id": "t-codex-worker", "runtime": "codex", "project_root": str(self.project), "spawn": spawn},
            message={"id": "m"}, ttl_seconds=30, expected_close_by="2026-10-03T12:00:30+00:00", db_path=str(self.db),
        )
        with mock.patch.object(paths, "dispatch_log_path", return_value=self.root / "worker.log"), \
                mock.patch.object(paths, "dispatch_events_path", return_value=self.root / "events.jsonl"), \
                mock.patch.object(supervisor, "janitor_sweep", return_value=[]), \
                mock.patch.object(supervisor, "spawn_supervised", side_effect=AssertionError("started")) as started:
            try:
                CodexAdapter().dispatch(context)
            finally:
                if not expect_start:
                    started.assert_not_called()

    def test_native_codex_refuses_what_the_launch_would_rebind(self) -> None:
        worker_home = _fresh_codex_home(self.root / "worker-home")
        provisioning.write_codex_home(worker_home, "t-codex-worker", str(self.project))
        # INTEGRATION-004 F1: a stored -c naming the architect.
        stored = ["-c", f'mcp_servers.agent-comms.args=["--actor-id","t-architect","--db",{json.dumps(str(self.db))}]']
        with self.assertRaisesRegex(McpBindingMismatch, "stored spawn args"):
            self.native_dispatch(worker_home, *stored)
        # A profile the template no longer names is not loaded (slice 3b)...
        profile = worker_home / "t-codex-worker.config.toml"
        profile.write_text('[mcp_servers.agent-comms]\nenabled_tools = ["whoami"]\n')
        self.native_dispatch_ok(worker_home)
        # ...but a stored --profile loads it, and so does the check.
        with self.assertRaisesRegex(McpBindingMismatch, "enabled_tools restricts"):
            self.native_dispatch(worker_home, "--profile", "t-codex-worker")
        # The base config restricting the tools: Codex loads it, so the check does.
        profile.unlink()
        base = worker_home / "config.toml"
        base.write_text(base.read_text() + '\n[mcp_servers.agent-comms]\nenabled_tools = ["whoami"]\n')
        with self.assertRaisesRegex(McpBindingMismatch, "enabled_tools restricts"):
            self.native_dispatch(worker_home)


if __name__ == "__main__":
    unittest.main()
