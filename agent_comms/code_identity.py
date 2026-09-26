from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import logging
from pathlib import Path

from . import paths

logger = logging.getLogger(__name__)

SURFACE_ROOT = paths.REPO_ROOT.resolve()
CONTRACT_VERSION: int = 23
# Version of the consumer-visible dispatch contract. A loaded MCP process
# refuses dispatch when its version differs from this checkout's, so advance it
# for any change a stale process could misapply. A contract-compatible change to
# a CONTRACT_GOVERNING file refreshes CONTRACT_SURFACE_DIGEST (the canonical
# contract_surface_digest() over that surface) and keeps the version.
CONTRACT_SURFACE_DIGEST = (
    "dcec44e4dd6b78ebd09330971d73a16f5c136a580c054ab9c41a2d4ddaa2055a"
)


class StaleModuleError(RuntimeError):
    """Raised when this process loaded an older dispatch surface."""


INCLUDED_EXPLICIT = frozenset(
    {
        "agent_comms/actors.py",
        "agent_comms/clock.py",
        "agent_comms/code_identity.py",
        "agent_comms/codex_home.py",
        "agent_comms/codex_auth_refresh.py",
        "agent_comms/codex_auth_constants.py",
        "agent_comms/db.py",
        "agent_comms/delta_manifest.py",
        "agent_comms/dispatch_ledger.py",
        "agent_comms/handoff.py",
        "agent_comms/mailbox.py",
        "agent_comms/mcp_server.py",
        "agent_comms/param_leak.py",
        "agent_comms/paths.py",
        "agent_comms/payload.py",
        "agent_comms/reviewing/git_evidence.py",
        "agent_comms/reviewing/intents.py",
        "agent_comms/reviewing/reply_snapshots.py",
        "agent_comms/runtime_pins.json",
        "agent_comms/runtime_pins.py",
        "agent_comms/schema.py",
        "agent_comms/spawn.py",
        "agent_comms/store.py",
        "agent_comms/supervisor.py",
        "agent_comms/timeout_wrapper.py",
        "agent_comms/worker_usage.py",
    }
)
INCLUDED_GLOBS = (
    "agent_comms/adapters/*.py",
    "agent_comms/policies/*.py",
    "agent_comms/hooks/*.py",
)
CONTRACT_GOVERNING = frozenset(
    {
        "agent_comms/adapters/__init__.py",
        "agent_comms/adapters/_base.py",
        "agent_comms/adapters/claude.py",
        "agent_comms/adapters/codex.py",
        "agent_comms/adapters/registry.py",
        "agent_comms/actors.py",
        "agent_comms/codex_home.py",
        "agent_comms/codex_auth_refresh.py",
        "agent_comms/codex_auth_constants.py",
        "agent_comms/db.py",
        "agent_comms/delta_manifest.py",
        "agent_comms/dispatch_ledger.py",
        "agent_comms/handoff.py",
        "agent_comms/hooks/__init__.py",
        "agent_comms/hooks/pre_tool_use.py",
        "agent_comms/mailbox.py",
        "agent_comms/mcp_server.py",
        "agent_comms/param_leak.py",
        "agent_comms/paths.py",
        "agent_comms/payload.py",
        "agent_comms/policies/__init__.py",
        "agent_comms/reviewing/git_evidence.py",
        "agent_comms/reviewing/intents.py",
        "agent_comms/reviewing/reply_snapshots.py",
        "agent_comms/runtime_pins.json",
        "agent_comms/runtime_pins.py",
        "agent_comms/schema.py",
        "agent_comms/spawn.py",
        "agent_comms/store.py",
        "agent_comms/supervisor.py",
    }
)
CONTRACT_NEUTRAL_REASONS = {
    "agent_comms/adapters/fake.py": "test-only fake adapter; not part of the production consumer contract",
    "agent_comms/adapters/fake_worker.py": "test-only fake worker; not part of the production consumer contract",
    "agent_comms/clock.py": "internal clock helper; cannot alter the consumer-visible dispatch contract",
    "agent_comms/code_identity.py": "holds contract version/digest metadata; does not define dispatch behavior",
    "agent_comms/hooks/session_start.py": "session orientation is not dispatch-correctness surface",
    "agent_comms/timeout_wrapper.py": "internal timeout helper; cannot alter the consumer-visible dispatch contract",
    "agent_comms/worker_usage.py": "operator-observational payload; governed dispatch_ledger owns the durable write",
}
EXCLUDED_WITH_REASON = {
    "agent_comms/cli/**": "admin and operator CLI surface; after DEFAULT_DB decoupling it is not server-imported",
    "agent_comms/codex_refresh_driver.py": "post-expiry auth refresh pass started by the monitor, off the dispatch spawn path",
    "agent_comms/monitor.py": "separate cron/operator reconciliation process, off the dispatch spawn path",
    "agent_comms/onboarding.py": "separate onboarding CLI helper, off the dispatch spawn path",
    "agent_comms/provisioning.py": "separate provisioning CLI helper, off the dispatch spawn path",
    "agent_comms/review.py": "separate review CLI workflow, off the dispatch spawn path",
    "agent_comms/reviewing/**": "review CLI implementation, off the dispatch spawn path",
    "agent_comms/push_approval.py": "separate guarded-push approval workflow, off the dispatch spawn path",
    "agent_comms/release.py": "release metadata self-reporting for operator CLI, off the dispatch spawn path",
    "agent_comms/status.py": "Store imports it, but it has no dispatch allow-decision or worker-execution dependency",
    "agent_comms/__init__.py": "package marker with no dispatch logic",
}


class SurfaceCaptureError(RuntimeError):
    def __init__(self, relpath: str) -> None:
        super().__init__(relpath)
        self.relpath = relpath


def _repo_root() -> Path:
    return SURFACE_ROOT


def _rel(path: Path) -> str:
    return path.relative_to(_repo_root()).as_posix()


def included_surface_paths() -> list[Path]:
    root = _repo_root()
    included: set[Path] = {root / relpath for relpath in INCLUDED_EXPLICIT}
    for pattern in INCLUDED_GLOBS:
        included.update(root.glob(pattern))
    return sorted(included, key=lambda path: _rel(path))


def is_included_surface(relpath: str) -> bool:
    normalized = relpath.replace("\\", "/")
    return any(_rel(path) == normalized for path in included_surface_paths())


def exclusion_reason(relpath: str) -> str | None:
    normalized = relpath.replace("\\", "/")
    # Explicit inclusion is authoritative: an included-surface path is never
    # excluded, even when a broad exclusion glob (such as
    # ``agent_comms/reviewing/**``) also matches it.
    if is_included_surface(normalized):
        return None
    for pattern, reason in EXCLUDED_WITH_REASON.items():
        if fnmatch.fnmatchcase(normalized, pattern):
            return reason
    return None


def _surface_map() -> dict[str, str]:
    root = _repo_root()
    if not root.is_dir():
        raise SurfaceCaptureError(".")
    result: dict[str, str] = {}
    for path in included_surface_paths():
        relpath = _rel(path)
        try:
            result[relpath] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise SurfaceCaptureError(relpath) from exc
    if not result:
        raise SurfaceCaptureError(".")
    return result


def _contract_surface_map(surface: dict[str, str] | None = None) -> dict[str, str]:
    source = _surface_map() if surface is None else surface
    return {relpath: source[relpath] for relpath in sorted(CONTRACT_GOVERNING)}


def contract_surface_digest(surface: dict[str, str] | None = None) -> str:
    pairs = [
        (path, filehash) for path, filehash in _contract_surface_map(surface).items()
    ]
    payload = json.dumps(pairs, separators=(",", ":"), sort_keys=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def unclassified_contract_surface_paths(included: set[str] | None = None) -> list[str]:
    included_set = (
        {_rel(path) for path in included_surface_paths()}
        if included is None
        else {relpath.replace("\\", "/") for relpath in included}
    )
    classified = set(CONTRACT_GOVERNING) | set(CONTRACT_NEUTRAL_REASONS)
    return sorted(included_set - classified)


def _read_contract_version(source: bytes) -> int | None:
    try:
        module = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return None
    for node in module.body:
        value_node: ast.expr | None = None
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == "CONTRACT_VERSION"
                for target in node.targets
            ):
                value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == "CONTRACT_VERSION"
            ):
                value_node = node.value
        if value_node is None:
            continue
        if (
            not isinstance(value_node, ast.Constant)
            or type(value_node.value) is not int
        ):
            return None
        return value_node.value
    return None


def current_contract_version() -> int | None:
    try:
        source = (_repo_root() / "agent_comms" / "code_identity.py").read_bytes()
    except OSError:
        return None
    return _read_contract_version(source)


def _identity_from_surface(surface: dict[str, str]) -> str:
    pairs = [(path, surface[path]) for path in sorted(surface)]
    payload = json.dumps(pairs, separators=(",", ":"), sort_keys=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _capture_surface() -> dict[str, str] | None:
    try:
        return _surface_map()
    except SurfaceCaptureError:
        return None


def _capture() -> str | None:
    surface = _capture_surface()
    if surface is None:
        return None
    return _identity_from_surface(surface)


def _changed_files(loaded: dict[str, str], current: dict[str, str]) -> list[str]:
    changed = []
    for relpath in sorted(set(loaded) | set(current)):
        if loaded.get(relpath) != current.get(relpath):
            changed.append(relpath)
    return changed


LOADED_SURFACE: dict[str, str] | None = _capture_surface()
LOADED_CODE_IDENTITY: str | None = (
    _identity_from_surface(LOADED_SURFACE) if LOADED_SURFACE is not None else None
)
LOADED_CONTRACT_VERSION = CONTRACT_VERSION
_LAST_TOLERATED_CODE_IDENTITY: str | None = None


def current_code_identity() -> str | None:
    return _capture()


def require_fresh_module() -> None:
    global _LAST_TOLERATED_CODE_IDENTITY

    loaded = LOADED_CODE_IDENTITY
    loaded_surface = LOADED_SURFACE
    if loaded is None or loaded_surface is None:
        return
    try:
        current_surface = _surface_map()
    except SurfaceCaptureError as exc:
        raise StaleModuleError(
            f"dispatch surface file unreadable at {exc.relpath}; reconnect"
        ) from exc
    current = _identity_from_surface(current_surface)
    if loaded == current:
        return
    changed = _changed_files(loaded_surface, current_surface)
    culprit = changed[0] if changed else "unknown"
    current_contract = current_contract_version()
    if current_contract is not None:
        if current_contract == LOADED_CONTRACT_VERSION:
            if _LAST_TOLERATED_CODE_IDENTITY != current:
                logger.warning(
                    "dispatch surface changed at %s; contract v%s unchanged, continuing without reconnect",
                    ", ".join(changed) if changed else "unknown",
                    LOADED_CONTRACT_VERSION,
                )
                _LAST_TOLERATED_CODE_IDENTITY = current
            return
        raise StaleModuleError(
            f"dispatch CONTRACT changed (v{LOADED_CONTRACT_VERSION} -> v{current_contract}) at {culprit}; "
            "reconnect the MCP server (/mcp) then retry"
        )
    raise StaleModuleError(
        f"dispatch surface changed at {culprit}; reconnect the MCP server (/mcp) then retry"
    )
