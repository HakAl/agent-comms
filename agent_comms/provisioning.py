"""Provisioning logic for runtime deployment artifacts.

These artifacts (currently the codex ``CODEX_HOME`` config) are *generated*
from the repo location + the worker's deployment root, never tracked with one
machine's absolute paths baked in. The repo-internal paths come from
``paths``; the deployment root is operator data passed in. This is the seed of
the Slice 4 ``onboard`` command — for now it materializes the pilot codex
worker's home.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

from . import paths
from .codex_binding import toml_str as _toml_str
from .schema import ValidationError, identity_to_path_segment


def _resolved_within(child: Path, root: Path) -> bool:
    """True when ``child`` resolves inside ``root`` (both fully resolved)."""
    try:
        child_resolved = child.resolve()
        root_resolved = root.resolve()
    except OSError:
        return False
    if child_resolved == root_resolved:
        return True
    return root_resolved in child_resolved.parents


def within_custody_root(codex_home: Path) -> bool:
    """Whether ``codex_home`` resolves inside the runtime custody root."""
    return _resolved_within(codex_home, paths.codex_custody_root())


def refuse_home_outside_custody_root(codex_home: Path) -> None:
    """Refuse a production Codex home resolved outside the runtime custody root.

    Containment is production-authoritative: it is ALWAYS enforced against the
    runtime custody root (``paths.codex_custody_root()``), never gated on an
    environment switch that production never sets. Scratch/test materialization
    opts out explicitly via ``write_codex_home(..., enforce_custody_root=False)``.
    """
    root = paths.codex_custody_root()
    if not _resolved_within(codex_home, root):
        raise ValidationError(
            "refusing to provision a Codex home outside the runtime custody root; "
            f"home={codex_home}; custody_root={root}; "
            "provision it under the custody root (the default home), or "
            "pass enforce_custody_root=False for scratch/test materialization"
        )


def refuse_shared_login_overlap(project_root: str | Path, *, actor_id: str | None = None) -> None:
    """Refuse to share the operator's login with a worker whose project root
    contains or sits inside that login, the operator's Codex home, the runtime
    root or the custody root. The same check ``setup`` applies, repeated at
    every provisioning entry (bootstrap of an existing registry, onboarding,
    provision-codex-home), because a registry or a ``CODEX_HOME`` can change
    after setup."""
    # The runtime's shared file is what the worker will chain into; when an
    # existing link or file is kept, that is the credential, not the ambient one.
    problem = paths.shared_login_boundary_problem(Path(project_root), auth=paths.runtime_codex_auth_source())
    if problem:
        who = f"worker {actor_id}: " if actor_id else ""
        raise ValidationError(f"{who}project root {problem}")


def _replace_with_symlink(target: Path, source: Path) -> None:
    """Install ``target -> source`` atomically inside a home agent-comms owns,
    over a symlink if one is there."""
    tmp = target.with_name(f".auth.json.{os.getpid()}.{uuid.uuid4().hex}.new")
    try:
        tmp.symlink_to(source)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _publish_symlink(link: Path, source: Path) -> bool:
    """Create ``link -> source`` only if nothing is at ``link``; False when
    something appeared there first (it is left untouched)."""
    try:
        link.symlink_to(source)
    except FileExistsError:
        return False
    return True


def link_shared_codex_auth(source: Path | None = None, *, replace: bool = False) -> dict:
    """Point the runtime's shared ``auth.json`` at the operator's own login.

    The link is installed even when ``source`` (the operator's login file by
    default) does not exist yet, so a later ``codex login`` completes setup
    with no further agent-comms command. An existing link to that file is
    left alone (``already``). A link elsewhere (``elsewhere``) or a regular
    file, the separate worker login an earlier install made (``separate``),
    is kept unless ``replace``; a replaced regular file is moved aside as
    ``auth.json.separate-<stamp>``, never deleted. Publication is create-only:
    whatever another process puts at the path between the inspection and the
    link (a concurrent login or bind) is kept, and the call reports the
    failed installation instead of replacing it. A source inside the runtime
    root or the custody root is refused before anything is written.
    """
    source = paths.operator_codex_auth() if source is None else Path(source)
    problem = paths.operator_codex_auth_problem(source)
    if problem:
        raise ValidationError(problem)
    source = Path(os.path.abspath(source))
    link = paths.runtime_codex_auth_source()
    result = {"link": str(link), "source": str(source), "source_present": source.is_file()}
    current = None
    aside = None
    if link.is_symlink():
        current = os.readlink(link)
        if os.path.realpath(link) == os.path.realpath(source):
            return {**result, "status": "already"}
        if not replace:
            return {**result, "status": "elsewhere", "current": current}
        aside = link.with_name(f"auth.json.link-{_aside_stamp()}")
    elif link.is_file():
        if not replace:
            return {**result, "status": "separate"}
        aside = link.with_name(f"auth.json.separate-{_aside_stamp()}")
    elif link.exists():
        raise ValidationError(f"unexpected non-file at {link}; move it away and run agent-comms bind-codex-login")
    if aside is not None:
        # Move the old object aside first (a symlink or a file, whatever it is
        # by now), so nothing is ever unlinked in place.
        os.rename(link, aside)
    link.parent.mkdir(parents=True, exist_ok=True)
    if not _publish_symlink(link, source):
        kept = f"; your previous file is kept at {aside}" if aside is not None and not aside.is_symlink() else ""
        raise ValidationError(
            f"{link} appeared while binding (another codex login or bind-codex-login running?); "
            f"nothing was replaced{kept}; look at it, then rerun: agent-comms bind-codex-login"
            + (" --replace" if replace else "")
        )
    if aside is None:
        return {**result, "status": "linked"}
    if aside.is_symlink():
        # The link we moved aside: its target string is the record of it.
        aside.unlink()
        return {**result, "status": "linked", "replaced": current}
    return {**result, "status": "linked", "moved_aside": str(aside)}


def _aside_stamp() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def render_codex_base_config(project_root: str) -> str:
    """The base ``config.toml`` codex loads from a redirected ``CODEX_HOME``.

    It holds trust only: the worker's ``project_root`` and the operator's
    home are trusted sandbox roots, and an install with its own
    ``AGENT_COMMS_HOME`` outside the operator's home is trusted too. The
    worker's ``agent-comms`` server is not here: the adapter binds it per run
    with ``-c`` (``codex_binding``), which carries the actor, ledger and
    install, and the spawn passes the sandbox and approval policy as flags.
    """
    home = paths.home_override()
    trusted = [project_root, str(Path.home())]
    if home is not None and not home.is_relative_to(Path.home()):
        trusted.append(str(home))
    # Every path is serialized as a TOML basic string: JSON string escapes are
    # valid there, so a quote or backslash in a path stays data. A key twice
    # (the project is the home, say) is invalid TOML, so each is trusted once.
    trust_tables = []
    for path in dict.fromkeys(trusted):
        trust_tables += [f"[projects.{_toml_str(path)}]", 'trust_level = "trusted"', ""]
    return "\n".join(
        [
            "# Generated by agent_comms.provisioning (do not hand-edit; regenerate).",
            "# The agent-comms server is bound per run with -c, not here.",
            "",
            *trust_tables,
        ]
    )


def write_codex_home(
    codex_home: Path,
    actor_id: str,
    project_root: str,
    *,
    auth_source: Path | None = None,
    enforce_custody_root: bool = False,
    link_absent_source: bool = False,
) -> list[Path]:
    """Materialize the codex base config into ``codex_home``.

    Returns the written paths. Auth is optionally linked from a shared source;
    this always (re)writes the generated ``config.toml``. A per-actor profile
    an older version wrote is left alone: the spawn no longer loads it. This stays a reusable
    low-level primitive: ``enforce_custody_root`` defaults to ``False`` so
    existing scratch callers keep working without edits. Production onboarding /
    provision commands pass ``enforce_custody_root=True`` explicitly so a home
    resolved outside the runtime custody root refuses before any mutation.

    ``link_absent_source`` installs the ``auth.json`` symlink even when the
    shared source does not exist yet (a fresh install before the first
    ``codex login``): the link resolves the moment the login writes the
    target, and the warning names that login command. The default keeps the
    legacy skip for a missing source.
    """
    if enforce_custody_root:
        refuse_home_outside_custody_root(codex_home)
    # A non-canonical actor id is refused before anything is written, though
    # the home no longer holds a file named after it.
    identity_to_path_segment(actor_id)
    codex_home.mkdir(parents=True, exist_ok=True)
    base = codex_home / "config.toml"
    base.write_text(render_codex_base_config(project_root), encoding="utf-8")
    written = [base]

    if auth_source is None:
        return written

    # Absolute but unresolved: the worker's link points at the runtime's
    # shared file, which in turn links to the operator's login, so the chain
    # survives a later ``bind-codex-login`` and the lineage key stays the
    # operator's file.
    auth_source = Path(os.path.abspath(auth_source))
    if not auth_source.is_file():
        if not link_absent_source or auth_source.exists():
            print(
                f"warning: auth source {auth_source} not a regular file; "
                f"you must authenticate codex in {codex_home} before dispatching",
                file=sys.stderr,
            )
            return written
        print(
            f"warning: auth source {auth_source} does not exist yet; linked anyway, "
            f"log in with: {paths.codex_login_command(auth_source)}",
            file=sys.stderr,
        )

    target = codex_home / "auth.json"
    if target.is_symlink():
        pass
    elif target.is_file():
        print(
            f"warning: regular file at {target} not replaced; remove "
            f"by hand to enable shared-auth symlink",
            file=sys.stderr,
        )
        return written
    elif target.exists():
        raise RuntimeError(
            f"unexpected non-file non-symlink at {target}; refusing to install symlink"
        )

    _replace_with_symlink(target, auth_source)
    written.append(target)
    return written
