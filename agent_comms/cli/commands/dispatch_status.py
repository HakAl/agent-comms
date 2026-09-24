from __future__ import annotations

from .._helpers import DegradedState, print_dispatch_table, print_json

NAME = "dispatch-status"


def register(subparsers) -> None:
    dispatch_status = subparsers.add_parser(NAME)
    dispatch_status.add_argument("--status", help="Filter by ledger status; use --status dlq for the DLQ view")
    dispatch_status.add_argument("--limit", type=int, default=50)
    dispatch_status.add_argument("--json", action="store_true", help="Print raw JSON instead of a compact table")


def _codex_actor_defects(store) -> list[dict]:
    from ...codex_refresh_driver import scan_codex_actors

    _actors, defects = scan_codex_actors(store)
    return defects


_REMEDIES = {
    "malformed_spawn_json": "repair the actor's spawn JSON or deregister it",
    "malformed_spawn_env": "repair the actor's spawn env or deregister it",
    "missing_codex_home": "set spawn.env.CODEX_HOME or deregister the actor",
}


def handle(store, args):
    dispatches = store.list_dispatches(status=args.status, limit=args.limit)
    defects = _codex_actor_defects(store)
    if args.json:
        # JSON shape is a list of dispatch rows (unchanged).
        if defects:
            raise DegradedState({"dispatches": dispatches, "actor_defects": defects})
        print_json(dispatches)
    else:
        print_dispatch_table(dispatches)
        for dispatch in dispatches:
            if dispatch.get("observed_values_malformed"):
                print(f"{dispatch['dispatch_id']} observed_values=MALFORMED")
            observed = dispatch.get("observed_values") or {}
            for key in ("worker_log", "worker_events"):
                if key in observed:
                    print(f"{dispatch['dispatch_id']} {key}={observed[key]}")
        from ...codex_auth_refresh import dead_credentials

        for row in dead_credentials(store):
            print(
                f"codex-credential-dead {row['lineage_key']} reason={row['dead_reason']} "
                f"since={row['dead_at']}  remedy=run codex login for that CODEX_HOME"
            )
        for defect in defects:
            print(
                f"custody-defect {defect['actor_id']} {defect['code']}  "
                f"remedy={_REMEDIES[defect['code']]}"
            )
        if defects:
            raise DegradedState(already_printed=True)
    return None
