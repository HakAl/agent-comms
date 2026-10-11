"""Certified runtime versions, shipped inside the package.

``runtime_pins.json`` (schema 2) is the single record of which runtime
versions the gated cells certified, per platform. For each runtime it holds
``boundary`` (how strictly an installed version must match: ``exact`` or
``minor``) and ``platforms``, a map from a platform key to the ``version``
certified there, the date it was certified (``certified_on``) and, for a
custody-managed runtime, its ``sha256``: the Claude binary's file digest, or
the Codex native bundle's digest (:func:`bundle_sha256`). A platform key is the OS and
CPU architecture as Python reports them, ``darwin-arm64`` or
``linux-x86_64``, which is what ``uname -m`` prints after the OS name.

An entry exists only for a platform on which the gated cell ran and passed;
certifying a new platform is described in CONTRIBUTING.md under "Runtime
pins". On a platform without an entry every consumer fails closed with
:class:`PlatformNotCertified`, whose message names the platform and what to
do. The file is package data, so an installed wheel carries it; the cell
drift test and ``agent-comms version`` read the same file.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import sys
from dataclasses import dataclass
from pathlib import Path

RUNTIME_PINS_PATH = Path(__file__).with_name("runtime_pins.json")
RUNTIME_PINS_SCHEMA = 2
CLAUDE_VERSIONS_DIR_ENV = "AGENT_COMMS_CLAUDE_VERSIONS_DIR"
CLAUDE_PINNED_SHA256_ENV = "AGENT_COMMS_CLAUDE_PINNED_SHA256"
CODEX_VERSIONS_DIR_ENV = "AGENT_COMMS_CODEX_VERSIONS_DIR"
CODEX_PINNED_SHA256_ENV = "AGENT_COMMS_CODEX_PINNED_SHA256"


class PlatformNotCertified(RuntimeError):
    """No gated cell has certified this runtime on the current platform."""


class BundleNotDigestible(ValueError):
    """A runtime bundle holds something its digest cannot describe safely."""


@dataclass(frozen=True)
class RuntimePin:
    runtime: str
    platform: str
    version: str
    boundary: str
    certified_on: str
    sha256: str | None = None


def current_platform() -> str:
    """The platform key of this process: ``<sys.platform>-<machine>``."""
    return f"{sys.platform}-{_platform.machine().lower()}"


def load_runtime_pins() -> dict:
    """The certified runtime manifest as written, checked for the schema."""
    manifest = json.loads(RUNTIME_PINS_PATH.read_text(encoding="utf-8"))
    schema = manifest.get("schema") if isinstance(manifest, dict) else None
    if schema != RUNTIME_PINS_SCHEMA:
        raise ValueError(
            f"{RUNTIME_PINS_PATH}: expected runtime pins schema {RUNTIME_PINS_SCHEMA}, found {schema!r}"
        )
    return manifest


def certified_platforms(runtime: str) -> tuple[str, ...]:
    """Platform keys with an entry for ``runtime``, sorted."""
    return tuple(sorted(_runtime_entry(runtime)["platforms"]))


def pin_for(runtime: str, platform: str | None = None) -> RuntimePin:
    """The certified pin of ``runtime`` on ``platform`` (default: this one).

    Raises :class:`PlatformNotCertified` when the platform has no entry.
    """
    entry = _runtime_entry(runtime)
    key = platform or current_platform()
    data = entry["platforms"].get(key)
    if data is None:
        certified = ", ".join(sorted(entry["platforms"])) or "none"
        raise PlatformNotCertified(
            f"{runtime} has no certified pin for {key} (certified: {certified}); "
            f"recover with: run the gated cell for {runtime} on this platform and add its entry "
            f"under runtimes.{runtime}.platforms in agent_comms/runtime_pins.json "
            "(see CONTRIBUTING.md, Runtime pins)"
        )
    return RuntimePin(
        runtime=runtime,
        platform=key,
        version=data["version"],
        boundary=entry["boundary"],
        certified_on=data["certified_on"],
        sha256=data.get("sha256"),
    )


def claude_pin() -> RuntimePin:
    return pin_for("claude")


def codex_pin() -> RuntimePin:
    return pin_for("codex")


def _runtime_entry(runtime: str) -> dict:
    runtimes = load_runtime_pins()["runtimes"]
    if runtime not in runtimes:
        raise KeyError(f"{RUNTIME_PINS_PATH} has no runtime {runtime!r} (has: {', '.join(sorted(runtimes))})")
    return runtimes[runtime]


def claude_versions_dir() -> Path:
    override = os.environ.get(CLAUDE_VERSIONS_DIR_ENV)
    if override:
        return Path(override).expanduser()
    from .paths import runtime_root

    return runtime_root() / "runtime-custody"


def claude_binary_path() -> Path:
    """The custody binary of the Claude version certified for this platform.

    The pin is resolved first, so an unlisted platform fails closed before
    any path is derived or touched.
    """
    version = claude_pin().version
    return claude_versions_dir() / version


def claude_pinned_sha256() -> str:
    override = os.environ.get(CLAUDE_PINNED_SHA256_ENV)
    if override:
        return override
    return claude_pin().sha256 or ""


def custody_binary_sha256(binary: Path) -> str:
    return hashlib.sha256(binary.read_bytes()).hexdigest()


def custody_binary_digest_matches(binary: Path, expected_sha256: str | None = None) -> bool:
    if not binary.exists():
        return False
    return custody_binary_sha256(binary) == (expected_sha256 or claude_pinned_sha256())


def codex_versions_dir() -> Path:
    """Where custody copies live; an override must be absolute.

    The adapter checks the bundle in the dispatcher's directory but the binding
    check and launch run in the worker's, so a relative path could verify one
    bundle and start another.
    """
    override = os.environ.get(CODEX_VERSIONS_DIR_ENV)
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            from .schema import ValidationError

            raise ValidationError(f"{CODEX_VERSIONS_DIR_ENV}={override!r} is not an absolute path; set it to one")
        return Path(os.path.normpath(path))
    from .paths import runtime_root

    return runtime_root() / "runtime-custody" / "codex"


def codex_bundle_path() -> Path:
    """The custody copy of the native Codex bundle certified for this platform.

    A bundle is the platform package's target-triple directory
    (``bin/codex`` and the files it runs with), copied whole. The pin is
    resolved first, so an unlisted platform fails closed before any path is
    derived or touched.
    """
    return codex_versions_dir() / codex_pin().version


def codex_binary_path() -> Path:
    return codex_bundle_path() / "bin" / "codex"


def codex_pinned_sha256() -> str:
    override = os.environ.get(CODEX_PINNED_SHA256_ENV)
    if override:
        return override
    return codex_pin().sha256 or ""


def bundle_manifest(root: Path) -> list[list[str]]:
    """Every entry of the bundle at ``root``, sorted by relative path.

    A regular file is ``[relpath, "755" | "644", sha256]``, the mode taken
    from the owner execute bit only, so a umask or copy tool cannot change
    the digest. A symlink is ``[relpath, "link", target]`` and must be
    relative and stay inside the bundle. Directories are walked, not
    recorded. Anything else, or a root that is not a real directory, raises
    :class:`BundleNotDigestible`.
    """
    if root.is_symlink() or not root.is_dir():
        raise BundleNotDigestible(f"bundle root is not a directory: {root}")
    real_root = os.path.realpath(root)
    entries: list[list[str]] = []

    def walk(directory: Path) -> None:
        with os.scandir(directory) as scan:
            for item in scan:
                path = Path(item.path)
                rel = path.relative_to(root).as_posix()
                if item.is_symlink():
                    target = os.readlink(path)
                    resolved = os.path.realpath(path)
                    if os.path.isabs(target) or os.path.commonpath([real_root, resolved]) != real_root:
                        raise BundleNotDigestible(f"symlink leaves the bundle: {rel} -> {target}")
                    entries.append([rel, "link", target])
                elif item.is_dir(follow_symlinks=False):
                    walk(path)
                elif item.is_file(follow_symlinks=False):
                    mode = "755" if item.stat(follow_symlinks=False).st_mode & 0o100 else "644"
                    entries.append([rel, mode, _file_sha256(path)])
                else:
                    raise BundleNotDigestible(f"unsupported file type in bundle: {rel}")

    walk(root)
    return sorted(entries)


def bundle_sha256(root: Path) -> str:
    """SHA-256 of :func:`bundle_manifest` as canonical JSON."""
    manifest = json.dumps(bundle_manifest(root), separators=(",", ":"))
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()


def codex_bundle_digest_matches(bundle: Path, expected_sha256: str | None = None) -> bool:
    expected = expected_sha256 or codex_pinned_sha256()
    if not expected:
        return False
    try:
        return bundle_sha256(bundle) == expected
    except (BundleNotDigestible, OSError):
        return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
