"""Single source of truth for agent-comms-internal paths.

Every path that points *inside this repository* is derived here from the
package location, never hardcoded. Moving the repo (or running from a clone at
a different prefix) requires changing nothing. External deployment paths
(a worker's `project_root`) are operator data and are NOT derived here — they
come from config with `~`/`${ENV}` expansion.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .schema import ValidationError, identity_to_path_segment

# agent_comms/paths.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path.home() / ".agent-comms" / "agent-comms.sqlite"
DISPATCH_ID_RE = re.compile(r"dispatch_[0-9]{8}_[0-9]{6}_[0-9a-f]{8}")


def runtime_root() -> Path:
    """Checkout-independent root for durable agent-comms runtime state.

    Everything under here survives moving or re-cloning the checkout. The
    canonical ledger and the Codex-home custody root both live beneath it.
    """
    return Path.home() / ".agent-comms"


def db_path() -> Path:
    """Canonical SQLite database path."""
    import os

    override = os.environ.get("AGENT_COMMS_DB")
    if override:
        return Path(override).expanduser()
    return canonical_db_path()


def canonical_db_path() -> Path:
    """Checkout-independent default SQLite database path."""
    return runtime_root() / "agent-comms.sqlite"


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


def mcp_command() -> Path:
    """The `agent-comms-mcp` launcher MCP clients exec."""
    return REPO_ROOT / "scripts" / "agent-comms-mcp"


def cli_command() -> Path:
    """The `agent-comms` CLI launcher."""
    return REPO_ROOT / "scripts" / "agent-comms"


def hooks_path() -> Path:
    """The Claude `PreToolUse` hook script."""
    return REPO_ROOT / "agent_comms" / "hooks" / "pre_tool_use.py"


def dispatch_log_dir() -> Path:
    """Directory for per-dispatch worker stdout/stderr logs."""
    path = REPO_ROOT / "logs" / "dispatch"
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
