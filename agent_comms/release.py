from __future__ import annotations

import subprocess

from . import __version__, code_identity, paths, runtime_pins


def _run_git(repo_root, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def repo_git_info(repo_root=None) -> dict:
    root = paths.REPO_ROOT if repo_root is None else repo_root
    commit = _run_git(root, "rev-parse", "--short=12", "HEAD")
    branch = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    exact_tag = _run_git(root, "describe", "--tags", "--exact-match")
    describe = _run_git(root, "describe", "--tags", "--always", "--dirty")
    if commit is None:
        head_state = "unknown"
        branch_name = "unknown"
        pin_worktree = "unknown"
    elif branch == "HEAD":
        head_state = "detached"
        branch_name = None
        pin_worktree = bool(exact_tag)
    elif branch:
        head_state = "live"
        branch_name = branch
        pin_worktree = False
    else:
        head_state = "unknown"
        branch_name = "unknown"
        pin_worktree = "unknown"
    return {
        "repo_root": str(root),
        "git_commit": commit or "unknown",
        "git_branch": branch_name,
        "git_describe": describe or "unknown",
        "git_exact_tag": exact_tag,
        "git_head_state": head_state,
        "pin_worktree": pin_worktree,
    }


def release_info() -> dict:
    return {
        "version": __version__,
        "code_identity": code_identity.LOADED_CODE_IDENTITY,
        "contract_version": code_identity.CONTRACT_VERSION,
        "certified_runtimes": runtime_pins.load_runtime_pins(),
        **repo_git_info(paths.REPO_ROOT),
    }


def startup_report(info: dict | None = None) -> str:
    payload = release_info() if info is None else info
    return (
        "agent-comms startup: "
        f"version={payload.get('version', 'unknown')} "
        f"repo_root={payload.get('repo_root', 'unknown')} "
        f"git={payload.get('git_describe', 'unknown')} "
        f"head={payload.get('git_head_state', 'unknown')} "
        f"branch={payload.get('git_branch', 'unknown')} "
        f"tag={payload.get('git_exact_tag') or 'none'} "
        f"pin_worktree={payload.get('pin_worktree', 'unknown')} "
        "ledger_schema_advances_still_require_upgrade=true"
    )
