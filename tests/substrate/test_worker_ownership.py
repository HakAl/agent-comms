import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_comms.actors import ActorRegistry
from agent_comms.db import Database
from agent_comms.schema import ValidationError
from agent_comms.store import Store


class WorkerOwnershipTest(unittest.TestCase):
    def make_store(self, root: Path) -> Store:
        store = Store(root / "agent-comms.sqlite")
        store.register_agent_actor("alpha-architect", "alpha", "architect", str(root), [])
        store.register_agent_actor("beta-architect", "beta", "architect", str(root), [])
        store.register_agent_actor(
            "alpha-worker", "alpha", "worker", str(root), [], owner="alpha-architect"
        )
        return store

    def make_legacy_actors(self, path: Path, rows: list[tuple[str, str, str, str]]) -> Database:
        database = Database(path)
        with database.connection() as conn:
            conn.execute(
                """
                create table actors(
                  id text primary key, kind text not null, display_name text not null,
                  system_class text, system_instance text, project_root text, runtime text,
                  spawn_json text, capabilities_json text not null default '[]', team text,
                  role text, last_seen_at text not null, dispatch_cap integer not null default 4,
                  protected integer not null default 0
                )
                """
            )
            conn.executemany(
                "insert into actors(id, kind, display_name, team, role, last_seen_at) values(?, 'agent', ?, ?, ?, 'now')",
                rows,
            )
        return database

    def test_schema_check_and_owner_delete_restriction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))
            with store.connection() as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("update actors set owner_actor_id = null where id = 'alpha-worker'")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("update actors set owner_actor_id = 'alpha-architect' where id = 'beta-architect'")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("delete from actors where id = 'alpha-architect'")

    def test_backfill_resolves_sole_architect_and_accepts_cross_team_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = self.make_legacy_actors(
                Path(temp_dir) / "legacy.sqlite",
                [
                    ("alpha-architect", "Alpha", "alpha", "architect"),
                    ("beta-architect-one", "Beta one", "beta", "architect"),
                    ("beta-architect-two", "Beta two", "beta", "architect"),
                    ("alpha-worker", "Alpha worker", "alpha", "worker"),
                    ("beta-worker", "Beta worker", "beta", "worker"),
                    ("orphan-worker", "Orphan worker", "orphan", "worker"),
                ],
            )
            with database.connection() as conn:
                database._migrate_worker_ownership(
                    conn,
                    {"beta-worker": "alpha-architect", "orphan-worker": "beta-architect-one"},
                )
                owners = dict(conn.execute(
                    "select id, owner_actor_id from actors where role = 'worker' order by id"
                ).fetchall())
            self.assertEqual(
                owners,
                {
                    "alpha-worker": "alpha-architect",
                    "beta-worker": "alpha-architect",
                    "orphan-worker": "beta-architect-one",
                },
            )

    def test_backfill_aborts_without_partial_write_for_invalid_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = self.make_legacy_actors(
                Path(temp_dir) / "legacy.sqlite",
                [
                    ("architect", "Architect", "team", "architect"),
                    ("worker-one", "Worker one", "none", "worker"),
                    ("worker-two", "Worker two", "none", "worker"),
                ],
            )
            with database.connection() as conn:
                before = "\n".join(conn.iterdump())
                with self.assertRaises(ValidationError):
                    database._migrate_worker_ownership(
                        conn, {"worker-one": "architect", "worker-two": "missing"}
                    )
                after = "\n".join(conn.iterdump())
            self.assertEqual(after, before)

    def test_backfill_rejects_existing_non_architect_declared_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = self.make_legacy_actors(
                Path(temp_dir) / "legacy.sqlite",
                [
                    ("worker-one", "Worker one", "team", "worker"),
                    ("non-architect", "Non-architect", "other", "reviewer"),
                ],
            )
            with database.connection() as conn:
                before = "\n".join(conn.iterdump())
                with self.assertRaisesRegex(
                    ValidationError, "requires an explicit architect declaration"
                ):
                    database._migrate_worker_ownership(
                        conn,
                        {"worker-one": "non-architect"},
                    )
                after = "\n".join(conn.iterdump())
            self.assertEqual(after, before)

    def test_actor_registry_enforces_worker_owner_contract_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "agent-comms.sqlite")
            registry = ActorRegistry(database)
            registry.register_agent_actor(
                "alpha-architect", "alpha", "architect", str(root), []
            )
            registry.register_agent_actor(
                "beta-architect", "beta", "architect", str(root), []
            )
            registry.register_agent_actor(
                "alpha-worker",
                "alpha",
                "worker",
                str(root),
                [],
                owner="alpha-architect",
            )

            with self.assertRaisesRegex(ValidationError, "worker actors require owner"):
                registry.register_agent_actor(
                    "missing-owner-worker", "alpha", "worker", str(root), []
                )
            with self.assertRaisesRegex(
                ValidationError, "worker owner must name an agent architect"
            ):
                registry.register_agent_actor(
                    "worker-owned-worker",
                    "alpha",
                    "worker",
                    str(root),
                    [],
                    owner="alpha-worker",
                )

            registry.register_agent_actor(
                "cross-team-worker",
                "alpha",
                "worker",
                str(root),
                [],
                owner="beta-architect",
            )
            with database.connection() as conn:
                owner = conn.execute(
                    "select owner_actor_id from actors where id = 'cross-team-worker'"
                ).fetchone()[0]
            self.assertEqual(owner, "beta-architect")

            with self.assertRaisesRegex(
                ValidationError, "owner is only valid for worker actors"
            ):
                registry.register_agent_actor(
                    "owned-architect",
                    "alpha",
                    "architect",
                    str(root),
                    [],
                    owner="alpha-architect",
                )

    def test_registration_requires_valid_owner_and_persists_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            with self.assertRaises(ValidationError):
                store.register_agent_actor("bad-worker", "alpha", "worker", str(root), [])
            with self.assertRaises(ValidationError):
                store.register_agent_actor(
                    "bad-worker", "alpha", "worker", str(root), [], owner="alpha-worker"
                )
            store.register_agent_actor(
                "good-worker", "beta", "worker", str(root), [], owner="alpha-architect"
            )
            with store.connection() as conn:
                owner = conn.execute(
                    "select owner_actor_id from actors where id = 'good-worker'"
                ).fetchone()[0]
            self.assertEqual(owner, "alpha-architect")

    def test_transfer_moves_owner_and_rejects_non_architect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))
            store.transfer_worker("alpha-worker", "beta-architect")
            with store.connection() as conn:
                owner = conn.execute(
                    "select owner_actor_id from actors where id = 'alpha-worker'"
                ).fetchone()[0]
            self.assertEqual(owner, "beta-architect")
            with self.assertRaises(ValidationError):
                store.transfer_worker("alpha-worker", "alpha-worker")

    def test_whoami_reports_owned_workers_only_for_architects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))
            self.assertEqual(store.whoami("alpha-architect")["owned_worker_ids"], ["alpha-worker"])
            self.assertNotIn("owned_worker_ids", store.whoami("alpha-worker"))

    def test_authorization_follows_ownership_not_team(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.register_agent_actor(
                "beta-worker", "beta", "worker", str(root), [], owner="alpha-architect"
            )
            accepted = store.dispatch_agent(
                "alpha-architect", "beta-worker", "cross-team", "Work", "Do work", []
            )
            self.assertEqual(accepted["recipient_actor_id"], "beta-worker")
            with self.assertRaisesRegex(ValidationError, "worker it owns"):
                store.dispatch_agent(
                    "beta-architect", "beta-worker", "team-mate", "Work", "Do work", []
                )


if __name__ == "__main__":
    unittest.main()
