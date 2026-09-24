#!/usr/bin/env python3
"""Lint gate: ruff must not add findings beyond the recorded baseline.

The baseline (``scripts/gates/ruff-baseline.txt``) records how many findings
each (file, rule) pair had when the gate was introduced; those are inherited
from the source and cleaned up over time. The gate fails when a pair grows or
a new pair appears. Fewer findings always pass. Line numbers are not part of
the baseline, so edits elsewhere in a file do not churn it.

Trade-off: counting per (file, rule) pair means one fixed finding leaves
headroom for one new finding of the same rule in the same file. To keep that
window short the gate prints an INFO line whenever the tree is below the
baseline; ratchet with ``--update-baseline`` (``make lint-baseline``) as
part of the change that removed the findings.

The gate also requires every GitHub Action used by the workflows to be
pinned to a full commit SHA with a trailing version comment, so a moved or
missing tag can neither break nor silently change CI.

Run through ``make lint`` and ``make lint-baseline``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).resolve().parent / "ruff-baseline.txt"
RUFF_VERSION = "0.16.8"
RUFF_ENV = "RUFF"
WORKFLOWS_DIR = Path(".github") / "workflows"
# ``uses: owner/repo[/path]@<40-hex>  # vN[.N.N]``; local ``./`` actions are exempt.
USES_LINE = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
PINNED_REF = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[^@\s]+)?@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^\s*#\s*v\d+(?:\.\d+)*\s*$")

Counts = dict[tuple[str, str], int]


def workflow_pin_findings(repo: Path) -> list[str]:
    """Every ``uses:`` in the workflows must be ``@<sha>`` with a ``# vN`` comment."""
    findings = []
    workflows = repo / WORKFLOWS_DIR
    if not workflows.is_dir():
        return findings
    for path in sorted(list(workflows.glob("*.yml")) + list(workflows.glob("*.yaml"))):
        rel = path.relative_to(repo).as_posix()
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            match = USES_LINE.match(line)
            if match is None:
                continue
            ref = match.group("ref").strip("'\"")
            if ref.startswith("./"):
                continue
            if not PINNED_REF.match(ref):
                findings.append(f"{rel}:{number}: not pinned to a commit SHA: {ref}")
            elif not VERSION_COMMENT.match(match.group("rest")):
                findings.append(f"{rel}:{number}: SHA pin lacks a `# vN` version comment: {ref}")
    return findings


def ruff_command() -> list[str]:
    override = os.environ.get(RUFF_ENV)
    if override:
        return shlex.split(override)
    return ["uvx", f"ruff@{RUFF_VERSION}"]


def run_ruff(repo: Path) -> list[dict]:
    result = subprocess.run(
        [*ruff_command(), "check", "--output-format", "json", "--exit-zero", "."],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ruff failed: {result.stderr.strip()}")
    return json.loads(result.stdout or "[]")


def aggregate(findings: Sequence[dict], *, repo: Path) -> Counts:
    counts: Counts = {}
    for finding in findings:
        path = os.path.relpath(finding["filename"], repo).replace(os.sep, "/")
        key = (path, str(finding["code"]))
        counts[key] = counts.get(key, 0) + 1
    return counts


def headroom(current: Counts, baseline: Counts) -> int:
    """How many baseline findings the current tree no longer has."""
    return sum(
        max(recorded - current.get(key, 0), 0) for key, recorded in baseline.items()
    )


def compare(current: Counts, baseline: Counts) -> list[str]:
    regressions = []
    for (path, code), count in sorted(current.items()):
        recorded = baseline.get((path, code))
        if recorded is None:
            regressions.append(f"{path}: {code}: {count} new")
        elif count > recorded:
            regressions.append(f"{path}: {code}: {count} (baseline {recorded})")
    return regressions


def load_baseline(path: Path) -> Counts:
    counts: Counts = {}
    if not path.exists():
        return counts
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        count, file_path, code = line.split("\t")
        counts[(file_path, code)] = int(count)
    return counts


def write_baseline(path: Path, counts: Counts) -> None:
    lines = ["# count\tpath\trule; regenerate with `make lint-baseline`"]
    for (file_path, code), count in sorted(counts.items()):
        lines.append(f"{count}\t{file_path}\t{code}")
    path.write_text("\n".join(lines) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--baseline", default=str(BASELINE))
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    baseline_path = Path(args.baseline)

    current = aggregate(run_ruff(repo), repo=repo)
    total = sum(current.values())
    if args.update_baseline:
        write_baseline(baseline_path, current)
        print(f"PASS lint-baseline ({total} findings recorded in {baseline_path})")
        return 0

    pins = workflow_pin_findings(repo)
    if pins:
        print(f"FAIL lint: {len(pins)} unpinned GitHub Action(s)")
        for line in pins:
            print(f"  {line}")
        return 1

    baseline = load_baseline(baseline_path)
    regressions = compare(current, baseline)
    below = headroom(current, baseline)
    if below:
        print(f"INFO lint: {below} finding(s) below the baseline; run `make lint-baseline` to ratchet")
    if regressions:
        print(f"FAIL lint: {len(regressions)} regression(s) against {baseline_path.name}")
        for line in regressions:
            print(f"  {line}")
        return 1
    print(f"PASS lint ({total} findings, none beyond the baseline; actions pinned)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"FAIL lint: {exc}", file=sys.stderr)
        raise SystemExit(1)
