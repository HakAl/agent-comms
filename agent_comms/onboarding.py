from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import paths, provisioning
from .schema import ValidationError, identity_to_path_segment
from .spawn import render_spawn
from .store import Store


def onboard_worker(
    store: Store,
    *,
    team: str,
    runtime: str,
    actor_id: str,
    owner: str,
    project_root: str | None = None,
    worktree_root: str | None = None,
    repo_root: Path = paths.REPO_ROOT,
    override_protected: str | None = None,
) -> dict:
    """Provision a new dispatchable worker actor and its git worktree."""
    team = team.strip()
    runtime = runtime.strip()
    if not team:
        raise ValidationError("team must not be empty")
    if not actor_id:
        raise ValidationError("actor_id must not be empty")

    repo_root = repo_root.expanduser().resolve()
    root_for_worktrees = (
        Path(worktree_root).expanduser().resolve()
        if worktree_root is not None
        else repo_root.parent
    )
    actor_segment = identity_to_path_segment(actor_id)
    worktree_path = root_for_worktrees / f"agent-comms-{actor_segment}"
    branch_name = f"worker/{actor_segment}"

    override_payload = _preflight(store, runtime, actor_id, worktree_path, branch_name, repo_root, override_protected)

    created_worktree = False
    try:
        _run_git(
            ["worktree", "add", "-b", branch_name, str(worktree_path), "HEAD"],
            repo_root,
            f"failed to create worktree for {actor_id}",
        )
        created_worktree = True

        project_root_value = (
            str(Path(project_root).expanduser().resolve())
            if project_root is not None
            else str(worktree_path)
        )

        codex_home = None
        if runtime == "codex":
            # store._db.is_default_db_open is the production/scratch boundary.
            if store._db.is_default_db_open:
                # Canonical production surface: the checkout-independent per-actor
                # home under the runtime custody root, the runtime shared auth
                # source, containment enforced before any mutation.
                codex_home_path = paths.provisioned_codex_home(actor_id)
                provisioning.write_codex_home(
                    codex_home_path,
                    actor_id,
                    project_root_value,
                    auth_source=paths.runtime_codex_auth_source(),
                    enforce_custody_root=True,
                )
            else:
                # Explicit-DB / scratch Store: preserve the byte-frozen legacy
                # worktree-local home + legacy auth source behavior.
                codex_home_path = worktree_path / ".agent-comms-codex-home"
                provisioning.write_codex_home(
                    codex_home_path,
                    actor_id,
                    project_root_value,
                    auth_source=paths.codex_auth_source(),
                )
            codex_home = str(codex_home_path)

        spawn = render_spawn(runtime, actor_id, codex_home=codex_home)
        actor = store.register_agent_actor(
            actor_id,
            team,
            "worker",
            project_root_value,
            [],
            runtime=runtime,
            spawn=spawn,
            protected=True,
            owner=owner,
        )
    except Exception as exc:
        if created_worktree:
            raise ValidationError(
                f"onboard-worker failed after creating worktree {worktree_path} "
                f"and branch {branch_name}; clean them up manually; cause: {exc}"
            ) from exc
        raise

    result = {
        "actor": actor,
        "actor_id": actor_id,
        "team": team,
        "runtime": runtime,
        "worktree_path": str(worktree_path),
        "branch": branch_name,
        "project_root": project_root_value,
        "codex_home": codex_home,
        "codex_auth_required": runtime == "codex",
    }
    if override_payload is not None:
        result["override_protected"] = override_payload
    return result


def _preflight(
    store: Store,
    runtime: str,
    actor_id: str,
    worktree_path: Path,
    branch_name: str,
    repo_root: Path,
    override_protected: str | None,
) -> dict | None:
    from .cli._helpers import require_unprotected_or_override

    override_payload = None
    if any(actor["id"] == actor_id for actor in store.list_actors()):
        protection = store.actor_protection(actor_id)
        if protection is None or not protection["protected"]:
            raise ValidationError(f"actor already exists: {actor_id}")
        override_payload = require_unprotected_or_override(store, actor_id, override_protected)
    if os.path.lexists(worktree_path):
        raise ValidationError(f"target worktree path already exists: {worktree_path}")
    if _branch_exists(repo_root, branch_name):
        raise ValidationError(f"branch already exists: {branch_name}")
    render_spawn(runtime, actor_id)
    return override_payload


def _branch_exists(repo_root: Path, branch_name: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode in {0, 1}:
        return result.returncode == 0
    raise ValidationError(
        f"failed to inspect branch {branch_name!r} in {repo_root}: "
        f"{(result.stderr or result.stdout).strip()}"
    )


def _run_git(args: list[str], repo_root: Path, failure: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValidationError(f"{failure}: {detail}")
