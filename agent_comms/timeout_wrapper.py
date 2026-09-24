from __future__ import annotations

import argparse
import sys
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent-comms-timeout-wrapper")
    # Supervise-only entry point for the per-dispatch supervisor. The dispatch
    # identity, run token, and TTL arrive over the inherited socketpair bootstrap
    # FD, never on argv, so the run token cannot leak to the native runtime
    # child. There is no legacy best-effort / external-timeout mode: a missing
    # supervisor is unreachable/unconfirmed and stage-2/manual, never a PID
    # signal.
    parser.add_argument("--supervise", action="store_true")
    parser.add_argument("--bootstrap-fd", type=int, default=None)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("command is required")
    if not args.supervise or args.bootstrap_fd is None:
        parser.error("--supervise with --bootstrap-fd is required")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Imported lazily and, when launched as a bare script path with no package
    # context, via an absolute import fallback.
    try:
        from .supervisor import run_supervisor
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from agent_comms.supervisor import run_supervisor

    return run_supervisor(args.bootstrap_fd, args.command)


if __name__ == "__main__":
    raise SystemExit(main())
