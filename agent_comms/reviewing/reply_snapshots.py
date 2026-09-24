"""Stable reply-time delta snapshots for review-bound implementation dispatches.

The cohesive owner of the review evidence-lifecycle boundary (Landing 2). Before
``mailbox.send_message`` publishes a direct reply whose parent is an in-flight v2
dispatch bound to an implementation ``review_dispatch_intents`` row, this module
measures an immutable delta identity from the worker worktree for atomic
publication; ``mailbox.close_dispatch`` later consumes the snapshot attached to the
exact reply, independent of the caller's legacy ``delta`` flag. It also owns the
legacy single-capture close delta for non-review parity.

It imports only its ``reviewing.intents`` and ``reviewing.git_evidence`` siblings
plus shared ``delta_manifest`` / ``clock`` / ``schema`` primitives, opens no SQL of
its own, and (classification being SQL-only) runs zero Git for an ordinary message.
Git runs strictly outside every SQL write transaction and sequences ``git_evidence``
custody: each child fchdirs into a retained checkout descriptor (never a re-resolved
pathname), with ``GIT_OPTIONAL_LOCKS=0``, a private ``GIT_INDEX_FILE``, and an
isolated object directory over durable alternates.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_comms import delta_manifest
from agent_comms.clock import utc_now
from agent_comms.schema import ValidationError
from agent_comms.reviewing import git_evidence, intents

SNAPSHOT_ALGORITHM = "reply-snapshot-two-scan-v1"
_RAW_ARGS = ("diff-tree", "-r", "--no-renames", "--raw", "--abbrev=40", "-z")
_NAME_STATUS_ARGS = ("diff-tree", "-r", "--no-renames", "--name-status", "-z")


class ReplySnapshotError(ValidationError):
    """A refused reply-snapshot step; ``retryable`` marks the unstable family (an
    identical retry is safe because no message id or row was published)."""

    def __init__(self, code: str, detail: str = "", *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.retryable = retryable


def _refuse(code: str, detail: str = "", *, retryable: bool = False) -> None:
    raise ReplySnapshotError(code, detail, retryable=retryable)


@dataclass(frozen=True)
class ReplyBinding:
    """The exact bound implementation identity a reply must snapshot against."""

    dispatch_id: str
    intent_id: str
    recipient_actor_id: str
    producer_actor_id: str
    round_kind: str
    real_project_root: str
    source_branch: str
    source_head: str
    source_tree: str
    base_commit: str
    base_tree: str
    digest: str


class _RetainedCheckout:
    """Sequences ``git_evidence`` custody for one capture: retains the worktree plus
    its Git/common/object dirs as no-follow descriptors, fchdirs each child into the
    retained worktree, and identity-revalidates the metadata dirs around it.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._retained: list[tuple[Path, tuple[int, int], int]] = []

    def __enter__(self) -> "_RetainedCheckout":
        self._retain(self.root)  # worktree descriptor first
        out = (
            git_evidence.run_git_fchdir(
                self._worktree_fd,
                ["rev-parse", "--git-dir", "--git-common-dir"],
                git_evidence.clean_git_env(),
            )
            .decode()
            .splitlines()
        )
        self.git_dir = self._abs(out[0])
        common = self._abs(out[1])
        self.objects_dir = str(Path(common) / "objects")
        self._git_dir_fd = self._retain(self.git_dir)
        self._retain(common)
        self._retain(self.objects_dir)
        return self

    def __exit__(self, *exc: object) -> bool:
        for _path, _identity, fd in self._retained:
            try:
                os.close(fd)
            except OSError:
                pass
        return False

    @property
    def _worktree_fd(self) -> int:
        return self._retained[0][2]

    def index_digest(self) -> str:
        """Digest the real index opened descriptor-relative to the retained Git-dir
        FD, so a persistent rename/replacement of the Git-dir pathname between steps
        cannot redirect the read away from the retained inode's ``index``."""
        return git_evidence.bounded_regular_file_digest(
            "index", dir_fd=self._git_dir_fd
        )

    def _retain(self, path) -> int:
        fd, identity = git_evidence.open_retained_dir(path)
        self._retained.append((Path(path), identity, fd))
        return fd

    def _abs(self, raw: str) -> str:
        path = Path(raw)
        return str(path if path.is_absolute() else (self.root / raw).resolve())

    def revalidate(self) -> None:
        for path, identity, _fd in self._retained:
            git_evidence.revalidate_retained(path, identity)

    def run(self, args, extra_env: dict[str, str] | None = None) -> bytes:
        env = git_evidence.clean_git_env()
        env["GIT_DIR"] = self.git_dir
        if extra_env:
            env.update(extra_env)
        self.revalidate()
        out = git_evidence.run_git_fchdir(self._worktree_fd, list(args), env)
        self.revalidate()
        return out


def _build_isolated_tree(
    co: _RetainedCheckout, seed_ref: str, base_tree: str
) -> tuple[str, bytes, bytes]:
    """Seed a private index from ``seed_ref``, ``add -A``, and measure the tree.
    Uses a private temporary index/object dir (alternates read the durable objects)
    so the real store is never written and temporary objects are discarded on exit.
    """
    with tempfile.TemporaryDirectory(prefix="agent-comms-reply-snapshot-") as tmp:
        objects = Path(tmp) / "objects"
        objects.mkdir()
        extra = {
            "GIT_WORK_TREE": ".",
            "GIT_INDEX_FILE": str(Path(tmp) / "index"),
            "GIT_OBJECT_DIRECTORY": str(objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": co.objects_dir,
        }
        co.run(["read-tree", seed_ref], extra)
        co.run(["add", "-A"], extra)
        tree = co.run(["write-tree"], extra).decode().strip()

        def diff(args: tuple[str, ...], failure: str) -> bytes:
            # Preserve the legacy diff-tree failure taxonomy for non-review parity.
            try:
                return co.run([*args, base_tree, tree], extra)
            except git_evidence.GitCustodyError as exc:
                raise ReplySnapshotError(failure, str(exc), retryable=True) from exc

        raw = diff(_RAW_ARGS, "delta_manifest_command_failed")
        names = diff(_NAME_STATUS_ARGS, "delta_name_status_command_failed")
    return tree, raw, names


def classify_reply(
    conn: sqlite3.Connection,
    *,
    sender: str,
    to_agents: list[str],
    parent_message_id: str | None,
) -> ReplyBinding | None:
    """Read-only classify a pending reply; ``None`` preserves ordinary delivery.
    A match requires an in-flight v2 parent whose ledger recipient is the sender and
    which joins exactly one ``bound`` implementation intent; once matched, every
    payload member is validated against the canonical intent contract, and any
    missing/renamed/malformed/digest-inconsistent member, multiple match, or
    actor/root drift is a partially bound match that refuses, never falls back.
    """
    if not parent_message_id:
        return None
    row = conn.execute(
        "select * from dispatch_ledger where message_id=?", (parent_message_id,)
    ).fetchone()
    if row is None or row["policy_version"] != "v2" or row["status"] != "in_flight":
        return None
    if row["recipient_actor_id"] != sender:
        return None
    dispatch_id = row["dispatch_id"]
    bound = conn.execute(
        "select * from review_dispatch_intents where dispatch_id=? and state='bound'",
        (dispatch_id,),
    ).fetchall()
    if not bound:
        return None
    # From here it is a match: any inconsistency refuses, never falls through.
    if len(bound) > 1:
        _refuse("reply_snapshot_multiple_bound", f"{dispatch_id}: {len(bound)}")
    intent_row = bound[0]
    producer = row["producer_actor_id"]
    if producer not in to_agents:
        _refuse("reply_snapshot_producer_not_addressed", producer)
    try:
        payload = json.loads(intent_row["payload_json"])
    except (TypeError, ValueError) as exc:
        _refuse("reply_snapshot_payload_malformed", str(exc))
    try:
        digest = intents.payload_digest(payload)
    except intents.IntentError as exc:
        _refuse("reply_snapshot_payload_invalid", str(exc))
    if digest != intent_row["digest"] or intent_row[
        "intent_id"
    ] != intents.intent_id_for(digest):
        _refuse("reply_snapshot_digest_inconsistent", intent_row["intent_id"])
    if payload.get("round_kind") != intents.ROUND_KIND_IMPLEMENTATION:
        _refuse(
            "reply_snapshot_round_kind_not_implementation",
            str(payload.get("round_kind")),
        )
    if payload.get("recipient_actor_id") != sender:
        _refuse(
            "reply_snapshot_recipient_drift", str(payload.get("recipient_actor_id"))
        )
    sender_row = conn.execute(
        "select project_root from actors where id=?", (sender,)
    ).fetchone()
    # Resolve exactly as dispatch_agent's intent binding did (the bound payload
    # root was canonically resolved at bind time).
    raw_root = sender_row["project_root"] if sender_row else None
    sender_root = str(Path(raw_root).expanduser().resolve()) if raw_root else None
    if not sender_root or payload.get("real_project_root") != sender_root:
        _refuse("reply_snapshot_root_drift", str(payload.get("real_project_root")))
    return ReplyBinding(
        dispatch_id=dispatch_id,
        intent_id=intent_row["intent_id"],
        recipient_actor_id=sender,
        producer_actor_id=producer,
        round_kind=intents.ROUND_KIND_IMPLEMENTATION,
        real_project_root=sender_root,
        source_branch=payload["source_branch"],
        source_head=payload["source_head"],
        source_tree=payload["source_tree"],
        base_commit=payload["base_commit"],
        base_tree=payload["base_tree"],
        digest=digest,
    )


def _identity_probe(co: _RetainedCheckout, binding: ReplyBinding) -> dict[str, str]:
    """Cheap read-only HEAD/branch/ref/index probe; runs no ``git status``. HEAD,
    branch, and source ref must equal the bound source identity (so object content
    is bound by identity); the index digest streams from one bounded no-follow fd.
    """
    head = co.run(["rev-parse", "HEAD"]).decode().strip()
    branch = co.run(["rev-parse", "--abbrev-ref", "HEAD"]).decode().strip()
    ref = co.run(["rev-parse", f"refs/heads/{binding.source_branch}"]).decode().strip()
    if head != binding.source_head or branch != binding.source_branch or ref != head:
        _refuse("reply_snapshot_unstable", f"source drift {head}", retryable=True)
    return {
        "head": head,
        "branch": branch,
        "ref": ref,
        "index_sha256": co.index_digest(),
    }


def _measured_snapshot(co: _RetainedCheckout, base_tree: str) -> dict[str, Any]:
    tree, raw, names = _build_isolated_tree(co, base_tree, base_tree)
    if not raw and not names:
        entries, status_counts = 0, {}
    elif not raw or not names:
        _refuse("reply_snapshot_unstable", "manifest emptiness", retryable=True)
    else:
        entries, status_counts = delta_manifest.parse_and_crosscheck(raw, names)
    return {
        "tree": tree,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "entries": entries,
        "status_counts": status_counts,
    }


def capture(binding: ReplyBinding) -> dict[str, Any]:
    """Measure the stable delta identity for a bound reply, before any SQL write.
    Two cheap identity probes bracket two independent isolated snapshots under one
    retained-checkout custody; accepted only when both probes are byte-identical and
    both snapshots agree (the second is the linearization point). Any disagreement or
    custody drift is retryable instability.
    """
    root = Path(binding.real_project_root)
    try:
        with _RetainedCheckout(root) as co:
            probe_a = _identity_probe(co, binding)
            first = _measured_snapshot(co, binding.base_tree)
            second = _measured_snapshot(co, binding.base_tree)
            probe_b = _identity_probe(co, binding)
    except git_evidence.GitCustodyError as exc:
        raise ReplySnapshotError(exc.code, str(exc), retryable=True) from exc
    if probe_a != probe_b:
        _refuse("reply_snapshot_unstable", "identity probe drift", retryable=True)
    if first != second:
        _refuse("reply_snapshot_unstable", "snapshots disagree", retryable=True)
    probe_bytes = json.dumps(probe_b, sort_keys=True, separators=(",", ":")).encode()
    return {
        "base_commit": binding.base_commit,
        "base_tree": binding.base_tree,
        "measured_head": probe_b["head"],
        "snapshot_tree": second["tree"],
        "manifest_sha256": second["manifest_sha256"],
        "entry_count": second["entries"],
        "status_counts": second["status_counts"],
        "boundary_probe_sha256": hashlib.sha256(probe_bytes).hexdigest(),
        "snapshot_algorithm": SNAPSHOT_ALGORITHM,
        "measured_at": utc_now(),
    }


def revalidate(
    conn: sqlite3.Connection,
    binding: ReplyBinding,
    *,
    sender: str,
    to_agents: list[str],
    parent_message_id: str,
) -> None:
    """SQL-only revalidate the exact binding inside the write transaction."""
    current = classify_reply(
        conn, sender=sender, to_agents=to_agents, parent_message_id=parent_message_id
    )
    if current != binding:
        _refuse(
            "reply_snapshot_revalidation_failed",
            "binding changed between capture and publication",
            retryable=True,
        )


def insert_snapshot_row(
    conn: sqlite3.Connection,
    binding: ReplyBinding,
    snapshot: dict[str, Any],
    *,
    reply_message_id: str,
) -> None:
    conn.execute(
        """
        insert into review_reply_snapshots(
          reply_message_id, dispatch_id, intent_id, recipient_actor_id, round_kind,
          base_commit, base_tree, measured_head, snapshot_tree, manifest_sha256,
          entry_count, status_counts_json, boundary_probe_sha256, snapshot_algorithm,
          measured_at
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reply_message_id,
            binding.dispatch_id,
            binding.intent_id,
            binding.recipient_actor_id,
            binding.round_kind,
            snapshot["base_commit"],
            snapshot["base_tree"],
            snapshot["measured_head"],
            snapshot["snapshot_tree"],
            snapshot["manifest_sha256"],
            snapshot["entry_count"],
            json.dumps(
                snapshot["status_counts"], sort_keys=True, separators=(",", ":")
            ),
            snapshot["boundary_probe_sha256"],
            snapshot["snapshot_algorithm"],
            snapshot["measured_at"],
        ),
    )


def bound_implementation_intent(
    conn: sqlite3.Connection, dispatch_id: str
) -> sqlite3.Row | None:
    """The single bound implementation intent for a dispatch, or ``None``."""
    return conn.execute(
        "select intent_id, round_kind from review_dispatch_intents "
        "where dispatch_id=? and state='bound' and round_kind=?",
        (dispatch_id, intents.ROUND_KIND_IMPLEMENTATION),
    ).fetchone()


def consume_for_close(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    intent_id: str,
    reply_message_id: str,
    recipient: str,
    result: str,
) -> dict[str, Any]:
    """Load and verify the snapshot named by the exact reply, returning its delta.
    Keyed by the already-verified ``reply_message_id`` (never "latest"); wrong or
    missing/duplicate evidence refuses before settlement, and a ``satisfied`` close
    requires a nonempty delta (``blocked`` may bind an empty snapshot).
    """
    rows = conn.execute(
        "select * from review_reply_snapshots where reply_message_id=?",
        (reply_message_id,),
    ).fetchall()
    if not rows:
        _refuse("reply_snapshot_missing", reply_message_id)
    if len(rows) > 1:
        _refuse("reply_snapshot_duplicate", reply_message_id)
    row = rows[0]
    if row["dispatch_id"] != dispatch_id:
        _refuse("reply_snapshot_wrong_dispatch", row["dispatch_id"])
    if row["intent_id"] != intent_id:
        _refuse("reply_snapshot_wrong_intent", row["intent_id"])
    if row["recipient_actor_id"] != recipient:
        _refuse("reply_snapshot_wrong_worker", row["recipient_actor_id"])
    delta = {
        "base_commit": row["base_commit"],
        "snapshot_tree": row["snapshot_tree"],
        "manifest_sha256": row["manifest_sha256"],
        "entries": row["entry_count"],
        "status_counts": json.loads(row["status_counts_json"]),
        "measured_at": row["measured_at"],
    }
    if result == "satisfied" and delta["entries"] == 0:
        _refuse("reply_snapshot_empty_delta_for_satisfied", dispatch_id)
    return delta


def legacy_close_delta(root: Path) -> dict[str, Any]:
    """Close-time single-capture delta against HEAD (non-review parity), over the
    same retained custody; refuses ``delta_empty``."""
    root = Path(root).resolve()
    try:
        with _RetainedCheckout(root) as co:
            base = co.run(["rev-parse", "HEAD"]).decode().strip()
            base_tree = co.run(["rev-parse", f"{base}^{{tree}}"]).decode().strip()
            tree, raw, names = _build_isolated_tree(co, base, base_tree)
    except git_evidence.GitCustodyError as exc:
        raise ValidationError(f"snapshot_failed: {exc}") from exc
    if not raw:
        raise ValidationError("delta_empty")
    entries, statuses = delta_manifest.parse_and_crosscheck(raw, names)
    return {
        "base_commit": base,
        "snapshot_tree": tree,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "entries": entries,
        "status_counts": statuses,
        "measured_at": utc_now(),
    }
