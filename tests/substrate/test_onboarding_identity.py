import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import subprocess
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


class OnboardingRepoRootTest(unittest.TestCase):
    """Without --repo-root the worktree comes from the owner's project_root."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.worktrees = self.root / "worktrees"
        self.worktrees.mkdir()
        self.store = Store(self.root / "agent-comms.sqlite")

    def git_checkout(self, path: Path) -> Path:
        path.mkdir()
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "base"]):
            subprocess.run(
                ["git", "-c", "user.email=t@t.invalid", "-c", "user.name=T", *args],
                cwd=path, check=True, capture_output=True,
            )
        return path

    def onboard(self, **overrides):
        arguments = dict(
            team="alpha", runtime="fake", actor_id="alpha-fake-worker",
            owner="alpha-architect", worktree_root=str(self.worktrees),
        )
        arguments.update(overrides)
        return onboard_worker(self.store, **arguments)

    def test_owner_project_root_that_is_a_git_checkout_is_the_default(self) -> None:
        checkout = self.git_checkout(self.root / "owner checkout")
        self.store.register_agent("alpha-architect", "alpha", "architect", str(checkout), [])
        result = self.onboard()
        worktree = Path(result["worktree_path"])
        self.assertEqual(worktree.parent, self.worktrees.resolve())
        toplevel = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=worktree, check=True, text=True, capture_output=True
        ).stdout.strip()
        self.assertEqual(Path(toplevel).resolve(), (checkout / ".git").resolve())

    def test_owner_project_root_that_is_not_a_checkout_names_the_flag(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        self.store.register_agent("alpha-architect", "alpha", "architect", str(plain), [])
        with self.assertRaises(ValidationError) as caught:
            self.onboard()
        self.assertIn("--repo-root", str(caught.exception))
        self.assertIn(str(plain), str(caught.exception))
        self.assertEqual(list(self.worktrees.iterdir()), [])

    def test_unknown_owner_names_the_flag(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self.onboard()
        self.assertIn("--repo-root", str(caught.exception))
        self.assertIn("alpha-architect", str(caught.exception))

    def test_explicit_repo_root_that_is_not_a_checkout_is_a_validation_error(self) -> None:
        # Review finding: a missing path used to surface as a FileNotFoundError
        # traceback from git's cwd, not as the CLI's structured error.
        self.store.register_agent("alpha-architect", "alpha", "architect", str(self.root), [])
        for candidate in (self.root / "does-not-exist", self.root):
            with self.subTest(candidate=candidate), self.assertRaises(ValidationError) as caught:
                self.onboard(repo_root=candidate)
            self.assertIn("--repo-root", str(caught.exception))
            self.assertIn(str(candidate), str(caught.exception))
        self.assertEqual(list(self.worktrees.iterdir()), [])

    def test_explicit_repo_root_may_be_a_string(self) -> None:
        # Review finding: a str crashed on .expanduser() instead of being a path.
        explicit = self.git_checkout(self.root / "explicit")
        self.store.register_agent("alpha-architect", "alpha", "architect", str(self.root), [])
        result = self.onboard(repo_root=str(explicit))
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=result["worktree_path"], check=True, text=True, capture_output=True
        ).stdout.strip()
        self.assertEqual(Path(common).resolve(), (explicit / ".git").resolve())

    def test_subdirectory_of_a_checkout_places_the_worktree_beside_the_checkout(self) -> None:
        # Review finding: a project_root inside a checkout (a monorepo service
        # directory) passed the git check, and the default worktree location,
        # the parent of the repo root, then landed inside the repository.
        checkout = self.git_checkout(self.root / "mono")
        service = checkout / "services" / "backend"
        service.mkdir(parents=True)
        self.store.register_agent("alpha-architect", "alpha", "architect", str(service), [])
        for index, repo_root in enumerate((None, service, str(service))):
            with self.subTest(repo_root=repo_root):
                result = self.onboard(worktree_root=None, repo_root=repo_root, actor_id=f"alpha-fake-worker-{index}")
                worktree = Path(result["worktree_path"])
                self.assertEqual(worktree.parent, checkout.parent.resolve())
                self.assertNotIn(checkout.resolve(), worktree.parents)
                subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=checkout, check=True, capture_output=True)
                subprocess.run(["git", "branch", "-D", result["branch"]], cwd=checkout, check=True, capture_output=True)
        self.assertEqual(
            subprocess.run(["git", "status", "--porcelain"], cwd=checkout, check=True, text=True, capture_output=True).stdout,
            "",
        )

    def test_explicit_repo_root_wins_over_the_owner(self) -> None:
        self.git_checkout(self.root / "owner")
        explicit = self.git_checkout(self.root / "explicit")
        self.store.register_agent("alpha-architect", "alpha", "architect", str(self.root / "owner"), [])
        result = self.onboard(repo_root=explicit)
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=result["worktree_path"], check=True, text=True, capture_output=True
        ).stdout.strip()
        self.assertEqual(Path(common).resolve(), (explicit / ".git").resolve())


if __name__ == "__main__":
    unittest.main()
