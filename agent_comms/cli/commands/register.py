from __future__ import annotations

from .._helpers import expand_path_value, load_spawn_arg, parse_json_object, require_unprotected_or_override

NAME = "register"


def register(subparsers) -> None:
    register = subparsers.add_parser(NAME)
    register.add_argument("agent_id")
    register.add_argument("--team", required=True)
    register.add_argument("--role", default="architect")
    register.add_argument("--owner")
    register.add_argument("--project-root", required=True)
    register.add_argument("--capability", action="append", default=[])
    register.add_argument("--runtime")
    spawn_source = register.add_mutually_exclusive_group()
    spawn_source.add_argument("--spawn", help="Path to a JSON object containing the runtime spawn block")
    spawn_source.add_argument("--spawn-json", type=parse_json_object, help="Inline JSON object containing the runtime spawn block")
    register.add_argument("--override-protected")


def handle(store, args):
    override_payload = require_unprotected_or_override(store, args.agent_id, args.override_protected)
    result = store.register_agent_actor(
        args.agent_id,
        args.team,
        args.role,
        expand_path_value(args.project_root),
        args.capability,
        runtime=args.runtime,
        spawn=load_spawn_arg(args),
        owner=args.owner,
    )
    if override_payload is not None:
        result["override_protected"] = override_payload
    return result
