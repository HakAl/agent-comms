"""Codex binary custody: the bundle digest and custody paths (ac-4ao.10 slice 3a)."""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import custody, paths, runtime_pins
from agent_comms.cli import run as cli_run
from agent_comms.custody import CustodyError, fill_codex_custody
from agent_comms.runtime_pins import BundleNotDigestible, bundle_manifest, bundle_sha256
from tests.codex_custody_support import default_codex_script


def _bundle(root: Path) -> Path:
    bundle = root / "bundle"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "bin" / "codex").write_bytes(b"binary")
    (bundle / "bin" / "codex").chmod(0o755)
    (bundle / "codex-resources").mkdir()
    (bundle / "codex-resources" / "data.txt").write_bytes(b"data")
    (bundle / "codex-resources" / "data.txt").chmod(0o644)
    return bundle


class BundleDigestTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bundle = _bundle(self.root)

    def test_manifest_lists_files_sorted_with_owner_exec_mode_and_digest(self) -> None:
        manifest = bundle_manifest(self.bundle)
        self.assertEqual([entry[0] for entry in manifest], ["bin/codex", "codex-resources/data.txt"])
        self.assertEqual([entry[1] for entry in manifest], ["755", "644"])
        self.assertEqual(len(manifest[0][2]), 64)

    def test_digest_is_stable_across_copies_and_group_or_other_mode_bits(self) -> None:
        before = bundle_sha256(self.bundle)
        (self.bundle / "codex-resources" / "data.txt").chmod(0o664)
        (self.bundle / "bin" / "codex").chmod(0o700)
        self.assertEqual(bundle_sha256(self.bundle), before)
        other = _bundle(self.root / "other")
        self.assertEqual(bundle_sha256(other), before)

    def test_digest_changes_with_content_owner_exec_bit_name_or_new_file(self) -> None:
        before = bundle_sha256(self.bundle)
        for change in (
            lambda b: (b / "bin" / "codex").write_bytes(b"tampered"),
            lambda b: (b / "codex-resources" / "data.txt").chmod(0o755),
            lambda b: (b / "codex-resources" / "data.txt").rename(b / "codex-resources" / "renamed.txt"),
            lambda b: (b / "extra").write_bytes(b""),
        ):
            with self.subTest(change=change):
                bundle = _bundle(Path(tempfile.mkdtemp(dir=self.root)))
                change(bundle)
                self.assertNotEqual(bundle_sha256(bundle), before)

    def test_relative_symlink_inside_is_recorded_by_target(self) -> None:
        (self.bundle / "bin" / "alias").symlink_to("codex")
        entry = [e for e in bundle_manifest(self.bundle) if e[0] == "bin/alias"]
        self.assertEqual(entry, [["bin/alias", "link", "codex"]])

    def test_symlink_that_is_absolute_or_leaves_the_bundle_is_refused(self) -> None:
        for target in (str(self.root), "../../outside"):
            with self.subTest(target=target):
                link = self.bundle / "bin" / "escape"
                link.symlink_to(target)
                with self.assertRaisesRegex(BundleNotDigestible, "symlink leaves the bundle"):
                    bundle_sha256(self.bundle)
                link.unlink()

    def test_other_file_types_and_non_directory_roots_are_refused(self) -> None:
        os.mkfifo(self.bundle / "fifo")
        with self.assertRaisesRegex(BundleNotDigestible, "unsupported file type"):
            bundle_sha256(self.bundle)
        (self.bundle / "fifo").unlink()
        link_root = self.root / "link-root"
        link_root.symlink_to(self.bundle)
        for root in (link_root, self.root / "missing", self.bundle / "bin" / "codex"):
            with self.subTest(root=root), self.assertRaisesRegex(BundleNotDigestible, "not a directory"):
                bundle_sha256(root)

    def test_digest_matches_needs_a_pin_and_fails_closed_on_bad_bundles(self) -> None:
        digest = bundle_sha256(self.bundle)
        self.assertTrue(runtime_pins.codex_bundle_digest_matches(self.bundle, digest))
        self.assertFalse(runtime_pins.codex_bundle_digest_matches(self.root / "missing", digest))
        with mock.patch.object(runtime_pins, "codex_pinned_sha256", return_value=""):
            self.assertFalse(runtime_pins.codex_bundle_digest_matches(self.bundle))


class CodexCustodyPathTest(unittest.TestCase):
    def test_default_layout_is_under_the_runtime_root(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(runtime_pins.CODEX_VERSIONS_DIR_ENV, None)
            version = runtime_pins.codex_pin().version
            self.assertEqual(
                runtime_pins.codex_binary_path(),
                paths.runtime_root() / "runtime-custody" / "codex" / version / "bin" / "codex",
            )

    def test_overrides_move_the_custody_dir_and_the_pinned_digest(self) -> None:
        with mock.patch.dict(
            os.environ,
            {runtime_pins.CODEX_VERSIONS_DIR_ENV: "/custody", runtime_pins.CODEX_PINNED_SHA256_ENV: "ab" * 32},
        ):
            self.assertEqual(runtime_pins.codex_bundle_path(), Path("/custody") / runtime_pins.codex_pin().version)
            self.assertEqual(runtime_pins.codex_pinned_sha256(), "ab" * 32)

    def test_a_relative_versions_dir_override_is_refused(self) -> None:
        from agent_comms.schema import ValidationError

        with mock.patch.dict(os.environ, {runtime_pins.CODEX_VERSIONS_DIR_ENV: "custody"}):
            for call in (runtime_pins.codex_versions_dir, runtime_pins.codex_bundle_path, runtime_pins.codex_binary_path):
                with self.subTest(call=call), self.assertRaisesRegex(ValidationError, "not an absolute path"):
                    call()

    def test_unlisted_platform_fails_closed_before_any_path(self) -> None:
        with mock.patch.object(runtime_pins, "current_platform", return_value="linux-x86_64"):
            for call in (runtime_pins.codex_bundle_path, runtime_pins.codex_binary_path):
                with self.subTest(call=call), self.assertRaises(runtime_pins.PlatformNotCertified):
                    call()


def _native_bundle(root: Path, version: str | None = None) -> Path:
    """A bundle whose bin/codex reports ``version`` (default: the pin)."""
    bundle = root
    (bundle / "bin").mkdir(parents=True)
    script = default_codex_script()
    if version is not None:
        script = script.replace(runtime_pins.codex_pin().version, version)
    (bundle / "bin" / "codex").write_text(script)
    (bundle / "bin" / "codex").chmod(0o755)
    (bundle / "codex-resources").mkdir()
    (bundle / "codex-resources" / "r.txt").write_text("resource")
    return bundle


class FillCustodyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = _native_bundle(self.root / "installed")
        self.versions = self.root / "custody"
        self.pin(bundle_sha256(self.source))

    def pin(self, digest: str) -> None:
        patch = mock.patch.dict(
            os.environ,
            {runtime_pins.CODEX_VERSIONS_DIR_ENV: str(self.versions), runtime_pins.CODEX_PINNED_SHA256_ENV: digest},
        )
        patch.start()
        self.addCleanup(patch.stop)

    @property
    def dest(self) -> Path:
        return self.versions / runtime_pins.codex_pin().version

    def test_fills_from_a_named_bundle_then_is_a_noop(self) -> None:
        result = fill_codex_custody(self.source)
        self.assertEqual(result["status"], "filled")
        self.assertEqual(bundle_sha256(self.dest), bundle_sha256(self.source))
        self.assertTrue(os.access(self.dest / "bin" / "codex", os.X_OK))
        self.assertEqual(fill_codex_custody(self.source)["status"], "present")
        self.assertEqual([p.name for p in self.versions.iterdir()], [self.dest.name])

    def test_refuses_a_wrong_version_and_leaves_nothing(self) -> None:
        other = _native_bundle(self.root / "other", "0.0.1")
        with self.assertRaisesRegex(CustodyError, r"is codex 0\.0\.1, the pin is .*make certify RUNTIME=codex"):
            fill_codex_custody(other)
        self.assertFalse(self.dest.exists())

    def test_refuses_a_same_version_build_that_is_not_the_certified_one(self) -> None:
        (self.source / "codex-resources" / "r.txt").write_text("different build")
        with self.assertRaisesRegex(CustodyError, "not the certified build"):
            fill_codex_custody(self.source)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.versions.iterdir()), [])

    def test_a_tampered_copy_is_refused_until_replace(self) -> None:
        fill_codex_custody(self.source)
        (self.dest / "bin" / "codex").write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaisesRegex(CustodyError, "custody codex --replace"):
            fill_codex_custody(self.source)
        self.assertEqual(fill_codex_custody(self.source, replace=True)["status"], "replaced")
        self.assertEqual(bundle_sha256(self.dest), bundle_sha256(self.source))
        self.assertEqual([p.name for p in self.versions.iterdir()], [self.dest.name])

    def test_replace_survives_a_retired_dir_left_by_a_killed_run(self) -> None:
        fill_codex_custody(self.source)
        (self.dest / "bin" / "codex").write_text("#!/bin/sh\nexit 0\n")
        leftover = self.versions / f".{self.dest.name}.old-{os.getpid()}"
        (leftover / "bin").mkdir(parents=True)
        (leftover / "bin" / "codex").write_text("stale")
        self.assertEqual(fill_codex_custody(self.source, replace=True)["status"], "replaced")
        self.assertEqual(bundle_sha256(self.dest), bundle_sha256(self.source))
        self.assertEqual([p.name for p in self.versions.iterdir()], [self.dest.name])

    def test_uncertified_platform_is_a_custody_refusal(self) -> None:
        import contextlib
        import io
        import json

        with mock.patch.object(runtime_pins, "current_platform", return_value="linux-x86_64"):
            with self.assertRaisesRegex(CustodyError, "no certified pin for linux-x86_64"):
                fill_codex_custody(self.source)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli_run(["custody", "codex", "--from", str(self.source)]), 2)
        self.assertIn("no certified pin", json.loads(out.getvalue())["error"])

    def test_refuses_without_a_pinned_digest(self) -> None:
        self.pin("")
        with mock.patch.object(runtime_pins, "codex_pin", return_value=runtime_pins.RuntimePin(
            "codex", "darwin-arm64", runtime_pins.codex_pin().version, "exact", "2026-10-10", None,
        )):
            with self.assertRaisesRegex(CustodyError, "records no bundle sha256"):
                fill_codex_custody(self.source)

    def test_locates_the_bundle_behind_the_npm_wrapper(self) -> None:
        triple, package = custody.NATIVE_TARGETS["darwin-arm64"]
        for layout in ("nested", "hoisted"):
            with self.subTest(layout=layout):
                scope = self.root / layout / "lib" / "node_modules" / "@openai"
                wrapper = scope / "codex" / "bin" / "codex.js"
                wrapper.parent.mkdir(parents=True)
                wrapper.write_text("// wrapper")
                link = self.root / layout / "bin" / "codex"
                link.parent.mkdir(parents=True)
                link.symlink_to(wrapper)
                base = scope / "codex" / "node_modules" / "@openai" if layout == "nested" else scope
                native = _native_bundle(base / package / "vendor" / triple)
                found = custody.locate_native_bundle(lambda _name: str(link), platform="darwin-arm64")
                self.assertEqual(found, native.resolve())

    def test_locate_names_the_fix_when_nothing_is_found(self) -> None:
        with self.assertRaisesRegex(CustodyError, "codex is not on PATH.*--from DIR"):
            custody.locate_native_bundle(lambda _name: None)
        lonely = self.root / "lonely" / "bin" / "codex"
        lonely.parent.mkdir(parents=True)
        lonely.write_text("")
        with self.assertRaisesRegex(CustodyError, "no native Codex bundle found.*--from DIR"):
            custody.locate_native_bundle(lambda _name: str(lonely), platform="darwin-arm64")

    def test_cli_fills_and_refuses_cleanly(self) -> None:
        import contextlib
        import io
        import json

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli_run(["custody", "codex", "--from", str(self.source)]), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "filled")
        (self.dest / "bin" / "codex").write_text("tampered")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli_run(["custody", "codex", "--from", str(self.source)]), 2)
        self.assertIn("--replace", json.loads(out.getvalue())["error"])

    def test_custody_state_reports_missing_mismatch_and_present(self) -> None:
        self.assertEqual(custody.codex_custody_state()["reason"], "missing")
        fill_codex_custody(self.source)
        self.assertTrue(custody.codex_custody_state()["present"])
        (self.dest / "extra").write_text("")
        self.assertEqual(custody.codex_custody_state()["reason"], "digest differs from the pin")


class AdapterCustodyTest(unittest.TestCase):
    """A Codex dispatch runs only the certified custody bundle; anything else starts nothing."""

    def setUp(self) -> None:
        from tests.codex_custody_support import install_fake_codex_custody

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.binary = install_fake_codex_custody(self)

    def dispatch(self, mutate=None):  # type: ignore[no-untyped-def]
        from datetime import timedelta

        from agent_comms import supervisor
        from agent_comms.adapters.codex import CodexAdapter
        from tests.substrate.test_codex_auth_preflight import NOW, _codex_context, _write_auth

        home = self.root / "codex-home"
        _write_auth(home, timedelta(hours=1))
        context = _codex_context(self.root, home)
        if mutate is not None:
            mutate(context.recipient["spawn"])
        with mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW), \
                mock.patch.object(CodexAdapter, "_binding_preflight", return_value=None), \
                mock.patch.object(supervisor, "spawn_supervised") as spawn:
            try:
                CodexAdapter().dispatch(context)
            finally:
                self.spawned = spawn.called

    def test_missing_custody_refuses_naming_the_fill_command(self) -> None:
        from agent_comms.adapters.codex import CodexCustodyUnavailable

        self.binary.unlink()
        with self.assertRaisesRegex(CodexCustodyUnavailable, "recover with: agent-comms custody codex$"):
            self.dispatch()
        self.assertFalse(self.spawned)

    def test_tampered_custody_refuses_naming_replace(self) -> None:
        from agent_comms.adapters.codex import CodexCustodyDigestMismatch

        (self.binary.parent.parent / "extra").write_text("")
        with self.assertRaisesRegex(CodexCustodyDigestMismatch, "differs from the certified bundle.*--replace"):
            self.dispatch()
        self.assertFalse(self.spawned)

    def test_a_pin_without_a_digest_refuses_naming_certify(self) -> None:
        from agent_comms.adapters.codex import CodexCustodyDigestMismatch

        with mock.patch("agent_comms.adapters.codex.codex_pinned_sha256", return_value=""):
            with self.assertRaisesRegex(CodexCustodyDigestMismatch, "make certify RUNTIME=codex"):
                self.dispatch()
        self.assertFalse(self.spawned)

    def test_a_stored_command_outside_custody_refuses_naming_rerender(self) -> None:
        from agent_comms.adapters.codex import CodexSpawnRowNotPinnedError

        for command in ("codex", "/usr/local/bin/codex", str(self.root / "elsewhere" / "codex"),
                        f"{self.binary.parent}/../bin/codex"):
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                    CodexSpawnRowNotPinnedError, "rerender-spawn alpha-codex-worker --apply"
                ):
                    self.dispatch(lambda spawn: spawn.update(command=command))
                self.assertFalse(self.spawned)

    def test_a_relative_command_that_resolves_to_custody_here_is_refused(self) -> None:
        # Validation runs in the dispatcher's cwd but launch runs in the worker's,
        # so a relative command could name the custody copy here and another binary there.
        from agent_comms.adapters.codex import CodexSpawnRowNotPinnedError

        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.binary.parent.parent.parent)
        relative = os.path.relpath(self.binary)
        self.assertEqual(Path(relative).resolve(), self.binary.resolve())
        with self.assertRaisesRegex(CodexSpawnRowNotPinnedError, "not the custody binary"):
            self.dispatch(lambda spawn: spawn.update(command=relative))
        self.assertFalse(self.spawned)

    def test_the_custody_binary_is_what_starts(self) -> None:
        from agent_comms import supervisor

        with mock.patch.object(supervisor, "reaper_registry", return_value=mock.MagicMock()), \
                mock.patch.object(supervisor, "janitor_sweep", return_value=[]):
            from datetime import timedelta

            from agent_comms.adapters.codex import CodexAdapter
            from tests.substrate.test_codex_auth_preflight import NOW, _codex_context, _fake_supervised, _write_auth

            home = self.root / "codex-home"
            _write_auth(home, timedelta(hours=1))
            with mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW), \
                    mock.patch.object(CodexAdapter, "_binding_preflight", return_value=None), \
                    mock.patch.object(supervisor, "spawn_supervised", return_value=_fake_supervised()) as spawn:
                CodexAdapter().dispatch(_codex_context(self.root, home))
        self.assertEqual(spawn.call_args.args[0][0], str(self.binary))


class NativeBundleProbeTest(unittest.TestCase):
    """The native binary custody runs works without the npm wrapper's
    ``CODEX_MANAGED_*`` environment (no login, no model call)."""

    def setUp(self) -> None:
        try:
            self.bundle = custody.locate_native_bundle()
        except CustodyError as exc:
            self.skipTest(f"no native Codex bundle on this machine: {exc}")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = {"PATH": "/usr/bin:/bin", "HOME": self._tmp.name, "CODEX_HOME": self._tmp.name}

    def run_native(self, *args: str):  # type: ignore[no-untyped-def]
        import subprocess

        return subprocess.run(
            [str(self.bundle / "bin" / "codex"), *args],
            env=self.env, cwd=self._tmp.name, text=True, capture_output=True, check=False, timeout=30,
        )

    def test_version_and_mcp_resolution_run_without_the_wrapper_env(self) -> None:
        import json

        version = self.run_native("--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertRegex(version.stdout, r"\d+\.\d+\.\d+")
        resolved = self.run_native("-c", 'mcp_servers.probe.command="/bin/echo"', "mcp", "get", "probe", "--json")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(json.loads(resolved.stdout)["transport"]["command"], "/bin/echo")


if __name__ == "__main__":
    unittest.main()
