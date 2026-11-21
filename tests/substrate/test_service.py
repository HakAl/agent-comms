import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_comms import cli, monitor, paths, service
from agent_comms.store import Store

HUMAN = "01M36YTJV9XBW95S6ZWV47C4RG"
UID = 501

# Shaped like real `launchctl print gui/<uid>/<label>` output: the top-level
# keys are followed by nested blocks that reuse the key `state`.
LAUNCHD_RUNNING = """\
gui/501/com.agent-comms.monitor = {
	active count = 1
	path = /opt/example/LaunchAgents/com.agent-comms.monitor.plist
	type = LaunchAgent
	state = running

	program = /opt/tools/bin/agent-comms-monitor
	runs = 1
	pid = 4242
	immediate reason = speculative
	last exit code = (never exited)

	resource coalition = {
		ID = 1201
		type = resource
		state = active
		active count = 1
		name = com.agent-comms.monitor
	}

	jetsam coalition = {
		ID = 1202
		type = jetsam
		state = active
	}

	job state = running
}
"""

LAUNCHD_EXITED = """\
gui/501/com.agent-comms.monitor = {
	active count = 0
	type = LaunchAgent
	state = not running

	program = /opt/tools/bin/agent-comms-monitor
	runs = 3
	last exit code = 1

	resource coalition = {
		ID = 1201
		type = resource
		state = active
	}
}
"""


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


class FakeLaunchctl:
    """A recording launchctl: one job, loaded or not.

    ``linger_prints`` is how many more ``print`` calls still find the job
    after a ``bootout``, which is how the real one behaves."""

    def __init__(self, *, loaded=False, printed=LAUNCHD_RUNNING, linger_prints=0, fail=()):
        self.loaded = loaded
        self.printed = printed
        self.linger_prints = linger_prints
        self.fail = set(fail)
        self.calls: list[list[str]] = []
        self._lingering = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        assert argv[0] == "launchctl", argv
        verb = argv[1]
        if verb in self.fail:
            return completed(argv, 5, stderr=f"{verb}: Input/output error")
        if verb == "print":
            if self.loaded:
                return completed(argv, 0, stdout=self.printed)
            if self._lingering > 0:
                self._lingering -= 1
                return completed(argv, 0, stdout=self.printed)
            return completed(argv, 113, stderr="Could not find service")
        if verb == "bootout":
            self.loaded = False
            self._lingering = self.linger_prints
            return completed(argv)
        if verb == "bootstrap":
            self.loaded = True
            return completed(argv)
        if verb == "kickstart":
            return completed(argv)
        raise AssertionError(f"unexpected command {argv!r}")

    def verbs(self):
        return [call[1] for call in self.calls]


def systemd_show(**values):
    base = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "MainPID": "4242",
        "ExecMainStatus": "0",
        "Result": "success",
        "UnitFileState": "enabled",
    }
    base.update(values)
    return "".join(f"{key}={value}\n" for key, value in base.items())


class FakeSystemctl:
    def __init__(self, *, show=None, linger="yes", fail=(), cat_rc=0):
        self.show = systemd_show() if show is None else show
        self.linger = linger
        self.fail = set(fail)
        self.cat_rc = cat_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "loginctl":
            if self.linger is None:
                return completed(argv, 1, stderr="Failed to get user")
            return completed(argv, 0, stdout=f"Linger={self.linger}\n")
        assert argv[:2] == ["systemctl", "--user"], argv
        verb = argv[2]
        if verb in self.fail:
            return completed(argv, 1, stderr=f"Failed to {verb} unit")
        if verb == "show":
            return completed(argv, 0, stdout=self.show)
        if verb == "cat":
            return completed(argv, self.cat_rc)
        return completed(argv)

    def systemctl_calls(self):
        return [call[2:] for call in self.calls if call[0] == "systemctl"]


def parse_systemd_words(text: str, *, command: bool, literal_program: bool = False) -> list[str]:
    """Read one unit value back the way systemd does: quoted words, C-style
    escapes, ``%%`` for a percent sign and, in a command line, ``$$`` for a
    dollar sign. Written independently of the serializer under test."""
    words: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == " ":
            index += 1
            continue
        if text[index] != '"':
            raise AssertionError(f"unquoted word at {index} in {text!r}")
        index += 1
        word = []
        while text[index] != '"':
            char = text[index]
            if char == "\\":
                word.append(text[index + 1])
                index += 2
            elif char == "%":
                if text[index + 1] != "%":
                    raise AssertionError(f"live specifier in {text!r}")
                word.append("%")
                index += 2
            elif char == "$" and command and not (literal_program and not words):
                if text[index + 1] != "$":
                    raise AssertionError(f"live variable in {text!r}")
                word.append("$")
                index += 2
            else:
                word.append(char)
                index += 1
        index += 1
        words.append("".join(word))
    return words


def parse_unit(text: str) -> dict:
    """``{section: [(key, value), ...]}`` of a rendered unit."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = line.strip("[]")
            sections[current] = []
            continue
        key, _, value = line.partition("=")
        sections[current].append((key, value))
    return sections


def unit_command(text: str) -> list[str]:
    (value,) = [value for key, value in parse_unit(text)["Service"] if key == "ExecStart"]
    # systemd substitutes variables in the arguments only: in the program
    # path a dollar sign is literal (probed on systemd 257).
    return parse_systemd_words(value, command=True, literal_program=True)


def unit_environment(text: str) -> dict[str, str]:
    env = {}
    for key, value in parse_unit(text)["Service"]:
        if key == "Environment":
            (word,) = parse_systemd_words(value, command=False)
            name, _, content = word.partition("=")
            env[name] = content
    return env


class _Scratch(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(os.path.realpath(scratch.name))
        self.program = self.root / "bin" / "agent-comms-monitor"
        self.program.parent.mkdir()
        self.program.write_text("#!/bin/sh\n")
        self.program.chmod(0o755)
        self.units = self.root / "units"

    def spec(self, **changes) -> service.ServiceSpec:
        runtime_root = changes.pop("runtime_root", self.root / "runtime")
        values = {
            "runtime_root": runtime_root,
            "default_root": False,
            "db_path": runtime_root / "agent-comms.sqlite",
            "db_explicit": False,
            "human_actor_id": HUMAN,
            "program": self.program,
            "path_env": "/usr/local/bin:/usr/bin:/bin",
        }
        values.update(changes)
        return service.ServiceSpec(**values)

    def launchd(self, runner, suffix="") -> service.LaunchdBackend:
        self.sleeps: list[float] = []
        return service.LaunchdBackend(suffix, run=runner, agents_dir=self.units, uid=UID, sleep=self.sleeps.append)

    def systemd(self, runner, suffix="") -> service.SystemdBackend:
        return service.SystemdBackend(suffix, run=runner, unit_dir=self.units, user="alice")

    def heartbeat(self, db_path: Path) -> int:
        """A fresh heartbeat in a real ledger; returns the pid it names."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return Store(db_path).upsert_monitor_heartbeat(interval_seconds=5.0, monitor_version="test")["pid"]


class SpecTest(_Scratch):
    def test_default_install_passes_no_db_binds_the_root_and_clears_the_ledger_variable(self) -> None:
        spec = self.spec(default_root=True)
        self.assertEqual(
            spec.argv(),
            [str(self.program), "--human-actor-id", HUMAN, "--log-file", str(self.root / "runtime" / "logs" / "monitor.log")],
        )
        env = spec.environment()
        # The root is written even for the default install: the manager's
        # HOME need not be the installing shell's.
        self.assertEqual(env["AGENT_COMMS_HOME"], str(self.root / "runtime"))
        self.assertEqual(env["AGENT_COMMS_DB"], "")
        self.assertEqual(env["PATH"], "/usr/local/bin:/usr/bin:/bin")
        self.assertNotIn("LANG", env)

    def test_own_root_and_explicit_ledger_ride_on_the_root_variable_and_the_command_line(self) -> None:
        ledger = self.root / "elsewhere" / "ledger.sqlite"
        spec = self.spec(db_path=ledger, db_explicit=True, lang="en_US.UTF-8")
        self.assertEqual(spec.argv()[:3], [str(self.program), "--db", str(ledger)])
        env = spec.environment()
        self.assertEqual(env["AGENT_COMMS_HOME"], str(self.root / "runtime"))
        self.assertEqual(env["AGENT_COMMS_DB"], "")
        self.assertEqual(env["LANG"], "en_US.UTF-8")

    def test_every_override_the_monitor_reads_is_written_set_or_empty(self) -> None:
        spec = self.spec(resolver_env={"AGENT_COMMS_SUPERVISOR_ROOT": "/short/run"})
        env = spec.environment()
        self.assertEqual(env["AGENT_COMMS_SUPERVISOR_ROOT"], "/short/run")
        for name in ("AGENT_COMMS_CODEX_CUSTODY_ROOT", "AGENT_COMMS_DISPATCH_LOG_DIR"):
            self.assertEqual(env[name], "", name)
        # Overrides that select another pin manifest or custody copy are test
        # switches; a service always runs the shipped pins.
        for name in service.CLEARED_ENV:
            self.assertEqual(env[name], "", name)
        self.assertIn("AGENT_COMMS_RUNTIME_PINS", service.CLEARED_ENV)
        self.assertIn("AGENT_COMMS_CODEX_VERSIONS_DIR", service.CLEARED_ENV)
        self.assertIn("AGENT_COMMS_CLAUDE_VERSIONS_DIR", service.CLEARED_ENV)

    def test_the_unit_carries_no_credential_variable(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_COMMS_ADMIN_TOKEN": "x", "AGENT_COMMS_HOME": str(self.root / "runtime")}):
            spec = service.spec_from_environment(
                human_actor_id=HUMAN, db_path=self.root / "runtime" / "agent-comms.sqlite", db_explicit=False, program=self.program
            )
        self.assertNotIn("AGENT_COMMS_ADMIN_TOKEN", spec.environment())
        self.assertFalse([name for name in spec.environment() if "TOKEN" in name or "KEY" in name])

    def test_suffix_is_empty_for_the_default_root_and_stable_per_other_root(self) -> None:
        self.assertEqual(service.suffix_for(Path("/a/b"), default_root=True), "")
        one = service.suffix_for(Path("/a/b"), default_root=False)
        self.assertEqual(one, service.suffix_for(Path("/a/b"), default_root=False))
        self.assertNotEqual(one, service.suffix_for(Path("/a/c"), default_root=False))
        self.assertRegex(one, r"\A[0-9a-f]{10}\Z")

    def test_refuses_control_characters_and_line_breaks(self) -> None:
        for changes in (
            {"human_actor_id": "abc\ndef"},
            {"runtime_root": self.root / "run\ttime"},
            {"path_env": "/usr/bin:/opt/a\x1bb"},
            {"lang": "C\r"},
        ):
            with self.subTest(changes=changes), self.assertRaises(service.ServiceError) as raised:
                self.spec(**changes).validate()
            self.assertIn("control character", str(raised.exception))

    def test_refuses_a_path_entry_that_is_relative_or_empty(self) -> None:
        for value in ("", "bin:/usr/bin", "/usr/bin::/bin", "/usr/bin:", ":/usr/bin", "/usr/bin:."):
            with self.subTest(value=value), self.assertRaises(service.ServiceError) as raised:
                self.spec(path_env=value).validate()
            self.assertIn("PATH", str(raised.exception))

    def test_refuses_relative_paths_for_the_program_root_and_ledger(self) -> None:
        for changes in ({"program": Path("bin/monitor")}, {"runtime_root": Path("runtime")}, {"db_path": Path("x.sqlite")}):
            with self.subTest(changes=changes), self.assertRaises(service.ServiceError):
                self.spec(**changes).validate()

    def test_unsupported_platform_is_refused_with_one_message(self) -> None:
        with self.assertRaises(service.ServiceError) as raised:
            service.backend_for_platform("win32")
        self.assertIn("win32", str(raised.exception))
        self.assertIn("agent-comms-monitor", str(raised.exception))
        self.assertEqual(service.backend_for_platform("darwin"), service.LAUNCHD)
        self.assertEqual(service.backend_for_platform("linux"), service.SYSTEMD)

    def test_make_backend_follows_the_platform(self) -> None:
        self.assertIsInstance(service.make_backend("", platform="darwin"), service.LaunchdBackend)
        self.assertIsInstance(service.make_backend("", platform="linux"), service.SystemdBackend)
        with self.assertRaises(service.ServiceError):
            service.make_backend("", platform="sunos5")


class SpecFromEnvironmentTest(_Scratch):
    def build(self, environ: dict, **kwargs) -> service.ServiceSpec:
        base = {"PATH": "/usr/bin:/bin"}
        base.update(environ)
        with mock.patch.dict(os.environ, base):
            for name in ("AGENT_COMMS_HOME", "LANG", *service.RESOLVER_ENV):
                if name not in base:
                    os.environ.pop(name, None)
            defaults = {"human_actor_id": HUMAN, "db_path": paths.db_path(), "db_explicit": False, "program": self.program}
            defaults.update(kwargs)
            return service.spec_from_environment(**defaults)

    def test_default_root_is_decided_from_the_resolved_root(self) -> None:
        spec = self.build({})
        self.assertTrue(spec.default_root)
        self.assertEqual(spec.runtime_root, Path.home() / ".agent-comms")
        self.assertEqual(spec.suffix, "")
        self.assertEqual(spec.db_path, Path.home() / ".agent-comms" / "agent-comms.sqlite")

    def test_a_variable_that_spells_out_the_default_root_is_still_the_default_install(self) -> None:
        default = Path.home() / ".agent-comms"
        for spelled in (str(default), str(default) + "/", str(default.parent / "x" / ".." / default.name)):
            with self.subTest(spelled=spelled):
                spec = self.build({"AGENT_COMMS_HOME": spelled})
                self.assertTrue(spec.default_root)
                self.assertEqual(spec.suffix, "")
                self.assertEqual(spec.environment()["AGENT_COMMS_HOME"], str(spec.runtime_root))
                self.assertEqual(spec.runtime_root, default)
        self.assertTrue(service.is_default_root(default))
        self.assertFalse(service.is_default_root(default.parent / (default.name + "-dev")))

    def test_own_root_gets_a_suffix_and_carries_the_installers_overrides(self) -> None:
        own = self.root / "own"
        spec = self.build(
            {"AGENT_COMMS_HOME": str(own), "LANG": "C.UTF-8", "AGENT_COMMS_DISPATCH_LOG_DIR": str(self.root / "dlogs")}
        )
        self.assertFalse(spec.default_root)
        self.assertEqual(spec.runtime_root, own)
        self.assertEqual(spec.suffix, service.suffix_for(own, default_root=False))
        self.assertEqual(spec.lang, "C.UTF-8")
        self.assertEqual(spec.environment()["AGENT_COMMS_DISPATCH_LOG_DIR"], str(self.root / "dlogs"))
        self.assertEqual(spec.path_env, "/usr/bin:/bin")

    def test_a_relative_override_is_refused(self) -> None:
        with self.assertRaises(service.ServiceError) as raised:
            self.build({"AGENT_COMMS_SUPERVISOR_ROOT": "run/s"})
        self.assertIn("AGENT_COMMS_SUPERVISOR_ROOT", str(raised.exception))

    def test_a_relative_path_entry_in_the_shell_is_refused(self) -> None:
        with self.assertRaises(service.ServiceError):
            self.build({"PATH": "/usr/bin:node_modules/.bin"})

    def test_the_program_defaults_to_the_installed_monitor_command(self) -> None:
        with mock.patch.object(paths, "console_script", return_value=self.program) as console_script:
            spec = self.build({}, program=None)
        console_script.assert_called_once_with("agent-comms-monitor")
        self.assertEqual(spec.program, self.program)


class RenderTest(_Scratch):
    def test_plist_parses_back_to_the_command_environment_and_policy(self) -> None:
        spec = self.spec(lang="en_US.UTF-8")
        document = plistlib.loads(service.render_launchd_plist(spec, "com.agent-comms.monitor.abc"))
        self.assertEqual(document["Label"], "com.agent-comms.monitor.abc")
        self.assertEqual(document["ProgramArguments"], spec.argv())
        self.assertEqual(document["EnvironmentVariables"], spec.environment())
        self.assertIs(document["RunAtLoad"], True)
        # Restarted after a crash, left alone after a clean exit.
        self.assertEqual(document["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(document["ThrottleInterval"], 10)
        self.assertEqual(document["ProcessType"], "Background")
        # Workers already running are not killed with the job.
        self.assertIs(document["AbandonProcessGroup"], True)

    def test_unit_parses_back_to_the_command_environment_and_policy(self) -> None:
        spec = self.spec(lang="en_US.UTF-8")
        text = service.render_systemd_unit(spec)
        self.assertEqual(unit_command(text), spec.argv())
        self.assertEqual(unit_environment(text), spec.environment())
        sections = parse_unit(text)
        settings = dict(pair for pair in sections["Service"] if pair[0] not in ("ExecStart", "Environment"))
        self.assertEqual(settings["Restart"], "on-failure")
        self.assertEqual(settings["RestartSec"], "5")
        self.assertEqual(settings["Type"], "simple")
        # Only the monitor's own process is signalled on stop.
        self.assertEqual(settings["KillMode"], "process")
        self.assertEqual(sections["Install"], [("WantedBy", "default.target")])

    def awkward_spec(self) -> service.ServiceSpec:
        awkward = self.root / 'a b%c$d"e\\f'
        return self.spec(
            runtime_root=awkward / "runtime",
            program=awkward / "bin" / "agent-comms-monitor",
            path_env=f"{awkward}/bin:/usr/bin",
            db_path=awkward / "ledger %h $HOME.sqlite",
            db_explicit=True,
        )

    def test_systemd_values_with_space_quote_backslash_percent_and_dollar_read_back_literally(self) -> None:
        spec = self.awkward_spec()
        text = service.render_systemd_unit(spec)
        self.assertEqual(unit_command(text), spec.argv())
        self.assertEqual(unit_environment(text), spec.environment())
        (command,) = [value for key, value in parse_unit(text)["Service"] if key == "ExecStart"]
        environment = [value for key, value in parse_unit(text)["Service"] if key == "Environment"]
        program, _, arguments = command.partition('" "')
        self.assertIn("a b%%c$d\\\"e\\\\f", program)
        self.assertNotIn("$$", program)
        self.assertIn("a b%%c$$d\\\"e\\\\f", arguments)
        self.assertNotRegex(arguments, r"(?<!\$)\$(?!\$)")
        # A dollar sign is literal in Environment= and must not be doubled there.
        home = next(value for value in environment if value.startswith('"AGENT_COMMS_HOME='))
        self.assertIn("a b%%c$d\\\"e\\\\f", home)
        self.assertNotIn("$$", home)
        for line in text.splitlines():
            self.assertNotRegex(line, r"(?<!%)%(?!%)", line)

    def test_systemd_description_cannot_carry_a_specifier(self) -> None:
        text = service.render_systemd_unit(self.awkward_spec())
        (description,) = [value for key, value in parse_unit(text)["Unit"] if key == "Description"]
        self.assertIn("%%c", description)

    def test_plist_carries_awkward_values_unchanged(self) -> None:
        spec = self.awkward_spec()
        document = plistlib.loads(service.render_launchd_plist(spec, "x"))
        self.assertEqual(document["ProgramArguments"], spec.argv())
        self.assertEqual(document["EnvironmentVariables"], spec.environment())

    def test_render_refuses_what_validate_refuses(self) -> None:
        bad = self.spec(human_actor_id="a\nb")
        with self.assertRaises(service.ServiceError):
            service.render_systemd_unit(bad)
        with self.assertRaises(service.ServiceError):
            service.render_launchd_plist(bad, "x")

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("plutil"), "plutil is a macOS tool")
    def test_plutil_accepts_the_rendered_plist_syntax(self) -> None:
        path = self.root / "check.plist"
        path.write_bytes(service.render_launchd_plist(self.awkward_spec(), "com.agent-comms.monitor.check"))
        result = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(
        sys.platform.startswith("linux") and shutil.which("systemd-analyze"), "systemd-analyze is not on this host"
    )
    def test_systemd_analyze_accepts_the_rendered_unit(self) -> None:
        path = self.root / "agent-comms-monitor-check.service"
        path.write_text(service.render_systemd_unit(self.spec(program=Path(sys.executable))))
        result = subprocess.run(
            ["systemd-analyze", "verify", "--user", str(path)], capture_output=True, text=True, check=False
        )
        if "Failed to connect" in result.stderr or "No medium found" in result.stderr:
            self.skipTest(f"no systemd user manager to verify against: {result.stderr.strip()}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(path.name, result.stderr, result.stderr)


class TargetBindingTest(_Scratch):
    """A variable the service manager hands the process cannot move the target."""

    def resolved_by_the_monitor(self, argv: list[str], unit_env: dict[str, str], manager_home: Path | None = None) -> dict:
        hostile = {
            **({"HOME": str(manager_home)} if manager_home is not None else {}),
            "AGENT_COMMS_HOME": str(self.root / "someone-elses-root"),
            "AGENT_COMMS_DB": str(self.root / "someone-elses.sqlite"),
            "AGENT_COMMS_DISPATCH_LOG_DIR": str(self.root / "someone-elses-logs"),
            "AGENT_COMMS_CODEX_CUSTODY_ROOT": str(self.root / "someone-elses-custody"),
            "AGENT_COMMS_RUNTIME_PINS": str(self.root / "someone-elses-pins.json"),
            "AGENT_COMMS_CODEX_VERSIONS_DIR": str(self.root / "someone-elses-codex"),
            "AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(self.root / "someone-elses-claude"),
        }
        from agent_comms import runtime_pins

        with mock.patch.dict(os.environ, {**hostile, **unit_env}):
            args = monitor.parse_args(argv[1:])
            return {
                "db": Path(args.db) if args.db is not None else paths.db_path(),
                "db_explicit": args.db is not None or bool(os.environ.get("AGENT_COMMS_DB")),
                "root": paths.runtime_root(),
                "custody": paths.codex_custody_root(),
                "pins": runtime_pins.runtime_pins_path(),
                "codex_versions": runtime_pins.codex_versions_dir(),
                "human": args.human_actor_id,
                "log_file": args.log_file,
            }

    def renderings(self, spec: service.ServiceSpec):
        document = plistlib.loads(service.render_launchd_plist(spec, "x"))
        yield "launchd", document["ProgramArguments"], document["EnvironmentVariables"]
        text = service.render_systemd_unit(spec)
        yield "systemd", unit_command(text), unit_environment(text)

    def test_default_install_resolves_the_default_root_and_ledger(self) -> None:
        default_root = Path.home() / ".agent-comms"
        spec = self.spec(runtime_root=default_root, default_root=True)
        from agent_comms import runtime_pins

        shipped = runtime_pins.runtime_pins_path()
        for name, argv, env in self.renderings(spec):
            with self.subTest(backend=name):
                seen = self.resolved_by_the_monitor(argv, env)
                self.assertEqual(seen["root"], default_root)
                self.assertEqual(seen["db"], default_root / "agent-comms.sqlite")
                # Not passing --db keeps the monitor's default-ledger open path.
                self.assertFalse(seen["db_explicit"])
                self.assertEqual(seen["custody"], default_root / "codex-homes")
                self.assertEqual(seen["pins"], shipped)
                self.assertEqual(seen["codex_versions"], default_root / "runtime-custody" / "codex")
                self.assertEqual(seen["human"], HUMAN)
                self.assertEqual(seen["log_file"], str(default_root / "logs" / "monitor.log"))

    def test_default_install_is_bound_to_the_installers_root_under_another_manager_home(self) -> None:
        # The unit does not set HOME; a systemd user manager supplies its own.
        default_root = Path.home() / paths.runtime_root().name
        spec = self.spec(runtime_root=default_root, default_root=True)
        other_home = self.root / "manager home"
        other_home.mkdir()
        for name, argv, env in self.renderings(spec):
            with self.subTest(backend=name):
                self.assertNotIn("HOME", env)
                seen = self.resolved_by_the_monitor(argv, env, manager_home=other_home)
                self.assertEqual(seen["root"], default_root)
                self.assertEqual(seen["db"], default_root / "agent-comms.sqlite")
                self.assertFalse(seen["db_explicit"])
                self.assertEqual(seen["custody"], default_root / "codex-homes")
                self.assertEqual(seen["log_file"], str(default_root / "logs" / "monitor.log"))

    def test_own_root_install_resolves_its_root_and_ledger(self) -> None:
        own = self.root / "own root"
        spec = self.spec(runtime_root=own)
        for name, argv, env in self.renderings(spec):
            with self.subTest(backend=name):
                seen = self.resolved_by_the_monitor(argv, env)
                self.assertEqual(seen["root"], own)
                self.assertEqual(seen["db"], own / "agent-comms.sqlite")
                self.assertFalse(seen["db_explicit"])
                self.assertEqual(seen["custody"], own / "codex-homes")

    def test_explicit_ledger_install_resolves_that_ledger(self) -> None:
        own = self.root / "own"
        ledger = self.root / "ledgers" / "team.sqlite"
        spec = self.spec(runtime_root=own, db_path=ledger, db_explicit=True)
        for name, argv, env in self.renderings(spec):
            with self.subTest(backend=name):
                seen = self.resolved_by_the_monitor(argv, env)
                self.assertEqual(seen["root"], own)
                self.assertEqual(seen["db"], ledger)
                self.assertTrue(seen["db_explicit"])


class LaunchdBackendTest(_Scratch):
    LABEL = "com.agent-comms.monitor"
    TARGET = f"gui/{UID}/com.agent-comms.monitor"

    def test_label_and_unit_path_carry_the_suffix(self) -> None:
        backend = self.launchd(FakeLaunchctl(), suffix="0123456789")
        self.assertEqual(backend.label, "com.agent-comms.monitor.0123456789")
        self.assertEqual(backend.unit_path, self.units / "com.agent-comms.monitor.0123456789.plist")
        self.assertEqual(self.launchd(FakeLaunchctl()).unit_path, self.units / "com.agent-comms.monitor.plist")

    def test_default_directory_is_the_users_launch_agents(self) -> None:
        backend = service.LaunchdBackend(run=FakeLaunchctl())
        self.assertEqual(backend.agents_dir, Path.home() / "Library" / "LaunchAgents")

    def test_install_writes_a_private_plist_and_bootstraps(self) -> None:
        runner = FakeLaunchctl()
        backend = self.launchd(runner)
        spec = self.spec()
        backend.install(spec)
        self.assertEqual(
            runner.calls,
            [
                ["launchctl", "print", self.TARGET],
                ["launchctl", "bootstrap", f"gui/{UID}", str(backend.unit_path)],
            ],
        )
        self.assertEqual(stat.S_IMODE(backend.unit_path.stat().st_mode), 0o600)
        self.assertEqual(plistlib.loads(backend.unit_path.read_bytes())["ProgramArguments"], spec.argv())
        self.assertEqual([path.name for path in self.units.iterdir()], [backend.unit_path.name])

    def test_install_over_a_loaded_job_boots_it_out_waits_and_bootstraps(self) -> None:
        runner = FakeLaunchctl(loaded=True, linger_prints=2)
        backend = self.launchd(runner)
        backend.unit_path.parent.mkdir(parents=True)
        backend.unit_path.write_bytes(b"old")
        os.chmod(backend.unit_path, 0o644)
        backend.install(self.spec())
        self.assertEqual(runner.verbs(), ["print", "bootout", "print", "print", "print", "bootstrap"])
        self.assertEqual(len(self.sleeps), 2)
        self.assertEqual(stat.S_IMODE(backend.unit_path.stat().st_mode), 0o600)
        self.assertNotEqual(backend.unit_path.read_bytes(), b"old")

    def test_install_is_idempotent(self) -> None:
        runner = FakeLaunchctl()
        backend = self.launchd(runner)
        backend.install(self.spec())
        first = backend.unit_path.read_bytes()
        backend.install(self.spec())
        self.assertEqual(backend.unit_path.read_bytes(), first)
        self.assertEqual(runner.verbs(), ["print", "bootstrap", "print", "bootout", "print", "bootstrap"])

    def test_an_invalid_spec_changes_nothing(self) -> None:
        runner = FakeLaunchctl(loaded=True)
        backend = self.launchd(runner)
        with self.assertRaises(service.ServiceError):
            backend.install(self.spec(path_env="bin"))
        self.assertEqual(runner.calls, [])
        self.assertFalse(backend.unit_path.exists())

    def test_a_job_that_never_unloads_is_reported(self) -> None:
        runner = FakeLaunchctl(loaded=True, linger_prints=1000)
        backend = self.launchd(runner)
        with self.assertRaises(service.ServiceError) as raised:
            backend.stop()
        self.assertIn("still loaded", str(raised.exception))
        self.assertNotIn("bootstrap", runner.verbs())

    def test_a_failed_query_is_not_taken_for_an_absent_job(self) -> None:
        for runner in (
            FakeLaunchctl(loaded=True, fail={"print"}),
            lambda argv: completed(argv, 127, stderr="launchctl: No such file or directory"),
        ):
            backend = self.launchd(runner)
            backend.unit_path.parent.mkdir(parents=True, exist_ok=True)
            backend.unit_path.write_bytes(b"definition")
            for operation in (backend.stop, backend.uninstall, backend.start, backend.state, lambda: backend.install(self.spec())):
                with self.assertRaises(service.ServiceError) as raised:
                    operation()
                self.assertIn("launchctl print", str(raised.exception))
            # Nothing was removed, rewritten or sent on a failed query.
            self.assertEqual(backend.unit_path.read_bytes(), b"definition")
            if isinstance(runner, FakeLaunchctl):
                self.assertEqual(set(runner.verbs()), {"print"})
                self.assertTrue(runner.loaded)

    def test_a_failed_query_while_waiting_for_bootout_is_an_error(self) -> None:
        calls = []

        def runner(argv):
            calls.append(argv[1])
            if argv[1] == "print" and calls.count("print") > 1:
                return completed(argv, 5, stderr="print: Input/output error")
            return completed(argv, 0, stdout=LAUNCHD_RUNNING)

        backend = self.launchd(runner)
        backend.unit_path.parent.mkdir(parents=True)
        backend.unit_path.write_bytes(b"definition")
        with self.assertRaises(service.ServiceError):
            backend.uninstall()
        self.assertEqual(calls, ["print", "bootout", "print"])
        self.assertTrue(backend.unit_path.is_file())

    def test_absence_is_recognized_by_exit_code_or_by_message(self) -> None:
        for response in ((113, ""), (1, 'Could not find service "x" in domain for user gui: 501')):
            backend = self.launchd(lambda argv, response=response: completed(argv, response[0], stderr=response[1]))
            self.assertFalse(backend.state().loaded)

    def test_a_refused_bootstrap_carries_the_managers_words(self) -> None:
        backend = self.launchd(FakeLaunchctl(fail={"bootstrap"}))
        with self.assertRaises(service.ServiceError) as raised:
            backend.install(self.spec())
        self.assertIn("Input/output error", str(raised.exception))
        self.assertIn("launchctl bootstrap", str(raised.exception))

    def test_start_refuses_when_nothing_is_installed(self) -> None:
        runner = FakeLaunchctl()
        with self.assertRaises(service.ServiceError) as raised:
            self.launchd(runner).start()
        self.assertIn("agent-comms service install", str(raised.exception))
        self.assertEqual(runner.calls, [])

    def test_start_after_stop_bootstraps_the_unloaded_definition_then_kickstarts(self) -> None:
        runner = FakeLaunchctl()
        backend = self.launchd(runner)
        backend.install(self.spec())
        backend.stop()
        self.assertTrue(backend.unit_path.is_file())
        runner.calls.clear()
        backend.start()
        self.assertEqual(
            runner.calls,
            [
                ["launchctl", "print", self.TARGET],
                ["launchctl", "bootstrap", f"gui/{UID}", str(backend.unit_path)],
                ["launchctl", "kickstart", self.TARGET],
            ],
        )

    def test_start_of_a_loaded_job_kickstarts_it(self) -> None:
        runner = FakeLaunchctl()
        backend = self.launchd(runner)
        backend.install(self.spec())
        runner.calls.clear()
        backend.start()
        self.assertEqual(runner.calls, [["launchctl", "print", self.TARGET], ["launchctl", "kickstart", self.TARGET]])

    def test_stop_boots_the_job_out_and_keeps_the_file(self) -> None:
        runner = FakeLaunchctl()
        backend = self.launchd(runner)
        backend.install(self.spec())
        runner.calls.clear()
        backend.stop()
        self.assertEqual(
            runner.calls,
            [
                ["launchctl", "print", self.TARGET],
                ["launchctl", "bootout", self.TARGET],
                ["launchctl", "print", self.TARGET],
            ],
        )
        self.assertTrue(backend.unit_path.is_file())

    def test_stop_of_an_unloaded_job_is_quiet(self) -> None:
        runner = FakeLaunchctl()
        self.launchd(runner).stop()
        self.assertEqual(runner.verbs(), ["print"])

    def test_uninstall_leaves_no_file_and_no_loaded_job(self) -> None:
        runner = FakeLaunchctl()
        backend = self.launchd(runner)
        backend.install(self.spec())
        runner.calls.clear()
        backend.uninstall()
        self.assertEqual(runner.verbs(), ["print", "bootout", "print"])
        self.assertFalse(runner.loaded)
        self.assertEqual(list(self.units.iterdir()), [])
        backend.uninstall()

    def test_state_reads_the_top_level_keys_of_a_running_job(self) -> None:
        backend = self.launchd(FakeLaunchctl(loaded=True, printed=LAUNCHD_RUNNING))
        backend.unit_path.parent.mkdir(parents=True)
        backend.unit_path.write_bytes(b"x")
        state = backend.state()
        self.assertTrue(state.installed)
        self.assertTrue(state.loaded)
        self.assertTrue(state.running)
        self.assertEqual(state.pid, 4242)
        self.assertEqual(state.detail, "running")

    def test_state_of_an_exited_job_names_the_exit_code_and_no_pid(self) -> None:
        state = self.launchd(FakeLaunchctl(loaded=True, printed=LAUNCHD_EXITED)).state()
        self.assertFalse(state.installed)
        self.assertTrue(state.loaded)
        self.assertFalse(state.running)
        self.assertIsNone(state.pid)
        self.assertEqual(state.last_exit, "1")
        self.assertEqual(state.detail, "not running")

    def test_state_of_an_unloaded_job(self) -> None:
        state = self.launchd(FakeLaunchctl()).state()
        self.assertFalse(state.installed)
        self.assertFalse(state.loaded)
        self.assertFalse(state.running)
        self.assertIsNone(state.pid)


class SystemdBackendTest(_Scratch):
    UNIT = "agent-comms-monitor.service"

    def test_unit_name_and_path_carry_the_suffix(self) -> None:
        backend = self.systemd(FakeSystemctl(), suffix="0123456789")
        self.assertEqual(backend.unit, "agent-comms-monitor-0123456789.service")
        self.assertEqual(backend.unit_path, self.units / "agent-comms-monitor-0123456789.service")

    def test_default_directory_honours_an_absolute_xdg_config_home(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}):
            self.assertEqual(service.SystemdBackend(run=FakeSystemctl()).unit_dir, self.root / "xdg" / "systemd" / "user")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative"}):
            self.assertEqual(
                service.SystemdBackend(run=FakeSystemctl()).unit_dir, Path.home() / ".config" / "systemd" / "user"
            )

    def test_install_writes_a_private_unit_reloads_enables_and_restarts(self) -> None:
        runner = FakeSystemctl()
        backend = self.systemd(runner)
        spec = self.spec()
        backend.install(spec)
        self.assertEqual(runner.systemctl_calls(), [["daemon-reload"], ["enable", self.UNIT], ["restart", self.UNIT]])
        self.assertEqual(stat.S_IMODE(backend.unit_path.stat().st_mode), 0o600)
        self.assertEqual(unit_command(backend.unit_path.read_text()), spec.argv())
        self.assertEqual([path.name for path in self.units.iterdir()], [self.UNIT])

    def test_install_is_idempotent(self) -> None:
        runner = FakeSystemctl()
        backend = self.systemd(runner)
        backend.install(self.spec())
        first = backend.unit_path.read_bytes()
        backend.install(self.spec())
        self.assertEqual(backend.unit_path.read_bytes(), first)
        self.assertEqual(runner.systemctl_calls()[3:], [["daemon-reload"], ["enable", self.UNIT], ["restart", self.UNIT]])

    def test_an_invalid_spec_changes_nothing(self) -> None:
        runner = FakeSystemctl()
        backend = self.systemd(runner)
        with self.assertRaises(service.ServiceError):
            backend.install(self.spec(human_actor_id="a\nb"))
        self.assertEqual(runner.calls, [])
        self.assertFalse(backend.unit_path.exists())

    def test_a_refused_command_carries_the_managers_words(self) -> None:
        backend = self.systemd(FakeSystemctl(fail={"enable"}))
        with self.assertRaises(service.ServiceError) as raised:
            backend.install(self.spec())
        self.assertIn("systemctl --user enable", str(raised.exception))
        self.assertIn("Failed to enable unit", str(raised.exception))

    def test_start_refuses_when_nothing_is_installed(self) -> None:
        runner = FakeSystemctl()
        with self.assertRaises(service.ServiceError) as raised:
            self.systemd(runner).start()
        self.assertIn("agent-comms service install", str(raised.exception))
        self.assertEqual(runner.calls, [])

    def test_start_and_stop_are_one_command_each(self) -> None:
        runner = FakeSystemctl()
        backend = self.systemd(runner)
        backend.install(self.spec())
        runner.calls.clear()
        backend.stop()
        backend.start()
        self.assertEqual(runner.systemctl_calls(), [["stop", self.UNIT], ["start", self.UNIT]])
        self.assertTrue(backend.unit_path.is_file())

    def test_uninstall_disables_stops_removes_and_reloads(self) -> None:
        runner = FakeSystemctl()
        backend = self.systemd(runner)
        backend.install(self.spec())
        runner.calls.clear()
        backend.uninstall()
        self.assertEqual(runner.systemctl_calls(), [["disable", "--now", self.UNIT], ["daemon-reload"]])
        self.assertEqual(list(self.units.iterdir()), [])

    def test_a_failed_disable_keeps_the_definition_and_is_reported(self) -> None:
        # `disable --now` stops the unit only after the unit-file operation
        # succeeds; reporting success here could leave the monitor running.
        runner = FakeSystemctl(fail={"disable"})
        backend = self.systemd(runner)
        backend.unit_path.parent.mkdir(parents=True)
        backend.unit_path.write_bytes(b"definition")
        with self.assertRaises(service.ServiceError) as raised:
            backend.uninstall()
        self.assertIn("Failed to disable unit", str(raised.exception))
        self.assertEqual(runner.systemctl_calls(), [["disable", "--now", self.UNIT]])
        self.assertEqual(backend.unit_path.read_bytes(), b"definition")
        with mock.patch.object(service, "read_ledger_view", return_value=service.LedgerView(exists=False)):
            with self.assertRaises(service.ServiceError):
                service.uninstall(backend, self.root / "l.sqlite")

    def test_a_failed_stop_is_reported(self) -> None:
        backend = self.systemd(FakeSystemctl(fail={"stop"}))
        with self.assertRaises(service.ServiceError):
            service.stop(backend, self.root / "l.sqlite")

    GONE = {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "MainPID": "0"}

    def test_uninstall_with_nothing_installed_and_nothing_running_only_reloads(self) -> None:
        runner = FakeSystemctl(cat_rc=1, show=systemd_show(**self.GONE))
        self.systemd(runner).uninstall()
        self.systemd(runner).uninstall()
        self.assertEqual(
            runner.systemctl_calls()[:3],
            [["cat", self.UNIT], ["show", self.UNIT, "--property=ActiveState"], ["daemon-reload"]],
        )
        self.assertNotIn(["stop", self.UNIT], runner.systemctl_calls())

    def test_uninstall_stops_a_running_unit_whose_definition_is_gone(self) -> None:
        # The file was removed by hand: `cat` fails, the manager still runs it.
        for active in ("active", "activating", "deactivating", "reloading"):
            runner = FakeSystemctl(cat_rc=1, show=systemd_show(ActiveState=active))
            self.systemd(runner).uninstall()
            self.assertEqual(
                runner.systemctl_calls(),
                [["cat", self.UNIT], ["show", self.UNIT, "--property=ActiveState"], ["stop", self.UNIT], ["daemon-reload"]],
                active,
            )

    def test_uninstall_without_a_definition_reports_a_failed_stop_or_query(self) -> None:
        for runner in (
            FakeSystemctl(cat_rc=1, fail={"stop"}),
            FakeSystemctl(cat_rc=1, fail={"show"}),
            FakeSystemctl(cat_rc=1, show=""),
        ):
            with self.assertRaises(service.ServiceError):
                self.systemd(runner).uninstall()
            self.assertNotIn(["daemon-reload"], runner.systemctl_calls())

    def state(self, **kwargs) -> service.ManagerState:
        return self.systemd(FakeSystemctl(**kwargs)).state()

    def test_state_of_a_running_unit(self) -> None:
        runner = FakeSystemctl()
        backend = self.systemd(runner)
        state = backend.state()
        self.assertTrue(state.loaded)
        self.assertTrue(state.running)
        self.assertEqual(state.pid, 4242)
        self.assertIs(state.enabled, True)
        self.assertIs(state.linger, True)
        self.assertIsNone(state.last_exit)
        self.assertEqual(state.detail, "active/running")
        self.assertEqual(runner.calls[0][:4], ["systemctl", "--user", "show", self.UNIT])
        self.assertEqual(runner.calls[1], ["loginctl", "show-user", "alice", "--property=Linger"])

    def test_state_of_a_failed_unit_names_the_result_and_status(self) -> None:
        state = self.state(
            show=systemd_show(ActiveState="failed", SubState="failed", MainPID="0", ExecMainStatus="2", Result="exit-code")
        )
        self.assertFalse(state.running)
        self.assertIsNone(state.pid)
        self.assertEqual(state.last_exit, "exit-code (status 2)")
        self.assertEqual(state.detail, "failed/failed")

    def test_state_reports_a_hit_start_limit(self) -> None:
        state = self.state(
            show=systemd_show(ActiveState="failed", SubState="failed", MainPID="0", ExecMainStatus="1", Result="start-limit-hit")
        )
        self.assertIn("start-limit-hit", state.last_exit)
        self.assertTrue(state.start_limit_hit)
        self.assertFalse(self.state().start_limit_hit)

    def test_state_of_a_stopped_unit_has_no_last_exit(self) -> None:
        state = self.state(show=systemd_show(ActiveState="inactive", SubState="dead", MainPID="0", UnitFileState="disabled"))
        self.assertFalse(state.running)
        self.assertIsNone(state.last_exit)
        self.assertIs(state.enabled, False)

    def test_state_of_an_unknown_unit(self) -> None:
        state = self.state(
            show=systemd_show(LoadState="not-found", ActiveState="inactive", SubState="dead", MainPID="0", UnitFileState="")
        )
        self.assertFalse(state.installed)
        self.assertFalse(state.loaded)
        self.assertFalse(state.running)

    def test_linger_is_off_or_unknown(self) -> None:
        self.assertIs(self.state(linger="no").linger, False)
        self.assertIsNone(self.state(linger=None).linger)

    def test_linger_command_names_the_user(self) -> None:
        self.assertEqual(self.systemd(FakeSystemctl()).linger_command(), "loginctl enable-linger alice")


class RunCommandTest(unittest.TestCase):
    def test_a_missing_tool_is_a_failed_command_not_a_traceback(self) -> None:
        result = service.run_command(["agent-comms-no-such-service-manager-tool", "print"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("agent-comms-no-such-service-manager-tool", result.stderr)

    def test_output_is_captured_as_text(self) -> None:
        result = service.run_command([sys.executable, "-c", "print('hello')"])
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "hello"))


class LedgerViewTest(_Scratch):
    def test_a_missing_ledger_is_reported_and_not_created(self) -> None:
        ledger = self.root / "none" / "agent-comms.sqlite"
        view = service.read_ledger_view(ledger)
        self.assertFalse(view.exists)
        self.assertIsNone(view.heartbeat)
        self.assertFalse(ledger.parent.exists())

    def test_reads_the_heartbeat_without_writing(self) -> None:
        ledger = self.root / "runtime" / "agent-comms.sqlite"
        pid = self.heartbeat(ledger)
        before = ledger.read_bytes()
        view = service.read_ledger_view(ledger)
        self.assertTrue(view.exists)
        self.assertEqual(view.heartbeat["pid"], pid)
        self.assertEqual(view.in_flight, 0)
        self.assertIsNone(view.error)
        self.assertEqual(ledger.read_bytes(), before)

    def test_a_file_that_is_not_a_ledger_is_an_error_not_a_crash(self) -> None:
        ledger = self.root / "junk.sqlite"
        ledger.write_bytes(b"this is not a database, and it is longer than one sqlite header" * 4)
        view = service.read_ledger_view(ledger)
        self.assertTrue(view.exists)
        self.assertIsNotNone(view.error)

    def test_foreign_monitor_needs_a_fresh_heartbeat_a_live_pid_and_another_owner(self) -> None:
        now = datetime.now(timezone.utc)
        fresh = service.LedgerView(exists=True, heartbeat={"last_pass_at": now.isoformat(), "pid": 77})
        stale = service.LedgerView(
            exists=True, heartbeat={"last_pass_at": (now - timedelta(hours=1)).isoformat(), "pid": 77}
        )

        def alive(_pid):
            return True

        def dead(_pid):
            return False

        self.assertEqual(service.foreign_monitor_pid(fresh, None, alive=alive, now=now), 77)
        self.assertEqual(service.foreign_monitor_pid(fresh, 12, alive=alive, now=now), 77)
        # The service's own monitor is not foreign.
        self.assertIsNone(service.foreign_monitor_pid(fresh, 77, alive=alive, now=now))
        # A fresh heartbeat left by a process that is gone is nobody's.
        self.assertIsNone(service.foreign_monitor_pid(fresh, None, alive=dead, now=now))
        self.assertIsNone(service.foreign_monitor_pid(stale, None, alive=alive, now=now))
        self.assertIsNone(service.foreign_monitor_pid(service.LedgerView(exists=False), None, alive=alive, now=now))

    def test_workers_note_counts_dispatches_in_flight(self) -> None:
        self.assertIsNone(service.workers_note(service.LedgerView(exists=True)))
        one = service.workers_note(service.LedgerView(exists=True, in_flight=1))
        self.assertIn("1 dispatch is in flight", one)
        self.assertIn("not stopped with the monitor", one)
        self.assertIn("3 dispatches are in flight", service.workers_note(service.LedgerView(exists=True, in_flight=3)))


class OperationsTest(_Scratch):
    def test_install_refuses_while_a_monitor_outside_the_service_is_alive(self) -> None:
        spec = self.spec()
        pid = self.heartbeat(spec.db_path)
        for backend, runner in ((self.launchd(r := FakeLaunchctl()), r), (self.systemd(s := FakeSystemctl(
            show=systemd_show(LoadState="not-found", ActiveState="inactive", SubState="dead", MainPID="0")
        )), s)):
            with self.subTest(backend=backend.name):
                with self.assertRaises(service.ServiceError) as raised:
                    service.install(spec, backend, alive=lambda _pid: True)
                self.assertIn(f"pid {pid}", str(raised.exception))
                self.assertIn("two monitors", str(raised.exception))
                self.assertFalse(backend.unit_path.exists())
                changing = [call for call in runner.calls if not {"print", "show", "show-user"} & set(call)]
                self.assertEqual(changing, [])

    def test_a_fresh_heartbeat_from_a_dead_pid_does_not_block_install(self) -> None:
        spec = self.spec()
        self.heartbeat(spec.db_path)
        backend = self.launchd(FakeLaunchctl())
        result = service.install(spec, backend, alive=lambda _pid: False)
        self.assertTrue(result["ok"])
        self.assertTrue(backend.unit_path.is_file())

    def test_reinstall_over_the_services_own_monitor_is_not_refused(self) -> None:
        spec = self.spec()
        pid = self.heartbeat(spec.db_path)
        printed = LAUNCHD_RUNNING.replace("pid = 4242", f"pid = {pid}")
        runner = FakeLaunchctl(loaded=True, printed=printed)
        backend = self.launchd(runner)
        result = service.install(spec, backend, alive=lambda _pid: True)
        self.assertTrue(result["ok"])
        self.assertIn("bootstrap", runner.verbs())

    def test_install_reports_the_target_the_command_and_the_environment(self) -> None:
        spec = self.spec()
        backend = self.launchd(FakeLaunchctl())
        result = service.install(spec, backend)
        self.assertEqual(result["action"], "install")
        self.assertEqual(result["manager"], "launchd")
        self.assertEqual(result["unit"], str(backend.unit_path))
        self.assertEqual(result["command"], spec.argv())
        self.assertEqual(result["environment"], spec.environment())
        self.assertEqual(result["runtime_root"], str(spec.runtime_root))
        self.assertEqual(result["ledger"], str(spec.db_path))
        self.assertIs(result["ledger_explicit"], False)
        self.assertEqual(result["log_file"], str(spec.log_file))
        self.assertTrue(spec.log_file.parent.is_dir())
        self.assertTrue(any("PATH" in note for note in result["notes"]))
        self.assertFalse(any("linger" in note for note in result["notes"]))

    def test_systemd_install_prints_the_linger_command_only_when_lingering_is_off(self) -> None:
        off = service.install(self.spec(), self.systemd(FakeSystemctl(linger="no")))
        self.assertTrue(any("loginctl enable-linger alice" in note for note in off["notes"]))
        runner = FakeSystemctl(linger="yes")
        on = service.install(self.spec(), self.systemd(runner))
        self.assertFalse(any("linger" in note for note in on["notes"]))
        # agent-comms reports lingering; it never turns it on.
        self.assertFalse([call for call in runner.calls if "enable-linger" in call])

    def test_stop_and_uninstall_say_that_running_workers_are_left_alone(self) -> None:
        view = service.LedgerView(exists=True, in_flight=2)
        with mock.patch.object(service, "read_ledger_view", return_value=view):
            stopped = service.stop(self.launchd(FakeLaunchctl(loaded=True)), self.root / "l.sqlite")
            removed = service.uninstall(self.systemd(FakeSystemctl()), self.root / "l.sqlite")
        for result in (stopped, removed):
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["notes"]), 1)
            self.assertIn("2 dispatches are in flight", result["notes"][0])
        self.assertEqual(stopped["action"], "stop")
        self.assertEqual(removed["action"], "uninstall")

    def test_stop_with_nothing_in_flight_has_no_note(self) -> None:
        self.assertEqual(service.stop(self.systemd(FakeSystemctl()), self.root / "missing.sqlite")["notes"], [])

    def test_start_reports_the_unit(self) -> None:
        backend = self.systemd(FakeSystemctl())
        backend.install(self.spec())
        self.assertEqual(
            service.start(backend),
            {"ok": True, "action": "start", "manager": "systemd", "unit": str(backend.unit_path)},
        )

    def status(self, backend, spec, **kwargs) -> dict:
        return service.status(
            backend, runtime_root=spec.runtime_root, db_path=spec.db_path, db_explicit=spec.db_explicit, **kwargs
        )

    def test_status_of_nothing_installed(self) -> None:
        spec = self.spec()
        result = self.status(self.launchd(FakeLaunchctl()), spec)
        self.assertFalse(result["ok"])
        self.assertFalse(result["installed"])
        self.assertIn("agent-comms service install", result["problems"][0])
        self.assertEqual(result["heartbeat"], {"last_pass_at": None, "pid": None, "fresh": False, "from_service": False})
        self.assertEqual(result["ledger"], str(spec.db_path))

    def test_status_of_a_running_service_with_its_own_fresh_heartbeat_is_ok(self) -> None:
        spec = self.spec()
        pid = self.heartbeat(spec.db_path)
        for backend in (
            self.launchd(FakeLaunchctl(loaded=True, printed=LAUNCHD_RUNNING.replace("pid = 4242", f"pid = {pid}"))),
            self.systemd(FakeSystemctl(show=systemd_show(MainPID=str(pid)))),
        ):
            with self.subTest(backend=backend.name):
                backend.unit_path.parent.mkdir(parents=True, exist_ok=True)
                backend.unit_path.write_bytes(b"x")
                result = self.status(backend, spec, alive=lambda _pid: True)
                self.assertEqual(result["problems"], [])
                self.assertTrue(result["ok"])
                self.assertTrue(result["running"])
                self.assertEqual(result["pid"], pid)
                self.assertTrue(result["heartbeat"]["fresh"])
                self.assertTrue(result["heartbeat"]["from_service"])

    def test_status_of_an_exited_service_names_the_exit_and_the_start_command(self) -> None:
        spec = self.spec()
        backend = self.launchd(FakeLaunchctl(loaded=True, printed=LAUNCHD_EXITED))
        backend.unit_path.parent.mkdir(parents=True)
        backend.unit_path.write_bytes(b"x")
        result = self.status(backend, spec)
        self.assertFalse(result["ok"])
        self.assertEqual(result["last_exit"], "1")
        self.assertIn("last exit: 1", result["problems"][0])
        self.assertIn("agent-comms service start", result["problems"][0])

    def test_status_of_a_running_service_without_a_heartbeat_yet(self) -> None:
        spec = self.spec()
        backend = self.systemd(FakeSystemctl())
        backend.unit_path.parent.mkdir(parents=True)
        backend.unit_path.write_bytes(b"x")
        result = self.status(backend, spec)
        self.assertFalse(result["ok"])
        self.assertIn("no fresh heartbeat", result["problems"][0])

    def test_status_says_plainly_when_the_heartbeat_is_another_monitors(self) -> None:
        spec = self.spec()
        pid = self.heartbeat(spec.db_path)
        backend = self.systemd(FakeSystemctl(show=systemd_show(MainPID="4242")))
        backend.unit_path.parent.mkdir(parents=True)
        backend.unit_path.write_bytes(b"x")
        result = self.status(backend, spec, alive=lambda _pid: True)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["problems"]), 1)
        self.assertIn(f"a monitor outside the service is running on this ledger (pid {pid})", result["problems"][0])
        self.assertIn("not the service's pid 4242", result["problems"][0])
        self.assertFalse(result["heartbeat"]["from_service"])

    def test_status_does_not_call_a_dead_predecessors_heartbeat_another_monitor(self) -> None:
        # Just after a restart: the service runs as a new pid and the fresh
        # heartbeat is still the one its dead predecessor wrote.
        spec = self.spec()
        pid = self.heartbeat(spec.db_path)
        backend = self.systemd(FakeSystemctl(show=systemd_show(MainPID="4242")))
        backend.unit_path.parent.mkdir(parents=True)
        backend.unit_path.write_bytes(b"x")
        result = self.status(backend, spec, alive=lambda _pid: False)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["problems"]), 1)
        self.assertIn(f"pid {pid}, which is no longer running", result["problems"][0])
        self.assertIn("has not written its own yet", result["problems"][0])
        self.assertNotIn("outside the service", result["problems"][0])
        self.assertNotIn("another monitor", result["problems"][0])

    def test_status_of_a_hit_start_limit_names_the_reset_not_a_bare_start(self) -> None:
        spec = self.spec()
        backend = self.systemd(FakeSystemctl(
            show=systemd_show(ActiveState="failed", SubState="failed", MainPID="0", ExecMainStatus="1", Result="start-limit-hit")
        ))
        backend.unit_path.parent.mkdir(parents=True)
        backend.unit_path.write_bytes(b"x")
        result = self.status(backend, spec)
        self.assertFalse(result["ok"])
        (problem,) = result["problems"]
        self.assertIn("start-limit-hit", problem)
        self.assertIn(
            "systemctl --user reset-failed agent-comms-monitor.service && systemctl --user start agent-comms-monitor.service",
            problem,
        )
        self.assertNotIn("agent-comms service start", problem)
        # An install with its own root: the line names that install's unit,
        # so it works in a shell that does not carry the root variable.
        suffixed = self.systemd(FakeSystemctl(
            show=systemd_show(ActiveState="failed", SubState="failed", MainPID="0", ExecMainStatus="1", Result="start-limit-hit")
        ), suffix="0123456789")
        suffixed.unit_path.write_bytes(b"x")
        (problem,) = self.status(suffixed, spec)["problems"]
        unit = "agent-comms-monitor-0123456789.service"
        self.assertIn(f"systemctl --user reset-failed {unit} && systemctl --user start {unit}", problem)
        self.assertIn(str(spec.runtime_root / "logs" / "monitor.log"), problem)
        self.assertNotIn("; run: agent-comms service start", problem)

    def test_status_names_a_terminal_monitor_when_the_service_is_not_installed(self) -> None:
        spec = self.spec()
        pid = self.heartbeat(spec.db_path)
        result = self.status(self.launchd(FakeLaunchctl()), spec, alive=lambda _pid: True)
        self.assertEqual(len(result["problems"]), 2)
        self.assertIn(f"outside the service is running on this ledger (pid {pid})", result["problems"][1])

    def test_systemd_status_reports_enablement_and_lingering(self) -> None:
        spec = self.spec()
        backend = self.systemd(FakeSystemctl(linger="no"))
        backend.unit_path.parent.mkdir(parents=True)
        backend.unit_path.write_bytes(b"x")
        result = self.status(backend, spec)
        self.assertIs(result["enabled"], True)
        self.assertIs(result["linger"], False)
        self.assertEqual(result["linger_command"], "loginctl enable-linger alice")
        launchd = self.status(self.launchd(FakeLaunchctl()), spec)
        self.assertNotIn("linger", launchd)

    def test_status_reports_an_unreadable_ledger(self) -> None:
        spec = self.spec()
        spec.db_path.parent.mkdir(parents=True)
        spec.db_path.write_bytes(b"not a database at all, padded past one sqlite header size" * 4)
        result = self.status(self.launchd(FakeLaunchctl()), spec)
        self.assertTrue(any("could not be read" in problem for problem in result["problems"]))


class CliTest(_Scratch):
    def run_cli(self, *argv, backend, environ=None) -> tuple[int, dict]:
        out = io.StringIO()
        base = {"AGENT_COMMS_HOME": str(self.root / "runtime"), "PATH": "/usr/bin:/bin"}
        base.update(environ or {})
        with (
            mock.patch.dict(os.environ, base),
            mock.patch.object(service, "make_backend", return_value=backend) as make_backend,
            mock.patch.object(paths, "console_script", return_value=self.program),
            contextlib.redirect_stdout(out),
        ):
            code = cli.run(list(argv))
        self.make_backend = make_backend
        return code, json.loads(out.getvalue())

    def test_install_writes_the_unit_for_this_install_and_touches_no_ledger(self) -> None:
        backend = self.launchd(FakeLaunchctl())
        code, payload = self.run_cli("service", "install", "--human-actor-id", HUMAN, backend=backend)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["unit"], str(backend.unit_path))
        self.assertEqual(payload["runtime_root"], str(self.root / "runtime"))
        self.assertEqual(payload["ledger"], str(self.root / "runtime" / "agent-comms.sqlite"))
        self.assertNotIn("--db", payload["command"])
        self.assertEqual(payload["environment"]["AGENT_COMMS_HOME"], str(self.root / "runtime"))
        self.assertEqual(payload["command"][0], str(self.program))
        self.assertFalse((self.root / "runtime" / "agent-comms.sqlite").exists())
        self.make_backend.assert_called_once_with(service.suffix_for(self.root / "runtime", default_root=False))

    def test_install_with_an_explicit_ledger_passes_it_on_the_command_line(self) -> None:
        ledger = self.root / "ledgers" / "team.sqlite"
        backend = self.systemd(FakeSystemctl())
        code, payload = self.run_cli("--db", str(ledger), "service", "install", "--human-actor-id", HUMAN, backend=backend)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["command"][1:3], ["--db", str(ledger)])
        self.assertIs(payload["ledger_explicit"], True)

    def test_a_ledger_named_by_the_variable_is_explicit_too(self) -> None:
        ledger = self.root / "ledgers" / "env.sqlite"
        backend = self.systemd(FakeSystemctl())
        code, payload = self.run_cli(
            "service", "install", "--human-actor-id", HUMAN, backend=backend, environ={"AGENT_COMMS_DB": str(ledger)}
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["command"][1:3], ["--db", str(ledger)])
        self.assertEqual(payload["environment"]["AGENT_COMMS_DB"], "")

    def test_install_without_the_human_actor_is_refused(self) -> None:
        runner = FakeLaunchctl()
        code, payload = self.run_cli("service", "install", backend=self.launchd(runner))
        self.assertEqual(code, 2)
        self.assertIn("--human-actor-id", payload["error"])
        self.assertEqual(runner.calls, [])

    def test_the_human_actor_belongs_to_install_only(self) -> None:
        runner = FakeLaunchctl()
        code, payload = self.run_cli("service", "stop", "--human-actor-id", HUMAN, backend=self.launchd(runner))
        self.assertEqual(code, 2)
        self.assertIn("install", payload["error"])
        self.assertEqual(runner.calls, [])

    def test_a_refusal_by_the_service_manager_exits_2_with_its_words(self) -> None:
        backend = self.launchd(FakeLaunchctl(fail={"bootstrap"}))
        code, payload = self.run_cli("service", "install", "--human-actor-id", HUMAN, backend=backend)
        self.assertEqual(code, 2)
        self.assertIn("Input/output error", payload["error"])

    def test_start_stop_and_uninstall(self) -> None:
        runner = FakeSystemctl()
        backend = self.systemd(runner)
        self.assertEqual(self.run_cli("service", "install", "--human-actor-id", HUMAN, backend=backend)[0], 0)
        runner.calls.clear()
        code, stopped = self.run_cli("service", "stop", backend=backend)
        self.assertEqual((code, stopped["action"]), (0, "stop"))
        code, started = self.run_cli("service", "start", backend=backend)
        self.assertEqual((code, started["action"]), (0, "start"))
        code, removed = self.run_cli("service", "uninstall", backend=backend)
        self.assertEqual((code, removed["action"]), (0, "uninstall"))
        unit = "agent-comms-monitor.service"
        self.assertEqual(
            runner.systemctl_calls(),
            [["stop", unit], ["start", unit], ["disable", "--now", unit], ["daemon-reload"]],
        )
        self.assertFalse(backend.unit_path.exists())

    def test_status_exits_3_with_the_report_when_not_running_and_0_when_healthy(self) -> None:
        code, payload = self.run_cli("service", "status", backend=self.launchd(FakeLaunchctl()))
        self.assertEqual(code, 3)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "status")

        pid = self.heartbeat(self.root / "runtime" / "agent-comms.sqlite")
        self.assertEqual(pid, os.getpid())
        backend = self.systemd(FakeSystemctl(show=systemd_show(MainPID=str(pid))))
        backend.unit_path.parent.mkdir(parents=True, exist_ok=True)
        backend.unit_path.write_bytes(b"x")
        code, payload = self.run_cli("service", "status", backend=backend)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])

    def test_an_unsupported_platform_exits_2(self) -> None:
        out = io.StringIO()
        with mock.patch.object(sys, "platform", "win32"), contextlib.redirect_stdout(out):
            code = cli.run(["service", "status"])
        self.assertEqual(code, 2)
        self.assertIn("win32", json.loads(out.getvalue())["error"])
