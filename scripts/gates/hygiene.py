#!/usr/bin/env python3
"""Hygiene gate: keep personal data and secrets out of a public push.

Scans every tracked file (and the printable strings of allowlisted binaries)
plus the commit range that a push would publish: for every commit beyond the
base, the commit message, the author and committer identities, every added
line of its diff, and the layout and binary rules for the files it adds or
changes. Scanning each commit, not just the tree at HEAD, is what catches a
secret that an intermediate commit introduced and a later commit removed;
that secret would still be in the pushed history.

All checks are public-safe; they need nothing from any maintainer's machine.
When a private word or fragment list is present under ``local/gates``
(gitignored) it is applied too, and its absence is printed as a skip, never
hidden. A line that carries the marker ``hygiene:allow`` is exempt from the
pattern checks (paths, emails, hostnames, secrets) but never from the
private lists.

Environment: ``GATES_PRIVATE_DIR`` overrides the private list directory,
``GATES_BASE`` the commit range base; a base given explicitly must resolve,
only the defaults may be absent. Exit status is nonzero when any check fails.
Run through ``make hygiene``.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, TextIO

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_DIR_ENV = "GATES_PRIVATE_DIR"
BASE_ENV = "GATES_BASE"
PRIVATE_WORDS_FILE = "private-words.txt"
PRIVATE_FRAGMENTS_FILE = "private-fragments.txt"
ALLOW_MARKER = "hygiene:allow"

SKIPPED_FILES = frozenset({"COPYING", "LICENSE"})
FORBIDDEN_DIRS = ("data/", "logs/", "local/", ".claude/", ".beads/")
CONFIG_DIR = "config/"
DEFAULT_BINARY_ALLOWLIST: tuple[str, ...] = ("tests/fixtures/v0_1_0/agent-comms.sqlite",)
DEFAULT_BASES = ("origin/main", "main")

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # The character class keeps the literal out of the absolute-path grep gate
    # in tests/substrate/test_path_relativization.py.
    ("personal-path", re.compile(r"/U[s]ers/[A-Za-z0-9._-]+|/home/[a-z_][a-z0-9_-]*/")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("hostname", re.compile(r"\b[A-Za-z0-9-]+\.local\b(?!\.)")),
    (
        "secret",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
            r"|ssh-(?:ed25519|rsa|dss|ecdsa[a-z0-9-]*) AAAA[0-9A-Za-z+/]{20,}"
            r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
            r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
            r"|\bsk-[A-Za-z0-9_-]{20,}"
            r"|\bAKIA[0-9A-Z]{16}\b"
            r"|\bAIza[0-9A-Za-z_-]{35}"
            r"|\bhf_[A-Za-z0-9]{20,}"
            r"|\bxox[abpr]-[A-Za-z0-9-]{10,}"
        ),
    ),
)
# Reserved names (RFC 2606 and RFC 6761) that can never receive mail.
EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")
RESERVED_TLDS = (".invalid", ".test", ".example", ".localhost")
# ``git@host`` is an ssh remote, not a mailbox.
ALLOWED_LOCAL_PARTS = frozenset({"git"})

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    check: str
    excerpt: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.check}: {self.excerpt}"


def _allowed_email(address: str) -> bool:
    local_part, domain = address.rsplit("@", 1)
    domain = domain.lower()
    if local_part.lower() in ALLOWED_LOCAL_PARTS:
        return True
    if domain in EXAMPLE_DOMAINS or domain.endswith(tuple("." + d for d in EXAMPLE_DOMAINS)):
        return True
    if domain.endswith(RESERVED_TLDS):
        return True
    return "noreply" in domain


def _excerpt(line: str) -> str:
    text = line.strip()
    return text if len(text) <= 120 else text[:117] + "..."


def _compile_private(
    private_words: Sequence[str], private_fragments: Sequence[str]
) -> tuple[list[re.Pattern[str]], list[str]]:
    word_patterns = [
        re.compile(r"\b" + re.escape(word.strip()) + r"\b", re.IGNORECASE)
        for word in private_words
        if word.strip()
    ]
    fragments = [fragment.strip().lower() for fragment in private_fragments if fragment.strip()]
    return word_patterns, fragments


def _scan_line(
    line: str,
    *,
    path: str,
    number: int,
    word_patterns: Sequence[re.Pattern[str]],
    fragments: Sequence[str],
) -> list[Finding]:
    findings: list[Finding] = []
    if ALLOW_MARKER not in line:
        for check, pattern in PATTERNS:
            for match in pattern.finditer(line):
                if check == "email" and _allowed_email(match.group(0)):
                    continue
                findings.append(Finding(path, number, check, _excerpt(line)))
                break
    if any(pattern.search(line) for pattern in word_patterns):
        findings.append(Finding(path, number, "private-word", _excerpt(line)))
    lowered = line.lower()
    if any(fragment in lowered for fragment in fragments):
        findings.append(Finding(path, number, "private-fragment", _excerpt(line)))
    return findings


def scan_text(
    text: str,
    *,
    path: str,
    private_words: Sequence[str] = (),
    private_fragments: Sequence[str] = (),
) -> list[Finding]:
    """Findings for one text, one per check per line."""
    word_patterns, fragments = _compile_private(private_words, private_fragments)
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), 1):
        findings += _scan_line(
            line, path=path, number=number, word_patterns=word_patterns, fragments=fragments
        )
    return findings


def scan_patch(
    patch: str,
    *,
    label: str,
    private_words: Sequence[str] = (),
    private_fragments: Sequence[str] = (),
) -> list[Finding]:
    """Findings for the added lines of a unified diff.

    ``label`` prefixes every path (``<label>:<file>``); line numbers are those
    of the file after the change. Files named in ``SKIPPED_FILES`` are skipped
    like they are in the tree scan.
    """
    word_patterns, fragments = _compile_private(private_words, private_fragments)
    findings: list[Finding] = []
    path: str | None = None
    number = 0
    in_hunk = False  # file headers appear only between "diff --git" and the first hunk
    for raw in patch.splitlines():
        if raw.startswith("diff --git "):
            in_hunk = False
            path = None
            continue
        header = HUNK_HEADER.match(raw)
        if header:
            in_hunk = True
            number = int(header.group(1))
            continue
        if not in_hunk:
            if raw.startswith("+++ "):
                target = raw[4:]
                if target.startswith('"') and target.endswith('"'):
                    target = target[1:-1]
                if target.startswith("b/"):
                    target = target[2:]
                path = None if target == "/dev/null" else target
            continue
        if not raw.startswith("+"):
            continue
        line = raw[1:]
        if path is not None and Path(path).name not in SKIPPED_FILES:
            findings += _scan_line(
                line,
                path=f"{label}:{path}",
                number=number,
                word_patterns=word_patterns,
                fragments=fragments,
            )
        number += 1
    return findings


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout if result.returncode == 0 else ""


def tracked_files(repo: Path) -> list[str]:
    return [name for name in git(repo, "ls-files", "-z").split("\0") if name]


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def printable_strings(data: bytes) -> str:
    return "\n".join(match.group(0).decode("ascii") for match in re.finditer(rb"[\x20-\x7e]{6,}", data))


def load_private_lists(directory: Path | None) -> tuple[list[str], list[str]] | None:
    if directory is None:
        return None
    words_path = directory / PRIVATE_WORDS_FILE
    fragments_path = directory / PRIVATE_FRAGMENTS_FILE
    if not words_path.exists() and not fragments_path.exists():
        return None
    words = words_path.read_text().splitlines() if words_path.exists() else []
    fragments = fragments_path.read_text().splitlines() if fragments_path.exists() else []
    return [w for w in words if w.strip()], [f for f in fragments if f.strip()]


def resolve_base(repo: Path, base: str | None) -> str | None:
    candidates = (base,) if base else DEFAULT_BASES
    for candidate in candidates:
        if git(repo, "rev-parse", "--verify", "-q", f"{candidate}^{{commit}}", check=False).strip():
            return candidate
    return None


def commits_beyond(repo: Path, base: str) -> list[dict[str, str]]:
    raw = git(repo, "log", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e", f"{base}..HEAD")
    commits = []
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        sha, author, author_email, committer, committer_email, body = record.lstrip("\n").split("\x1f", 5)
        commits.append(
            {
                "sha": sha,
                "author": author,
                "author_email": author_email,
                "committer": committer,
                "committer_email": committer_email,
                "body": body,
            }
        )
    return commits


def commit_patch(repo: Path, sha: str) -> str:
    """Unified diff of one commit against its first parent, no context lines."""
    return git(
        repo,
        "show",
        "--format=",
        "--unified=0",
        "--no-color",
        "--diff-merges=first-parent",
        sha,
    )


def commit_files(repo: Path, sha: str) -> list[tuple[str, bool]]:
    """(path, is_binary) for every file the commit adds or modifies."""
    raw = git(
        repo,
        "show",
        "--format=",
        "--numstat",
        "--no-renames",
        "--diff-filter=AM",
        "--diff-merges=first-parent",
        sha,
    )
    files = []
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            files.append((parts[2], parts[0] == "-" and parts[1] == "-"))
    return files


def _layout_violation(name: str) -> str | None:
    if name.startswith(FORBIDDEN_DIRS):
        return f"{name}: tracked under a forbidden directory"
    if name.startswith(CONFIG_DIR) and ".example" not in Path(name).name:
        return f"{name}: config/ may only hold *.example* files"
    base = Path(name).name
    if base == ".env" or base.startswith(".env."):
        return f"{name}: environment files are never tracked"
    return None


def _report(out: TextIO, name: str, findings: Sequence[str], detail: str = "") -> bool:
    if findings:
        out.write(f"FAIL {name}: {len(findings)} finding(s)\n")
        for finding in findings:
            out.write(f"  {finding}\n")
        return False
    out.write(f"PASS {name}{f' ({detail})' if detail else ''}\n")
    return True


def check_repo(
    repo: Path,
    *,
    out: TextIO = sys.stdout,
    base: str | None = None,
    private_dir: Path | None = None,
    binary_allowlist: Sequence[str] = DEFAULT_BINARY_ALLOWLIST,
) -> int:
    ok = True
    files = tracked_files(repo)

    layout = [violation for violation in map(_layout_violation, files) if violation]
    ok &= _report(out, "tracked-layout", layout, f"{len(files)} tracked files")

    if private_dir is None:
        env_dir = os.environ.get(PRIVATE_DIR_ENV)
        private_dir = Path(env_dir) if env_dir else repo / "local" / "gates"
    lists = load_private_lists(private_dir)
    if lists is None:
        out.write(f"SKIP private-lists (no {PRIVATE_WORDS_FILE} or {PRIVATE_FRAGMENTS_FILE} in {private_dir})\n")
        words: list[str] = []
        fragments: list[str] = []
    else:
        words, fragments = lists
        out.write(f"INFO private-lists ({len(words)} words, {len(fragments)} fragments from {private_dir})\n")

    contents: list[str] = []
    binaries: list[str] = []
    for name in files:
        if Path(name).name in SKIPPED_FILES:
            continue
        full = repo / name
        if full.is_symlink() or not full.is_file():
            continue
        data = full.read_bytes()
        if is_binary(data):
            if name in binary_allowlist:
                text = printable_strings(data)
            else:
                binaries.append(f"{name}: binary file not in the allowlist")
                continue
        else:
            text = data.decode("utf-8", errors="replace")
        contents += [
            f.render()
            for f in scan_text(text, path=name, private_words=words, private_fragments=fragments)
        ]
    ok &= _report(out, "contents", contents)
    ok &= _report(out, "binaries", binaries, f"allowlist: {', '.join(binary_allowlist) or 'none'}")

    if base is None:
        base = os.environ.get(BASE_ENV) or None
    base_ref = resolve_base(repo, base)
    if base_ref is None and base:
        ok &= _report(out, "commits", [f"base {base} does not resolve to a commit in {repo}"])
    elif base_ref is None:
        out.write(f"SKIP commits (no base ref among {', '.join(DEFAULT_BASES)})\n")
    else:
        commits = commits_beyond(repo, base_ref)
        if not commits:
            out.write(f"SKIP commits (HEAD has no commits beyond {base_ref})\n")
        else:
            commit_findings: list[str] = []
            for commit in commits:
                label = f"commit {commit['sha'][:7]}"
                for role in ("author", "committer"):
                    email = commit[f"{role}_email"]
                    identity = f"{commit[role]} <{email}>"
                    if email.lower().endswith(".local"):
                        commit_findings.append(f"{label}: {role}: hostname: {identity}")
                    elif "@" not in email or not _allowed_email(email):
                        commit_findings.append(f"{label}: {role}: email: {identity}")
                    commit_findings += [
                        f"{label}: {role}: {f.check}: {identity}"
                        for f in scan_text(
                            identity, path=label, private_words=words, private_fragments=fragments
                        )
                        if f.check != "email"  # the address itself was judged above
                    ]
                for name, binary in commit_files(repo, commit["sha"]):
                    violation = _layout_violation(name)
                    if violation:
                        commit_findings.append(f"{label}: {violation}")
                    if binary and name not in binary_allowlist and Path(name).name not in SKIPPED_FILES:
                        commit_findings.append(f"{label}: {name}: binary file not in the allowlist")
                commit_findings += [
                    f.render()
                    for f in scan_text(
                        commit["body"], path=label, private_words=words, private_fragments=fragments
                    )
                ]
                commit_findings += [
                    f.render()
                    for f in scan_patch(
                        commit_patch(repo, commit["sha"]),
                        label=label,
                        private_words=words,
                        private_fragments=fragments,
                    )
                ]
            ok &= _report(
                out,
                "commits",
                commit_findings,
                f"{len(commits)} commit(s) beyond {base_ref}: messages, identities, added lines, layout",
            )

    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--base", help=f"commit range base (default: ${BASE_ENV}, then origin/main, then main)")
    parser.add_argument("--private-dir", help=f"directory with the private lists (default: ${PRIVATE_DIR_ENV} or local/gates)")
    args = parser.parse_args(argv)
    return check_repo(
        Path(args.repo).resolve(),
        base=args.base,
        private_dir=Path(args.private_dir) if args.private_dir else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
