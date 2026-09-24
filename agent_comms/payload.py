"""Deterministic dispatch payload transport: capture, CAS, load, and audit.

This module owns the file-backed logical-body mode for dispatch creation:
single-FD stable source capture, create-only durable SHA-256 content-addressed
publication, exact verification/load for the shared spawn preflight and
``read_message`` resolution, and the read-only store audit. It is stdlib-only
and holds no SQL lifecycle authority: the additive ``dispatch_payload_refs``
row (inserted by the dispatch transaction) is the only artifact-backed routing
discriminator, and the SQL ledger remains lifecycle authority.

Exact payload text is never placed in error messages, logs, or audit output;
failures report expected and observed identity values (codes, digests, sizes)
only.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .schema import ValidationError

# Contract constants (design docs/dispatch/dispatch_payload_artifacts.md §3.2).
# They are deliberately not environment variables or per-call escape hatches.
INLINE_BODY_MAX_BYTES = 8_192
FILE_BODY_MAX_BYTES = 262_144
STORAGE_KIND = "sha256_utf8_v1"
PAYLOAD_ORIGINS = ("authored_brief", "generated_artifact", "verbatim_source")

# Fixed refusal-safe compatibility marker stored in ``messages.body`` for an
# artifact-backed dispatch. Informational only: no code parses it, and an
# inline body equal to it remains inline because it has no payload row.
ARTIFACT_BODY_MARKER = (
    "[artifact-backed dispatch body] This dispatch stores its logical body in "
    "the agent-comms dispatch payload store. Read it with a payload-compatible "
    "read_message(message_id); this marker is not the payload."
)

# Fixed artifact-backed snippet for bounded listings (list_inbox and
# wait_for_reply(full=True)). Exact resolution is exclusive to read_message.
ARTIFACT_SNIPPET = (
    "[artifact-backed dispatch body: call read_message(message_id) for the "
    "exact verified payload]"
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Closed store layout names. A shard is the first two hex characters of a
# digest; a staging entry is created exclusively as ``uuid4().hex``. Any other
# name at those levels is layout corruption, classified from the name alone.
_SHARD_NAME_RE = re.compile(r"^[0-9a-f]{2}$")
_STAGING_NAME_RE = re.compile(r"^[0-9a-f]{32}$")

# Canonical stored timestamp shape: exactly what ``clock.utc_now()`` writes,
# an aware UTC ISO-8601 timestamp at second precision with a literal +00:00
# offset. ``captured_at`` has no SQL shape CHECK, so this is the only guard.
_CAPTURED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
_CAPTURED_AT_STRPTIME = "%Y-%m-%dT%H:%M:%S+00:00"

# Post-read stability seam: tests patch this to prove the unstable-source
# refusal without racing a real writer. Production behavior is ``os.fstat``.
_fstat_after_read = os.fstat

# Component-walk seam: tests patch this to mutate the source tree between
# verified component opens and prove the dir_fd walk cannot be redirected
# mid-walk. Called with the root-relative slash-joined path of the component
# about to be opened. Production behavior is a no-op.
def _before_component_open(rel_so_far: str) -> None:
    return None


# Publish-link seam: tests patch this to swap the verified shard directory for
# a symlink after the shard directory FD is retained but before ``os.link``,
# proving the blob is created relative to the retained shard FD and cannot be
# redirected outside the sibling store mid-publish. Called with the
# store-root-relative slash-joined blob path. Production behavior is a no-op.
def _before_final_link(rel_path: str) -> None:
    return None


# Load-open seam: tests patch this to swap the verified shard directory after
# the shard directory FD is retained but before the leaf open, proving the read
# resolves relative to the retained shard FD and cannot be redirected outside
# the sibling store, and to race the leaf itself (FIFO/non-regular swap,
# mutation, growth) between its lstat and its open, proving the bounded
# FD-authoritative read refuses promptly. Called with the store-root-relative
# slash-joined blob path. Production behavior is a no-op.
def _before_load_open(rel_path: str) -> None:
    return None


# Reuse-open seam: tests patch this to race the existing destination leaf
# between the reuse re-verification's lstat and its open (FIFO/non-regular
# swap, mutation, growth), proving existing-blob reuse refuses promptly from
# the authoritative opened FD instead of blocking on a FIFO or reading
# unbounded. Called with the store-root-relative slash-joined blob path.
# Production behavior is a no-op.
def _before_reuse_open(rel_path: str) -> None:
    return None


# Every directory in the walk is opened relative to its verified parent FD and
# must itself be a real directory, never a symlink.
_DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

# Every leaf file open (source capture, existing-blob reuse re-verification,
# and authenticated load/preflight) uses these flags: O_NOFOLLOW so a symlink
# raced into place refuses, O_NONBLOCK so a FIFO raced into place between the
# leaf lstat and the open cannot block waiting for a writer. O_NONBLOCK has no
# effect on the regular-file reads, which are bounded by the expected byte
# count; the opened FD is always fstat-verified before any read.
_LEAF_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

# The store hierarchy is anchored at the DB parent directory, addressed by the
# trusted absolute ledger path (as ``capture_source`` anchors the producer
# source root). Only the store levels below it are policed no-follow; the anchor
# itself may legitimately be reached through system symlinks in its own parents.
_DIR_ANCHOR_FLAGS = os.O_RDONLY | os.O_DIRECTORY

# A symlink hit under ``O_DIRECTORY|O_NOFOLLOW`` surfaces as ELOOP on Linux,
# ENOTDIR on macOS, and EMLINK on some BSDs. EMLINK cannot otherwise occur on
# open(2), so the mapping is safe.
_SYMLINK_OPEN_ERRNOS = frozenset(
    {errno.ELOOP} | ({errno.EMLINK} if hasattr(errno, "EMLINK") else set())
)
_DIR_REFUSAL_ERRNOS = _SYMLINK_OPEN_ERRNOS | {errno.ENOTDIR}


def _is_symlink_diagnostic(path: str | Path, *, dir_fd: int | None = None) -> bool:
    """Diagnostic-only symlink check for a refusal message.

    The refusal decision was already made by a failed ``O_NOFOLLOW`` open;
    this only picks the precise wording and never re-opens anything.
    """
    try:
        st = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode)


class PayloadError(ValidationError):
    """A payload transport refusal carrying a stable failure code.

    ``str(exc)`` always begins with the stable code so ledger failure_reason
    rows and CLI/MCP errors are mechanically greppable.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class PayloadIntegrityError(PayloadError):
    """A post-durable artifact verification failure (preflight or read)."""


@dataclass(frozen=True)
class CapturedPayload:
    data: bytes
    sha256: str
    byte_count: int
    char_count: int


def store_root_for_db(db_path: Path) -> Path:
    """Sibling payload root for a ledger: ``<db-parent>/<db-stem>-dispatch-payloads``."""
    db_path = Path(db_path)
    return db_path.parent / f"{db_path.stem}-dispatch-payloads"


def validate_payload_origin(payload_origin: str) -> str:
    if payload_origin not in PAYLOAD_ORIGINS:
        allowed = ", ".join(PAYLOAD_ORIGINS)
        raise PayloadError(
            "dispatch_payload_origin_invalid",
            f"payload_origin must be one of: {allowed}; got {payload_origin!r}",
        )
    return payload_origin


def validate_sha256_hex(value: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise PayloadIntegrityError(
            "dispatch_payload_metadata_invalid",
            "payload_sha256 must be exactly 64 lowercase hexadecimal characters",
        )
    return value


def blob_path(store_root: Path, sha256_hex: str) -> Path:
    """Digest-derived blob path; the digest is validated, never trusted raw."""
    sha256_hex = validate_sha256_hex(sha256_hex)
    return Path(store_root) / "blobs" / "sha256" / sha256_hex[:2] / sha256_hex


def _ensure_store_dir_fd(parent_fd: int, name: str, *, label: str) -> int:
    """Create (0o700) or open one store level below ``parent_fd`` and retain it.

    The level is created and opened relative to the already-verified parent FD
    with ``O_DIRECTORY|O_NOFOLLOW`` and its FD is returned, so a concurrent
    rename that replaces this level with a symlink after validation cannot
    redirect the walk: the retained FD keeps pointing at the real directory. An
    existing level that is a symlink or not a real directory refuses. When this
    call creates the level, the retained ``parent_fd`` itself is fsynced before
    returning (never a re-opened parent pathname a concurrent rename could have
    replaced), so a freshly created hierarchy is durable before publication or
    the SQL commit that relies on it (one fsync per created level).
    """
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        created = False
    except OSError as exc:
        raise PayloadError(
            "dispatch_payload_publish_failed",
            f"could not create payload store directory {label!r}: {exc.strerror}",
        ) from exc
    try:
        fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in _DIR_REFUSAL_ERRNOS:
            kind = (
                "a symlink"
                if _is_symlink_diagnostic(name, dir_fd=parent_fd)
                else "not a real directory"
            )
            raise PayloadError(
                "dispatch_payload_publish_failed",
                f"payload store level {label!r} is {kind}; "
                "the store hierarchy must be real directories",
            ) from exc
        raise PayloadError(
            "dispatch_payload_publish_failed",
            f"payload store level {label!r} could not be opened: {exc.strerror}",
        ) from exc
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise PayloadError(
            "dispatch_payload_publish_failed",
            f"payload store level {label!r} is not a real directory",
        )
    if created:
        try:
            _fsync_fd(parent_fd)
        except OSError as exc:
            os.close(fd)
            raise PayloadError(
                "dispatch_payload_publish_failed",
                f"could not make payload store directory {label!r} durable: {exc.strerror}",
            ) from exc
    return fd


def _open_store_dir_fd(parent_fd: int, name: str, *, payload_sha256: str) -> int:
    """Open one existing store level below ``parent_fd`` no-follow for load.

    A missing, symlinked, or otherwise unopenable level refuses with
    ``dispatch_payload_missing`` instead of following a swapped parent
    component outside the sibling store; the retained FD is returned for the
    next level so the whole chain is a verified no-follow chain.
    """
    try:
        return os.open(name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise PayloadIntegrityError(
            "dispatch_payload_missing",
            f"payload blob for digest {payload_sha256} is unavailable: {exc.strerror}",
        ) from exc


def capture_source(source_root: Path, body_file: str) -> CapturedPayload:
    """Validate and read one stable source file into bounded memory.

    Path shape, no-symlink component walk, single-FD read, pre/post identity
    and size/timestamp stability, strict UTF-8, no-NUL, non-empty, and the
    file-size cap are all enforced here. The exact captured bytes are hashed
    without newline normalization, trimming, or reserialization.
    """
    if not isinstance(body_file, str) or not body_file.strip():
        raise PayloadError("dispatch_payload_path_invalid", "body_file must be a non-empty relative path")
    rel = Path(body_file)
    if rel.is_absolute():
        raise PayloadError(
            "dispatch_payload_path_invalid",
            f"body_file must be relative to the producer source root, got absolute path {body_file!r}",
        )
    if any(part in ("..", "") for part in rel.parts):
        raise PayloadError(
            "dispatch_payload_path_invalid",
            f"body_file must not contain traversal components: {body_file!r}",
        )
    if not rel.parts:
        # Path(".") and Path("./") normalize to zero components; they name the
        # source root itself, never a file under it.
        raise PayloadError(
            "dispatch_payload_path_invalid",
            f"body_file must name a file under the producer source root: {body_file!r}",
        )

    source_root = Path(source_root)
    if not source_root.is_absolute():
        raise PayloadError(
            "dispatch_payload_path_invalid",
            f"payload source root must be absolute, got {str(source_root)!r}",
        )

    # Open the source root and every directory component relative to its
    # already-verified parent FD (O_DIRECTORY|O_NOFOLLOW), fstat the opened
    # objects, and open the leaf relative to its verified parent FD with
    # O_NOFOLLOW. The final path is never reopened by pathname, so swapping a
    # component to a symlink between opens refuses instead of redirecting the
    # walk outside the source root.
    opened_fds: list[int] = []
    try:
        try:
            root_fd = os.open(source_root, _DIR_OPEN_FLAGS)
        except OSError as exc:
            if exc.errno in _DIR_REFUSAL_ERRNOS:
                kind = (
                    "a symlink" if _is_symlink_diagnostic(source_root) else "not a directory"
                )
                raise PayloadError(
                    "dispatch_payload_path_invalid",
                    f"payload source root {str(source_root)!r} is {kind}; "
                    "the source root must be a real directory",
                ) from exc
            raise PayloadError(
                "dispatch_payload_source_unavailable",
                f"payload source root {str(source_root)!r} could not be opened: {exc.strerror}",
            ) from exc
        opened_fds.append(root_fd)
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise PayloadError(
                "dispatch_payload_path_invalid",
                f"payload source root {str(source_root)!r} is not a directory",
            )

        parent_fd = root_fd
        for index, part in enumerate(rel.parts[:-1]):
            _before_component_open("/".join(rel.parts[: index + 1]))
            try:
                child_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in _DIR_REFUSAL_ERRNOS:
                    if _is_symlink_diagnostic(part, dir_fd=parent_fd):
                        raise PayloadError(
                            "dispatch_payload_source_unavailable",
                            f"body_file component {part!r} is a symlink; symlinked components refuse",
                        ) from exc
                    raise PayloadError(
                        "dispatch_payload_source_unavailable",
                        f"body_file component {part!r} is not a directory",
                    ) from exc
                raise PayloadError(
                    "dispatch_payload_source_unavailable",
                    f"body_file component {part!r} is unavailable under the source root: {exc.strerror}",
                ) from exc
            opened_fds.append(child_fd)
            if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                raise PayloadError(
                    "dispatch_payload_source_unavailable",
                    f"body_file component {part!r} is not a directory",
                )
            parent_fd = child_fd

        leaf = rel.parts[-1]
        _before_component_open("/".join(rel.parts))
        try:
            fd = os.open(leaf, _LEAF_OPEN_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in _SYMLINK_OPEN_ERRNOS:
                raise PayloadError(
                    "dispatch_payload_source_unavailable",
                    f"body_file component {leaf!r} is a symlink; symlinked components refuse",
                ) from exc
            raise PayloadError(
                "dispatch_payload_source_unavailable",
                f"body_file {body_file!r} could not be opened: {exc.strerror}",
            ) from exc
        opened_fds.append(fd)

        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise PayloadError(
                "dispatch_payload_source_unavailable",
                f"body_file {body_file!r} must name a regular file",
            )
        if before.st_size > FILE_BODY_MAX_BYTES:
            raise PayloadError(
                "dispatch_payload_too_large",
                f"file-backed body is limited to {FILE_BODY_MAX_BYTES} bytes; "
                f"observed {before.st_size} bytes",
            )
        chunks: list[bytes] = []
        remaining = FILE_BODY_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = _fstat_after_read(fd)
        # Full stat identity, not just size/mtime: an equal-size same-mtime
        # replacement still changes (st_dev, st_ino) or st_ctime_ns, and a
        # capture that observed two different file identities is unstable.
        before_identity = (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        if len(data) != before.st_size or after_identity != before_identity:
            raise PayloadError(
                "dispatch_payload_source_unstable",
                f"body_file {body_file!r} changed during capture "
                f"(stat identity (dev, ino, size, mtime_ns, ctime_ns) before "
                f"{before_identity}, after {after_identity}; read {len(data)} bytes)",
            )
    finally:
        for opened in reversed(opened_fds):
            os.close(opened)

    if len(data) == 0:
        raise PayloadError(
            "dispatch_payload_invalid_utf8",
            f"file-backed body must be non-empty strict UTF-8; {body_file!r} is empty",
        )
    if b"\x00" in data:
        raise PayloadError(
            "dispatch_payload_invalid_utf8",
            f"file-backed body must not contain NUL bytes: {body_file!r}",
        )
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PayloadError(
            "dispatch_payload_invalid_utf8",
            f"file-backed body is not strict UTF-8 at byte {exc.start}: {body_file!r}",
        ) from exc

    return CapturedPayload(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        byte_count=len(data),
        char_count=len(text),
    )


@dataclass
class StagedPayload:
    """Custody handle for one staged payload entry.

    ``dir_fd`` is the retained ``O_DIRECTORY|O_NOFOLLOW`` FD of the real
    staging directory the entry was created in and ``file_fd`` is the retained
    FD of the exclusively created, written, and fsynced staging inode. Both
    are held from creation through the final link (or discard), so every later
    operation on the entry is FD-relative: a rename that swaps a validated
    store or staging directory for a symlink after validation cannot redirect
    the write, the source-link resolution, or the cleanup unlink. ``path`` is
    the canonical pathname for reporting only and is never re-opened.
    """

    path: Path
    name: str
    dir_fd: int
    file_fd: int
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self.file_fd)
        os.close(self.dir_fd)


# Staging-create seam: tests patch this to swap the staging directory between
# the retained-FD chain build and the exclusive staging create, proving the
# create and write happen relative to the retained real directory (an attacker
# decoy behind a swapped pathname receives nothing) and that a detached
# staging chain refuses before any SQL write. Called with the
# store-root-relative slash-joined path of the staging entry about to be
# created. Production behavior is a no-op.
def _before_staging_create(rel_path: str) -> None:
    return None


def stage_payload(store_root: Path, data: bytes) -> StagedPayload:
    """Create, write, and fsync a private staging entry, retaining custody FDs.

    The store root and staging directory are created/opened as a retained
    ``O_DIRECTORY|O_NOFOLLOW`` FD chain anchored at the DB parent (each
    freshly created level fsyncs its retained parent FD), and the staging
    entry is created exclusively, written, fsynced, and on failure unlinked
    relative to the retained staging FD; no staging operation resolves a full
    pathname again after validation. ``os.write`` is looped over a memoryview
    until every byte is written; zero progress is a failure, and the staged
    size is re-verified via fstat before fsync. Before returning, the retained
    chain is re-checked against the canonical hierarchy so a staging tree
    detached by a concurrent rename refuses instead of feeding a later publish
    from outside the sibling store. The returned handle keeps the staging
    directory and staged-file FDs open through the final link or discard.
    """
    store_root = Path(store_root)
    chain_fds: list[int] = []
    staging_fd: int | None = None
    file_fd: int | None = None
    name: str | None = None
    try:
        try:
            anchor_fd = os.open(store_root.parent, _DIR_ANCHOR_FLAGS)
        except OSError as exc:
            raise PayloadError(
                "dispatch_payload_publish_failed",
                f"could not open payload store parent directory: {exc.strerror}",
            ) from exc
        chain_fds.append(anchor_fd)
        store_fd = _ensure_store_dir_fd(anchor_fd, store_root.name, label=store_root.name)
        chain_fds.append(store_fd)
        staging_fd = _ensure_store_dir_fd(store_fd, "staging", label="staging")

        name = uuid4().hex
        _before_staging_create(f"staging/{name}")
        try:
            file_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=staging_fd,
            )
            view = memoryview(data)
            written = 0
            while written < len(view):
                progress = os.write(file_fd, view[written:])
                if progress <= 0:
                    raise PayloadError(
                        "dispatch_payload_publish_failed",
                        f"os.write made zero progress staging the payload at byte "
                        f"{written} of {len(view)}",
                    )
                written += progress
            staged_size = os.fstat(file_fd).st_size
            if staged_size != len(data):
                raise PayloadError(
                    "dispatch_payload_publish_failed",
                    f"staged payload has {staged_size} bytes, expected {len(data)}",
                )
            os.fsync(file_fd)
        except OSError as exc:
            raise PayloadError(
                "dispatch_payload_publish_failed",
                f"could not stage payload in the store: {exc.strerror}",
            ) from exc
        # The staged inode is durable in the retained staging directory. A
        # concurrent rename that detached that directory from the canonical
        # hierarchy would leave the later publish sourcing from outside the
        # sibling store, so refuse now, before any SQL write can rely on it.
        _assert_chain_attached(
            store_root.parent,
            [(store_root.name, store_fd), ("staging", staging_fd)],
            make_error=lambda detail: PayloadError(
                "dispatch_payload_publish_failed", detail
            ),
        )
    except BaseException:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
            try:
                os.unlink(name, dir_fd=staging_fd)
            except OSError:
                pass
        if staging_fd is not None:
            try:
                os.close(staging_fd)
            except OSError:
                pass
        raise
    finally:
        for fd in reversed(chain_fds):
            os.close(fd)
    return StagedPayload(
        path=store_root / "staging" / name,
        name=name,
        dir_fd=staging_fd,
        file_fd=file_fd,
    )


def discard_staging(staged: StagedPayload | None) -> None:
    """Best-effort FD-relative removal of a staging entry; residue is audit-visible.

    The unlink targets the entry name relative to the retained staging
    directory FD, so a swapped staging pathname can never redirect cleanup
    onto an attacker decoy. Safe to call more than once; the custody FDs are
    closed exactly once.
    """
    if staged is None or staged._closed:
        return
    try:
        os.unlink(staged.name, dir_fd=staged.dir_fd)
    except OSError:
        pass
    staged.close()


# Durability seam: fsync a retained, already-verified FD directly, never
# re-opening a pathname a concurrent rename could have replaced with a
# symlink. Every store durability fsync operates on a retained FD: created
# store levels fsync their retained parent FD, publication durability and the
# failure-cleanup unlink fsync the retained shard/sha256 FDs, and the SQL
# pre-rollback cleanup fsyncs the shard FD retained in the PublishedBlob.
# Tests patch this to fault-inject durability failures against a specific
# retained FD.
def _fsync_fd(fd: int) -> None:
    os.fsync(fd)


def _dir_identity(fd: int) -> tuple[int, int]:
    """``(st_dev, st_ino)`` of an open directory FD.

    Two FDs share an identity exactly when they refer to the same directory
    inode, which is how a retained directory FD is compared against the
    directory reached by re-walking the canonical no-follow pathname.
    """
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


def _assert_chain_attached(
    anchor_path: Path,
    chain: list[tuple[str, int]],
    *,
    make_error,
) -> None:
    """Confirm the retained directory-FD chain is still the canonical hierarchy.

    Retained ``O_DIRECTORY|O_NOFOLLOW`` FDs keep pointing at the real
    directories they were opened on even after a concurrent rename moves those
    directories out of the canonical store and drops a replacement (or symlink)
    in their place. That prevents symlink traversal but does not prove the blob
    linked or read through those FDs is still reachable at the canonical digest
    path. This re-opens the canonical hierarchy fresh from ``anchor_path`` (the
    anchor may legitimately be reached through system symlinks, matching the
    walk) and, for each retained level, opens the same-named child no-follow and
    compares directory identities. A completed component rename/replacement
    detaches the retained chain: the re-open reaches a different inode, or fails
    no-follow, and this raises the caller's typed error so the operation fails
    closed instead of returning success for a blob outside the canonical store.
    Every re-opened FD is closed before returning.
    """
    reopened: list[int] = []
    try:
        try:
            parent_fd = os.open(anchor_path, _DIR_ANCHOR_FLAGS)
        except OSError as exc:
            raise make_error(
                "could not re-open the payload store anchor to confirm canonical "
                f"attachment: {exc.strerror}"
            ) from exc
        reopened.append(parent_fd)
        for name, retained_fd in chain:
            try:
                child_fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise make_error(
                    f"canonical payload store level {name!r} could not be re-opened "
                    f"no-follow to confirm attachment: {exc.strerror}"
                ) from exc
            reopened.append(child_fd)
            if _dir_identity(child_fd) != _dir_identity(retained_fd):
                raise make_error(
                    f"canonical payload store level {name!r} was replaced during the "
                    "operation; the retained directory chain is detached from the "
                    "canonical store hierarchy"
                )
            parent_fd = child_fd
    finally:
        for fd in reversed(reopened):
            os.close(fd)


def _read_leaf_bounded(fd: int, byte_count: int) -> bytes:
    """Read at most ``byte_count + 1`` bytes from an fstat-verified leaf FD.

    The single extra byte makes concurrent growth observable as excess at the
    caller's exact-length check without ever reading unbounded from a leaf
    that grew after its size was verified.
    """
    chunks: list[bytes] = []
    remaining = byte_count + 1
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _verify_existing_blob(
    shard_fd: int, leaf_name: str, *, sha256_hex: str, byte_count: int
) -> None:
    """Independently verify type, byte count, and digest of an existing blob.

    The blob is statted and opened relative to the retained, verified shard
    directory FD with no-follow/nonblocking semantics, so a parent-component
    swap cannot redirect the re-verification outside the sibling store and a
    FIFO raced into the leaf between the lstat and the open cannot block the
    open. The opened FD is authoritative: it is fstat-verified to be a
    regular file of exactly ``byte_count`` bytes, at most ``byte_count + 1``
    bytes are read from it, short or excess reads refuse, and only those
    bounded bytes are hashed, so a leaf raced to a growing file cannot force
    an unbounded read.
    """
    st = os.lstat(leaf_name, dir_fd=shard_fd)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise PayloadError(
            "dispatch_payload_publish_failed",
            f"existing destination for digest {sha256_hex} is not a regular file; refusing to overwrite",
        )
    if st.st_size != byte_count:
        raise PayloadError(
            "dispatch_payload_publish_failed",
            f"existing destination for digest {sha256_hex} has {st.st_size} bytes, "
            f"expected {byte_count}; refusing to overwrite",
        )
    _before_reuse_open(f"blobs/sha256/{sha256_hex[:2]}/{sha256_hex}")
    try:
        fd = os.open(leaf_name, _LEAF_OPEN_FLAGS, dir_fd=shard_fd)
    except OSError as exc:
        if exc.errno in _SYMLINK_OPEN_ERRNOS:
            raise PayloadError(
                "dispatch_payload_publish_failed",
                f"existing destination for digest {sha256_hex} is not a regular file; refusing to overwrite",
            ) from exc
        raise PayloadError(
            "dispatch_payload_publish_failed",
            f"existing destination for digest {sha256_hex} could not be opened: {exc.strerror}",
        ) from exc
    try:
        opened_st = os.fstat(fd)
        if not stat.S_ISREG(opened_st.st_mode):
            raise PayloadError(
                "dispatch_payload_publish_failed",
                f"existing destination for digest {sha256_hex} is not a regular file; refusing to overwrite",
            )
        if opened_st.st_size != byte_count:
            raise PayloadError(
                "dispatch_payload_publish_failed",
                f"existing destination for digest {sha256_hex} has {opened_st.st_size} bytes, "
                f"expected {byte_count}; refusing to overwrite",
            )
        data = _read_leaf_bounded(fd, byte_count)
    finally:
        os.close(fd)
    if len(data) != byte_count:
        raise PayloadError(
            "dispatch_payload_publish_failed",
            f"existing destination for digest {sha256_hex} read {len(data)} bytes, "
            f"expected {byte_count}; refusing to overwrite",
        )
    observed = hashlib.sha256(data).hexdigest()
    if observed != sha256_hex:
        raise PayloadError(
            "dispatch_payload_publish_failed",
            f"existing destination for digest {sha256_hex} hashes to {observed}; "
            "refusing to overwrite",
        )


@dataclass
class PublishedBlob:
    """Result and custody of one payload publication.

    For a blob this call created, ``shard_fd`` is a retained duplicate of the
    verified shard directory FD and ``identity`` is the ``(st_dev, st_ino)``
    of the newly linked inode; both stay valid through the caller's SQL commit
    or rollback so a rollback can remove exactly the created inode
    FD-relative (``rollback_created_blob``), never a replacement reachable
    through a swapped pathname. A reused pre-existing blob carries no custody
    (nothing may be removed on rollback). ``close()`` is idempotent and must
    run on every success and error path once the SQL outcome is settled.
    """

    path: Path
    created: bool
    leaf_name: str
    shard_fd: int | None = None
    identity: tuple[int, int] | None = None
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.shard_fd is not None:
            os.close(self.shard_fd)


def publish_final(
    store_root: Path, staged: StagedPayload, capture: CapturedPayload
) -> PublishedBlob:
    """Publish the staged inode at its digest-derived final path, create-only.

    ``created`` on the returned ``PublishedBlob`` is True only when this call
    materialized the final blob; a pre-existing destination is reused only
    after independent type/size/digest re-verification and is never
    overwritten or deleted by this call. The link source resolves relative to
    the retained staging directory FD and the newly linked inode is
    independently verified (type, identity, and size against the retained
    staged-file FD) before any durability work, so neither side of the link
    can be redirected by a component swap. The blob and its parent
    directories are durable (fsynced) before this function returns, and a
    created blob's shard custody is returned for the caller's SQL
    commit/rollback window.
    """
    store_root = Path(store_root)
    final_path = blob_path(store_root, capture.sha256)
    leaf_name = capture.sha256
    blob_rel = f"blobs/sha256/{capture.sha256[:2]}/{capture.sha256}"

    # Build and retain a verified O_DIRECTORY|O_NOFOLLOW FD for every store
    # level, creating any missing level relative to its verified parent FD.
    # Each freshly created level fsyncs its parent by pathname (the DB parent
    # when store_root itself is new), so the whole chain is durable before the
    # blob link and the SQL commit that references it. The final link, the
    # existing-blob re-verification, and the concurrent-winner reuse all happen
    # relative to the retained shard FD, so a rename that swaps a validated
    # store/shard directory for a symlink after validation cannot redirect
    # publication outside the sibling store.
    fds: list[int] = []
    try:
        try:
            db_parent_fd = os.open(store_root.parent, _DIR_ANCHOR_FLAGS)
        except OSError as exc:
            raise PayloadError(
                "dispatch_payload_publish_failed",
                f"could not open payload store parent directory: {exc.strerror}",
            ) from exc
        fds.append(db_parent_fd)
        store_fd = _ensure_store_dir_fd(db_parent_fd, store_root.name, label=store_root.name)
        fds.append(store_fd)
        blobs_fd = _ensure_store_dir_fd(store_fd, "blobs", label="blobs")
        fds.append(blobs_fd)
        sha_fd = _ensure_store_dir_fd(blobs_fd, "sha256", label="sha256")
        fds.append(sha_fd)
        shard_fd = _ensure_store_dir_fd(sha_fd, capture.sha256[:2], label=capture.sha256[:2])
        fds.append(shard_fd)

        try:
            os.lstat(leaf_name, dir_fd=shard_fd)
        except FileNotFoundError:
            leaf_present = False
        else:
            leaf_present = True
        chain = [
            (store_root.name, store_fd),
            ("blobs", blobs_fd),
            ("sha256", sha_fd),
            (capture.sha256[:2], shard_fd),
        ]
        if leaf_present:
            _verify_existing_blob(
                shard_fd, leaf_name, sha256_hex=capture.sha256, byte_count=capture.byte_count
            )
            discard_staging(staged)
            _assert_publish_chain_attached(store_root, chain)
            return PublishedBlob(path=final_path, created=False, leaf_name=leaf_name)

        _before_final_link(blob_rel)
        try:
            os.link(
                staged.name,
                leaf_name,
                src_dir_fd=staged.dir_fd,
                dst_dir_fd=shard_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            # A concurrent publisher won the create; verify and reuse its blob.
            _verify_existing_blob(
                shard_fd, leaf_name, sha256_hex=capture.sha256, byte_count=capture.byte_count
            )
            discard_staging(staged)
            _assert_publish_chain_attached(store_root, chain)
            return PublishedBlob(path=final_path, created=False, leaf_name=leaf_name)
        except OSError as exc:
            raise PayloadError(
                "dispatch_payload_publish_failed",
                f"could not publish payload blob: {exc.strerror}",
            ) from exc

        # The link source resolved relative to the retained staging FD, but the
        # entry name could have been replaced inside the real staging directory
        # between write and link. Independently verify the newly linked inode
        # against the retained staged-file FD (type, identity, size) before any
        # durability work or SQL success can reference it; a mismatch removes
        # exactly the entry this call created, durably, and refuses.
        staged_st = os.fstat(staged.file_fd)
        custody_error: PayloadError | None = None
        try:
            leaf_st = os.lstat(leaf_name, dir_fd=shard_fd)
        except OSError as exc:
            custody_error = PayloadError(
                "dispatch_payload_publish_failed",
                f"newly linked blob for digest {capture.sha256} could not be "
                f"re-verified in the retained shard: {exc.strerror}",
            )
        else:
            if (
                stat.S_ISLNK(leaf_st.st_mode)
                or not stat.S_ISREG(leaf_st.st_mode)
                or (leaf_st.st_dev, leaf_st.st_ino) != (staged_st.st_dev, staged_st.st_ino)
                or leaf_st.st_size != capture.byte_count
            ):
                custody_error = PayloadError(
                    "dispatch_payload_publish_failed",
                    f"newly linked blob for digest {capture.sha256} is not the "
                    "verified staged payload inode; the staging entry was replaced "
                    "between write and link, so the linked entry was removed and "
                    "publication refuses",
                )
        if custody_error is not None:
            discard_staging(staged)
            _remove_linked_blob_durably(
                shard_fd,
                leaf_name,
                cause=custody_error,
                unlink_failed=lambda err: (
                    f"{custody_error} and unlink of the newly linked entry also "
                    f"failed ({err}); orphan residue remains measurable via "
                    "`agent-comms payload-audit`"
                ),
                fsync_failed=lambda err: (
                    f"{custody_error}; the newly linked entry was unlinked but the "
                    f"cleanup fsync failed ({err}), so the unlink is not durable "
                    "and orphan residue may survive a crash; residue remains "
                    "measurable via `agent-comms payload-audit`"
                ),
            )
            raise custody_error
        identity = (staged_st.st_dev, staged_st.st_ino)

        _finalize_published_blob(shard_fd, sha_fd, leaf_name, sha256_hex=capture.sha256)
        # The blob is durable in the retained shard. Only now confirm the
        # retained chain is still the canonical hierarchy: a rename that
        # detached it mid-publish means the durable blob is not at the canonical
        # digest path, so this must fail closed rather than let SQL commit a
        # reference to an unreachable blob. Remove exactly the blob this call
        # linked, relative to the retained shard FD, never an attacker
        # replacement reachable by the canonical pathname.
        try:
            _assert_publish_chain_attached(store_root, chain)
        except PayloadError as attach_exc:
            # The retained shard is detached from the canonical hierarchy, so it
            # may lie outside audit_store's no-follow reach: silent residue is
            # unacceptable and an un-fsynced unlink could be lost on crash. Remove
            # exactly the blob this call linked, relative to the retained shard
            # FD, and make that removal durable; never touch an attacker
            # replacement reachable only through the swapped pathname. Any cleanup
            # failure raises a typed error that preserves the attachment-failure
            # context and reports the residue and its operational-audit limits,
            # rather than silently re-raising only the attachment mismatch.
            discard_staging(staged)
            _remove_linked_blob_durably(
                shard_fd,
                leaf_name,
                cause=attach_exc,
                unlink_failed=lambda err: (
                    f"published blob for digest {capture.sha256} could not be confirmed "
                    f"attached to the canonical payload store ({attach_exc}); unlink of the "
                    f"newly created blob from the detached shard also failed ({err}), so "
                    "orphan residue remains and, because the shard is detached from the "
                    "canonical hierarchy, it may lie outside the reach of `agent-comms "
                    "payload-audit`"
                ),
                fsync_failed=lambda err: (
                    f"published blob for digest {capture.sha256} could not be confirmed "
                    f"attached to the canonical payload store ({attach_exc}); the newly "
                    f"created blob was unlinked from the detached shard but the cleanup "
                    f"fsync failed ({err}), so the unlink is not durable and orphan residue "
                    "may survive a crash, potentially outside the reach of `agent-comms "
                    "payload-audit`"
                ),
            )
            raise
        discard_staging(staged)
        # Retain shard custody past this function's own FD cleanup so the
        # caller can remove exactly this created inode, FD-relative, if its
        # SQL transaction fails after publication.
        try:
            retained_shard_fd = os.dup(shard_fd)
        except OSError as exc:
            dup_error = PayloadError(
                "dispatch_payload_publish_failed",
                f"could not retain shard custody for the published blob: {exc.strerror}",
            )
            _remove_linked_blob_durably(
                shard_fd,
                leaf_name,
                cause=dup_error,
                unlink_failed=lambda err: (
                    f"{dup_error} and unlink of the newly created blob also failed "
                    f"({err}); orphan residue remains measurable via "
                    "`agent-comms payload-audit`"
                ),
                fsync_failed=lambda err: (
                    f"{dup_error}; the newly created blob was unlinked but the "
                    f"cleanup fsync failed ({err}), so the unlink is not durable "
                    "and orphan residue may survive a crash; residue remains "
                    "measurable via `agent-comms payload-audit`"
                ),
            )
            raise dup_error
        return PublishedBlob(
            path=final_path,
            created=True,
            leaf_name=leaf_name,
            shard_fd=retained_shard_fd,
            identity=identity,
        )
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _assert_publish_chain_attached(
    store_root: Path, chain: list[tuple[str, int]]
) -> None:
    _assert_chain_attached(
        store_root.parent,
        chain,
        make_error=lambda detail: PayloadError("dispatch_payload_publish_failed", detail),
    )


def rollback_created_blob(published: PublishedBlob) -> None:
    """Unlink exactly the blob a failed dispatch created, FD-relative.

    Callable only for a publication that created its blob. The leaf is
    re-verified against the recorded created-inode identity through the
    retained shard FD before the unlink, so a parent-directory swap cannot
    redirect cleanup and a leaf replacement (an attacker decoy at the
    canonical digest name) is never deleted: a mismatch raises the typed
    error, leaves the replacement untouched, and reports the created inode as
    residue. ``OSError`` from the guard stat or the unlink propagates for the
    caller to classify. The caller owns the follow-up durability fsync of
    ``published.shard_fd`` and the closing of the custody FDs.
    """
    if not published.created or published.shard_fd is None or published.identity is None:
        raise PayloadError(
            "dispatch_payload_publish_failed",
            "rollback cleanup is only defined for a publication that created its blob",
        )
    st = os.lstat(published.leaf_name, dir_fd=published.shard_fd)
    if stat.S_ISLNK(st.st_mode) or (st.st_dev, st.st_ino) != published.identity:
        raise PayloadError(
            "dispatch_payload_publish_failed",
            f"canonical entry for digest {published.leaf_name} is no longer the "
            "blob this dispatch created; refusing to unlink the replacement, and "
            "the created inode remains as orphan residue",
        )
    os.unlink(published.leaf_name, dir_fd=published.shard_fd)


def cleanup_failed_publication(
    published: PublishedBlob | None, cause: BaseException
) -> None:
    """Clean a newly published blob before SQL rollback, or fail typed."""
    if published is None or not published.created:
        return
    relative = f"blobs/sha256/{published.leaf_name[:2]}/{published.leaf_name}"
    try:
        rollback_created_blob(published)
        _fsync_fd(published.shard_fd)
    except (OSError, PayloadError) as exc:
        raise PayloadError(
            "cleanup_uncertain",
            f"cleanup uncertain for digest {published.leaf_name} at {relative}: {exc}",
        ) from cause


def _remove_linked_blob_durably(
    shard_fd: int,
    leaf_name: str,
    *,
    cause: BaseException,
    unlink_failed: Callable[[str], str],
    fsync_failed: Callable[[str], str],
) -> None:
    """Remove a just-linked blob and make the removal durable, both FD-relative.

    The unlink and its follow-up shard fsync both act on the retained, verified
    ``shard_fd``, never a re-opened pathname, so a concurrent rename that swapped
    the validated shard for a symlink cannot redirect cleanup onto another
    directory or an attacker replacement reachable through the swapped pathname.
    A clean removal returns normally, leaving the caller to surface ``cause``.

    A failure never silently succeeds. On unlink failure a typed
    ``dispatch_payload_publish_failed`` built by ``unlink_failed`` is raised; on
    cleanup-fsync failure one built by ``fsync_failed`` is raised. Both chain
    ``cause`` so the triggering failure context is preserved rather than replaced
    by the cleanup error, and each detail builder receives the cleanup errno's
    ``strerror`` so it can report residue and its operational-audit implications.
    """
    try:
        os.unlink(leaf_name, dir_fd=shard_fd)
    except OSError as unlink_exc:
        raise PayloadError(
            "dispatch_payload_publish_failed",
            unlink_failed(unlink_exc.strerror),
        ) from cause
    try:
        _fsync_fd(shard_fd)
    except OSError as cleanup_exc:
        raise PayloadError(
            "dispatch_payload_publish_failed",
            fsync_failed(cleanup_exc.strerror),
        ) from cause


def _finalize_published_blob(
    shard_fd: int, sha_fd: int, leaf_name: str, *, sha256_hex: str
) -> None:
    """Make a freshly linked blob durable, cleaning up on a durability failure.

    Durability and cleanup are performed against the retained, verified shard
    and ``sha256`` directory FDs, never by re-opening the digest pathname: a
    concurrent rename that swaps a validated store/shard directory for a symlink
    cannot redirect the fsync or the failure-unlink onto a different directory
    or an attacker replacement. On a durability failure the newly created blob
    is unlinked relative to the retained shard FD while the caller still holds
    the SQL write lock, so no unreported blob survives; a pre-existing verified
    blob never reaches this function and is never unlinked.
    """
    try:
        _fsync_fd(shard_fd)
        _fsync_fd(sha_fd)
    except OSError as exc:
        # The blob lives in the still-attached canonical shard, so any surviving
        # residue is measurable through the read-only audit. The unlink itself
        # must be durable before this call may claim clean cleanup: fsync the
        # shard directory the entry was removed from through its retained FD. If
        # that fsync fails, the removal can be lost on crash and the blob may
        # reappear as an orphan, so the diagnostic says so.
        _remove_linked_blob_durably(
            shard_fd,
            leaf_name,
            cause=exc,
            unlink_failed=lambda err: (
                f"could not make payload blob durable ({exc.strerror}) and unlink of the "
                f"newly created blob for digest {sha256_hex} also failed "
                f"({err}); orphan residue remains measurable via "
                "`agent-comms payload-audit`"
            ),
            fsync_failed=lambda err: (
                f"could not make payload blob durable ({exc.strerror}); the newly created "
                f"blob for digest {sha256_hex} was unlinked but the cleanup fsync of "
                f"its directory failed ({err}), so the unlink is not "
                "durable and orphan residue may survive a crash; residue remains "
                "measurable via `agent-comms payload-audit`"
            ),
        )
        raise PayloadError(
            "dispatch_payload_publish_failed",
            f"could not make payload blob durable: {exc.strerror}",
        ) from exc


def _validate_count(name: str, value: object) -> int:
    # The ledger's `integer check(> 0)` columns cannot guarantee this: SQLite
    # compares TEXT and REAL values as greater than zero, so malformed rows
    # such as byte_count='abc' or 2.5 survive storage. Raw values must reach
    # this boundary unconverted; an eager int(...) upstream would leak a raw
    # ValueError instead of the typed metadata code.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PayloadIntegrityError(
            "dispatch_payload_metadata_invalid",
            f"{name} must be a positive integer, got {value!r}",
        )
    return value


def _validate_reference_metadata(payload_origin: object, captured_at: object) -> None:
    """Validate untrusted per-reference SQL metadata before it may count good.

    The ``payload_origin`` CHECK constraint can be bypassed (for example via
    ``pragma ignore_check_constraints``) and ``captured_at`` carries no shape
    CHECK at all, so both values arrive here raw and are never eagerly
    coerced: a non-string, a value outside the closed origin vocabulary, or a
    timestamp off the canonical stored shape is typed metadata corruption.
    """
    if not isinstance(payload_origin, str) or payload_origin not in PAYLOAD_ORIGINS:
        allowed = ", ".join(PAYLOAD_ORIGINS)
        raise PayloadIntegrityError(
            "dispatch_payload_metadata_invalid",
            f"payload_origin must be one of: {allowed}; got {payload_origin!r}",
        )
    if not isinstance(captured_at, str) or _CAPTURED_AT_RE.fullmatch(captured_at) is None:
        raise PayloadIntegrityError(
            "dispatch_payload_metadata_invalid",
            "captured_at must be the canonical stored UTC timestamp "
            f"(YYYY-MM-DDTHH:MM:SS+00:00); got {captured_at!r}",
        )
    try:
        datetime.strptime(captured_at, _CAPTURED_AT_STRPTIME)
    except ValueError as exc:
        raise PayloadIntegrityError(
            "dispatch_payload_metadata_invalid",
            f"captured_at is not a real calendar timestamp: {captured_at!r}",
        ) from exc


def _validate_verified_metadata(
    storage_kind: str, payload_sha256: str, byte_count: object, char_count: object
) -> tuple[int, int]:
    if storage_kind != STORAGE_KIND:
        raise PayloadIntegrityError(
            "dispatch_payload_metadata_invalid",
            f"unknown storage_kind {storage_kind!r}; expected {STORAGE_KIND!r}",
        )
    validate_sha256_hex(payload_sha256)
    return (
        _validate_count("byte_count", byte_count),
        _validate_count("char_count", char_count),
    )


def _read_verified_text_fd(
    fd: int, *, payload_sha256: str, byte_count: int, char_count: int
) -> str:
    """Verify and decode bytes from the same already-opened payload FD."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise PayloadIntegrityError(
            "dispatch_payload_not_regular",
            f"payload blob for digest {payload_sha256} is not a regular file",
        )
    if st.st_size != byte_count:
        raise PayloadIntegrityError(
            "dispatch_payload_size_mismatch",
            f"payload blob for digest {payload_sha256} has {st.st_size} bytes, expected {byte_count}",
        )
    data = _read_leaf_bounded(fd, byte_count)
    if len(data) != byte_count:
        raise PayloadIntegrityError(
            "dispatch_payload_size_mismatch",
            f"payload blob for digest {payload_sha256} read {len(data)} bytes, expected {byte_count}",
        )
    observed_sha256 = hashlib.sha256(data).hexdigest()
    if observed_sha256 != payload_sha256:
        raise PayloadIntegrityError(
            "dispatch_payload_digest_mismatch",
            f"payload blob digest mismatch: expected {payload_sha256}, observed {observed_sha256}",
        )
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PayloadIntegrityError(
            "dispatch_payload_decode_mismatch",
            f"payload blob for digest {payload_sha256} is not strict UTF-8 at byte {exc.start}",
        ) from exc
    if len(text) != char_count:
        raise PayloadIntegrityError(
            "dispatch_payload_decode_mismatch",
            f"payload blob for digest {payload_sha256} decoded to {len(text)} characters, "
            f"expected {char_count}",
        )
    return text


def load_verified_text(
    store_root: Path,
    *,
    storage_kind: str,
    payload_sha256: str,
    byte_count: object,
    char_count: object,
) -> str:
    """Load and exactly verify one referenced blob, returning the decoded text.

    Shared by the adapter-start preflight and ``read_message`` resolution. The
    returned text decodes from the same bytes that were verified in this call,
    so a filesystem change after verification cannot alter the result. Raises
    ``PayloadIntegrityError`` with a stable code on any mismatch. Count
    metadata is accepted raw and validated here, so a malformed ledger value
    fails typed instead of at a caller-side conversion.
    """
    byte_count, char_count = _validate_verified_metadata(
        storage_kind, payload_sha256, byte_count, char_count
    )
    store_root = Path(store_root)
    leaf_name = payload_sha256
    blob_rel = f"blobs/sha256/{payload_sha256[:2]}/{payload_sha256}"

    # Open and retain a verified O_DIRECTORY|O_NOFOLLOW FD for every store level
    # from the sibling store root down to the digest shard, then stat and open
    # the leaf relative to the retained shard FD. A rename that swaps a
    # validated store/shard directory for a symlink cannot redirect the read
    # outside the sibling store: the retained FDs keep pointing at the real
    # directories, and the bytes are read from the same leaf FD that was
    # fstat-verified.
    fds: list[int] = []
    try:
        try:
            store_fd = os.open(store_root, _DIR_OPEN_FLAGS)
        except OSError as exc:
            raise PayloadIntegrityError(
                "dispatch_payload_missing",
                f"payload blob for digest {payload_sha256} is unavailable: {exc.strerror}",
            ) from exc
        fds.append(store_fd)
        blobs_fd = _open_store_dir_fd(store_fd, "blobs", payload_sha256=payload_sha256)
        fds.append(blobs_fd)
        sha_fd = _open_store_dir_fd(blobs_fd, "sha256", payload_sha256=payload_sha256)
        fds.append(sha_fd)
        shard_fd = _open_store_dir_fd(
            sha_fd, payload_sha256[:2], payload_sha256=payload_sha256
        )
        fds.append(shard_fd)

        try:
            st = os.lstat(leaf_name, dir_fd=shard_fd)
        except OSError as exc:
            raise PayloadIntegrityError(
                "dispatch_payload_missing",
                f"payload blob for digest {payload_sha256} is unavailable: {exc.strerror}",
            ) from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise PayloadIntegrityError(
                "dispatch_payload_not_regular",
                f"payload blob for digest {payload_sha256} is not a regular file",
            )
        if st.st_size != byte_count:
            raise PayloadIntegrityError(
                "dispatch_payload_size_mismatch",
                f"payload blob for digest {payload_sha256} has {st.st_size} bytes, expected {byte_count}",
            )
        _before_load_open(blob_rel)
        try:
            fd = os.open(leaf_name, _LEAF_OPEN_FLAGS, dir_fd=shard_fd)
        except OSError as exc:
            raise PayloadIntegrityError(
                "dispatch_payload_missing",
                f"payload blob for digest {payload_sha256} could not be opened: {exc.strerror}",
            ) from exc
        try:
            text = _read_verified_text_fd(
                fd,
                payload_sha256=payload_sha256,
                byte_count=byte_count,
                char_count=char_count,
            )
        finally:
            os.close(fd)
        # The bytes verified above came from the retained shard FD. Before
        # returning them as the canonical payload, confirm that retained chain
        # is still attached to the canonical hierarchy: a rename that moved the
        # shard out of the sibling store mid-read means the bytes are no longer
        # the canonical digest path's contents, so this must fail closed.
        _assert_chain_attached(
            store_root.parent,
            [
                (store_root.name, store_fd),
                ("blobs", blobs_fd),
                ("sha256", sha_fd),
                (payload_sha256[:2], shard_fd),
            ],
            make_error=lambda detail: PayloadIntegrityError(
                "dispatch_payload_missing", detail
            ),
        )
        return text
    finally:
        for fd in reversed(fds):
            os.close(fd)


# Audit-walk seam: tests patch this to mutate the store between the audit's
# directory listing and its no-follow open/stat of a listed entry, proving a
# replacement race classifies as irregular instead of being traversed. Called
# with the store-root-relative slash-joined path of the entry about to be
# opened or statted ("." is the store root itself). Production behavior is a
# no-op.
def _before_audit_entry(rel_path: str) -> None:
    return None


def _audit_open_dir(
    name: str | Path,
    *,
    dir_fd: int | None,
    rel_path: str,
    absent_ok: bool,
    irregular: list[dict],
) -> int | None:
    """Open one store level as a real directory, never following symlinks.

    Returns a directory FD, or None when the level is absent (tolerated only
    for the fixed levels, ``absent_ok=True``) or was classified irregular. A
    symlink, a non-directory, a listed entry that vanished before its open,
    or an otherwise unopenable level is recorded in ``irregular`` and never
    traversed, so store corruption and replacement races fail closed instead
    of redirecting the walk outside the sibling store.
    """
    _before_audit_entry(rel_path)
    try:
        return os.open(name, _DIR_OPEN_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ENOENT and absent_ok:
            return None
        if exc.errno in _DIR_REFUSAL_ERRNOS:
            kind = (
                "symlink"
                if exc.errno in _SYMLINK_OPEN_ERRNOS
                or _is_symlink_diagnostic(name, dir_fd=dir_fd)
                else "not_a_directory"
            )
            _record_irregular(irregular, rel_path, kind)
            return None
        _record_irregular(irregular, rel_path, "unavailable")
        return None


def _record_irregular(irregular: list[dict], rel_path: str, kind: str) -> None:
    """Record one deterministic irregular-store finding without duplicates."""
    entry = {"path": rel_path, "kind": kind}
    if entry not in irregular:
        irregular.append(entry)


def _audit_confirm_chain_attached(
    store_root: Path,
    chain: list[tuple[str, int]],
    *,
    rels: list[str],
    irregular: list[dict],
) -> bool:
    """Confirm a retained audit directory-FD chain is still the canonical one.

    Retained ``O_DIRECTORY|O_NOFOLLOW`` FDs keep pointing at the real
    directories they were opened on even after a concurrent rename replaces
    those directories in the canonical hierarchy, so an audit that walked
    only retained FDs could report ``ok`` for a store whose canonical
    components were detached mid-walk. This re-opens the canonical hierarchy
    fresh from the DB parent (no-follow below it, matching the walk) and
    compares each level's directory identity against the retained FD. The
    first mismatched, unopenable, or replaced level records a ``detached``
    irregular finding at its store-relative path (forcing ``ok`` False) and
    returns False; a fully attached chain returns True. Every re-opened FD is
    closed before returning.
    """
    reopened: list[int] = []
    try:
        try:
            parent_fd = os.open(store_root.parent, _DIR_ANCHOR_FLAGS)
        except OSError:
            _record_irregular(irregular, rels[0] if rels else ".", "detached")
            return False
        reopened.append(parent_fd)
        for (name, retained_fd), rel in zip(chain, rels):
            try:
                child_fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
            except OSError:
                _record_irregular(irregular, rel, "detached")
                return False
            reopened.append(child_fd)
            if _dir_identity(child_fd) != _dir_identity(retained_fd):
                _record_irregular(irregular, rel, "detached")
                return False
            parent_fd = child_fd
    finally:
        for fd in reversed(reopened):
            os.close(fd)
    return True


def _audit_lstat_regular(
    name: str,
    *,
    dir_fd: int,
    rel_path: str,
    irregular: list[dict],
) -> os.stat_result | None:
    """No-follow stat of one listed leaf entry through its parent's FD.

    Returns the stat result only for a regular file; a symlink, any other
    irregular file type, or an entry that vanished after listing is recorded
    in ``irregular`` and returns None.
    """
    _before_audit_entry(rel_path)
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        _record_irregular(irregular, rel_path, "unavailable")
        return None
    if stat.S_ISLNK(st.st_mode):
        _record_irregular(irregular, rel_path, "symlink")
        return None
    if not stat.S_ISREG(st.st_mode):
        _record_irregular(irregular, rel_path, "not_a_regular_file")
        return None
    return st


def _audit_load_verified_text(
    store_root: Path,
    fixed_chain: list[tuple[str, int]],
    *,
    storage_kind: str,
    payload_sha256: str,
    byte_count: object,
    char_count: object,
    irregular: list[dict],
) -> str:
    """Verify one referenced blob through the audit's no-follow FD chain.

    ``fixed_chain`` is the retained ``[(store, fd), (blobs, fd), (sha256, fd)]``
    chain (empty or truncated when a fixed level is absent). After the blob's
    bytes verify, the complete retained chain including the shard is
    re-confirmed against the canonical hierarchy; a detached or replaced
    component records an irregular ``detached`` finding and fails the
    reference closed instead of counting a blob outside the canonical store
    as ``referenced_good``.
    """
    byte_count, char_count = _validate_verified_metadata(
        storage_kind, payload_sha256, byte_count, char_count
    )
    if len(fixed_chain) != 3:
        raise PayloadIntegrityError(
            "dispatch_payload_missing",
            f"payload blob for digest {payload_sha256} is unavailable",
        )

    shard_rel = f"blobs/sha256/{payload_sha256[:2]}"
    shard_fd = _audit_open_dir(
        payload_sha256[:2],
        dir_fd=fixed_chain[-1][1],
        rel_path=shard_rel,
        absent_ok=True,
        irregular=irregular,
    )
    if shard_fd is None:
        raise PayloadIntegrityError(
            "dispatch_payload_missing",
            f"payload blob for digest {payload_sha256} is unavailable",
        )

    try:
        blob_rel = f"{shard_rel}/{payload_sha256}"
        _before_audit_entry(blob_rel)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(payload_sha256, flags, dir_fd=shard_fd)
        except OSError as exc:
            if exc.errno in _SYMLINK_OPEN_ERRNOS or _is_symlink_diagnostic(
                payload_sha256, dir_fd=shard_fd
            ):
                _record_irregular(irregular, blob_rel, "symlink")
                code = "dispatch_payload_not_regular"
            elif exc.errno == errno.ENOENT:
                code = "dispatch_payload_missing"
            else:
                _record_irregular(irregular, blob_rel, "unavailable")
                code = "dispatch_payload_missing"
            raise PayloadIntegrityError(
                code,
                f"payload blob for digest {payload_sha256} could not be opened: {exc.strerror}",
            ) from exc

        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                _record_irregular(irregular, blob_rel, "not_a_regular_file")
                raise PayloadIntegrityError(
                    "dispatch_payload_not_regular",
                    f"payload blob for digest {payload_sha256} is not a regular file",
                )
            text = _read_verified_text_fd(
                fd,
                payload_sha256=payload_sha256,
                byte_count=byte_count,
                char_count=char_count,
            )
        finally:
            os.close(fd)

        # The verified bytes came through retained FDs. Before the reference
        # may count good, confirm that chain is still the canonical hierarchy:
        # a rename that detached the root, blobs, sha256, or shard mid-audit
        # means the bytes are not the canonical digest path's contents.
        if not _audit_confirm_chain_attached(
            store_root,
            fixed_chain + [(payload_sha256[:2], shard_fd)],
            rels=[".", "blobs", "blobs/sha256", shard_rel],
            irregular=irregular,
        ):
            raise PayloadIntegrityError(
                "dispatch_payload_missing",
                f"payload blob for digest {payload_sha256} was verified through a "
                "store hierarchy component that was detached or replaced during "
                "the audit",
            )
        return text
    finally:
        os.close(shard_fd)


def audit_store(db_path: Path) -> dict:
    """Read-only audit of every SQL payload reference against the sibling store.

    Verifies each ``dispatch_payload_refs`` row and separately reports
    referenced-good, referenced-missing/corrupt, unreferenced blob,
    staging-residue, and irregular-entry counts. It never repairs or deletes
    anything. The store walk opens every level relative to its verified
    parent FD with ``O_DIRECTORY|O_NOFOLLOW`` and stats leaf entries
    no-follow through that FD, so it never recurses through a symlink, and it
    enforces the closed store layout at every directory level: the store root
    may contain only ``blobs`` and ``staging``, ``blobs`` only ``sha256``,
    shards only correctly prefixed 64-hex digest names, and ``staging`` only
    ``uuid4().hex`` names. Unexpected names are classified from the name
    alone and never opened or statted; symlinked, non-regular, and
    replacement-raced entries (including a fixed child listed but gone or
    replaced before its open) are classified under ``irregular_entries``
    instead of being traversed or silently skipped. A reference row counts
    ``referenced_good`` only after its raw ``payload_origin`` and
    ``captured_at`` metadata validate against the closed vocabulary and
    canonical stored timestamp shape. Because retained FDs keep following the
    real directories after a rename, the retained root/blobs/sha256/shard
    chain is re-confirmed against the canonical hierarchy after each
    referenced verification, after each shard enumeration, and at the end of
    the walk; any detach or replacement classifies as a ``detached``
    irregular entry and the affected references fail closed. ``ok`` is False
    exactly when any referenced artifact is unavailable or corrupt, or the
    store contains any irregular entry.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise ValidationError(f"payload-audit requires an existing agent-comms ledger at {db_path}")
    store_root = store_root_for_db(db_path)

    uri = f"file:{db_path}?mode=ro&cache=private"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma query_only = on")
        dispatch_table_exists = (
            conn.execute(
                """
            select 1
            from sqlite_master
            where type = 'table' and name = 'dispatch_payload_refs'
            """
            ).fetchone()
            is not None
        )
        message_table_exists = (
            conn.execute(
                "select 1 from sqlite_master where type='table' and name='message_payload_refs'"
            ).fetchone()
            is not None
        )
        refs = []
        if dispatch_table_exists:
            refs.extend(
                conn.execute(
                    """
                select 'dispatch' as reference_kind, dispatch_id as reference_id,
                       storage_kind, payload_origin, payload_sha256,
                       byte_count, char_count, captured_at
                from dispatch_payload_refs
                order by dispatch_id
                """
                ).fetchall()
            )
        if message_table_exists:
            refs.extend(
                conn.execute(
                    """
                select 'message' as reference_kind, message_id as reference_id,
                       storage_kind, NULL as payload_origin, payload_sha256,
                       byte_count, char_count, captured_at
                from message_payload_refs order by message_id
                """
                ).fetchall()
            )
    finally:
        conn.close()

    referenced_good = {"count": 0, "bytes": 0}
    referenced_bad: list[dict] = []
    referenced_digests: set[str] = set()
    unreferenced = {"count": 0, "bytes": 0}
    staging_residue = {"count": 0, "bytes": 0}
    irregular: list[dict] = []
    store_fd = _audit_open_dir(
        store_root, dir_fd=None, rel_path=".", absent_ok=True, irregular=irregular
    )
    blobs_fd: int | None = None
    sha_fd: int | None = None
    try:
        # Closed-layout enumeration: each fixed level lists its children
        # through the verified directory FD and classifies any name outside
        # the closed layout as irregular without ever opening or statting it.
        # A fixed child is opened only when the listing showed it, with
        # ``absent_ok=False``: listed-then-gone (or replaced) is a
        # replacement race that classifies irregular instead of being
        # tolerated as an absent level.
        root_names: list[str] = []
        if store_fd is not None:
            root_names = sorted(os.listdir(store_fd))
            for name in root_names:
                if name not in ("blobs", "staging"):
                    _record_irregular(irregular, name, "unexpected_name")
        if "blobs" in root_names:
            blobs_fd = _audit_open_dir(
                "blobs", dir_fd=store_fd, rel_path="blobs", absent_ok=False, irregular=irregular
            )
        blobs_names: list[str] = []
        if blobs_fd is not None:
            blobs_names = sorted(os.listdir(blobs_fd))
            for name in blobs_names:
                if name != "sha256":
                    _record_irregular(irregular, f"blobs/{name}", "unexpected_name")
        if "sha256" in blobs_names:
            sha_fd = _audit_open_dir(
                "sha256", dir_fd=blobs_fd, rel_path="blobs/sha256",
                absent_ok=False, irregular=irregular,
            )

        # The retained fixed chain (root, blobs, sha256) backs both referenced
        # verification and the detach re-confirmation checks below; a missing
        # fixed level truncates the chain.
        fixed_chain: list[tuple[str, int]] = []
        fixed_rels: list[str] = []
        for name, fd, rel in (
            (store_root.name, store_fd, "."),
            ("blobs", blobs_fd, "blobs"),
            ("sha256", sha_fd, "blobs/sha256"),
        ):
            if fd is None:
                break
            fixed_chain.append((name, fd))
            fixed_rels.append(rel)

        # Referenced verification uses the same verified parent-directory FDs
        # as the store walk. The shard and leaf are opened relative to these
        # FDs with O_NOFOLLOW, the bytes are read from the same leaf FD that
        # was fstat-verified, and the retained chain must still be attached to
        # the canonical hierarchy for the reference to count good. SQL
        # metadata reaches validation raw: the origin/timestamp gate runs
        # before the blob is consulted, so a row with tampered metadata can
        # never count referenced_good.
        for ref in refs:
            digest = str(ref["payload_sha256"])
            referenced_digests.add(digest)
            try:
                if ref["reference_kind"] == "dispatch":
                    _validate_reference_metadata(
                        ref["payload_origin"], ref["captured_at"]
                    )
                _audit_load_verified_text(
                    store_root,
                    fixed_chain,
                    storage_kind=ref["storage_kind"],
                    payload_sha256=ref["payload_sha256"],
                    byte_count=ref["byte_count"],
                    char_count=ref["char_count"],
                    irregular=irregular,
                )
            except PayloadIntegrityError as exc:
                referenced_bad.append(
                    {
                        "reference_kind": ref["reference_kind"],
                        "reference_id": ref["reference_id"],
                        "payload_sha256": digest,
                        "code": exc.code,
                        "detail": str(exc),
                    }
                )
            else:
                referenced_good["count"] += 1
                referenced_good["bytes"] += ref["byte_count"]

        if sha_fd is not None:
            for shard_name in sorted(os.listdir(sha_fd)):
                shard_rel = f"blobs/sha256/{shard_name}"
                if _SHARD_NAME_RE.fullmatch(shard_name) is None:
                    _record_irregular(irregular, shard_rel, "unexpected_name")
                    continue
                shard_fd = _audit_open_dir(
                    shard_name, dir_fd=sha_fd, rel_path=shard_rel,
                    absent_ok=False, irregular=irregular,
                )
                if shard_fd is None:
                    continue
                try:
                    for blob_name in sorted(os.listdir(shard_fd)):
                        blob_rel = f"{shard_rel}/{blob_name}"
                        if _HEX64_RE.fullmatch(blob_name) is None or not blob_name.startswith(
                            shard_name
                        ):
                            _record_irregular(irregular, blob_rel, "unexpected_name")
                            continue
                        st = _audit_lstat_regular(
                            blob_name, dir_fd=shard_fd,
                            rel_path=blob_rel,
                            irregular=irregular,
                        )
                        if st is None or blob_name in referenced_digests:
                            continue
                        unreferenced["count"] += 1
                        unreferenced["bytes"] += st.st_size
                    # Before this shard's enumeration stands, confirm the
                    # retained chain down to the shard is still the canonical
                    # hierarchy; a mid-walk detach records ``detached``.
                    _audit_confirm_chain_attached(
                        store_root,
                        fixed_chain + [(shard_name, shard_fd)],
                        rels=fixed_rels + [shard_rel],
                        irregular=irregular,
                    )
                finally:
                    os.close(shard_fd)

        if "staging" in root_names:
            staging_fd = _audit_open_dir(
                "staging", dir_fd=store_fd, rel_path="staging", absent_ok=False, irregular=irregular
            )
            if staging_fd is not None:
                try:
                    for entry_name in sorted(os.listdir(staging_fd)):
                        entry_rel = f"staging/{entry_name}"
                        if _STAGING_NAME_RE.fullmatch(entry_name) is None:
                            _record_irregular(irregular, entry_rel, "unexpected_name")
                            continue
                        st = _audit_lstat_regular(
                            entry_name, dir_fd=staging_fd,
                            rel_path=entry_rel, irregular=irregular,
                        )
                        if st is None:
                            continue
                        staging_residue["count"] += 1
                        staging_residue["bytes"] += st.st_size
                finally:
                    os.close(staging_fd)

        # End-of-walk revalidation of the complete fixed chain: a canonical
        # root/blobs/sha256 component detached or replaced at any point after
        # its open must classify the store irregular (``detached``) and force
        # ``ok`` False instead of letting the audit vouch for a snapshot the
        # canonical hierarchy no longer contains.
        if fixed_chain:
            _audit_confirm_chain_attached(
                store_root, fixed_chain, rels=fixed_rels, irregular=irregular
            )
    finally:
        if sha_fd is not None:
            os.close(sha_fd)
        if blobs_fd is not None:
            os.close(blobs_fd)
        if store_fd is not None:
            os.close(store_fd)

    return {
        "ok": not referenced_bad
        and not irregular
        and not unreferenced["count"]
        and not staging_residue["count"],
        "db_path": str(db_path),
        "store_root": str(store_root),
        "referenced_good": referenced_good,
        "referenced_missing_or_corrupt": {
            "count": len(referenced_bad),
            "entries": referenced_bad,
        },
        "unreferenced_blobs": unreferenced,
        "staging_residue": staging_residue,
        "irregular_entries": {
            "count": len(irregular),
            "entries": irregular,
        },
    }
