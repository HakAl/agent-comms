"""Single source of truth for agent-comms-internal paths.

Two kinds of path live here. Package-relative paths (the hook script, the
code-identity surface) are derived from the package location so they resolve
the same from a checkout and from an installed wheel. Runtime paths (the
ledger, config, logs, review and approval records) live under the runtime
root, ``AGENT_COMMS_HOME`` when set and ``~/.agent-comms`` otherwise, and
never under the source tree or the package directory, so deleting the source
tree cannot affect an install and a second install can run beside the first. External
deployment paths (a worker's ``project_root``) are operator data and are NOT
derived here: they come from config with ``~``/``${ENV}`` expansion.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

from .schema import ValidationError, identity_to_path_segment

# The directory containing the ``agent_comms`` package: the checkout root in
# development, ``site-packages`` when installed. Only development-time
# consumers (release git info, tests) may treat it as a checkout.
REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = Path(__file__).resolve().parent
DISPATCH_ID_RE = re.compile(r"dispatch_[0-9]{8}_[0-9]{6}_[0-9a-f]{8}")


HOME_ENV = "AGENT_COMMS_HOME"


def home_override() -> Path | None:
    """The ``AGENT_COMMS_HOME`` runtime root, or None when it is not set.

    ``~`` is expanded; a relative value is refused rather than resolved
    against whatever directory a client or worker happens to start in.
    """
    raw = os.environ.get(HOME_ENV)
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValidationError(f"{HOME_ENV}={raw!r} is not an absolute path; set it to one, for example $HOME/.agent-comms-dev")
    return Path(os.path.normpath(path))


def runtime_root() -> Path:
    """Checkout-independent root for durable agent-comms runtime state.

    Everything under here survives moving or re-cloning the checkout. The
    canonical ledger and the Codex-home custody root both live beneath it.
    ``AGENT_COMMS_HOME`` moves the whole root, so a second install shares
    nothing with the first; the narrower overrides still win for their path.
    """
    return home_override() or Path.home() / ".agent-comms"



def db_path() -> Path:
    """Canonical SQLite database path."""
    override = os.environ.get("AGENT_COMMS_DB")
    if override:
        return Path(override).expanduser()
    return canonical_db_path()


def canonical_db_path() -> Path:
    """Checkout-independent default SQLite database path."""
    return runtime_root() / "agent-comms.sqlite"


def actors_config_path() -> Path:
    """Default actor registry read by ``agent-comms bootstrap``."""
    return runtime_root() / "actors.json"


def review_root() -> Path:
    """Review-cycle records (``reviewing.store``) under the runtime root."""
    return runtime_root() / "dispatch" / "reviews"


def push_approval_root() -> Path:
    """Signed push-approval records under the runtime root."""
    return runtime_root() / "dispatch" / "push-approvals"


def codex_custody_root() -> Path:
    """The runtime custody root for provisioned per-worker Codex homes.

    Freshly provisioned default homes (``provisioned_codex_home``) live under
    this root. It is checkout-independent so a moved or re-cloned checkout keeps
    resolving the same durable homes. In production it is authoritative and
    always active (no environment switch enables it); the
    ``AGENT_COMMS_CODEX_CUSTODY_ROOT`` override only redirects the root to a
    scratch/test location that production never sets. Containment enforcement is
    NOT gated on that override -- it is always on, and scratch/test
    materialization opts out explicitly via ``enforce_custody_root=False``.
    """
    override = os.environ.get("AGENT_COMMS_CODEX_CUSTODY_ROOT")
    if override:
        return Path(override).expanduser()
    return runtime_root() / "codex-homes"


def console_script(name: str) -> Path:
    """The installed console script ``name`` next to this interpreter.

    The four commands are ``[project.scripts]`` entry points, so they live in
    the ``bin`` directory of whatever environment holds the package: the
    checkout's ``.venv`` after ``uv sync``, or the tool venv of an installed
    wheel. A missing script is refused loudly rather than rendered into a
    worker's MCP configuration, where it would only fail at the worker's start.
    """
    # Not resolved: .venv/bin/python is a symlink to the base interpreter, and
    # the scripts live next to the symlink, not next to its target.
    path = Path(sys.executable).parent / name
    if not path.is_file():
        raise FileNotFoundError(
            f"{name} is not installed next to {sys.executable}; a checkout recovers "
            "with: uv sync; an installed package by reinstalling it"
        )
    return path


def mcp_command() -> Path:
    """The `agent-comms-mcp` command MCP clients exec."""
    return console_script("agent-comms-mcp")


def cli_command() -> Path:
    """The `agent-comms` CLI command."""
    return console_script("agent-comms")


def hooks_path() -> Path:
    """The Claude `PreToolUse` hook script, package-relative."""
    return PACKAGE_ROOT / "hooks" / "pre_tool_use.py"


def dispatch_log_dir() -> Path:
    """Directory for per-dispatch worker stdout/stderr logs.

    Lives under the checkout-independent runtime root, next to the ledger,
    so a test run never writes into the source tree and an installed package
    without a checkout has somewhere to write. ``AGENT_COMMS_DISPATCH_LOG_DIR``
    redirects it (tests, or an operator who wants the logs elsewhere).
    """
    override = os.environ.get("AGENT_COMMS_DISPATCH_LOG_DIR")
    path = Path(override).expanduser() if override else runtime_root() / "logs" / "dispatch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def dispatch_log_path(dispatch_id: str) -> Path:
    """Per-dispatch worker log path for a generated dispatch id."""
    if re.fullmatch(DISPATCH_ID_RE, dispatch_id) is None:
        raise ValueError(f"invalid dispatch_id for dispatch log path: {dispatch_id!r}")

    base = dispatch_log_dir().resolve()
    path = (base / f"{dispatch_id}.log").resolve()
    if path.parent != base:
        raise ValueError(f"dispatch log path escapes log dir: {dispatch_id!r}")
    return path


def dispatch_events_path(dispatch_id: str) -> Path:
    """Per-dispatch stdout artifact, with the same identity and path checks."""
    log = dispatch_log_path(dispatch_id)
    path = log.with_suffix(".events.jsonl").resolve()
    if path.parent != log.parent:
        raise ValueError(f"dispatch events path escapes log dir: {dispatch_id!r}")
    return path


def codex_home(actor_id: str) -> Path:
    """The LEGACY repo-local per-actor ``CODEX_HOME``.

    This retains its pre-custody meaning so frozen literal / ``{codex_home}``
    actors and their tests are never silently reinterpreted: it is what
    ``resolve_codex_home('{codex_home}')`` and ``codex_auth_lineage_key``
    resolve for a legacy actor. New workers live under the checkout-
    independent custody root via ``provisioned_codex_home`` instead.
    """
    segment = identity_to_path_segment(actor_id)
    return REPO_ROOT / "config" / f"codex-home-{segment}"


def provisioned_codex_home(actor_id: str) -> Path:
    """The checkout-independent per-actor production ``CODEX_HOME``.

    New codex workers and installed provisioning materialize here, under the
    durable runtime custody root, so a moved or re-cloned checkout keeps
    resolving the same home.
    It is deliberately distinct from the legacy repo-local ``codex_home``.
    """
    segment = identity_to_path_segment(actor_id)
    return codex_custody_root() / "default" / segment


def resolve_codex_home(actor_id: str, value: str) -> Path:
    """Resolve the supported literal/placeholder ``CODEX_HOME`` vocabulary.

    ``{codex_home}`` resolves to the LEGACY repo-local per-actor home; a literal
    path (optionally with ``~``/``$VAR`` expansion) resolves as written. Any
    other brace placeholder is refused.
    """
    if value == "{codex_home}":
        return codex_home(actor_id)
    if "{" in value or "}" in value:
        raise ValidationError(
            "unsupported CODEX_HOME placeholder; allowed forms are a literal path or {codex_home}"
        )
    return Path(value).expanduser()


def codex_auth_lineage_key(actor_id: str, codex_home_value: str) -> str:
    """Comparison key for a codex actor's effective auth.json path."""
    codex_home_path = resolve_codex_home(actor_id, codex_home_value)
    return os.path.realpath(str(codex_home_path / "auth.json"))


def codex_auth_source() -> Path:
    """The LEGACY repo-local shared codex auth file.

    Retains its pre-custody meaning so frozen actors/tests are not silently
    reinterpreted. New-worker provisioning uses ``runtime_codex_auth_source``.
    """
    return REPO_ROOT / "config" / "codex-home" / "auth.json"


def runtime_codex_auth_source() -> Path:
    """The checkout-independent shared codex auth file for production callers.

    Symlinked into per-actor homes by production provisioning. Checkout-
    independent so a moved or re-cloned checkout keeps resolving the same shared
    credential. Distinct from the legacy repo-local ``codex_auth_source``.
    """
    return runtime_root() / "codex-auth" / "auth.json"


CODEX_HOME_ENV = "CODEX_HOME"


def operator_codex_home() -> Path:
    """The operator's own ``CODEX_HOME``: the variable when set, else ``~/.codex``."""
    raw = os.environ.get(CODEX_HOME_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def operator_codex_auth() -> Path:
    """The operator's own Codex login file, the one workers share.

    Always a path, even before the first ``codex login`` writes it, so the
    shared link can be installed first and the login completes it.
    """
    return operator_codex_home() / "auth.json"


def operator_codex_auth_problem(path: Path | None = None) -> str | None:
    """Why ``path`` (the operator's login file by default) cannot be shared.

    A login file inside the runtime root or the custody root would make the
    shared link point at agent-comms' own files (``CODEX_HOME`` set to the
    shared directory, say), a loop that no login could complete. Checked as
    written and resolved through symlinks, so the shared link itself (which
    may already resolve elsewhere) and an aliased runtime root are caught.
    """
    path = operator_codex_auth() if path is None else path
    forms = {Path(os.path.abspath(path)), Path(os.path.realpath(path))}
    roots = (("the runtime root", runtime_root()), ("the Codex custody root", codex_custody_root()))
    for name, root in roots:
        root_forms = {Path(os.path.abspath(root)), Path(os.path.realpath(root))}
        if any(form == base or form.is_relative_to(base) for form in forms for base in root_forms):
            return (
                f"{CODEX_HOME_ENV} puts the Codex login {path} inside {name} ({root}); "
                "workers share your own login, so point it at your own Codex home"
            )
    return None


def shared_login_boundary_problem(project_root: Path, auth: Path | None = None) -> str | None:
    """Why a worker rooted at ``project_root`` cannot share the operator's login.

    The project is a worker's sandbox write set, so it may not contain or sit
    inside the operator's Codex home, the login file that home's ``auth.json``
    resolves to (through symlinks, dangling or not: a renamed or relocated
    target is still the credential), the runtime root or the custody root.
    Every path is compared resolved. One check for setup, provisioning and
    dispatch admission, so no entry path shares the login past the boundary.
    """
    root = Path(os.path.realpath(project_root))
    auth = operator_codex_auth() if auth is None else Path(auth)
    for name, path in (
        ("your Codex home", operator_codex_home()),
        ("your Codex login file", auth),
        ("the agent-comms runtime root", runtime_root()),
        ("the Codex custody root", codex_custody_root()),
    ):
        resolved = Path(os.path.realpath(path))
        if root == resolved or root.is_relative_to(resolved) or resolved.is_relative_to(root):
            return f"{root} contains or is inside {name} ({path}); choose a project directory outside it"
    return None


def codex_login_command(home_or_auth: Path) -> str:
    """The login that fills ``home_or_auth``: a ``CODEX_HOME``, its ``auth.json``,
    or a lineage key (the file a home's ``auth.json`` resolves to).

    ``codex login`` when the path chains (through the shared link, dangling
    or not) into the operator's own login; otherwise the explicit
    ``CODEX_HOME=<home> codex login`` for a home that keeps its own file. A
    lineage key under another name has no known home, so the remedy names the
    file rather than inventing a home from it.
    """
    home_or_auth = Path(home_or_auth)
    operator = os.path.realpath(operator_codex_auth())
    if home_or_auth.name == "auth.json":
        home, auth = home_or_auth.parent, home_or_auth
    elif os.path.realpath(home_or_auth) == operator or home_or_auth.is_file():
        home, auth = None, home_or_auth
    else:
        home, auth = home_or_auth, home_or_auth / "auth.json"
    if os.path.realpath(auth) == operator:
        return "codex login"
    if home is None:
        return f"log in to the Codex home whose auth.json links to {auth}: {CODEX_HOME_ENV}=<that home> codex login"
    return f"{CODEX_HOME_ENV}={shlex.quote(str(home))} codex login"
