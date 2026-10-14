"""The check rows doctor reports and the helpers that build them.

Doctor and each runtime's checks (``agent_comms/runtimes/``) share these, so
a runtime can report on itself without importing doctor.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import paths

OK, FAIL, WARN, SKIP = "ok", "fail", "warn", "skip"
SEMVER_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def check(check_id: str, status: str, detail: str, fix: str | None = None) -> dict:
    if status not in (OK, FAIL, WARN, SKIP):
        raise ValueError(f"unknown check status {status!r}")
    return {"id": check_id, "status": status, "detail": detail, "fix": fix}


_PLACEHOLDER = re.compile(r"^<[^<>]+>$")


def sh(*argv: object) -> str:
    """A shell line from an argv, quoting every word except ``<placeholders>``."""
    return " ".join(str(arg) if _PLACEHOLDER.match(str(arg)) else shlex.quote(str(arg)) for arg in argv)


@dataclass(frozen=True)
class LedgerRef:
    """The ledger doctor was pointed at, so every repair targets the same one.

    ``explicit`` mirrors the CLI: ``--db`` or ``AGENT_COMMS_DB`` was given.
    A repair rendered for an explicit ledger carries ``--db``; one for the
    default ledger carries nothing, which is what the consumer commands
    resolve to. Under ``AGENT_COMMS_HOME`` every repair sets it, so a line
    pasted into another shell still targets this install.
    """

    path: Path
    explicit: bool

    def db_args(self) -> list[str]:
        return ["--db", str(self.path)] if self.explicit else []

    def cli(self, *args: object) -> str:
        return home_prefix() + sh("agent-comms", *self.db_args(), *args)

    def monitor(self, human: str) -> str:
        return home_prefix() + sh("agent-comms-monitor", *self.db_args(), "--human-actor-id", human)


def home_prefix() -> str:
    """``AGENT_COMMS_HOME=<root> `` for an install with its own home, else empty."""
    home = paths.home_override()
    return f"{paths.HOME_ENV}={shlex.quote(str(home))} " if home is not None else ""


def version_from(result: subprocess.CompletedProcess[str]) -> tuple[str | None, str]:
    output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
    match = SEMVER_RE.search(output)
    return (match.group(1) if match else None), output


def version_matches(installed: str, pin) -> bool:  # type: ignore[no-untyped-def]
    if pin.boundary == "minor":
        return installed.split(".")[:2] == pin.version.split(".")[:2]
    return installed == pin.version


def run_quietly(run: Runner, argv: list[str]) -> subprocess.CompletedProcess[str] | Exception:
    try:
        return run(argv, text=True, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return exc
