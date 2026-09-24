"""Regression tests for the runtime-cell Git fixture."""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests.dispatch_cell_harness as harness


FIXTURE_NAME = "Agent Comms Cell Fixture"
FIXTURE_EMAIL = "cell-fixture@agent-comms.invalid"


class WorkerGitRootProvisioningTest(unittest.TestCase):
    def test_provisioning_ignores_inherited_git_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            project_root = temp_root / "project"
            decoy_git_dir = temp_root / "decoy.git"
            decoy_git_dir.mkdir()
            decoy_index = temp_root / "decoy.index"
            polluted_env = {
                "GIT_AUTHOR_NAME": "Polluted Author",
                "GIT_AUTHOR_EMAIL": "polluted-author@example.invalid",
                "GIT_COMMITTER_NAME": "Polluted Committer",
                "GIT_COMMITTER_EMAIL": "polluted-committer@example.invalid",
                "GIT_DIR": str(decoy_git_dir),
                "GIT_WORK_TREE": str(project_root),
                "GIT_INDEX_FILE": str(decoy_index),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "user.name",
                "GIT_CONFIG_VALUE_0": "Polluted Config Identity",
            }

            with patch.dict(os.environ, polluted_env):
                harness.provision_worker_git_root(project_root)

            clean_git_env = {
                key: value for key, value in os.environ.items() if not key.startswith("GIT_")
            }
            self.assertTrue((project_root / ".git").is_dir())
            resolved_git_dir = subprocess.run(
                ["git", "rev-parse", "--absolute-git-dir"],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
                env=clean_git_env,
            ).stdout.strip()
            self.assertEqual(Path(resolved_git_dir).resolve(), (project_root / ".git").resolve())
            metadata = subprocess.run(
                [
                    "git",
                    "show",
                    "-s",
                    "--format=%an%n%ae%n%cn%n%ce",
                    "HEAD",
                ],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
                env=clean_git_env,
            ).stdout.splitlines()

            self.assertEqual(
                metadata,
                [FIXTURE_NAME, FIXTURE_EMAIL, FIXTURE_NAME, FIXTURE_EMAIL],
            )
            self.assertEqual(list(decoy_git_dir.iterdir()), [])
            self.assertFalse(decoy_index.exists())


if __name__ == "__main__":
    unittest.main()
