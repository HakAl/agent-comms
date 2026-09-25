"""Read-only Git evidence behind the ``agent_comms.review`` facade.

Argument-vector Git subprocess helpers; checkout, HEAD, branch, clean-tree,
commit, ancestry, and common-directory observations; and the pinned patch
bytes, offset-neutral canonicalization/digest, and stable patch identity.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from agent_comms.reviewing.contracts import ReviewError


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode != 0:
        raise ReviewError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def git_head(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD")


INTEGRATION_CHECKOUT_ENV = "AGENT_COMMS_MAIN"


def integration_checkout() -> Path:
    """The clean main-branch checkout that cycle-land merges reviewed work into.

    Named by ``AGENT_COMMS_MAIN``. There is no default: the project under
    review is operator data, and the old fallback to the agent-comms source
    tree only ever described the maintainer's own setup.
    """
    configured = os.environ.get(INTEGRATION_CHECKOUT_ENV, "").strip()
    if not configured:
        raise ReviewError(
            f"{INTEGRATION_CHECKOUT_ENV} is not set; export it as the absolute path of the "
            "clean main-branch checkout that cycle-land merges reviewed work into"
        )
    return Path(configured).expanduser().resolve()


def git_proc(repo: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ReviewError(
            f"git {' '.join(args)} could not run in {repo}: {exc}"
        ) from exc


def require_git_checkout(context: str, label: str, path: Path) -> None:
    if not path.is_dir():
        raise ReviewError(
            f"{context}: {label} {path} is not a git checkout (directory missing)"
        )
    proc = git_proc(path, "rev-parse", "--is-inside-work-tree")
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        raise ReviewError(
            f"{context}: {label} {path} is not a git checkout: {proc.stderr.strip()}"
        )


def resolve_head(context: str, label: str, path: Path) -> str:
    proc = git_proc(path, "rev-parse", "--verify", "HEAD^{commit}")
    if proc.returncode != 0:
        raise ReviewError(
            f"{context}: {label} {path} HEAD is unresolvable: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def require_clean_tree(context: str, label: str, path: Path) -> None:
    proc = git_proc(path, "status", "--porcelain")
    if proc.returncode != 0:
        raise ReviewError(
            f"{context}: could not read {label} {path} status: {proc.stderr.strip()}"
        )
    if proc.stdout.strip():
        raise ReviewError(f"{context}: {label} {path} working tree is dirty")


def commit_in_repo(path: Path, sha: str) -> bool:
    return git_proc(path, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def git_bytes(
    context: str, repo: Path, *args: str, env: dict[str, str] | None = None
) -> bytes:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ReviewError(
            f"{context}: git {' '.join(args)} could not run in {repo}: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise ReviewError(
            f"{context}: git {' '.join(args)} failed in {repo}: {proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return proc.stdout


CANONICAL_DIFF_ALGORITHM = "git-patch-offset-neutral-v1"

# Position-dependent patch metadata, matched with re.fullmatch against one
# LF-terminated physical line. The index mode group is optional: present
# byte-for-byte or absent with no extra space. A hunk header may omit either
# count (meaning 1) and may carry a function/context suffix.
_INDEX_LINE_RE = re.compile(rb"index ([0-9a-f]+)\.\.([0-9a-f]+)( [0-7]{6})?\n")
_HUNK_HEADER_RE = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n")
_OID_SENTINEL = b"!oid!"


def _git_patch_env() -> dict[str, str]:
    """Child environment for every pinned patch stream and patch-id call.

    GIT_DIFF_OPTS can inject presentation options (such as -u0) that no
    command-line flag cancels, so it is removed; every other pinned knob is
    outranked by the explicit command-line flags and -c overrides."""
    env = dict(os.environ)
    env.pop("GIT_DIFF_OPTS", None)
    return env


def pinned_patch_bytes(context: str, repo: Path, base: str, head: str) -> bytes:
    """Raw patch bytes under a fully pinned, driverless Git presentation.

    One helper produces the stream both identity factors consume: the
    canonical digest hashes it after metadata canonicalization and the
    stable patch identity hashes it unmodified. Pagers, external diff
    drivers, textconv, rename detection, color, order files, the indent
    heuristic, prefix/quoting config, and the text/binary size threshold
    are all pinned, so neither repository nor ambient configuration can
    change the bytes. Git attribute sources deliberately stay live:
    attribute-driven representation drift must refuse downstream, never
    silently normalize.
    """
    return git_bytes(
        context,
        repo,
        "--no-pager",
        "-c",
        "core.quotePath=true",
        "-c",
        "diff.suppressBlankEmpty=false",
        "-c",
        "core.bigFileThreshold=512m",
        "diff-tree",
        "-r",
        "-p",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--full-index",
        "--unified=3",
        "--no-color",
        "--no-commit-id",
        "--diff-algorithm=myers",
        "--no-indent-heuristic",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "-O/dev/null",
        base,
        head,
        env=_git_patch_env(),
    )


def canonicalize_patch_bytes(context: str, patch: bytes) -> bytes:
    """Neutralize only position-dependent patch metadata, byte-oriented.

    A physical line is a byte sequence terminated by 0x0a; splitting is on
    b"\\n" only, so 0x0d and every other byte stay inside their containing
    line. A line is candidate metadata only when it begins with b"index "
    or b"@@ " at offset zero; hunk-body lines always carry their
    context/add/delete prefix and binary-patch payload lines cannot begin
    with either prefix. A candidate that fails its fullmatch grammar
    refuses rather than passing position-dependent bytes through as
    canonical. Every other byte is left unchanged.
    """
    pieces = patch.split(b"\n")
    tail = pieces.pop()
    out = bytearray()
    for piece in pieces:
        line = piece + b"\n"
        if line.startswith(b"index "):
            match = _INDEX_LINE_RE.fullmatch(line)
            if match is None:
                raise ReviewError(
                    f"{context}: unrecognized index metadata line in patch stream"
                )
            out += (
                b"index "
                + _OID_SENTINEL
                + b".."
                + _OID_SENTINEL
                + (match.group(3) or b"")
                + b"\n"
            )
        elif line.startswith(b"@@ "):
            match = _HUNK_HEADER_RE.fullmatch(line)
            if match is None:
                raise ReviewError(
                    f"{context}: unrecognized hunk header in patch stream"
                )
            out += (
                b"@@ -"
                + (match.group(2) or b"1")
                + b" +"
                + (match.group(4) or b"1")
                + b" @@\n"
            )
        else:
            out += line
    if tail.startswith(b"index ") or tail.startswith(b"@@ "):
        raise ReviewError(
            f"{context}: unterminated candidate metadata line in patch stream"
        )
    out += tail
    return bytes(out)


def canonical_diff_digest(context: str, repo: Path, base: str, head: str) -> str:
    return hashlib.sha256(
        canonicalize_patch_bytes(context, pinned_patch_bytes(context, repo, base, head))
    ).hexdigest()


def stable_patch_id(context: str, repo: Path, patch: bytes) -> str | None:
    """Aggregate `git patch-id --stable` over the pinned raw patch bytes,
    or None when Git cannot represent the diff as a patch identity."""
    if not patch.strip():
        return None
    try:
        proc = subprocess.run(
            ["git", "-c", "patchid.verbatim=false", "patch-id", "--stable"],
            cwd=str(repo),
            input=patch,
            env=_git_patch_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ReviewError(
            f"{context}: git patch-id could not run in {repo}: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise ReviewError(
            f"{context}: git patch-id failed in {repo}: {proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if not out:
        return None
    return out.split()[0]


def is_ancestor(context: str, repo: Path, ancestor: str, descendant: str) -> bool:
    proc = git_proc(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise ReviewError(
        f"{context}: git merge-base --is-ancestor {ancestor} {descendant} failed in {repo}: {proc.stderr.strip()}"
    )


def git_common_dir(path: Path) -> Path | None:
    """Absolute ``.git`` common directory for ``path``, or None if not a repo.

    Repository identity is detected from this shared object store, never from
    branch names or path prefixes: a review worktree and the integration
    checkout that are linked worktrees of one repository resolve to the same
    common dir even though their branches and paths differ.
    """
    proc = git_proc(path, "rev-parse", "--git-common-dir")
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    common = Path(raw)
    if not common.is_absolute():
        common = Path(path) / common
    try:
        return common.resolve()
    except OSError:
        return None


def git_branch(repo: Path) -> str:
    branch = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    return branch or "HEAD"


# --- Reply-snapshot custody primitives (review evidence lifecycle Landing 2) ---
# Reusable low-level custody for reviewing.reply_snapshots: no-follow directory
# retention/revalidation, a fchdir/exec Git child bound to a retained worktree
# descriptor, and a bounded no-follow regular-file digest. No snapshot
# classification, sequencing, SQL, or lifecycle policy lives here; the caller
# sequences these and the review facade above is unchanged.
_GIT_ENV_OVERRIDES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)
# A fresh same-interpreter child binds the retained checkout descriptor with
# fchdir before exec of Git, so worktree reads follow the retained inode and never
# a re-resolved pathname. No preexec_fn; the server process cwd is untouched.
_FCHDIR_EXEC_CHILD = (
    "import os,sys\nos.fchdir(int(sys.argv[1]))\nos.execv(sys.argv[2], sys.argv[2:])\n"
)
INDEX_DIGEST_MAX_BYTES = 64 * 1024 * 1024
INDEX_DIGEST_ABSENT = "absent"


class GitCustodyError(Exception):
    """A refused custody step; the caller maps it to retryable instability."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def clean_git_env() -> dict[str, str]:
    """Ambient env minus the Git overrides a caller sets explicitly, locks off."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_OVERRIDES}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def open_retained_dir(path) -> tuple[int, tuple[int, int]]:
    """Open a directory's final component no-follow; return (fd, (dev, ino))."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise GitCustodyError("git_custody_open_failed", f"{path}: {exc}") from exc
    st = os.fstat(fd)
    return fd, (st.st_dev, st.st_ino)


def revalidate_retained(path, identity: tuple[int, int]) -> None:
    """Refuse unless ``path`` still resolves to the retained inode identity."""
    try:
        st = os.stat(os.fspath(path))
    except OSError as exc:
        raise GitCustodyError("git_custody_gone", f"{path}: {exc}") from exc
    if (st.st_dev, st.st_ino) != identity:
        raise GitCustodyError("git_custody_identity_drift", str(path))


def run_git_fchdir(worktree_fd: int, args: list[str], env: dict[str, str]) -> bytes:
    """Run one Git child that fchdirs into ``worktree_fd`` before exec of Git."""
    git_exe = shutil.which("git")
    if git_exe is None:
        raise GitCustodyError("git_custody_git_missing")
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                _FCHDIR_EXEC_CHILD,
                str(worktree_fd),
                git_exe,
                *args,
            ],
            env=env,
            pass_fds=(worktree_fd,),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise GitCustodyError("git_custody_spawn_failed", str(exc)) from exc
    if proc.returncode:
        raise GitCustodyError(
            "git_custody_command_failed", proc.stderr.decode(errors="replace").strip()
        )
    return proc.stdout


def bounded_regular_file_digest(
    path, *, dir_fd: int | None = None, max_bytes: int = INDEX_DIGEST_MAX_BYTES
) -> str:
    """SHA-256 of a regular file streamed from one no-follow, nonblocking fd.

    When ``dir_fd`` is given, ``path`` is opened descriptor-relative to that
    retained directory (``openat``), so a concurrently renamed or replaced
    directory pathname cannot redirect the read to another file; callers pass the
    already retained Git-dir FD and the bare ``index`` name. Missing returns
    ``absent``. A symlink/FIFO/device/socket/directory (rejected by the open or
    fstat), oversize, or device/inode/total-size/``st_mtime_ns``/``st_ctime_ns``
    drift across the read refuses. ``st_ctime_ns`` is load-bearing: a same-size
    in-place mutation by the owner cannot be masked, because ``utime`` cannot
    restore ``ctime``; ``st_mtime_ns`` is defense in depth.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return INDEX_DIGEST_ABSENT
    except OSError as exc:  # ELOOP (symlink), ENXIO (device), EWOULDBLOCK, ...
        raise GitCustodyError("git_index_unreadable", f"{path}: {exc}") from exc
    try:
        st0 = os.fstat(fd)
        if not stat.S_ISREG(st0.st_mode):
            raise GitCustodyError("git_index_not_regular", str(path))
        if st0.st_size > max_bytes:
            raise GitCustodyError("git_index_oversized", str(st0.st_size))
        digest, total = hashlib.sha256(), 0
        while True:
            try:
                chunk = os.read(fd, 1 << 20)
            except BlockingIOError as exc:
                raise GitCustodyError("git_index_would_block", str(path)) from exc
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise GitCustodyError("git_index_grew", str(total))
            digest.update(chunk)
        st1 = os.fstat(fd)
        if (
            (st1.st_dev, st1.st_ino) != (st0.st_dev, st0.st_ino)
            or st1.st_size != total
            or st1.st_mtime_ns != st0.st_mtime_ns
            or st1.st_ctime_ns != st0.st_ctime_ns
        ):
            raise GitCustodyError("git_index_raced", f"{total}/{st0.st_size}")
        return digest.hexdigest()
    finally:
        os.close(fd)
