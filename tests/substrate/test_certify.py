"""Automated certification: cert mode in the isolation guard, the cell
runner's outcome records, and ``scripts/certify.py`` writing a pin only on a
full pass. Hermetic: no runtime login, fake cell reports."""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from agent_comms import supervisor
from tests import isolation
from tests.cells import certify_run

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_certify():
    spec = importlib.util.spec_from_file_location("certify_script", str(REPO_ROOT / "scripts" / "certify.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


certify = _load_certify()

PROBE = textwrap.dedent(
    """
    import json, os
    from pathlib import Path
    import tests
    from agent_comms import supervisor
    print(json.dumps({
        "home": os.environ.get("HOME"),
        "cert_home": os.environ.get("AGENT_COMMS_TEST_CODEX_CERT_HOME"),
        "db": os.environ.get("AGENT_COMMS_DB"),
        "default_control_root": str(supervisor.default_control_root()),
    }))
    """
)


class CertModeIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="certmode-")
        self.addCleanup(self.temp.cleanup)
        self.fake_home = Path(self.temp.name) / "home"
        (self.fake_home / ".agent-comms").mkdir(parents=True)

    def run_probe(self, argv: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.fake_home), "PYTHONDONTWRITEBYTECODE": "1"}
        for name in ("TMPDIR", "TEMP", "TMP"):
            if name in os.environ:
                env[name] = os.environ[name]
        env.update(extra_env)
        code = f"import sys\nsys.argv = {argv}\n" + PROBE
        return subprocess.run(
            [sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False, timeout=120
        )

    def test_cert_mode_stays_isolated_and_keeps_the_test_cert_input(self) -> None:
        cert = str(Path(self.temp.name) / "cert")
        result = self.run_probe(
            '["-m", "tests.cells.test_cell_codex"]',
            **{
                isolation.CERT_ENV: "1",
                "AGENT_COMMS_TEST_CODEX_CERT_HOME": cert,
                "AGENT_COMMS_DB": str(self.fake_home / ".agent-comms" / "agent-comms.sqlite"),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertNotEqual(report["home"], str(self.fake_home))
        self.assertEqual(report["cert_home"], cert)
        self.assertIsNone(report["db"])
        socket_path = Path(report["default_control_root"]) / ("1" * 32) / "s"
        self.assertLessEqual(len(os.fsencode(str(socket_path))), supervisor.SUN_PATH_MAX_BYTES)

    def test_cert_mode_is_refused_without_cells_only_targets(self) -> None:
        for argv in (
            '["-m"]',
            '["-m", "tests.substrate.test_store"]',
            '["unittest", "discover", "-s", "tests", "-t", "."]',
            '["-m", "tests.cells.test_cell_codex", "tests.substrate.test_store"]',
        ):
            with self.subTest(argv=argv):
                result = self.run_probe(argv, **{isolation.CERT_ENV: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(isolation.CERT_ENV, result.stderr)
                self.assertIn("tests/cells", result.stderr)

    def test_scratch_base_is_the_first_existing_candidate_that_fits(self) -> None:
        short = self.temp.name
        too_long = str(Path(short) / ("x" * 80))
        Path(too_long).mkdir()
        missing = str(Path(short) / "missing")
        if isolation._cert_socket_budget(short) > isolation.SUN_PATH_MAX_BYTES:
            self.skipTest(f"system temp {short} is itself too long for this check")
        self.assertEqual(isolation.cert_scratch_base((too_long, missing, short)), short)
        with self.assertRaisesRegex(RuntimeError, "no candidate base fits"):
            isolation.cert_scratch_base((too_long, missing))

    def test_guard_byte_limit_matches_the_supervisor(self) -> None:
        self.assertEqual(isolation.SUN_PATH_MAX_BYTES, supervisor.SUN_PATH_MAX_BYTES)


class _Sample:  # wrapped into a TestCase only inside the test, so discovery skips it
    def test_pass(self) -> None:
        pass

    def test_fail(self) -> None:
        self.fail("boom")

    def test_error(self) -> None:
        raise ValueError("bad")

    def test_skip(self) -> None:
        self.skipTest("no login")

    def test_subtest_fail(self) -> None:
        for value in (1, 2):
            with self.subTest(value=value):
                self.assertEqual(value, 1)

    @unittest.expectedFailure
    def test_expected_failure(self) -> None:
        self.fail("known")


class CertifyRunRecordsTest(unittest.TestCase):
    def test_every_outcome_is_recorded_per_test_id(self) -> None:
        sample = type("Sample", (_Sample, unittest.TestCase), {})
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(sample)
        with open(os.devnull, "w") as sink:
            result = unittest.TextTestRunner(stream=sink, verbosity=2, resultclass=certify_run.RecordingResult).run(suite)
        outcomes = {test_id.rsplit(".", 1)[1]: record["outcome"] for test_id, record in result.records.items()}
        self.assertEqual(
            outcomes,
            {
                "test_pass": "passed",
                "test_fail": "failed",
                "test_error": "error",
                "test_skip": "skipped",
                "test_subtest_fail": "failed",
                "test_expected_failure": "expected_failure",
            },
        )


def report(*tests: tuple[str, str]) -> dict:
    return {"tests": [{"id": test_id, "outcome": outcome, "detail": ""} for test_id, outcome in tests]}


CELLS_PASS = report(
    ("tests.cells.test_cell_codex.CodexCellTest.test_a", "passed"),
    ("tests.cells.test_adapter_codex.CodexAdapterTest.test_b", "passed"),
)
DRIFT_PASS = report((certify.DRIFT_TEST["codex"], "passed"))


class CertifyScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="certify-")
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)
        (self.repo / "agent_comms").mkdir()
        for relpath in (certify.PINS_RELPATH, certify.IDENTITY_RELPATH):
            shutil.copy2(REPO_ROOT / relpath, self.repo / relpath)
        self.pins = self.repo / certify.PINS_RELPATH
        self.identity = self.repo / certify.IDENTITY_RELPATH
        self.before = (self.pins.read_bytes(), self.identity.read_bytes())
        self.platform = "darwin-arm64"
        self.current = json.loads(self.pins.read_text())["runtimes"]["codex"]["platforms"][self.platform]["version"]
        self.calls: list[list[str]] = []

    def runner(self, *reports):
        queue = list(reports)

        def run(targets: list[str]) -> dict:
            self.calls.append(targets)
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        return run

    def certify(self, run_cells, *, installed: str = "9.9.9", force: bool = False, in_sync=lambda: True):
        return certify.certify(
            "codex",
            repo=self.repo,
            platform=self.platform,
            installed=installed,
            today="2026-10-10",
            force=force,
            run_cells=run_cells,
            compute_digest=lambda: "a" * 64,
            digest_in_sync=in_sync,
        )

    def assert_untouched(self) -> None:
        self.assertEqual((self.pins.read_bytes(), self.identity.read_bytes()), self.before)

    def test_full_pass_writes_pin_and_digest_after_the_cells(self) -> None:
        outcome = self.certify(self.runner(CELLS_PASS, DRIFT_PASS))
        self.assertTrue(outcome.certified)
        self.assertEqual(outcome.previous, self.current)
        self.assertEqual(self.calls, [list(certify.CELL_MODULES["codex"]), [certify.DRIFT_TEST["codex"]]])
        entry = json.loads(self.pins.read_text())["runtimes"]["codex"]["platforms"][self.platform]
        self.assertEqual(entry, {"version": "9.9.9", "certified_on": "2026-10-10"})
        self.assertIn('"' + "a" * 64 + '"', self.identity.read_text())
        claude_before = json.loads(self.before[0])["runtimes"]["claude"]
        self.assertEqual(json.loads(self.pins.read_text())["runtimes"]["claude"], claude_before)

    def test_cells_run_before_any_write(self) -> None:
        seen: list[bool] = []

        def run(targets: list[str]) -> dict:
            if targets == list(certify.CELL_MODULES["codex"]):
                seen.append((self.pins.read_bytes(), self.identity.read_bytes()) == self.before)
                return CELLS_PASS
            return DRIFT_PASS

        self.certify(run)
        self.assertEqual(seen, [True])

    def test_any_non_pass_refuses_and_leaves_files_untouched(self) -> None:
        cases = {
            "skip": report(
                ("tests.cells.test_cell_codex.CodexCellTest.test_a", "skipped"),
                ("tests.cells.test_adapter_codex.CodexAdapterTest.test_b", "passed"),
            ),
            "failure": report(
                ("tests.cells.test_cell_codex.CodexCellTest.test_a", "failed"),
                ("tests.cells.test_adapter_codex.CodexAdapterTest.test_b", "passed"),
            ),
            "zero tests": report(),
            "module missing": report(("tests.cells.test_cell_codex.CodexCellTest.test_a", "passed")),
            "import error": report(
                ("tests.cells.test_cell_codex.CodexCellTest.test_a", "passed"),
                ("tests.cells.test_adapter_codex.CodexAdapterTest.test_b", "passed"),
                ("unittest.loader._FailedTest.tests", "error"),
            ),
        }
        for name, cells in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(certify.Refused):
                    self.certify(self.runner(cells))
                self.assert_untouched()

    def test_drift_failure_or_interrupt_after_pinning_restores_both_files(self) -> None:
        for name, drift in {
            "drift failed": report((certify.DRIFT_TEST["codex"], "failed")),
            "drift skipped": report((certify.DRIFT_TEST["codex"], "skipped")),
            "interrupt": KeyboardInterrupt(),
            "sigterm exit": SystemExit(143),
        }.items():
            with self.subTest(case=name):
                expected = certify.Refused if isinstance(drift, dict) else type(drift)
                with self.assertRaises(expected):
                    self.certify(self.runner(CELLS_PASS, drift))
                self.assert_untouched()

    def test_digest_mismatch_after_pinning_restores(self) -> None:
        checks = iter([True, False])
        with self.assertRaisesRegex(certify.Refused, "does not match"):
            self.certify(self.runner(CELLS_PASS, DRIFT_PASS), in_sync=lambda: next(checks))
        self.assert_untouched()

    def test_stale_digest_before_certification_refuses_without_running_cells(self) -> None:
        with self.assertRaisesRegex(certify.Refused, "out of date before certification"):
            self.certify(self.runner(), in_sync=lambda: False)
        self.assertEqual(self.calls, [])
        self.assert_untouched()

    def test_matching_pin_is_a_no_op_unless_forced(self) -> None:
        outcome = self.certify(self.runner(), installed=self.current)
        self.assertTrue(outcome.nothing_to_do)
        self.assertEqual(self.calls, [])
        outcome = self.certify(self.runner(CELLS_PASS, DRIFT_PASS), installed=self.current, force=True)
        self.assertTrue(outcome.certified)

    def test_claude_is_refused_with_the_reason(self) -> None:
        with self.assertRaisesRegex(certify.Refused, "not automated yet"):
            certify.certify(
                "claude", repo=self.repo, platform=self.platform, installed="1.0.0", today="2026-10-10",
                force=False, run_cells=self.runner(), compute_digest=lambda: "a" * 64, digest_in_sync=lambda: True,
            )
        self.assert_untouched()

    def test_pin_rewrite_keeps_the_manifest_layout(self) -> None:
        text = self.before[0].decode()
        same = certify.updated_pins(text, "codex", self.platform, self.current,
                                    json.loads(text)["runtimes"]["codex"]["platforms"][self.platform]["certified_on"])
        self.assertEqual(same, text)


class CertHomeTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="certhome-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve() / "home"
        for relpath in (".agent-comms/codex-cert-home", ".agent-comms/codex-auth", ".codex", ".claude", "elsewhere"):
            (self.home / relpath).mkdir(parents=True)
            (self.home / relpath / "auth.json").write_text("{}")
        (self.home / "auth.json").write_text("{}")
        (self.home / "empty").mkdir()

    def test_dedicated_and_outside_dirs_are_accepted(self) -> None:
        for relpath in (".agent-comms/codex-cert-home", "elsewhere"):
            with self.subTest(relpath=relpath):
                self.assertEqual(certify.check_cert_home(self.home / relpath, self.home), self.home / relpath)

    def test_live_roots_and_missing_login_are_refused(self) -> None:
        for relpath in ("", ".codex", ".claude", ".agent-comms", ".agent-comms/codex-auth", "empty"):
            with self.subTest(relpath=relpath):
                with self.assertRaises(certify.Refused):
                    certify.check_cert_home(self.home / relpath, self.home)

    def test_symlink_into_a_live_root_is_refused(self) -> None:
        link = self.home / "elsewhere-link"
        link.symlink_to(self.home / ".codex")
        with self.assertRaises(certify.Refused):
            certify.check_cert_home(link, self.home)


if __name__ == "__main__":
    unittest.main()
