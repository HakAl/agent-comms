"""The startup-file fixture the Codex cell relies on is live, and the helpers
that read a worker's command shell say what they claim.

The cell asserts that markers are absent from a worker command's environment.
Absence only means something if the same fixture does export the markers when
a shell loads it, which these tests show without a runtime login.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tests.dispatch_cell_harness as harness

BASH = shutil.which("bash")


def _probe_env(home: Path, **extra: str) -> dict[str, str]:
    # A closed environment: nothing of the host's startup state leaks in.
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), **extra}


def _run_probe(argv_prefix: list[str], home: Path, **extra: str) -> dict:
    result_path = home / "probe-result.json"
    probe = home / "probe.py"
    probe.write_text(harness.startup_probe_source(result_path))
    command = f"{sys.executable} {probe}"
    subprocess.run(
        [*argv_prefix, command],
        env=_probe_env(home, **extra),
        check=True,
        capture_output=True,
        # bash loads .bashrc for a command shell whose stdin is a socket
        # (its remote-shell rule), which a test runner's stdin can be.
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    return json.loads(result_path.read_text())


@unittest.skipUnless(BASH, "no bash on this host")
class StartupFixtureIsLiveTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.env_file = harness.write_operator_startup_fixture(self.home, "the-secret")

    def test_login_bash_loads_the_profile_and_the_secret(self) -> None:
        payload = _run_probe([BASH, "-lc"], self.home, AGENT_COMMS_DISPATCH_ID="d1")
        startup = payload["startup"]
        self.assertEqual(startup[harness.STARTUP_SECRET_NAME], "the-secret")
        self.assertEqual(startup["STARTUP_MARKER_BASH_PROFILE"], "1")
        self.assertEqual(payload["home"], str(self.home))
        self.assertEqual(payload["dispatch_id"], "d1")

    def test_bash_env_file_is_loaded_by_a_plain_command_shell(self) -> None:
        payload = _run_probe([BASH, "-c"], self.home, BASH_ENV=str(self.env_file))
        startup = payload["startup"]
        self.assertEqual(startup[harness.STARTUP_SECRET_NAME], "the-secret")
        self.assertEqual(startup[harness.STARTUP_ENV_FILE_MARKER], "1")

    def test_plain_command_shell_loads_nothing(self) -> None:
        payload = _run_probe([BASH, "-c"], self.home)
        self.assertEqual({k: v for k, v in payload["startup"].items() if v}, {})

    def test_empty_bash_env_names_no_file(self) -> None:
        payload = _run_probe([BASH, "-c"], self.home, BASH_ENV="")
        self.assertEqual({k: v for k, v in payload["startup"].items() if v}, {})

    def test_every_marker_is_reported(self) -> None:
        payload = _run_probe([BASH, "-c"], self.home)
        self.assertEqual(
            set(payload["startup"]),
            {harness.STARTUP_SECRET_NAME, *harness.STARTUP_MARKERS},
        )
        self.assertEqual(len(harness.STARTUP_MARKERS), 7)


class OuterShellFlagsTest(unittest.TestCase):
    def test_reads_the_options_of_the_outer_shell_only(self) -> None:
        cases = {
            "/bin/bash -lc 'cat x'": ("bash", "lc"),
            "/bin/bash -c \"zsh -lic 'true'\"": ("bash", "c"),
            "/bin/zsh -l -i -c 'true'": ("zsh", "lic"),
            "bash -c 'ls -l'": ("bash", "c"),
            "sh -c 'echo -l'": ("sh", "c"),
            "/bin/bash --noprofile -lc x": ("bash", "lc"),
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(harness.outer_shell_flags(command), expected)

    def test_non_shell_and_unparsable_commands_are_none(self) -> None:
        for command in ("ls -l", "python3 -c 'print(1)'", "", "bash -c 'unterminated"):
            with self.subTest(command=command):
                self.assertIsNone(harness.outer_shell_flags(command))


if __name__ == "__main__":
    unittest.main()
