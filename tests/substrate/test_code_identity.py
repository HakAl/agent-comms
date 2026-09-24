from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_comms import code_identity
from agent_comms.adapters import DispatchContext, DispatchStart
from agent_comms.code_identity import StaleModuleError
from agent_comms.store import Store

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"


class StubAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.contexts: list[DispatchContext] = []
        self.halted: list[str] = []

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        self.contexts.append(context)
        if self.fail:
            raise RuntimeError("adapter spawn failed")
        return DispatchStart(
            spawn_handle=f"stub:{context.recipient['id']}:{context.dispatch['dispatch_id']}",
            observed_values={"adapter": "stub"},
        )

    def halt(self, spawn_handle: str, observed_values=None) -> None:
        self.halted.append(spawn_handle)


def seed_dispatch_actors(store: Store, root: Path) -> None:
    store.register_actor(HUMAN_ID, "human", "alice")
    store.register_agent_actor("alpha-architect", "alpha", "architect", str(root / "alpha-architect"), [])
    store.register_agent_actor("echo-architect", "echo", "architect", str(root / "echo-architect"), [])
    store.register_agent_actor(
        "alpha-worker",
        "alpha",
        "worker",
        str(root / "alpha-worker"),
        [],
        runtime="stub",
        spawn={"command": "stub"},
        owner="alpha-architect",
    )
    store.register_agent_actor(
        "echo-worker", "echo", "worker", str(root / "echo-worker"), [],
        owner="echo-architect",
    )


def stale_error() -> StaleModuleError:
    return StaleModuleError(
        "dispatch surface changed at agent_comms/mailbox.py; "
        "reconnect the MCP server (/mcp) then retry"
    )


def row_counts(store: Store) -> tuple[int, int]:
    store.init()
    with store.connection() as conn:
        dispatch_count = conn.execute("select count(*) from dispatch_ledger").fetchone()[0]
        message_count = conn.execute("select count(*) from messages").fetchone()[0]
    return int(dispatch_count), int(message_count)


def dispatch_by_id(store: Store, dispatch_id: str) -> dict:
    with store.connection() as conn:
        return store._dispatch._dispatch_by_id(conn, dispatch_id)


def copy_identity_fixture(root: Path) -> None:
    for source in code_identity.included_surface_paths():
        relpath = source.relative_to(code_identity.SURFACE_ROOT).as_posix()
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for relpath in ("agent_comms/status.py",):
        source = code_identity.SURFACE_ROOT / relpath
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


class CodeIdentityTest(unittest.TestCase):
    def test_T1_stale_dispatch_agent_refuses_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            with patch("agent_comms.dispatch_ledger.require_fresh_module", side_effect=stale_error()):
                with self.assertRaisesRegex(StaleModuleError, "agent_comms/mailbox.py.*reconnect"):
                    store.dispatch_agent("alpha-architect", "alpha-worker", "stale-dispatch", "Work", "Body.", [])

            self.assertEqual(row_counts(store), (0, 0))

    def test_T2_fresh_dispatch_agent_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)

            with patch("agent_comms.dispatch_ledger.require_fresh_module", return_value=None):
                dispatch = store.dispatch_agent(
                    "alpha-architect",
                    "alpha-worker",
                    "fresh-dispatch",
                    "Work",
                    "Body.",
                    [],
                    adapter_for_runtime=lambda _runtime: StubAdapter(),
                )

            self.assertEqual(dispatch["status"], "in_flight")
            self.assertEqual(len(store.list_inbox("alpha-worker")), 1)

    def test_T3_indeterminate_identity_arms_proceed(self) -> None:
        with patch.object(code_identity, "LOADED_CODE_IDENTITY", None):
            with patch.object(code_identity, "LOADED_SURFACE", None):
                code_identity.require_fresh_module()

    def test_contract_version_is_declared_and_ast_read_from_disk(self) -> None:
        self.assertIsInstance(code_identity.CONTRACT_VERSION, int)
        self.assertGreater(code_identity.CONTRACT_VERSION, 0)
        self.assertEqual(code_identity.LOADED_CONTRACT_VERSION, code_identity.CONTRACT_VERSION)
        self.assertEqual(code_identity.current_contract_version(), code_identity.CONTRACT_VERSION)

        self.assertEqual(code_identity._read_contract_version(b"CONTRACT_VERSION: int = 7\n"), 7)
        self.assertIsNone(code_identity._read_contract_version(b"CONTRACT_VERSION = '7'\n"))
        self.assertIsNone(code_identity._read_contract_version(b"CONTRACT_VERSION = True\n"))
        self.assertIsNone(code_identity._read_contract_version(b"OTHER_VERSION = 7\n"))
        self.assertIsNone(code_identity._read_contract_version(b"CONTRACT_VERSION: int =\n"))

    def test_compatible_surface_delta_returns_but_contract_changed_and_fallback_raise(self) -> None:
        loaded_surface = {
            "agent_comms/mailbox.py": "old",
            "agent_comms/store.py": "same",
        }
        current_surface = {
            "agent_comms/mailbox.py": "new",
            "agent_comms/store.py": "same",
        }
        loaded_identity = code_identity._identity_from_surface(loaded_surface)

        loaded_contract = code_identity.CONTRACT_VERSION
        with patch.object(code_identity, "LOADED_CODE_IDENTITY", loaded_identity):
            with patch.object(code_identity, "LOADED_SURFACE", loaded_surface):
                with patch.object(code_identity, "_surface_map", return_value=current_surface):
                    with patch.object(code_identity, "current_contract_version", return_value=loaded_contract):
                        with patch.object(code_identity, "_LAST_TOLERATED_CODE_IDENTITY", None):
                            with self.assertLogs("agent_comms.code_identity", level="WARNING"):
                                code_identity.require_fresh_module()

        cases = [
            (
                loaded_contract + 1,
                f"dispatch CONTRACT changed (v{loaded_contract} -> v{loaded_contract + 1})",
                "compatible upgrade",
            ),
            (None, "dispatch surface changed at agent_comms/mailbox.py; reconnect", "CONTRACT changed"),
        ]
        for current_contract, expected, unexpected in cases:
            with self.subTest(current_contract=current_contract):
                with patch.object(code_identity, "LOADED_CODE_IDENTITY", loaded_identity):
                    with patch.object(code_identity, "LOADED_SURFACE", loaded_surface):
                        with patch.object(code_identity, "_surface_map", return_value=current_surface):
                            with patch.object(code_identity, "current_contract_version", return_value=current_contract):
                                with self.assertRaises(StaleModuleError) as raised:
                                    code_identity.require_fresh_module()

                message = str(raised.exception)
                self.assertIn(expected, message)
                self.assertNotIn(unexpected, message)
                self.assertIn("reconnect the MCP server (/mcp) then retry", message)

    def test_unreadable_surface_file_still_raises(self) -> None:
        loaded_surface = {"agent_comms/mailbox.py": "old"}
        loaded_identity = code_identity._identity_from_surface(loaded_surface)

        with patch.object(code_identity, "LOADED_CODE_IDENTITY", loaded_identity):
            with patch.object(code_identity, "LOADED_SURFACE", loaded_surface):
                with patch.object(code_identity, "_surface_map", side_effect=code_identity.SurfaceCaptureError("agent_comms/mailbox.py")):
                    with self.assertRaisesRegex(
                        StaleModuleError,
                        "dispatch surface file unreadable at agent_comms/mailbox.py; reconnect",
                    ):
                        code_identity.require_fresh_module()

    def test_compatible_surface_delta_logs_once_and_fresh_path_does_not_log(self) -> None:
        loaded_surface = {
            "agent_comms/mailbox.py": "old",
            "agent_comms/store.py": "same",
        }
        current_surface = {
            "agent_comms/mailbox.py": "new",
            "agent_comms/store.py": "same",
        }
        loaded_identity = code_identity._identity_from_surface(loaded_surface)
        current_identity = code_identity._identity_from_surface(current_surface)

        with patch.object(code_identity, "LOADED_CODE_IDENTITY", loaded_identity):
            with patch.object(code_identity, "LOADED_SURFACE", loaded_surface):
                with patch.object(code_identity, "_surface_map", return_value=current_surface):
                    with patch.object(code_identity, "current_contract_version", return_value=code_identity.LOADED_CONTRACT_VERSION):
                        with patch.object(code_identity, "_LAST_TOLERATED_CODE_IDENTITY", None):
                            with self.assertLogs("agent_comms.code_identity", level="WARNING") as logs:
                                code_identity.require_fresh_module()
                            self.assertEqual(len(logs.output), 1)
                            self.assertIn("agent_comms/mailbox.py", logs.output[0])
                            self.assertIn(f"contract v{code_identity.LOADED_CONTRACT_VERSION} unchanged", logs.output[0])
                            self.assertEqual(code_identity._LAST_TOLERATED_CODE_IDENTITY, current_identity)

                            with self.assertNoLogs("agent_comms.code_identity", level="WARNING"):
                                code_identity.require_fresh_module()

        with patch.object(code_identity, "LOADED_CODE_IDENTITY", loaded_identity):
            with patch.object(code_identity, "LOADED_SURFACE", loaded_surface):
                with patch.object(code_identity, "_surface_map", return_value=loaded_surface):
                    with patch.object(code_identity, "_LAST_TOLERATED_CODE_IDENTITY", None):
                        with self.assertNoLogs("agent_comms.code_identity", level="WARNING"):
                            code_identity.require_fresh_module()

    def test_contract_surface_digest_and_classification_are_complete(self) -> None:
        self.assertIn("agent_comms/codex_auth_refresh.py", code_identity.INCLUDED_EXPLICIT)
        self.assertIn("agent_comms/codex_auth_refresh.py", code_identity.CONTRACT_GOVERNING)
        self.assertEqual(
            code_identity.contract_surface_digest(),
            code_identity.CONTRACT_SURFACE_DIGEST,
            "contract-governing surface changed; if this is a contract change bump CONTRACT_VERSION, "
            "then refresh CONTRACT_SURFACE_DIGEST; if it is contract-compatible, refresh the digest only -- "
            "both are a conscious declaration.",
        )
        included = {path.relative_to(code_identity.SURFACE_ROOT).as_posix() for path in code_identity.included_surface_paths()}
        classified = set(code_identity.CONTRACT_GOVERNING) | set(code_identity.CONTRACT_NEUTRAL_REASONS)
        self.assertEqual(classified, included)
        self.assertEqual(code_identity.unclassified_contract_surface_paths(), [])
        self.assertEqual(
            code_identity.unclassified_contract_surface_paths({"agent_comms/new_dispatch_surface.py"}),
            ["agent_comms/new_dispatch_surface.py"],
        )
        self.assertTrue(set(code_identity.CONTRACT_GOVERNING).isdisjoint(code_identity.CONTRACT_NEUTRAL_REASONS))

        surface = code_identity._surface_map()
        changed_surface = dict(surface)
        changed_surface["agent_comms/mailbox.py"] = "0" * 64
        self.assertNotEqual(code_identity.contract_surface_digest(changed_surface), code_identity.CONTRACT_SURFACE_DIGEST)

    def test_real_adapter_policy_and_hook_are_contract_governing_not_neutral(self) -> None:
        for relpath in (
            "agent_comms/adapters/_base.py",
            "agent_comms/policies/__init__.py",
            "agent_comms/hooks/pre_tool_use.py",
        ):
            with self.subTest(relpath=relpath):
                self.assertIn(relpath, code_identity.CONTRACT_GOVERNING)
                self.assertNotIn(relpath, code_identity.CONTRACT_NEUTRAL_REASONS)

        for relpath in ("agent_comms/adapters/fake.py", "agent_comms/adapters/fake_worker.py"):
            with self.subTest(relpath=relpath):
                self.assertIn(relpath, code_identity.CONTRACT_NEUTRAL_REASONS)
                self.assertNotIn(relpath, code_identity.CONTRACT_GOVERNING)

    def test_T4_stale_start_refuses_and_reconcile_pages_queued_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            first = store.dispatch_agent("alpha-architect", "alpha-worker", "queued-one", "Work", "Body.", [])
            second = store.dispatch_agent("alpha-architect", "alpha-worker", "queued-two", "Work", "Body.", [])
            adapter = StubAdapter()

            with patch("agent_comms.dispatch_ledger.require_fresh_module", side_effect=stale_error()):
                refused = store.start_queued_dispatches(lambda _runtime: adapter, limit=16)

            self.assertEqual(refused, [{"status": "stale_module_refused", "detail": str(stale_error())}])
            self.assertEqual(adapter.contexts, [])
            self.assertEqual(dispatch_by_id(store, first["dispatch_id"])["status"], "queued")
            self.assertEqual(dispatch_by_id(store, second["dispatch_id"])["status"], "queued")

            with patch("agent_comms.dispatch_ledger.require_fresh_module", side_effect=stale_error()):
                actions = store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)

            paged = [action for action in actions if action["status"] == "stale_module_refused_paged"]
            self.assertEqual({action["dispatch_id"] for action in paged}, {first["dispatch_id"], second["dispatch_id"]})
            inbox = store.list_inbox(HUMAN_ID, unread_only=False)
            self.assertEqual(len(inbox), 2)
            joined_bodies = "\n".join(store.read_message(HUMAN_ID, message["id"])["body"] for message in inbox)
            self.assertIn("agent_comms/mailbox.py", joined_bodies)
            self.assertIn(first["dispatch_id"], joined_bodies)
            self.assertIn(second["dispatch_id"], joined_bodies)
            self.assertIn("reconnect", joined_bodies)
            self.assertIn("stale_module_refused_paged_at", dispatch_by_id(store, first["dispatch_id"])["observed_values"])

            with patch("agent_comms.dispatch_ledger.require_fresh_module", side_effect=stale_error()):
                store.reconcile_dispatches(lambda _runtime: adapter, human_actor_id=HUMAN_ID)
            self.assertEqual(len(store.list_inbox(HUMAN_ID, unread_only=False)), 2)

            with patch("agent_comms.dispatch_ledger.require_fresh_module", return_value=None):
                fresh = store.start_queued_dispatches(lambda _runtime: adapter, limit=16)
            self.assertEqual([row["status"] for row in fresh], ["in_flight", "in_flight"])

    def test_T5_retry_spawn_under_staleness_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent("alpha-architect", "alpha-worker", "retry-stale", "Work", "Body.", [])
            store.start_queued_dispatches(lambda _runtime: StubAdapter(fail=True))

            with patch("agent_comms.dispatch_ledger.require_fresh_module", side_effect=stale_error()):
                with self.assertRaisesRegex(StaleModuleError, "reconnect"):
                    store.retry_spawn(dispatch["dispatch_id"], lambda _runtime: StubAdapter())

    def test_T6_reply_actor_mismatch_records_pages_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent("alpha-architect", "alpha-worker", "mismatch", "Work", "Body.", [])
            started = store.start_queued_dispatches(lambda _runtime: StubAdapter())[0]
            reply = store.send_message(
                "echo-worker",
                ["alpha-architect"],
                "Wrong worker reply",
                "I should not be on this dispatch.",
                [],
                parent_message_id=started["message_id"],
            )
            store.register_agent_actor(
                "echo-worker-two", "echo", "worker", str(root / "echo-worker-two"), [],
                owner="echo-architect",
            )
            second_reply = store.send_message(
                "echo-worker-two",
                ["alpha-architect"],
                "Second wrong worker reply",
                "I also should not be on this dispatch.",
                [],
                parent_message_id=started["message_id"],
            )
            with store.connection() as conn:
                conn.execute(
                    "update messages set created_at = ? where id = ?",
                    ("2026-06-11T00:00:01+00:00", reply["id"]),
                )
                conn.execute(
                    "update messages set created_at = ? where id = ?",
                    ("2026-06-11T00:00:02+00:00", second_reply["id"]),
                )

            actions = store.reconcile_dispatches(lambda _runtime: StubAdapter(), human_actor_id=HUMAN_ID)

            mismatch_actions = [action for action in actions if action["status"] == "reply_actor_mismatch"]
            self.assertEqual(mismatch_actions, [{"dispatch_id": dispatch["dispatch_id"], "status": "reply_actor_mismatch"}])
            page_actions = [action for action in actions if action["status"] == "producer_mismatch_paged"]
            self.assertEqual(len(page_actions), 1)
            row = dispatch_by_id(store, dispatch["dispatch_id"])
            observed = row["observed_values"]
            self.assertEqual(row["status"], "in_flight")
            self.assertEqual(observed["reply_actor_mismatch"]["message_id"], reply["id"])
            self.assertNotEqual(observed["reply_actor_mismatch"]["message_id"], second_reply["id"])
            self.assertEqual(observed["reply_actor_mismatch"]["from_agent"], "echo-worker")
            self.assertIn("producer_mismatch_paged_at", observed)
            self.assertNotIn("human_paged_at", observed)
            page = store.list_inbox("alpha-architect", unread_only=False)[0]
            self.assertEqual(page["from"], HUMAN_ID)
            self.assertEqual(page["parent_message_id"], started["message_id"])
            page_body = store.read_message("alpha-architect", page["id"])["body"]
            for value in ("alpha-architect", "alpha-worker", "echo-worker", reply["id"], dispatch["dispatch_id"]):
                self.assertIn(value, page_body)

            second_actions = store.reconcile_dispatches(lambda _runtime: StubAdapter(), human_actor_id=HUMAN_ID)
            self.assertNotIn("reply_actor_mismatch", [action["status"] for action in second_actions])
            self.assertNotIn("producer_mismatch_paged", [action["status"] for action in second_actions])
            self.assertEqual(
                len([message for message in store.list_inbox("alpha-architect", unread_only=False) if message["priority"] == "blocker"]),
                1,
            )
            self.assertEqual(dispatch_by_id(store, dispatch["dispatch_id"])["observed_values"]["reply_actor_mismatch"], observed["reply_actor_mismatch"])

    def test_T6b_legacy_reply_actor_mismatch_page_key_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "agent-comms.sqlite")
            seed_dispatch_actors(store, root)
            dispatch = store.dispatch_agent("alpha-architect", "alpha-worker", "legacy-mismatch-page", "Work", "Body.", [])
            started = store.start_queued_dispatches(lambda _runtime: StubAdapter())[0]
            store.send_message(
                "echo-worker",
                ["alpha-architect"],
                "Wrong worker reply",
                "I should not be on this dispatch.",
                [],
                parent_message_id=started["message_id"],
            )
            with store.connection() as conn:
                conn.execute(
                    """
                    update dispatch_ledger
                    set observed_values_json = json_set(
                      observed_values_json,
                      '$.reply_actor_mismatch_page_message_id',
                      'legacy-page'
                    )
                    where dispatch_id = ?
                    """,
                    (dispatch["dispatch_id"],),
                )

            actions = store.reconcile_dispatches(lambda _runtime: StubAdapter(), human_actor_id=HUMAN_ID)

            self.assertIn("reply_actor_mismatch", [action["status"] for action in actions])
            self.assertNotIn("producer_mismatch_paged", [action["status"] for action in actions])
            self.assertEqual(
                [message for message in store.list_inbox("alpha-architect", unread_only=False) if message["priority"] == "blocker"],
                [],
            )

    def test_T7_recipient_producer_and_human_replies_do_not_flag_mismatch(self) -> None:
        arms = [
            ("alpha-worker", "alpha-architect"),
            ("alpha-architect", "alpha-worker"),
            (HUMAN_ID, "alpha-architect"),
        ]
        for from_actor, to_actor in arms:
            with self.subTest(from_actor=from_actor):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    store = Store(root / "agent-comms.sqlite")
                    seed_dispatch_actors(store, root)
                    dispatch = store.dispatch_agent("alpha-architect", "alpha-worker", f"allowed-{from_actor}", "Work", "Body.", [])
                    started = store.start_queued_dispatches(lambda _runtime: StubAdapter())[0]
                    store.send_message(from_actor, [to_actor], "Reply", "Body.", [], parent_message_id=started["message_id"])

                    actions = store.reconcile_dispatches(lambda _runtime: StubAdapter(), human_actor_id=HUMAN_ID)

                    self.assertNotIn("reply_actor_mismatch", [action["status"] for action in actions])
                    self.assertNotIn("reply_actor_mismatch", dispatch_by_id(store, dispatch["dispatch_id"])["observed_values"])
                    self.assertEqual(
                        [message for message in store.list_inbox("alpha-architect", unread_only=False) if message["priority"] == "blocker"],
                        [],
                    )

    def test_T8_closed_and_dlq_rows_are_not_scanned_for_mismatch(self) -> None:
        for status in ("closed", "dlq"):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    store = Store(root / "agent-comms.sqlite")
                    seed_dispatch_actors(store, root)
                    dispatch = store.dispatch_agent("alpha-architect", "alpha-worker", f"terminal-{status}", "Work", "Body.", [])
                    started = store.start_queued_dispatches(lambda _runtime: StubAdapter())[0]
                    with store.connection() as conn:
                        conn.execute(
                            "update dispatch_ledger set policy_version = 'v1', status = ?, observed_values_json = '{}' where dispatch_id = ?",
                            (status, dispatch["dispatch_id"]),
                        )
                        self.assertEqual(
                            conn.execute(
                                "select policy_version from dispatch_ledger where dispatch_id = ?",
                                (dispatch["dispatch_id"],),
                            ).fetchone()["policy_version"],
                            "v1",
                        )
                    store.send_message(
                        "echo-worker",
                        ["alpha-architect"],
                        "Wrong worker reply",
                        "Terminal rows should not be scanned.",
                        [],
                        parent_message_id=started["message_id"],
                    )

                    actions = store.reconcile_dispatches(lambda _runtime: StubAdapter(), human_actor_id=HUMAN_ID)

                    self.assertNotIn("reply_actor_mismatch", [action["status"] for action in actions])
                    self.assertNotIn("reply_actor_mismatch", dispatch_by_id(store, dispatch["dispatch_id"])["observed_values"])

    def test_T9_included_file_changes_digest_excluded_file_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            copy_identity_fixture(root)

            with patch.object(code_identity, "SURFACE_ROOT", root):
                original = code_identity.current_code_identity()
                self.assertIsNotNone(original)

                (root / "agent_comms" / "status.py").write_text("EXCLUDED_STATUS_CHANGE = True\n")
                self.assertEqual(code_identity.current_code_identity(), original)

                mailbox = root / "agent_comms" / "mailbox.py"
                mailbox.write_bytes(mailbox.read_bytes() + b"\n# included digest change\n")
                self.assertNotEqual(code_identity.current_code_identity(), original)

    def test_T10_every_agent_comms_py_file_is_classified(self) -> None:
        repo_root = code_identity.SURFACE_ROOT
        unclassified = []
        for path in sorted((repo_root / "agent_comms").glob("**/*.py")):
            if "__pycache__" in path.parts:
                continue
            relpath = path.relative_to(repo_root).as_posix()
            if code_identity.is_included_surface(relpath):
                continue
            if code_identity.exclusion_reason(relpath):
                continue
            unclassified.append(relpath)

        self.assertEqual(unclassified, [])
        self.assertTrue(code_identity.is_included_surface("agent_comms/mailbox.py"))
        self.assertTrue(code_identity.is_included_surface("agent_comms/adapters/__init__.py"))

    def test_T11_require_fresh_module_names_changed_included_file_and_noops_when_equal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            copy_identity_fixture(root)

            with patch.object(code_identity, "SURFACE_ROOT", root):
                loaded_surface = code_identity._surface_map()
                loaded_identity = code_identity._identity_from_surface(loaded_surface)

                with patch.object(code_identity, "LOADED_SURFACE", loaded_surface):
                    with patch.object(code_identity, "LOADED_CODE_IDENTITY", loaded_identity):
                        code_identity.require_fresh_module()

                        mailbox = root / "agent_comms" / "mailbox.py"
                        mailbox.write_bytes(mailbox.read_bytes() + b"\n# changed after load\n")
                        with patch.object(code_identity, "_LAST_TOLERATED_CODE_IDENTITY", None):
                            with self.assertLogs("agent_comms.code_identity", level="WARNING") as logs:
                                code_identity.require_fresh_module()
                            self.assertEqual(len(logs.output), 1)
                            self.assertIn("agent_comms/mailbox.py", logs.output[0])
                            self.assertIn(f"contract v{code_identity.LOADED_CONTRACT_VERSION} unchanged", logs.output[0])

                            with self.assertNoLogs("agent_comms.code_identity", level="WARNING"):
                                code_identity.require_fresh_module()

    def test_T12_import_fail_open_and_live_fail_closed_paths(self) -> None:
        with patch.object(code_identity, "LOADED_CODE_IDENTITY", None):
            with patch.object(code_identity, "LOADED_SURFACE", None):
                with patch("agent_comms.code_identity._surface_map", side_effect=AssertionError("should not recompute")):
                    code_identity.require_fresh_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            copy_identity_fixture(root)

            with patch.object(code_identity, "SURFACE_ROOT", root):
                loaded_surface = code_identity._surface_map()
                loaded_identity = code_identity._identity_from_surface(loaded_surface)
                (root / "agent_comms" / "mailbox.py").unlink()

                with patch.object(code_identity, "LOADED_SURFACE", loaded_surface):
                    with patch.object(code_identity, "LOADED_CODE_IDENTITY", loaded_identity):
                        with self.assertRaisesRegex(
                            StaleModuleError,
                            r"dispatch surface file unreadable at agent_comms/mailbox.py; reconnect",
                        ):
                            code_identity.require_fresh_module()


if __name__ == "__main__":
    unittest.main()
