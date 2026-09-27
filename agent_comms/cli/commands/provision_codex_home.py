from __future__ import annotations

import os
from pathlib import Path

from ... import paths
from .._helpers import expand_path_value, require_unprotected_or_override

NAME = "provision-codex-home"


def register(subparsers) -> None:
    provision_codex = subparsers.add_parser(NAME)
    provision_codex.add_argument("--actor-id", required=True)
    provision_codex.add_argument(
        "--project-root",
        required=True,
        help="Worker deployment root; supports ~ and ${ENV} expansion",
    )
    provision_codex.add_argument(
        "--codex-home",
        default=None,
        help=(
            "Target CODEX_HOME dir (default: checkout-independent per-actor dir "
            "under the runtime custody root, derived from --actor-id)"
        ),
    )
    provision_codex.add_argument("--override-protected")


def handle(store, args):
    from ... import provisioning

    override_payload = require_unprotected_or_override(store, args.actor_id, args.override_protected)
    # The existing store._db.is_default_db_open flag is the production/scratch
    # boundary: True is the canonical production surface (the default ledger was
    # opened); False is the explicit-DB/scratch surface the frozen tests use. That
    # decision is authoritative and precedes the default/explicit home selection so
    # the scratch surface -- default OR explicit -- never inherits production
    # custody containment or the runtime shared auth source.
    production = bool(store._db.is_default_db_open)
    # The worker's MCP server binds its ledger through this config (the
    # dispatch adapter passes no AGENT_COMMS_DB), so a store other than the
    # default ledger writes its absolute path; the default ledger writes none.
    db_path = None if production else os.path.abspath(os.path.expanduser(str(store._db.db_path)))
    link_absent_source = False
    if production:
        # Canonical production surface: both the default and an explicit --codex-home
        # are contained in the runtime custody root and use the runtime shared auth
        # source, so a home resolved outside the root refuses before any mutation.
        # Do not weaken production containment just because the home was named
        # explicitly. The default materializes the checkout-independent provisioned
        # home under that root.
        if args.codex_home is None:
            codex_home_path = paths.provisioned_codex_home(args.actor_id)
        else:
            codex_home_path = Path(expand_path_value(args.codex_home))
        auth_source = paths.runtime_codex_auth_source()
        enforce_custody_root = True
        link_absent_source = True
    elif args.codex_home is not None and provisioning.within_custody_root(Path(expand_path_value(args.codex_home))):
        # A home under the runtime custody root is custody-managed whichever
        # ledger it serves (the adapter's own rule), so the repair doctor
        # prescribes for a setup-provisioned worker on an explicit ledger
        # rebuilds what setup built: shared runtime auth, linked before login.
        codex_home_path = Path(expand_path_value(args.codex_home))
        auth_source = paths.runtime_codex_auth_source()
        enforce_custody_root = True
        link_absent_source = True
    else:
        # Explicit-DB / scratch surface (is_default_db_open False): preserve the
        # frozen legacy behavior for BOTH forms -- honored as written, legacy repo-
        # local auth source, no containment enforcement. The default resolves to the
        # legacy repo-local per-actor home; an explicit home is honored as written.
        # To move a legacy actor into custody, provision it on the default database.
        if args.codex_home is None:
            codex_home_path = paths.codex_home(args.actor_id)
        else:
            codex_home_path = Path(expand_path_value(args.codex_home))
        auth_source = paths.codex_auth_source()
        enforce_custody_root = False
    written = provisioning.write_codex_home(
        codex_home_path,
        args.actor_id,
        expand_path_value(args.project_root),
        auth_source=auth_source,
        enforce_custody_root=enforce_custody_root,
        link_absent_source=link_absent_source,
        db_path=db_path,
    )
    from ... import db, release

    try:
        git_info = release.repo_git_info(paths.REPO_ROOT)
    except Exception:
        git_info = {
            "git_commit": "unknown",
            "git_describe": "unknown",
            "git_head_state": "unknown",
            "git_branch": "unknown",
            "git_exact_tag": None,
            "pin_worktree": "unknown",
        }
    launcher = {
        "command": str(paths.mcp_command()),
        "repo_root": str(paths.REPO_ROOT),
        **{
            key: git_info[key]
            for key in (
                "git_commit",
                "git_describe",
                "git_head_state",
                "git_branch",
                "git_exact_tag",
                "pin_worktree",
            )
        },
        "ledger_schema_version": db.LEDGER_SCHEMA_VERSION,
    }
    result = {"written": [str(path) for path in written], "launcher": launcher}
    if override_payload is not None:
        result["override_protected"] = override_payload
    return result
