import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_comms import cli, doctor, mcp_clients, paths, runtime_pins
from agent_comms.cli._helpers import bootstrap_store
from agent_comms.db import LEDGER_SCHEMA_VERSION
from agent_comms.runtime_pins import RuntimePin
from agent_comms.store import Store

NOW = datetime.now(timezone.utc)
HUMAN = "01M36YTJV9XBW95S6ZWV47C4RG"
CERTIFIED_PLATFORM = sorted(runtime_pins.certified_platforms("claude"))[0]
MCP_COMMAND = str(Path(sys.executable).parent / "agent-comms-mcp")


def _completed(argv, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def _runner(responses):
    """A subprocess runner keyed by the argv tail: ``("--version",)`` etc."""

    def run(argv, **_kwargs):
        for tail, response in responses.items():
            if tuple(argv[-len(tail):]) == tail:
                return response(argv) if callable(response) else _completed(argv, **response)
        raise AssertionError(f"unexpected command {argv!r}")

    return run


def _registry(root: Path, *, workers=("fake",), seats=None, humans=(HUMAN,)) -> dict:
    actors = {human: {"kind": "human", "display_name": "alice"} for human in humans}
    actors["t-architect"] = {
        "kind": "agent",
        "display_name": "t-architect",
        "team": "t",
        "role": "architect",
        "project_root": str(root / "project"),
        "capabilities": ["planning"],
    }
    for runtime in workers:
        actors[f"t-{runtime}-worker"] = {
            "kind": "agent",
            "display_name": f"t-{runtime}-worker",
            "team": "t",
            "role": "worker",
            "owner": "t-architect",
            "runtime": runtime,
            "project_root": str(root / "project"),
            "capabilities": ["implementation"],
        }
    document = {"actors": actors}
    if seats is not None:
        document["seats"] = seats
    return document


class _Scratch(unittest.TestCase):
    """A runtime root, registry and ledger under a temporary directory."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "project").mkdir()
        self.runtime_root = self.root / ".agent-comms"
        self.runtime_root.mkdir()
        self.db = self.root / "ledger.sqlite"
        patch = mock.patch.object(paths, "runtime_root", return_value=self.runtime_root)
        patch.start()
        self.addCleanup(patch.stop)
        self.claude_config = self.root / "claude.json"
        self.codex_home = self.root / "codex"
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

    def write_registry(self, **kwargs) -> Path:
        config = paths.actors_config_path()
        config.write_text(json.dumps(_registry(self.root, **kwargs), indent=2))
        return config

    def bootstrap(self, **kwargs) -> Store:
        config = self.write_registry(**kwargs)
        store = Store(self.db)
        bootstrap_store(store, config)
        return store

    def write_claude_seat(self, args, command=MCP_COMMAND, project=None, env=None) -> None:
        # Claude Code keys the local scope by the physical working directory,
        # which is what the ledger stores for the architect's project root.
        key = os.path.realpath(str(project or self.root / "project"))
        server = {"command": command, "args": args}
        if env is not None:
            server["env"] = env
        self.claude_config.write_text(json.dumps({"projects": {key: {"mcpServers": {"agent-comms": server}}}}))

    def write_codex_seat(self, args, command=MCP_COMMAND) -> None:
        self.codex_home.mkdir(exist_ok=True)
        rendered = ", ".join(json.dumps(item) for item in args)
        (self.codex_home / "config.toml").write_text(
            f'[mcp_servers.agent-comms]\ncommand = {json.dumps(command)}\nargs = [{rendered}]\n'
        )

    def run_cli(self, *argv) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cli.run(["--db", str(self.db), "doctor", *argv])
        return rc, json.loads(buffer.getvalue())


class ReportShapeTest(unittest.TestCase):
    def test_report_ok_iff_nothing_failed_and_fixes_follow_check_order(self) -> None:
        checks = [
            doctor.check("a", doctor.OK, "fine"),
            doctor.check("b", doctor.FAIL, "broken", "fix b"),
            doctor.check("c", doctor.WARN, "meh", "fix c"),
            doctor.check("d", doctor.FAIL, "broken", "fix d"),
        ]
        result = doctor.report("darwin-arm64", checks)
        self.assertFalse(result["ok"])
        self.assertEqual(result["fixes"], ["fix b", "fix d"])
        self.assertEqual(result["platform"], "darwin-arm64")
        self.assertTrue(doctor.report("x", checks[:1] + checks[2:3])["ok"])
        with self.assertRaises(ValueError):
            doctor.check("e", "bogus", "no such status")


class InstallAndRootChecksTest(_Scratch):
    def test_install_names_missing_console_scripts(self) -> None:
        fake_bin = self.root / "prefix with space" / "bin"
        fake_bin.mkdir(parents=True)
        result = doctor.check_install(str(fake_bin / "python"))
        self.assertEqual(result["status"], doctor.FAIL)
        for name in doctor.CONSOLE_SCRIPTS:
            self.assertIn(name, result["detail"])
        self.assertIn("uv sync", result["fix"])
        for name in doctor.CONSOLE_SCRIPTS:
            (fake_bin / name).write_text("#!/bin/sh\n")
        self.assertEqual(doctor.check_install(str(fake_bin / "python"))["status"], doctor.OK)

    def test_runtime_root_absent_and_unwritable(self) -> None:
        missing = self.root / "nowhere"
        result = doctor.check_runtime_root(missing)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertEqual(result["fix"], f"mkdir -p {missing}")
        self.assertEqual(doctor.check_runtime_root(self.runtime_root)["status"], doctor.OK)
        as_file = self.root / "file-root"
        as_file.write_text("")
        result = doctor.check_runtime_root(as_file)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("not a directory", result["detail"])
        self.assertEqual(result["fix"], f"rm -f {as_file} && mkdir -p {as_file}")
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        result = doctor.check_runtime_root(locked)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertEqual(result["fix"], f"chmod u+w {locked}")

    def test_runtime_root_fixes_survive_a_path_with_spaces(self) -> None:
        # DOCTOR-001 F3: one intended operand, however the path is spelled.
        missing = self.root / "run time" / "with spaces"
        self.assertEqual(shlex.split(doctor.check_runtime_root(missing)["fix"]), ["mkdir", "-p", str(missing)])
        as_file = self.root / "file root"
        as_file.write_text("")
        words = shlex.split(doctor.check_runtime_root(as_file)["fix"])
        self.assertEqual(words, ["rm", "-f", str(as_file), "&&", "mkdir", "-p", str(as_file)])
        if os.geteuid() != 0:
            locked = self.root / "locked dir"
            locked.mkdir()
            locked.chmod(0o500)
            self.addCleanup(locked.chmod, 0o700)
            self.assertEqual(shlex.split(doctor.check_runtime_root(locked)["fix"]), ["chmod", "u+w", str(locked)])
        token = self.root / "run time" / "admin token"
        token.parent.mkdir()
        token.write_text("secret")  # hygiene:allow
        token.chmod(0o644)
        self.assertEqual(shlex.split(doctor.check_admin_token(token)["fix"]), ["chmod", "600", str(token)])
        absent = self.root / "run time" / "missing"
        self.assertTrue(doctor.check_admin_token(absent)["fix"].endswith(f"> {shlex.quote(str(absent))})"))

    def test_admin_token_warns_when_absent_or_open(self) -> None:
        token = self.runtime_root / "admin-token"
        result = doctor.check_admin_token(token)
        self.assertEqual(result["status"], doctor.WARN)
        self.assertIn("umask 077", result["fix"])
        token.write_text("secret")  # hygiene:allow
        token.chmod(0o644)
        result = doctor.check_admin_token(token)
        self.assertEqual(result["status"], doctor.WARN)
        self.assertEqual(result["fix"], f"chmod 600 {token}")
        token.chmod(0o600)
        self.assertEqual(doctor.check_admin_token(token)["status"], doctor.OK)


class RegistryAndLedgerChecksTest(_Scratch):
    def test_registry_missing_points_at_setup_and_writes_nothing(self) -> None:
        config = paths.actors_config_path()
        result, loaded = doctor.check_registry(config)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIsNone(loaded)
        self.assertIn("agent-comms setup", result["fix"])
        self.assertFalse(config.exists())

    def test_registry_legacy_file_is_not_migrated_by_doctor(self) -> None:
        config = paths.actors_config_path()
        config.with_name("agents.json").write_text("{}")
        result, _ = doctor.check_registry(config)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("bootstrap", result["fix"])
        self.assertFalse(config.exists())

    def test_registry_reports_unresolved_env_and_bad_json(self) -> None:
        config = paths.actors_config_path()
        document = _registry(self.root)
        document["actors"]["t-architect"]["project_root"] = "${AGENT_COMMS_DOCTOR_TEST_UNSET}"
        config.write_text(json.dumps(document))
        result, loaded = doctor.check_registry(config)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIsNone(loaded)
        self.assertIn("AGENT_COMMS_DOCTOR_TEST_UNSET", result["fix"])
        config.write_text("{")
        result, _ = doctor.check_registry(config)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("JSON", result["detail"])

    def test_registry_unreadable_and_empty(self) -> None:
        config = self.write_registry()
        if os.geteuid() != 0:
            config.chmod(0)
            self.addCleanup(config.chmod, 0o600)
            result, loaded = doctor.check_registry(config)
            self.assertEqual(result["status"], doctor.FAIL)
            self.assertIsNone(loaded)
            self.assertEqual(result["fix"], f"chmod u+r {config}")
            config.chmod(0o600)
        config.write_text(json.dumps({"actors": {}}))
        result, loaded = doctor.check_registry(config)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("no actors", result["detail"])
        self.assertIn("agent-comms setup", result["fix"])
        document = _registry(self.root)
        document["actors"]["t-architect"]["team"] = 123
        config.write_text(json.dumps(document))
        result, _ = doctor.check_registry(config)
        self.assertEqual(result["status"], doctor.OK)
        self.assertIn("123", result["detail"])

    def test_registry_and_ledger_fixes_name_the_inspected_ledger(self) -> None:
        # DOCTOR-001 F1: an explicit ledger is carried into every repair; the default one is not.
        config = paths.actors_config_path()
        explicit = doctor.LedgerRef(self.db, True)
        default = doctor.LedgerRef(self.db, False)
        self.assertEqual(
            doctor.check_registry(config, explicit)[0]["fix"],
            f"agent-comms --db {self.db} setup --project-root <project-root> --runtimes <claude,codex,fake>",
        )
        self.assertEqual(doctor.check_registry(config, default)[0]["fix"], "agent-comms setup --project-root <project-root> --runtimes <claude,codex,fake>")
        config.with_name("agents.json").write_text("{}")
        self.assertEqual(doctor.check_registry(config, explicit)[0]["fix"], f"agent-comms --db {self.db} bootstrap (migrates agents.json)")
        config.with_name("agents.json").unlink()
        self.assertEqual(doctor.check_ledger(self.db, explicit)[0]["fix"], f"agent-comms --db {self.db} bootstrap (after agent-comms --db {self.db} setup)")
        self.assertEqual(doctor.check_ledger(self.db, default)[0]["fix"], "agent-comms bootstrap (after agent-comms setup)")
        self.bootstrap(humans=())
        result, _ = doctor.check_ledger(self.db, explicit)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertTrue(result["fix"].startswith(f"agent-comms --db {self.db} setup "), result)
        if os.geteuid() != 0:
            spaced = self.root / "reg dir" / "actors.json"
            spaced.parent.mkdir()
            spaced.write_text("{}")
            spaced.chmod(0)
            self.addCleanup(spaced.chmod, 0o600)
            self.assertEqual(shlex.split(doctor.check_registry(spaced)[0]["fix"]), ["chmod", "u+r", str(spaced)])

    def test_ledger_ref_renders_quoted_commands(self) -> None:
        ledger = doctor.LedgerRef(Path("/ledgers/team one.sqlite"), True)
        self.assertEqual(ledger.cli("setup", "--project-root", "/p q", "--runtimes", "<claude,codex,fake>"), "agent-comms --db '/ledgers/team one.sqlite' setup --project-root '/p q' --runtimes <claude,codex,fake>")
        self.assertEqual(ledger.monitor("<human-actor-id>"), "agent-comms-monitor --db '/ledgers/team one.sqlite' --human-actor-id <human-actor-id>")
        self.assertEqual(doctor.LedgerRef(Path("/ledgers/team one.sqlite"), False).monitor("h"), "agent-comms-monitor --human-actor-id h")
        self.assertEqual(doctor.sh("a b", "<placeholder>", "<not a placeholder", "c"), "'a b' <placeholder> '<not a placeholder' c")

    def test_registry_loads(self) -> None:
        result, loaded = doctor.check_registry(self.write_registry(seats={"t-architect": ["claude"]}))
        self.assertEqual(result["status"], doctor.OK)
        self.assertIn("teams: t", result["detail"])
        self.assertEqual(loaded["seats"], {"t-architect": ["claude"]})

    def test_ledger_missing_fails_without_creating_it(self) -> None:
        result, snapshot = doctor.check_ledger(self.db)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIsNone(snapshot)
        self.assertIn("bootstrap", result["fix"])
        self.assertFalse(self.db.exists())

    def test_ledger_bootstrapped_passes_and_snapshots_rows(self) -> None:
        self.bootstrap(workers=("fake", "codex"))
        result, snapshot = doctor.check_ledger(self.db)
        self.assertEqual(result["status"], doctor.OK, result)
        self.assertEqual([actor["id"] for actor in snapshot.humans()], [HUMAN])
        self.assertEqual([actor["id"] for actor in snapshot.by_role("architect")], ["t-architect"])
        self.assertEqual([actor["id"] for actor in snapshot.workers("codex")], ["t-codex-worker"])
        self.assertIsNone(snapshot.heartbeat)
        self.assertEqual(snapshot.dispatch_count, 0)
        self.assertEqual(snapshot.dead_credentials, [])
        self.assertEqual(doctor.runtimes_in_use(snapshot, None), {"fake", "codex"})

    def test_ledger_two_humans_need_the_operator_override(self) -> None:
        self.bootstrap(humans=(HUMAN, "01M36YTJV9XBW95S6ZWV47C4RH"))
        result, snapshot = doctor.check_ledger(self.db)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("AGENT_COMMS_OPERATOR_ACTOR", result["fix"])
        self.assertIsNotNone(snapshot)
        with mock.patch.dict(os.environ, {"AGENT_COMMS_OPERATOR_ACTOR": HUMAN}):
            self.assertEqual(doctor.check_ledger(self.db)[0]["status"], doctor.OK)
        with mock.patch.dict(os.environ, {"AGENT_COMMS_OPERATOR_ACTOR": "nobody"}):
            result, _ = doctor.check_ledger(self.db)
            self.assertEqual(result["status"], doctor.FAIL)
            self.assertIn("nobody", result["detail"])

    def test_ledger_newer_schema_is_refused_read_only(self) -> None:
        store = self.bootstrap()
        with store.connection() as conn:
            conn.execute(f"pragma user_version = {LEDGER_SCHEMA_VERSION + 1}")
        result, snapshot = doctor.check_ledger(self.db)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("newer ledger schema", result["detail"])
        self.assertIn("upgrade", result["fix"])
        self.assertEqual(snapshot.schema_version, LEDGER_SCHEMA_VERSION + 1)


class PlatformCheckTest(unittest.TestCase):
    def test_no_native_runtime_skips(self) -> None:
        result, pins = doctor.check_platform("linux-riscv64", {"fake"})
        self.assertEqual(result["status"], doctor.SKIP)
        self.assertEqual(pins, {})

    def test_unlisted_platform_fails_with_the_pin_message(self) -> None:
        result, pins = doctor.check_platform("linux-riscv64", {"claude", "fake"})
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("linux-riscv64", result["detail"])
        self.assertIn("CONTRIBUTING.md", result["fix"])
        self.assertEqual(pins, {})

    def test_certified_platform_resolves_every_pin(self) -> None:
        result, pins = doctor.check_platform(CERTIFIED_PLATFORM, {"claude", "codex"})
        self.assertEqual(result["status"], doctor.OK, result)
        self.assertEqual(set(pins), {"claude", "codex"})
        self.assertIn(pins["claude"].version, result["detail"])


class ClaudeCheckTest(_Scratch):
    def setUp(self) -> None:
        super().setUp()
        self.binary = self.root / "custody" / "9.9.9"
        self.binary.parent.mkdir()
        self.binary.write_bytes(b"#!/bin/sh\necho 9.9.9\n")
        self.pin = RuntimePin("claude", CERTIFIED_PLATFORM, "9.9.9", "exact", "2026-09-26", hashlib.sha256(self.binary.read_bytes()).hexdigest())

    def test_uncertified_pin_skips(self) -> None:
        self.assertEqual(doctor.check_claude(None)["status"], doctor.SKIP)

    def test_missing_binary_names_the_version_and_path(self) -> None:
        self.binary.unlink()
        result = doctor.check_claude(self.pin)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn(str(self.binary), result["detail"])
        self.assertIn("download Claude Code 9.9.9", result["fix"])
        self.assertIn(str(self.binary), result["fix"])

    def test_digest_mismatch(self) -> None:
        self.binary.write_bytes(b"tampered")
        result = doctor.check_claude(self.pin)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("digest mismatch", result["detail"])
        self.assertIn("certified Claude 9.9.9 binary", result["fix"])

    def test_unreadable_binary_is_a_failed_check_not_a_crash(self) -> None:
        # DOCTOR-001 F4: a mode-000 custody binary is a diagnosis, with the fix.
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        self.binary.chmod(0)
        self.addCleanup(self.binary.chmod, 0o700)
        result = doctor.check_claude(self.pin, run=_runner({}))
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("cannot be read", result["detail"])
        self.assertEqual(result["fix"], f"chmod u+rx {self.binary}")

    def test_version_drift_logged_out_and_logged_in(self) -> None:
        drift = _runner({("--version",): {"stdout": "9.9.8 (Claude Code)"}})
        result = doctor.check_claude(self.pin, run=drift)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("expected 9.9.9, got 9.9.8", result["detail"])
        self.assertIn("install Claude Code 9.9.9", result["fix"])
        logged_out = _runner({("--version",): {"stdout": "9.9.9"}, ("auth", "status"): {"stdout": json.dumps({"loggedIn": False}), "returncode": 1}})
        result = doctor.check_claude(self.pin, run=logged_out)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("not logged in", result["detail"])
        self.assertEqual(result["fix"], f"{self.binary} auth login")
        logged_in = _runner({("--version",): {"stdout": "9.9.9"}, ("auth", "status"): {"stdout": json.dumps({"loggedIn": True})}})
        result = doctor.check_claude(self.pin, run=logged_in)
        self.assertEqual(result["status"], doctor.OK, result)
        self.assertIsNone(result["fix"])

    def test_version_failure_is_reported(self) -> None:
        broken = _runner({("--version",): {"stdout": "", "stderr": "boom", "returncode": 2}})
        result = doctor.check_claude(self.pin, run=broken)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("exited 2", result["detail"])


class CodexCheckTest(_Scratch):
    def setUp(self) -> None:
        super().setUp()
        self.pin = RuntimePin("codex", CERTIFIED_PLATFORM, "0.157.0", "exact", "2026-09-26")
        self.which = lambda name: "/opt/bin/codex" if name == "codex" else None
        self.version = _runner({("--version",): {"stdout": "codex-cli 0.157.0"}})
        self.auth_source = self.runtime_root / "codex-auth" / "auth.json"

    def _write_auth(self, path: Path, *, age_days: float = 1) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_refresh": (NOW - timedelta(days=age_days)).isoformat()}))

    def _worker(self, actor_id: str, home: Path, *, project_root: str = "/srv/project") -> dict:
        return {
            "id": actor_id,
            "kind": "agent",
            "role": "worker",
            "runtime": "codex",
            "project_root": project_root,
            "spawn_json": json.dumps({"command": "codex", "args": [], "env": {"CODEX_HOME": str(home)}}),
        }

    def _provision(self, home: Path, actor_id: str, *, age_days: float = 1) -> None:
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text("# base\n")
        (home / f"{actor_id}.config.toml").write_text("# profile\n")
        self._write_auth(home / "auth.json", age_days=age_days)

    def test_uncertified_pin_skips(self) -> None:
        self.assertEqual(doctor.check_codex(None, [], [])[0]["status"], doctor.SKIP)

    def test_codex_absent_from_path(self) -> None:
        [result] = doctor.check_codex(self.pin, [], [], which=lambda _name: None)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("not on PATH", result["detail"])
        self.assertIn("install the Codex CLI", result["fix"])

    def test_version_drift_respects_the_boundary(self) -> None:
        drift = _runner({("--version",): {"stdout": "codex-cli 0.158.0"}})
        [result] = doctor.check_codex(self.pin, [], [], which=self.which, run=drift)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("expected 0.157.0 (exact), got 0.158.0", result["detail"])
        self.assertIn("install codex 0.157.0", result["fix"])
        minor = RuntimePin("codex", CERTIFIED_PLATFORM, "0.157.0", "minor", "2026-09-26")
        patch = _runner({("--version",): {"stdout": "codex-cli 0.157.4"}})
        self._write_auth(self.auth_source)
        [result] = doctor.check_codex(minor, [], [], which=self.which, run=patch)
        self.assertEqual(result["status"], doctor.OK, result)

    def test_version_failure_is_reported(self) -> None:
        broken = _runner({("--version",): {"stdout": "", "stderr": "boom", "returncode": 2}})
        [result] = doctor.check_codex(self.pin, [], [], which=self.which, run=broken)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("exited 2", result["detail"])
        self.assertIn("install codex 0.157.0", result["fix"])

    def test_missing_shared_auth_prescribes_the_login(self) -> None:
        [result] = doctor.check_codex(self.pin, [], [], which=self.which, run=self.version)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertEqual(result["fix"], f"CODEX_HOME={self.auth_source.parent} codex login")
        self._write_auth(self.auth_source)
        [result] = doctor.check_codex(self.pin, [], [], which=self.which, run=self.version)
        self.assertEqual(result["status"], doctor.OK, result)

    def test_worker_homes_missing_stale_and_fresh(self) -> None:
        self._write_auth(self.auth_source)
        missing = self.root / "codex-homes" / "default" / "t-codex-missing"
        stale = self.root / "codex-homes" / "default" / "t-codex-stale"
        fresh = self.root / "codex-homes" / "default" / "t-codex-fresh"
        self._provision(stale, "t-codex-stale", age_days=20)
        self._provision(fresh, "t-codex-fresh")
        workers = [
            self._worker("t-codex-missing", missing, project_root="/srv/one"),
            self._worker("t-codex-stale", stale),
            self._worker("t-codex-fresh", fresh),
        ]
        results = {item["id"]: item for item in doctor.check_codex(self.pin, workers, [], which=self.which, run=self.version, now=NOW)}
        self.assertEqual(results["runtime:codex"]["status"], doctor.OK)
        missing_check = results["runtime:codex:home:t-codex-missing"]
        self.assertEqual(missing_check["status"], doctor.FAIL)
        self.assertEqual(
            missing_check["fix"],
            f"agent-comms provision-codex-home --actor-id t-codex-missing --project-root /srv/one "
            f"--codex-home {missing} --override-protected 'doctor: restore the missing codex home'",
        )
        self.assertIn("protected", missing_check["detail"])
        stale_check = results["runtime:codex:home:t-codex-stale"]
        self.assertEqual(stale_check["status"], doctor.FAIL)
        self.assertIn("stale", stale_check["detail"])
        self.assertEqual(stale_check["fix"], f"CODEX_HOME={stale} codex login")
        self.assertEqual(results["runtime:codex:home:t-codex-fresh"]["status"], doctor.OK)

    def test_worker_repairs_target_the_inspected_ledger_and_home(self) -> None:
        # DOCTOR-001 F1: with an explicit ledger the provisioning repair carries
        # ``--db`` and the inspected (custom) home, so it cannot land on the
        # default ledger or the default home. F3: every operand is quoted.
        self._write_auth(self.auth_source)
        custom = self.root / "custom homes" / "t-codex-custom"
        worker = self._worker("t-codex-custom", custom, project_root="/srv/my project")
        ledger = doctor.LedgerRef(self.root / "scratch ledger.sqlite", True)
        results = {item["id"]: item for item in doctor.check_codex(self.pin, [worker], [], which=self.which, run=self.version, now=NOW, ledger=ledger)}
        fix = results["runtime:codex:home:t-codex-custom"]["fix"]
        self.assertEqual(
            shlex.split(fix),
            [
                "agent-comms", "--db", str(ledger.path), "provision-codex-home",
                "--actor-id", "t-codex-custom", "--project-root", "/srv/my project",
                "--codex-home", str(custom), "--override-protected", "doctor: restore the missing codex home",
            ],
        )
        self._provision(custom, "t-codex-custom", age_days=20)
        results = {item["id"]: item for item in doctor.check_codex(self.pin, [worker], [], which=self.which, run=self.version, now=NOW, ledger=ledger)}
        self.assertEqual(shlex.split(results["runtime:codex:home:t-codex-custom"]["fix"]), [f"CODEX_HOME={custom}", "codex", "login"])
        bare = {"id": "t-codex-bare", "kind": "agent", "role": "worker", "runtime": "codex", "project_root": "/srv", "spawn_json": json.dumps({"env": {}})}
        results = {item["id"]: item for item in doctor.check_codex(self.pin, [bare], [], which=self.which, run=self.version, ledger=ledger)}
        self.assertTrue(results["runtime:codex:home:t-codex-bare"]["fix"].endswith(f"agent-comms --db {shlex.quote(str(ledger.path))} bootstrap"))

    def test_shared_auth_login_is_quoted(self) -> None:
        source = self.root / "auth dir" / "auth.json"
        [result] = doctor.check_codex(self.pin, [], [], which=self.which, run=self.version, auth_source=source)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertEqual(shlex.split(result["fix"]), [f"CODEX_HOME={source.parent}", "codex", "login"])

    def test_worker_without_codex_home_in_spawn(self) -> None:
        self._write_auth(self.auth_source)
        worker = {"id": "t-codex-bare", "kind": "agent", "role": "worker", "runtime": "codex", "project_root": "/srv", "spawn_json": json.dumps({"env": {}})}
        results = {item["id"]: item for item in doctor.check_codex(self.pin, [worker], [], which=self.which, run=self.version)}
        result = results["runtime:codex:home:t-codex-bare"]
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("CODEX_HOME", result["detail"])

    def test_each_dead_lineage_gets_its_own_login_command(self) -> None:
        self._write_auth(self.auth_source)
        dead = [
            {"lineage_key": str(self.root / "homes" / "a" / "auth.json"), "dead_reason": "expired", "dead_at": "2026-09-01T00:00:00+00:00"},
            {"lineage_key": str(self.root / "homes" / "b" / "auth.json"), "dead_reason": "revoked", "dead_at": "2026-09-02T00:00:00+00:00"},
        ]
        results = doctor.check_codex(self.pin, [], dead, which=self.which, run=self.version)
        logins = [item for item in results if item["id"].startswith("runtime:codex:login:")]
        self.assertEqual(len(logins), 2)
        self.assertEqual([item["status"] for item in logins], [doctor.FAIL, doctor.FAIL])
        self.assertEqual(logins[0]["fix"], f"CODEX_HOME={self.root / 'homes' / 'a'} codex login")
        self.assertEqual(logins[1]["fix"], f"CODEX_HOME={self.root / 'homes' / 'b'} codex login")
        self.assertIn("refresh failed: expired since 2026-09-01T00:00:00+00:00", logins[0]["detail"])
        self.assertIn("revoked", logins[1]["detail"])


class MonitorCheckTest(unittest.TestCase):
    def _snapshot(self, heartbeat=None, dispatches=0, humans=(HUMAN,)) -> doctor.LedgerSnapshot:
        actors = [{"id": human, "kind": "human", "role": None, "runtime": None} for human in humans]
        return doctor.LedgerSnapshot(schema_version=LEDGER_SCHEMA_VERSION, actors=actors, heartbeat=heartbeat, dispatch_count=dispatches)

    def test_absent_heartbeat_warns_before_the_first_dispatch_and_fails_after(self) -> None:
        result = doctor.check_monitor(self._snapshot())
        self.assertEqual(result["status"], doctor.WARN)
        self.assertEqual(result["fix"], f"agent-comms-monitor --human-actor-id {HUMAN}")
        result = doctor.check_monitor(self._snapshot(dispatches=3))
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("3 dispatches", result["detail"])

    def test_stale_dead_pid_and_running(self) -> None:
        stale = {"last_pass_at": (NOW - timedelta(hours=1)).isoformat(), "pid": 4242}
        result = doctor.check_monitor(self._snapshot(stale), now=NOW)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("stale", result["detail"])
        fresh = {"last_pass_at": (NOW - timedelta(seconds=10)).isoformat(), "pid": 4242}
        result = doctor.check_monitor(self._snapshot(fresh), pid_alive=lambda _pid: False, now=NOW)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("pid 4242 is not running", result["detail"])
        result = doctor.check_monitor(self._snapshot(fresh), pid_alive=lambda pid: pid == 4242, now=NOW)
        self.assertEqual(result["status"], doctor.OK)
        zero = {"last_pass_at": fresh["last_pass_at"], "pid": 0}
        result = doctor.check_monitor(self._snapshot(zero), pid_alive=lambda _pid: True, now=NOW)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("pid 0", result["detail"])

    def test_fix_uses_the_operator_override_when_set(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_COMMS_OPERATOR_ACTOR": "op"}):
            result = doctor.check_monitor(self._snapshot(humans=(HUMAN, "other")))
        self.assertEqual(result["fix"], "agent-comms-monitor --human-actor-id op")

    def test_fix_names_an_explicit_ledger(self) -> None:
        # DOCTOR-001 F1: the monitor repair runs against the ledger doctor inspected.
        explicit = doctor.LedgerRef(Path("/ledgers/scratch.sqlite"), True)
        result = doctor.check_monitor(self._snapshot(dispatches=1), ledger=explicit)
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertEqual(result["fix"], f"agent-comms-monitor --db /ledgers/scratch.sqlite --human-actor-id {HUMAN}")
        result = doctor.check_monitor(self._snapshot(dispatches=1), ledger=doctor.LedgerRef(Path("/ledgers/scratch.sqlite"), False))
        self.assertEqual(result["fix"], f"agent-comms-monitor --human-actor-id {HUMAN}")


class McpCheckTest(_Scratch):
    def setUp(self) -> None:
        super().setUp()
        self.architects = [{"id": "t-architect", "kind": "agent", "role": "architect", "project_root": str(self.root / "project")}]
        self.project = str(self.root / "project")

    def _check(self, seats=None, *, clients=None, db_explicit=True) -> dict:
        results = doctor.check_mcp(self.architects, seats or {}, db_path=self.db, db_explicit=db_explicit, clients=clients)
        return {item["id"]: item for item in results}

    def test_no_seats_recorded_skips(self) -> None:
        result = self._check()["mcp:t-architect"]
        self.assertEqual(result["status"], doctor.SKIP)
        self.assertIn("no seats recorded", result["detail"])

    def test_absent_claude_seat_fix_adds_from_the_project_directory(self) -> None:
        result = self._check({"t-architect": ["claude"]})["mcp:t-architect:claude"]
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertEqual(
            result["fix"],
            f"cd {self.project} && claude mcp add agent-comms --scope local -- {MCP_COMMAND} --actor-id t-architect --db {self.db}",
        )

    def test_wrong_actor_fix_removes_then_adds(self) -> None:
        self.write_claude_seat(["--actor-id", "other-architect", "--db", str(self.db)])
        result = self._check({"t-architect": ["claude"]})["mcp:t-architect:claude"]
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("bound to actor 'other-architect'", result["detail"])
        self.assertEqual(
            result["fix"],
            f"cd {self.project} && claude mcp remove agent-comms --scope local && "
            f"claude mcp add agent-comms --scope local -- {MCP_COMMAND} --actor-id t-architect --db {self.db}",
        )

    def test_ledger_binding_is_part_of_the_seat(self) -> None:
        self.write_claude_seat(["--actor-id", "t-architect"])
        result = self._check({"t-architect": ["claude"]})["mcp:t-architect:claude"]
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("bound to ledger None", result["detail"])
        self.assertIn("claude mcp remove", result["fix"])
        self.assertEqual(self._check({"t-architect": ["claude"]}, db_explicit=False)["mcp:t-architect:claude"]["status"], doctor.OK)
        self.write_claude_seat(["--actor-id", "t-architect", "--db", str(self.db)])
        self.assertEqual(self._check({"t-architect": ["claude"]})["mcp:t-architect:claude"]["status"], doctor.OK)

    def test_equals_form_and_entry_env_bind_the_ledger(self) -> None:
        # DOCTOR-001 F2: ``--db=PATH`` and the entry's AGENT_COMMS_DB are ledger bindings.
        self.write_claude_seat(["--actor-id=t-architect", f"--db={self.db}"])
        self.assertEqual(self._check({"t-architect": ["claude"]})["mcp:t-architect:claude"]["status"], doctor.OK)
        result = self._check({"t-architect": ["claude"]}, db_explicit=False)["mcp:t-architect:claude"]
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn(f"bound to ledger '{self.db}', expected None", result["detail"])
        self.write_claude_seat(["--actor-id", "t-architect"], env={"AGENT_COMMS_DB": str(self.db)})
        self.assertEqual(self._check({"t-architect": ["claude"]})["mcp:t-architect:claude"]["status"], doctor.OK)
        self.assertEqual(self._check({"t-architect": ["claude"]}, db_explicit=False)["mcp:t-architect:claude"]["status"], doctor.FAIL)
        self.write_codex_seat(["--actor-id", "t-architect", f"--db={self.db}"])
        self.assertEqual(self._check({"t-architect": ["codex"]})["mcp:t-architect:codex"]["status"], doctor.OK)

    def test_duplicate_flags_are_malformed_not_healthy(self) -> None:
        # DOCTOR-001 F2: a repeated ``--db`` never passes as the default-ledger binding.
        self.write_claude_seat(["--actor-id", "t-architect", "--db", str(self.db), "--db", str(self.db)])
        for explicit in (True, False):
            result = self._check({"t-architect": ["claude"]}, db_explicit=explicit)["mcp:t-architect:claude"]
            self.assertEqual(result["status"], doctor.FAIL, result)
            self.assertIn("malformed arguments (repeated or valueless --db)", result["detail"])
            self.assertIn("claude mcp remove", result["fix"])
        self.write_claude_seat(["--actor-id", "t-architect", "--actor-id", "t-architect", "--db"])
        result = self._check({"t-architect": ["claude"]})["mcp:t-architect:claude"]
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("--actor-id, --db", result["detail"])

    def test_malformed_seats_entry_fails_instead_of_crashing(self) -> None:
        # DOCTOR-001 F4: ``seats`` that is not a list of client names is a failed check.
        for bad in (1, "claude", ["claude", 2], {"claude": True}):
            with self.subTest(seats=bad):
                results = self._check({"t-architect": bad})
                self.assertEqual(list(results), ["mcp:t-architect"])
                result = results["mcp:t-architect"]
                self.assertEqual(result["status"], doctor.FAIL)
                self.assertIn("not a list of client names", result["detail"])
                self.assertIn("seats.t-architect", result["fix"])
                self.assertTrue(result["fix"].endswith(f"agent-comms --db {self.db} setup"), result["fix"])
        # ``--clients`` sidesteps the recorded entry entirely.
        self.write_codex_seat(["--actor-id", "t-architect", "--db", str(self.db)])
        self.assertEqual(self._check({"t-architect": 1}, clients=["codex"])["mcp:t-architect:codex"]["status"], doctor.OK)

    def test_wrong_command_is_reported(self) -> None:
        self.write_claude_seat(["--actor-id", "t-architect", "--db", str(self.db)], command="/elsewhere/agent-comms-mcp")
        result = self._check({"t-architect": ["claude"]})["mcp:t-architect:claude"]
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("runs '/elsewhere/agent-comms-mcp'", result["detail"])

    def test_command_matches_by_file_identity_not_spelling(self) -> None:
        # An installed package puts a symlink to the script on PATH; a seat
        # added by hand with that path runs the same server. A bare name or a
        # relative path is resolved by the client, so it is not accepted.
        link_dir = self.root / "bin on path"
        link_dir.mkdir()
        (link_dir / "agent-comms-mcp").symlink_to(MCP_COMMAND)
        self.write_claude_seat(["--actor-id", "t-architect", "--db", str(self.db)], command=str(link_dir / "agent-comms-mcp"))
        self.assertEqual(self._check({"t-architect": ["claude"]})["mcp:t-architect:claude"]["status"], doctor.OK)
        for spelled in ("agent-comms-mcp", ".venv/bin/agent-comms-mcp"):
            self.write_claude_seat(["--actor-id", "t-architect", "--db", str(self.db)], command=spelled)
            result = self._check({"t-architect": ["claude"]})["mcp:t-architect:claude"]
            self.assertEqual(result["status"], doctor.FAIL, result)
            self.assertIn(f"runs {spelled!r}", result["detail"])
        self.assertFalse(mcp_clients.same_command(None, MCP_COMMAND))

    def test_codex_seat_absent_wrong_and_present(self) -> None:
        result = self._check({"t-architect": ["codex"]})["mcp:t-architect:codex"]
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertEqual(result["fix"], f"codex mcp add agent-comms -- {MCP_COMMAND} --actor-id t-architect --db {self.db}")
        self.write_codex_seat(["--actor-id", "someone-else", "--db", str(self.db)])
        result = self._check({"t-architect": ["codex"]})["mcp:t-architect:codex"]
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertEqual(result["fix"], f"codex mcp add agent-comms -- {MCP_COMMAND} --actor-id t-architect --db {self.db}")
        self.write_codex_seat(["--actor-id", "t-architect", "--db", str(self.db)])
        self.assertEqual(self._check({"t-architect": ["codex"]})["mcp:t-architect:codex"]["status"], doctor.OK)

    def test_clients_override_replaces_the_recorded_seats(self) -> None:
        self.write_codex_seat(["--actor-id", "t-architect", "--db", str(self.db)])
        results = self._check({"t-architect": ["claude"]}, clients=["codex"])
        self.assertEqual(list(results), ["mcp:t-architect:codex"])
        self.assertEqual(results["mcp:t-architect:codex"]["status"], doctor.OK)
        results = self._check({"t-architect": ["claude"]}, clients=[])
        self.assertEqual(results["mcp:t-architect"]["status"], doctor.SKIP)
        result = self._check({"t-architect": ["cursor"]})["mcp:t-architect:cursor"]
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("unknown MCP client", result["detail"])

    def test_no_architects_and_missing_project_root(self) -> None:
        [result] = doctor.check_mcp([], {}, db_path=self.db, db_explicit=True)
        self.assertEqual((result["id"], result["status"]), ("mcp", doctor.SKIP))
        bare = [{"id": "t-architect", "kind": "agent", "role": "architect", "project_root": None}]
        [result] = doctor.check_mcp(bare, {"t-architect": ["claude"]}, db_path=self.db, db_explicit=True)
        self.assertEqual((result["id"], result["status"]), ("mcp:t-architect", doctor.FAIL))
        self.assertNotIn("cd ''", result["fix"])
        self.assertIn("project_root", result["detail"])

    def test_unparseable_codex_config_counts_as_absent(self) -> None:
        self.codex_home.mkdir()
        (self.codex_home / "config.toml").write_text("[broken")
        result = self._check({"t-architect": ["codex"]})["mcp:t-architect:codex"]
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("config.toml", result["detail"])
        self.assertEqual(result["fix"], f"codex mcp add agent-comms -- {MCP_COMMAND} --actor-id t-architect --db {self.db}")

    def test_unparseable_claude_config_counts_as_absent(self) -> None:
        self.claude_config.write_text("{")
        result = self._check({"t-architect": ["claude"]})["mcp:t-architect:claude"]
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn(str(self.claude_config), result["detail"])
        self.assertNotIn("remove", result["fix"])


class McpClientHelpersTest(unittest.TestCase):
    def test_binding_and_actor_ids(self) -> None:
        self.assertEqual(mcp_clients.binding(["x", "--actor-id", "a", "--db", "/d"]), {"actor_id": "a", "db": "/d", "malformed": []})
        self.assertEqual(mcp_clients.binding(["--actor-id"]), {"actor_id": None, "db": None, "malformed": ["--actor-id"]})
        self.assertEqual(mcp_clients.actor_ids_in(["--actor-id", "a", "--actor-id", "b"]), (["a", "b"], True))
        self.assertEqual(mcp_clients.actor_ids_in(["--actor-id"]), ([], True))
        self.assertEqual(mcp_clients.actor_ids_in(["--actor-id", "a"]), (["a"], False))
        self.assertEqual(mcp_clients.actor_ids_in(["--actor-id=a"]), (["a"], False))

    def test_binding_reads_both_flag_forms_and_rejects_duplicates(self) -> None:
        # ``--db=PATH`` is a ledger binding, not the absence of one (DOCTOR-001 F2).
        self.assertEqual(mcp_clients.binding(["--actor-id=a", "--db=/scratch/other.sqlite"]), {"actor_id": "a", "db": "/scratch/other.sqlite", "malformed": []})
        duplicate = mcp_clients.binding(["--actor-id", "a", "--db", "/one", "--db", "/two"])
        self.assertEqual(duplicate, {"actor_id": "a", "db": None, "malformed": ["--db"]})
        mixed = mcp_clients.binding(["--actor-id", "a", "--db=/one", "--db", "/two"])
        self.assertEqual(mixed["malformed"], ["--db"])
        self.assertEqual(mcp_clients.binding(["--actor-id", "a", "--db"])["malformed"], ["--db"])
        self.assertEqual(mcp_clients.flag_values(["--db", "/one", "--db=/two"], "--db"), (["/one", "/two"], True))

    def test_same_ledger_compares_absolute_paths_by_file_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            real = Path(temp_dir) / "real"
            real.mkdir()
            (real / "ledger.sqlite").write_text("")
            link = Path(temp_dir) / "link"
            link.symlink_to(real)
            self.assertTrue(mcp_clients.same_ledger(str(link / "ledger.sqlite"), str(real / "ledger.sqlite")))
            self.assertTrue(mcp_clients.same_ledger(str(real / "ledger.sqlite"), str(real / "ledger.sqlite")))
            self.assertFalse(mcp_clients.same_ledger(str(real / "other.sqlite"), str(real / "ledger.sqlite")))
            self.assertFalse(mcp_clients.same_ledger("ledger.sqlite", str(real / "ledger.sqlite")))
            # The client does not expand ~ when it execs the server, so a stored ~ path is not the file.
            with mock.patch.dict(os.environ, {"HOME": temp_dir}):
                self.assertFalse(mcp_clients.same_ledger("~/real/ledger.sqlite", str(real / "ledger.sqlite")))
                self.assertTrue(mcp_clients.same_ledger(str(real / "ledger.sqlite"), "~/real/ledger.sqlite"))
            self.assertTrue(mcp_clients.same_ledger(None, None))
            self.assertFalse(mcp_clients.same_ledger(None, str(real / "ledger.sqlite")))
            self.assertFalse(mcp_clients.same_ledger(str(real / "ledger.sqlite"), None))

    def test_binding_honours_the_entry_environment(self) -> None:
        # The server resolves AGENT_COMMS_DB itself, so an entry that sets it is bound to that ledger.
        self.assertEqual(mcp_clients.binding(["--actor-id", "a"], {"AGENT_COMMS_DB": "/env.sqlite"})["db"], "/env.sqlite")
        self.assertEqual(mcp_clients.binding(["--actor-id", "a", "--db", "/flag"], {"AGENT_COMMS_DB": "/env.sqlite"})["db"], "/flag")
        self.assertIsNone(mcp_clients.binding(["--actor-id", "a"], {"AGENT_COMMS_DB": ""})["db"])
        self.assertIsNone(mcp_clients.binding(["--actor-id", "a", "--db", "/one", "--db", "/two"], {"AGENT_COMMS_DB": "/env.sqlite"})["db"])

    def test_commands_render_the_same_argv(self) -> None:
        argv = mcp_clients.server_argv("/bin/agent-comms-mcp", "arch")
        self.assertEqual(argv, ["/bin/agent-comms-mcp", "--actor-id", "arch"])
        self.assertEqual(mcp_clients.claude_add_command(argv), ["claude", "mcp", "add", "agent-comms", "--scope", "local", "--", *argv])
        self.assertEqual(mcp_clients.codex_add_command(argv), ["codex", "mcp", "add", "agent-comms", "--", *argv])
        self.assertEqual(mcp_clients.render_fix("claude", "/p q", argv, replace=False), "cd '/p q' && claude mcp add agent-comms --scope local -- /bin/agent-comms-mcp --actor-id arch")
        self.assertEqual(mcp_clients.render_fix("codex", "/p", argv, replace=True), "codex mcp add agent-comms -- /bin/agent-comms-mcp --actor-id arch")
        with self.assertRaises(ValueError):
            mcp_clients.render_fix("cursor", "/p", argv, replace=False)

    def test_claude_reader_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "claude.json"
            self.assertEqual(mcp_clients.read_claude_seat(config, temp_dir).status, "missing")
            config.write_text("{")
            self.assertEqual(mcp_clients.read_claude_seat(config, temp_dir).status, "unparseable")
            config.write_bytes(b"\xff\xfe{}")  # not UTF-8: a decode error is unparseable, not a crash
            self.assertEqual(mcp_clients.read_claude_seat(config, temp_dir).status, "unparseable")
            config.write_text(json.dumps({"projects": {}}))
            self.assertEqual(mcp_clients.read_claude_seat(config, temp_dir).status, "no_project")
            config.write_text(json.dumps({"projects": {os.path.realpath(temp_dir): {"mcpServers": {"agent-comms": {"command": "/m", "args": ["--actor-id", "a"]}, "other": {"args": ["--actor-id", "b"]}}}}}))
            reading = mcp_clients.read_claude_seat(config, temp_dir)
            self.assertEqual(reading.status, "ok")
            found = reading.server()
            self.assertEqual((found.name, found.command, found.args, found.env), ("agent-comms", "/m", ["--actor-id", "a"], {}))
            self.assertEqual(reading.servers, [("agent-comms", ["--actor-id", "a"]), ("other", ["--actor-id", "b"])])
            self.assertEqual(reading.server("other").command, None)
            self.assertIsNone(reading.server("nope"))
            config.write_text(json.dumps({"projects": {os.path.realpath(temp_dir): {"mcpServers": {"agent-comms": {"command": "/m", "args": "--actor-id a", "env": {"AGENT_COMMS_DB": "/e"}}}}}}))
            found = mcp_clients.read_claude_seat(config, temp_dir).server()
            self.assertEqual((found.args, found.env), ([], {"AGENT_COMMS_DB": "/e"}))

    def test_codex_reader_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.toml"
            self.assertEqual(mcp_clients.read_codex_seat(config).status, "missing")
            config.write_text("[broken")
            self.assertEqual(mcp_clients.read_codex_seat(config).status, "unparseable")
            config.write_bytes(b"\xff\xfe")
            self.assertEqual(mcp_clients.read_codex_seat(config).status, "unparseable")
            config.write_text('[mcp_servers.agent-comms]\ncommand = "/m"\nargs = ["--actor-id", "a"]\n')
            reading = mcp_clients.read_codex_seat(config)
            self.assertEqual(reading.status, "ok")
            self.assertEqual((reading.server().command, reading.server().args), ("/m", ["--actor-id", "a"]))
            config.write_text('[mcp_servers.agent-comms]\ncommand = "/m"\nargs = ["--actor-id", "a"]\n[mcp_servers.agent-comms.env]\nAGENT_COMMS_DB = "/e"\n')
            self.assertEqual(mcp_clients.read_codex_seat(config).server().env, {"AGENT_COMMS_DB": "/e"})

    def test_config_paths_follow_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {mcp_clients.CLAUDE_CONFIG_ENV: "/x/claude.json", "CODEX_HOME": "/x/codex"}):
            self.assertEqual(mcp_clients.claude_config_path(), Path("/x/claude.json"))
            self.assertEqual(mcp_clients.codex_config_path(), Path("/x/codex/config.toml"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(mcp_clients.CLAUDE_CONFIG_ENV, None)
            os.environ.pop("CODEX_HOME", None)
            self.assertEqual(mcp_clients.claude_config_path(), Path.home() / ".claude.json")
            self.assertEqual(mcp_clients.codex_config_path(), Path.home() / ".codex" / "config.toml")


class DoctorCommandTest(_Scratch):
    def test_missing_ledger_exits_three_without_creating_it(self) -> None:
        rc, result = self.run_cli()
        self.assertEqual(rc, 3)
        self.assertFalse(result["ok"])
        self.assertFalse(self.db.exists())
        ids = [item["id"] for item in result["checks"]]
        self.assertEqual(ids[:4], ["install", "runtime_root", "registry", "ledger"])
        self.assertEqual(ids[-1], "admin_token")
        failing = [item["id"] for item in result["checks"] if item["status"] == doctor.FAIL]
        self.assertEqual(failing, ["registry", "ledger"])
        self.assertEqual(result["fixes"], [item["fix"] for item in result["checks"] if item["status"] == doctor.FAIL])
        # ``--db`` was given, so every repair names that ledger rather than the default one.
        self.assertTrue(result["fixes"][0].startswith(f"agent-comms --db {self.db} setup "), result["fixes"])
        self.assertEqual(result["fixes"][1], f"agent-comms --db {self.db} bootstrap (after agent-comms --db {self.db} setup)")

    def test_clean_install_exits_zero_with_no_fixes(self) -> None:
        self.bootstrap(seats={"t-architect": ["claude", "codex"]})
        self.write_claude_seat(["--actor-id", "t-architect", "--db", str(self.db)])
        self.write_codex_seat(["--actor-id", "t-architect", "--db", str(self.db)])
        rc, result = self.run_cli()
        self.assertEqual(rc, 0, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["fixes"], [])
        by_id = {item["id"]: item for item in result["checks"]}
        self.assertEqual(by_id["monitor"]["status"], doctor.WARN)
        self.assertEqual(by_id["platform"]["status"], doctor.SKIP)
        self.assertEqual(by_id["runtime:claude"]["status"], doctor.SKIP)
        self.assertEqual(by_id["runtime:codex"]["status"], doctor.SKIP)
        self.assertEqual(by_id["mcp:t-architect:claude"]["status"], doctor.OK)
        self.assertEqual(by_id["mcp:t-architect:codex"]["status"], doctor.OK)
        self.assertEqual(by_id["admin_token"]["status"], doctor.WARN)
        self.assertEqual(result["platform"], runtime_pins.current_platform())

    def test_fixes_are_ordered_and_the_clients_flag_overrides_seats(self) -> None:
        self.bootstrap(seats={"t-architect": ["claude"]})
        rc, result = self.run_cli()
        self.assertEqual(rc, 3)
        self.assertEqual([item["id"] for item in result["checks"] if item["status"] == doctor.FAIL], ["mcp:t-architect:claude"])
        self.assertTrue(result["fixes"][0].startswith(f"cd {os.path.realpath(self.root / 'project')} && claude mcp add"), result["fixes"])
        rc, result = self.run_cli("--clients", "")
        self.assertEqual(rc, 0, result)
        self.write_codex_seat(["--actor-id", "t-architect", "--db", str(self.db)])
        rc, result = self.run_cli("--clients", "codex")
        self.assertEqual(rc, 0, result)
        self.assertNotIn("mcp:t-architect:claude", {item["id"] for item in result["checks"]})

    def test_roster_problem_still_reports_monitor_and_seats(self) -> None:
        self.bootstrap(humans=(HUMAN, "01M36YTJV9XBW95S6ZWV47C4RH"), seats={"t-architect": ["codex"]})
        rc, result = self.run_cli()
        self.assertEqual(rc, 3)
        by_id = {item["id"]: item for item in result["checks"]}
        self.assertEqual(by_id["ledger"]["status"], doctor.FAIL)
        self.assertEqual(by_id["monitor"]["status"], doctor.WARN)
        self.assertEqual(by_id["monitor"]["fix"], f"agent-comms-monitor --db {self.db} --human-actor-id <human-actor-id>")
        self.assertEqual(by_id["mcp:t-architect:codex"]["status"], doctor.FAIL)

    def test_seat_on_another_ledger_exits_three(self) -> None:
        # DOCTOR-001 F2 as observed through the CLI: a seat whose ``--db=`` names
        # another ledger is not a healthy seat.
        self.bootstrap(seats={"t-architect": ["claude"]})
        other = self.root / "other.sqlite"
        self.write_claude_seat(["--actor-id", "t-architect", f"--db={other}"])
        rc, result = self.run_cli()
        self.assertEqual(rc, 3)
        by_id = {item["id"]: item for item in result["checks"]}
        self.assertEqual(by_id["mcp:t-architect:claude"]["status"], doctor.FAIL)
        self.assertIn(f"bound to ledger '{other}'", by_id["mcp:t-architect:claude"]["detail"])
        self.write_claude_seat(["--actor-id", "t-architect", "--db", str(self.db), "--db", str(self.db)])
        rc, result = self.run_cli()
        self.assertEqual(rc, 3)
        self.assertEqual(result["fixes"], [f"cd {os.path.realpath(self.root / 'project')} && claude mcp remove agent-comms --scope local && claude mcp add agent-comms --scope local -- {MCP_COMMAND} --actor-id t-architect --db {self.db}"])

    def test_malformed_seats_entry_still_yields_the_report(self) -> None:
        # DOCTOR-001 F4 through the CLI: exit 3 with the report, not a traceback.
        self.bootstrap(seats={"t-architect": 1})
        rc, result = self.run_cli()
        self.assertEqual(rc, 3)
        by_id = {item["id"]: item for item in result["checks"]}
        self.assertEqual(by_id["mcp:t-architect"]["status"], doctor.FAIL)
        self.assertEqual([item["id"] for item in result["checks"]][-1], "admin_token")

    def test_default_ledger_repairs_carry_no_db(self) -> None:
        # DOCTOR-001 F1: without ``--db`` or AGENT_COMMS_DB the repairs run against the default ledger.
        self.bootstrap(seats={"t-architect": ["codex"]})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_COMMS_DB", None)
            with mock.patch.object(paths, "db_path", return_value=self.db):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    rc = cli.run(["doctor"])
        result = json.loads(buffer.getvalue())
        self.assertEqual(rc, 3)
        by_id = {item["id"]: item for item in result["checks"]}
        self.assertEqual(by_id["monitor"]["fix"], f"agent-comms-monitor --human-actor-id {HUMAN}")
        self.assertEqual(by_id["mcp:t-architect:codex"]["fix"], f"codex mcp add agent-comms -- {MCP_COMMAND} --actor-id t-architect")
        with mock.patch.dict(os.environ, {"AGENT_COMMS_DB": str(self.db)}):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cli.run(["doctor"])
        by_id = {item["id"]: item for item in json.loads(buffer.getvalue())["checks"]}
        self.assertEqual(by_id["monitor"]["fix"], f"agent-comms-monitor --db {self.db} --human-actor-id {HUMAN}")

    def test_run_doctor_normalizes_a_relative_ledger_path(self) -> None:
        # A library caller passing a relative path gets the same absolute binding the CLI would.
        self.bootstrap(seats={"t-architect": ["codex"]})
        self.write_codex_seat(["--actor-id", "t-architect", "--db", str(self.db)])
        previous = os.getcwd()
        os.chdir(self.db.parent)
        self.addCleanup(os.chdir, previous)
        result = doctor.run_doctor(db_path=Path(self.db.name), db_explicit=True)
        by_id = {item["id"]: item for item in result["checks"]}
        self.assertEqual(by_id["mcp:t-architect:codex"]["status"], doctor.OK, by_id["mcp:t-architect:codex"])
        self.assertTrue(by_id["monitor"]["fix"].startswith(f"agent-comms-monitor --db {shlex.quote(os.path.abspath(self.db.name))} "), by_id["monitor"])

    def test_native_runtimes_are_checked_through_the_injected_runners(self) -> None:
        self.bootstrap(workers=("claude", "codex"), seats={"t-architect": []})
        pins = {
            "claude": RuntimePin("claude", "test-platform", "9.9.9", "exact", "2026-09-26", hashlib.sha256(b"claude").hexdigest()),
            "codex": RuntimePin("codex", "test-platform", "0.157.0", "exact", "2026-09-26"),
        }
        binary = self.root / "custody" / "9.9.9"
        binary.parent.mkdir()
        binary.write_bytes(b"claude")
        run = _runner(
            {
                (str(binary), "--version"): {"stdout": "9.9.9"},
                ("auth", "status"): {"stdout": json.dumps({"loggedIn": True})},
                ("/opt/bin/codex", "--version"): {"stdout": "codex-cli 0.157.0"},
            }
        )
        with mock.patch.object(doctor, "current_platform", return_value="test-platform"), \
                mock.patch.object(doctor, "pin_for", side_effect=lambda runtime, platform=None: pins[runtime]):
            result = doctor.run_doctor(
                db_path=self.db,
                db_explicit=True,
                which=lambda name: "/opt/bin/codex" if name == "codex" else None,
                run=run,
                now=NOW,
            )
        by_id = {item["id"]: item for item in result["checks"]}
        self.assertEqual(result["platform"], "test-platform")
        self.assertEqual(by_id["platform"]["status"], doctor.OK)
        self.assertEqual(by_id["runtime:claude"]["status"], doctor.OK, by_id["runtime:claude"])
        self.assertEqual(by_id["runtime:codex"]["status"], doctor.FAIL)
        self.assertEqual(by_id["runtime:codex"]["fix"], f"CODEX_HOME={self.runtime_root / 'codex-auth'} codex login")
        home_check = by_id["runtime:codex:home:t-codex-worker"]
        self.assertEqual(home_check["status"], doctor.FAIL)
        self.assertIn("--override-protected", home_check["fix"])
        self.assertEqual(by_id["mcp:t-architect"]["status"], doctor.SKIP)
        self.assertFalse(result["ok"])
