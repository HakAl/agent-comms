"""``tests/hermetic_pins.py``: a pin table for hermetic tests on an uncertified host.

The uncertified path is exercised here by patching ``current_platform``, so
it is proven on a certified host as well as on Linux.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import paths, runtime_pins
from tests import hermetic_pins, isolation

UNCERTIFIED = "linux-x86_64"
CERTIFIED = "darwin-arm64"
RUNTIMES = ("claude", "codex")


def _patched_platform(key: str) -> mock._patch:
    return mock.patch.object(runtime_pins, "current_platform", return_value=key)


def _manifest_with(platforms_by_runtime: dict[str, dict[str, dict]]) -> dict:
    return {
        "schema": runtime_pins.RUNTIME_PINS_SCHEMA,
        "runtimes": {
            runtime: {"boundary": "exact", "platforms": dict(platforms)}
            for runtime, platforms in platforms_by_runtime.items()
        },
    }


def _entry(version: str) -> dict:
    return {"version": version, "certified_on": "2026-01-01", "sha256": "a" * 64}


class WriteHermeticManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.json"

    def _write_source(self, manifest: dict) -> None:
        self.source.write_text(json.dumps(manifest), encoding="utf-8")

    def test_returns_none_when_both_runtimes_are_certified_for_the_platform(self) -> None:
        self._write_source(
            _manifest_with(
                {
                    "claude": {CERTIFIED: _entry("1.0.0"), UNCERTIFIED: _entry("1.0.1")},
                    "codex": {CERTIFIED: _entry("2.0.0"), UNCERTIFIED: _entry("2.0.1")},
                }
            )
        )
        self.assertIsNone(hermetic_pins.write_hermetic_manifest(UNCERTIFIED, self.source, self.root / "out"))
        self.assertFalse((self.root / "out").exists())

    def test_synthesizes_the_missing_entries_from_the_source_platform(self) -> None:
        self._write_source(
            _manifest_with({"claude": {CERTIFIED: _entry("1.0.0")}, "codex": {CERTIFIED: _entry("2.0.0")}})
        )
        written = hermetic_pins.write_hermetic_manifest(UNCERTIFIED, self.source, self.root / "out")
        self.assertIsNotNone(written)
        self.assertTrue(isolation.is_within(written, self.root / "out"))
        manifest = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], runtime_pins.RUNTIME_PINS_SCHEMA)
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                platforms = manifest["runtimes"][runtime]["platforms"]
                self.assertEqual(platforms[UNCERTIFIED], platforms[CERTIFIED])
        # The source is untouched.
        self.assertNotIn(UNCERTIFIED, json.loads(self.source.read_text())["runtimes"]["claude"]["platforms"])

    def test_a_runtime_already_certified_for_the_platform_is_left_alone(self) -> None:
        self._write_source(
            _manifest_with(
                {
                    "claude": {CERTIFIED: _entry("1.0.0")},
                    "codex": {CERTIFIED: _entry("2.0.0"), UNCERTIFIED: _entry("2.5.0")},
                }
            )
        )
        written = hermetic_pins.write_hermetic_manifest(UNCERTIFIED, self.source, self.root / "out")
        manifest = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(manifest["runtimes"]["codex"]["platforms"][UNCERTIFIED]["version"], "2.5.0")
        self.assertEqual(manifest["runtimes"]["claude"]["platforms"][UNCERTIFIED]["version"], "1.0.0")

    def test_a_runtime_without_a_source_entry_is_left_alone(self) -> None:
        self._write_source(
            _manifest_with({"claude": {CERTIFIED: _entry("1.0.0")}, "codex": {"linux-aarch64": _entry("2.0.0")}})
        )
        written = hermetic_pins.write_hermetic_manifest(UNCERTIFIED, self.source, self.root / "out")
        manifest = json.loads(written.read_text(encoding="utf-8"))
        self.assertNotIn(UNCERTIFIED, manifest["runtimes"]["codex"]["platforms"])
        self.assertIn(UNCERTIFIED, manifest["runtimes"]["claude"]["platforms"])

    def test_the_written_file_loads_through_the_resolver(self) -> None:
        self._write_source(
            _manifest_with({"claude": {CERTIFIED: _entry("1.0.0")}, "codex": {CERTIFIED: _entry("2.0.0")}})
        )
        written = hermetic_pins.write_hermetic_manifest(UNCERTIFIED, self.source, self.root / "out")
        with mock.patch.dict(os.environ, {runtime_pins.RUNTIME_PINS_ENV: str(written)}):
            self.assertEqual(runtime_pins.pin_for("claude", UNCERTIFIED).version, "1.0.0")
            self.assertEqual(runtime_pins.pin_for("codex", UNCERTIFIED).sha256, "a" * 64)


class HermeticPinsEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        hermetic_pins.reset_cache()
        self.addCleanup(hermetic_pins.reset_cache)

    def test_no_op_on_a_certified_host(self) -> None:
        with _patched_platform(CERTIFIED):
            self.assertEqual(hermetic_pins.hermetic_pins_env(), {})

    def test_no_op_in_certification_and_live_home_modes_even_where_uncertified(self) -> None:
        with _patched_platform(UNCERTIFIED):
            with mock.patch.dict(os.environ, {isolation.CERT_ENV: "1"}):
                self.assertEqual(hermetic_pins.hermetic_pins_env(), {})
            with mock.patch.dict(os.environ, {isolation.LIVE_HOME_ENV: "1"}):
                self.assertEqual(hermetic_pins.hermetic_pins_env(), {})

    def test_uncertified_host_gets_a_scratch_manifest_with_the_host_entries(self) -> None:
        with _patched_platform(UNCERTIFIED):
            env = hermetic_pins.hermetic_pins_env()
        self.assertEqual(set(env), {runtime_pins.RUNTIME_PINS_ENV})
        path = Path(env[runtime_pins.RUNTIME_PINS_ENV])
        self.assertTrue(path.is_absolute())
        self.assertTrue(isolation.is_within(path, Path(os.environ["HOME"])), f"{path} is not under the scratch home")
        self.assertFalse(isolation.is_within(path, paths.REPO_ROOT))
        shipped = runtime_pins.load_runtime_pins()
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                platforms = manifest["runtimes"][runtime]["platforms"]
                self.assertEqual(
                    platforms[UNCERTIFIED],
                    shipped["runtimes"][runtime]["platforms"][hermetic_pins.SOURCE_PLATFORM],
                )
                self.assertEqual(
                    {key: value for key, value in platforms.items() if key != UNCERTIFIED},
                    shipped["runtimes"][runtime]["platforms"],
                )
        # Through the resolver, as a helper would see it.
        with mock.patch.dict(os.environ, env), _patched_platform(UNCERTIFIED):
            self.assertEqual(
                runtime_pins.claude_pin().version,
                runtime_pins.pin_for("claude", hermetic_pins.SOURCE_PLATFORM).version,
            )
            self.assertEqual(runtime_pins.codex_pin().platform, UNCERTIFIED)

    def test_the_manifest_is_written_once_per_platform_and_source(self) -> None:
        with _patched_platform(UNCERTIFIED):
            first = hermetic_pins.hermetic_pins_env()
            with mock.patch.object(hermetic_pins, "write_hermetic_manifest", side_effect=AssertionError("rewritten")):
                second = hermetic_pins.hermetic_pins_env()
        self.assertEqual(first, second)
        with _patched_platform("linux-aarch64"):
            third = hermetic_pins.hermetic_pins_env()
        self.assertNotEqual(first, third)

    def test_the_env_reaches_a_child_process(self) -> None:
        # On a certified host the shipped file has no entry for UNCERTIFIED,
        # so a child that resolves that key proves it read the override.
        with _patched_platform(UNCERTIFIED):
            env = hermetic_pins.hermetic_pins_env()
        probe = (
            "import json, tests\n"
            "from agent_comms import runtime_pins\n"
            f"pin = runtime_pins.pin_for('claude', {UNCERTIFIED!r})\n"
            "print(json.dumps({'path': str(runtime_pins.runtime_pins_path()), 'version': pin.version}))\n"
        )
        child_env = {**os.environ, **env}
        pythonpath = str(paths.REPO_ROOT)
        if child_env.get("PYTHONPATH"):
            pythonpath = os.pathsep.join((pythonpath, child_env["PYTHONPATH"]))
        child_env["PYTHONPATH"] = pythonpath
        result = subprocess.run(
            [sys.executable, "-c", probe],
            env=child_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(result.stdout)
        self.assertEqual(seen["path"], env[runtime_pins.RUNTIME_PINS_ENV])
        self.assertEqual(seen["version"], runtime_pins.pin_for("claude", hermetic_pins.SOURCE_PLATFORM).version)


class UseHermeticPinsTest(unittest.TestCase):
    def setUp(self) -> None:
        hermetic_pins.reset_cache()
        self.addCleanup(hermetic_pins.reset_cache)

    def _run(self, *cases: type[unittest.TestCase]) -> unittest.TestResult:
        suite = unittest.TestSuite()
        for case in cases:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
        return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)

    def test_override_is_present_for_the_class_and_gone_afterwards(self) -> None:
        seen: dict[str, object] = {}

        @hermetic_pins.with_hermetic_pins
        class Probe(unittest.TestCase):
            def test_pin(self) -> None:
                seen["env"] = os.environ.get(runtime_pins.RUNTIME_PINS_ENV)
                seen["pin"] = runtime_pins.claude_pin().platform

        class After(unittest.TestCase):
            def test_env(self) -> None:
                seen["after"] = os.environ.get(runtime_pins.RUNTIME_PINS_ENV)

        with _patched_platform(UNCERTIFIED):
            result = self._run(Probe, After)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        self.assertIsNotNone(seen["env"])
        self.assertEqual(seen["pin"], UNCERTIFIED)
        self.assertIsNone(seen["after"])
        self.assertNotIn(runtime_pins.RUNTIME_PINS_ENV, os.environ)

    def test_override_precedes_an_existing_set_up_class(self) -> None:
        seen: dict[str, object] = {}

        class Base(unittest.TestCase):
            @classmethod
            def setUpClass(cls) -> None:
                super().setUpClass()
                seen["in_setup_class"] = runtime_pins.codex_pin().version

            def test_pin(self) -> None:
                seen["in_test"] = runtime_pins.codex_pin().version

        Probe = hermetic_pins.with_hermetic_pins(Base)
        with _patched_platform(UNCERTIFIED):
            result = self._run(Probe)
        self.assertEqual(result.errors, [], result.errors)
        expected = runtime_pins.pin_for("codex", hermetic_pins.SOURCE_PLATFORM).version
        self.assertEqual(seen, {"in_setup_class": expected, "in_test": expected})

    def test_use_hermetic_pins_from_a_set_up_class_body(self) -> None:
        seen: dict[str, object] = {}

        class Probe(unittest.TestCase):
            @classmethod
            def setUpClass(cls) -> None:
                hermetic_pins.use_hermetic_pins(cls)
                seen["in_setup_class"] = os.environ.get(runtime_pins.RUNTIME_PINS_ENV)

            def test_pin(self) -> None:
                seen["in_test"] = runtime_pins.claude_pin().platform

        with _patched_platform(UNCERTIFIED):
            result = self._run(Probe)
        self.assertEqual(result.errors, [], result.errors)
        self.assertIsNotNone(seen["in_setup_class"])
        self.assertEqual(seen["in_test"], UNCERTIFIED)
        self.assertNotIn(runtime_pins.RUNTIME_PINS_ENV, os.environ)

    def test_is_a_no_op_on_a_certified_host(self) -> None:
        seen: dict[str, object] = {}

        @hermetic_pins.with_hermetic_pins
        class Probe(unittest.TestCase):
            def test_env(self) -> None:
                seen["env"] = os.environ.get(runtime_pins.RUNTIME_PINS_ENV)

        with _patched_platform(CERTIFIED):
            result = self._run(Probe)
        self.assertEqual(result.errors, [])
        self.assertIsNone(seen["env"])


if __name__ == "__main__":
    unittest.main()
