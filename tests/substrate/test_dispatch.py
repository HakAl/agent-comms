import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import contextlib
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.cli import ADMIN_CREDENTIAL_ERROR
from agent_comms.dispatch_ledger import ConcurrencyError
from agent_comms.policies import compile_policy, scoped_env
from agent_comms.runtime_pins import CLAUDE_PINNED_SHA256_ENV, CLAUDE_PINNED_VERSION, CLAUDE_VERSIONS_DIR_ENV
from agent_comms.schema import ValidationError
from agent_comms.store import Store, WORKER_DISPATCH_POLICY


def seed_dispatch_actors(store: Store, root: Path) -> None:
    store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
    store.register_agent_actor("alpha-worker", "alpha", "worker", str(root / "alpha-worker"), [], owner="alpha-architect")
    store.register_agent_actor("echo-architect", "echo", "architect", str(root / "echo-architect"), [])
    store.register_agent_actor(
        "echo-worker", "echo", "worker", str(root / "echo-worker"), [],
        owner="echo-architect",
    )


def operator_env(root: Path, token: str = "operator-secret") -> dict[str, str]:
    home = root / "home"
    secret_dir = home / ".agent-comms"
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret_file = secret_dir / "admin-token"
    secret_file.write_text(token)
    secret_file.chmod(0o600)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["AGENT_COMMS_ADMIN_TOKEN"] = token
    env["PYTHONPYCACHEPREFIX"] = "/private/tmp/agent-comms-pycache"
    return env


def write_claude_pin_stub(root: Path) -> Path:
    versions_dir = root / "claude-versions"
    binary = versions_dir / CLAUDE_PINNED_VERSION
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        f"  printf 'Claude Code {CLAUDE_PINNED_VERSION}\\n'\n"
        "  exit 0\n"
        "fi\n"
        # Non-version invocation: be a self-contained bounded long-running child.
        # The Claude adapter renders runtime flags/prompt around the spawn args,
        # so `exec \"$@\"` would try to exec a flag/invalid runtime invocation.
        # A fixed bounded sleep keeps the supervised child alive across the spawn
        # commit (dispatch settles in_flight) and is torn down at the dispatch TTL.
        "exec sleep 30\n"
    )
    binary.chmod(0o755)
    return versions_dir


def claude_pin_stub_sha256(versions_dir: Path) -> str:
    return hashlib.sha256((versions_dir / CLAUDE_PINNED_VERSION).read_bytes()).hexdigest()


class DispatchTest(unittest.TestCase):
    def test_architect_can_dispatch_own_team_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "idem-1",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )

            self.assertEqual(dispatch["status"], "queued")
            self.assertEqual(dispatch["recipient_actor_id"], "alpha-worker")
            self.assertEqual(dispatch["producer_actor_id"], "alpha-architect")
            self.assertEqual(dispatch["policy_name"], WORKER_DISPATCH_POLICY)
            self.assertEqual(dispatch["message_id"], dispatch["thread_ref"])
            inbox = store.list_inbox("alpha-worker")
            self.assertEqual([message["id"] for message in inbox], [dispatch["message_id"]])

    def test_dispatch_idempotency_replays_existing_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            first = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "idem-replay",
                "Measure signal",
                "First body.",
                [],
            )
            second = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "idem-replay",
                "Different subject ignored",
                "Different body ignored.",
                [],
            )

            self.assertEqual(second["dispatch_id"], first["dispatch_id"])
            self.assertEqual(second["message_id"], first["message_id"])
            self.assertEqual(len(store.list_inbox("alpha-worker")), 1)

    def test_idempotency_key_is_per_producer_not_global(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            alpha_dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "shared-key",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            echo_dispatch = store.dispatch_agent(
                "echo-architect",
                "echo-worker",
                "shared-key",
                "Route signal",
                "Run the bounded worker task.",
                [],
            )

            self.assertNotEqual(alpha_dispatch["dispatch_id"], echo_dispatch["dispatch_id"])
            self.assertNotEqual(alpha_dispatch["message_id"], echo_dispatch["message_id"])
            self.assertEqual(alpha_dispatch["producer_actor_id"], "alpha-architect")
            self.assertEqual(echo_dispatch["producer_actor_id"], "echo-architect")
            self.assertEqual(len(store.list_inbox("alpha-worker")), 1)
            self.assertEqual(len(store.list_inbox("echo-worker")), 1)

    def test_same_producer_same_key_same_target_replays(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            first = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "replay-key",
                "Measure signal",
                "First body.",
                [],
            )
            second = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "replay-key",
                "Different subject ignored",
                "Different body ignored.",
                [],
            )

            self.assertEqual(second["dispatch_id"], first["dispatch_id"])
            self.assertEqual(second["message_id"], first["message_id"])
            self.assertEqual(len(store.list_inbox("alpha-worker")), 1)

    def test_same_producer_key_reuse_different_target_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            store.register_agent_actor("alpha-worker-2", "alpha", "worker", str(root / "alpha-worker-2"), [], owner="alpha-architect")

            store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "dup-key",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )

            with self.assertRaisesRegex(ValidationError, "different target"):
                store.dispatch_agent(
                    "alpha-architect",
                    "alpha-worker-2",
                    "dup-key",
                    "Measure signal",
                    "Run the bounded worker task.",
                    [],
                )

    def test_composite_unique_constraint_rejects_true_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            store._db.init()

            with store.connection() as conn:
                base_values = {
                    "parent_dispatch_id": None,
                    "idempotency_key": "sql-dup-key",
                    "message_id": None,
                    "thread_ref": "sql-thread",
                    "spawn_handle": None,
                    "recipient_actor_id": "alpha-worker",
                    "originating_actor_id": "alpha-architect",
                    "policy_name": WORKER_DISPATCH_POLICY,
                    "policy_version": "v1",
                    "policy_issued_by": "alpha-architect",
                    "expected_close_by": None,
                    "status": "queued",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "spawned_at": None,
                    "closed_at": None,
                    "dlq_at": None,
                    "override_reason": None,
                    "failure_reason": None,
                    "observed_values_json": "{}",
                }

                def insert_row(dispatch_id: str, producer_actor_id: str) -> None:
                    values = dict(base_values, dispatch_id=dispatch_id, producer_actor_id=producer_actor_id)
                    conn.execute(
                        """
                        insert into dispatch_ledger(
                          dispatch_id, parent_dispatch_id, idempotency_key, message_id,
                          thread_ref, spawn_handle, recipient_actor_id, producer_actor_id,
                          originating_actor_id, policy_name, policy_version, policy_issued_by,
                          expected_close_by, status, created_at, spawned_at, closed_at,
                          dlq_at, override_reason, failure_reason, observed_values_json
                        )
                        values(
                          :dispatch_id, :parent_dispatch_id, :idempotency_key, :message_id,
                          :thread_ref, :spawn_handle, :recipient_actor_id, :producer_actor_id,
                          :originating_actor_id, :policy_name, :policy_version, :policy_issued_by,
                          :expected_close_by, :status, :created_at, :spawned_at, :closed_at,
                          :dlq_at, :override_reason, :failure_reason, :observed_values_json
                        )
                        """,
                        values,
                    )

                insert_row("dispatch_sql_dup_1", "alpha-architect")
                with self.assertRaises(sqlite3.IntegrityError):
                    insert_row("dispatch_sql_dup_2", "alpha-architect")
                insert_row("dispatch_sql_dup_3", "echo-architect")

    def test_migration_rebuild_preserves_existing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            first = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "migrate-key",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "migrate-second",
                "Measure signal",
                "Run another bounded worker task.",
                [],
            )
            with store.connection() as conn:
                pre_count = conn.execute("select count(*) from dispatch_ledger").fetchone()[0]

            store._db.init()

            with store.connection() as conn:
                post_count = conn.execute("select count(*) from dispatch_ledger").fetchone()[0]
                rows = conn.execute(
                    """
                    select producer_actor_id, idempotency_key
                    from dispatch_ledger
                    order by idempotency_key
                    """
                ).fetchall()
                fk_rows = conn.execute("pragma foreign_key_check").fetchall()
                unique_columns = []
                for index_row in conn.execute("pragma index_list(dispatch_ledger)").fetchall():
                    if not index_row["unique"]:
                        continue
                    unique_columns.append(
                        [
                            column_row["name"]
                            for column_row in conn.execute(f"pragma index_info({index_row['name']})").fetchall()
                        ]
                    )

            self.assertEqual(post_count, pre_count)
            self.assertIn(("alpha-architect", "migrate-key"), [tuple(row) for row in rows])
            self.assertIn(("alpha-architect", "migrate-second"), [tuple(row) for row in rows])
            self.assertEqual(fk_rows, [])
            self.assertIn(["producer_actor_id", "idempotency_key"], unique_columns)
            self.assertIn(["message_id"], unique_columns)
            self.assertNotIn(["idempotency_key"], unique_columns)

            second_producer = store.dispatch_agent(
                "echo-architect",
                "echo-worker",
                first["idempotency_key"],
                "Route signal",
                "Run the bounded worker task.",
                [],
            )
            self.assertEqual(second_producer["producer_actor_id"], "echo-architect")

    def test_cross_team_and_non_worker_targets_reject(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            with self.assertRaisesRegex(ValidationError, "worker it owns"):
                store.dispatch_agent("alpha-architect", "echo-worker", "cross-team", "No", "No", [])
            with self.assertRaisesRegex(ValidationError, "must be a worker"):
                store.dispatch_agent("alpha-architect", "echo-architect", "architect-target", "No", "No", [])
            with self.assertRaisesRegex(ValidationError, "producer is not allowed"):
                store.dispatch_agent("alpha-worker", "alpha-worker", "worker-producer", "No", "No", [])

    def test_operator_override_requires_reason_and_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            with self.assertRaisesRegex(ValidationError, "override_reason"):
                store.dispatch_agent(
                    "01M36YTJV9XBW95S6ZWV47C4RG",
                    "echo-worker",
                    "operator-no-reason",
                    "Incident",
                    "Override without reason.",
                    [],
                )
            with self.assertRaisesRegex(ValidationError, "unknown or unavailable policy"):
                store.dispatch_agent(
                    "01M36YTJV9XBW95S6ZWV47C4RG",
                    "echo-worker",
                    "operator-bad-policy",
                    "Incident",
                    "Override with bad policy.",
                    [],
                    requested_policy="not_a_policy",
                    override_reason="incident response",
                )

            dispatch = store.dispatch_agent(
                "01M36YTJV9XBW95S6ZWV47C4RG",
                "echo-worker",
                "operator-ok",
                "Incident",
                "Override with reason.",
                [],
                requested_policy=WORKER_DISPATCH_POLICY,
                override_reason="incident response",
            )

            self.assertEqual(dispatch["producer_actor_id"], "01M36YTJV9XBW95S6ZWV47C4RG")
            self.assertEqual(dispatch["recipient_actor_id"], "echo-worker")
            self.assertEqual(dispatch["override_reason"], "incident response")
            self.assertIsNone(dispatch["failure_reason"])

    def test_operator_override_can_target_agent_architect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            dispatch = store.dispatch_agent(
                "01M36YTJV9XBW95S6ZWV47C4RG",
                "echo-architect",
                "operator-architect-ok",
                "Incident",
                "Override to an architect target.",
                [],
                requested_policy=WORKER_DISPATCH_POLICY,
                override_reason="incident response",
            )

            self.assertEqual(dispatch["status"], "queued")
            self.assertEqual(dispatch["recipient_actor_id"], "echo-architect")

    def test_admin_dispatch_cli_requires_requested_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            store.register_agent_actor(
                "echo-worker",
                "echo",
                "worker",
                str(root / "echo-worker"),
                [],
                runtime="claude",
                spawn={
                    "command": "{claude_binary}",
                    "args": [sys.executable, "-c", "print('started')", f"WakePolicy={WORKER_DISPATCH_POLICY}"],
                },
                owner="echo-architect",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_comms.cli",
                    "--db",
                    str(root / "agent-comms.sqlite"),
                    "admin",
                    "dispatch",
                    "--from-actor-id",
                    "01M36YTJV9XBW95S6ZWV47C4RG",
                    "--target-actor-id",
                    "echo-worker",
                    "--idempotency-key",
                    "cli-missing-policy",
                    "--override-reason",
                    "incident",
                    "--subject",
                    "Incident",
                    "--body",
                    "Body.",
                ],
                env=operator_env(root),
                text=True,
                capture_output=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--requested-policy", result.stderr)

    def test_admin_dispatch_cli_writes_ledger(self) -> None:
        # Root under a short literal /tmp: the CLI subprocess derives its control
        # root from HOME (<root>/home/.agent-comms/run/s), and the bound socket is
        # <control_root>/<32-hex>/s. The macOS default TMPDIR (/var/folders/...) is
        # long enough that this encoded sun_path exceeds the 103-byte ceiling and
        # bind fails before READY. A short /tmp root keeps the path under the limit
        # while staying auto-cleaned by TemporaryDirectory.
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            store.register_agent_actor(
                "echo-worker",
                "echo",
                "worker",
                str(root / "echo-worker"),
                [],
                runtime="claude",
                spawn={
                    "command": "{claude_binary}",
                    # A long-running child stays alive across the spawn commit so
                    # the supervised dispatch settles in_flight; a fast-exiting
                    # child would (truthfully) become an exit-before-close DLQ
                    # under the supervisor. Bounded by the dispatch TTL.
                    "args": [sys.executable, "-c", "import time; time.sleep(10)", f"WakePolicy={WORKER_DISPATCH_POLICY}"],
                },
                owner="echo-architect",
            )

            env = operator_env(root)
            versions_dir = write_claude_pin_stub(root)
            env[CLAUDE_VERSIONS_DIR_ENV] = str(versions_dir)
            env[CLAUDE_PINNED_SHA256_ENV] = claude_pin_stub_sha256(versions_dir)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_comms.cli",
                    "--db",
                    str(root / "agent-comms.sqlite"),
                    "admin",
                    "dispatch",
                    "--from-actor-id",
                    "01M36YTJV9XBW95S6ZWV47C4RG",
                    "--target-actor-id",
                    "echo-worker",
                    "--idempotency-key",
                    "cli-ok",
                    "--requested-policy",
                    WORKER_DISPATCH_POLICY,
                    "--override-reason",
                    "incident",
                    "--subject",
                    "Incident",
                    "--body",
                    "Body.",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "in_flight")
            self.assertEqual(payload["producer_actor_id"], "01M36YTJV9XBW95S6ZWV47C4RG")

    def test_cli_register_worker_with_runtime_and_spawn_is_dispatchable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            store = Store(db_path)
            store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
            spawn = {
                "command": sys.executable,
                "args": ["-c", "print('started')", f"WakePolicy={WORKER_DISPATCH_POLICY}"],
            }

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_comms.cli",
                    "--db",
                    str(db_path),
                    "register",
                    "alpha-cli-worker",
                    "--team",
                    "alpha",
                    "--role",
                    "worker",
                    "--owner",
                    "alpha-architect",
                    "--project-root",
                    str(root / "alpha-cli-worker"),
                    "--runtime",
                    "fake",
                    "--spawn-json",
                    json.dumps(spawn),
                    "--capability",
                    "signal-read",
                ],
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(result.stdout)
            self.assertEqual(payload["agent_id"], "alpha-cli-worker")
            actors = {actor["id"]: actor for actor in store.list_actors()}
            self.assertEqual(actors["alpha-cli-worker"]["runtime"], "fake")
            self.assertEqual(actors["alpha-cli-worker"]["spawn"], spawn)
            self.assertEqual(actors["alpha-cli-worker"]["capabilities"], ["signal-read"])

            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-cli-worker",
                "cli-register-worker",
                "Measure signal",
                "Run from a CLI-registered worker.",
                [],
            )

            self.assertEqual(dispatch["status"], "queued")
            self.assertEqual(dispatch["recipient_actor_id"], "alpha-cli-worker")

    def test_admin_write_paths_require_operator_credential(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            worker_policy = compile_policy(WORKER_DISPATCH_POLICY)
            worker_env = scoped_env(operator_env(root), worker_policy)
            self.assertNotIn("AGENT_COMMS_ADMIN_TOKEN", worker_env)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_comms.cli",
                    "--db",
                    str(root / "agent-comms.sqlite"),
                    "admin",
                    "send",
                    "--from-actor-id",
                    "alpha-worker",
                    "--to",
                    "alpha-architect",
                    "--subject",
                    "Bypass",
                    "--body",
                    "Should fail.",
                ],
                env=worker_env,
                text=True,
                capture_output=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(ADMIN_CREDENTIAL_ERROR, result.stdout)
            self.assertEqual(store.list_inbox("alpha-architect", unread_only=False), [])

            success = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_comms.cli",
                    "--db",
                    str(root / "agent-comms.sqlite"),
                    "admin",
                    "send",
                    "--from-actor-id",
                    "alpha-worker",
                    "--to",
                    "alpha-architect",
                    "--subject",
                    "Operator send",
                    "--body",
                    "Should succeed.",
                ],
                env=operator_env(root),
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(success.stdout)
            self.assertEqual(payload["from"], "alpha-worker")
            self.assertEqual(len(store.list_inbox("alpha-architect", unread_only=False)), 1)

    def test_dispatch_start_cli_starts_queued_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            store.register_agent_actor(
                "echo-worker",
                "echo",
                "worker",
                str(root / "echo-worker"),
                [],
                runtime="claude",
                spawn={
                    "command": "{claude_binary}",
                    # Long-running child: stays alive across the supervised spawn
                    # commit so dispatch-start settles in_flight (a fast exit would
                    # truthfully become an exit-before-close DLQ). TTL-bounded.
                    "args": [sys.executable, "-c", "import time; time.sleep(10)", f"WakePolicy={WORKER_DISPATCH_POLICY}"],
                },
                owner="echo-architect",
            )
            store.dispatch_agent(
                "01M36YTJV9XBW95S6ZWV47C4RG",
                "echo-worker",
                "cli-start",
                "Incident",
                "Body.",
                [],
                requested_policy=WORKER_DISPATCH_POLICY,
                override_reason="incident",
            )
            versions_dir = write_claude_pin_stub(root)
            env = os.environ.copy()
            env[CLAUDE_VERSIONS_DIR_ENV] = str(versions_dir)
            env[CLAUDE_PINNED_SHA256_ENV] = claude_pin_stub_sha256(versions_dir)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_comms.cli",
                    "--db",
                    str(root / "agent-comms.sqlite"),
                    "dispatch-start",
                    "--ttl-seconds",
                    "5",
                ],
                text=True,
                capture_output=True,
                check=True,
                env=env,
            )

            payload = json.loads(result.stdout)
            self.assertEqual(len(payload["started"]), 1)
            self.assertEqual(payload["started"][0]["status"], "in_flight")
            self.assertTrue(payload["started"][0]["spawn_handle"].startswith("claude:echo-worker:"))


class StubAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.contexts: list[DispatchContext] = []

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        self.contexts.append(context)
        if self.fail:
            raise RuntimeError("adapter spawn failed")
        return DispatchStart(
            spawn_handle=f"fake:{context.recipient['id']}:{context.dispatch['dispatch_id']}",
            observed_values={"adapter": "fake"},
        )

    def halt(self, spawn_handle: str, observed_values=None) -> None:
        return None


class DispatchStartTest(unittest.TestCase):
    def _stamp_v1(self, store: Store, *dispatches: dict) -> None:
        with store.connection() as conn:
            for dispatch in dispatches:
                conn.execute(
                    "update dispatch_ledger set policy_version = 'v1' where dispatch_id = ?",
                    (dispatch["dispatch_id"],),
                )
                policy_version = conn.execute(
                    "select policy_version from dispatch_ledger where dispatch_id = ?",
                    (dispatch["dispatch_id"],),
                ).fetchone()["policy_version"]
                self.assertEqual(policy_version, "v1")

    def test_start_queued_dispatch_marks_in_flight_after_adapter_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "start-ok",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            adapter = StubAdapter()

            started = store.start_queued_dispatches(lambda _runtime: adapter, ttl_seconds=30)

            self.assertEqual(len(started), 1)
            self.assertEqual(started[0]["dispatch_id"], dispatch["dispatch_id"])
            self.assertEqual(started[0]["status"], "in_flight")
            self.assertEqual(started[0]["spawn_handle"], f"fake:alpha-worker:{dispatch['dispatch_id']}")
            self.assertIsNotNone(started[0]["spawned_at"])
            self.assertIsNotNone(started[0]["expected_close_by"])
            self.assertEqual(started[0]["observed_values"], {"adapter": "fake"})
            self.assertEqual(adapter.contexts[0].message["id"], dispatch["message_id"])
            self.assertEqual(adapter.contexts[0].ttl_seconds, 30)

    def test_start_queued_dispatch_records_spawn_failure_without_losing_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "start-fail",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )

            adapter = StubAdapter(fail=True)
            started = store.start_queued_dispatches(lambda _runtime: adapter)

            self.assertEqual(started[0]["dispatch_id"], dispatch["dispatch_id"])
            self.assertEqual(started[0]["status"], "spawn_failed_message_landed")
            self.assertIsNone(started[0]["spawn_handle"])
            self.assertEqual(started[0]["failure_reason"], "adapter spawn failed")
            inbox = store.list_inbox("alpha-worker")
            self.assertEqual([message["id"] for message in inbox], [dispatch["message_id"]])

    def test_retry_spawn_promotes_failed_dispatch_to_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "retry-ok",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            store.start_queued_dispatches(lambda _runtime: StubAdapter(fail=True))

            row = store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: StubAdapter(), ttl_seconds=30)

            self.assertEqual(row["status"], "in_flight")
            self.assertEqual(row["spawn_handle"], f"fake:alpha-worker:{dispatch['dispatch_id']}")
            self.assertIsNone(row["failure_reason"])
            self.assertIsNotNone(row["spawned_at"])
            self.assertIsNotNone(row["expected_close_by"])
            self.assertEqual(row["observed_values"]["retry_count"], 1)
            self.assertEqual(row["observed_values"]["last_retry_outcome"], "in_flight")
            self.assertTrue(row["observed_values"]["last_retry_at"])
            self.assertIn("spawn_failed_at", row["observed_values"])
            self.assertEqual(row["message_id"], dispatch["message_id"])
            inbox = store.list_inbox("alpha-worker")
            self.assertEqual([message["id"] for message in inbox], [dispatch["message_id"]])

    def test_retry_spawn_repeated_failure_stays_failed_and_increments_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "retry-fail",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            store.start_queued_dispatches(lambda _runtime: StubAdapter(fail=True))
            expected_exc = RuntimeError("adapter spawn failed")

            row = store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: StubAdapter(fail=True))

            self.assertEqual(row["status"], "spawn_failed_message_landed")
            self.assertEqual(row["failure_reason"], str(expected_exc))
            self.assertEqual(row["observed_values"]["retry_count"], 1)
            self.assertEqual(row["observed_values"]["last_retry_outcome"], "spawn_failed_message_landed")
            self.assertIsNone(row["spawn_handle"])
            inbox = store.list_inbox("alpha-worker")
            self.assertEqual([message["id"] for message in inbox], [dispatch["message_id"]])

            row = store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: StubAdapter(fail=True))

            self.assertEqual(row["observed_values"]["retry_count"], 2)

    def test_retry_spawn_rejects_when_status_is_not_spawn_failed_message_landed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "retry-bad-status",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )

            with self.assertRaises(ValidationError):
                store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: StubAdapter())
            with store.connection() as conn:
                status = conn.execute(
                    "select status from dispatch_ledger where dispatch_id = ?",
                    (dispatch["dispatch_id"],),
                ).fetchone()["status"]
            self.assertEqual(status, "queued")

            store.start_queued_dispatches(lambda _runtime: StubAdapter())
            with self.assertRaises(ValidationError):
                store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: StubAdapter())

            with store.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set policy_version = 'v1', status = 'closed' where dispatch_id = ?",
                    (dispatch["dispatch_id"],),
                )
            with self.assertRaises(ValidationError):
                store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: StubAdapter())

            with store.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set status = 'dlq' where dispatch_id = ?",
                    (dispatch["dispatch_id"],),
                )
            with self.assertRaises(ValidationError):
                store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: StubAdapter())

    def test_retry_spawn_unknown_dispatch_id_raises_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            with self.assertRaisesRegex(ValidationError, "unknown dispatch_id"):
                store.retry_spawn("dispatch_does_not_exist", lambda _runtime: StubAdapter())

            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "retry-invalid-ttl",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            for ttl_seconds in (0, -1):
                with self.assertRaisesRegex(ValidationError, "ttl_seconds must be at least 1"):
                    store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: StubAdapter(), ttl_seconds=ttl_seconds)

    def test_retry_spawn_concurrent_success_race_detects_orphan_and_halts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "retry-race",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            store.start_queued_dispatches(lambda _runtime: StubAdapter(fail=True))

            class RaceAdapter:
                def __init__(self) -> None:
                    self.halt_calls: list[str] = []

                def dispatch(self, context: DispatchContext) -> DispatchStart:
                    with store.connection() as conn:
                        conn.execute(
                            """
                            update dispatch_ledger
                            set status = 'in_flight',
                                spawn_handle = 'winner-handle',
                                expected_close_by = '2099-01-01T00:00:00+00:00',
                                spawned_at = '2099-01-01T00:00:00+00:00',
                                observed_values_json = ?
                            where dispatch_id = ?
                            """,
                            (
                                json.dumps(
                                    {
                                        "last_retry_outcome": "in_flight",
                                        "retry_count": 1,
                                        "winner_marker": "race-winner",
                                    },
                                    sort_keys=True,
                                ),
                                context.dispatch["dispatch_id"],
                            ),
                        )
                    return DispatchStart(spawn_handle="race-orphan-handle", observed_values={})

                def halt(self, spawn_handle: str, observed_values=None) -> None:
                    self.halt_calls.append(spawn_handle)

            race_adapter = RaceAdapter()

            with self.assertRaises(ConcurrencyError) as raised:
                store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: race_adapter)

            message = str(raised.exception)
            self.assertIn(dispatch["dispatch_id"], message)
            self.assertIn("race-orphan-handle", message)
            self.assertTrue("halted" in message or "halt failed" in message)
            self.assertEqual(race_adapter.halt_calls, ["race-orphan-handle"])
            with store.connection() as conn:
                row = conn.execute(
                    "select * from dispatch_ledger where dispatch_id = ?",
                    (dispatch["dispatch_id"],),
                ).fetchone()
            observed = json.loads(row["observed_values_json"])
            self.assertEqual(row["status"], "in_flight")
            self.assertEqual(row["spawn_handle"], "winner-handle")
            self.assertEqual(row["expected_close_by"], "2099-01-01T00:00:00+00:00")
            self.assertEqual(row["spawned_at"], "2099-01-01T00:00:00+00:00")
            self.assertEqual(observed["winner_marker"], "race-winner")
            self.assertEqual(observed["retry_count"], 1)
            self.assertNotEqual(observed.get("last_retry_outcome"), "spawn_failed_message_landed")

    def test_start_queued_dispatches_empty_queue_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            adapter = StubAdapter()
            self.assertEqual(store.start_queued_dispatches(lambda _runtime: adapter), [])

    def test_close_message_non_dispatch_message_succeeds_without_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            message = store.send_message(
                "alpha-architect",
                ["echo-architect"],
                "Regular",
                "Regular message.",
                [],
            )

            closed = store.close_message("echo-architect", message["id"], "")

            self.assertEqual(closed["status"], "closed")
            self.assertTrue(closed["closed_at"])

    def test_close_message_dispatch_trigger_without_reply_raises_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "idem-close-norep",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            self._stamp_v1(store, dispatch)

            with self.assertRaises(ValidationError) as cm:
                store.close_message("alpha-worker", dispatch["message_id"], "Done.")
            message = str(cm.exception)
            self.assertIn("dispatch trigger", message)
            self.assertIn(dispatch["message_id"], message)
            self.assertIn("alpha-architect", message)
            self.assertIn("parent_message_id", message)
            with store._db.connection() as conn:
                status = conn.execute(
                    "select status from message_recipients where to_agent = ? and message_id = ?",
                    ("alpha-worker", dispatch["message_id"]),
                ).fetchone()["status"]
            self.assertEqual(status, "sent")

    def test_close_dispatch_trigger_empty_response_without_reply_still_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect", "alpha-worker", "idem-close-empty-norep",
                "Measure signal", "Run the bounded worker task.", [],
            )

            with self.assertRaises(ValidationError):
                store.close_message("alpha-worker", dispatch["message_id"], "")

            with store.connection() as conn:
                status = conn.execute(
                    "select status from message_recipients where to_agent = ? and message_id = ?",
                    ("alpha-worker", dispatch["message_id"]),
                ).fetchone()["status"]
            self.assertEqual(status, "sent")

    def test_close_message_dispatch_trigger_after_reply_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "idem-close-reply",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            self._stamp_v1(store, dispatch)
            store.send_message(
                "alpha-worker",
                ["alpha-architect"],
                "Re: ping",
                "PONG",
                [],
                parent_message_id=dispatch["message_id"],
            )

            closed = store.close_message("alpha-worker", dispatch["message_id"], "Done.")

            self.assertEqual(closed["status"], "closed")
            with store._db.connection() as conn:
                status = conn.execute(
                    "select status from message_recipients where to_agent = ? and message_id = ?",
                    ("alpha-worker", dispatch["message_id"]),
                ).fetchone()["status"]
            self.assertEqual(status, "closed")

    def test_ack_message_dispatch_trigger_without_reply_raises_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "idem-ack-norep",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            self._stamp_v1(store, dispatch)

            with self.assertRaises(ValidationError) as cm:
                store.ack_message("alpha-worker", dispatch["message_id"], "Got it.")
            message = str(cm.exception)
            self.assertIn("dispatch trigger", message)
            self.assertIn(dispatch["message_id"], message)
            self.assertIn("alpha-architect", message)
            self.assertIn("ack_message", message)
            with store._db.connection() as conn:
                status = conn.execute(
                    "select status from message_recipients where to_agent = ? and message_id = ?",
                    ("alpha-worker", dispatch["message_id"]),
                ).fetchone()["status"]
            self.assertEqual(status, "sent")

    def test_ack_dispatch_trigger_empty_response_without_reply_still_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect", "alpha-worker", "idem-ack-empty-norep",
                "Measure signal", "Run the bounded worker task.", [],
            )

            with self.assertRaises(ValidationError):
                store.ack_message("alpha-worker", dispatch["message_id"], "")

            with store.connection() as conn:
                status = conn.execute(
                    "select status from message_recipients where to_agent = ? and message_id = ?",
                    ("alpha-worker", dispatch["message_id"]),
                ).fetchone()["status"]
            self.assertEqual(status, "sent")

    def test_ack_message_dispatch_trigger_after_reply_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "idem-ack-reply",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            self._stamp_v1(store, dispatch)
            store.send_message(
                "alpha-worker",
                ["alpha-architect"],
                "Re: ping",
                "PONG",
                [],
                parent_message_id=dispatch["message_id"],
            )

            acked = store.ack_message("alpha-worker", dispatch["message_id"], "Got it.")

            self.assertEqual(acked["status"], "acknowledged")
            with store._db.connection() as conn:
                status = conn.execute(
                    "select status from message_recipients where to_agent = ? and message_id = ?",
                    ("alpha-worker", dispatch["message_id"]),
                ).fetchone()["status"]
            self.assertEqual(status, "acknowledged")

    def test_close_message_dispatch_trigger_reply_from_non_recipient_does_not_satisfy_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "idem-close-third-party",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            self._stamp_v1(store, dispatch)
            store.send_message(
                "echo-architect",
                ["alpha-architect"],
                "Re: ping",
                "third party comment",
                [],
                parent_message_id=dispatch["message_id"],
            )

            with self.assertRaises(ValidationError) as cm:
                store.close_message("alpha-worker", dispatch["message_id"], "Done.")
            self.assertIn("dispatch trigger", str(cm.exception))
            self.assertIn(dispatch["message_id"], str(cm.exception))

            store.send_message(
                "alpha-worker",
                ["alpha-architect"],
                "Re: ping",
                "PONG",
                [],
                parent_message_id=dispatch["message_id"],
            )
            closed = store.close_message("alpha-worker", dispatch["message_id"], "Done.")
            self.assertEqual(closed["status"], "closed")

    def test_close_message_dispatch_trigger_reply_addressed_only_to_third_party_does_not_satisfy_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "idem-close-wrong-recipient",
                "Measure signal",
                "Run the bounded worker task.",
                [],
            )
            self._stamp_v1(store, dispatch)
            store.send_message(
                "alpha-worker",
                ["echo-architect"],
                "Re: ping",
                "PONG to wrong recipient",
                [],
                parent_message_id=dispatch["message_id"],
            )

            with self.assertRaises(ValidationError) as cm:
                store.close_message("alpha-worker", dispatch["message_id"], "Done.")
            self.assertIn("dispatch trigger", str(cm.exception))
            self.assertIn(dispatch["message_id"], str(cm.exception))

            store.send_message(
                "alpha-worker",
                ["alpha-architect", "echo-architect"],
                "Re: ping",
                "PONG (multi-recipient)",
                [],
                parent_message_id=dispatch["message_id"],
            )
            closed = store.close_message("alpha-worker", dispatch["message_id"], "Done.")
            self.assertEqual(closed["status"], "closed")

    def test_close_message_dispatch_trigger_reply_to_different_trigger_does_not_satisfy_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch_a = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "idem-A",
                "Measure A",
                "Run the first bounded worker task.",
                [],
            )
            dispatch_b = store.dispatch_agent(
                "alpha-architect",
                "alpha-worker",
                "idem-B",
                "Measure B",
                "Run the second bounded worker task.",
                [],
            )
            self._stamp_v1(store, dispatch_a, dispatch_b)
            store.send_message(
                "alpha-worker",
                ["alpha-architect"],
                "Re: A",
                "PONG-A",
                [],
                parent_message_id=dispatch_a["message_id"],
            )

            with self.assertRaises(ValidationError) as cm:
                store.close_message("alpha-worker", dispatch_b["message_id"], "Done.")
            self.assertIn("dispatch trigger", str(cm.exception))
            self.assertIn(dispatch_b["message_id"], str(cm.exception))

            store.send_message(
                "alpha-worker",
                ["alpha-architect"],
                "Re: B",
                "PONG-B",
                [],
                parent_message_id=dispatch_b["message_id"],
            )
            closed = store.close_message("alpha-worker", dispatch_b["message_id"], "Done.")
            self.assertEqual(closed["status"], "closed")

    def test_close_message_parented_non_dispatch_message_succeeds_without_further_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            first = store.send_message(
                "alpha-architect",
                ["echo-architect"],
                "Regular",
                "Regular message.",
                [],
            )
            second = store.send_message(
                "echo-architect",
                ["alpha-architect"],
                "Re: Regular",
                "Thread reply.",
                [],
                parent_message_id=first["id"],
            )

            closed = store.close_message("alpha-architect", second["id"], "")

            self.assertEqual(closed["status"], "closed")


class ConcurrencyCapTest(unittest.TestCase):
    class CountingStubAdapter(StubAdapter):
        def __init__(self, *, fail: bool = False) -> None:
            super().__init__(fail=fail)
            self.calls = 0

        def dispatch(self, context: DispatchContext) -> DispatchStart:
            self.calls += 1
            return super().dispatch(context)

    def _store(self, root: Path) -> Store:
        store = Store(root / "agent-comms.sqlite")
        seed_dispatch_actors(store, root)
        return store

    def _set_cap(self, store: Store, actor_id: str, cap: int) -> None:
        with store._db.connection() as conn:
            conn.execute("update actors set dispatch_cap = ? where id = ?", (cap, actor_id))

    def _counts(self, store: Store, producer_actor_id: str) -> dict[str, int]:
        with store._db.connection() as conn:
            rows = conn.execute(
                """
                select status, count(*) as count
                from dispatch_ledger
                where producer_actor_id = ?
                group by status
                """,
                (producer_actor_id,),
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def _dispatch(
        self,
        store: Store,
        idempotency_key: str,
        *,
        producer: str = "alpha-architect",
        target: str = "alpha-worker",
        adapter: StubAdapter | None = None,
    ) -> dict:
        dispatch = store.dispatch_agent(
            producer,
            target,
            idempotency_key,
            f"Subject {idempotency_key}",
            f"Body {idempotency_key}.",
            [],
            adapter_for_runtime=(lambda _runtime: adapter) if adapter is not None else None,
        )
        with store._db.connection() as conn:
            conn.execute(
                "update dispatch_ledger set policy_version = 'v1' where dispatch_id = ?",
                (dispatch["dispatch_id"],),
            )
            policy_version = conn.execute(
                "select policy_version from dispatch_ledger where dispatch_id = ?",
                (dispatch["dispatch_id"],),
            ).fetchone()["policy_version"]
        self.assertEqual(policy_version, "v1")
        return dispatch

    def _reply_and_close(self, store: Store, dispatch: dict) -> None:
        store.send_message(
            dispatch["recipient_actor_id"],
            [dispatch["producer_actor_id"]],
            "Re: dispatch",
            "Done.",
            [],
            parent_message_id=dispatch["message_id"],
        )
        store.close_message(dispatch["recipient_actor_id"], dispatch["message_id"], "Done.")

    def _reply_and_ack(self, store: Store, dispatch: dict) -> None:
        store.send_message(
            dispatch["recipient_actor_id"],
            [dispatch["producer_actor_id"]],
            "Re: dispatch",
            "Done.",
            [],
            parent_message_id=dispatch["message_id"],
        )
        store.ack_message(dispatch["recipient_actor_id"], dispatch["message_id"], "Got it.")

    def _ledger_row(self, store: Store, dispatch_id: str):
        with store._db.connection() as conn:
            return conn.execute("select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)).fetchone()

    def _recipient_status(self, store: Store, message_id: str, recipient_actor_id: str) -> str:
        with store._db.connection() as conn:
            return conn.execute(
                "select status from message_recipients where message_id = ? and to_agent = ?",
                (message_id, recipient_actor_id),
            ).fetchone()["status"]

    def test_actors_table_has_dispatch_cap_column_with_default_4(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            store.init()
            with store._db.connection() as conn:
                columns = {row["name"]: row for row in conn.execute("pragma table_info(actors)").fetchall()}
            self.assertIn("dispatch_cap", columns)
            self.assertEqual(columns["dispatch_cap"]["type"].lower(), "integer")
            self.assertEqual(columns["dispatch_cap"]["notnull"], 1)
            self.assertEqual(int(columns["dispatch_cap"]["dflt_value"]), 4)

            store.register_actor(
                "test-arch",
                "agent",
                "test-arch",
                team="test",
                role="architect",
                project_root=str(root / "test-arch"),
                capabilities=[],
            )
            with store._db.connection() as conn:
                dispatch_cap = conn.execute(
                    "select dispatch_cap from actors where id = ?",
                    ("test-arch",),
                ).fetchone()["dispatch_cap"]
            self.assertEqual(dispatch_cap, 4)

    def test_dispatch_agent_below_cap_promotes_inline_to_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            dispatch = self._dispatch(store, "idem-1", adapter=StubAdapter())

            self.assertEqual(dispatch["status"], "in_flight")
            self.assertTrue(dispatch["spawn_handle"])
            self.assertEqual(self._counts(store, "alpha-architect")["in_flight"], 1)

    def test_dispatch_agent_at_cap_returns_queued_without_calling_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            self._set_cap(store, "alpha-architect", 2)
            adapter = self.CountingStubAdapter()

            rows = [self._dispatch(store, f"idem-{idx}", adapter=adapter) for idx in range(3)]

            self.assertEqual([row["status"] for row in rows], ["in_flight", "in_flight", "queued"])
            self.assertIsNone(rows[2]["spawn_handle"])
            self.assertEqual(adapter.calls, 2)

    def test_dispatch_agent_at_cap_with_different_producer_promotes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            self._set_cap(store, "alpha-architect", 1)
            self._set_cap(store, "echo-architect", 1)

            alpha = self._dispatch(store, "alpha-1", adapter=StubAdapter())
            routing = self._dispatch(
                store,
                "echo-1",
                producer="echo-architect",
                target="echo-worker",
                adapter=StubAdapter(),
            )

            self.assertEqual(alpha["status"], "in_flight")
            self.assertEqual(routing["status"], "in_flight")

    def test_dispatch_agent_idempotent_retry_at_cap_returns_existing_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            self._set_cap(store, "alpha-architect", 2)

            first = self._dispatch(store, "idem-A", adapter=StubAdapter())
            self._dispatch(store, "idem-B", adapter=StubAdapter())
            queued = self._dispatch(store, "idem-C", adapter=StubAdapter())
            replay = self._dispatch(store, "idem-A", adapter=StubAdapter())

            self.assertEqual(queued["status"], "queued")
            self.assertEqual(replay["dispatch_id"], first["dispatch_id"])
            self.assertEqual(replay["status"], "in_flight")
            self.assertEqual(self._counts(store, "alpha-architect")["in_flight"], 2)

    def test_start_queued_dispatches_skips_producer_at_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            self._set_cap(store, "alpha-architect", 1)
            self._set_cap(store, "echo-architect", 4)

            first = self._dispatch(store, "alpha-1", adapter=StubAdapter())
            second = self._dispatch(store, "alpha-2", adapter=StubAdapter())
            third = self._dispatch(store, "alpha-3", adapter=StubAdapter())
            with store._db.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set created_at = ? where dispatch_id = ?",
                    ("2026-01-01T00:00:01+00:00", second["dispatch_id"]),
                )
                conn.execute(
                    "update dispatch_ledger set created_at = ? where dispatch_id = ?",
                    ("2026-01-01T00:00:02+00:00", third["dispatch_id"]),
                )
            self._dispatch(
                store,
                "echo-1",
                producer="echo-architect",
                target="echo-worker",
                adapter=StubAdapter(),
            )

            self.assertEqual(store.start_queued_dispatches(lambda _runtime: StubAdapter(), limit=10), [])
            self._reply_and_close(store, first)
            promoted = store.start_queued_dispatches(lambda _runtime: StubAdapter(), limit=10)

            self.assertEqual(len(promoted), 1)
            self.assertEqual(promoted[0]["idempotency_key"], "alpha-2")
            self.assertEqual(promoted[0]["status"], "in_flight")
            self.assertEqual(self._counts(store, "alpha-architect")["queued"], 1)

    def test_start_queued_dispatches_promotes_in_fifo_order_when_cap_allows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            self._set_cap(store, "alpha-architect", 4)

            in_flight = [self._dispatch(store, f"idem-{idx}", adapter=StubAdapter()) for idx in range(4)]
            queued_1 = self._dispatch(store, "idem-Q1", adapter=StubAdapter())
            queued_2 = self._dispatch(store, "idem-Q2", adapter=StubAdapter())
            with store._db.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set created_at = ? where dispatch_id = ?",
                    ("2026-01-01T00:00:01+00:00", queued_1["dispatch_id"]),
                )
                conn.execute(
                    "update dispatch_ledger set created_at = ? where dispatch_id = ?",
                    ("2026-01-01T00:00:02+00:00", queued_2["dispatch_id"]),
                )
            for dispatch in in_flight:
                self._reply_and_close(store, dispatch)

            promoted = store.start_queued_dispatches(lambda _runtime: StubAdapter(), limit=10)

            self.assertEqual(len(promoted), 2)
            self.assertEqual(promoted[0]["idempotency_key"], "idem-Q1")
            self.assertEqual([row["status"] for row in promoted], ["in_flight", "in_flight"])

    def test_retry_spawn_at_cap_raises_concurrency_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            self._set_cap(store, "alpha-architect", 1)
            failed = self._dispatch(store, "failed", adapter=StubAdapter(fail=True))
            in_flight = self._dispatch(store, "running", adapter=StubAdapter())

            self.assertEqual(failed["status"], "spawn_failed_message_landed")
            self.assertEqual(in_flight["status"], "in_flight")
            with self.assertRaises(ConcurrencyError) as raised:
                store.retry_spawn(failed["dispatch_id"], lambda _runtime: StubAdapter(), ttl_seconds=60)

            message = str(raised.exception)
            self.assertIn("retry_spawn", message)
            self.assertIn("at cap 1/1", message)
            self.assertIn("alpha-architect", message)
            self.assertEqual(self._ledger_row(store, failed["dispatch_id"])["status"], "spawn_failed_message_landed")

    def test_retry_spawn_post_adapter_race_halts_and_records_observed_values(self) -> None:
        for halt_fails in (False, True):
            with self.subTest(halt_fails=halt_fails):
                with tempfile.TemporaryDirectory() as temp_dir:
                    store = self._store(Path(temp_dir))
                    self._set_cap(store, "alpha-architect", 1)
                    failed = self._dispatch(store, f"failed-{halt_fails}", adapter=StubAdapter(fail=True))

                    class RaceAdapter:
                        def __init__(self) -> None:
                            self.halt_calls: list[str] = []

                        def dispatch(self, context: DispatchContext) -> DispatchStart:
                            with store._db.connection() as conn:
                                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                                conn.execute(
                                    """
                                    insert into dispatch_ledger(
                                      dispatch_id, parent_dispatch_id, idempotency_key, message_id,
                                      thread_ref, spawn_handle, recipient_actor_id, producer_actor_id,
                                      originating_actor_id, policy_name, policy_version, policy_issued_by,
                                      expected_close_by, status, created_at, spawned_at, observed_values_json
                                    )
                                    values(?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, 'v1', ?, ?, 'in_flight', ?, ?, '{}')
                                    """,
                                    (
                                        f"dispatch_retry_race_{halt_fails}",
                                        f"retry-race-winner-{halt_fails}",
                                        f"retry-race-thread-{halt_fails}",
                                        f"retry-race-handle-{halt_fails}",
                                        "alpha-worker",
                                        "alpha-architect",
                                        "alpha-architect",
                                        WORKER_DISPATCH_POLICY,
                                        "alpha-architect",
                                        "2099-01-01T00:00:00+00:00",
                                        now,
                                        now,
                                    ),
                                )
                            return DispatchStart(spawn_handle=f"retry-orphan-{halt_fails}", observed_values={})

                        def halt(self, spawn_handle: str, observed_values=None) -> None:
                            self.halt_calls.append(spawn_handle)
                            if halt_fails:
                                raise Exception("halt-fails")

                    race_adapter = RaceAdapter()
                    with self.assertRaises(ConcurrencyError) as raised:
                        store.retry_spawn(failed["dispatch_id"], lambda _runtime: race_adapter)

                    self.assertIn("at cap 1/1", str(raised.exception))
                    self.assertEqual(race_adapter.halt_calls, [f"retry-orphan-{halt_fails}"])
                    row = self._ledger_row(store, failed["dispatch_id"])
                    observed = json.loads(row["observed_values_json"])
                    expected = "halt failed: halt-fails" if halt_fails else "halted"
                    self.assertEqual(row["status"], "spawn_failed_message_landed")
                    self.assertTrue(observed["retry_race_halted_at"])
                    self.assertEqual(observed["retry_race_halt_outcome"], expected)

    def test_start_dispatch_by_id_post_adapter_race_halts_and_keeps_queued(self) -> None:
        for halt_fails in (False, True):
            with self.subTest(halt_fails=halt_fails):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    store = self._store(root)
                    self._set_cap(store, "alpha-architect", 2)
                    self._dispatch(store, "winner-1", adapter=StubAdapter())

                    class RaceAdapter:
                        def __init__(self) -> None:
                            self.halt_calls: list[str] = []

                        def dispatch(self, context: DispatchContext) -> DispatchStart:
                            with store._db.connection() as conn:
                                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                                conn.execute(
                                    """
                                    insert into dispatch_ledger(
                                      dispatch_id, parent_dispatch_id, idempotency_key, message_id,
                                      thread_ref, spawn_handle, recipient_actor_id, producer_actor_id,
                                      originating_actor_id, policy_name, policy_version, policy_issued_by,
                                      expected_close_by, status, created_at, spawned_at, observed_values_json
                                    )
                                    values(?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, 'v1', ?, ?, 'in_flight', ?, ?, '{}')
                                    """,
                                    (
                                        f"dispatch_race_{halt_fails}",
                                        f"race-winner-{halt_fails}",
                                        f"race-thread-{halt_fails}",
                                        f"race-handle-{halt_fails}",
                                        "alpha-worker",
                                        "alpha-architect",
                                        "alpha-architect",
                                        WORKER_DISPATCH_POLICY,
                                        "alpha-architect",
                                        "2099-01-01T00:00:00+00:00",
                                        now,
                                        now,
                                    ),
                                )
                            return DispatchStart(spawn_handle=f"orphan-{halt_fails}", observed_values={"adapter": "race"})

                        def halt(self, spawn_handle: str, observed_values=None) -> None:
                            self.halt_calls.append(spawn_handle)
                            if halt_fails:
                                raise Exception("halt-fails")

                    race_adapter = RaceAdapter()
                    raced = self._dispatch(store, f"raced-{halt_fails}", adapter=race_adapter)

                    self.assertEqual(raced["status"], "queued")
                    self.assertIsNone(raced["spawn_handle"])
                    self.assertTrue(raced["observed_values"]["start_race_halted_at"])
                    expected = "halt failed: halt-fails" if halt_fails else "halted"
                    self.assertEqual(raced["observed_values"]["start_race_halt_outcome"], expected)
                    self.assertEqual(race_adapter.halt_calls, [f"orphan-{halt_fails}"])

    def test_reconcile_dispatches_drains_queue_after_in_flight_closes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            self._set_cap(store, "alpha-architect", 2)
            rows = [self._dispatch(store, f"idem-{idx}", adapter=StubAdapter()) for idx in range(4)]
            self._reply_and_close(store, rows[0])

            actions = store.reconcile_dispatches(
                lambda _runtime: StubAdapter(),
                human_actor_id="01M36YTJV9XBW95S6ZWV47C4RG",
            )

            self.assertTrue(any(action["status"] == "in_flight" for action in actions))
            counts = self._counts(store, "alpha-architect")
            self.assertEqual(counts["in_flight"], 2)
            self.assertEqual(counts["queued"], 1)
            self.assertEqual(counts["closed"], 1)

    def test_reconcile_dispatches_drain_runs_after_dlq_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            self._set_cap(store, "alpha-architect", 1)
            row1 = self._dispatch(store, "overdue", adapter=StubAdapter())
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
            with store._db.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set expected_close_by = ? where dispatch_id = ?",
                    (past, row1["dispatch_id"]),
                )
            row2 = self._dispatch(store, "queued", adapter=StubAdapter())

            actions = store.reconcile_dispatches(
                lambda _runtime: StubAdapter(),
                human_actor_id="01M36YTJV9XBW95S6ZWV47C4RG",
            )

            statuses = {row["dispatch_id"]: self._ledger_row(store, row["dispatch_id"])["status"] for row in (row1, row2)}
            self.assertEqual(statuses[row1["dispatch_id"]], "dlq")
            self.assertEqual(statuses[row2["dispatch_id"]], "in_flight")
            action_pairs = [(action["dispatch_id"], action["status"]) for action in actions if "dispatch_id" in action]
            self.assertLess(
                action_pairs.index((row1["dispatch_id"], "dlq")),
                action_pairs.index((row2["dispatch_id"], "in_flight")),
            )

    def test_close_message_on_dispatch_trigger_transitions_ledger_to_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            dispatch = self._dispatch(store, "close-ledger", adapter=StubAdapter())

            self._reply_and_close(store, dispatch)

            row = self._ledger_row(store, dispatch["dispatch_id"])
            self.assertEqual(row["status"], "closed")
            self.assertTrue(row["closed_at"])
            self.assertTrue(json.loads(row["observed_values_json"])["recipient_closed_at"])
            with store._db.connection() as conn:
                recipient_status = conn.execute(
                    "select status from message_recipients where message_id = ? and to_agent = ?",
                    (dispatch["message_id"], "alpha-worker"),
                ).fetchone()["status"]
            self.assertEqual(recipient_status, "closed")

    def test_ack_message_on_dispatch_trigger_transitions_ledger_to_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            dispatch = self._dispatch(store, "ack-ledger", adapter=StubAdapter())

            self._reply_and_ack(store, dispatch)

            row = self._ledger_row(store, dispatch["dispatch_id"])
            self.assertEqual(row["status"], "closed")
            self.assertTrue(row["closed_at"])
            self.assertTrue(json.loads(row["observed_values_json"])["recipient_closed_at"])
            with store._db.connection() as conn:
                recipient_status = conn.execute(
                    "select status from message_recipients where message_id = ? and to_agent = ?",
                    (dispatch["message_id"], "alpha-worker"),
                ).fetchone()["status"]
            self.assertEqual(recipient_status, "acknowledged")

    def test_close_and_ack_record_mismatch_when_dispatch_ledger_left_in_flight(self) -> None:
        for action in ("close", "ack"):
            for forced_status in ("queued", "dlq", "spawn_failed_message_landed"):
                with self.subTest(action=action, forced_status=forced_status):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        store = self._store(Path(temp_dir))
                        dispatch = self._dispatch(store, f"mismatch-{action}-{forced_status}", adapter=StubAdapter())
                        with store._db.connection() as conn:
                            conn.execute(
                                "update dispatch_ledger set status = ? where dispatch_id = ?",
                                (forced_status, dispatch["dispatch_id"]),
                            )
                        store.send_message(
                            dispatch["recipient_actor_id"],
                            [dispatch["producer_actor_id"]],
                            "Re: dispatch",
                            "Done.",
                            [],
                            parent_message_id=dispatch["message_id"],
                        )

                        if action == "close":
                            result = store.close_message(dispatch["recipient_actor_id"], dispatch["message_id"], "Done.")
                            expected_recipient_status = "closed"
                        else:
                            result = store.ack_message(dispatch["recipient_actor_id"], dispatch["message_id"], "Done.")
                            expected_recipient_status = "acknowledged"

                        self.assertEqual(result["status"], expected_recipient_status)
                        self.assertEqual(
                            self._recipient_status(store, dispatch["message_id"], dispatch["recipient_actor_id"]),
                            expected_recipient_status,
                        )
                        row = self._ledger_row(store, dispatch["dispatch_id"])
                        observed = json.loads(row["observed_values_json"])
                        self.assertEqual(row["status"], forced_status)
                        self.assertEqual(observed["close_ledger_status_mismatch"], forced_status)
                        self.assertTrue(observed["close_ledger_status_mismatch_at"])

    def test_close_message_with_already_closed_ledger_row_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            dispatch = self._dispatch(store, "closed-idempotent", adapter=StubAdapter())
            self._reply_and_close(store, dispatch)

            closed = store.close_message(dispatch["recipient_actor_id"], dispatch["message_id"], "Done again.")

            self.assertEqual(closed["status"], "closed")
            row = self._ledger_row(store, dispatch["dispatch_id"])
            observed = json.loads(row["observed_values_json"])
            self.assertEqual(row["status"], "closed")
            self.assertNotIn("close_ledger_status_mismatch", observed)

    def test_start_dispatch_by_id_success_rowcount_miss_halts_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            dispatch = self._dispatch(store, "direct-rowcount-miss")

            class RaceAdapter:
                def __init__(self) -> None:
                    self.halt_calls: list[str] = []

                def dispatch(self, context: DispatchContext) -> DispatchStart:
                    with store._db.connection() as conn:
                        conn.execute(
                            "update dispatch_ledger set status = 'closed' where dispatch_id = ?",
                            (context.dispatch["dispatch_id"],),
                        )
                    return DispatchStart(spawn_handle="orphan-direct", observed_values={"adapter": "race"})

                def halt(self, spawn_handle: str, observed_values=None) -> None:
                    self.halt_calls.append(spawn_handle)

            adapter = RaceAdapter()
            with self.assertRaises(ConcurrencyError) as raised:
                store._dispatch._start_dispatch_by_id(lambda _runtime: adapter, dispatch["dispatch_id"], ttl_seconds=60)

            self.assertIn(dispatch["dispatch_id"], str(raised.exception))
            self.assertIn("orphan-direct", str(raised.exception))
            self.assertIn("halted", str(raised.exception))
            self.assertEqual(adapter.halt_calls, ["orphan-direct"])
            self.assertEqual(self._ledger_row(store, dispatch["dispatch_id"])["status"], "closed")
            self.assertEqual(self._counts(store, "alpha-architect").get("in_flight", 0), 0)

    def test_start_queued_dispatches_success_rowcount_miss_halts_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            dispatch = self._dispatch(store, "queued-rowcount-miss")
            continued = self._dispatch(
                store,
                "queued-rowcount-continued",
                producer="echo-architect",
                target="echo-worker",
            )
            with store._db.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set created_at = ? where dispatch_id = ?",
                    ("2026-01-01T00:00:01+00:00", dispatch["dispatch_id"]),
                )
                conn.execute(
                    "update dispatch_ledger set created_at = ? where dispatch_id = ?",
                    ("2026-01-01T00:00:02+00:00", continued["dispatch_id"]),
                )

            class RaceAdapter:
                def __init__(self) -> None:
                    self.halt_calls: list[str] = []

                def dispatch(self, context: DispatchContext) -> DispatchStart:
                    if context.dispatch["dispatch_id"] == dispatch["dispatch_id"]:
                        with store._db.connection() as conn:
                            conn.execute(
                                "update dispatch_ledger set status = 'closed' where dispatch_id = ?",
                                (context.dispatch["dispatch_id"],),
                            )
                        return DispatchStart(spawn_handle="orphan-queued", observed_values={"adapter": "race"})
                    return DispatchStart(
                        spawn_handle=f"continued:{context.dispatch['dispatch_id']}",
                        observed_values={"adapter": "continued"},
                    )

                def halt(self, spawn_handle: str, observed_values=None) -> None:
                    self.halt_calls.append(spawn_handle)

            adapter = RaceAdapter()
            started = store.start_queued_dispatches(lambda _runtime: adapter, limit=2)

            self.assertEqual([row["dispatch_id"] for row in started], [dispatch["dispatch_id"], continued["dispatch_id"]])
            self.assertEqual(started[0]["status"], "start_race_concurrent")
            self.assertIn(dispatch["dispatch_id"], started[0]["detail"])
            self.assertIn("orphan-queued", started[0]["detail"])
            self.assertIn("halted", started[0]["detail"])
            self.assertEqual(started[1]["status"], "in_flight")
            self.assertEqual(adapter.halt_calls, ["orphan-queued"])
            self.assertEqual(self._ledger_row(store, dispatch["dispatch_id"])["status"], "closed")
            self.assertEqual(self._ledger_row(store, continued["dispatch_id"])["status"], "in_flight")
            self.assertEqual(self._counts(store, "alpha-architect").get("in_flight", 0), 0)
            self.assertEqual(self._counts(store, "echo-architect").get("in_flight", 0), 1)

    def test_start_queued_dispatches_releases_writer_lock_across_adapter_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            dispatch = self._dispatch(store, "lock-release")

            class WritingAdapter(StubAdapter):
                def __init__(self) -> None:
                    super().__init__()
                    self.write_succeeded = False

                def dispatch(self, context: DispatchContext) -> DispatchStart:
                    with contextlib.closing(sqlite3.connect(store.db_path, timeout=0.05)) as conn:
                        with conn:
                            conn.execute("pragma foreign_keys = on")
                            conn.execute(
                                "update messages set subject = subject where id = ?",
                                (context.message["id"],),
                            )
                    self.write_succeeded = True
                    return super().dispatch(context)

            adapter = WritingAdapter()
            started = store.start_queued_dispatches(lambda _runtime: adapter)

            self.assertTrue(adapter.write_succeeded)
            self.assertEqual(started[0]["dispatch_id"], dispatch["dispatch_id"])
            self.assertEqual(started[0]["status"], "in_flight")

    def test_start_queued_dispatches_post_spawn_cap_recheck_halts_second_start_race(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            self._set_cap(store, "alpha-architect", 3)
            self._dispatch(store, "already-running", adapter=StubAdapter())
            first = self._dispatch(store, "queued-first")
            second = self._dispatch(store, "queued-second")
            with store._db.connection() as conn:
                conn.execute(
                    "update dispatch_ledger set created_at = ? where dispatch_id = ?",
                    ("2026-01-01T00:00:01+00:00", first["dispatch_id"]),
                )
                conn.execute(
                    "update dispatch_ledger set created_at = ? where dispatch_id = ?",
                    ("2026-01-01T00:00:02+00:00", second["dispatch_id"]),
                )

            class CapRaceAdapter(StubAdapter):
                def __init__(self) -> None:
                    super().__init__()
                    self.calls = 0
                    self.halt_calls: list[str] = []

                def dispatch(self, context: DispatchContext) -> DispatchStart:
                    self.calls += 1
                    if self.calls == 2:
                        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        with store._db.connection() as conn:
                            conn.execute(
                                """
                                insert into dispatch_ledger(
                                  dispatch_id, parent_dispatch_id, idempotency_key, message_id,
                                  thread_ref, spawn_handle, recipient_actor_id, producer_actor_id,
                                  originating_actor_id, policy_name, policy_version, policy_issued_by,
                                  expected_close_by, status, created_at, spawned_at, observed_values_json
                                )
                                values(
                                  'dispatch_cap_race_winner', NULL, 'cap-race-winner', NULL,
                                  'cap-race-thread', 'cap-race-handle', 'alpha-worker', 'alpha-architect',
                                  'alpha-architect', ?, 'v1', 'alpha-architect',
                                  '2099-01-01T00:00:00+00:00', 'in_flight', ?, ?, '{}'
                                )
                                """,
                                (WORKER_DISPATCH_POLICY, now, now),
                            )
                    return super().dispatch(context)

                def halt(self, spawn_handle: str, observed_values=None) -> None:
                    self.halt_calls.append(spawn_handle)

            adapter = CapRaceAdapter()
            started = store.start_queued_dispatches(lambda _runtime: adapter, limit=2)

            self.assertEqual([row["dispatch_id"] for row in started], [first["dispatch_id"], second["dispatch_id"]])
            self.assertEqual(self._ledger_row(store, first["dispatch_id"])["status"], "in_flight")
            second_row = self._ledger_row(store, second["dispatch_id"])
            second_observed = json.loads(second_row["observed_values_json"])
            self.assertEqual(second_row["status"], "queued")
            self.assertEqual(second_observed["start_race_halt_outcome"], "halted")
            self.assertEqual(adapter.halt_calls, [f"fake:alpha-worker:{second['dispatch_id']}"])
            self.assertEqual(self._counts(store, "alpha-architect")["in_flight"], 3)

    def test_spawn_failed_rowcount_miss_raises_without_halt_for_direct_and_queued_start(self) -> None:
        for mode in ("direct", "queued"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as temp_dir:
                    store = self._store(Path(temp_dir))
                    dispatch = self._dispatch(store, f"spawn-fail-miss-{mode}")

                    class FailingRaceAdapter:
                        def __init__(self) -> None:
                            self.halt_calls: list[str] = []

                        def dispatch(self, context: DispatchContext) -> DispatchStart:
                            with store._db.connection() as conn:
                                conn.execute(
                                    "update dispatch_ledger set status = 'closed' where dispatch_id = ?",
                                    (context.dispatch["dispatch_id"],),
                                )
                            raise RuntimeError("spawn failed after race")

                        def halt(self, spawn_handle: str, observed_values=None) -> None:
                            self.halt_calls.append(spawn_handle)

                    adapter = FailingRaceAdapter()
                    if mode == "direct":
                        with self.assertRaises(ConcurrencyError) as raised:
                            store._dispatch._start_dispatch_by_id(
                                lambda _runtime: adapter,
                                dispatch["dispatch_id"],
                                ttl_seconds=60,
                            )
                        message = str(raised.exception)
                    else:
                        started = store.start_queued_dispatches(lambda _runtime: adapter)
                        self.assertEqual(len(started), 1)
                        self.assertEqual(started[0]["dispatch_id"], dispatch["dispatch_id"])
                        self.assertEqual(started[0]["status"], "start_race_concurrent")
                        message = started[0]["detail"]
                    self.assertIn(dispatch["dispatch_id"], message)
                    self.assertIn("observed status=closed", message)
                    self.assertEqual(adapter.halt_calls, [])
                    self.assertEqual(self._ledger_row(store, dispatch["dispatch_id"])["status"], "closed")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            dispatch = self._dispatch(store, "ordinary-spawn-fail")
            row = store.start_queued_dispatches(lambda _runtime: StubAdapter(fail=True))[0]

            self.assertEqual(row["dispatch_id"], dispatch["dispatch_id"])
            self.assertEqual(row["status"], "spawn_failed_message_landed")
            self.assertEqual(row["failure_reason"], "adapter spawn failed")

    def test_start_race_annotation_rowcount_miss_raises_with_halt_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            self._set_cap(store, "alpha-architect", 1)
            self._dispatch(store, "running", adapter=StubAdapter())
            raced = self._dispatch(store, "start-race-miss")

            class HaltClosesAdapter(StubAdapter):
                def __init__(self) -> None:
                    super().__init__()
                    self.halt_calls: list[str] = []

                def halt(self, spawn_handle: str, observed_values=None) -> None:
                    # The cap-race orphan halt runs OUTSIDE the write transaction
                    # (socket/halt/wait must never span BEGIN IMMEDIATE). A
                    # concurrent writer terminates the still-queued row while the
                    # lock is released here, so the follow-up race annotation must
                    # miss and the start must refuse loudly with the observed
                    # status rather than resurrect a false in_flight.
                    with store._db.connection() as conn:
                        conn.execute(
                            "update dispatch_ledger set status = 'closed' where dispatch_id = ?",
                            (raced["dispatch_id"],),
                        )
                    self.halt_calls.append(spawn_handle)

            adapter = HaltClosesAdapter()
            with self.assertRaises(ConcurrencyError) as raised:
                store._dispatch._start_dispatch_by_id(lambda _runtime: adapter, raced["dispatch_id"], ttl_seconds=60)

            message = str(raised.exception)
            self.assertIn(raced["dispatch_id"], message)
            self.assertIn(f"fake:alpha-worker:{raced['dispatch_id']}", message)
            self.assertIn("halted", message)
            self.assertIn("observed status=closed", message)
            self.assertEqual(adapter.halt_calls, [f"fake:alpha-worker:{raced['dispatch_id']}"])
            self.assertEqual(self._ledger_row(store, raced["dispatch_id"])["status"], "closed")


if __name__ == "__main__":
    unittest.main()
