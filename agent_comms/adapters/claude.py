from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

from . import DispatchContext
from ._base import ProcessSpawnAdapter
from ..runtime_pins import (
    CLAUDE_PINNED_VERSION,
    claude_binary_path,
    claude_pinned_sha256,
    custody_binary_digest_matches,
    custody_binary_sha256,
)

SEMVER_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")


class RuntimePinUnavailable(RuntimeError):
    """The pinned Claude runtime cannot be used for worker spawn."""


class RuntimePinDrift(RuntimeError):
    """The pinned Claude runtime resolved to an unexpected version."""


class RuntimePinDigestMismatch(RuntimeError):
    """The pinned Claude runtime bytes do not match the certified digest."""


class ClaudeSpawnRowNotPinnedError(RuntimeError):
    """A Claude actor spawn row rendered a command outside runtime custody."""


class ClaudeAdapter(ProcessSpawnAdapter):
    """Claude-runtime command-template adapter.

    This shells out to the configured command. It is not named after
    Claude Agent Teams because it does not use those platform primitives.
    """

    runtime_label = "claude"
    supported_runtimes = ("claude",)

    def __init__(
        self,
        version_runner: Callable[[Path], subprocess.CompletedProcess[str]] | None = None,
        expected_sha256: str | None = None,
    ) -> None:
        super().__init__()
        self._version_runner = version_runner or self._run_version
        self._expected_sha256 = expected_sha256

    def _preflight(self, context: DispatchContext) -> None:
        if context.recipient.get("runtime") not in self.supported_runtimes:
            return

        binary = claude_binary_path()
        if not binary.exists():
            raise self._pin_unavailable(binary, "pinned Claude binary is missing")

        expected_sha256 = self._expected_sha256 or claude_pinned_sha256()
        if not custody_binary_digest_matches(binary, expected_sha256):
            actual_sha256 = custody_binary_sha256(binary)
            raise self._pin_digest_mismatch(binary, expected_sha256, actual_sha256)

        try:
            result = self._version_runner(binary)
        except RuntimePinUnavailable:
            raise
        except RuntimePinDrift:
            raise
        except RuntimePinDigestMismatch:
            raise
        except Exception as exc:
            raise self._pin_unavailable(binary, f"could not execute pinned Claude --version: {exc}") from exc

        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        match = SEMVER_RE.search(output)
        if result.returncode != 0:
            raise self._pin_unavailable(binary, f"pinned Claude --version exited {result.returncode}: {output!r}")
        if match is None:
            raise self._pin_unavailable(binary, f"could not parse pinned Claude --version output: {output!r}")
        installed = match.group(1)
        if installed != CLAUDE_PINNED_VERSION:
            raise self._pin_drift(binary, installed)

    def _validate_rendered_command(self, command: str, context: DispatchContext) -> None:
        if context.recipient.get("runtime") not in self.supported_runtimes:
            return
        try:
            command_path = Path(command).resolve()
        except (TypeError, ValueError, OSError) as exc:
            detail = f"could not resolve rendered command: {exc}"
        else:
            detail = None
        if detail is not None or command_path != claude_binary_path().resolve():
            raise ClaudeSpawnRowNotPinnedError(
                "Claude spawn command is not the pinned runtime custody binary; "
                f"rendered command={command!r}; actor_id={context.recipient.get('id')!r}; "
                f"{detail + '; ' if detail else ''}"
                "recover with: re-register the actor using render_spawn('claude', actor_id)"
            )

    @staticmethod
    def _run_version(binary: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(binary), "--version"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    @staticmethod
    def _pin_unavailable(binary: Path, reason: str) -> RuntimePinUnavailable:
        return RuntimePinUnavailable(
            f"{reason}; expected claude {CLAUDE_PINNED_VERSION}; path={binary}; "
            "recover with: install that Claude version at the resolved path, or re-certify and bump the pin"
        )

    @staticmethod
    def _pin_drift(binary: Path, installed: str) -> RuntimePinDrift:
        return RuntimePinDrift(
            f"pinned Claude version drifted: expected {CLAUDE_PINNED_VERSION}, got {installed}; "
            f"path={binary}; recover with: install that Claude version at the resolved path, "
            "or re-certify and bump the pin"
        )

    @staticmethod
    def _pin_digest_mismatch(binary: Path, expected: str, actual: str) -> RuntimePinDigestMismatch:
        return RuntimePinDigestMismatch(
            f"pinned Claude digest mismatch: expected sha256 {expected}, got {actual}; "
            f"path={binary}; recover with: populate runtime custody with the certified Claude binary, "
            "or re-certify and bump the pin"
        )
