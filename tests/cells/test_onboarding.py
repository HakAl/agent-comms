from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import subprocess
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import paths, supervisor
from agent_comms.adapters.fake import FakeAdapter
from agent_comms.onboarding import onboard_worker
from agent_comms.schema import ValidationError
from agent_comms.store import Store

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"
ARCHITECT_ID = "alpha-architect"


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "README.md").write_text("fixture\n")
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "agent_comms",
        repo / "agent_comms",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _git(repo, "add", "README.md")
    _git(repo, "add", "agent_comms")
    _git(repo, "-c", "user.name=Test User", "-c", "user.email=test@example.invalid", "commit", "-m", "init")
    return repo


def _assert_no_onboard_mutation(test: unittest.TestCase, store: Store, actor_id: str, worktree: Path, repo: Path) -> None:
    test.assertNotIn(actor_id, {actor["id"] for actor in store.list_actors()})
    test.assertFalse(worktree.exists())
    branch_exists = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/worker/{actor_id}"],
        cwd=repo,
        check=False,
    ).returncode == 0
    test.assertFalse(branch_exists)


class OnboardWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.tmp = Path(self._tmpdir.name)
        self.control_root = self.tmp / "s"
        self._owned_registry = supervisor.ReaperRegistry()
        self._fixture_cleaned = False
        with supervisor._REGISTRY_LOCK:
            self._prior_registry = supervisor._REGISTRY
            supervisor._REGISTRY = self._owned_registry
        self._control_root_patch = mock.patch.dict(
            "os.environ", {supervisor.CONTROL_ROOT_ENV: str(self.control_root)}
        )
        self._control_root_patch.start()
        self.addCleanup(self._cleanup_fixture)
        self.repo = _make_repo(self.tmp)
        self.worktree_root = self.tmp / "worktrees"
        self.worktree_root.mkdir()
        self.store = Store(self.tmp / "agent-comms.sqlite")
        self.store.register_actor(HUMAN_ID, "human", "alice")
        self.store.register_agent_actor(ARCHITECT_ID, "alpha", "architect", str(self.repo), [])

    def tearDown(self) -> None:
        self._cleanup_fixture()

    def _cleanup_fixture(self) -> None:
        if self._fixture_cleaned:
            return
        self._fixture_cleaned = True
        try:
            deadline = time.monotonic() + 8
            while self._owned_registry.pending() and time.monotonic() < deadline:
                self._owned_registry.reap_ready()
                if self._owned_registry.pending():
                    threading.Event().wait(0.05)
            self.assertEqual(self._owned_registry.pending(), 0)
            self._owned_registry.stop()
            self.assertFalse(
                self._owned_registry._thread is not None
                and self._owned_registry._thread.is_alive()
            )
            self.assertFalse(
                self.control_root.exists() and any(self.control_root.iterdir())
            )
        finally:
            self._owned_registry.stop()
            registry_was_owned = False
            try:
                with supervisor._REGISTRY_LOCK:
                    registry_was_owned = supervisor._REGISTRY is self._owned_registry
                    if registry_was_owned:
                        supervisor._REGISTRY = self._prior_registry
            finally:
                self._control_root_patch.stop()
                self._tmpdir.cleanup()
            self.assertTrue(registry_was_owned)
        self.assertIs(supervisor._REGISTRY, self._prior_registry)

    def test_onboard_fake_worker_dispatch_replies_and_closes(self) -> None:
        actor_id = "alpha-onboard-fake-worker"
        result = onboard_worker(
            self.store,
            team="alpha",
            runtime="fake",
            actor_id=actor_id,
            owner=ARCHITECT_ID,
            worktree_root=str(self.worktree_root),
            repo_root=self.repo,
        )
        self.assertEqual(result["project_root"], str((self.worktree_root / f"agent-comms-{actor_id}").resolve()))
        self.assertIsNone(result["codex_home"])

        adapter = FakeAdapter()
        with mock.patch.object(paths, "db_path", return_value=self.store.db_path):
            dispatch = self.store.dispatch_agent(
                ARCHITECT_ID,
                actor_id,
                "onboard-fake-dispatch",
                "ping",
                "Reply with PONG.",
                [],
                adapter_for_runtime=lambda _runtime: adapter,
                ttl_seconds=2,
            )

            deadline = time.monotonic() + 8
            while dispatch["status"] == "in_flight" and time.monotonic() < deadline:
                time.sleep(0.1)
                self.store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)
                dispatch = self.store._dispatch_by_idempotency_key_fresh(ARCHITECT_ID, "onboard-fake-dispatch")
        control_socket = Path(dispatch["observed_values"]["control_socket"])
        self.assertEqual(control_socket.parent.parent, self.control_root)
        self.assertNotEqual(self.control_root, supervisor._DEFAULT_CONTROL_ROOT)
        self.assertNotIn(supervisor._DEFAULT_CONTROL_ROOT, control_socket.parents)
        if dispatch.get("spawn_handle"):
            # Authenticated teardown via the recorded control identity (current
            # API); tolerate an already-gone wrapper for the settled dispatch.
            try:
                adapter.halt(dispatch["spawn_handle"], dispatch.get("observed_values"))
            except Exception:
                pass

        self.assertEqual(dispatch["status"], "closed", dispatch)
        trigger = [
            message
            for message in self.store.list_inbox(actor_id, unread_only=False, include_closed=True)
            if message["id"] == dispatch["message_id"]
        ]
        replies = [
            message
            for message in self.store.list_inbox(ARCHITECT_ID, unread_only=False, include_closed=True)
            if message["parent_message_id"] == dispatch["message_id"]
        ]
        self.assertEqual(len(trigger), 1)
        self.assertEqual(trigger[0]["status"], "closed")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["from"], actor_id)

    def test_duplicate_actor_fails_before_mutation(self) -> None:
        actor_id = "alpha-duplicate-worker"
        self.store.register_agent_actor(
            actor_id,
            "alpha",
            "worker",
            str(self.repo),
            [],
            runtime="fake",
            spawn={},
            owner=ARCHITECT_ID,
        )

        with self.assertRaisesRegex(ValidationError, "actor already exists"):
            onboard_worker(
                self.store,
                team="alpha",
                runtime="fake",
                actor_id=actor_id,
                owner=ARCHITECT_ID,
                worktree_root=str(self.worktree_root),
                repo_root=self.repo,
            )

        self.assertFalse((self.worktree_root / f"agent-comms-{actor_id}").exists())

    def test_existing_worktree_path_fails_before_mutation(self) -> None:
        actor_id = "alpha-existing-path-worker"
        worktree = self.worktree_root / f"agent-comms-{actor_id}"
        worktree.mkdir()

        with self.assertRaisesRegex(ValidationError, "target worktree path already exists"):
            onboard_worker(
                self.store,
                team="alpha",
                runtime="fake",
                actor_id=actor_id,
                owner=ARCHITECT_ID,
                worktree_root=str(self.worktree_root),
                repo_root=self.repo,
            )

        self.assertNotIn(actor_id, {actor["id"] for actor in self.store.list_actors()})
        branch_exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/worker/{actor_id}"],
            cwd=self.repo,
            check=False,
        ).returncode == 0
        self.assertFalse(branch_exists)

    def test_existing_branch_fails_before_mutation(self) -> None:
        actor_id = "alpha-existing-branch-worker"
        _git(self.repo, "branch", f"worker/{actor_id}")
        worktree = self.worktree_root / f"agent-comms-{actor_id}"

        with self.assertRaisesRegex(ValidationError, "branch already exists"):
            onboard_worker(
                self.store,
                team="alpha",
                runtime="fake",
                actor_id=actor_id,
                owner=ARCHITECT_ID,
                worktree_root=str(self.worktree_root),
                repo_root=self.repo,
            )

        self.assertNotIn(actor_id, {actor["id"] for actor in self.store.list_actors()})
        self.assertFalse(worktree.exists())

    def test_unknown_runtime_fails_before_mutation(self) -> None:
        actor_id = "alpha-unknown-runtime-worker"
        worktree = self.worktree_root / f"agent-comms-{actor_id}"

        with self.assertRaisesRegex(ValidationError, "unsupported runtime 'bogus'"):
            onboard_worker(
                self.store,
                team="alpha",
                runtime="bogus",
                actor_id=actor_id,
                owner=ARCHITECT_ID,
                worktree_root=str(self.worktree_root),
                repo_root=self.repo,
            )

        _assert_no_onboard_mutation(self, self.store, actor_id, worktree, self.repo)


if __name__ == "__main__":
    unittest.main()
