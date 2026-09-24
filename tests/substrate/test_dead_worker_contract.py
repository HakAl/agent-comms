"""T10 executable contract: dead-worker terminalization stage 2 invariants.

These tests pin the machine contract, so a later edit that silently drifts
trips a red:

- ``CONTRACT_VERSION`` advanced exactly one (10 -> 11, a SINGLE advance
  covering dispatch result semantics and stage 2, folding the supervisor
  RPE INCLUDED-surface change into the same single advance) and the declared
  digest matches the canonical ``contract_surface_digest()``;
- the v11 DB change itself is additive-only (nullable ``cancelled_at``
  columns and the nullable checked ``result``); the v11-era floor freeze at 1
  ended with the deliberate breaking payload-transport floor advance to 2
  (``user_version > 1`` is exactly what makes prior recency-guard readers
  refuse a payload-capable default ledger);
- the MCP tool set gained exactly ``close_dispatch`` (result-bound dispatch
  closeout) and ``cancel_dispatch`` (no identity/authority/credential
  argument), the mailbox vocabulary gained exactly the terminal ``cancelled``,
  and the dispatch terminal set gained exactly ``cancelled``;
- the worker policy denies ``cancel_dispatch`` at both the MCP allowlist and
  the runtime hook (no worker self-cancel);
- the sanctioned-cancellation cluster projects distinctly and is never folded
  into ordinary outcomes.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import ast
import tempfile
import unittest
from pathlib import Path

from agent_comms import code_identity, schema, supervisor
from agent_comms.db import LEDGER_SCHEMA_VERSION, Database
from agent_comms.dispatch_ledger import (
    CANCELLATION_AUTHORITIES,
    CANCELLATION_CONFIRMED_RESULTS,
    CANCELLATION_ESCALATION_SECONDS,
    CANCELLATION_KEY,
    DISPATCH_TERMINAL_STATUSES,
    SETTLEMENT_FAILURE_REASON,
    SETTLEMENT_KEY,
    SETTLEMENT_TERMINATION_RESULT,
    TERMINATION_NOT_CONFIRMED_PHRASE,
    project_dispatch_transport,
)
from agent_comms.hooks import pre_tool_use
from agent_comms.policies import compile_policy
from agent_comms.store import WORKER_DISPATCH_POLICY

ROOT = Path(__file__).resolve().parents[2]

# The full v11 MCP tool surface: result-bound dispatch closeout
# (``close_dispatch``) plus EXACTLY ``cancel_dispatch`` from stage 2; this
# set is the pin.
EXPECTED_MCP_TOOLS = frozenset(
    {
        "list_agents",
        "list_actors",
        "list_status",
        "send_message",
        "dispatch_agent",
        "cancel_dispatch",
        "list_inbox",
        "read_message",
        "ack_message",
        "close_message",
        "close_dispatch",
        "wait_for_reply",
        "post_status",
        "post_handoff",
        "read_handoff",
        "whoami",
    }
)

def _mcp_functions() -> dict[str, ast.FunctionDef]:
    """Runtime-free enumeration of ``@mcp.tool()``-decorated functions."""
    tree = ast.parse((ROOT / "agent_comms" / "mcp_server.py").read_text())
    functions: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "tool"
                and isinstance(target.value, ast.Name)
                and target.value.id == "mcp"
            ):
                functions[node.name] = node
    return functions


class DeadWorkerContractInvariantsTest(unittest.TestCase):
    def test_contract_version_is_nineteen(self) -> None:
        self.assertEqual(code_identity.CONTRACT_VERSION, 22)
        self.assertEqual(code_identity.LOADED_CONTRACT_VERSION, 22)
        self.assertEqual(code_identity.current_contract_version(), 22)

    def test_contract_surface_digest_matches_declaration(self) -> None:
        self.assertEqual(
            code_identity.contract_surface_digest(),
            code_identity.CONTRACT_SURFACE_DIGEST,
        )

    def test_ledger_schema_floor_advanced_to_three_for_message_payload(self) -> None:
        # Floor 2 was the dispatch payload transport advance. Floor 3 adds the
        # message payload binding; prior readers refuse on ``user_version > 2``
        # rather than returning the marker as an ordinary message body.
        self.assertEqual(LEDGER_SCHEMA_VERSION, 3)

    def test_stage2_migration_is_additive_and_floor_matches_declaration(self) -> None:
        # A fresh init lands the stage-2 columns as nullable additive columns
        # while the on-disk marker is stamped to exactly the declared floor.
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent-comms.sqlite")
            db.init()
            with db.connection() as conn:
                self.assertEqual(
                    int(conn.execute("pragma user_version").fetchone()[0]),
                    LEDGER_SCHEMA_VERSION,
                )
                ledger_cols = {
                    row["name"]: row
                    for row in conn.execute("pragma table_info(dispatch_ledger)")
                }
                recipient_cols = {
                    row["name"]: row
                    for row in conn.execute("pragma table_info(message_recipients)")
                }
            self.assertIn("cancelled_at", ledger_cols)
            self.assertIn("cancelled_at", recipient_cols)
            # Additive means nullable, no NOT NULL retrofit on either machine.
            self.assertEqual(ledger_cols["cancelled_at"]["notnull"], 0)
            self.assertEqual(recipient_cols["cancelled_at"]["notnull"], 0)

    def test_mailbox_status_vocabulary_exact(self) -> None:
        # Stage 2 adds EXACTLY the terminal ``cancelled`` to the transport
        # vocabulary; nothing else changed.
        self.assertEqual(
            schema.STATUSES, {"sent", "read", "acknowledged", "closed", "cancelled"}
        )

    def test_dispatch_terminal_status_set_exact_and_in_lockstep(self) -> None:
        expected = {"closed", "dlq", "spawn_failed_message_landed", "cancelled"}
        self.assertEqual(set(DISPATCH_TERMINAL_STATUSES), expected)
        # The supervisor's duplicate of the terminal set stays in lockstep.
        self.assertEqual(set(supervisor.DISPATCH_TERMINAL_STATUSES), expected)

    def test_mcp_tool_set_exact(self) -> None:
        self.assertEqual(set(_mcp_functions()), set(EXPECTED_MCP_TOOLS))

    def test_cancel_dispatch_has_no_identity_or_authority_argument(self) -> None:
        # The requesting identity and producer authority come exclusively from
        # the scoped ``--actor-id`` process, never from request JSON.
        node = _mcp_functions()["cancel_dispatch"]
        arg_names = [arg.arg for arg in node.args.args]
        self.assertEqual(arg_names, ["dispatch_id", "reason"])
        self.assertEqual(node.args.kwonlyargs, [])
        self.assertIsNone(node.args.vararg)
        self.assertIsNone(node.args.kwarg)

    def test_worker_policy_denies_cancel_dispatch_at_both_layers(self) -> None:
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        self.assertIn("cancel_dispatch", policy.mcp_denied_tools)
        self.assertNotIn("cancel_dispatch", policy.mcp_allowed_tools)
        for name in (
            "cancel_dispatch",
            "mcp__agent_comms__cancel_dispatch",
            "mcp__agent-comms__cancel_dispatch",
        ):
            self.assertIn(name, policy.hook_denied_tools)
            self.assertIn(name, pre_tool_use.MCP_DENIED_TOOLS)

    def test_cancellation_engine_vocabulary_exact(self) -> None:
        self.assertEqual(set(CANCELLATION_AUTHORITIES), {"producer", "admin"})
        self.assertEqual(
            set(CANCELLATION_CONFIRMED_RESULTS),
            {"not_started", "supervised_halt_confirmed", "same_run_exit_confirmed"},
        )
        # The durable escalation deadline is exactly 60 seconds; it escalates,
        # it never weakens single-lineage or releases cap/lineage.
        self.assertEqual(CANCELLATION_ESCALATION_SECONDS, 60.0)
        self.assertEqual(CANCELLATION_KEY, "cancellation")
        self.assertEqual(SETTLEMENT_KEY, "settlement")
        self.assertEqual(
            SETTLEMENT_FAILURE_REASON, "operator_settled_termination_unconfirmed"
        )
        self.assertEqual(SETTLEMENT_TERMINATION_RESULT, "termination_not_confirmed")
        self.assertEqual(
            TERMINATION_NOT_CONFIRMED_PHRASE,
            "ledger released; termination not confirmed",
        )

    def test_sanctioned_cancellation_cluster_projects_distinctly(self) -> None:
        cases = {
            ("cancelled", "cancelled"): "confirmed_cancel",
            ("dlq", "cancelled"): "operator_settled_termination_unconfirmed",
            ("cancelled", "closed"): "confirmed_cancel_transport_closed_first",
        }
        for (dispatch_status, transport_status), outcome in cases.items():
            with self.subTest(pair=(dispatch_status, transport_status)):
                projection = project_dispatch_transport(dispatch_status, transport_status)
                self.assertEqual(projection["outcome"], outcome)
        # An unreachable pair stays a loud ``unknown``: cancelled transport is
        # committed jointly, so a live-transport cancelled ledger is refused
        # rather than folded into an ordinary outcome.
        self.assertEqual(
            project_dispatch_transport("cancelled", "sent")["outcome"], "unknown"
        )


if __name__ == "__main__":
    unittest.main()
