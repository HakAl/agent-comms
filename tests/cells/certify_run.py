"""Run named cells and record every test's outcome for ``scripts/certify.py``.

Invoked as ``python -m tests.cells.certify_run <target>...`` where each
target is a ``tests.cells`` module or test id, so the isolation guard's
cells-only check (``AGENT_COMMS_TEST_CERT=1``) sees exactly the targets.
The verbose unittest log goes to stderr; the outcome of each test is written
as JSON to the path in ``AGENT_COMMS_TEST_CERT_RESULT``. Exit 0 only when
every test passed and none was skipped.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import sys
import unittest
from pathlib import Path

RESULT_ENV = "AGENT_COMMS_TEST_CERT_RESULT"
PASSED = "passed"
DETAIL_LIMIT = 4000


class RecordingResult(unittest.TextTestResult):
    """A verbose text result that also keeps one outcome per test id."""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.records: dict[str, dict[str, str]] = {}

    def _record(self, test: unittest.TestCase, outcome: str, detail: str = "") -> None:
        current = self.records.get(test.id())
        # A failed subtest has already marked the test; a later success call
        # for the same id must not hide it.
        if current is not None and current["outcome"] != PASSED:
            return
        self.records[test.id()] = {"outcome": outcome, "detail": detail[-DETAIL_LIMIT:]}

    def addSuccess(self, test):  # type: ignore[no-untyped-def]
        super().addSuccess(test)
        self._record(test, PASSED)

    def addFailure(self, test, err):  # type: ignore[no-untyped-def]
        super().addFailure(test, err)
        self._record(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test, err):  # type: ignore[no-untyped-def]
        super().addError(test, err)
        self._record(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):  # type: ignore[no-untyped-def]
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def addExpectedFailure(self, test, err):  # type: ignore[no-untyped-def]
        super().addExpectedFailure(test, err)
        self._record(test, "expected_failure", self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):  # type: ignore[no-untyped-def]
        super().addUnexpectedSuccess(test)
        self._record(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):  # type: ignore[no-untyped-def]
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.records[test.id()] = {
                "outcome": "failed",
                "detail": f"{subtest.id()}: {self._exc_info_to_string(err, test)}"[-DETAIL_LIMIT:],
            }


def main(argv: list[str]) -> int:
    result_path = os.environ.get(RESULT_ENV)
    if not argv or not result_path:
        print(
            f"usage: {RESULT_ENV}=<file> python -m tests.cells.certify_run <tests.cells target>...",
            file=sys.stderr,
        )
        return 2
    suite = unittest.defaultTestLoader.loadTestsFromNames(argv)
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=RecordingResult)
    result = runner.run(suite)
    records = result.records  # type: ignore[attr-defined]
    Path(result_path).write_text(
        json.dumps(
            {
                "targets": argv,
                "tests_run": result.testsRun,
                "tests": [{"id": test_id, **record} for test_id, record in sorted(records.items())],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    passed = bool(records) and all(record["outcome"] == PASSED for record in records.values())
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
