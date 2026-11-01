"""scripts/release.sh, run against a scratch checkout with fake gh, uv and shasum.

The script is copied into a temporary git repository with its own bare origin
and a tagged commit, so build, publish and verify never see the source tree,
the real local/release/ directory, or GitHub. The fakes record what the script
asked of them; the tests assert that publish never reaches ``gh release
create`` and verify never reports PASS when the hash tool fails or when the
artifacts no longer match the tag build.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "release.sh"
VERSION = "9.9.9"
TAG = f"v{VERSION}"
WHEEL_NAME = f"agent_comms-{VERSION}-py3-none-any.whl"
SDIST_NAME = f"agent_comms-{VERSION}.tar.gz"

FAKE_UV = f"""#!/bin/sh
# uv build --project <src> --out-dir <out>: write two distinct artifacts.
out=""
while [ $# -gt 0 ]; do
  case "$1" in --out-dir) out=$2; shift ;; esac
  shift
done
[ -n "$out" ] || exit 2
printf 'wheel built from %s\\n' "$(cat "$FAKE_BUILD_STAMP" 2>/dev/null || echo tag)" > "$out/{WHEEL_NAME}"
printf 'sdist\\n' > "$out/{SDIST_NAME}"
"""

FAKE_GH = f"""#!/bin/sh
# Records every call; "release create" stores the wheel it was given as the
# served asset, "release view" succeeds only once that asset exists, and
# "release download" serves it back.
printf '%s\\n' "$*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "release view") [ -f "$FAKE_GH_RELEASE" ] || exit 1; echo "INFO release: {TAG} draft=false assets={WHEEL_NAME}" ;;
  "release create") cp "$4" "$FAKE_GH_RELEASE" ;;
  "release download")
    dir=""
    while [ $# -gt 0 ]; do
      case "$1" in --dir) dir=$2; shift ;; esac
      shift
    done
    cp "$FAKE_GH_RELEASE" "$dir/{WHEEL_NAME}" ;;
  *) exit 1 ;;
esac
"""

FAILING_SHASUM = "#!/bin/sh\nexit 1\n"
SILENT_SHASUM = "#!/bin/sh\nexit 0\n"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class ReleaseScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        base = Path(self._temp.name)
        self.work = base / "work"
        self.bin = base / "bin"
        self.bin.mkdir()
        self.gh_log = base / "gh.log"
        self.gh_release = base / "served.whl"
        _write_executable(self.bin / "uv", FAKE_UV)
        _write_executable(self.bin / "gh", FAKE_GH)
        self._init_repo(base)
        self.root = self.work / "local" / "release" / VERSION
        self.wheel = self.root / "wheels" / WHEEL_NAME
        self.sdist = self.root / "wheels" / SDIST_NAME
        self.notes = self.root / "notes.md"
        self.record = self.root / "build.txt"

    def _init_repo(self, base: Path) -> None:
        env = self._git_env()
        self.work.mkdir()
        (self.work / "scripts").mkdir()
        (self.work / "scripts" / "release.sh").write_text(SCRIPT.read_text())
        (self.work / "pyproject.toml").write_text(
            f'[project]\nname = "agent-comms"\nversion = "{VERSION}"\n'
        )
        git = ["git", "-C", str(self.work)]
        subprocess.run([*git, "init", "-q", "-b", "main"], env=env, check=True)
        subprocess.run([*git, "add", "."], env=env, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "release commit"], env=env, check=True)
        subprocess.run([*git, "tag", "-a", TAG, "-m", TAG], env=env, check=True)
        origin = base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], env=env, check=True)
        subprocess.run([*git, "remote", "add", "origin", str(origin)], env=env, check=True)
        subprocess.run([*git, "push", "-q", "origin", "main", TAG], env=env, check=True)
        self.commit = subprocess.run(
            [*git, "rev-parse", f"{TAG}^{{commit}}"], env=env, check=True, text=True, capture_output=True
        ).stdout.strip()

    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        return env

    def run_script(self, *args: str, shasum: str | None = None) -> subprocess.CompletedProcess[str]:
        env = self._git_env()
        path_dirs = [str(self.bin)]
        if shasum is not None:
            shasum_dir = Path(self._temp.name) / "shasum-bin"
            shasum_dir.mkdir(exist_ok=True)
            _write_executable(shasum_dir / "shasum", shasum)
            path_dirs.insert(0, str(shasum_dir))
        env["PATH"] = os.pathsep.join([*path_dirs, env.get("PATH", "")])
        env["FAKE_GH_LOG"] = str(self.gh_log)
        env["FAKE_GH_RELEASE"] = str(self.gh_release)
        env["FAKE_BUILD_STAMP"] = str(Path(self._temp.name) / "stamp")
        return subprocess.run(
            ["sh", str(self.work / "scripts" / "release.sh"), *args],
            cwd=self._temp.name,
            env=env,
            text=True,
            capture_output=True,
        )

    def build(self) -> None:
        result = self.run_script("build", VERSION)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"PASS release build ({TAG})", result.stdout)

    def write_notes(self, digest: str | None = None) -> None:
        if digest is None:
            digest = self.record_value("wheel_sha256")
        self.notes.write_text(f"# agent-comms {VERSION}\n\nwheel sha256 `{digest}`\n")

    def record_value(self, key: str) -> str:
        for line in self.record.read_text().splitlines():
            name, _, value = line.partition("=")
            if name == key:
                return value
        raise AssertionError(f"{key} missing from {self.record}")

    def gh_calls(self) -> list[str]:
        if not self.gh_log.exists():
            return []
        return self.gh_log.read_text().splitlines()

    def assert_no_create(self) -> None:
        self.assertFalse(
            [call for call in self.gh_calls() if call.startswith("release create")],
            self.gh_calls(),
        )

    def test_build_records_the_tag_commit_and_both_digests(self) -> None:
        self.build()

        self.assertEqual(self.record_value("commit"), self.commit)
        for key in ("wheel_sha256", "sdist_sha256"):
            value = self.record_value(key)
            self.assertEqual(len(value), 64, value)
            self.assertTrue(all(c in "0123456789abcdef" for c in value), value)
        self.assertEqual(self.record_value("wheel_sha256"), self._sha256(self.wheel))
        self.assertEqual(self.record_value("sdist_sha256"), self._sha256(self.sdist))

    def test_publish_then_verify_pass_for_the_tag_build(self) -> None:
        self.build()
        self.write_notes()

        publish = self.run_script("publish", VERSION)
        self.assertEqual(publish.returncode, 0, publish.stderr)
        creates = [call for call in self.gh_calls() if call.startswith("release create")]
        self.assertEqual(len(creates), 1, self.gh_calls())
        self.assertIn(
            f"release create {TAG} local/release/{VERSION}/wheels/{WHEEL_NAME} "
            f"local/release/{VERSION}/wheels/{SDIST_NAME} --verify-tag",
            creates[0],
        )
        self.assertEqual(self.gh_release.read_bytes(), self.wheel.read_bytes())
        self.assertIn("--notes-file", creates[0])

        verify = self.run_script("verify", VERSION)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertIn(f"PASS release verify ({TAG}, wheel sha256 {self.record_value('wheel_sha256')})", verify.stdout)

    def test_publish_refuses_notes_without_the_wheel_digest(self) -> None:
        self.build()
        self.write_notes(digest="0" * 64)

        result = self.run_script("publish", VERSION)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("notes do not state the wheel's sha256", result.stderr)
        self.assert_no_create()

    def test_publish_refuses_a_missing_build_record(self) -> None:
        self.build()
        self.write_notes()
        self.record.unlink()

        result = self.run_script("publish", VERSION)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no build record", result.stderr)
        self.assert_no_create()

    def test_publish_refuses_a_replaced_wheel_even_with_matching_notes(self) -> None:
        self.build()
        self.wheel.write_text("wheel built from the working tree\n")
        self.write_notes(digest=self._sha256(self.wheel))

        result = self.run_script("publish", VERSION)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wheel sha256", result.stderr)
        self.assertIn("differs from the build record", result.stderr)
        self.assert_no_create()

    def test_publish_refuses_a_replaced_sdist(self) -> None:
        self.build()
        self.write_notes()
        self.sdist.write_text("sdist from somewhere else\n")

        result = self.run_script("publish", VERSION)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sdist sha256", result.stderr)
        self.assert_no_create()

    def test_publish_refuses_a_record_from_another_commit(self) -> None:
        self.build()
        self.write_notes()
        text = self.record.read_text().replace(f"commit={self.commit}", "commit=" + "f" * 40)
        self.record.write_text(text)

        result = self.run_script("publish", VERSION)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build record names commit", result.stderr)
        self.assert_no_create()

    def test_hash_tool_failure_blocks_build_publish_and_verify(self) -> None:
        self.build()
        self.write_notes()
        publish_ok = self.run_script("publish", VERSION)
        self.assertEqual(publish_ok.returncode, 0, publish_ok.stderr)
        self.gh_log.unlink()

        for shasum in (FAILING_SHASUM, SILENT_SHASUM):
            with self.subTest(shasum=shasum):
                self.gh_release.unlink(missing_ok=True)
                publish = self.run_script("publish", VERSION, shasum=shasum)
                self.assertNotEqual(publish.returncode, 0)
                self.assertNotIn("PASS", publish.stdout)
                self.assertIn("FAIL release", publish.stderr)
                self.assert_no_create()

                self.gh_release.write_bytes(self.wheel.read_bytes())
                verify = self.run_script("verify", VERSION, shasum=shasum)
                self.assertNotEqual(verify.returncode, 0)
                self.assertNotIn("PASS", verify.stdout)
                self.assertIn("FAIL release", verify.stderr)

                build = self.run_script("build", VERSION, shasum=shasum)
                self.assertNotEqual(build.returncode, 0)
                self.assertNotIn("PASS", build.stdout)

    def test_verify_refuses_a_served_wheel_that_differs(self) -> None:
        self.build()
        self.write_notes()
        self.assertEqual(self.run_script("publish", VERSION).returncode, 0)
        self.gh_release.write_text("something else was uploaded\n")

        result = self.run_script("verify", VERSION)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PASS", result.stdout)
        self.assertIn("served wheel sha256", result.stderr)

    @staticmethod
    def _sha256(path: Path) -> str:
        import hashlib

        return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
