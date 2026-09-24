from __future__ import annotations

import hashlib
import os
from pathlib import Path

CLAUDE_PINNED_VERSION = "2.1.195"
CLAUDE_PINNED_SHA256 = "8b45adad93f336ab95f33e714494b19fd3377a494eb05c122c8677bc895876ad"
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
