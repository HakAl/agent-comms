"""The per-platform runtime pin manifest and its resolver (ac-4ao.3).

Only one platform is certified here, so the unlisted-platform behaviour is
proven by patching ``current_platform``; no claim is made for any platform
the manifest does not list.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import io
import os
import platform
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from agent_comms import runtime_pins
from agent_comms.runtime_pins import PlatformNotCertified, RuntimePin

PLATFORM_KEY_RE = re.compile(r"^(darwin|linux)-[a-z0-9_]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UNLISTED = "linux-x86_64"
RELEASE_PLATFORM = "darwin-arm64"


def _patched_platform(key: str) -> mock._patch:
    return mock.patch.object(runtime_pins, "current_platform", return_value=key)


class ManifestDataTest(unittest.TestCase):
    """Every listed platform entry is well formed; the release claim is present."""

    def setUp(self) -> None:
        self.manifest = runtime_pins.load_runtime_pins()

    def test_schema_is_current(self) -> None:
        self.assertEqual(self.manifest["schema"], runtime_pins.RUNTIME_PINS_SCHEMA)
        self.assertEqual(set(self.manifest), {"schema", "runtimes"})

    def test_every_runtime_and_platform_entry_is_well_formed(self) -> None:
        runtimes = self.manifest["runtimes"]
        self.assertEqual(set(runtimes), {"codex", "claude"})
        for runtime, entry in runtimes.items():
            with self.subTest(runtime=runtime):
                self.assertEqual(set(entry), {"boundary", "platforms"})
                self.assertIn(entry["boundary"], {"exact", "minor"})
                self.assertTrue(entry["platforms"], f"{runtime} lists no platform")
                for key, data in entry["platforms"].items():
                    with self.subTest(runtime=runtime, platform=key):
                        self.assertRegex(key, PLATFORM_KEY_RE)
                        self.assertRegex(data["version"], SEMVER_RE)
                        date.fromisoformat(data["certified_on"])
                        expected_keys = {"version", "certified_on"}
                        if runtime == "claude":
                            expected_keys.add("sha256")
                            self.assertRegex(data["sha256"], SHA256_RE)
                        self.assertEqual(set(data), expected_keys)

    def test_release_platform_is_certified_for_both_runtimes(self) -> None:
        for runtime in ("codex", "claude"):
            with self.subTest(runtime=runtime):
                self.assertIn(RELEASE_PLATFORM, runtime_pins.certified_platforms(runtime))

    def test_every_listed_platform_resolves_to_a_pin(self) -> None:
        for runtime, entry in self.manifest["runtimes"].items():
            for key, data in entry["platforms"].items():
                with self.subTest(runtime=runtime, platform=key):
                    pin = runtime_pins.pin_for(runtime, key)
                    self.assertEqual(
                        pin,
                        RuntimePin(
                            runtime=runtime,
                            platform=key,
                            version=data["version"],
                            boundary=entry["boundary"],
                            certified_on=data["certified_on"],
                            sha256=data.get("sha256"),
                        ),
                    )

    def test_wrong_schema_fails_at_load_naming_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bad = Path(temp_dir) / "runtime_pins.json"
            bad.write_text('{"codex": {"last_verified": "0.1.0", "boundary": "exact"}}')
            with mock.patch.object(runtime_pins, "RUNTIME_PINS_PATH", bad):
                with self.assertRaisesRegex(ValueError, re.escape(str(bad))) as raised:
                    runtime_pins.load_runtime_pins()
        self.assertIn("schema 2", str(raised.exception))


class CurrentPlatformTest(unittest.TestCase):
    def test_key_is_derived_from_the_host(self) -> None:
        # Not a hardcoded darwin-arm64: the expectation comes from the host,
        # so this holds wherever the suite runs.
        key = runtime_pins.current_platform()
        self.assertEqual(key, f"{sys.platform}-{platform.machine().lower()}")
        self.assertRegex(key, PLATFORM_KEY_RE)


class PinResolverTest(unittest.TestCase):
    def test_unlisted_platform_raises_with_platform_certified_list_and_pointer(self) -> None:
        certified = ", ".join(runtime_pins.certified_platforms("claude"))
        with self.assertRaises(PlatformNotCertified) as raised:
            runtime_pins.pin_for("claude", UNLISTED)
        message = str(raised.exception)
        self.assertIn(f"claude has no certified pin for {UNLISTED}", message)
        self.assertIn(f"(certified: {certified})", message)
        self.assertIn("runtimes.claude.platforms", message)
        self.assertIn("CONTRIBUTING.md, Runtime pins", message)
        self.assertIsInstance(raised.exception, RuntimeError)

    def test_unknown_runtime_is_a_key_error_not_a_platform_error(self) -> None:
        with self.assertRaisesRegex(KeyError, "no runtime 'gemini'"):
            runtime_pins.pin_for("gemini")

    def test_default_platform_is_the_current_one(self) -> None:
        with _patched_platform(UNLISTED):
            with self.assertRaisesRegex(PlatformNotCertified, UNLISTED):
                runtime_pins.pin_for("codex")
        with _patched_platform(RELEASE_PLATFORM):
            self.assertEqual(runtime_pins.pin_for("codex").platform, RELEASE_PLATFORM)

    def test_claude_pin_follows_current_platform(self) -> None:
        with _patched_platform(RELEASE_PLATFORM):
            pin = runtime_pins.claude_pin()
        self.assertEqual(pin.runtime, "claude")
        self.assertEqual(pin.platform, RELEASE_PLATFORM)
        self.assertRegex(pin.sha256, SHA256_RE)
        with _patched_platform(UNLISTED):
            with self.assertRaises(PlatformNotCertified):
                runtime_pins.claude_pin()


class ClaudeConsumersFailClosedTest(unittest.TestCase):
    def test_binary_path_and_sha256_raise_on_unlisted_platform(self) -> None:
        with _patched_platform(UNLISTED), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(runtime_pins.CLAUDE_PINNED_SHA256_ENV, None)
            with self.assertRaisesRegex(PlatformNotCertified, UNLISTED):
                runtime_pins.claude_binary_path()
            with self.assertRaisesRegex(PlatformNotCertified, UNLISTED):
                runtime_pins.claude_pinned_sha256()

    def test_sha256_env_override_wins_without_reading_the_manifest(self) -> None:
        override = "a" * 64
        with _patched_platform(UNLISTED):
            with mock.patch.dict(os.environ, {runtime_pins.CLAUDE_PINNED_SHA256_ENV: override}):
                with mock.patch.object(runtime_pins, "load_runtime_pins") as load:
                    self.assertEqual(runtime_pins.claude_pinned_sha256(), override)
        load.assert_not_called()


class FakeDispatchOnUnlistedPlatformTest(unittest.TestCase):
    def test_fake_dispatch_succeeds_where_no_claude_pin_is_certified(self) -> None:
        # The whole dispatch path, not just the placeholder helper: a fake
        # worker never names {claude_binary}, so the missing Claude pin for
        # this (patched) platform must not reach it.
        from tests.dispatch_cell_harness import make_fake_harness

        with tempfile.TemporaryDirectory() as temp_dir:
            harness = make_fake_harness(Path(temp_dir), cell_delta=True)
            with _patched_platform(UNLISTED):
                dispatch = harness.dispatch_and_wait(
                    idempotency_key="fake-unlisted-platform",
                    ttl_seconds=10,
                    timeout_seconds=45,
                )
            harness.assert_closed_with_parented_reply(dispatch)


class DriftCellSkipTest(unittest.TestCase):
    def test_drift_cell_skips_on_unlisted_platform_naming_it(self) -> None:
        from tests.cells import test_cell_version_drift as drift

        suite = unittest.TestSuite()
        suite.addTest(drift.CellVersionDriftTest("test_codex_version_has_not_drifted"))
        suite.addTest(drift.CellVersionDriftTest("test_claude_version_has_not_drifted"))
        stream = io.StringIO()
        with _patched_platform(UNLISTED):
            result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)

        self.assertEqual(result.testsRun, 2)
        self.assertEqual(result.failures, [])
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.skipped), 2)
        for test, reason in result.skipped:
            with self.subTest(test=test.id()):
                self.assertIn(f"no certified pin for {UNLISTED}", reason)
                self.assertIn("CONTRIBUTING.md, Runtime pins", reason)


class ClaudeCellSkipReasonTest(unittest.TestCase):
    def test_reason_carries_platform_diagnostic_before_any_disk_access(self) -> None:
        from tests import dispatch_cell_harness as harness

        with _patched_platform(UNLISTED):
            with mock.patch.object(harness, "custody_binary_digest_matches") as digest:
                reason = harness.claude_cell_skip_reason()
        digest.assert_not_called()
        self.assertIsInstance(reason, str)
        self.assertIn(f"claude has no certified pin for {UNLISTED}", reason)
        self.assertIn("CONTRIBUTING.md, Runtime pins", reason)

    def test_reason_is_login_text_when_custody_binary_is_absent(self) -> None:
        from tests import dispatch_cell_harness as harness

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(os.environ, {runtime_pins.CLAUDE_VERSIONS_DIR_ENV: temp_dir}):
                reason = harness.claude_cell_skip_reason()
        self.assertEqual(reason, harness.CLAUDE_NOT_LOGGED_IN_REASON)


if __name__ == "__main__":
    unittest.main()
