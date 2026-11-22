from __future__ import annotations

import os
from pathlib import Path

NAME = "service"
NEEDS_STORE = False

VERBS = ("install", "start", "stop", "status", "uninstall")


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        NAME,
        help="Run the monitor as a user service (launchd on macOS, systemd user unit on Linux)",
    )
    parser.add_argument("verb", choices=VERBS)
    parser.add_argument(
        "--human-actor-id", default=None,
        help="Human actor the monitor pages for DLQ rows (install only)",
    )


def handle(_store, args):
    from ... import paths, service
    from .._helpers import DegradedState

    db_path = Path(os.path.abspath(os.path.expanduser(args.db)))
    db_explicit = bool(args.db_explicit)
    root = paths.runtime_root()
    default_root = service.is_default_root(root)
    if args.verb != "install" and args.human_actor_id:
        raise service.ServiceError("--human-actor-id belongs to install; the other verbs act on the installed unit")
    backend = service.make_backend(service.suffix_for(root, default_root=default_root))
    if args.verb == "install":
        if not args.human_actor_id:
            raise service.ServiceError("install needs --human-actor-id, the human actor the monitor pages")
        spec = service.spec_from_environment(
            human_actor_id=args.human_actor_id, db_path=db_path, db_explicit=db_explicit
        )
        return service.install(spec, backend)
    if args.verb == "start":
        return service.start(backend)
    if args.verb == "stop":
        return service.stop(backend, db_path)
    if args.verb == "uninstall":
        return service.uninstall(backend, db_path)
    result = service.status(backend, runtime_root=root, db_path=db_path, db_explicit=db_explicit)
    if not result["ok"]:
        raise DegradedState(result)
    return result
