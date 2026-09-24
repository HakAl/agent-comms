from __future__ import annotations

from .._helpers import expand_path_value

NAME = "onboard-worker"


def register(subparsers) -> None:
    onboard = subparsers.add_parser(NAME)
    onboard.add_argument("--team", required=True)
    onboard.add_argument("--runtime", required=True)
    onboard.add_argument("--actor-id", required=True)
    onboard.add_argument("--owner", required=True)
    onboard.add_argument("--project-root", help="Worker project root; defaults to the new worktree")
    onboard.add_argument("--worktree-root", help="Parent directory for the new worker worktree")
    onboard.add_argument("--override-protected")


def handle(store, args):
    from ...onboarding import onboard_worker

    return onboard_worker(
        store,
        team=args.team,
        runtime=args.runtime,
        actor_id=args.actor_id,
        owner=args.owner,
        project_root=expand_path_value(args.project_root) if args.project_root else None,
        worktree_root=expand_path_value(args.worktree_root) if args.worktree_root else None,
        override_protected=args.override_protected,
    )
