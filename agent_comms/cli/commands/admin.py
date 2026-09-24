from __future__ import annotations

from ...adapters.registry import adapter_for
from ...schema import ValidationError
from ...store import WORKER_DISPATCH_TTL_SECONDS
from .._helpers import protected_actor_notice, refs_from_args, require_admin_credential

NAME = "admin"


def register(subparsers) -> None:
    admin = subparsers.add_parser(NAME)
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    admin_send = admin_sub.add_parser("send")
    admin_send.add_argument("--from-actor-id", required=True)
    admin_send.add_argument("--to", action="append", required=True)
    admin_send.add_argument("--subject", required=True)
    admin_send.add_argument("--body", required=True)
    admin_send.add_argument("--ref", action="append", default=[], help="Path ref; repeatable")
    admin_send.add_argument("--ref-summary", action="append", default=[], help="Summary for matching --ref")
    admin_send.add_argument("--priority", default="normal")
    admin_send.add_argument("--requires-ack", action="store_true")
    admin_send.add_argument("--parent-message-id")

    admin_dispatch = admin_sub.add_parser("dispatch")
    admin_dispatch.add_argument("--from-actor-id", required=True)
    admin_dispatch.add_argument("--target-actor-id", required=True)
    admin_dispatch.add_argument("--idempotency-key", required=True)
    admin_dispatch.add_argument("--requested-policy", required=True)
    admin_dispatch.add_argument("--override-reason", required=True)
    admin_dispatch.add_argument("--subject", required=True)
    admin_dispatch.add_argument("--body", help="Inline logical body (bounded); exclusive with --body-file")
    admin_dispatch.add_argument(
        "--body-file",
        help="Repository-relative path to the logical body, resolved under --source-root",
    )
    admin_dispatch.add_argument(
        "--payload-origin",
        help="Required with --body-file: authored_brief, generated_artifact, or verbatim_source",
    )
    admin_dispatch.add_argument(
        "--source-root",
        help=(
            "Explicit absolute source root for --body-file; the working directory is never "
            "inferred. An agent producer's root must resolve to its registered project_root."
        ),
    )
    admin_dispatch.add_argument("--ref", action="append", default=[], help="Path ref; repeatable")
    admin_dispatch.add_argument("--ref-summary", action="append", default=[], help="Summary for matching --ref")

    admin_retry_spawn = admin_sub.add_parser("retry-spawn")
    admin_retry_spawn.add_argument("--dispatch-id", required=True)
    admin_retry_spawn.add_argument("--ttl-seconds", type=int, default=WORKER_DISPATCH_TTL_SECONDS)

    admin_cancel = admin_sub.add_parser("cancel-dispatch")
    admin_cancel.add_argument("--from-actor-id", required=True)
    admin_cancel.add_argument("--dispatch-id", required=True)
    admin_cancel.add_argument("--reason", required=True)

    # Emergency settlement (T7). ``--from-actor-id`` and ``--dispatch-id`` are
    # required in BOTH modes; the mode is a pinned, mutually exclusive choice
    # between ``--dry-run`` and ``--execute-plan <plan>``, enforced by the parser
    # (exactly one is required). Dry-run additionally requires ``--reason``;
    # execution additionally requires the literal
    # ``--release-with-termination-unconfirmed`` and takes its reason ONLY from
    # the sealed plan. No snapshot / expected-value arguments are defined, so a
    # caller who tries to pass one is rejected by the parser as an unrecognized
    # argument -- execution re-derives the snapshot from the sealed plan and
    # never accepts caller-copied expected values.
    admin_settle = admin_sub.add_parser("settle-dispatch")
    admin_settle.add_argument("--from-actor-id", required=True)
    admin_settle.add_argument("--dispatch-id", required=True)
    admin_settle.add_argument("--reason")
    settle_mode = admin_settle.add_mutually_exclusive_group(required=True)
    settle_mode.add_argument("--dry-run", action="store_true")
    settle_mode.add_argument("--execute-plan", dest="execute_plan", metavar="PLAN")
    admin_settle.add_argument(
        "--release-with-termination-unconfirmed",
        dest="release_with_termination_unconfirmed",
        action="store_true",
    )

    admin_handoff = admin_sub.add_parser("handoff-post")
    admin_handoff.add_argument("--target-actor-id", required=True)
    admin_handoff.add_argument("--created-by-actor-id", required=True)
    admin_handoff.add_argument("--body-file", required=True)

    admin_transfer = admin_sub.add_parser("transfer-worker")
    admin_transfer.add_argument("--worker", required=True)
    admin_transfer.add_argument("--owner", required=True)


def _settlement_secret(args) -> str:
    """Return the operator secret verified during preflight.

    ``run`` always calls ``preflight`` before Store construction, which stashes
    the verified secret. If ``handle`` is invoked directly (outside ``run``), the
    credential is verified here as a fallback so the gate is never skipped.
    """
    secret = getattr(args, "_settlement_secret", None)
    if secret is None:
        secret = require_admin_credential()
    return secret


def preflight(args) -> None:
    """Validate the settlement credential and CLI mode BEFORE Store construction.

    Runs for ``admin settle-dispatch`` only. It verifies the operator credential
    (stashing the verified secret on ``args`` as the plan HMAC key) and enforces
    the pinned mode cross-constraints, all before any Store/Database exists, so a
    bad/missing credential or an invalid mode refuses without a single filesystem
    effect (no parent directory, database file, schema, or sidecar). The parser
    has already enforced that exactly one of ``--dry-run`` / ``--execute-plan`` is
    present; this adds the reason/acknowledgement constraints.
    """
    if getattr(args, "command", None) != NAME:
        return
    if getattr(args, "admin_command", None) != "settle-dispatch":
        return
    # Credential preflight first: a missing/bad operator credential refuses here,
    # before Store/Database construction and any settlement work.
    secret = require_admin_credential()
    if args.dry_run:
        if args.release_with_termination_unconfirmed:
            raise ValidationError(
                "--dry-run does not accept --release-with-termination-unconfirmed"
            )
        if args.reason is None:
            raise ValidationError("--dry-run requires a bounded nonempty --reason")
    else:
        # Execution mode (``--execute-plan`` present per the parser's exclusive
        # required group): the reason comes ONLY from the sealed plan, and the
        # literal release acknowledgement is mandatory.
        if args.reason is not None:
            raise ValidationError(
                "settlement execution does not accept a caller-supplied --reason; "
                "the reason is bound in the sealed plan"
            )
        if not args.release_with_termination_unconfirmed:
            raise ValidationError(
                "settlement execution requires the literal "
                "--release-with-termination-unconfirmed acknowledgement"
            )
    args._settlement_secret = secret


def handle(store, args):
    if args.admin_command == "send":
        require_admin_credential()
        return store.send_message(
            from_agent=args.from_actor_id,
            to_agents=args.to,
            subject=args.subject,
            body=args.body,
            refs=refs_from_args(args.ref, args.ref_summary),
            priority=args.priority,
            requires_ack=args.requires_ack,
            parent_message_id=args.parent_message_id,
        )
    elif args.admin_command == "dispatch":
        require_admin_credential()
        protection = store.actor_protection(args.target_actor_id)
        if protection is not None and protection["protected"]:
            protected_actor_notice(protection)
        return store.dispatch_agent(
            producer_actor_id=args.from_actor_id,
            target_actor_id=args.target_actor_id,
            idempotency_key=args.idempotency_key,
            subject=args.subject,
            body=args.body,
            refs=refs_from_args(args.ref, args.ref_summary),
            # The parser always defines the payload-source options; ``getattr``
            # keeps ``handle`` callable with legacy pre-payload namespaces that
            # never carried them (inline-body dispatches).
            body_file=getattr(args, "body_file", None),
            payload_origin=getattr(args, "payload_origin", None),
            source_root=getattr(args, "source_root", None),
            requested_policy=args.requested_policy,
            override_reason=args.override_reason,
            adapter_for_runtime=adapter_for,
        )
    elif args.admin_command == "retry-spawn":
        require_admin_credential()
        return store.retry_spawn(
            dispatch_id=args.dispatch_id,
            adapter_for_runtime=adapter_for,
            ttl_seconds=args.ttl_seconds,
        )
    elif args.admin_command == "cancel-dispatch":
        # Credential is verified BEFORE any Store mutation or cancellation
        # request. The accepted engine's admin authorization then requires the
        # supplied actor to be registered and kind == human, and applies the same
        # bounded cancellation semantics (idempotency, conflict/terminal refusal)
        # under authority='admin' with the runtime adapter driving reachable
        # in-flight termination.
        require_admin_credential()
        return store.request_cancellation(
            args.dispatch_id,
            requesting_actor_id=args.from_actor_id,
            reason=args.reason,
            authority="admin",
            adapter_for_runtime=adapter_for,
        )
    elif args.admin_command == "settle-dispatch":
        # The credential was verified in ``preflight`` (before Store/Database
        # construction) and the verified secret stashed on ``args`` as the plan's
        # HMAC key; it is never printed, persisted, or surfaced. Preflight also
        # enforced the pinned mode cross-constraints. Reaching here, ``args`` is a
        # valid, credentialed dry-run or execute request. The dry run mints the
        # sealed plan (read-only, no mutation); execution re-verifies it and
        # settles atomically.
        secret = _settlement_secret(args)
        if args.dry_run:
            return store.settle_dispatch_preview(
                args.dispatch_id,
                actor_id=args.from_actor_id,
                reason=args.reason,
                secret=secret,
            )
        return store.settle_dispatch_execute(
            args.dispatch_id,
            actor_id=args.from_actor_id,
            plan=args.execute_plan,
            secret=secret,
            release_ack=args.release_with_termination_unconfirmed,
        )
    elif args.admin_command == "handoff-post":
        require_admin_credential()
        with open(args.body_file, encoding="utf-8") as handle:
            body = handle.read()
        return store.post_handoff(
            args.target_actor_id,
            body,
            [],
            created_by_actor_id=args.created_by_actor_id,
        )
    elif args.admin_command == "transfer-worker":
        require_admin_credential()
        return store.transfer_worker(args.worker, args.owner)
    else:
        raise AssertionError(args.admin_command)
