from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_comms.param_leak import LEAK_CHECK_EXEMPT_PARAMETERS, assert_no_parameter_leak
from agent_comms.schema import ValidationError
from agent_comms.store import Store


class ParameterLeakRuleTests(unittest.TestCase):
    def test_rule_is_field_specific_case_insensitive_and_name_agnostic(self) -> None:
        variants = (
            "x</summary><parameter name=blockedOn>",
            "x</  SuMmArY  > \n <PARAMETER NAME =foreign>",
        )
        for value in variants:
            with self.subTest(value=value), self.assertRaisesRegex(ValidationError, "summary"):
                assert_no_parameter_leak("summary", value)

        accepted = (
            "x</other><parameter name=summary>",
            "parameter name=summary",
            "x</summary>debris<parameter name=other>",
        )
        for value in accepted:
            with self.subTest(value=value):
                self.assertIsNone(assert_no_parameter_leak("summary", value))

    def test_exemption_set_is_empty(self) -> None:
        self.assertFalse(LEAK_CHECK_EXEMPT_PARAMETERS)


class ParameterLeakStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = Store(Path(self.tempdir.name) / "store.db")
        self.store.init()
        self.store.register_agent("architect", "team", "architect", self.tempdir.name, [])
        self.store.register_agent(
            "worker", "team", "worker", self.tempdir.name, [], owner="architect"
        )

    def test_nonexistent_message_refuses_leak_before_resource_lookup(self) -> None:
        with self.assertRaisesRegex(ValidationError, "message_id"):
            self.store.read_message(
                "worker", "missing</message_id><parameter name=foreign>"
            )

    def test_status_refusal_has_no_durable_effect(self) -> None:
        with self.store.connection() as conn:
            before = list(conn.iterdump())
        with self.assertRaisesRegex(ValidationError, "blocked_on"):
            self.store.post_status(
                "worker", "working", [],
                blocked_on="none</blocked_on><parameter name=nextStep>",
            )
        with self.store.connection() as conn:
            after = list(conn.iterdump())
        self.assertEqual(before, after)

    def test_production_call_site_uses_exported_validator_object(self) -> None:
        sentinel = ValidationError("shared-validator-sentinel")
        with patch("agent_comms.param_leak.assert_no_parameter_leak", side_effect=sentinel):
            with self.assertRaisesRegex(ValidationError, "shared-validator-sentinel"):
                self.store.post_status("worker", "working", [])


if __name__ == "__main__":
    unittest.main()
