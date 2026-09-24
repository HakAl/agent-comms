"""Landing policy behind the ``agent_comms.review`` facade.

The landing preflight shared by approve and gate-merge (the reviewed source must
fast-forward into the integration checkout), plus the same-repository check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_comms.reviewing.contracts import ReviewError
from agent_comms.reviewing.git_evidence import (
    commit_in_repo,
    git_common_dir,
    git_proc,
    integration_checkout,
    is_ancestor,
    require_clean_tree,
    require_git_checkout,
    resolve_head,
)


def landing_preflight(record: dict[str, Any]) -> None:
    """Read-only fast-forwardability preflight shared by approve and gate-merge.

    Fails loudly unless, at the instant checked: the review worktree and the
    integration checkout are clean Git checkouts, the review worktree HEAD and
    the recorded source-branch tip (resolved in the integration repository,
    the namespace cycle-land merges from) equal reviewed_head, and integration
    HEAD is an ancestor of reviewed_head so the fast-forward can succeed.
    """
    context = "landing preflight"
    repo = Path(record["repo"])
    integration = integration_checkout()
    reviewed_head = record.get("reviewed_head")
    if not reviewed_head:
        raise ReviewError(f"{context}: reviewed_head is not recorded")
    target_branch = record.get("target_branch")
    if not target_branch or target_branch == "HEAD":
        raise ReviewError(f"{context}: recorded target_branch is not a named branch")
    require_git_checkout(context, "review worktree", repo)
    require_git_checkout(context, "integration checkout", integration)
    integration_head = resolve_head(context, "integration checkout", integration)
    require_clean_tree(context, "review worktree", repo)
    require_clean_tree(context, "integration checkout", integration)
    repo_head = resolve_head(context, "review worktree", repo)
    if repo_head != reviewed_head:
        raise ReviewError(
            f"{context}: review worktree HEAD {repo_head} in {repo} does not equal reviewed_head {reviewed_head}"
        )
    if not commit_in_repo(integration, reviewed_head):
        raise ReviewError(
            f"{context}: reviewed_head {reviewed_head} is not an object in integration checkout {integration}"
        )
    branch_proc = git_proc(
        integration,
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{target_branch}^{{commit}}",
    )
    if branch_proc.returncode != 0:
        raise ReviewError(
            f"{context}: recorded source branch {target_branch} does not resolve in integration checkout "
            f"{integration}: {branch_proc.stderr.strip() or 'missing ref'}"
        )
    branch_tip = branch_proc.stdout.strip()
    if branch_tip != reviewed_head:
        raise ReviewError(
            f"{context}: source branch {target_branch} tip {branch_tip} in integration checkout {integration} "
            f"does not equal reviewed_head {reviewed_head}"
        )
    if not is_ancestor(context, integration, integration_head, reviewed_head):
        raise ReviewError(
            f"{context}: integration HEAD {integration_head} in {integration} is not an ancestor of "
            f"reviewed_head {reviewed_head}; the reviewed source is not fast-forwardable"
        )


def same_git_repository(a: Path, b: Path) -> bool:
    common_a = git_common_dir(a)
    common_b = git_common_dir(b)
    return common_a is not None and common_a == common_b
