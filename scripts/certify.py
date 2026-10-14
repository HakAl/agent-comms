#!/usr/bin/env python3
"""Certify the installed runtime and update its pin: ``make certify RUNTIME=codex``.

Anyone may run it, an agent or a dispatch included; it needs no live-home
switch. Steps:

1. find the native Codex bundle behind the ``codex`` on PATH (``--bundle``
   names one directly) and read its version; when it already matches this
   platform's pin, digest included, there is nothing to do (``--force``
   certifies anyway);
2. check the cert login directory (``--cert-home``, default
   ``~/.agent-comms/codex-cert-home``): it must hold ``auth.json`` and must not
   be the home itself, ``~/.codex``, ``~/.claude`` or any other part of
   ``~/.agent-comms``;
3. copy the bundle into a cert custody directory under
   ``local/certifications/`` and take its digest: workers run only a custody
   copy whose digest matches the pin, so the cells run that copy;
4. journal both files (``local/certifications/pending-restore.json``), then
   write the candidate pin (version, date, bundle sha256) and
   ``CONTRACT_SURFACE_DIGEST``: the adapter checks the pin, so it must be on
   disk while the cells run;
5. run the runtime's cells through ``tests.cells.certify_run`` in
   certification mode (``AGENT_COMMS_TEST_CERT=1``: test isolation stays on),
   then the drift cell, then check the digest in a fresh interpreter;
6. unless every test of every module ran and passed, restore both files
   byte for byte; the journal is deleted last, and a leftover one (a killed
   run) is restored at the next start, so an uncertified pin never stays;
7. write a record under ``local/certifications/`` and print the diff to commit.

One run at a time: ``local/certifications/certify.lock`` is held from before
any recovery to the end, so a second run is refused instead of taking the
first run's live journal for an abandoned one. The journal is flushed to
stable storage before the pin changes, and the kept or restored files before
the journal goes (F_FULLFSYNC on macOS, fsync elsewhere; a run refuses when
it cannot flush), so recovery also holds across a power loss.

Only Codex is automated. Claude certification still uses the live-home
procedure in CONTRIBUTING.md, because its login under a scratch HOME is not
verified yet.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as _dt
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
PINS_RELPATH = "agent_comms/runtime_pins.json"
IDENTITY_RELPATH = "agent_comms/code_identity.py"
RECORD_DIR_RELPATH = "local/certifications"
JOURNAL_RELPATH = "local/certifications/pending-restore.json"
CERT_CUSTODY_RELPATH = "local/certifications/custody"
LOCK_RELPATH = "local/certifications/certify.lock"

CERT_ENV = "AGENT_COMMS_TEST_CERT"
LIVE_HOME_ENV = "AGENT_COMMS_TEST_LIVE_HOME"
RESULT_ENV = "AGENT_COMMS_TEST_CERT_RESULT"
CODEX_CERT_HOME_ENV = "AGENT_COMMS_TEST_CODEX_CERT_HOME"
CODEX_VERSIONS_TEST_ENV = "AGENT_COMMS_TEST_CODEX_VERSIONS_DIR"

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
    sha256: str = ""
    certified: bool = False
    nothing_to_do: bool = False
    cells: list[dict] = field(default_factory=list)
    drift: list[dict] = field(default_factory=list)


def _agent_comms():
    """Import the package from this checkout (the script runs from scripts/)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from agent_comms import custody

    return custody


def installed_bundle(bundle: Path | None) -> tuple[Path, str]:
    """The native Codex bundle to certify and its version."""
    custody = _agent_comms()
    try:
        located = Path(os.path.realpath(bundle)) if bundle is not None else custody.locate_native_bundle()
        return located, custody.bundle_version(located)
    except custody.CustodyError as exc:
        raise Refused(str(exc)) from exc


def prepare_cert_custody(repo: Path, bundle: Path, version: str) -> tuple[Path, str]:
    """Copy ``bundle`` into a fresh cert custody directory; return it and the digest."""
    custody = _agent_comms()
    versions = repo / CERT_CUSTODY_RELPATH
    shutil.rmtree(versions, ignore_errors=True)
    print(f"certify: copying {bundle} into {versions}", file=sys.stderr, flush=True)
    return versions, custody.copy_bundle(bundle, versions / version)


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


def updated_pins(text: str, runtime: str, platform: str, version: str, today: str, sha256: str | None = None) -> str:
    manifest = json.loads(text)
    entry = {"version": version, "certified_on": today}
    if sha256:
        entry["sha256"] = sha256
    manifest["runtimes"][runtime]["platforms"][platform] = entry
    return json.dumps(manifest, indent=2) + "\n"


def updated_identity(text: str, digest: str) -> str:
    new, count = DIGEST_RE.subn(lambda m: m.group(1) + digest + m.group(3), text)
    if count != 1:
        raise Refused(f"could not find CONTRACT_SURFACE_DIGEST in {IDENTITY_RELPATH}")
    return new


def previous_entry(pins_text: str, runtime: str, platform: str) -> dict:
    return json.loads(pins_text)["runtimes"][runtime]["platforms"].get(platform) or {}


@contextlib.contextmanager
def run_lock(repo: Path) -> Iterator[None]:
    """Hold the checkout's certify lock, or refuse while another run holds it."""
    path = repo / LOCK_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(f"another certify run holds {LOCK_RELPATH}; wait for it to finish") from None
        yield


def sync_fd(fd: int, path: Path) -> None:
    """Flush ``fd`` to stable storage, or refuse.

    On macOS plain fsync can leave writes in the drive cache and let later
    ones land first across a power loss; F_FULLFSYNC flushes the cache.
    """
    try:
        if sys.platform == "darwin":
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        else:
            os.fsync(fd)
    except OSError as exc:
        raise Refused(f"cannot make {path} durable: {exc}") from exc


def fsync_file(path: Path) -> None:
    with open(path, "rb") as handle:
        sync_fd(handle.fileno(), path)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        sync_fd(fd, path)
    finally:
        os.close(fd)


def write_journal(repo: Path, originals: dict[str, bytes]) -> Path:
    """Record the original bytes durably before any write, so a killed run is undone."""
    journal = repo / JOURNAL_RELPATH
    journal.parent.mkdir(parents=True, exist_ok=True)
    staged = journal.with_suffix(".tmp")
    staged.write_text(
        json.dumps({relpath: base64.b64encode(data).decode("ascii") for relpath, data in originals.items()}, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        fsync_file(staged)
        os.replace(staged, journal)
        fsync_dir(journal.parent)
    except Refused:
        # Nothing is pinned yet, so a journal that is not durable is dropped.
        staged.unlink(missing_ok=True)
        journal.unlink(missing_ok=True)
        raise
    return journal


def remove_journal(repo: Path, relpaths: list[str]) -> None:
    """Make the files the journal guards durable, then drop the journal durably."""
    for relpath in relpaths:
        fsync_file(repo / relpath)
    journal = repo / JOURNAL_RELPATH
    journal.unlink()
    fsync_dir(journal.parent)


def restore_pending(repo: Path) -> bool:
    """Undo a run that ended without its restore (SIGKILL, power loss)."""
    journal = repo / JOURNAL_RELPATH
    if not journal.is_file():
        return False
    originals = json.loads(journal.read_text(encoding="utf-8"))
    for relpath, data in originals.items():
        (repo / relpath).write_bytes(base64.b64decode(data))
    remove_journal(repo, list(originals))
    print(f"certify: restored {', '.join([PINS_RELPATH, IDENTITY_RELPATH])} from a leftover {JOURNAL_RELPATH}", file=sys.stderr)
    return True


def certify(
    runtime: str,
    *,
    repo: Path,
    platform: str,
    installed: str,
    sha256: str,
    today: str,
    force: bool,
    run_cells: CellRunner,
    compute_digest: Callable[[], str],
    digest_in_sync: Callable[[], bool],
) -> Outcome:
    """Pin the candidate, run the cells against it, keep it only on a full pass."""
    if runtime in UNSUPPORTED:
        raise Refused(UNSUPPORTED[runtime])
    if runtime not in CELL_MODULES:
        raise Refused(f"unknown runtime {runtime!r} (automated: {', '.join(sorted(CELL_MODULES))})")
    restore_pending(repo)
    pins_path = repo / PINS_RELPATH
    identity_path = repo / IDENTITY_RELPATH
    pins_before = pins_path.read_bytes()
    identity_before = identity_path.read_bytes()
    previous = previous_entry(pins_before.decode(), runtime, platform)
    outcome = Outcome(runtime, platform, installed, previous.get("version"), sha256)
    if previous.get("version") == installed and previous.get("sha256") == sha256 and not force:
        outcome.nothing_to_do = True
        return outcome
    if not digest_in_sync():
        raise Refused(
            f"CONTRACT_SURFACE_DIGEST in {IDENTITY_RELPATH} is out of date before certification; "
            "the contract surface changed outside this command (tests/substrate/test_code_identity.py "
            "explains), fix that first"
        )

    write_journal(repo, {PINS_RELPATH: pins_before, IDENTITY_RELPATH: identity_before})
    certified = False
    try:
        pins_path.write_text(updated_pins(pins_before.decode(), runtime, platform, installed, today, sha256), encoding="utf-8")
        identity_path.write_text(updated_identity(identity_before.decode(), compute_digest()), encoding="utf-8")
        modules = list(CELL_MODULES[runtime])
        report = run_cells(modules)
        outcome.cells = report.get("tests") or []
        problems = failures(report, modules)
        if problems:
            raise Refused("cells did not all pass; pin restored:\n  " + "\n  ".join(problems))
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
        remove_journal(repo, [PINS_RELPATH, IDENTITY_RELPATH])
    outcome.certified = True
    return outcome


def subprocess_cell_runner(repo: Path, cert_home: Path, record_dir: Path, cert_custody: Path) -> CellRunner:
    def run(targets: list[str]) -> dict:
        record_dir.mkdir(parents=True, exist_ok=True)
        result_path = record_dir / "last-run.json"
        result_path.unlink(missing_ok=True)
        env = dict(os.environ)
        env.pop(LIVE_HOME_ENV, None)
        env[CERT_ENV] = "1"
        env[CODEX_CERT_HOME_ENV] = str(cert_home)
        env[CODEX_VERSIONS_TEST_ENV] = str(cert_custody)
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
        "sha256": outcome.sha256,
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
    parser.add_argument("--bundle", type=Path, default=None, help="native Codex bundle (holding bin/codex); default: the one behind codex on PATH")
    parser.add_argument("--force", action="store_true", help="certify even when the pin already matches")
    args = parser.parse_args(argv)

    # SIGTERM unwinds like an interrupt so the restore in certify() runs.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    repo = REPO_ROOT
    try:
        with run_lock(repo):
            return run(args, repo)
    except Refused as exc:
        print(f"certify: REFUSED: {exc}", file=sys.stderr)
        return 1


def run(args: argparse.Namespace, repo: Path) -> int:
    """One certification, with the checkout's certify lock held throughout."""
    platform = current_platform()
    cert_custody = repo / CERT_CUSTODY_RELPATH
    try:
        if args.runtime in UNSUPPORTED:
            raise Refused(UNSUPPORTED[args.runtime])
        restore_pending(repo)
        bundle, installed = installed_bundle(args.bundle)
        cert_home = check_cert_home(args.cert_home, Path.home())
        cert_custody, sha256 = prepare_cert_custody(repo, bundle, installed)
        outcome = certify(
            args.runtime,
            repo=repo,
            platform=platform,
            installed=installed,
            sha256=sha256,
            today=_dt.date.today().isoformat(),
            force=args.force,
            run_cells=subprocess_cell_runner(repo, cert_home, repo / RECORD_DIR_RELPATH, cert_custody),
            compute_digest=fresh_digest(repo),
            digest_in_sync=fresh_digest_check(repo),
        )
    except Refused as exc:
        print(f"certify: REFUSED: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(cert_custody, ignore_errors=True)
    if outcome.nothing_to_do:
        print(f"certify: {args.runtime} {installed} on {platform} is already the pin; nothing to do (--force to re-run)")
        return 0
    record = write_record(repo, outcome, cert_home)
    print(
        f"certify: {args.runtime} {installed} certified on {platform} "
        f"(was {outcome.previous or 'unpinned'}; bundle sha256 {outcome.sha256}); record {record.relative_to(repo)}"
    )
    print("certify: review and commit, then fill the live custody copy with: agent-comms custody codex")
    sys.stdout.flush()
    subprocess.run(["git", "--no-pager", "diff", "--", PINS_RELPATH, IDENTITY_RELPATH], cwd=repo, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
