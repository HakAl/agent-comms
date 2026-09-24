from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import cli, codex_home, codex_refresh_driver
from agent_comms.schema import ValidationError
from agent_comms.spawn import render_spawn
from agent_comms.store import Store


class CustodyActorScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = Store(self.root / "agent-comms.sqlite")
        self.store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")
        self.store.register_agent_actor(
            "alpha-architect", "alpha", "architect", str(self.root / "architect"), []
        )

    def _actor(self, actor_id: str, raw_spawn: str) -> None:
        spawn = render_spawn("codex", actor_id)
        spawn["env"]["CODEX_HOME"] = str(self.root / actor_id)
        self.store.register_agent_actor(
            actor_id,
            "alpha",
            "worker",
            str(self.root / actor_id),
            [],
            runtime="codex",
            spawn=spawn,
            owner="alpha-architect",
        )
        with self.store._db.connection() as conn:
            conn.execute(
                "update actors set spawn_json = ? where id = ?", (raw_spawn, actor_id)
            )

    def test_each_code_precedence_order_and_healthy_equivalence(self) -> None:
        self._actor("c-home", json.dumps({"env": {}}))
        self._actor("a-json", "{")
        self._actor("b-env", json.dumps({"env": []}))
        healthy_spawn = {"env": {"CODEX_HOME": str(self.root / "healthy")}}
        self._actor("d-healthy", json.dumps(healthy_spawn))
        with self.store._db.connection() as conn:
            healthy, defects = codex_home._scan_codex_actors(conn)
        self.assertEqual(
            [defect["actor_id"] for defect in defects],
            ["a-json", "b-env", "c-home"],
        )
        self.assertEqual(
            [defect["code"] for defect in defects],
            ["malformed_spawn_json", "malformed_spawn_env", "missing_codex_home"],
        )
        self.assertTrue(
            all(set(d) == {"actor_id", "code", "scope", "context"} for d in defects)
        )
        self.assertTrue(
            all(d["scope"] == "isolated" and d["context"] == {} for d in defects)
        )
        with self.store._db.connection() as conn:
            conn.execute("delete from actors where id in ('a-json', 'b-env', 'c-home')")
            strict = codex_home._codex_actor_rows(conn)
        self.assertEqual(healthy, strict)

    def test_strict_wrapper_preserves_exact_per_code_refusals(self) -> None:
        cases = (
            (
                "malformed-json",
                "{",
                "refusing: registered Codex actor malformed-json has malformed spawn_json",
                False,
            ),
            (
                "malformed-env",
                json.dumps({"env": []}),
                "refusing: registered Codex actor malformed-env has a malformed spawn env",
                False,
            ),
            (
                "missing-home",
                json.dumps({"env": {}}),
                "refusing: registered Codex actor missing-home has no "
                "spawn.env.CODEX_HOME; resolve or deregister it first",
                True,
            ),
        )
        clause = "resolve or deregister it first"
        for actor_id, raw_spawn, expected, has_resolution_clause in cases:
            with self.subTest(actor_id=actor_id):
                self._actor(actor_id, raw_spawn)
                with self.store._db.connection() as conn:
                    with self.assertRaises(ValidationError) as raised:
                        codex_home._codex_actor_rows(conn)
                    conn.execute("delete from actors where id = ?", (actor_id,))
                self.assertEqual(str(raised.exception), expected)
                self.assertEqual(clause in str(raised.exception), has_resolution_clause)

    def test_strict_wrapper_refuses_lowest_id_among_multiple_defects(self) -> None:
        self._actor("c-malformed-json", "{")
        self._actor("a-malformed-env", json.dumps({"env": []}))
        self._actor("b-missing-home", json.dumps({"env": {}}))

        with self.store._db.connection() as conn:
            with self.assertRaises(ValidationError) as raised:
                codex_home._codex_actor_rows(conn)

        self.assertEqual(
            str(raised.exception),
            "refusing: registered Codex actor a-malformed-env has a malformed "
            "spawn env",
        )

    def test_strict_wrapper_skips_healthy_lowest_id_before_defects(self) -> None:
        healthy_spawn = {"env": {"CODEX_HOME": str(self.root / "healthy")}}
        self._actor("a-healthy", json.dumps(healthy_spawn))
        self._actor("b-missing-home", json.dumps({"env": {}}))
        self._actor("c-malformed-json", "{")
        self._actor("d-malformed-env", json.dumps({"env": []}))

        with self.store._db.connection() as conn:
            with self.assertRaises(ValidationError) as raised:
                codex_home._codex_actor_rows(conn)

        self.assertEqual(
            str(raised.exception),
            "refusing: registered Codex actor b-missing-home has no "
            "spawn.env.CODEX_HOME; resolve or deregister it first",
        )

    def test_auth_report_degrades_but_refresh_stays_strict(self) -> None:
        self._actor("bad", json.dumps({"env": {}}))
        report = codex_refresh_driver.auth_targets_report(self.store)
        self.assertEqual(
            set(report),
            {
                "actors",
                "lineages",
                "unresolved_actors",
                "actor_defects",
                "refresh_blocked",
            },
        )
        self.assertIs(report["refresh_blocked"], True)
        with mock.patch.object(
            codex_refresh_driver, "refresh_if_due"
        ) as refresh_if_due:
            with self.assertRaises(ValidationError):
                codex_refresh_driver.refresh(self.store)
        refresh_if_due.assert_not_called()

    def test_auth_targets_cli_uses_exit_three_and_one_json_document(self) -> None:
        self._actor("bad", json.dumps({"env": {}}))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            rc = cli.run(["--db", str(self.store._db.db_path), "codex-auth-targets"])
        self.assertEqual(rc, 3)
        report = json.loads(output.getvalue())
        self.assertEqual(report["actor_defects"][0]["code"], "missing_codex_home")
        self.assertTrue(report["refresh_blocked"])



if __name__ == "__main__":
    unittest.main()
