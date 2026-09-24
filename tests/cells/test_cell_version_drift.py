from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from agent_comms.runtime_pins import claude_binary_path, custody_binary_digest_matches


VERSIONS_PATH = Path(__file__).with_name("cell_versions.json")
SEMVER_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")


def load_manifest() -> dict[str, dict[str, str]]:
    return json.loads(VERSIONS_PATH.read_text())


def runtime_version(runtime: str) -> str:
    command = runtime_command(runtime)
    result = subprocess.run(
        [command, "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
    match = SEMVER_RE.search(output)
    if match is None:
        raise AssertionError(
            f"could not parse {runtime} --version output as X.Y.Z: {output!r}"
        )
    return match.group(1)


def runtime_command(runtime: str) -> str:
    if runtime == "claude":
        return str(claude_binary_path())
    return runtime


def comparable(version: str, boundary: str) -> str:
    if boundary == "exact":
        return version
    if boundary == "minor":
        major, minor, _patch = version.split(".")
        return f"{major}.{minor}"
    raise AssertionError(f"unsupported version boundary {boundary!r}")


class CellVersionDriftTest(unittest.TestCase):
    def assert_runtime_not_drifted(self, runtime: str) -> None:
        command = runtime_command(runtime)
        manifest = load_manifest()
        self.assertIn(runtime, manifest)
        if runtime == "claude":
            if not custody_binary_digest_matches(Path(command), manifest[runtime]["sha256"]):
                self.skipTest(f"{runtime} pinned CLI is absent or does not match recorded sha256 at {command}")
        elif shutil.which(command) is None:
            self.skipTest(f"{runtime} CLI is not installed")

        last_verified = manifest[runtime]["last_verified"]
        boundary = manifest[runtime]["boundary"]
        installed = runtime_version(runtime)

        if comparable(installed, boundary) != comparable(last_verified, boundary):
            self.fail(
                f"VERSION DRIFT: {runtime} installed {installed}, "
                f"last verified {last_verified} -- re-cert required "
                f"(run the gated cell, then bump tests/cells/cell_versions.json)"
            )

    def test_codex_version_has_not_drifted(self) -> None:
        self.assert_runtime_not_drifted("codex")

    def test_claude_version_has_not_drifted(self) -> None:
        self.assert_runtime_not_drifted("claude")


if __name__ == "__main__":
    unittest.main()
