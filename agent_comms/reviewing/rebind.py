"""Audited rebind and rebind-dod orchestration behind the review facade.

Owns the exact ``rebind`` recovery for a reviewed-but-not-integrated change and
the ``rebind-dod`` re-binding of the executable DoD. Preserves the derived-only
evidence sourcing, the canonical-digest/stable-patch-id identity checks, the
ancestry/history/epoch mutation, and the drafted/revised/reviewed state gate for
``rebind-dod`` exactly. Runs no Git mutations and never rewrites completed
history.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

from agent_comms.reviewing.contracts import ReviewError, gate_epoch
from agent_comms.reviewing.briefs import load_dod
from agent_comms.reviewing.git_evidence import (
    CANONICAL_DIFF_ALGORITHM,
    canonicalize_patch_bytes,
    commit_in_repo,
    git_branch,
    git_proc,
    integration_checkout,
    is_ancestor,
    pinned_patch_bytes,
    require_clean_tree,
    require_git_checkout,
    resolve_head,
    stable_patch_id,
)
from agent_comms.reviewing.store import locked_update, require_state, utc_now


def command_rebind(args: argparse.Namespace) -> None:
    """Audited recovery for a reviewed-but-not-integrated change after the
    integration checkout advanced: the architect updates and measures the
    integration checkout first, rebases the source branch onto that exact
    tip, then rebind validates everything and atomically re-binds the
    record. It never runs Git mutations and never rewrites completed
    history; `merged` and `verified` records refuse."""

    def update(record: dict[str, Any]) -> str:
        require_state(record, "review_clean", "human_approved", "merge_eligible")
        context = "rebind"
        repo = Path(record["repo"])
        integration = integration_checkout()

        # Derived evidence only: no CLI input can supply heads or bases.
        old_base = record.get("base_commit")
        if not old_base:
            raise ReviewError(f"{context}: base_commit is not recorded")
        old_reviewed = record.get("reviewed_head")
        if not old_reviewed:
            raise ReviewError(f"{context}: reviewed_head is not recorded")
        target_branch = record.get("target_branch")
        if not target_branch or target_branch == "HEAD":
            raise ReviewError(
                f"{context}: recorded target_branch is not a named branch"
            )

        require_git_checkout(context, "review worktree", repo)
        require_git_checkout(context, "integration checkout", integration)
        require_clean_tree(context, "review worktree", repo)
        require_clean_tree(context, "integration checkout", integration)

        new_head = resolve_head(context, "review worktree", repo)
        branch = git_branch(repo)
        if branch != target_branch:
            raise ReviewError(
                f"{context}: review worktree {repo} is on {branch}, not the recorded "
                f"source branch {target_branch}"
            )
        new_base = resolve_head(context, "integration checkout", integration)

        # Source-branch evidence resolves in the integration repository,
        # the same namespace the landing preflight fast-forwards from.
        branch_proc = git_proc(
            integration,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{target_branch}^{{commit}}",
        )
        if branch_proc.returncode != 0:
            raise ReviewError(
                f"{context}: recorded source branch {target_branch} does not resolve "
                f"in integration checkout {integration}: "
                f"{branch_proc.stderr.strip() or 'missing ref'}"
            )
        branch_tip = branch_proc.stdout.strip()
        if branch_tip != new_head:
            raise ReviewError(
                f"{context}: source branch {target_branch} tip {branch_tip} in "
                f"integration checkout {integration} does not equal the review "
                f"worktree HEAD {new_head}"
            )
        if new_head == old_reviewed:
            raise ReviewError(
                f"{context}: review worktree HEAD already equals the recorded "
                f"reviewed_head {old_reviewed}; nothing to rebind"
            )

        # Old objects must resolve where they were originally bound: the
        # recorded review repository. A reflog elsewhere does not count.
        for label, sha in (("old base", old_base), ("old reviewed_head", old_reviewed)):
            if not commit_in_repo(repo, sha):
                raise ReviewError(
                    f"{context}: {label} {sha} is not an object in the review "
                    f"worktree {repo}"
                )
        if not is_ancestor(context, repo, old_base, old_reviewed):
            raise ReviewError(
                f"{context}: old base {old_base} is not an ancestor of old "
                f"reviewed_head {old_reviewed} in review worktree {repo}"
            )
        if not commit_in_repo(integration, old_base):
            raise ReviewError(
                f"{context}: old base {old_base} is not an object in integration "
                f"checkout {integration}"
            )
        if not is_ancestor(context, integration, old_base, new_base):
            raise ReviewError(
                f"{context}: old base {old_base} is not an ancestor of integration "
                f"HEAD {new_base} in {integration}; refusing to bless an unseen base"
            )
        if not is_ancestor(context, integration, new_base, new_head):
            raise ReviewError(
                f"{context}: integration HEAD {new_base} is not an ancestor of the "
                f"new reviewed head {new_head}; update and measure the integration "
                "checkout first, then rebase the source branch onto that exact tip"
            )

        # Exact diff identity. The old net change is computed in the review
        # repository (where its objects were bound); the new net change is
        # computed in the integration repository, where both the new base
        # (integration HEAD) and the new head (the source-branch tip) have
        # just been verified to resolve. One pinned patch stream per side
        # feeds both identity factors: the offset-neutral canonical digest
        # and the untouched stable patch identity.
        old_patch = pinned_patch_bytes(context, repo, old_base, old_reviewed)
        new_patch = pinned_patch_bytes(context, integration, new_base, new_head)
        if not old_patch:
            raise ReviewError(
                f"{context}: old net change {old_base}..{old_reviewed} is empty; "
                "nothing was reviewed"
            )
        if not new_patch:
            raise ReviewError(
                f"{context}: new net change {new_base}..{new_head} is empty; "
                "nothing to rebind"
            )
        old_digest = hashlib.sha256(
            canonicalize_patch_bytes(context, old_patch)
        ).hexdigest()
        new_digest = hashlib.sha256(
            canonicalize_patch_bytes(context, new_patch)
        ).hexdigest()
        if old_digest != new_digest:
            raise ReviewError(
                f"{context}: canonical diff digests differ (old {old_digest}, new "
                f"{new_digest}); the rebased change is not the reviewed change -- run "
                "a new governed review"
            )
        old_patch_id = stable_patch_id(context, repo, old_patch)
        new_patch_id = stable_patch_id(context, integration, new_patch)
        if (old_patch_id is None) != (new_patch_id is None):
            raise ReviewError(
                f"{context}: stable patch identity availability is asymmetric "
                f"(old {old_patch_id or 'unavailable'}, new "
                f"{new_patch_id or 'unavailable'}); refusing to discard an available "
                "identity"
            )
        if old_patch_id is None:
            # Both sides unavailable: the exact binary-safe digest above
            # remains authoritative.
            patch_identity = "unavailable"
        elif old_patch_id != new_patch_id:
            raise ReviewError(
                f"{context}: stable patch identities differ (old {old_patch_id}, new "
                f"{new_patch_id})"
            )
        else:
            patch_identity = old_patch_id

        # All validation passed: atomic, append-only mutation.
        prior_state = record["state"]
        old_epoch = gate_epoch(record)
        new_epoch = old_epoch + 1
        if record.get("approval") is not None or record.get("approved_head"):
            record.setdefault("superseded_approvals", []).append(
                {
                    "approval": record.get("approval"),
                    "approved_head": record.get("approved_head"),
                    "prior_state": prior_state,
                    "superseded_at": utc_now(),
                    "reason": "rebind",
                }
            )
        record["history"].append(
            {
                "event": "rebind",
                "timestamp": utc_now(),
                "old_base": old_base,
                "new_base": new_base,
                "old_reviewed_head": old_reviewed,
                "new_reviewed_head": new_head,
                "canonical_diff_digest": old_digest,
                "canonical_diff_algorithm": CANONICAL_DIFF_ALGORITHM,
                "stable_patch_id": patch_identity,
                "prior_state": prior_state,
                "old_gate_epoch": old_epoch,
                "new_gate_epoch": new_epoch,
            }
        )
        record["base_commit"] = new_base
        record["reviewed_head"] = new_head
        record["approved_head"] = None
        record["approval"] = None
        record["gate_epoch"] = new_epoch
        record["state"] = "executed"
        return (
            f"rebind {record['dispatch_id']}: reviewed_head {old_reviewed} -> "
            f"{new_head}, gate epoch {new_epoch}"
        )

    print(locked_update(args.dispatch_id, update))


def command_rebind_dod(args: argparse.Namespace) -> None:
    reason = args.reason.strip()
    if not reason:
        raise ReviewError("rebind-dod: --reason must be non-empty")
    dod_path = Path(args.dod).resolve()
    dod_bytes = dod_path.read_bytes()
    criteria = load_dod(dod_path, None, raw_bytes=dod_bytes)
    new_sha256 = hashlib.sha256(dod_bytes).hexdigest()

    def update(record: dict[str, Any]) -> None:
        require_state(record, "drafted_brief", "brief_revised", "brief_reviewed")
        old_sha256 = record.get("dod_sha256")
        old_count = len(record["dod"])
        record["dod"] = criteria
        record["dod_path"] = str(dod_path)
        record["dod_sha256"] = new_sha256
        record["history"].append(
            {
                "event": "rebind-dod",
                "timestamp": utc_now(),
                "reason": reason,
                "old_sha256": old_sha256,
                "new_sha256": new_sha256,
                "old_criterion_count": old_count,
                "new_criterion_count": len(criteria),
            }
        )

    locked_update(args.dispatch_id, update)
