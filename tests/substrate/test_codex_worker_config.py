"""A Codex worker's home holds trust only (ac-4ao.10 slice 3b).

The worker's ``agent-comms`` server is bound per run with ``-c``, and the
spawn passes the sandbox and approval policy as flags, so the generated home
holds no ``mcp_servers`` and no per-actor profile, and the spawn passes no
``--profile``.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_comms import codex_home, provisioning
from agent_comms.schema import ValidationError
from agent_comms.spawn import render_spawn, rerender_spawn_block

WORKER = "alpha-codex-worker"


def _fresh() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")


class WorkerHomeConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ac-3b-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.project = self.tmp / "project"
        self.project.mkdir()

    def test_base_config_holds_trust_and_no_mcp_servers(self) -> None:
        config = tomllib.loads(provisioning.render_codex_base_config(str(self.project)))
        self.assertNotIn("mcp_servers", config)
        self.assertEqual(config["projects"][str(self.project)], {"trust_level": "trusted"})

    def test_write_codex_home_writes_no_profile(self) -> None:
        home = self.tmp / "home"
        written = provisioning.write_codex_home(home, WORKER, str(self.project))
        self.assertEqual(written, [home / "config.toml"])
        self.assertEqual(sorted(p.name for p in home.iterdir()), ["config.toml"])

    def test_write_codex_home_leaves_an_old_profile_alone(self) -> None:
        home = self.tmp / "home"
        home.mkdir()
        old = home / f"{WORKER}.config.toml"
        old.write_text('sandbox_mode = "workspace-write"\n')
        provisioning.write_codex_home(home, WORKER, str(self.project))
        self.assertEqual(old.read_text(), 'sandbox_mode = "workspace-write"\n')

    def _home(self, *, profile: bool) -> Path:
        home = self.tmp / "home"
        provisioning.write_codex_home(home, WORKER, str(self.project))
        if profile:
            (home / f"{WORKER}.config.toml").write_text("")
        (home / "auth.json").write_text(json.dumps({"last_refresh": _fresh()}))
        return home

    def test_preflight_passes_a_home_without_a_profile(self) -> None:
        home = self._home(profile=False)
        self.assertEqual(codex_home.preflight_home(home, actor_id=WORKER), (True, None))
        self.assertEqual(codex_home.preflight_home(home), (True, None))

    def test_preflight_passes_a_home_with_an_old_profile(self) -> None:
        home = self._home(profile=True)
        self.assertEqual(codex_home.preflight_home(home, actor_id=WORKER), (True, None))


class SpawnTemplateTest(unittest.TestCase):
    def test_codex_spawn_passes_no_profile(self) -> None:
        args = render_spawn("codex", WORKER, codex_home="/h")["args"]
        self.assertNotIn("--profile", args)
        self.assertNotIn("-p", args)
        self.assertNotIn("{actor_id}", args)


class RerenderRetiredProfileTest(unittest.TestCase):
    def _actor(self, args: list[str]) -> dict:
        return {
            "id": WORKER,
            "runtime": "codex",
            "spawn": {"command": "{codex_binary}", "args": args, "env": {"CODEX_HOME": "/h"}},
        }

    def _current(self) -> list[str]:
        return render_spawn("codex", WORKER, codex_home="/h")["args"]

    def _with_profile(self, value: str, *, json_flag: bool = True) -> list[str]:
        args = self._current()
        args.insert(args.index("--skip-git-repo-check"), "--profile")
        args.insert(args.index("--skip-git-repo-check"), value)
        if not json_flag:
            args.remove("--json")
        return args

    def test_stored_placeholder_profile_pair_migrates(self) -> None:
        status, spawn = rerender_spawn_block(self._actor(self._with_profile("{actor_id}")))
        self.assertEqual(status, "update")
        self.assertNotIn("--profile", spawn["args"])
        self.assertEqual(spawn["args"][-1], "{worker_prompt}")
        self.assertEqual(spawn["env"], {"CODEX_HOME": "/h"})

    def test_stored_concrete_profile_pair_migrates(self) -> None:
        status, spawn = rerender_spawn_block(self._actor(self._with_profile(WORKER)))
        self.assertEqual(status, "update")
        self.assertNotIn("--profile", spawn["args"])

    def test_retired_and_introduced_migrate_together(self) -> None:
        status, spawn = rerender_spawn_block(self._actor(self._with_profile("{actor_id}", json_flag=False)))
        self.assertEqual(status, "update")
        self.assertIn("--json", spawn["args"])
        self.assertNotIn("--profile", spawn["args"])

    def test_a_profile_naming_another_actor_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            rerender_spawn_block(self._actor(self._with_profile("other-actor")))

    def test_a_repeated_profile_pair_is_refused(self) -> None:
        # Lookout 3b: only the one pair the old template rendered is retired.
        args = self._with_profile("{actor_id}")
        at = args.index("--profile")
        args[at:at] = ["--profile", "{actor_id}"]
        with self.assertRaises(ValidationError):
            rerender_spawn_block(self._actor(args))

    def test_another_difference_is_still_refused(self) -> None:
        args = self._with_profile("{actor_id}")
        args.insert(0, "--extra")
        with self.assertRaises(ValidationError):
            rerender_spawn_block(self._actor(args))

    def test_current_template_is_a_no_op(self) -> None:
        args = self._current()
        status, _ = rerender_spawn_block(self._actor(args))
        self.assertEqual(status, "no-op")


if __name__ == "__main__":
    unittest.main()
