"""The local gates: hygiene scrub, ruff baseline, and the Makefile targets.

The gate scripts live under ``scripts/gates`` and are loaded here by path;
they are plain stdlib programs, not part of the package. Every check runs in
temporary directories only.

Planted violations are assembled at runtime (``plant``) so that this file,
which is tracked and scanned, does not itself contain anything the hygiene
gate would flag.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
GATES = REPO_ROOT / "scripts" / "gates"


def load(name: str):
    module_name = f"gates_{name}"
    spec = importlib.util.spec_from_file_location(module_name, str(GATES / f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def plant(*parts: str) -> str:
    """Join fragments into a value the hygiene gate must flag."""
    return "".join(parts)


PERSONAL_PATH = plant("/Us", "ers/somebody")
HOME_PATH = plant("/ho", "me/somebody/project")
REAL_EMAIL = plant("somebody@", "gmail.com")
MACHINE_HOST = plant("Somebodys-MacBook-Pro", ".local")
PRIVATE_KEY_HEADER = plant("-----BEGIN ", "OPENSSH PRIVATE KEY-----")
SSH_PUBLIC_KEY = plant("ssh-ed25519 ", "AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyMaterial0000 x")
GITHUB_TOKEN = plant("gh", "p_" + "a" * 36)
GITHUB_OAUTH_TOKEN = plant("gh", "o_" + "b" * 36)
AWS_KEY = plant("AK", "IA" + "A" * 16)
GOOGLE_KEY = plant("AI", "za" + "S" * 35)
HUGGINGFACE_TOKEN = plant("hf", "_" + "c" * 30)
OPENAI_TOKEN = plant("sk", "-proj-" + "d" * 30)
PRIVATE_ULID = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ["HOME"],
        "GIT_AUTHOR_NAME": "Example Author",
        "GIT_AUTHOR_EMAIL": "author@example.com",
        "GIT_COMMITTER_NAME": "Example Author",
        "GIT_COMMITTER_EMAIL": "author@example.com",
    }
    if env:
        base_env.update(env)
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=base_env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


class HygieneScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hygiene = load("hygiene")

    def checks_for(self, text: str, **lists) -> set[str]:
        findings = self.hygiene.scan_text(text, path="x.txt", **lists)
        return {finding.check for finding in findings}

    def test_personal_paths_are_caught(self) -> None:
        self.assertIn("personal-path", self.checks_for(f"root = '{PERSONAL_PATH}/dev/x'"))
        self.assertIn("personal-path", self.checks_for(f"cd {HOME_PATH}"))
        self.assertNotIn("personal-path", self.checks_for("state lives under ~/.agent-comms"))
        self.assertNotIn("personal-path", self.checks_for("HOME=/opt/scratch/home is fine"))

    def test_emails_are_caught_except_reserved_addresses(self) -> None:
        self.assertIn("email", self.checks_for(f"contact: {REAL_EMAIL}"))
        for allowed in (
            "approver@example.com",
            "bot@users.noreply.github.com",
            "test@example.invalid",
            "cell-fixture@agent-comms.invalid",
            "t@e.test",
            "someone@host.example",
            "root@box.localhost",
            "git@github.com:org/repo.git",
            "noreply@github.com",
        ):
            self.assertNotIn("email", self.checks_for(allowed), allowed)
        self.assertNotIn("email", self.checks_for("decorator @unittest.skip and a@b"))

    def test_machine_hostnames_are_caught(self) -> None:
        self.assertIn("hostname", self.checks_for(f"somebody@{MACHINE_HOST}"))
        self.assertNotIn("hostname", self.checks_for(".claude/settings.local.json"))
        self.assertNotIn("hostname", self.checks_for("kept under local/ and tests.local_helper"))

    def test_key_and_token_material_is_caught(self) -> None:
        for secret in (
            PRIVATE_KEY_HEADER,
            SSH_PUBLIC_KEY,
            f"token = '{GITHUB_TOKEN}'",
            GITHUB_OAUTH_TOKEN,
            AWS_KEY,
            GOOGLE_KEY,
            HUGGINGFACE_TOKEN,
            OPENAI_TOKEN,
        ):
            self.assertIn("secret", self.checks_for(secret), secret)
        self.assertNotIn("secret", self.checks_for("ssh-ed25519 REPLACE_WITH_APPROVER_PUBLIC_KEY"))
        self.assertNotIn("secret", self.checks_for("the task id is task-1 and sk-ip is a word"))
        self.assertNotIn("secret", self.checks_for("shelf_" + "x" * 30 + " is not a token"))

    def test_allow_marker_exempts_patterns_but_not_private_lists(self) -> None:
        marked = f"token = '{OPENAI_TOKEN}'  # hygiene:allow token-shaped test value"
        self.assertEqual(self.checks_for(marked), set())
        with_private = f"mallory {OPENAI_TOKEN}  # hygiene:allow"
        self.assertEqual(self.checks_for(with_private, private_words=["mallory"]), {"private-word"})

    def test_private_lists_are_word_and_substring_matched(self) -> None:
        checks = self.checks_for(
            f"Adjacent nodes; the Mallory team; id {PRIVATE_ULID}",
            private_words=["jace", "mallory"],
            private_fragments=[PRIVATE_ULID],
        )
        self.assertIn("private-word", checks)
        self.assertIn("private-fragment", checks)
        self.assertNotIn(
            "private-word",
            self.checks_for("Adjacent nodes only", private_words=["jace"]),
        )

    def test_findings_carry_line_numbers(self) -> None:
        findings = self.hygiene.scan_text(f"ok\n{REAL_EMAIL}\n", path="p")
        self.assertEqual([(f.path, f.line) for f in findings], [("p", 2)])

    def test_patch_scan_reports_added_lines_with_new_line_numbers(self) -> None:
        patch = (
            "diff --git a/notes.md b/notes.md\n"
            "index 1111111..2222222 100644\n"
            "--- a/notes.md\n"
            "+++ b/notes.md\n"
            "@@ -3,0 +4,2 @@ heading\n"
            "+fine line\n"
            f"+{REAL_EMAIL}\n"
            "@@ -10 +12 @@\n"
            f"-{PERSONAL_PATH} was removed, removed lines are not scanned\n"
            f"+{PERSONAL_PATH} was added\n"
            "diff --git a/LICENSE b/LICENSE\n"
            "--- a/LICENSE\n"
            "+++ b/LICENSE\n"
            "@@ -0,0 +1 @@\n"
            f"+{REAL_EMAIL}\n"
            "diff --git a/gone.txt b/gone.txt\n"
            "deleted file mode 100644\n"
            "--- a/gone.txt\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            f"-{REAL_EMAIL}\n"
        )
        findings = self.hygiene.scan_patch(patch, label="commit abc1234")
        self.assertEqual(
            [(f.path, f.line, f.check) for f in findings],
            [
                ("commit abc1234:notes.md", 5, "email"),
                ("commit abc1234:notes.md", 12, "personal-path"),
            ],
        )

    def test_patch_scan_does_not_mistake_added_lines_for_file_headers(self) -> None:
        patch = (
            "diff --git a/notes.md b/notes.md\n"
            "--- a/notes.md\n"
            "+++ b/notes.md\n"
            "@@ -0,0 +1,3 @@\n"
            "+++ a changelog line that starts with two pluses\n"
            f"+++ contact {REAL_EMAIL}\n"
            "+-- and one that starts with two minuses\n"
            'diff --git "a/sp ace.md" "b/sp ace.md"\n'
            '--- "a/sp ace.md"\n'
            '+++ "b/sp ace.md"\n'
            "@@ -0,0 +1 @@\n"
            f"+{PERSONAL_PATH}\n"
        )
        findings = self.hygiene.scan_patch(patch, label="c")
        self.assertEqual(
            [(f.path, f.line, f.check) for f in findings],
            [("c:notes.md", 2, "email"), ("c:sp ace.md", 1, "personal-path")],
        )


class HygieneRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hygiene = load("hygiene")
        # The runner's own gate settings (for example from `make preverify`) must not reach the tests.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (self.hygiene.PRIVATE_DIR_ENV, self.hygiene.BASE_ENV):
            os.environ.pop(name, None)
        self.temp = tempfile.TemporaryDirectory(prefix="gates-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# Example\n\nsee ~/.agent-comms\n")
        (self.repo / "config").mkdir()
        (self.repo / "config" / "actors.example.json").write_text("{}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial commit")

    def run_hygiene(self, **kwargs) -> tuple[int, str]:
        out = io.StringIO()
        code = self.hygiene.check_repo(self.repo, out=out, **kwargs)
        return code, out.getvalue()

    def commit_file(self, name: str, text: str, message: str) -> str:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "--short=7", "HEAD").strip()

    def test_clean_repo_passes_and_reports_skips(self) -> None:
        code, report = self.run_hygiene(base="main")
        self.assertEqual(code, 0, report)
        self.assertIn("SKIP commits", report)
        self.assertIn("SKIP private-lists", report)
        self.assertNotIn("FAIL", report)

    def test_clean_feature_branch_passes_the_commit_scan(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "feature")
        self.commit_file("notes.md", "plain notes\ntest@example.invalid\n", "add notes")
        code, report = self.run_hygiene(base="main")
        self.assertEqual(code, 0, report)
        self.assertIn("PASS commits (1 commit(s) beyond main", report)

    def test_planted_violations_fail_with_named_findings(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "feature")
        (self.repo / "notes.md").write_text(f"built at {PERSONAL_PATH}/dev\nmail {REAL_EMAIL}\n")
        (self.repo / "config" / "actors.json").write_text("{}\n")
        (self.repo / ".env").write_text("TOKEN=x\n")
        (self.repo / "deploy").mkdir()
        (self.repo / "deploy" / ".env.production").write_text("TOKEN=y\n")
        (self.repo / "data").mkdir()
        (self.repo / "data" / "x.sqlite").write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
        private = Path(self.temp.name) / "private"
        private.mkdir()
        (private / "private-words.txt").write_text("mallory\n")
        (private / "private-fragments.txt").write_text(f"{PRIVATE_ULID}\n")
        (self.repo / "team.md").write_text(f"the Mallory team, actor {PRIVATE_ULID}\n")
        git(self.repo, "add", "-A")
        git(
            self.repo,
            "commit",
            "-q",
            "-m",
            f"add notes from {PERSONAL_PATH}",
            env={"GIT_COMMITTER_EMAIL": f"somebody@{MACHINE_HOST}"},
        )
        code, report = self.run_hygiene(base="main", private_dir=private)
        self.assertNotEqual(code, 0)
        for expected in (
            "notes.md:1: personal-path",
            "notes.md:2: email",
            "config/actors.json",
            ".env: environment files",
            "deploy/.env.production: environment files",
            "data/x.sqlite",
            "team.md:1: private-word",
            "team.md:1: private-fragment",
            "personal-path",
            "committer",
            ".local",
        ):
            self.assertIn(expected, report, report)
        self.assertIn("FAIL tracked-layout", report)
        self.assertIn("FAIL binaries", report)
        self.assertIn("FAIL contents", report)
        self.assertIn("FAIL commits", report)

    def test_secret_removed_by_a_later_commit_still_fails(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "feature")
        sha = self.commit_file("settings.py", f"TOKEN = '{GITHUB_TOKEN}'\n", "add settings")
        self.commit_file("settings.py", "TOKEN = os.environ['TOKEN']\n", "read the token from the environment")
        code, report = self.run_hygiene(base="main")
        self.assertNotEqual(code, 0, report)
        self.assertIn("PASS contents", report)
        self.assertIn("FAIL commits", report)
        self.assertIn(f"commit {sha}:settings.py:1: secret", report)

    def test_forbidden_files_and_binaries_in_an_intermediate_commit_still_fail(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "feature")
        (self.repo / "blob.bin").write_bytes(b"\x00\x01\x02" * 40)
        sha = self.commit_file(".env", "TOKEN=x\n", "add env and a binary")
        git(self.repo, "rm", "-q", ".env", "blob.bin")
        git(self.repo, "commit", "-q", "-m", "remove them again")
        code, report = self.run_hygiene(base="main")
        self.assertNotEqual(code, 0, report)
        self.assertIn("PASS tracked-layout", report)
        self.assertIn("PASS binaries", report)
        self.assertIn(f"commit {sha}: .env: environment files are never tracked", report)
        self.assertIn(f"commit {sha}: blob.bin: binary file not in the allowlist", report)

    def test_commit_identities_are_screened(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "feature")
        (self.repo / "a.md").write_text("fine\n")
        git(self.repo, "add", "-A")
        git(
            self.repo,
            "commit",
            "-q",
            "-m",
            "identity",
            env={"GIT_AUTHOR_EMAIL": REAL_EMAIL, "GIT_COMMITTER_NAME": "Mallory Example"},
        )
        private = Path(self.temp.name) / "private"
        private.mkdir()
        (private / "private-words.txt").write_text("mallory\n")
        code, report = self.run_hygiene(base="main", private_dir=private)
        self.assertNotEqual(code, 0, report)
        self.assertIn(f": author: email: Example Author <{REAL_EMAIL}>", report)
        self.assertIn(": committer: private-word: Mallory Example <author@example.com>", report)
        self.assertNotIn(": committer: email:", report)
        git(
            self.repo,
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "odd identities",
            env={"GIT_AUTHOR_NAME": f"Dev at {PERSONAL_PATH}", "GIT_COMMITTER_EMAIL": "nobody"},
        )
        code, report = self.run_hygiene(base="main", private_dir=private)
        self.assertIn(f": author: personal-path: Dev at {PERSONAL_PATH} <author@example.com>", report)
        self.assertIn(": committer: email: Example Author <nobody>", report)

    def test_github_merge_commit_identity_is_allowed(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "feature")
        self.commit_file("a.md", "fine\n", "work")
        git(self.repo, "checkout", "-q", "main")
        git(
            self.repo,
            "merge",
            "-q",
            "--no-ff",
            "-m",
            "Merge pull request #1 from example/feature",
            "feature",
            env={"GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com"},
        )
        git(self.repo, "branch", "-q", "-f", "base", "HEAD~1")
        code, report = self.run_hygiene(base="base")
        self.assertEqual(code, 0, report)
        self.assertIn("PASS commits (2 commit(s) beyond base", report)

    def test_explicit_base_that_does_not_resolve_fails(self) -> None:
        code, report = self.run_hygiene(base="no-such-ref")
        self.assertNotEqual(code, 0, report)
        self.assertIn("FAIL commits", report)
        self.assertIn("no-such-ref does not resolve", report)
        code, report = self.run_hygiene(base="main")
        self.assertEqual(code, 0, report)

    def test_private_lists_apply_to_added_lines_and_allow_marker_does_not_cover_them(self) -> None:
        private = Path(self.temp.name) / "private"
        private.mkdir()
        (private / "private-words.txt").write_text("mallory\n")
        git(self.repo, "checkout", "-q", "-b", "feature")
        sha = self.commit_file("a.md", "for mallory  # hygiene:allow\n", "mention")
        self.commit_file("a.md", "for nobody\n", "unmention")
        code, report = self.run_hygiene(base="main", private_dir=private)
        self.assertNotEqual(code, 0, report)
        self.assertIn(f"commit {sha}:a.md:1: private-word", report)

    def test_allow_marker_exempts_a_tracked_line(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "feature")
        self.commit_file("t.py", f"token = '{OPENAI_TOKEN}'  # hygiene:allow test value\n", "shaped")
        code, report = self.run_hygiene(base="main")
        self.assertEqual(code, 0, report)

    def test_private_dir_and_base_come_from_the_environment(self) -> None:
        private = Path(self.temp.name) / "elsewhere"
        private.mkdir()
        (private / "private-words.txt").write_text("mallory\n")
        git(self.repo, "checkout", "-q", "-b", "feature")
        self.commit_file("a.md", "mallory was here\n", "mention")
        with mock.patch.dict(
            os.environ,
            {self.hygiene.PRIVATE_DIR_ENV: str(private), self.hygiene.BASE_ENV: "main"},
        ):
            code, report = self.run_hygiene()
        self.assertNotEqual(code, 0, report)
        self.assertIn(f"INFO private-lists (1 words, 0 fragments from {private})", report)
        self.assertIn("commit ", report)
        self.assertIn("a.md:1: private-word", report)
        with mock.patch.dict(os.environ, {self.hygiene.BASE_ENV: "HEAD"}):
            code, report = self.run_hygiene()
        self.assertIn("SKIP commits (HEAD has no commits beyond HEAD)", report)

    def test_allowlisted_binary_is_string_scanned(self) -> None:
        (self.repo / "fixture.bin").write_bytes(b"\x00\x01 " + REAL_EMAIL.encode() + b" \x00")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "fixture")
        code, report = self.run_hygiene(base="main", binary_allowlist=("fixture.bin",))
        self.assertNotEqual(code, 0)
        self.assertIn("fixture.bin", report)
        self.assertIn("email", report)


class PreverifyScriptTests(unittest.TestCase):
    """The script works on the repository it lives in, so a copy runs in a scratch repo."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="gates-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "scripts" / "gates").mkdir(parents=True)
        script = self.repo / "scripts" / "gates" / "preverify.sh"
        script.write_text((GATES / "preverify.sh").read_text())
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# Example\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial commit")

    def preverify(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        base_env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ["HOME"]}
        base_env.update(env or {})
        return subprocess.run(
            ["sh", "scripts/gates/preverify.sh", *args],
            cwd=self.repo,
            env=base_env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_dirty_tree_is_refused_before_anything_runs(self) -> None:
        (self.repo / "scratch.txt").write_text("uncommitted\n")
        result = self.preverify()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("working tree is dirty", result.stderr)
        self.assertFalse((self.repo / "local" / "preverify").exists())

    def test_explicit_base_that_does_not_resolve_is_refused(self) -> None:
        result = self.preverify(env={"GATES_BASE": "no-such-ref"})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("GATES_BASE=no-such-ref does not resolve", result.stderr)
        self.assertFalse((self.repo / "local" / "preverify").exists())

    def test_unknown_argument_is_a_usage_error(self) -> None:
        result = self.preverify("--bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)


class LintBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lint = load("lint")

    def test_aggregate_counts_per_file_and_code(self) -> None:
        findings = [
            {"filename": "/r/a.py", "code": "F401"},
            {"filename": "/r/a.py", "code": "F401"},
            {"filename": "/r/b.py", "code": "E701"},
        ]
        counts = self.lint.aggregate(findings, repo=Path("/r"))
        self.assertEqual(counts, {("a.py", "F401"): 2, ("b.py", "E701"): 1})

    def test_compare_flags_growth_and_new_pairs_only(self) -> None:
        baseline = {("a.py", "F401"): 2, ("b.py", "E701"): 1}
        self.assertEqual(self.lint.compare({("a.py", "F401"): 1}, baseline), [])
        regressions = self.lint.compare({("a.py", "F401"): 3, ("c.py", "F841"): 1}, baseline)
        self.assertEqual(len(regressions), 2)
        self.assertTrue(any("a.py" in line and "3" in line and "2" in line for line in regressions))
        self.assertTrue(any("c.py" in line and "new" in line for line in regressions))

    def test_headroom_counts_findings_below_the_baseline(self) -> None:
        baseline = {("a.py", "F401"): 2, ("b.py", "E701"): 1}
        self.assertEqual(self.lint.headroom(baseline, baseline), 0)
        self.assertEqual(self.lint.headroom({("a.py", "F401"): 1}, baseline), 2)
        self.assertEqual(self.lint.headroom({("a.py", "F401"): 5}, baseline), 1)

    def test_baseline_round_trips_through_a_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gates-") as temp:
            path = Path(temp) / "baseline.txt"
            counts = {("b.py", "E701"): 1, ("a.py", "F401"): 2}
            self.lint.write_baseline(path, counts)
            self.assertEqual(self.lint.load_baseline(path), counts)
            lines = [line for line in path.read_text().splitlines() if not line.startswith("#")]
            self.assertEqual(lines[0].split("\t")[1], "a.py")

    def test_current_tree_baseline_matches_the_committed_file(self) -> None:
        baseline = self.lint.load_baseline(GATES / "ruff-baseline.txt")
        self.assertTrue(baseline)
        self.assertNotIn(("tests/substrate/test_gates.py", "F401"), baseline)


class WorkflowPinTests(unittest.TestCase):
    SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"

    def setUp(self) -> None:
        self.lint = load("lint")

    def _findings(self, workflow: str) -> list[str]:
        with tempfile.TemporaryDirectory(prefix="gates-") as temp:
            repo = Path(temp)
            workflows = repo / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "ci.yml").write_text(workflow)
            return self.lint.workflow_pin_findings(repo)

    def test_sha_pin_with_version_comment_passes(self) -> None:
        self.assertEqual(
            self._findings(
                f"steps:\n  - uses: actions/checkout@{self.SHA} # v7.0.1\n"
                f"  - uses: 'owner/repo/sub/dir@{self.SHA}'  #v1\n"
            ),
            [],
        )

    def test_tag_and_branch_refs_fail_with_the_line_named(self) -> None:
        findings = self._findings(
            "steps:\n  - uses: actions/checkout@v7\n  - uses: astral-sh/setup-uv@v10.2.0\n"
            "  - uses: owner/repo@main\n"
        )
        self.assertEqual(len(findings), 3)
        self.assertTrue(findings[0].startswith(".github/workflows/ci.yml:2:"))
        self.assertIn("actions/checkout@v7", findings[0])
        self.assertIn("not pinned to a commit SHA", findings[1])

    def test_sha_pin_without_a_version_comment_fails(self) -> None:
        findings = self._findings(f"steps:\n  - uses: actions/checkout@{self.SHA}\n")
        self.assertEqual(len(findings), 1)
        self.assertIn("version comment", findings[0])
        findings = self._findings(f"steps:\n  - uses: actions/checkout@{self.SHA} # latest\n")
        self.assertEqual(len(findings), 1)

    def test_local_actions_and_non_uses_lines_are_ignored(self) -> None:
        self.assertEqual(
            self._findings("steps:\n  - uses: ./.github/actions/setup\n  - run: echo uses: x@v1\n"),
            [],
        )

    def test_missing_workflows_dir_is_clean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gates-") as temp:
            self.assertEqual(self.lint.workflow_pin_findings(Path(temp)), [])

    def test_committed_workflows_are_pinned(self) -> None:
        self.assertEqual(self.lint.workflow_pin_findings(REPO_ROOT), [])


class MakefileTests(unittest.TestCase):
    def test_documented_gate_targets_exist(self) -> None:
        text = (REPO_ROOT / "Makefile").read_text()
        phony = re.search(r"^\.PHONY:(.*)$", text, re.M).group(1).split()
        for target in ("help", "gate", "hygiene", "lint", "test", "preverify"):
            self.assertIn(target, phony)
            self.assertRegex(text, rf"(?m)^{target}:", f"target {target} is not defined")
        self.assertRegex(text, r"(?m)^\.NOTPARALLEL:")
        self.assertIn("scripts/gates/hygiene.py", text)
        self.assertIn("scripts/gates/lint.py", text)
        self.assertIn("scripts/gates/preverify.sh $(PREVERIFY_ARGS)", text)
        self.assertIn("--frozen --extra test python -m unittest discover -s tests -t .", text)


if __name__ == "__main__":
    unittest.main()
