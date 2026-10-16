"""``AGENT_COMMS_HOME``: a second install beside the first (ac-4ao.10 slice 1)."""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import argparse
import contextlib
import hashlib
import io
import json
import os
import shlex
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import cli, doctor, mcp_clients, mcp_server, paths, policies, provisioning, push_approval, runtime_pins, setup, supervisor
from agent_comms.adapters.fake import FakeAdapter
from agent_comms.cli import _helpers
from agent_comms.dispatch_ledger import DispatchLedger, _admin_token_prefix
from agent_comms.reviewing import approval
from agent_comms.reviewing import store as review_store
from agent_comms.schema import ValidationError
from agent_comms.store import Store
from tests.substrate.test_setup import FakeClients

HUMAN = "01M36YTJV9XBW95S6ZWV47C4RG"
NARROW_OVERRIDES = (
    "AGENT_COMMS_DB",
    "AGENT_COMMS_CODEX_CUSTODY_ROOT",
    "AGENT_COMMS_DISPATCH_LOG_DIR",
    runtime_pins.CLAUDE_VERSIONS_DIR_ENV,
    "AGENT_COMMS_APPROVAL_KEY",
)


def tree(root: Path) -> dict[str, str]:
    """Every entry under ``root`` with a content hash for files."""
    if not root.exists():
        return {}
    listing = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_symlink():
            listing[key] = "link:" + os.readlink(path)
        elif path.is_file():
            listing[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            listing[key] = "dir"
    return listing


class _Home(unittest.TestCase):
    """A short ``AGENT_COMMS_HOME`` and no narrower overrides."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="h")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / "ac"
        env = mock.patch.dict(os.environ, {paths.HOME_ENV: str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for name in NARROW_OVERRIDES:
            saved = os.environ.pop(name, None)
            if saved is not None:
                self.addCleanup(os.environ.__setitem__, name, saved)


class RuntimePathsUnderHomeTest(_Home):
    def test_every_runtime_path_resolves_under_the_home(self) -> None:
        home = self.home
        self.assertEqual(paths.runtime_root(), home)
        self.assertEqual(paths.canonical_db_path(), home / "agent-comms.sqlite")
        self.assertEqual(paths.db_path(), home / "agent-comms.sqlite")
        self.assertEqual(paths.actors_config_path(), home / "actors.json")
        self.assertEqual(paths.review_root(), home / "dispatch" / "reviews")
        self.assertEqual(paths.push_approval_root(), home / "dispatch" / "push-approvals")
        self.assertEqual(paths.codex_custody_root(), home / "codex-homes")
        self.assertEqual(paths.provisioned_codex_home("w"), home / "codex-homes" / "default" / "w")
        self.assertEqual(paths.runtime_codex_auth_source(), home / "codex-auth" / "auth.json")
        self.assertEqual(paths.dispatch_log_dir(), home / "logs" / "dispatch")
        self.assertEqual(runtime_pins.claude_versions_dir(), home / "runtime-custody")
        self.assertEqual(supervisor.default_control_root(), home / "run" / "s")
        self.assertEqual(approval.approval_key_path(argparse.Namespace(key=None)), home / "approval-key")
        # Module roots that tests patch are resolved per call, not at import.
        self.assertEqual(review_store.review_paths("d").json, home / "dispatch" / "reviews" / "d.json")
        self.assertIsNone(push_approval.PUSH_APPROVAL_ROOT)

    def test_unset_home_keeps_every_path_where_it_was(self) -> None:
        del os.environ[paths.HOME_ENV]
        default = Path.home() / ".agent-comms"
        self.assertIsNone(paths.home_override())
        self.assertEqual(paths.runtime_root(), default)
        self.assertEqual(paths.db_path(), default / "agent-comms.sqlite")
        self.assertEqual(supervisor.default_control_root(), default / "run" / "s")
        self.assertEqual(runtime_pins.claude_versions_dir(), default / "runtime-custody")
        self.assertEqual(approval.approval_key_path(argparse.Namespace(key=None)), default / "approval-key")
        self.assertEqual(policies.credential_read_deny(), policies.CREDENTIAL_READ_DENY)

    def test_narrower_overrides_still_win_over_the_home(self) -> None:
        elsewhere = Path(self.tmp.name) / "elsewhere"
        with mock.patch.dict(
            os.environ,
            {
                "AGENT_COMMS_DB": str(elsewhere / "l.sqlite"),
                "AGENT_COMMS_CODEX_CUSTODY_ROOT": str(elsewhere / "homes"),
                "AGENT_COMMS_DISPATCH_LOG_DIR": str(elsewhere / "logs"),
                runtime_pins.CLAUDE_VERSIONS_DIR_ENV: str(elsewhere / "custody"),
                "AGENT_COMMS_APPROVAL_KEY": str(elsewhere / "key"),
            },
        ):
            self.assertEqual(paths.db_path(), elsewhere / "l.sqlite")
            self.assertEqual(paths.codex_custody_root(), elsewhere / "homes")
            self.assertEqual(paths.dispatch_log_dir(), elsewhere / "logs")
            self.assertEqual(runtime_pins.claude_versions_dir(), elsewhere / "custody")
            self.assertEqual(approval.approval_key_path(argparse.Namespace(key=None)), elsewhere / "key")

    def test_admin_token_is_read_from_the_home(self) -> None:
        self.home.mkdir(parents=True)
        token = self.home / "admin-token"
        token.write_text("operator-secret\n")
        os.chmod(token, 0o600)
        with mock.patch.dict(os.environ, {"AGENT_COMMS_ADMIN_TOKEN": "operator-secret"}):
            self.assertEqual(_helpers.require_admin_credential(), "operator-secret")
        token.unlink()
        with mock.patch.dict(os.environ, {"AGENT_COMMS_ADMIN_TOKEN": "operator-secret"}):
            with self.assertRaises(ValidationError):
                _helpers.require_admin_credential()

    def test_operator_hints_name_the_homes_admin_token(self) -> None:
        line = DispatchLedger._settle_execute_command(None, "human", "dispatch_x", "plan")  # type: ignore[arg-type]
        self.assertTrue(
            line.startswith(f'AGENT_COMMS_HOME={self.home} AGENT_COMMS_ADMIN_TOKEN="$(cat {self.home}/admin-token)" '),
            line,
        )
        del os.environ[paths.HOME_ENV]
        self.assertEqual(_admin_token_prefix(), 'AGENT_COMMS_ADMIN_TOKEN="$(cat ~/.agent-comms/admin-token)" ')

    def test_worker_sandbox_denies_reads_of_the_home(self) -> None:
        self.assertEqual(policies.credential_read_deny(), (*policies.CREDENTIAL_READ_DENY, str(self.home)))
        compiled = policies.compile_policy(policies.WORKER_DISPATCH_POLICY)
        self.assertIn(str(self.home), compiled.claude_sandbox.deny_read)

    def test_a_relative_home_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {paths.HOME_ENV: "relative/home"}):
            with self.assertRaisesRegex(ValidationError, "not an absolute path"):
                paths.runtime_root()

    def test_home_with_tilde_is_expanded(self) -> None:
        with mock.patch.dict(os.environ, {paths.HOME_ENV: "~/.ac-dev"}):
            self.assertEqual(paths.runtime_root(), Path.home() / ".ac-dev")


class SocketLengthTest(_Home):
    """A home too long for the control socket is refused up front."""

    def long_home(self) -> Path:
        home = Path(self.tmp.name).resolve() / ("x" * 70)
        os.environ[paths.HOME_ENV] = str(home)
        os.environ.pop(supervisor.CONTROL_ROOT_ENV, None)
        return home

    def test_limit_is_62_bytes_of_home(self) -> None:
        os.environ.pop(supervisor.CONTROL_ROOT_ENV, None)
        # Only the byte count matters; nothing is created.
        fits = "/" + "y" * 61
        self.assertIsNone(supervisor.control_root_problem(Path(fits) / "run" / "s"))
        problem = supervisor.control_root_problem(Path(fits + "y") / "run" / "s")
        self.assertIn("104 bytes", problem)
        self.assertIn("at most 62 bytes", problem)

    def test_spawn_refuses_before_the_wrapper_starts(self) -> None:
        self.long_home()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("wrapper started")):
            with self.assertRaisesRegex(supervisor.SunPathTooLong, "AGENT_COMMS_HOME"):
                supervisor.spawn_supervised(
                    ["true"], dispatch_id="d", db_path="x.sqlite", ttl_seconds=1, kill_after_seconds=1
                )

    def test_setup_and_doctor_name_the_byte_count_and_the_fix(self) -> None:
        home = self.long_home()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.run(["setup", "--project-root", self.tmp.name, "--runtimes", "fake", "--clients", "none", "--yes"])
        self.assertEqual(rc, 2)
        error = json.loads(out.getvalue())["error"]
        self.assertIn("bytes, over the AF_UNIX limit", error)
        self.assertIn("a shorter AGENT_COMMS_HOME", error)
        self.assertFalse(home.exists())
        del os.environ[paths.HOME_ENV]
        check = doctor.check_runtime_root(home)
        self.assertEqual(check["status"], doctor.FAIL)
        self.assertIn("bytes", check["detail"])
        self.assertIn(paths.HOME_ENV, check["fix"])


class SeatBindingTest(_Home):
    """Seats carry the home in argv; doctor and setup compare it."""

    def test_server_argv_and_binding_carry_the_home(self) -> None:
        argv = mcp_clients.server_argv("/bin/agent-comms-mcp", "a", None, self.home)
        self.assertEqual(argv, ["/bin/agent-comms-mcp", "--actor-id", "a", "--home", str(self.home)])
        self.assertEqual(mcp_clients.binding(argv)["home"], str(self.home))
        self.assertEqual(mcp_clients.binding(["--actor-id", "a"], {"AGENT_COMMS_HOME": "/h"})["home"], "/h")
        self.assertEqual(mcp_clients.binding(["--actor-id", "a", "--home", "/h"], {"AGENT_COMMS_HOME": "/g"})["home"], "/h")
        self.assertEqual(mcp_clients.binding(["--home", "/h", "--home", "/g"])["malformed"], ["--home"])
        self.assertTrue(mcp_clients.same_home(None, None))
        self.assertFalse(mcp_clients.same_home(None, self.home))
        self.assertFalse(mcp_clients.same_home(str(self.home), None))
        self.assertTrue(mcp_clients.same_home(str(self.home), self.home))

    def check_seat(self, args: list[str]) -> dict:
        project = Path(self.tmp.name) / "p"
        project.mkdir(exist_ok=True)
        config = Path(self.tmp.name) / "claude.json"
        command = str(Path(sys.executable).parent / "agent-comms-mcp")
        config.write_text(json.dumps({"projects": {str(project): {"mcpServers": {"agent-comms": {"command": command, "args": args}}}}}))
        with mock.patch.dict(os.environ, {mcp_clients.CLAUDE_CONFIG_ENV: str(config)}):
            (result,) = doctor.check_mcp(
                [{"id": "t-architect", "project_root": str(project)}],
                {"t-architect": ["claude"]},
                db_path=paths.db_path(),
                db_explicit=False,
            )
        return result

    def test_doctor_fails_a_seat_without_or_with_another_home(self) -> None:
        missing = self.check_seat(["--actor-id", "t-architect"])
        self.assertEqual(missing["status"], doctor.FAIL)
        self.assertIn(f"bound to home None, expected '{self.home}'", missing["detail"])
        # The fix is the bind-seat line, run under this install's home.
        self.assertEqual(missing["fix"], f"AGENT_COMMS_HOME={self.home} agent-comms bind-seat t-architect --client claude --replace-seat")
        other = self.check_seat(["--actor-id", "t-architect", "--home", "/other"])
        self.assertEqual(other["status"], doctor.FAIL)
        ok = self.check_seat(["--actor-id", "t-architect", "--home", str(self.home)])
        self.assertEqual(ok["status"], doctor.OK, ok)

    def test_doctor_fails_a_seat_with_a_home_when_none_is_set(self) -> None:
        del os.environ[paths.HOME_ENV]
        result = self.check_seat(["--actor-id", "t-architect", "--home", str(self.home)])
        self.assertEqual(result["status"], doctor.FAIL)
        self.assertIn("expected None", result["detail"])

    def test_repairs_carry_the_home(self) -> None:
        ledger = doctor.LedgerRef(self.home / "agent-comms.sqlite", False)
        self.assertEqual(ledger.cli("doctor"), f"AGENT_COMMS_HOME={self.home} agent-comms doctor")
        del os.environ[paths.HOME_ENV]
        self.assertEqual(ledger.cli("doctor"), "agent-comms doctor")


class SetupSeatUnderHomeTest(_Home):
    def test_setup_writes_the_home_into_the_seat_and_doctor_accepts_it(self) -> None:
        project = Path(self.tmp.name) / "p"
        project.mkdir()
        claude_config = Path(self.tmp.name) / "claude.json"
        clients = FakeClients(claude_config, Path(self.tmp.name) / "codex" / "config.toml")
        with mock.patch.dict(os.environ, {mcp_clients.CLAUDE_CONFIG_ENV: str(claude_config)}):
            result = setup.run_setup(
                Store(paths.db_path(), is_default_db_open=True),
                db_path=paths.db_path(),
                db_explicit=False,
                project_root=str(project),
                team="t",
                runtimes=["fake"],
                clients=["claude"],
                human_id=HUMAN,
                which=clients.which,
                run=clients.run,
            )
            (add,) = [argv for argv, _cwd in clients.calls if argv[1:3] == ["mcp", "add"]]
            self.assertEqual(add[-2:], ["--home", str(self.home)])
            report = doctor.run_doctor(db_path=paths.db_path(), db_explicit=False, which=clients.which, run=clients.run)
        seat = next(c for c in report["checks"] if c["id"] == "mcp:t-architect:claude")
        self.assertEqual(seat["status"], doctor.OK, seat)
        self.assertEqual(result["runtime_root"], str(self.home))


class McpServerHomeTest(_Home):
    def run_main(self, *argv: str) -> dict:
        seen: dict = {}

        def create_server(**kwargs):
            seen["home"] = os.environ.get(paths.HOME_ENV)
            seen["root"] = paths.runtime_root()
            return mock.Mock()

        with mock.patch.object(sys, "argv", ["agent-comms-mcp", *argv]), \
                mock.patch.object(mcp_server, "preflight_mcp_import"), \
                mock.patch.object(mcp_server, "print_startup_report"), \
                mock.patch.object(mcp_server, "create_server", side_effect=create_server), \
                mock.patch.dict(os.environ, {}):
            mcp_server.main()
        return seen

    def test_home_flag_wins_over_the_env(self) -> None:
        other = Path(self.tmp.name) / "other"
        seen = self.run_main("--actor-id", "a", "--home", str(other))
        self.assertEqual(seen["root"], other)
        self.assertEqual(self.run_main("--actor-id", "a")["root"], self.home)

    def test_relative_home_flag_is_refused(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as raised:
            self.run_main("--actor-id", "a", "--home", "rel")
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not an absolute path", err.getvalue())


class WorkerPropagationTest(_Home):
    def context(self):
        from agent_comms.adapters import DispatchContext

        return DispatchContext(
            dispatch={"dispatch_id": "dispatch_20260101_000000_00000000", "policy_name": policies.WORKER_DISPATCH_POLICY},
            message={"id": "m"},
            recipient={"id": "w", "project_root": self.tmp.name, "runtime": "fake"},
            ttl_seconds=60,
            expected_close_by="2026-01-01T00:00:00+00:00",
            db_path=str(Path(self.tmp.name) / "l.sqlite"),
        )

    def test_worker_env_carries_the_home_only_when_set(self) -> None:
        adapter = FakeAdapter()
        policy = policies.compile_policy(policies.WORKER_DISPATCH_POLICY)
        context = self.context()
        env = adapter._build_env(context, policy, {}, zdotdir=Path(self.tmp.name))
        self.assertEqual(env[paths.HOME_ENV], str(self.home))
        self.assertEqual(adapter._format_arg("{agent_comms_home}", context), str(self.home))
        del os.environ[paths.HOME_ENV]
        env = adapter._build_env(context, policy, {}, zdotdir=Path(self.tmp.name))
        self.assertNotIn(paths.HOME_ENV, env)
        self.assertEqual(adapter._format_arg("{agent_comms_home}", context), str(Path.home() / ".agent-comms"))

    def test_codex_base_config_binds_and_trusts_the_home(self) -> None:
        # The home reaches the worker's server through the per-run -c binding
        # (slice 3b); the base config only trusts it.
        config = tomllib.loads(provisioning.render_codex_base_config(self.tmp.name))
        self.assertNotIn("mcp_servers", config)
        self.assertEqual(config["projects"][str(self.home)]["trust_level"], "trusted")
        del os.environ[paths.HOME_ENV]
        config = tomllib.loads(provisioning.render_codex_base_config(self.tmp.name))
        self.assertEqual(set(config["projects"]), {self.tmp.name, str(Path.home())})


def _worker_overrides(actor_id: str, project: str, db: str) -> dict:
    """The worker's per-run ``-c`` binding, each value parsed as TOML; the
    policy hook's command under ``"hook"``, as the shell will split it."""
    from agent_comms.adapters import DispatchContext
    from agent_comms.adapters.codex import CodexAdapter

    context = DispatchContext(
        dispatch={"dispatch_id": "dispatch_20260101_000000_00000000", "policy_name": policies.WORKER_DISPATCH_POLICY},
        recipient={"id": actor_id, "runtime": "codex", "project_root": project},
        message={"id": "m"}, ttl_seconds=30, expected_close_by="2026-01-01T00:00:00+00:00", db_path=db,
    )
    argv = CodexAdapter()._runtime_prefix_args(context)
    prefix = "mcp_servers.agent-comms."
    server = {}
    for flag, pair in zip(argv[::2], argv[1::2]):
        assert flag == "-c", flag
        if pair.startswith("hooks.PreToolUse="):
            command = tomllib.loads(pair)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
            server["hook"] = shlex.split(command)
            continue
        if pair.startswith("hooks.state="):
            continue
        assert pair.startswith(prefix), pair
        key, value = pair[len(prefix):].split("=", 1)
        server[key] = tomllib.loads(f"v = {value}")["v"]
    return server


class GeneratedConfigEscapingTest(_Home):
    """Paths are data in the generated Codex TOML and Claude MCP JSON."""

    def odd_home(self) -> Path:
        home = Path(self.tmp.name).resolve() / 'q"b\\s'
        os.environ[paths.HOME_ENV] = str(home)
        return home

    def test_home_equal_to_an_outside_project_is_trusted_once(self) -> None:
        project = str(self.home)
        self.assertFalse(self.home.is_relative_to(Path.home()))
        text = provisioning.render_codex_base_config(project)
        config = tomllib.loads(text)
        self.assertEqual(set(config["projects"]), {project, str(Path.home())})
        self.assertEqual(text.count("[projects."), 2)

    def test_codex_config_round_trips_quotes_and_backslashes(self) -> None:
        home = self.odd_home()
        project = str(Path(self.tmp.name).resolve() / 'p"r\\j')
        db = str(Path(self.tmp.name).resolve() / 'l"g.sqlite')
        config = tomllib.loads(provisioning.render_codex_base_config(project))
        self.assertEqual(set(config["projects"]), {project, str(Path.home()), str(home)})
        server = _worker_overrides("w", project, db)
        self.assertEqual(server["args"], ["--actor-id", "w", "--db", db, "--home", str(home)])
        self.assertEqual(server["env"]["AGENT_COMMS_PROJECT_ROOT"], project)
        self.assertEqual(server["hook"][server["hook"].index("--project-root") + 1], project)

    def test_codex_configs_round_trip_non_ascii_and_control_characters(self) -> None:
        odd = "proj-\U0001F600-\u00e9-\u4e2d-tab\there-del\x7f-nl\n"
        home = Path(self.tmp.name).resolve() / ("h-\U0001F600" + "\x7f")
        os.environ[paths.HOME_ENV] = str(home)
        project = str(Path(self.tmp.name).resolve() / odd)
        db = str(Path(self.tmp.name).resolve() / (odd + ".sqlite"))
        config = tomllib.loads(provisioning.render_codex_base_config(project))
        self.assertIn(project, config["projects"])
        self.assertIn(str(home), config["projects"])
        server = _worker_overrides(odd, project, db)
        self.assertEqual(server["args"], ["--actor-id", odd, "--db", db, "--home", str(home)])
        self.assertEqual(server["env"]["AGENT_COMMS_ACTOR_ID"], odd)
        self.assertEqual(server["env"]["AGENT_COMMS_PROJECT_ROOT"], project)
        self.assertEqual(server["hook"][server["hook"].index("--project-root") + 1], project)

    def test_claude_mcp_config_round_trips_quotes_and_backslashes(self) -> None:
        from agent_comms.adapters import DispatchContext
        from agent_comms.spawn import render_spawn

        home = self.odd_home()
        project = str(Path(self.tmp.name).resolve() / 'p"r\\j')
        db = str(Path(self.tmp.name).resolve() / 'l"g.sqlite')
        args = render_spawn("claude", "w")["args"]
        template = args[args.index("--mcp-config") + 1]
        context = DispatchContext(
            dispatch={"dispatch_id": "dispatch_20260101_000000_00000000", "policy_name": policies.WORKER_DISPATCH_POLICY},
            message={"id": "m"},
            recipient={"id": "w", "project_root": project, "runtime": "fake"},
            ttl_seconds=60,
            expected_close_by="2026-01-01T00:00:00+00:00",
            db_path=db,
        )
        policy = policies.compile_policy(policies.WORKER_DISPATCH_POLICY)
        (flag, rendered) = FakeAdapter()._resolved_spawn_args(context, {"args": ["--mcp-config", template]}, policy)
        server = json.loads(rendered)["mcpServers"]["agent-comms"]
        self.assertEqual(server["args"], ["--db", db, "--actor-id", "w"])
        self.assertEqual(server["env"]["AGENT_COMMS_HOME"], str(home))
        self.assertEqual(server["env"]["AGENT_COMMS_PROJECT_ROOT"], project)
        # Outside an --mcp-config value a placeholder is filled verbatim.
        self.assertEqual(FakeAdapter()._format_arg("{project_root}", context), project)


class UpgradeCompatibilityTest(unittest.TestCase):
    """A server loaded before ``{agent_comms_home}`` existed cannot format the
    new Claude spawn block, so it must refuse dispatch and ask for a reconnect
    rather than pass the freshness check (INTEGRATION-002 F1)."""

    # The placeholders a loaded adapter must know to format each runtime's
    # rendered spawn block. Adding one is a contract change: bump
    # code_identity.CONTRACT_VERSION and record the new set here.
    PLACEHOLDERS_BY_CONTRACT = {
        24: {
            "actor_id", "agent_comms_home", "claude_binary", "claude_settings", "codex_home",
            "db_path", "mcp_command", "message_id", "project_root", "python", "worker_prompt",
        },
        # 25 adds no placeholder: the adapter renders the Codex -c binding.
        25: {
            "actor_id", "agent_comms_home", "claude_binary", "claude_settings", "codex_home",
            "db_path", "mcp_command", "message_id", "project_root", "python", "worker_prompt",
        },
        # 26 adds {codex_binary}: Codex workers run the custody copy.
        26: {
            "actor_id", "agent_comms_home", "claude_binary", "claude_settings", "codex_binary", "codex_home",
            "db_path", "mcp_command", "message_id", "project_root", "python", "worker_prompt",
        },
        # 27 adds none; Codex drops --profile {actor_id} (Claude still uses
        # {actor_id}), and a v26 server would refuse the profile-less home.
        27: {
            "actor_id", "agent_comms_home", "claude_binary", "claude_settings", "codex_binary", "codex_home",
            "db_path", "mcp_command", "message_id", "project_root", "python", "worker_prompt",
        },
        # 28 adds none; the adapter renders the Codex worker's policy hook
        # per run, and a v27 server would dispatch Codex workers without it.
        28: {
            "actor_id", "agent_comms_home", "claude_binary", "claude_settings", "codex_binary", "codex_home",
            "db_path", "mcp_command", "message_id", "project_root", "python", "worker_prompt",
        },
        # 29 adds none; a worker that exited on its own before closing settles
        # as worker_exited_before_close even when reconcile first sees it past
        # its TTL, where a v28 server would label it timeout.
        29: {
            "actor_id", "agent_comms_home", "claude_binary", "claude_settings", "codex_binary", "codex_home",
            "db_path", "mcp_command", "message_id", "project_root", "python", "worker_prompt",
        },
        # 30 adds none; Claude workers gain --verbose, which Claude 2.1.300
        # requires with stream-json, and a v29 server would launch without it.
        30: {
            "actor_id", "agent_comms_home", "claude_binary", "claude_settings", "codex_binary", "codex_home",
            "db_path", "mcp_command", "message_id", "project_root", "python", "worker_prompt",
        },
    }

    def test_spawn_placeholders_are_recorded_for_this_contract(self) -> None:
        import string

        from agent_comms import code_identity
        from agent_comms.spawn import ALLOWED_RUNTIMES, render_spawn

        found = set()
        for runtime in ALLOWED_RUNTIMES:
            spawn = render_spawn(runtime, "w")
            texts = [spawn["command"], *spawn.get("args", []), *spawn.get("env", {}).values()]
            for text in texts:
                found |= {name for _, name, _, _ in string.Formatter().parse(str(text)) if name}
        self.assertEqual(found, self.PLACEHOLDERS_BY_CONTRACT[code_identity.CONTRACT_VERSION])

    def test_a_server_loaded_at_contract_27_must_reconnect(self) -> None:
        # A v27 server would launch Codex workers without the policy hook.
        from agent_comms import code_identity

        loaded_surface = {"agent_comms/adapters/codex.py": "before-policy-hook"}
        with mock.patch.object(code_identity, "LOADED_CODE_IDENTITY", code_identity._identity_from_surface(loaded_surface)), \
                mock.patch.object(code_identity, "LOADED_SURFACE", loaded_surface), \
                mock.patch.object(code_identity, "LOADED_CONTRACT_VERSION", 27):
            with self.assertRaises(code_identity.StaleModuleError) as raised:
                code_identity.require_fresh_module()
        self.assertIn(f"v27 -> v{code_identity.CONTRACT_VERSION}", str(raised.exception))

    def test_a_server_loaded_at_contract_26_must_reconnect(self) -> None:
        # A v26 server requires the per-actor profile a v27 home lacks, and
        # renders --profile; it must refuse and reconnect.
        from agent_comms import code_identity

        loaded_surface = {"agent_comms/spawn.py": "before-profile-retired"}
        with mock.patch.object(code_identity, "LOADED_CODE_IDENTITY", code_identity._identity_from_surface(loaded_surface)), \
                mock.patch.object(code_identity, "LOADED_SURFACE", loaded_surface), \
                mock.patch.object(code_identity, "LOADED_CONTRACT_VERSION", 26):
            with self.assertRaises(code_identity.StaleModuleError) as raised:
                code_identity.require_fresh_module()
        self.assertIn(f"v26 -> v{code_identity.CONTRACT_VERSION}", str(raised.exception))

    def test_a_server_loaded_at_contract_25_must_reconnect(self) -> None:
        # A v25 server cannot format {codex_binary}; it must refuse, not
        # dispatch the PATH codex.
        from agent_comms import code_identity

        loaded_surface = {"agent_comms/spawn.py": "before-codex-custody"}
        with mock.patch.object(code_identity, "LOADED_CODE_IDENTITY", code_identity._identity_from_surface(loaded_surface)), \
                mock.patch.object(code_identity, "LOADED_SURFACE", loaded_surface), \
                mock.patch.object(code_identity, "LOADED_CONTRACT_VERSION", 25):
            with self.assertRaises(code_identity.StaleModuleError) as raised:
                code_identity.require_fresh_module()
        self.assertIn(f"v25 -> v{code_identity.CONTRACT_VERSION}", str(raised.exception))

    def test_a_server_loaded_at_contract_23_must_reconnect(self) -> None:
        from agent_comms import code_identity

        loaded_surface = {"agent_comms/spawn.py": "before-agent-comms-home"}
        with mock.patch.object(code_identity, "LOADED_CODE_IDENTITY", code_identity._identity_from_surface(loaded_surface)), \
                mock.patch.object(code_identity, "LOADED_SURFACE", loaded_surface), \
                mock.patch.object(code_identity, "LOADED_CONTRACT_VERSION", 23):
            with self.assertRaises(code_identity.StaleModuleError) as raised:
                code_identity.require_fresh_module()
        self.assertIn(f"v23 -> v{code_identity.CONTRACT_VERSION}", str(raised.exception))
        self.assertIn("reconnect the MCP server", str(raised.exception))


class InstallBesideAnotherTest(_Home):
    """Setup, a real fake dispatch and the Codex paths under a set home write
    nothing under ``HOME/.agent-comms``, even when that install is populated."""

    def setUp(self) -> None:
        super().setUp()
        self.other = Path.home() / ".agent-comms"
        self.other.mkdir(exist_ok=True)
        self.addCleanup(lambda: [p.unlink() for p in (self.other / "actors.json", self.other / "admin-token") if p.exists()])
        (self.other / "actors.json").write_text('{"actors": {}}\n')
        (self.other / "admin-token").write_text("other-install\n")
        self.before = tree(self.other)
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "codex"
        stub.write_text("#!/bin/sh\necho 'codex-cli 0.157.0'\n")
        stub.chmod(0o755)
        env = mock.patch.dict(
            os.environ,
            {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                mcp_clients.CLAUDE_CONFIG_ENV: str(Path(self.tmp.name) / "claude.json"),
                "CODEX_HOME": str(Path(self.tmp.name) / "codex"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.wait_for_workers)

    @staticmethod
    def wait_for_workers() -> None:
        registry = supervisor.reaper_registry()
        deadline = time.monotonic() + 30
        while registry.pending() and time.monotonic() < deadline:
            registry.reap_ready()
            time.sleep(0.1)

    def cli(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = cli.run(list(argv))
            except SystemExit as exc:
                rc = int(exc.code or 0)
        text = out.getvalue().strip()
        return rc, json.loads(text) if text else {}

    def test_second_install_leaves_the_first_untouched(self) -> None:
        from agent_comms import custody

        # Setup would copy the real Codex bundle into custody; not here.
        with mock.patch.object(custody, "locate_native_bundle", side_effect=custody.CustodyError("no codex in this test")):
            rc, result = self.cli(
                "setup", "--project-root", str(self.project), "--team", "dog", "--runtimes", "fake,codex",
                "--clients", "none", "--human-id", HUMAN, "--yes",
            )
        self.assertIn(rc, (0, 3), result)
        self.assertEqual(result["runtime_root"], str(self.home))
        self.assertEqual(result["registry"], str(self.home / "actors.json"))
        rc, result = self.cli("demo")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["final"]["status"], "closed")
        self.cli("provision-codex-home", "--actor-id", "dog-codex-worker", "--project-root", str(self.project))
        self.cli("refresh-codex-auth")
        rc, report = self.cli("doctor")
        self.assertEqual(report["runtime_root"], str(self.home))
        self.assertEqual(approval.approval_key_path(argparse.Namespace(key=None)), self.home / "approval-key")

        self.assertEqual(tree(self.other), self.before)
        written = tree(self.home)
        for expected in ("actors.json", "agent-comms.sqlite", "logs/dispatch", "codex-homes/default/dog-codex-worker"):
            self.assertIn(expected, written)


if __name__ == "__main__":
    unittest.main()
