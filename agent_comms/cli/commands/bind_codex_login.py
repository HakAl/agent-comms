from __future__ import annotations

from ...schema import ValidationError

NAME = "bind-codex-login"
NEEDS_STORE = False


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        NAME,
        help="Share your own Codex login (CODEX_HOME or ~/.codex) with the workers",
    )
    parser.add_argument(
        "--replace", action="store_true",
        help="Replace a separate worker login or a link elsewhere; a replaced file is moved aside, not deleted",
    )


def handle(_store, args):
    from ...provisioning import link_shared_codex_auth

    result = link_shared_codex_auth(replace=args.replace)
    if result["status"] == "separate":
        raise ValidationError(
            f"{result['link']} is a separate worker login, not a link to your own ({result['source']}); "
            f"share yours with: agent-comms {NAME} --replace (the file is moved aside, not deleted)"
        )
    if result["status"] == "elsewhere":
        raise ValidationError(
            f"{result['link']} links to {result['current']}, not your own login ({result['source']}); "
            f"relink it with: agent-comms {NAME} --replace"
        )
    return result
