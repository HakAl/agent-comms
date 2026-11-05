import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import fsutil


class FsutilTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_atomic_write_text_replaces_the_target_and_leaves_no_temp_file(self) -> None:
        target = self.root / "config.json"
        target.write_text("old")
        fsutil.atomic_write_text(target, "new\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["config.json"])

    def test_atomic_write_json_is_sorted_indented_and_newline_terminated(self) -> None:
        target = self.root / "record.json"
        fsutil.atomic_write_json(target, {"b": 1, "a": [1, 2]})
        text = target.read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps({"a": [1, 2], "b": 1}, indent=2, sort_keys=True) + "\n")

    def test_failed_write_keeps_the_old_target_and_removes_the_temp_file(self) -> None:
        target = self.root / "config.json"
        target.write_text("old")
        with mock.patch.object(fsutil.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                fsutil.atomic_write_text(target, "new")
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["config.json"])

    def test_atomic_write_fsyncs_before_replacing(self) -> None:
        target = self.root / "config.json"
        calls: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(fsutil.os, "fsync", side_effect=lambda fd: (calls.append("fsync"), real_fsync(fd))), \
                mock.patch.object(fsutil.os, "replace", side_effect=lambda a, b: (calls.append("replace"), real_replace(a, b))):
            fsutil.atomic_write_text(target, "x")
        self.assertEqual(calls, ["fsync", "replace"])

    def test_missing_parent_directory_is_the_callers_problem(self) -> None:
        with self.assertRaises(FileNotFoundError):
            fsutil.atomic_write_text(self.root / "missing" / "f", "x")

    def test_file_sha256_matches_hashlib_and_raises_oserror_when_unreadable(self) -> None:
        target = self.root / "blob"
        payload = os.urandom(3 * (1 << 20) + 17)
        target.write_bytes(payload)
        self.assertEqual(fsutil.file_sha256(target), hashlib.sha256(payload).hexdigest())
        with self.assertRaises(OSError):
            fsutil.file_sha256(self.root / "absent")


if __name__ == "__main__":
    unittest.main()
