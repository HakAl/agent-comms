"""Automated certification: cert mode in the isolation guard, the cell
runner's outcome records, and ``scripts/certify.py`` writing a pin only on a
full pass. Hermetic: no runtime login, fake cell reports."""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import base64
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

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

    def certify(self, run_cells, *, installed: str = "9.9.9", sha256: str = "b" * 64, force: bool = False, in_sync=lambda: True):
        return certify.certify(
            "codex",
            repo=self.repo,
            platform=self.platform,
            installed=installed,
            sha256=sha256,
            today="2026-10-10",
            force=force,
            run_cells=run_cells,
            compute_digest=lambda: "a" * 64,
            digest_in_sync=in_sync,
        )

    @property
    def journal(self) -> Path:
        return self.repo / certify.JOURNAL_RELPATH

    def assert_untouched(self) -> None:
        self.assertEqual((self.pins.read_bytes(), self.identity.read_bytes()), self.before)
        self.assertFalse(self.journal.exists())

    def test_full_pass_keeps_the_candidate_pin_and_digest(self) -> None:
        outcome = self.certify(self.runner(CELLS_PASS, DRIFT_PASS))
        self.assertTrue(outcome.certified)
        self.assertEqual(outcome.previous, self.current)
        self.assertEqual(self.calls, [list(certify.CELL_MODULES["codex"]), [certify.DRIFT_TEST["codex"]]])
        entry = json.loads(self.pins.read_text())["runtimes"]["codex"]["platforms"][self.platform]
        self.assertEqual(entry, {"version": "9.9.9", "certified_on": "2026-10-10", "sha256": "b" * 64})
        self.assertFalse(self.journal.exists())
        self.assertIn('"' + "a" * 64 + '"', self.identity.read_text())
        claude_before = json.loads(self.before[0])["runtimes"]["claude"]
        self.assertEqual(json.loads(self.pins.read_text())["runtimes"]["claude"], claude_before)

    def test_cells_run_against_the_candidate_pin_with_the_originals_journaled(self) -> None:
        # The adapter checks the pin, so the candidate is on disk while the
        # cells run, and the journal holds what a killed run must restore.
        seen: list[tuple] = []

        def run(targets: list[str]) -> dict:
            if targets == list(certify.CELL_MODULES["codex"]):
                entry = json.loads(self.pins.read_text())["runtimes"]["codex"]["platforms"][self.platform]
                journal = json.loads(self.journal.read_text())
                seen.append((entry["version"], entry["sha256"], {k: base64.b64decode(v) for k, v in journal.items()}))
                return CELLS_PASS
            return DRIFT_PASS

        self.certify(run)
        self.assertEqual(
            seen,
            [("9.9.9", "b" * 64, {certify.PINS_RELPATH: self.before[0], certify.IDENTITY_RELPATH: self.before[1]})],
        )

    def test_a_leftover_journal_is_restored_before_anything_else(self) -> None:
        certify.write_journal(self.repo, {certify.PINS_RELPATH: self.before[0], certify.IDENTITY_RELPATH: self.before[1]})
        self.pins.write_text("{}\n")
        self.identity.write_text("# half-written\n")
        with self.assertRaises(certify.Refused):
            self.certify(self.runner(report()))
        self.assert_untouched()

    def test_a_cell_exception_restores_and_removes_the_journal(self) -> None:
        with self.assertRaises(RuntimeError):
            self.certify(self.runner(RuntimeError("runner broke")))
        self.assert_untouched()

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
        entry = json.loads(self.before[0])["runtimes"]["codex"]["platforms"][self.platform]
        pinned = entry.get("sha256", "c" * 64)
        self.pins.write_text(certify.updated_pins(self.before[0].decode(), "codex", self.platform, self.current, entry["certified_on"], pinned))
        self.before = (self.pins.read_bytes(), self.before[1])
        outcome = self.certify(self.runner(), installed=self.current, sha256=pinned)
        self.assertTrue(outcome.nothing_to_do)
        self.assertEqual(self.calls, [])
        outcome = self.certify(self.runner(CELLS_PASS, DRIFT_PASS), installed=self.current, sha256=pinned, force=True)
        self.assertTrue(outcome.certified)

    def test_same_version_with_another_bundle_digest_is_certified(self) -> None:
        outcome = self.certify(self.runner(CELLS_PASS, DRIFT_PASS), installed=self.current, sha256="d" * 64)
        self.assertTrue(outcome.certified)

    def test_claude_runs_its_own_cells_and_drift_cell_and_pins_claude(self) -> None:
        cells = report(
            ("tests.cells.test_cell_claude.ClaudeCellTest.test_a", "passed"),
            ("tests.cells.test_adapter_claude.ClaudeAdapterTest.test_b", "passed"),
        )
        drift = report((certify.DRIFT_TEST["claude"], "passed"))
        outcome = certify.certify(
            "claude", repo=self.repo, platform=self.platform, installed="9.9.9", sha256="c" * 64, today="2026-10-15",
            force=False, run_cells=self.runner(cells, drift), compute_digest=lambda: "a" * 64, digest_in_sync=lambda: True,
        )
        self.assertTrue(outcome.certified)
        self.assertEqual(self.calls, [list(certify.CELL_MODULES["claude"]), [certify.DRIFT_TEST["claude"]]])
        pins = json.loads(self.pins.read_text())["runtimes"]
        self.assertEqual(
            pins["claude"]["platforms"][self.platform],
            {"version": "9.9.9", "certified_on": "2026-10-15", "sha256": "c" * 64},
        )
        before = json.loads(self.before[0])["runtimes"]
        self.assertEqual(pins["codex"], before["codex"])

    def test_claude_cells_that_skip_restore_the_pin(self) -> None:
        cells = report(
            ("tests.cells.test_cell_claude.ClaudeCellTest.test_a", "skipped"),
            ("tests.cells.test_adapter_claude.ClaudeAdapterTest.test_b", "passed"),
        )
        with self.assertRaisesRegex(certify.Refused, "skipped"):
            certify.certify(
                "claude", repo=self.repo, platform=self.platform, installed="9.9.9", sha256="c" * 64,
                today="2026-10-15", force=False, run_cells=self.runner(cells), compute_digest=lambda: "a" * 64,
                digest_in_sync=lambda: True,
            )
        self.assert_untouched()

    def test_an_unknown_runtime_is_refused(self) -> None:
        with self.assertRaisesRegex(certify.Refused, "unknown runtime"):
            certify.certify(
                "agy", repo=self.repo, platform=self.platform, installed="1.0.0", sha256="", today="2026-10-10",
                force=False, run_cells=self.runner(), compute_digest=lambda: "a" * 64, digest_in_sync=lambda: True,
            )
        self.assert_untouched()

    def test_a_second_run_is_refused_while_another_holds_the_lock(self) -> None:
        # The journal of a live run is not an abandoned one: a second run must
        # not restore it, rebuild cert custody or touch the result file.
        certify.write_journal(self.repo, {certify.PINS_RELPATH: self.before[0], certify.IDENTITY_RELPATH: self.before[1]})
        self.pins.write_text("{}\n")
        custody = self.repo / certify.CERT_CUSTODY_RELPATH
        custody.mkdir(parents=True)
        (custody / "marker").write_text("first run")
        result = self.repo / certify.RECORD_DIR_RELPATH / "last-run.json"
        result.write_text("{}")
        snapshot = (self.journal.read_bytes(), self.pins.read_bytes(), self.identity.read_bytes())
        with certify.run_lock(self.repo), mock.patch.object(certify, "REPO_ROOT", self.repo), \
                mock.patch.object(certify, "installed_bundle", side_effect=AssertionError("ran past the lock")):
            self.assertEqual(certify.main(["--runtime", "codex"]), 1)
        self.assertEqual((self.journal.read_bytes(), self.pins.read_bytes(), self.identity.read_bytes()), snapshot)
        self.assertEqual((custody / "marker").read_text(), "first run")
        self.assertEqual(result.read_text(), "{}")

    def test_the_lock_is_released_when_a_run_ends(self) -> None:
        with mock.patch.object(certify, "REPO_ROOT", self.repo), \
                mock.patch.object(certify, "installed_bundle", side_effect=certify.Refused("no bundle")):
            self.assertEqual(certify.main(["--runtime", "codex"]), 1)
        with certify.run_lock(self.repo):
            with self.assertRaisesRegex(certify.Refused, "another certify run"):
                with certify.run_lock(self.repo):
                    pass

    def durability_events(self):  # type: ignore[no-untyped-def]
        """Record each durability barrier with the journal and pin state at that moment."""
        events: list[tuple[str, str, bool, bytes]] = []

        def recorder(name, real):  # type: ignore[no-untyped-def]
            def call(path, *args):  # type: ignore[no-untyped-def]
                real(path, *args)
                events.append((name, Path(path).name, self.journal.exists(), self.pins.read_bytes()))
            return call

        patches = [
            mock.patch.object(certify, "fsync_file", recorder("file", certify.fsync_file)),
            mock.patch.object(certify, "fsync_dir", recorder("dir", certify.fsync_dir)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return events

    def test_the_journal_is_durable_before_the_pin_changes(self) -> None:
        events = self.durability_events()
        self.certify(self.runner(CELLS_PASS, DRIFT_PASS))
        journal_dir = self.journal.parent.name
        first_dir = next(e for e in events if e[0] == "dir" and e[1] == journal_dir and e[2])
        journal_file = next(e for e in events if e[0] == "file" and e[1].startswith("pending-restore"))
        self.assertEqual(first_dir[3], self.before[0])
        self.assertEqual(journal_file[3], self.before[0])
        self.assertLess(events.index(journal_file), events.index(first_dir))

    def test_kept_or_restored_files_are_durable_before_the_journal_goes(self) -> None:
        for name, runner in {
            "kept": self.runner(CELLS_PASS, DRIFT_PASS),
            "restored": self.runner(report()),
        }.items():
            with self.subTest(case=name):
                events = self.durability_events()
                try:
                    self.certify(runner, force=True)
                except certify.Refused:
                    pass
                synced = {e[1] for e in events if e[0] == "file" and e[2]}
                self.assertLessEqual({self.pins.name, self.identity.name}, synced)
                last = events[-1]
                self.assertEqual((last[0], last[1], last[2]), ("dir", self.journal.parent.name, False))

    def test_a_leftover_journal_restore_is_durable_before_the_journal_goes(self) -> None:
        certify.write_journal(self.repo, {certify.PINS_RELPATH: self.before[0], certify.IDENTITY_RELPATH: self.before[1]})
        self.pins.write_text("{}\n")
        events = self.durability_events()
        self.assertTrue(certify.restore_pending(self.repo))
        synced = {e[1] for e in events if e[0] == "file" and e[2]}
        self.assertLessEqual({self.pins.name, self.identity.name}, synced)
        self.assertEqual(events[-1][:3], ("dir", self.journal.parent.name, False))
        self.assert_untouched()

    def test_macos_barriers_use_full_fsync_and_others_use_fsync(self) -> None:
        # Plain fsync on macOS may leave data in the drive cache and reorder
        # writes across a power loss; F_FULLFSYNC flushes it.
        target = self.repo / "agent_comms"
        for platform, expect_full in (("darwin", True), ("linux", False)):
            with self.subTest(platform=platform):
                full: list[int] = []
                plain: list[int] = []
                with mock.patch.object(certify.sys, "platform", platform), \
                        mock.patch.object(certify.fcntl, "fcntl", side_effect=lambda fd, op: full.append(op) or 0), \
                        mock.patch.object(certify.os, "fsync", side_effect=lambda fd: plain.append(fd)):
                    certify.fsync_file(self.pins)
                    certify.fsync_dir(target)
                if expect_full:
                    self.assertEqual((full, plain), ([certify.fcntl.F_FULLFSYNC] * 2, []))
                else:
                    self.assertEqual((len(full), len(plain)), (0, 2))

    def test_a_failed_barrier_before_pinning_refuses_without_a_journal(self) -> None:
        def fail(fd, op):  # type: ignore[no-untyped-def]
            raise OSError(45, "Operation not supported")

        with mock.patch.object(certify.sys, "platform", "darwin"), mock.patch.object(certify.fcntl, "fcntl", side_effect=fail):
            with self.assertRaisesRegex(certify.Refused, "cannot make .* durable"):
                self.certify(self.runner())
        self.assertEqual(self.calls, [])
        self.assert_untouched()
        self.assertFalse(self.journal.with_suffix(".tmp").exists())

    def test_pin_rewrite_keeps_the_manifest_layout(self) -> None:
        text = self.before[0].decode()
        pin = json.loads(text)["runtimes"]["codex"]["platforms"][self.platform]
        same = certify.updated_pins(text, "codex", self.platform, self.current, pin["certified_on"], pin.get("sha256"))
        self.assertEqual(same, text)


class CertCustodyTest(unittest.TestCase):
    def test_the_named_bundle_is_versioned_and_copied_into_cert_custody(self) -> None:
        from agent_comms.runtime_pins import bundle_sha256, codex_pin
        from tests.codex_custody_support import fake_codex_bundle

        temp = tempfile.TemporaryDirectory(prefix="certcustody-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        fake_codex_bundle(root / "installed")
        source = root / "installed" / codex_pin().version
        bundle, version = certify.installed_bundle(source)
        self.assertEqual((bundle, version), (source.resolve(), codex_pin().version))
        versions, digest = certify.prepare_cert_custody(root, bundle, version)
        self.assertEqual(versions, root / certify.CERT_CUSTODY_RELPATH)
        self.assertEqual(digest, bundle_sha256(source))
        self.assertEqual(bundle_sha256(versions / version), digest)

    def test_a_directory_without_codex_is_refused(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="certcustody-")
        self.addCleanup(temp.cleanup)
        with self.assertRaisesRegex(certify.Refused, "not a Codex bundle"):
            certify.installed_bundle(Path(temp.name))


class NoPendingCertificationTest(unittest.TestCase):
    def test_no_certification_journal_is_left_in_this_checkout(self) -> None:
        # A journal means a certification is running or was killed with a
        # candidate pin on disk; the suite must not pass over it.
        journal = REPO_ROOT / certify.JOURNAL_RELPATH
        self.assertFalse(
            journal.exists(),
            f"{journal} exists: a certification is running or was interrupted; wait for it, "
            "or run make certify RUNTIME=codex (or claude) to restore the pin",
        )


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


def fake_claude(
    path: Path,
    version: str = "9.9.9",
    logged_in: str = "token",
    *,
    without: tuple[int, str] | None = None,
    with_token: tuple[int, str] | None = None,
    env_log: Path | None = None,
) -> Path:
    """A stand-in ``claude``: ``--version`` and ``auth status``.

    ``logged_in`` is ``token`` (logged in only with CLAUDE_CODE_OAUTH_TOKEN
    set), ``always`` (a login reachable without it) or ``never``. The replies
    match claude 2.1.300: logged out exits 1 with authMethod "none", the token
    exits 0 with authMethod "oauth_token". ``without`` and ``with_token``
    replace the reply (exit code, stdout) for the run without or with the
    token. ``env_log`` records HOME and CLAUDE_CONFIG_DIR of ``--version``.
    """
    out = '{"loggedIn": false, "authMethod": "none", "apiProvider": "firstParty"}'
    token_in = '{"loggedIn": true, "authMethod": "oauth_token", "apiProvider": "firstParty"}'
    ambient = '{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty"}'
    replies = {
        "token": ((1, out), (0, token_in)),
        "always": ((0, ambient), (0, token_in)),
        "never": ((1, out), (1, out)),
    }[logged_in]
    (no_code, no_out), (yes_code, yes_out) = without or replies[0], with_token or replies[1]
    log = f'echo "HOME=$HOME CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR" >> {shlex.quote(str(env_log))}; ' if env_log else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then {log}echo "{version} (Claude Code)"; exit 0; fi\n'
        'if [ "$1 $2" = "auth status" ]; then\n'
        f'  if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then printf "%s\\n" {shlex.quote(yes_out)}; exit {yes_code}; fi\n'
        f'  printf "%s\\n" {shlex.quote(no_out)}; exit {no_code}\n'
        "fi\n"
        "exit 2\n"
    )
    path.chmod(0o755)
    return path


class ClaudeCertCustodyTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="claudecustody-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_the_binary_behind_a_link_is_versioned_and_copied_into_cert_custody(self) -> None:
        from agent_comms.runtime_pins import custody_binary_sha256

        real = fake_claude(self.root / "share" / "versions" / "9.9.9")
        link = self.root / "bin" / "claude"
        link.parent.mkdir()
        link.symlink_to(real)
        binary, version = certify.installed_claude(link, probe=self.root / "probe")
        self.assertEqual((binary, version), (real, "9.9.9"))
        versions, digest = certify.prepare_claude_cert_custody(self.root, binary, version)
        self.assertEqual(versions, self.root / certify.CERT_CUSTODY_RELPATH)
        copied = versions / version
        self.assertTrue(copied.is_file())
        self.assertTrue(os.access(copied, os.X_OK))
        self.assertEqual(digest, custody_binary_sha256(real))
        self.assertEqual(custody_binary_sha256(copied), digest)

    def test_claude_on_path_is_found_when_no_binary_is_named(self) -> None:
        real = fake_claude(self.root / "versions" / "9.9.9")
        with mock.patch.object(certify.shutil, "which", return_value=str(real)):
            self.assertEqual(certify.installed_claude(None, probe=self.root / "probe"), (real, "9.9.9"))
        with mock.patch.object(certify.shutil, "which", return_value=None):
            with self.assertRaisesRegex(certify.Refused, "no claude on PATH"):
                certify.installed_claude(None, probe=self.root / "probe")

    def test_a_missing_or_unversioned_binary_is_refused(self) -> None:
        with self.assertRaisesRegex(certify.Refused, "not an executable file"):
            certify.installed_claude(self.root / "missing", probe=self.root / "probe")
        silent = self.root / "silent"
        silent.write_text("#!/bin/sh\necho hello\n")
        silent.chmod(0o755)
        with self.assertRaisesRegex(certify.Refused, "could not read a version"):
            certify.installed_claude(silent, probe=self.root / "probe")


    def test_the_version_probe_runs_under_the_scratch_home(self) -> None:
        # The first native call must not see the caller's HOME or Claude config.
        log = self.root / "env.log"
        real = fake_claude(self.root / "versions" / "9.9.9", env_log=log)
        probe = self.root / "probe"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/live/claude", "HOME": "/live/home"}):
            certify.installed_claude(real, probe=probe)
        self.assertEqual(log.read_text().strip(), f"HOME={probe} CLAUDE_CONFIG_DIR={probe / '.claude'}")
        self.assertFalse(probe.exists())


class ClaudeLoginCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="claudelogin-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cert_home = self.root / "cert"
        self.cert_home.mkdir()
        token = self.cert_home / certify.CLAUDE_TOKEN_NAME
        token.write_text("tok\n")
        token.chmod(0o600)

    def test_the_token_logs_in_and_nothing_else_does(self) -> None:
        binary = fake_claude(self.root / "claude", logged_in="token")
        certify.check_claude_login(binary, self.cert_home, self.root / "probe")
        self.assertFalse((self.root / "probe").exists())

    def test_a_login_reachable_without_the_token_is_refused(self) -> None:
        binary = fake_claude(self.root / "claude", logged_in="always")
        with self.assertRaisesRegex(certify.Refused, "reports a login .*without the cert token"):
            certify.check_claude_login(binary, self.cert_home, self.root / "probe")

    def test_a_token_that_does_not_log_in_is_refused(self) -> None:
        binary = fake_claude(self.root / "claude", logged_in="never")
        with self.assertRaisesRegex(certify.Refused, "setup-token"):
            certify.check_claude_login(binary, self.cert_home, self.root / "probe")

    def test_unusable_negative_evidence_is_refused(self) -> None:
        # A startup error or a reply that is not the logged-out status does not
        # show that the token-free runtime found no login.
        cases = {
            "startup error": (2, "Error: unknown option"),
            "empty object": (1, "{}"),
            "string false": (1, '{"loggedIn": "false", "authMethod": "none"}'),
            "numeric zero": (1, '{"loggedIn": 0, "authMethod": "none"}'),
            "float zero": (1, '{"loggedIn": 0.0, "authMethod": "none"}'),
            "not an object": (1, "[]"),
            "exit 0": (0, '{"loggedIn": false, "authMethod": "none"}'),
        }
        for name, reply in cases.items():
            with self.subTest(name):
                binary = fake_claude(self.root / name.replace(" ", "-"), without=reply)
                with self.assertRaisesRegex(certify.Refused, "without the token .*not the logged-out status"):
                    certify.check_claude_login(binary, self.cert_home, self.root / "probe")

    def test_unusable_positive_evidence_is_refused(self) -> None:
        cases = {
            "string true": (0, '{"loggedIn": "true", "authMethod": "oauth_token"}'),
            "numeric one": (0, '{"loggedIn": 1, "authMethod": "oauth_token"}'),
            "float one": (0, '{"loggedIn": 1.0, "authMethod": "oauth_token"}'),
            "exit 2": (2, '{"loggedIn": true, "authMethod": "oauth_token"}'),
            "keychain login": (0, '{"loggedIn": true, "authMethod": "claude.ai"}'),
            "malformed": (0, "not json"),
        }
        for name, reply in cases.items():
            with self.subTest(name):
                binary = fake_claude(self.root / name.replace(" ", "-"), with_token=reply)
                with self.assertRaisesRegex(certify.Refused, "with the token .*not a token login"):
                    certify.check_claude_login(binary, self.cert_home, self.root / "probe")

    def test_the_probe_env_is_scratch_only(self) -> None:
        env = certify.claude_probe_env(self.root / "probe", "tok")
        self.assertEqual(env["HOME"], str(self.root / "probe"))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.root / "probe" / ".claude"))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok")
        self.assertLessEqual(set(env), {"PATH", "HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "TMPDIR", "LANG"})


class ClaudeCertHomeTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="claudecerthome-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve() / "home"
        for relpath in (".agent-comms/claude-cert-home", ".agent-comms/codex-cert-home", ".claude", "elsewhere", "loose", "blank"):
            (self.home / relpath).mkdir(parents=True)
            token = self.home / relpath / certify.CLAUDE_TOKEN_NAME
            token.write_text("" if relpath == "blank" else "tok\n")
            token.chmod(0o644 if relpath == "loose" else 0o600)
        (self.home / "empty").mkdir()

    def check(self, relpath: str) -> Path:
        return certify.check_cert_home(self.home / relpath, self.home, "claude")

    def test_the_default_is_per_runtime_under_this_install_root(self) -> None:
        root = self.home / ".agent-comms-public"
        self.assertEqual(certify.default_cert_home("claude", root), root / "claude-cert-home")
        self.assertEqual(certify.default_cert_home("codex", root), root / "codex-cert-home")

    def test_dedicated_and_outside_dirs_with_a_private_token_are_accepted(self) -> None:
        for relpath in (".agent-comms/claude-cert-home", "elsewhere"):
            with self.subTest(relpath=relpath):
                self.assertEqual(self.check(relpath), self.home / relpath)

    def test_live_roots_other_cert_homes_and_bad_tokens_are_refused(self) -> None:
        for relpath in ("", ".claude", ".agent-comms", ".agent-comms/codex-cert-home", "empty", "blank", "loose"):
            with self.subTest(relpath=relpath):
                with self.assertRaises(certify.Refused):
                    self.check(relpath)

    def test_codex_does_not_accept_the_claude_cert_home(self) -> None:
        (self.home / ".agent-comms/claude-cert-home/auth.json").write_text("{}")
        with self.assertRaises(certify.Refused):
            certify.check_cert_home(self.home / ".agent-comms/claude-cert-home", self.home, "codex")


class CertRunnerEnvTest(unittest.TestCase):
    def test_each_runtime_gets_its_own_test_names(self) -> None:
        cert, custody, result = Path("/c/cert"), Path("/c/custody"), Path("/c/r.json")
        base = {"PATH": "/bin", certify.LIVE_HOME_ENV: "1"}
        claude = certify.runner_env("claude", base, cert, custody, result)
        self.assertEqual(claude[certify.CERT_ENV], "1")
        self.assertNotIn(certify.LIVE_HOME_ENV, claude)
        self.assertEqual(claude["AGENT_COMMS_TEST_CLAUDE_CERT_HOME"], str(cert))
        self.assertEqual(claude["AGENT_COMMS_TEST_CLAUDE_VERSIONS_DIR"], str(custody))
        self.assertNotIn("AGENT_COMMS_TEST_CODEX_CERT_HOME", claude)
        codex = certify.runner_env("codex", base, cert, custody, result)
        self.assertEqual(codex["AGENT_COMMS_TEST_CODEX_CERT_HOME"], str(cert))
        self.assertEqual(codex["AGENT_COMMS_TEST_CODEX_VERSIONS_DIR"], str(custody))
        self.assertNotIn("AGENT_COMMS_TEST_CLAUDE_CERT_HOME", codex)

    def test_the_cell_runner_maps_each_cert_custody_dir(self) -> None:
        from agent_comms.runtime_pins import CLAUDE_VERSIONS_DIR_ENV, CODEX_VERSIONS_DIR_ENV

        with mock.patch.dict(
            os.environ,
            {"AGENT_COMMS_TEST_CLAUDE_VERSIONS_DIR": "/c/claude", "AGENT_COMMS_TEST_CODEX_VERSIONS_DIR": "/c/codex"},
        ):
            certify_run.apply_cert_custody()
            self.assertEqual(os.environ[CLAUDE_VERSIONS_DIR_ENV], "/c/claude")
            self.assertEqual(os.environ[CODEX_VERSIONS_DIR_ENV], "/c/codex")

    def test_source_options_belong_to_their_runtime(self) -> None:
        with self.assertRaisesRegex(certify.Refused, "--bundle is for codex"):
            certify.check_source_options("claude", bundle=Path("/x"), binary=None)
        with self.assertRaisesRegex(certify.Refused, "--binary is for claude"):
            certify.check_source_options("codex", bundle=None, binary=Path("/x"))
        certify.check_source_options("claude", bundle=None, binary=Path("/x"))
        certify.check_source_options("codex", bundle=Path("/x"), binary=None)


class ClaudeCellCertEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="claudecellenv-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cert = self.root / "cert"
        self.cert.mkdir()
        (self.cert / certify.CLAUDE_TOKEN_NAME).write_text("tok\n")

    def test_outside_cert_mode_nothing_is_added(self) -> None:
        from tests import dispatch_cell_harness as harness

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(isolation.CERT_ENV, None)
            os.environ.pop("AGENT_COMMS_TEST_CLAUDE_CERT_HOME", None)
            self.assertEqual(harness.claude_cert_env(self.root), {})

    def test_cert_mode_gives_each_cell_a_fresh_config_dir_and_the_token(self) -> None:
        from tests import dispatch_cell_harness as harness

        with mock.patch.dict(
            os.environ,
            {isolation.CERT_ENV: "1", "AGENT_COMMS_TEST_CLAUDE_CERT_HOME": str(self.cert), "CLAUDE_CONFIG_DIR": "/live"},
        ):
            env = harness.claude_cert_env(self.root)
            self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok")
            config = Path(env["CLAUDE_CONFIG_DIR"])
            self.assertTrue(config.is_dir())
            self.assertTrue(config.is_relative_to(self.root))
            self.assertNotEqual(config, self.cert)
            adapter = harness.make_claude_harness(self.root).adapter
            child = adapter._extra_child_env(None)
            self.assertEqual(child["CLAUDE_CODE_OAUTH_TOKEN"], "tok")
            self.assertTrue(Path(child["CLAUDE_CONFIG_DIR"]).is_relative_to(self.root))

    def test_a_cert_home_with_a_tilde_is_expanded(self) -> None:
        from tests import dispatch_cell_harness as harness

        home = self.root / "home"
        (home / "cert").mkdir(parents=True)
        (home / "cert" / certify.CLAUDE_TOKEN_NAME).write_text("tok\n")
        with mock.patch.dict(
            os.environ, {isolation.CERT_ENV: "1", "AGENT_COMMS_TEST_CLAUDE_CERT_HOME": "~/cert", "HOME": str(home)}
        ):
            self.assertEqual(harness.claude_cert_env(self.root)["CLAUDE_CODE_OAUTH_TOKEN"], "tok")

    def test_cert_mode_without_a_token_adds_nothing(self) -> None:
        from tests import dispatch_cell_harness as harness

        (self.cert / certify.CLAUDE_TOKEN_NAME).unlink()
        with mock.patch.dict(os.environ, {isolation.CERT_ENV: "1", "AGENT_COMMS_TEST_CLAUDE_CERT_HOME": str(self.cert)}):
            self.assertEqual(harness.claude_cert_env(self.root), {})


class ClaudeCellSkipReasonTest(unittest.TestCase):
    """The import-time skip check: never raises, and in cert mode asks with the cert token."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="claudeskip-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cert = self.root / "cert"
        self.cert.mkdir()
        (self.cert / certify.CLAUDE_TOKEN_NAME).write_text("tok\n")

    def reason(self, binary: Path, env: dict[str, str]):
        from tests import dispatch_cell_harness as harness

        with mock.patch.object(harness, "claude_binary_path", return_value=binary), \
             mock.patch.object(harness, "custody_binary_digest_matches", return_value=True), \
             mock.patch.dict(os.environ, env):
            for name in (isolation.CERT_ENV, "AGENT_COMMS_TEST_CLAUDE_CERT_HOME", "CLAUDE_CODE_OAUTH_TOKEN"):
                if name not in env:
                    os.environ.pop(name, None)
            return harness.claude_cell_skip_reason()

    def cert_env(self) -> dict[str, str]:
        return {isolation.CERT_ENV: "1", "AGENT_COMMS_TEST_CLAUDE_CERT_HOME": str(self.cert)}

    def test_cert_mode_runs_when_the_token_logs_in(self) -> None:
        binary = fake_claude(self.root / "claude", logged_in="token")
        self.assertIsNone(self.reason(binary, self.cert_env()))

    def test_cert_mode_without_a_token_skips_with_the_reason(self) -> None:
        from tests import dispatch_cell_harness as harness

        (self.cert / certify.CLAUDE_TOKEN_NAME).unlink()
        binary = fake_claude(self.root / "claude", logged_in="always")
        reason = self.reason(binary, self.cert_env())
        self.assertIn(harness.CLAUDE_NOT_LOGGED_IN_REASON, reason)
        self.assertIn("no cert token", reason)

    def test_a_logged_out_binary_skips(self) -> None:
        from tests import dispatch_cell_harness as harness

        binary = fake_claude(self.root / "claude", logged_in="never")
        self.assertEqual(self.reason(binary, self.cert_env()), harness.CLAUDE_NOT_LOGGED_IN_REASON)

    def test_a_hanging_or_unrunnable_binary_skips_instead_of_raising(self) -> None:
        from tests import dispatch_cell_harness as harness

        binary = fake_claude(self.root / "claude")
        for error in (subprocess.TimeoutExpired(["claude"], 10), OSError("exec format error")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(harness.subprocess, "run", side_effect=error):
                    self.assertEqual(self.reason(binary, self.cert_env()), harness.CLAUDE_NOT_LOGGED_IN_REASON)


class SecondInstallCertHomeTest(unittest.TestCase):
    """A checkout with its own AGENT_COMMS_HOME never certifies from the default home."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="secondinstall-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve() / "home"
        self.root = self.home / ".agent-comms-public"
        for relpath in (".agent-comms/codex-cert-home", ".agent-comms-public/codex-cert-home", ".agent-comms-public/other", ".codex"):
            (self.home / relpath).mkdir(parents=True)
        (self.home / ".codex" / "auth.json").write_text("{}")
        (self.home / ".agent-comms/codex-cert-home/auth.json").write_text("{}")
        (self.home / ".agent-comms-public/other/auth.json").write_text("{}")
        # The operator links the install's cert login to an existing login.
        (self.root / "codex-cert-home" / "auth.json").symlink_to(self.home / ".codex" / "auth.json")

    def check(self, path: Path) -> Path:
        return certify.check_cert_home(path, self.home, "codex", runtime_root=self.root)

    def test_the_dedicated_dir_in_this_install_with_a_linked_login_is_accepted(self) -> None:
        self.assertEqual(self.check(self.root / "codex-cert-home"), self.root / "codex-cert-home")

    def test_the_other_install_is_refused_entirely(self) -> None:
        with self.assertRaisesRegex(certify.Refused, "another install"):
            self.check(self.home / ".agent-comms" / "codex-cert-home")
        with self.assertRaisesRegex(certify.Refused, "another install"):
            self.check(self.home / ".agent-comms")

    def test_only_the_dedicated_dir_inside_this_install(self) -> None:
        with self.assertRaisesRegex(certify.Refused, "only .*codex-cert-home"):
            self.check(self.root / "other")
        with self.assertRaises(certify.Refused):
            self.check(self.root)

    def test_a_login_linked_through_the_other_install_to_an_outside_login_is_accepted(self) -> None:
        # The operator's design: link to the other install's cert login, which
        # itself links to a login outside both installs.
        (self.home / ".agent-comms/codex-cert-home/auth.json").unlink()
        (self.home / ".agent-comms/codex-cert-home/auth.json").symlink_to(self.home / ".codex" / "auth.json")
        cert = self.root / "codex-cert-home"
        (cert / "auth.json").unlink()
        (cert / "auth.json").symlink_to(self.home / ".agent-comms/codex-cert-home/auth.json")
        self.assertEqual(self.check(cert), cert)

    def test_a_login_that_ends_inside_the_other_install_is_refused(self) -> None:
        cert = self.root / "codex-cert-home"
        (cert / "auth.json").unlink()
        (cert / "auth.json").symlink_to(self.home / ".agent-comms/codex-cert-home/auth.json")
        with self.assertRaisesRegex(certify.Refused, "another install"):
            self.check(cert)

    def test_a_login_that_ends_elsewhere_in_this_install_is_refused(self) -> None:
        cert = self.root / "codex-cert-home"
        (cert / "auth.json").unlink()
        (cert / "auth.json").symlink_to(self.root / "other" / "auth.json")
        with self.assertRaisesRegex(certify.Refused, "live runtime root"):
            self.check(cert)

    def alias_private_root(self) -> Path:
        # The other install's root is a link to a directory outside home.
        vault = self.home.parent / "private-vault"
        (self.home / ".agent-comms").rename(vault)
        (self.home / ".agent-comms").symlink_to(vault)
        return vault

    def test_a_cert_home_under_a_linked_other_install_is_refused(self) -> None:
        self.alias_private_root()
        with self.assertRaisesRegex(certify.Refused, "another install"):
            self.check(self.home / ".agent-comms" / "codex-cert-home")

    def test_a_login_that_ends_inside_a_linked_other_install_is_refused(self) -> None:
        vault = self.alias_private_root()
        cert = self.root / "codex-cert-home"
        (cert / "auth.json").unlink()
        (cert / "auth.json").symlink_to(vault / "codex-cert-home" / "auth.json")
        with self.assertRaisesRegex(certify.Refused, "another install"):
            self.check(cert)

    def test_a_cert_home_inside_a_linked_live_runtime_dir_is_refused(self) -> None:
        live = self.home.parent / "live-claude"
        live.mkdir()
        (self.home / ".claude").symlink_to(live)
        with self.assertRaisesRegex(certify.Refused, "inside the live"):
            self.check(self.home / ".claude")

    def test_a_claude_token_that_ends_inside_the_other_install_is_refused(self) -> None:
        private_token = self.home / ".agent-comms" / "oauth-token"
        private_token.write_text("tok\n")
        private_token.chmod(0o600)
        cert = self.root / "claude-cert-home"
        cert.mkdir()
        (cert / certify.CLAUDE_TOKEN_NAME).symlink_to(private_token)
        with self.assertRaisesRegex(certify.Refused, "another install"):
            certify.check_cert_home(cert, self.home, "claude", runtime_root=self.root)

    def test_a_relative_agent_comms_home_is_refused_cleanly(self) -> None:
        from agent_comms import paths

        with mock.patch.dict(os.environ, {paths.HOME_ENV: "relative/home"}):
            with self.assertRaises(certify.Refused):
                certify.install_runtime_root()

    def test_the_default_install_keeps_its_own_dedicated_dir(self) -> None:
        default_root = self.home / ".agent-comms"
        self.assertEqual(
            certify.check_cert_home(default_root / "codex-cert-home", self.home, "codex", runtime_root=default_root),
            default_root / "codex-cert-home",
        )


if __name__ == "__main__":
    unittest.main()
