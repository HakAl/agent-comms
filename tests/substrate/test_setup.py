import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import functools
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import cli, doctor, mcp_clients, mcp_server, paths, runtime_pins, setup
from agent_comms.adapters import DispatchContext
from agent_comms.adapters.codex import CodexAdapter
from agent_comms.policies import compile_policy
from agent_comms.runtime_pins import RuntimePin
from agent_comms.schema import ValidationError
from agent_comms.store import WORKER_DISPATCH_POLICY, Store

MCP_COMMAND = str(Path(sys.executable).parent / "agent-comms-mcp")
HUMAN = "01M36YTJV9XBW95S6ZWV47C4RG"


def _completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


class FakeClients:
    """``claude`` and ``codex`` as setup sees them: a ``which`` and a runner
    that records every argv and writes what the real ``mcp add`` writes."""

    def __init__(self, claude_config: Path, codex_config: Path) -> None:
        self.claude_config = claude_config
        self.codex_config = codex_config
        self.on_path = {"claude", "codex"}
        self.failing: set[str] = set()
        self.failing_subcommands: set[tuple[str, str]] = set()  # e.g. ("claude", "add")
        self.calls: list[tuple[list[str], str | None]] = []

    def which(self, name: str) -> str | None:
        return f"/fake/bin/{name}" if name in self.on_path else None

    def _claude(self) -> dict:
        try:
            return json.loads(self.claude_config.read_text())
        except FileNotFoundError:
            return {"projects": {}}

    def run(self, argv, **kwargs):
        argv = [str(item) for item in argv]
        self.calls.append((argv, kwargs.get("cwd")))
        tool = Path(argv[0]).name
        if argv[1:] == ["--version"]:
            return _completed(argv, stdout="codex-cli 0.157.0")
        if tool in self.failing or (argv[1:2] == ["mcp"] and (tool, argv[2]) in self.failing_subcommands):
            return _completed(argv, 1, stderr=f"{tool}: boom")
        if tool == "claude" and argv[1:3] == ["mcp", "add"]:
            key = os.path.realpath(kwargs["cwd"])
            separator = argv.index("--")
            data = self._claude()
            servers = data.setdefault("projects", {}).setdefault(key, {}).setdefault("mcpServers", {})
            if argv[3] in servers:
                return _completed(argv, 1, stderr=f"MCP server {argv[3]} already exists in local config")
            servers[argv[3]] = {"command": argv[separator + 1], "args": argv[separator + 2:]}
            self.claude_config.write_text(json.dumps(data))
        elif tool == "claude" and argv[1:3] == ["mcp", "remove"]:
            key = os.path.realpath(kwargs["cwd"])
            data = self._claude()
            data.get("projects", {}).get(key, {}).get("mcpServers", {}).pop(argv[3], None)
            self.claude_config.write_text(json.dumps(data))
        elif tool == "codex" and argv[1:3] == ["mcp", "add"]:
            separator = argv.index("--")
            rendered = ", ".join(json.dumps(item) for item in argv[separator + 2:])
            self.codex_config.parent.mkdir(parents=True, exist_ok=True)
            self.codex_config.write_text(f"[mcp_servers.{argv[3]}]\ncommand = {json.dumps(argv[separator + 1])}\nargs = [{rendered}]\n")
        else:
            raise AssertionError(f"unexpected command {argv!r}")
        return _completed(argv)

    def commands(self, tool: str) -> list[list[str]]:
        return [argv for argv, _cwd in self.calls if Path(argv[0]).name == tool and argv[1:2] == ["mcp"]]


class _Scratch(unittest.TestCase):
    """A project, a ledger and fake clients under a temporary directory; the
    registry and codex auth live under the scratch HOME's runtime root, so the
    canonical-registry path bootstrap_store checks holds, and are removed
    after each test."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "my project"
        self.project.mkdir()
        self.db = self.root / "ledger dir" / "ledger.sqlite"
        self.db.parent.mkdir()
        self.claude_config = self.root / "claude.json"
        self.codex_home = self.root / "codex"
        self.codex_config = self.codex_home / "config.toml"
        env = mock.patch.dict(
            os.environ,
            {
                mcp_clients.CLAUDE_CONFIG_ENV: str(self.claude_config),
                "CODEX_HOME": str(self.codex_home),
                runtime_pins.CLAUDE_VERSIONS_DIR_ENV: str(self.root / "custody"),
                "AGENT_COMMS_CODEX_CUSTODY_ROOT": str(self.root / "codex-homes"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.registry = paths.actors_config_path()
        self.auth_dir = paths.runtime_codex_auth_source().parent
        self.auth_dir_existed = self.auth_dir.exists()
        self.addCleanup(self._restore_runtime_root)
        self.clients = FakeClients(self.claude_config, self.codex_config)
        # The CLI path reaches run_setup through the module attribute, so the
        # fakes are injected there; direct calls pass them explicitly.
        injected = functools.partial(setup.run_setup, which=self.clients.which, run=self.clients.run)
        patch = mock.patch.object(setup, "run_setup", injected)
        patch.start()
        self.addCleanup(patch.stop)

    def _restore_runtime_root(self) -> None:
        self.registry.unlink(missing_ok=True)
        self.registry.with_name("agents.json").unlink(missing_ok=True)
        if not self.auth_dir_existed and self.auth_dir.exists():
            shutil.rmtree(self.auth_dir)

    def run_cli(self, *argv, db: bool = True) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cli.run([*(["--db", str(self.db)] if db else []), "setup", *argv])
        return rc, json.loads(buffer.getvalue())

    def setup(self, *argv) -> tuple[int, dict]:
        return self.run_cli("--project-root", str(self.project), "--yes", *argv)

    def read_registry(self) -> dict:
        return json.loads(self.registry.read_text())

    def expected_argv(self) -> list[str]:
        return [MCP_COMMAND, "--actor-id", "my-project-architect", "--db", str(self.db)]


class HelpersTest(unittest.TestCase):
    def test_ulid_shape_and_time_prefix(self) -> None:
        first = setup.new_ulid()
        self.assertEqual(len(first), 26)
        self.assertTrue(set(first) <= set(setup.CROCKFORD))
        self.assertNotEqual(first, setup.new_ulid())
        self.assertEqual(setup.new_ulid(0)[:10], "0000000000")
        self.assertEqual(setup.new_ulid(1)[:10], "0000000001")
        self.assertEqual(setup.new_ulid(32)[:10], "0000000010")

    def test_team_from_path_and_parse_list(self) -> None:
        self.assertEqual(setup.team_from_path("/srv/My Project_v2"), "my-project-v2")
        self.assertEqual(setup.team_from_path("/srv/---"), "")
        self.assertIsNone(setup.parse_list(None))
        self.assertEqual(setup.parse_list(""), [])
        self.assertEqual(setup.parse_list(" claude, codex ,"), ["claude", "codex"])
        self.assertEqual(setup.parse_list(["a", " b "]), ["a", "b"])

    def test_detect_runtimes(self) -> None:
        with mock.patch.object(setup, "current_platform", return_value="nowhere-x"):
            self.assertEqual(setup.detect_runtimes(which=lambda _name: None), ["fake"])
            self.assertEqual(setup.detect_runtimes(which=lambda name: "/bin/codex" if name == "codex" else None), ["codex"])
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "9.9.9"
            binary.write_text("")
            pin = RuntimePin("claude", "p", "9.9.9", "exact", "2026-09-26", "x")
            with mock.patch.object(setup, "pin_for", return_value=pin), \
                    mock.patch.dict(os.environ, {runtime_pins.CLAUDE_VERSIONS_DIR_ENV: temp_dir}):
                self.assertEqual(setup.detect_runtimes(which=lambda _name: "/bin/codex"), ["claude", "codex"])


class SetupCommandTest(_Scratch):
    def test_fresh_machine_writes_registry_registers_and_binds_both_seats(self) -> None:
        rc, result = self.setup("--runtimes", "fake", "--clients", "claude,codex", "--human", "alice", "--human-id", HUMAN)
        self.assertEqual(rc, 0, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["team"], "my-project")
        self.assertEqual(result["actors"], ["my-project-architect", "my-project-fake-worker"])
        self.assertEqual(result["human"], {"id": HUMAN, "display_name": "alice", "reused": False})
        self.assertEqual(result["project_root"], str(self.project.resolve()))
        self.assertEqual(result["codex_homes"], [])
        self.assertIsNone(result["claude_custody"])
        # The registry: the human, the architect, one worker per runtime, and the seats.
        document = self.read_registry()
        self.assertEqual(document["actors"][HUMAN], {"kind": "human", "display_name": "alice"})
        architect = document["actors"]["my-project-architect"]
        self.assertEqual((architect["role"], architect["team"], architect["project_root"]), ("architect", "my-project", str(self.project.resolve())))
        worker = document["actors"]["my-project-fake-worker"]
        self.assertEqual((worker["role"], worker["runtime"], worker["owner"], worker["capabilities"]), ("worker", "fake", "my-project-architect", ["demo"]))
        self.assertEqual(document["seats"], {"my-project-architect": ["claude", "codex"]})
        # The ledger: rows exist with the spawn rendered.
        actors = {actor["id"]: actor for actor in Store(self.db).list_actors()}
        self.assertEqual(actors[HUMAN]["kind"], "human")
        self.assertEqual(actors["my-project-fake-worker"]["spawn"]["command"], "{python}")
        # The seats: the exact client commands, carrying --db because the ledger is explicit.
        self.assertEqual(self.clients.commands("claude"), [["claude", "mcp", "add", "agent-comms", "--scope", "local", "--", *self.expected_argv()]])
        self.assertEqual([cwd for argv, cwd in self.clients.calls if argv[0] == "claude"], [str(self.project.resolve())])
        self.assertEqual(self.clients.commands("codex"), [["codex", "mcp", "add", "agent-comms", "--", *self.expected_argv()]])
        self.assertEqual([item["client"] for item in result["mcp"]["applied"]], ["claude", "codex"])
        self.assertEqual(result["mcp"]["pending"], [])
        # Doctor ran in-process and is clean; the next step is doctor itself.
        self.assertEqual(result["doctor"], {"ok": True, "fixes": []})
        self.assertEqual(result["next"], [f"agent-comms --db {shlex_quote(self.db)} doctor"])
        # And doctor agrees from a fresh process view.
        report = doctor.run_doctor(db_path=self.db, db_explicit=True)
        by_id = {item["id"]: item for item in report["checks"]}
        self.assertEqual(by_id["mcp:my-project-architect:claude"]["status"], doctor.OK, by_id)
        self.assertEqual(by_id["mcp:my-project-architect:codex"]["status"], doctor.OK, by_id)
        self.assertEqual(report["fixes"], [])

    def test_default_ledger_binds_no_db(self) -> None:
        store = Store(self.db, is_default_db_open=True)
        result = setup.run_setup(
            store, db_path=self.db, db_explicit=False, project_root=str(self.project), team="t",
            runtimes=["fake"], clients=["claude", "codex"], human_name="alice", human_id=HUMAN,
            which=self.clients.which, run=self.clients.run,
        )
        self.assertTrue(result["ok"], result)
        argv = [MCP_COMMAND, "--actor-id", "t-architect"]
        self.assertEqual(self.clients.commands("claude")[0][-3:], argv)
        self.assertEqual(self.clients.commands("codex")[0][-3:], argv)
        self.assertEqual(result["next"][0], "agent-comms doctor")

    def test_codex_worker_gets_a_custody_home_with_a_dangling_auth_link(self) -> None:
        pins = {"codex": RuntimePin("codex", "test-platform", "0.157.0", "exact", "2026-09-26")}
        stderr = io.StringIO()
        with mock.patch.object(doctor, "current_platform", return_value="test-platform"), \
                mock.patch.object(doctor, "pin_for", side_effect=lambda runtime, platform=None: pins[runtime]), \
                contextlib.redirect_stderr(stderr):
            rc, result = self.setup("--runtimes", "codex,fake", "--human-id", HUMAN)
        # Setup itself succeeded; doctor reports the login that is still missing.
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["clients"], ["codex"])
        home = self.root / "codex-homes" / "default" / "my-project-codex-worker"
        self.assertEqual(result["codex_homes"], [str(home)])
        self.assertTrue((home / "config.toml").is_file())
        self.assertTrue((home / "my-project-codex-worker.config.toml").is_file())
        auth = home / "auth.json"
        self.assertTrue(auth.is_symlink())
        self.assertFalse(auth.exists())
        self.assertEqual(os.readlink(auth), str(paths.runtime_codex_auth_source().resolve()))
        self.assertIn("codex login", stderr.getvalue())
        actors = {actor["id"]: actor for actor in Store(self.db).list_actors()}
        worker = actors["my-project-codex-worker"]
        self.assertEqual(worker["spawn"]["env"]["CODEX_HOME"], str(home))
        # SETUP-002 F1: the worker's config.toml is its only ledger binding (the
        # adapter's child environment carries no AGENT_COMMS_DB), so it names
        # the ledger setup registered the worker in.
        config = tomllib.loads((home / "config.toml").read_text())
        self.assertEqual(config["mcp_servers"]["agent-comms"]["args"], ["--actor-id", "my-project-codex-worker", "--db", str(self.db)])
        self.assertEqual(config["mcp_servers"]["agent-comms"]["command"], MCP_COMMAND)
        context = DispatchContext(
            dispatch={"dispatch_id": "dispatch_20260927_120000_abcdef12", "policy_name": WORKER_DISPATCH_POLICY},
            recipient=worker, message={"id": "m"}, ttl_seconds=30, expected_close_by="2026-09-27T12:00:30+00:00", db_path=str(self.db),
        )
        with mock.patch.dict(os.environ, {"AGENT_COMMS_DB": "/elsewhere/ledger.sqlite"}):
            env = CodexAdapter()._build_env(context, compile_policy(WORKER_DISPATCH_POLICY), worker["spawn"], zdotdir=self.root / "zdot")
        self.assertNotIn("AGENT_COMMS_DB", env)
        self.assertEqual(env["CODEX_HOME"], str(home))
        login = f"CODEX_HOME={shlex_quote(self.auth_dir)} codex login"
        self.assertIn(login, result["doctor"]["fixes"])
        self.assertIn(login, result["next"])
        self.assertFalse(result["doctor"]["ok"])
        self.assertEqual(self.clients.commands("codex"), [["codex", "mcp", "add", "agent-comms", "--", MCP_COMMAND, "--actor-id", "my-project-architect", "--db", str(self.db)]])

    def test_doctor_repair_of_a_codex_home_keeps_the_ledger_binding(self) -> None:
        # SETUP-003: the repair doctor prescribes for a broken custody home must
        # rebuild what setup built, ledger binding included, and the server
        # constructed from the rewritten binding must find the worker.
        pins = {"codex": RuntimePin("codex", "test-platform", "0.157.0", "exact", "2026-09-26")}
        platform = mock.patch.object(doctor, "current_platform", return_value="test-platform")
        pin_for = mock.patch.object(doctor, "pin_for", side_effect=lambda runtime, platform=None: pins[runtime])
        with platform, pin_for, contextlib.redirect_stderr(io.StringIO()):
            rc, result = self.setup("--runtimes", "codex", "--clients", "", "--human-id", HUMAN)
        self.assertEqual(rc, 0, result)
        home = Path(result["codex_homes"][0])
        (home / "my-project-codex-worker.config.toml").unlink()
        (home / "auth.json").unlink()
        # Shared auth is fresh, so the only problem is the broken home.
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        (self.auth_dir / "auth.json").write_text(json.dumps({"last_refresh": "2026-09-27T00:00:00+00:00"}))
        with platform, pin_for:
            report = doctor.run_doctor(db_path=self.db, db_explicit=True, which=self.clients.which, run=self.clients.run)
        by_id = {item["id"]: item for item in report["checks"]}
        repair = by_id["runtime:codex:home:my-project-codex-worker"]
        self.assertEqual(repair["status"], doctor.FAIL, repair)
        argv = shlex_split(repair["fix"])
        self.assertEqual(argv[:3], ["agent-comms", "--db", str(self.db)])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.run(argv[1:])
        self.assertEqual(rc, 0, buffer.getvalue())
        config = tomllib.loads((home / "config.toml").read_text())
        self.assertEqual(config["mcp_servers"]["agent-comms"]["args"], ["--actor-id", "my-project-codex-worker", "--db", str(self.db)])
        self.assertTrue((home / "my-project-codex-worker.config.toml").is_file())
        auth = home / "auth.json"
        self.assertTrue(auth.is_symlink())
        self.assertEqual(os.readlink(auth), str(paths.runtime_codex_auth_source().resolve()))
        # The real server, built from the repaired binding, binds the worker in the selected ledger.
        args = config["mcp_servers"]["agent-comms"]["args"]
        server = mcp_server.create_server(db_path=args[args.index("--db") + 1], actor_id="my-project-codex-worker")
        self.assertIsNotNone(server)
        with self.assertRaisesRegex(ValidationError, "unknown actor"):
            mcp_server.create_server(db_path=str(self.root / "other.sqlite"), actor_id="my-project-codex-worker")
        with platform, pin_for:
            report = doctor.run_doctor(db_path=self.db, db_explicit=True, which=self.clients.which, run=self.clients.run)
        by_id = {item["id"]: item for item in report["checks"]}
        self.assertEqual(by_id["runtime:codex:home:my-project-codex-worker"]["status"], doctor.OK, by_id)

    def test_valid_existing_teams_bootstrap_whatever_the_file_order(self) -> None:
        # SETUP-003: a worker that sorts before its owner, both in the registry
        # and both in the live ledger, is valid; so is an owner that lives only
        # in the live ledger. Neither is refused by the dry run.
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "actors": {
                HUMAN: {"kind": "human", "display_name": "alice"},
                "a-worker": {"kind": "agent", "display_name": "a-worker", "team": "old", "role": "worker", "runtime": "fake", "owner": "z-architect", "project_root": str(self.project), "capabilities": []},
                "z-architect": {"kind": "agent", "display_name": "z-architect", "team": "old", "role": "architect", "project_root": str(self.project), "capabilities": []},
            }
        }
        self.registry.write_text(json.dumps(document, sort_keys=True))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(cli.run(["--db", str(self.db), "bootstrap"]), 0, buffer.getvalue())
        rc, result = self.setup("--runtimes", "fake", "--clients", "")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["team"], "my-project")
        ids = {actor["id"] for actor in Store(self.db).list_actors()}
        self.assertTrue({"a-worker", "z-architect", "my-project-architect"} <= ids)
        # An owner registered only in the ledger, not in the registry file.
        Store(self.db).register_agent_actor("ledger-only-architect", "old2", "architect", str(self.project), [])
        document = json.loads(self.registry.read_text())
        document["actors"]["b-worker"] = {"kind": "agent", "display_name": "b-worker", "team": "old2", "role": "worker", "runtime": "fake", "owner": "ledger-only-architect", "project_root": str(self.project), "capabilities": []}
        self.registry.write_text(json.dumps(document, sort_keys=True))
        other = self.root / "other"
        other.mkdir()
        rc, result = self.run_cli("--project-root", str(other), "--runtimes", "fake", "--clients", "", "--yes")
        self.assertEqual(rc, 0, result)
        # But an owner that exists nowhere is still refused before the write.
        document["actors"]["c-worker"] = dict(document["actors"]["b-worker"], display_name="c-worker", owner="nobody-architect")
        before = json.dumps(document, sort_keys=True)
        self.registry.write_text(before)
        third = self.root / "third"
        third.mkdir()
        rc, result = self.run_cli("--project-root", str(third), "--runtimes", "fake", "--clients", "", "--yes")
        self.assertEqual(rc, 2, result)
        self.assertIn("unknown actor: nobody-architect", result["error"])
        self.assertEqual(self.registry.read_text(), before)

    def test_claude_runtime_reports_the_custody_binary(self) -> None:
        pin = RuntimePin("claude", "test-platform", "9.9.9", "exact", "2026-09-26", "x")
        with mock.patch.object(setup, "current_platform", return_value="test-platform"), \
                mock.patch.object(setup, "pin_for", return_value=pin), \
                mock.patch.object(doctor, "current_platform", return_value="test-platform"), \
                mock.patch.object(doctor, "pin_for", return_value=pin):
            rc, result = self.setup("--runtimes", "claude", "--clients", "", "--human-id", HUMAN)
        self.assertEqual(rc, 0, result)
        binary = self.root / "custody" / "9.9.9"
        self.assertEqual(result["claude_custody"], {"version": "9.9.9", "path": str(binary), "present": False})
        self.assertFalse(result["doctor"]["ok"])
        self.assertTrue(any("download Claude Code 9.9.9" in fix for fix in result["next"]), result["next"])
        with mock.patch.object(setup, "current_platform", return_value="nowhere-x"), \
                mock.patch.object(setup, "pin_for", side_effect=runtime_pins.PlatformNotCertified("not certified: nowhere-x")):
            result = setup.run_setup(
                Store(self.db), db_path=self.db, db_explicit=True, project_root=str(self.project), team="other",
                runtimes=["claude"], clients=[], human_id=HUMAN, human_name="a", which=self.clients.which, run=self.clients.run,
            )
        self.assertFalse(result["claude_custody"]["present"])
        self.assertIn("nowhere-x", result["claude_custody"]["detail"])

    def test_failing_client_lands_in_pending_and_exits_three(self) -> None:
        self.clients.failing.add("claude")
        rc, result = self.setup("--runtimes", "fake", "--clients", "claude,codex", "--human-id", HUMAN)
        self.assertEqual(rc, 3, result)
        self.assertFalse(result["ok"])
        [pending] = result["mcp"]["pending"]
        self.assertEqual(pending["client"], "claude")
        self.assertIn("exited 1: claude: boom", pending["reason"])
        self.assertEqual(pending["fix"], f"cd {shlex_quote(self.project.resolve())} && claude mcp add agent-comms --scope local -- {MCP_COMMAND} --actor-id my-project-architect --db {shlex_quote(self.db)}")
        self.assertEqual([item["client"] for item in result["mcp"]["applied"]], ["codex"])
        # The registry and ledger were still written; doctor names the same fix.
        self.assertTrue(self.registry.exists())
        self.assertIn(pending["fix"], result["doctor"]["fixes"])
        self.assertEqual(result["next"][1], pending["fix"])

    def test_missing_client_on_path_is_pending_with_the_command_to_run(self) -> None:
        self.clients.on_path.discard("codex")
        rc, result = self.setup("--runtimes", "fake", "--clients", "claude,codex", "--human-id", HUMAN)
        self.assertEqual(rc, 3)
        [pending] = result["mcp"]["pending"]
        self.assertEqual((pending["client"], pending["reason"]), ("codex", "codex is not on PATH"))
        self.assertEqual(pending["fix"], f"codex mcp add agent-comms -- {MCP_COMMAND} --actor-id my-project-architect --db {shlex_quote(self.db)}")
        self.assertEqual(self.clients.commands("codex"), [])

    def test_relative_ledger_path_is_persisted_and_compared_absolute(self) -> None:
        # SETUP-002 F2: the seat runs from the project directory, so a ledger
        # path typed relative to setup's directory must be stored absolute.
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cli.run(["--db", "ledger dir/ledger.sqlite", "setup", "--project-root", str(self.project), "--runtimes", "fake", "--clients", "claude,codex", "--human-id", HUMAN, "--yes"])
        result = json.loads(buffer.getvalue())
        self.assertEqual(rc, 0, result)
        for tool in ("claude", "codex"):
            [command] = self.clients.commands(tool)
            stored = command[-1]
            self.assertTrue(os.path.isabs(stored), command)
            self.assertEqual(Path(stored).resolve(), self.db.resolve())
        self.assertEqual(result["doctor"], {"ok": True, "fixes": []})
        self.assertTrue(os.path.isabs(shlex_split(result["next"][0])[2]), result["next"])
        # Doctor typed from another directory with another relative spelling agrees.
        os.chdir(self.project)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cli.run(["--db", os.path.join("..", "ledger dir", "ledger.sqlite"), "doctor"])
        self.assertEqual(rc, 0, buffer.getvalue())
        # A seat that stores a relative path is bound to whatever directory the client runs in: a failure.
        self.write_claude_seat_relative()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cli.run(["--db", str(self.db), "doctor"])
        report = json.loads(buffer.getvalue())
        self.assertEqual(rc, 3)
        by_id = {item["id"]: item for item in report["checks"]}
        self.assertIn("bound to ledger 'ledger dir/ledger.sqlite'", by_id["mcp:my-project-architect:claude"]["detail"])

    def write_claude_seat_relative(self) -> None:
        key = os.path.realpath(str(self.project))
        self.claude_config.write_text(json.dumps({"projects": {key: {"mcpServers": {"agent-comms": {"command": MCP_COMMAND, "args": ["--actor-id", "my-project-architect", "--db", "ledger dir/ledger.sqlite"]}}}}}))

    def test_human_id_must_not_collide_with_an_existing_or_new_actor(self) -> None:
        # SETUP-002 F3: a chosen human id never replaces another actor.
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        before = json.dumps({"actors": {"operator-key": {"kind": "system", "display_name": "ops"}}})
        self.registry.write_text(before)
        rc, result = self.setup("--runtimes", "fake", "--clients", "", "--human", "alice", "--human-id", "operator-key")
        self.assertEqual(rc, 2, result)
        self.assertIn("--human-id 'operator-key' is already an actor", result["error"])
        self.assertIn("(system)", result["error"])
        rc, result = self.setup("--runtimes", "fake", "--clients", "", "--human", "alice", "--human-id", "my-project-architect")
        self.assertEqual(rc, 2, result)
        self.assertIn("collides with the new team", result["error"])
        self.assertEqual(self.registry.read_text(), before)
        self.assertFalse(self.db.exists())

    def test_existing_entry_bootstrap_would_refuse_is_refused_before_the_write(self) -> None:
        # SETUP-002 F4: everything bootstrap_store checks is checked on the
        # candidate registry first, so a refusal never leaves the new team
        # written and a retry refused as 'already exists'.
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "actors": {
                HUMAN: {"kind": "human", "display_name": "alice"},
                "old-architect": {"kind": "agent", "display_name": "old-architect", "team": "old", "role": "architect", "project_root": str(self.project), "capabilities": []},
                "old-fake-worker": {"kind": "agent", "display_name": "old-fake-worker", "team": "old", "role": "worker", "runtime": "fake", "project_root": str(self.project), "capabilities": []},
            }
        }
        before = json.dumps(document)
        self.registry.write_text(before)
        rc, result = self.setup("--runtimes", "fake", "--clients", "claude")
        self.assertEqual(rc, 2, result)
        self.assertIn("would not bootstrap", result["error"])
        self.assertIn("worker actor old-fake-worker requires owner", result["error"])
        self.assertEqual(self.registry.read_text(), before)
        self.assertFalse(self.db.exists())
        self.assertEqual(self.clients.calls, [])
        # Once the existing entry is repaired the same command succeeds: nothing was half-done.
        document["actors"]["old-fake-worker"]["owner"] = "old-architect"
        self.registry.write_text(json.dumps(document))
        rc, result = self.setup("--runtimes", "fake", "--clients", "claude")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["team"], "my-project")
        # Other bootstrap refusals are caught the same way: a forbidden field, an owner that is not an architect.
        for entry, expected in (
            ({"kind": "human", "display_name": "bob", "project_root": "/x"}, "must not define"),
            ({"kind": "agent", "display_name": "w", "team": "old", "role": "worker", "runtime": "fake", "project_root": str(self.project), "owner": HUMAN, "capabilities": []}, "owner must name an agent architect"),
        ):
            with self.subTest(expected=expected):
                self._restore_runtime_root()
                self.registry.write_text(json.dumps({"actors": {HUMAN: {"kind": "human", "display_name": "alice"}, "old-architect": document["actors"]["old-architect"], "odd-one": entry}}))
                rc, result = self.setup("--runtimes", "fake", "--clients", "")
                self.assertEqual(rc, 2, result)
                self.assertIn(expected, result["error"])

    def test_failed_add_after_a_successful_remove_prescribes_add_only(self) -> None:
        self.clients.run(["claude", "mcp", "add", "agent-comms", "--scope", "local", "--", MCP_COMMAND, "--actor-id", "old-architect", "--db", str(self.db)], cwd=str(self.project))
        self.clients.calls.clear()
        self.clients.failing_subcommands.add(("claude", "add"))
        rc, result = self.setup("--runtimes", "fake", "--clients", "claude", "--replace-seat", "--human-id", HUMAN)
        self.assertEqual(rc, 3, result)
        self.assertEqual([argv[2] for argv in self.clients.commands("claude")], ["remove", "add"])
        [pending] = result["mcp"]["pending"]
        self.assertNotIn("remove", pending["fix"])
        self.assertEqual(pending["fix"], f"cd {shlex_quote(self.project.resolve())} && claude mcp add agent-comms --scope local -- {MCP_COMMAND} --actor-id my-project-architect --db {shlex_quote(self.db)}")

    def test_taken_seat_refuses_before_anything_is_written(self) -> None:
        self.clients.run(["claude", "mcp", "add", "agent-comms", "--scope", "local", "--", MCP_COMMAND, "--actor-id", "old-architect", "--db", str(self.db)], cwd=str(self.project))
        before_claude = self.claude_config.read_text()
        self.clients.calls.clear()
        rc, result = self.setup("--runtimes", "fake", "--clients", "claude,codex", "--human-id", HUMAN)
        self.assertEqual(rc, 2, result)
        self.assertIn("seat already taken", result["error"])
        self.assertIn("bound to actor 'old-architect'", result["error"])
        self.assertIn("--replace-seat", result["error"])
        self.assertFalse(self.registry.exists())
        self.assertFalse(self.db.exists())
        self.assertEqual(self.claude_config.read_text(), before_claude)
        self.assertFalse(self.codex_config.exists())
        self.assertEqual(self.clients.calls, [])

    def test_wrong_command_and_malformed_entries_are_named_in_the_refusal(self) -> None:
        self.clients.run(["codex", "mcp", "add", "agent-comms", "--", "/elsewhere/agent-comms-mcp", *self.expected_argv()[1:]], cwd=None)
        rc, result = self.setup("--runtimes", "fake", "--clients", "codex", "--human-id", HUMAN)
        self.assertEqual(rc, 2, result)
        self.assertIn(f"runs '/elsewhere/agent-comms-mcp', expected '{MCP_COMMAND}'", result["error"])
        self.assertNotIn("to ledger", result["error"])
        self.clients.run(["codex", "mcp", "add", "agent-comms", "--", MCP_COMMAND, "--actor-id", "my-project-architect", "--db", str(self.db), "--db", str(self.db)], cwd=None)
        rc, result = self.setup("--runtimes", "fake", "--clients", "codex", "--human-id", HUMAN)
        self.assertEqual(rc, 2, result)
        self.assertIn("malformed arguments (repeated or valueless --db)", result["error"])
        self.assertFalse(self.registry.exists())

    def test_same_architect_on_another_ledger_is_a_taken_seat(self) -> None:
        self.clients.run(["codex", "mcp", "add", "agent-comms", "--", MCP_COMMAND, "--actor-id", "my-project-architect"], cwd=None)
        rc, result = self.setup("--runtimes", "fake", "--clients", "codex", "--human-id", HUMAN)
        self.assertEqual(rc, 2, result)
        self.assertIn("binds my-project-architect to ledger None", result["error"])
        self.assertFalse(self.registry.exists())

    def test_replace_seat_hands_the_seat_over(self) -> None:
        # The old team holds both seats; its registry entry records them.
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        self.registry.write_text(json.dumps({
            "actors": {
                HUMAN: {"kind": "human", "display_name": "alice"},
                "old-architect": {"kind": "agent", "display_name": "old-architect", "team": "old", "role": "architect", "project_root": str(self.project), "capabilities": []},
            },
            "seats": {"old-architect": ["claude", "codex"]},
        }))
        old_argv = [MCP_COMMAND, "--actor-id", "old-architect", "--db", str(self.db)]
        self.clients.run(["claude", "mcp", "add", "agent-comms", "--scope", "local", "--", *old_argv], cwd=str(self.project))
        self.clients.run(["codex", "mcp", "add", "agent-comms", "--", *old_argv], cwd=None)
        self.clients.calls.clear()
        rc, result = self.setup("--runtimes", "fake", "--clients", "claude,codex", "--replace-seat")
        self.assertEqual(rc, 0, result)
        self.assertEqual(
            self.clients.commands("claude"),
            [
                ["claude", "mcp", "remove", "agent-comms", "--scope", "local"],
                ["claude", "mcp", "add", "agent-comms", "--scope", "local", "--", *self.expected_argv()],
            ],
        )
        self.assertEqual(self.clients.commands("codex"), [["codex", "mcp", "add", "agent-comms", "--", *self.expected_argv()]])
        document = self.read_registry()
        self.assertEqual(document["seats"], {"old-architect": [], "my-project-architect": ["claude", "codex"]})
        self.assertEqual(result["human"], {"id": HUMAN, "display_name": "alice", "reused": True})
        self.assertEqual(mcp_clients.read_claude_seat(self.claude_config, self.project).server().args, self.expected_argv()[1:])
        self.assertEqual(tomllib.loads(self.codex_config.read_text())["mcp_servers"]["agent-comms"]["args"], self.expected_argv()[1:])
        # The old architect is still registered; doctor no longer expects its seats.
        ids = {actor["id"] for actor in Store(self.db).list_actors()}
        self.assertIn("old-architect", ids)
        self.assertEqual(result["doctor"], {"ok": True, "fixes": []})

    def test_already_bound_seat_is_left_alone(self) -> None:
        # Bound by hand through the symlink an installed package puts on PATH: the same server.
        link_dir = self.root / "bin on path"
        link_dir.mkdir()
        (link_dir / "agent-comms-mcp").symlink_to(MCP_COMMAND)
        self.clients.run(["codex", "mcp", "add", "agent-comms", "--", str(link_dir / "agent-comms-mcp"), *self.expected_argv()[1:]], cwd=None)
        self.clients.calls.clear()
        rc, result = self.setup("--runtimes", "fake", "--clients", "codex", "--human-id", HUMAN)
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["mcp"]["applied"], [{"client": "codex", "commands": [], "detail": "already bound to my-project-architect"}])
        self.assertEqual(self.clients.commands("codex"), [])

    def test_second_run_for_the_same_team_refuses_without_touching_files(self) -> None:
        rc, _ = self.setup("--runtimes", "fake", "--clients", "claude", "--human-id", HUMAN)
        self.assertEqual(rc, 0)
        registry_before = self.registry.read_text()
        claude_before = self.claude_config.read_text()
        self.clients.calls.clear()
        rc, result = self.setup("--runtimes", "fake,codex", "--clients", "claude")
        self.assertEqual(rc, 2, result)
        self.assertIn("team 'my-project' already exists", result["error"])
        self.assertIn("another --team", result["error"])
        self.assertEqual(self.registry.read_text(), registry_before)
        self.assertEqual(self.claude_config.read_text(), claude_before)
        self.assertEqual(self.clients.calls, [])

    def test_second_team_reuses_the_human_and_a_free_codex_seat_is_the_only_seat(self) -> None:
        rc, _ = self.setup("--runtimes", "fake", "--clients", "claude", "--human", "alice", "--human-id", HUMAN)
        self.assertEqual(rc, 0)
        other = self.root / "other"
        other.mkdir()
        rc, result = self.run_cli("--project-root", str(other), "--runtimes", "fake", "--clients", "codex", "--human", "bob", "--yes")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["team"], "other")
        self.assertEqual(result["human"], {"id": HUMAN, "display_name": "alice", "reused": True})
        document = self.read_registry()
        humans = [actor_id for actor_id, entry in document["actors"].items() if entry["kind"] == "human"]
        self.assertEqual(humans, [HUMAN])
        self.assertEqual(document["seats"], {"my-project-architect": ["claude"], "other-architect": ["codex"]})
        self.assertEqual(sorted(document["actors"]), sorted([HUMAN, "my-project-architect", "my-project-fake-worker", "other-architect", "other-fake-worker"]))
        ids = {actor["id"] for actor in Store(self.db).list_actors()}
        self.assertTrue({"my-project-architect", "other-architect", "other-fake-worker"} <= ids)
        self.assertEqual(result["doctor"], {"ok": True, "fixes": []})

    def test_non_tty_refuses_omitted_required_flags_by_name(self) -> None:
        rc, result = self.run_cli("--runtimes", "fake", "--yes")
        self.assertEqual(rc, 2)
        self.assertIn("--project-root", result["error"])
        rc, result = self.run_cli("--project-root", str(self.project))  # stdin is not a tty under the test runner
        self.assertEqual(rc, 2)
        self.assertIn("--runtimes", result["error"])
        self.assertIn("detected on this machine", result["error"])
        self.assertFalse(self.registry.exists())

    def test_bad_arguments_are_refused_before_writing(self) -> None:
        for argv, expected in (
            (["--project-root", str(self.root / "nope"), "--runtimes", "fake"], "is not a directory"),
            (["--project-root", str(self.project), "--runtimes", "cursor"], "--runtimes must name"),
            (["--project-root", str(self.project), "--runtimes", "fake", "--clients", "cursor"], "--clients must be a subset"),
            (["--project-root", str(self.project), "--runtimes", "fake", "--team", "Bad Team"], "invalid actor id"),
            (["--project-root", str(self.project), "--runtimes", "fake", "--human-id", "human:me"], "opaque"),
        ):
            with self.subTest(argv=argv):
                rc, result = self.run_cli(*argv, "--yes")
                self.assertEqual(rc, 2, result)
                self.assertIn(expected, result["error"])
        self.assertFalse(self.registry.exists())
        self.assertFalse(self.db.exists())

    def test_existing_entry_that_does_not_load_is_refused_before_the_write(self) -> None:
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        broken = json.dumps({"actors": {"old-architect": {"kind": "agent", "display_name": "old-architect", "team": "old", "role": "architect", "project_root": "${AGENT_COMMS_SETUP_TEST_UNSET}", "capabilities": []}}})
        self.registry.write_text(broken)
        rc, result = self.setup("--runtimes", "fake", "--clients", "")
        self.assertEqual(rc, 2, result)
        self.assertIn("does not load as it is", result["error"])
        self.assertIn("AGENT_COMMS_SETUP_TEST_UNSET", result["error"])
        self.assertEqual(self.registry.read_text(), broken)
        self.assertFalse(self.db.exists())

    def test_legacy_registry_and_broken_registry_are_refused(self) -> None:
        legacy = self.registry.with_name("agents.json")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("{}")
        rc, result = self.setup("--runtimes", "fake", "--clients", "")
        self.assertEqual(rc, 2)
        self.assertIn("legacy registry", result["error"])
        self.assertIn("bootstrap", result["error"])
        legacy.unlink()
        self.registry.write_text("{")
        rc, result = self.setup("--runtimes", "fake", "--clients", "")
        self.assertEqual(rc, 2)
        self.assertIn("cannot be read as JSON", result["error"])
        self.assertEqual(self.registry.read_text(), "{")

    def test_prompts_ask_every_omitted_argument_with_its_default(self) -> None:
        asked: list[tuple[str, str]] = []

        def ask(prompt: str, default: str) -> str:
            asked.append((prompt, default))
            if prompt.startswith("Runtimes"):
                return "fake"
            if prompt.startswith("MCP clients"):
                return ""
            if prompt.startswith("Your display name"):
                return "carol"
            return default

        with mock.patch.object(setup, "current_platform", return_value="nowhere-x"):
            result = setup.run_setup(
                Store(self.db), db_path=self.db, db_explicit=True, project_root=None,
                ask=ask, which=lambda _name: None, run=self.clients.run,
            )
        prompts = [prompt for prompt, _ in asked]
        self.assertEqual(prompts[0], "Project root")
        self.assertEqual(asked[0][1], os.getcwd())
        self.assertIn(("Team name", setup.team_from_path(os.getcwd())), asked)
        self.assertEqual([default for prompt, default in asked if prompt.startswith("Runtimes")], ["fake"])
        # With no client-capable runtime chosen the clients prompt offers none, the explicit empty answer.
        self.assertEqual([default for prompt, default in asked if prompt.startswith("MCP clients")], ["none"])
        self.assertTrue(any(prompt.startswith("Your actor id") for prompt in prompts))
        self.assertEqual(result["human"]["display_name"], "carol")
        self.assertEqual(result["clients"], [])
        self.assertEqual(len(result["human"]["id"]), 26)
        self.assertEqual(result["project_root"], str(Path(os.getcwd()).resolve()))

    def test_prompt_answers_none_for_no_clients_and_skips_a_registered_human(self) -> None:
        rc, _ = self.setup("--runtimes", "fake", "--clients", "", "--human", "alice", "--human-id", HUMAN)
        self.assertEqual(rc, 0)
        asked: list[str] = []

        def ask(prompt: str, default: str) -> str:
            asked.append(prompt)
            if prompt.startswith("MCP clients"):
                self.assertEqual(default, "codex")
                return "none"
            return default

        other = self.root / "other"
        other.mkdir()
        with mock.patch.object(doctor, "current_platform", return_value="test-platform"), \
                mock.patch.object(doctor, "pin_for", return_value=RuntimePin("codex", "test-platform", "0.157.0", "exact", "2026-09-26")):
            result = setup.run_setup(
                Store(self.db), db_path=self.db, db_explicit=True, project_root=str(other),
                runtimes=["codex"], ask=ask, which=self.clients.which, run=self.clients.run,
            )
        self.assertEqual(result["clients"], [])
        self.assertEqual(result["human"], {"id": HUMAN, "display_name": "alice", "reused": True})
        self.assertFalse(any(prompt.startswith("Your ") for prompt in asked), asked)
        self.assertTrue(any(prompt.startswith("MCP clients") for prompt in asked), asked)
        self.assertEqual(self.clients.commands("codex"), [])

    def test_console_ask_takes_the_default_on_a_blank_answer(self) -> None:
        with mock.patch("builtins.input", side_effect=["", "  ", "codex "]):
            self.assertEqual(setup.console_ask("Q", "claude,codex"), "claude,codex")
            self.assertEqual(setup.console_ask("Q", "none"), "none")
            self.assertEqual(setup.console_ask("Q", "claude,codex"), "codex")
        self.assertEqual(setup.parse_list("none"), ["none"])
        rc, result = self.run_cli("--project-root", str(self.project), "--runtimes", "fake", "--clients", "None", "--human-id", HUMAN, "--yes")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["clients"], [])

    def test_broken_human_entries_are_refused_before_the_write(self) -> None:
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        self.registry.write_text(json.dumps({"actors": {HUMAN: {"kind": "human"}}}))
        rc, result = self.setup("--runtimes", "fake", "--clients", "")
        # bootstrap refuses a human without a display name; setup refuses before writing anything.
        self.assertEqual(rc, 2, result)
        self.assertIn(f"human actor {HUMAN} has no display_name", result["error"])
        self.assertEqual(json.loads(self.registry.read_text()), {"actors": {HUMAN: {"kind": "human"}}})
        self.assertFalse(self.db.exists())
        self.registry.write_text(json.dumps({"actors": {HUMAN: "alice"}}))
        rc, result = self.setup("--runtimes", "fake", "--clients", "")
        self.assertEqual(rc, 2, result)
        self.assertIn("is not an object", result["error"])


def shlex_quote(path) -> str:
    import shlex

    return shlex.quote(str(path))


def shlex_split(line: str) -> list[str]:
    import shlex

    return shlex.split(line)


if __name__ == "__main__":
    raise SystemExit("run through unittest discovery; see tests/isolation.py")
