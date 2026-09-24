"""Runtime-free checks for the shared message-only cell evidence contract."""

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import copy
import unittest
from unittest import mock

from tests.dispatch_cell_harness import ARCHITECT_ID, DispatchCellHarness


class FileBackedCellContractTest(unittest.TestCase):
    def setUp(self):
        self.harness = object.__new__(DispatchCellHarness)
        self.harness.worker_id = "worker"
        self.harness.store = mock.Mock()
        self.fixture = b"exact\r\nartifact bytes\n"
        self.trigger = {"id": "trigger", "status": "closed"}
        self.reply = {
            "id": "reply",
            "from": "worker",
            "parent_message_id": "trigger",
            "body_storage": "artifact",
        }
        self.triggers = [self.trigger]
        self.replies = [self.reply]
        self.harness.store.list_inbox.side_effect = lambda actor, **kw: (
            self.replies if actor == ARCHITECT_ID else self.triggers
        )
        self.harness.store.read_message.return_value = {"body": self.fixture.decode()}
        self.dispatch = {
            "message_id": "trigger",
            "status": "closed",
            "policy_version": "v2",
            "result": "satisfied",
            "observed_values": {
                "closeout": {
                    "protocol": 1,
                    "reply_message_id": "reply",
                    "delta": None,
                }
            },
        }

    def check(self):
        return self.harness.assert_closed_with_file_backed_reply(
            self.dispatch, self.fixture
        )

    def test_accepts_exact_artifact_without_delta(self):
        self.assertEqual(self.check(), (self.reply, []))
        self.harness.store.read_message.assert_called_once_with(ARCHITECT_ID, "reply")

    def test_rejects_altered_bytes(self):
        self.harness.store.read_message.return_value = {
            "body": "exact\nartifact bytes\n"
        }
        with self.assertRaises(AssertionError):
            self.check()

    def test_rejects_invalid_closeout(self):
        original = copy.deepcopy(self.dispatch)
        for key, value in (
            ("protocol", 2),
            ("reply_message_id", "other"),
            ("delta", {}),
            ("delta", {"entries": ["file"]}),
        ):
            with self.subTest(key=key, value=value):
                self.dispatch = copy.deepcopy(original)
                self.dispatch["observed_values"]["closeout"][key] = value
                with self.assertRaises(AssertionError):
                    self.check()
        for key, value in (
            ("status", "in_flight"),
            ("result", "blocked"),
            ("policy_version", "v1"),
        ):
            with self.subTest(key=key):
                self.dispatch = copy.deepcopy(original)
                self.dispatch[key] = value
                with self.assertRaises(AssertionError):
                    self.check()

    def test_rejects_invalid_message_bindings(self):
        for key, value in (
            ("from", "other"),
            ("parent_message_id", "other"),
            ("body_storage", "inline"),
        ):
            with self.subTest(key=key):
                original = self.reply[key]
                self.reply[key] = value
                with self.assertRaises(AssertionError):
                    self.check()
                self.reply[key] = original
        self.replies.append(dict(self.reply, id="duplicate"))
        with self.assertRaises(AssertionError):
            self.check()

    def test_rejects_missing_duplicate_or_open_trigger(self):
        for triggers in (
            [],
            [self.trigger, self.trigger],
            [dict(self.trigger, status="read")],
        ):
            with self.subTest(triggers=triggers):
                self.triggers = triggers
                with self.assertRaises(AssertionError):
                    self.check()

    def test_returns_other_parented_replies_for_callers_to_reject(self):
        other = dict(self.reply, id="inline", body_storage="inline")
        self.replies.append(other)
        self.assertEqual(self.check(), (self.reply, [other]))

    def test_generic_edit_assertion_still_requires_delta(self):
        with self.assertRaisesRegex(AssertionError, "no delta snapshot"):
            self.harness.assert_closed_with_parented_reply(self.dispatch)
