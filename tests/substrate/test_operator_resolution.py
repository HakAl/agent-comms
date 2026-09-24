"""The operator human is configuration, never a hardcoded id."""

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms.schema import ValidationError
from agent_comms.store import Store

HUMAN_A = "01M36YTJV9XBW95S6ZWV47C4RG"
HUMAN_B = "01M36YTJV9XBW95S6ZWV47C4RH"


class OperatorResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / "agent-comms.sqlite")
        self.store.init()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGENT_COMMS_OPERATOR_ACTOR", None)

    def resolve(self) -> str:
        return self.store._actors.resolve_operator_human()

    def test_single_registered_human_is_the_operator(self) -> None:
        self.store.register_actor(HUMAN_A, "human", "alice")
        self.store.register_agent("team-a-architect", "team-a", "architect", str(self.root), [])
        self.assertEqual(self.resolve(), HUMAN_A)

    def test_no_human_refuses(self) -> None:
        self.store.register_agent("team-a-architect", "team-a", "architect", str(self.root), [])
        with self.assertRaisesRegex(ValidationError, "no human actor is registered"):
            self.resolve()

    def test_several_humans_refuse_without_override(self) -> None:
        self.store.register_actor(HUMAN_A, "human", "alice")
        self.store.register_actor(HUMAN_B, "human", "bob")
        with self.assertRaisesRegex(ValidationError, "AGENT_COMMS_OPERATOR_ACTOR"):
            self.resolve()

    def test_override_selects_among_several_humans(self) -> None:
        self.store.register_actor(HUMAN_A, "human", "alice")
        self.store.register_actor(HUMAN_B, "human", "bob")
        os.environ["AGENT_COMMS_OPERATOR_ACTOR"] = HUMAN_B
        self.assertEqual(self.resolve(), HUMAN_B)

    def test_override_must_name_a_human(self) -> None:
        self.store.register_actor(HUMAN_A, "human", "alice")
        self.store.register_agent("team-a-architect", "team-a", "architect", str(self.root), [])
        os.environ["AGENT_COMMS_OPERATOR_ACTOR"] = "team-a-architect"
        with self.assertRaisesRegex(ValidationError, "not human"):
            self.resolve()


if __name__ == "__main__":
    unittest.main()
