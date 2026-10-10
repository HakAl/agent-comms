#!/usr/bin/env python3
"""Certify the installed runtime and update its pin: ``make certify RUNTIME=codex``.

Anyone may run it, an agent or a dispatch included; it needs no live-home
switch. Steps, with nothing written until the cells have passed:

1. read the installed version and this platform's pin; when they already
   match there is nothing to do (``--force`` certifies anyway);
2. check the cert login directory (``--cert-home``, default
   ``~/.agent-comms/codex-cert-home``): it must hold ``auth.json`` and must not
   be the home itself, ``~/.codex``, ``~/.claude`` or any other part of
   ``~/.agent-comms``;
3. run the runtime's cells through ``tests.cells.certify_run`` in
   certification mode (``AGENT_COMMS_TEST_CERT=1``: test isolation stays on);
4. refuse unless every test of every module ran and passed;
5. write the pin and ``CONTRACT_SURFACE_DIGEST``, then run the drift cell and
   check the digest in a fresh interpreter; any failure, exception or
   interrupt restores both files byte for byte;
6. write a record under ``local/certifications/`` and print the diff to commit.

Only Codex is automated. Claude certification still uses the live-home
procedure in CONTRIBUTING.md, because its login under a scratch HOME is not
verified yet.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
PINS_RELPATH = "agent_comms/runtime_pins.json"
IDENTITY_RELPATH = "agent_comms/code_identity.py"
RECORD_DIR_RELPATH = "local/certifications"

CERT_ENV = "AGENT_COMMS_TEST_CERT"
LIVE_HOME_ENV = "AGENT_COMMS_TEST_LIVE_HOME"
RESULT_ENV = "AGENT_COMMS_TEST_CERT_RESULT"
CODEX_CERT_HOME_ENV = "AGENT_COMMS_TEST_CODEX_CERT_HOME"

CELL_MODULES = {
    "codex": ("tests.cells.test_cell_codex", "tests.cells.test_adapter_codex"),
}
DRIFT_TEST = {
    "codex": "tests.cells.test_cell_version_drift.CellVersionDriftTest.test_codex_version_has_not_drifted",
}
UNSUPPORTED = {
    "claude": (
        "claude certification is not automated yet: its login under a scratch HOME is "
        "unverified; use the live-home procedure in CONTRIBUTING.md (Runtime pins)"
    ),
}

SEMVER_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")
DIGEST_RE = re.compile(r'(CONTRACT_SURFACE_DIGEST = \(\n\s*")([0-9a-f]{64})("\n\))')

CellRunner = Callable[[list[str]], dict]


class Refused(Exception):
    """Certification did not pass; the message says why."""


@dataclass
class Outcome:
    runtime: str
    platform: str
    installed: str
    previous: str | None
    certified: bool = False
    nothing_to_do: bool = False
    cells: list[dict] = field(default_factory=list)
    drift: list[dict] = field(default_factory=list)


def installed_version(command: str) -> str:
    executable = shutil.which(command)
    if executable is None:
        raise Refused(f"{command} is not on PATH")
    result = subprocess.run([executable, "--version"], text=True, capture_output=True, check=False)
    output = f"{result.stdout}\n{result.stderr}"
    match = SEMVER_RE.search(output)
    if result.returncode != 0 or match is None:
        raise Refused(f"{executable} --version exited {result.returncode}: {output.strip()!r}")
    return match.group(1)


def check_cert_home(cert_home: Path, home: Path) -> Path:
    """The resolved cert login directory, or :class:`Refused`.

    Cells copy ``auth.json`` back into it, so it must be a dedicated login and
    never a live one.
    """
    resolved = cert_home.expanduser().resolve()
    home = home.resolve()
    dedicated = home / ".agent-comms" / "codex-cert-home"

    def within(path: Path, root: Path) -> bool:
        return path == root or path.is_relative_to(root)

    if resolved == home:
        raise Refused(f"cert home {resolved} is the home directory itself")
    for name in (".codex", ".claude"):
        if within(resolved, home / name):
            raise Refused(f"cert home {resolved} is inside the live {home / name}")
    if within(resolved, home / ".agent-comms") and not within(resolved, dedicated):
        raise Refused(
            f"cert home {resolved} is inside the live runtime root {home / '.agent-comms'}; "
            f"only {dedicated} is a dedicated cert login there"
        )
    if not (resolved / "auth.json").is_file():
        raise Refused(f"cert home {resolved} has no auth.json; log in with: CODEX_HOME={resolved} codex login")
    return resolved


def failures(report: dict, modules: tuple[str, ...] | list[str]) -> list[str]:
    """Why a runner report is not a full pass, empty when it is one."""
    tests = report.get("tests") or []
    problems = []
    for module in modules:
        if not any(test["id"] == module or test["id"].startswith(module + ".") for test in tests):
            problems.append(f"{module}: no test ran")
    for test in tests:
        if not any(test["id"] == module or test["id"].startswith(module + ".") for module in modules):
            problems.append(f"{test['id']}: {test['outcome']} (outside the requested targets)")
        elif test["outcome"] != "passed":
            detail = test.get("detail", "").strip().splitlines()
            problems.append(f"{test['id']}: {test['outcome']}" + (f" ({detail[-1]})" if detail else ""))
    return problems


def updated_pins(text: str, runtime: str, platform: str, version: str, today: str) -> str:
    manifest = json.loads(text)
    manifest["runtimes"][runtime]["platforms"][platform] = {"version": version, "certified_on": today}
    return json.dumps(manifest, indent=2) + "\n"


def updated_identity(text: str, digest: str) -> str:
    new, count = DIGEST_RE.subn(lambda m: m.group(1) + digest + m.group(3), text)
    if count != 1:
        raise Refused(f"could not find CONTRACT_SURFACE_DIGEST in {IDENTITY_RELPATH}")
    return new


def previous_version(pins_text: str, runtime: str, platform: str) -> str | None:
    entry = json.loads(pins_text)["runtimes"][runtime]["platforms"].get(platform)
    return entry["version"] if entry else None


def certify(
    runtime: str,
    *,
    repo: Path,
    platform: str,
    installed: str,
    today: str,
    force: bool,
    run_cells: CellRunner,
    compute_digest: Callable[[], str],
    digest_in_sync: Callable[[], bool],
) -> Outcome:
    """Run the cells, then pin. Leaves both files untouched unless certified."""
    if runtime in UNSUPPORTED:
        raise Refused(UNSUPPORTED[runtime])
    if runtime not in CELL_MODULES:
        raise Refused(f"unknown runtime {runtime!r} (automated: {', '.join(sorted(CELL_MODULES))})")
    pins_path = repo / PINS_RELPATH
    identity_path = repo / IDENTITY_RELPATH
    pins_before = pins_path.read_bytes()
    identity_before = identity_path.read_bytes()
    outcome = Outcome(runtime, platform, installed, previous_version(pins_before.decode(), runtime, platform))
    if outcome.previous == installed and not force:
        outcome.nothing_to_do = True
        return outcome
    if not digest_in_sync():
        raise Refused(
            f"CONTRACT_SURFACE_DIGEST in {IDENTITY_RELPATH} is out of date before certification; "
            "the contract surface changed outside this command (tests/substrate/test_code_identity.py "
            "explains), fix that first"
        )

    modules = list(CELL_MODULES[runtime])
    report = run_cells(modules)
    outcome.cells = report.get("tests") or []
    problems = failures(report, modules)
    if problems:
        raise Refused("cells did not all pass; nothing was written:\n  " + "\n  ".join(problems))

    certified = False
    try:
        pins_path.write_text(updated_pins(pins_before.decode(), runtime, platform, installed, today), encoding="utf-8")
        identity_path.write_text(updated_identity(identity_before.decode(), compute_digest()), encoding="utf-8")
        drift_target = [DRIFT_TEST[runtime]]
        drift = run_cells(drift_target)
        outcome.drift = drift.get("tests") or []
        problems = failures(drift, drift_target)
        if problems:
            raise Refused("drift cell failed after pinning; pin restored:\n  " + "\n  ".join(problems))
        if not digest_in_sync():
            raise Refused("CONTRACT_SURFACE_DIGEST does not match after pinning; pin restored")
        certified = True
    finally:
        if not certified:
            pins_path.write_bytes(pins_before)
            identity_path.write_bytes(identity_before)
    outcome.certified = True
    return outcome


def subprocess_cell_runner(repo: Path, cert_home: Path, record_dir: Path) -> CellRunner:
    def run(targets: list[str]) -> dict:
        record_dir.mkdir(parents=True, exist_ok=True)
        result_path = record_dir / "last-run.json"
        result_path.unlink(missing_ok=True)
        env = dict(os.environ)
        env.pop(LIVE_HOME_ENV, None)
        env[CERT_ENV] = "1"
        env[CODEX_CERT_HOME_ENV] = str(cert_home)
        env[RESULT_ENV] = str(result_path)
        print(f"certify: running {' '.join(targets)}", file=sys.stderr, flush=True)
        completed = subprocess.run(
            [sys.executable, "-m", "tests.cells.certify_run", *targets], cwd=repo, env=env, check=False
        )
        if not result_path.is_file():
            raise Refused(f"cell runner exited {completed.returncode} without a result for {' '.join(targets)}")
        return json.loads(result_path.read_text(encoding="utf-8"))

    return run


def fresh_digest_check(repo: Path) -> Callable[[], bool]:
    def check() -> bool:
        code = (
            "import sys\nfrom agent_comms import code_identity as c\n"
            "sys.exit(0 if c.contract_surface_digest() == c.CONTRACT_SURFACE_DIGEST else 1)\n"
        )
        return subprocess.run([sys.executable, "-c", code], cwd=repo, check=False).returncode == 0

    return check


def fresh_digest(repo: Path) -> Callable[[], str]:
    def compute() -> str:
        code = "from agent_comms import code_identity as c\nprint(c.contract_surface_digest())\n"
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=repo, text=True, capture_output=True, check=True
        )
        return result.stdout.strip()

    return compute


def current_platform() -> str:
    import platform as _platform

    return f"{sys.platform}-{_platform.machine().lower()}"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    return result.stdout.strip()


def write_record(repo: Path, outcome: Outcome, cert_home: Path) -> Path:
    record_dir = repo / RECORD_DIR_RELPATH
    record_dir.mkdir(parents=True, exist_ok=True)
    path = record_dir / f"{outcome.runtime}-{outcome.installed}-{outcome.platform}.json"
    status = git(repo, "status", "--porcelain", "--", ".", f":(exclude){PINS_RELPATH}", f":(exclude){IDENTITY_RELPATH}")
    record = {
        "runtime": outcome.runtime,
        "platform": outcome.platform,
        "installed": outcome.installed,
        "previous_pin": outcome.previous,
        "certified_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "head": git(repo, "rev-parse", "HEAD"),
        "other_uncommitted_changes": bool(status),
        "python": sys.version.split()[0],
        "cert_home": str(cert_home),
        "cells": outcome.cells,
        "drift": outcome.drift,
    }
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--cert-home", type=Path, default=Path("~/.agent-comms/codex-cert-home"))
    parser.add_argument("--force", action="store_true", help="certify even when the pin already matches")
    args = parser.parse_args(argv)

    # SIGTERM unwinds like an interrupt so the restore in certify() runs.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    repo = REPO_ROOT
    platform = current_platform()
    try:
        if args.runtime in UNSUPPORTED:
            raise Refused(UNSUPPORTED[args.runtime])
        installed = installed_version(args.runtime)
        cert_home = check_cert_home(args.cert_home, Path.home())
        outcome = certify(
            args.runtime,
            repo=repo,
            platform=platform,
            installed=installed,
            today=_dt.date.today().isoformat(),
            force=args.force,
            run_cells=subprocess_cell_runner(repo, cert_home, repo / RECORD_DIR_RELPATH),
            compute_digest=fresh_digest(repo),
            digest_in_sync=fresh_digest_check(repo),
        )
    except Refused as exc:
        print(f"certify: REFUSED: {exc}", file=sys.stderr)
        return 1
    if outcome.nothing_to_do:
        print(f"certify: {args.runtime} {installed} on {platform} is already the pin; nothing to do (--force to re-run)")
        return 0
    record = write_record(repo, outcome, cert_home)
    print(
        f"certify: {args.runtime} {installed} certified on {platform} "
        f"(was {outcome.previous or 'unpinned'}); record {record.relative_to(repo)}"
    )
    print("certify: review and commit:")
    sys.stdout.flush()
    subprocess.run(["git", "--no-pager", "diff", "--", PINS_RELPATH, IDENTITY_RELPATH], cwd=repo, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
