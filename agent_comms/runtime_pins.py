"""Certified runtime versions, shipped inside the package.

``runtime_pins.json`` is the single record of which runtime versions the
gated cells last certified (``last_verified``), how strictly a version must
match (``boundary``), and for the custody-managed Claude binary its sha256.
It is package data, so an installed wheel carries it; the cell drift test
and ``agent-comms version`` read the same file. Re-certifying a runtime is
a one-line edit here plus a contract surface digest refresh.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

RUNTIME_PINS_PATH = Path(__file__).with_name("runtime_pins.json")


def load_runtime_pins() -> dict[str, dict[str, str]]:
    """The certified runtime manifest, keyed by runtime name."""
    return json.loads(RUNTIME_PINS_PATH.read_text(encoding="utf-8"))


_PINS = load_runtime_pins()
CLAUDE_PINNED_VERSION = _PINS["claude"]["last_verified"]
CLAUDE_PINNED_SHA256 = _PINS["claude"]["sha256"]
CLAUDE_VERSIONS_DIR_ENV = "AGENT_COMMS_CLAUDE_VERSIONS_DIR"
CLAUDE_PINNED_SHA256_ENV = "AGENT_COMMS_CLAUDE_PINNED_SHA256"


def claude_versions_dir() -> Path:
    override = os.environ.get(CLAUDE_VERSIONS_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agent-comms" / "runtime-custody"


def claude_binary_path() -> Path:
    return claude_versions_dir() / CLAUDE_PINNED_VERSION


def claude_pinned_sha256() -> str:
    return os.environ.get(CLAUDE_PINNED_SHA256_ENV, CLAUDE_PINNED_SHA256)


def custody_binary_sha256(binary: Path) -> str:
    return hashlib.sha256(binary.read_bytes()).hexdigest()


def custody_binary_digest_matches(binary: Path, expected_sha256: str | None = None) -> bool:
    if not binary.exists():
        return False
    return custody_binary_sha256(binary) == (expected_sha256 or claude_pinned_sha256())
