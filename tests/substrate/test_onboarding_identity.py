import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import tempfile
import unittest
from pathlib import Path

from agent_comms.onboarding import onboard_worker
from agent_comms.schema import ValidationError
from agent_comms.store import Store


class OnboardingIdentityTest(unittest.TestCase):
    def test_onboard_worker_rejects_noncanonical_before_git_mutation(self) -> None:
        bad_actor_ids = ["a/b", " alpha-worker ", "Alpha-Worker"]
        for actor_id in bad_actor_ids:
            with self.subTest(actor_id=actor_id):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    repo_root = root / "repo"
                    worktree_root = root / "worktrees"
                    repo_root.mkdir()
                    worktree_root.mkdir()
                    store = Store(root / "agent-comms.sqlite")

                    with self.assertRaises(ValidationError):
                        onboard_worker(
                            store,
                            team="alpha",
                            runtime="codex",
                            actor_id=actor_id,
                            repo_root=repo_root,
                            worktree_root=str(worktree_root),
                            owner="alpha-architect",
                        )

                    self.assertEqual(store.list_actors(), [])
                    self.assertEqual(list(worktree_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
