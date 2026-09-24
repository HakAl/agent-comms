import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import unittest

from agent_comms.adapters.claude import ClaudeAdapter
from agent_comms.adapters.codex import CodexAdapter
from agent_comms.adapters.fake import FakeAdapter
from agent_comms.adapters.registry import adapter_for


class AdapterRegistryTest(unittest.TestCase):
    def test_unknown_runtime_rejects(self) -> None:
        with self.assertRaisesRegex(ValueError, "no adapter registered for runtime: nonexistent"):
            adapter_for("nonexistent")

    def test_known_runtimes_resolve_to_fresh_instances(self) -> None:
        expected = {
            "claude": ClaudeAdapter,
            "codex": CodexAdapter,
            "fake": FakeAdapter,
        }

        for runtime, adapter_type in expected.items():
            with self.subTest(runtime=runtime):
                first = adapter_for(runtime)
                second = adapter_for(runtime)

                self.assertIsInstance(first, adapter_type)
                self.assertIsInstance(second, adapter_type)
                self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
