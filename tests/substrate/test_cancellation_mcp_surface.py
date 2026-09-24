"""Stage-2 T1: the scoped ``cancel_dispatch`` MCP tool over REAL stdio.

These tests drive the same JSON-RPC stdio harness as ``test_mcp_stdio.py`` -- a
real ``python -m agent_comms.mcp_server`` subprocess doing ``initialize`` ->
``tools/list`` / ``tools/call`` -- to prove:

* an unscoped producer session (architect) discovers ``cancel_dispatch`` whose
  input schema carries ONLY ``dispatch_id`` and ``reason`` (no caller-supplied
  actor / identity / authority / admin / credential / producer field), and that
  a producer can cancel its own dispatch, idempotently, with refusals for a
  wrong producer, unknown dispatch, and empty/oversized reasons;
* a bounded worker session can neither discover nor call it (absent from
  ``tools/list``; ``tools/call`` is an error);
* the operator-mailbox session is mailbox-only: ``cancel_dispatch`` is absent
  from discovery and its call is an error (no admin mutation MCP surface).

Requires the ``mcp`` extra (FastMCP) in the interpreter, exactly like
``test_mcp_stdio.py``; run under the standard ``uv run --extra mcp`` gate.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_comms.dispatch_ledger import CANCELLATION_REASON_MAX
from agent_comms.policies import OPERATOR_MAILBOX_POLICY
from agent_comms.store import Store, WORKER_DISPATCH_POLICY

ROOT = Path(__file__).resolve().parents[2]
HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"


class CancelDispatchMcpSurfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = str(self.root / "agent-comms.sqlite")
        store = Store(Path(self.db_path))
        store.register_actor(HUMAN_ID, "human", "alice")
        store.register_agent_actor("arch", "alpha", "architect", str(self.root / "arch"), [])
        store.register_agent_actor("arch2", "alpha", "architect", str(self.root / "arch2"), [])
        store.register_agent_actor(
            "wrk", "alpha", "worker", str(self.root / "wrk"), [], runtime="stub", spawn={"command": "stub"},
            owner="arch",
        )
        self.store = store

    def base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("WAKE_POLICY", None)
        env.pop("WAKE_POLICY_VERSION", None)
        env["PYTHONPYCACHEPREFIX"] = "/private/tmp/agent-comms-pycache"
        return env

    def _queued(self, key: str = "q", producer: str = "arch") -> dict:
        return self.store.dispatch_agent(producer, "wrk", key, f"S {key}", f"B {key}", [])

    def call_mcp(self, args: list[str], calls: list[dict], env: dict[str, str]) -> list[dict]:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "0.1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            *calls,
        ]
        payload = "\n".join(json.dumps(message) for message in messages) + "\n"
        result = subprocess.run(
            [sys.executable, "-m", "agent_comms.mcp_server", *args],
            cwd=ROOT,
            env=env,
            input=payload,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]

    def _tools(self, args: list[str], env: dict[str, str]) -> dict[str, dict]:
        responses = self.call_mcp(
            args, [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}], env
        )
        listed = next(r for r in responses if r.get("id") == 2)
        return {tool["name"]: tool for tool in listed["result"]["tools"]}

    def _call(self, args: list[str], name: str, arguments: dict, env: dict[str, str], call_id: int = 3) -> dict:
        responses = self.call_mcp(
            args,
            [
                {
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            ],
            env,
        )
        return next(r for r in responses if r.get("id") == call_id)

    @staticmethod
    def _content_text(response: dict) -> str:
        return "\n".join(item.get("text", "") for item in response["result"]["content"])

    def _message_count(self) -> int:
        with self.store._db.connection() as conn:
            return conn.execute("select count(*) as c from messages").fetchone()["c"]

    def _thread_children(self, parent_message_id: str) -> list[str]:
        with self.store._db.connection() as conn:
            rows = conn.execute(
                "select message_id from message_threads where parent_message_id = ?",
                (parent_message_id,),
            ).fetchall()
        return [row["message_id"] for row in rows]

    def assert_exact_cancel_schema(self, schema: dict) -> None:
        # Exact-set predicate (no substring / subset slack): the inputSchema
        # property-key set AND required-field set must each equal exactly
        # {"dispatch_id", "reason"}. An exact set therefore admits no
        # caller-supplied actor/identity/authority/admin/credential/producer
        # field, since any such field would enlarge the property-key set.
        self.assertEqual(set(schema.get("properties", {})), {"dispatch_id", "reason"})
        self.assertEqual(set(schema.get("required", [])), {"dispatch_id", "reason"})

    # --- producer (unscoped architect) surface -------------------------- #
    def test_producer_discovers_cancel_dispatch_with_exact_identity_free_schema(self) -> None:
        env = self.base_env()
        tools = self._tools(["--db", self.db_path, "--actor-id", "arch"], env)
        self.assertIn("cancel_dispatch", tools)
        schema = tools["cancel_dispatch"]["inputSchema"]

        # The real scoped-stdio inputSchema satisfies the exact-set predicate.
        self.assert_exact_cancel_schema(schema)

        # Mechanical negative control: the SAME exact-set predicate must FAIL
        # when a third property is injected, proving the assertion is exact-set
        # and would reject any extra identity/authority field, not merely scan
        # for the two expected substrings.
        injected = {
            "properties": {**schema["properties"], "actor_id": {"type": "string"}},
            "required": schema.get("required", []),
        }
        with self.assertRaises(AssertionError):
            self.assert_exact_cancel_schema(injected)

    def test_producer_cancels_own_queued_dispatch(self) -> None:
        env = self.base_env()
        d = self._queued("mcp-cancel")
        response = self._call(
            ["--db", self.db_path, "--actor-id", "arch"],
            "cancel_dispatch",
            {"dispatch_id": d["dispatch_id"], "reason": "obsolete via mcp"},
            env,
        )
        self.assertFalse(response["result"].get("isError", False))
        result = json.loads(self._content_text(response))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["cancellation_state"], "confirmed")
        self.assertEqual(result["authority"], "producer")

    def test_producer_cancellation_adds_no_producer_notice_or_semaphore(self) -> None:
        # T8: a real MCP stdio producer cancellation is entirely notice-free. It
        # adds no producer notice / page / message / thread child / wake semaphore.
        env = self.base_env()
        d = self._queued("mcp-notice-free")
        # Baseline: the producer has no inbox and no wake semaphore of its own.
        self.assertEqual(self.store.list_inbox("arch", unread_only=False), [])
        before_msgs = self._message_count()

        response = self._call(
            ["--db", self.db_path, "--actor-id", "arch"],
            "cancel_dispatch",
            {"dispatch_id": d["dispatch_id"], "reason": "obsolete via mcp"},
            env,
        )
        self.assertFalse(response["result"].get("isError", False))
        result = json.loads(self._content_text(response))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["authority"], "producer")
        # No admin-style notice fields leak onto a producer cancellation.
        self.assertIsNone(result.get("admin_notice_message_id"))
        # No producer notice/page/message and no thread child of the dispatch.
        self.assertEqual(self.store.list_inbox("arch", unread_only=False), [])
        self.assertEqual(self._message_count(), before_msgs)
        self.assertEqual(self._thread_children(d["message_id"]), [])
        # No producer wake semaphore was written under the producer's project root.
        self.assertEqual(list((self.root / "arch").rglob("new_messages")), [])

    def test_producer_cancel_is_idempotent(self) -> None:
        env = self.base_env()
        d = self._queued("mcp-idem")
        args = ["--db", self.db_path, "--actor-id", "arch"]
        first = json.loads(self._content_text(self._call(args, "cancel_dispatch", {"dispatch_id": d["dispatch_id"], "reason": "drop"}, env)))
        second = json.loads(self._content_text(self._call(args, "cancel_dispatch", {"dispatch_id": d["dispatch_id"], "reason": "drop"}, env, call_id=4)))
        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(second["status"], "cancelled")
        self.assertEqual(second["cancellation_state"], "confirmed")
        self.assertEqual(second["termination_result"], "not_started")

    def test_wrong_producer_is_refused(self) -> None:
        env = self.base_env()
        d = self._queued("mcp-wrongproducer", producer="arch")
        response = self._call(
            ["--db", self.db_path, "--actor-id", "arch2"],
            "cancel_dispatch",
            {"dispatch_id": d["dispatch_id"], "reason": "not mine"},
            env,
        )
        self.assertTrue(response["result"]["isError"])
        # The queued row is untouched by the refused foreign-producer request.
        with self.store._db.connection() as conn:
            status = conn.execute(
                "select status from dispatch_ledger where dispatch_id = ?", (d["dispatch_id"],)
            ).fetchone()["status"]
        self.assertEqual(status, "queued")

    def test_unknown_dispatch_is_refused(self) -> None:
        env = self.base_env()
        response = self._call(
            ["--db", self.db_path, "--actor-id", "arch"],
            "cancel_dispatch",
            {"dispatch_id": "dispatch_missing", "reason": "x"},
            env,
        )
        self.assertTrue(response["result"]["isError"])

    def test_empty_and_oversized_reason_refused(self) -> None:
        env = self.base_env()
        args = ["--db", self.db_path, "--actor-id", "arch"]
        empty = self._queued("mcp-empty")
        empty_response = self._call(args, "cancel_dispatch", {"dispatch_id": empty["dispatch_id"], "reason": "   "}, env)
        self.assertTrue(empty_response["result"]["isError"])

        big = self._queued("mcp-big")
        big_response = self._call(
            args,
            "cancel_dispatch",
            {"dispatch_id": big["dispatch_id"], "reason": "x" * (CANCELLATION_REASON_MAX + 1)},
            env,
            call_id=4,
        )
        self.assertTrue(big_response["result"]["isError"])

    # --- bounded worker denial ------------------------------------------ #
    def test_worker_cannot_discover_or_call_cancel_dispatch(self) -> None:
        env = self.base_env()
        env["WAKE_POLICY"] = WORKER_DISPATCH_POLICY
        args = ["--db", self.db_path, "--actor-id", "wrk"]
        tools = self._tools(args, env)
        self.assertNotIn("cancel_dispatch", tools)
        self.assertNotIn("dispatch_agent", tools)

        response = self._call(args, "cancel_dispatch", {"dispatch_id": "d", "reason": "x"}, env)
        self.assertTrue(response["result"]["isError"])

    # --- operator mailbox: mailbox-only, no admin mutation surface ------- #
    def test_operator_mailbox_cannot_discover_or_call_cancel_dispatch(self) -> None:
        env = self.base_env()
        env["WAKE_POLICY"] = OPERATOR_MAILBOX_POLICY
        args = ["--db", self.db_path, "--actor-id", HUMAN_ID]
        tools = self._tools(args, env)
        self.assertNotIn("cancel_dispatch", tools)
        self.assertNotIn("dispatch_agent", tools)

        response = self._call(args, "cancel_dispatch", {"dispatch_id": "d", "reason": "x"}, env)
        self.assertTrue(response["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
